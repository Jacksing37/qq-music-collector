DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>群音乐收集 · 管理面板</title>
<style>
:root{
  --bg:#0b0f1a; --bg2:#111726; --card:rgba(255,255,255,.04); --card-bd:rgba(255,255,255,.08);
  --txt:#e8edf6; --muted:#8b97ad; --accent:#6ea8fe; --accent2:#a78bfa; --ok:#34d399; --bad:#f87171;
  --input:rgba(255,255,255,.06); --shadow:0 10px 30px rgba(0,0,0,.35); --side:#0e1320;
}
[data-theme="light"]{
  --bg:#f4f6fb; --bg2:#ffffff; --card:rgba(20,30,60,.03); --card-bd:rgba(20,30,60,.1);
  --txt:#1a2233; --muted:#5b6678; --accent:#3b6fe0; --accent2:#7c5cf0; --ok:#0f9d63; --bad:#d8453b;
  --input:rgba(20,30,60,.05); --shadow:0 10px 30px rgba(20,30,60,.1); --side:#eef1f8;
}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg);color:var(--txt);line-height:1.55;min-height:100vh}
a{color:var(--accent);text-decoration:none}
button{font:inherit;cursor:pointer;border:1px solid var(--card-bd);background:var(--input);color:var(--txt);
  border-radius:10px;padding:8px 14px;transition:.18s}
button:hover{border-color:var(--accent);transform:translateY(-1px)}
.btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#fff;font-weight:600}
.btn-danger{border-color:rgba(248,113,113,.5);color:var(--bad)}
.btn-danger:hover{border-color:var(--bad)}
input,select,textarea{width:100%;background:var(--input);border:1px solid var(--card-bd);color:var(--txt);
  border-radius:10px;padding:9px 11px;font:inherit;transition:.18s}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(110,168,254,.18)}
textarea{resize:vertical;font-family:ui-monospace,monospace;font-size:13px}
label.chk{display:inline-flex;align-items:center;gap:10px;cursor:pointer;font-size:15px}
input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent)}
.hidden{display:none!important}

/* 顶栏 */
.topbar{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:14px;padding:12px 18px;
  background:linear-gradient(var(--bg2),rgba(17,23,38,.7));border-bottom:1px solid var(--card-bd);backdrop-filter:blur(14px)}
.topbar h1{font-size:18px;margin:0;font-weight:700}
.status-pill{font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--card-bd);color:var(--muted)}
.status-pill.on{color:var(--ok);border-color:var(--ok)}
.spacer{flex:1}

/* 布局 */
.layout{display:flex;min-height:calc(100vh - 57px)}
.sidebar{width:210px;flex:0 0 210px;background:var(--side);border-right:1px solid var(--card-bd);
  padding:14px 10px;position:sticky;top:57px;height:calc(100vh - 57px);overflow:auto}
.sidebar .brand{padding:6px 10px 12px;font-size:14px;color:var(--muted);font-weight:600}
.nav{display:block;width:100%;text-align:left;margin-bottom:6px;background:transparent;border:none;color:var(--txt)}
.nav:hover{background:var(--input);transform:none}
.nav.active{background:linear-gradient(135deg,rgba(110,168,254,.18),rgba(167,139,250,.18));
  border:1px solid var(--card-bd);color:#fff;font-weight:600}
.content{flex:1;padding:22px 24px 120px;overflow:auto}
.page{max-width:1000px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--card-bd);border-radius:18px;padding:18px 20px;
  margin-bottom:16px;box-shadow:var(--shadow)}
.card h2{font-size:16px;margin:0 0 14px;display:flex;align-items:center;gap:8px}
.card h2 .dot{width:8px;height:8px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2))}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:var(--input);border:1px solid var(--card-bd);border-radius:14px;padding:12px 14px}
.stat .k{font-size:12px;color:var(--muted)}
.stat .v{font-size:17px;font-weight:600;margin-top:2px}
pre.runs{margin:10px 0 0;font-size:12px;color:var(--muted);white-space:pre-wrap;font-family:ui-monospace,monospace}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.row.end{justify-content:flex-end}
.muted{color:var(--muted);font-size:13px}
.badge{font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--card-bd)}
.badge.ok{color:var(--ok);border-color:var(--ok)}
.badge.bad{color:var(--bad);border-color:var(--bad)}

/* 配置表单 */
.field{display:grid;grid-template-columns:230px 1fr;gap:14px;padding:10px 0;border-top:1px dashed var(--card-bd)}
.field:first-of-type{border-top:none}
.flabel{font-size:14px}.flabel .hint{display:block;font-size:12px;color:var(--muted);margin-top:2px}
.fctrl.dirty input,.fctrl.dirty select,.fctrl.dirty textarea{border-color:var(--accent2)}

/* 收集管理表格 */
.gcard{background:var(--input);border:1px solid var(--card-bd);border-radius:14px;padding:14px 16px;margin-bottom:14px}
.gtitle{font-size:15px;font-weight:600}.gtitle .cnt{font-size:12px;color:var(--muted);font-weight:400;margin-left:8px}
.gtbl{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
.gtbl th,.gtbl td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--card-bd);vertical-align:middle}
.gtbl th{color:var(--muted);font-weight:500;font-size:12px}
.gtbl tr:hover td{background:rgba(110,168,254,.05)}
.gtbl .idx{color:var(--accent2);font-weight:600;width:30px}
.gtbl .plat{font-size:11px;color:var(--muted);width:64px}
.gtbl .date{font-size:11px;color:var(--muted);width:118px;white-space:nowrap}
.gtbl .mt{color:var(--ok);font-size:12px}.gtbl .un{color:var(--muted);font-size:12px}
.gtbl .acts{white-space:nowrap;width:1%}
.gtbl .acts button{padding:3px 7px;font-size:12px;margin-left:4px}
.empty{color:var(--muted);font-size:13px;padding:8px 2px}
/* 收集管理：拖拽排序 + 歌单链接 */
.songrow{cursor:grab}
.songrow.dragging{opacity:.4;background:rgba(110,168,254,.12)}
.songrow.droptgt{box-shadow:inset 0 2px 0 var(--accent);background:rgba(110,168,254,.08)}
.plrow{margin-top:4px;font-size:13px}
.plabel{color:var(--muted)}
.plink{color:var(--accent);font-weight:600;text-decoration:none}
.plink:hover{text-decoration:underline}
.muted{color:var(--muted)}

/* 预览抽屉 */
.pv-mask{position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.5);backdrop-filter:blur(3px)}
.pv-drawer{position:fixed;top:0;right:0;bottom:0;z-index:61;width:min(560px,94vw);background:var(--bg2);
  border-left:1px solid var(--card-bd);box-shadow:var(--shadow);display:flex;flex-direction:column;
  transform:translateX(105%);transition:transform .26s cubic-bezier(.16,1,.3,1)}
.pv-drawer.open{transform:none}
.pv-head{display:flex;align-items:center;gap:10px;padding:15px 18px;border-bottom:1px solid var(--card-bd)}
.pv-head h3{margin:0;font-size:16px;flex:1}.pv-head button{width:32px;height:32px;padding:0;border-radius:9px;font-size:14px}
.pv-body{flex:1;overflow:auto;padding:16px 18px 30px}
.pv-sec{margin-bottom:18px}.pv-tag{font-size:11px;color:var(--muted);letter-spacing:.5px;margin-bottom:6px;text-transform:uppercase}
.pv-name{font-size:18px;font-weight:700;padding:11px 13px;background:var(--input);border:1px solid var(--card-bd);border-radius:12px;word-break:break-all}
.pv-desc{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:13px;line-height:1.65;background:var(--input);
  border:1px solid var(--card-bd);border-radius:12px;padding:11px 13px;max-height:300px;overflow:auto;margin:0}
.pv-desc.empty{font-style:italic}

/* 弹窗 */
.modal{position:fixed;inset:0;z-index:70;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.55);backdrop-filter:blur(4px)}
.modal .box{background:var(--bg2);border:1px solid var(--card-bd);border-radius:18px;padding:22px;width:min(460px,92vw);box-shadow:var(--shadow)}
.modal h3{margin:0 0 6px}.modal p{color:var(--muted);font-size:13px;margin:0 0 14px}
.modal .fld{margin-bottom:12px}.modal .fld label{display:block;font-size:13px;margin-bottom:5px;color:var(--muted)}
.modal .row{margin-top:8px;justify-content:flex-end;gap:10px}

/* 底部保存条 */
.footbar{position:fixed;left:210px;right:0;bottom:0;z-index:40;display:flex;align-items:center;gap:14px;justify-content:flex-end;
  padding:12px 22px;background:linear-gradient(transparent,var(--bg) 40%)}
.footbar .msg{font-size:13px;color:var(--muted);margin-right:auto}
.footbar .msg.ok{color:var(--ok)}.footbar .msg.bad{color:var(--bad)}
.footbar .count{font-size:13px;color:var(--accent2);font-weight:600}
</style>
</head>
<body>

<div class="topbar">
  <h1>🎵 群音乐收集</h1>
  <span id="statusPill" class="status-pill">—</span>
  <div class="spacer"></div>
  <button id="themeBtn" title="切换主题">🌓 主题</button>
  <button id="logoutBtn" title="清除本地令牌">退出</button>
</div>

<div class="layout">
  <aside class="sidebar">
    <div class="brand">管理面板</div>
    <button class="nav active" data-page="overview">📊 概览</button>
    <button class="nav" data-page="collect">🎵 收集管理</button>
    <button class="nav" data-page="master">📚 总库</button>
    <button class="nav" data-page="config">⚙ 配置</button>
    <button class="nav" data-page="aliases">✏ 昵称映射</button>
    <button class="nav" data-page="admin">🛡 管理员</button>
    <button class="nav" data-page="account">🔑 网易云账号</button>
  </aside>

  <main class="content">

    <!-- 概览 -->
    <section id="page-overview" class="page">
      <div class="card">
        <div class="stat-grid">
          <div class="stat"><div class="k">当前窗口</div><div class="v" id="ovWindow">—</div></div>
          <div class="stat"><div class="k">收集状态</div><div class="v" id="ovCollect">—</div></div>
          <div class="stat"><div class="k">收集模式</div><div class="v" id="ovOverride">—</div></div>
          <div class="stat"><div class="k">网易云</div><div class="v" id="ovNetease">—</div></div>
        </div>
        <pre class="runs" id="ovRuns">加载中…</pre>
      </div>
      <div class="card">
        <h2><span class="dot"></span>窗口与收集控制</h2>
        <div class="row">
          <label class="muted">窗口：<select id="winSel"></select></label>
          <span id="neteaseBadge" class="badge">网易云：…</span>
          <div class="spacer"></div>
          <button id="opStart">▶ 强制开始</button>
          <button id="opStop">⏸ 强制停止</button>
          <button id="opAuto">↺ 恢复自动</button>
          <button id="opArchiveAll" class="btn-primary">📦 归档当前窗口全部</button>
        </div>
      </div>
      <div class="card">
        <h2><span class="dot"></span>各群收集概览</h2>
        <div id="ovGroups"><div class="empty">加载中…</div></div>
      </div>
    </section>

    <!-- 收集管理 -->
    <section id="page-collect" class="page hidden">
      <div class="card">
        <h2><span class="dot"></span>收集管理</h2>
        <div class="row">
          <label class="muted">窗口：<select id="cWinSel"></select></label>
          <button id="cAddBtn">➕ 手动添加歌曲</button>
          <button id="cArchiveBtn" class="btn-primary">📦 归档本窗口全部</button>
          <button id="cSyncAllBtn">🔄 同步全部歌单</button>
        </div>
        <p class="muted">在下方各群卡片里可编辑、手动匹配、调整顺序、删除，并对单个群「同步到歌单」（增+删+简介）。</p>
      </div>
      <div id="collectGroups"><div class="empty">加载中…</div></div>
    </section>

    <!-- 总库 -->
    <section id="page-master" class="page hidden">
      <div class="card">
        <h2><span class="dot"></span>总库（跨窗口去重）</h2>
        <div class="row">
          <button id="mAggBtn">📥 汇总现有窗口到总库</button>
          <button id="mArchiveBtn" class="btn-primary">📦 归档总库全部</button>
          <button id="mSyncBtn">🔄 同步全部歌单</button>
          <button id="mAddBtn">➕ 手动添加歌曲</button>
        </div>
        <p class="muted">总库把当前群里<strong>所有窗口</strong>的歌曲汇聚去重。有人分享了总库里已存在的歌时，会在群里提示（提示开关与文案在「配置」页的「总库」分组里设置）。下面可对总库做编辑、匹配、拖拽排序、删除，并归档 / 同步到独立的<strong>总库网易云歌单</strong>（命名 / 简介 / 期号等配置同样在「配置」页设置）。</p>
      </div>
      <div id="masterGroups"><div class="empty">加载中…</div></div>
    </section>

    <!-- 配置 -->
    <section id="page-config" class="page hidden">
      <div id="configForm"></div>
    </section>

    <!-- 昵称映射 -->
    <section id="page-aliases" class="page hidden">
      <div class="card">
        <h2><span class="dot"></span>分享者昵称映射</h2>
        <p class="muted">每行一条 <code>原昵称=显示名</code> 或 <code>QQ号码=显示名</code>。仅展示层替换，入库仍保留原始昵称。</p>
        <textarea id="aliasInput" placeholder="菜老名=Jacksing&#10;123456789=Jacksing"></textarea>
      </div>
      <div class="card">
        <h2><span class="dot"></span>实时预览</h2>
        <ul class="pv" id="aliasPreview"><li class="empty">（暂无映射）</li></ul>
      </div>
    </section>

    <!-- 管理员 -->
    <section id="page-admin" class="page hidden">
      <div class="card">
        <h2><span class="dot"></span>管理员（SUPERUSERS）</h2>
        <p class="muted">每行一个 QQ 号。修改后需<strong>重启 bot</strong> 才能生效（nonebot 在启动时读取 SUPERUSERS）。</p>
        <textarea id="suInput" placeholder="2531546239&#10;123456789"></textarea>
        <div class="row end" style="margin-top:12px">
          <button id="suSave" class="btn-primary">保存管理员</button>
        </div>
        <p class="muted" id="suNote"></p>
      </div>
    </section>

    <!-- 网易云账号 -->
    <section id="page-account" class="page hidden">
      <div class="card">
        <h2><span class="dot"></span>网易云账号登录</h2>
        <div id="accStatus" class="muted">加载中…</div>
        <div id="accLogin" class="hidden" style="margin-top:14px">
          <p class="muted">粘贴浏览器 Cookie 里的 <code>MUSIC_U=xxxx</code>（只要 xx 部分也行）。建议私聊机器人用 <code>/music cookie</code> 设置。</p>
          <input id="accCookie" placeholder="MUSIC_U=xxxx 或仅 xxxx">
          <div class="row end" style="margin-top:12px">
            <button id="accLogout" class="btn-danger">退出登录</button>
            <button id="accLoginBtn" class="btn-primary">登录</button>
          </div>
        </div>
      </div>
    </section>

  </main>
</div>

<!-- 底部保存条（仅配置页显示） -->
<div class="footbar hidden" id="footbar">
  <span class="count" id="dirtyCount"></span>
  <span class="msg" id="saveMsg"></span>
  <button id="resetBtn">重置改动</button>
  <button id="saveBtn" class="btn-primary">保存更改</button>
</div>

<!-- 预览抽屉 -->
<div class="pv-mask hidden" id="pvMask"></div>
<aside class="pv-drawer" id="pvDrawer" aria-hidden="true">
  <div class="pv-head">
    <h3>🎵 本期预览 <span class="muted" id="pvWin"></span></h3>
    <button id="pvClose" title="关闭">✕</button>
  </div>
  <div class="pv-body" id="pvBody">加载中…</div>
</aside>

<!-- 编辑弹窗 -->
<div class="modal hidden" id="editModal">
  <div class="box">
    <h3>编辑歌曲</h3>
    <div class="fld"><label>歌名</label><input id="edTitle"></div>
    <div class="fld"><label>歌手</label><input id="edArtists"></div>
    <div class="fld"><label>分享者昵称</label><input id="edSharer"></div>
    <div class="fld"><label>分享者 QQ 号</label><input id="edSharerId" type="number"></div>
    <div class="fld"><label>原链接（来源平台）</label><input id="edUrl" placeholder="如 QQ音乐 / 酷狗 / 网易云分享链接"></div>
    <div class="fld"><label>匹配链接（网易云）</label><input id="edNetease" placeholder="https://music.163.com/song?id=..."></div>
    <p class="hint">修改「匹配链接」会按新链接重新匹配（歌名/歌手/专辑更新为匹配结果）；留空或不变则不重新匹配。</p>
    <div class="row"><button id="edCancel">取消</button><button id="edSave" class="btn-primary">保存</button></div>
  </div>
</div>

<!-- 手动匹配弹窗 -->
<div class="modal hidden" id="matchModal">
  <div class="box">
    <h3>手动匹配网易云歌曲</h3>
    <p>粘贴正确的网易云歌曲链接（或分享短链），将其绑定为这首歌的正确版本。</p>
    <div class="fld"><label>网易云链接</label><input id="mtLink" placeholder="https://music.163.com/song?id=2692690431"></div>
    <div class="row"><button id="mtCancel">取消</button><button id="mtSave" class="btn-primary">绑定</button></div>
  </div>
</div>

<!-- 手动添加弹窗 -->
<div class="modal hidden" id="addModal">
  <div class="box">
    <h3>手动添加歌曲</h3>
    <div class="fld"><label>平台</label>
      <select id="adPlatform">
        <option value="netease">网易云音乐</option>
        <option value="qq">QQ音乐</option>
        <option value="kugou">酷狗音乐</option>
        <option value="kuwo">酷我音乐</option>
        <option value="qishui">汽水音乐</option>
        <option value="apple">Apple Music</option>
        <option value="bilibili">哔哩哔哩</option>
      </select>
    </div>
    <div class="fld"><label>歌曲 id（网易云为数字 id）</label><input id="adSongId"></div>
    <div class="fld"><label>歌名</label><input id="adTitle"></div>
    <div class="fld"><label>歌手</label><input id="adArtists"></div>
    <div class="fld"><label>分享者昵称</label><input id="adSharer" placeholder="手动添加"></div>
    <div class="fld"><label>分享者 QQ 号</label><input id="adSharerId" type="number" placeholder="0"></div>
    <div class="row"><button id="adCancel">取消</button><button id="adSave" class="btn-primary">添加</button></div>
  </div>
</div>

<!-- token 弹窗 -->
<div class="modal hidden" id="tokenModal">
  <div class="box">
    <h3>需要访问令牌</h3>
    <p>在服务器 .env 里设置 <code>MUSIC_WEBUI_TOKEN</code> 的值填到这里（首次启动未设置时，令牌会打印在机器人启动日志里）。</p>
    <input id="tokenInput" placeholder="粘贴令牌…" autocomplete="off">
    <button class="btn-primary" id="tokenOk" style="width:100%;margin-top:6px">进入</button>
  </div>
</div>

<script>
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
  const m=$("#saveMsg"); m.textContent=msg; m.className="msg"+(kind?(" "+kind):"");
}
function fmtDate(ts){
  if(ts==null || ts===0) return "—";
  const d = new Date(ts*1000);
  if(isNaN(d.getTime())) return "—";
  const p = n => String(n).padStart(2,"0");
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
async function doAction(body){
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
</script>
</body>
</html>
"""


ALIASES_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>分享者昵称映射 · 群音乐收集</title>
<style>
:root{
  --bg:#0b0f1a; --bg2:#111726; --card:rgba(255,255,255,.04); --card-bd:rgba(255,255,255,.08);
  --txt:#e8edf6; --muted:#8b97ad; --accent:#6ea8fe; --accent2:#a78bfa; --ok:#34d399; --bad:#f87171;
  --input:rgba(255,255,255,.06); --shadow:0 10px 30px rgba(0,0,0,.35);
}
[data-theme="light"]{
  --bg:#f4f6fb; --bg2:#ffffff; --card:rgba(20,30,60,.03); --card-bd:rgba(20,30,60,.1);
  --txt:#1a2233; --muted:#5b6678; --accent:#3b6fe0; --accent2:#7c5cf0; --ok:#0f9d63; --bad:#d8453b;
  --input:rgba(20,30,60,.05); --shadow:0 10px 30px rgba(20,30,60,.1);
}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  background:radial-gradient(1200px 600px at 80% -10%,rgba(110,168,254,.12),transparent),var(--bg);
  color:var(--txt);line-height:1.55;min-height:100vh}
.wrap{max-width:860px;margin:0 auto;padding:28px 20px 120px}
header{position:sticky;top:0;z-index:20;backdrop-filter:blur(14px);
  background:linear-gradient(var(--bg),rgba(11,15,26,.6));padding:14px 0;margin-bottom:18px;
  border-bottom:1px solid var(--card-bd)}
[data-theme="light"] header{background:linear-gradient(#fff,rgba(255,255,255,.7))}
.hrow{display:flex;align-items:center;gap:14px}
.hrow h1{font-size:19px;margin:0;font-weight:700}
.spacer{flex:1}
button{font:inherit;cursor:pointer;border:1px solid var(--card-bd);background:var(--input);color:var(--txt);
  border-radius:10px;padding:8px 14px;transition:.18s}
button:hover{border-color:var(--accent);transform:translateY(-1px)}
.btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#fff;font-weight:600}
a.back{color:var(--accent);text-decoration:none;font-size:14px}
.card{background:var(--card);border:1px solid var(--card-bd);border-radius:18px;padding:18px 20px;
  margin-bottom:16px;box-shadow:var(--shadow);backdrop-filter:blur(8px)}
.card h2{font-size:16px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
.card h2 .dot{width:8px;height:8px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2))}
.hint{color:var(--muted);font-size:13px;margin:0 0 12px}
textarea{width:100%;background:var(--input);border:1px solid var(--card-bd);color:var(--txt);
  border-radius:10px;padding:11px;font:14px/1.6 ui-monospace,monospace;resize:vertical;min-height:180px}
textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(110,168,254,.18)}
.pv{list-style:none;margin:0;padding:0}
.pv li{display:flex;align-items:center;gap:10px;padding:9px 12px;background:var(--input);
  border:1px solid var(--card-bd);border-radius:10px;margin-bottom:8px;font-size:14px}
.pv .from{font-weight:600}
.pv .arrow{color:var(--accent2)}
.pv .to{font-weight:700;color:var(--accent)}
.pv .empty{color:var(--muted);background:none;border:none;padding:4px 0}
.footbar{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;align-items:center;gap:14px;
  justify-content:center;padding:14px;background:linear-gradient(transparent,var(--bg) 40%)}
.footbar .msg{font-size:13px;color:var(--muted)}
.footbar .msg.ok{color:var(--ok)}
.footbar .msg.bad{color:var(--bad)}
.modal{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.55);backdrop-filter:blur(4px)}
.modal .box{background:var(--bg2);border:1px solid var(--card-bd);border-radius:18px;padding:26px;width:min(420px,92vw);box-shadow:var(--shadow)}
.modal h3{margin:0 0 6px}
.modal p{color:var(--muted);font-size:13px;margin:0 0 14px}
.modal input{width:100%;background:var(--input);border:1px solid var(--card-bd);color:var(--txt);border-radius:10px;padding:10px;font:inherit;margin-bottom:14px}
.hidden{display:none!important}
</style>
</head>
<body>
<header><div class="wrap hrow" style="padding-bottom:0;margin-bottom:0">
  <h1>✏ 分享者昵称映射</h1>
  <div class="spacer"></div>
  <button id="themeBtn" title="切换主题">🌓 主题</button>
  <a class="back" href="/music-admin">← 返回面板</a>
</div></header>

<div class="wrap">
  <div class="card">
    <h2><span class="dot"></span>映射规则</h2>
    <p class="hint">每行写一条 <code>原昵称=显示名</code> 或 <code>QQ号码=显示名</code>，例如 <code>菜老名=Jacksing</code> 或 <code>123456789=Jacksing</code>。
    保存后，<b>网易云简介 / 群内文字榜单 / WebUI 表格</b> 里对应的分享者名字都会替换成显示名，
    但数据库里仍保留原始昵称不变。昵称优先于 QQ 号码匹配；空行和以 <code>#</code> 开头的注释会被忽略。</p>
    <textarea id="aliasInput" placeholder="菜老名=Jacksing&#10;123456789=Jacksing&#10;# 一行一条，原昵称或QQ号码=显示名"></textarea>
  </div>

  <div class="card">
    <h2><span class="dot"></span>实时预览</h2>
    <ul class="pv" id="previewList"><li class="empty">（暂无映射）</li></ul>
  </div>
</div>

<div class="footbar">
  <span class="msg" id="saveMsg"></span>
  <button id="saveBtn" class="btn-primary">保存映射</button>
</div>

<div class="modal hidden" id="tokenModal">
  <div class="box">
    <h3>需要访问令牌</h3>
    <p>与主配置面板共用同一令牌（服务器 .env 里的 <code>MUSIC_WEBUI_TOKEN</code>，未设置时打印在启动日志）。</p>
    <input id="tokenInput" placeholder="粘贴令牌…" autocomplete="off">
    <button class="btn-primary" id="tokenOk" style="width:100%">进入</button>
  </div>
</div>

<script>
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
</script>
</body>
</html>
"""

