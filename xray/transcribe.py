from fastapi import APIRouter, Request, HTTPException, Query, Body, UploadFile, Form, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse, Response
from xray.config import *
from xray.scan import store_recognized
router = APIRouter()

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

@router.get("/api/transcribe/check")
def transcribe_check():
    avail, err = whisper_check()
    return {"available": avail, "model": WHISPER_MODEL_NAME,
            "lang": WHISPER_LANG or "auto", "error": err}

@router.post("/api/transcribe")
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

@router.get("/api/transcribe/{jid}")
def transcribe_progress(jid: str):
    j = transcribe_jobs.get(jid)
    if not j:
        raise HTTPException(404, "无此转写任务")
    return j

# ----------------------------------------------------------------------------
# OCR（图片文字识别：Umi-OCR 优先 + tesseract 兜底）
# ----------------------------------------------------------------------------
