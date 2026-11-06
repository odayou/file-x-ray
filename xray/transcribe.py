from fastapi import APIRouter, Request, HTTPException
from xray.config import *
from xray.scan import store_recognized
router = APIRouter()

WHISPER_AVAIL = None          # None=尚未检测, True/False=是否已安装
WHISPER_ERR = ""
WHISPER_MODEL = None
WHISPER_MODEL_ACTUAL = None   # 当前已加载模型对应的档位，设置改档后自动重载
WHISPER_LOCK = threading.Lock()


def whisper_settings():
    """每次转写重新读取设置（设置面板保存后，下一个任务即生效，无需重启）。

    中文提示词：实测 Whisper 的 zh 输出默认偏繁体，给一段简体中文 initial_prompt 可稳定
    转到简体（且不影响英文识别）。设 0/false/off/none 可关闭。
    """
    raw_lang = str(setting("FIT_WHISPER_LANG", "whisper_lang", "") or "").strip()
    raw_prompt = str(setting("FIT_WHISPER_PROMPT", "whisper_prompt",
                             "以下是普通话的句子。") or "").strip()
    return {
        "model": str(setting("FIT_WHISPER_MODEL", "whisper_model", "base") or "base").strip() or "base",
        "lang": raw_lang or None,          # None=自动检测语种
        "prompt": None if raw_prompt.lower() in ("0", "false", "no", "off", "none")
                  else (raw_prompt or None),
        # 内置 VAD（切掉静音段）：实测对短音频/干净音频无收益，仅在长录音含大段静音时略好，
        # 默认关闭；设 1 开启。
        "vad": str(setting("FIT_WHISPER_VAD", "whisper_vad", "0")).lower()
               not in ("0", "false", "no", "off"),
    }


TRANSCRIBE_MAX_BYTES = int(os.environ.get("FIT_TRANSCRIBE_MAX_MB", "300")) * 1024 * 1024

# ----------------------------------------------------------------------------
# 逐次提示词（优先级：本次任务 > 该文件上次 > 全局默认）
# 说明：Whisper 的 initial_prompt 只有约 224 token 预算（faster-whisper 会保留
# previous_tokens 的末尾 223 个），超长时开头的词会被丢弃，故这里只做恶性输入上限。
# ----------------------------------------------------------------------------
PROMPT_OFF = {"off", "0", "false", "no", "none", "关闭"}
RECENT_KEY = "prompt_recent"
RECENT_MAX = 8


def prompt_is_off(text):
    return str(text or "").strip().lower() in PROMPT_OFF


def validate_task_prompt(raw):
    """校验 POST /api/transcribe 的 prompt 参数。
    None = 未提供（走 文件上次 > 全局默认）；空串 = 回到全局默认；off = 本次不使用提示词。
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("提示词必须是字符串")
    s = raw.strip()
    if len(s) > PROMPT_MAX_LEN:
        raise ValueError("提示词过长（最多 %d 个字符）" % PROMPT_MAX_LEN)
    return s


def resolve_prompt(task_prompt, file_prompt, global_prompt):
    """返回 (实际提示词 或 None, 来源说明, 要写回 files.prompt 的值 或 None=不改)。"""
    file_prompt = (file_prompt or "").strip()
    if task_prompt is None:                    # 接口未传：该文件上次 > 全局默认
        if file_prompt:
            if prompt_is_off(file_prompt):
                return None, "该文件上次：关闭", None
            return file_prompt, "该文件上次使用", None
        if global_prompt:
            return global_prompt, "全局默认", None
        return None, "全局默认：关闭", None
    if prompt_is_off(task_prompt):             # 显式关闭：不注入任何提示词
        return None, "本次指定：关闭", "off"
    if not task_prompt:                        # 显式空：回到全局默认（保留简体行为）
        if global_prompt:
            return global_prompt, "全局默认", ""
        return None, "全局默认：关闭", ""
    return task_prompt, "本次指定", task_prompt  # 本次任务最高优先级


def recent_prompts(conn=None):
    """最近使用过的自定义提示词（复用 app_meta，不引入新存储）。"""
    own = conn is None
    conn = conn or get_db()
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key=?", (RECENT_KEY,)).fetchone()
        try:
            lst = json.loads(row["value"]) if row else []
        except Exception:
            lst = []
        return [x for x in lst if isinstance(x, str) and x.strip()][:RECENT_MAX]
    except Exception:
        return []
    finally:
        if own:
            conn.close()


def remember_prompt(conn, text):
    """把本次显式使用的提示词记入最近列表（去重、最新在前）。"""
    text = (text or "").strip()
    if not text or prompt_is_off(text):
        return
    lst = [x for x in recent_prompts(conn) if x != text]
    lst.insert(0, text)
    conn.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)",
                 (RECENT_KEY, json.dumps(lst[:RECENT_MAX], ensure_ascii=False)))

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

def get_whisper_model(name=None):
    global WHISPER_MODEL, WHISPER_MODEL_ACTUAL
    name = (name or whisper_settings()["model"]).strip() or "base"
    with WHISPER_LOCK:
        if WHISPER_MODEL is not None and WHISPER_MODEL_ACTUAL == name:
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
        _log.info("加载 Whisper 模型：%s（device=%s, compute=%s）", name, device, compute)
        WHISPER_MODEL = WhisperModel(name, device=device, compute_type=compute)
        WHISPER_MODEL_ACTUAL = name
        return WHISPER_MODEL

def transcribe_path(path, on_progress=None, cfg=None):
    """对音频/视频文件转写，返回文字。视频由 faster-whisper 经 PyAV 直接读取音轨。

    cfg 为任务开始时快照的 whisper_settings()（模型/语种/提示词/VAD）。
    on_progress(done_seconds, total_seconds)：按语音片段回调真实推进（total 未知时为 0）。
    """
    cfg = cfg or whisper_settings()
    model = get_whisper_model(cfg["model"])
    # 只记录来源与长度，不落提示词正文（避免把用户术语写进日志）
    if cfg.get("prompt"):
        _log.info("转写提示词：%s（%d 字符）", cfg.get("prompt_source", ""), len(cfg["prompt"]))
    else:
        _log.info("转写提示词：未启用（%s）", cfg.get("prompt_source", ""))

    def _collect(p):
        segments, info = model.transcribe(p, language=cfg["lang"], beam_size=5,
                                          initial_prompt=cfg["prompt"],
                                          vad_filter=cfg["vad"])
        total = float(getattr(info, "duration", 0) or 0)
        parts = []
        for seg in segments:
            parts.append(seg.text or "")
            if on_progress:
                try:
                    end = float(getattr(seg, "end", 0) or 0)
                    on_progress(min(end, total) if total else end, total)
                except Exception:
                    pass
        return "".join(parts).strip()

    try:
        return _collect(path)
    except Exception as e:
        # 视频若直接读取失败，尝试用 ffmpeg 抽音轨后再试一次
        if shutil.which("ffmpeg"):
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            try:
                subprocess.run(["ffmpeg", "-y", "-i", path, "-vn", tmp.name],
                               check=True, capture_output=True)
                return _collect(tmp.name)
            finally:
                try:
                    os.remove(tmp.name)
                except Exception:
                    pass
        raise

transcribe_jobs = {}

def transcribe_worker(ids, jid, cfg=None, task_prompt=None):
    cfg = cfg or whisper_settings()
    conn = get_db()
    try:
        total = len(ids)
        done = 0
        errors = []
        last_pub = [0.0]

        def _publish(force=False):
            """进度落 jobs 表（重启后可回放），节流 1s。"""
            now = time.time()
            if not force and now - last_pub[0] < 1.0:
                return
            last_pub[0] = now
            j = transcribe_jobs.get(jid) or {}
            job_update(jid, done=j.get("done", 0), total=j.get("total", 0),
                       current=j.get("current", ""),
                       extra={"errors": errors,
                              "pending": j.get("pending", 0),
                              "stage": j.get("stage", ""),
                              "seg_done": j.get("seg_done", 0),
                              "seg_total": j.get("seg_total", 0)})

        def _seg_progress(sec_done, sec_total):
            # 片段级进度：长音频不会停在 0/N 不动
            transcribe_jobs[jid]["seg_done"] = sec_done
            transcribe_jobs[jid]["seg_total"] = sec_total
            # 首个片段到达即说明模型已加载完毕，阶段文案切到“转写中”
            if sec_done > 0 and transcribe_jobs[jid].get("stage", "").startswith("加载模型"):
                transcribe_jobs[jid]["stage"] = "转写中"
            _publish()

        def _stage(text):
            transcribe_jobs[jid]["stage"] = text
            _publish()

        for fid in ids:
            transcribe_jobs[jid]["current"] = ""
            transcribe_jobs[jid]["stage"] = "读取文件"
            transcribe_jobs[jid]["seg_done"] = 0
            transcribe_jobs[jid]["seg_total"] = 0
            _publish()
            r = conn.execute("SELECT id,path,ext,name,prompt FROM files WHERE id=?", (fid,)).fetchone()
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
                # 逐文件解析提示词：本次任务 > 该文件上次 > 全局默认
                eff_prompt, prompt_source, store_prompt = resolve_prompt(
                    task_prompt, r["prompt"], cfg["prompt"])
                seg_cfg = dict(cfg, prompt=eff_prompt, prompt_source=prompt_source)
                transcribe_jobs[jid]["prompt"] = eff_prompt or ""
                transcribe_jobs[jid]["prompt_source"] = prompt_source
                _stage("加载模型 %s（首次使用需下载）" % cfg["model"])
                text = transcribe_path(path, on_progress=_seg_progress, cfg=seg_cfg)
                if not (text or "").strip():
                    raise Exception("未转写出文字")
                _stage("写入索引")
                if store_recognized(conn, fid, text, "transcribe"):
                    transcribe_jobs[jid]["pending"] = transcribe_jobs[jid].get("pending", 0) + 1
                # 只有本次显式指定（含空/off）才改写该文件保存的提示词
                if store_prompt is not None:
                    conn.execute("UPDATE files SET prompt=? WHERE id=?", (store_prompt, fid))
                    conn.commit()
                _stage("完成")
            except Exception as e:
                # 失败时不写入，保留原有内容；先回滚未提交写事务，避免自锁
                try:
                    conn.rollback()
                except Exception:
                    pass
                errors.append({"name": name, "error": str(e)})
                transcribe_jobs[jid]["errors"] = errors
            done += 1
            transcribe_jobs[jid]["done"] = done
            _publish(force=True)
        # 文本已由 store_recognized 提交落库，先置完成标记，再做不持锁的 job_update
        if errors and len(errors) >= total:
            status = "failed"
        elif errors:
            status = "partial"
        else:
            status = "done"
        transcribe_jobs[jid]["status"] = status
        transcribe_jobs[jid]["stage"] = "处理完成" if status != "failed" else "转写失败"
        transcribe_jobs[jid]["finished"] = True
        job_update(jid, status=status, done=total, total=total,
                   current=transcribe_jobs[jid].get("current", ""),
                   extra={"errors": errors,
                          "pending": transcribe_jobs[jid].get("pending", 0),
                          "stage": transcribe_jobs[jid]["stage"],
                          "seg_done": transcribe_jobs[jid].get("seg_done", 0),
                          "seg_total": transcribe_jobs[jid].get("seg_total", 0)})
    except Exception as e:
        transcribe_jobs[jid]["error"] = str(e)
        transcribe_jobs[jid]["status"] = "failed"
        transcribe_jobs[jid]["finished"] = True
        job_update(jid, status="failed", error=str(e),
                   extra={"errors": transcribe_jobs[jid].get("errors", []),
                          "stage": transcribe_jobs[jid].get("stage", "")})
    finally:
        conn.close()

@router.get("/api/transcribe/check")
def transcribe_check():
    avail, err = whisper_check()
    cfg = whisper_settings()
    return {"available": avail, "model": cfg["model"],
            "lang": cfg["lang"] or "auto", "error": err,
            "prompt": bool(cfg["prompt"]), "prompt_text": cfg["prompt"] or "",
            "vad": cfg["vad"]}


@router.get("/api/transcribe/prompts")
def transcribe_prompts():
    """逐次提示词的辅助信息：全局默认值、最近使用、长度上限。"""
    cfg = whisper_settings()
    conn = get_db()
    try:
        recent = recent_prompts(conn)
    finally:
        conn.close()
    return {"global_prompt": cfg["prompt"] or "", "recent": recent,
            "max_len": PROMPT_MAX_LEN, "off_token": "off",
            "budget_tokens": 223}

@router.post("/api/transcribe")
async def start_transcribe(request: Request):
    data = await request.json()
    ids = data.get("ids", [])
    if not ids:
        raise HTTPException(400, "未选择任何文件")
    avail, err = whisper_check()
    if not avail:
        raise HTTPException(409, "未安装转录依赖 faster-whisper：" + err)
    try:
        task_prompt = validate_task_prompt(data.get("prompt"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    jid = str(int(time.time() * 1000))
    cfg = whisper_settings()          # 任务开始时快照：设置保存后下一次转写即采用新值
    transcribe_jobs[jid] = {"total": len(ids), "done": 0, "current": "", "error": None,
                            "finished": False, "status": "running", "stage": "",
                            "model": cfg["model"],
                            "prompt": "", "prompt_source": "",
                            "seg_done": 0, "seg_total": 0}
    job_create(jid, "transcribe", "", 0, len(ids))
    # 最近使用：只记显式输入的自定义提示词，便于下次从下拉直接复用
    if task_prompt and not prompt_is_off(task_prompt):
        try:
            conn = get_db()
            remember_prompt(conn, task_prompt)
            conn.commit()
            conn.close()
        except Exception:
            _log.debug("记录最近提示词失败", exc_info=True)
    t = threading.Thread(target=transcribe_worker,
                         args=(ids, jid, cfg, task_prompt), daemon=True)
    t.start()
    return {"job_id": jid}

@router.get("/api/transcribe/{jid}")
def transcribe_progress(jid: str):
    j = transcribe_jobs.get(jid)
    if not j:
        # 内存态缺失（服务重启）：回落到 jobs 表，避免误报 404
        row = job_get(jid)
        if not row or row["kind"] != "transcribe":
            raise HTTPException(404, "无此转写任务")
        return job_progress_from_row(row, "transcribe")
    return j

# ----------------------------------------------------------------------------
# OCR（图片文字识别：Umi-OCR 优先 + tesseract 兜底）
# ----------------------------------------------------------------------------
