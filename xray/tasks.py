from fastapi import APIRouter, Request, HTTPException, Query, Body, UploadFile, Form, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse, Response
from xray.config import *
from xray.scan import scan_worker, relocate_records, update_project_roots_moved
from xray.ocr import extract_pdf_text
router = APIRouter()

def open_folder(path):
    fallback = ""
    if not os.path.exists(path):
        # 原路径已不存在（文件被移动/删除、索引未更新）：回退到最近一个仍存在的祖先目录
        p = path
        while True:
            parent = os.path.dirname(p)
            if parent == p:
                raise HTTPException(404, "路径及其上层目录均不存在：" + path)
            p = parent
            if os.path.isdir(p):
                fallback = p
                path = p
                break
    try:
        is_dir = os.path.isdir(path)
        if sys.platform == "win32":
            # 文件：资源管理器中定位；目录：直接打开该目录
            if is_dir:
                subprocess.run(f'explorer "{path}"', shell=True)
            else:
                subprocess.run(f'explorer /select,"{path}"', shell=True)
        elif sys.platform == "darwin":
            subprocess.run(["open", path] if is_dir else ["open", "-R", path])
        else:
            subprocess.run(["xdg-open", path if is_dir else os.path.dirname(path)])
    except Exception as e:
        raise HTTPException(500, f"打开失败: {e}")
    return {"ok": True, "fallback": fallback}

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


@router.post("/api/batch")
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
def _resolve_open_path(conn, path):
    """把调用方给的路径解析为可打开的真实路径。
    绝对路径原样返回；相对路径（左侧文件夹树/位置过滤的值，如“笔记”）依次尝试：
    1) 现存项目根下拼接；2) 用索引记录反查（文件 realpath 就在库里，dirname 即目录，
    因此即使记录来自已删除的项目也能定位）。解析不出返回原值（后续按不存在处理）。"""
    if os.path.isabs(path):
        return path
    rel = path.replace("\\", "/").strip("/")
    if ".." in rel.split("/"):
        raise HTTPException(403, "路径不允许包含 ..")
    roots = []
    for r in conn.execute("SELECT roots FROM projects").fetchall():
        try:
            roots += [norm_root(x) for x in json.loads(r["roots"] or "[]")]
        except Exception:
            continue
    for rt in dict.fromkeys(roots):                  # 去重保持顺序
        cand = os.path.join(rt, *rel.split("/")) if rel else rt
        if os.path.exists(cand):
            return cand
    if not rel:
        return path
    rel_bs = rel.replace("/", os.sep)
    row = conn.execute("SELECT path FROM files WHERE parent=? LIMIT 1", (rel_bs,)).fetchone()
    if row:                                          # 有文件直接位于该文件夹：dirname 即真实目录
        return os.path.dirname(row["path"])
    row = conn.execute("SELECT path, rel_path FROM files WHERE rel_path LIKE ? ESCAPE '\\' LIMIT 1",
                       (like_escape(rel_bs + os.sep) + "%",)).fetchone()
    if row:                                          # 命中更深层文件：从其相对路径剥掉多出的后缀
        file_rel = (row["rel_path"] or "").replace("/", os.sep)
        d = os.path.dirname(row["path"])
        suffix = file_rel[len(rel_bs):]
        if not suffix:
            return d
        if d.endswith(suffix):
            return d[:-len(suffix)]
    return path


@router.post("/api/open")
async def open_file(request: Request):
    data = await request.json()
    path = (data.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "缺少路径")
    conn = get_db()
    try:
        path = _resolve_open_path(conn, path)
    finally:
        conn.close()
    if not os.path.exists(path):
        raise HTTPException(404, "路径不存在：" + path)
    return open_folder(path)

# ---------- 导出 ----------
@router.post("/api/export")
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
@router.post("/api/report")
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

@router.post("/api/tasks")
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

@router.get("/api/tasks")
def list_tasks():
    conn = get_db()
    rows = conn.execute("SELECT * FROM search_tasks ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"tasks": [dict(r) for r in rows]}

@router.post("/api/tasks/{tid}/retry")
def retry_task(tid: int):
    return run_task(tid)

# ----------------------------------------------------------------------------
# 目录实时感知（watchdog）：文件增删改自动更新索引
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
    """感知已开启时，把新的根目录追加进感知（无需停止再启动）。"""
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
        is_text = ext in TEXT_EXTS or (
            ext not in KNOWN_BIN_EXTS and st.st_size <= FILE_READ_LIMIT and sniff_text(path))
        if is_text and st.st_size <= FILE_READ_LIMIT:
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
            # 扫描件 OCR 不在此同步做：入库后由待识别池/后台任务统一补
        conn.execute(
            """INSERT OR IGNORE INTO files(path,rel_path,name,ext,parent,size,mtime,depth,status,note,tags,content,content_source,indexed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (path, rel, fn, ext, parent, st.st_size, st.st_mtime, depth,
             "未处理", "", "", content,
             "text_layer" if (ext in PDF_EXTS and (content or "").strip()) else "", time.time()))
        conn.execute(
            """UPDATE files SET rel_path=?,name=?,ext=?,parent=?,size=?,mtime=?,depth=?,indexed_at=?,missing=0
               WHERE path=?""",
            (rel, fn, ext, parent, st.st_size, st.st_mtime, depth, time.time(), path))
        if is_text or ext in PDF_EXTS:
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


@router.get("/api/watch")
def watch_status():
    return {"enabled": bool(WATCH_STATE["enabled"]), "roots": WATCH_STATE["roots"],
            "error": WATCH_STATE["error"], "watchdog_installed": WATCHDOG_OK,
            "watchdog_error": WATCHDOG_ERR}


@router.post("/api/watch")
async def watch_control(request: Request):
    """开启/关闭目录实时感知。start 时可传 roots，省略则用已扫描过的根目录。"""
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
        raise HTTPException(400, "没有可感知的目录，请先扫描一次")

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
