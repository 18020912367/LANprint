# -*- coding: utf-8 -*-
"""开机自启动配置的检查、修复与诊断。

自启动链路（用户登录后由系统触发）：
    HKCU\\...\\Run\\LANPrintClient
      → wscript.exe <安装目录>\\start_agent_hidden.vbs（隐藏窗口运行）
      → lanprint_client.exe agent

设计要点：
    - Run 项位于 HKCU，普通权限即可写回 → 可自动修复（部分安全软件
      会清理登录启动项，agent 启动时自检发现被清理即重建并通知用户）；
    - vbs 脚本位于 Program Files，写回需管理员权限 → 仅检测并报告，
      提示以管理员身份重跑安装包修复；
    - Windows 10/11 对登录启动项默认可能延迟数分钟执行，属正常现象，
      诊断文案中予以说明，避免被误判为"自启动失败"。
"""

import sys
from pathlib import Path

import win32api
import win32con

_RUN_SUBKEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
_RUN_VALUE = 'LANPrintClient'
_VBS_NAME = 'start_agent_hidden.vbs'
DELAY_HINT = '登录后 Windows 可能延迟 1~5 分钟启动，属正常现象'


def vbs_path() -> Path:
    """自启动脚本路径（与主程序同目录；PyInstaller 打包后即为安装目录）。"""
    return Path(sys.executable).resolve().parent / _VBS_NAME


def _expected_run_value() -> str:
    """Run 项应有的值（与 installer/client.iss 写入的内容保持一致）。"""
    return 'wscript.exe "%s"' % vbs_path()


def read_run_value() -> str:
    """读取自启动注册表值；缺失或不可读时返回空串。"""
    try:
        key = win32api.RegOpenKeyEx(
            win32con.HKEY_CURRENT_USER, _RUN_SUBKEY, 0,
            win32con.KEY_QUERY_VALUE)
        try:
            value, _type = win32api.RegQueryValueEx(key, _RUN_VALUE)
        finally:
            win32api.RegCloseKey(key)
        return value or ''
    except win32api.error:
        return ''


def _write_run_value(data: str) -> bool:
    """写回 Run 项（HKCU 用户键，普通权限可写）。"""
    try:
        key = win32api.RegOpenKeyEx(
            win32con.HKEY_CURRENT_USER, _RUN_SUBKEY, 0,
            win32con.KEY_SET_VALUE)
        try:
            win32api.RegSetValueEx(
                key, _RUN_VALUE, 0, win32con.REG_SZ, data)
        finally:
            win32api.RegCloseKey(key)
        return True
    except win32api.error:
        return False


def check_vbs() -> str:
    """检查启动脚本是否存在且内容正确；返回问题描述，正常返回空串。"""
    path = vbs_path()
    if not path.is_file():
        return '启动脚本缺失（%s）' % path
    try:
        content = path.read_text(encoding='gbk', errors='replace')
    except OSError:
        return '启动脚本不可读（%s）' % path
    # 仅校验关键字段，避免与安装器生成逻辑强耦合
    if 'lanprint_client.exe' not in content or ' agent' not in content:
        return '启动脚本内容异常（%s）' % path
    return ''


def check_and_repair() -> tuple:
    """自检并修复自启动配置，返回 (已修复项, 未修复项) 两组描述列表。

    - Run 注册表项缺失/被改写：自动写回（普通权限可完成）；
    - vbs 脚本缺失/损坏：需管理员权限，仅报告并建议重跑安装包；
    - 两项均正常时返回 ([], [])，调用方无需打扰用户。
    """
    fixed, broken = [], []
    expected = _expected_run_value()
    current = read_run_value()
    if current != expected:
        if _write_run_value(expected):
            fixed.append('自启动注册表项被%s，已重建'
                         % ('篡改' if current else '清理'))
        else:
            broken.append('自启动注册表项写回失败（权限不足）')
    vbs_problem = check_vbs()
    if vbs_problem:
        broken.append('%s；请以管理员身份重新运行安装包修复' % vbs_problem)
    return fixed, broken
