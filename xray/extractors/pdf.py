# -*- coding: utf-8 -*-
"""PDF 文字层抽取：行为与旧 scan.py 内联逻辑一致。

- 仅认领 .pdf，且仅在 FILE_READ_LIMIT 内做抽取（超大文件跳过，保留旧内容）。
- 抽取上限 PDF_TEXT_LIMIT；有文字则 source=text_layer 且写库。
- 抽不到文字（扫描件）不写库 —— 既保留旧 OCR 结果，也把补识别交给既有
  _start_pdf_backlog_ocr 后台队列（“无文字层才走 OCR”的判定不变）。
- 抽取失败走既有 skipped 通道（pdf_extract_failed）。
"""
import logging

from xray.config import PDF_EXTS, FILE_READ_LIMIT, PDF_TEXT_LIMIT

from .base import Extractor, ExtractResult

_log = logging.getLogger("xray")


class PdfExtractor(Extractor):
    name = "pdf"
    auto = True

    def matches(self, path, ext, size):
        return ext in PDF_EXTS

    def extract(self, path, ext, size):
        if size > FILE_READ_LIMIT:
            return ExtractResult("", "text_layer", False, meta={"reason": "over_read_limit"})
        try:
            # 惰性导入：ocr 依赖 scan 的 store_recognized，顶层互导会形成循环
            from xray.ocr import extract_pdf_text
            text = extract_pdf_text(path, raise_on_error=True)[:PDF_TEXT_LIMIT]
        except Exception:
            _log.debug("PDF 文字层抽取失败，内容留空：%s", path, exc_info=True)
            return ExtractResult("", "text_layer", False, skip_reason="pdf_extract_failed")
        # 空文字不写库：移动/改名时保留旧的 OCR 内容
        return ExtractResult(text, "text_layer", bool((text or "").strip()))
