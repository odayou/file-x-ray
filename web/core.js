// 核心：token fetch 包装、全局状态、工具函数、API 封装
// 全局 fetch 包装：对所有 /api/ 请求自动附带本地访问令牌（后端启动后注入）
(function(){
  var T = window.__FIT_AUTH_TOKEN__ || "";
  var _fetch = window.fetch.bind(window);
  window.fetch = function(url, opts){
    opts = opts || {};
    if (typeof url === "string" && url.indexOf("/api/") === 0) {
      opts.headers = Object.assign({}, opts.headers, {"X-Fit-Token": T});
    }
    return _fetch(url, opts);
  };
})();

const state = { search:"", status:"all", ext:"", type:"", folder:"", folderReal:"", tag:"", page:1, pages:1, pageSize:100, sort:"name", showAll:false,
                selection:new Set(), currentId:null, statuses:[], whisper:null, ocr:null,
                project:0, projects:[], showMissing:false };
let treeData = [];

// ---------- 调试日志：累加记录所有 toast 提示，供“🐞 调试”面板查看/复制 ----------
const fitLogs = [];
function fmtLogTime(ts){ const d=new Date(ts), p=n=>String(n).padStart(2,'0');
  return p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds()); }
function renderDebugLog(){
  const dp=document.getElementById('debugLog'); if(!dp) return;
  dp.innerHTML = fitLogs.length
    ? fitLogs.map(l=>`<div class="log-item ${l.err?'log-err':''}"><span class="log-t">${fmtLogTime(l.t)}</span>${esc(l.msg)}</div>`).join('')
    : '<div class="empty">暂无日志</div>';
  dp.scrollTop = dp.scrollHeight;
}
function logMsg(msg, isErr){
  fitLogs.push({ t: Date.now(), msg, err: !!isErr });
  if (fitLogs.length > 500) fitLogs.splice(0, fitLogs.length - 500);   // 只保留最近 500 条
  const panel=document.getElementById('debugPanel');
  if (panel && panel.classList.contains('open')) renderDebugLog();
}

function toast(msg){ logMsg(msg); const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show'); clearTimeout(t._t); t._t=setTimeout(()=>t.classList.remove('show'),2200); }

// ---------- 现场记忆：localStorage 持久化会话状态，刷新页面自动恢复 ----------
const FIT_STATE_KEY='fit_state_v1';
function saveState(){
  try{ localStorage.setItem(FIT_STATE_KEY, JSON.stringify({
    project:state.project, folder:state.folder, folderReal:state.folderReal, tag:state.tag,
    search:state.search, status:state.status, ext:state.ext, type:state.type,
    showAll:state.showAll, showMissing:state.showMissing, page:state.page,
    currentId:state.currentId, selection:[...state.selection] })); }catch(e){}
}
function restoreState(){
  try{
    const s=JSON.parse(localStorage.getItem(FIT_STATE_KEY)||'null');
    if(!s) return false;
    Object.assign(state, {
      project:s.project||0, folder:s.folder||'', folderReal:s.folderReal||'', tag:s.tag||'',
      search:s.search||'', status:s.status||'all', ext:s.ext||'', type:s.type||'',
      showAll:!!s.showAll, showMissing:!!s.showMissing, page:s.page||1,
      currentId:s.currentId||null });
    state.selection=new Set(s.selection||[]);
    // 恢复输入控件（statusFilter 的选项是异步加载的，在其完成后单独回填）
    document.getElementById('searchInput').value=state.search;
    document.getElementById('typeFilter').value=state.type||'';
    if(state.type==='_custom'){
      const ef=document.getElementById('extFilter');
      ef.style.display=''; ef.value=state.ext;
    }
    const b=document.getElementById('missingBtn');
    b.textContent=state.showMissing?'👻 隐藏已失效':'👻 显示已失效';
    b.classList.toggle('btn-primary', state.showMissing);
    return true;
  }catch(e){ return false; }
}
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function humanSize(b){ if(b==null) return ''; const u=['B','KB','MB','GB','TB']; let i=0,n=b; while(n>=1024&&i<u.length-1){n/=1024;i++;} return (i?n.toFixed(1):n)+' '+u[i]; }
function fmtDate(ts){ if(!ts) return ''; return new Date(ts*1000).toLocaleString('zh-CN'); }
function statusColor(label){ const s=state.statuses.find(x=>x.label===label); return s?s.color:'#999999'; }

async function api(url, opts){ const r=await fetch(url, Object.assign({headers:{'Content-Type':'application/json'}}, opts)); if(!r.ok){ let m=''; try{m=(await r.json()).detail}catch(e){} throw new Error(m||('请求失败 '+r.status)); } return r.json(); }
const apiGet=u=>api(u); const apiPost=(u,b)=>api(u,{method:'POST',body:JSON.stringify(b||{})}); const apiPut=(u,b)=>api(u,{method:'PUT',body:JSON.stringify(b||{})});
const apiDelete=u=>api(u,{method:'DELETE'});
