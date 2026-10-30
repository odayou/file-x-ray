// 搜索/过滤/面包屑
// ---------- 搜索 / 过滤 ----------
function runSearch(){
  state.search=document.getElementById('searchInput').value.trim();
  state.status=document.getElementById('statusFilter').value;
  const tv=document.getElementById('typeFilter').value;
  state.type = tv==='_custom' ? '' : tv;                    // 预设类型
  state.ext   = tv==='_custom' ? document.getElementById('extFilter').value.trim().toLowerCase() : '';
  state.page=1;
  loadFiles(); saveTask();
}
async function saveTask(){
  try{ await apiPost('/api/tasks',{query:state.search,status_filter:state.status,ext_filter:state.ext,folder_filter:state.folder,tag_filter:state.tag,project_id:state.project}); }
  catch(e){ /* 非致命 */ }
}
