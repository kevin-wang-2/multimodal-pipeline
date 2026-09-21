"""媒体 → 页图像列表。PNG / JPEG / TIFF（PIL）与 PDF（pypdfium2，paddleocr 自带）。按魔数判类型，不信 content_type。"""
from __future__ import annotations

from dataclasses import dataclass


class UnsupportedDocument(Exception):
    pass


@dataclass
class Page:
    index: int          # 1-based
    image: "object"     # PIL.Image RGB


def sniff(head: bytes) -> str:
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    raise UnsupportedDocument("not a PDF / PNG / JPEG / TIFF / WEBP")


def load_pages(path: str, wanted: list[int] | None, dpi: int) -> tuple[int, list[Page]]:
    """返回 (总页数, 选中的页)。wanted 里超出范围的页码忽略；全部超范围 → 空列表。"""
    from PIL import Image

    with open(path, "rb") as f:
        kind = sniff(f.read(16))
    pages: list[Page] = []
    if kind == "pdf":
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(path)
        n = len(doc)
        for i in range(1, n + 1):
            if wanted and i not in wanted:
                continue
            pages.append(Page(i, doc[i - 1].render(scale=dpi / 72.0).to_pil().convert("RGB")))
        return n, pages
    img = Image.open(path)
    n = getattr(img, "n_frames", 1)
    for i in range(1, n + 1):
        if wanted and i not in wanted:
            continue
        if n > 1:
            img.seek(i - 1)
        pages.append(Page(i, img.convert("RGB")))
    return n, pages
