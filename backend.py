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
import shutil
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

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
    c.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_files_status ON files(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext)")
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
    conn.commit()
    conn.close()

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
# 扫描（后台线程 + 进度）
# ----------------------------------------------------------------------------
scan_state = {}

def scan_worker(root, max_depth, scan_id):
    conn = get_db()
    try:
        count = 0
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
                rel = os.path.relpath(full, root)
                ext = os.path.splitext(fn)[1].lower()
                parent = os.path.dirname(rel)
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
                # 入库：先插入（已存在则忽略），再更新可变元数据，避免冲掉备注/状态/转写/识别结果
                conn.execute(
                    """INSERT OR IGNORE INTO files(path, rel_path, name, ext, parent, size, mtime, depth, status, note, tags, content, indexed_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (full, rel, fn, ext, parent, st.st_size, st.st_mtime, depth,
                     "未处理", "", "", "", time.time()),
                )
                conn.execute(
                    """UPDATE files SET rel_path=?,name=?,ext=?,parent=?,size=?,mtime=?,depth=?,indexed_at=?
                       WHERE path=?""",
                    (rel, fn, ext, parent, st.st_size, st.st_mtime, depth, time.time(), full),
                )
                if is_text or is_pdf:
                    conn.execute("UPDATE files SET content=? WHERE path=?", (content, full))
                count += 1
                if count % 200 == 0:
                    conn.commit()
                    scan_state[scan_id]["processed"] = count
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

def _build_where(search, status, ext, folder, tag):
    conds, params = [], []
    if search:
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
    return where, params

SELECT_COLS = "id,path,rel_path,name,ext,parent,size,mtime,depth,status,note,tags,transcript,transcribed_at"

@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

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
def get_tags():
    conn = get_db()
    rows = conn.execute("SELECT tags FROM files WHERE tags IS NOT NULL AND tags<>''").fetchall()
    conn.close()
    cnt = Counter()
    for r in rows:
        for t in parse_tags(r["tags"]):
            cnt[t] += 1
    return {"tags": [{"tag": t, "count": c} for t, c in cnt.most_common()]}

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
    sid = str(int(time.time() * 1000))
    scan_state[sid] = {"processed": 0, "done": False, "error": None, "root": root}
    t = threading.Thread(target=scan_worker, args=(root, max_depth, sid), daemon=True)
    t.start()
    return {"scan_id": sid}

@app.get("/api/scan/{sid}")
def scan_progress(sid: str):
    s = scan_state.get(sid)
    if not s:
        raise HTTPException(404, "无此扫描任务")
    return {"processed": s["processed"], "done": s["done"], "error": s["error"], "root": s["root"]}

# ---------- 统计 ----------
@app.get("/api/stats")
def stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    by_status = {}
    for r in conn.execute("SELECT status, COUNT(*) c FROM files GROUP BY status"):
        by_status[r[0]] = r[1]
    tagged = conn.execute("SELECT COUNT(*) FROM files WHERE tags IS NOT NULL AND tags<>''").fetchone()[0]
    transcribed = conn.execute("SELECT COUNT(*) FROM files WHERE transcript IS NOT NULL AND transcript<>'' AND transcript NOT LIKE '[转写失败]%'").fetchone()[0]
    img_set = tuple(IMAGE_EXTS)
    ocr_count = 0
    for r in conn.execute("SELECT ext, transcript FROM files WHERE transcript IS NOT NULL AND transcript<>''"):
        t = r["transcript"]
        if r["ext"] in img_set and not (t.startswith("[识别失败]") or t.startswith("[转写失败]")):
            ocr_count += 1
    by_ext = []
    for r in conn.execute("SELECT ext, COUNT(*) c FROM files GROUP BY ext ORDER BY c DESC LIMIT 15"):
        by_ext.append({"ext": r[0] or "(无扩展名)", "count": r[1]})
    conn.close()
    return {"total": total, "by_status": by_status, "tagged": tagged,
            "transcribed": transcribed, "ocr_count": ocr_count, "by_ext": by_ext}

# ---------- 文件夹树 ----------
@app.get("/api/tree")
def get_tree(depth: int = 99):
    conn = get_db()
    folders = {}
    for r in conn.execute("SELECT parent FROM files").fetchall():
        p = r["parent"]
        folders[p] = folders.get(p, 0) + 1
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
               tag: str = "", page: int = 1, page_size: int = 100, sort: str = "name"):
    conn = get_db()
    where, params = _build_where(search, status, ext, folder, tag)
    total = conn.execute(f"SELECT COUNT(*) FROM files {where}", params).fetchone()[0]
    allowed = {"name", "size", "mtime", "status", "depth"}
    order = sort if sort in allowed else "name"
    offset = max(0, (page - 1) * page_size)
    rows = conn.execute(
        f"SELECT {SELECT_COLS} FROM files {where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [page_size, offset]).fetchall()
    items = [dict(r) for r in rows]
    conn.close()
    return {"items": items, "total": total, "page": page, "page_size": page_size}

# ---------- 文件详情 ----------
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
    d["color"] = status_color(d["status"])
    d["transcript"] = d.get("transcript") or ""
    d["transcribed_at"] = d.get("transcribed_at") or None
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

# ---------- 打开文件夹 ----------
@app.post("/api/open")
async def open_file(request: Request):
    data = await request.json()
    return open_folder(data.get("path", ""))

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

# ---------- 搜索任务清单 ----------
def run_task(tid):
    conn = get_db()
    t = conn.execute("SELECT * FROM search_tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        conn.close()
        raise HTTPException(404, "任务不存在")
    where, params = _build_where(t["query"], t["status_filter"], t["ext_filter"], t["folder_filter"], t["tag_filter"])
    total = conn.execute(f"SELECT COUNT(*) FROM files {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {SELECT_COLS} FROM files {where} ORDER BY name LIMIT 500", params).fetchall()
    conn.execute("UPDATE search_tasks SET last_run=?, result_count=? WHERE id=?",
                 (time.time(), total, tid))
    conn.commit()
    task = dict(conn.execute("SELECT * FROM search_tasks WHERE id=?", (tid,)).fetchone())
    conn.close()
    return {"task": task, "items": [dict(r) for r in rows]}

@app.post("/api/tasks")
async def create_task(request: Request):
    data = await request.json()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO search_tasks(query,status_filter,ext_filter,folder_filter,tag_filter,created_at,last_run,result_count) VALUES(?,?,?,?,?,?,?,?)",
        (data.get("query", ""), data.get("status_filter", "all"),
         data.get("ext_filter", ""), data.get("folder_filter", ""),
         data.get("tag_filter", ""), time.time(), time.time(), 0))
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
                conn.execute("UPDATE files SET transcript=?, transcribed_at=? WHERE id=?",
                             (text, time.time(), fid))
                conn.commit()
            except Exception as e:
                conn.execute("UPDATE files SET transcript=? WHERE id=?",
                             ("[转写失败] " + str(e), fid))
                conn.commit()
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
                conn.execute("UPDATE files SET transcript=?, transcribed_at=? WHERE id=?",
                             (text, time.time(), fid))
                conn.commit()
            except Exception as e:
                conn.execute("UPDATE files SET transcript=? WHERE id=?",
                             ("[识别失败] " + str(e), fid))
                conn.commit()
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
