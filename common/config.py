# -*- coding: utf-8 -*-
"""配置读写（服务端/客户端共用）。

配置文件位于 %ProgramData%\\LANPrint\\ 下，JSON 格式，UTF-8 编码。
"""

import json
import os
from pathlib import Path

APP_DIR_NAME = "LANPrint"

SERVER_CONFIG = "config.json"    # 服务端配置文件名
CLIENT_CONFIG = "client.json"    # 客户端配置文件名


def app_data_dir() -> Path:
    """返回数据目录（默认 %ProgramData%\\LANPrint，不存在则创建）。

    支持用环境变量 LANPRINT_DATA_DIR 覆盖（用于测试/便携模式）。
    """
    base = os.environ.get("LANPRINT_DATA_DIR") or os.environ.get(
        "PROGRAMDATA") or os.path.expanduser("~")
    p = Path(base) if os.environ.get("LANPRINT_DATA_DIR") \
        else Path(base) / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path(name: str) -> Path:
    """返回指定配置文件的完整路径。"""
    return app_data_dir() / name


def load_config(name: str, defaults: dict) -> dict:
    """加载配置；缺失字段用 defaults 补齐（不自动回写，避免多进程写冲突）。"""
    cfg = dict(defaults)
    path = config_path(name)
    if path.exists():
        try:
            # utf-8-sig 兼容记事本等工具写入的 BOM 头
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, ValueError):
            pass  # 配置损坏时退回默认值
    return cfg


def save_config(name: str, cfg: dict) -> None:
    """原子写入配置（临时文件 + replace，避免写一半损坏）。"""
    path = config_path(name)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


# 服务端默认配置（config.json 缺失字段时补齐）
SERVER_DEFAULTS = {
    "listen_host": "0.0.0.0",
    "listen_port": 17878,
    "printer_name": "",
    "share_name": "我的打印机",
    "max_upload_mb": 200,
    "job_timeout_sec": 300,
    "keep_failed_days": 3,
    "spool_dir": str(app_data_dir() / "spool"),
    "log_dir": str(app_data_dir() / "logs"),
}

# 客户端默认配置（client.json 缺失字段时补齐）
CLIENT_DEFAULTS = {
    "server_host": "",        # 服务端 IPv4 地址（register 时写入）
    "server_port": 17878,     # 服务端 HTTP 端口
    "share_name": "",         # 服务端共享名（register 时写入）
    "printer_display": "",    # 本地虚拟打印机显示名（register 时生成）
    "local_tcp_port": 9100,   # agent 回环监听端口（虚拟打印机输出经此到达）
    "poll_interval_sec": 3,   # 打印结果轮询间隔
    "poll_timeout_sec": 600,  # 轮询超时（服务端队列高峰兜底）
    "capture_dir": str(app_data_dir() / "capture"),
    "offline_dir": str(app_data_dir() / "offline"),
    "notify_dir": str(app_data_dir() / "notify"),
    "log_dir": str(app_data_dir() / "logs"),
}
