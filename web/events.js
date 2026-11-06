// 事件绑定与初始化
// ---------- 事件绑定 ----------
document.getElementById('searchBtn').onclick=runSearch;
document.getElementById('projRescanBtn').onclick=()=>{
  if(!state.project){ toast('当前未选择项目，请先选择或新建项目后再扫描'); return; }
  onProjAction('scan', state.project);
};
document.getElementById('searchInput').addEventListener('keydown',e=>{ if(e.key==='Enter') runSearch(); });
document.getElementById('statusFilter').onchange=runSearch;
document.getElementById('extFilter').addEventListener('keydown',e=>{ if(e.key==='Enter') runSearch(); });
document.getElementById('typeFilter').onchange=()=>{
  // 选“自定义格式…”时显示多格式输入框，其余预设隐藏它
  document.getElementById('extFilter').style.display =
    document.getElementById('typeFilter').value==='_custom' ? '' : 'none';
  runSearch();
};
// ---------- 「更多 ⋯」收纳菜单：低频维护类按钮 ----------
function moreMenuSetOpen(open){
  const menu=document.getElementById('moreMenu'), btn=document.getElementById('moreBtn');
  const wrap=document.getElementById('moreMenuWrap');
  if(!menu||!btn) return;
  if(open){
    menu.hidden=false;
    // 默认靠右对齐（外层按钮在窄屏会换行到左侧），再按视口夹一次，保证不被裁掉
    menu.style.left=''; menu.style.right='';
    const mr=menu.getBoundingClientRect(), wr=wrap.getBoundingClientRect();
    if(mr.right > innerWidth-8){
      menu.style.right='auto';
      menu.style.left=(Math.max(8, innerWidth-8-mr.width) - wr.left)+'px';
    }else if(mr.left < 8){
      menu.style.right='auto';
      menu.style.left=(8-wr.left)+'px';
    }
    const f=menu.querySelector('.more-item'); if(f) f.focus();
  }else{
    menu.hidden=true;
    menu.style.left=''; menu.style.right='';
    if(document.activeElement && menu.contains(document.activeElement)) btn.focus();
  }
  btn.setAttribute('aria-expanded', open?'true':'false');
}
// 菜单里可能改变过滤/感知状态，用一个小圆点提示“有开关处于开启状态”，避免收进菜单后看不见状态
function updateMoreDot(){
  const d=document.getElementById('moreDot');
  if(!d) return;
  const watchOn = document.getElementById('watchBtn').dataset.on==='1';
  d.hidden = !(state.showMissing || watchOn);
  document.getElementById('moreBtn').title = d.hidden ? '更多维护类操作'
    : '更多维护类操作（有筛选/实时感知处于开启状态）';
}
document.getElementById('moreBtn').onclick=e=>{
  e.stopPropagation();
  moreMenuSetOpen(document.getElementById('moreMenu').hidden);
};
document.getElementById('moreMenu').addEventListener('keydown',e=>{
  if(e.key==='Escape'){ e.preventDefault(); moreMenuSetOpen(false); return; }
  if(e.key!=='ArrowDown'&&e.key!=='ArrowUp') return;
  const items=Array.from(document.querySelectorAll('#moreMenu .more-item'));
  const i=items.indexOf(document.activeElement);
  if(i<0) return;
  e.preventDefault();
  items[(i+(e.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();
});
// 菜单项点击后收起；每个菜单项仍走各自的原有处理函数，这里不复制任何业务逻辑
document.getElementById('moreMenu').addEventListener('click',e=>{
  if(e.target.closest('.more-item')) moreMenuSetOpen(false);
});
// 点外部 / Esc 关闭
document.addEventListener('click',e=>{
  const wrap=document.getElementById('moreMenuWrap');
  if(wrap && !wrap.contains(e.target)) moreMenuSetOpen(false);
});
document.addEventListener('keydown',e=>{
  const menu=document.getElementById('moreMenu');
  if(e.key==='Escape' && menu && !menu.hidden){ e.preventDefault(); moreMenuSetOpen(false); }
});
// ---------- 调试日志面板 ----------
document.getElementById('debugBtn').onclick=()=>{
  document.getElementById('debugPanel').classList.add('open'); renderDebugLog(); };
document.getElementById('closeDebug').onclick=()=>
  document.getElementById('debugPanel').classList.remove('open');
document.getElementById('clearDebugLog').onclick=()=>{ fitLogs.length=0; renderDebugLog(); };
document.getElementById('copyDebugLog').onclick=()=>{
  const text=fitLogs.map(l=>'['+fmtLogTime(l.t)+']'+(l.err?'[错误] ':' ')+l.msg).join('\n');
  navigator.clipboard.writeText(text).then(()=>toast('已复制全部日志（'+fitLogs.length+' 条）'))
    .catch(()=>toast('复制失败，请在面板中手动选择文本'));
};
// ←/→ 快速在详情间切换（输入框/下拉内不生效）
document.addEventListener('keydown',e=>{
  if(e.altKey||e.ctrlKey||e.metaKey) return;
  if(e.key!=='ArrowLeft'&&e.key!=='ArrowRight') return;
  const tg=e.target&&e.target.tagName;
  if(tg==='INPUT'||tg==='TEXTAREA'||tg==='SELECT') return;
  if(document.getElementById('dirModal').classList.contains('show')) return;  // 目录选择器内方向键用于浏览
  navDetail(e.key==='ArrowLeft'?-1:1);
});

document.getElementById('selectAll').onchange=e=>{
  // 全选/取消全选都作用于“当前页”文件行；写回集合后由 updateSelBar → syncSelectAllState 同步表头
  selectAllPage(e.target.checked);
};
document.getElementById('prevPage').onclick=()=>{ if(state.page>1){ state.page--; loadFiles(); } };
document.getElementById('nextPage').onclick=()=>{
  // 上限 = ceil(total/page_size)，由 renderList 写入 state.pages
  if(state.page < (state.pages||1)){ state.page++; loadFiles(); }
};
document.getElementById('tasksBtn').onclick=()=>{ document.getElementById('tasksPanel').classList.toggle('open'); if(document.getElementById('tasksPanel').classList.contains('open')) loadTasks(); };
document.getElementById('closeTasks').onclick=()=>document.getElementById('tasksPanel').classList.remove('open');
document.getElementById('exportBtn').onclick=doExport;
document.getElementById('batchEditBtn').onclick=openBatchModal;
document.getElementById('saveBatch').onclick=saveBatch;
document.getElementById('cancelBatch').onclick=()=>document.getElementById('batchModal').classList.remove('show');
document.getElementById('reportBtn').onclick=doReport;
document.getElementById('watchBtn').onclick=toggleWatch;
document.getElementById('cancelDir').onclick=closeDirModal;
document.getElementById('okDirBtn').onclick=()=>{ if(dirCur) pickDir(dirCur); else closeDirModal(); };
document.getElementById('transcribeSelBtn').onclick=()=>{
  const ids=Array.from(state.selection);
  if(!ids.length){ toast('请先勾选文件'); return; }
  openTranscribePromptModal(ids);
};
document.getElementById('ocrSelBtn').onclick=()=>{
  const ids=Array.from(state.selection);
  if(!ids.length){ toast('请先勾选文件'); return; }
  startOcr(ids);
};
// OCR 进度浮层：可转入后台（任务继续跑，完成时 toast 提示并刷新）
document.getElementById('ocrHide').onclick=()=>document.getElementById('ocrOverlay').classList.remove('show');
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
  b.classList.toggle('on', state.showMissing);
  updateMoreDot();
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
const restored=restoreState();                 // 先恢复上次现场（项目/位置/搜索/类型/文件）
renderTagFilter();
updateMoreDot();                               // 恢复的“显示已失效”开关要在“更多”入口上体现出来
loadStatuses().then(()=>{ document.getElementById('statusFilter').value=state.status; }).catch(()=>{});
// 先载入项目列表，再按恢复的项目载入统计 / 树 / 标签 / 清单，最后回到上次选中的文件
loadProjects().then(()=>{
  loadStats(); loadTree(); loadTags(); loadFiles();
  if(restored && state.currentId) selectFile(state.currentId);
}).catch(()=>{});
apiGet('/api/transcribe/check').then(r=>{ state.whisper=r; if(state.currentId) selectFile(state.currentId); })
  .catch(()=>{ state.whisper={available:false}; });
refreshOcrBadge();
refreshWatchBtn();
// P4：进程重启后，上次未完成的任务已在启动时标记为中断，这里提示用户
apiGet('/api/jobs/interrupted').then(r=>{
  if(r && r.count) toast('上次运行有 '+r.count+' 个任务未完成（已标记为中断，可重新触发）');
}).catch(()=>{});
