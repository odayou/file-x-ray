#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件索引与管理工具 —— 后端服务 (FastAPI)
运行：pip install fastapi uvicorn  →  python backend.py
默认监听 http://127.0.0.1:8000
环境变量：FIT_DB(数据库路径) / FIT_HOST / FIT_PORT
说明：本进程运行在用户本机，拥有完整文件系统权限；浏览器仅负责界面与发请求。

增强：主题标签(多标签) + 可自定义状态(持久化) + 标签云/按标签筛选。
"""
import os
import sys
import io
import csv
import json
import zipfile
import time
import mimetypes
import subprocess
import threading
import sqlite3
import secrets
import shutil
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse

# ----------------------------------------------------------------------------
# 配置（兼容 PyInstaller 打包后的运行环境）
# ----------------------------------------------------------------------------
if getattr(sys, "_MEIPASS", None):          # 被 PyInstaller 打包后运行
    BASE_DIR = sys._MEIPASS
    DEFAULT_DB = os.path.join(os.path.expanduser("~"), "file_index_data", "file_index.db")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DEFAULT_DB = os.path.join(BASE_DIR, "file_index.db")
DB_PATH = os.environ.get("FIT_DB", DEFAULT_DB)
HOST = os.environ.get("FIT_HOST", "127.0.0.1")
PORT = int(os.environ.get("FIT_PORT", "8000"))

# 本地访问令牌：默认每次启动随机生成，前端页面会被自动注入该值并在请求中携带。
# 设为空字符串（FIT_TOKEN=""）可关闭校验（仅本地调试时使用）。
SECRET_TOKEN = os.environ.get("FIT_TOKEN", "") or secrets.token_hex(16)
# 写接口请求体大小上限（防止异常大请求），默认 25MB
MAX_BODY = int(os.environ.get("FIT_MAX_BODY_MB", "25")) * 1024 * 1024

# 全文检索：FTS5 + trigram 分词。trigram 以 3 字符为单位建索引，
# 因此搜索词长度 >= 3 时走索引（快），短于 3 个字（如中文双字词）回退 LIKE 保证不漏结果。
# 全文检索默认开启；设 FIT_FTS=0 可关闭（始终用原有 LIKE 检索，行为与加索引前完全一致）
FTS_ENABLED = os.environ.get("FIT_FTS", "1").lower() not in ("0", "false", "no")
FTS_OK = False
FTS_MIN_LEN = 3
# 目录实时监听（watchdog）相关状态
WATCHDOG_ERR = ""

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".jsonl",
    ".csv", ".log", ".html", ".htm", ".css", ".scss", ".xml", ".yaml", ".yml", ".ini",
    ".cfg", ".conf", ".toml", ".srt", ".vtt", ".ass", ".ssa", ".java", ".c", ".cpp",
    ".cc", ".h", ".hpp", ".sh", ".bash", ".bat", ".cmd", ".ps1", ".go", ".rs", ".php",
    ".rb", ".sql", ".r", ".tex", ".rst", ".lua", ".pl", ".swift", ".kt", ".scala",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tiff", ".tif", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".amr", ".aiff", ".opus"}
PDF_EXTS = {".pdf"}

# 默认状态（首次运行写入数据库，之后以数据库为准，可在界面增删改）
DEFAULT_STATUSES = [
    {"label": "未处理", "color": "#9ca3af"},
    {"label": "处理中", "color": "#3b82f6"},
    {"label": "已翻译", "color": "#22c55e"},
    {"label": "已审阅", "color": "#14b8a6"},
    {"label": "重要", "color": "#f59e0b"},
    {"label": "作废", "color": "#ef4444"},
]
CONTENT_INDEX_LIMIT = 200_000
FILE_READ_LIMIT = 5_000_000
PDF_TEXT_LIMIT = 200_000          # 单个 PDF 抽取文字上限（字符）
OCR_LANGS = os.environ.get("FIT_OCR_LANGS", "chi_sim+eng")  # OCR 语言，+ 连接多个
OCR_MAX_BYTES = int(os.environ.get("FIT_OCR_MAX_MB", "30")) * 1024 * 1024
UMI_OCR_URL = os.environ.get("FIT_UMI_OCR_URL", "http://127.0.0.1:1224").rstrip("/")  # Umi-OCR HTTP 接口
OCR_ENGINE = os.environ.get("FIT_OCR_ENGINE", "auto")  # auto（优先 Umi-OCR，否则 tesseract）| umi | tesseract

# ----------------------------------------------------------------------------
# 数据库
# ----------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ----------------------------------------------------------------------------
# 项目 = 文件的“自由组合”（虚拟视图，不固化任何成员关系）
#
#   · 项目只记录一组根目录 roots；成员在查询时按路径前缀实时匹配，
#     因此新建/改名/改根目录后即刻生效，无需重新扫描。
#   · 文件实体在 files 表中始终只有一份，备注/状态/标签/转写天然跨项目复用，
#     项目之间可以任意交叉、嵌套、重叠。
#   · 项目内展示的相对路径与层级，一律以“当前项目的根”为基准实时计算，
#     不落库、不留快照；文件在磁盘上消失即标记为 missing，视图随之变化。
#   · roots 为空数组 = 不限制根目录（即“全部文件”）。
# ----------------------------------------------------------------------------
def norm_root(p):
    """规范化根目录：统一分隔符、去掉结尾分隔符。"""
    if not p:
        return ""
    p = os.path.normpath(p.strip())
    if p in ("/", "\\"):
        return p
    return p.rstrip("/\\")


def root_default_name(root):
    base = os.path.basename(norm_root(root))
    return base or (norm_root(root) or "项目")


def migrate_projects(conn):
    """旧库升级：把此前扫描过的根目录（scan_roots）转成项目，已有文件数据完全不动。"""
    if conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] > 0:
        return
    roots = [r["root"] for r in conn.execute(
        "SELECT root FROM scan_roots ORDER BY added_at").fetchall()]
    now = time.time()
    if not roots:
        # 无根目录记录但已有索引：建一个“全部文件”项目兜底，避免升级后文件无处可看
        if conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]:
            conn.execute("INSERT INTO projects(name,roots,max_depth,note,created_at,last_scan_at)"
                         " VALUES(?,?,?,?,?,?)",
                         ("全部文件", "[]", None, "升级前已索引的文件（不限制目录）", now, None))
        return
    for root in roots:
        r = norm_root(root)
        conn.execute("INSERT INTO projects(name,roots,max_depth,note,created_at,last_scan_at)"
                     " VALUES(?,?,?,?,?,?)",
                     (root_default_name(r), json.dumps([r]), None, "", now, now))
    conn.commit()


def project_roots(conn, pid):
    """项目规范化后的根目录列表。None = 项目不存在；[] = 不限制目录。"""
    if not pid:
        return []                                   # 0 / 空 = 全部文件
    row = conn.execute("SELECT roots FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        return None
    try:
        roots = json.loads(row["roots"] or "[]")
    except Exception:
        roots = []
    return [norm_root(x) for x in roots if x]


def all_project_roots(conn):
    """全部项目根目录的合集（用于“全部文件”视图的展示基准）。"""
    out = []
    for row in conn.execute("SELECT roots FROM projects").fetchall():
        try:
            for x in json.loads(row["roots"] or "[]"):
                x = norm_root(x)
                if x:
                    out.append(x)
        except Exception:
            pass
    return out


def common_parent(roots):
    """一组目录的最长公共父目录。单个目录时取其上一级。"""
    roots = [r for r in roots if r]
    if not roots:
        return None
    parts = [r.split(os.sep) for r in roots]
    if len(parts) == 1:
        return os.sep.join(parts[0][:-1]) or os.sep
    n = min(len(p) for p in parts)
    common = []
    for i in range(n):
        c = parts[0][i]
        if all(p[i] == c for p in parts):
            common.append(c)
        else:
            break
    return os.sep.join(common) or os.sep


def project_scope(conn, pid):
    """返回 (where_sql, params, view_roots)。
    where_sql：成员过滤条件，为空串表示不限目录；
    view_roots：相对路径的展示基准（“全部文件”视图下取公共父目录，
                使来自不同项目的文件各归其位，而不是平铺混在一起）。
    """
    roots = project_roots(conn, pid)
    if roots is None:                               # 项目已不存在 -> 空集合
        return "1=0", [], []
    if roots:
        conds, params = [], []
        for r in roots:
            conds.append("path LIKE ? ESCAPE '\\'")
            params.append(like_escape(r + os.sep) + "%")
        return "(" + " OR ".join(conds) + ")", params, roots
    if pid:
        return "", [], []                           # 项目显式设为“不限制目录”
    virt = common_parent(all_project_roots(conn))
    return "", [], ([virt] if virt else [])


def rel_for(path, roots):
    """按当前项目的根目录实时计算相对路径（统一用 / 分隔）；取最长匹配的根，兼容嵌套项目。"""
    if not roots:
        return None                                  # None = 不覆写，沿用表中既有相对路径
    best = None
    for r in roots:
        base = r.rstrip("/\\")
        if path.startswith(base + os.sep):
            if best is None or len(r) > len(best):
                best = r
    if best is None:
        return os.path.basename(path)
    return os.path.relpath(path, best).replace(os.sep, "/")


def apply_project_view(items, roots):
    """把列表项的相对结构改写为“当前项目视角”。roots 为空时仅统一分隔符。"""
    for it in items:
        rel = rel_for(it.get("path") or "", roots)
        if rel is None:
            rel = (it.get("rel_path") or "").replace("\\", "/")
        it["rel_path"] = rel
        it["parent"] = rel.rsplit("/", 1)[0] if "/" in rel else ""
        it["depth"] = rel.count("/")
    return items


def touch_project(conn, pid):
    """更新项目最后扫描时间。"""
    if pid:
        conn.execute("UPDATE projects SET last_scan_at=? WHERE id=?", (time.time(), pid))


def find_project_by_root(conn, root):
    """按根目录查找项目（优先完全匹配，其次包含该目录的项目），用于扫描时复用同一项目。"""
    r = norm_root(root)
    rows = conn.execute("SELECT id, roots FROM projects").fetchall()
    exact = None
    for row in rows:
        try:
            roots = [norm_root(x) for x in (json.loads(row["roots"] or "[]")) if x]
        except Exception:
            roots = []
        if r in roots:
            exact = row["id"]
            break
    return exact


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE,
            rel_path TEXT,
            name TEXT,
            ext TEXT,
            parent TEXT,
            size INTEGER,
            mtime REAL,
            depth INTEGER,
            status TEXT DEFAULT '未处理',
            note TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            content TEXT DEFAULT '',
            indexed_at REAL
        )
    """)
    # 兼容旧库：补齐 tags 列
    try:
        c.execute("ALTER TABLE files ADD COLUMN tags TEXT DEFAULT ''")
    except Exception:
        pass
    # 兼容旧库：补齐 transcript（转写文字稿）相关列
    try:
        c.execute("ALTER TABLE files ADD COLUMN transcript TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE files ADD COLUMN transcribed_at REAL")
    except Exception:
        pass
    # 兼容旧库：补齐 missing（扫描时标记的“磁盘上已不存在”标记）
    try:
        c.execute("ALTER TABLE files ADD COLUMN missing INTEGER DEFAULT 0")
    except Exception:
        pass
    # 待确认的识别结果：已有旧内容时新结果先进这里，人工确认后才覆盖正式内容
    try:
        c.execute("ALTER TABLE files ADD COLUMN pending_text TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE files ADD COLUMN pending_kind TEXT DEFAULT ''")
    except Exception:
        pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_files_status ON files(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext)")
    # 指纹索引：文件改名/移动后靠 (size, mtime) 把备注/状态/标签/识别结果迁移到新路径
    c.execute("CREATE INDEX IF NOT EXISTS idx_files_fp ON files(size, mtime)")
    c.execute("CREATE TABLE IF NOT EXISTS user_statuses(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT UNIQUE, color TEXT)")
    if conn.execute("SELECT COUNT(*) FROM user_statuses").fetchone()[0] == 0:
        for s in DEFAULT_STATUSES:
            c.execute("INSERT INTO user_statuses(label,color) VALUES(?,?)", (s["label"], s["color"]))
    c.execute("""
        CREATE TABLE IF NOT EXISTS search_tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT DEFAULT '',
            status_filter TEXT DEFAULT 'all',
            ext_filter TEXT DEFAULT '',
            folder_filter TEXT DEFAULT '',
            tag_filter TEXT DEFAULT '',
            created_at REAL,
            last_run REAL,
            result_count INTEGER DEFAULT 0
        )
    """)
    # 兼容旧库：搜索任务记录所属项目（须在建表之后）
    try:
        c.execute("ALTER TABLE search_tasks ADD COLUMN project_id INTEGER DEFAULT 0")
    except Exception:
        pass
    # 记录已扫描过的根目录，供实时监听（watchdog）使用
    c.execute("CREATE TABLE IF NOT EXISTS scan_roots(root TEXT PRIMARY KEY, added_at REAL)")
    # 项目：文件的“自由组合”。本身不复制文件，只记录一组根目录与展示信息。
    c.execute("""
        CREATE TABLE IF NOT EXISTS projects(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            roots TEXT NOT NULL DEFAULT '[]',
            max_depth INTEGER,
            note TEXT DEFAULT '',
            created_at REAL,
            last_scan_at REAL
        )
    """)
    conn.commit()
    migrate_projects(conn)
    init_fts(conn)
    conn.close()

# ----------------------------------------------------------------------------
# 全文检索索引（FTS5 + trigram）
# ----------------------------------------------------------------------------
def init_fts(conn):
    """建立 FTS5 外部内容表与同步触发器；不可用时静默回退到 LIKE 检索。"""
    global FTS_OK
    if not FTS_ENABLED:
        FTS_OK = False
        return
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5("
            "name, note, tags, content, transcript, ext, "
            "content='files', content_rowid='id', tokenize='trigram')")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS files_fts_ai AFTER INSERT ON files BEGIN
                INSERT INTO files_fts(rowid,name,note,tags,content,transcript,ext)
                VALUES(new.id,new.name,new.note,new.tags,new.content,new.transcript,new.ext);
            END""")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS files_fts_ad AFTER DELETE ON files BEGIN
                INSERT INTO files_fts(files_fts,rowid,name,note,tags,content,transcript,ext)
                VALUES('delete',old.id,old.name,old.note,old.tags,old.content,old.transcript,old.ext);
            END""")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS files_fts_au AFTER UPDATE ON files BEGIN
                INSERT INTO files_fts(files_fts,rowid,name,note,tags,content,transcript,ext)
                VALUES('delete',old.id,old.name,old.note,old.tags,old.content,old.transcript,old.ext);
                INSERT INTO files_fts(rowid,name,note,tags,content,transcript,ext)
                VALUES(new.id,new.name,new.note,new.tags,new.content,new.transcript,new.ext);
            END""")
        # 老库首次升级：把已有记录灌进索引
        nf = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        ni = conn.execute("SELECT COUNT(*) FROM files_fts").fetchone()[0]
        if nf and ni == 0:
            conn.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
        conn.commit()
        FTS_OK = True
    except Exception as e:
        FTS_OK = False
        print("提示：FTS5 全文索引不可用，搜索将回退到 LIKE（%s）" % e)

def fts_phrase(term):
    """把用户输入转成 FTS5 短语查询（双引号转义后整体作为字面量）。"""
    t = (term or "").replace('"', '""')
    return '"%s"' % t

def get_statuses():
    conn = get_db()
    rows = conn.execute("SELECT label, color FROM user_statuses ORDER BY id").fetchall()
    conn.close()
    return [{"label": r["label"], "color": r["color"]} for r in rows]

def status_color(label):
    for s in get_statuses():
        if s["label"] == label:
            return s["color"]
    return "#999999"

# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def categorize(ext):
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in TEXT_EXTS:
        return "text"
    return "other"

def like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def parse_tags(s):
    return [t.strip() for t in (s or "").split(",") if t.strip()]

def to_tag_string(v):
    """兼容前端传入的数组或字符串，统一成 'a,b,c' 形式。"""
    if isinstance(v, (list, tuple)):
        v = ",".join(v)
    return ",".join(parse_tags(v or ""))

# ----------------------------------------------------------------------------
# 媒体文字提取：PDF 文本抽取 + 图片 OCR
# ----------------------------------------------------------------------------
def extract_pdf_text(path):
    """抽取 PDF 文字内容（优先 PyMuPDF，回退 pypdf / pdfminer）。无文字层返回空。"""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(path)
        try:
            return "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    except Exception:
        pass
    try:
        from pdfminer.high_level import extract_text as pm_extract
        return pm_extract(path)
    except Exception:
        return ""

def _umi_lang(tesseract_langs):
    """把 tesseract 语言串（chi_sim+eng）映射为 Umi-OCR 语言码（ch_and_en/ch/en）。"""
    s = (tesseract_langs or "").lower()
    has_ch = any(k in s for k in ("chi_sim", "chi_tra", "chinese"))
    has_en = "eng" in s or s == "en"
    if has_ch and has_en:
        return "ch_and_en"
    if has_ch:
        return "ch"
    if has_en:
        return "en"
    return "ch_and_en"

def umi_ocr(path, langs=OCR_LANGS):
    """通过 Umi-OCR 的 HTTP API 识别图片，返回文字；服务不可达/解析失败返回 None。"""
    import urllib.request
    url = UMI_OCR_URL + "/api/ocr"
    payload = json.dumps({
        "base64": False,
        "images": [path],
        "options": {"language": _umi_lang(langs)},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    try:
        texts = []
        for item in data.get("data", []):
            t = item.get("OCR_Text")
            if isinstance(t, list):
                texts.extend([x for x in t if x])
            elif t:
                texts.append(str(t))
        return "\n".join(texts).strip()
    except Exception:
        return None

def tesseract_ocr(path, langs=OCR_LANGS):
    import pytesseract
    from PIL import Image
    img = Image.open(path)
    try:
        img = img.convert("RGB")
    except Exception:
        pass
    return pytesseract.image_to_string(img, lang=langs).strip()

def ocr_image(path, langs=OCR_LANGS):
    """图片 OCR：默认优先 Umi-OCR（中文识别更强），不可用时回退 tesseract。"""
    engine = (OCR_ENGINE or "auto").lower()
    if engine == "tesseract":
        return tesseract_ocr(path, langs)
    if engine == "umi":
        text = umi_ocr(path, langs)
        if text is None:
            raise RuntimeError("Umi-OCR 服务不可达，请确认其 HTTP 接口已启动（默认 %s）" % UMI_OCR_URL)
        return text
    # auto：Umi-OCR 可用则用之，否则 tesseract
    try:
        text = umi_ocr(path, langs)
        if text is not None:
            return text
    except Exception:
        pass
    return tesseract_ocr(path, langs)

# ----------------------------------------------------------------------------
# 指纹追认：文件改名 / 移动 / 所在目录改名 / 根目录改名后，把标注迁到新路径
# ----------------------------------------------------------------------------
def find_orphan(conn, new_path, name, size, mtime):
    """为新发现的路径找回旧记录。

    指纹 = (size, mtime)。命中且旧路径在磁盘上已不存在，才认为是同一个文件
    （被改名或移走了）。返回旧记录 id，无法确定时返回 None。
    """
    rows = conn.execute(
        "SELECT id,path,name FROM files WHERE size=? AND mtime=? AND path<>? LIMIT 6",
        (size, mtime, new_path)).fetchall()
    if not rows:
        return None
    if len(rows) > 5:
        return None                                  # 大量同指纹文件，不猜
    # 只看那些在磁盘上已经消失的候选：仍存在的那些是副本/另存为，不能搬走它们的标注
    gone = []
    for r in rows:
        try:
            if not os.path.exists(r["path"]):
                gone.append(r)
        except Exception:
            pass
    if not gone:
        return None                                  # 原文件都还在，说明这是副本，不是搬过来的
    same_name = [r for r in gone if r["name"] == name]
    if len(same_name) == 1:
        return same_name[0]["id"]                    # 目录改名/移动
    if len(gone) == 1:
        return gone[0]["id"]                         # 文件改名/移动
    return None                                      # 多个候选且无法区分，不猜


def store_recognized(conn, fid, text, kind):
    """保存识别结果（OCR / 转写）。

    已有正式内容时：新结果只进暂存，等人工确认/修改后才覆盖（不直接冲掉旧内容）。
    尚无正式内容时：直接作为正式内容，省去无意义的确认。
    返回 True 表示进了暂存，False 表示直接写入正式内容。
    """
    text = (text or "").strip()
    if not text:
        return False
    row = conn.execute("SELECT transcript FROM files WHERE id=?", (fid,)).fetchone()
    if row and (row["transcript"] or "").strip():
        conn.execute("UPDATE files SET pending_text=?, pending_kind=? WHERE id=?",
                     (text, kind, fid))
        conn.commit()
        return True
    conn.execute("UPDATE files SET transcript=?, transcribed_at=? WHERE id=?",
                 (text, time.time(), fid))
    conn.commit()
    return False


def update_project_roots_moved(conn, src, dest):
    """目录被改名/移动后，同步更新以它作为根目录的项目（子目录移动不影响项目设置）。"""
    src_n, dest_n = norm_root(src), norm_root(dest)
    changed = 0
    for r in conn.execute("SELECT id, roots FROM projects").fetchall():
        try:
            roots = json.loads(r["roots"] or "[]")
        except Exception:
            continue
        new_roots, hit = [], False
        for x in roots:
            if norm_root(x) == src_n:
                new_roots.append(dest_n)
                hit = True
            else:
                new_roots.append(x)
        if hit:
            conn.execute("UPDATE projects SET roots=? WHERE id=?",
                         (json.dumps(new_roots), r["id"]))
            changed += 1
    if changed:
        conn.execute("DELETE FROM scan_roots WHERE root=?", (src_n,))
        conn.execute("INSERT OR REPLACE INTO scan_roots(root,added_at) VALUES(?,?)",
                     (dest_n, time.time()))
        conn.commit()
    return changed


def relocate_records(conn, old_prefix, new_prefix):
    """目录改名/移动：把该目录下所有记录的路径前缀整体重写，标注原样保留。"""
    old_prefix = old_prefix.rstrip(os.sep) + os.sep
    new_prefix = new_prefix.rstrip(os.sep) + os.sep
    rows = conn.execute(
        "SELECT id,path FROM files WHERE path=? OR path LIKE ? ESCAPE '\\'",
        (old_prefix.rstrip(os.sep),
         like_escape(old_prefix) + "%")).fetchall()
    n = 0
    for r in rows:
        tail = r["path"][len(old_prefix):] if r["path"].startswith(old_prefix) else ""
        new_path = new_prefix + tail
        if new_path == r["path"]:
            continue
        # 目标已存在则跳过，避免违反 path 唯一约束
        if conn.execute("SELECT 1 FROM files WHERE path=?", (new_path,)).fetchone():
            continue
        conn.execute("UPDATE files SET path=? WHERE id=?", (new_path, r["id"]))
        n += 1
    conn.commit()
    return n


# ----------------------------------------------------------------------------
# 扫描（后台线程 + 进度）
# ----------------------------------------------------------------------------
scan_state = {}

def scan_worker(root, max_depth, scan_id, project_id=None):
    conn = get_db()
    try:
        count = 0
        # 先把本根目录下已有记录标记为“待复核”，扫描中命中即清零；
        # 扫描结束后仍为 1 的即为磁盘上已删除/移走的残留记录（保留其备注，供人工清理）。
        conn.execute("UPDATE files SET missing=1 WHERE path LIKE ?", (root.rstrip("/") + "/%",))
        conn.commit()
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            if max_depth is not None and depth >= max_depth:
                dirnames[:] = []
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(full)
                    if not os.path.isfile(full):
                        continue
                except Exception:
                    continue
                # 增量：大小与 mtime 均未变化则跳过抽取与写入，完整保留备注/状态/标签/转写/识别结果
                existing = conn.execute(
                    "SELECT size, mtime FROM files WHERE path=?", (full,)).fetchone()
                if existing and existing["size"] == st.st_size and existing["mtime"] == st.st_mtime:
                    conn.execute("UPDATE files SET missing=0 WHERE path=?", (full,))
                    count += 1
                    if count % 200 == 0:
                        conn.commit()
                        scan_state[scan_id]["processed"] = count
                    continue
                rel = os.path.relpath(full, root)
                ext = os.path.splitext(fn)[1].lower()
                parent = os.path.dirname(rel)
                # 指纹追认：路径是新的，但可能是改名/移动来的老文件 —— 把整行搬到新路径，
                # 备注/状态/标签/识别结果随之保留（改行不改标注，无需复制+删除）
                moved_id = None
                if not existing:
                    moved_id = find_orphan(conn, full, fn, st.st_size, st.st_mtime)
                # 仅对文本/PDF 抽取可搜索文字；图片 OCR、音视频转写为按需，重扫不覆盖
                content = ""
                is_text = ext in TEXT_EXTS
                is_pdf = ext in PDF_EXTS
                if is_text and st.st_size <= FILE_READ_LIMIT:
                    try:
                        with open(full, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read(CONTENT_INDEX_LIMIT)
                    except Exception:
                        content = ""
                elif is_pdf and st.st_size <= FILE_READ_LIMIT:
                    try:
                        content = extract_pdf_text(full)[:PDF_TEXT_LIMIT]
                    except Exception:
                        content = ""
                if moved_id:
                    # 老文件换了位置/名字：整行迁移，标注零丢失
                    conn.execute(
                        """UPDATE files SET path=?,rel_path=?,name=?,ext=?,parent=?,size=?,mtime=?,
                           depth=?,indexed_at=?,missing=0 WHERE id=?""",
                        (full, rel, fn, ext, parent, st.st_size, st.st_mtime, depth,
                         time.time(), moved_id),
                    )
                    if is_text or is_pdf:
                        conn.execute("UPDATE files SET content=? WHERE id=?", (content, moved_id))
                else:
                    # 入库：先插入（已存在则忽略），再更新可变元数据，避免冲掉备注/状态/转写/识别结果
                    conn.execute(
                        """INSERT OR IGNORE INTO files(path, rel_path, name, ext, parent, size, mtime, depth, status, note, tags, content, indexed_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (full, rel, fn, ext, parent, st.st_size, st.st_mtime, depth,
                         "未处理", "", "", "", time.time()),
                    )
                    conn.execute(
                        """UPDATE files SET rel_path=?,name=?,ext=?,parent=?,size=?,mtime=?,depth=?,indexed_at=?, missing=0
                           WHERE path=?""",
                        (rel, fn, ext, parent, st.st_size, st.st_mtime, depth, time.time(), full),
                    )
                    if is_text or is_pdf:
                        conn.execute("UPDATE files SET content=? WHERE path=?", (content, full))
                count += 1
                if count % 200 == 0:
                    conn.commit()
                    scan_state[scan_id]["processed"] = count
        touch_project(conn, project_id)
        conn.commit()
        scan_state[scan_id]["processed"] = count
        scan_state[scan_id]["done"] = True
    except Exception as e:
        scan_state[scan_id]["error"] = str(e)
        scan_state[scan_id]["done"] = True
    finally:
        conn.close()

# ----------------------------------------------------------------------------
# 打开文件夹
# ----------------------------------------------------------------------------
def open_folder(path):
    if not os.path.exists(path):
        raise HTTPException(404, "文件不存在")
    try:
        if sys.platform == "win32":
            subprocess.run(f'explorer /select,"{path}"', shell=True)
        elif sys.platform == "darwin":
            subprocess.run(["open", "-R", path])
        else:
            subprocess.run(["xdg-open", os.path.dirname(path)])
    except Exception as e:
        raise HTTPException(500, f"打开失败: {e}")
    return {"ok": True}

# ----------------------------------------------------------------------------
# 导出打包
# ----------------------------------------------------------------------------
def build_export(ids, sidecar):
    conn = get_db()
    qmarks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM files WHERE id IN ({qmarks})", ids).fetchall()
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    manifest = []
    for r in rows:
        rel = r["rel_path"]
        # 安全：跳过含路径穿越或绝对路径的异常记录，避免写出预期目录之外
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            continue
        entry = {
            "id": r["id"], "name": r["name"], "rel_path": rel,
            "ext": r["ext"], "size": r["size"], "status": r["status"],
            "note": r["note"], "tags": parse_tags(r["tags"]),
            "transcript": r["transcript"] or "",
            "content": (r["content"] or "")[:50000],
        }
        if os.path.isfile(r["path"]):
            z.write(r["path"], rel)
        if sidecar and os.path.isfile(r["path"]):
            base, name = os.path.split(rel)
            scname = ("." + name + ".note.json") if name else (rel + ".note.json")
            scpath = os.path.join(base, scname) if base else scname
            z.writestr(scpath, json.dumps(entry, ensure_ascii=False, indent=2))
        manifest.append(entry)
    z.writestr("manifest.json", json.dumps(
        {"exported_at": time.time(), "total": len(manifest), "items": manifest},
        ensure_ascii=False, indent=2))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["rel_path", "name", "ext", "size", "status", "tags", "note", "transcript", "content"])
    for m in manifest:
        w.writerow([m["rel_path"], m["name"], m["ext"], m["size"], m["status"], ",".join(m["tags"]), m["note"], m["transcript"], m["content"]])
    z.writestr("manifest.csv", out.getvalue())
    z.close()
    buf.seek(0)
    conn.close()
    return buf

# ----------------------------------------------------------------------------
# FastAPI 应用
# ----------------------------------------------------------------------------
app = FastAPI(title="文件索引与管理工具")

# ----------------------------------------------------------------------------
# 安全中间件：本地令牌校验 + 写接口请求体大小限制
# 仅对 /api/ 路径生效；页面本身（/）不受限，由后端在返回时注入令牌。
# ----------------------------------------------------------------------------
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        # 写接口限制请求体大小，避免异常大请求拖垮本地进程
        if request.method in ("POST", "PUT", "PATCH"):
            cl = request.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > MAX_BODY:
                return JSONResponse(status_code=413, content={"error": "请求体过大"})
        # 本地令牌校验：前端自动携带；未设置 SECRET_TOKEN（FIT_TOKEN=""）则放行
        if SECRET_TOKEN:
            tok = request.headers.get("X-Fit-Token") or request.query_params.get("token") or ""
            if tok != SECRET_TOKEN:
                return JSONResponse(status_code=401, content={"error": "未授权：缺少或错误的访问令牌"})
    return await call_next(request)

def _build_where(search, status, ext, folder, tag, force_like=False, scope=None,
                 include_missing=False):
    """构造查询条件。返回 (where_sql, params, used_fts)。
    搜索词长度 >= 3 且 FTS 可用时走全文索引；否则（含中文双字词）回退 LIKE。
    scope=(sql, params) 为项目范围条件（按根目录实时匹配）。
    include_missing=False 时排除磁盘上已不存在的记录，使视图反映当前真实结构。
    """
    conds, params = [], []
    used_fts = False
    if scope and scope[0]:
        conds.append(scope[0])
        params += list(scope[1])
    if not include_missing:
        conds.append("(missing IS NULL OR missing=0)")
    if search:
        if FTS_OK and not force_like and len(search.strip()) >= FTS_MIN_LEN:
            conds.append("id IN (SELECT rowid FROM files_fts WHERE files_fts MATCH ?)")
            params.append(fts_phrase(search))
            used_fts = True
        else:
            q = "%" + like_escape(search) + "%"
            conds.append("(name LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' OR ext LIKE ? ESCAPE '\\' OR transcript LIKE ? ESCAPE '\\')")
            params += [q, q, q, q, q, q]
    if status and status != "all":
        conds.append("status=?")
        params.append(status)
    if ext:
        conds.append("ext=?")
        params.append(ext.lower())
    if folder:
        conds.append("(parent=? OR rel_path LIKE ? ESCAPE '\\')")
        params += [folder, folder + "/%"]
    if tag:
        q = "%" + like_escape(tag) + "%"
        conds.append("tags LIKE ? ESCAPE '\\'")
        params.append(q)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params, used_fts

SELECT_COLS = ("id,path,rel_path,name,ext,parent,size,mtime,depth,status,note,tags,"
               "transcript,transcribed_at,pending_kind")

@app.get("/")
def index():
    p = os.path.join(BASE_DIR, "index.html")
    try:
        with open(p, "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("FIT_TOKEN_PLACEHOLDER", SECRET_TOKEN)
        return HTMLResponse(html)
    except Exception:
        # 兜底：直接返回静态文件（此时前端拿不到令牌，仅本地调试用）
        return FileResponse(p)

# ---------- 状态（可自定义） ----------
@app.get("/api/statuses")
def api_statuses():
    return {"statuses": get_statuses()}

@app.put("/api/statuses")
async def set_statuses(request: Request):
    data = await request.json()
    lst = data.get("statuses", [])
    cleaned = []
    for s in lst:
        label = (s.get("label") or "").strip()
        if label:
            cleaned.append({"label": label, "color": s.get("color") or "#999999"})
    if not cleaned:
        raise HTTPException(400, "至少需要一个状态")
    conn = get_db()
    conn.execute("DELETE FROM user_statuses")
    for s in cleaned:
        conn.execute("INSERT INTO user_statuses(label,color) VALUES(?,?)", (s["label"], s["color"]))
    conn.commit()
    conn.close()
    return {"ok": True, "statuses": get_statuses()}

# ---------- 标签云 ----------
@app.get("/api/tags")
def get_tags(project: int = 0):
    conn = get_db()
    scope = project_scope(conn, project)
    w = "WHERE 1=1" + ((" AND " + scope[0]) if scope[0] else "")
    rows = conn.execute(
        f"SELECT tags FROM files {w} AND tags IS NOT NULL AND tags<>''",
        list(scope[1])).fetchall()
    conn.close()
    cnt = Counter()
    for r in rows:
        for t in parse_tags(r["tags"]):
            cnt[t] += 1
    return {"tags": [{"tag": t, "count": c} for t, c in cnt.most_common()]}

# ---------- 目录浏览（供根目录点击选择） ----------
@app.get("/api/dirs")
def list_dirs(path: str = ""):
    """列出指定目录下的子目录。path 为空时列出可用根目录（Windows 盘符 / Unix 顶层）。"""
    def _mk(name, p):
        return {"name": name, "path": p}

    if not path:
        if os.name == "nt":
            import string
            roots = [_mk(d + ":", d + ":\\") for d in string.ascii_uppercase
                     if os.path.isdir(d + ":\\")]
            return {"current": "", "parent": None, "dirs": roots, "is_root": True}
        tops = []
        for name in sorted(os.listdir("/")):
            fp = os.path.join("/", name)
            try:
                if os.path.isdir(fp) and not os.path.islink(fp):
                    tops.append(_mk(name, fp))
            except Exception:
                continue
        return {"current": "/", "parent": None, "dirs": tops, "is_root": True}

    p = os.path.abspath(path)
    if not os.path.isdir(p):
        raise HTTPException(400, "目录不存在")
    parent = os.path.dirname(p)
    if not parent or parent == p:
        parent = None
    entries = []
    try:
        for name in sorted(os.listdir(p)):
            fp = os.path.join(p, name)
            try:
                if os.path.isdir(fp) and not os.path.islink(fp):
                    entries.append(_mk(name, fp))
            except Exception:
                continue
    except PermissionError:
        raise HTTPException(403, "无权限访问该目录")
    return {"current": p, "parent": parent, "dirs": entries, "is_root": False}


@app.get("/api/dirs/quick")
def quick_dirs():
    """常用位置快捷入口（主目录 / 桌面 / 文档 / 下载）。"""
    out = []
    home = os.path.expanduser("~")
    if os.path.isdir(home):
        out.append({"name": "用户主目录", "path": home})
    for nm, label in (("Desktop", "桌面"), ("Documents", "文档"),
                      ("Downloads", "下载"), ("Pictures", "图片")):
        p = os.path.join(home, nm)
        if os.path.isdir(p):
            out.append({"name": label, "path": p})
    if os.name != "nt" and os.path.isdir("/media"):
        out.append({"name": "/media", "path": "/media"})
    return {"quick": out}


# ---------- 项目（文件的自由组合 / 虚拟视图） ----------
@app.get("/api/projects")
def list_projects():
    """项目列表。文件数按根目录实时统计（不入库），所以新建或改目录后立刻反映。"""
    conn = get_db()
    rows = conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        try:
            roots = [norm_root(x) for x in (json.loads(r["roots"] or "[]")) if x]
        except Exception:
            roots = []
        conds, params = [], []
        for x in roots:
            conds.append("path LIKE ? ESCAPE '\\'")
            params.append(like_escape(x + os.sep) + "%")
        w = ("WHERE " + " OR ".join(conds)) if conds else ""
        total = conn.execute(f"SELECT COUNT(*) FROM files {w}", params).fetchone()[0]
        wm = (w + " AND missing=1") if w else "WHERE missing=1"
        missing = conn.execute(f"SELECT COUNT(*) FROM files {wm}", params).fetchone()[0]
        # 根目录是否还在（被改名/移动后会失效，可一键重新定位）
        roots_info = []
        for x in roots:
            try:
                alive = os.path.isdir(x)
            except Exception:
                alive = False
            roots_info.append({"path": x, "exists": alive})
        out.append({
            "id": r["id"], "name": r["name"], "roots": roots,
            "roots_info": roots_info,
            "max_depth": r["max_depth"], "note": r["note"] or "",
            "created_at": r["created_at"], "last_scan_at": r["last_scan_at"],
            "file_count": total, "missing_count": missing,
        })
    conn.close()
    return {"projects": out}


@app.post("/api/projects/{pid}/relocate")
async def relocate_project_root(pid: int, request: Request):
    """项目根目录被改名/移动后重新定位：把旧根下的记录整体迁到新根，标注全部保留。"""
    data = await request.json()
    old = norm_root((data.get("old") or "").strip())
    new = norm_root((data.get("new") or "").strip())
    if not old or not new:
        raise HTTPException(400, "缺少原路径或新路径")
    if not os.path.isdir(new):
        raise HTTPException(400, "目标目录不存在：" + new)
    if old == new:
        return {"ok": True, "moved": 0}
    conn = get_db()
    try:
        moved = relocate_records(conn, old, new)
        update_project_roots_moved(conn, old, new)
    finally:
        conn.close()
    return {"ok": True, "moved": moved, "old": old, "new": new}


@app.post("/api/projects")
async def create_project(request: Request):
    """新建项目。只需指定根目录，成员即时可见（已索引的文件无需重扫）。"""
    data = await request.json()
    roots = [norm_root(x) for x in (data.get("roots") or []) if str(x).strip()]
    name = (data.get("name") or "").strip() or (root_default_name(roots[0]) if roots else "新项目")
    note = (data.get("note") or "").strip()
    md = data.get("max_depth")
    max_depth = None if md in (None, "", -1) else int(md)
    now = time.time()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO projects(name,roots,max_depth,note,created_at) VALUES(?,?,?,?,?)",
        (name, json.dumps(roots), max_depth, note, now))
    pid = cur.lastrowid
    for r in roots:
        conn.execute("INSERT OR REPLACE INTO scan_roots(root,added_at) VALUES(?,?)", (r, now))
    conn.commit()
    conn.close()
    watch_add_roots(roots)
    return {"id": pid, "name": name, "roots": roots}


@app.put("/api/projects/{pid}")
async def update_project(pid: int, request: Request):
    """改名 / 改备注 / 改根目录。改动立即生效，不影响任何文件实体。"""
    data = await request.json()
    conn = get_db()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "项目不存在")
    name = (data.get("name") or row["name"]).strip() or row["name"]
    note = data.get("note")
    if note is None:
        note = row["note"] or ""
    if data.get("roots") is not None:
        roots = [norm_root(x) for x in data["roots"] if str(x).strip()]
    else:
        try:
            roots = [norm_root(x) for x in (json.loads(row["roots"] or "[]")) if x]
        except Exception:
            roots = []
    md = data.get("max_depth", row["max_depth"])
    max_depth = None if md in (None, "", -1) else int(md)
    conn.execute("UPDATE projects SET name=?,roots=?,max_depth=?,note=? WHERE id=?",
                 (name, json.dumps(roots), max_depth, note, pid))
    for r in roots:
        conn.execute("INSERT OR REPLACE INTO scan_roots(root,added_at) VALUES(?,?)", (r, time.time()))
    conn.commit()
    conn.close()
    watch_add_roots(roots)
    return {"ok": True, "id": pid}


@app.delete("/api/projects/{pid}")
def delete_project(pid: int):
    """只删除项目这个视图；文件实体与其备注/状态/标签一律保留。"""
    conn = get_db()
    conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/projects/{pid}/scan")
def scan_project(pid: int):
    """按项目当前设置的根目录重新扫描。同一项目反复扫描只刷新内容，不会堆出新项目。"""
    conn = get_db()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "项目不存在")
    try:
        roots = [norm_root(x) for x in (json.loads(row["roots"] or "[]")) if x]
    except Exception:
        roots = []
    md = row["max_depth"]
    conn.close()
    if not roots:
        raise HTTPException(400, "该项目未设置根目录，无法扫描")
    sids = []
    for i, r in enumerate(roots):
        if not os.path.isdir(r):
            continue
        c = get_db()
        c.execute("INSERT OR REPLACE INTO scan_roots(root,added_at) VALUES(?,?)", (r, time.time()))
        touch_project(c, pid)
        c.commit()
        c.close()
        sid = "%d_%d" % (int(time.time() * 1000), i)
        scan_state[sid] = {"processed": 0, "done": False, "error": None,
                           "root": r, "project_id": pid}
        threading.Thread(target=scan_worker, args=(r, md, sid, pid), daemon=True).start()
        sids.append(sid)
    watch_add_roots(roots)
    if not sids:
        raise HTTPException(400, "项目的根目录均不存在或无法访问")
    return {"scan_ids": sids, "roots": roots}


# ---------- 扫描 ----------
@app.post("/api/scan")
async def start_scan(request: Request):
    data = await request.json()
    root = (data.get("root") or "").strip()
    md = data.get("max_depth")
    if not root or not os.path.isdir(root):
        raise HTTPException(400, "根目录不存在或无法访问")
    if md in (None, "", -1):
        max_depth = None
    else:
        try:
            max_depth = int(md)
        except Exception:
            max_depth = None
    # 记录扫描过的根目录，供实时监听复用
    r_norm = norm_root(root)
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO scan_roots(root, added_at) VALUES(?,?)",
                 (r_norm, time.time()))
    # 归入项目：显式指定 > 按根目录复用已有项目 > 自动新建项目
    pid = int(data.get("project_id") or 0)
    if not pid:
        pid = find_project_by_root(conn, r_norm) or 0
    if not pid:
        cur = conn.execute(
            "INSERT INTO projects(name,roots,max_depth,note,created_at,last_scan_at)"
            " VALUES(?,?,?,?,?,?)",
            (root_default_name(r_norm), json.dumps([r_norm]), max_depth, "",
             time.time(), time.time()))
        pid = cur.lastrowid
    conn.commit()
    conn.close()
    sid = str(int(time.time() * 1000))
    scan_state[sid] = {"processed": 0, "done": False, "error": None,
                       "root": root, "project_id": pid}
    t = threading.Thread(target=scan_worker, args=(root, max_depth, sid, pid), daemon=True)
    t.start()
    return {"scan_id": sid, "project_id": pid}

@app.get("/api/scan/{sid}")
def scan_progress(sid: str):
    s = scan_state.get(sid)
    if not s:
        raise HTTPException(404, "无此扫描任务")
    return {"processed": s["processed"], "done": s["done"], "error": s["error"], "root": s["root"]}

# ---------- 统计 ----------
@app.get("/api/stats")
def stats(project: int = 0):
    conn = get_db()
    scope = project_scope(conn, project)
    base = "WHERE 1=1" + ((" AND " + scope[0]) if scope[0] else "")
    sp = list(scope[1])
    total = conn.execute(f"SELECT COUNT(*) FROM files {base}", sp).fetchone()[0]
    by_status = {}
    for r in conn.execute(f"SELECT status, COUNT(*) c FROM files {base} GROUP BY status", sp):
        by_status[r[0]] = r[1]
    tagged = conn.execute(
        f"SELECT COUNT(*) FROM files {base} AND tags IS NOT NULL AND tags<>''", sp).fetchone()[0]
    transcribed = conn.execute(
        f"SELECT COUNT(*) FROM files {base} AND transcript IS NOT NULL AND transcript<>''"
        " AND transcript NOT LIKE '[转写失败]%'", sp).fetchone()[0]
    img_set = tuple(IMAGE_EXTS)
    ocr_count = 0
    for r in conn.execute(
            f"SELECT ext, transcript FROM files {base} AND transcript IS NOT NULL AND transcript<>''", sp):
        t = r["transcript"]
        if r["ext"] in img_set and not (t.startswith("[识别失败]") or t.startswith("[转写失败]")):
            ocr_count += 1
    by_ext = []
    for r in conn.execute(f"SELECT ext, COUNT(*) c FROM files {base} GROUP BY ext ORDER BY c DESC LIMIT 15", sp):
        by_ext.append({"ext": r[0] or "(无扩展名)", "count": r[1]})
    missing = conn.execute(f"SELECT COUNT(*) FROM files {base} AND missing=1", sp).fetchone()[0]
    conn.close()
    return {"total": total, "by_status": by_status, "tagged": tagged,
            "transcribed": transcribed, "ocr_count": ocr_count, "by_ext": by_ext,
            "missing": missing}

# ---------- 文件夹树 ----------
@app.get("/api/tree")
def get_tree(depth: int = 99, project: int = 0):
    conn = get_db()
    scope = project_scope(conn, project)
    roots = scope[2]
    w = ("WHERE (missing IS NULL OR missing=0)" +
         ((" AND " + scope[0]) if scope[0] else ""))
    folders = {}
    for r in conn.execute(f"SELECT path, rel_path FROM files {w}", list(scope[1])).fetchall():
        rel = rel_for(r["path"], roots)
        if rel is None:
            rel = (r["rel_path"] or "").replace("\\", "/")
        p = rel.rsplit("/", 1)[0] if "/" in rel else ""
        folders[p] = folders.get(p, 0) + 1
    if "" not in folders and folders:
        folders[""] = 0                             # 保证存在“（根目录）”入口，可点回全部
    cum = {}
    for fpath in list(folders.keys()):
        c = 0
        for other, cnt in folders.items():
            if other == fpath or other.startswith(fpath + "/"):
                c += cnt
        cum[fpath] = c
    result = []
    for fpath, cnt in cum.items():
        d = 0 if fpath == "" else fpath.count("/") + 1
        if d <= depth:
            name = "（根目录）" if fpath == "" else fpath.split("/")[-1]
            result.append({"path": fpath, "name": name, "depth": d, "count": cnt})
    result.sort(key=lambda x: (x["depth"], x["path"]))
    conn.close()
    return {"folders": result}

# ---------- 文件清单 ----------
@app.get("/api/files")
def list_files(search: str = "", status: str = "all", ext: str = "", folder: str = "",
               tag: str = "", page: int = 1, page_size: int = 100, sort: str = "name",
               project: int = 0, show_missing: int = 0):
    conn = get_db()
    allowed = {"name", "size", "mtime", "status", "depth"}
    order = sort if sort in allowed else "name"
    offset = max(0, (page - 1) * page_size)
    scope = project_scope(conn, project)
    inc_missing = bool(show_missing)

    def _run(force_like):
        w, p, u = _build_where(search, status, ext, folder, tag,
                               force_like=force_like, scope=scope,
                               include_missing=inc_missing)
        t = conn.execute(f"SELECT COUNT(*) FROM files {w}", p).fetchone()[0]
        rr = conn.execute(
            f"SELECT {SELECT_COLS} FROM files {w} ORDER BY {order} LIMIT ? OFFSET ?",
            p + [page_size, offset]).fetchall()
        return t, rr, u

    try:
        total, rows, used_fts = _run(False)
    except Exception:
        # FTS 查询异常（特殊字符等）时回退 LIKE，保证搜索始终可用
        total, rows, used_fts = _run(True)
    items = apply_project_view([dict(r) for r in rows], scope[2])
    conn.close()
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "fts": bool(used_fts)}

# ---------- 文件详情 ----------
@app.post("/api/file/{fid}/text")
async def resolve_pending(fid: int, request: Request):
    """处理待确认的识别结果。
    action=accept   把暂存内容（可用 text 覆盖，即边改边采用）写入正式内容
    action=discard  丢弃暂存内容，旧内容原样保留
    """
    data = await request.json()
    action = (data.get("action") or "").strip()
    conn = get_db()
    try:
        r = conn.execute("SELECT id,pending_text,transcript FROM files WHERE id=?", (fid,)).fetchone()
        if not r:
            raise HTTPException(404, "未找到文件")
        if action == "accept":
            text = data.get("text")
            if text is None:
                text = r["pending_text"] or ""
            text = (text or "").strip()
            if not text:
                raise HTTPException(400, "暂存内容为空，无可采用的内容")
            conn.execute(
                "UPDATE files SET transcript=?, transcribed_at=?, pending_text='', pending_kind='' "
                "WHERE id=?", (text, time.time(), fid))
        elif action == "discard":
            conn.execute("UPDATE files SET pending_text='', pending_kind='' WHERE id=?", (fid,))
        else:
            raise HTTPException(400, "未知操作：" + action)
        conn.commit()
        row = conn.execute("SELECT transcript,pending_text,pending_kind FROM files WHERE id=?",
                           (fid,)).fetchone()
    finally:
        conn.close()
    return {"ok": True, "transcript": row["transcript"] or "",
            "pending_text": row["pending_text"] or "", "pending_kind": row["pending_kind"] or ""}


@app.get("/api/file/{fid}")
def file_detail(fid: int):
    conn = get_db()
    r = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, "未找到文件")
    d = dict(r)
    cat = categorize(d["ext"])
    d["category"] = cat
    d["raw_url"] = f"/api/raw/{fid}"
    d["preview_text"] = (d["content"] or "")[:100000] if cat == "text" else ""
    d["tags"] = parse_tags(d["tags"])
    d["exists"] = os.path.isfile(d["path"])
    d["missing"] = bool(d.get("missing") or 0)
    d["color"] = status_color(d["status"])
    d["transcript"] = d.get("transcript") or ""
    d["transcribed_at"] = d.get("transcribed_at") or None
    d["pending_text"] = d.get("pending_text") or ""
    d["pending_kind"] = d.get("pending_kind") or ""
    d["can_transcribe"] = (d["category"] in ("audio", "video")) and bool(d["exists"])
    d["can_ocr"] = (d["category"] == "image") and bool(d["exists"])
    d["ocr_langs"] = OCR_LANGS
    d["pdf_text"] = (d.get("content") or "") if d["category"] == "pdf" else ""
    conn.close()
    return d

@app.get("/api/raw/{fid}")
def raw_file(fid: int):
    conn = get_db()
    r = conn.execute("SELECT path, name FROM files WHERE id=?", (fid,)).fetchone()
    conn.close()
    if not r or not os.path.isfile(r["path"]):
        raise HTTPException(404, "文件缺失或已被删除")
    mt, _ = mimetypes.guess_type(r["path"])
    return FileResponse(r["path"], media_type=mt or "application/octet-stream",
                        filename=r["name"], content_disposition_type="inline")

@app.put("/api/file/{fid}")
async def update_file(fid: int, request: Request):
    data = await request.json()
    note = data.get("note", "")
    status = data.get("status", "未处理")
    tags = to_tag_string(data.get("tags", ""))
    conn = get_db()
    conn.execute("UPDATE files SET note=?, status=?, tags=? WHERE id=?", (note, status, tags, fid))
    conn.commit()
    conn.close()
    return {"ok": True}

# ---------- 批量编辑（状态 / 标签 / 备注） ----------
@app.post("/api/batch")
async def batch_update(request: Request):
    """批量修改选中文件的状态、标签、备注。
    tags_mode: replace（覆盖）| append（追加）| remove（删除指定标签）
    note_mode: replace（覆盖）| append（追加到原备注之后）
    status / tags / note 传空表示不改动该项。
    """
    data = await request.json()
    ids = [int(i) for i in (data.get("ids") or []) if str(i).strip().isdigit()]
    if not ids:
        raise HTTPException(400, "未选择任何文件")
    status = (data.get("status") or "").strip()
    tags_val = to_tag_string(data.get("tags", ""))
    tags_mode = (data.get("tags_mode") or "replace").lower()
    note_val = data.get("note")
    note_mode = (data.get("note_mode") or "replace").lower()

    conn = get_db()
    qmarks = ",".join("?" * len(ids))
    # 状态
    if status:
        conn.execute(f"UPDATE files SET status=? WHERE id IN ({qmarks})", [status] + ids)
    # 标签
    if tags_mode == "append" and tags_val:
        add = parse_tags(tags_val)
        for fid in ids:
            r = conn.execute("SELECT tags FROM files WHERE id=?", (fid,)).fetchone()
            cur = parse_tags(r["tags"]) if r else []
            merged = list(dict.fromkeys(cur + add))
            conn.execute("UPDATE files SET tags=? WHERE id=?", (",".join(merged), fid))
    elif tags_mode == "remove" and tags_val:
        rm = set(parse_tags(tags_val))
        for fid in ids:
            r = conn.execute("SELECT tags FROM files WHERE id=?", (fid,)).fetchone()
            cur = parse_tags(r["tags"]) if r else []
            conn.execute("UPDATE files SET tags=? WHERE id=?",
                         (",".join([t for t in cur if t not in rm]), fid))
    elif tags_mode == "replace" and tags_val:
        conn.execute(f"UPDATE files SET tags=? WHERE id IN ({qmarks})", [tags_val] + ids)
    # 备注
    if note_mode == "append" and note_val:
        for fid in ids:
            r = conn.execute("SELECT note FROM files WHERE id=?", (fid,)).fetchone()
            cur = (r["note"] or "") if r else ""
            conn.execute("UPDATE files SET note=? WHERE id=?",
                         ((cur + "\n" + note_val).strip(), fid))
    elif note_mode == "replace" and note_val is not None:
        conn.execute(f"UPDATE files SET note=? WHERE id IN ({qmarks})", [note_val] + ids)
    conn.commit()
    conn.close()
    return {"ok": True, "updated": len(ids)}

# ---------- 打开文件夹 ----------
@app.post("/api/open")
async def open_file(request: Request):
    data = await request.json()
    path = data.get("path", "")
    # 安全：仅允许打开“已索引”的文件，避免被借接口打开任意路径
    conn = get_db()
    row = conn.execute("SELECT 1 FROM files WHERE path=?", (path,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(403, "只能打开已被本工具索引的文件")
    return open_folder(path)

# ---------- 导出 ----------
@app.post("/api/export")
async def export_files(request: Request):
    data = await request.json()
    ids = data.get("ids", [])
    sidecar = bool(data.get("sidecar", False))
    name = (data.get("name") or "file_index_export").strip() or "file_index_export"
    if not ids:
        raise HTTPException(400, "未选择任何文件")
    buf = build_export(ids, sidecar)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})

# ---------- 报告导出（CSV / Markdown） ----------
@app.post("/api/report")
async def export_report(request: Request):
    """把选中文件的元数据导出为 CSV 或 Markdown 报告（不含原始文件内容）。"""
    data = await request.json()
    ids = [int(i) for i in (data.get("ids") or []) if str(i).strip().isdigit()]
    fmt = (data.get("format") or "csv").lower()
    if not ids:
        raise HTTPException(400, "未选择任何文件")
    conn = get_db()
    qmarks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM files WHERE id IN ({qmarks})", ids).fetchall()
    conn.close()

    if fmt in ("md", "markdown"):
        lines = ["# 文件索引报告", "",
                 "导出时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"), "",
                 "共 %d 个文件。" % len(rows), ""]
        for r in rows:
            lines.append("## %s" % (r["name"] or ""))
            lines.append("- 路径：`%s`" % (r["rel_path"] or ""))
            lines.append("- 状态：%s" % (r["status"] or ""))
            tg = parse_tags(r["tags"])
            if tg:
                lines.append("- 标签：%s" % "、".join(tg))
            if r["note"]:
                lines.append("- 备注：%s" % r["note"])
            tr = (r["transcript"] or "").strip()
            if tr:
                lines.append("- 转写/识别：%s" % tr[:1000])
            lines.append("")
        body = "\n".join(lines).encode("utf-8")
        return StreamingResponse(io.BytesIO(body), media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="file_report.md"'})
    # CSV（带 BOM，Excel 直接打开不乱码）
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rel_path", "name", "ext", "size", "status", "tags", "note", "transcript", "content"])
    for r in rows:
        w.writerow([r["rel_path"], r["name"], r["ext"], r["size"], r["status"], r["tags"],
                    r["note"], (r["transcript"] or "")[:2000], (r["content"] or "")[:2000]])
    body = buf.getvalue().encode("utf-8-sig")
    return StreamingResponse(io.BytesIO(body), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="file_report.csv"'})

# ---------- 搜索任务清单 ----------
def run_task(tid):
    conn = get_db()
    t = conn.execute("SELECT * FROM search_tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        conn.close()
        raise HTTPException(404, "任务不存在")
    scope = project_scope(conn, t["project_id"] or 0)

    def _run(force_like):
        w, p, u = _build_where(t["query"], t["status_filter"], t["ext_filter"],
                               t["folder_filter"], t["tag_filter"],
                               force_like=force_like, scope=scope)
        tt = conn.execute(f"SELECT COUNT(*) FROM files {w}", p).fetchone()[0]
        rr = conn.execute(f"SELECT {SELECT_COLS} FROM files {w} ORDER BY name LIMIT 500", p).fetchall()
        return tt, rr, u

    try:
        total, rows, used_fts = _run(False)
    except Exception:
        total, rows, used_fts = _run(True)
    rows = apply_project_view([dict(r) for r in rows], scope[2])
    conn.execute("UPDATE search_tasks SET last_run=?, result_count=? WHERE id=?",
                 (time.time(), total, tid))
    conn.commit()
    task = dict(conn.execute("SELECT * FROM search_tasks WHERE id=?", (tid,)).fetchone())
    conn.close()
    return {"task": task, "items": rows}

@app.post("/api/tasks")
async def create_task(request: Request):
    data = await request.json()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO search_tasks(query,status_filter,ext_filter,folder_filter,tag_filter,"
        "project_id,created_at,last_run,result_count) VALUES(?,?,?,?,?,?,?,?,?)",
        (data.get("query", ""), data.get("status_filter", "all"),
         data.get("ext_filter", ""), data.get("folder_filter", ""),
         data.get("tag_filter", ""), int(data.get("project_id") or 0),
         time.time(), time.time(), 0))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return run_task(tid)

@app.get("/api/tasks")
def list_tasks():
    conn = get_db()
    rows = conn.execute("SELECT * FROM search_tasks ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"tasks": [dict(r) for r in rows]}

@app.post("/api/tasks/{tid}/retry")
def retry_task(tid: int):
    return run_task(tid)

# ----------------------------------------------------------------------------
# 目录实时监听（watchdog）：文件增删改自动更新索引
# ----------------------------------------------------------------------------
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_OK = True
except Exception as e:                       # 未安装 watchdog 时降级但不影响其它功能
    Observer = None
    FileSystemEventHandler = object
    WATCHDOG_OK = False
    WATCHDOG_ERR = str(e)

WATCH_STATE = {"enabled": False, "roots": [], "error": ""}
_observer = None


def watch_add_roots(roots):
    """监听已开启时，把新的根目录追加进监听（无需停止再启动）。"""
    global _observer
    if not WATCH_STATE["enabled"] or _observer is None or not WATCHDOG_OK:
        return
    cur = list(WATCH_STATE["roots"])
    changed = False
    for r in roots:
        if r and os.path.isdir(r) and r not in cur:
            try:
                _observer.schedule(_WatchHandler(), r, recursive=True)
                cur.append(r)
                changed = True
            except Exception:
                pass
    if changed:
        WATCH_STATE["roots"] = cur


def _guess_root(path):
    """根据已记录的扫描根目录，推断某个文件属于哪个根。"""
    conn = get_db()
    rows = conn.execute("SELECT root FROM scan_roots").fetchall()
    conn.close()
    best = None
    for r in rows:
        root = r["root"]
        if path.startswith(root.rstrip("/") + "/"):
            if best is None or len(root) > len(best):
                best = root
    return best


def _index_single(path):
    """对单个文件重新索引：不存在则标记缺失；内容未变则跳过。"""
    conn = get_db()
    try:
        if not os.path.isfile(path):
            conn.execute("UPDATE files SET missing=1 WHERE path=?", (path,))
            conn.commit()
            return
        st = os.lstat(path)
        fn = os.path.basename(path)
        ext = os.path.splitext(fn)[1].lower()
        row = conn.execute("SELECT id,size,mtime FROM files WHERE path=?", (path,)).fetchone()
        if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
            return                                   # 未变化
        if row:
            r2 = conn.execute("SELECT rel_path,parent,depth FROM files WHERE id=?", (row["id"],)).fetchone()
            rel, parent, depth = r2["rel_path"], r2["parent"], r2["depth"]
            # 文件可能刚被移动/改名，按它现在所属的根目录重算相对结构
            g = _guess_root(path)
            if g:
                _r = os.path.relpath(path, g)
                rel, parent, depth = _r, os.path.dirname(_r), _r.count(os.sep)
        else:
            root = _guess_root(path)
            if not root:
                return                               # 不属于任何已扫描根目录，忽略
            rel = os.path.relpath(path, root)
            parent = os.path.dirname(rel)
            depth = rel.count(os.sep)
        content = ""
        if ext in TEXT_EXTS and st.st_size <= FILE_READ_LIMIT:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read(CONTENT_INDEX_LIMIT)
            except Exception:
                content = ""
        elif ext in PDF_EXTS and st.st_size <= FILE_READ_LIMIT:
            try:
                content = extract_pdf_text(path)[:PDF_TEXT_LIMIT]
            except Exception:
                content = ""
        conn.execute(
            """INSERT OR IGNORE INTO files(path,rel_path,name,ext,parent,size,mtime,depth,status,note,tags,content,indexed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (path, rel, fn, ext, parent, st.st_size, st.st_mtime, depth,
             "未处理", "", "", content, time.time()))
        conn.execute(
            """UPDATE files SET rel_path=?,name=?,ext=?,parent=?,size=?,mtime=?,depth=?,indexed_at=?,missing=0
               WHERE path=?""",
            (rel, fn, ext, parent, st.st_size, st.st_mtime, depth, time.time(), path))
        if ext in TEXT_EXTS or ext in PDF_EXTS:
            conn.execute("UPDATE files SET content=? WHERE path=?", (content, path))
        conn.commit()
    finally:
        conn.close()


class _WatchHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            _index_single(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            _index_single(event.src_path)

    def on_deleted(self, event):
        if event.is_directory:
            return
        conn = get_db()
        conn.execute("UPDATE files SET missing=1 WHERE path=? OR path LIKE ?",
                     (event.src_path, event.src_path + "/%"))
        conn.commit()
        conn.close()

    def on_moved(self, event):
        if event.is_directory:
            # 目录改名/移动：把该目录下所有记录的路径前缀整体重写，
            # 子文件的备注/状态/标签/识别结果一行不丢
            conn = get_db()
            try:
                n = relocate_records(conn, event.src_path, event.dest_path)
                update_project_roots_moved(conn, event.src_path, event.dest_path)
            finally:
                conn.close()
            if n:
                WATCH_STATE["last_move"] = {
                    "kind": "dir", "from": event.src_path, "to": event.dest_path,
                    "count": n, "at": time.time()}
            return
        conn = get_db()
        conn.execute("UPDATE files SET path=?, name=?, missing=0 WHERE path=?",
                     (event.dest_path, os.path.basename(event.dest_path), event.src_path))
        conn.commit()
        conn.close()
        _index_single(event.dest_path)


@app.get("/api/watch")
def watch_status():
    return {"enabled": bool(WATCH_STATE["enabled"]), "roots": WATCH_STATE["roots"],
            "error": WATCH_STATE["error"], "watchdog_installed": WATCHDOG_OK,
            "watchdog_error": WATCHDOG_ERR}


@app.post("/api/watch")
async def watch_control(request: Request):
    """开启/关闭目录实时监听。start 时可传 roots，省略则用已扫描过的根目录。"""
    global _observer
    data = await request.json()
    action = (data.get("action") or "").lower()

    if action == "stop":
        if _observer is not None:
            try:
                _observer.stop()
                _observer.join(timeout=3)
            except Exception:
                pass
            _observer = None
        WATCH_STATE.update({"enabled": False, "error": ""})
        return {"enabled": False, "roots": WATCH_STATE["roots"], "error": ""}

    if action != "start":
        raise HTTPException(400, "action 需为 start 或 stop")
    if not WATCHDOG_OK:
        WATCH_STATE["error"] = "未安装 watchdog，请执行 pip install watchdog"
        raise HTTPException(409, WATCH_STATE["error"])

    roots = data.get("roots")
    if not roots:
        conn = get_db()
        roots = [r["root"] for r in conn.execute(
            "SELECT root FROM scan_roots ORDER BY added_at DESC").fetchall()]
        conn.close()
    roots = [r for r in roots if r and os.path.isdir(r)]
    if not roots:
        raise HTTPException(400, "没有可监听的目录，请先扫描一次")

    if _observer is not None:
        try:
            _observer.stop()
            _observer.join(timeout=2)
        except Exception:
            pass
    _observer = Observer()
    for rt in roots:
        _observer.schedule(_WatchHandler(), rt, recursive=True)
    _observer.start()
    WATCH_STATE.update({"enabled": True, "roots": roots, "error": ""})
    return {"enabled": True, "roots": roots, "error": ""}


# ----------------------------------------------------------------------------
# 转录（语音 / 视频 -> 文字，基于 faster-whisper）
# ----------------------------------------------------------------------------
WHISPER_AVAIL = None          # None=尚未检测, True/False=是否已安装
WHISPER_ERR = ""
WHISPER_MODEL = None
WHISPER_LOCK = threading.Lock()
WHISPER_MODEL_NAME = os.environ.get("FIT_WHISPER_MODEL", "base")
WHISPER_LANG = os.environ.get("FIT_WHISPER_LANG", "") or None   # None=自动检测语种
TRANSCRIBE_MAX_BYTES = int(os.environ.get("FIT_TRANSCRIBE_MAX_MB", "300")) * 1024 * 1024

def whisper_check():
    global WHISPER_AVAIL, WHISPER_ERR
    if WHISPER_AVAIL is not None:
        return WHISPER_AVAIL, WHISPER_ERR
    try:
        import faster_whisper  # noqa
        WHISPER_AVAIL = True
    except Exception as e:
        WHISPER_AVAIL = False
        WHISPER_ERR = str(e)
    return WHISPER_AVAIL, WHISPER_ERR

def get_whisper_model():
    global WHISPER_MODEL
    with WHISPER_LOCK:
        if WHISPER_MODEL is not None:
            return WHISPER_MODEL
        from faster_whisper import WhisperModel
        use_cuda = False
        try:
            import torch
            use_cuda = torch.cuda.is_available()
        except Exception:
            use_cuda = False
        device = "cuda" if use_cuda else "cpu"
        compute = "float16" if use_cuda else "int8"
        WHISPER_MODEL = WhisperModel(WHISPER_MODEL_NAME, device=device, compute_type=compute)
        return WHISPER_MODEL

def transcribe_path(path):
    """对音频/视频文件转写，返回文字。视频由 faster-whisper 经 PyAV 直接读取音轨。"""
    model = get_whisper_model()
    try:
        segments, _info = model.transcribe(path, language=WHISPER_LANG, beam_size=5)
        return "".join(s.text for s in segments).strip()
    except Exception as e:
        # 视频若直接读取失败，尝试用 ffmpeg 抽音轨后再试一次
        if shutil.which("ffmpeg"):
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            try:
                subprocess.run(["ffmpeg", "-y", "-i", path, "-vn", tmp.name],
                               check=True, capture_output=True)
                segments, _ = model.transcribe(tmp.name, language=WHISPER_LANG, beam_size=5)
                return "".join(s.text for s in segments).strip()
            finally:
                try:
                    os.remove(tmp.name)
                except Exception:
                    pass
        raise

transcribe_jobs = {}

def transcribe_worker(ids, jid):
    conn = get_db()
    try:
        total = len(ids)
        done = 0
        for fid in ids:
            transcribe_jobs[jid]["current"] = ""
            r = conn.execute("SELECT id,path,ext,name FROM files WHERE id=?", (fid,)).fetchone()
            if not r:
                done += 1
                transcribe_jobs[jid]["done"] = done
                continue
            ext = r["ext"]; name = r["name"]; path = r["path"]
            transcribe_jobs[jid]["current"] = name
            if categorize(ext) not in ("audio", "video"):
                done += 1
                transcribe_jobs[jid]["done"] = done
                continue
            if not os.path.isfile(path):
                done += 1
                transcribe_jobs[jid]["done"] = done
                continue
            try:
                if os.path.getsize(path) > TRANSCRIBE_MAX_BYTES:
                    raise Exception("文件过大已跳过（>%dMB）" % (TRANSCRIBE_MAX_BYTES // 1024 // 1024))
                text = transcribe_path(path)
                if store_recognized(conn, fid, text, "transcribe"):
                    transcribe_jobs[jid]["pending"] = transcribe_jobs[jid].get("pending", 0) + 1
            except Exception as e:
                # 失败时不写入，保留原有内容（绝不把旧结果冲掉）
                transcribe_jobs[jid].setdefault("errors", []).append(
                    {"name": name, "error": str(e)})
            done += 1
            transcribe_jobs[jid]["done"] = done
        transcribe_jobs[jid]["finished"] = True
    except Exception as e:
        transcribe_jobs[jid]["error"] = str(e)
        transcribe_jobs[jid]["finished"] = True
    finally:
        conn.close()

@app.get("/api/transcribe/check")
def transcribe_check():
    avail, err = whisper_check()
    return {"available": avail, "model": WHISPER_MODEL_NAME,
            "lang": WHISPER_LANG or "auto", "error": err}

@app.post("/api/transcribe")
async def start_transcribe(request: Request):
    data = await request.json()
    ids = data.get("ids", [])
    if not ids:
        raise HTTPException(400, "未选择任何文件")
    avail, err = whisper_check()
    if not avail:
        raise HTTPException(409, "未安装转录依赖 faster-whisper：" + err)
    jid = str(int(time.time() * 1000))
    transcribe_jobs[jid] = {"total": len(ids), "done": 0, "current": "", "error": None, "finished": False}
    t = threading.Thread(target=transcribe_worker, args=(ids, jid), daemon=True)
    t.start()
    return {"job_id": jid}

@app.get("/api/transcribe/{jid}")
def transcribe_progress(jid: str):
    j = transcribe_jobs.get(jid)
    if not j:
        raise HTTPException(404, "无此转写任务")
    return j

# ----------------------------------------------------------------------------
# OCR（图片文字识别：Umi-OCR 优先 + tesseract 兜底）
# ----------------------------------------------------------------------------
OCR_AVAIL = None
OCR_ERR = ""
OCR_LANG_LIST = []

def ocr_check():
    global OCR_AVAIL, OCR_ERR, OCR_LANG_LIST
    if OCR_AVAIL is not None:
        return OCR_AVAIL, OCR_ERR, OCR_LANG_LIST
    langs = []
    try:
        import pytesseract
        try:
            langs = pytesseract.get_languages(config="") or []
        except Exception:
            out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
            for line in (out.stderr + out.stdout).splitlines():
                line = line.strip()
                if line and "available languages" not in line and "Tesseract" not in line:
                    langs.append(line)
        OCR_LANG_LIST = langs
        OCR_AVAIL = True
    except Exception as e:
        OCR_AVAIL = False
        OCR_ERR = str(e)
    return OCR_AVAIL, OCR_ERR, OCR_LANG_LIST

def umi_reachable():
    import urllib.request
    try:
        urllib.request.urlopen(UMI_OCR_URL, timeout=2)
        return True
    except Exception:
        return False

ocr_jobs = {}

def ocr_worker(ids, jid):
    conn = get_db()
    try:
        total = len(ids); done = 0
        for fid in ids:
            ocr_jobs[jid]["current"] = ""
            r = conn.execute("SELECT id,path,ext,name FROM files WHERE id=?", (fid,)).fetchone()
            if not r:
                done += 1; ocr_jobs[jid]["done"] = done; continue
            ext = r["ext"]; name = r["name"]; path = r["path"]
            ocr_jobs[jid]["current"] = name
            if categorize(ext) != "image":
                done += 1; ocr_jobs[jid]["done"] = done; continue
            if not os.path.isfile(path):
                done += 1; ocr_jobs[jid]["done"] = done; continue
            try:
                if os.path.getsize(path) > OCR_MAX_BYTES:
                    raise Exception("图片过大已跳过（>%dMB）" % (OCR_MAX_BYTES // 1024 // 1024))
                text = ocr_image(path)
                if not (text or "").strip():
                    raise Exception("未识别出文字")
                if store_recognized(conn, fid, text, "ocr"):
                    ocr_jobs[jid]["pending"] = ocr_jobs[jid].get("pending", 0) + 1
            except Exception as e:
                # 失败时不写入，保留原有内容（绝不把旧结果冲掉）
                ocr_jobs[jid].setdefault("errors", []).append(
                    {"name": name, "error": str(e)})
            done += 1
            ocr_jobs[jid]["done"] = done
        ocr_jobs[jid]["finished"] = True
    except Exception as e:
        ocr_jobs[jid]["error"] = str(e)
        ocr_jobs[jid]["finished"] = True
    finally:
        conn.close()

@app.get("/api/ocr/check")
def ocr_check_ep():
    avail, err, langs = ocr_check()
    return {"available": avail, "langs": langs, "ocr_langs": OCR_LANGS,
            "engine": OCR_ENGINE, "umi_url": UMI_OCR_URL,
            "umi_reachable": umi_reachable(), "error": err}

@app.post("/api/ocr")
async def start_ocr(request: Request):
    data = await request.json()
    ids = data.get("ids", [])
    if not ids:
        raise HTTPException(400, "未选择任何文件")
    avail, err, _ = ocr_check()
    if not avail:
        raise HTTPException(409, "未安装 OCR 依赖（tesseract + pytesseract）：" + err)
    jid = str(int(time.time() * 1000))
    ocr_jobs[jid] = {"total": len(ids), "done": 0, "current": "", "error": None, "finished": False}
    t = threading.Thread(target=ocr_worker, args=(ids, jid), daemon=True)
    t.start()
    return {"job_id": jid}

@app.get("/api/ocr/{jid}")
def ocr_progress(jid: str):
    j = ocr_jobs.get(jid)
    if not j:
        raise HTTPException(404, "无此识别任务")
    return j

# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    import webbrowser
    init_db()
    url = f"http://{HOST}:{PORT}"
    print(f"文件索引工具已启动: {url}")
    # 启动后自动打开浏览器（打包成 exe 时更友好；无桌面环境静默忽略）
    def _open():
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Timer(1.2, _open).start()
    uvicorn.run(app, host=HOST, port=PORT)
