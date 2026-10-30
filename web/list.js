// 中间文件清单：列表渲染与勾选
// ---------- 文件清单 ----------
async function loadFiles(){
  const p=new URLSearchParams({ search:state.search, status:state.status, ext:state.ext, type:state.type,
    folder:state.folder, tag:state.tag, page:state.page, page_size:state.pageSize, sort:state.sort,
    project:state.project, show_missing:state.showMissing?1:0 });
  const r=await apiGet('/api/files?'+p.toString());
  renderList(r);
}
function tagChipsHtml(tags){
  if(!tags||!tags.length) return '';
  return `<div class="tagchips">`+tags.map(t=>`<span class="tg">#${esc(t)}</span>`).join('')+`</div>`;
}
function mediaBlock(d){
  if(d.category==='pdf'){
    const len=(d.pdf_text||'').length;
    return `<div class="transcript-block">
      <div class="meta-row"><b>PDF 文字</b><span>${len?('已抽取 '+len+' 字，可被搜索'):'（扫描件/无文字层，暂无文字）'}</span></div>
      <textarea id="transcriptView" placeholder="PDF 抽取的文字内容，可直接修改校对（存入索引，可被搜索）">${esc(d.pdf_text||'')}</textarea>
      <div class="detail-actions">
        ${len?'<button class="btn-ghost btn-sm" id="copyTranscript">复制文字</button>':''}
      </div>
    </div>`;
  }
  if(d.category==='image'){
    return `<div class="transcript-block">
      <div class="meta-row"><b>图中文字</b><span>${d.transcript?('已识别文字稿'+(d.transcribed_at?' · '+fmtDate(d.transcribed_at):'')):'尚未识别'+(d.can_ocr?'（可点击下方按钮）':'（当前环境未启用 OCR）')}</span></div>
      <textarea id="transcriptView" placeholder="识别结果，可直接修改校对；也可点击下方按钮重新识别">${esc(d.transcript||'')}</textarea>
      <div class="detail-actions">
        ${d.can_ocr?`<button class="btn-primary btn-sm" id="ocrBtn">🖼 ${d.transcript?"重新识别":"识别图中文字"}</button>`:''}
        ${d.transcript?'<button class="btn-ghost btn-sm" id="copyTranscript">复制文字稿</button>':''}
      </div>
    </div>`+pendingBlock(d);
  }
  if(d.category==='audio'||d.category==='video'){
    return `<div class="transcript-block">
      <div class="meta-row"><b>转写</b><span>${d.transcript?('已生成文字稿'+(d.transcribed_at?' · '+fmtDate(d.transcribed_at):'')):'尚未转写'}</span></div>
      <textarea id="transcriptView" placeholder="转写结果，可直接修改校对；也可点击下方按钮重新转写">${esc(d.transcript||'')}</textarea>
      <div class="detail-actions">
        ${d.can_transcribe?`<button class="btn-primary btn-sm" id="transcribeBtn">🎙 ${d.transcript?"重新转写":"转写此文件"}</button>`:''}
        ${d.transcript?'<button class="btn-ghost btn-sm" id="copyTranscript">复制文字稿</button>':''}
      </div>
    </div>`+pendingBlock(d);
  }
  return '';
}
function pendingBlock(d){
  if(!d.pending_kind) return '';
  const label = d.pending_kind==='ocr' ? '新识别的文字' : '新转写的文字';
  return `<div class="pending-block">
    <div class="meta-row"><b>⚠ ${label}（待确认）</b><span>采用后才会覆盖上方正式内容</span></div>
    <textarea id="pendingView" placeholder="可在此直接修改，再点采用">${esc(d.pending_text||'')}</textarea>
    <div class="detail-actions">
      <button class="btn-primary btn-sm" id="acceptText">✓ 采用此版本</button>
      <button class="btn-ghost btn-sm" id="discardText">✗ 丢弃</button>
    </div>
    <p class="pending-tip">上方是目前生效的正式内容。新结果先进这里等你确认，不采用就不会动正式内容。</p>
  </div>`;
}
function renderList(r){
  const tb=document.getElementById('fileRows');
  if(!r.items.length){ tb.innerHTML='<tr><td colspan="7"><div class="empty">没有匹配的文件</div></td></tr>'; }
  else{
    tb.innerHTML=r.items.map(it=>{
      const c=statusColor(it.status);
      const sel=state.selection.has(it.id)?'sel':'';
      const act=state.currentId===it.id?'active':'';
      const note=it.note?esc(it.note):'<span style="color:#cbd5e1">—</span>';
      const tags=tagChipsHtml(it.tags);
      return `<tr class="row ${sel} ${act}" data-id="${it.id}" data-path="${esc(it.path)}">
        <td><input type="checkbox" class="rowchk" ${state.selection.has(it.id)?'checked':''}></td>
        <td class="name-cell">${esc(it.name)}${it.pending_kind?'<span class="badge-pending">待确认</span>':''}</td>
        <td class="type-ic">${esc(it.ext||'')}</td>
        <td>${humanSize(it.size)}</td>
        <td class="type-ic">${fmtDate(it.mtime)}</td>
        <td><span class="badge" style="background:${c}">${esc(it.status)}</span></td>
        <td class="note-snip">${note}${tags}</td>
      </tr>`;
    }).join('');
  }
  tb.querySelectorAll('tr.row').forEach(tr=>{
    tr.querySelector('.rowchk').onclick=e=>{ e.stopPropagation(); toggleSelect(parseInt(tr.dataset.id)); };
    tr.onclick=()=>selectFile(parseInt(tr.dataset.id), tr.dataset.path);
  });
  const total=r.total, ps=state.pageSize, pages=Math.max(1,Math.ceil(total/ps));
  document.getElementById('pageInfo').textContent=`第 ${state.page}/${pages} 页 · 共 ${total.toLocaleString()} 条`;
  document.getElementById('listTitle').textContent= state.folder?('📂 '+state.folder):'全部文件';
  updateSelBar();
}
function toggleSelect(id){
  if(state.selection.has(id)) state.selection.delete(id); else state.selection.add(id);
  const tr=document.querySelector(`tr.row[data-id="${id}"]`);
  if(tr){ tr.classList.toggle('sel', state.selection.has(id)); const c=tr.querySelector('.rowchk'); if(c)c.checked=state.selection.has(id); }
  updateSelBar();
}
function updateSelBar(){
  const bar=document.getElementById('selBar');
  if(state.selection.size>0){ bar.style.display='flex'; document.getElementById('selCount').textContent='已选 '+state.selection.size; }
  else bar.style.display='none';
  const sa=document.getElementById('selectAll'); if(sa) sa.checked=false;
}
