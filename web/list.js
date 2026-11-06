// 中间文件清单：列表渲染与勾选
// ---------- 文件清单 ----------
async function loadFiles(){
  saveState();                       // 任何触发清单刷新的状态变化都落一次现场
  // 筛选/搜索/目录/项目变化后，选中集合必须清空：否则批量操作会作用到当前看不见的记录。
  // 翻页不算条件变化（允许跨页勾选）；页面刷新不恢复选中集合（见 core.js restoreState）。
  const ctx=JSON.stringify([state.search,state.status,state.ext,state.type,state.folder,
                            state.tag,state.project,state.showMissing]);
  if(state._selCtx!==undefined && state._selCtx!==ctx){ state.selection.clear(); selAnchorId=null; }
  state._selCtx=ctx;
  // 未选项目 = “全部项目”：默认空视图（标注数据躺库，建项目后自动召回）；
  // “查看全部扫描记录”是显式的彩蛋入口，开启后才真正查全表
  // if(state.project===0 && !state.showAll){ renderNoProject(); return; }
  const p=new URLSearchParams({ search:state.search, status:state.status, ext:state.ext, ftype:state.type,
    folder:state.folder, tag:state.tag, page:state.page, page_size:state.pageSize, sort:state.sort,
    project:state.project, show_missing:state.showMissing?1:0 });
  const r=await apiGet('/api/files?'+p.toString());
  renderList(r);
  pruneSelection();                  // 记录被删除/重扫消失后，把选中集合里已不存在的 id 剔除
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
                     : (d.can_ocr ? '（未抽取到文字：可能是扫描件，或该记录尚未抽取文字层；可点下方按钮识别）'
                                  : '（未抽取到文字：可能是扫描件，或该记录尚未抽取文字层）');
    const textLayer = len && d.content_source==='text_layer';
    const guide = (!len && !d.can_ocr && state.ocr && state.ocr.install_guide)
      ? `<div class="ocr-guide">⚠ 当前环境未启用 OCR 引擎，无法识别扫描件 PDF。<br>${esc(state.ocr.install_guide)}</div>` : '';
    return `<div class="transcript-block">
      <div class="meta-row"><b>PDF 文字</b><span>${hint}</span></div>
      <textarea id="transcriptView" placeholder="PDF 抽取的文字内容，可直接修改校对（存入索引，可被搜索）">${esc(d.pdf_text||'')}</textarea>
      <div class="detail-actions">
        ${d.can_ocr && !textLayer?`<button class="btn-primary btn-sm" id="ocrBtn">📄 ${len?"重新识别":"识别 PDF 文字（OCR）"}</button>`:''}
        ${textLayer?'<span style="font-size:12px;color:var(--muted)">✓ 该 PDF 有文字层，可直接搜索，无需 OCR</span>':''}
        ${d.content_source==='ocr'?'<button class="btn-ghost btn-sm" id="pdfProofBtn">🔍 对照原文校对</button>':''}
        ${len?'<button class="btn-ghost btn-sm" id="copyTranscript">复制文字</button>':''}
      </div>
      ${guide}
    </div>`;
  }
  if(d.category==='doc'){
    const len=(d.doc_text||'').length;
    const srcMap={'docx':'Word 文档提取','xlsx':'Excel 工作簿提取','pptx':'PowerPoint 提取','manual':'人工校对'};
    const srcInfo = (len && srcMap[d.content_source]) ? (' · '+srcMap[d.content_source]) : '';
    const trunc = d.content_truncated ? ' · 内容超限已截断（仅索引前 20 万字符/单表前 5000 行）' : '';
    const hint = len ? ('已抽取 '+len+' 字，可被搜索'+srcInfo+trunc)
                     : '未抽取到文字（文件可能损坏、加密或本身无文本内容）';
    return `<div class="transcript-block">
      <div class="meta-row"><b>文档正文</b><span>${hint}</span></div>
      <textarea id="transcriptView" placeholder="抽取的文档正文，可直接修改校对（存入索引，可被搜索）">${esc(d.doc_text||'')}</textarea>
      <div class="detail-actions">
        ${d.content_truncated?'<span style="font-size:12px;color:var(--muted)">⚠ 已按上限截断</span>':''}
        ${len?'<button class="btn-ghost btn-sm" id="copyTranscript">复制文字</button>':''}
      </div>
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
    const pv=d.prompt||'';
    const gdef=(state.whisper&&state.whisper.prompt_text)||'以下是普通话的句子。';
    const pstate = d.prompt==='off' ? '已保存：关闭提示（中文可能变繁体）'
      : (d.prompt ? '已保存本文件提示词，复转自动带出' : '未单独保存；留空=全局默认');
    return `<div class="transcript-block">
      <div class="meta-row"><b>转写</b><span>${d.transcript?('已生成文字稿'+(d.transcribed_at?' · '+fmtDate(d.transcribed_at):'')):'尚未转写'}</span></div>
      <textarea id="transcriptView" placeholder="转写结果，可直接修改校对；也可点击下方按钮重新转写">${esc(d.transcript||'')}</textarea>
      <div class="prompt-editor">
        <div class="pe-head"><b>🎯 本次提示词</b><span id="promptState">${esc(pstate)}</span></div>
        <textarea id="detailPrompt" maxlength="500" spellcheck="false"
          placeholder="留空=全局默认（${esc(gdef)}）；填 off 可完全关闭">${esc(pv)}</textarea>
        <div class="pe-meta"><span id="detailPromptCount"></span><span>最多 500 字 · Whisper 预算约 224 token，超出从开头截断</span></div>
      </div>
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
// 结果行的“命中片段”行：显示命中次数 + 前 N 个不同位置的上下文片段。
// 片段 HTML 由服务端生成（带 \u0001/\u0002 定界符），这里只做转义 + 替换 <mark>，不把正文整段拉到前端。
function hitSnippetsRow(it, showN){
  const h=it.hits;
  if(!h || !h.snippets || !h.snippets.length) return '';
  const head=(h.snippets.slice(0,showN));
  const rest=(h.snippets.slice(showN));
  const mk=(s,seq)=>`<div class="snip" data-field="${esc(s.field)}" data-occ="${s.occ}"
      title="点击定位：${esc(s.label)} 第 ${s.occ+1} 处"><span class="snip-no">${seq}</span><span class="snip-label">${esc(s.label)}</span><span class="snip-text">${snippetHtml(s.text)}</span></div>`;
  const count = (h.capped ? '共 '+h.count+'+ 处' : '共 '+h.count+' 处');
  return `<tr class="snip-row" data-id="${it.id}"><td colspan="7"><div class="snip-box">
    <div class="snip-head"><span class="hit-count">${count}</span><span class="hit-hint">${h.snippets.length<h.count?'已展示 '+h.snippets.length+' 处不同位置，点击可直接定位':'点击片段可直接定位到该处'}</span></div>
    ${head.map((s,i)=>mk(s,i+1)).join('')}
    ${rest.length?`<div class="snip-more" style="display:none">${rest.map((s,i)=>mk(s,showN+i+1)).join('')}</div>
      <button class="btn-ghost btn-sm snip-toggle">展开其余 ${rest.length} 处</button>`:''}
  </div></td></tr>`;
}

function officeMeta(it){
  if(!it.ext || ['.docx','.xlsx','.pptx'].indexOf(it.ext)<0) return '';
  const n=it.content_len||0;
  const info = n ? ('已抽取 '+n.toLocaleString()+' 字'+(it.content_truncated?' · 已截断':''))
                 : '未抽取文字';
  return `<div class="ext-meta" style="font-size:11px;color:var(--muted);margin-top:2px">${info}</div>`;
}
function renderList(r){
  const tb=document.getElementById('fileRows');
  // 高亮词以后端下发的 terms 为准（后端按 _build_where 的实际匹配语义解析，前端不自行分词）
  const terms = (r.terms && r.terms.length) ? r.terms : (state.search ? [state.search] : []);
  state.searchTerms = terms;
  const showN = r.snippet_show || 3;
  if(!r.items.length){ tb.innerHTML='<tr><td colspan="7"><div class="empty">没有匹配的文件</div></td></tr>'; }
  else{
    tb.innerHTML=r.items.map(it=>{
      const c=statusColor(it.status);
      const isDir=!!it.is_dir;
      const sel=state.selection.has(it.id)?'sel':'';
      const act=state.currentId===it.id?'active':'';
      const note=it.note?esc(it.note):'<span style="color:#cbd5e1">—</span>';
      const tags=tagChipsHtml(it.tags);
      const nameHtml = terms.length ? highlightHtml(it.name, terms, 50).html : esc(it.name);
      if(isDir){
        // 目录行：一等实体，点击钻取（而非预览）；无勾选框（不参与导出/OCR/转写）
        return `<tr class="row dir-row ${act}" data-id="${it.id}" data-path="${esc(it.path)}" data-rel="${esc(it.rel_path)}">
          <td></td>
          <td class="name-cell">📁 ${nameHtml}</td>
          <td class="type-ic">目录</td>
          <td>—</td>
          <td class="type-ic">${fmtDate(it.mtime)}</td>
          <td><span class="badge" style="background:${c}">${esc(it.status)}</span></td>
          <td class="note-snip">${note}${tags}</td>
        </tr>`;
      }
      return `<tr class="row ${sel} ${act}" data-id="${it.id}" data-path="${esc(it.path)}">
        <td><input type="checkbox" class="rowchk" ${state.selection.has(it.id)?'checked':''}></td>
        <td class="name-cell">${nameHtml}${it.pending_kind?'<span class="badge-pending">待确认</span>':''}</td>
        <td class="type-ic">${esc(it.ext||'')}${officeMeta(it)}</td>
        <td>${humanSize(it.size)}</td>
        <td class="type-ic">${fmtDate(it.mtime)}</td>
        <td><span class="badge" style="background:${c}">${esc(it.status)}</span></td>
        <td class="note-snip">${note}${tags}</td>
      </tr>` + hitSnippetsRow(it, showN);
    }).join('');
  }
  // 片段行：点具体片段 → 带着查询词与“第几处”进详情，直接定位到那一处
  tb.querySelectorAll('tr.snip-row').forEach(tr=>{
    const id=parseInt(tr.dataset.id);
    tr.querySelectorAll('.snip').forEach(el=>el.onclick=e=>{
      e.stopPropagation();
      selectFile(id, null, { q:state.search.trim(), field:el.dataset.field, occ:parseInt(el.dataset.occ,10)||0 });
    });
    const tg=tr.querySelector('.snip-toggle');
    const more=tr.querySelector('.snip-more');
    if(tg&&more) tg.onclick=e=>{
      e.stopPropagation();
      const open = more.style.display!=='none';
      more.style.display = open?'none':'';
      tg.textContent = open ? ('展开其余 '+more.children.length+' 处') : '收起';
    };
  });
  tb.querySelectorAll('tr.row').forEach(tr=>{
    if(tr.classList.contains('dir-row')){
      tr.onclick=()=>{
        state.folder=tr.dataset.rel; state.folderReal=tr.dataset.path; state.page=1;
        ancestorsOf(state.folder).forEach(x=>treeExpanded.add(x));   // 同步左侧树展开到当前位置
        renderTree(); loadFiles();
      };
    }else{
      const chk=tr.querySelector('.rowchk');
      if(chk) chk.onclick=e=>{ e.stopPropagation(); handleChkClick(parseInt(tr.dataset.id), e); };
      // 行点击默认打开详情；Shift = 连选，Ctrl/Cmd = 叠加/取消单条（不打开详情）
      tr.onclick=e=>{
        const id=parseInt(tr.dataset.id);
        if(e.shiftKey||e.ctrlKey||e.metaKey){
          handleChkClick(id, {shiftKey:e.shiftKey});
          return;
        }
        selectFile(id, tr.dataset.path);
      };
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
  applySelectionToRow(id, !state.selection.has(id));
  selAnchorId=id;                   // Shift 连选的锚点
  saveState();
  updateSelBar();
}
// ---------- 勾选集合与表头/批量栏联动 ----------
let selAnchorId=null;               // 最近一次点击的行（Shift 连选的起点，仅内存态）

function pageFileRows(){            // 只取“文件清单”里的文件行，目录行与任务面板不算
  return Array.from(document.querySelectorAll('#fileRows tr.row:not(.dir-row)'));
}
function applySelectionToRow(id, on){
  if(on) state.selection.add(id); else state.selection.delete(id);
  const tr=document.querySelector(`#fileRows tr.row[data-id="${id}"]`);
  if(tr){
    tr.classList.toggle('sel',on);
    const c=tr.querySelector('.rowchk'); if(c) c.checked=on;
  }
}
// Shift 连选：从锚点到最后点击行整段设为同一状态（只作用于当前页可见行）
function selectRange(fromId, toId, on){
  const rows=pageFileRows();
  const a=rows.findIndex(t=>parseInt(t.dataset.id)===fromId);
  const b=rows.findIndex(t=>parseInt(t.dataset.id)===toId);
  if(a<0||b<0){ applySelectionToRow(toId,on); return; }
  const [s,e]=a<b?[a,b]:[b,a];
  rows.slice(s,e+1).forEach(t=>applySelectionToRow(parseInt(t.dataset.id),on));
}
function handleChkClick(id, e){
  const on=!state.selection.has(id);
  if(e.shiftKey && selAnchorId!==null) selectRange(selAnchorId,id,on);
  else applySelectionToRow(id,on);
  selAnchorId=id;
  saveState();
  updateSelBar();
}
// 表头全选：作用于当前页全部文件行（保持既有“全选本页”语义）
function selectAllPage(on){
  pageFileRows().forEach(t=>applySelectionToRow(parseInt(t.dataset.id),on));
  selAnchorId=null;
  saveState();
  updateSelBar();
}
// 表头复选框状态：全选 / 半选 / 未选，与行勾选实时同步（含计数）
function syncSelectAllState(){
  const sa=document.getElementById('selectAll');
  if(!sa) return;
  const rows=pageFileRows().filter(t=>t.querySelector('.rowchk'));
  const checked=rows.filter(t=>t.querySelector('.rowchk').checked).length;
  sa.disabled=rows.length===0;
  sa.checked=rows.length>0 && checked===rows.length;
  sa.indeterminate=checked>0 && checked<rows.length;
  const lb=document.getElementById('selectAllLabel');
  if(lb) lb.textContent='全选本页'+(rows.length?('（'+rows.length+'）'):'');
}
// 删除/重扫后记录可能已不存在：用一次性轻量校验清理失效 id，避免“已选 N”里混入幽灵记录
async function pruneSelection(){
  if(!state.selection.size) return;
  const ids=Array.from(state.selection).slice(0,5000);
  try{
    const r=await apiGet('/api/files/exist?ids='+ids.join(','));
    const live=new Set((r.ids||[]).map(Number));
    let changed=false;
    ids.forEach(id=>{ if(!live.has(id)){ state.selection.delete(id); changed=true; } });
    if(changed){ saveState(); updateSelBar(); }
  }catch(e){}
}
function updateSelBar(){
  const bar=document.getElementById('selBar');
  const cnt=document.getElementById('selCount');
  if(state.selection.size>0){ bar.style.display='flex'; cnt.textContent='已选 '+state.selection.size; }
  else{ bar.style.display='none'; cnt.textContent='已选 0'; }
  syncSelectAllState();
}
