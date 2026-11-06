# -*- coding: utf-8 -*-
"""Office Open XML（docx / xlsx / pptx）抽取。

只依赖标准库 zipfile + xml.etree.ElementTree，不引入 python-docx / openpyxl / pandas。
三种格式本质都是 ZIP + XML，读取时逐个成员解析，并对解压后大小、行数、字符数设上限，
避免超大工作簿把扫描拖死。

老格式 .doc / .xls / .ppt（二进制 OLE）不在本模块支持范围内。
"""
import re
import zipfile
import xml.etree.ElementTree as ET

from xray.config import (
    OFFICE_TEXT_LIMIT, OFFICE_MAX_BYTES, OFFICE_MAX_ENTRY_BYTES,
    XLSX_MAX_ROWS_PER_SHEET, XLSX_MAX_COL_GAP,
)

from .base import Extractor, ExtractResult, ExtractFailure

# WordprocessingML / DrawingML / SpreadsheetML 命名空间
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _tag(ns, name):
    return "{%s}%s" % (ns, name)


_W_P = _tag(_W, "p")
_W_T = _tag(_W, "t")
_W_BR = _tag(_W, "br")
_W_TAB = _tag(_W, "tab")
_W_TC = _tag(_W, "tc")

_A_P = _tag(_A, "p")
_A_T = _tag(_A, "t")
_A_BR = _tag(_A, "br")

_S_SHEET = _tag(_S, "sheet")
_S_SI = _tag(_S, "si")
_S_T = _tag(_S, "t")
_S_ROW = _tag(_S, "row")
_S_C = _tag(_S, "c")
_S_V = _tag(_S, "v")
_S_IS = _tag(_S, "is")
_R_ID = _tag(_R, "id")
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"


class OfficeExtractor(Extractor):
    """docx / xlsx / pptx 的公共外壳：认领 + 统一错误处理。"""

    ext = ""

    def matches(self, path, ext, size):
        return ext == self.ext

    def extract(self, path, ext, size):
        if size > OFFICE_MAX_BYTES:
            return ExtractResult("", self.ext.lstrip("."), False,
                                 meta={"reason": "over_office_limit", "size": size},
                                 skip_reason="office_too_large")
        try:
            with zipfile.ZipFile(path) as z:
                return self._extract_zip(z, path)
        except ExtractFailure as e:
            return ExtractResult("", self.ext.lstrip("."), False, error=e)
        except zipfile.BadZipFile as e:
            return ExtractResult("", self.ext.lstrip("."), False,
                                 error=ExtractFailure("不是有效的 Office 文件（ZIP 损坏或已加密）：%s" % e))
        except ET.ParseError as e:
            return ExtractResult("", self.ext.lstrip("."), False,
                                 error=ExtractFailure("XML 解析失败：%s" % e))
        except (KeyError, OSError, RuntimeError) as e:
            return ExtractResult("", self.ext.lstrip("."), False,
                                 error=ExtractFailure("读取失败：%s" % e))

    def _extract_zip(self, z, path):
        raise NotImplementedError

    @staticmethod
    def _read_member(z, name):
        """按解压后大小上限读取成员，避免 ZIP 炸弹。"""
        info = z.getinfo(name)
        if info.file_size > OFFICE_MAX_ENTRY_BYTES:
            raise ExtractFailure("成员 %s 解压后过大（%d 字节）" % (name, info.file_size))
        return z.open(name)


class DocxExtractor(OfficeExtractor):
    name = "docx"
    ext = ".docx"

    def _extract_zip(self, z, path):
        if "word/document.xml" not in z.namelist():
            raise ExtractFailure("不是有效的 Word 文档：缺少 word/document.xml")
        text, truncated = _parse_word_xml(self._read_member(z, "word/document.xml"))
        text = text[:OFFICE_TEXT_LIMIT]
        meta = {"format": "docx", "truncated": truncated or len(text) >= OFFICE_TEXT_LIMIT}
        return ExtractResult(text, "docx", bool(text.strip()),
                             truncated=meta["truncated"], meta=meta)


class PptxExtractor(OfficeExtractor):
    name = "pptx"
    ext = ".pptx"

    _SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
    _NOTE_RE = re.compile(r"^ppt/notesSlides/notesSlide(\d+)\.xml$")

    def _extract_zip(self, z, path):
        names = z.namelist()
        slides = sorted(
            ((int(m.group(1)), n) for n in names for m in [self._SLIDE_RE.match(n)] if m),
            key=lambda x: x[0])
        if not slides:
            raise ExtractFailure("不是有效的 PowerPoint 文件：未找到任何幻灯片")
        notes = {int(m.group(1)): n
                 for n in names for m in [self._NOTE_RE.match(n)] if m}
        chunks, total, truncated = [], 0, False
        for idx, name in slides:
            body, cut = _parse_drawing_xml(self._read_member(z, name))
            chunk = "第 %d 页\n%s" % (idx, body.strip())
            if idx in notes:
                nt, _ = _parse_drawing_xml(self._read_member(z, notes[idx]))
                if nt.strip():
                    chunk += "\n【备注】\n" + nt.strip()
            if total + len(chunk) > OFFICE_TEXT_LIMIT:
                remain = max(0, OFFICE_TEXT_LIMIT - total)
                chunks.append(chunk[:remain])
                total = OFFICE_TEXT_LIMIT
                truncated = True
                break
            chunks.append(chunk)
            total += len(chunk)
            truncated = truncated or cut
        text = "\n\n".join(chunks)[:OFFICE_TEXT_LIMIT]
        return ExtractResult(text, "pptx", bool(text.strip()), truncated=truncated,
                             meta={"format": "pptx", "slides": len(slides), "truncated": truncated})


class XlsxExtractor(OfficeExtractor):
    name = "xlsx"
    ext = ".xlsx"

    def _extract_zip(self, z, path):
        names = z.namelist()
        if "xl/workbook.xml" not in names:
            raise ExtractFailure("不是有效的 Excel 文件：缺少 xl/workbook.xml")
        shared = _parse_shared_strings(z, names)
        sheets = _parse_workbook(z)          # [(显示名, zip 内成员路径)]
        if not sheets:
            raise ExtractFailure("工作簿中没有任何工作表")
        chunks, total, truncated, rows_total = [], 0, False, 0
        for sheet_name, member in sheets:
            if member not in names:
                continue
            lines, cut = _parse_worksheet(z, member, shared)
            rows_total += len(lines)
            chunk = "【工作表：%s】\n%s" % (sheet_name, "\n".join(lines))
            if cut:
                truncated = True
            if total + len(chunk) > OFFICE_TEXT_LIMIT:
                remain = max(0, OFFICE_TEXT_LIMIT - total)
                chunks.append(chunk[:remain])
                total = OFFICE_TEXT_LIMIT
                truncated = True
                break
            chunks.append(chunk)
            total += len(chunk)
        text = "\n\n".join(chunks)[:OFFICE_TEXT_LIMIT]
        return ExtractResult(
            text, "xlsx", bool(text.strip()), truncated=truncated,
            meta={"format": "xlsx", "sheets": len(sheets), "rows": rows_total,
                  "truncated": truncated})


# ----------------------------------------------------------------------------
# 解析实现
# ----------------------------------------------------------------------------
def _parse_word_xml(fp):
    """按 <w:p> 分段，保留换行；表格单元格内的段落同样按文档顺序收集。"""
    parts, total, truncated = [], 0, False
    stack = []
    try:
        for event, el in ET.iterparse(fp, events=("start", "end")):
            if event == "start":
                if el.tag == _W_P:
                    stack.append([])
                continue
            if el.tag == _W_T:
                if stack:
                    stack[-1].append(el.text or "")
                    total += len(el.text or "")
                    if total > OFFICE_TEXT_LIMIT:
                        truncated = True
            elif el.tag == _W_BR:
                if stack:
                    stack[-1].append("\n")
                    total += 1
            elif el.tag == _W_TAB:
                if stack:
                    stack[-1].append("\t")
                    total += 1
            elif el.tag == _W_P:
                if stack:
                    parts.append("".join(stack.pop()).strip())
            el.clear()
            if truncated:
                break
    finally:
        try:
            fp.close()
        except Exception:
            pass
    return "\n".join(p for p in parts if p), truncated


def _parse_drawing_xml(fp):
    """DrawingML（pptx / 备注页）：按 <a:p> 分段。"""
    parts, total, truncated = [], 0, False
    stack = []
    try:
        for event, el in ET.iterparse(fp, events=("start", "end")):
            if event == "start":
                if el.tag == _A_P:
                    stack.append([])
                continue
            if el.tag == _A_T:
                if stack:
                    stack[-1].append(el.text or "")
                    total += len(el.text or "")
                    if total > OFFICE_TEXT_LIMIT:
                        truncated = True
            elif el.tag == _A_BR:
                if stack:
                    stack[-1].append("\n")
                    total += 1
            elif el.tag == _A_P:
                if stack:
                    parts.append("".join(stack.pop()).strip())
            el.clear()
            if truncated:
                break
    finally:
        try:
            fp.close()
        except Exception:
            pass
    return "\n".join(p for p in parts if p), truncated


def _col_index(ref):
    """A1 -> 0，AB12 -> 27。"""
    m = re.match(r"([A-Za-z]+)", ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _parse_shared_strings(z, names):
    """xl/sharedStrings.xml -> [文本]，富文本条目把多个 <t> 拼接。"""
    if "xl/sharedStrings.xml" not in names:
        return []
    out = []
    try:
        fp = OfficeExtractor._read_member(z, "xl/sharedStrings.xml")
    except ExtractFailure:
        return []
    buf = None
    try:
        for event, el in ET.iterparse(fp, events=("start", "end")):
            if event == "start":
                if el.tag == _S_SI:
                    buf = []
                continue
            if el.tag == _S_T:
                if buf is not None:
                    buf.append(el.text or "")
            elif el.tag == _S_SI:
                out.append("".join(buf or [])[:5000])
                buf = None
            el.clear()
    except ET.ParseError:
        pass                     # 共享字符串损坏时按空表处理，工作表仍可抽数字/内联文本
    finally:
        try:
            fp.close()
        except Exception:
            pass
    return out


def _parse_workbook(z):
    """解析工作表显示名与关系目标，保持工作簿中的顺序。"""
    xml = z.read("xl/workbook.xml")
    root = ET.fromstring(xml)
    rels = {}
    if "xl/_rels/workbook.xml.rels" in z.namelist():
        try:
            rroot = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            for rel in rroot.findall(_REL):
                rid, target = rel.get("Id"), rel.get("Target") or ""
                if rid and target:
                    target = target.lstrip("/")
                    if not target.startswith("xl/"):
                        target = "xl/" + target
                    rels[rid] = target.replace("\\", "/")
        except Exception:
            rels = {}
    out = []
    for el in root.findall(".//" + _S_SHEET):
        name = el.get("name") or "Sheet"
        rid = el.get(_R_ID)
        member = rels.get(rid) if rid else None
        if member:
            out.append((name, member))
    return out


def _parse_worksheet(z, member, shared):
    """逐行读取缓存值：单元格以制表符分隔，行以换行分隔；只取 <v>/内联文本，不算公式。"""
    lines, total, truncated = [], 0, False
    rows_seen = 0
    try:
        fp = OfficeExtractor._read_member(z, member)
    except ExtractFailure:
        return [], True
    try:
        for event, el in ET.iterparse(fp, events=("end",)):
            if el.tag != _S_ROW:
                continue
            rows_seen += 1
            if rows_seen > XLSX_MAX_ROWS_PER_SHEET:
                truncated = True
                break
            vals = {}
            seq = 0
            for c in el.findall(_S_C):
                ci = _col_index(c.get("r")) if c.get("r") else None
                if ci is None:
                    ci = seq
                seq = ci + 1
                t = c.get("t") or ""
                val = ""
                if t == "s":
                    v = c.find(_S_V)
                    try:
                        val = shared[int(v.text)] if v is not None and v.text else ""
                    except (ValueError, IndexError):
                        val = ""
                elif t == "inlineStr":
                    is_el = c.find(_S_IS)
                    if is_el is not None:
                        val = "".join(x.text or "" for x in is_el.iter(_S_T))
                else:
                    v = c.find(_S_V)
                    if v is not None and v.text is not None:
                        val = v.text
                        if t == "b":
                            val = "TRUE" if val == "1" else "FALSE"
                if val:
                    vals[ci] = val.replace("\r", " ").replace("\n", " ").replace("\t", " ")
            if vals:
                last = max(vals)
                # 列间隔过大（稀疏表）只补一个制表符，避免一行几万个 \t
                cells = []
                for i in range(last + 1):
                    if i in vals:
                        cells.append(vals[i])
                    elif cells and len(cells) < XLSX_MAX_COL_GAP + 1:
                        cells.append("")
                line = "\t".join(cells)
            else:
                line = ""
            if line:
                if total + len(line) > OFFICE_TEXT_LIMIT:
                    truncated = True
                    break
                lines.append(line)
                total += len(line) + 1
            el.clear()
    except ET.ParseError:
        raise ExtractFailure("工作表 XML 解析失败：%s" % member)
    finally:
        try:
            fp.close()
        except Exception:
            pass
    return lines, truncated
