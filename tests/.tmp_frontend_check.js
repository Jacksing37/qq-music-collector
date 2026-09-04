
const LS_KEY = "mwc_token";
let TOKEN = localStorage.getItem(LS_KEY) || "";
let ORIG = {}, DIRTY = {};
let CUR_WIN = null, OV = null, COLL = null, MASTER = null, ADD_CTX = null;
const MASTER_KEY = "__master__";
function getColl(wk){ return wk===MASTER_KEY ? MASTER : COLL; }
const $ = (s, r=document) => r.querySelector(s);
const csrf = {"Authorization": "Bearer " + TOKEN};

async function api(path, opts={}){
  opts.headers = Object.assign({}, (opts.headers||{}), csrf);
  const r = await fetch(path, opts);
  if (r.status === 401){ showToken(); throw new Error("unauthorized"); }
  return r;
}
function showToken(){ $("#tokenModal").classList.remove("hidden"); $("#tokenInput").focus(); }
function esc(t){ return (t==null?"":String(t)).replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

/* ---- 主题 ---- */
const savedTheme = localStorage.getItem("mwc_theme");
if (savedTheme) document.documentElement.setAttribute("data-theme", savedTheme);
$("#themeBtn").onclick = () => {
  const next = document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("mwc_theme", next);
};
$("#logoutBtn").onclick = () => { localStorage.removeItem(LS_KEY); TOKEN=""; csrf.Authorization="Bearer "; showToken(); };
$("#tokenOk").onclick = () => {
  const v = $("#tokenInput").value.trim(); if(!v) return;
  TOKEN=v; localStorage.setItem(LS_KEY,v); csrf.Authorization="Bearer "+v;
  $("#tokenModal").classList.add("hidden"); loadAll(); loadCollect();
};
$("#tokenInput").addEventListener("keydown", e=>{ if(e.key==="Enter") $("#tokenOk").click(); });

/* ---- 侧边栏切换 ---- */
function switchPage(name){
  document.querySelectorAll(".nav").forEach(b=>b.classList.toggle("active", b.dataset.page===name));
  document.querySelectorAll(".page").forEach(s=>s.classList.add("hidden"));
  $("#page-"+name).classList.remove("hidden");
  $("#footbar").classList.toggle("hidden", name!=="config");
  if(name==="overview") loadOverview();
  if(name==="collect") loadCollect();
  if(name==="master") loadMaster();
  if(name==="config") loadConfig();
  if(name==="aliases") loadAliases();
  if(name==="admin") loadAdmin();
  if(name==="account") loadAccount();
}
document.querySelectorAll(".nav").forEach(b=> b.onclick=()=>switchPage(b.dataset.page));

/* ---- 概览 ---- */
async function loadStatus(){
  try{
    const s = await (await api("/api/music-admin/status")).json();
    $("#ovWindow").textContent = s.window_label || "—";
    $("#ovCollect").textContent = s.collecting ? "收集中" : "未在收集期";
    $("#ovCollect").style.color = s.collecting ? "var(--ok)" : "var(--muted)";
    $("#ovOverride").textContent = s.collect_override || "—";
    $("#ovNetease").textContent = (OV&&OV.netease_logged_in)?"已登录 ✓":"未登录";
    $("#statusPill").textContent = s.collecting ? "● 收集中" : "○ 空闲";
    $("#statusPill").className = "status-pill" + (s.collecting? " on":"");
    $("#ovRuns").textContent = s.next_runs || "";
  }catch(e){ if(e.message!=="unauthorized") console.warn("status 加载失败", e); }
}
function fillWinSel(sel, selected){
  sel.innerHTML="";
  (OV?OV.windows:[]).forEach(w=>{
    const op=document.createElement("option"); op.value=w.key; op.textContent=`${w.key} (${w.count}首)`; sel.appendChild(op);
  });
  if(OV && OV.windows.length){ sel.value = selected || OV.selected_window || OV.windows[0].key; }
}
async function loadOverview(){
  try{
    const url = "/api/music-admin/overview" + (CUR_WIN?("?window_key="+encodeURIComponent(CUR_WIN)):"");
    OV = await (await api(url)).json();
    CUR_WIN = OV.selected_window || (OV.windows[0]&&OV.windows[0].key) || null;
    fillWinSel($("#winSel"), CUR_WIN);
    fillWinSel($("#cWinSel"), CUR_WIN);
    const nb=$("#neteaseBadge");
    if(OV.netease_logged_in){ nb.textContent="网易云：已登录 ✓"; nb.className="badge ok"; }
    else { nb.textContent="网易云：未登录 ✗"; nb.className="badge bad"; }
    loadStatus();
    const gl=$("#ovGroups"); gl.innerHTML="";
    (OV.groups||[]).forEach(g=>{
      const d=document.createElement("div"); d.className="gcard";
      d.innerHTML=`<div class="gtitle">群 ${g.group_id}<span class="cnt">${g.count} 首</span>
        <button class="btn-primary" style="float:right;padding:4px 10px" data-g="${g.group_id}">去管理 →</button></div>`;
      gl.appendChild(d);
    });
    gl.querySelectorAll("button[data-g]").forEach(b=> b.onclick=()=>{ switchPage("collect"); });
  }catch(e){ if(e.message!=="unauthorized") console.warn("overview 加载失败", e); }
}
$("#winSel").onchange = e=>{ CUR_WIN=e.target.value; loadOverview(); };
$("#cWinSel").onchange = e=>{ CUR_WIN=e.target.value; loadCollect(); };
$("#opStart").onclick=()=>doAction({action:"start"});
$("#opStop").onclick=()=>doAction({action:"stop"});
$("#opAuto").onclick=()=>doAction({action:"auto"});
$("#opArchiveAll").onclick=()=>doAction({action:"archive_all"});

/* ---- 实时操作 ---- */
function flashOp(msg, kind=""){
  const m=$("#saveMsg"); if(m){ m.textContent=msg; m.className="msg"+(kind?(" "+kind):""); }
  if(msg) toast(msg, kind);   // 同时弹常驻 toast，避免提示被隐藏的 footbar 吞掉
}
function toast(msg, kind=""){
  const t=$("#toast"); if(!t || !msg) return;
  clearTimeout(t._timer);
  t.textContent=msg; t.className="toast show"+(kind?(" "+kind):"");
  t._timer=setTimeout(()=> t.classList.remove("show"), 3600);
}
function setBusy(on){
  const b=$("#busy"); if(b) b.classList.toggle("hidden", !on);
  document.querySelectorAll("button").forEach(x=>{
    if(on){ if(!x.disabled){ x.dataset._b="1"; x.disabled=true; } }
    else if(x.dataset._b){ delete x.dataset._b; x.disabled=false; }
  });
}
function fmtDate(ts){
  if(ts==null || ts===0) return "—";
  const d = new Date(ts*1000);
  if(isNaN(d.getTime())) return "—";
  const p = n => String(n).padStart(2,"0");
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
async function doAction(body){
  setBusy(true);
  try{
    const r = await api("/api/music-admin/action", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    const j = await r.json();
    if(body.action==="preview" && j.ok && j.data){ openPreview(j.data); return; }
    flashOp(j.message || (j.ok?"操作成功":"操作失败"), j.ok?"ok":"bad");
    if(j.ok){
      const cur = (document.querySelector(".nav.active")||{}).dataset?.page;
      if(cur==="master"){ loadMaster(); }
      else { if(CUR_WIN) loadOverview(); loadCollect(); loadStatus(); }
    }
    return j;
  }catch(e){ flashOp("操作失败: "+e.message,"bad"); }
  finally{ setBusy(false); }
}

/* ---- 收集管理 ---- */
async function loadCollect(){
  try{
    const url = "/api/music-admin/overview" + (CUR_WIN?("?window_key="+encodeURIComponent(CUR_WIN)):"");
    COLL = await (await api(url)).json();
    CUR_WIN = COLL.selected_window || (COLL.windows[0]&&COLL.windows[0].key) || null;
    fillWinSel($("#cWinSel"), CUR_WIN);
    const wrap=$("#collectGroups"); wrap.innerHTML="";
    const groups = COLL.groups||[];
    if(!groups.length){ wrap.innerHTML=`<div class="empty">该窗口下暂无收集记录。</div>`; return; }
    groups.forEach(g=> wrap.appendChild(renderGroupCard(g, COLL.selected_window)) );
  }catch(e){ if(e.message!=="unauthorized") console.warn("collect 加载失败", e); }
}
async function loadMaster(){
  try{
    const url = "/api/music-admin/overview?scope=master";
    MASTER = await (await api(url)).json();
    const wrap=$("#masterGroups"); wrap.innerHTML="";
    const groups = MASTER.groups||[];
    if(!groups.length){ wrap.innerHTML=`<div class="empty">总库还是空的。分享歌曲（启用总库后）或点上方「📥 汇总现有窗口到总库」即可填充。</div>`; return; }
    groups.forEach(g=> wrap.appendChild(renderGroupCard(g, MASTER_KEY)) );
  }catch(e){ if(e.message!=="unauthorized") console.warn("master 加载失败", e); }
}
function renderGroupCard(g, wk){
  const card=document.createElement("div"); card.className="gcard"; card.dataset.wk=wk||""; card.dataset.gid=g.group_id;
  const pl = g.playlist_url
    ? `<a class="plink" href="${esc(g.playlist_url)}" target="_blank" rel="noreferrer">🔗 网易云歌单</a>`
    : `<span class="muted">（本窗口尚未建歌单，归档或同步后在此显示）</span>`;
  const ops=`<div class="row" style="margin-bottom:6px">
    <button data-act="preview" data-g="${g.group_id}">👁 预览</button>
    <button data-act="archive" data-g="${g.group_id}">📦 归档本群</button>
    <button class="btn-primary" data-act="sync" data-g="${g.group_id}">🔄 同步到歌单</button>
    <button data-act="del" data-g="${g.group_id}" class="btn-danger">删除选中</button>
    <button data-act="clear" data-g="${g.group_id}" class="btn-danger">清空本窗口</button>
  </div>
  <div class="row plrow"><span class="plabel">网易云歌单：</span>${pl}</div>`;
  let rows="";
  if(!g.songs.length){ rows=`<tr><td colspan="7" class="empty">本群该窗口暂无歌曲</td></tr>`; }
  else {
    g.songs.forEach((s,i)=>{
      const mt = s.matched?`<span class="mt">✓</span>`:`<span class="un">·</span>`;
      rows+=`<tr class="songrow" draggable="true" data-g="${g.group_id}" data-idx="${s.index}">
        <td><input type="checkbox" class="songchk" data-g="${g.group_id}" data-idx="${s.index}"></td>
        <td class="idx">${s.index}</td>
        <td><b>${esc(s.title)}</b><br><span style="color:var(--muted);font-size:12px">${esc(s.artists||"")}</span></td>
        <td>${esc(s.sharer_name||"")}</td>
        <td class="plat">${esc(s.platform_name||s.platform)}</td>
        <td class="date">${fmtDate(s.created_at)}</td>
        <td>${mt}</td>
        <td class="acts">
          <button title="上移" data-mv="-1" data-g="${g.group_id}" data-idx="${s.index}">↑</button>
          <button title="下移" data-mv="1" data-g="${g.group_id}" data-idx="${s.index}">↓</button>
          <button title="编辑" data-edit data-g="${g.group_id}" data-idx="${s.index}">✎</button>
          <button title="匹配" data-match data-g="${g.group_id}" data-idx="${s.index}">🔗</button>
        </td></tr>`;
    });
  }
  const tbl=`<table class="gtbl"><thead><tr>
    <th></th><th>#</th><th>歌曲 / 歌手（可拖拽行排序）</th><th>分享者</th><th>平台</th><th>收录日期</th><th>匹配</th><th></th>
  </tr></thead><tbody>${rows}</tbody></table>`;
  card.innerHTML=`<div class="gtitle">群 ${g.group_id}<span class="cnt">${g.count} 首</span></div>${ops}${tbl}`;
  card.querySelectorAll("button[data-act]").forEach(b=> b.onclick=()=>groupAction(b.dataset.act,b.dataset.g, wk));
  card.querySelectorAll("button[data-mv]").forEach(b=> b.onclick=()=>moveRow(g.group_id, parseInt(b.dataset.idx,10), parseInt(b.dataset.mv,10), wk));
  card.querySelectorAll("button[data-edit]").forEach(b=> b.onclick=()=>openEdit(g.group_id, parseInt(b.dataset.idx,10), wk));
  card.querySelectorAll("button[data-match]").forEach(b=> b.onclick=()=>openMatch(g.group_id, parseInt(b.dataset.idx,10), wk));
  // 拖拽排序
  card.querySelectorAll("tr.songrow").forEach(tr=>{
    tr.addEventListener("dragstart", e=>{ DRAG_IDX=parseInt(tr.dataset.idx,10); tr.classList.add("dragging"); if(e.dataTransfer){ e.dataTransfer.effectAllowed="move"; } });
    tr.addEventListener("dragend", ()=>{ DRAG_IDX=null; card.querySelectorAll("tr.songrow").forEach(x=>x.classList.remove("dragging","droptgt")); });
    tr.addEventListener("dragover", e=>{ if(DRAG_IDX===null) return; e.preventDefault(); tr.classList.add("droptgt"); });
    tr.addEventListener("dragleave", ()=> tr.classList.remove("droptgt"));
    tr.addEventListener("drop", e=>{
      e.preventDefault(); tr.classList.remove("droptgt");
      if(DRAG_IDX===null) return;
      const g2=(getColl(wk).groups||[]).find(x=>x.group_id===g.group_id); if(!g2) return;
      const arr=g2.songs.slice();
      const from=arr.findIndex(s=>s.index===DRAG_IDX); if(from<0) return;
      const toIdx=parseInt(tr.dataset.idx,10);
      let to=arr.findIndex(s=>s.index===toIdx); if(to<0) return;
      const r=tr.getBoundingClientRect(); const after=(e.clientY-r.top)>r.height/2; if(after) to+=1;
      const [m]=arr.splice(from,1); arr.splice(to,0,m);
      reorderGroup(g.group_id, arr, wk);
    });
  });
  return card;
}
async function groupAction(act, gid, wk){
  gid=parseInt(gid,10);
  const ACT_MAP={del:"delete", pname:"preview_name", pdesc:"preview_desc"};
  act=ACT_MAP[act]||act;
  wk = wk || (COLL&&COLL.selected_window)||"";
  let body={action:act, group_id:gid, window_key:wk};
  if(act==="del"){
    const card=document.querySelector(`.gcard[data-wk="${wk}"][data-gid="${gid}"]`);
    const checked=card?card.querySelectorAll(`.songchk[data-g="${gid}"]:checked`):[];
    const indices=Array.from(checked).map(c=>parseInt(c.dataset.idx,10));
    if(!indices.length){ flashOp("请先勾选要删除的歌曲"); return; }
    body.indices=indices;
  }
  await doAction(body);
}
let DRAG_IDX=null;
async function reorderGroup(gid, arr, wk){
  const ordered=arr.map(s=>s.index);
  wk = wk || (COLL&&COLL.selected_window)||"";
  await doAction({action:"reorder", group_id:gid, window_key:wk, ordered_indices:ordered});
}
async function moveRow(gid, idx, dir, wk){
  const g=(getColl(wk).groups||[]).find(x=>x.group_id===gid); if(!g) return;
  const i=g.songs.findIndex(s=>s.index===idx); if(i<0) return;
  const j=i+dir; if(j<0||j>=g.songs.length) return;
  const arr=g.songs.slice(); [arr[i],arr[j]]=[arr[j],arr[i]];
  await reorderGroup(gid, arr, wk);
}
$("#cArchiveBtn").onclick=()=>doAction({action:"archive_all"});
$("#cSyncAllBtn").onclick=async()=>{
  const gids=(COLL&&COLL.groups||[]).map(g=>g.group_id);
  if(!gids.length){ flashOp("当前窗口无群可同步"); return; }
  for(const gid of gids){ await doAction({action:"sync", group_id:gid}); }
};
$("#cAddBtn").onclick=()=>{ ADD_CTX={window_key:(COLL&&COLL.selected_window)||"", group_id:(COLL&&COLL.groups[0]?COLL.groups[0].group_id:0)}; $("#addModal").classList.remove("hidden"); };

/* 总库页头部操作 */
$("#mAggBtn").onclick=async()=>{
  const gids=(OV&&OV.groups||[]).map(g=>g.group_id);
  if(!gids.length){ flashOp("没有任何已收集的群可汇总"); return; }
  for(const gid of gids){ await doAction({action:"master_aggregate", group_id:gid}); }
};
$("#mArchiveBtn").onclick=async()=>{
  const gids=(MASTER&&MASTER.groups||[]).map(g=>g.group_id);
  if(!gids.length){ flashOp("总库暂无歌曲可归档"); return; }
  for(const gid of gids){ await doAction({action:"archive", group_id:gid, window_key:MASTER_KEY}); }
};
$("#mSyncBtn").onclick=async()=>{
  const gids=(MASTER&&MASTER.groups||[]).map(g=>g.group_id);
  if(!gids.length){ flashOp("总库暂无歌曲可同步"); return; }
  for(const gid of gids){ await doAction({action:"sync", group_id:gid, window_key:MASTER_KEY}); }
};
$("#mAddBtn").onclick=()=>{
  const g=(MASTER&&MASTER.groups||[])[0];
  ADD_CTX={window_key:MASTER_KEY, group_id:g?g.group_id:0};
  $("#addModal").classList.remove("hidden");
};

/* ---- 编辑 / 匹配 / 添加 弹窗 ---- */
let EDIT_CTX=null, MATCH_CTX=null;
function openEdit(gid, idx, wk){
  const g=(getColl(wk).groups||[]).find(x=>x.group_id===gid); if(!g) return;
  const s=g.songs.find(x=>x.index===idx); if(!s) return;
  const neteaseLink = s.netease_id ? `https://music.163.com/song?id=${s.netease_id}` : "";
  EDIT_CTX={gid, idx, wk, netease_orig: neteaseLink};
  $("#edTitle").value=s.title||""; $("#edArtists").value=s.artists||""; $("#edSharer").value=s.sharer_name||"";
  $("#edSharerId").value=""; $("#edUrl").value=s.url||""; $("#edNetease").value=neteaseLink;
  $("#editModal").classList.remove("hidden");
}
$("#edCancel").onclick=()=>$("#editModal").classList.add("hidden");
$("#edSave").onclick=async()=>{
  if(!EDIT_CTX) return;
  const fields={title:$("#edTitle").value.trim(), artists:$("#edArtists").value.trim(),
    sharer_name:$("#edSharer").value.trim(), url:$("#edUrl").value.trim()};
  const sid=$("#edSharerId").value.trim(); if(sid) fields.sharer_id=parseInt(sid,10);
  // 仅当匹配链接被修改时才发送，避免误触发重新匹配
  const nl=$("#edNetease").value.trim();
  if(nl && nl!==EDIT_CTX.netease_orig) fields.netease_link=nl;
  const wk=EDIT_CTX.wk||"";
  await doAction({action:"edit_song", group_id:EDIT_CTX.gid, window_key:wk, index:EDIT_CTX.idx, fields});
  $("#editModal").classList.add("hidden");
};
function openMatch(gid, idx, wk){ MATCH_CTX={gid, idx, wk}; $("#mtLink").value=""; $("#matchModal").classList.remove("hidden"); }
$("#mtCancel").onclick=()=>$("#matchModal").classList.add("hidden");
$("#mtSave").onclick=async()=>{
  if(!MATCH_CTX) return;
  const link=$("#mtLink").value.trim();
  const wk=MATCH_CTX.wk||"";
  await doAction({action:"match", group_id:MATCH_CTX.gid, window_key:wk, index:MATCH_CTX.idx, link});
  $("#matchModal").classList.add("hidden");
};
$("#adCancel").onclick=()=>$("#addModal").classList.add("hidden");
$("#adSave").onclick=async()=>{
  if(!ADD_CTX) return;
  const song={platform:$("#adPlatform").value, song_id:$("#adSongId").value.trim(),
    title:$("#adTitle").value.trim(), artists:$("#adArtists").value.trim(),
    sharer_name:$("#adSharer").value.trim(), sharer_id:parseInt($("#adSharerId").value||"0",10)};
  await doAction({action:"add_song", group_id:ADD_CTX.group_id, window_key:ADD_CTX.window_key, song});
  $("#addModal").classList.add("hidden");
};

/* ---- 预览抽屉 ---- */
function openPreview(d){
  $("#pvWin").textContent="· "+(d.window_label||d.window_key||"");
  const songs=d.songs||[];
  let rows="";
  if(!songs.length){ rows=`<tr><td colspan="5" class="empty">该窗口暂无歌曲</td></tr>`; }
  else songs.forEach(s=>{
    const mt=s.matched?`<span class="mt">✓</span>`:`<span class="un">·</span>`;
    rows+=`<tr><td class="idx">${s.index}</td>
      <td><b>${esc(s.title)}</b><br><span style="color:var(--muted);font-size:12px">${esc(s.artists||"")}</span></td>
      <td>${esc(s.sharer_name||"")}</td><td class="plat">${esc(s.platform_name||s.platform)}</td><td class="date">${fmtDate(s.created_at)}</td><td>${mt}</td></tr>`;
  });
  $("#pvBody").innerHTML=`
    <div class="pv-sec"><div class="pv-tag">歌单名</div><div class="pv-name">${esc(d.name||"(未生成)")}</div></div>
    <div class="pv-sec"><div class="pv-tag">简介</div>
      <pre class="pv-desc${(d.description?"":" empty")}">${esc(d.description||"（简介为空）")}</pre>
      <div class="row" style="margin-top:8px"><button id="pvCopyDesc">📋 复制简介</button></div></div>
    <div class="pv-sec"><div class="pv-tag">歌曲清单（${songs.length} 首）</div>
      <table class="gtbl"><thead><tr><th>#</th><th>歌曲/歌手</th><th>分享者</th><th>平台</th><th>收录日期</th><th>匹配</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  const cb=$("#pvCopyDesc"); if(cb) cb.onclick=()=>navigator.clipboard.writeText(d.description||"").then(()=>flashOp("简介已复制","ok"),()=>flashOp("复制失败","bad"));
  $("#pvDrawer").classList.add("open"); $("#pvDrawer").setAttribute("aria-hidden","false"); $("#pvMask").classList.remove("hidden");
}
function closePreview(){ $("#pvDrawer").classList.remove("open"); $("#pvDrawer").setAttribute("aria-hidden","true"); $("#pvMask").classList.add("hidden"); }
$("#pvClose").onclick=closePreview; $("#pvMask").onclick=closePreview;
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closePreview(); });

/* ---- 配置表单 ---- */
function fieldControl(f, value){
  const wrap=document.createElement("div"); wrap.className="fctrl";
  if(f.type==="bool"){
    const id="f_"+f.key.replace(/\./g,"_"); const lbl=document.createElement("label"); lbl.className="chk";
    const cb=document.createElement("input"); cb.type="checkbox"; cb.id=id; cb.checked=!!value;
    cb.onchange=()=>markDirty(f.key, cb.checked);
    const sp=document.createElement("span"); sp.textContent=f.label; lbl.appendChild(cb); lbl.appendChild(sp); wrap.appendChild(lbl);
  } else if(f.type==="enum"){
    const sel=document.createElement("select");
    (f.enum||[]).forEach(o=>{const op=document.createElement("option");op.value=o;op.textContent=o;sel.appendChild(op);});
    sel.value=value??""; sel.onchange=()=>markDirty(f.key,sel.value); wrap.appendChild(sel);
  } else if(f.type==="int"||f.type==="float"){
    const inp=document.createElement("input"); inp.type="number"; inp.value=value??"";
    inp.step = f.type==="int"?"1":"any"; inp.oninput=()=>markDirty(f.key,inp.value); wrap.appendChild(inp);
  } else if(f.type==="intlist"||f.type==="strlist"){
    const inp=document.createElement("input"); inp.type="text";
    inp.value=Array.isArray(value)?value.join(", "):(value??""); inp.placeholder="逗号分隔";
    inp.oninput=()=>markDirty(f.key,inp.value); wrap.appendChild(inp);
  } else {
    if(f.multiline){ const ta=document.createElement("textarea"); ta.value=value??""; ta.oninput=()=>markDirty(f.key,ta.value); wrap.appendChild(ta); }
    else { const inp=document.createElement("input"); inp.type="text"; inp.value=value??""; inp.oninput=()=>markDirty(f.key,inp.value); wrap.appendChild(inp); }
  }
  return wrap;
}
function markDirty(key,val){
  const orig=ORIG[key]; let same=(orig===val);
  if(!same && typeof orig==="boolean") same=(String(orig)===String(val));
  if(same){ delete DIRTY[key]; } else { DIRTY[key]=val; }
  refreshDirty();
}
function refreshDirty(){
  const n=Object.keys(DIRTY).length;
  $("#dirtyCount").textContent=n?`● ${n} 项待保存`:"";
  $("#saveBtn").disabled=n===0;
  document.querySelectorAll("#configForm .field").forEach(fr=>{
    const k=fr.dataset.key; const ctrl=fr.querySelector(".fctrl");
    if(ctrl) ctrl.classList.toggle("dirty", !!DIRTY[k]);
  });
}
function renderForm(schema, values){
  ORIG=Object.assign({},values); DIRTY={};
  const form=$("#configForm"); form.innerHTML="";
  schema.forEach(sec=>{
    const card=document.createElement("section"); card.className="card";
    const h=document.createElement("h2"); h.innerHTML=`<span class="dot"></span>${sec.title}`; card.appendChild(h);
    sec.fields.forEach(f=>{
      if(f.type==="map") return;
      const fr=document.createElement("div"); fr.className="field"; fr.dataset.key=f.key;
      const lab=document.createElement("div"); lab.className="flabel";
      lab.innerHTML=`${f.label}${f.hint?`<span class="hint">${f.hint}</span>`:""}`;
      const ctrl=fieldControl(f, values[f.key]);
      fr.appendChild(lab); fr.appendChild(ctrl); card.appendChild(fr);
    });
    form.appendChild(card);
  });
  refreshDirty();
}
async function loadConfig(){
  try{
    const [c,s]=await Promise.all([api("/api/music-admin/config"), api("/api/music-admin/status")]);
    const cj=await c.json(); renderForm(cj.schema, cj.values);
    const sj=await s.json();
    $("#statusPill").textContent = sj.collecting?"● 收集中":"○ 空闲";
    $("#statusPill").className="status-pill"+(sj.collecting?" on":"");
    flashOp("");
  }catch(e){ if(e.message!=="unauthorized") flashOp("加载失败: "+e.message,"bad"); }
}
async function saveConfig(){
  setMsg("保存中…");
  try{
    const r=await api("/api/music-admin/config",{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({values:DIRTY})});
    const j=await r.json();
    if(!j.ok){ const msgs=Object.entries(j.errors||{}).map(([k,v])=>`${k}: ${v}`).join("；"); setMsg("保存失败 — "+msgs,"bad"); return; }
    setMsg("已保存 ✓","ok"); await loadConfig();
  }catch(e){ setMsg("保存失败: "+e.message,"bad"); }
}
function setMsg(t,kind=""){ const m=$("#saveMsg"); m.textContent=t; m.className="msg"+(kind?(" "+kind):""); }
$("#saveBtn").onclick=saveConfig;
$("#resetBtn").onclick=()=>{ DIRTY={}; document.querySelectorAll(".fctrl.dirty").forEach(c=>c.classList.remove("dirty")); refreshDirty(); setMsg("已重置本地改动"); };

/* ---- 昵称映射 ---- */
function dictToLines(m){ return Object.keys(m||{}).map(k=>k+"="+m[k]).join("\n"); }
function linesToDict(text){
  const out={}; (text||"").split(/\r?\n/).forEach(line=>{ line=line.trim(); if(!line||line.startsWith("#"))return;
    const i=line.indexOf("="); if(i<0)return; const k=line.slice(0,i).trim(), v=line.slice(i+1).trim(); if(k) out[k]=v; }); return out;
}
function renderAliasPreview(m){
  const ul=$("#aliasPreview"); const keys=Object.keys(m||{});
  if(!keys.length){ ul.innerHTML=`<li class="empty">（暂无映射）</li>`; return; }
  ul.innerHTML=keys.map(k=>`<li><span style="font-weight:600">${esc(k)}</span> → <span style="color:var(--accent);font-weight:700">${esc(m[k])}</span></li>`).join("");
}
async function loadAliases(){
  try{
    const cj=await (await api("/api/music-admin/config")).json();
    const m=(cj.values&&cj.values["playlist.sharer_aliases"])||{};
    $("#aliasInput").value=dictToLines(m); renderAliasPreview(m);
  }catch(e){ if(e.message!=="unauthorized") console.warn("aliases 加载失败",e); }
}
$("#aliasInput").addEventListener("input",()=>renderAliasPreview(linesToDict($("#aliasInput").value)));
// 昵称映射随配置一起保存（复用 /config PATCH）
$("#page-aliases").addEventListener("focusout", async ()=>{
  const m=linesToDict($("#aliasInput").value);
  try{ await api("/api/music-admin/config",{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({values:{"playlist.sharer_aliases":m}})}); }
  catch(e){ if(e.message!=="unauthorized") console.warn("aliases 保存失败",e); }
}, true);

/* ---- 管理员 ---- */
async function loadAdmin(){
  try{ const j=await (await api("/api/music-admin/admin")).json();
    $("#suInput").value=(j.superusers||[]).join("\n"); $("#suNote").textContent=j.note||""; }
  catch(e){ if(e.message!=="unauthorized") console.warn("admin 加载失败",e); }
}
$("#suSave").onclick=async()=>{
  const ids=$("#suInput").value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
  try{ const j=await (await api("/api/music-admin/admin",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({superusers:ids})})).json();
    if(!j.ok){ $("#suNote").textContent=j.message; return; }
    $("#suNote").textContent=j.note||"已保存（需重启 bot 生效）";
  }catch(e){ $("#suNote").textContent="保存失败: "+e.message; }
};

/* ---- 网易云账号 ---- */
async function loadAccount(){
  try{ const j=await (await api("/api/music-admin/account")).json(); renderAccount(j); }
  catch(e){ if(e.message!=="unauthorized") console.warn("account 加载失败",e); }
}
function renderAccount(j){
  const box=$("#accStatus");
  if(j.valid){ box.innerHTML=`<span class="badge ok">已登录</span> 昵称：<b>${esc(j.nickname||"")}</b>　userId：${esc(j.userId||"")}`; $("#accLogin").classList.add("hidden"); }
  else if(j.logged_in){ box.innerHTML=`<span class="badge bad">凭证存在但已失效</span> 请重新登录。`; $("#accLogin").classList.remove("hidden"); }
  else { box.innerHTML=`<span class="badge bad">未登录</span> 请粘贴 MUSIC_U 登录。`; $("#accLogin").classList.remove("hidden"); }
}
$("#accLoginBtn").onclick=async()=>{
  const cookie=$("#accCookie").value.trim(); if(!cookie){ return; }
  try{ const j=await (await api("/api/music-admin/account",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"login",cookie})})).json();
    if(!j.ok){ $("#accStatus").textContent=j.message; return; } renderAccount(j); loadStatus(); }
  catch(e){ $("#accStatus").textContent="登录失败: "+e.message; }
};
$("#accLogout").onclick=async()=>{
  try{ const j=await (await api("/api/music-admin/account",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"logout"})})).json();
    if(j.ok){ renderAccount(j); loadStatus(); } } catch(e){}
};

/* ---- 启动 ---- */
loadOverview(); loadCollect();

;

const LS_KEY = "mwc_token";
let TOKEN = localStorage.getItem(LS_KEY) || "";
const $ = (s, r=document) => r.querySelector(s);
const csrf = {"Authorization": "Bearer " + TOKEN};

async function api(path, opts={}){
  opts.headers = Object.assign({}, (opts.headers||{}), csrf);
  const r = await fetch(path, opts);
  if (r.status === 401){ showToken(); throw new Error("unauthorized"); }
  return r;
}
function showToken(){ $("#tokenModal").classList.remove("hidden"); $("#tokenInput").focus(); }
function esc(t){ return (t==null?"":String(t)).replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

function dictToLines(m){
  return Object.keys(m||{}).map(k => k + "=" + m[k]).join("\n");
}
function linesToDict(text){
  const out = {};
  (text||"").split(/\r?\n/).forEach(line=>{
    line = line.trim();
    if (!line || line.startsWith("#")) return;
    const i = line.indexOf("=");
    if (i < 0) return;
    const k = line.slice(0, i).trim(), v = line.slice(i+1).trim();
    if (k) out[k] = v;
  });
  return out;
}
function renderPreview(m){
  const ul = $("#previewList");
  const keys = Object.keys(m||{});
  if (!keys.length){ ul.innerHTML = `<li class="empty">（暂无映射）</li>`; return; }
  ul.innerHTML = keys.map(k =>
    `<li><span class="from">${esc(k)}</span><span class="arrow">→</span><span class="to">${esc(m[k])}</span></li>`
  ).join("");
}

function setMsg(t, kind=""){ const m=$("#saveMsg"); m.textContent=t; m.className="msg"+(kind?(" "+kind):""); }

async function load(){
  try{
    const r = await api("/api/music-admin/config");
    const cj = await r.json();
    const m = (cj.values && cj.values["playlist.sharer_aliases"]) || {};
    $("#aliasInput").value = dictToLines(m);
    renderPreview(m);
    setMsg("");
  }catch(e){ if (e.message!=="unauthorized") setMsg("加载失败: "+e.message, "bad"); }
}

async function save(){
  const m = linesToDict($("#aliasInput").value);
  setMsg("保存中…");
  try{
    const r = await api("/api/music-admin/config", {method:"PATCH",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify({values:{"playlist.sharer_aliases": m}})});
    const j = await r.json();
    if (!j.ok){
      const msgs = Object.entries(j.errors||{}).map(([k,v])=>`${k}: ${v}`).join("；");
      setMsg("保存失败 — "+msgs, "bad");
      return;
    }
    setMsg("已保存 ✓ 共 "+Object.keys(m).length+" 条映射", "ok");
    renderPreview(m);
  }catch(e){ setMsg("保存失败: "+e.message, "bad"); }
}

const savedTheme = localStorage.getItem("mwc_theme");
if (savedTheme) document.documentElement.setAttribute("data-theme", savedTheme);
$("#themeBtn").onclick = () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur==="dark"?"light":"dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("mwc_theme", next);
};
$("#aliasInput").addEventListener("input", () => renderPreview(linesToDict($("#aliasInput").value)));
$("#saveBtn").onclick = save;
$("#tokenOk").onclick = () => {
  const v = $("#tokenInput").value.trim();
  if (!v) return;
  TOKEN = v; localStorage.setItem(LS_KEY, v); csrf.Authorization = "Bearer "+v;
  $("#tokenModal").classList.add("hidden");
  load();
};
$("#tokenInput").addEventListener("keydown", e=>{ if(e.key==="Enter") $("#tokenOk").click(); });

load();
