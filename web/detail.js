// 右侧详情面板：预览/文字稿/保存/OCR转写按钮
// ---------- 详情 ----------
async function selectFile(id, path){
  state.currentId=id;
  document.querySelectorAll('tr.row').forEach(tr=>tr.classList.toggle('active', parseInt(tr.dataset.id)===id));
  let d;
  try{ d=await apiGet('/api/file/'+id); }catch(e){ toast('加载失败: '+e.message); return; }
  renderDetail(d);
}
// 媒体/<a> 标签无法携带 X-Fit-Token 头，需把令牌放进 query 参数，否则预览/下载请求会被 401 拦截
function rawWithToken(d){
  const u = (d && d.raw_url) || "";
  if(!u) return u;
  const t = window.__FIT_AUTH_TOKEN__ || "";
  if(!t) return u;
  return u + (u.indexOf("?") >= 0 ? "&" : "?") + "token=" + encodeURIComponent(t);
}

function renderDetail(d){
  let preview='';
  if(!d.exists){ preview='<div class="ph">⚠ 原文件不存在或已被移动/删除<br>'+esc(d.path)+'</div>'; }
  else if(d.category==='image'){ preview=`<img src="${rawWithToken(d)}" alt="preview">`; }
  else if(d.category==='video'){ preview=`<video controls src="${rawWithToken(d)}"></video>`; }
  else if(d.category==='audio'){ preview=`<audio controls src="${rawWithToken(d)}" style="width:100%;margin-top:80px"></audio>`; }
  else if(d.category==='pdf'){ preview=`<iframe src="${rawWithToken(d)}" style="width:100%;height:360px;border:0"></iframe>`; }
  else if(d.category==='text'){ preview=`<pre>${esc(d.preview_text)||'(空文件)'}</pre>`; }
  else{ preview='<div class="ph">该类型不支持内嵌预览<br><a class="dl" href="'+rawWithToken(d)+'" target="_blank">打开/下载原文件</a></div>'; }

  const statusOpts=state.statuses.map(s=>`<option ${s.label===d.status?'selected':''}>${esc(s.label)}</option>`).join('');
  const tagsHtml=(d.tags||[]).map(t=>`<span class="tg" data-tag="${esc(t)}">#${esc(t)}<span data-x="1">✕</span></span>`).join('');
  document.getElementById('detail').innerHTML=`
    <div class="preview">${preview}</div>
    <div class="meta-row"><b>名称</b><span>${esc(d.name)}</span></div>
    <div class="meta-row"><b>路径</b><span style="word-break:break-all">${esc(d.path)}</span></div>
    <div class="meta-row"><b>大小</b><span>${humanSize(d.size)}</span><b>类型</b><span>${esc(d.ext||'')}</span></div>
    <div class="meta-row"><b>修改</b><span>${fmtDate(d.mtime)}</span><b>层级</b><span>${d.depth}</span></div>
    ${mediaBlock(d)}
    <label class="fld">状态</label>
    <select class="status-sel" id="detailStatus">${statusOpts}</select>
    <label class="fld">主题标签（回车添加，可多个）</label>
    <div class="tagchips" id="detailTags" style="margin-bottom:4px">${tagsHtml||'<span style="color:#cbd5e1;font-size:12px">暂无标签</span>'}</div>
    <input id="addTagInput" placeholder="输入标签后回车，如 旅行 / 待翻译" style="width:100%;padding:6px;border:1px solid var(--border);border-radius:6px" />
    <label class="fld">备注</label>
    <textarea id="detailNote" placeholder="给这个文件写备注…">${esc(d.note)}</textarea>
    <div class="detail-actions">
      <button class="btn-primary btn-sm" id="saveDetail">保存备注/状态/标签</button>
      ${(d.category==='image'||d.category==='pdf')?'<button class="btn-ghost btn-sm" id="proofBtn">🔍 校对模式</button>':''}
      <button class="btn-ghost btn-sm" id="openFolder">打开所在文件夹</button>
      <button class="btn-ghost btn-sm" id="toggleSel">${state.selection.has(d.id)?'移出选择':'加入选择'}</button>
      <a class="btn-ghost btn-sm" style="text-decoration:none" href="${rawWithToken(d)}" target="_blank">新窗口打开</a>
    </div>`;
  const save=async()=>{
    const tags=Array.from(document.querySelectorAll('#detailTags .tg')).map(x=>x.dataset.tag);
    const payload={note:document.getElementById('detailNote').value,
                   status:document.getElementById('detailStatus').value, tags};
    // 文字稿可编辑后随详情一起保存（图片/音视频 -> transcript；PDF -> content）
    const tv=document.getElementById('transcriptView');
    if(tv){ if(d.category==='pdf') payload.pdf_text=tv.value; else payload.transcript=tv.value; }
    try{ await apiPut('/api/file/'+d.id, payload);
      toast('已保存'); loadStats(); loadTags(); loadFiles(); }catch(e){ toast('保存失败: '+e.message); }
  };
  document.getElementById('saveDetail').onclick=save;
  const proofBtn=document.getElementById('proofBtn');
  if(proofBtn) proofBtn.onclick=()=>openProofread(d);
  document.getElementById('openFolder').onclick=async()=>{ try{ await apiPost('/api/open',{path:d.path}); toast('已调用系统打开文件夹'); }catch(e){ toast('打开失败: '+e.message); } };
  document.getElementById('toggleSel').onclick=()=>{ toggleSelect(d.id); renderDetail(d); };
  const trBtn=document.getElementById('transcribeBtn');
  if(trBtn) trBtn.onclick=()=>startTranscribe([d.id]);
  const ocrBtn=document.getElementById('ocrBtn');
  if(ocrBtn) ocrBtn.onclick=()=>startOcr([d.id]);
  const cpBtn=document.getElementById('copyTranscript');
  if(cpBtn) cpBtn.onclick=()=>{ const t=document.getElementById('transcriptView').value; navigator.clipboard.writeText(t).then(()=>toast('已复制文字稿')).catch(()=>toast('复制失败，请手动选择')); };
  const accBtn=document.getElementById('acceptText');
  if(accBtn) accBtn.onclick=async()=>{
    try{ await apiPost('/api/file/'+d.id+'/text',{action:'accept',text:document.getElementById('pendingView').value});
      toast('已采用新版本'); await selectFile(d.id, d.path); loadFiles(); }
    catch(e){ toast('采用失败: '+e.message); }
  };
  const disBtn=document.getElementById('discardText');
  if(disBtn) disBtn.onclick=async()=>{
    try{ await apiPost('/api/file/'+d.id+'/text',{action:'discard'});
      toast('已丢弃暂存内容，正式内容未改动'); await selectFile(d.id, d.path); loadFiles(); }
    catch(e){ toast('丢弃失败: '+e.message); }
  };
  // 标签删除
  document.querySelectorAll('#detailTags .tg [data-x]').forEach(x=>x.onclick=()=>{ x.parentElement.remove(); });
  // 标签添加
  const ai=document.getElementById('addTagInput');
  ai.addEventListener('keydown',e=>{ if(e.key==='Enter'){ e.preventDefault();
    const v=ai.value.trim(); if(!v) return;
    const cur=Array.from(document.querySelectorAll('#detailTags .tg')).map(x=>x.dataset.tag);
    if(!cur.includes(v)){ const span=document.createElement('span'); span.className='tg'; span.dataset.tag=v;
      span.innerHTML=`#${esc(v)}<span data-x="1">✕</span>`; span.querySelector('[data-x]').onclick=()=>span.remove();
      document.getElementById('detailTags').appendChild(span); }
    ai.value=''; } });
}

// ---------- 校对模式：放大区域 = 大图（缩放/平移）+ 可编辑文字并排 ----------
function openProofread(d){
  const isPdf = d.category==='pdf';
  const textKey = isPdf ? 'pdf_text' : 'transcript';
  const overlay=document.createElement('div');
  overlay.id='proofModal';
  overlay.innerHTML=`
    <div class="proof-bar">
      <b>校对模式</b>
      <span class="proof-name">${esc(d.name)}</span>
      <span class="spacer"></span>
      ${isPdf?'':'<span class="proof-tip">滚轮缩放 · 按住拖动 · 双击复位</span>'}
      <button class="btn-primary btn-sm" id="proofSave">保存文字</button>
      <button class="btn-ghost btn-sm" id="proofClose">退出校对</button>
    </div>
    <div class="proof-main">
      <div class="proof-view" id="proofView">
        ${isPdf?`<iframe src="${rawWithToken(d)}" style="width:100%;height:100%;border:0"></iframe>`
               :`<img id="proofImg" src="${rawWithToken(d)}" alt="">`}
      </div>
      <textarea class="proof-text" id="proofText" spellcheck="false"
        placeholder="左侧对照原文，这里可直接修改，改完点“保存文字”">${esc(d[textKey]||'')}</textarea>
    </div>`;
  // 覆盖文件清单 + 详情区域（从布局区左上角开始）
  const lr=document.querySelector('.layout').getBoundingClientRect();
  overlay.style.left=lr.left+'px';
  overlay.style.top=lr.top+'px';
  document.body.appendChild(overlay);

  const onKey=e=>{ if(e.key==='Escape') overlay.remove(); };
  document.addEventListener('keydown', onKey);
  overlay.querySelector('#proofClose').onclick=async()=>{
    overlay.remove(); document.removeEventListener('keydown', onKey);
    await selectFile(d.id, d.path); loadFiles();
  };
  overlay.querySelector('#proofSave').onclick=async()=>{
    const val=overlay.querySelector('#proofText').value;
    try{ await apiPut('/api/file/'+d.id, isPdf?{pdf_text:val}:{transcript:val});
      toast('文字已保存，可被搜索'); }catch(e){ toast('保存失败: '+e.message); }
  };
  if(!isPdf){
    const view=overlay.querySelector('#proofView'), img=overlay.querySelector('#proofImg');
    let scale=1, tx=0, ty=0, drag=null;
    const apply=()=>{ img.style.transform='translate('+tx+'px,'+ty+'px) scale('+scale+')'; };
    // 滚轮缩放（无级），拖动平移，双击复位
    view.addEventListener('wheel',e=>{ e.preventDefault();
      scale*=(e.deltaY<0?1.2:1/1.2); if(scale<0.02) scale=0.02; apply(); },{passive:false});
    view.addEventListener('pointerdown',e=>{
      drag={x:e.clientX,y:e.clientY,tx,ty}; view.setPointerCapture(e.pointerId); });
    view.addEventListener('pointermove',e=>{
      if(!drag) return; tx=drag.tx+e.clientX-drag.x; ty=drag.ty+e.clientY-drag.y; apply(); });
    view.addEventListener('pointerup',()=>drag=null);
    view.addEventListener('dblclick',()=>{ scale=1; tx=0; ty=0; apply(); });
  }
}
