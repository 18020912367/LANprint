# -*- coding: utf-8 -*-
"""本地虚拟打印机注册/卸载（需管理员权限）。

原理：
    1. 使用系统**内置**的 Standard TCP/IP 端口监视器（tcpmon.dll）在
       本机注册一个回环端口（127.0.0.1:9100），端口名为 LANPRINT：
       - 这是 Windows 自带的端口监视器，因此与任何打印驱动兼容；
    2. 用系统自带 "Microsoft Print To PDF"（v4 驱动）创建虚拟打印机，
       显示名为 `<服务端IP>_<共享名>`，端口绑定到该回环端口；
    3. 用户向该打印机打印 → v4 驱动渲染出 PDF → spooler 把 PDF 字节流
       经回环 TCP 送到 127.0.0.1:9100；
    4. 客户端常驻代理（agent）监听 127.0.0.1:9100，收到连接即取走
       整段 PDF 流 → 上传到 LANPrint 服务端。

方案演进（重要）：
    - Windows 11(26200) 实测系统内置 Virtual Port Monitor 已不再支持
      传统 Command 重定向端口，注册后导致 EnumPorts 整体报错；
    - 之后尝试第三方 RedMon 端口监视器，但被事件 242 明确拒绝：
      v4 驱动（Microsoft Print To PDF 即为 v4）**只能**与内置端口
      监视器组合，不能绑定第三方监视器；
    - v3 驱动虽可绑定 RedMon，但系统没有可再分发的 v3 PDF 驱动；
    - 结论：只有"内置监视器 + 回环 TCP 端口 + agent 接收"这一条路
      在 26200 上可行且不依赖任何第三方 DLL。

所有写 HKLM / 调用 Add-PrinterPort 的操作都需要管理员权限
（安装器提权运行；register 命令在管理员上下文执行）。
"""

import contextlib
import ctypes
from ctypes import wintypes
import logging
import os
import subprocess
import sys
import time
import winreg
from pathlib import Path

import win32print

from common import config

log = logging.getLogger("lanprint.client")

# ---- 端口 / 驱动常量 ------------------------------------------------------
PORT_NAME = "LANPRINT"                # Standard TCP/IP 端口名（本机回环）
PORT_HOST = "127.0.0.1"               # tcpmon Raw 协议目标地址
PORT_TCP_PORT = 9100                  # tcpmon Raw 协议默认目标端口
PRINTER_DRIVER = "Microsoft Print To PDF"  # 系统内部驱动名（To 大写）

# 64 位注册表视图访问标志：32 位 Python 默认走 WOW6432Node，必须显式指定
if hasattr(winreg, "KEY_WOW64_64KEY"):
    _SAM_64 = winreg.KEY_WOW64_64KEY
else:
    _SAM_64 = 0x0100

# 历史版本遗留键（1.0.0：Windows 自带 Virtual Port Monitor，Win11 不可用）
_LEGACY_MON_KEY = (r"SYSTEM\CurrentControlSet\Control\Print\Monitors"
                   r"\Virtual Port Monitor")
_LEGACY_PORT_KEY = _LEGACY_MON_KEY + r"\Ports\LANPRT:"
# 历史版本遗留键（1.0.1/1.0.2：第三方 RedMon 监视器，v4 驱动拒绝绑定）
_REDMON_MON_KEY = (r"SYSTEM\CurrentControlSet\Control\Print\Monitors"
                   r"\LANPRINT")
_REDMON_PORT_KEY = _REDMON_MON_KEY + r"\Ports\LANPRT:"
_REDMON_DLL = "redmon64lanprint.dll"  # 曾部署到 System32 的 RedMon DLL


def build_display_name(server_host: str, share_name: str) -> str:
    """虚拟打印机显示名：<服务端IP>_<共享名>。"""
    return "%s_%s" % (server_host, share_name)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _run(cmd, timeout=120):
    """执行命令并记录输出；失败抛 RuntimeError。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError("命令执行失败: %s" % cmd) from e
    if proc.returncode != 0:
        raise RuntimeError("命令失败(%d): %s\n%s"
                           % (proc.returncode, cmd, proc.stderr.strip()))
    return proc


@contextlib.contextmanager
def _open_key(root, sub_key, access, create=False):
    """按需创建并打开注册表键；自动关闭。"""
    key = None
    try:
        if create:
            key = winreg.CreateKeyEx(root, sub_key, 0, access)
        else:
            key = winreg.OpenKey(root, sub_key, 0, access)
        yield key
    finally:
        if key is not None:
            winreg.CloseKey(key)


def _delete_hklm_key(sub_key: str) -> None:
    """删除 HKLM 下键（64 位视图；键不存在时静默）。"""
    delete_ex = getattr(winreg, "DeleteKeyEx", None)
    try:
        if delete_ex is not None:
            delete_ex(winreg.HKEY_LOCAL_MACHINE, sub_key, _SAM_64, 0)
        else:
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, sub_key)
    except OSError:
        pass


@contextlib.contextmanager
def _disable_wow64_fs_redirect():
    """32 位进程写 System32 前临时关闭文件系统重定向。"""
    if sys.maxsize > 2 ** 32:
        yield
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p()
    if kernel32.Wow64DisableWow64FsRedirection(ctypes.byref(handle)):
        try:
            yield
        finally:
            kernel32.Wow64RevertWow64FsRedirection(handle)
    else:
        yield


def _enum_port_names() -> set:
    """当前 spooler 可枚举到的端口名集合（异常时返回空集）。"""
    try:
        return {p["Name"] for p in win32print.EnumPorts(None, 1)}
    except Exception as e:
        log.warning("EnumPorts 失败: %s", e)
        return set()


def wait_port_online(timeout_sec: float = 30.0) -> bool:
    """轮询直到 LANPRINT 回环端口可被枚举。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if PORT_NAME in _enum_port_names():
            return True
        time.sleep(1.0)
    return False


def ensure_tcp_port() -> None:
    """确保 Standard TCP/IP 回环端口 LANPRINT(127.0.0.1:9100) 存在。"""
    if PORT_NAME in _enum_port_names():
        return
    log.info("创建回环端口 %s -> %s:%s", PORT_NAME, PORT_HOST,
             PORT_TCP_PORT)
    _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
          "Add-PrinterPort -Name '%s' -PrinterHostAddress '%s'"
          % (PORT_NAME, PORT_HOST)])
    if not wait_port_online():
        raise RuntimeError("回环端口 %s 创建后仍不可用" % PORT_NAME)


def remove_tcp_port() -> None:
    """删除 LANPRINT 回环端口（须先解除打印机绑定）。"""
    if PORT_NAME not in _enum_port_names():
        return
    _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
          "Remove-PrinterPort -Name '%s'" % PORT_NAME])
    if PORT_NAME in _enum_port_names():
        log.warning("回环端口 %s 删除失败（可能仍被占用）", PORT_NAME)


# ---- 历史方案残留清理 ------------------------------------------------------
def cleanup_legacy() -> None:
    """清理 1.0.x 历史方案残留，保证升级后干净。

    1.0.0 在系统内置 Virtual Port Monitor 下注册过 LANPRT:，该写法在
    Windows 11(26200) 会让 EnumPorts 整体报错；1.0.1/1.0.2 迁移到
    RedMon 后在 Monitors\\LANPRINT 注册监视器并部署 DLL 到 System32。
    新方案不再使用二者，注册/卸载时主动清除遗留。
    """
    _delete_hklm_key(_LEGACY_PORT_KEY)
    _delete_hklm_key(_REDMON_PORT_KEY)
    _delete_hklm_key(_REDMON_MON_KEY)

    # 删除随旧包部署到 System32 的 RedMon DLL（spooler 占用时容忍，
    # 重启后自然失效，不影响新方案）
    dll = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" \
        / _REDMON_DLL
    try:
        with _disable_wow64_fs_redirect():
            dll.unlink(missing_ok=True)
    except OSError as e:
        log.warning("删除遗留 DLL %s 失败（可忽略）: %s", dll, e)


# ---- 创建虚拟打印机（ctypes 直调 winspool AddPrinterW） -------------------
# pywin32 的 AddPrinter 转换器要求 PRINTER_INFO_2 以 dict 传入，但它的结构解析
# 存在"关键字最多 21 个"与"pServerName/pDevMode 强制必填"的矛盾（实测 23 字段
# 报 at most 21、21 字段报 missing pDevMode），无法直接使用。因此按 MSDN 布局
# 用 ctypes 定义 PRINTER_INFO_2W 直调 winspool.drv AddPrinterW，字段完整可控
# （pServerName=NULL 表示创建到本机）。
class _PrinterInfo2W(ctypes.Structure):
    _fields_ = [
        ("pServerName", wintypes.LPWSTR),
        ("pPrinterName", wintypes.LPWSTR),
        ("pShareName", wintypes.LPWSTR),
        ("pPortName", wintypes.LPWSTR),
        ("pDriverName", wintypes.LPWSTR),
        ("pComment", wintypes.LPWSTR),
        ("pLocation", wintypes.LPWSTR),
        ("pDevMode", ctypes.c_void_p),
        ("pSepFile", wintypes.LPWSTR),
        ("pPrintProcessor", wintypes.LPWSTR),
        ("pDatatype", wintypes.LPWSTR),
        ("pParameters", wintypes.LPWSTR),
        ("pSecurityDescriptor", ctypes.c_void_p),
        ("Attributes", wintypes.DWORD),
        ("Priority", wintypes.DWORD),
        ("DefaultPriority", wintypes.DWORD),
        ("StartTime", wintypes.DWORD),
        ("UntilTime", wintypes.DWORD),
        ("Status", wintypes.DWORD),
        ("cJobs", wintypes.DWORD),
        ("AveragePPM", wintypes.DWORD),
        ("TotalPages", wintypes.DWORD),
        ("PagesPrinted", wintypes.DWORD),
    ]


_WINSPOOL = ctypes.WinDLL("winspool.drv", use_last_error=True)
_AddPrinterW = _WINSPOOL.AddPrinterW
_AddPrinterW.restype = wintypes.HANDLE
_AddPrinterW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                         ctypes.POINTER(_PrinterInfo2W)]

# 同名打印机被删除后由后台异步释放，期间立即重建会返回该错误码（需短暂重试）
_ERROR_PRINTER_ALREADY_EXISTS = 1802
_CREATE_RETRY_TIMES = 5
_CREATE_RETRY_INTERVAL_SEC = 0.6


class PrinterOpError(RuntimeError):
    """打印机创建失败，winerror 为对应 Win32 错误码。"""

    def __init__(self, message: str, winerror: int = 0):
        super().__init__(message)
        self.winerror = winerror


def _add_printer(display_name: str, port_name: str,
                 driver_name: str) -> wintypes.HANDLE:
    """调用 AddPrinterW 创建本地虚拟打印机，成功返回句柄（失败抛异常）。"""
    info = _PrinterInfo2W()
    info.pPrinterName = display_name
    info.pPortName = port_name
    info.pDriverName = driver_name
    info.pPrintProcessor = "winprint"
    info.pDatatype = "RAW"
    handle = _AddPrinterW(None, 2, ctypes.byref(info))
    if not handle:
        code = ctypes.get_last_error()
        raise PrinterOpError("AddPrinterW 失败（系统错误码 %d）" % code, code)
    return handle


def create_printer(display_name: str) -> None:
    """创建虚拟打印机（驱动 Microsoft Print To PDF，端口 LANPRINT）。

    同名旧打印机可能仍在后台异步释放，遇 ERROR_PRINTER_ALREADY_EXISTS
    时短暂重试，避免覆盖安装/重注册时报错（1802）。
    """
    _ensure_driver()
    for attempt in range(_CREATE_RETRY_TIMES):
        try:
            handle = _add_printer(display_name, PORT_NAME, PRINTER_DRIVER)
        except PrinterOpError as e:
            if (e.winerror != _ERROR_PRINTER_ALREADY_EXISTS
                    or attempt == _CREATE_RETRY_TIMES - 1):
                raise RuntimeError(
                    "创建打印机 [%s] 失败: %s" % (display_name, e)) from e
            time.sleep(_CREATE_RETRY_INTERVAL_SEC)
            continue
        except Exception as e:  # 其他错误立即失败，不做无谓重试
            raise RuntimeError(
                "创建打印机 [%s] 失败: %s" % (display_name, e)) from e
        win32print.ClosePrinter(handle)
        log.info("虚拟打印机已创建: %s -> 端口 %s", display_name, PORT_NAME)
        return


def _ensure_driver() -> None:
    """确保 Microsoft Print To PDF 驱动已安装（缺失则通过 PowerShell 装）。"""
    # EnumPrinterDrivers 返回 (dict,) 元组，取其中的 Name 做不区分大小写比对
    installed = {str(d.get("Name", "")).lower()
                 for d in win32print.EnumPrinterDrivers(None, None, 1)}
    if PRINTER_DRIVER.lower() in installed:
        return
    _run(["powershell.exe", "-NoProfile", "-NonInteractive",
          "-Command", 'Add-PrinterDriver -Name "%s"' % PRINTER_DRIVER],
         timeout=180)


def printer_exists(display_name: str) -> bool:
    # Level 1 返回 tuple 无字段名，须用 Level 2（dict，含 pPrinterName）
    flags = (win32print.PRINTER_ENUM_LOCAL
             | win32print.PRINTER_ENUM_CONNECTIONS)
    for p in win32print.EnumPrinters(flags, None, 2):
        if p["pPrinterName"] == display_name:
            return True
    return False


def delete_printer(display_name: str) -> None:
    """删除虚拟打印机（先清空作业队列）。"""
    try:
        handle = win32print.OpenPrinter(
            display_name,
            {"DesiredAccess": win32print.PRINTER_ALL_ACCESS})
    except Exception:
        return
    try:
        try:
            jobs = win32print.EnumJobs(handle, 0, 500, 1)
            for job in jobs:
                win32print.SetJob(handle, job["JobId"], 0, None,
                                  win32print.JOB_CONTROL_DELETE)
        except Exception:
            pass
        win32print.DeletePrinter(handle)
        log.info("虚拟打印机已删除: %s", display_name)
    finally:
        win32print.ClosePrinter(handle)


# ---- 注册 / 卸载编排 ------------------------------------------------------
def register_client(server_host: str, server_port: int,
                    share_name: str) -> dict:
    """注册全流程编排（register 子命令），返回写入的 client 配置。

    注意：本机回环接收端口固定为 127.0.0.1:9100（tcpmon Raw 协议），
    agent 需随后启动监听该端口接收打印产生的 PDF 流。
    """
    if not is_admin():
        raise RuntimeError("需要管理员权限运行")

    display_name = build_display_name(server_host, share_name)

    # 0) 清理历史 Virtual Port Monitor / RedMon 残留
    cleanup_legacy()

    # 1) 创建回环 TCP 端口（内置监视器，天然兼容 v4 驱动）并等待就绪
    ensure_tcp_port()

    # 2) 打印机（先清理同名旧打印机保证幂等）
    if printer_exists(display_name):
        delete_printer(display_name)
    create_printer(display_name)

    # 3) 保存客户端配置（含 share_name / 回环接收信息，供重注册与 agent 使用）
    cfg = config.load_config(config.CLIENT_CONFIG, config.CLIENT_DEFAULTS)
    cfg.update({
        "server_host": server_host,
        "server_port": int(server_port),
        "share_name": share_name,
        "printer_display": display_name,
        "local_tcp_port": PORT_TCP_PORT,
    })
    config.save_config(config.CLIENT_CONFIG, cfg)
    return cfg


def unregister_client() -> None:
    """卸载流程编排（unregister 子命令）。"""
    if not is_admin():
        raise RuntimeError("需要管理员权限运行")

    cfg = config.load_config(config.CLIENT_CONFIG, config.CLIENT_DEFAULTS)
    display_name = cfg.get("printer_display") or ""
    if display_name and printer_exists(display_name):
        delete_printer(display_name)

    # 删除回环端口，并清理历史方案残留（含 RedMon 监视器键与 DLL）
    remove_tcp_port()
    cleanup_legacy()
    log.info("LANPrint 客户端打印机组件已卸载")
