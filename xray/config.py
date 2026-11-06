#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件索引与管理工具 —— 后端服务 (FastAPI)
运行：pip install fastapi uvicorn  →  python backend.py
默认感知 http://127.0.0.1:8000
环境变量：FIT_DB(数据库路径) / FIT_HOST / FIT_PORT
说明：本进程运行在用户本机，拥有完整文件系统权限；浏览器仅负责界面与发请求。

增强：主题标签(多标签) + 可自定义状态(持久化) + 标签云/按标签筛选。
"""
import os
import sys
# 注意：以下标准库模块经 `from xray.config import *` 供 scan/browse/ocr/transcribe/tasks
# 共用（例如 tasks 用 io/zipfile/csv、ocr 用 mimetypes、transcribe 用 shutil），不是未使用导入。
import io
import csv
import json
import zipfile
import time
import mimetypes
import subprocess
import threading
import sqlite3
import tempfile
import secrets
import shutil
from collections import Counter
import re
import logging
from logging.handlers import RotatingFileHandler

from fastapi import APIRouter, Request, HTTPException

router = APIRouter()

# ----------------------------------------------------------------------------
# 配置（兼容 PyInstaller 打包后的运行环境）
# ----------------------------------------------------------------------------
if getattr(sys, "_MEIPASS", None):          # 被 PyInstaller 打包后运行
    BASE_DIR = sys._MEIPASS
else:
    # 源码运行：本文件位于 xray/ 子包内，项目根目录为其父目录（backend.py / web/ 所在处）
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _resolve_db_path():
    """自动挑选一个可写的数据库路径，全程零配置。

    源码运行：沿用程序目录下的 file_index.db（开发体验与旧版一致）。
    打包后优先级（环境变量 FIT_DB 可强制覆盖）：
      1) 用户目录 ~/file_index_data/file_index.db
      2) 可执行文件旁边的 file_index_data/file_index.db
      3) 系统临时目录下的 file_xray/file_index.db
    返回 (path, where) —— where 用于启动时打印实际落盘位置。
    """
    forced = os.environ.get("FIT_DB")
    if forced:
        return forced, forced
    if not getattr(sys, "_MEIPASS", None):     # 源码运行：行为与旧版一致
        return os.path.join(BASE_DIR, "file_index.db"), "程序目录"
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates = [
        (os.path.join(os.path.expanduser("~"), "file_index_data", "file_index.db"),
         "用户目录"),
        (os.path.join(exe_dir, "file_index_data", "file_index.db"),
         "程序所在目录"),
        (os.path.join(tempfile.gettempdir(), "file_xray", "file_index.db"),
         "系统临时目录"),
    ]
    for path, where in candidates:
        d = os.path.dirname(path)
        try:
            os.makedirs(d, exist_ok=True)
            # 用追加模式试写，确认目录真的可写
            with open(path, "a"):
                pass
            return path, where
        except Exception:
            continue
    # 兜底：理论上到不了
    return candidates[-1][0], candidates[-1][1]


DB_PATH, DB_WHERE = _resolve_db_path()
HOST = os.environ.get("FIT_HOST", "127.0.0.1")
PORT = int(os.environ.get("FIT_PORT", "8000"))

# P5：配置文件外置。位置与 DB 同级（源码运行即项目根目录；打包后随用户数据目录）。
# 优先级：环境变量 > config.json > 代码默认。采用“下次启动生效”，避免运行中改
# 配置导致各模块已导入的常量与内存态不一致。
CONFIG_FILE = os.path.join(os.path.dirname(DB_PATH), "config.json")
_FILE_CFG = {}


def load_settings():
    """读取 config.json（不存在/损坏/非对象一律回落空配置，绝不影响启动）。"""
    global _FILE_CFG
    try:
        # utf-8-sig：兼容 Windows 记事本/PowerShell 写出的带 BOM 文件
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        _FILE_CFG = data if isinstance(data, dict) else {}
    except Exception:
        _FILE_CFG = {}
    return _FILE_CFG


def _setting(env_key, cfg_key, default, cast=str):
    """env > config.json > default。空字符串视作未配置。"""
    try:
        if os.environ.get(env_key, "") != "":
            return cast(os.environ[env_key])
    except Exception:
        pass
    val = _FILE_CFG.get(cfg_key)
    if val not in (None, ""):
        try:
            return cast(val)
        except Exception:
            pass
    return default


load_settings()
setting = _setting    # 供兄弟模块（经 `from xray.config import *`）使用，下划线名不会导出

# 本地访问令牌：优先用环境变量 FIT_TOKEN；否则持久化到用户目录（跨重启不变，
# 重启后旧链接/书签仍然有效）；首次启动自动生成。写失败（只读环境等）则退回每次随机。
# 设为空字符串（FIT_TOKEN=""）可关闭校验（仅本地调试时使用）。
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".file_xray_token")
SECRET_TOKEN = os.environ.get("FIT_TOKEN", "")
if not SECRET_TOKEN:
    try:
        if os.path.isfile(TOKEN_FILE):
            with open(TOKEN_FILE, "r", encoding="utf-8") as _f:
                SECRET_TOKEN = _f.read().strip()
    except Exception:
        pass
if not SECRET_TOKEN:
    SECRET_TOKEN = secrets.token_hex(16)
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as _f:
            _f.write(SECRET_TOKEN)
    except Exception:
        pass
# 写接口请求体大小上限（防止异常大请求），默认 25MB
MAX_BODY = int(_setting("FIT_MAX_BODY_MB", "max_body_mb", "25")) * 1024 * 1024

# 全文检索：FTS5 + trigram 分词。trigram 以 3 字符为单位建索引，
# 因此搜索词长度 >= 3 时走索引（快），短于 3 个字（如中文双字词）回退 LIKE 保证不漏结果。
# 全文检索默认开启；设 FIT_FTS=0 可关闭（始终用原有 LIKE 检索，行为与加索引前完全一致）
FTS_ENABLED = _setting("FIT_FTS", "fts_enabled", "1")
FTS_ENABLED = str(FTS_ENABLED).lower() not in ("0", "false", "no")
FTS_OK = False
FTS_MIN_LEN = 3
# 搜索结果“定位到具体位置”：片段/命中数在服务端生成，绝不把 20 万字的 content 整段发前端。
# 标记用控制字符做定界符（正文几乎不可能自带），前端先转义 HTML 再替换成 <mark>，避免 XSS。
SNIPPET_CTX = 60              # 命中词前后各保留的上下文字符数
SNIPPET_MAX = 20              # 单行最多生成多少个“不同位置”的片段（前端默认展示前 3 个，其余按需展开）
SNIPPET_SHOW = 3              # 列表默认展示的片段数
HIT_COUNT_LIMIT = 2000        # 单字段命中计数上限（超大文本保护；超出显示为 N+）
SNIPPET_MAX_ROWS = 200        # 单页最多为多少行生成片段（防止异常的 page_size 拖慢接口）
SNIPPET_MARK_START = "\u0001"
SNIPPET_MARK_END = "\u0002"
# 目录实时感知（watchdog）相关状态
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
DOC_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".wps", ".et",
            ".dps", ".odt", ".ods", ".odp"}
# 由抽取器注册表原生支持（stdlib zipfile + XML）的三种 Office Open XML 格式。
# 老格式 .doc/.xls/.ppt（二进制 OLE）明确不在支持范围，仍按普通二进制文件入库。
OFFICE_EXTS = {".docx", ".xlsx", ".pptx"}
# 类型过滤预设：每个类别对应一组扩展名（前端下拉直接选用）
CATEGORY_EXTS = {
    "image": IMAGE_EXTS, "video": VIDEO_EXTS, "audio": AUDIO_EXTS,
    "pdf": PDF_EXTS, "doc": DOC_EXTS, "text": TEXT_EXTS,
}
# 内容必然为二进制的扩展名（嗅探可跳过，省一次 IO）
KNOWN_BIN_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS | PDF_EXTS | DOC_EXTS

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
# Office 文档抽取上限：与正文上限对齐，并按 ZIP 成员/文件大小设体积防御
OFFICE_TEXT_LIMIT = 200_000       # docx/xlsx/pptx 抽取正文总字符上限
OFFICE_MAX_BYTES = 50 * 1024 * 1024       # 单个 office 文件（压缩包）大小上限
OFFICE_MAX_ENTRY_BYTES = 200 * 1024 * 1024  # 单个 ZIP 成员解压后大小上限（防 ZIP 炸弹）
XLSX_MAX_ROWS_PER_SHEET = 5000    # 单个工作表最多抽取的行数
XLSX_MAX_COL_GAP = 100            # 稀疏表补空列的最大数量（避免一行几万个制表符）
OCR_LANGS = _setting("FIT_OCR_LANGS", "ocr_langs", "chi_sim+eng")  # OCR 语言，+ 连接多个
OCR_MAX_BYTES = int(_setting("FIT_OCR_MAX_MB", "ocr_max_mb", "30")) * 1024 * 1024
UMI_OCR_URL = _setting("FIT_UMI_OCR_URL", "umi_ocr_url", "http://127.0.0.1:1224").rstrip("/")  # Umi-OCR HTTP 接口
OCR_ENGINE = _setting("FIT_OCR_ENGINE", "ocr_engine", "auto")  # auto（优先 Umi-OCR，否则 tesseract）| umi | tesseract
# 自动拉起 Umi-OCR（已安装但未运行时按需启动一次）：UMI_OCR_PATH 指定可执行文件，
# 留空则按常见路径探测；FIT_UMI_OCR_AUTOSTART=0 可关闭自动拉起。
UMI_OCR_PATH = _setting("FIT_UMI_OCR_PATH", "umi_ocr_path", "")
UMI_OCR_AUTOSTART = _setting("FIT_UMI_OCR_AUTOSTART", "umi_ocr_autostart", "1")
# 扫描件/无文字层 PDF 是否在扫描时自动 OCR（需 OCR 引擎可用）。设 0/false 可关闭以免拖慢扫描。
OCR_PDF_ENABLED = str(_setting("FIT_OCR_PDF", "ocr_pdf_enabled", "1")).lower() not in ("0", "false", "no")
# 单个 PDF 的 OCR 页数上限（超出部分不识别，避免超大扫描件拖死任务）。0 = 不限制。
PDF_OCR_MAX_PAGES = max(0, int(_setting("FIT_PDF_OCR_PAGES", "pdf_ocr_max_pages", "200")))

# P3：日志基础设施——落盘到 DB 同级 logs/xray.log，便于打包版（无控制台）排查问题。
# 逐文件失败用 DEBUG（默认不写入），扫描级失败才 ERROR；RotatingFileHandler 防止无限增长。
LOG_DIR = os.path.join(os.path.dirname(DB_PATH), "logs")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:
    LOG_DIR = tempfile.gettempdir()
_log = logging.getLogger("xray")
if not _log.handlers:
    _log.setLevel(str(_setting("FIT_LOG_LEVEL", "log_level", "INFO")).upper())
    _h = RotatingFileHandler(os.path.join(LOG_DIR, "xray.log"),
                             maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _log.addHandler(_h)
_log.propagate = False

# P2：排除规则——扫描时跳过命中路径；默认空规则 = 零行为变化。
EXCLUDES = []   # 内存中的 pattern 列表，启动时由 load_excludes 从 DB 载入

# 内置默认规则：只播种一次（标记存在 app_meta.excludes_seeded），用户删掉后不再自动恢复。
# 只收低风险项（几乎不可能承载用户自己的有用文件）；dist / build / target / vendor 等
# 可能装真实产物的目录刻意不内置，避免“搜索不到自己的文件”。
DEFAULT_EXCLUDES = [
    "**/.git/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/.idea/**",
]
SEEDED_FLAG = "excludes_seeded"


def _glob_to_regex(pattern: str) -> "re.Pattern":
    """简化 glob -> 正则：
    **  ->  .*            （跨目录吞路径）
    *   ->  [^/]*          （单层，不跨 /）
    ?   ->  [^/]
    其余字符原样 re.escape。fnmatch 不支持 ** 跨目录语义，故自实现。
    """
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                if i < len(pattern) and pattern[i] in ("/", "\\"):   # 吞掉紧随斜杠，使 **/ 与 /** 跨目录
                    i += 1
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^(?:" + "".join(out) + ")$")


def is_excluded(path: str) -> bool:
    """路径是否命中任一排除规则：含 */? 按 glob 整路径匹配，否则按路径包含匹配；大小写不敏感。"""
    norm = path.replace("\\", "/").lower()
    for rule in EXCLUDES:
        r = (rule or "").strip()
        if not r:
            continue
        if "*" in r or "?" in r:
            if _glob_to_regex(r).match(norm):
                return True
            # 约定：`dir/**` 同时命中目录本身。否则只跳过目录里的内容，
            # `.git` / `node_modules` 这类目录节点仍会被单独入库（也会白走一遍子树）。
            if r.endswith("/**") and _glob_to_regex(r[:-3]).match(norm):
                return True
        elif r.replace("\\", "/").lower() in norm:
            return True
    return False


def load_excludes(conn=None):
    """从 app_excludes 表载入内存中的 EXCLUDES 列表（新增/删除规则后调用以热更新）。"""
    global EXCLUDES
    close = False
    if conn is None:
        conn = get_db()
        close = True
    try:
        rows = conn.execute("SELECT pattern FROM app_excludes ORDER BY id").fetchall()
        EXCLUDES = [r["pattern"] for r in rows]
    finally:
        if close:
            conn.close()


def _rec_skip(scan_id, path, reason):
    """P3：记录扫描中被跳过/未取到内容的文件（上限 200 条，避免内存膨胀）。"""
    stt = scan_state.get(scan_id)
    if stt is not None:
        sk = stt.setdefault("skipped", [])
        if len(sk) < 200:
            sk.append({"path": path, "reason": reason})


def _rec_error(scan_id, path, error):
    """P3：记录扫描级/单文件级异常（上限 200 条）。"""
    stt = scan_state.get(scan_id)
    if stt is not None:
        er = stt.setdefault("errors", [])
        if len(er) < 200:
            er.append({"path": path, "error": str(error)})

# ----------------------------------------------------------------------------
# 数据库
# ----------------------------------------------------------------------------
def get_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.isdir(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except Exception as e:
            raise RuntimeError(
                f"无法创建数据库目录 {db_dir}，请检查权限或设置环境变量 "
                f"FIT_DB 指定可写路径：{e}"
            )
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # P1：并发安全姿势——开启 WAL + 适度 busy_timeout，避免大目录扫描时读接口被写锁阻塞。
    # 网络映射盘可能不支持 WAL，失败时回落到默认回滚日志模式（行为不退化，不报错）。
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        except Exception:
            pass
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
    # 公共父目录无意义（不同盘符/无交集时只剩分隔符）则不设虚拟根，
    # 沿用各文件入库时的相对路径，避免整棵树/清单被拍平成纯文件名
    if virt and virt not in ("/", "\\"):
        return "", [], [virt]
    return "", [], []


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
    # PDF 文字来源：text_layer（文字层精确抽取）/ ocr（OCR 识别，建议校对）/ manual（人工校对）
    try:
        c.execute("ALTER TABLE files ADD COLUMN content_source TEXT DEFAULT ''")
    except Exception:
        pass
    # 正文是否因抽取上限被截断（目前由 xlsx/docx/pptx 设置；老记录默认 0）
    try:
        c.execute("ALTER TABLE files ADD COLUMN content_truncated INTEGER DEFAULT 0")
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
    # 物化目录：把目录也作为 files 表中的一等实体（is_dir=1），空目录也会成为节点/可标注
    try:
        c.execute("ALTER TABLE files ADD COLUMN is_dir INTEGER DEFAULT 0")
    except Exception:
        pass
    # 逐文件提示词：记录该文件最后一次转写使用的提示词（空=不覆盖全局默认；off=关闭）。
    # 按既有迁移方式：先 PRAGMA 检测缺失列，再 ALTER，旧库数据完全不动。
    try:
        _fcols = {r[1] for r in c.execute("PRAGMA table_info(files)")}
        if "prompt" not in _fcols:
            c.execute("ALTER TABLE files ADD COLUMN prompt TEXT DEFAULT ''")
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
    # 记录已扫描过的根目录，供实时感知（watchdog）使用
    c.execute("CREATE TABLE IF NOT EXISTS scan_roots(root TEXT PRIMARY KEY, added_at REAL)")
    # P2：排除规则（新增表，不改动 files 结构）。扫描时跳过命中路径，存量清理走独立接口。
    c.execute("""
        CREATE TABLE IF NOT EXISTS app_excludes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern TEXT UNIQUE NOT NULL,
            added_at REAL,
            builtin INTEGER NOT NULL DEFAULT 0
        )
    """)
    # 旧库升级：没有 builtin 列就安全补列；存量规则一律按“用户自建”对待（不覆盖、不重排）
    try:
        _cols = {r[1] for r in c.execute("PRAGMA table_info(app_excludes)")}
        if "builtin" not in _cols:
            c.execute("ALTER TABLE app_excludes ADD COLUMN builtin INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    # 一次性播种标记单独存放：避免“用户删光规则”被误判成“从未播种过”
    c.execute("CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT)")
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
    # P4：后台任务（扫描 / 转写 / OCR）持久化，供重启后续查与“上次中断”提示。
    # 内存态仍是快路径，DB 仅做落库；两表互不影响既有 files 结构。
    c.execute("""
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            root TEXT DEFAULT '',
            project_id INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0,
            current TEXT DEFAULT '',
            status TEXT DEFAULT 'running',
            error TEXT,
            extra TEXT,
            created_at REAL,
            updated_at REAL
        )
    """)
    # P5：config.json 中的 excludes 作为初始规则注入（已有规则不重复，之后仍以 DB 为准）
    try:
        for _pat in (_FILE_CFG.get("excludes") or []):
            if isinstance(_pat, str) and _pat.strip():
                c.execute("INSERT OR IGNORE INTO app_excludes(pattern, added_at) VALUES(?,?)",
                          (_pat.strip(), time.time()))
    except Exception:
        pass
    # 内置默认规则只播种一次：标记写入后，用户即使删光规则，下次启动也不会再补回。
    # 升级已有库的策略：不删除、不覆盖任何既有规则，只把缺失的内置规则补齐一次。
    try:
        _seeded = c.execute("SELECT value FROM app_meta WHERE key=?", (SEEDED_FLAG,)).fetchone()
        if not _seeded:
            for _pat in DEFAULT_EXCLUDES:
                c.execute("INSERT OR IGNORE INTO app_excludes(pattern, added_at, builtin) VALUES(?,?,1)",
                          (_pat, time.time()))
            c.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES(?, '1')", (SEEDED_FLAG,))
    except Exception:
        pass
    # 进程重启：上次遗留的 running 一律视为中断（P4 可续/可提示）
    try:
        c.execute("UPDATE jobs SET status='aborted', updated_at=? WHERE status='running'",
                  (time.time(),))
    except Exception:
        pass
    conn.commit()
    migrate_projects(conn)
    init_fts(conn)
    load_excludes(conn)        # P2：把排除规则载入内存，供扫描匹配
    try:                       # P1：长期运行后让 -wal 文件不无限增长
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    conn.close()

# ----------------------------------------------------------------------------
# 后台任务落库（P4）：写失败一律吞掉，绝不影响扫描/识别主流程
# ----------------------------------------------------------------------------
def job_create(jid, kind, root="", project_id=0, total=0):
    try:
        conn = get_db()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO jobs(id,kind,root,project_id,total,done,current,
                   status,error,extra,created_at,updated_at) VALUES(?,?,?,?,?,0,'','running',NULL,NULL,?,?)""",
                (jid, kind, root or "", int(project_id or 0), int(total or 0),
                 time.time(), time.time()))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def job_update(jid, **kw):
    """按白名单更新任务进度。finished=True/False 映射为 status done/running。"""
    try:
        if "finished" in kw:
            kw["status"] = "done" if kw.pop("finished") else "running"
        sets, vals = [], []
        for k in ("total", "done", "current", "status", "error", "extra"):
            if k in kw and kw[k] is not None:
                sets.append("%s=?" % k)
                vals.append(json.dumps(kw[k], ensure_ascii=False) if k == "extra" else kw[k])
        if not sets:
            return
        sets.append("updated_at=?")
        vals.append(time.time())
        vals.append(jid)
        conn = get_db()
        try:
            conn.execute("UPDATE jobs SET %s WHERE id=?" % ",".join(sets), vals)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def job_get(jid):
    """读单个任务；不存在返回 None。"""
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    finally:
        conn.close()


# 任务终态：done=全部成功 / partial=部分失败 / failed=全部失败或异常 / aborted=进程重启中断。
# 旧值 error 保留在识别里（兼容历史行），前端一律按“失败”处理。
JOB_TERMINAL = ("done", "partial", "failed", "error", "aborted")


def job_progress_from_row(row, kind=""):
    """把 jobs 表行还原成前端进度对象（内存态缺失/服务重启后的回落读取）。

    字段与内存态保持一致；额外带 status/finished/interrupted，便于前端区分
    “正常完成 / 部分失败 / 全部失败 / 被中断”，不再用 finished 单一布尔值猜语义。
    """
    try:
        extra = json.loads(row["extra"] or "{}")
    except Exception:
        extra = {}
    status = row["status"] or "running"
    out = {
        "total": row["total"] or 0,
        "done": row["done"] or 0,
        "current": row["current"] or "",
        "status": status,
        "finished": status in JOB_TERMINAL,
        "error": row["error"],
        "errors": extra.get("errors") or [],
        "pending": extra.get("pending") or 0,
        "stage": extra.get("stage") or "",
        "pages_done": extra.get("pages_done") or 0,
        "pages_total": extra.get("pages_total") or 0,
        "seg_done": extra.get("seg_done") or 0,
        "seg_total": extra.get("seg_total") or 0,
        # 转写失败可读化 / 模型下载进度（旧字段全部保留，仅追加）
        "dl_done": extra.get("dl_done") or 0,
        "dl_total": extra.get("dl_total") or 0,
        "reason": extra.get("reason") or None,
        "action": extra.get("action") or None,
        "code": extra.get("code") or None,
        "failed_stage": extra.get("failed_stage") or "",
    }
    if kind:
        out["kind"] = kind
    if status == "aborted":
        out["interrupted"] = True
        if not out["error"]:
            out["error"] = "任务已中断（服务重启），可重新发起"
    return out


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

# ----------------------------------------------------------------------------
# 搜索命中定位：命中数 + 服务端片段（列表/详情共用）
# ----------------------------------------------------------------------------
def search_terms(search):
    """返回实际参与文本匹配的高亮词。

    本项目 `_build_where` 把整个搜索串当成**一个字面短语**（FTS 用 fts_phrase 整体加引号，
    LIKE 用 %整串%），并不按空格分词；因此这里必须原样返回整串，否则会出现“标了但没命中”
    或“命中了却没标”的错位。多词输入（如 "foo bar"）匹配的是连续短语 "foo bar"。
    """
    s = (search or "").strip()
    return [s] if s else []


def _find_spans(text, phrase, limit):
    """大小写不敏感、不重叠地找出 phrase 在 text 中的位置，最多 limit 处。

    不重叠：同一位置不会因为重复字符被算成多次（"aaa" 里的 "aa" 只算 1 处）。
    """
    spans = []
    if not text or not phrase:
        return spans
    low = text.lower()
    p = phrase.lower()
    n = len(p)
    i = low.find(p)
    while i >= 0 and len(spans) < limit:
        spans.append((i, i + n))
        i = low.find(p, i + n)
    return spans


def _compact_piece(s):
    """片段内的连续空白压成单个空格，让列表里的片段紧凑可读（PDF 抽取常带大量换行）。"""
    return re.sub(r"[ \t\r\n\f\v]+", " ", s)


def _make_snippet(text, start, end, ctx=SNIPPET_CTX):
    """从原文切出命中词前后各 ctx 个字符，并用控制字符定界符包住命中词。

    三段分别压缩空白，保证标记区间不会因为压缩而错位；两端截断处补省略号。
    """
    a = max(0, start - ctx)
    b = min(len(text), end + ctx)
    body = text[a:b]
    rs, re_ = start - a, end - a
    lead = "…" if a > 0 else ""
    tail = "…" if b < len(text) else ""
    return (lead + _compact_piece(body[:rs]).strip() +
            SNIPPET_MARK_START + _compact_piece(body[rs:re_]).strip() + SNIPPET_MARK_END +
            _compact_piece(body[re_:]).strip() + tail)


def build_hit_info(name, note, tags, content, transcript, search,
                   max_snippets=SNIPPET_MAX, ctx=SNIPPET_CTX):
    """为一个搜索结果行生成命中信息（服务端）：

    返回 {count, capped, snippets, shown, max}；无命中返回 None。
      count   ：各字段命中总次数（与 _build_where 检索字段 name/note/tags/content/transcript 对齐）
      snippets：正文(content) → 转写(transcript) → 备注(note) 顺序的前 max_snippets 个
                **不同位置**片段，每项 {field,label,occ,text}；occ 是该字段内的第几处（0 起），
                供详情页“点第 2 个片段直接定位第 2 处”使用。
    正文里自带的定界符会先剔除，避免正文尖括号/控制字符导致前端标记错乱。
    """
    phrase = (search or "").strip()
    if not phrase:
        return None
    # 字段与 _build_where 的 LIKE/FTS 列保持一致；只有正文/转写/备注参与片段展示
    fields = (
        ("name", "文件名", name or "", False),
        ("note", "备注", note or "", True),
        ("tags", "标签", tags or "", False),
        ("content", "正文", content or "", True),
        ("transcript", "转写", transcript or "", True),
    )
    total = 0
    capped = False
    snip_fields = {}
    for key, label, txt, snip in fields:
        safe = txt.replace(SNIPPET_MARK_START, "").replace(SNIPPET_MARK_END, "")
        spans = _find_spans(safe, phrase, HIT_COUNT_LIMIT)
        if len(spans) >= HIT_COUNT_LIMIT:
            capped = True
        total += len(spans)
        if snip and spans:
            snip_fields[key] = (label, safe, spans)
    if total <= 0:
        return None
    snippets = []
    for key in ("content", "transcript", "note"):
        if key not in snip_fields or len(snippets) >= max_snippets:
            continue
        label, txt, spans = snip_fields[key]
        for occ, (s, e) in enumerate(spans):
            if len(snippets) >= max_snippets:
                break
            snippets.append({"field": key, "label": label, "occ": occ,
                             "text": _make_snippet(txt, s, e, ctx)})
    return {"count": total, "capped": capped, "snippets": snippets,
            "shown": len(snippets), "max": max_snippets}


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
    if ext in OFFICE_EXTS:
        return "doc"
    if ext in TEXT_EXTS:
        return "text"
    return "other"


def sniff_text(path, sample=8192):
    """内容嗅探：判断文件是否为纯文本。TEXT_EXTS 清单之外的新类型零维护自动覆盖。

    规则：含 NUL 字节 → 二进制；UTF-8 严格解码成功 → 文本；
    GB18030 解码成功再用控制字符占比排除伪装成文本的二进制。"""
    try:
        with open(path, "rb") as f:
            chunk = f.read(sample)
    except Exception:
        return False
    if not chunk:
        return True                                    # 空文件按文本处理
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass
    try:
        text = chunk.decode("gb18030")
    except UnicodeDecodeError:
        return False
    ctrl = sum(1 for c in text if ord(c) < 32 and c not in "\t\n\r\f\v")
    return ctrl / max(len(text), 1) < 0.1              # GB18030 兼容字节面太宽，用控制字符占比兜底

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
SELECT_COLS = ("id,path,rel_path,name,ext,parent,size,mtime,depth,status,note,tags,"
               "transcript,transcribed_at,pending_kind,is_dir,content_source,"
               "content_truncated,length(content) AS content_len")

@router.get("/api/statuses")
def api_statuses():
    return {"statuses": get_statuses()}

@router.put("/api/statuses")
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

# ---------- 排除规则（P2） ----------
def _instance_lock_path():
    return os.path.join(tempfile.gettempdir(), "file_xray", "instance.lock")


def _read_running_url():
    try:
        with open(_instance_lock_path(), "r", encoding="utf-8") as _f:
            return _f.read().strip()
    except Exception:
        return None


def _server_alive(url):
    """探测某个 URL 是否真的是本程序还在运行（而非被别的程序占用端口）。"""
    try:
        import urllib.request
        import urllib.error
        with urllib.request.urlopen(url + "/api/health", timeout=1.5) as _r:
            return _r.status == 200 and b"file-xray" in _r.read()
    except Exception:
        return False


def _write_lock(url):
    _p = _instance_lock_path()
    os.makedirs(os.path.dirname(_p), exist_ok=True)
    with open(_p, "w", encoding="utf-8") as _f:
        _f.write(url)


def _remove_lock():
    try:
        os.remove(_instance_lock_path())
    except Exception:
        pass




# 扫描状态字典（模块级可变状态，所有模块通过 config 共享，避免循环依赖）
scan_state = {}


def _norm_exts(ext):
    """把 'png, .jpg mp4' 之类输入归一化为扩展名列表（去重、补点、小写）。"""
    import re
    out = set()
    for e in re.split(r"[,;，；\s]+", (ext or "").strip()):
        e = e.strip().lower()
        if not e:
            continue
        if not e.startswith("."):
            e = "." + e
        out.add(e)
    return sorted(out)


def _build_where(search, status, ext, folder, tag, force_like=False, scope=None,
                 include_missing=False, ftype=""):
    """构造查询条件。返回 (where_sql, params, used_fts)。
    搜索词长度 >= 3 且 FTS 可用时走全文索引；否则（含中文双字词）回退 LIKE。
    ext 支持逗号/空格分隔的多格式；ftype 为类型预设（image/video/...），
    二者同时给出时 ftype 优先。
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
    # 列表/搜索中隐藏“根目录自身”这一行：它只是当前浏览起点，点它钻取等于原地不动，
    # 目录树里已经展示了根；同时避免多根项目里每个根在列表里重复成一行。
    conds.append("NOT (is_dir=1 AND rel_path='')")
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
    exts = sorted(CATEGORY_EXTS[ftype]) if ftype in CATEGORY_EXTS else _norm_exts(ext)
    if exts:
        conds.append("ext IN (%s)" % ",".join("?" * len(exts)))
        params += exts
    # 仅展示该文件夹“直接包含”的子项：精确匹配 parent。
    # folder 为空字符串表示“根目录本级”（parent=''），非空表示对应子目录；
    # 列表始终只显示“当前目录的直接子项”，配合左侧树形成下钻式浏览（目录行可点击进入）。
    # 树/UI 传正斜杠路径，库内存贮用 OS 分隔符（Windows 为反斜杠），先归一化
    fbs = folder.replace("/", os.sep)
    conds.append("parent=?")
    params.append(fbs)
    if tag:
        q = "%" + like_escape(tag) + "%"
        conds.append("tags LIKE ? ESCAPE '\\'")
        params.append(q)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params, used_fts


# ----------------------------------------------------------------------------
# 设置面板（可视化配置）：GET/POST /api/settings
# 只暴露真实存在、适合普通用户调整的键；优先级仍为 环境变量 > config.json > 默认值。
# ----------------------------------------------------------------------------
# 逐任务提示词的长度上限（字符）。Whisper 的提示预算约 224 token，超出会被截断，
# 这里只做硬上限防止异常大请求；界面另给“建议 100 字以内”的提示。
PROMPT_MAX_LEN = 500

SETTINGS_EFFECTS = {
    "transcribe": "下次转写生效",
    "ocr": "下次识别生效",
    "scan": "下次扫描或识别生效",
    "restart": "需重启服务生效",
}


def _setting_schema_item(key, env, group, label, type_, default, help_, effect, **kw):
    item = {"key": key, "env": env, "group": group, "label": label, "type": type_,
            "default": default, "help": help_, "effect": effect}
    item.update(kw)
    return item


SETTINGS_SCHEMA = [
    _setting_schema_item(
        "whisper_model", "FIT_WHISPER_MODEL", "转写", "转写模型", "enum", "base",
        "默认 base。噪声音频建议 small：实测 5dB 噪声下相似度由 0.56 提升到 0.93，"
        "代价是约 2.4 倍耗时与 740MB 内存。",
        "transcribe",
        options=[
            {"value": "tiny", "label": "tiny（最快、最省内存，准确率最低）"},
            {"value": "base", "label": "base（139MB 磁盘 / 319MB 内存 / 基准速度）"},
            {"value": "small", "label": "small（461MB / 740MB / 约 2.4x 慢于 base）"},
            {"value": "medium", "label": "medium（1457MB / 1.76GB / 约 6.9x 慢于 base）"},
            {"value": "large-v3", "label": "large-v3（约 3GB 以上磁盘 / 明显更慢，需大内存）"},
        ]),
    _setting_schema_item(
        "whisper_prompt", "FIT_WHISPER_PROMPT", "转写", "全局默认提示词", "text",
        "以下是普通话的句子。",
        "作为未指定本次提示词时的默认值，可被单次任务覆盖（单次任务 > 本文件上次 > 本项）。"
        "只影响中文识别（让输出稳定为简体）；转写其它语种建议关闭（留空或填 off）。"
        "也可加入产品名/术语提升专有名词准确率。",
        "transcribe", max_len=500, placeholder="留空或填 off 表示关闭"),
    _setting_schema_item(
        "whisper_vad", "FIT_WHISPER_VAD", "转写", "VAD 静音切分", "bool", "0",
        "默认关闭。适合长录音、大段静音或噪声垫底的音频；干净短音频开启无收益。",
        "transcribe"),
    _setting_schema_item(
        "whisper_lang", "FIT_WHISPER_LANG", "转写", "识别语种", "enum", "",
        "默认自动检测。固定语种会提升该语种准确率，但会影响其它语种文件的识别。",
        "transcribe",
        options=[
            {"value": "", "label": "自动检测"},
            {"value": "zh", "label": "中文"},
            {"value": "en", "label": "英文"},
        ]),
    _setting_schema_item(
        "ocr_engine", "FIT_OCR_ENGINE", "OCR", "识别引擎", "enum", "auto",
        "auto 优先 Umi-OCR（中文更强），不可用时回退 tesseract；也可强制指定其一。",
        "ocr",
        options=[
            {"value": "auto", "label": "自动（优先 Umi-OCR，失败回退 tesseract）"},
            {"value": "umi", "label": "Umi-OCR（需开启 HTTP 接口）"},
            {"value": "tesseract", "label": "tesseract（需安装并加 PATH）"},
        ]),
    _setting_schema_item(
        "ocr_langs", "FIT_OCR_LANGS", "OCR", "tesseract 语言", "text", "chi_sim+eng",
        "多语言用 + 连接，如 chi_sim+eng。仅影响 tesseract；Umi-OCR 会自动映射。",
        "ocr", max_len=100),
    _setting_schema_item(
        "ocr_max_mb", "FIT_OCR_MAX_MB", "OCR", "图片识别大小上限（MB）", "int", "30",
        "超过此大小的图片跳过 OCR，避免超大图片拖死任务。", "ocr", min=1, max=200),
    _setting_schema_item(
        "ocr_pdf_enabled", "FIT_OCR_PDF", "OCR", "扫描后自动识别扫描件 PDF", "bool", "1",
        "扫描结束后在后台对无文字层的 PDF 逐个 OCR（不拖慢扫描）。", "scan"),
    _setting_schema_item(
        "pdf_ocr_max_pages", "FIT_PDF_OCR_PAGES", "OCR", "单个 PDF 识别页数上限", "int", "200",
        "超出的页不再识别，0 表示不限制。", "ocr", min=0, max=5000),
    _setting_schema_item(
        "umi_ocr_autostart", "FIT_UMI_OCR_AUTOSTART", "OCR", "自动拉起 Umi-OCR", "bool", "1",
        "已安装但未运行时，识别前自动启动一次。", "ocr"),
    _setting_schema_item(
        "umi_ocr_url", "FIT_UMI_OCR_URL", "OCR", "Umi-OCR 接口地址", "text",
        "http://127.0.0.1:1224", "Umi-OCR「全局设置 → 开放API端口」中的地址。",
        "ocr", max_len=200),
    _setting_schema_item(
        "umi_ocr_path", "FIT_UMI_OCR_PATH", "OCR", "Umi-OCR 可执行文件路径", "text", "",
        "留空则按常见安装路径自动探测；自动拉起失败时可在此指定。",
        "ocr", max_len=500, placeholder=r"如 C:\Program Files\Umi-OCR\Umi-OCR.exe"),
    _setting_schema_item(
        "log_level", "FIT_LOG_LEVEL", "系统", "日志级别", "enum", "INFO",
        "逐文件失败记 DEBUG，扫描级失败记 ERROR。排查问题时可临时调为 DEBUG。",
        "restart",
        options=[{"value": "DEBUG", "label": "DEBUG（最详细）"},
                 {"value": "INFO", "label": "INFO（默认）"},
                 {"value": "WARNING", "label": "WARNING"},
                 {"value": "ERROR", "label": "ERROR（只记错误）"}]),
    _setting_schema_item(
        "fts_enabled", "FIT_FTS", "系统", "全文索引（FTS5）", "bool", "1",
        "关闭后完全回退到 LIKE 检索；索引结构在启动时建立，故需重启。", "restart"),
    _setting_schema_item(
        "max_body_mb", "FIT_MAX_BODY_MB", "系统", "写接口请求体上限（MB）", "int", "25",
        "限制前端提交的 JSON 体积，防止异常大请求；中间件在启动时读取，故需重启。",
        "restart", min=1, max=1024),
]

_SETTINGS_BY_KEY = {i["key"]: i for i in SETTINGS_SCHEMA}


def _setting_parse_value(item, raw):
    """把配置原始值转换为界面/业务用的类型；非法值抛 ValueError。"""
    t = item["type"]
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        raise ValueError("不是布尔值")
    if t == "int":
        if isinstance(raw, bool):
            raise ValueError("不是整数")
        v = int(str(raw).strip())
        if "min" in item and v < item["min"]:
            raise ValueError("不能小于 %s" % item["min"])
        if "max" in item and v > item["max"]:
            raise ValueError("不能大于 %s" % item["max"])
        return v
    if t == "enum":
        s = "" if raw is None else str(raw).strip()
        if s not in [o["value"] for o in item.get("options", [])]:
            raise ValueError("不是合法选项")
        return s
    s = "" if raw is None else str(raw).strip()
    if item.get("max_len") and len(s) > item["max_len"]:
        raise ValueError("长度不能超过 %s 个字符" % item["max_len"])
    return s


def _setting_store_value(item, value):
    """转换为适合写入 config.json 的形式（布尔写 1/0，与现有读取逻辑一致）。"""
    if item["type"] == "bool":
        return "1" if value else "0"
    return value


def _setting_display_value(item, value):
    if item["type"] == "bool":
        return "开启" if value else "关闭"
    if value in ("", None):
        return "（空）"
    return str(value)


def _setting_resolve(item):
    """按 env > config.json > default 解析并返回 (生效值, 来源)。"""
    env_raw = os.environ.get(item["env"], "")
    if env_raw != "":
        try:
            return _setting_parse_value(item, env_raw), "env"
        except Exception:
            pass
    file_raw = _FILE_CFG.get(item["key"])
    if file_raw not in (None, ""):
        try:
            return _setting_parse_value(item, file_raw), "file"
        except Exception:
            pass
    return _setting_parse_value(item, item["default"]), "default"


def settings_payload():
    """供界面渲染：每项的元数据 + 当前生效值 + 默认值 + 来源。"""
    out = []
    for item in SETTINGS_SCHEMA:
        value, source = _setting_resolve(item)
        row = {k: v for k, v in item.items() if k != "default"}
        row["default"] = _setting_parse_value(item, item["default"])
        row["value"] = value
        row["source"] = source
        row["locked"] = source == "env"
        row["effect_text"] = SETTINGS_EFFECTS.get(item["effect"], "")
        out.append(row)
    return out


def _settings_write(values):
    """写入 config.json：只覆盖被管理的键，保留用户其它未管理字段。"""
    data = {}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    data.update(values)
    d = os.path.dirname(CONFIG_FILE)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)
    load_settings()          # 让后续任务读取到新值（无需重启）


@router.get("/api/settings")
def api_settings_get():
    return {"settings": settings_payload(), "config_file": CONFIG_FILE,
            "config_exists": os.path.isfile(CONFIG_FILE),
            "priority": "环境变量 > config.json > 代码默认值"}


@router.post("/api/settings")
async def api_settings_set(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法 JSON")
    if not isinstance(data, dict) or not isinstance(data.get("values"), dict):
        raise HTTPException(400, '请求体需为 {"values": {设置项: 值}}')
    values = data["values"]
    errors, cleaned = [], {}
    for key, raw in values.items():
        item = _SETTINGS_BY_KEY.get(key)
        if not item:
            errors.append("未知设置项：%s" % key)
            continue
        try:
            cleaned[key] = _setting_store_value(item, _setting_parse_value(item, raw))
        except Exception as e:
            errors.append("%s 的值不合法（%s）：%s" % (key, e, raw))
    if errors:
        raise HTTPException(400, "；".join(errors))
    if not cleaned:
        raise HTTPException(400, "没有需要保存的设置")
    # 只把“值确实变化”的键回报给界面，避免 toast 里出现未改动项的生效时机
    before = {k: _setting_resolve(_SETTINGS_BY_KEY[k])[0] for k in cleaned}
    _settings_write(cleaned)
    after = {k: _setting_parse_value(_SETTINGS_BY_KEY[k], cleaned[k]) for k in cleaned}
    changed = sorted(k for k in cleaned if after[k] != before[k])
    messages = []
    for key in cleaned:
        item = _SETTINGS_BY_KEY[key]
        value, source = _setting_resolve(item)
        if source == "env":
            messages.append("%s 已写入配置文件，但当前被环境变量 %s 覆盖，实际仍用 %s"
                            % (key, item["env"], _setting_display_value(item, value)))
    try:
        _log.info("设置已更新：%s", ", ".join(sorted(cleaned)))
    except Exception:
        pass
    return {"ok": True, "settings": settings_payload(), "config_file": CONFIG_FILE,
            "messages": messages, "changed": changed}


# 允许 `from xray.config import *` 一并导出下划线开头的工具名（Python 默认 import * 会跳过 _ 前缀）
__all__ = [n for n in dir() if not n.startswith("__")]
