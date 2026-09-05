# -*- coding: utf-8 -*-
"""HTTP 服务与路由（仅服务端）。

提供四个接口：
  GET  /ping          连通性握手
  GET  /status        队列概况
  POST /print         上传打印文件（multipart/form-data）
  GET  /job/{id}      查询任务结果
"""

import json
import logging
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import protocol
from common.protocol import (API_JOB, API_PING, API_PRINT, API_STATUS,
                             JobState, PayloadTooLarge)
from server import print_engine

log = logging.getLogger("lanprint.server")

# 打印机名称中不允许出现的字符（防止写入任务元数据时出问题）
_FILENAME_BAD_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')

JOB_ROUTE_RE = re.compile(r"^%s/([0-9a-fA-F]+)$" % re.escape(API_JOB))


def safe_filename(name: str) -> str:
    """清理客户端传来的文件名，去除路径分隔符与非法字符。"""
    name = (name or "").strip()
    cleaned = _FILENAME_BAD_RE.sub("_", name).strip(". ")
    return cleaned[:200] or "未命名文档"


class LanPrintHTTPServer(ThreadingHTTPServer):
    """多线程 HTTP 服务，持有运行期上下文（配置/存储/队列）。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, cfg: dict, job_store, worker, log_=None):
        self.cfg = cfg
        self.store = job_store
        self.worker = worker
        self.app_log = log_ or log
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "LANPrint/1.0"
    protocol_version = "HTTP/1.1"      # 显式带 Content-Length，支持连接复用
    timeout = 300                       # 收发超时，防止慢连接长期占用线程

    # -- 基础设施 ----------------------------------------------------------

    @property
    def api(self):
        return self.server

    def log_message(self, fmt, *args):  # 重定向默认 stderr 日志
        self.api.app_log.info("HTTP %s - %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, obj: dict, close: bool = False) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # -- 路由 --------------------------------------------------------------

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == API_PING:
                self.handle_ping()
            elif path == API_STATUS:
                self.handle_status()
            else:
                m = JOB_ROUTE_RE.match(path)
                if m:
                    self.handle_job(m.group(1))
                else:
                    self._send_json(404, {"ok": False, "error": "未知路径"})
        except OSError:
            # 连接中断/超时：直接关闭
            self.close_connection = True
        except Exception as e:
            log.exception("处理 GET %s 失败", path)
            self._send_json(500, {"ok": False, "error": str(e)}, close=True)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == API_PRINT:
                self.handle_print()
            else:
                self._send_json(404, {"ok": False, "error": "未知路径"})
        except OSError:
            # 连接中断/超时：直接关闭
            self.close_connection = True
        except Exception as e:
            log.exception("处理 POST %s 失败", path)
            self._send_json(500, {"ok": False, "error": str(e)}, close=True)

    # -- 各接口实现 ---------------------------------------------------------

    def handle_ping(self):
        status = print_engine.get_printer_status(self.api.cfg["printer_name"])
        self._send_json(200, {
            "ok": True,
            "server": protocol.SERVER_NAME,
            "version": protocol.SERVER_VERSION,
            "share_name": self.api.cfg.get("share_name", ""),
            "printer_online": status["online"],
        })

    def handle_status(self):
        status = print_engine.get_printer_status(self.api.cfg["printer_name"])
        waiting = len(self.api.store.pending_jobs()) + self.api.worker.job_queue.qsize()
        printing = sum(
            1 for m in self.api.store.all_jobs()
            if m.get("state") == JobState.PRINTING)
        self._send_json(200, {
            "ok": True,
            "waiting": waiting,
            "printing": printing,
            "printer_online": status["online"],
        })

    def handle_print(self):
        # 1) 请求头校验
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type.lower():
            self._send_json(400, {"ok": False,
                                  "error": "需要 multipart/form-data 请求"})
            return
        try:
            content_length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            content_length = 0
        max_bytes = int(self.api.cfg.get("max_upload_mb", 200)) * 1024 * 1024
        if content_length > max_bytes:
            # 丢弃请求体后规范返回 413，客户端可正常读到错误响应；
            # 超过硬上限（1GB）时提前断开，避免长时间占用连接
            if content_length <= 1024 * 1024 * 1024:
                remaining = content_length
                try:
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except OSError:
                    self.close_connection = True
                    return
            self._send_json(413, {
                "ok": False,
                "error": "文件超过大小上限（%d MB）" % (max_bytes // 1024 // 1024),
            }, close=(content_length > 1024 * 1024 * 1024))
            return

        # 2) 流式接收落盘
        try:
            fields, files = protocol.recv_multipart_to_file(
                self.rfile, content_type, self.api.store.spool_dir,
                max_bytes, content_length)
        except PayloadTooLarge:
            self._send_json(413, {
                "ok": False,
                "error": "文件超过大小上限（%d MB）" % (max_bytes // 1024 // 1024),
            }, close=True)
            return
        except (ValueError, OSError) as e:
            self._send_json(400, {"ok": False,
                                  "error": "接收失败: %s" % e}, close=True)
            return

        file_item = next((f for f in files if f[0] == "file"), None) \
            or (files[0] if files else None)
        if file_item is None:
            self._send_json(400, {"ok": False, "error": "缺少 file 文件字段"})
            return
        _, _, tmp_path = file_item

        # 3) 登记任务（FIFO 由 JobStore.created_at 保证）
        job_id = protocol.new_job_id()
        final_path = tmp_path.with_name("%s.pdf" % job_id)
        try:
            tmp_path = Path(tmp_path)
            tmp_path.replace(final_path)
        except OSError:
            final_path = tmp_path

        filename = safe_filename(fields.get("filename")
                                 or Path(file_item[1]).name)
        try:
            copies = int(fields.get("copies") or 1)
        except ValueError:
            copies = 1
        copies = max(1, min(copies, 99))

        self.api.store.add_job(job_id, filename, copies, final_path)
        self.api.worker.submit(job_id)
        log.info("接收打印任务: job=%s 文件=%s 副本=%d 大小=%d 字节",
                 job_id, filename, copies, content_length)
        self._send_json(200, {"ok": True, "job_id": job_id,
                              "state": JobState.QUEUED})

    def handle_job(self, job_id: str):
        meta = self.api.store.get_job(job_id)
        if meta is None:
            self._send_json(404, {"ok": False, "error": "任务不存在"})
            return
        self._send_json(200, {
            "ok": True,
            "job_id": meta["job_id"],
            "state": meta["state"],
            "pages": meta.get("pages"),
            "error": meta.get("error"),
            "created_at": meta.get("created_at"),
            "finished_at": meta.get("finished_at"),
        })
