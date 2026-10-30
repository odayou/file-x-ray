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
let treeExpanded = new Set();                   // 已展开的文件夹（下探式）
let treeNodeSet = new Set();                    // 真实存在的目录节点集合（用于可见性判定跳过缺省中间层）
function ancestorsOf(p){                        // p 的所有上层目录链
  const out=[]; if(!p) return out;
  const parts=p.split("/"); let a="";
  for(let i=0;i<parts.length-1;i++){ a=a?a+"/"+parts[i]:parts[i]; out.push(a); }
  return out;
}
function nodeVisible(p, exp){
  if(!p) return true;                           // 根节点（全部/项目根）始终可见
  // 只要求“真实存在的祖先节点”都已展开；中间层目录若因无直接文件而不在树上，直接跳过，
  // 否则会出现“点了父目录却看不到更深层子目录”的假象
  const parts=p.split("/"); let a="";
  for(let i=0;i<parts.length-1;i++){
    a=a?a+"/"+parts[i]:parts[i];
    if(treeNodeSet.has(a) && !exp.has(a)) return false;
  }
  return true;
}
async function loadTree(){
  // 深度不再由用户选择：下探式只渲染已展开部分，后端给一个足够大的兜底上限（防极深嵌套）即可
  const r=await apiGet('/api/tree?depth=999&project='+state.project);
  treeData=r.folders||[];
  treeNodeSet=new Set(treeData.map(f=>f.path));
  treeExpanded=new Set();                        // 切换项目重置展开状态
  renderTree();
}
function renderTree(){
  const el=document.getElementById('tree');
  if(!treeData.length){ el.innerHTML='<div class="empty">暂无数据，请先扫描</div>'; return; }
  // 首次渲染：展开当前所选目录的祖先链，使其可见（刷新恢复现场时也适用）
  if(treeExpanded.size===0 && state.folder){ ancestorsOf(state.folder).forEach(x=>treeExpanded.add(x)); }
  // 有子节点 = 存在任何以该路径开头的更深路径（即使本层无直接文件，只要更深层有文件也应可展开）
  const hasChild=new Set();
  treeData.forEach(f=>{
    if(!f.path) return;
    const pre=f.path+"/";
    for(const g of treeData){ if(g.path.startsWith(pre)){ hasChild.add(f.path); break; } }
  });
  // 按完整路径排序：父目录紧邻其子目录，形成下探式嵌套（而非先平铺所有一级再二级）
  const vis=treeData.filter(f=>nodeVisible(f.path,treeExpanded)).sort((a,b)=>a.path.localeCompare(b.path,'zh'));
  el.innerHTML=vis.map(f=>{
    const depth=f.path==="" ? 0 : f.path.split("/").length;
    const pad=8+depth*14, active=state.folder===f.path?'active':'';
    const name = f.path ? f.name : (state.project ? '（项目根目录）' : '（扫描过的所有文件）');
    const isOpen=treeExpanded.has(f.path);
    const tri=hasChild.has(f.path) ? (isOpen?'▾':'▸') : '';   // 含子目录才有展开三角
    return `<div class="tree-node ${active}" style="padding-left:${pad}px" data-folder="${esc(f.path)}" data-real="${esc(f.real||'')}">`
         + `<span class="tw" data-toggle="1">${tri}</span> 📂 ${esc(name)}<span class="cnt">(${f.count})</span></div>`;
  }).join('');
  el.querySelectorAll('.tree-node').forEach(n=>{
    n.onclick=(e)=>{
      const fp=n.dataset.folder;
      if(e.target.dataset.toggle){               // 点三角：仅切换展开/折叠，不改变列表筛选
        if(treeExpanded.has(fp)) treeExpanded.delete(fp); else treeExpanded.add(fp);
        renderTree(); return;
      }
      state.folder=fp;
      state.folderReal=n.dataset.real||'';        // 节点自带真实路径，打开时无需任何解析
      if(fp && !treeExpanded.has(fp)) treeExpanded.add(fp);
      state.page=1; renderTree(); loadFiles();
    };
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
