// 转写与 OCR 任务发起及进度轮询
// ---------- 转写（语音/视频 -> 文字） ----------
let _trBusy=false;          // 连点闸门：同一批转写未结束前不再提交
let _trPollToken=0;         // 轮询令牌：新任务让旧循环退出
let _trLastDone=-1;         // 进度单调不减
let _trTotal=0;

function setTranscribeProgress(done, total, cur){
  const num=document.getElementById('trNum');
  const ce=document.getElementById('trCur');
  if(num){
    const text=total>0?(done+' / '+total):'';
    if(num.textContent!==text) num.textContent=text;   // 只在变化时写 DOM
  }
  if(ce && ce.textContent!==cur) ce.textContent=cur;
}

// 下载进度：优先显示百分比 + 已下载/总字节；hub 没给总大小时只显示已下载量
function dlProgressText(r){
  const d=Number(r.dl_done)||0, t=Number(r.dl_total)||0;
  if(!d && !t) return '';
  const mb=v=>(v/1048576).toFixed(1);
  if(t>0) return '（'+Math.min(100,Math.floor(d*100/t))+'%，'+mb(d)+' / '+mb(t)+' MB）';
  return '（已下载 '+mb(d)+' MB）';
}

// 成功/失败面板复位（开始新任务时调用）
function resetTranscribePanel(){
  const box=document.getElementById('trResult'), btn=document.getElementById('trClose'),
        title=document.getElementById('trTitle');
  if(box){ box.innerHTML=''; box.style.display='none'; }
  if(btn) btn.style.display='none';
  if(title) title.textContent='🎙 正在转写音频 / 视频…';
}

// 失败/部分失败：在面板里直接写“原因 + 建议 + 停在哪一步”，而不是只弹 toast
function showTranscribeResult(r){
  const box=document.getElementById('trResult'), btn=document.getElementById('trClose'),
        title=document.getElementById('trTitle');
  const errs=(r&&r.errors)||[];
  const failed=!!r && r.status==='failed';
  const rows=[];
  if(failed){
    const reason=r.reason||r.error||('全部 '+(r.total||errs.length)+' 个文件未能转写');
    rows.push('<div style="color:#c0392b"><b>失败原因：</b>'+esc(reason)+'</div>');
    if(r.action) rows.push('<div><b>建议：</b>'+esc(r.action)+'</div>');
    if(r.failed_stage) rows.push('<div style="color:var(--muted)">停在：'+esc(r.failed_stage)+'</div>');
  }else if(errs.length){
    const e=errs[0]||{};
    rows.push('<div style="color:#c0392b"><b>失败原因：</b>'+esc(e.reason||e.error||'转写失败')+'</div>');
    if(e.action) rows.push('<div><b>建议：</b>'+esc(e.action)+'</div>');
    if(errs.length>1) rows.push('<div style="color:var(--muted)">另有 '+(errs.length-1)+' 个文件失败（原有内容未被覆盖）</div>');
  }
  if(box){ box.innerHTML=rows.join(''); box.style.display=rows.length?'block':'none'; }
  if(btn) btn.style.display=rows.length?'inline-block':'none';
  if(title) title.textContent=failed?'🎙 转写失败':(errs.length?'🎙 部分文件转写失败':'🎙 转写完成');
}

document.addEventListener('DOMContentLoaded', function(){
  const btn=document.getElementById('trClose');
  if(btn) btn.addEventListener('click', function(){
    const ov=document.getElementById('transcribeOverlay');
    if(ov) ov.classList.remove('show');
  });
});

async function startTranscribe(ids, opts){
  if(_trBusy){ toast('转写任务正在进行中，请稍候'); return; }
  if(!state.whisper || !state.whisper.available){ toast('转写功能未安装：pip install faster-whisper'); return; }
  let jid;
  _trBusy=true;
  const body={ids};
  // undefined=不传（逐文件沿用各自上次/全局）；字符串（含空串、off）=本次显式指定
  if(opts && opts.prompt!==undefined) body.prompt=opts.prompt;
  try{ const r=await apiPost('/api/transcribe',body); jid=r.job_id; }
  catch(e){ _trBusy=false; toast('启动转写失败: '+e.message); return; }
  if(!jid){ _trBusy=false; toast('启动转写失败：未返回任务号'); return; }
  resetTranscribePanel();
  document.getElementById('transcribeOverlay').classList.add('show');
  pollTranscribe(jid);
}

async function pollTranscribe(jid){
  const token=++_trPollToken;
  _trLastDone=-1; _trTotal=0;
  let fails=0;
  let r=null;
  while(token===_trPollToken){
    try{ r=await apiGet('/api/transcribe/'+jid); fails=0; }
    catch(e){
      fails++;
      if(fails>=3){
        document.getElementById('transcribeOverlay').classList.remove('show');
        _trBusy=false;
        toast('转写进度读取失败: '+e.message);
        return;
      }
      setTranscribeProgress(_trLastDone, _trTotal, '读取进度失败，重试中（'+fails+'/3）…');
      await new Promise(res=>setTimeout(res, OCR_POLL_MS));
      continue;
    }
    const total=Math.max(_trTotal, Number(r.total)||0);
    let done=Number(r.done)||0;
    if(total) done=Math.min(done, total);
    if(done<_trLastDone) done=_trLastDone;
    _trTotal=total; _trLastDone=done;
    if(r.finished){
      const sd=Math.floor(Number(r.seg_done)||0), st=Math.floor(Number(r.seg_total)||0);
      const errs=r.errors||[];
      let fin='处理完成';
      if(r.status==='failed') fin='转写失败'+(r.failed_stage?('（停在：'+r.failed_stage+'）'):'');
      else if(errs.length) fin='部分完成（'+errs.length+' 个失败）';
      else if(st) fin+='（语音 '+sd+'s / '+st+'s）';
      setTranscribeProgress(done, total, fin);
      break;                                   // 完成即停
    }
    const sd=Math.floor(Number(r.seg_done)||0), st=Math.floor(Number(r.seg_total)||0);
    const isTr=!!r.stage && r.stage.indexOf('转写')>=0;
    const seg=(isTr&&st)?('（语音 '+sd+'s / '+st+'s）'):'';
    let msg;
    if(r.stage && r.stage.indexOf('下载模型')>=0 && (Number(r.dl_done)||Number(r.dl_total))){
      msg='正在'+r.stage.replace(/^下载模型\s*/,'下载模型 ')+dlProgressText(r)+'，首次使用会自动下载，请稍候…';
    }else if(r.stage){
      msg=r.stage+seg;
    }else{
      msg=r.current?('当前: '+r.current):'正在准备模型…（首次使用会自动下载，请稍候）';
    }
    if(total && done>=total) msg='文本已写入索引，正在收尾…';
    setTranscribeProgress(done, total, msg);
    await new Promise(res=>setTimeout(res, OCR_POLL_MS));
  }
  if(token!==_trPollToken) return;
  const errs=(r&&r.errors)||[];
  const failedNow=!!r && (r.status==='failed' || r.interrupted);
  _trBusy=false;
  if(failedNow || errs.length){
    // 失败/部分失败：面板保留，直接显示原因与建议，用户点“关闭”再收起
    showTranscribeResult(r);
  }else{
    resetTranscribePanel();
    document.getElementById('transcribeOverlay').classList.remove('show');
  }
  if(r.interrupted){ toast('转写任务已中断（服务重启），可重新发起'); }
  else if(r.status==='failed' || r.error){
    toast('转写失败: '+(r.error||('全部 '+errs.length+' 个文件未能转写')));
  }else if(r.status==='partial' || errs.length){
    toast('转写完成，但有 '+errs.length+' 个失败（原内容已保留未覆盖）');
  }else if(r.pending){
    toast('转写完成：'+r.pending+' 个文件已有旧内容，新结果已放入暂存等你确认');
  }else{
    toast('转写完成，共处理 '+r.total+' 个文件');
  }
  if(state.currentId) await selectFile(state.currentId);   // 完成后立刻回显文字
  await loadStats(); await loadFiles();
}

// ---------- 逐次提示词（批量入口：一次输入、应用于本批） ----------
let _promptIds=[];
let _promptMeta=null;          // {global_prompt,recent,max_len,budget_tokens}

function promptCount(){
  const inp=document.getElementById('promptInput');
  const el=document.getElementById('promptCount');
  if(!inp || !el) return;
  const n=inp.value.trim().length;
  el.textContent='已填 '+n+' / '+((_promptMeta&&_promptMeta.max_len)||500)+' 字'+(n?'':'（留空=各自默认）');
}

function promptTip(){
  const tip=document.getElementById('promptTip');
  if(!tip) return;
  const g=(_promptMeta&&_promptMeta.global_prompt)||'（全局默认已关闭）';
  tip.innerHTML='留空 = 各文件沿用各自上次提示，再回落到全局默认 <b>'+esc(g)+'</b>；'
    +'填 <b>off</b> 可本次完全关闭提示（中文可能变繁体）。'
    +'专有名词/术语多时值得填；提示里的词可能被模型“顺”进结果，别塞无关内容。';
}

async function openTranscribePromptModal(ids){
  _promptIds=ids.slice();
  document.getElementById('promptScope').textContent=
    '将应用于本批 '+ids.length+' 个文件。'+(ids.length===1?'':'一次输入即可整批生效，不必逐条填。');
  const inp=document.getElementById('promptInput');
  inp.value='';
  const sel=document.getElementById('promptRecent');
  sel.innerHTML='<option value="">最近使用…</option>';
  document.getElementById('promptModal').classList.add('show');
  promptCount(); promptTip();
  try{
    _promptMeta=await apiGet('/api/transcribe/prompts');
    if(_promptMeta.recent && _promptMeta.recent.length){
      sel.innerHTML='<option value="">最近使用…</option>'+
        _promptMeta.recent.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join('');
    }
  }catch(e){ /* 最近列表只是便利功能，失败不阻塞提交 */ }
  promptTip(); promptCount();
  inp.focus();
}

function closePromptModal(){
  document.getElementById('promptModal').classList.remove('show');
  _promptIds=[];
}

async function submitPromptTranscribe(){
  const v=document.getElementById('promptInput').value.trim();
  const ids=_promptIds.slice();
  closePromptModal();
  if(!ids.length) return;
  // 空 = 不传参数，逐文件走“上次 > 全局”；有值 = 本次显式指定，整批同用
  await startTranscribe(ids, v?{prompt:v}:{});
}

document.getElementById('cancelPrompt').onclick=closePromptModal;
document.getElementById('startPromptTranscribe').onclick=submitPromptTranscribe;
document.getElementById('promptDefault').onclick=()=>{
  document.getElementById('promptInput').value='';
  promptCount();
  document.getElementById('promptInput').focus();
};
document.getElementById('promptInput').addEventListener('input',promptCount);
document.getElementById('promptRecent').onchange=e=>{
  const v=e.target.value;
  if(!v) return;
  document.getElementById('promptInput').value=v;
  promptCount();
};
document.getElementById('promptModal').onclick=e=>{
  if(e.target===document.getElementById('promptModal')) closePromptModal();
};

// ---------- OCR（图片/扫描件PDF -> 文字；引擎优先级：Umi-OCR > tesseract > 浏览器内置WASM） ----------
// 同一批识别任务未结束前不再提交新的（连点只创建一个任务）
let _ocrBusy=false;
let _ocrBtnLabel='';
function setOcrSubmitBusy(on){
  _ocrBusy=!!on;
  const b=document.getElementById('ocrSelBtn');
  if(!b) return;
  if(on){
    _ocrBtnLabel=b.textContent;          // 记下原按钮文案，恢复时用
    b.disabled=true;
    b.textContent='🖼 提交中…';
  }else{
    b.disabled=false;
    b.textContent=_ocrBtnLabel||'🖼 识别选中图片';
  }
}

async function startOcr(ids){
  if(_ocrBusy) return;
  // 1) 后端引擎可用：走原生批量任务（最快）
  if(state.ocr && state.ocr.available){
    let jid, r=null;
    // 创建任务可能要拉起外部引擎，先给即时反馈，再等接口返回
    setOcrSubmitBusy(true);
    document.getElementById('ocrNum').textContent='';
    document.getElementById('ocrCur').textContent='正在提交识别任务…';
    document.getElementById('ocrOverlay').classList.add('show');
    toast('已提交识别任务，正在启动引擎…');
    try{ r=await apiPost('/api/ocr',{ids}); jid=r&&r.job_id; }
    catch(e){
      setOcrSubmitBusy(false);
      document.getElementById('ocrOverlay').classList.remove('show');
      toast('启动识别失败: '+e.message);
      return;
    }
    if(!jid){
      // 被拦截（已有文字层）：不创建任务，不进入 loading，直接给出原因并恢复按钮
      setOcrSubmitBusy(false);
      document.getElementById('ocrOverlay').classList.remove('show');
      const blocked=(r&&r.skipped)||[];
      if(blocked.length){
        const names=blocked.slice(0,2).map(s=>s.name).join('、')+(blocked.length>2?' 等':'');
        toast((r.message||'该 PDF 已有文字层，可直接搜索，无需识别')+(names?('：'+names):''));
        if(state.currentId) await selectFile(state.currentId);   // 文字层文本本来就在，刷新详情即可看到
        await loadFiles(); refreshOcrBadge();
      }else{
        toast('启动识别失败：未返回任务号');
      }
      return;
    }
    if(r.skipped && r.skipped.length){
      toast(r.message||('已跳过 '+r.skipped.length+' 个已有文字层的 PDF'));
    }
    pollOcr(jid);
    return;
  }
  // 2) 无后端引擎但浏览器内置 OCR 就绪：PDF 走浏览器识别（零安装兜底）
  if(WasmOcr.available()){
    let pdfIds, r=null;
    try{
      r=await apiPost('/api/ocr/pdf_ids',{ids});
      pdfIds=r.ids||[];
      if(r.blocked && r.blocked.length){
        const names=r.blocked.slice(0,2).map(s=>s.name).join('、')+(r.blocked.length>2?' 等':'');
        toast(r.blocked[0].message+(names?('：'+names):''));
      }
      if(r.skipped) toast(r.skipped+' 个非 PDF 文件已跳过（图片识别需安装 Umi-OCR / tesseract）');
    }catch(e){ toast('筛选失败: '+e.message); return; }
    if(!pdfIds.length){
      if(!(r && r.blocked && r.blocked.length)) toast('所选文件中没有 PDF');
      return;
    }
    runBrowserBacklog(pdfIds);
    return;
  }
  // 3) 都没有：给出跨平台安装引导
  const g=(state.ocr && state.ocr.install_guide) || '请先安装 Tesseract OCR 或 Umi-OCR（开启 HTTP 接口）';
  toast('当前环境未启用 OCR 引擎，无法识别');
  showOcrGuide(g);
}

// 浏览器内置 OCR：批量补识别（无后端引擎时的零安装路径）
async function runBrowserBacklog(ids){
  if(!ids || !ids.length) return;
  document.getElementById('ocrOverlay').classList.add('show');
  document.getElementById('ocrNum').textContent='0 / '+ids.length;
  let done=0;
  for(let k=0;k<ids.length;k++){
    const nm=(state.ocrPending&&state.ocrPending.names)||[];
    document.getElementById('ocrCur').textContent='当前: '+(nm[k]||('#'+ids[k]));
    try{ await WasmOcr.ocrPdf(ids[k]); }
    catch(e){ /* 单个失败不中断整批，稍后仍会留在待识别池 */ }
    done++;
    document.getElementById('ocrNum').textContent=done+' / '+ids.length;
  }
  document.getElementById('ocrOverlay').classList.remove('show');
  toast('浏览器内置 OCR 完成，共处理 '+ids.length+' 个 PDF');
  if(state.currentId) selectFile(state.currentId);
  await loadStats(); await loadFiles();
  refreshOcrBadge();
}

// 顶栏常驻 OCR 状态徽标：可用性（原生引擎/浏览器内置）+ 扫描件待识别数（一键补识别入口）
let _ocrPendingToasted=false;
async function refreshOcrBadge(){
  let r;
  try{ r=await apiGet('/api/ocr/check'); }catch(e){ r={available:false}; }
  state.ocr=r;
  updateOcrBadge(r);
  if((r && r.available) || WasmOcr.available()){
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
  }else if(WasmOcr.available()){
    b.textContent='OCR ✓ 浏览器内置';
    b.className='ocr-badge ok';
    b.title='使用随程序内置的浏览器 OCR，可直接识别扫描件 PDF（无需安装）。\n安装 Umi-OCR 可获得更高识别精度与速度。';
    b.onclick=()=>showOcrGuide('当前使用浏览器内置 OCR，扫描件 PDF 已可直接识别。\n\n如需更高精度/更快速度（尤其图片 OCR），可另装：\n'
                               + ((r&&r.install_guide)||'Umi-OCR / Tesseract'));
  }else{
    b.textContent='OCR ✕ 未启用';
    b.className='ocr-badge warn';
    b.title='点击查看如何启用 OCR（识别图片 / 扫描件 PDF）';
    b.onclick=()=>showOcrGuide((r&&r.install_guide)||'请安装 Tesseract OCR 或 Umi-OCR（开启 HTTP 接口）');
  }
}
function applyOcrPending(p){
  const b=document.getElementById('ocrBadge');
  if(!b) return;
  const native=(state.ocr&&state.ocr.available);
  if(!native && !WasmOcr.available()) return;
  state.ocrPending=p||{count:0,ids:[],names:[]};
  const n=state.ocrPending.count||0;
  if(n>0){
    b.textContent='OCR ✓ · '+n+' 待识别';
    b.title='检测到 '+n+' 个扫描件 PDF 尚未识别，点击开始批量 OCR'
            +(native?'':'（使用浏览器内置 OCR，逐页识别较慢）');
    b.onclick=()=>{
      const ids=(state.ocrPending&&state.ocrPending.ids)||[];
      if(!ids.length) return;
      if(confirm('对 '+ids.length+' 个扫描件 PDF 批量 OCR 识别？\n（逐个执行，进度实时显示，可转入后台）'))
        startOcr(ids);
    };
    if(!_ocrPendingToasted){
      _ocrPendingToasted=true;
      toast('检测到 '+n+' 个扫描件 PDF 未识别，点击顶栏「OCR 徽标」可批量补识别');
    }
  }else{
    b.textContent=native?'OCR ✓ 已启用':'OCR ✓ 浏览器内置';
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
// 进度轮询：只在数值变化时原地改文本（不复建节点），进度单调不减，完成或连续失败即停
const OCR_POLL_MS=900;
let _ocrPollToken=0;
let _ocrLastDone=-1;
let _ocrTotal=0;

function setOcrProgress(done, total, cur){
  const num=document.getElementById('ocrNum');
  const ce=document.getElementById('ocrCur');
  if(num){
    const text=total>0 ? (done+' / '+total) : '';
    if(num.textContent!==text) num.textContent=text;
  }
  if(ce && ce.textContent!==cur) ce.textContent=cur;
}

async function pollOcr(jid){
  const token=++_ocrPollToken;        // 新任务开始轮询时自增，旧循环据此退出
  _ocrLastDone=-1; _ocrTotal=0;
  let fails=0;
  let r=null;                          // 收尾时要用最后一次响应，必须在循环外声明
  while(token===_ocrPollToken){
    try{ r=await apiGet('/api/ocr/'+jid); fails=0; }
    catch(e){
      fails++;
      if(fails>=3){                     // 连续失败：明确报错而不是卡在进度条上
        document.getElementById('ocrOverlay').classList.remove('show');
        setOcrSubmitBusy(false);
        toast('识别进度读取失败: '+e.message);
        return;
      }
      setOcrProgress(_ocrLastDone, _ocrTotal, '读取进度失败，重试中（'+fails+'/3）…');
      await new Promise(res=>setTimeout(res, OCR_POLL_MS));
      continue;
    }
    const total=Math.max(_ocrTotal, Number(r.total)||0);
    let done=Number(r.done)||0;
    if(total) done=Math.min(done, total);
    if(done<_ocrLastDone) done=_ocrLastDone;      // 防止后端刷新时进度回跳
    _ocrTotal=total; _ocrLastDone=done;
    if(r.finished){
      const pagesDone=Number(r.pages_done)||0, pagesTotal=Number(r.pages_total)||0;
      setOcrProgress(done, total, pagesTotal?('处理完成（'+pagesDone+' / '+pagesTotal+' 页）'):'处理完成');
      break;                                       // 完成即停轮询
    }
    // 进度文案：优先展示后端阶段（抽取文字层 / 逐页 OCR 第 x/N 页 / 写入索引），
    // 长任务期间不再只显示“0 / 1”定格不动。
    const pagesTotal=Number(r.pages_total)||0;
    const pages=pagesTotal?('（第 '+(Number(r.pages_done)||0)+' / '+pagesTotal+' 页）'):'';
    let msg=r.stage?(r.stage+pages):(r.current?('当前: '+r.current):'正在识别…');
    if(total && done>=total) msg='文本已写入索引，正在收尾…';
    setOcrProgress(done, total, msg);
    await new Promise(res=>setTimeout(res, OCR_POLL_MS));
  }
  if(token!==_ocrPollToken) return;                // 已被新轮询接管，不重复收尾
  document.getElementById('ocrOverlay').classList.remove('show');
  setOcrSubmitBusy(false);
  const errs=r.errors||[];
  if(r.interrupted){ toast('识别任务已中断（服务重启），可重新发起'); }
  else if(r.status==='failed' || r.error){
    toast('识别失败: '+(r.error||('全部 '+errs.length+' 个文件未能识别')));
  }else if(r.status==='partial' || errs.length){
    toast('识别完成，但有 '+errs.length+' 个失败（原内容已保留未覆盖）');
  }else if(r.pending){
    toast('识别完成：'+r.pending+' 个文件已有旧内容，新结果已放入暂存等你确认');
  }else{
    toast('识别完成，共处理 '+r.total+' 个文件');
  }
  if(state.currentId) await selectFile(state.currentId);   // PDF 文字已落库，刷新详情后再收尾
  await loadStats(); await loadFiles();
  refreshOcrBadge();                                  // 待识别数已变化，刷新徽标
}
