# -*- coding: utf-8 -*-
"""抽取器统一接口。

每个抽取器负责一类文件：判断“是否认领”该文件，并把它转成可搜索文本。
扫描流程只关心结果，不再内联任何格式判断。
"""
import time


class ExtractFailure(Exception):
    """单个文件抽取失败（损坏、加密、缺成员、XML 异常等）。

    扫描主流程捕获后按既有机制记录 error 并继续，绝不中断扫描。
    """


class ExtractResult:
    """一次抽取的结果。

    字段语义（与旧 scan.py 内联逻辑逐一对齐）：
      text        抽取出的正文（已按各自上限截断）
      source      写入 files.content_source 的值；文本为空串，PDF 为 text_layer
      write       是否应把 text 写入 content。文本恒为 True（旧逻辑即使读空也覆盖）；
                  PDF / Office 仅在抽到非空文字时为 True —— 这保证“移动/改名不冲掉旧内容”
      truncated   正文是否因上限被截断（写入 content_truncated 标记）
      meta        供日志/排查的附加信息（工作表数、行数等）
      skip_reason 走既有 _rec_skip 通道的原因（文本读取失败、PDF 抽取失败）
      error       走既有 _rec_error 通道的异常（Office 损坏/加密）
    """

    __slots__ = ("text", "source", "write", "truncated", "meta", "skip_reason", "error")

    def __init__(self, text="", source="", write=False, truncated=False,
                 meta=None, skip_reason="", error=None):
        self.text = text or ""
        self.source = source or ""
        self.write = bool(write)
        self.truncated = bool(truncated)
        self.meta = meta or {}
        self.skip_reason = skip_reason or ""
        self.error = error


class Extractor:
    """抽取器基类。子类实现 matches() 与 extract()。"""

    name = ""
    # auto=True：普通扫描时自动抽取；False：按需触发（图片 OCR / 音视频转写）
    auto = True

    def matches(self, path, ext, size):
        raise NotImplementedError

    def extract(self, path, ext, size):
        raise NotImplementedError


def _elapsed_ms(start):
    return int((time.time() - start) * 1000)
