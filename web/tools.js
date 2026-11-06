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
    b.dataset.on = s.enabled ? '1' : '0';
    b.classList.toggle('on', !!s.enabled);
    updateMoreDot();                       // 感知状态收在菜单里，用小圆点提示
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

// ---------- 排除规则 ----------
async function renderExcludes(){
  const box=document.getElementById('excludeList');
  try{
    const r=await apiGet('/api/excludes');
    if(!r.excludes.length){
      box.innerHTML='<div class="empty" style="padding:10px">暂无排除规则，所有目录都会被索引。'
        +'（删除内置规则后不会再自动恢复，可用「恢复默认规则」找回）</div>';
      return;
    }
    box.innerHTML=r.excludes.map(x=>`
      <div class="root-row">
        <input type="text" value="${esc(x.pattern)}" readonly>
        ${x.builtin?'<span class="tag-builtin">内置</span>':''}
        <button class="btn-danger btn-sm" data-eid="${x.id}">删</button>
      </div>`).join('');
    box.querySelectorAll('[data-eid]').forEach(b=>{
      b.onclick=async()=>{
        try{
          await apiPost('/api/excludes/delete',{id:Number(b.dataset.eid)});
          await renderExcludes();
          toast('已删除规则；已索引的数据仍在，可点“清理已索引”后移除');
        }catch(e){ toast('删除失败: '+e.message); }
      };
    });
  }catch(e){
    box.innerHTML='<div class="empty" style="padding:10px">加载失败：'+esc(e.message)+'</div>';
  }
}
async function restoreDefaultExcludes(){
  try{
    const r=await apiPost('/api/excludes/restore',{});
    await renderExcludes();
    toast(r.added?('已恢复 '+r.added+' 条内置规则'):'内置规则已齐全，无需恢复');
  }catch(e){ toast('恢复失败: '+e.message); }
}
async function openExcludeModal(){
  document.getElementById('excludeInput').value='';
  document.getElementById('excludeModal').classList.add('show');
  await renderExcludes();
}
async function addExclude(){
  const inp=document.getElementById('excludeInput');
  const v=inp.value.trim();
  if(!v){ toast('请先输入规则'); return; }
  try{
    await apiPost('/api/excludes',{pattern:v});
    inp.value='';
    await renderExcludes();
    toast('已添加规则：'+v+'（重扫时生效；如需清理存量请点“清理已索引”）');
  }catch(e){ toast('添加失败: '+e.message); }
}
async function pruneExcludes(){
  if(!confirm('将按当前排除规则，从索引中删除命中的文件记录。\n\n只删除索引，不会删除磁盘上的任何文件。\n\n确定继续？')) return;
  if(!confirm('再次确认：这些文件的备注 / 状态 / 标签会一并从索引移除，且无法通过界面恢复。仍要继续？')) return;
  try{
    const r=await apiPost('/api/excludes/prune',{confirm:true});
    await loadStats(); loadFiles();
    toast('已清理 '+(r.deleted||0)+' 条索引记录');
  }catch(e){ toast('清理失败: '+e.message); }
}
document.getElementById('excludeBtn').onclick=openExcludeModal;
document.getElementById('closeExclude').onclick=()=>document.getElementById('excludeModal').classList.remove('show');
document.getElementById('addExcludeBtn').onclick=addExclude;
document.getElementById('restoreExcludeBtn').onclick=restoreDefaultExcludes;
document.getElementById('pruneExcludeBtn').onclick=pruneExcludes;
document.getElementById('excludeInput').addEventListener('keydown',e=>{ if(e.key==='Enter') addExclude(); });

// ---------- 设置面板 ----------
// GET /api/settings 返回每项的元数据 + 当前生效值 + 来源（env/file/default），
// 由后端下发的 schema 直接渲染，前端不重复维护键名与默认值。
let settingsPayload=null;
const SETTINGS_SRC_LABEL={env:'环境变量（只读）',file:'配置文件',default:'默认值'};

function settingsControl(it){
  const dis=it.locked?'disabled':'';
  if(it.type==='enum'){
    const opts=(it.options||[]).map(o=>
      `<option value="${esc(o.value)}"${o.value===it.value?' selected':''}>${esc(o.label)}</option>`).join('');
    return `<select data-key="${esc(it.key)}" ${dis}>${opts}</select>`;
  }
  if(it.type==='bool'){
    return `<label class="chk"><input type="checkbox" data-key="${esc(it.key)}" ${it.value?'checked':''} ${dis}> 开启</label>`;
  }
  if(it.type==='int'){
    return `<input type="number" data-key="${esc(it.key)}" value="${esc(String(it.value))}"`
      +` min="${it.min!=null?it.min:''}" max="${it.max!=null?it.max:''}" ${dis}>`;
  }
  return `<input type="text" data-key="${esc(it.key)}" value="${esc(it.value)}"`
    +` placeholder="${esc(it.placeholder||'')}" ${dis}>`;
}

function renderSettings(){
  const body=document.getElementById('settingsBody');
  if(!settingsPayload || !settingsPayload.settings){
    body.innerHTML='<div class="empty">暂无设置项</div>'; return;
  }
  const groups=[];
  settingsPayload.settings.forEach(it=>{
    let g=groups.find(x=>x.name===it.group);
    if(!g){ g={name:it.group,items:[]}; groups.push(g); }
    g.items.push(it);
  });
  body.innerHTML=groups.map(g=>`
    <div class="set-group">${esc(g.name)}</div>
    ${g.items.map(it=>`
      <div class="set-row">
        <div class="set-top">
          <span class="set-label">${esc(it.label)}</span>
          <span class="set-src ${esc(it.source)}">${esc(SETTINGS_SRC_LABEL[it.source]||it.source)}</span>
          <span class="set-eff">${esc(it.effect_text||'')}</span>
        </div>
        <div class="set-ctl">${settingsControl(it)}</div>
        ${it.locked?`<div class="set-lock">该值由环境变量 <b>${esc(it.env)}</b> 控制，界面修改不会生效；如需在界面修改，请先移除该环境变量。</div>`:''}
        <div class="set-help">${esc(it.help||'')}</div>
      </div>`).join('')}`).join('');
  const f=document.getElementById('settingsFile');
  if(f) f.textContent='配置文件：'+(settingsPayload.config_file||'');
  document.getElementById('settingsErr').textContent='';
}

async function openSettingsModal(){
  document.getElementById('settingsModal').classList.add('show');
  document.getElementById('settingsErr').textContent='';
  document.getElementById('settingsBody').innerHTML='<div class="empty">加载中…</div>';
  try{
    settingsPayload=await apiGet('/api/settings');
    renderSettings();
  }catch(e){
    document.getElementById('settingsBody').innerHTML='<div class="empty">加载失败：'+esc(e.message)+'</div>';
  }
}

function closeSettingsModal(){
  document.getElementById('settingsModal').classList.remove('show');
}

async function saveSettings(){
  const err=document.getElementById('settingsErr');
  err.textContent='';
  if(!settingsPayload) return;
  const values={};
  document.querySelectorAll('#settingsBody [data-key]').forEach(el=>{
    if(el.disabled) return;                     // 被环境变量锁住的项不提交
    const k=el.dataset.key;
    values[k]= el.type==='checkbox' ? (el.checked?'1':'0') : el.value;
  });
  try{
    const r=await apiPost('/api/settings',{values});
    settingsPayload=r;
    renderSettings();
    // 只对本次真正改动的项汇总生效时机，避免“保存了但不知道何时生效”
    const effects=(r.changed||[]).map(k=>{
      const it=(r.settings||[]).find(x=>x.key===k);
      return it?it.effect_text:'';
    }).filter((v,i,a)=>v && a.indexOf(v)===i);
    let msg='设置已保存';
    if(effects.length) msg+='（'+effects.join('、')+'）';
    if(r.messages && r.messages.length) msg+='；'+r.messages.join('；');
    toast(msg);
    // 立即刷新顶部 OCR 徽标 / 转写状态，让用户保存后看到实际生效值
    apiGet('/api/transcribe/check').then(x=>{ state.whisper=x; if(state.currentId) selectFile(state.currentId); }).catch(()=>{});
    refreshOcrBadge();
  }catch(e){
    err.textContent='保存失败：'+e.message;
    toast('保存失败：'+e.message);
  }
}

document.getElementById('settingsBtn').onclick=openSettingsModal;
document.getElementById('cancelSettings').onclick=closeSettingsModal;
document.getElementById('saveSettingsBtn').onclick=saveSettings;
