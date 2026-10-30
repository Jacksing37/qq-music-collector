"""猜歌游戏的页面（玩家侧 + 名单管理页）。

独立成文件而不是塞进 ``webui_frontend.py``：那边 2700 行是管理面板，再加两千行
只会让人不敢动。配色沿用管理面板那套 CSS 变量，风格上看不出是两个人写的。

**前端拿不到答案**：服务端快照里根本没有 sharer 字段（见 ``rooms.py`` 的
``Card.public``），所以这里也不需要做任何"隐藏"处理。
"""

GAME_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>猜歌 · 猜猜这首歌是谁分享的</title>
<style>
:root{
  --bg:#0b0f1a; --bg2:#111726; --card:rgba(255,255,255,.04); --card-bd:rgba(255,255,255,.08);
  --txt:#e8edf6; --muted:#8b97ad; --accent:#6ea8fe; --accent2:#a78bfa; --ok:#34d399; --bad:#f87171;
  --warn:#fbbf24; --input:rgba(255,255,255,.06); --shadow:0 10px 30px rgba(0,0,0,.35);
  color-scheme:dark;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--txt);line-height:1.6;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  min-height:100vh;padding:16px 14px 40px}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:20px;margin:8px 0 4px;font-weight:600}
h2{font-size:16px;margin:0 0 10px;font-weight:600}
p.sub{color:var(--muted);font-size:13px;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--card-bd);border-radius:14px;padding:16px;margin-bottom:14px}
button{font:inherit;cursor:pointer;border:1px solid var(--card-bd);background:var(--input);color:var(--txt);
  border-radius:10px;padding:10px 16px;transition:.15s}
button:hover:not(:disabled){border-color:var(--accent)}
button:disabled{opacity:.45;cursor:not-allowed}
.btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#fff;font-weight:600}
.btn-big{width:100%;padding:14px;font-size:15px}
.btn-sm{padding:6px 12px;font-size:13px;border-radius:8px}
.btn-ghost{background:transparent}
input,textarea,select{width:100%;background:var(--input);border:1px solid var(--card-bd);color:var(--txt);
  border-radius:10px;padding:11px 12px;font:inherit}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent)}
.row{display:flex;gap:10px;flex-wrap:wrap}
.row>*{flex:1 1 160px}
.mt{margin-top:12px}
.msg{font-size:13px;margin-top:10px;color:var(--muted);min-height:18px}
.msg.err{color:var(--bad)} .msg.ok{color:var(--ok)}
.tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;
  background:rgba(110,168,254,.15);color:var(--accent);margin-left:6px;vertical-align:middle}
.tag.host{background:rgba(251,191,36,.16);color:var(--warn)}
.tag.done{background:rgba(52,211,153,.16);color:var(--ok)}
.hd{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:20px;letter-spacing:2px;font-weight:600}
.plist{display:flex;flex-wrap:wrap;gap:8px}
.pill{font-size:13px;padding:5px 11px;border-radius:999px;background:var(--input);border:1px solid var(--card-bd)}
.pill.off{opacity:.5}
.pill.me{border-color:var(--accent)}
.songlist{display:flex;flex-direction:column;gap:8px}
.song{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:10px;background:var(--input)}
.song img{width:40px;height:40px;border-radius:6px;object-fit:cover;background:#222}
.song .t{font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.song .a{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gcard{background:var(--card);border:1px solid var(--card-bd);border-radius:14px;padding:14px;margin-bottom:14px}
.gcard .idx{display:inline-block;font-size:12px;color:var(--muted);margin-bottom:6px}
.gcard .title{font-size:15px;font-weight:600}
.gcard .artist{font-size:13px;color:var(--muted);margin-bottom:10px}
.player{width:100%;height:86px;border:0;border-radius:10px;margin-bottom:10px;background:#0d1220}
.noaudio{display:flex;align-items:center;gap:10px;padding:10px;border-radius:10px;background:var(--input);
  font-size:13px;color:var(--muted);margin-bottom:10px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{font-size:13px;padding:6px 12px;border-radius:999px;border:1px solid var(--card-bd);
  background:var(--input);cursor:pointer;user-select:none;transition:.12s}
.chip:hover{border-color:var(--accent)}
.chip.on{background:rgba(110,168,254,.2);border-color:var(--accent);color:#fff}
.chip .n{font-size:11px;opacity:.9;margin-left:5px}
.bar{height:6px;border-radius:999px;background:var(--input);overflow:hidden;margin:10px 0}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));width:0}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--card-bd)}
th{color:var(--muted);font-weight:500;font-size:13px}
.rank1{color:var(--warn);font-weight:600}
.detail{border-top:1px solid var(--card-bd);padding:12px 0}
.detail:first-child{border-top:0}
.dname{font-size:14px;font-weight:600}
.dsub{font-size:13px;color:var(--muted);margin:2px 0 8px}
.pick{display:flex;gap:8px;font-size:13px;padding:3px 0;color:var(--muted);flex-wrap:wrap}
.pick .who{color:var(--txt);min-width:88px}
.pick .hit{color:var(--ok)}
.pick .miss{color:var(--muted)}
.hidden{display:none}
.foot{color:var(--muted);font-size:12px;text-align:center;margin-top:22px}
a{color:var(--accent)}
</style>
</head>
<body>
<div class="wrap" id="app"></div>
<div class="foot">猜歌 · 每首歌猜一个人的名字</div>
<script>
const LS = "guess_token";
let TOKEN = localStorage.getItem(LS) || "";
let ME = null;
let S = null;            // 当前房间快照
let PICKS = {};          // uid -> [qq,...] 本地已选（未提交前以本地为准）
let WS = null;
let BUSY = false;
let NOTE = {text:"", kind:""};
const $app = document.getElementById("app");

function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function note(t,k){NOTE={text:t||"",kind:k||""};paintNote();}
function paintNote(){
  const el=document.getElementById("note");
  if(el){el.textContent=NOTE.text;el.className="msg "+(NOTE.kind||"");}
}
async function api(path,opt){
  const o=Object.assign({headers:{}},opt||{});
  o.headers["Content-Type"]="application/json";
  if(TOKEN) o.headers["Authorization"]="Bearer "+TOKEN;
  const r=await fetch(path,o);
  let j={};
  try{ j=await r.json(); }catch(e){}
  if(r.status===401){ logoutLocal(); throw new Error("登录已过期，请重新登录"); }
  if(!r.ok){ throw new Error(j.message||("请求失败 "+r.status)); }
  return j;
}
function logoutLocal(){TOKEN="";ME=null;S=null;localStorage.removeItem(LS);closeWS();}

/* ---------------------------------------------------------------- 登录 */
function viewLogin(){
  $app.innerHTML = `
  <h1>猜歌</h1>
  <p class="sub">猜猜每首歌是谁分享的。登录用机器人私聊发的验证码。</p>
  <div class="card">
    <h2>用 QQ 登录</h2>
    <input id="qq" inputmode="numeric" placeholder="你的 QQ 号" autocomplete="off">
    <div class="row mt">
      <button id="send" class="btn-primary">发验证码给我</button>
    </div>
    <div class="row mt">
      <input id="code" inputmode="numeric" placeholder="6 位验证码" autocomplete="off">
      <button id="login" class="btn-primary">进入</button>
    </div>
    <div class="msg" id="note"></div>
    <p class="sub mt" style="margin-bottom:0">没收到？<b>在 QQ 里私聊机器人发一句「登录」</b>，
    机器人会直接把验证码回给你（不必先加好友）。名单由管理员维护，不在名单里会提示。</p>
  </div>`;
  const qqEl=document.getElementById("qq"), codeEl=document.getElementById("code");
  document.getElementById("send").onclick=async()=>{
    const qq=(qqEl.value||"").trim();
    if(!/^\d{5,12}$/.test(qq)){ note("QQ 号看起来不对","err"); return; }
    try{ const j=await api("/api/guess/login/code",{method:"POST",body:JSON.stringify({qq})});
      note(j.message||"验证码已发送","ok");
    }catch(e){ note(e.message,"err"); }
  };
  document.getElementById("login").onclick=async()=>{
    const qq=(qqEl.value||"").trim(), code=(codeEl.value||"").trim();
    if(!code){ note("先填验证码","err"); return; }
    try{ const j=await api("/api/guess/login",{method:"POST",body:JSON.stringify({qq,code})});
      TOKEN=j.token; localStorage.setItem(LS,TOKEN); ME={qq:j.qq,nick:j.nick||String(j.qq)};
      note(""); await boot();
    }catch(e){ note(e.message,"err"); }
  };
  paintNote();
}

/* ---------------------------------------------------------------- 首页 */
function viewHome(){
  const roomParam = new URLSearchParams(location.search).get("r");
  $app.innerHTML = `
  <h1>猜歌</h1>
  <p class="sub">你好 ${esc(ME.nick||ME.qq)} · <a href="#" id="out">退出登录</a></p>
  ${roomParam?`<div class="card"><h2>有人邀请你进房间</h2>
    <p class="sub">房间号 <b class="code">${esc(roomParam)}</b></p>
    <button class="btn-primary btn-big" id="joinInvite">进房间</button></div>`:""}
  ${ME.single_allowed?`<div class="card">
    <h2>一个人玩</h2>
    <p class="sub">自己刷一局，从任意历史收集窗口里挑题目。</p>
    <button class="btn-primary btn-big" id="single">单人开一局</button>
  </div>`:""}
  <div class="card">
    <h2>和朋友一起</h2>
    <button class="btn-primary btn-big" id="create">创建房间</button>
    <div class="row mt">
      <input id="code" placeholder="房间号，如 K7M2QX" maxlength="6" style="text-transform:uppercase">
      <button id="join">加入</button>
    </div>
    <div class="msg" id="note"></div>
  </div>`;
  const joinBy=async(code)=>{
    try{ const j=await api("/api/guess/room/join",{method:"POST",body:JSON.stringify({code})});
      S=j.state; history.replaceState(null,"","?r="+j.code); openWS(); render();
    }catch(e){ note(e.message,"err"); }
  };
  document.getElementById("out").onclick=e=>{e.preventDefault();logoutLocal();render();};
  if(roomParam) document.getElementById("joinInvite").onclick=()=>joinBy(roomParam);
  const singleBtn=document.getElementById("single");
  if(singleBtn) singleBtn.onclick=async()=>{
    try{ const j=await api("/api/guess/room/create",{method:"POST",body:JSON.stringify({mode:"single"})});
      S=j.state; history.replaceState(null,"","?r="+j.code); openWS(); render();
    }catch(e){ note(e.message,"err"); }
  };
  document.getElementById("create").onclick=async()=>{
    try{ const j=await api("/api/guess/room/create",{method:"POST",body:JSON.stringify({mode:"multi"})});
      S=j.state; history.replaceState(null,"","?r="+j.code); openWS(); render();
    }catch(e){ note(e.message,"err"); }
  };
  document.getElementById("join").onclick=()=>{
    const c=(document.getElementById("code").value||"").trim().toUpperCase();
    if(!c){ note("先填房间号","err"); return; }
    joinBy(c);
  };
  paintNote();
}

/* ---------------------------------------------------------------- 房间 */
function viewRoom(){
  if(!S){ return viewHome(); }
  const st=S;
  const head = `
  <div class="hd">
    <div>
      <div class="code">${esc(st.code)}<span class="tag">${esc(st.phase_label)}</span>
        ${st.is_host?'<span class="tag host">房主</span>':""}</div>
      <div class="sub" style="margin:4px 0 0">${st.mode==="single"?"单人模式":"多人房间"} ·
        ${esc(st.label||"还没选题目")}</div>
    </div>
    <div style="display:flex;gap:8px">
      ${st.mode==="multi"?'<button class="btn-sm" id="share">复制链接</button>':""}
      <button class="btn-sm btn-ghost" id="leave">退出</button>
    </div>
  </div>
  <div class="card">
    <h2>房间里的人（${st.total}）</h2>
    <div class="plist" id="plist">${st.players.map(p=>`
      <span class="pill ${p.online?"":"off"} ${p.is_me?"me":""}">
        ${esc(p.nick)}${p.is_host?' <span class="tag host">房主</span>':""}${p.submitted?' <span class="tag done">已交</span>':""}${p.is_me?" (我)":""}
        ${(st.is_host&&!p.is_host)?`<a href="#" data-transfer="${p.qq}" style="margin-left:6px;font-size:12px">转让</a>`:""}
      </span>`).join("")}</div>
    <div class="msg" id="note"></div>
  </div>`;

  let body="";
  if(st.phase==="lobby"||st.phase==="setup") body = viewSetup(st);
  else if(st.phase==="playing") body = viewPlaying(st);
  else body = viewSettled(st);

  $app.innerHTML = head + body;
  bindRoom(st);
  paintNote();
}

function viewSetup(st){
  const cards = st.cards||[];
  const live = st.source && st.source.kind==="live";
  return `
  ${st.is_host?`<div class="card">
    <h2>选题目</h2>
    <p class="sub">可以拿某个历史收集窗口当题目，也可以让大家现场贴歌。</p>
    <div class="row">
      <select id="grp"><option value="">选择群…</option></select>
      <select id="win"><option value="">选择窗口…</option></select>
    </div>
    <div class="row mt">
      <button id="useWin">用这个窗口</button>
      <button id="useLive" class="${live?"btn-primary":""}">现场收集</button>
    </div>
    <div class="msg" id="note2"></div>
  </div>`:""}
  ${live?`<div class="card">
    <h2>贴歌</h2>
    <p class="sub">支持网易云 / QQ音乐 / 酷狗 / 酷我。可以一次贴多条，也能发整段分享文案。</p>
    <textarea id="live" rows="3" placeholder="把分享链接粘到这里…"></textarea>
    <div class="row mt"><button class="btn-primary" id="addSongs">加进题目</button></div>
    <div class="msg" id="note3"></div>
  </div>`:""}
  <div class="card">
    <h2>曲目预览（${cards.length} 首）</h2>
    <p class="sub">这里不会显示分享者 —— 那是要猜的东西。</p>
    ${cards.length?`<div class="songlist">${cards.map(c=>songRow(c,st)).join("")}</div>`
      :`<p class="sub">还没有曲目。</p>`}
    ${st.is_host&&cards.length?`<button class="btn-primary btn-big mt" id="start">开始游戏</button>`:""}
    ${!st.is_host&&cards.length?`<p class="sub mt">等房主点开始。</p>`:""}
  </div>`;
}

function songRow(c,st){
  return `<div class="song">
    ${c.cover?`<img src="${esc(c.cover)}" loading="lazy" alt="">`:`<div style="width:40px;height:40px;border-radius:6px;background:#222"></div>`}
    <div style="flex:1;min-width:0"><div class="t">${esc(c.title)}</div>
      <div class="a">${esc(c.artists||"未知歌手")}</div></div>
    <button class="btn-sm btn-ghost" data-del="${c.uid}">删</button>
  </div>`;
}

function chipsHtml(uid, cands){
  const picks=PICKS[uid]||[];
  return cands.map(p=>{
    const at=picks.indexOf(p.qq);
    return `<span class="chip ${at>=0?"on":""}" data-uid="${uid}" data-qq="${p.qq}">
      ${esc(p.nick)}${at>=0?`<span class="n">${at+1}</span>`:""}</span>`;
  }).join("");
}

function neteaseLink(c){
  if(c.netease_id) return `https://music.163.com/#/song?id=${encodeURIComponent(c.netease_id)}`;
  const q = encodeURIComponent((c.title||"") + " " + (c.artists||""));
  return `https://music.163.com/#/search/m/?s=${q}`;
}

function viewPlaying(st){
  const cards = st.cards||[];
  const cands = st.candidates||[];
  const done = Object.keys(PICKS).filter(k=>(PICKS[k]||[]).length).length;
  return `
  <div class="card">
    <h2>作答（<span id="doneCount">${done}</span>/${cards.length}）</h2>
    <p class="sub">每首歌按顺序点 ${st.slots} 个名字；点错了再点一下就撤销。选中后角标是名次。</p>
    <div class="bar"><i id="progBar" style="width:${cards.length?Math.round(done*100/cards.length):0}%"></i></div>
    <div class="sub">已提交 <span id="subCount">${st.submitted}</span>/${st.total} 人</div>
  </div>
  ${cards.map((c,i)=>{
    const picks = PICKS[c.uid]||[];
    return `<div class="gcard" data-uid="${c.uid}">
      <div class="idx">第 ${i+1} 首</div>
      <div class="title">${esc(c.title)}</div>
      <div class="artist">${esc(c.artists||"未知歌手")}
        <a href="${neteaseLink(c)}" target="_blank" rel="noreferrer" style="margin-left:8px;font-size:12px">去网易云听</a></div>
      ${c.netease_id?`<iframe class="player" loading="lazy" src="https://music.163.com/outchain/player?type=2&id=${esc(c.netease_id)}&auto=0&height=66" allow="autoplay"></iframe>`
        :`<div class="noaudio">这首没匹配到网易云的 id，内嵌播放器放不了，点上面的「去网易云听」。</div>`}
      <div class="chips" data-chips="${c.uid}">${chipsHtml(c.uid, cands)}</div>
    </div>`;
  }).join("")}
  <button class="btn-primary btn-big" id="submit">提交</button>`;
}

function viewSettled(st){
  const r = st.result||{ranking:[],details:[]};
  const arch = st.archive;
  return `
  <div class="card">
    <h2>排行榜</h2>
    <table><thead><tr><th>名次</th><th>玩家</th><th>分数</th><th>猜中</th></tr></thead><tbody>
    ${r.ranking.map(x=>`<tr><td class="${x.rank===1?"rank1":""}">${x.rank}</td>
      <td>${esc(x.nick)}${x.qq===st.me.qq?" (我)":""}</td>
      <td>${x.score}</td><td>${x.hits}/${(r.details||[]).length}</td></tr>`).join("")}
    </tbody></table>
    <div class="row mt">
      ${st.is_host?`<button id="next">开下一局</button>`:""}
      ${st.is_host&&st.source&&st.source.kind==="live"?`<button class="btn-primary" id="archive">归档到网易云歌单</button>`:""}
    </div>
    <div class="msg" id="note3"></div>
    ${arch?`<div class="msg ${arch.ok?"ok":"err"}">${esc(arch.message||"")}
      ${arch.ok&&arch.url?`<br><a href="${esc(arch.url)}" target="_blank" rel="noreferrer">${esc(arch.name||"打开歌单")}</a>`:""}
      ${(arch.unmatched&&arch.unmatched.length)?`<br>没匹配到：${esc(arch.unmatched.join("、"))}`:""}</div>`:""}
  </div>
  <div class="card">
    <h2>每首歌的答案</h2>
    ${r.details.map(d=>`
      <div class="detail">
        <div class="dname">第 ${d.idx+1} 首 · ${esc(d.title)}</div>
        <div class="dsub">分享者：<b>${esc(d.sharer.nick)}</b></div>
        ${(d.picks||[]).map(p=>`<div class="pick"><span class="who">${esc(p.nick)}</span>
          <span class="${p.hit>=0?"hit":"miss"}">${p.guesses&&p.guesses.length
            ? p.guesses.map(g=>esc(g.nick)).join(" → ")
            : "没作答"}</span>
          <span>${p.score>0?"+"+p.score:"0"}</span></div>`).join("")}
      </div>`).join("")}
  </div>`;
}

/* ---------------------------------------------------------------- 交互绑定 */
function bindRoom(st){
  const q=(s)=>document.querySelector(s);
  const on=(s,fn)=>{const el=q(s); if(el) el.onclick=fn;};

  on("#leave", async()=>{
    try{ await api("/api/guess/room/leave",{method:"POST",body:JSON.stringify({code:st.code})}); }catch(e){}
    closeWS(); S=null; history.replaceState(null,"","?"); render();
  });
  on("#share", async()=>{
    const url = location.origin + "/guess?r=" + st.code;
    try{ await navigator.clipboard.writeText(url); note("链接已复制","ok"); }
    catch(e){ prompt("复制这个链接", url); }
  });
  document.querySelectorAll("[data-transfer]").forEach(el=>{
    el.onclick=async(e)=>{ e.preventDefault();
      if(!confirm("把房主转给 Ta？")) return;
      await act(st,"transfer",{qq:Number(el.dataset.transfer)});
    };
  });
  if(st.is_host && (st.phase==="lobby"||st.phase==="setup")) bindSourcePicker(st);
  on("#useLive", ()=>act(st,"select_source",{kind:"live"}));
  on("#addSongs", async()=>{
    const ta=q("#live"); const text=(ta&&ta.value||"").trim();
    if(!text){ setNote("先贴点东西","err",3); return; }
    if(BUSY) return; BUSY=true;
    try{ await act(st,"add_songs",{text}); if(ta) ta.value=""; }
    finally{ BUSY=false; }
  });
  on("#start", ()=>act(st,"start"));
  on("#submit", ()=>submitAnswers(st));
  on("#next", ()=>act(st,"reset"));
  on("#archive", async()=>{
    if(!confirm("把这一局的曲目归档成网易云歌单？")) return;
    setNote("归档中，需要逐首匹配网易云，可能有几十秒…","",3);
    try{
      const j=await api("/api/guess/room/archive",{method:"POST",body:JSON.stringify({code:st.code})});
      S=j.state; render(); note(j.archive.message||"完成", j.archive.ok?"ok":"err");
    }catch(e){ setNote(e.message,"err",3); }
  });

  document.querySelectorAll("[data-del]").forEach(el=>{
    el.onclick=()=>act(st,"remove_song",{uid:Number(el.dataset.del)});
  });

  document.querySelectorAll("[data-chips]").forEach(box=>{
    bindChips(box);
  });
}

/* 选项点击：**只重绘那一张卡的选项区**，不做整页 render。
   整页重绘会重建所有 iframe，正在放的歌会被打断。 */
function bindChips(box){
  const uid = Number(box.dataset.chips);
  box.querySelectorAll(".chip").forEach(el=>{
    el.onclick=()=>togglePick(uid, Number(el.dataset.qq));
  });
}

function togglePick(uid, qq){
  const slots = (S&&S.slots)||3;
  const arr = (PICKS[uid]||[]).slice();
  const at = arr.indexOf(qq);
  if(at>=0) arr.splice(at,1);
  else { if(arr.length>=slots) arr.shift(); arr.push(qq); }
  PICKS[uid] = arr;
  const box = document.querySelector(`[data-chips="${uid}"]`);
  if(box){
    box.innerHTML = chipsHtml(uid, (S&&S.candidates)||[]);
    bindChips(box);
  }
  updateProgress();
}

function updateProgress(){
  const total = (S&&S.cards||[]).length;
  const done = Object.keys(PICKS).filter(k=>(PICKS[k]||[]).length).length;
  const dc=document.getElementById("doneCount"); if(dc) dc.textContent=done;
  const bar=document.getElementById("progBar");
  if(bar) bar.style.width=(total?Math.round(done*100/total):0)+"%";
  const sc=document.getElementById("subCount");
  if(sc && S) sc.textContent=S.submitted;
}

function bindSourcePicker(st){
  const grp=document.getElementById("grp"), win=document.getElementById("win");
  if(!grp||!win) return;
  const setWins=(gid)=>{
    const list=(windowCache||[]).filter(w=>String(w.group_id)===String(gid));
    win.innerHTML = `<option value="">选择窗口…</option>` + list.map(w=>
      `<option value="${esc(w.window_key)}" data-label="${esc(w.label)}">${esc(w.label)} · ${w.count} 首</option>`).join("");
  };
  const groups=[...new Set((windowCache||[]).map(w=>w.group_id))];
  grp.innerHTML = `<option value="">选择群…</option>` + groups.map(g=>`<option value="${g}">群 ${g}</option>`).join("");
  grp.onchange=()=>setWins(grp.value);
  document.getElementById("useWin").onclick=()=>{
    if(!grp.value||!win.value){ setNote("先选群和窗口","err",2); return; }
    const opt=win.selectedOptions[0];
    act(st,"select_source",{kind:"window",group_id:Number(grp.value),window_key:win.value,label:opt?opt.dataset.label:""});
  };
}

function setNote(t,k,which){
  const el=document.getElementById(which===3?"note3":(which===2?"note2":"note"));
  if(el){ el.textContent=t; el.className="msg "+(k||""); }
  else note(t,k);
}

async function act(st,action,extra,keepNote){
  try{
    const j=await api("/api/guess/room/action",{method:"POST",
      body:JSON.stringify(Object.assign({code:st.code,action},extra||{}))});
    applyState(j.state);
    if(j.message) note(j.message,"ok");
  }catch(e){ note(e.message,"err"); }
  return null;
}

async function submitAnswers(st){
  const answers={};
  Object.keys(PICKS).forEach(uid=>{ if((PICKS[uid]||[]).length) answers[uid]=PICKS[uid]; });
  if(!Object.keys(answers).length){ note("至少排一首再提交","err"); return; }
  const unanswered=(st.cards||[]).length-Object.keys(answers).length;
  if(unanswered>0 && !confirm(`还有 ${unanswered} 首没排，这些题会得 0 分。确定提交？`)) return;
  await act(st,"submit",{answers});
}

/* ---------------------------------------------------------------- 状态 */
function applyState(next){
  if(!next) return;
  const prev = S;
  const sameCards = prev && prev.phase==="playing" && next.phase==="playing"
    && (prev.cards||[]).length===(next.cards||[]).length
    && (prev.cards||[]).every((c,i)=>next.cards[i]&&c.uid===next.cards[i].uid);
  S = next;
  if(next.phase==="playing"){
    if(!prev || prev.phase!=="playing"){
      PICKS={};
      const mine=next.my_answers||{};
      Object.keys(mine).forEach(k=>{ PICKS[k]=mine[k]; });
    }
  } else {
    PICKS={};
  }
  // 只是「有人交卷」这种小变化：不动 DOM 主体，避免 iframe 重载打断播放
  if(sameCards){ lightUpdate(); return; }
  render();
}

function lightUpdate(){
  updateProgress();
  const box=document.getElementById("plist");
  if(box && S){
    box.innerHTML=S.players.map(p=>`
      <span class="pill ${p.online?"":"off"} ${p.is_me?"me":""}">
        ${esc(p.nick)}${p.is_host?' <span class="tag host">房主</span>':""}${p.submitted?' <span class="tag done">已交</span>':""}${p.is_me?" (我)":""}
      </span>`).join("");
  }
}

function openWS(){
  closeWS();
  if(!S||!TOKEN) return;
  const proto = location.protocol==="https:"?"wss":"ws";
  const url = `${proto}://${location.host}/api/guess/ws?code=${encodeURIComponent(S.code)}&token=${encodeURIComponent(TOKEN)}`;
  try{
    WS = new WebSocket(url);
    WS.onmessage = (ev)=>{
      let m={}; try{ m=JSON.parse(ev.data); }catch(e){ return; }
      if(m.type==="state") applyState(m.state);
    };
    WS.onclose = ()=>{ WS=null; };
  }catch(e){}
}
function closeWS(){ if(WS){ try{WS.close();}catch(e){} WS=null; } }

/* ---------------------------------------------------------------- 启动 */
let windowCache=null;
async function loadWindows(){
  try{ const j=await api("/api/guess/windows"); windowCache=j.windows||[]; }
  catch(e){ windowCache=[]; }
}

async function boot(){
  try{
    const j=await api("/api/guess/me");
    ME={qq:j.qq,nick:j.nick||String(j.qq),single_allowed:j.single_allowed};
  }catch(e){ viewLogin(); return; }
  const code = new URLSearchParams(location.search).get("r");
  if(code){
    try{
      const j=await api("/api/guess/room?code="+encodeURIComponent(code));
      S=j.state; await loadWindows(); openWS(); render(); return;
    }catch(e){ /* 房间没了，退回首页 */ }
  }
  await loadWindows();
  render();
}

function render(){
  if(!ME) return viewLogin();
  if(!S) return viewHome();
  viewRoom();
}

window.addEventListener("beforeunload", closeWS);
boot();
</script>
</body>
</html>
"""


ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>猜歌名单 · 管理</title>
<style>
:root{
  --bg:#0b0f1a; --bg2:#111726; --card:rgba(255,255,255,.04); --card-bd:rgba(255,255,255,.08);
  --txt:#e8edf6; --muted:#8b97ad; --accent:#6ea8fe; --accent2:#a78bfa; --ok:#34d399; --bad:#f87171;
  --input:rgba(255,255,255,.06);color-scheme:dark;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);line-height:1.55;padding:18px 14px 40px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:19px;margin:0 0 4px}
h2{font-size:15px;margin:0 0 10px}
p.sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--card-bd);border-radius:14px;padding:16px;margin-bottom:14px}
button{font:inherit;cursor:pointer;border:1px solid var(--card-bd);background:var(--input);color:var(--txt);
  border-radius:10px;padding:9px 14px}
button:hover{border-color:var(--accent)}
.btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#fff;font-weight:600}
input,textarea,select{width:100%;background:var(--input);border:1px solid var(--card-bd);color:var(--txt);
  border-radius:10px;padding:10px 12px;font:inherit}
textarea{font-family:ui-monospace,Menlo,monospace;font-size:13px}
.row{display:flex;gap:10px;flex-wrap:wrap}
.row>*{flex:1 1 160px}
.mt{margin-top:12px}
.msg{font-size:13px;margin-top:8px;color:var(--muted)}
.msg.err{color:var(--bad)}.msg.ok{color:var(--ok)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--card-bd)}
th{color:var(--muted);font-weight:500;font-size:13px}
td.n{font-family:ui-monospace,Menlo,monospace}
.off{opacity:.5}
</style>
</head>
<body>
<div class="wrap">
  <h1>猜歌名单</h1>
  <p class="sub">只有在名单里、且未被停用的 QQ 号才能进猜歌页面。改动立刻生效。</p>

  <div class="card" id="authCard">
    <h2>管理令牌</h2>
    <div class="row">
      <input id="token" placeholder="MUSIC_WEBUI_TOKEN">
      <button class="btn-primary" id="save">进入</button>
    </div>
    <div class="msg" id="authMsg"></div>
  </div>

  <div id="main" class="hidden">
    <div class="card">
      <h2>从群成员批量导入</h2>
      <p class="sub">机器人必须在线且在群里。</p>
      <div class="row">
        <select id="grp"><option value="">选择群…</option></select>
        <button id="pull">拉取并导入</button>
      </div>
      <div class="msg" id="pullMsg"></div>
    </div>

    <div class="card">
      <h2>粘贴导入</h2>
      <p class="sub">一行一个，支持 <code>123456789 昵称</code> 或只有号码。</p>
      <textarea id="bulk" rows="5" placeholder="123456789 小明&#10;987654321"></textarea>
      <div class="row mt"><button class="btn-primary" id="imp">导入</button></div>
      <div class="msg" id="impMsg"></div>
    </div>

    <div class="card">
      <h2>名单（<span id="cnt">0</span>）</h2>
      <input id="q" placeholder="搜索 QQ 号或昵称" class="mt">
      <table class="mt"><thead><tr><th>QQ</th><th>昵称</th><th>来源群</th><th>状态</th><th></th></tr></thead>
      <tbody id="tb"></tbody></table>
    </div>
  </div>
</div>
<script>
const LS="mwc_token";
let TOKEN=localStorage.getItem(LS)||"";
const $=s=>document.querySelector(s);
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
async function api(p,o){
  const opt=Object.assign({headers:{"Content-Type":"application/json"}},o||{});
  if(TOKEN) opt.headers["Authorization"]="Bearer "+TOKEN;
  const r=await fetch(p,opt); let j={}; try{j=await r.json();}catch(e){}
  if(!r.ok) throw new Error(j.message||("请求失败 "+r.status));
  return j;
}
function msg(id,t,k){const el=$(id);if(el){el.textContent=t;el.className="msg "+(k||"");}}

async function load(){
  const kw=encodeURIComponent($("#q").value||"");
  const j=await api("/api/music-admin/game/users?q="+kw);
  $("#cnt").textContent=j.count;
  const tb=$("#tb");
  tb.innerHTML=(j.users||[]).map(u=>`<tr class="${u.enabled?"":"off"}">
    <td class="n">${u.qq}</td><td>${esc(u.nick||"—")}</td>
    <td>${u.source_group||"—"}</td>
    <td>${u.enabled?"正常":"停用"}</td>
    <td style="text-align:right;white-space:nowrap">
      <button data-tog="${u.qq}" data-en="${u.enabled?0:1}" style="padding:4px 9px;font-size:12px">${u.enabled?"停用":"启用"}</button>
      <button data-del="${u.qq}" style="padding:4px 9px;font-size:12px;color:var(--bad)">删除</button>
    </td></tr>`).join("") || `<tr><td colspan="5" class="msg">还没有人</td></tr>`;
  tb.querySelectorAll("[data-del]").forEach(el=>el.onclick=async()=>{
    if(!confirm("从名单里删除 QQ "+el.dataset.del+"？")) return;
    await api("/api/music-admin/game/users",{method:"POST",body:JSON.stringify({action:"delete",qq:Number(el.dataset.del)})});
    load();
  });
  tb.querySelectorAll("[data-tog]").forEach(el=>el.onclick=async()=>{
    await api("/api/music-admin/game/users",{method:"POST",
      body:JSON.stringify({action:"toggle",qq:Number(el.dataset.tog),enabled:el.dataset.en==="1"})});
    load();
  });
  const grp=$("#grp");
  if(grp.options.length<=1 && j.groups){
    grp.innerHTML=`<option value="">选择群…</option>`+j.groups.map(g=>`<option value="${g}">群 ${g}</option>`).join("");
  }
}

$("#save").onclick=async()=>{
  TOKEN=$("#token").value.trim(); localStorage.setItem(LS,TOKEN);
  try{ await load(); $("#authCard").classList.add("hidden"); $("#main").classList.remove("hidden"); }
  catch(e){ msg("#authMsg",e.message,"err"); }
};
$("#pull").onclick=async()=>{
  const gid=$("#grp").value;
  if(!gid){ msg("#pullMsg","先选群","err"); return; }
  msg("#pullMsg","拉取中…");
  try{ const j=await api("/api/music-admin/game/users",{method:"POST",
      body:JSON.stringify({action:"import_group",group_id:Number(gid)})});
    msg("#pullMsg",j.message,"ok"); load();
  }catch(e){ msg("#pullMsg",e.message,"err"); }
};
$("#imp").onclick=async()=>{
  const text=$("#bulk").value;
  if(!text.trim()){ msg("#impMsg","先贴点内容","err"); return; }
  try{ const j=await api("/api/music-admin/game/users",{method:"POST",
      body:JSON.stringify({action:"import_text",text})});
    msg("#impMsg",j.message,"ok"); $("#bulk").value=""; load();
  }catch(e){ msg("#impMsg",e.message,"err"); }
};
$("#q").oninput=()=>{ clearTimeout(window._t); window._t=setTimeout(load,300); };

(async()=>{
  if(!TOKEN) return;
  try{ await load(); $("#authCard").classList.add("hidden"); $("#main").classList.remove("hidden"); }
  catch(e){ /* 令牌失效就留在登录卡 */ }
})();
</script>
</body>
</html>
"""
