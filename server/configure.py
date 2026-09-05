# -*- coding: utf-8 -*-
"""安装期配置向导（仅服务端，控制台交互，由安装器以管理员身份调用）。

流程：枚举本机可用打印机 -> 用户选择"共享打印机" -> 输入对外共享名称 ->
输入监听端口 -> 保存配置 -> 写防火墙规则。
"""

import logging
import subprocess
import sys

from common import config
from common.config import SERVER_DEFAULTS
from server import print_engine

FIREWALL_RULE_NAME = "LANPrint Server"

prompt = print
input_ = input


def ensure_firewall_rule(port: int) -> bool:
    """重建防火墙入站规则（放行 TCP 监听端口）。需要管理员权限。"""
    try:
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule",
             "name=%s" % FIREWALL_RULE_NAME],
            capture_output=True, check=False)
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             "name=%s" % FIREWALL_RULE_NAME, "dir=in", "action=allow",
             "protocol=TCP", "localport=%d" % port],
            capture_output=True, check=False)
        return result.returncode == 0
    except OSError as e:
        logging.getLogger("lanprint.server").warning("netsh 执行失败: %s", e)
        return False


def choose_printer() -> str:
    """编号列出可用打印机，循环直到用户输入有效序号。"""
    while True:
        printers = print_engine.list_local_printers()
        if not printers:
            prompt("未检测到已连接且运行中的打印机，请先连接打印机后重试。")
            prompt("按回车键重新扫描，输入 q 退出...")
            if input_().strip().lower() == "q":
                sys.exit(1)
            continue
        prompt("")
        prompt("请选择要共享的打印机（当前设备已连接并运行中）：")
        for i, name in enumerate(printers, 1):
            prompt("  %d. %s" % (i, name))
        raw = input_("输入序号: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(printers):
            return printers[int(raw) - 1]
        prompt("输入无效，请重新选择。")


def verify_printer(printer_name: str) -> bool:
    """校验打印机当前是否可用。"""
    status = print_engine.get_printer_status(printer_name)
    if not status["online"]:
        prompt("警告: 打印机当前不可用（%s），仍将继续，请稍后确认打印机状态。"
               % (status["error"] or "未知"))
        return False
    return True


def run_configure() -> int:
    """交互式配置向导入口。返回进程退出码。"""
    prompt("=" * 56)
    prompt("  LANPrint 服务端配置向导")
    prompt("=" * 56)
    prompt("本向导将选择要共享的本地打印机并设置监听端口。")

    cfg = config.load_config(config.SERVER_CONFIG, SERVER_DEFAULTS)

    # 1) 选择共享打印机
    printer_name = choose_printer()
    verify_printer(printer_name)
    cfg["printer_name"] = printer_name

    # 2) 对外共享名称
    prompt("")
    default_share = cfg.get("share_name") or "我的打印机"
    share = input_("输入对外共享名称 [默认: %s]: " % default_share).strip()
    cfg["share_name"] = share or default_share

    # 3) 监听端口
    while True:
        raw = input_("输入监听端口 [默认: %d]: "
                     % cfg.get("listen_port", 17878)).strip()
        if not raw:
            break
        if raw.isdigit() and 1 <= int(raw) <= 65535:
            cfg["listen_port"] = int(raw)
            break
        prompt("端口无效，请输入 1-65535 的数字。")

    # 4) 保存配置
    config.save_config(config.SERVER_CONFIG, cfg)
    prompt("")
    prompt("配置已保存: %s" % config.config_path(config.SERVER_CONFIG))

    # 5) 防火墙规则
    if ensure_firewall_rule(cfg["listen_port"]):
        prompt("防火墙入站规则已就绪（TCP %d）" % cfg["listen_port"])
    else:
        prompt("警告: 防火墙规则设置失败，请以管理员身份重新运行向导，")
        prompt("      否则其他设备将无法连接本服务。")

    prompt("")
    prompt("-" * 56)
    prompt("配置摘要:")
    prompt("  共享打印机  : %s" % cfg["share_name"])
    prompt("  本地打印机  : %s" % cfg["printer_name"])
    prompt("  监听地址    : %s:%d" % (cfg["listen_host"], cfg["listen_port"]))
    prompt("-" * 56)
    prompt("请将【服务器IP + 共享名称】告知需要打印的同事，")
    prompt("对方在客户端安装时填入即可。")
    return 0
