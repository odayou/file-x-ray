// 转写与 OCR 任务发起及进度轮询
// ---------- 转写（语音/视频 -> 文字） ----------
async function startTranscribe(ids){
  if(!state.whisper || !state.whisper.available){ toast('转写功能未安装：pip install faster-whisper'); return; }
  let jid;
  try{ const r=await apiPost('/api/transcribe',{ids}); jid=r.job_id; }
  catch(e){ toast('启动转写失败: '+e.message); return; }
  document.getElementById('transcribeOverlay').classList.add('show');
  pollTranscribe(jid);
}
async function pollTranscribe(jid){
  const r=await apiGet('/api/transcribe/'+jid);
  document.getElementById('trNum').textContent=r.done+' / '+r.total;
  document.getElementById('trCur').textContent = r.finished
    ? '处理完成' : (r.current ? ('当前: '+r.current) : '加载模型中…（首次使用需下载模型，请稍候）');
  if(!r.finished){ setTimeout(()=>pollTranscribe(jid), 800); return; }
  document.getElementById('transcribeOverlay').classList.remove('show');
  if(r.error){ toast('转写出错: '+r.error); }
  else if(r.pending){ toast('转写完成：'+r.pending+' 个文件已有旧内容，新结果已放入暂存等你确认'); }
  else { toast('转写完成，共处理 '+r.total+' 个文件'); }
  if(r.errors && r.errors.length){ toast('有 '+r.errors.length+' 个转写失败（原内容已保留未覆盖）'); }
  if(state.currentId) selectFile(state.currentId);
  await loadStats(); await loadFiles();
}

// ---------- OCR（图片 -> 文字，Umi-OCR 优先 + tesseract 兜底） ----------
async function startOcr(ids){
  let jid;
  try{ const r=await apiPost('/api/ocr',{ids}); jid=r.job_id; }
  catch(e){ toast('启动识别失败: '+e.message); return; }
  document.getElementById('ocrOverlay').classList.add('show');
  pollOcr(jid);
}
async function pollOcr(jid){
  const r=await apiGet('/api/ocr/'+jid);
  document.getElementById('ocrNum').textContent=r.done+' / '+r.total;
  document.getElementById('ocrCur').textContent = r.finished
    ? '处理完成' : (r.current ? ('当前: '+r.current) : '正在识别…');
  if(!r.finished){ setTimeout(()=>pollOcr(jid), 800); return; }
  document.getElementById('ocrOverlay').classList.remove('show');
  if(r.error){ toast('识别出错: '+r.error); }
  else if(r.pending){ toast('识别完成：'+r.pending+' 个文件已有旧内容，新结果已放入暂存等你确认'); }
  else { toast('识别完成，共处理 '+r.total+' 个图片'); }
  if(r.errors && r.errors.length){ toast('有 '+r.errors.length+' 个识别失败（原内容已保留未覆盖）'); }
  if(state.currentId) selectFile(state.currentId);
  await loadStats(); await loadFiles();
}
