# -*- coding: utf-8 -*-
"""客户端阶段软测试（离线为主，全程不出纸、不需要管理员）。

用例：
  1. check-server：无服务端 → 退出码 1
  2. capture：无服务端 → 离线暂存（offline/*.pdf + meta + 通知文件）
  3. check-server：有服务端 → 退出码 0
  4. send_and_track：打印（打印机不存在）→ rc=1 + 通知（已接收/打印失败）
  5. 暂存重发：_resend_worker → meta 清理 + 通知（已重发/打印失败）
  6. notify.drain_notifications：读取并清理通知文件
  7. build_display_name 纯函数

运行：python tools/test_client.py
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

TEST_DIR = ROOT / ".client_test"
PORT = 17879

import test_post  # noqa: E402  （生成测试 PDF）


def setup():
    """准备测试数据目录与客户端配置。"""
    import shutil
    shutil.rmtree(TEST_DIR, ignore_errors=True)
    TEST_DIR.mkdir(parents=True)
    os.environ["LANPRINT_DATA_DIR"] = str(TEST_DIR)

    # 服务端配置（打印机不存在 → 打印必然失败，符合测试预期）
    server_cfg = {
        "listen_port": PORT,
        "printer_name": "NO_SUCH_PRINTER_TEST",
        "share_name": "测试共享打印机",
        "spool_dir": str(TEST_DIR / "spool"),
        "log_dir": str(TEST_DIR / "server_logs"),
    }
    (TEST_DIR / "config.json").write_text(
        json.dumps(server_cfg, ensure_ascii=False), encoding="utf-8")

    # 客户端配置
    client_cfg = {
        "server_host": "127.0.0.1",
        "server_port": PORT,
        "printer_display": "127.0.0.1_测试共享打印机",
        "capture_dir": str(TEST_DIR / "capture"),
        "offline_dir": str(TEST_DIR / "offline"),
        "notify_dir": str(TEST_DIR / "notify"),
        "log_dir": str(TEST_DIR / "client_logs"),
    }
    (TEST_DIR / "client.json").write_text(
        json.dumps(client_cfg, ensure_ascii=False), encoding="utf-8")
    return client_cfg


def run_client(*args, stdin_data=None):
    """以子进程方式运行客户端（继承测试环境变量）。"""
    return subprocess.run(
        [sys.executable, "-m", "client.main", *args],
        input=stdin_data, capture_output=True, cwd=str(ROOT), timeout=120)


def server_job_states():
    states = {}
    for meta in (TEST_DIR / "spool" / "jobs").glob("*.json"):
        job = json.loads(meta.read_text(encoding="utf-8"))
        states[job["job_id"]] = job["state"]
    return states


def wait_server_alive(timeout=20):
    from common import protocol
    url = "http://127.0.0.1:%s%s" % (PORT, protocol.API_PING)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, data = protocol.http_get_json(url, timeout=3)
            if code == 200 and data.get("ok"):
                return True
        except protocol.TransportError:
            pass
        time.sleep(0.5)
    return False


def notify_messages():
    out = []
    for path in (TEST_DIR / "notify").glob("*.json"):
        out.append(json.loads(path.read_text(encoding="utf-8"))["message"])
    return out


def cleanup_server(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    from client import uploader
    from client.agent import _resend_worker
    from client.setup_printer import build_display_name
    from client import notify

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(" [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))

    cfg = setup()

    # 准备测试 PDF（1 页）
    pdf_path = TEST_DIR / "test_doc.pdf"
    test_post.make_test_pdf(str(pdf_path), pages=1)
    pdf_bytes = pdf_path.read_bytes()

    # ---- 1) check-server 无服务端 ----
    proc = run_client("check-server", "--host", "127.0.0.1", "--port",
                      str(PORT))
    check("check-server 离线退出码=1", proc.returncode == 1,
          "rc=%d" % proc.returncode)

    # ---- 2) capture 离线暂存 ----
    proc = run_client("capture", "--docname", "软测试文档",
                      stdin_data=pdf_bytes)
    stashed = list((TEST_DIR / "offline").glob("*.pdf"))
    metas = list((TEST_DIR / "offline").glob("*.json"))
    check("capture 离线退出码=2", proc.returncode == 2,
          "rc=%d" % proc.returncode)
    check("capture 离线暂存文件", len(stashed) == 1 and len(metas) == 1,
          "pdf=%d meta=%d" % (len(stashed), len(metas)))
    check("capture 暂存通知", any("暂存" in m for m in notify_messages()),
          str(notify_messages()))
    meta = json.loads(metas[0].read_text(encoding="utf-8")) if metas else {}
    check("暂存 meta 内容", meta.get("docname") == "软测试文档"
          and meta.get("copies") == 1, str(meta.get("docname")))

    # ---- 3) 启动服务端 → check-server 成功 ----
    server = subprocess.Popen(
        [sys.executable, "-m", "server.main", "run"], cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ})
    try:
        alive = wait_server_alive()
        check("服务端启动", alive)
        proc = run_client("check-server", "--host", "127.0.0.1", "--port",
                          str(PORT))
        check("check-server 在线退出码=0", proc.returncode == 0,
              "rc=%d out=%s" % (proc.returncode,
                                proc.stdout.decode("utf-8", "replace")[:60]))

        # ---- 4) send_and_track（服务端在线，打印机不存在 → failed）----
        doc2 = TEST_DIR / "direct.pdf"
        test_post.make_test_pdf(str(doc2), pages=1)
        rc = uploader.send_and_track(doc2, "直连文档", 1, cfg)
        states = server_job_states()
        check("send_and_track rc=1(打印失败)", rc == 1, "rc=%d" % rc)
        check("服务端任务状态 failed", any(s == "failed"
                                          for s in states.values()),
              str(states))
        check("通知序列（已接收+打印失败）",
              any("已发送" in m for m in notify_messages())
              and any("打印失败" in m for m in notify_messages()),
              "%d 条通知" % len(notify_messages()))

        # ---- 5) 暂存重发 ----
        rc_count = _resend_worker(cfg)
        check("暂存重发处理 1 条", rc_count == 1, "处理=%d" % rc_count)
        check("重发后 offline 清空", not list((TEST_DIR / "offline")
                                             .glob("*")),
              "剩余=%d" % len(list((TEST_DIR / "offline").glob("*"))))
        check("重发通知（已重发+失败终态）",
              any("已重发" in m for m in notify_messages())
              and any("打印失败" in m for m in notify_messages()),
              "")

        # ---- 6) drain 通知 ----
        drained = []
        n = notify.drain_notifications(cfg["notify_dir"],
                                       lambda p: drained.append(p["message"]))
        check("drain 清理通知文件", n == len(drained)
              and not list((TEST_DIR / "notify").glob("*.json")),
              "drain=%d" % n)
    finally:
        cleanup_server(server)

    # ---- 7) 显示名 ----
    check("build_display_name",
          build_display_name("192.168.1.20", "我的打印机")
          == "192.168.1.20_我的打印机")

    print()
    failed = [r for r in results if not r[1]]
    print("==== 客户端软测试: %d/%d 通过 ===="
          % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
