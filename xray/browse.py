from fastapi import APIRouter, Request, HTTPException
import logging
from xray.config import *
from xray.ocr import ocr_is_available, wasm_available
router = APIRouter()
_log = logging.getLogger("xray")

@router.get("/api/tags")
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
@router.get("/api/dirs")
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


@router.get("/api/dirs/quick")
def quick_dirs():
    """常用位置快捷入口：此电脑 / 主目录 / 桌面 / 文档 / 下载 / 外接设备。"""
    out = []
    if os.name == "nt":
        out.append({"name": "此电脑", "path": ""})   # 空路径 = 盘符列表
    home = os.path.expanduser("~")
    if os.path.isdir(home):
        out.append({"name": "用户主目录", "path": home})
    for nm, label in (("Desktop", "桌面"), ("Documents", "文档"),
                      ("Downloads", "下载"), ("Pictures", "图片")):
        p = os.path.join(home, nm)
        if os.path.isdir(p):
            out.append({"name": label, "path": p})
    if os.name == "nt":
        # 外接设备：可移动磁盘（U盘/移动硬盘）与光驱，按盘符枚举
        import ctypes
        import string
        for d in string.ascii_uppercase:
            root = d + ":\\"
            t = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
            if t in (2, 4):                          # DRIVE_REMOVABLE / DRIVE_CDROM
                out.append({"name": "外接设备 %s:" % d, "path": root})
    elif os.path.isdir("/Volumes"):                  # macOS 挂载的外接卷
        for nm in sorted(os.listdir("/Volumes")):
            if nm != "Macintosh HD":
                out.append({"name": "外接设备 " + nm, "path": os.path.join("/Volumes", nm)})
    elif os.path.isdir("/media"):                    # Linux 挂载点
        out.append({"name": "外接设备 /media", "path": "/media"})
    return {"quick": out}


# ---------- 项目（文件的自由组合 / 虚拟视图） ----------
@router.get("/api/stats")
def stats(project: int = 0):
    conn = get_db()
    scope = project_scope(conn, project)
    # 目录（is_dir=1）也是一等实体，统计一并计入（与列表“共 N 条”对齐）
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
    for r in conn.execute(
            f"SELECT (CASE WHEN is_dir=1 THEN '📁目录' ELSE COALESCE(ext,'') END) AS k, COUNT(*) c "
            f"FROM files {base} GROUP BY k ORDER BY c DESC LIMIT 15", sp):
        k = r["k"]
        label = "📁目录" if k == "📁目录" else (k or "(无扩展名)")
        by_ext.append({"ext": label, "count": r["c"]})
    missing = conn.execute(f"SELECT COUNT(*) FROM files {base} AND missing=1", sp).fetchone()[0]
    conn.close()
    return {"total": total, "by_status": by_status, "tagged": tagged,
            "transcribed": transcribed, "ocr_count": ocr_count, "by_ext": by_ext,
            "missing": missing}

# ---------- 文件夹树 ----------
@router.get("/api/tree")
def get_tree(depth: int = 99, project: int = 0):
    conn = get_db()
    scope = project_scope(conn, project)
    roots = scope[2]
    w = ("WHERE (missing IS NULL OR missing=0)" +
         ((" AND " + scope[0]) if scope[0] else ""))
    folders = {}
    real_map = {}                                   # 相对目录 -> 真实绝对目录（沿文件祖先链一对一映射）
    for r in conn.execute(f"SELECT path, rel_path, is_dir FROM files {w}", list(scope[1])).fetchall():
        rel = rel_for(r["path"], roots)
        if rel is None:
            rel = (r["rel_path"] or "").replace("\\", "/")
        if r["is_dir"]:
            # 目录本身即一个文件夹节点（含空目录）：自身与各级祖先都注册，保证层级连续
            if rel:
                folders.setdefault(rel, 0)
                parts = rel.split("/")
                for k in range(1, len(parts)):
                    folders.setdefault("/".join(parts[:k]), 0)
                real_map.setdefault(rel, r["path"])
            continue
        parts = rel.split("/") if rel else []
        # 注册路径上的每一级祖先目录（含直接父目录）：即便某层目录没有直接文件，
        # 只要更深层有文件，它也应作为树节点出现，否则层级/缩进会错位
        for k in range(1, len(parts)):
            folders.setdefault("/".join(parts[:k]), 0)
        p = "/".join(parts[:-1]) if len(parts) > 1 else ""
        if p:
            folders[p] = folders.get(p, 0) + 1      # 直接父目录累计本层文件数
        else:
            folders[""] = folders.get("", 0) + 1     # 根目录下的文件计入根节点
        # 同一条记录的相对目录链与绝对目录链逐级对应，一遍扫描即可建立映射
        ad = os.path.dirname(r["path"])
        rd = p
        while True:
            real_map.setdefault(rd, ad)
            if not rd:
                break
            rd = rd.rsplit("/", 1)[0] if "/" in rd else ""
            ad = os.path.dirname(ad)
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
            # 根节点只是“回到全部”的入口，不代表某个真实目录，不给 real（不可打开）
            result.append({"path": fpath, "name": name, "depth": d, "count": cnt,
                           "real": real_map.get(fpath, "") if fpath else ""})
    result.sort(key=lambda x: (x["depth"], x["path"]))
    conn.close()
    return {"folders": result}

# ---------- 文件清单 ----------
@router.get("/api/files/exist")
def files_exist(ids: str = ""):
    """轻量校验：返回给定 id 中仍存在的那些（前端清理失效勾选，避免“已选 N”混入幽灵记录）。"""
    want = []
    for x in (ids or "").split(","):
        x = x.strip()
        if x.isdigit():
            want.append(int(x))
    want = want[:5000]
    if not want:
        return {"ids": []}
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id FROM files WHERE id IN (%s)" % ",".join("?" * len(want)),
            want).fetchall()
    finally:
        conn.close()
    return {"ids": [r["id"] for r in rows]}


@router.get("/api/files")
def list_files(search: str = "", status: str = "all", ext: str = "", folder: str = "",
               tag: str = "", page: int = 1, page_size: int = 100, sort: str = "name",
               project: int = 0, show_missing: int = 0, ftype: str = ""):
    conn = get_db()
    allowed = {"name", "size", "mtime", "status", "depth"}
    order = sort if sort in allowed else "name"
    offset = max(0, (page - 1) * page_size)
    scope = project_scope(conn, project)
    inc_missing = bool(show_missing)

    def _run(force_like):
        w, p, u = _build_where(search, status, ext, folder, tag,
                               force_like=force_like, scope=scope,
                               include_missing=inc_missing, ftype=ftype)
        t = conn.execute(f"SELECT COUNT(*) FROM files {w}", p).fetchone()[0]
        rr = conn.execute(
            f"SELECT {SELECT_COLS} FROM files {w} ORDER BY is_dir DESC, {order} LIMIT ? OFFSET ?",
            p + [page_size, offset]).fetchall()
        return t, rr, u

    try:
        total, rows, used_fts = _run(False)
    except Exception:
        # FTS 查询异常（特殊字符等）时回退 LIKE，保证搜索始终可用
        total, rows, used_fts = _run(True)
    items = apply_project_view([dict(r) for r in rows], scope[2])
    # 搜索时补充“定位到具体位置”：命中次数 + 服务端截取的上下文片段。
    # 只查当前页（≤ SNIPPET_MAX_ROWS 行）的正文/转写，避免把 20 万字正文整段发给前端。
    terms = search_terms(search)
    if terms and items:
        body_ids = [it["id"] for it in items[:SNIPPET_MAX_ROWS]]
        marks = ",".join("?" * len(body_ids))
        body_rows = conn.execute(
            "SELECT id, content, transcript FROM files WHERE id IN (%s)" % marks,
            body_ids).fetchall()
        body_map = {r["id"]: r for r in body_rows}
        for it in items:
            row = body_map.get(it["id"])
            if row is None:      # 超出片段行数上限的条目只保留原有字段（前端不会误判）
                continue
            info = build_hit_info(it.get("name"), it.get("note"), it.get("tags"),
                                  row["content"], row["transcript"], search)
            if info:
                it["hits"] = info
    conn.close()
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "fts": bool(used_fts), "terms": terms,
            "snippet_show": SNIPPET_SHOW}

# ---------- 文件详情 ----------
@router.post("/api/file/{fid}/text")
async def resolve_pending(fid: int, request: Request):
    """处理待确认的识别结果。
    action=accept   把暂存内容（可用 text 覆盖，即边改边采用）写入正式内容
    action=discard  丢弃暂存内容，旧内容原样保留
    """
    data = await request.json()
    action = (data.get("action") or "").strip()
    conn = get_db()
    try:
        r = conn.execute(
            "SELECT id,ext,pending_text,pending_kind,content,content_source,transcript "
            "FROM files WHERE id=?", (fid,)).fetchone()
        if not r:
            raise HTTPException(404, "未找到文件")
        if action == "accept":
            text = data.get("text")
            if text is None:
                text = r["pending_text"] or ""
            text = (text or "").strip()
            if not text:
                raise HTTPException(400, "暂存内容为空，无可采用的内容")
            kind = (r["pending_kind"] or "").strip()
            cat = categorize(r["ext"] or "")
            # 按文件类型写正确字段：PDF 类 -> content（详情映射为 pdf_text），
            # 图片/音视频 -> transcript。类型与暂存来源不匹配时明确报错，不静默丢弃。
            if cat == "pdf":
                if kind and kind not in ("ocr", "pdf"):
                    _log.warning("采用暂存失败：PDF %s 的暂存来源为 %s，类型不匹配", fid, kind)
                    raise HTTPException(400, "该 PDF 的暂存结果来源为「%s」，与 PDF 文字字段不匹配，已拒绝写入" % kind)
                conn.execute(
                    "UPDATE files SET content=?, content_source='ocr', "
                    "pending_text='', pending_kind='' WHERE id=?", (text, fid))
                target = "content"
            elif cat in ("image", "audio", "video"):
                expect = "ocr" if cat == "image" else "transcribe"
                if kind and kind != expect:
                    _log.warning("采用暂存失败：%s 类文件 %s 的暂存来源为 %s，应为 %s",
                                 cat, fid, kind, expect)
                    raise HTTPException(
                        400, "该文件（%s）的暂存结果来源为「%s」，应为「%s」，字段不匹配，已拒绝写入"
                             % (cat, kind, expect))
                conn.execute(
                    "UPDATE files SET transcript=?, transcribed_at=?, pending_text='', pending_kind='' "
                    "WHERE id=?", (text, time.time(), fid))
                target = "transcript"
            else:
                _log.warning("采用暂存失败：文件 %s（ext=%s）不支持文字结果", fid, r["ext"])
                raise HTTPException(
                    400, "该文件类型（%s）不支持文字结果，无法采用" % (r["ext"] or "未知"))
        elif action == "discard":
            conn.execute("UPDATE files SET pending_text='', pending_kind='' WHERE id=?", (fid,))
            target = ""
        else:
            raise HTTPException(400, "未知操作：" + action)
        conn.commit()
        row = conn.execute(
            "SELECT content,content_source,transcript,pending_text,pending_kind FROM files WHERE id=?",
            (fid,)).fetchone()
    finally:
        conn.close()
    return {"ok": True, "target": target,
            "transcript": row["transcript"] or "",
            "pdf_text": (row["content"] or "") if target == "content" else "",
            "content_source": row["content_source"] or "",
            "pending_text": row["pending_text"] or "",
            "pending_kind": row["pending_kind"] or ""}


@router.get("/api/file/{fid}")
def file_detail(fid: int):
    conn = get_db()
    r = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(404, "未找到文件")
    d = dict(r)
    is_dir = bool(d.get("is_dir") or 0)
    d["is_dir"] = is_dir
    cat = "dir" if is_dir else categorize(d["ext"])
    # 清单外但内容为纯文本的扩展名（嗅探判定）：详情中同样支持预览
    if not is_dir and cat == "other" and d["path"] and os.path.isfile(d["path"]) and sniff_text(d["path"]):
        cat = "text"
    d["category"] = cat
    d["raw_url"] = f"/api/raw/{fid}"
    preview = (d["content"] or "")[:100000]
    if cat == "text" and not preview and d["path"] and os.path.isfile(d["path"]) \
            and os.path.getsize(d["path"]) <= FILE_READ_LIMIT:
        try:
            with open(d["path"], "r", encoding="utf-8", errors="ignore") as f:
                preview = f.read(100000)
        except Exception:
            preview = ""
    # 文本与三种 Office 文档都直接展示抽取正文；Office 另外给出 doc_text，
    # 与 PDF 的 pdf_text 对称，避免“库里有值、界面没字”。
    d["preview_text"] = preview if cat in ("text", "doc") else ""
    d["doc_text"] = (d["content"] or "") if cat == "doc" else ""
    d["tags"] = parse_tags(d["tags"])
    d["exists"] = os.path.isfile(d["path"]) or os.path.isdir(d["path"])
    d["missing"] = bool(d.get("missing") or 0)
    d["color"] = status_color(d["status"])
    d["transcript"] = d.get("transcript") or ""
    d["transcribed_at"] = d.get("transcribed_at") or None
    # 该文件最后一次转写使用的提示词（空=沿用全局默认；off=关闭）
    d["prompt"] = d.get("prompt") or ""
    d["pending_text"] = d.get("pending_text") or ""
    d["pending_kind"] = d.get("pending_kind") or ""
    d["can_transcribe"] = (d["category"] in ("audio", "video")) and bool(d["exists"])
    # 图片 OCR 需后端引擎；PDF 可用后端引擎或浏览器内置 OCR（零安装兜底）
    if d["category"] == "image":
        d["can_ocr"] = bool(d["exists"]) and ocr_is_available()
    elif d["category"] == "pdf":
        d["can_ocr"] = bool(d["exists"]) and (ocr_is_available() or wasm_available())
    else:
        d["can_ocr"] = False
    d["ocr_langs"] = OCR_LANGS
    d["pdf_text"] = (d.get("content") or "") if d["category"] == "pdf" else ""
    conn.close()
    return d

@router.put("/api/file/{fid}")
async def update_file(fid: int, request: Request):
    data = await request.json()
    note = data.get("note", "")
    status = data.get("status", "未处理")
    tags = to_tag_string(data.get("tags", ""))
    conn = get_db()
    conn.execute("UPDATE files SET note=?, status=?, tags=? WHERE id=?", (note, status, tags, fid))
    # 文字稿人工校对：transcript（图片/音视频）；pdf_text（PDF 文字层，存 content 列）。
    # FTS 触发器 files_fts_au 会在 UPDATE 后自动同步搜索索引。
    transcript = data.get("transcript")
    if transcript is not None:
        conn.execute("UPDATE files SET transcript=?, transcribed_at=? WHERE id=?",
                     (transcript, time.time(), fid))
    pdf_text = data.get("pdf_text")
    if pdf_text is not None:
        src = data.get("pdf_text_source") or "manual"   # ocr=浏览器内置识别 / manual=人工
        if src not in ("ocr", "manual", "text_layer"):
            src = "manual"
        conn.execute("UPDATE files SET content=?, content_source=? WHERE id=?", (pdf_text, src, fid))
    conn.commit()
    conn.close()
    return {"ok": True}

# ---------- 批量编辑（状态 / 标签 / 备注） ----------
