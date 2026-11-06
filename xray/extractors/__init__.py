# -*- coding: utf-8 -*-
"""抽取器有序注册表。

扫描与实时感知统一走 `extract_for_scan()`：按 EXTRACTORS 顺序找到第一个认领者，
再调用它的 extract()。新增格式只需在这里注册，不必改扫描主流程。

顺序原则：
  1. 结构明确的格式优先（Office / PDF），避免被“未知文本嗅探”抢先；
  2. 纯文本兜底（含清单外扩展名的内容嗅探）；
  3. 图片 OCR、音视频转写为 on_demand（auto=False）：扫描阶段不自动跑，
     仍由既有 /api/ocr、/api/transcribe 按需触发，行为与迁移前一致。
"""
from xray.config import IMAGE_EXTS, AUDIO_EXTS, VIDEO_EXTS

from .base import Extractor, ExtractResult, ExtractFailure
from .text import TextExtractor
from .pdf import PdfExtractor
from .office import DocxExtractor, XlsxExtractor, PptxExtractor


class _OnDemandExtractor(Extractor):
    """按需抽取器占位：只用于类型认领与注册表可查，扫描不会自动调用。"""

    auto = False
    reason = ""

    def __init__(self, name, exts, reason):
        self.name = name
        self.exts = set(exts)
        self.reason = reason

    def matches(self, path, ext, size):
        return ext in self.exts

    def extract(self, path, ext, size):
        # 真正的识别/转写仍由 xray.ocr / xray.transcribe 的既有任务入口执行
        return ExtractResult("", "", False, skip_reason=self.reason)


ImageOcrExtractor = _OnDemandExtractor(
    "image_ocr", IMAGE_EXTS, "image_ocr_on_demand")
AudioTranscribeExtractor = _OnDemandExtractor(
    "audio_transcribe", AUDIO_EXTS | VIDEO_EXTS, "transcribe_on_demand")

# 有序注册表：扫描时按此顺序匹配
EXTRACTORS = [
    DocxExtractor(),
    XlsxExtractor(),
    PptxExtractor(),
    PdfExtractor(),
    TextExtractor(),
    ImageOcrExtractor,
    AudioTranscribeExtractor,
]

AUTO_EXTRACTORS = [e for e in EXTRACTORS if e.auto]


def extract_for_scan(path, ext, size):
    """扫描/实时感知入口：返回首个认领抽取器的结果；无人认领返回 None。"""
    for ex in AUTO_EXTRACTORS:
        try:
            if ex.matches(path, ext, size):
                return ex.extract(path, ext, size)
        except ExtractFailure as e:
            return ExtractResult("", "", False, error=e)
    return None


def extractor_for(path, ext, size=0):
    """按扩展名查注册表（含按需项），供诊断/测试使用。"""
    for ex in EXTRACTORS:
        try:
            if ex.matches(path, ext, size):
                return ex
        except Exception:
            continue
    return None


__all__ = [
    "Extractor", "ExtractResult", "ExtractFailure",
    "EXTRACTORS", "AUTO_EXTRACTORS",
    "extract_for_scan", "extractor_for",
]
