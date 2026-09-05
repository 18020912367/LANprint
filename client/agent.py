# -*- coding: utf-8 -*-
"""客户端常驻代理（开机自启、后台运行）。

职责：
    1. 托盘图标状态灯：绿=服务端在线、灰=离线、黄=有暂存任务待重发；
       悬停显示服务器地址与队列概况，右键菜单可手动重发/退出；
    2. 消费 notify 目录中的通知文件 → 以用户会话身份弹窗/Toast
       （capture 进程在 SYSTEM 会话，无法显示 UI）；
    3. 服务端离线期间的暂存任务（offline 目录）重发；
    4. 监听 127.0.0.1:9100，接收虚拟打印机（内置 TCP/IP 回环端口）送来的
       PDF 流并上传（v4 驱动只允许内置端口监视器，见 setup_printer 说明）。
"""

import ctypes
import io
import json
import logging
import os
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

import win32con
import win32gui
from PIL import Image, ImageDraw

from common import protocol
from client import notify, uploader

log = logging.getLogger("lanprint.client")

PING_INTERVAL_SEC = 10      # 服务端在线探测周期
NOTIFY_POLL_SEC = 2         # 通知文件扫描周期
RETRY_THROTTLE_SEC = 2      # 暂存重发间隔

REDIRECT_HOST = "127.0.0.1"  # 回环接收地址（仅本机可达）
_MIN_PDF_BYTES = 100         # 过小数据视为无效作业（防御）

# 托盘状态索引
ST_ONLINE, ST_OFFLINE, ST_STASHED = 0, 1, 2

WM_TRAY = win32con.WM_APP + 1
WM_STATE = win32con.WM_APP + 2     # 主循环 → UI 线程：更新状态
CMD_RESEND = 1001
CMD_QUIT = 1002

_STATE_COLORS = {
    ST_ONLINE: (46, 204, 113, 255),     # 绿
    ST_OFFLINE: (149, 165, 166, 255),   # 灰
    ST_STASHED: (241, 196, 15, 255),    # 黄
}

_TOOLTIP = {
    ST_ONLINE: "LANPrint：服务端在线",
    ST_OFFLINE: "LANPrint：服务端离线，打印任务将自动暂存",
    ST_STASHED: "LANPrint：有暂存打印任务，等待重发",
}


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_load_image = _user32.LoadImageW
_load_image.restype = ctypes.c_void_p
_load_image.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                        ctypes.c_int, ctypes.c_int, ctypes.c_uint]

_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x00000010


def _make_icon_ico(color) -> bytes:
    """PIL 生成三色圆点图标字节（无外部资源文件依赖）。"""
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 28, 28), fill=color, outline=(45, 45, 45, 255),
                 width=2)
    bio = io.BytesIO()
    img.save(bio, "ICO")
    return bio.getvalue()


def _hicon_from_ico(data: bytes) -> int:
    """ICO 字节 → HICON 句柄。

    部分 pywin32 构建未导出 win32gui.CreateIconFromResourceEx，改用
    user32.LoadImageW（需文件路径，先落临时文件，随即删除）。
    """
    fd, path = tempfile.mkstemp(suffix=".ico")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        handle = _load_image(None, path, _IMAGE_ICON, 32, 32,
                             _LR_LOADFROMFILE)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


class TrayIcon:
    """托盘图标（独立线程跑消息循环）。"""

    def __init__(self):
        ico_bytes = [_make_icon_ico(_STATE_COLORS[i])
                     for i in (ST_ONLINE, ST_OFFLINE, ST_STASHED)]
        self._icons = [_hicon_from_ico(data) for data in ico_bytes]
        self._hwnd = None
        self._added = False
        self._state = ST_OFFLINE
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._thread.start()
        self._ready.wait(5)

    # ---- UI 线程 ----
    def _run(self):
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32gui.GetModuleHandle(None)
        wc.lpszClassName = "LANPrintAgentTray"
        wc.lpfnWndProc = self._wnd_proc
        class_atom = win32gui.RegisterClass(wc)
        self._hwnd = win32gui.CreateWindowEx(
            0, class_atom, "LANPrintAgent", 0, 0, 0, 0, 0,
            0, 0, wc.hInstance, None)
        self._update_tray()
        self._ready.set()
        win32gui.PumpMessages()

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam == win32con.WM_RBUTTONUP:
                self._show_menu(hwnd)
            elif lparam == win32con.WM_LBUTTONDBLCLK:
                self._request_resend()
        elif msg == WM_STATE:
            self._state = wparam
            self._update_tray()
        elif msg == win32con.WM_COMMAND:
            if wparam == CMD_RESEND:
                self._request_resend()
            elif wparam == CMD_QUIT:
                win32gui.PostQuitMessage(0)
        elif msg == win32con.WM_DESTROY:
            self._remove_tray()
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _update_tray(self):
        tip = _TOOLTIP[self._state]
        nid = (self._hwnd, 0,
               win32gui.NIF_MESSAGE | win32gui.NIF_ICON | win32gui.NIF_TIP,
               WM_TRAY, self._icons[self._state], tip)
        action = win32gui.NIM_MODIFY if self._added else win32gui.NIM_ADD
        win32gui.Shell_NotifyIcon(action, nid)
        self._added = True

    def _show_menu(self, hwnd):
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, CMD_RESEND,
                            "立即重发暂存任务")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, CMD_QUIT, "退出")
        win32gui.SetForegroundWindow(hwnd)
        pos = win32gui.GetCursorPos()
        win32gui.TrackPopupMenu(menu, win32con.TPM_RIGHTBUTTON,
                                pos[0], pos[1], 0, hwnd, None)
        win32gui.DestroyMenu(menu)

    def _request_resend(self):
        # 在后台线程处理，避免阻塞 UI 线程
        threading.Thread(target=_resend_worker, daemon=True).start()

    # ---- 跨线程 ----
    def set_state(self, state: int) -> None:
        if self._hwnd:
            win32gui.PostMessage(self._hwnd, WM_STATE, state, 0)

    def _remove_tray(self):
        if self._hwnd:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self._hwnd, 0))


class Agent:
    """常驻主循环：ping 探测 + 通知消费 + 暂存重发。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.online = False
        self.stashed = 0
        self._stop = threading.Event()

    def _tray_state(self) -> int:
        if self.stashed:
            return ST_STASHED
        return ST_ONLINE if self.online else ST_OFFLINE

    def _ping(self) -> bool:
        url = "http://%s:%s%s" % (self.cfg["server_host"],
                                  self.cfg["server_port"],
                                  protocol.API_PING)
        try:
            code, data = protocol.http_get_json(url, timeout=8)
            return code == 200 and bool(data.get("ok"))
        except protocol.TransportError:
            return False

    def loop(self, tray: TrayIcon = None) -> None:
        last_ping = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last_ping >= PING_INTERVAL_SEC:
                last_ping = now
                was_online = self.online
                self.online = self._ping()
                if self.online and not was_online:
                    # 刚恢复在线 → 重发暂存任务
                    self.stashed = _resend_worker(self.cfg)
                if not self.online:
                    self.stashed = _count_stashed(self.cfg)
                if tray:
                    tray.set_state(self._tray_state())
            # 消费通知文件（capture 转交的用户通知）
            notify.drain_notifications(
                self.cfg["notify_dir"], self._on_notification)
            self._stop.wait(NOTIFY_POLL_SEC)

    @staticmethod
    def _on_notification(payload: dict) -> None:
        notify.notify_user(payload.get("title", "LANPrint"),
                           payload["message"],
                           payload.get("level", "info"))

    def stop(self) -> None:
        self._stop.set()


class RedirectListener:
    """监听回环 TCP，接收虚拟打印机输出的 PDF 流并上传。

    打印作业经 Standard TCP/IP 端口（LANPRINT -> 127.0.0.1:9100）
    以一次连接一个作业的方式送达；每个连接读完（EOF）即是一份 PDF。
    """

    def __init__(self, cfg: dict, stop_event: threading.Event):
        self.cfg = cfg
        self.port = int(cfg.get("local_tcp_port", 9100))
        self._stop = stop_event
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)

    def _bind_or_retry(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        while not self._stop.is_set():
            try:
                sock.bind((REDIRECT_HOST, self.port))
                sock.listen(8)
                sock.settimeout(1.0)
                log.info("redirect: 已监听 %s:%s", REDIRECT_HOST, self.port)
                return sock
            except OSError as e:
                log.error("redirect: 监听 %s:%s 失败（%s），5 秒后重试",
                          REDIRECT_HOST, self.port, e)
                self._stop.wait(5)
        return sock

    def _serve(self) -> None:
        sock = self._bind_or_retry()
        self._ready.set()
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()
        try:
            sock.close()
        except OSError:
            pass

    def _handle(self, conn) -> None:
        chunks = []
        total = 0
        try:
            with conn:
                conn.settimeout(180)
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                    total += len(data)
        except OSError as e:
            log.warning("redirect: 读取打印流中断: %s", e)
        if total < _MIN_PDF_BYTES:
            log.info("redirect: 收到的数据过小(%d 字节)，忽略", total)
            return
        self._process(b"".join(chunks), total)

    def _process(self, pdf: bytes, total: int) -> None:
        capture = Path(self.cfg["capture_dir"])
        capture.mkdir(parents=True, exist_ok=True)
        docname = "打印文档_%s" % time.strftime("%H%M%S")
        tmp = capture / ("%s.pdf" % uuid.uuid4().hex)
        tmp.write_bytes(pdf)
        log.info("redirect: 收到打印任务 %s (%d 字节)", docname, total)
        rc = uploader.send_and_track(tmp, docname, 1, self.cfg)
        try:
            tmp.unlink()  # send_and_track 成功后源文件不再需要
        except OSError:
            pass
        log.info("redirect: 打印任务处理结束 rc=%d", rc)


def _count_stashed(cfg: dict) -> int:
    return len(list(Path(cfg["offline_dir"]).glob("*.json")))


def _resend_worker(cfg: dict) -> int:
    """重发所有暂存任务，返回处理条数（托盘菜单线程/主循环调用）。"""
    offline = Path(cfg["offline_dir"])
    if not offline.is_dir():
        return 0
    processed = 0
    for meta_path in sorted(offline.glob("*.json")):
        if _retry_one(cfg, meta_path):
            processed += 1
        time.sleep(RETRY_THROTTLE_SEC)
    return processed


def _retry_one(cfg: dict, meta_path: Path) -> bool:
    """重发单个暂存任务；返回是否处理完毕（成功或终态失败）。"""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        file_path = Path(meta["file"])
    except (OSError, ValueError, KeyError):
        try:
            meta_path.unlink()
        except OSError:
            pass
        return False
    if not file_path.is_file():
        try:
            meta_path.unlink()
        except OSError:
            pass
        return False

    docname = meta.get("docname", "暂存文档")
    copies = int(meta.get("copies", 1))
    try:
        job_id = uploader.upload(file_path, docname, copies, cfg)
    except protocol.PayloadTooLarge:
        notify.emit_notification(
            cfg["notify_dir"], "文件超过服务端上限，已放弃重发。\n文档: %s"
            % docname, level="error")
        _remove(file_path, meta_path)
        return True
    except protocol.TransportError:
        return False  # 仍不可达，保留待下次

    notify.emit_notification(
        cfg["notify_dir"], "打印机 [%s]：暂存文档已重发，排队打印中。\n文档: %s"
        % (cfg.get("printer_display") or "LANPrint", docname))
    try:
        job = uploader.poll_result(job_id, cfg)
    except protocol.TransportError:
        return False

    if job.get("state") == protocol.JobState.DONE:
        notify.emit_notification(
            cfg["notify_dir"], "打印机 [%s]：暂存文档打印完成（共 %s 页）。\n文档: %s"
            % (cfg.get("printer_display") or "LANPrint",
               job.get("pages") or "?", docname))
    else:
        notify.emit_notification(
            cfg["notify_dir"], "打印机 [%s]：暂存文档打印失败。\n原因: %s\n文档: %s"
            % (cfg.get("printer_display") or "LANPrint",
               job.get("error") or "未知原因", docname), level="error")
    _remove(file_path, meta_path)
    return True


def _remove(file_path: Path, meta_path: Path) -> None:
    for path in (file_path, meta_path):
        try:
            path.unlink()
        except OSError:
            pass


def run_agent(cfg: dict) -> int:
    """agent 入口：单实例锁 → 托盘 → 主循环。"""
    if not cfg.get("server_host"):
        log.error("agent: 客户端尚未注册，退出")
        return 1
    if not _acquire_lock():
        log.warning("agent: 已有实例在运行，退出")
        return 0

    Path(cfg["offline_dir"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["notify_dir"]).mkdir(parents=True, exist_ok=True)

    agent = Agent(cfg)
    tray = None
    try:
        tray = TrayIcon()
    except Exception as e:
        # 托盘失败不阻断后台职责
        log.warning("agent: 托盘初始化失败（%s），以无图标模式运行", e)
    log.info("agent: 已启动（服务端 %s:%s）",
             cfg["server_host"], cfg["server_port"])
    try:
        redirect = RedirectListener(cfg, agent._stop)
        redirect.start()
        agent.loop(tray)
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()
        log.info("agent: 已退出")
    return 0


def _acquire_lock() -> bool:
    """单实例锁（每登录会话一个 agent）。"""
    import ctypes
    handle = ctypes.windll.kernel32.CreateMutexW(None, False,
                                                 "Local\\LANPrintAgent")
    return bool(handle) and ctypes.windll.kernel32.GetLastError() != 183
