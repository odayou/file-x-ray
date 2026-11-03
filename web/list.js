// 中间文件清单：列表渲染与勾选
// ---------- 文件清单 ----------
async function loadFiles(){
  saveState();                       // 任何触发清单刷新的状态变化都落一次现场
  // 未选项目 = “全部项目”：默认空视图（标注数据躺库，建项目后自动召回）；
  // “查看全部扫描记录”是显式的彩蛋入口，开启后才真正查全表
  // if(state.project===0 && !state.showAll){ renderNoProject(); return; }
  const p=new URLSearchParams({ search:state.search, status:state.status, ext:state.ext, ftype:state.type,
    folder:state.folder, tag:state.tag, page:state.page, page_size:state.pageSize, sort:state.sort,
    project:state.project, show_missing:state.showMissing?1:0 });
  const r=await apiGet('/api/files?'+p.toString());
  renderList(r);
}
function renderNoProject(){
  const tb=document.getElementById('fileRows');
  tb.innerHTML=`<tr><td colspan="7"><div class="empty">
    尚未选择项目。<br>文件记录与标注安全保留，选择或新建项目后会自动召回。<br><br>
    <a href="javascript:void(0)" id="showAllRecords" style="color:var(--primary)">（想直接翻全部扫描记录？点这里）</a>
  </div></td></tr>`;
  document.getElementById('pageInfo').textContent='';
  const lt=document.getElementById('listTitle');
  lt.textContent='🗂 全部项目';
  lt.classList.remove('clickable'); lt.title=''; lt.onclick=null;
  document.getElementById('detailActionBar').innerHTML='';
  updateSelBar();
  const link=document.getElementById('showAllRecords');
  if(link) link.onclick=()=>{ state.showAll=true; loadFiles(); };
}
function tagChipsHtml(tags){
  if(!tags||!tags.length) return '';
  return `<div class="tagchips">`+tags.map(t=>`<span class="tg">#${esc(t)}</span>`).join('')+`</div>`;
}
function mediaBlock(d){
  if(d.category==='pdf'){
    const len=(d.pdf_text||'').length;
    const srcMap={'text_layer':'文字层精确抽取','ocr':'OCR 识别（建议校对）','manual':'人工校对'};
    const srcInfo = (len && srcMap[d.content_source]) ? (' · '+srcMap[d.content_source]) : '';
    const hint = len ? ('已抽取 '+len+' 字，可被搜索'+srcInfo)
                     : (d.can_ocr ? '（扫描件/无文字层，可点击下方按钮 OCR 识别）'
                                  : '（扫描件/无文字层，暂无文字）');
    const guide = (!len && !d.can_ocr && state.ocr && state.ocr.install_guide)
      ? `<div class="ocr-guide">⚠ 当前环境未启用 OCR 引擎，无法识别扫描件 PDF。<br>${esc(state.ocr.install_guide)}</div>` : '';
    return `<div class="transcript-block">
      <div class="meta-row"><b>PDF 文字</b><span>${hint}</span></div>
      <textarea id="transcriptView" placeholder="PDF 抽取的文字内容，可直接修改校对（存入索引，可被搜索）">${esc(d.pdf_text||'')}</textarea>
      <div class="detail-actions">
        ${d.can_ocr?`<button class="btn-primary btn-sm" id="ocrBtn">📄 ${len?"重新识别":"识别 PDF 文字（OCR）"}</button>`:''}
        ${d.content_source==='ocr'?'<button class="btn-ghost btn-sm" id="pdfProofBtn">🔍 对照原文校对</button>':''}
        ${len?'<button class="btn-ghost btn-sm" id="copyTranscript">复制文字</button>':''}
      </div>
      ${guide}
    </div>`;
  }
  if(d.category==='image'){
    const guide = (!d.transcript && !d.can_ocr && state.ocr && state.ocr.install_guide)
      ? `<div class="ocr-guide">⚠ 当前环境未启用 OCR 引擎，无法识别图片文字。<br>${esc(state.ocr.install_guide)}</div>` : '';
    return `<div class="transcript-block">
      <div class="meta-row"><b>图中文字</b><span>${d.transcript?('已识别文字稿'+(d.transcribed_at?' · '+fmtDate(d.transcribed_at):'')):'尚未识别'+(d.can_ocr?'（可点击下方按钮）':'（当前环境未启用 OCR）')}</span></div>
      <textarea id="transcriptView" placeholder="识别结果，可直接修改校对；也可点击下方按钮重新识别">${esc(d.transcript||'')}</textarea>
      <div class="detail-actions">
        ${d.can_ocr?`<button class="btn-primary btn-sm" id="ocrBtn">🖼 ${d.transcript?"重新识别":"识别图中文字"}</button>`:''}
        ${d.transcript?'<button class="btn-ghost btn-sm" id="copyTranscript">复制文字稿</button>':''}
      </div>
      ${guide}
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
      const isDir=!!it.is_dir;
      const sel=state.selection.has(it.id)?'sel':'';
      const act=state.currentId===it.id?'active':'';
      const note=it.note?esc(it.note):'<span style="color:#cbd5e1">—</span>';
      const tags=tagChipsHtml(it.tags);
      if(isDir){
        // 目录行：一等实体，点击钻取（而非预览）；无勾选框（不参与导出/OCR/转写）
        return `<tr class="row dir-row ${act}" data-id="${it.id}" data-path="${esc(it.path)}" data-rel="${esc(it.rel_path)}">
          <td></td>
          <td class="name-cell">📁 ${esc(it.name)}</td>
          <td class="type-ic">目录</td>
          <td>—</td>
          <td class="type-ic">${fmtDate(it.mtime)}</td>
          <td><span class="badge" style="background:${c}">${esc(it.status)}</span></td>
          <td class="note-snip">${note}${tags}</td>
        </tr>`;
      }
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
    if(tr.classList.contains('dir-row')){
      tr.onclick=()=>{
        state.folder=tr.dataset.rel; state.folderReal=tr.dataset.path; state.page=1;
        ancestorsOf(state.folder).forEach(x=>treeExpanded.add(x));   // 同步左侧树展开到当前位置
        renderTree(); loadFiles();
      };
    }else{
      const chk=tr.querySelector('.rowchk');
      if(chk) chk.onclick=e=>{ e.stopPropagation(); toggleSelect(parseInt(tr.dataset.id)); };
      tr.onclick=()=>selectFile(parseInt(tr.dataset.id), tr.dataset.path);
    }
  });
  const total=r.total, ps=state.pageSize;
  state.pages=Math.max(1,Math.ceil(total/ps));
  if(state.page>state.pages){ state.page=state.pages; loadFiles(); return; }  // 过滤/删除后页码越界，自动回跳最后一页
  document.getElementById('pageInfo').textContent=`第 ${state.page}/${state.pages} 页 · 共 ${total.toLocaleString()} 条`;
  // 所在位置：展示真实完整路径（过长中间缩略，悬停看全貌），点击打开；
  // 未选项目时是“扫描过的所有文件”（索引全集，不对应任何目录，不可点）
  const lt=document.getElementById('listTitle');
  const proj=state.projects.find(x=>x.id===state.project);
  const openPath = state.folderReal || state.folder || ((proj&&proj.roots&&proj.roots.length)?proj.roots[0]:'');
  lt.textContent = state.folderReal ? ('📂 '+shortPath(state.folderReal, 60))
                 : state.folder ? ('📂 '+state.folder)
                 : state.project ? '📁 项目根目录'
                 : (state.showAll ? '🗂 扫描过的所有文件（彩蛋视图 · 切换项目即退出）' : '🗂 扫描过的所有文件');
  if(state.project && !state.folder && proj && (proj.roots||[]).length){
    lt.title = '点击打开：'+proj.roots.join('\n');
  }
  if(openPath){
    lt.classList.add('clickable');
    lt.title='点击打开：'+openPath;
    lt.onclick=async()=>{ try{ await apiPost('/api/open',{path:openPath}); toast('已打开：'+openPath); }catch(e){ toast('打开失败: '+e.message); } };
  }else{
    lt.classList.remove('clickable'); lt.title=''; lt.onclick=null;
  }
  updateSelBar();
}
function toggleSelect(id){
  if(state.selection.has(id)) state.selection.delete(id); else state.selection.add(id);
  saveState();
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
