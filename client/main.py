# -*- coding: utf-8 -*-
"""LANPrint 客户端入口。

子命令：
  capture       打印捕获（由端口监视器拉起，PDF 流经 stdin 传入）
  check-server  安装期连通校验（不通则退出码 1，安装器据此中止安装）
  register      注册虚拟打印机（端口 + 打印机，需管理员）
                首次带 --server/--port/--share；已注册过可省略参数，
                自动复用 client.json 里保存的服务端配置完成"重注册"
  unregister    卸载虚拟打印机组件（需管理员）
  agent         常驻代理（开机自启；托盘状态/通知弹窗/离线重发）
  version       显示版本

用法示例：
  lanprint_client.exe check-server --host 192.168.1.20 --port 17878
  lanprint_client.exe register --server 192.168.1.20 --port 17878 \
      --share 我的打印机
  lanprint_client.exe register      # 复用已保存配置一键重注册
  lanprint_client.exe capture
  lanprint_client.exe agent
"""

import logging
import sys
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

from common import config, protocol
from client import notify, setup_printer, uploader

# 修复 exe 后需递增版本号（并与 installer\client.iss 的 MyAppVersion 同步），
# 便于用户通过 version 子命令确认已更新到新构建
CLIENT_VERSION = "1.0.4"
LOG = "lanprint.client"

# capture 拒绝处理的过小数据（端口监视器偶发空任务的防御）
_MIN_PDF_BYTES = 100


def setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler = RotatingFileHandler(Path(log_dir) / "client.log",
                                  maxBytes=5 * 1024 * 1024, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(fmt)
    root.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)


def _client_cfg() -> dict:
    return config.load_config(config.CLIENT_CONFIG, config.CLIENT_DEFAULTS)


def cmd_capture(cfg: dict) -> int:
    """打印捕获主流程：stdin PDF 流 → 上传 → 跟踪 → 通知。"""
    log = logging.getLogger(LOG)
    docname = "--docname" in sys.argv and \
        sys.argv[sys.argv.index("--docname") + 1] or None
    if not docname:
        docname = "打印文档_%s" % time.strftime("%H%M%S")

    data = sys.stdin.buffer.read()
    if len(data) < _MIN_PDF_BYTES:
        # 空流/无效流：端口监视器偶发空任务，静默退出
        log.info("capture: 输入数据过小(%d 字节)，忽略", len(data))
        return 0

    if not cfg.get("server_host"):
        log.error("capture: 客户端尚未注册（client.json 缺失），打印任务丢弃")
        notify.emit_notification(
            cfg["notify_dir"], "LANPrint 客户端尚未配置服务端，本次打印被取消。",
            level="error")
        return 1

    capture_dir = Path(cfg["capture_dir"])
    capture_dir.mkdir(parents=True, exist_ok=True)
    tmp = capture_dir / ("%s.pdf" % uuid.uuid4().hex)
    tmp.write_bytes(data)

    log.info("capture: 收到打印任务 %s (%d 字节)", docname, len(data))
    rc = uploader.send_and_track(tmp, docname, 1, cfg)
    _safe_unlink(tmp)  # send_and_track 内部已移走/删除，兜底清理
    log.info("capture: 流程结束 rc=%d 文档=%s", rc, docname)
    return rc


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def cmd_check_server(host: str, port: int) -> int:
    """安装期连通校验：GET /ping，成功打印服务端信息。"""
    host = (host or "").strip()
    if not host:
        print("[失败] 未指定服务端地址，请使用 --host 参数")
        return 1
    url = "http://%s:%s%s" % (host, port, protocol.API_PING)
    print("正在连接服务端 %s:%s ..." % (host, port))
    try:
        code, data = protocol.http_get_json(url, timeout=10)
    except protocol.TransportError as e:
        print("[失败] 无法连接服务端：%s" % e)
        print("请检查：1) 服务端已启动  2) 防火墙已放行端口 %s  "
              "3) 两台设备在同一局域网" % port)
        return 1
    except Exception as e:  # 兜底：清晰呈现而非裸堆栈
        print("[失败] 检测程序异常：%s" % e)
        return 1
    if code != 200 or not data.get("ok"):
        print("[失败] 服务端响应异常: [%s] %s" % (code, data))
        return 1
    online = "在线" if data.get("printer_online") else "离线/未配置"
    print("[成功] LANPrint 服务端 v%s，共享名: %s（本地打印机: %s）"
          % (data.get("version"), data.get("share_name"), online))
    return 0


def cmd_register(server_host: str, server_port: int, share_name: str) -> int:
    """注册虚拟打印机（需管理员）。"""
    cfg = setup_printer.register_client(server_host, server_port, share_name)
    print("[成功] 虚拟打印机已创建（客户端 v%s）" % CLIENT_VERSION)
    print("  打印机名称: %s" % cfg["printer_display"])
    print("  服务端地址: %s:%s" % (cfg["server_host"], cfg["server_port"]))
    print("在任意应用中选择该打印机即可打印到服务端（请保持后台代理运行）。")
    return 0


def cmd_unregister() -> int:
    setup_printer.unregister_client()
    print("[成功] LANPrint 客户端打印机组件已卸载")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""

    # 轻量自检类命令先于配置加载/日志初始化处理：它们不读 client.json，
    # 也不写日志目录，避免在无 %ProgramData% 写权限的环境（如普通权限
    # 排障、安装器预检）下因初始化崩溃，干扰对连通性的判断。
    if cmd in ("version", "v"):
        print("LANPrint 客户端 v%s" % CLIENT_VERSION)
        return 0
    if cmd == "check-server":
        host, port = _parse_host_port(argv[1:])
        return cmd_check_server(host, port)

    cfg = _client_cfg()
    setup_logging(cfg["log_dir"])

    if cmd == "capture":
        return cmd_capture(cfg)
    if cmd == "agent":
        from client.agent import run_agent
        return run_agent(cfg)
    if cmd == "register":
        return _cmd_register_from_argv(argv[1:])
    if cmd == "unregister":
        return cmd_unregister()
    print(__doc__)
    return 2


def _parse_host_port(args):
    host, port = "", protocol.DEFAULT_PORT
    if "--host" in args:
        host = args[args.index("--host") + 1]
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    return host, int(port)


def _cmd_register_from_argv(args) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="register")
    parser.add_argument("--server", default="", help="服务端 IPv4 地址")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--share", default="", help="服务端共享名")
    ns = parser.parse_args(args)

    # 未显式传参时复用 client.json 中已保存配置，实现升级后无参"一键重注册"
    cfg = _client_cfg()
    server = ns.server or cfg.get("server_host", "")
    port = ns.port or int(cfg.get("server_port", protocol.DEFAULT_PORT))
    share = ns.share or cfg.get("share_name", "")
    if not server or not share:
        print("[失败] 缺少服务端参数。首次注册请使用 --server/--port/--share；"
              "已注册过则直接运行 register（不带参数）即可自动重注册。")
        return 1

    # 先校验连通性，再动系统配置
    if cmd_check_server(server, port) != 0:
        return 1
    try:
        return cmd_register(server, port, share)
    except RuntimeError as e:
        print("[失败] %s" % e)
        logging.getLogger(LOG).exception("register 失败")
        return 1
    except Exception as e:  # 兜底：未知异常也给出清晰提示而非裸堆栈静默退出
        print("[失败] 注册异常: %s" % e)
        logging.getLogger(LOG).exception("register 意外异常")
        return 1


if __name__ == "__main__":
    sys.exit(main())
