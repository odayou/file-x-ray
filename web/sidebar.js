// 左侧栏：状态/扫描/统计/文件夹树/标签云
// ---------- 状态（自定义） ----------
async function loadStatuses(){
  const r=await apiGet('/api/statuses'); state.statuses=r.statuses;
  const sf=document.getElementById('statusFilter');
  sf.innerHTML='<option value="all">全部状态</option>'+state.statuses.map(s=>`<option value="${esc(s.label)}">${esc(s.label)}</option>`).join('');
}

// ---------- 扫描 ----------
async function doScan(){
  const root=document.getElementById('rootPath').value.trim();
  if(!root){ toast('请先填写根目录'); return; }
  const md=document.getElementById('maxDepth').value.trim();
  let sid, pid=null;
  // 未切换项目时，后端会按根目录自动复用或新建项目
  try{ const r=await apiPost('/api/scan',{root, max_depth: md===''?null:md, project_id: state.project}); sid=r.scan_id; pid=r.project_id; }
  catch(e){ toast('扫描启动失败: '+e.message); return; }
  document.getElementById('scanOverlay').classList.add('show');
  document.getElementById('scanRoot').textContent=root;
  await pollScan(sid);
  await loadProjects();
  if(pid && state.project!==pid){ state.project=pid; renderProjectSel(); }
}
async function pollScan(sid){
  // 轮询直到扫描结束（await 到底，便于调用方在完成后刷新项目列表）
  for(;;){
    let r;
    try{ r=await apiGet('/api/scan/'+sid); }
    catch(e){ document.getElementById('scanOverlay').classList.remove('show'); toast('扫描状态读取失败: '+e.message); return; }
    document.getElementById('scanNum').textContent=r.processed.toLocaleString();
    if(!r.done){ await new Promise(res=>setTimeout(res,500)); continue; }
    document.getElementById('scanOverlay').classList.remove('show');
    if(r.error){ toast('扫描出错: '+r.error); return; }
    toast('扫描完成，共 '+r.processed.toLocaleString()+' 个文件');
    // 扫描会自动创建/复用项目，这里一并刷新，新项目立刻出现在切换器里
    await loadProjects();
    await loadStats(); await loadTree(); await loadTags(); await loadFiles();
    return;
  }
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
    return `<div class="tree-node ${active}" style="padding-left:${pad}px" data-folder="${esc(f.path)}">📂 ${esc(f.name)}<span class="cnt">(${f.count})</span></div>`;
  }).join('');
  el.querySelectorAll('.tree-node').forEach(n=>n.onclick=()=>{
    state.folder=n.dataset.folder; state.page=1; renderTree(); updateCrumb(); loadFiles();
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
