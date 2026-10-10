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
  /* 配置页吸顶栈：--cfg-stick 是顶栏高度；--cfg-bar-h 由 JS 量出「配置对象」条的实际高度，
     跳转栏据此紧贴在它下面，两条都跟着页面滚动 */
  --cfg-stick:57px; --cfg-bar-h:0px;
  color-scheme: dark;
}
[data-theme="light"]{
  --bg:#f4f6fb; --bg2:#ffffff; --card:rgba(20,30,60,.03); --card-bd:rgba(20,30,60,.1);
  --txt:#1a2233; --muted:#5b6678; --accent:#3b6fe0; --accent2:#7c5cf0; --ok:#0f9d63; --bad:#d8453b;
  --input:rgba(20,30,60,.05); --shadow:0 10px 30px rgba(20,30,60,.1); --side:#eef1f8;
  color-scheme: light;
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
[data-theme="dark"] input[type="date"]{color-scheme:dark}
[data-theme="dark"] input[type="date"]::-webkit-calendar-picker-indicator{filter:invert(.85);cursor:pointer}
/* 暗色主题下拉选项需显式配色，否则原生弹层文字与背景同色看不清 */
select option,select optgroup{background:var(--bg2);color:var(--txt)}
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
  border:1px solid var(--card-bd);color:var(--txt);font-weight:600}
/* ⚠️ 这里必须是 overflow:visible：一旦给 .content 设了非 visible 的 overflow，它就变成
   「最近的滚动容器」，而真正滚动的是 body —— 于是 .content 里的 position:sticky 全部失效
   （配置页的「配置对象」条与分组跳转栏会一滚就跑掉）。sidebar 之所以吸得住，就是因为它是
   .layout 的直接子元素、不在 .content 里。表格的横向滚动交给 .gcard 自己做。 */
.content{flex:1;padding:22px 24px 120px;overflow:visible}
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
/* overflow-x 是为了让过宽的表格在卡片内部横滚（.content 已不能当滚动容器用） */
.gcard{background:var(--input);border:1px solid var(--card-bd);border-radius:14px;padding:14px 16px;margin-bottom:14px;
  overflow-x:auto;-webkit-overflow-scrolling:touch}
.gtitle{font-size:15px;font-weight:600}.gtitle .cnt{font-size:12px;color:var(--muted);font-weight:400;margin-left:8px}
/* 群卡片可折叠：标题行永远可见（手机上默认收起，点一下展开本群并收起其他群） */
.gtitle{cursor:pointer;user-select:none;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.gtitle .caret{font-size:12px;color:var(--muted);transition:transform .18s;display:inline-block}
.gtitle .cnt{margin-left:0}
.gtitle .gsp{flex:1}
.gcard.collapsed .gbody{display:none}
.gcard.collapsed .caret{transform:rotate(-90deg)}
.gcard.focus{box-shadow:0 0 0 2px var(--accent2)}
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

/* 全局通知 toast + 处理中遮罩（同步等联网操作必现，不再依赖隐藏的 footbar） */
.toast{position:fixed;left:50%;top:18px;transform:translateX(-50%) translateY(-12px);
  z-index:300;max-width:90vw;padding:11px 18px;border-radius:12px;font-size:14px;line-height:1.4;
  background:var(--bg2);border:1px solid var(--card-bd);box-shadow:var(--shadow);
  opacity:0;pointer-events:none;transition:opacity .18s,transform .18s;text-align:center}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.ok{border-color:var(--ok);color:var(--ok)}
.toast.bad{border-color:var(--bad);color:var(--bad)}
.toast.wait{border-color:var(--accent2);color:var(--accent2)}
.busybar{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:300;
  padding:9px 16px;border-radius:22px;font-size:13px;background:var(--accent2);color:#fff;
  box-shadow:var(--shadow);display:flex;align-items:center;gap:9px}
.busybar .spin{width:14px;height:14px;border:2px solid rgba(255,255,255,.45);
  border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
button:disabled{opacity:.5;cursor:not-allowed}

/* 运行日志 */
.log-toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.log-toolbar select,.log-toolbar input{width:auto}
.log-toolbar input#logSearch{flex:1;min-width:180px}
.log-toolbar label.chk{font-size:13px;color:var(--muted);gap:6px}
.log-toolbar label.chk input{width:16px;height:16px}
.log-view{background:var(--input);border:1px solid var(--card-bd);border-radius:14px;padding:10px 12px;
  height:min(62vh,620px);overflow:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;line-height:1.6}
.log-line{white-space:pre-wrap;word-break:break-word;padding:3px 0;border-bottom:1px dashed var(--card-bd)}
.log-line:last-child{border-bottom:none}
.log-line .lt{color:var(--muted);margin-right:8px}
.log-line .ll{display:inline-block;min-width:64px;font-weight:600;margin-right:8px}
.log-line .ln{color:var(--muted);margin-right:8px}
.log-line .lex{display:block;margin:4px 0 2px;padding-left:10px;border-left:2px solid var(--bad);color:var(--bad);white-space:pre-wrap}
.log-lv-TRACE .ll,.log-lv-DEBUG .ll{color:var(--muted)}
.log-lv-INFO .ll{color:var(--accent)}
.log-lv-SUCCESS .ll{color:var(--ok)}
.log-lv-WARNING .ll{color:#eab308}
[data-theme="light"] .log-lv-WARNING .ll{color:#a16207}
.log-lv-ERROR .ll,.log-lv-CRITICAL .ll{color:var(--bad)}
.log-line.log-lv-ERROR,.log-line.log-lv-CRITICAL{background:rgba(248,113,113,.06)}

/* 配置页：吸顶栈（「配置对象」条 + 分组跳转栏），滚动时都跟着走 */
.cfg-bar{position:sticky;top:var(--cfg-stick);z-index:22;display:flex;align-items:center;gap:10px;
  flex-wrap:wrap;padding:8px 12px;margin-bottom:8px;background:var(--bg2);
  border:1px solid var(--card-bd);border-radius:14px;
  /* 第二层 0 0 0 8px 是页面底色的「包围环」，用来糊住它与跳转栏之间那 8px 缝隙，
     免得滚动时下面的内容从缝里穿过去（var(--bg) 与页面底色一致，平时看不出来） */
  box-shadow:var(--shadow),0 0 0 8px var(--bg)}
.cfg-bar .cb-label{font-size:13px;color:var(--muted);white-space:nowrap}
.cfg-bar select{width:auto;min-width:150px;padding:6px 10px}
.cfg-bar .cb-hint{font-size:12px;color:var(--muted)}
.cfg-bar .cb-badge{font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--card-bd);color:var(--muted)}

.cfg-nav{position:sticky;top:calc(var(--cfg-stick) + var(--cfg-bar-h));z-index:20;display:flex;flex-wrap:wrap;gap:8px;
  margin-bottom:14px;padding:10px 12px;background:var(--bg2);border:1px solid var(--card-bd);
  border-radius:14px;box-shadow:var(--shadow)}
.cfg-nav .cn-title{font-size:12px;color:var(--muted);align-self:center;padding-right:2px}
.cfg-nav-btn{background:var(--input);border:1px solid var(--card-bd);color:var(--muted);
  padding:5px 12px;border-radius:999px;font-size:13px;white-space:nowrap}
.cfg-nav-btn:hover{color:var(--txt)}
.cfg-nav-btn.active{color:var(--txt);font-weight:600;border-color:var(--accent);
  background:linear-gradient(135deg,rgba(110,168,254,.22),rgba(167,139,250,.22))}
.cfg-sec{scroll-margin-top:calc(var(--cfg-stick) + var(--cfg-bar-h) + 72px)}

/* 按群配置：被单独覆盖过的项高亮，并给一个「继承全局」的还原按钮 */
.field.overridden{border-left:3px solid var(--accent2);padding-left:11px;margin-left:-14px}
.field.overridden .flabel .ink{font-size:11px;color:var(--accent2);border:1px solid var(--accent2);
  border-radius:999px;padding:1px 7px;margin-left:6px}
.field.inherited .flabel .inh{font-size:11px;color:var(--muted);margin-left:6px}
.field .frow{display:flex;align-items:flex-start;gap:8px}
.field .frow .fctrl{flex:1;min-width:0}
.field .revert-btn{flex:0 0 auto;padding:6px 9px;font-size:12px;color:var(--muted)}
.field.global-only{opacity:.62}
.fctrl.disabled input,.fctrl.disabled select,.fctrl.disabled textarea{opacity:.65;cursor:not-allowed}

/* 网易云账号（内嵌在「配置 → 网易云登录」分组里） */
.acc-box{margin-top:14px;padding-top:14px;border-top:1px dashed var(--card-bd)}
.acc-box .acc-title{font-size:13px;color:var(--muted);margin-bottom:8px}
/* 风控人工验证提示（code=8810）：醒目但仍跟主题走 */
.acc-risk{margin-top:10px;padding:10px 12px;border:1px solid var(--bad);background:var(--input);border-radius:8px;line-height:1.7;word-break:break-all}
.acc-risk a{color:var(--accent);font-weight:600}

/* ---------------- 手机 / 小屏适配 ---------------- */
@media (max-width:900px){
  /* 手机上顶栏不再吸顶，把屏幕顶部让给吸顶的导航条；否则两者会在 top:0 打架 */
  .topbar{position:static;padding:10px 12px;gap:10px}
  .topbar h1{font-size:16px}
  /* 侧边栏改成顶部横向可滑动的导航条 */
  .layout{flex-direction:column;min-height:0}
  .sidebar{position:sticky;top:0;z-index:25;width:auto;flex:none;height:auto;
    display:flex;gap:8px;overflow-x:auto;overflow-y:hidden;padding:9px 10px;
    background:var(--side);border-right:none;border-bottom:1px solid var(--card-bd);
    -webkit-overflow-scrolling:touch;scrollbar-width:none}
  .sidebar::-webkit-scrollbar{display:none}
  .sidebar .brand{display:none}
  .nav{flex:0 0 auto;width:auto;margin-bottom:0;white-space:nowrap;padding:7px 13px;
    border:1px solid var(--card-bd);border-radius:999px;font-size:13px;background:var(--input)}
  .nav:hover{transform:none}
  .content{padding:14px 12px 110px}
  .page{max-width:100%}
  .card{padding:14px 14px;border-radius:16px}
  .card h2{font-size:15px}
  /* 配置项：标签与控件上下排布，窄屏更好点 */
  .field{grid-template-columns:1fr;gap:6px;padding:10px 0}
  .flabel{font-size:13px}
  /* 吸顶栈：导航条占掉顶部 ~54px，配置对象条与跳转栏依次贴在它下面 */
  :root{--cfg-stick:54px}
  .cfg-bar{gap:8px;padding:7px 10px}
  .cfg-bar select{min-width:120px}
  .cfg-nav{padding:9px 10px;gap:6px}
  .cfg-nav-btn{padding:5px 10px;font-size:12px}
  .stat-grid{grid-template-columns:repeat(auto-fit,minmax(120px,1fr))}
  /* 表格：窄屏把列撑开，横向滚动交给 .gcard 的通用规则 */
  .gtbl{min-width:600px}
  .log-view{height:min(58vh,520px);font-size:12px}
  .log-toolbar{gap:8px}
  .log-toolbar input#logSearch{flex:1 1 100%;min-width:0}
  .footbar{left:0;padding:10px 12px}
  .toast{top:auto;bottom:12px;transform:translateX(-50%) translateY(12px)}
  .toast.show{transform:translateX(-50%) translateY(0)}
}
@media (max-width:560px){
  .topbar h1{font-size:15px}
  .topbar button{padding:6px 10px;font-size:13px}
  .status-pill{display:none}         /* 超窄屏先舍掉状态胶囊，给标题和按钮腾地方 */
  .content{padding:12px 10px 120px}
  .stat-grid{grid-template-columns:1fr 1fr}
  .gtbl{min-width:0;font-size:12px}
  .gtbl .date{display:none}          /* 小屏先舍掉日期列，表格才放得下 */
  .gtbl th,.gtbl td{padding:6px 5px}
  .gtitle{font-size:14px}
  .pv-drawer{width:100vw}
  .pv-body{padding:14px 14px 26px}
}
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
    <button class="nav" data-page="logs">📜 运行日志</button>
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
          <button id="opArchiveAll" class="btn-primary" title="把当前窗口全部歌曲写入网易云歌单（新建或追加）；不删除窗口歌曲">📦 归档当前窗口全部</button>
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
          <button id="cAddBtn" title="手动录入一首歌（可填原平台链接与匹配后的网易云链接）">➕ 手动添加歌曲</button>
          <button id="cArchiveBtn" class="btn-primary" title="把本窗口全部歌曲写入网易云歌单（新建或追加）；不删除窗口歌曲">📦 归档本窗口全部</button>
          <button id="cSyncAllBtn" title="按各窗口当前歌曲对账歌单：补齐缺失、移除已删歌曲，不清除窗口">🔄 同步全部歌单</button>
        </div>
        <div class="row" style="margin-top:10px;gap:8px">
          <input id="cSearch" placeholder="搜索歌曲 / ID / 分享者…" style="flex:1;min-width:160px">
          <input id="cDate" type="date" title="按收录日期筛选（本地时区）" style="width:auto">
          <button id="cClearSearch" title="清除筛选条件">✕ 清除</button>
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
          <button id="mAggBtn" title="把该群所有窗口的歌曲汇聚去重进总库">📥 汇总现有窗口到总库</button>
          <button id="mArchiveBtn" class="btn-primary" title="把总库全部歌曲写入独立的「总库歌单」（新建或追加）">📦 归档总库全部</button>
          <button id="mSyncBtn" title="按总库当前歌曲对账总库歌单">🔄 同步全部歌单</button>
          <button id="mAddBtn" title="手动录入一首歌（可填原平台链接与匹配后的网易云链接）">➕ 手动添加歌曲</button>
        </div>
        <div class="row" style="margin-top:10px">
          <label class="muted">目标群：<select id="mImportGroup"></select></label>
          <input id="mImportUrl" placeholder="粘贴网易云歌单链接，如 https://music.163.com/#/playlist?id=123456" style="flex:1;min-width:200px" />
          <button id="mImportBtn" class="btn-primary" title="从网易云歌单链接批量导入歌曲到上方所选群的总库">📥 从歌单导入总库</button>
          <button id="mUndoBtn" title="撤回该群最近一次歌单导入（仅删除本次新加入总库的歌曲，不影响已生成的歌单）">↩ 撤回上次导入</button>
        </div>
        <div class="row" style="margin-top:10px;gap:8px">
          <input id="mSearch" placeholder="搜索总库歌曲 / ID / 分享者…" style="flex:1;min-width:160px">
          <input id="mDate" type="date" title="按收录日期筛选（本地时区）" style="width:auto">
          <button id="mClearSearch" title="清除筛选条件">✕ 清除</button>
        </div>
        <div id="mImportHistory" class="muted" style="margin-top:8px"></div>
        <p class="muted">总库把当前群里<strong>所有窗口</strong>的歌曲汇聚去重。有人分享了总库里已存在的歌时，会在群里提示（提示开关与文案在「配置」页的「总库」分组里设置）。下面可对总库做编辑、匹配、拖拽排序、删除，并归档 / 同步到独立的<strong>总库网易云歌单</strong>（命名 / 简介 / 期号等配置同样在「配置」页设置）。</p>
      </div>
      <div id="masterGroups"><div class="empty">加载中…</div></div>
    </section>

    <!-- 配置 -->
    <section id="page-config" class="page hidden">
      <div class="cfg-bar" id="cfgBar">
        <span class="cb-label">配置对象</span>
        <select id="cfgGroupSel" title="选择给「全局默认」还是某个群单独配置"></select>
        <span class="cb-badge" id="cfgScopeBadge">全局默认</span>
        <span class="cb-hint" id="cfgScopeHint"></span>
        <div class="spacer"></div>
        <button id="cfgResetAll" class="btn-danger hidden" title="把本群所有单独设过的项恢复成继承全局默认">↺ 全部恢复继承</button>
      </div>
      <nav id="cfgNav" class="cfg-nav hidden" aria-label="配置分组跳转"></nav>
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

    <!-- 网易云账号已并入「配置 → 网易云登录」分组，不再单开一页 -->

    <!-- 运行日志 -->
    <section id="page-logs" class="page hidden">
      <div class="card">
        <h2><span class="dot"></span>运行日志</h2>
        <div class="log-toolbar">
          <label class="muted">来源 <select id="logSource" title="按打出这条日志的模块筛选"></select></label>
          <label class="muted">级别 <select id="logLevel" title="只看该等级及以上"></select></label>
          <input id="logSearch" placeholder="搜索关键词（消息 / 模块名 / 异常堆栈）…">
          <label class="chk"><input type="checkbox" id="logAuto" checked> 自动刷新（3 秒）</label>
          <div class="spacer"></div>
          <button id="logRefresh">↻ 刷新</button>
          <button id="logCopy" title="把当前筛选结果复制到剪贴板">⧉ 复制</button>
          <button id="logClear" class="btn-danger" title="清空服务器内存里的日志缓冲（落盘文件不受影响）">🗑 清空</button>
        </div>
        <div id="logMeta" class="muted" style="margin-bottom:8px"></div>
        <div class="log-view" id="logView"><div class="empty">加载中…</div></div>
        <p class="muted" style="margin-top:10px">这里只显示<strong>本次进程启动后</strong>的日志（内存环形缓冲，重启即清空）。条目数、最低等级、是否额外落盘文件，在「配置」页的<strong>运行日志</strong>分组里改，保存后立即生效。<br>服务器上日志的大头是协议端（<code>nonebot</code>）与网页请求（<code>uvicorn</code>），想看机器人自己干了什么，把「来源」切到 <strong>🤖 只看机器人</strong> 即可。</p>
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

<!-- 全局通知（同步等联网操作的结果提示，常驻可见） -->
<div class="toast" id="toast"></div>
<div class="busybar hidden" id="busy"><span class="spin"></span><span>处理中…</span></div>

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
let LOG_TIMER = null, LOG_SEARCH_T = null, LOGS_LAST = [];
/* 配置页当前在编辑哪一层：null = 全局默认，数字 = 该群的覆盖层 */
let CFG_GID = null, CFG_OVER = new Set(), CFG_GROUPS = [];
/* 从概览「去管理 →」进来时要展开的目标群（渲染完群卡片后一次性消费掉） */
let FOCUS_GID = null;
const MASTER_KEY = "__master__";
/* 手机上群卡片默认折叠（桌面保持展开） */
const isNarrow = () => window.matchMedia("(max-width:900px)").matches;
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
  stopLogTimer();   // 离开日志页就停掉轮询
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
  if(name==="logs"){ loadLogs(); startLogTimer(); }
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
    gl.querySelectorAll("button[data-g]").forEach(b=> b.onclick=()=>{
      // 进入收集管理页后展开这个群、收起其他群（手机上尤其有用）
      FOCUS_GID = parseInt(b.dataset.g, 10);
      switchPage("collect");
    });
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
    const q = $("#cSearch").value.trim();
    const d = $("#cDate").value.trim();
    let url = "/api/music-admin/overview" + (CUR_WIN?("?window_key="+encodeURIComponent(CUR_WIN)):"");
    if(q) url += "&q="+encodeURIComponent(q);
    if(d) url += "&date="+encodeURIComponent(d);
    COLL = await (await api(url)).json();
    CUR_WIN = COLL.selected_window || (COLL.windows[0]&&COLL.windows[0].key) || null;
    fillWinSel($("#cWinSel"), CUR_WIN);
    const wrap=$("#collectGroups"); wrap.innerHTML="";
    const groups = COLL.groups||[];
    if(!groups.length){ wrap.innerHTML=`<div class="empty">该窗口下暂无收集记录。</div>`; return; }
    groups.forEach(g=> wrap.appendChild(renderGroupCard(g, COLL.selected_window)) );
    applyFocus(wrap);
  }catch(e){ if(e.message!=="unauthorized") console.warn("collect 加载失败", e); }
}
async function loadMaster(){
  try{
    const q = $("#mSearch").value.trim();
    const d = $("#mDate").value.trim();
    let url = "/api/music-admin/overview?scope=master";
    if(q) url += "&q="+encodeURIComponent(q);
    if(d) url += "&date="+encodeURIComponent(d);
    MASTER = await (await api(url)).json();
    const wrap=$("#masterGroups"); wrap.innerHTML="";
    const groups = MASTER.groups||[];
    if(!groups.length){ wrap.innerHTML=`<div class="empty">总库还是空的。分享歌曲（启用总库后）或点上方「📥 汇总现有窗口到总库」即可填充。</div>`; }
    else { groups.forEach(g=> wrap.appendChild(renderGroupCard(g, MASTER_KEY)) ); applyFocus(wrap); }
    fillImportGroup();
  }catch(e){ if(e.message!=="unauthorized") console.warn("master 加载失败", e); }
}
function fillImportGroup(){
  const sel=$("#mImportGroup"); if(!sel) return;
  const groups=(MASTER&&MASTER.import_groups)||[];
  const def = (MASTER&&MASTER.groups&&MASTER.groups[0]&&MASTER.groups[0].group_id) || (groups[0]||0);
  sel.innerHTML="";
  (groups.length?groups:[def]).forEach(gid=>{
    const op=document.createElement("option"); op.value=gid; op.textContent="群 "+gid; sel.appendChild(op);
  });
  if(def) sel.value=def;
  renderImportHistory();
}
function renderImportHistory(){
  const box=$("#mImportHistory"); if(!box) return;
  const sel=$("#mImportGroup"); if(!sel) return;
  const gid=parseInt(sel.value,10);
  const hist=((MASTER&&MASTER.import_history)||[]).filter(h=>h.group_id===gid);
  if(!hist.length){ box.innerHTML=`<span class="muted">该群暂无歌单导入记录。</span>`; return; }
  box.innerHTML=`<div style="margin-bottom:4px">导入历史（本群，撤回仅删除本次新加入总库的歌）：</div>`+
    hist.map(h=>{
      const t=fmtDate(h.created_at);
      const pid=h.playlist_id?`歌单 ${h.playlist_id}`:"";
      const undone=h.undone?` <span style="color:var(--muted)">(已撤回)</span>`:"";
      const btn=h.undone?"":`<button data-undo="${h.id}" style="padding:2px 8px;font-size:12px;margin-left:6px">撤回</button>`;
      return `<div style="margin:3px 0">${t} · ${pid} · 新增 ${h.added}/${h.total} 首 ${undone} ${btn}</div>`;
    }).join("");
  box.querySelectorAll("button[data-undo]").forEach(b=> b.onclick=()=>{
    doAction({action:"undo_master_import", group_id:gid, history_id:parseInt(b.dataset.undo,10)});
  });
}
// 区选（Shift+点击）：记录上次点击的复选框，Shift 点击时把两者之间的范围一并选中/取消
let LAST_CHK = null;
function onChkClick(e, chk, gid){
  const card = chk.closest(".gcard");
  const all = Array.from(card.querySelectorAll(`.songchk[data-g="${gid}"]`));
  if (e && e.shiftKey && LAST_CHK && all.indexOf(LAST_CHK) >= 0){
    const a = all.indexOf(LAST_CHK), b = all.indexOf(chk);
    const [lo, hi] = a < b ? [a, b] : [b, a];
    for (let k = lo; k <= hi; k++) all[k].checked = chk.checked;
  }
  LAST_CHK = chk;
  const selall = card.querySelector(`input.selall[data-g="${gid}"]`);
  if (selall) selall.checked = all.length > 0 && all.every(c=> c.checked);
}
/* 群卡片折叠：手机上默认收起（列表很长时不至于刷屏），桌面保持展开。
   点某个群标题 → 展开它并收起同容器里的其他群（手风琴），避免手机上要找半天。 */
function setCardCollapsed(card, collapsed){
  card.classList.toggle("collapsed", !!collapsed);
  const caret = card.querySelector(".gtitle .caret");
  if(caret) caret.textContent = collapsed ? "▸" : "▾";
}
function toggleGroupCard(card, exclusive){
  const collapsed = !card.classList.contains("collapsed");
  if(exclusive && !collapsed){
    const wrap = card.parentElement;
    Array.from(wrap.children).forEach(c=>{ if(c!==card && c.classList.contains("gcard")) setCardCollapsed(c, true); });
  }
  setCardCollapsed(card, collapsed);
}
/* 从概览「去管理 →」进来：展开目标群、收起其他群，并滚到它 */
function applyFocus(wrap){
  if(FOCUS_GID===null) return;
  const gid = FOCUS_GID; FOCUS_GID = null;
  const cards = Array.from(wrap.querySelectorAll(".gcard"));
  if(!cards.length) return;
  cards.forEach(c=>{
    const hit = parseInt(c.dataset.gid,10)===gid;
    setCardCollapsed(c, !hit);
    c.classList.toggle("focus", hit);
    if(hit) c.scrollIntoView({behavior:"smooth", block:"start"});
  });
}
function renderGroupCard(g, wk){
  const card=document.createElement("div"); card.className="gcard"; card.dataset.wk=wk||""; card.dataset.gid=g.group_id;
  const pl = g.playlist_url
    ? `<a class="plink" href="${esc(g.playlist_url)}" target="_blank" rel="noreferrer">🔗 网易云歌单</a>`
    : `<span class="muted">（本窗口尚未建歌单，归档或同步后在此显示）</span>`;
  const ops=`<div class="row" style="margin-bottom:6px;gap:6px">
    <button data-sel="all" data-g="${g.group_id}" title="全选本群所有歌曲">☑ 全选</button>
    <button data-sel="inv" data-g="${g.group_id}" title="反选本群已勾选状态">⧉ 反选</button>
    <button data-sel="none" data-g="${g.group_id}" title="清除本群已选中的歌曲">⌫ 清除</button>
    <span class="muted" style="font-size:12px;align-self:center">按住 Shift 点复选框可<span style="color:var(--accent)">区选</span>一段</span>
  </div>
  <div class="row" style="margin-bottom:6px">
    <button data-act="preview" data-g="${g.group_id}" title="预览本窗口歌单样式与简介清单">👁 预览</button>
    <button data-act="archive" data-g="${g.group_id}" title="把本窗口歌曲归档/追加到网易云歌单；若配置「归档后清空」会清空本期，用于一期结束定稿">📦 归档本群</button>
    <button class="btn-primary" data-act="sync" data-g="${g.group_id}" title="让网易云歌单与当前窗口完全一致：窗口有而歌单无的加入，歌单有而窗口已删的移除；不清除本期">🔄 同步到歌单</button>
    <button data-act="aggregate_window_to_master" data-g="${g.group_id}" title="把当前窗口本群歌曲去重汇总进总库（不影响当前窗口），用于跨窗口/跨期累积成总歌单">📥 汇总到总库</button>
    <button data-act="del" data-g="${g.group_id}" class="btn-danger" title="删除选中的歌曲（从窗口移除，不影响歌单）">删除选中</button>
    <button data-act="clear" data-g="${g.group_id}" class="btn-danger" title="清空本窗口全部歌曲（不删歌单）">清空本窗口</button>
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
    <th style="width:34px"><input type="checkbox" class="selall" data-g="${g.group_id}" title="全选 / 取消全选本群"></th><th>#</th><th>歌曲 / 歌手（可拖拽行排序）</th><th>分享者</th><th>平台</th><th>收录日期</th><th>匹配</th><th></th>
  </tr></thead><tbody>${rows}</tbody></table>`;
  card.innerHTML=`<div class="gtitle" title="点击展开 / 收起本群">
      <span class="caret">▾</span><span>群 ${g.group_id}</span><span class="cnt">${g.count} 首</span>
    </div><div class="gbody">${ops}${tbl}</div>`;
  const head = card.querySelector(".gtitle");
  if(head) head.onclick = (e)=>{
    if(e.target.closest("button,input,a")) return;   // 标题行里的按钮不触发折叠
    toggleGroupCard(card, isNarrow());
  };
  if(isNarrow()) setCardCollapsed(card, true);       // 手机上默认全部收起
  card.querySelectorAll("button[data-act]").forEach(b=> b.onclick=()=>groupAction(b.dataset.act,b.dataset.g, wk));
  card.querySelectorAll("button[data-mv]").forEach(b=> b.onclick=()=>moveRow(g.group_id, parseInt(b.dataset.idx,10), parseInt(b.dataset.mv,10), wk));
  card.querySelectorAll("button[data-edit]").forEach(b=> b.onclick=()=>openEdit(g.group_id, parseInt(b.dataset.idx,10), wk));
  card.querySelectorAll("button[data-match]").forEach(b=> b.onclick=()=>openMatch(g.group_id, parseInt(b.dataset.idx,10), wk));
  // 选择：全选 / 反选 / 清除 / Shift 区选
  const selall = card.querySelector(`input.selall[data-g="${g.group_id}"]`);
  const chks = ()=> Array.from(card.querySelectorAll(`.songchk[data-g="${g.group_id}"]`));
  if (selall) selall.onchange = ()=>{ chks().forEach(c=> c.checked = selall.checked); };
  card.querySelectorAll('button[data-sel]').forEach(b=> b.onclick=()=>{
    const mode = b.dataset.sel;
    const all = chks();
    all.forEach(c=> c.checked = mode==="all" ? true : mode==="none" ? false : !c.checked);
    if (selall) selall.checked = all.length>0 && all.every(c=>c.checked);
  });
  chks().forEach(chk=> chk.addEventListener("click", e=> onChkClick(e, chk, g.group_id)));
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
  if(act==="delete"){
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

/* 搜索框：关键词实时过滤（防抖）+ 日期筛选；清除按钮重置两者 */
function bindSearch(inputId, dateId, clearId, reloadFn){
  const inp=$(inputId), dt=$(dateId), clr=$(clearId);
  let t=null;
  inp.addEventListener("input", ()=>{ clearTimeout(t); t=setTimeout(reloadFn, 250); });
  dt.addEventListener("change", reloadFn);
  clr.onclick=()=>{ inp.value=""; dt.value=""; reloadFn(); };
}
bindSearch("#cSearch","#cDate","#cClearSearch", loadCollect);
bindSearch("#mSearch","#mDate","#mClearSearch", loadMaster);

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
$("#mImportGroup").onchange=renderImportHistory;
$("#mImportBtn").onclick=async()=>{
  const gid=parseInt($("#mImportGroup").value,10)||0;
  const url=($("#mImportUrl").value||"").trim();
  if(!url){ flashOp("请输入网易云歌单链接","bad"); return; }
  await doAction({action:"import_playlist_master", group_id:gid, url});
  $("#mImportUrl").value="";
};
$("#mUndoBtn").onclick=async()=>{
  const gid=parseInt($("#mImportGroup").value,10)||0;
  await doAction({action:"undo_master_import", group_id:gid});
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
/* 吸顶栈：--cfg-stick 是「页面顶部已经被谁占掉多少」（宽屏=顶栏，窄屏=吸顶的横向导航条），
   --cfg-bar-h 是「配置对象」条的高度 + 它与跳转栏之间的 8px 缝，跳转栏据此紧贴在它下面。
   两个值都实测，避免写死高度在小屏/缩放时对不齐。 */
function syncStickyOffsets(){
  const root=document.documentElement.style;
  const tb=document.querySelector(".topbar"), sb=document.querySelector(".sidebar");
  const stick = isNarrow()
    ? (sb ? sb.getBoundingClientRect().height : 0)      /* 窄屏：顶栏不吸顶，占位的是横向导航条 */
    : (tb ? tb.getBoundingClientRect().height : 0);     /* 宽屏：顶栏吸顶 */
  if(stick>0) root.setProperty("--cfg-stick", Math.round(stick)+"px");
  const bar=$("#cfgBar");
  const h = bar && !bar.classList.contains("hidden") ? bar.offsetHeight : 0;
  root.setProperty("--cfg-bar-h", (h+8)+"px");
}
window.addEventListener("resize", ()=>{ syncStickyOffsets(); if($("#cfgGroupSel")) buildGroupSelOptions(); });

function fieldControl(f, value, disabled){
  const wrap=document.createElement("div"); wrap.className="fctrl";
  if(disabled) wrap.classList.add("disabled");
  if(f.type==="bool"){
    const id="f_"+f.key.replace(/\./g,"_"); const lbl=document.createElement("label"); lbl.className="chk";
    const cb=document.createElement("input"); cb.type="checkbox"; cb.id=id; cb.checked=!!value;
    if(!disabled) cb.onchange=()=>markDirty(f.key, cb.checked);
    const sp=document.createElement("span"); sp.textContent=f.label; lbl.appendChild(cb); lbl.appendChild(sp); wrap.appendChild(lbl);
  } else if(f.type==="enum"){
    const sel=document.createElement("select");
    (f.enum||[]).forEach(o=>{const op=document.createElement("option");op.value=o;op.textContent=o;sel.appendChild(op);});
    sel.value=value??""; if(!disabled) sel.onchange=()=>markDirty(f.key,sel.value); wrap.appendChild(sel);
  } else if(f.type==="int"||f.type==="float"){
    const inp=document.createElement("input"); inp.type="number"; inp.value=value??"";
    inp.step = f.type==="int"?"1":"any"; if(!disabled) inp.oninput=()=>markDirty(f.key,inp.value); wrap.appendChild(inp);
  } else if(f.type==="intlist"||f.type==="strlist"){
    const inp=document.createElement("input"); inp.type="text";
    inp.value=Array.isArray(value)?value.join(", "):(value??""); inp.placeholder="逗号分隔";
    if(!disabled) inp.oninput=()=>markDirty(f.key,inp.value); wrap.appendChild(inp);
  } else {
    if(f.multiline){ const ta=document.createElement("textarea"); ta.value=value??""; if(!disabled) ta.oninput=()=>markDirty(f.key,ta.value); wrap.appendChild(ta); }
    else { const inp=document.createElement("input"); inp.type=f.secret?"password":"text"; inp.value=value??""; if(!disabled) inp.oninput=()=>markDirty(f.key,inp.value); wrap.appendChild(inp); }
  }
  if(disabled) wrap.querySelectorAll("input,select,textarea").forEach(x=>x.disabled=true);
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

/* 「配置对象」下拉：全局默认 + 各群（群号排序） */
function buildGroupSelOptions(){
  const sel=$("#cfgGroupSel"); if(!sel) return;
  const keep = CFG_GID===null ? "" : String(CFG_GID);
  sel.innerHTML = "";
  const op0=document.createElement("option"); op0.value=""; op0.textContent="全局默认（所有群）"; sel.appendChild(op0);
  CFG_GROUPS.forEach(g=>{
    const op=document.createElement("option"); op.value=String(g);
    const over=(CFG_OVER_BY_GROUP[String(g)]||[]).length;
    op.textContent = `群 ${g}` + (over?`（已单独配置 ${over} 项）`:"");
    sel.appendChild(op);
  });
  sel.value = (keep && CFG_GROUPS.some(g=>String(g)===keep)) ? keep : "";
  if(sel.value!==keep) CFG_GID = sel.value==="" ? null : parseInt(sel.value,10);
}
let CFG_OVER_BY_GROUP = {};

function renderForm(schema, values, meta){
  meta = meta || {};
  ORIG=Object.assign({},values); DIRTY={};
  CFG_OVER = new Set(meta.overridden||[]);
  const isGroup = CFG_GID!==null;
  const form=$("#configForm"); form.innerHTML="";
  const nav=$("#cfgNav"); nav.innerHTML="";
  const navItems=[];
  schema.forEach((sec,si)=>{
    const card=document.createElement("section"); card.className="card cfg-sec";
    card.id="cfg-sec-"+si;
    const h=document.createElement("h2"); h.innerHTML=`<span class="dot"></span>${sec.title}`; card.appendChild(h);
    sec.fields.forEach(f=>{
      if(f.type==="map") return;
      const globalOnly = !!f.global_only;
      const locked = isGroup && globalOnly;
      const overridden = isGroup && CFG_OVER.has(f.key);
      const fr=document.createElement("div");
      fr.className="field"+(overridden?" overridden":"")+(locked?" global-only":"");
      fr.dataset.key=f.key;
      const lab=document.createElement("div"); lab.className="flabel";
      let extra = "";
      if(overridden) extra += `<span class="ink">本群自定义</span>`;
      else if(isGroup && !locked) extra += `<span class="inh">继承全局</span>`;
      if(locked) extra = `<span class="inh">进程级设置，始终取全局值</span>`;
      lab.innerHTML=`${f.label}${extra}${f.hint?`<span class="hint">${f.hint}</span>`:""}`;
      const row=document.createElement("div"); row.className="frow";
      const ctrl=fieldControl(f, values[f.key], locked);
      row.appendChild(ctrl);
      if(overridden){
        const rb=document.createElement("button"); rb.type="button";
        rb.className="revert-btn"; rb.textContent="↺ 继承全局";
        rb.title="删掉本项的本群自定义值，恢复继承全局默认";
        rb.onclick=()=>revertOne(f.key);
        row.appendChild(rb);
      }
      fr.appendChild(lab); fr.appendChild(row); card.appendChild(fr);
    });
    // 网易云登录分组：把原先独立的「网易云账号」页内嵌到这里
    if(sec.key==="netease") card.appendChild(buildAccountBlock());
    form.appendChild(card);
    // 顶部跳转栏：分组多、页面长，点一下直接滚到对应分组
    const btn=document.createElement("button");
    btn.type="button"; btn.className="cfg-nav-btn"; btn.textContent=sec.title;
    btn.title="跳到「"+sec.title+"」";
    btn.onclick=()=>card.scrollIntoView({behavior:"smooth",block:"start"});
    nav.appendChild(btn); navItems.push({btn,card});
  });
  if(navItems.length>1){
    const tip=document.createElement("span"); tip.className="cn-title"; tip.textContent="跳转";
    nav.insertBefore(tip, nav.firstChild);
  }
  nav.classList.toggle("hidden", navItems.length<2);
  CFG_NAV=navItems;
  refreshDirty();
  syncCfgNav();
  syncStickyOffsets();
  updateScopeBar();
}

function updateScopeBar(){
  const badge=$("#cfgScopeBadge"), hint=$("#cfgScopeHint"), btn=$("#cfgResetAll");
  if(CFG_GID===null){
    badge.textContent="全局默认";
    hint.textContent="这里的改动对所有「没有单独配置过」的群生效";
    btn.classList.add("hidden");
  } else {
    badge.textContent="群 "+CFG_GID;
    hint.textContent = CFG_OVER.size
      ? `已单独配置 ${CFG_OVER.size} 项，其余继承全局默认`
      : "尚未单独配置，全部继承全局默认";
    btn.classList.toggle("hidden", CFG_OVER.size===0);
  }
  const sel=$("#cfgGroupSel");
  if(sel) sel.value = CFG_GID===null ? "" : String(CFG_GID);
}

/* 网易云账号：状态 + 重新登录 + 粘贴 MUSIC_U 登录 / 退出 */
function buildAccountBlock(){
  const box=document.createElement("div"); box.className="acc-box";
  box.innerHTML=`
    <div class="acc-title">网易云账号</div>
    <div id="accStatus" class="muted">加载中…</div>
    <div class="row end" style="margin-top:12px">
      <button id="accRelogin" title="优先用配置里的手机号+密码换一套全新 cookie；换不到时退回 cookie 续期">重新登录</button>
    </div>
    <p class="muted" style="margin-top:8px">开启「掉登录自动重登」后，写歌单简介遇到「需要登录」会自动续期 / 重登并重试；上面这个按钮是手动触发一次（优先账密换新 cookie，并告诉你指纹有没有变）。手机号密码填在本分组里。</p>
    <p class="muted" style="margin-top:6px">⚠️ 若提示 <code>code=8810 网络环境存在安全风险</code>，说明本机 IP 被网易云风控、账密重登会被拒；此时下方会给出<b>人工验证链接</b>（网易易盾），在浏览器打开过一遍验证后点「重新登录」即可。实在换不掉时，仍可粘贴浏览器登录后的 <code>MUSIC_U</code>。简介报 <code>code=405 操作频繁</code> 则是写接口频控，与登录态无关，等一会儿会自动补写。</p>
    <div id="accLogin" class="hidden" style="margin-top:14px">
      <p class="muted">粘贴浏览器 Cookie 里的 <code>MUSIC_U=xxxx</code>（只要 xx 部分也行）。建议私聊机器人用 <code>/music cookie</code> 设置。</p>
      <input id="accCookie" placeholder="MUSIC_U=xxxx 或仅 xxxx">
      <div class="row end" style="margin-top:12px">
        <button id="accLogout" class="btn-danger">退出登录</button>
        <button id="accLoginBtn" class="btn-primary">登录</button>
      </div>
    </div>`;
  // 元素建好后再绑事件（这几个按钮每次重建表单都要重新绑一次）
  box.querySelector("#accLoginBtn").onclick=onAccLogin;
  box.querySelector("#accLogout").onclick=onAccLogout;
  box.querySelector("#accRelogin").onclick=onAccRelogin;
  setTimeout(loadAccount, 0);
  return box;
}

/* 滚动时高亮当前所在的分组 */
let CFG_NAV=[];
function syncCfgNav(){
  if(!CFG_NAV.length) return;
  /* 判定线取「顶栏 + 跳转栏」下沿再往下一点，而不是写死 130px，小屏/缩放时才不会串位 */
  const cs=getComputedStyle(document.documentElement);
  const stack=(parseFloat(cs.getPropertyValue("--cfg-stick"))||0)+(parseFloat(cs.getPropertyValue("--cfg-bar-h"))||0);
  const line=stack+78;
  let active=0;
  CFG_NAV.forEach((it,i)=>{ if(it.card.getBoundingClientRect().top<=line) active=i; });
  CFG_NAV.forEach((it,i)=>it.btn.classList.toggle("active", i===active));
}
window.addEventListener("scroll",()=>{ if(!$("#page-config").classList.contains("hidden")) syncCfgNav(); },{passive:true});

function cfgQuery(){ return CFG_GID===null ? "" : "?group_id="+encodeURIComponent(CFG_GID); }
async function loadConfig(){
  try{
    const q=cfgQuery();
    const [c,s,g]=await Promise.all([
      api("/api/music-admin/config"+q),
      api("/api/music-admin/status"+q),
      api("/api/music-admin/groups"),
    ]);
    const cj=await c.json(); renderForm(cj.schema, cj.values, cj);
    const sj=await s.json();
    $("#statusPill").textContent = sj.collecting?"● 收集中":"○ 空闲";
    $("#statusPill").className="status-pill"+(sj.collecting?" on":"");
    // 群列表与「各群覆盖了几项」取自 groups 接口，下拉里才能标出「已单独配置 N 项」
    let gj={};
    try{ gj=await g.json(); }catch(e){ gj={}; }
    const gids = (gj.groups||[]).length ? gj.groups : (cj.groups||[]);
    if(gids.length || CFG_GROUPS.length===0) CFG_GROUPS = gids;
    CFG_OVER_BY_GROUP = gj.overrides||{};
    buildGroupSelOptions();
    updateScopeBar();
    flashOp("");
  }catch(e){ if(e.message!=="unauthorized") flashOp("加载失败: "+e.message,"bad"); }
}
async function saveConfig(){
  const scope = CFG_GID===null ? "全局默认" : ("群 "+CFG_GID);
  setMsg(`保存到「${scope}」中…`);
  try{
    const r=await api("/api/music-admin/config",{method:"PATCH",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({values:DIRTY, group_id:CFG_GID})});
    const j=await r.json();
    if(!j.ok){ const msgs=Object.entries(j.errors||{}).map(([k,v])=>`${k}: ${v}`).join("；"); setMsg("保存失败 — "+msgs,"bad"); return; }
    setMsg("已保存 ✓","ok"); await loadConfig();
  }catch(e){ setMsg("保存失败: "+e.message,"bad"); }
}
/* 取消某一项的本群覆盖，恢复继承全局默认 */
async function revertOne(key){
  if(CFG_GID===null) return;
  try{
    const j=await (await api("/api/music-admin/config",{method:"PATCH",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({group_id:CFG_GID, reset:[key]})})).json();
    if(!j.ok){ toast("恢复失败："+JSON.stringify(j.errors||{}),"bad"); return; }
    toast("已恢复继承全局默认","ok");
    await loadConfig();
  }catch(e){ toast("恢复失败："+e.message,"bad"); }
}
async function revertAll(){
  if(CFG_GID===null || !CFG_OVER.size) return;
  if(!confirm(`确定把「群 ${CFG_GID}」单独配置过的 ${CFG_OVER.size} 项全部恢复成继承全局默认？`)) return;
  try{
    const j=await (await api("/api/music-admin/config",{method:"PATCH",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({group_id:CFG_GID, reset:Array.from(CFG_OVER)})})).json();
    if(!j.ok){ toast("恢复失败："+JSON.stringify(j.errors||{}),"bad"); return; }
    toast("已全部恢复继承全局默认","ok");
    await loadConfig();
  }catch(e){ toast("恢复失败："+e.message,"bad"); }
}
function setMsg(t,kind=""){ const m=$("#saveMsg"); m.textContent=t; m.className="msg"+(kind?(" "+kind):""); }
$("#saveBtn").onclick=saveConfig;
$("#resetBtn").onclick=()=>{ DIRTY={}; document.querySelectorAll(".fctrl.dirty").forEach(c=>c.classList.remove("dirty")); refreshDirty(); setMsg("已重置本地改动"); };
$("#cfgResetAll").onclick=revertAll;
$("#cfgGroupSel").onchange=async (e)=>{
  const v=e.target.value;
  if(Object.keys(DIRTY).length && !confirm("当前有未保存的改动，切换配置对象会丢弃它们，继续？")){
    updateScopeBar(); return;
  }
  DIRTY={};
  CFG_GID = v==="" ? null : parseInt(v,10);
  await loadConfig();
};

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

/* ---- 网易云账号（内嵌在「配置 → 网易云登录」分组里） ---- */
async function loadAccount(){
  try{ const j=await (await api("/api/music-admin/account")).json(); renderAccount(j); }
  catch(e){ if(e.message!=="unauthorized") console.warn("account 加载失败",e); }
}
function renderAccount(j){
  const box=$("#accStatus"); if(!box) return;   // 配置页还没渲染时不报错
  const login=$("#accLogin");
  /* cookie 指纹（MUSIC_U 短哈希）：点完「重新登录」后对比它就知道 cookie 有没有真被换掉 */
  const fp = j.cookie_fp ? `　cookie 指纹：<code title="MUSIC_U 的短哈希，用来判断重登后 cookie 有没有真的换掉">${esc(j.cookie_fp)}</code>` : "";
  /* 风控人工验证链接（code=8810）：账密登录被拒时网易云会给一个易盾验证页， */
  /* 在浏览器打开过一遍验证即可放行本机 IP，然后再点「重新登录」。 */
  const risk = j.risk_url ? `<div class="acc-risk">⚠️ 本机 IP 被网易云风控（<code>code=8810</code>），账密登录被拒。<br>请<b>在浏览器打开</b>下面的链接完成人工验证，再点一次「重新登录」：<br><a href="${esc(j.risk_url)}" target="_blank" rel="noopener">${esc(j.risk_url)}</a></div>` : "";
  if(j.valid){ box.innerHTML=`<span class="badge ok">已登录</span> 昵称：<b>${esc(j.nickname||"")}</b>　userId：${esc(j.userId||"")}${fp}${risk}`; if(login) login.classList.add("hidden"); }
  else if(j.logged_in){ box.innerHTML=`<span class="badge bad">凭证存在但已失效</span> 请重新登录。${fp}${risk}`; if(login) login.classList.remove("hidden"); }
  else { box.innerHTML=`<span class="badge bad">未登录</span> 请粘贴 MUSIC_U 登录。${risk}`; if(login) login.classList.remove("hidden"); }
}
async function onAccLogin(){
  const inp=$("#accCookie"); const cookie=(inp?inp.value:"").trim(); if(!cookie){ return; }
  try{ const j=await (await api("/api/music-admin/account",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"login",cookie})})).json();
    if(!j.ok){ const b=$("#accStatus"); if(b) b.textContent=j.message; return; } renderAccount(j); loadStatus(); }
  catch(e){ const b=$("#accStatus"); if(b) b.textContent="登录失败: "+e.message; }
}
async function onAccLogout(){
  try{ const j=await (await api("/api/music-admin/account",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"logout"})})).json();
    if(j.ok){ renderAccount(j); loadStatus(); } } catch(e){}
}
async function onAccRelogin(){
  const btn=$("#accRelogin"); const old=btn.textContent; btn.disabled=true; btn.textContent="重登中…";
  try{
    const j=await (await api("/api/music-admin/account",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"relogin"})})).json();
    toast(j.message || (j.ok?"已重新登录":"重新登录失败"), j.ok?"ok":"bad");
    renderAccount(j);
  }catch(e){ toast("重新登录失败: "+e.message,"bad"); }
  finally{ btn.disabled=false; btn.textContent=old; }
}

/* ---- 运行日志 ---- */
function stopLogTimer(){ if(LOG_TIMER){ clearInterval(LOG_TIMER); LOG_TIMER=null; } }
function startLogTimer(){ stopLogTimer(); if($("#logAuto").checked) LOG_TIMER=setInterval(()=>loadLogs(true), 3000); }

/* 复制到剪贴板：WebUI 常挂在 http://<内网IP>:8080 上，不是安全上下文，
   navigator.clipboard 会直接不存在，所以必须有 execCommand 兜底。 */
function copyText(text){
  if(navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  return new Promise((resolve,reject)=>{
    const ta=document.createElement("textarea");
    ta.value=text; ta.setAttribute("readonly",""); ta.style.position="fixed"; ta.style.left="-9999px";
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, ta.value.length);
    try{ document.execCommand("copy") ? resolve() : reject(new Error("浏览器拒绝了复制")); }
    catch(e){ reject(e); }
    finally{ document.body.removeChild(ta); }
  });
}

async function loadLogs(silent){
  const lv=$("#logLevel").value||"";
  const q=$("#logSearch").value.trim();
  const src=$("#logSource").value||"";
  let url="/api/music-admin/logs?limit=2000";
  if(lv) url+="&level="+encodeURIComponent(lv);
  if(q) url+="&q="+encodeURIComponent(q);
  if(src) url+="&source="+encodeURIComponent(src);
  try{
    const j=await (await api(url)).json();
    fillLogLevels(j.levels);
    fillLogSources(j.sources, j.bot_logger);
    renderLogs(j.logs||[], j.stats||{});
  }catch(e){
    if(e.message==="unauthorized") return;
    if(!silent) toast("日志加载失败："+e.message,"bad");
  }
}

function fillLogSources(sources, botName){
  const sel=$("#logSource");
  const keep=sel.value;                        // 保留用户选中的来源
  const rows=sources||[];
  let html='<option value="">全部来源</option>';
  if(botName) html+=`<option value="bot">🤖 只看机器人</option>`;
  html+=rows.map(s=>`<option value="${esc(s.name)}">${esc(s.name)}（${s.count}）</option>`).join("");
  sel.innerHTML=html;
  // 原来选中的来源还在就保留；否则回落「全部来源」
  if(keep && (keep==="bot" ? botName : rows.some(s=>s.name===keep))) sel.value=keep; else sel.value="";
}

function fillLogLevels(levels){
  const sel=$("#logLevel");
  if(sel.dataset.filled==="1" || !levels || !levels.length) return;
  sel.innerHTML='<option value="">全部</option>'+levels.map(l=>`<option value="${esc(l)}">${esc(l)} 及以上</option>`).join("");
  sel.value="";     // 默认全部：缓冲里本来就只有 ≥ 配置等级的日志
  sel.dataset.filled="1";
}

function renderLogs(items, st){
  LOGS_LAST=items;
  const view=$("#logView");
  const atBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 28;
  if(!items.length){
    view.innerHTML='<div class="empty">（没有匹配的日志）</div>';
  }else{
    view.innerHTML=items.map(l=>{
      const exc = l.exc ? `<span class="lex">${esc(l.exc)}</span>` : "";
      return `<div class="log-line log-lv-${esc(l.level)}">`
        + `<span class="lt">${esc((l.time||"").slice(11))}</span>`
        + `<span class="ll">${esc(l.level)}</span>`
        + `<span class="ln">${esc(l.name)}</span>`
        + `<span class="lm">${esc(l.message)}</span>${exc}</div>`;
    }).join("");
  }
  const bits=[];
  if(st.installed===false) bits.push("⚠ 日志捕获未生效（请检查启动日志）");
  if(st.bridge===false) bits.push("⚠ 机器人自身的日志未接入（archiver/service 等模块的日志看不到）");
  bits.push(`本次启动共 ${st.total||0} 条`);
  bits.push(`缓冲 ${st.size||0}/${st.capacity||0}`);
  if(st.level) bits.push(`记录等级 ≥ ${st.level}`);
  bits.push(st.file ? ("落盘 "+st.file) : "未落盘文件");
  if(st.started_at) bits.push("自 "+new Date(st.started_at*1000).toLocaleString()+" 起");
  $("#logMeta").textContent=bits.join(" · ");
  if(atBottom) view.scrollTop=view.scrollHeight;   // 原来就在底部才自动跟随，避免打断翻看
}

$("#logRefresh").onclick=()=>loadLogs();
$("#logLevel").onchange=()=>loadLogs(true);
$("#logSource").onchange=()=>loadLogs(true);
$("#logAuto").onchange=startLogTimer;
$("#logSearch").addEventListener("input",()=>{
  clearTimeout(LOG_SEARCH_T);
  LOG_SEARCH_T=setTimeout(()=>loadLogs(true), 300);
});
$("#logCopy").onclick=async()=>{
  if(!LOGS_LAST.length){ toast("没有可复制的内容"); return; }
  const text=LOGS_LAST.map(l=>`${l.time} [${l.level}] ${l.name} | ${l.message}`+(l.exc?("\n"+l.exc):"")).join("\n");
  try{ await copyText(text); toast(`已复制 ${LOGS_LAST.length} 条日志`,"ok"); }
  catch(e){ toast("复制失败："+(e.message||e),"bad"); }
};
$("#logClear").onclick=async()=>{
  const btn=$("#logClear"); btn.disabled=true;
  try{
    const j=await (await api("/api/music-admin/logs",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"clear"})})).json();
    toast(j.message||"已清空", j.ok?"ok":"bad");
    loadLogs(true);
  }catch(e){ toast("清空失败："+e.message,"bad"); }
  finally{ btn.disabled=false; }
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
  color-scheme: dark;
}
[data-theme="light"]{
  --bg:#f4f6fb; --bg2:#ffffff; --card:rgba(20,30,60,.03); --card-bd:rgba(20,30,60,.1);
  --txt:#1a2233; --muted:#5b6678; --accent:#3b6fe0; --accent2:#7c5cf0; --ok:#0f9d63; --bad:#d8453b;
  --input:rgba(20,30,60,.05); --shadow:0 10px 30px rgba(20,30,60,.1);
  color-scheme: light;
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

/* 手机适配 */
@media (max-width:640px){
  .wrap{padding:16px 12px 110px}
  .hrow{gap:10px}
  .hrow h1{font-size:16px}
  .hrow button{padding:7px 11px;font-size:13px}
  .card{padding:14px;border-radius:16px}
  .card h2{font-size:15px}
  textarea{min-height:150px}
}
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

