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

// ---------- OCR（图片/扫描件PDF -> 文字，Umi-OCR 优先 + tesseract 兜底） ----------
async function startOcr(ids){
  // 引擎未就绪：直接给出跨平台安装引导，不再空跑请求
  if(!state.ocr || !state.ocr.available){
    const g=(state.ocr && state.ocr.install_guide) || '请先安装 Tesseract OCR 或 Umi-OCR（开启 HTTP 接口）';
    toast('当前环境未启用 OCR 引擎，无法识别');
    showOcrGuide(g);
    return;
  }
  let jid;
  try{ const r=await apiPost('/api/ocr',{ids}); jid=r.job_id; }
  catch(e){ toast('启动识别失败: '+e.message); return; }
  document.getElementById('ocrOverlay').classList.add('show');
  pollOcr(jid);
}

// 顶栏常驻 OCR 状态徽标：可用性 + 扫描件待识别数（一键补识别入口）
let _ocrPendingToasted=false;
async function refreshOcrBadge(){
  let r;
  try{ r=await apiGet('/api/ocr/check'); }catch(e){ r={available:false}; }
  state.ocr=r;
  updateOcrBadge(r);
  if(r && r.available){
    let p=null;
    try{ p=await apiGet('/api/ocr/pending'); }catch(e){}
    applyOcrPending(p);
  }
}
function updateOcrBadge(r){
  const b=document.getElementById('ocrBadge');
  if(!b) return;
  if(r && r.available){
    b.textContent='OCR ✓ 已启用';
    b.className='ocr-badge ok';
    b.title='OCR 引擎已就绪（Umi-OCR 优先 / tesseract 兜底）';
    b.onclick=null;
  }else{
    b.textContent='OCR ✕ 未启用';
    b.className='ocr-badge warn';
    b.title='点击查看如何启用 OCR（识别图片 / 扫描件 PDF）';
    b.onclick=()=>showOcrGuide((r&&r.install_guide)||'请安装 Tesseract OCR 或 Umi-OCR（开启 HTTP 接口）');
  }
}
function applyOcrPending(p){
  const b=document.getElementById('ocrBadge');
  if(!b || !(state.ocr&&state.ocr.available)) return;
  state.ocrPending=p||{count:0,ids:[]};
  const n=state.ocrPending.count||0;
  if(n>0){
    b.textContent='OCR ✓ · '+n+' 待识别';
    b.title='检测到 '+n+' 个扫描件 PDF 尚未识别，点击开始批量 OCR';
    b.onclick=()=>{
      const ids=(state.ocrPending&&state.ocrPending.ids)||[];
      if(!ids.length) return;
      if(confirm('对 '+ids.length+' 个扫描件 PDF 批量 OCR 识别？\n（在后台逐个执行，进度实时显示）'))
        startOcr(ids);
    };
    if(!_ocrPendingToasted){
      _ocrPendingToasted=true;
      toast('检测到 '+n+' 个扫描件 PDF 未识别，点击顶栏「OCR 徽标」可批量补识别');
    }
  }else{
    b.textContent='OCR ✓ 已启用';
    b.onclick=null;
  }
}

// OCR 安装引导弹窗（跨平台文案）
function showOcrGuide(text){
  const id='ocrGuideModal';
  let m=document.getElementById(id);
  if(m) m.remove();
  m=document.createElement('div');
  m.id=id; m.className='modal-mask';
  m.innerHTML=`<div class="modal-box" style="width:480px">
    <h3>🔧 启用 OCR（识别图片 / 扫描件 PDF 文字）</h3>
    <p style="font-size:13px;color:var(--muted);line-height:1.7">本机尚未检测到可用的 OCR 引擎。按你当前的系统安装其一即可，安装后刷新页面就能识别：</p>
    <div class="ocr-guide" style="white-space:pre-wrap">${esc(text)}</div>
    <p style="font-size:12px;color:var(--muted);line-height:1.6;margin-top:10px">
      · 推荐 <b>Umi-OCR</b>（中文更强）：启动后在设置里开启「HTTP 服务」即可，无需其他配置。<br>
      · <b>Tesseract</b> 更轻量：安装时勾选中文语言包，并把安装目录加入系统 PATH。<br>
      · 两者都装时，本工具优先用 Umi-OCR。
    </p>
    <div class="modal-actions"><button class="btn-primary btn-sm" id="ocrGuideClose">我知道了</button></div>
  </div>`;
  document.body.appendChild(m);
  m.querySelector('#ocrGuideClose').onclick=()=>m.remove();
  m.onclick=e=>{ if(e.target===m) m.remove(); };
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
  else { toast('识别完成，共处理 '+r.total+' 个文件'); }
  if(r.errors && r.errors.length){ toast('有 '+r.errors.length+' 个识别失败（原内容已保留未覆盖）'); }
  if(state.currentId) selectFile(state.currentId);   // PDF 文字会更新，刷新详情
  await loadStats(); await loadFiles();
  refreshOcrBadge();                                  // 待识别数已变化，刷新徽标
}
