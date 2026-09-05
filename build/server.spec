# -*- mode: python ; coding: utf-8 -*-
"""LANPrint 服务端打包配置（PyInstaller 单文件控制台 exe）。

用法（在项目根目录执行）:
    python -m PyInstaller --clean --noconfirm build/server.spec

产物:
    dist/lanprint_server.exe

说明:
    - console=True 保留控制台，供 `configure` 配置向导交互与排障；
      开机自启时由 start_server_hidden.vbs 以隐藏窗口方式调用 `run`。
    - pypdfium2 的 pdfium.dll 通过 collect_dynamic_libs 一并打入。
"""
import os

from PyInstaller.utils.hooks import collect_dynamic_libs

project_root = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(project_root, "server", "main.py")],
    pathex=[project_root],
    binaries=collect_dynamic_libs("pypdfium2"),
    datas=[],
    # win32timezone: pywin32 EnumJobs/GetPrinter 运行时延迟导入，必须显式声明
    hiddenimports=["win32timezone"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "unittest", "pydoc_data",
        "setuptools", "pip", "wheel", "pkg_resources",
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
    name="lanprint_server",
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
