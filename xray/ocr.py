import json
import os
import sys
import time
import logging
import subprocess
from fastapi import APIRouter, Request, HTTPException, Query, Body, UploadFile, Form, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse, Response
from xray.config import *
from xray.scan import store_recognized
router = APIRouter()
_ocr_log = logging.getLogger("xray")


def _load_fitz():
    """惰性加载 PyMuPDF：新版用 `import pymupdf`，旧版回退 `import fitz`（已被标记弃用）。"""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    return fitz

def extract_pdf_text(path):
    """抽取 PDF 文字内容（优先 PyMuPDF，回退 pypdf / pdfminer）。无文字层返回空。"""
    try:
        fitz = _load_fitz()
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
    """通过 Umi-OCR 的 HTTP API 识别图片，返回文字；服务不可达/失败返回 None。

    Umi-OCR 的 /api/ocr 只接受 base64 字段（本机路径字段不存在），
    因此读取文件转 base64 上送；options 用 data.format=text 让返回为纯文本。
    """
    import urllib.request
    import base64
    _ensure_umi_running()        # 已安装但未运行时按需拉起
    url = UMI_OCR_URL + "/api/ocr"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    payload = json.dumps({
        "base64": b64,
        "options": {"data.format": "text"},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if data.get("code") != 100:
        print("Umi-OCR 返回错误 code=%s: %s" % (data.get("code"), data.get("data")))
        return None
    d = data.get("data")
    if isinstance(d, str):
        return d.strip()
    if isinstance(d, list):
        texts = []
        for item in d:
            if isinstance(item, dict):
                t = item.get("text") or item.get("OCR_Text")
            else:
                t = item
            if t:
                texts.append(str(t))
        return "\n".join(texts).strip()
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
            raise RuntimeError("Umi-OCR 服务不可达：请确认其已启动，并在 Umi-OCR「全局设置 → 开放API端口」中开启 HTTP 接口（默认 %s）" % UMI_OCR_URL)
        return text
    # auto：Umi-OCR 可用则用之，否则 tesseract
    try:
        text = umi_ocr(path, langs)
        if text is not None:
            return text
    except Exception:
        pass
    return tesseract_ocr(path, langs)


def ocr_is_available():
    """OCR 引擎是否可用（供扫描自动 OCR / 前端按钮判断）。"""
    try:
        return ocr_check()[0]
    except Exception:
        return False


# 浏览器内置 OCR（tesseract.js WASM）：随包分发的零安装兜底引擎，仅处理扫描件 PDF
WASM_OCR_DIR = os.path.join(BASE_DIR, "web", "vendor", "tesseract")
WASM_OCR_FILES = ("tesseract.min.js", "worker.min.js",
                  "tesseract-core-simd.wasm.js", "chi_sim.traineddata.gz")


def wasm_available():
    """浏览器内置 OCR 的运行文件是否随包就绪。"""
    return all(os.path.isfile(os.path.join(WASM_OCR_DIR, f)) for f in WASM_OCR_FILES)


def ocr_install_guide():
    """按当前操作系统返回“如何安装 OCR 引擎”的引导文案（跨平台）。"""
    p = sys.platform
    if p.startswith("win"):
        return ("Windows：推荐安装 Umi-OCR（中文识别更强，绿色版解压即用）。启动后请在"
                "「全局设置 → 开放API端口」中开启 HTTP 接口（默认 http://127.0.0.1:1224），"
                "本工具会自动拉起它；若自动拉起失败，用环境变量 FIT_UMI_OCR_PATH 指向 Umi-OCR.exe。"
                "或安装 Tesseract：从 UB-Mannheim/tesseract 下载安装包并勾选中文语言包，"
                "再把安装目录加入系统 PATH。")
    if p == "darwin":
        return "macOS：在终端执行  brew install tesseract tesseract-lang  （需先安装 Homebrew）。"
    if p.startswith("linux"):
        return ("Linux：在终端执行  sudo apt install tesseract-ocr tesseract-ocr-chi-sim "
                "tesseract-ocr-eng  （其他发行版用对应包管理器，如 dnf/yum/pacman）。")
    return "请安装 Tesseract OCR 或 Umi-OCR（开启 HTTP 接口）后重试。"


def ocr_pdf(path, langs=OCR_LANGS):
    """扫描件/无文字层 PDF：逐页渲染为图片后 OCR，拼接返回文字。需 OCR 引擎可用。

    受 PDF_OCR_MAX_PAGES 限制页数，防止超大扫描件拖死任务。
    """
    fitz = _load_fitz()
    doc = fitz.open(path)
    try:
        tmp_dir = os.path.join(tempfile.gettempdir(), "file_xray")
        os.makedirs(tmp_dir, exist_ok=True)
        pages_text = []
        for i, page in enumerate(doc):
            if PDF_OCR_MAX_PAGES and i >= PDF_OCR_MAX_PAGES:
                break
            pix = page.get_pixmap(dpi=200)
            tmp = os.path.join(tmp_dir, "pdfpage_%d_%d.png" % (os.getpid(), i))
            pix.save(tmp)
            try:
                t = ocr_image(tmp, langs)
                if t:
                    pages_text.append(t)
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        return "\n".join(pages_text).strip()
    finally:
        doc.close()

# ----------------------------------------------------------------------------
# 指纹追认：文件改名 / 移动 / 所在目录改名 / 根目录改名后，把标注迁到新路径
# ----------------------------------------------------------------------------
@router.get("/api/raw/{fid}")
def raw_file(fid: int):
    conn = get_db()
    r = conn.execute("SELECT path, name FROM files WHERE id=?", (fid,)).fetchone()
    conn.close()
    if not r or not os.path.isfile(r["path"]):
        raise HTTPException(404, "文件缺失或已被删除")
    mt, _ = mimetypes.guess_type(r["path"])
    return FileResponse(r["path"], media_type=mt or "application/octet-stream",
                        filename=r["name"], content_disposition_type="inline")

@router.get("/api/pdf/{fid}/pages")
def pdf_pages(fid: int):
    """PDF 总页数（供浏览器内置 OCR 分页拉取）。"""
    conn = get_db()
    r = conn.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
    conn.close()
    if not r or not os.path.isfile(r["path"]):
        raise HTTPException(404, "文件缺失或已被删除")
    try:
        fitz = _load_fitz()
        doc = fitz.open(r["path"])
        try:
            n = doc.page_count
        finally:
            doc.close()
    except Exception as e:
        raise HTTPException(500, "无法读取 PDF：%s" % e)
    return {"pages": n}

@router.get("/api/pdf/{fid}/page/{num}")
def pdf_page_png(fid: int, num: int, dpi: int = 200):
    """把 PDF 第 num 页（1 起始）渲染成 PNG，供浏览器内置 OCR 逐页识别。"""
    dpi = max(72, min(dpi, 300))
    conn = get_db()
    r = conn.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
    conn.close()
    if not r or not os.path.isfile(r["path"]):
        raise HTTPException(404, "文件缺失或已被删除")
    try:
        fitz = _load_fitz()
        doc = fitz.open(r["path"])
        try:
            if num < 1 or num > doc.page_count:
                raise HTTPException(404, "页码超出范围")
            png = doc[num - 1].get_pixmap(dpi=dpi).tobytes("png")
        finally:
            doc.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, "页面渲染失败：%s" % e)
    return Response(content=png, media_type="image/png")

OCR_AVAIL = None
OCR_ERR = ""
OCR_LANG_LIST = []

def ocr_check():
    """按当前引擎设置判断 OCR 是否可用。

    - umi：只要求 Umi-OCR HTTP 服务在线（不检测 tesseract）
    - tesseract：只检测 tesseract + pytesseract
    - auto：tesseract 可用，或 Umi-OCR 在线，二者其一即可
    Umi 在线状态每次实时探测（服务可能后启动），不缓存。
    """
    engine = (OCR_ENGINE or "auto").lower()
    if engine in ("umi", "auto"):
        _ensure_umi_running()      # 已安装但未运行时按需拉起
    if engine == "umi":
        if umi_reachable():
            return True, "", []
        return False, "Umi-OCR 服务不可达（%s），请确认其已启动并在「全局设置 → 开放API端口」开启 HTTP 接口" % UMI_OCR_URL, []
    global OCR_AVAIL, OCR_ERR, OCR_LANG_LIST
    if OCR_AVAIL is None:
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
    if OCR_AVAIL:
        return True, "", OCR_LANG_LIST
    if engine == "auto" and umi_reachable():
        return True, "", []          # auto 模式：tesseract 缺失但 Umi-OCR 在线，同样可用
    return OCR_AVAIL, OCR_ERR, OCR_LANG_LIST

_umi_launched = False          # 本进程是否已尝试启动过 Umi-OCR（仅一次）
_umi_down_until = 0.0          # 负向缓存：Umi-OCR 不可达时，60s 内不再发起探测（省去每次 2s 超时）

def umi_reachable():
    import urllib.request
    import urllib.error
    global _umi_down_until
    now = time.time()
    if _umi_down_until and now < _umi_down_until:
        return False
    try:
        urllib.request.urlopen(UMI_OCR_URL, timeout=2)
        _umi_down_until = 0.0
        return True
    except urllib.error.HTTPError:
        _umi_down_until = 0.0
        return True                  # 有 HTTP 响应（含 404）即说明服务在线
    except Exception:
        _umi_down_until = now + 60
        return False


def _find_umi_exe():
    """按环境变量与若干常见路径定位 Umi-OCR 可执行文件；找不到返回 None。"""
    candidates = []
    if UMI_OCR_PATH:
        candidates.append(UMI_OCR_PATH)
    for base in (r"C:\Program Files", r"C:\Program Files (x86)",
                 os.path.expanduser("~"), os.path.dirname(BASE_DIR)):
        candidates.append(os.path.join(base, "Umi-OCR", "Umi-OCR.exe"))
        candidates.append(os.path.join(base, "Umi-OCR.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _ensure_umi_running():
    """Umi-OCR 在线则直接返回；否则按需尝试启动一次（后台脱离进程树），再探测。

    仅当能定位到可执行文件且未禁用（FIT_UMI_OCR_AUTOSTART=0）时才会拉起。
    注意：Umi-OCR 的 HTTP 接口需在「全局设置 → 开放API端口」中开启一次（会被记住），
    本函数只负责“启动进程”，端口是否监听取决于该设置。
    """
    global _umi_launched
    if umi_reachable():
        return True
    if _umi_launched or str(UMI_OCR_AUTOSTART).lower() in ("0", "false", "no"):
        return False
    exe = _find_umi_exe()
    if not exe:
        _ocr_log.warning("Umi-OCR 未运行且未找到可执行文件，跳过自动拉起（可设置 FIT_UMI_OCR_PATH）")
        return False
    try:
        if sys.platform.startswith("win"):
            # 新建进程组，使其脱离后端进程树独立存活（后端重启不影响它）
            subprocess.Popen([exe], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                             close_fds=True)
        else:
            subprocess.Popen([exe], start_new_session=True, close_fds=True)
        _umi_launched = True
        _ocr_log.info("已尝试后台启动 Umi-OCR：%s", exe)
    except Exception as e:
        _ocr_log.warning("启动 Umi-OCR 失败：%s", e)
        return False
    for _ in range(12):          # 最多等 12s 让其 HTTP 接口就绪
        time.sleep(1)
        if umi_reachable():
            return True
    return umi_reachable()

def pending_scan_pdf_ids(conn):
    """扫描件待识别池：在磁盘上、无文字内容的 PDF，返回 (id, name) 列表。"""
    rows = conn.execute(
        "SELECT id, name, path FROM files WHERE missing=0 AND LOWER(ext) IN ('.pdf')"
        " AND (content IS NULL OR content='')").fetchall()
    return [(r["id"], r["name"]) for r in rows if os.path.isfile(r["path"])]


_backlog_job_id = None


def _start_pdf_backlog_ocr():
    """对全部待识别扫描件 PDF 启动后台 OCR 任务（引擎可用且开关打开时）。

    返回 job_id；无可做或引擎不可用时返回 None。
    多根目录扫描会各自触发一次，已有任务在跑时不重复起。
    """
    global _backlog_job_id
    if not OCR_PDF_ENABLED or not ocr_is_available():
        return None
    if _backlog_job_id and ocr_jobs.get(_backlog_job_id) \
            and not ocr_jobs[_backlog_job_id].get("finished"):
        return None                      # 已有补识别任务在跑
    conn = get_db()
    try:
        items = pending_scan_pdf_ids(conn)
    finally:
        conn.close()
    if not items:
        return None
    ids = [i for i, _ in items]
    jid = str(int(time.time() * 1000)) + "b"      # b = backlog，避免与手动任务撞号
    ocr_jobs[jid] = {"total": len(ids), "done": 0, "current": "", "error": None,
                     "finished": False, "kind": "pdf"}
    _backlog_job_id = jid
    threading.Thread(target=ocr_worker, args=(ids, jid), daemon=True).start()
    print("扫描件 PDF 后台补识别已启动：%d 个文件" % len(ids))
    return jid


ocr_jobs = {}

def ocr_worker(ids, jid):
    conn = get_db()
    try:
        total = len(ids); done = 0
        for fid in ids:
            ocr_jobs[jid]["current"] = ""
            r = conn.execute("SELECT id,path,ext,name,content FROM files WHERE id=?", (fid,)).fetchone()
            if not r:
                done += 1; ocr_jobs[jid]["done"] = done; continue
            ext = r["ext"]; name = r["name"]; path = r["path"]
            ocr_jobs[jid]["current"] = name
            cat = categorize(ext)
            if cat not in ("image", "pdf"):
                done += 1; ocr_jobs[jid]["done"] = done; continue
            if not os.path.isfile(path):
                done += 1; ocr_jobs[jid]["done"] = done; continue
            try:
                if cat == "image" and os.path.getsize(path) > OCR_MAX_BYTES:
                    raise Exception("图片过大已跳过（>%dMB）" % (OCR_MAX_BYTES // 1024 // 1024))
                if cat == "pdf":
                    # 补识别（原本无内容）：先试文字层（快且精确），无文字层才 OCR；
                    # 显式重新识别（已有内容）：直接 OCR（用户想重识）。
                    text = ""
                    src = "ocr"
                    if not (r["content"] or "").strip():
                        try:
                            text = extract_pdf_text(path)[:PDF_TEXT_LIMIT]
                        except Exception:
                            text = ""
                        if (text or "").strip():
                            src = "text_layer"
                    if not (text or "").strip():
                        text = ocr_pdf(path)
                    if not (text or "").strip():
                        raise Exception("未识别出文字")
                    # PDF 识别结果直接进入 content（即界面“PDF 文字”），不进暂存
                    conn.execute("UPDATE files SET content=?, content_source=? WHERE id=?",
                                 (text[:PDF_TEXT_LIMIT], src, fid))
                else:
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

@router.get("/api/ocr/check")
def ocr_check_ep():
    avail, err, langs = ocr_check()
    return {"available": avail, "langs": langs, "ocr_langs": OCR_LANGS,
            "engine": OCR_ENGINE, "umi_url": UMI_OCR_URL,
            "umi_reachable": umi_reachable(), "error": err,
            "install_guide": ocr_install_guide(),
            "wasm_available": wasm_available()}


@router.get("/api/ocr/pending")
def ocr_pending_ep():
    """扫描件待识别池：无文字内容的 PDF 清单（供前端一键补识别）。"""
    conn = get_db()
    try:
        items = pending_scan_pdf_ids(conn)
    finally:
        conn.close()
    return {"count": len(items),
            "ids": [i for i, _ in items], "names": [n for _, n in items]}


@router.post("/api/ocr/pdf_ids")
async def ocr_pdf_ids_ep(request: Request):
    """从所选文件中筛出 PDF（浏览器内置 OCR 仅支持 PDF）。"""
    data = await request.json()
    ids = [int(i) for i in (data.get("ids") or []) if str(i).strip().isdigit()]
    if not ids:
        raise HTTPException(400, "未选择任何文件")
    conn = get_db()
    qmarks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id, ext FROM files WHERE id IN ({qmarks})", ids).fetchall()
    conn.close()
    out = [r["id"] for r in rows if categorize(r["ext"]) == "pdf"]
    return {"ids": out, "skipped": len(ids) - len(out)}

@router.post("/api/ocr")
async def start_ocr(request: Request):
    data = await request.json()
    ids = data.get("ids", [])
    if not ids:
        raise HTTPException(400, "未选择任何文件")
    avail, err, _ = ocr_check()
    if not avail:
        raise HTTPException(409, "OCR 当前不可用：" + err)
    jid = str(int(time.time() * 1000))
    ocr_jobs[jid] = {"total": len(ids), "done": 0, "current": "", "error": None, "finished": False}
    t = threading.Thread(target=ocr_worker, args=(ids, jid), daemon=True)
    t.start()
    return {"job_id": jid}

@router.get("/api/ocr/{jid}")
def ocr_progress(jid: str):
    j = ocr_jobs.get(jid)
    if not j:
        raise HTTPException(404, "无此识别任务")
    return j

# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------
