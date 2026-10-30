// 搜索/过滤/面包屑
// ---------- 搜索 / 过滤 ----------
function runSearch(){
  state.search=document.getElementById('searchInput').value.trim();
  state.status=document.getElementById('statusFilter').value;
  const tv=document.getElementById('typeFilter').value;
  state.type = tv==='_custom' ? '' : tv;                    // 预设类型
  state.ext   = tv==='_custom' ? document.getElementById('extFilter').value.trim().toLowerCase() : '';
  state.folder=document.querySelector('#folderCrumb b').dataset.folder||'';
  state.page=1;
  loadFiles(); saveTask();
}
async function saveTask(){
  try{ await apiPost('/api/tasks',{query:state.search,status_filter:state.status,ext_filter:state.ext,folder_filter:state.folder,tag_filter:state.tag,project_id:state.project}); }
  catch(e){ /* 非致命 */ }
}
function updateCrumb(){
  const cr=document.getElementById('folderCrumb');
  if(!state.folder){ cr.innerHTML='位置：<b data-folder="">全部</b>'; }
  else{
    const parts=state.folder.split('/'); let html='位置：<b data-folder="">全部</b> / '; let acc='';
    parts.forEach((p,i)=>{ acc= i===0?p:acc+'/'+p; html+=`<b data-folder="${esc(acc)}">${esc(p)}</b>${i<parts.length-1?' / ':''}`; });
    cr.innerHTML=html;
  }
  cr.querySelectorAll('b').forEach(b=>b.onclick=()=>{ state.folder=b.dataset.folder||''; state.page=1; renderTree(); updateCrumb(); loadFiles(); });
}
