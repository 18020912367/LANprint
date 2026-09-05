# -*- coding: utf-8 -*-
"""打印队列工作线程（仅服务端）。

单线程 FIFO：从队列逐个取任务 -> 渲染写入打印机 -> 轮询 Windows 打印作业
状态至终态 -> 收尾（成功删源文件 / 失败保留并记录原因）。
"""

import logging
import os
import queue
import threading
import time

import win32print

from common.protocol import JobState
from server import print_engine

log = logging.getLogger("lanprint.server")

# Windows 打印作业状态位（JOB_STATUS_*）
JOB_STATUS_DELETING = 0x4
JOB_STATUS_ERROR = 0x2
JOB_STATUS_OFFLINE = 0x20
JOB_STATUS_PAPEROUT = 0x40
JOB_STATUS_BLOCKED_DEVQ = 0x200
JOB_STATUS_USER_INTERVENTION = 0x400
JOB_STATUS_PRINTED = 0x80
JOB_STATUS_COMPLETE = 0x1000

JOB_POLL_INTERVAL = 2.0        # 打印作业状态轮询间隔（秒）


def _job_status_error(status: int):
    """把作业状态位翻译为可读错误；正常状态返回 None。"""
    if status & JOB_STATUS_BLOCKED_DEVQ:
        return "打印队列阻塞（设备队列被阻止）"
    if status & JOB_STATUS_USER_INTERVENTION:
        return "打印机需要用户干预"
    if status & JOB_STATUS_PAPEROUT:
        return "打印机缺纸"
    if status & JOB_STATUS_ERROR:
        return "打印机错误"
    if status & JOB_STATUS_OFFLINE:
        return "打印机脱机"
    return None


def wait_job_status(printer_name: str, win_job_id: int, timeout_sec: float):
    """轮询 Windows 打印作业直至完成/取消/超时。

    返回 (ok: bool, error: str|None)。
    判定逻辑：
      - 作业从队列中消失：若此前出现过 DELETING 视为被取消（failed），
        否则视为打印完成（done，最常见路径）；
      - 状态位出现 PRINTED/COMPLETE：直接判定完成；
      - 出现错误状态位：记录原因并继续观察（可能暂时缺纸），超时兜底；
      - 超时：failed。
    """
    handle = win32print.OpenPrinter(printer_name)
    try:
        deadline = time.time() + max(30, float(timeout_sec))
        last_error = None
        saw_deleting = False
        while time.time() < deadline:
            try:
                jobs = win32print.EnumJobs(handle, 0, 1000, 1)
            except Exception:
                jobs = ()
            status = None
            for job in jobs:
                if job.get("JobId") == win_job_id:
                    status = job.get("Status", 0) or 0
                    break
            if status is None:
                # 作业已从队列消失
                if saw_deleting:
                    return False, "打印任务被取消"
                return True, None
            if status & JOB_STATUS_DELETING:
                saw_deleting = True
            if status & (JOB_STATUS_PRINTED | JOB_STATUS_COMPLETE):
                return True, None
            err = _job_status_error(status)
            if err:
                last_error = err
            time.sleep(JOB_POLL_INTERVAL)
        msg = "打印状态等待超时（%d 秒）" % int(timeout_sec)
        if last_error:
            msg += "，最后状态: " + last_error
        return False, msg
    finally:
        win32print.ClosePrinter(handle)


class QueueWorker(threading.Thread):
    """串行打印队列线程（daemon）。"""

    def __init__(self, cfg: dict, job_store, printer_name: str):
        super().__init__(name="LANPrintQueueWorker", daemon=True)
        self.cfg = cfg
        self.store = job_store
        self.printer_name = printer_name
        self.job_queue = queue.Queue()
        self._stop_event = threading.Event()
        self._last_cleanup = 0.0

    def submit(self, job_id: str) -> None:
        """把任务加入打印队列（api 接收完成后调用）。"""
        self.job_queue.put(job_id)

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("打印队列线程已启动，目标打印机: %s", self.printer_name)
        while not self._stop_event.is_set():
            try:
                job_id = self.job_queue.get(timeout=1.0)
            except queue.Empty:
                self._periodic_cleanup()
                continue
            try:
                self._process(job_id)
            except Exception:
                # _process 内部已兜底，这里防线程意外退出
                log.exception("队列处理出现未捕获异常: job=%s", job_id)
            self._periodic_cleanup()
        log.info("打印队列线程已停止")

    # -- 内部逻辑 ----------------------------------------------------------

    def _periodic_cleanup(self) -> None:
        """每 10 分钟清理一次过期任务。"""
        now = time.time()
        if now - self._last_cleanup < 600:
            return
        self._last_cleanup = now
        try:
            self.store.cleanup_expired(self.cfg.get("keep_failed_days", 3))
        except Exception:
            log.exception("过期任务清理失败")

    def _process(self, job_id: str) -> None:
        meta = self.store.get_job(job_id)
        if meta is None or meta.get("state") in (JobState.DONE, JobState.FAILED):
            return  # 任务已被处理过（如重启恢复后重复出队）

        src = meta.get("file")
        if not src or not os.path.exists(src):
            self.store.set_state(job_id, JobState.FAILED, error="源文件丢失")
            return

        self.store.set_state(job_id, JobState.PRINTING)
        try:
            win_job_id, pages = print_engine.print_pdf(
                src, self.printer_name, copies=meta.get("copies", 1),
                doc_name=meta.get("filename") or job_id)
        except Exception as e:
            log.exception("任务 %s 写入打印队列失败", job_id)
            self.store.set_state(job_id, JobState.FAILED,
                                 error="写入打印机队列失败: %s" % e)
            return

        self.store.set_state(job_id, JobState.PRINTING,
                             win_job_id=win_job_id, pages=pages)
        ok, error = wait_job_status(self.printer_name, win_job_id,
                                    self.cfg.get("job_timeout_sec", 300))
        if ok:
            self.store.set_state(job_id, JobState.DONE)
            try:
                os.remove(src)  # 打印完成后删除源文件
                log.info("任务 %s 打印完成，已删除源文件", job_id)
            except OSError:
                log.warning("任务 %s 源文件删除失败: %s", job_id, src)
        else:
            self.store.set_state(job_id, JobState.FAILED, error=error)
            log.error("任务 %s 打印失败: %s（源文件保留）", job_id, error)
