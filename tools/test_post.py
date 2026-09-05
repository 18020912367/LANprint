# -*- coding: utf-8 -*-
"""LANPrint 服务端手工测试脚本（纯标准库，无第三方依赖）。

用法：
  生成测试 PDF:
    python tools/test_post.py make-pdf out.pdf --pages 3
  上传并跟踪打印结果:
    python tools/test_post.py send --host 127.0.0.1 --port 17878 ^
        --file out.pdf --copies 1
  连通性测试:
    python tools/test_post.py ping --host 192.168.1.10 --port 17878
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import protocol  # noqa: E402


# ---------------------------------------------------------------------------
# 测试 PDF 生成（手写最小合法 PDF，无需第三方库）
# ---------------------------------------------------------------------------

def make_test_pdf(path: str, pages: int = 1) -> None:
    """生成包含指定页数的测试 PDF（A4 纵向，带页码文本）。"""
    objects = []          # (obj_num, bytes)
    page_obj_nums = []

    # 对象编号分配: 1=Catalog 2=Pages 3=Font, 页面从 4 开始, 每页占 2 个对象
    catalog_num, pages_num, font_num = 1, 2, 3
    next_num = 4
    for i in range(pages):
        page_num, content_num = next_num, next_num + 1
        next_num += 2
        page_obj_nums.append(page_num)
        content = ("BT /F1 36 Tf 72 720 Td (LANPrint Test Page %d/%d) Tj ET"
                   % (i + 1, pages)).encode("ascii")
        objects.append((page_num,
                        b"<</Type/Page/Parent %d 0 R/MediaBox[0 0 612 792]"
                        b"/Contents %d 0 R/Resources<</Font<</F1 %d 0 R>>>>>>"
                        % (pages_num, content_num, font_num)))
        objects.append((content_num,
                        b"<</Length %d>>stream\n%b\nendstream"
                        % (len(content), content)))

    kids = b"[" + b" ".join(b"%d 0 R" % n for n in page_obj_nums) + b"]"
    objects.insert(0, (pages_num, b"<</Type/Pages/Kids %b/Count %d>>"
                       % (kids, pages)))
    objects.insert(0, (font_num, b"<</Type/Font/Subtype/Type1"
                                 b"/BaseFont/Helvetica>>"))
    objects.insert(0, (catalog_num, b"<</Type/Catalog/Pages %d 0 R>>"
                                    % pages_num))
    objects.sort(key=lambda o: o[0])

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num, body in objects:
        offsets[num] = len(out)
        out += b"%d 0 obj\n%b\nendobj\n" % (num, body)

    xref_pos = len(out)
    max_num = max(offsets) + 1
    out += b"xref\n0 %d\n" % max_num
    out += b"0000000000 65535 f \n"
    for n in range(1, max_num):
        out += b"%010d 00000 n \n" % offsets[n]
    out += (b"trailer\n<</Size %d/Root %d 0 R>>\nstartxref\n%d\n%%%%EOF\n"
            % (max_num, catalog_num, xref_pos))

    Path(path).write_bytes(bytes(out))
    print("已生成测试 PDF: %s（%d 页, %d 字节）" % (path, pages, len(out)))


# ---------------------------------------------------------------------------
# 上传与结果跟踪
# ---------------------------------------------------------------------------

def cmd_send(args) -> int:
    base = "http://%s:%d" % (args.host, args.port)
    if not Path(args.file).exists():
        print("文件不存在: %s" % args.file)
        return 2

    try:
        code, resp = protocol.http_post_multipart(
            base + protocol.API_PRINT,
            {"filename": Path(args.file).name, "copies": str(args.copies)},
            "file", args.file)
    except protocol.TransportError as e:
        print("[失败] 无法连接服务器: %s" % e)
        return 3

    print("[上传] HTTP %d -> %s" % (code, json.dumps(resp, ensure_ascii=False)))
    if code != 200 or not resp.get("ok"):
        return 4
    job_id = resp["job_id"]
    print("[接收] 服务器已接收文件，任务号 %s" % job_id)

    # 轮询打印结果
    deadline = time.time() + args.timeout
    last_state = None
    while time.time() < deadline:
        try:
            code, job = protocol.http_get_json(
                "%s%s/%s" % (base, protocol.API_JOB, job_id))
        except protocol.TransportError as e:
            print("[查询] 连接中断: %s" % e)
            return 5
        state = job.get("state")
        if state != last_state:
            print("[%s] 任务 %s 状态: %s"
                  % (time.strftime("%H:%M:%S"), job_id, state))
            last_state = state
        if state in protocol.TERMINAL_STATES:
            if state == protocol.JobState.DONE:
                print("[完成] 打印完成（页数: %s）" % job.get("pages"))
                return 0
            print("[失败] 打印失败: %s" % job.get("error"))
            return 6
        time.sleep(2)
    print("[失败] 等待打印结果超时")
    return 7


def cmd_ping(args) -> int:
    base = "http://%s:%d" % (args.host, args.port)
    try:
        code, resp = protocol.http_get_json(base + protocol.API_PING, 8)
    except protocol.TransportError as e:
        print("[失败] 无法连接服务器 %s: %s" % (base, e))
        return 1
    print("[连通] HTTP %d -> %s" % (code, json.dumps(resp, ensure_ascii=False)))
    return 0 if code == 200 and resp.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="LANPrint 服务端测试工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("make-pdf", help="生成测试 PDF")
    p.add_argument("path")
    p.add_argument("--pages", type=int, default=1)
    p.set_defaults(func=lambda a: (make_test_pdf(a.path, a.pages), 0)[1])

    p = sub.add_parser("ping", help="连通性测试")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=protocol.DEFAULT_PORT)
    p.set_defaults(func=cmd_ping)

    p = sub.add_parser("send", help="上传 PDF 并跟踪打印结果")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=protocol.DEFAULT_PORT)
    p.add_argument("--file", required=True)
    p.add_argument("--copies", type=int, default=1)
    p.add_argument("--timeout", type=int, default=600)
    p.set_defaults(func=cmd_send)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
