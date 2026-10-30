// 事件绑定与初始化
// ---------- 事件绑定 ----------
document.getElementById('scanBtn').onclick=doScan;
document.getElementById('searchBtn').onclick=runSearch;
document.getElementById('searchInput').addEventListener('keydown',e=>{ if(e.key==='Enter') runSearch(); });
document.getElementById('statusFilter').onchange=runSearch;
document.getElementById('extFilter').addEventListener('keydown',e=>{ if(e.key==='Enter') runSearch(); });
document.getElementById('typeFilter').onchange=()=>{
  // 选“自定义格式…”时显示多格式输入框，其余预设隐藏它
  document.getElementById('extFilter').style.display =
    document.getElementById('typeFilter').value==='_custom' ? '' : 'none';
  runSearch();
};
document.getElementById('treeDepth').onchange=loadTree;
document.getElementById('selectAll').onchange=e=>{
  const chk=e.target.checked;
  document.querySelectorAll('tr.row').forEach(tr=>{ const id=parseInt(tr.dataset.id); if(chk)state.selection.add(id); else state.selection.delete(id); tr.classList.toggle('sel',chk); tr.querySelector('.rowchk').checked=chk; });
  updateSelBar();
};
document.getElementById('prevPage').onclick=()=>{ if(state.page>1){ state.page--; loadFiles(); } };
document.getElementById('nextPage').onclick=()=>{ state.page++; loadFiles(); };
document.getElementById('tasksBtn').onclick=()=>{ document.getElementById('tasksPanel').classList.toggle('open'); if(document.getElementById('tasksPanel').classList.contains('open')) loadTasks(); };
document.getElementById('closeTasks').onclick=()=>document.getElementById('tasksPanel').classList.remove('open');
document.getElementById('exportBtn').onclick=doExport;
document.getElementById('batchEditBtn').onclick=openBatchModal;
document.getElementById('saveBatch').onclick=saveBatch;
document.getElementById('cancelBatch').onclick=()=>document.getElementById('batchModal').classList.remove('show');
document.getElementById('reportBtn').onclick=doReport;
document.getElementById('watchBtn').onclick=toggleWatch;
document.getElementById('pickDirBtn').onclick=openDirModal;
document.getElementById('cancelDir').onclick=closeDirModal;
document.getElementById('okDirBtn').onclick=()=>{ if(dirCur) pickDir(dirCur); else closeDirModal(); };
document.getElementById('clearSelBtn').onclick=()=>{ state.selection.clear(); updateSelBar(); loadFiles(); };
document.getElementById('transcribeSelBtn').onclick=()=>{
  const ids=Array.from(state.selection);
  if(!ids.length){ toast('请先勾选文件'); return; }
  startTranscribe(ids);
};
document.getElementById('ocrSelBtn').onclick=()=>{
  const ids=Array.from(state.selection);
  if(!ids.length){ toast('请先勾选文件'); return; }
  startOcr(ids);
};
// 项目：切换 / 管理面板 / 编辑弹窗
document.getElementById('projectSel').onchange=e=>switchProject(e.target.value);
document.getElementById('projMgrBtn').onclick=()=>{
  const pn=document.getElementById('projPanel');
  pn.classList.toggle('open');
  if(pn.classList.contains('open')) loadProjects();
};
document.getElementById('missingBtn').onclick=()=>{
  state.showMissing=!state.showMissing;
  const b=document.getElementById('missingBtn');
  b.textContent=state.showMissing?'👻 隐藏已失效':'👻 显示已失效';
  b.classList.toggle('btn-primary', state.showMissing);
  state.page=1; loadFiles();
};
document.getElementById('closeProj').onclick=()=>document.getElementById('projPanel').classList.remove('open');
document.getElementById('newProjBtn').onclick=()=>openProjEdit(null);
document.getElementById('cancelProjEdit').onclick=()=>document.getElementById('projModal').classList.remove('show');
document.getElementById('addProjRoot').onclick=()=>{ const v=projRootValues(); v.push(''); renderProjRoots(v); };
document.getElementById('saveProj').onclick=saveProj;

document.getElementById('statusMgrBtn').onclick=openStatusModal;
document.getElementById('cancelStatus').onclick=()=>document.getElementById('statusModal').classList.remove('show');
document.getElementById('saveStatus').onclick=saveStatuses;
document.getElementById('addStatusRow').onclick=()=>{
  const ed=document.getElementById('statusEditor');
  const d=document.createElement('div'); d.className='st-row';
  d.innerHTML=`<input type="color" value="#64748b"><input type="text" placeholder="新状态名"><button class="btn-danger btn-sm" data-del>删</button>`;
  d.querySelector('[data-del]').onclick=()=>d.remove(); ed.appendChild(d);
};

// ---------- 初始化 ----------
updateCrumb(); renderTagFilter();
loadStatuses().catch(()=>{});
// 先载入项目列表，再按当前项目（默认“全部文件”）载入统计 / 树 / 标签 / 清单
loadProjects().then(()=>{ loadStats(); loadTree(); loadTags(); loadFiles(); }).catch(()=>{});
apiGet('/api/transcribe/check').then(r=>{ state.whisper=r; if(state.currentId) selectFile(state.currentId); })
  .catch(()=>{ state.whisper={available:false}; });
apiGet('/api/ocr/check').then(r=>{ state.ocr=r; }).catch(()=>{ state.ocr={available:false}; });
refreshWatchBtn();
