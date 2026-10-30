// 任务清单
// ---------- 任务清单 ----------
async function loadTasks(){
  const r=await apiGet('/api/tasks');
  const el=document.getElementById('tasksList');
  if(!r.tasks.length){ el.innerHTML='<div class="empty">还没有搜索任务<br>每次搜索会自动记录到这里</div>'; return; }
  el.innerHTML=r.tasks.map(t=>{
    const flt=[]; if(t.project_id)flt.push('项目:'+projName(t.project_id));
    if(t.status_filter&&t.status_filter!=='all')flt.push('状态:'+t.status_filter); if(t.ext_filter)flt.push('类型:'+t.ext_filter); if(t.folder_filter)flt.push('目录:'+t.folder_filter); if(t.tag_filter)flt.push('标签:#'+t.tag_filter);
    return `<div class="task-item">
      <div class="q">${(t.query||'(空查询)')+(t.tag_filter?' #'+esc(t.tag_filter):'')}</div>
      <div class="meta">${flt.join(' · ')||'无附加过滤'} ｜ 结果 <b>${t.result_count}</b> ｜ ${fmtDate(t.last_run)}</div>
      <div class="acts">
        <button class="btn-primary btn-sm" data-act="apply" data-id="${t.id}">应用结果</button>
        <button class="btn-ghost btn-sm" data-act="retry" data-id="${t.id}">重试</button>
      </div>
    </div>`;
  }).join('');
  el.querySelectorAll('button[data-act]').forEach(b=>b.onclick=async()=>{
    try{ const r2=await apiPost('/api/tasks/'+b.dataset.id+'/retry'); applyTaskResults(r2);
      if(b.dataset.act==='retry'){ toast('已重试，结果 '+r2.task.result_count); loadTasks(); }
      else document.getElementById('tasksPanel').classList.remove('open');
    }catch(e){ toast('操作失败: '+e.message); }
  });
}
function applyTaskResults(r2){
  const tb=document.getElementById('fileRows');
  if(!r2.items.length){ tb.innerHTML='<tr><td colspan="7"><div class="empty">没有匹配的文件</div></td></tr>'; }
  else{
    tb.innerHTML=r2.items.map(it=>{
      const c=statusColor(it.status); const sel=state.selection.has(it.id)?'sel':'';
      const note=it.note?esc(it.note):'<span style="color:#cbd5e1">—</span>';
      const tags=tagChipsHtml(it.tags);
      return `<tr class="row ${sel}" data-id="${it.id}" data-path="${esc(it.path)}">
        <td><input type="checkbox" class="rowchk" ${state.selection.has(it.id)?'checked':''}></td>
        <td class="name-cell">${esc(it.name)}</td><td class="type-ic">${esc(it.ext||'')}</td>
        <td>${humanSize(it.size)}</td><td class="type-ic">${fmtDate(it.mtime)}</td>
        <td><span class="badge" style="background:${c}">${esc(it.status)}</span></td>
        <td class="note-snip">${note}${tags}</td></tr>`;
    }).join('');
    tb.querySelectorAll('tr.row').forEach(tr=>{
      tr.querySelector('.rowchk').onclick=e=>{ e.stopPropagation(); toggleSelect(parseInt(tr.dataset.id)); };
      tr.onclick=()=>selectFile(parseInt(tr.dataset.id), tr.dataset.path);
    });
  }
  document.getElementById('listTitle').textContent='任务结果';
  document.getElementById('pageInfo').textContent='结果 '+r2.items.length+' 条（最多显示500）';
}
