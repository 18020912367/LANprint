# -*- coding: utf-8 -*-
"""结果通知（弹框提示）。

通知链路：
    capture 进程（由打印 spooler 以 SYSTEM 身份拉起，处于 Session 0，
    无法向用户显示任何窗口）→ 写通知文件到 notify_dir；
    agent 常驻代理（用户登录会话）→ 监听 notify_dir → 弹 Toast
    （失败降级为 MessageBox）。

因此本模块两侧各取所需：
    emit_notification()      capture 侧：写通知文件（原子写入）
    drain_notifications()    agent 侧：读取并清理通知文件
    toast() / message_box()  agent 侧：真正弹窗
"""

import ctypes
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

# 通知级别 → MessageBox 图标
_ICON_INFO = 0x40
_ICON_WARNING = 0x30
_ICON_ERROR = 0x10

_MB_TOPMOST = 0x40000
_MB_SETFOREGROUND = 0x10000

# 陈旧通知最长保留时间（agent 长期未运行时积压的通知，超过则丢弃）
_STALE_SECONDS = 24 * 3600

# PowerShell 的 AppUserModelID（借 PowerShell 应用身份弹 Toast，免注册）
_POWERSHELL_AUMID = (r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
                     r"\WindowsPowerShell\v1.0\powershell.exe")


def emit_notification(notify_dir: str, message: str, title: str = "LANPrint",
                      level: str = "info") -> None:
    """写入一条通知文件（capture 侧，SYSTEM 会话安全）。

    失败只静默放弃（通知属于尽力而为，绝不能影响打印主流程）。
    """
    try:
        d = Path(notify_dir)
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "title": title,
            "message": message,
            "level": level,
            "ts": time.time(),
        }
        path = d / ("%s.json" % uuid.uuid4().hex)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def drain_notifications(notify_dir: str, callback) -> int:
    """读取并清理所有通知文件，逐条回调 callback(payload)；返回条数。"""
    d = Path(notify_dir)
    if not d.is_dir():
        return 0
    count = 0
    now = time.time()
    for path in list(d.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        try:
            path.unlink()
        except OSError:
            pass
        if not isinstance(payload, dict) or not payload.get("message"):
            continue
        if now - payload.get("ts", 0) > _STALE_SECONDS:
            continue  # 陈旧通知直接丢弃
        callback(payload)
        count += 1
    return count


def message_box(title: str, message: str, level: str = "info") -> None:
    """传统弹窗（阻塞，最前显示）。"""
    icon = {"warning": _ICON_WARNING, "error": _ICON_ERROR}.get(
        level, _ICON_INFO)
    ctypes.windll.user32.MessageBoxW(
        0, message, title, icon | _MB_TOPMOST | _MB_SETFOREGROUND)


def toast(title: str, message: str, level: str = "info",
          timeout_sec: float = 15.0) -> bool:
    """Windows Toast 通知（非阻塞显示，自动消失）。

    通过 powershell.exe 调 WinRT Toast API；返回是否成功。
    """
    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager,"
        " Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument,"
        " Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]"
        " > $null;"
        "$x=[Windows.UI.Notifications.ToastNotificationManager]"
        "::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]"
        "::ToastText02);"
        "$t=$x.GetElementsByTagName('text');"
        "$t.Item(0).AppendChild($x.CreateTextNode('%s')) > $null;"
        "$t.Item(1).AppendChild($x.CreateTextNode('%s')) > $null;"
        "$n=New-Object Windows.UI.Notifications.ToastNotification($x);"
        "[Windows.UI.Notifications.ToastNotificationManager]"
        "::CreateToastNotifier('%s').Show($n)"
        % (_xml_escape(title), _xml_escape(message), _POWERSHELL_AUMID))
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-WindowStyle", "Hidden", "-Command", ps_script],
            capture_output=True, timeout=timeout_sec)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def notify_user(title: str, message: str, level: str = "info") -> None:
    """给用户弹通知：优先 Toast，失败降级 MessageBox。"""
    if not toast(title, message, level):
        message_box(title, message, level)


def _xml_escape(text: str) -> str:
    """转义 Toast XML 文本与 PowerShell 单引号字符串。"""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("'", "''"))
