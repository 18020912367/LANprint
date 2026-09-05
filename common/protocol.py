# -*- coding: utf-8 -*-
"""LANPrint 通信协议与 HTTP 工具（服务端/客户端共用）。

包含：API 路径常量、任务状态定义、multipart 流式解析（边收边落盘）、
HTTP GET/POST 客户端工具（基于标准库）。
"""

import http.client
import json
import os
import re
import uuid
from pathlib import Path
from urllib import request as _urlrequest
from urllib.error import HTTPError, URLError

# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------
API_PING = "/ping"
API_STATUS = "/status"
API_PRINT = "/print"
API_JOB = "/job"            # 实际路径：/job/{job_id}

SERVER_NAME = "LANPrint"
SERVER_VERSION = "1.0.0"
DEFAULT_PORT = 17878
USER_AGENT = "LANPrint/1.0"

# 单个表单字段（非文件）允许的最大体积
MAX_FIELD_BYTES = 1 * 1024 * 1024


class JobState:
    """打印任务状态机：queued -> printing -> done | failed"""

    QUEUED = "queued"
    PRINTING = "printing"
    DONE = "done"
    FAILED = "failed"


TERMINAL_STATES = {JobState.DONE, JobState.FAILED}


class PayloadTooLarge(Exception):
    """上传内容超过大小上限（服务端映射为 HTTP 413）。"""


class TransportError(Exception):
    """网络传输失败（连接拒绝/超时/DNS 等）。"""


def new_job_id() -> str:
    """生成 8 位十六进制任务 ID。"""
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# multipart 解析（服务端接收侧，流式、边收边落盘）
# ---------------------------------------------------------------------------

def parse_multipart_boundary(content_type: str) -> str:
    """从 Content-Type 头中提取 multipart boundary。"""
    m = re.search(r'boundary="?([^";,\r\n]+)"?', content_type or "", re.I)
    if not m:
        raise ValueError("Content-Type 中缺少 multipart boundary")
    return m.group(1)


class _BufReader:
    """带缓冲的二进制读取器：支持跨块的行读取与边界定位。

    目的：HTTP 层把 multipart 主体按块读入内存缓冲，在缓冲中定位
    boundary，把 boundary 之前的数据直接写盘，避免整个文件驻留内存。

    注意：必须传入 content_length 并按剩余量读取——BufferedReader.read(n)
    会阻塞到读满 n 字节或连接关闭，直接固定块长会导致接收卡死。
    """

    def __init__(self, rfile, content_length: int = 0):
        self._rfile = rfile
        self._buf = bytearray()
        self.remaining = content_length     # 尚未读取的请求体字节数
        self.bytes_read = 0                 # 已从网络读取的字节数（含缓冲）
        self.max_bytes = 0                  # >0 时启用大小上限

    def _fill(self) -> bool:
        """从网络再读一块数据，返回是否读到内容。"""
        if self.remaining <= 0:
            return False
        chunk = self._rfile.read(min(65536, self.remaining))
        if not chunk:
            return False
        self._buf.extend(chunk)
        self.remaining -= len(chunk)
        self.bytes_read += len(chunk)
        if self.max_bytes and self.bytes_read > self.max_bytes:
            raise PayloadTooLarge()
        return True

    def drain(self) -> None:
        """读取并丢弃请求体中尚未消费的剩余字节（保持 keep-alive 干净）。"""
        while self.remaining > 0 and self._fill():
            self._buf.clear()

    def readline(self, limit: int = 8192) -> bytes:
        """读一行（含换行符）；数据流提前结束则返回剩余内容。"""
        while b"\n" not in self._buf:
            if not self._fill():
                break
        i = self._buf.find(b"\n")
        if i < 0:
            line = bytes(self._buf[:limit])
            del self._buf[:min(len(self._buf), limit)]
            return line
        line = bytes(self._buf[: i + 1])
        del self._buf[: i + 1]
        return line if len(line) <= limit else line[:limit]

    def read_until(self, delimiter: bytes, sink) -> None:
        """把数据写入 sink，直到遇到 delimiter（delimiter 保留在缓冲中）。"""
        keep = len(delimiter) - 1
        while True:
            i = self._buf.find(delimiter)
            if i >= 0:
                if i:
                    sink(bytes(self._buf[:i]))
                    del self._buf[:i]
                return
            if len(self._buf) > keep:
                cut = len(self._buf) - keep
                sink(bytes(self._buf[:cut]))
                del self._buf[:cut]
            if not self._fill():
                return  # 数据流提前结束：剩余内容全部交给 sink


def _disp_param(disposition: str, key: str) -> str:
    """从 Content-Disposition 中提取 name/filename 参数。"""
    m = re.search(r'%s="([^"]*)"' % key, disposition, re.I)
    if m:
        return m.group(1)
    # 兼容不带引号的写法
    m = re.search(r"%s=([^;\r\n]+)" % key, disposition, re.I)
    return m.group(1).strip() if m else ""


def recv_multipart_to_file(rfile, content_type: str, dest_dir: Path,
                           max_bytes: int, content_length: int = 0):
    """流式解析 multipart/form-data，文件字段直接落盘。

    参数 content_length 为请求体总长（Content-Length），用于精确控制
    网络读取量，防止阻塞等待。传 0 时退化为"读到连接关闭"。

    返回: (fields: dict[str, str], files: list[(name, filename, Path)])
    文件以 tmp_*.part 临时文件保存，由调用方在登记任务后重命名。

    可能抛出: ValueError(boundary 缺失/数据损坏)、PayloadTooLarge(超限)
    """
    boundary = parse_multipart_boundary(content_type)
    dash_boundary = b"--" + boundary.encode("utf-8")
    delimiter = b"\r\n" + dash_boundary

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    reader = _BufReader(rfile, content_length)
    reader.max_bytes = max_bytes + MAX_FIELD_BYTES + 65536

    fields: dict = {}
    files: list = []

    # 跳过 preamble，定位首个 --boundary 行
    first = reader.readline().strip()
    tries = 0
    while first != dash_boundary:
        tries += 1
        if tries > 64 or not first:
            raise ValueError("multipart 起始边界缺失")
        first = reader.readline().strip()

    while True:
        # 读取本部分的头部
        headers = {}
        while True:
            line = reader.readline().decode("utf-8", "replace").strip()
            if not line:
                break
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()

        disp = headers.get("content-disposition", "")
        name = _disp_param(disp, "name")
        filename = _disp_param(disp, "filename")

        if filename:
            # 文件部分：流式落盘
            tmp_path = dest_dir / ("tmp_%s.part" % uuid.uuid4().hex)
            with open(tmp_path, "wb") as out:
                reader.read_until(delimiter, out.write)
            files.append((name, filename, tmp_path))
        else:
            # 普通字段：收集到内存（有上限）
            acc = bytearray()

            def _collect(chunk, acc=acc):
                if len(acc) <= MAX_FIELD_BYTES:
                    acc.extend(chunk)

            reader.read_until(delimiter, _collect)
            fields[name or ""] = bytes(acc[:MAX_FIELD_BYTES]).decode(
                "utf-8", "replace").strip()

        # 此时缓冲以 delimiter（\r\n--boundary）开头，消费之
        if bytes(reader._buf[: len(delimiter)]) == delimiter:
            del reader._buf[: len(delimiter)]
        else:
            raise ValueError("multipart 边界数据损坏")

        # 结束标志：boundary 后紧跟 "--"
        if bytes(reader._buf[:2]) == b"--":
            break
        # 否则应为 \r\n（下一部分），消费掉
        if bytes(reader._buf[:2]) == b"\r\n":
            del reader._buf[:2]

    # 丢弃请求体尾部残留（如最后一个 boundary 后的 CRLF），保持连接干净
    reader.drain()

    return fields, files


# ---------------------------------------------------------------------------
# HTTP 客户端工具（客户端/测试脚本使用）
# ---------------------------------------------------------------------------

def _decode_json(data: bytes) -> dict:
    try:
        obj = json.loads(data.decode("utf-8"))
        return obj if isinstance(obj, dict) else {"ok": False}
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "raw": data[:200].decode("utf-8", "replace")}


def http_get_json(url: str, timeout: float = 10.0):
    """GET 并解析 JSON。返回 (status_code, dict)；连接失败抛 TransportError。"""
    try:
        req = _urlrequest.Request(url, headers={"User-Agent": USER_AGENT})
        with _urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode_json(resp.read())
    except HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, _decode_json(body)
    except (URLError, OSError) as e:
        raise TransportError(str(e))


def http_post_multipart(url: str, fields: dict, file_field: str,
                        file_path: str, timeout: float = 120.0):
    """POST multipart/form-data 上传文件（分块读取，不整体载入内存）。

    返回 (status_code, dict)；连接失败抛 TransportError。
    """
    parsed = _urlrequest.urlparse(url)
    secure = parsed.scheme == "https"
    boundary = "----LANPrint" + uuid.uuid4().hex
    b = boundary.encode("ascii")

    head = bytearray()
    for k, v in (fields or {}).items():
        head += b"--" + b
        head += ('\r\nContent-Disposition: form-data; name="%s"\r\n\r\n'
                 % k).encode("utf-8")
        head += str(v).encode("utf-8") + b"\r\n"
    head += b"--" + b
    head += ('\r\nContent-Disposition: form-data; name="%s"; filename="%s"'
             % (file_field,
                os.path.basename(file_path).replace('"', "_"))).encode("utf-8")
    head += b"\r\nContent-Type: application/pdf\r\n\r\n"
    tail = b"\r\n--" + b + b"--\r\n"

    total = len(head) + os.path.getsize(file_path) + len(tail)

    conn_cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
    conn = conn_cls(parsed.hostname,
                    parsed.port or (443 if secure else 80),
                    timeout=timeout)

    def _body():
        yield bytes(head)
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        yield tail

    try:
        conn.request("POST", parsed.path or "/", body=_body(), headers={
            "Content-Type": "multipart/form-data; boundary=%s" % boundary,
            "Content-Length": str(total),
            "User-Agent": USER_AGENT,
        })
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, _decode_json(data)
    except (OSError, http.client.HTTPException) as e:
        raise TransportError(str(e))
    finally:
        conn.close()
