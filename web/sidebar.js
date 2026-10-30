// 左侧栏：状态/统计/文件夹树/标签云（扫描已统一走项目管理器，见 projects.js）
// ---------- 状态（自定义） ----------
async function loadStatuses(){
  const r=await apiGet('/api/statuses'); state.statuses=r.statuses;
  const sf=document.getElementById('statusFilter');
  sf.innerHTML='<option value="all">全部状态</option>'+state.statuses.map(s=>`<option value="${esc(s.label)}">${esc(s.label)}</option>`).join('');
}


// ---------- 统计 ----------
async function loadStats(){
  const r=await apiGet('/api/stats?project='+state.project);
  let s=''; for(const k in r.by_status){ s+=`<span style="color:${statusColor(k)}">●</span>${k}:<b>${r.by_status[k]}</b> `; }
  document.getElementById('stats').innerHTML=`总 <b>${r.total.toLocaleString()}</b> ｜ 已打标签 <b>${r.tagged}</b> ｜ 已转写 <b>${r.transcribed||0}</b> ｜ 已识别图片 <b>${r.ocr_count||0}</b> ${s}`;
}

// ---------- 文件夹树 ----------
async function loadTree(){
  const d=document.getElementById('treeDepth').value;
  const r=await apiGet('/api/tree?depth='+d+'&project='+state.project);
  treeData=r.folders||[]; renderTree();
}
function renderTree(){
  const el=document.getElementById('tree');
  if(!treeData.length){ el.innerHTML='<div class="empty">暂无数据，请先扫描</div>'; return; }
  const folders=treeData.slice().sort((a,b)=> a.depth-b.depth || a.path.localeCompare(b.path));
  el.innerHTML=folders.map(f=>{
    const pad=8+f.depth*14, active=state.folder===f.path?'active':'';
    // 树的根节点按上下文命名：选了项目是“项目根目录”，未选项目是“全部项目”，避免“根目录”歧义
    const name = f.path ? f.name : (state.project ? '（项目根目录）' : '（扫描过的所有文件）');
    return `<div class="tree-node ${active}" style="padding-left:${pad}px" data-folder="${esc(f.path)}" data-real="${esc(f.real||'')}">📂 ${esc(name)}<span class="cnt">(${f.count})</span></div>`;
  }).join('');
  el.querySelectorAll('.tree-node').forEach(n=>n.onclick=()=>{
    state.folder=n.dataset.folder;
    state.folderReal=n.dataset.real||'';          // 节点自带真实路径，打开时无需任何解析
    state.page=1; renderTree(); loadFiles();
  });
}

// ---------- 标签云 ----------
async function loadTags(){
  const r=await apiGet('/api/tags?project='+state.project);
  const el=document.getElementById('tagCloud');
  if(!r.tags.length){ el.innerHTML='<div class="empty" style="padding:10px">暂无标签</div>'; return; }
  el.innerHTML=r.tags.map(t=>{
    const active=state.tag===t.tag?'active':'';
    return `<div class="t ${active}" data-tag="${esc(t.tag)}">#${esc(t.tag)}<b>${t.count}</b></div>`;
  }).join('');
  el.querySelectorAll('.t').forEach(n=>n.onclick=()=>{
    state.tag = state.tag===n.dataset.tag ? '' : n.dataset.tag;
    state.page=1; loadTags(); renderTagFilter(); loadFiles();
  });
}
function renderTagFilter(){
  const el=document.getElementById('tagFilter');
  if(!state.tag){ el.innerHTML=''; return; }
  el.innerHTML=`<span class="chip">#${esc(state.tag)}<span data-clear="1">✕</span></span>`;
  el.querySelector('[data-clear]').onclick=()=>{ state.tag=''; loadTags(); renderTagFilter(); loadFiles(); };
}
