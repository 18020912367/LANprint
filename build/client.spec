# -*- mode: python ; coding: utf-8 -*-
"""LANPrint 客户端打包配置（PyInstaller 单文件控制台 exe）。

用法（在项目根目录执行）:
    python -m PyInstaller --clean --noconfirm build/client.spec

产物:
    dist/lanprint_client.exe

说明:
    - console=True 保留控制台：check-server / register 需要输出与排障；
      capture 由 spooler 在 Session 0 拉起（窗口不可见）无闪窗问题；
      agent 常驻后台由安装器生成的 vbs 以隐藏窗口方式启动。
    - win32timezone: pywin32 运行时延迟导入，必须显式声明。
    - 采用 v1.0.3 后：打印捕获改用系统内置 Standard TCP/IP 回环端口
      （127.0.0.1:9100）+ agent 监听接收，不再随包内置 RedMon DLL。
"""
import os

project_root = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(project_root, "client", "main.py")],
    pathex=[project_root],
    binaries=[],
    datas=[],
    hiddenimports=["win32timezone"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "unittest", "pydoc_data",
        "setuptools", "pip", "wheel", "pkg_resources",
        "pypdfium2",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="lanprint_client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
