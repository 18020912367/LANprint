# -*- coding: utf-8 -*-
"""PDF 渲染与 GDI 打印（仅服务端，仅 Windows）。

打印链路：pypdfium2 逐页渲染为位图 -> Pillow 转 DIB -> win32ui 打印机 DC
StretchDIBits 写入打印机队列。对任何打印机（含虚拟打印机）均可用。

另提供：本机打印机枚举（安装向导用）、打印机状态查询（/ping /status 用）。
"""

import logging
import time

import pypdfium2 as pdfium
import win32con
import win32print
import win32ui
from PIL import Image, ImageWin

log = logging.getLogger("lanprint.server")

# PDF 渲染分辨率（DPI）。150 为清晰度与内存占用的平衡点。
RENDER_DPI = 150

# 打印机属性：用户手动设置了"脱机使用打印机"
PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x400

# PRINTER_STATUS 中表示不可用的位（ERROR/OFFLINE/NOT_AVAILABLE/USER_INTERVENTION
# /PENDING_DELETION 等），枚举与状态查询时据此剔除
PRINTER_BAD_STATUS_MASK = (0x2 | 0x3 | 0x4 | 0x5 | 0x7 | 0x8
                           | 0x1000 | 0x100000 | 0x400000 | 0x20000000)


# 常见虚拟打印机关键字（非物理设备，不作为共享打印机候选）
VIRTUAL_PRINTER_KEYWORDS = (
    "pdf", "xps", "onenote", "fax", "wps", "snagit",
    "evernote", "adobe", "foxit", "rustdesk", "document writer",
)


def _looks_virtual(printer_name: str) -> bool:
    low = printer_name.lower()
    return any(k in low for k in VIRTUAL_PRINTER_KEYWORDS)


def list_local_printers():
    """枚举本机已连接且可用的打印机名称列表（安装向导使用）。

    剔除脱机/错误状态以及常见虚拟打印机（PDF/XPS/传真等）。
    """
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    names = []
    for p in win32print.EnumPrinters(flags, None, 2):
        name = p["pPrinterName"]
        if _looks_virtual(name):
            continue
        status = p.get("Status", 0) or 0
        attributes = p.get("Attributes", 0) or 0
        if attributes & PRINTER_ATTRIBUTE_WORK_OFFLINE:
            continue
        if status & PRINTER_BAD_STATUS_MASK:
            continue
        names.append(name)
    return names


def _status_text(status: int, attributes: int) -> str:
    if attributes & PRINTER_ATTRIBUTE_WORK_OFFLINE:
        return "打印机被设置为脱机使用"
    mapping = [
        (0x2, "打印机错误"), (0x3, "打印机正在删除"), (0x4, "卡纸"),
        (0x5, "缺纸"), (0x7, "纸张问题"), (0x8, "打印机脱机"),
        (0x1000, "打印机不可用"), (0x100000, "需要用户干预"),
        (0x400000, "打印机舱门打开"), (0x20000000, "打印服务器离线"),
    ]
    for mask, text in mapping:
        if status & mask:
            return text
    return "状态码 0x%X" % status


def get_printer_status(printer_name: str) -> dict:
    """查询打印机状态。返回 {online, jobs_waiting, error}。"""
    try:
        handle = win32print.OpenPrinter(printer_name)
        try:
            info = win32print.GetPrinter(handle, 2)
            status = info.get("Status", 0) or 0
            attributes = info.get("Attributes", 0) or 0
            jobs = info.get("cJobs", 0) or 0
            ok = not (status & PRINTER_BAD_STATUS_MASK) and not (
                attributes & PRINTER_ATTRIBUTE_WORK_OFFLINE)
            return {
                "online": bool(ok),
                "jobs_waiting": int(jobs),
                "error": None if ok else _status_text(status, attributes),
            }
        finally:
            win32print.ClosePrinter(handle)
    except Exception as e:  # 打印机不存在/权限不足等
        return {"online": False, "jobs_waiting": 0, "error": str(e)}


def _snapshot_job_ids(printer_name: str) -> set:
    """枚举打印机队列中现有作业 ID 集合。"""
    try:
        handle = win32print.OpenPrinter(printer_name)
        try:
            jobs = win32print.EnumJobs(handle, 0, 500, 1)
            return {j["JobId"] for j in jobs}
        finally:
            win32print.ClosePrinter(handle)
    except Exception as e:
        log.warning("枚举打印队列失败: %s", e)
        return set()


def _wait_new_job_id(printer_name: str, before: set, doc_name: str = "",
                     timeout_sec: float = 3.0):
    """轮询队列，返回快照之后新出现的作业 ID；超时返回 None。

    快照差之外再按文档名兜底匹配，避免快照时序问题导致漏检。
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            handle = win32print.OpenPrinter(printer_name)
            try:
                jobs = win32print.EnumJobs(handle, 0, 500, 1)
            finally:
                win32print.ClosePrinter(handle)
        except Exception as e:
            log.warning("枚举打印队列失败: %s", e)
            time.sleep(0.2)
            continue
        new_ids = {j["JobId"] for j in jobs} - before
        if new_ids:
            return new_ids.pop()
        for job in jobs:
            if job.get("pDocument") == doc_name:
                return job["JobId"]
        time.sleep(0.05)
    return None


def print_pdf(pdf_path, printer_name: str, copies: int = 1,
              doc_name: str = None):
    """把 PDF 逐页渲染后经 GDI 写入打印机队列。

    返回 (win_job_id, pages)。win_job_id 供 queue_worker 轮询打印结果。

    可能抛出：pypdfium2 的解析异常、win32 的设备异常（如打印机名无效）。
    """
    doc_name = doc_name or pdf_path.name if hasattr(pdf_path, "name") \
        else str(pdf_path)
    copies = max(1, min(int(copies), 99))

    document = pdfium.PdfDocument(str(pdf_path))
    try:
        page_count = len(document)
        images = []
        for page in document:
            bitmap = page.render(scale=RENDER_DPI / 72.0)
            images.append(bitmap.to_pil().convert("RGB"))
    finally:
        document.close()

    if not images:
        raise ValueError("PDF 中没有可打印的页面")

    # 创建打印机设备上下文。
    # 注意：新编译的 pywin32 中 CDC.StartDoc 返回值不可靠（可能为 None），
    # 因此不信任返回值，改用"作业队列前后快照差"获取真实 Windows 作业 ID。
    before = _snapshot_job_ids(printer_name)
    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(printer_name)
    hdc.StartDoc(doc_name)
    win_job_id = _wait_new_job_id(printer_name, before, doc_name,
                                  timeout_sec=3.0)
    if win_job_id is None:
        hdc.DeleteDC()
        raise RuntimeError("创建 Windows 打印作业失败（打印机不可用？）")
    try:
        for _ in range(copies):
            for img in images:
                hdc.StartPage()
                _draw_fit(hdc, img)
                hdc.EndPage()
    finally:
        hdc.EndDoc()
        hdc.DeleteDC()

    log.info("已写入打印队列: 文档=%s 页数=%d 副本=%d win_job_id=%s",
             doc_name, page_count, copies, win_job_id)
    return win_job_id, page_count


def probe_print(printer_name: str) -> dict:
    """软打印探测：向打印机提交零页探测作业后立即中止，不出纸。

    用于安装向导/自检验证"本进程能否向该打印机提交打印作业"
    （覆盖 CreatePrinterDC -> StartDoc -> 作业在队列中可见 全链路）。
    返回 {ok: bool, job_id: int|None, error: str|None}。
    """
    before = _snapshot_job_ids(printer_name)
    hdc = win32ui.CreateDC()
    try:
        hdc.CreatePrinterDC(printer_name)
    except win32ui.error as e:
        return {"ok": False, "job_id": None,
                "error": "无法打开打印机: %s" % e}
    # 先轮询确认作业进入队列（文档保持打开状态），再中止——
    # 若先 AbortDoc，作业会被直接丢弃，队列中无从确认。
    job_id = None
    try:
        hdc.StartDoc("LANPrint-probe")
        job_id = _wait_new_job_id(printer_name, before, "LANPrint-probe", 3.0)
    finally:
        try:
            hdc.AbortDoc()  # 中止文档：作业被丢弃，不产生纸张输出
        except win32ui.error:
            pass
        hdc.DeleteDC()
    if job_id is None:
        return {"ok": False, "job_id": None,
                "error": "探测作业未在打印队列中出现（等待 3 秒超时）"}
    return {"ok": True, "job_id": int(job_id), "error": None}


def _draw_fit(hdc, img):
    """把单页位图按"适配纸张、保持纵横比、居中"绘制到打印机 DC。

    横向页面（宽>高）在纵向纸张上自动旋转 90 度铺满纸面。
    """
    out = hdc.GetHandleOutput()
    horz = hdc.GetDeviceCaps(win32con.HORZRES)   # 可打印区域宽（像素）
    vert = hdc.GetDeviceCaps(win32con.VERTRES)   # 可打印区域高（像素）

    if (img.width > img.height) != (horz > vert):
        img = img.transpose(Image.Transpose.ROTATE_90)

    scale = min(horz / img.width, vert / img.height)
    w = max(1, int(img.width * scale))
    h = max(1, int(img.height * scale))
    x = (horz - w) // 2
    y = (vert - h) // 2

    dib = ImageWin.Dib(img)
    dib.draw(out, (x, y, x + w, y + h))
