# -*- coding: utf-8 -*-
"""LANPrint 服务端入口。

子命令：
  run            启动后台打印服务（默认；由开机自启调用）
  configure      安装期配置向导（选打印机/共享名/端口/防火墙）
  list-printers  枚举本机可用打印机
  probe-print    软打印探测（零页作业即止，不出纸）
  version        显示版本
"""

import ctypes
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from common import config
from common.config import SERVER_DEFAULTS
from common.protocol import SERVER_NAME, SERVER_VERSION
from server import configure

log = logging.getLogger("lanprint.server")


def setup_logging(log_dir: str) -> None:
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("lanprint")
    root.setLevel(logging.INFO)
    handler = RotatingFileHandler(log_dir_path / "server.log",
                                  maxBytes=5 * 1024 * 1024, backupCount=5,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(handler)


def acquire_single_instance() -> bool:
    """命名互斥体防止服务被重复启动。"""
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\LANPrintServer")
    return ctypes.windll.kernel32.GetLastError() != 183  # 183=已存在


def run_server() -> int:
    """启动服务：加载配置 -> 恢复遗留任务 -> 队列线程 -> HTTP 常驻。"""
    if not acquire_single_instance():
        log.error("LANPrint 服务端已在运行，本次启动被拒绝")
        return 1

    cfg = config.load_config(config.SERVER_CONFIG, SERVER_DEFAULTS)
    if not cfg.get("printer_name"):
        log.error("尚未配置共享打印机，请先运行配置向导: "
                  "lanprint_server.exe configure")
        return 2

    setup_logging(cfg["log_dir"])

    from server.job_store import JobStore
    from server.queue_worker import QueueWorker
    from server.api import LanPrintHTTPServer

    store = JobStore(cfg["spool_dir"])
    worker = QueueWorker(cfg, store, cfg["printer_name"])

    # 重启恢复：遗留的 queued/printing 且源文件仍存在的任务重新入队
    from common.protocol import JobState
    for meta in store.all_jobs():
        if meta.get("state") in (JobState.QUEUED, JobState.PRINTING):
            if meta.get("file") and Path(meta["file"]).exists():
                if meta["state"] == JobState.PRINTING:
                    store.set_state(meta["job_id"], JobState.QUEUED)
                worker.submit(meta["job_id"])
                log.info("重启恢复任务: %s", meta["job_id"])
            else:
                store.set_state(meta["job_id"], JobState.FAILED,
                                error="服务重启时源文件丢失")

    worker.start()

    address = (cfg["listen_host"], int(cfg["listen_port"]))
    httpd = LanPrintHTTPServer(address, cfg, store, worker, log)
    log.info("%s 服务端 v%s 已启动: http://%s:%d | 共享名=%s | 本地打印机=%s",
             SERVER_NAME, SERVER_VERSION, cfg["listen_host"],
             cfg["listen_port"], cfg["share_name"], cfg["printer_name"])
    try:
        httpd.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        worker.stop()
        log.info("服务端已停止")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = (argv[0] if argv else "run").lower().lstrip("-")

    if cmd in ("run", "start", "service"):
        return run_server()
    if cmd == "configure":
        return configure.run_configure()
    if cmd == "list-printers":
        from server import print_engine
        names = print_engine.list_local_printers()
        print("\n".join(names) if names else "（未检测到可用打印机）")
        return 0
    if cmd == "probe-print":
        # 软探测：提交零页作业后立即中止，不出纸
        from server import print_engine
        name = argv[1] if len(argv) > 1 else None
        if not name:
            cfg = config.load_config(config.SERVER_CONFIG, SERVER_DEFAULTS)
            name = cfg.get("printer_name")
        if not name:
            print("未指定打印机且尚未配置。用法: probe-print [打印机名]")
            return 1
        result = print_engine.probe_print(name)
        if result["ok"]:
            print("探测成功: 打印机 [%s] 可接收打印作业 (job_id=%s)，"
                  "本次探测未产生纸张输出。" % (name, result["job_id"]))
            return 0
        print("探测失败: %s" % result["error"])
        return 1
    if cmd in ("version", "v"):
        print("%s server v%s" % (SERVER_NAME, SERVER_VERSION))
        return 0
    print("用法: lanprint_server[.exe] "
          "[run|configure|list-printers|probe-print|version]")
    return 0 if cmd in ("help", "h", "?") else 1


if __name__ == "__main__":
    sys.exit(main())
