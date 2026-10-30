// 项目（虚拟视图）及其编辑弹窗
// ---------- 项目（文件的自由组合 / 虚拟视图） ----------
async function loadProjects(){
  try{ const r=await apiGet('/api/projects'); state.projects=r.projects||[]; }
  catch(e){ state.projects=[]; }
  renderProjectSel();
  if(document.getElementById('projPanel').classList.contains('open')) renderProjList();
}

function projName(pid){
  const p=state.projects.find(x=>x.id===(pid||0));
  return p?p.name:'全部文件';
}

function renderProjectSel(){
  const el=document.getElementById('projectSel');
  // 项目被删除后自动回落到“全部文件”
  if(state.project && !state.projects.some(p=>p.id===state.project)) state.project=0;
  el.innerHTML='<option value="0">📁 全部文件</option>'+
    state.projects.map(p=>{
      const r0=(p.roots||[])[0]||'';
      return `<option value="${p.id}">${esc(p.name)}（${p.file_count}）${r0?' — '+esc(shortPath(r0)):''}</option>`;
    }).join('');
  el.value=String(state.project);
  renderProjRootBar();
}

// 路径太长时保留头尾，中间省略（尾部目录名通常才是关键信息）
function shortPath(p){
  if(!p) return '';
  if(p.length<=36) return p;
  const sep = p.indexOf('\\')>=0 ? '\\' : '/';
  const parts=p.split(/[\\/]/).filter(Boolean);
  if(parts.length<=2) return p;
  return parts[0]+sep+'…'+sep+parts.slice(-2).join(sep);
}

// 工具栏常驻显示：当前项目的根目录路径（点击打开、悬停看全路径、失效时标红）
function renderProjRootBar(){
  const el=document.getElementById('projRootBar');
  const p=state.projects.find(x=>x.id===state.project);
  if(!p || !(p.roots||[]).length){ el.classList.remove('show'); el.innerHTML=''; el.title=''; return; }
  const roots=p.roots, infos=p.roots_info||[];
  const dead=infos.filter(i=>i.exists===false);
  const txt=roots.length===1 ? shortPath(roots[0]) : shortPath(roots[0])+' 等 '+roots.length+' 个目录';
  el.innerHTML='📂 '+esc(txt)+(dead.length?' ⚠':'')
    + ' <button class="btn-ghost btn-sm" id="projRescanBtn" title="重新扫描该项目根目录">🔄 扫描</button>';
  el.title = (dead.length?'⚠ 有 '+dead.length+' 个目录已失效（被改名或移动过），点「📁 管理项目」可重新定位\n\n':'')
           + roots.join('\n') + '\n\n点击打开：'+roots[0];
  el.classList.add('show');
  el.classList.toggle('dead', dead.length>0);
  const rb=document.getElementById('projRescanBtn');
  if(rb) rb.onclick=e=>{ e.stopPropagation(); onProjAction('scan', state.project); };
  el.onclick=async()=>{
    if(dead.length && infos[0] && infos[0].exists===false){
      toast('该目录已失效：'+roots[0]+'（可在「📁 管理项目」里重新定位）'); return;
    }
    try{ await apiPost('/api/open',{path:roots[0]}); toast('已打开：'+roots[0]); }
    catch(e){ toast('打开失败: '+e.message); }
  };
}

function switchProject(pid){
  pid=parseInt(pid||0,10)||0;
  state.project=pid;
  state.page=1; state.folder=''; state.tag='';
  state.selection.clear(); state.currentId=null;
  updateSelBar(); renderProjectSel(); updateCrumb();
  if(document.getElementById('projPanel').classList.contains('open')) renderProjList();
  loadStats(); loadTree(); loadTags(); loadFiles();
}

function renderProjList(){
  const el=document.getElementById('projList');
  if(!state.projects.length){
    el.innerHTML='<div class="empty">还没有项目<br>扫描任意目录会自动创建一个<br>也可以点「＋ 新建」直接建立</div>';
    return;
  }
  el.innerHTML=state.projects.map(p=>{
    const roots=p.roots||[];
    const infos=p.roots_info||[];
    const rootHtml=roots.length
      ? roots.map((r,i)=>{
          const dead=infos[i] && infos[i].exists===false;
          return `<div class="meta">📂 ${esc(r)}${dead?' <span class="root-dead">（已失效：目录被改名或移动过）</span>':''}`
            + (dead?` <button class="btn-warn" data-act="relocate" data-id="${p.id}" data-root="${esc(r)}">📍 重新定位</button>`
                   :` <button class="btn-ghost btn-sm" data-act="openroot" data-id="${p.id}" data-root="${esc(r)}" title="在系统文件管理器中打开这个目录">打开</button>`)
            + ` <button class="root-copy" data-act="copyroot" data-id="${p.id}" data-root="${esc(r)}" title="复制完整路径">复制</button>`
            + `</div>`;
        }).join('')
      : '<div class="meta">📂 不限制目录（全部文件）</div>';
    return `<div class="proj-item ${p.id===state.project?'active':''}">
      <div class="nm">${esc(p.name)}
        <span style="font-weight:400;color:var(--muted)">${p.file_count} 个文件${p.missing_count?'（'+p.missing_count+' 个已失效）':''}</span></div>
      ${rootHtml}
      <div class="meta">最近扫描：${esc(p.last_scan_at?fmtDate(p.last_scan_at):'尚未扫描')}${p.note?' ｜ '+esc(p.note):''}</div>
      <div class="acts">
        <button class="btn-primary btn-sm" data-act="use" data-id="${p.id}">切换到此项目</button>
        <button class="btn-ghost btn-sm" data-act="scan" data-id="${p.id}">重新扫描</button>
        <button class="btn-ghost btn-sm" data-act="edit" data-id="${p.id}">改名 / 改目录</button>
        <button class="btn-ghost btn-sm" data-act="del" data-id="${p.id}">删除</button>
      </div>
    </div>`;
  }).join('');
  el.querySelectorAll('button[data-act]').forEach(b=>{
    b.onclick=()=>onProjAction(b.dataset.act, parseInt(b.dataset.id,10), b);
  });
}

async function onProjAction(act, id, btn){
  const p=state.projects.find(x=>x.id===id);
  if(act==='use'){
    switchProject(id);
    document.getElementById('projPanel').classList.remove('open');
    toast('已切换到项目：'+(p?p.name:''));
    return;
  }
  if(act==='edit'){ openProjEdit(p); return; }
  if(act==='scan'){
    if(!p||!(p.roots||[]).length){ toast('该项目未设置根目录，请先编辑'); return; }
    try{
      const r=await apiPost('/api/projects/'+id+'/scan');
      await pollProjectScan(r.scan_ids||[]);
    }catch(e){ toast('扫描失败: '+e.message); }
    return;
  }
  if(act==='relocate'){
    relocateRoot(id, btn ? btn.dataset.root : '');
    return;
  }
  if(act==='openroot'){
    const root=btn?btn.dataset.root:'';
    try{ await apiPost('/api/open',{path:root}); toast('已打开：'+root); }
    catch(e){ toast('打开失败: '+e.message); }
    return;
  }
  if(act==='copyroot'){
    const root=btn?btn.dataset.root:'';
    try{ await navigator.clipboard.writeText(root); toast('路径已复制：'+root); }
    catch(e){ prompt('复制这条路径：', root); }
    return;
  }
  if(act==='del'){
    if(!confirm('删除项目「'+(p?p.name:'')+'」？\n\n只删除这个视图，文件本身以及备注 / 状态 / 标签都会保留。')) return;
    try{
      await apiDelete('/api/projects/'+id);
      toast('项目已删除');
      if(state.project===id) switchProject(0);
      await loadProjects();
    }catch(e){ toast('删除失败: '+e.message); }
    return;
  }
}

// 项目根目录被改名 / 移动后，选新位置把索引整体迁过去（备注/状态/标签/识别结果全保留）
function relocateRoot(pid, oldRoot){
  if(!oldRoot){ toast('缺少原路径'); return; }
  openDirModal(async newRoot=>{
    if(!newRoot) return;
    if(newRoot===oldRoot){ toast('新位置与原位置相同'); return; }
    if(!confirm('把项目根目录\n'+oldRoot+'\n重新定位到\n'+newRoot+' ？\n\n该目录下所有文件的备注 / 状态 / 标签 / 识别结果都会跟着迁过去，不会丢失。')) return;
    try{
      const r=await apiPost('/api/projects/'+pid+'/relocate',{old:oldRoot,new:newRoot});
      toast('已重新定位，迁移了 '+r.moved+' 条记录');
      await loadProjects(); await loadTree(); await loadFiles(); await loadStats();
    }catch(e){ toast('重新定位失败: '+e.message); }
  });
}

async function pollProjectScan(sids){
  if(!sids.length){ toast('没有可扫描的目录'); return; }
  document.getElementById('scanOverlay').classList.add('show');
  document.getElementById('scanRoot').textContent='正在重新扫描项目…';
  const pending=new Set(sids);
  let total=0;
  while(pending.size){
    await new Promise(res=>setTimeout(res,500));
    for(const sid of Array.from(pending)){
      try{
        const r=await apiGet('/api/scan/'+sid);
        if(r.done){ pending.delete(sid); total+=r.processed||0; }
      }catch(e){ pending.delete(sid); }
    }
    document.getElementById('scanNum').textContent=total.toLocaleString();
  }
  document.getElementById('scanOverlay').classList.remove('show');
  toast('项目扫描完成，共 '+total.toLocaleString()+' 个文件');
  await loadProjects(); await loadStats(); await loadTree(); await loadTags(); await loadFiles();
}

// ---------- 项目编辑弹窗 ----------
let projEditingId=null;

function openProjEdit(p){
  projEditingId=p?p.id:null;
  document.getElementById('projEditTitle').textContent=p?('编辑项目：'+p.name):'新建项目';
  document.getElementById('projName').value=p?p.name:'';
  document.getElementById('projDepth').value=(p&&p.max_depth)?p.max_depth:'';
  document.getElementById('projNote').value=(p&&p.note)?p.note:'';
  const roots=(p&&p.roots&&p.roots.length)?p.roots:[];
  renderProjRoots(roots.length?roots:['']);
  document.getElementById('projModal').classList.add('show');
}

function renderProjRoots(roots){
  const el=document.getElementById('projRoots');
  el.innerHTML=roots.map((r,i)=>`<div class="root-row">
    <input data-root="${i}" value="${esc(r)}" placeholder="根目录路径，如 D:\\MyFiles" />
    <button class="btn-ghost btn-sm" data-pick="${i}">📂</button>
    <button class="btn-ghost btn-sm" data-rm="${i}">✕</button>
  </div>`).join('');
  el.querySelectorAll('[data-rm]').forEach(b=>b.onclick=()=>{
    const vals=projRootValues(); vals.splice(parseInt(b.dataset.rm,10),1);
    renderProjRoots(vals.length?vals:['']);
  });
  el.querySelectorAll('[data-pick]').forEach(b=>b.onclick=()=>{
    const i=parseInt(b.dataset.pick,10);
    openDirModal(p=>{ const v=projRootValues(); v[i]=p; renderProjRoots(v); });
  });
}

function projRootValues(){
  return Array.from(document.querySelectorAll('#projRoots input[data-root]'))
              .map(i=>i.value.trim()).filter(Boolean);
}

async function saveProj(){
  const name=document.getElementById('projName').value.trim();
  const roots=projRootValues();
  const md=document.getElementById('projDepth').value.trim();
  const note=document.getElementById('projNote').value.trim();
  if(!name){ toast('请填写项目名称'); return; }
  const created=!projEditingId;
  const oldP=created?null:state.projects.find(x=>x.id===projEditingId);
  const payload={name, roots, max_depth: md===''?null:(parseInt(md,10)||null), note};
  try{
    if(projEditingId) await apiPut('/api/projects/'+projEditingId, payload);
    else await apiPost('/api/projects', payload);
    document.getElementById('projModal').classList.remove('show');
    toast('项目已保存');
    await loadProjects();
    if(created){                               // 新建后自动切换并立即扫描
      const np=state.projects.find(p=>p.name===name);
      if(np){ switchProject(np.id); onProjAction('scan', np.id); }
    }else{                                     // 编辑时根目录有变化也自动重扫
      const np=state.projects.find(x=>x.id===projEditingId);
      const changed=JSON.stringify((oldP&&oldP.roots)||[])!==JSON.stringify((np&&np.roots)||[]);
      if(np && changed && np.roots.length){
        toast('根目录已变更，开始重新扫描');
        onProjAction('scan', np.id);
      }
    }
  }catch(e){ toast('保存失败: '+e.message); }
}
