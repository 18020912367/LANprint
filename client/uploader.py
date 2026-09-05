# -*- coding: utf-8 -*-
"""客户端上传与打印结果跟踪。

职责：
    - 把 capture 捕获到的 PDF 通过 HTTP 上传到服务端（POST /print）；
    - 轮询任务状态直到终态（done/failed）；
    - 服务端不可达时把文件暂存到 offline 目录，等待 agent 重发。

所有对外函数都不弹窗，通知统一经 notify.emit_notification 转交
用户会话的 agent 展示（capture 进程处于 SYSTEM 会话，无 UI 能力）。
"""

import json
import shutil
import time
import uuid
from pathlib import Path

from common import protocol
from client import notify


def upload(path, docname: str, copies: int, cfg: dict) -> str:
    """上传打印文件，返回 job_id。

    失败抛 protocol.TransportError（连接失败/HTTP 非 200）或
    protocol.PayloadTooLarge（超过服务端上限）。
    """
    url = "http://%s:%s%s" % (cfg["server_host"], cfg["server_port"],
                              protocol.API_PRINT)
    code, data = protocol.http_post_multipart(
        url,
        fields={"filename": docname, "copies": str(copies)},
        file_field="file",
        file_path=str(path),
        timeout=120,
    )
    if code != 200 or not data.get("ok"):
        raise protocol.TransportError(
            "服务端拒绝: [%s] %s" % (code, data.get("error", "未知错误")))
    return data["job_id"]


def poll_result(job_id: str, cfg: dict, timeout_sec: float = None) -> dict:
    """轮询任务状态直到终态，返回最终任务字典；超时抛 TransportError。"""
    url = "http://%s:%s%s/%s" % (cfg["server_host"], cfg["server_port"],
                                 protocol.API_JOB, job_id)
    interval = max(1, int(cfg.get("poll_interval_sec", 3)))
    deadline = time.time() + (timeout_sec or cfg.get("poll_timeout_sec", 600))
    started = time.time()
    # 任务记录短暂不可见的宽限窗口：服务端刚写入 meta 可能被
    # 杀软/索引等瞬时占用导致读取失败，宽限期内 404 不算丢失
    missing_grace = 30.0
    last_state = ""
    while time.time() < deadline:
        code, data = protocol.http_get_json(url, timeout=15)
        if code == 200 and data.get("ok"):
            # 服务端 /job 响应为扁平结构（state 在顶层），兼容嵌套 job
            job = data.get("job") if isinstance(data.get("job"), dict) else data
            state = job.get("state", "")
            if state != last_state:
                last_state = state
            if state in protocol.TERMINAL_STATES:
                return job
        elif code == 404:
            if time.time() - started >= missing_grace:
                raise protocol.TransportError("服务端丢失了该任务记录")
        time.sleep(interval)
    raise protocol.TransportError("等待打印结果超时")


def stash_offline(path, meta: dict, cfg: dict) -> Path:
    """把未发送的文件暂存到 offline 目录（伴随 meta.json 供重发）。"""
    d = Path(cfg["offline_dir"])
    d.mkdir(parents=True, exist_ok=True)
    name = "%s.pdf" % uuid.uuid4().hex
    dest = d / name
    shutil.move(str(path), str(dest))
    meta = dict(meta)
    meta.update({"file": str(dest), "stashed_at": time.time()})
    meta_path = dest.with_suffix(".json")
    tmp = meta_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os_replace(tmp, meta_path)
    return dest


def send_and_track(path, docname: str, copies: int, cfg: dict,
                   notify_dir: str = None) -> int:
    """完整流程：上传 → 通知已接收 → 轮询 → 通知结果。

    返回退出码：0 成功打印；1 打印失败；2 已暂存待重发；3 结果未知。
    """
    notify_dir = notify_dir or cfg.get("notify_dir", "")
    where = "打印机 [%s]" % (cfg.get("printer_display") or "LANPrint")

    # 1) 上传
    try:
        job_id = upload(path, docname, copies, cfg)
    except protocol.PayloadTooLarge:
        notify.emit_notification(
            notify_dir, "%s：文件过大，超过服务端上限，未发送。\n文档: %s"
            % (where, docname), level="error")
        _safe_unlink(path)
        return 1
    except protocol.TransportError as e:
        # 服务端不可达 → 暂存，等 agent 重发
        try:
            stash_offline(path, {"docname": docname, "copies": copies},
                          cfg)
            notify.emit_notification(
                notify_dir,
                "%s：无法连接服务端（%s）。\n文档已暂存，联网后自动重发。\n文档: %s"
                % (where, e, docname), level="warning")
        except OSError:
            notify.emit_notification(
                notify_dir,
                "%s：无法连接服务端，且本地暂存失败，文档未打印。\n文档: %s"
                % (where, docname), level="error")
        return 2

    # 2) 已接收
    notify.emit_notification(
        notify_dir, "%s：文档已发送到服务端，排队打印中。\n文档: %s"
        % (where, docname))

    # 3) 轮询结果
    try:
        job = poll_result(job_id, cfg)
    except protocol.TransportError as e:
        notify.emit_notification(
            notify_dir, "%s：打印结果跟踪中断（%s）。\n文档: %s (任务 %s)"
            % (where, e, docname, job_id), level="warning")
        return 3

    # 4) 终态
    if job.get("state") == protocol.JobState.DONE:
        pages = job.get("pages") or "?"
        notify.emit_notification(
            notify_dir, "%s：打印完成（共 %s 页）。\n文档: %s"
            % (where, pages, docname))
        return 0
    notify.emit_notification(
        notify_dir, "%s：打印失败。\n原因: %s\n文档: %s"
        % (where, job.get("error") or "未知原因", docname), level="error")
    return 1


def _safe_unlink(path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


# shutil 同名屏蔽保护（os.replace 别名，便于单测打桩）
def os_replace(src, dst):
    import os
    os.replace(src, dst)
