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

# ----------------------------------------------------------------------------
# 模型缓存 / 自动下载 / 失败原因可读化
# ----------------------------------------------------------------------------

def _short_err(exc, limit=240):
    """原始异常摘要：压成一行并限长，保留类型信息便于排查。"""
    s = " ".join(str(exc).split()) or exc.__class__.__name__
    return s if len(s) <= limit else s[:limit] + "…"


def classify_error(exc, model="", stage=""):
    """把转写/模型加载阶段的异常翻成“可读原因 + 建议动作”。

    返回 {code, reason, action}；本身绝不抛异常，未知异常保留原始摘要。
    """
    name = exc.__class__.__name__
    low = ("%s %s" % (name, str(exc))).lower()
    model = model or "该模型"
    in_model = "模型" in (stage or "")

    if isinstance(exc, PermissionError) or "permission denied" in low:
        return {"code": "no_permission",
                "reason": "没有读取模型目录的权限" if in_model else "没有读取该文件的权限",
                "action": "检查文件/目录权限，或把文件复制到有权限的目录后重试"}

    if not in_model and isinstance(exc, FileNotFoundError):
        return {"code": "file_missing",
                "reason": "文件不存在或已被移动/删除（索引里的路径可能已失效）",
                "action": "重新扫描该目录后再试"}

    if isinstance(exc, ValueError) and "提示词" in str(exc):
        return {"code": "prompt_invalid",
                "reason": "提示词不合法：%s" % _short_err(exc),
                "action": "把提示词改为不超过 500 字的纯文本后重试"}

    # 注意：huggingface_hub 的 LocalEntryNotFoundError 继承 FileNotFoundError，
    # 因此“下载/离线”判定必须排在一般的文件缺失之前。
    if any(k in low for k in ("snapshot", "huggingface", "hfhub", "localentrynotfound",
                              "offline", "hf_hub_offline", "outgoing traffic",
                              "502", "503", "504", "timed out", "timeout",
                              "connect", "max retries", "getaddrinfo", "name or service",
                              "network", "ssl", "proxy")):
        return {"code": "model_download_failed",
                "reason": "本地没有转写模型 %s，自动下载失败（网络不可用或 HuggingFace 服务异常）" % model,
                "action": "改用本机已缓存的档位（base / small）后重试；或等网络恢复后重试（首次使用需先下载模型）"}

    if in_model and isinstance(exc, FileNotFoundError):
        return {"code": "model_missing",
                "reason": "模型文件缺失或已被删除",
                "action": "在设置里重选档位（base / small）或重新下载模型"}

    if any(k in low for k in ("model.bin", "unable to open file", "invalid model", "corrupt",
                              "not a valid model", "failed to load model", "model directory",
                              "does not appear to be")):
        return {"code": "model_invalid",
                "reason": "模型文件不完整或已损坏，无法加载",
                "action": "删除该档位的缓存目录后重新下载，或改用 base / small 重试"}

    if any(k in low for k in ("ffmpeg", "av.error", "invalid data found", "could not open",
                              "decoder", "no audio", "audio stream", "does not contain any stream",
                              "unsupported codec", "invalid argument")):
        return {"code": "decode_failed",
                "reason": "音频/视频无法解码（文件损坏、编码不受支持或没有音轨）",
                "action": "先确认文件能正常播放且有声音；必要时转成 mp3/wav 后再试"}

    if "未转写出文字" in str(exc):
        return {"code": "no_speech",
                "reason": "没有识别到语音内容（可能是静音、纯音乐或音量过低）",
                "action": "确认该文件含清晰人声；可先关闭 VAD 或改用 small 档位再试"}

    if "文件过大" in str(exc):
        return {"code": "too_large",
                "reason": _short_err(exc),
                "action": "裁剪或压缩该文件后重试，或调高环境变量 FIT_TRANSCRIBE_MAX_MB"}

    return {"code": "unknown",
            "reason": "转写失败：%s" % _short_err(exc),
            "action": "查看 logs/xray.log 里的完整堆栈；若持续出现，请反馈该文件类型与异常摘要"}


def _model_repo_id(name):
    return name if "/" in name else "Systran/faster-whisper-%s" % name


def _cached_model_dir(name):
    """本地缓存里的模型目录；未缓存/不完整返回 None（只查本地，不联网）。"""
    if not name:
        return None
    if os.path.isdir(name):
        return name
    try:
        from huggingface_hub import snapshot_download
        return snapshot_download(_model_repo_id(name), local_files_only=True)
    except Exception:
        return None


def _make_download_tqdm(report):
    """构造 huggingface_hub 可用的 tqdm 类，把“下载字节”回调进任务进度。

    huggingface_hub 会给 snapshot_download 建两个进度条：transfer（网络收包字节）
    与 reconstruct（落盘字节）。这里只转发 transfer，避免重复计数。
    """
    class _DownloadTqdm:
        def __init__(self, *args, **kwargs):
            self._name = str(kwargs.get("name") or "")
            self.total = int(kwargs.get("total") or 0)
            self.n = int(kwargs.get("initial") or 0)
            self._report()

        def _report(self):
            if self._name.endswith("transfer"):
                try:
                    report(self.n, self.total)
                except Exception:
                    pass

        def update(self, n=1):
            try:
                self.n += int(n or 0)
            except Exception:
                pass
            self._report()
            return True

        def refresh(self):
            self._report()

        def set_description(self, *args, **kwargs):
            pass

        set_description_str = set_description

        def set_postfix_str(self, *args, **kwargs):
            pass

        def reset(self, total=None):
            if total:
                self.total = int(total)

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return _DownloadTqdm


def _ensure_model_local(name, on_download=None, on_stage=None):
    """返回本地模型目录；未缓存时自动下载（保留“首次使用自动下载”的默认行为）。

    下载字节进度通过 on_download(done_bytes, total_bytes) 回调；完成后把本地目录
    交给 WhisperModel，避免二次联网。
    """
    name = (name or "base").strip() or "base"
    local = _cached_model_dir(name)
    if local:
        return local
    if on_stage:
        on_stage("下载模型 %s" % name)
    _log.info("模型 %s 未在本机缓存，开始自动下载", name)
    from huggingface_hub import snapshot_download
    tqdm_class = _make_download_tqdm(on_download) if on_download else None
    path = snapshot_download(_model_repo_id(name), tqdm_class=tqdm_class)
    _log.info("模型 %s 下载完成：%s", name, path)
    return path

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

def get_whisper_model(name=None, on_stage=None, on_download=None):
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
        # 阶段一：确认本地缓存；未缓存则自动下载（带字节进度）
        local = _ensure_model_local(name, on_download=on_download, on_stage=on_stage)
        # 阶段二：加载模型（此处异常多为文件损坏/不完整）
        if on_stage:
            on_stage("加载模型 %s" % name)
        _log.info("加载 Whisper 模型：%s（device=%s, compute=%s，本地目录=%s）",
                  name, device, compute, local)
        WHISPER_MODEL = WhisperModel(local, device=device, compute_type=compute)
        WHISPER_MODEL_ACTUAL = name
        return WHISPER_MODEL

def transcribe_path(path, on_progress=None, cfg=None, on_stage=None, on_download=None):
    """对音频/视频文件转写，返回文字。视频由 faster-whisper 经 PyAV 直接读取音轨。

    cfg 为任务开始时快照的 whisper_settings()（模型/语种/提示词/VAD）。
    on_progress(done_seconds, total_seconds)：按语音片段回调真实推进（total 未知时为 0）。
    on_stage(text)：阶段文案（检查/下载/加载模型、转写中）。
    on_download(done_bytes, total_bytes)：模型下载字节进度。
    """
    cfg = cfg or whisper_settings()
    model = get_whisper_model(cfg["model"], on_stage=on_stage, on_download=on_download)
    # 只记录来源与长度，不落提示词正文（避免把用户术语写进日志）
    if cfg.get("prompt"):
        _log.info("转写提示词：%s（%d 字符）", cfg.get("prompt_source", ""), len(cfg["prompt"]))
    else:
        _log.info("转写提示词：未启用（%s）", cfg.get("prompt_source", ""))

    def _collect(p):
        if on_stage:
            on_stage("转写中")
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

    def _extra():
        """jobs.extra：旧字段（errors/pending/stage/seg_*）保持不变，只追加新字段。"""
        j = transcribe_jobs.get(jid) or {}
        return {"errors": j.get("errors", []),
                "pending": j.get("pending", 0),
                "stage": j.get("stage", ""),
                "seg_done": j.get("seg_done", 0),
                "seg_total": j.get("seg_total", 0),
                "dl_done": j.get("dl_done", 0),
                "dl_total": j.get("dl_total", 0),
                "reason": j.get("reason"),
                "action": j.get("action"),
                "code": j.get("code"),
                "failed_stage": j.get("failed_stage")}

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
                       current=j.get("current", ""), extra=_extra())

        def _seg_progress(sec_done, sec_total):
            # 片段级进度：长音频不会停在 0/N 不动
            transcribe_jobs[jid]["seg_done"] = sec_done
            transcribe_jobs[jid]["seg_total"] = sec_total
            if sec_done > 0 and not transcribe_jobs[jid].get("stage", "").startswith("转写"):
                transcribe_jobs[jid]["stage"] = "转写中"
            _publish()

        def _stage(text):
            transcribe_jobs[jid]["stage"] = text
            _publish(force=True)

        def _download(done_bytes, total_bytes):
            """模型下载进度：字节数写进任务，阶段文案带百分比（前端再补 MB 数）。"""
            j = transcribe_jobs[jid]
            j["dl_done"] = int(done_bytes or 0)
            j["dl_total"] = int(total_bytes or 0)
            j["stage"] = "下载模型 %s" % cfg["model"]
            _publish()

        for fid in ids:
            transcribe_jobs[jid]["current"] = ""
            transcribe_jobs[jid]["stage"] = "读取文件"
            transcribe_jobs[jid]["seg_done"] = 0
            transcribe_jobs[jid]["seg_total"] = 0
            transcribe_jobs[jid]["dl_done"] = 0
            transcribe_jobs[jid]["dl_total"] = 0
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
                # 索引还在但文件已没了：给出可读原因，不再静默跳过
                info = classify_error(FileNotFoundError(path), cfg["model"], "读取文件")
                _log.error("转写跳过：文件不存在 file=%s path=%s", name, path)
                errors.append({"name": name, "error": "文件不存在：%s" % path,
                               "code": info["code"], "reason": info["reason"],
                               "action": info["action"], "stage": "读取文件"})
                transcribe_jobs[jid]["errors"] = errors
                if not transcribe_jobs[jid].get("reason"):
                    transcribe_jobs[jid]["reason"] = info["reason"]
                if not transcribe_jobs[jid].get("action"):
                    transcribe_jobs[jid]["action"] = info["action"]
                if not transcribe_jobs[jid].get("code"):
                    transcribe_jobs[jid]["code"] = info["code"]
                transcribe_jobs[jid]["failed_stage"] = "读取文件"
                transcribe_jobs[jid]["stage"] = "失败：读取文件"
                done += 1
                transcribe_jobs[jid]["done"] = done
                _publish(force=True)
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
                text = transcribe_path(path, on_progress=_seg_progress, cfg=seg_cfg,
                                       on_stage=_stage, on_download=_download)
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
                stage_now = transcribe_jobs[jid].get("stage", "")
                info = classify_error(e, cfg["model"], stage_now)
                # 完整堆栈落 logs/xray.log（不再只塞进 jobs.extra.errors）
                _log.exception("转写失败：file=%s stage=%s code=%s", name, stage_now, info["code"])
                errors.append({"name": name, "error": str(e), "code": info["code"],
                               "reason": info["reason"], "action": info["action"],
                               "stage": stage_now})
                transcribe_jobs[jid]["errors"] = errors
                if not transcribe_jobs[jid].get("reason"):
                    transcribe_jobs[jid]["reason"] = info["reason"]
                if not transcribe_jobs[jid].get("action"):
                    transcribe_jobs[jid]["action"] = info["action"]
                if not transcribe_jobs[jid].get("code"):
                    transcribe_jobs[jid]["code"] = info["code"]
                transcribe_jobs[jid]["failed_stage"] = stage_now or "转写"
                transcribe_jobs[jid]["stage"] = "失败：%s" % (stage_now or "转写")
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
        if status == "failed":
            # 失败原因必须是可读文案，不允许为空
            first = errors[0]
            transcribe_jobs[jid]["error"] = (first.get("reason") or first.get("error")
                                             or "转写失败")
        elif status == "partial":
            transcribe_jobs[jid]["stage"] = "部分完成（%d 个失败）" % len(errors)
        else:
            transcribe_jobs[jid]["stage"] = "处理完成"
        transcribe_jobs[jid]["finished"] = True
        job_update(jid, status=status, done=total, total=total,
                   current=transcribe_jobs[jid].get("current", ""),
                   error=transcribe_jobs[jid].get("error"), extra=_extra())
    except Exception as e:
        info = classify_error(e, cfg.get("model", ""),
                              transcribe_jobs.get(jid, {}).get("stage", ""))
        _log.exception("转写任务异常退出：jid=%s", jid)
        transcribe_jobs[jid]["error"] = info["reason"]
        transcribe_jobs[jid]["status"] = "failed"
        transcribe_jobs[jid]["finished"] = True
        transcribe_jobs[jid]["stage"] = "失败：%s" % (transcribe_jobs[jid].get("stage") or "转写任务")
        if not transcribe_jobs[jid].get("reason"):
            transcribe_jobs[jid]["reason"] = info["reason"]
        if not transcribe_jobs[jid].get("action"):
            transcribe_jobs[jid]["action"] = info["action"]
        if not transcribe_jobs[jid].get("code"):
            transcribe_jobs[jid]["code"] = info["code"]
        job_update(jid, status="failed", error=info["reason"], extra=_extra())
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
                            "seg_done": 0, "seg_total": 0,
                            "dl_done": 0, "dl_total": 0,
                            "reason": None, "action": None, "code": None,
                            "failed_stage": ""}
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
