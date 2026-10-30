// 导出/目录选择弹窗/批量编辑/报告/实时感知/状态管理
// ---------- 导出 ----------
async function doExport(){
  if(!state.selection.size){ toast('请先勾选文件'); return; }
  const ids=Array.from(state.selection);
  const name=prompt('导出压缩包名称：', 'file_index_export');
  if(name===null) return;
  const sidecar=confirm('是否为每个文件额外生成 .文件名.note.json 备注副文件？\n（取消则仅在压缩包根目录提供 manifest 清单）');
  try{
    const r=await fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids, sidecar, name})});
    if(!r.ok) throw new Error('导出失败');
    const blob=await r.blob(); const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=name+'.zip'; a.click();
    toast('已导出 '+ids.length+' 个文件（含备注清单）');
  }catch(e){ toast('导出失败: '+e.message); }
}

// ---------- 根目录点击选择 ----------
let dirCur = "";
function closeDirModal(){ document.getElementById('dirModal').classList.remove('show'); }
let dirPickCb=null;                       // 非空的目录选择回调（项目编辑时用）
function pickDir(p){
  if(dirPickCb) dirPickCb(p);
  else{ const rp=document.getElementById('rootPath'); if(rp) rp.value=p; }  // 兼容旧入口
  closeDirModal();
}
async function openDirModal(onPick){
  dirPickCb=onPick||null;
  document.getElementById('dirModal').classList.add('show');
  try{
    const q=await apiGet('/api/dirs/quick');
    const qd=document.getElementById('dirQuick');
    qd.innerHTML=(q.quick||[]).map(x=>`<button class="btn-ghost btn-sm" data-p="${esc(x.path)}">${esc(x.name)}</button>`).join('')
      || '<span style="color:var(--muted);font-size:12px">无快捷位置</span>';
    qd.querySelectorAll('[data-p]').forEach(b=>b.onclick=()=>loadDir(b.dataset.p));
  }catch(e){}
  await loadDir("");
}
async function loadDir(p){
  try{
    const r=await apiGet('/api/dirs?path='+encodeURIComponent(p||""));
    dirCur=r.current||"";
    document.getElementById('dirCurrent').textContent=dirCur||"（计算机）";
    const el=document.getElementById('dirList');
    if(!r.dirs.length){
      el.innerHTML='<div class="empty" style="padding:14px">没有子文件夹</div>';
    }else{
      el.innerHTML=r.dirs.map(d=>`<div class="tree-node" data-p="${esc(d.path)}">📁 ${esc(d.name)}</div>`).join('');
      el.querySelectorAll('.tree-node').forEach(n=>{
        n.onclick=()=>loadDir(n.dataset.p);
        n.ondblclick=()=>pickDir(n.dataset.p);
      });
    }
    document.getElementById('dirUpBtn').onclick=()=>loadDir(r.parent||"");
  }catch(e){ toast('读取目录失败: '+e.message); }
}

// ---------- 批量编辑 ----------
function openBatchModal(){
  if(!state.selection.size){ toast('请先勾选文件'); return; }
  document.getElementById('batchCount').textContent=state.selection.size;
  const sel=document.getElementById('batchStatus');
  sel.innerHTML='<option value="">— 不修改 —</option>'+
    state.statuses.map(s=>`<option value="${esc(s.label)}">${esc(s.label)}</option>`).join('');
  document.getElementById('batchTags').value='';
  document.getElementById('batchNote').value='';
  document.getElementById('batchModal').classList.add('show');
}
async function saveBatch(){
  const payload={ ids:Array.from(state.selection),
    status:document.getElementById('batchStatus').value||'',
    tags:document.getElementById('batchTags').value,
    tags_mode:document.getElementById('batchTagsMode').value,
    note:document.getElementById('batchNote').value,
    note_mode:document.getElementById('batchNoteMode').value };
  try{
    const r=await apiPost('/api/batch', payload);
    document.getElementById('batchModal').classList.remove('show');
    toast('已更新 '+r.updated+' 个文件');
    await loadStats(); await loadTags(); await loadFiles();
    if(state.currentId) selectFile(state.currentId);
  }catch(e){ toast('批量更新失败: '+e.message); }
}

// ---------- 报告导出（CSV / Markdown） ----------
async function doReport(){
  if(!state.selection.size){ toast('请先勾选文件'); return; }
  const fmt=confirm('确定导出 Markdown 报告？\n（点“取消”则导出 CSV）')?'md':'csv';
  try{
    const r=await fetch('/api/report',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ids:Array.from(state.selection), format:fmt})});
    if(!r.ok) throw new Error('导出失败');
    const blob=await r.blob(); const a=document.createElement('a');
    a.href=URL.createObjectURL(blob); a.download='file_report.'+fmt; a.click();
    toast('已导出 '+(fmt==='md'?'Markdown':'CSV')+' 报告');
  }catch(e){ toast('导出失败: '+e.message); }
}

// ---------- 目录实时感知 ----------
async function refreshWatchBtn(){
  try{
    const s=await apiGet('/api/watch');
    const b=document.getElementById('watchBtn');
    b.textContent = s.enabled ? '👁 感知中（点击停止）' : '👁 实时感知';
    b.style.background = s.enabled ? '#16a34a' : '';
    b.style.color = s.enabled ? '#fff' : '';
    b.title = s.watchdog_installed ? (s.error || '自动同步新增/修改/删除的文件')
                                   : '未安装 watchdog，请 pip install watchdog';
  }catch(e){}
}
async function toggleWatch(){
  try{
    const s=await apiGet('/api/watch');
    if(s.enabled){ await apiPost('/api/watch',{action:'stop'}); toast('已停止实时感知'); }
    else{ const r=await apiPost('/api/watch',{action:'start'}); toast('已开启实时感知：'+r.roots.join('、')); }
    await refreshWatchBtn();
  }catch(e){ toast('实时感知不可用: '+e.message); }
}

// ---------- 状态管理弹窗 ----------
function openStatusModal(){
  const ed=document.getElementById('statusEditor');
  ed.innerHTML=state.statuses.map((s,i)=>`
    <div class="st-row">
      <input type="color" value="${s.color}">
      <input type="text" value="${esc(s.label)}">
      <button class="btn-danger btn-sm" data-del="${i}">删</button>
    </div>`).join('');
  ed.querySelectorAll('.st-row').forEach(row=>{
    row.querySelector('[data-del]').onclick=()=>row.remove();
  });
  document.getElementById('statusModal').classList.add('show');
}
async function saveStatuses(){
  const rows=document.querySelectorAll('#statusEditor .st-row');
  const lst=[];
  rows.forEach(r=>{ const color=r.querySelector('input[type=color]').value; const label=r.querySelector('input[type=text]').value.trim();
    if(label) lst.push({label, color}); });
  if(!lst.length){ toast('至少需要一个状态'); return; }
  try{ const r=await apiPut('/api/statuses',{statuses:lst}); state.statuses=r.statuses;
    document.getElementById('statusModal').classList.remove('show');
    await loadStatuses(); if(state.currentId) selectFile(state.currentId); loadStats(); loadFiles();
    toast('状态已更新'); }catch(e){ toast('保存失败: '+e.message); }
}
