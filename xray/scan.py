from fastapi import APIRouter, Request, HTTPException, Query, Body, UploadFile, Form, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse, Response
from xray.config import *
router = APIRouter()

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
# scan_state 定义于 xray.config（基础模块，无兄弟依赖），本模块经 `from xray.config import *` 共享同一字典
# ----------------------------------------------------------------------------

def scan_worker(root, max_depth, scan_id, project_id=None):
    # 惰性导入：extract_pdf_text/_start_pdf_backlog_ocr 在 ocr 模块，ocr 又依赖本模块的 store_recognized，
    # 顶层互导会形成循环；此处用函数内导入打破循环（模块均已加载，运行时安全）。
    from xray.ocr import extract_pdf_text, _start_pdf_backlog_ocr
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
            # P2：排除规则——命中则整棵子树跳过（不入库、不 descend）
            if is_excluded(dirpath):
                continue
            dirnames[:] = [d for d in dirnames if not is_excluded(os.path.join(dirpath, d))]
            # 物化目录为 files 表中的一等实体（is_dir=1）：空目录也作为节点出现，且可被打标签/备注。
            # 按路径 upsert：目录改名/移动后旧行保留（missing=1），若路径日后恢复则自动对应旧标注（小彩蛋）
            try:
                st_dir = os.stat(dirpath)
                rel_d = "" if dirpath == root else os.path.relpath(dirpath, root)
                rel_d = rel_d.replace(os.sep, "/")
                parent_d = rel_d.rsplit("/", 1)[0] if "/" in rel_d else ""
                name_d = os.path.basename(dirpath)
                if conn.execute("SELECT 1 FROM files WHERE path=?", (dirpath,)).fetchone():
                    conn.execute(
                        "UPDATE files SET rel_path=?,name=?,parent=?,mtime=?,depth=?,indexed_at=?,missing=0,is_dir=1 WHERE path=?",
                        (rel_d, name_d, parent_d, st_dir.st_mtime, depth, time.time(), dirpath))
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO files(path,rel_path,name,ext,parent,size,mtime,depth,"
                        "status,note,tags,content,indexed_at,is_dir) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                        (dirpath, rel_d, name_d, "", parent_d, 0, st_dir.st_mtime, depth,
                         "未处理", "", "", "", time.time()))
            except Exception:
                _log.debug("目录 stat/入库失败，已跳过：%s", dirpath, exc_info=True)
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if is_excluded(full):        # P2：命中排除规则的文件直接跳过（不入库）
                    continue
                try:
                    st = os.lstat(full)
                    if not os.path.isfile(full):
                        continue
                except Exception:
                    _log.debug("文件 lstat 失败，已跳过：%s", full, exc_info=True)
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
                # 仅对文本/PDF 抽取可搜索文字；图片 OCR、音视频转写为按需，重扫不覆盖。
                # 文本判定：清单内直接命中；清单外先内容嗅探（未知新类型零维护自动覆盖）
                content = ""
                is_text = ext in TEXT_EXTS or (
                    ext not in KNOWN_BIN_EXTS and st.st_size <= FILE_READ_LIMIT and sniff_text(full))
                is_pdf = ext in PDF_EXTS
                if is_text and st.st_size <= FILE_READ_LIMIT:
                    try:
                        with open(full, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read(CONTENT_INDEX_LIMIT)
                    except Exception:
                        _log.debug("文本读取失败，内容留空：%s", full, exc_info=True)
                        content = ""
                        _rec_skip(scan_id, full, "text_read_failed")
                elif is_pdf and st.st_size <= FILE_READ_LIMIT:
                    try:
                        content = extract_pdf_text(full)[:PDF_TEXT_LIMIT]
                    except Exception:
                        _log.debug("PDF 文字层抽取失败，内容留空：%s", full, exc_info=True)
                        content = ""
                        _rec_skip(scan_id, full, "pdf_extract_failed")
                    # 扫描件/无文字层的 OCR 不在此处同步做（会拖慢扫描）：
                    # 扫描结束后由后台任务统一补识别（见 _start_pdf_backlog_ocr）。
                if moved_id:
                    # 老文件换了位置/名字：整行迁移，标注零丢失
                    conn.execute(
                        """UPDATE files SET path=?,rel_path=?,name=?,ext=?,parent=?,size=?,mtime=?,
                           depth=?,indexed_at=?,missing=0 WHERE id=?""",
                        (full, rel, fn, ext, parent, st.st_size, st.st_mtime, depth,
                         time.time(), moved_id),
                    )
                    # PDF 抽不到文字层时保留旧内容（可能 是此前 OCR 的结果），不被空值冲掉
                    if is_text or (is_pdf and (content or "").strip()):
                        conn.execute("UPDATE files SET content=?, content_source=? WHERE id=?",
                                     (content, "text_layer" if is_pdf else "", moved_id))
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
                    if is_text or (is_pdf and (content or "").strip()):
                        conn.execute("UPDATE files SET content=?, content_source=? WHERE path=?",
                                     (content, "text_layer" if is_pdf else "", full))
                count += 1
                if count % 200 == 0:
                    conn.commit()
                    scan_state[scan_id]["processed"] = count
        touch_project(conn, project_id)
        conn.commit()
        scan_state[scan_id]["processed"] = count
        scan_state[scan_id]["done"] = True
        # 扫描完成：扫描件/无文字层 PDF 交给后台 OCR 队列（不阻塞扫描，进度走 /api/ocr/{jid}）
        try:
            scan_state[scan_id]["ocr_job_id"] = _start_pdf_backlog_ocr()
        except Exception as _e:
            print("启动扫描件补识别失败: %s" % _e)
    except Exception as e:
        scan_state[scan_id]["error"] = str(e)
        scan_state[scan_id]["done"] = True
    finally:
        conn.close()

# ----------------------------------------------------------------------------
# 打开文件夹
# ----------------------------------------------------------------------------
@router.get("/api/excludes")
def api_excludes():
    conn = get_db()
    rows = conn.execute("SELECT id, pattern, added_at FROM app_excludes ORDER BY id").fetchall()
    conn.close()
    return {"excludes": [dict(r) for r in rows], "count": len(rows)}


@router.post("/api/excludes")
async def add_exclude(request: Request):
    data = await request.json()
    pattern = (data.get("pattern") or "").strip()
    if not pattern:
        raise HTTPException(400, "规则不能为空")
    conn = get_db()
    try:
        conn.execute("INSERT INTO app_excludes(pattern, added_at) VALUES(?,?)", (pattern, time.time()))
        conn.commit()
    except Exception:
        conn.close()
        raise HTTPException(409, "规则已存在")
    conn.close()
    load_excludes()                      # 热更新内存中的规则
    return {"ok": True, "excludes": pattern}


@router.post("/api/excludes/delete")
async def delete_exclude(request: Request):
    data = await request.json()
    eid = data.get("id")
    conn = get_db()
    conn.execute("DELETE FROM app_excludes WHERE id=?", (eid,))
    conn.commit()
    conn.close()
    load_excludes()
    return {"ok": True}


@router.post("/api/excludes/prune")
async def prune_excludes(request: Request):
    """按当前排除规则清理已索引文件（仅 DELETE FROM files，不碰磁盘）。返回删除条数。"""
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, path FROM files").fetchall()
        ids = [r["id"] for r in rows if is_excluded(r["path"] or "")]
        deleted = 0
        if ids:
            qmarks = ",".join("?" * len(ids))
            # files_fts 由现有 DELETE 触发器自动同步，无需手动维护索引
            deleted = conn.execute(f"DELETE FROM files WHERE id IN ({qmarks})", ids).rowcount
            conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted": deleted, "scanned": len(rows)}

# ---------- 标签云 ----------
@router.get("/api/projects")
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


@router.post("/api/projects/{pid}/relocate")
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


@router.post("/api/projects")
async def create_project(request: Request):
    """新建项目。只需指定根目录，成员即时可见（已索引的文件无需重扫）。"""
    from xray.tasks import watch_add_roots   # 惰性导入，避免与 tasks 顶层循环依赖
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


@router.put("/api/projects/{pid}")
async def update_project(pid: int, request: Request):
    """改名 / 改备注 / 改根目录。改动立即生效，不影响任何文件实体。"""
    from xray.tasks import watch_add_roots   # 惰性导入，避免与 tasks 顶层循环依赖
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


@router.delete("/api/projects/{pid}")
def delete_project(pid: int):
    """只删除项目这个视图；文件实体与其备注/状态/标签一律保留。"""
    conn = get_db()
    conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.post("/api/projects/{pid}/scan")
def scan_project(pid: int):
    """按项目当前设置的根目录重新扫描。同一项目反复扫描只刷新内容，不会堆出新项目。"""
    from xray.tasks import watch_add_roots   # 惰性导入，避免与 tasks 顶层循环依赖
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
                          "root": r, "project_id": pid, "skipped": [], "errors": []}
        threading.Thread(target=scan_worker, args=(r, md, sid, pid), daemon=True).start()
        sids.append(sid)
    watch_add_roots(roots)
    if not sids:
        raise HTTPException(400, "项目的根目录均不存在或无法访问")
    return {"scan_ids": sids, "roots": roots}


# ---------- 扫描 ----------
@router.post("/api/scan")
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
    # 记录扫描过的根目录，供实时感知复用
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
                      "root": root, "project_id": pid, "skipped": [], "errors": []}
    t = threading.Thread(target=scan_worker, args=(root, max_depth, sid, pid), daemon=True)
    t.start()
    return {"scan_id": sid, "project_id": pid}

