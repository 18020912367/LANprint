# -*- coding: utf-8 -*-
"""multipart 解析器离线单元测试（不依赖网络/打印机）。"""

import io
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import protocol  # noqa: E402
from common.protocol import recv_multipart_to_file, parse_multipart_boundary  # noqa: E402


def build_body(fields: dict, file_name: str, file_bytes: bytes, chunks=None):
    """构造与 http_post_multipart 完全一致的请求体（可按块喂入模拟网络）。"""
    boundary = "----LANPrint" + uuid.uuid4().hex
    b = boundary.encode("ascii")
    head = bytearray()
    for k, v in fields.items():
        head += b"--" + b
        head += ('\r\nContent-Disposition: form-data; name="%s"\r\n\r\n'
                 % k).encode("utf-8")
        head += str(v).encode("utf-8") + b"\r\n"
    head += b"--" + b
    head += ('\r\nContent-Disposition: form-data; name="file"; filename="%s"'
             % file_name).encode("utf-8")
    head += b"\r\nContent-Type: application/pdf\r\n\r\n"
    tail = b"\r\n--" + b + b"--\r\n"
    return ("multipart/form-data; boundary=%s" % boundary,
            bytes(head) + file_bytes + tail)


class ChunkedReader:
    """模拟 socket 流：按给定块大小输出数据（最后一块可能很小）。"""

    def __init__(self, data: bytes, chunk: int):
        self.data = data
        self.chunk = chunk
        self.pos = 0

    def read(self, n: int) -> bytes:
        n = min(n, len(self.data) - self.pos)
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out


def run_case(name, fields, file_bytes, chunk, content_length=None):
    ct, body = build_body(fields, "文档 测试.pdf", file_bytes)
    reader = ChunkedReader(body, chunk)
    cl = len(body) if content_length is None else content_length
    dest = ROOT / ".test_data" / "parse_test"
    fields_out, files = recv_multipart_to_file(reader, ct, dest, 10_000_000, cl)
    assert fields_out == fields, (name, fields_out)
    assert len(files) == 1, (name, files)
    fname, saved_name, path = files[0]
    got = Path(path).read_bytes()
    assert got == file_bytes, "%s: 文件内容不一致" % name
    assert fname == "file"
    print("PASS %-28s chunk=%-6d 文件=%d 字节" % (name, chunk, len(file_bytes)))


def main() -> int:
    small = b"%PDF-1.4\n" + b"x" * 800 + b"\n%%EOF\n"
    big = b"%PDF-1.4\n" + bytes(range(256)) * 4096 + b"\n%%EOF\n"  # ~1MB
    tricky = b"data with \r\n-- inside \r\n and --dashes -- ok"

    run_case("小文件", {"filename": "a.pdf", "copies": "2"}, small, 65536)
    run_case("小文件-小块", {"filename": "a.pdf", "copies": "1"}, small, 7)
    run_case("1MB-64K块", {"filename": "b.pdf", "copies": "1"}, big, 65536)
    run_case("1MB-1K块", {"filename": "b.pdf", "copies": "1"}, big, 1024)
    run_case("1MB-1字节块", {"filename": "b.pdf", "copies": "3"}, big, 1)
    run_case("特殊内容", {"filename": "c.pdf", "copies": "1"}, tricky, 13)

    # 边界缺失应报错
    try:
        recv_multipart_to_file(io.BytesIO(b"junk"), "multipart/form-data",
                               ROOT / ".test_data" / "parse_test", 1000)
        print("FAIL 未检测到 boundary 缺失")
        return 1
    except ValueError:
        print("PASS boundary 缺失 -> ValueError")

    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
