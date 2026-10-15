# -*- coding: utf-8 -*-
"""导入 PDF：抽文字层。页数 / 字数 / 加密在这里拦。"""
from __future__ import annotations

import time
from io import BytesIO
from typing import Any


class PdfError(Exception):
    pass


def extract_pdf(data: bytes, deadline: float | None = None) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfError("服务器还没装 PDF 解析") from exc
    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise PdfError("读不了这份 PDF") from exc
    if getattr(reader, "is_encrypted", False):
        raise PdfError("这份 PDF 要密码")
    pages = list(reader.pages)
    if len(pages) > 80:
        raise PdfError("超过 80 页")
    parts: list[str] = []
    for page in pages:
        if deadline and time.time() > deadline:
            raise PdfError("解析超过 30 秒")
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
    text = "\n".join(parts)
    if len(text) > 600_000:
        raise PdfError("文字太多")
    if len(text) < 2000:
        raise PdfError("没有文字层（扫描件）")
    title = ""
    meta = getattr(reader, "metadata", None)
    if meta is not None:
        title = str(getattr(meta, "title", "") or "")[:200]
    return {"text": text, "pages": len(pages), "title": title}
