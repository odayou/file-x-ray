# -*- coding: utf-8 -*-
"""纯文本抽取：行为与旧 scan.py 内联逻辑一致。

- 清单内扩展名直接认领；清单外仅当文件不超过 FILE_READ_LIMIT 且内容嗅探为文本时认领。
- 读取上限 CONTENT_INDEX_LIMIT（200000 字符），UTF-8 + errors=ignore。
- 读取失败走既有 skipped 通道（text_read_failed），不抛到扫描主流程。
"""
import logging

from xray.config import (
    TEXT_EXTS, KNOWN_BIN_EXTS, FILE_READ_LIMIT, CONTENT_INDEX_LIMIT, sniff_text,
)

from .base import Extractor, ExtractResult

_log = logging.getLogger("xray")


class TextExtractor(Extractor):
    name = "text"
    auto = True

    def matches(self, path, ext, size):
        if ext in TEXT_EXTS:
            return True
        # 未知类型：仅在可读大小范围内做一次内容嗅探（与旧逻辑完全一致）
        if ext not in KNOWN_BIN_EXTS and size <= FILE_READ_LIMIT:
            return sniff_text(path)
        return False

    def extract(self, path, ext, size):
        # 旧逻辑：is_text 为真但超过读取上限时，content 置空且仍然覆盖（write=True）
        if size > FILE_READ_LIMIT:
            return ExtractResult("", "", True, meta={"reason": "over_read_limit"})
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read(CONTENT_INDEX_LIMIT)
            return ExtractResult(text, "", True)
        except Exception as e:
            _log.debug("文本读取失败，内容留空：%s", path, exc_info=True)
            return ExtractResult("", "", True, skip_reason="text_read_failed", error=None)
