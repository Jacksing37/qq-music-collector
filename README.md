# QQ 群音乐分享收集机器人

自动识别群里分享的网易云 / QQ 音乐 / 酷狗 / 酷我 / 汽水音乐等链接和卡片，@ 分享者并回发歌曲名片，按时间顺序整理榜单（文字 + 长图），并在设定时刻自动归档到网易云歌单。归档后的歌单**自动写入简介**（开头模板 + 「谁分享了什么歌」清单，一首一行）。

## 功能特性

- **自动识别**：链接 / JSON 卡片 / XML 卡片 / 短链跳转，含 QQ 音乐 mid、网易云数字 id 等
- **即时反馈**：识别后 @ 分享者并回发歌曲名片（卡片失败三级降级，信息不丢）
- **榜单整理**：文字 + 长图，按发送先后排序，支持中文标题渲染
- **定时归档**：到设定时刻在网易云新建歌单并加入全部歌曲，歌单名与简介均可自定义
- **简介自动生成**：开头模板 + 逐首「X 分享《歌名》- 歌手」或按人聚合，一首一行，超 1000 字自动截断
- **表情安全**：昵称 / 歌名里的 emoji 自动转中文词（如 🎵→`[音符]`），绕开网易云简介 utf8(3字节) 存储限制
- **未识别不入榜**：解析失败的链接不会污染榜单，仅给分享者一条轻量提示
- **未匹配标注分享者**：归档时没能匹配到网易云的歌曲，会在简介与归档结果里附上「分享者 - 歌名」，方便手动补录
- **被 @ 即自我介绍**：任何时候 @ 机器人都会回应，文案可自定义（支持占位符 + 冷却）
- **手动开关收集**：`/music collect on|off` 临时覆盖时间表，方便随时测试
- **三种清理方式**（已收集歌曲）：归档后自动清 / 手动批量清 / 定时清
- **缓存图片自动回收**：按天数 + 保留个数双规则清理封面与长图
- **全时间可配置**：每周 / 每日 / 单次，星期与时刻均可改，群内命令即时生效，撞点自动去重

## 技术栈

- 框架：[NoneBot2](https://github.com/nonebot/nonebot2) + OneBot v11
- 协议端：[NapCat](https://github.com/NapNeko/NapCatQQ)（推荐）/ Lagrange.OneBot
- 存储：SQLite（零部署）
- 渲染：Pillow（无需浏览器）
- 调度：APScheduler（`nonebot_plugin_apscheduler`）

---

## 快速启动

### 1. 准备 NapCat

配置 QQ 号，反向 WebSocket 指向本机器人：

```json
{ "url": "ws://127.0.0.1:8080/onebot/v11/ws" }
```

### 2. 配置环境

```bash
cp .env.example .env
# 编辑 .env，至少配置 SUPERUSERS（超管 QQ 号）
```

### 3. 安装依赖并启动

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python bot.py
```

首次运行自动把 `config.example.yaml` 复制为 `data/config.yaml`，之后用群内命令修改即可，无需重启。

> 依赖装漏 `nonebot-plugin-apscheduler` 会在启动时报 `ModuleNotFoundError`。
> 注意别用系统 Python 跑（会找不到 venv 里的包），始终用 `.venv\Scripts\python`。

### 4. 网易云登录（建歌单必需）

1. 浏览器打开 https://music.163.com 并登录
2. F12 → Network，刷新页面，找任意 `music.163.com` 请求
3. 在请求头 Cookie 中找到 `MUSIC_U=xxxxx`
4. **私聊**机器人发送：`/music cookie xxxxxx`

---

## 文件目录结构

```
qq-music-collector/
├── bot.py                  # NoneBot2 启动入口（加载插件 + 驱动）
├── .env                    # ⚠️ 运行配置（含密钥，已忽略，勿提交）
├── .env.example            # .env 模板，复制后填值
├── .gitignore              # ⚠️ 保护文件：忽略密钥与运行时数据，防止误提交
├── config.example.yaml     # 配置模板（首次运行复制为 data/config.yaml）
├── requirements.txt        # Python 依赖清单
├── pyproject.toml          # 项目元数据 + 依赖约束
├── README.md               # 本文件
├── Dockerfile              # 运行镜像（内置中文字体 + 时区，监听 0.0.0.0）
├── docker-compose.yml      # bot + napcat 编排（同网络，反向 WS 走容器名）
├── .dockerignore           # 构建上下文排除（.venv / data / .env 等）
├── deploy/                 # 服务器部署：start.sh 启动脚本 + systemd 单元 + 完整部署指南
│   ├── start.sh                       # 自动建 venv 并启动，可被 systemd/nohup 调用
│   ├── qq-music-collector.service     # systemd 守护单元（开机自启 + 崩溃重启）
│   └── README.md                      # 「从零到运行」完整步骤（Docker / 裸机两条路线）
├── scripts/
│   └── cleanup.py          # 一键回收运行时产物（缓存图 / 样例图 / 字节码）
├── src/
│   └── plugins/
│       └── music_collector/
│           ├── __init__.py      # 插件入口 + 群消息监听（识别→@→回卡片→自我介绍）
│           ├── commands.py      # 群内命令（/music 全部子命令）
│           ├── scheduler.py     # 定时任务：开始/汇总/结束/归档/补写简介/清理
│           ├── service.py       # 服务层：编排收集、归档、清理（不依赖 nonebot，可离线测）
│           ├── detector.py      # 链接识别：url/json/xml/短链 + 各平台正则
│           ├── window.py        # 时间窗口：weekly/daily/once 解析与匹配
│           ├── render.py        # 长图渲染（Pillow，含中文）
│           ├── archiver.py      # 归档器：匹配网易云 id + 建歌单 + 写简介
│           ├── netease_api.py   # 网易云 API（weapi 加密，自实现，无第三方 SDK）
│           ├── cache.py         # 缓存图片回收（按时间 + 保留个数）
│           ├── naming.py        # 歌单名 / 简介模板引擎（占位符渲染 + 清单排版）
│           ├── textutil.py      # 表情清洗：emoji → 中文词 / 删除 / 保留
│           ├── config.py        # 配置模型 + YAML 落盘 + 点分路径热更新
│           ├── store.py         # SQLite 收集池（增删查、匹配记录、归档记录）
│           ├── models.py        # 数据模型：MusicLink / Song
│           ├── bot_utils.py     # 发送辅助（卡片降级、分段、长图上传）
│           └── providers/       # 各平台元数据解析
│               ├── base.py      # Provider 抽象基类
│               ├── netease.py   # 网易云解析
│               ├── qqmusic.py   # QQ 音乐解析（多级回退）
│               └── generic.py   # 兜底解析（酷狗 / 酷我 / 汽水等）
├── data/                   # ⚠️ 运行时数据（整体已被 .gitignore 忽略）
│   ├── config.yaml         # 当前生效配置（命令修改后自动写入）
│   ├── collector.db        # 收集到的歌曲数据库（SQLite）
│   ├── netease_session.json  # ⚠️ 网易云登录凭证，等同账号密码
│   ├── cache/              # 封面缓存 *.img + render/ 榜单长图（自动回收）
│   └── sample/             # 测试渲染的长图样例（可随时删）
└── tests/
    ├── run_all.py                 # 一键跑完全部脚本并汇总
    ├── test_offline.py            # 离线基础测试（识别 / 存储 / 渲染 / 归档）
    ├── test_features.py           # 命名模板 / 分享者简介 / 缓存清理
    ├── test_clear.py              # 清理已收集歌曲三模式
    ├── test_description.py        # 歌单简介写入校验
    ├── test_naming.py             # 清单排版：一首一行 / 表情清洗贯通
    ├── test_textutil.py           # 表情清洗规则
    ├── test_config_compat.py      # 旧配置向后兼容
    ├── test_start_and_unmatched.py# 未识别不入榜 / 未匹配标注 / 开始播报
    ├── test_card_fallback.py      # 签名服务 500 时的卡片三级降级
    ├── test_schedule_dedup.py     # 调度撞点去重 + 卡片熔断
    └── smoke_commands.py          # 命令 dispatch + 自我介绍预览冒烟
```

### 🛡️ 受保护的文件（不要手动删除或提交）

| 路径 | 原因 | 处理 |
|------|------|------|
| `.env` | 含 OneBot 密钥与超管 QQ 号 | 已忽略；复制自 `.env.example` |
| `data/netease_session.json` | 网易云账号凭证 `MUSIC_U` | 已忽略；删除后需重新 `/music cookie` |
| `data/config.yaml` | 当前生效配置（含群号） | 已忽略；丢了会退回 `config.example.yaml` 默认值 |
| `data/collector.db` | 已收集的歌曲数据 | 已忽略；删除即丢失所有未归档收集 |
| `napcat/`（Docker 起的 NapCat） | QQ 登录态与协议端配置 | 已忽略；由 docker-compose 在仓库根生成，删了需重新扫码登录 |
| `data/cache/`、`data/sample/` | 图片产物 | 已忽略（位于 /data/ 内）；可随时删，会自动重建 |
| `.venv/`、`__pycache__/` | 依赖与字节码 | 已忽略；重建即可 |

---

## 群内命令

不带参数发送任意命令即可看到它自己的详细用法。完整清单见 `/music help`。

```
【所有人】
/music list          本期榜单（文字 + 长图）
/music count         已收集数量
/music window        时间窗口配置与下次触发
/music status        运行状态 / 网易云登录状态
/music preview       预览本期歌单名

【管理员 · 时间】
/music mode weekly|daily|once                切换循环模式
/music set time <开始>-<结束>                 一键设：开始-结束（汇总/归档统一=结束）
   例: /music set time 周五 12:00-周五 20:00
/music set start|summary|end|archive <时间点>  单独设置某一时刻
/music set tz <时区>                          如 Asia/Shanghai

【管理员 · 歌单命名 / 简介】
/music name <模板>                 长期命名模板（如 Wk.{seq}线上学习{slash}）
/music title <名称>                仅本次归档生效的歌单名，用完自动清空
/music seq <数字>                  期号（/music seq auto on|off 开自动递增）
/music desc <模板>                 简介开头模板
/music sharer list|by_person|by_name|none  简介分享清单样式

【管理员 · 简介排版】
/music emoji text|strip|keep   昵称/歌名表情处理（text=转中文词，推荐）
/music artist on|off           简介是否带歌手名
/music blank on|off            简介条目间空行（by_person 样式生效）

【管理员 · 收集与归档】
/music on | off               总开关
/music collect auto|on|off    临时覆盖收集状态（测试用，不改时间表）
/music archive [歌单名]        立即归档建歌单
/music descfix                补写失败的歌单简介

【管理员 · 音乐卡片】
/music card                        查看卡片模式与熔断状态
/music card native|custom|off      切换卡片模式（签名服务老挂就用 custom）
/music card text|cover on|off      文字兜底 / 兜底附封面
/music card retry <次数> [分钟]     熔断阈值与冷却时长（0 = 不熔断）
/music card reset                  立即解除熔断

【管理员 · 自我介绍】
/music intro                       查看当前文案与预览
/music intro on|off                开关
/music intro text <文案>            自定义文案，\n 表示换行
/music intro cooldown <秒>          同群冷却，0 = 不限频
/music intro at on|off              回复时是否 @ 提问者
/music intro always on|off          非收集期是否仍然回应
/music intro skipcmd|skipmusic on|off  带命令 / 带音乐分享时不打扰

【管理员 · 清理】
/music del <序号|范围|all|window>   删除收集（del window 看历史窗口）
   del 3 / del 1-5 / del 2 4 7 / del all / del window <key>
/music delauto on|off               归档后自动清空本期
/music prune on|off|days|at         定时清理历史收集
/music clean [天数]                 立即清理图片缓存

【管理员 · 调试】
/music cookie <MUSIC_U>   设置网易云登录凭证（建议私聊）
/music parse <链接>        诊断链接为何没被识别
/music debug on|off       识别过程详细日志
/music export             导出榜单文本（手动建歌单用）
```

### 时间点格式

- weekly: `MON 20:00` 或 `周一 20:00`
- daily : `23:00`
- once  : `2026-08-10 00:00`

自我介绍文案可用占位符：`{nick}` `{count}` `{window}` `{state}` `{playlist}` `{group}`。

---

## 配置管理 Web UI

不想记命令？内置了一个网页配置面板，打开浏览器就能改所有设置。

- **访问地址**：`http://<机器人IP>:8080/music-admin`（与 OneBot 共用 8080 端口，路径不同）
- **进入令牌**：在 `.env` 里设 `MUSIC_WEBUI_TOKEN=你的令牌`；
  若没设，机器人启动时会**随机生成并打印在日志里**（每次重启都变，建议固定下来）。
- **功能**：按分组（通用 / 时间窗口 / 歌单简介 / 卡片 / 渲染 / 缓存 / 清理 / 自我介绍）渲染表单，
  支持暗/亮主题切换，顶部实时显示当前收集状态与下次定时；保存即生效，
  **改时间窗口会自动重载定时任务**。
- **自动同步**：表单由 `config.py` 的 pydantic 结构**自动生成**，以后加配置项 UI 会自动出现，无需改前端。

### 收集预览与实时操作

面板里「📊 收集预览与实时操作」卡片可随时查看并干预收集，无需进群敲命令：

- **预览**：按窗口展示每个群收集了哪些歌（序号 / 歌名 / 歌手 / 分享者 / 平台 / 是否匹配网易云），
  顶部可切换不同窗口、显示网易云登录态。
- **实时开关**：`强制开始收集` / `强制停止收集` / `恢复自动` —— 等价于 `/music collect on|off|auto`。
- **归档**：`归档本群` 立即把某群建歌单；`归档当前窗口全部` 批量归档当前窗口所有群。
- **删除歌曲**：勾选表格里的歌曲 → `删除选中`；`清空本窗口` 清空该群本窗口全部记录。
- **预览**：`预览歌单名` / `预览简介` 先看这一期会生成什么，再决定是否归档。

> 所有操作都走 `MUSIC_WEBUI_TOKEN` 鉴权；归档/删除会直接写库或调网易云，**请确认后再点**。

> ⚠️ 安全：机器人 `HOST=0.0.0.0` 时 8080 暴露在所有网卡，WebUI 仅靠 `MUSIC_WEBUI_TOKEN` 保护，
> 务必设置强令牌，并让服务器防火墙**不要**对公网开放 8080 端口。

---

## 歌单命名与简介模板

通过占位符渲染，未知占位符原样保留不报错。

| 占位符 | 含义 | 示例 |
|--------|------|------|
| `{seq}` | 自增期号 | `86` |
| `{slash}` | 年月日 `/` | `26/8/7` |
| `{yy}{m}{d}` / `{mm}{dd}` | 年月日 | `26` `8` `7` / `08` `07` |
| `{ymd}` / `{dot}` | 日期 | `2026-08-07` / `26.8.7` |
| `{week}` | ISO 周数 | `32` |
| `{weekday}` | 星期 | `周五` |
| `{window}` | 窗口区间文案 | `本周` |
| `{start}` / `{end}` | 窗口起止日 | `2026-08-03` |
| `{count}` | 实际收录首数 | `12` |
| `{total}` | 分享总数 | `15` |
| `{sharers}` | 参与人数 | `4` |
| `{group}` | 群号 | `123456` |

示例：`/music name Wk.{seq}线上学习{slash}` → 歌单名 `Wk.86线上学习26/8/7`

简介 = `desc` 模板开头 + 「谁分享了什么歌」清单，**一首歌占一行**：

```
Wk.86 线上学习歌单，共 12 首。

张三 分享《起风了》- 买辣椒也用券
张三 分享《晴天》- 周杰伦
李四 分享《海阔天空》- Beyond
```

`sharer_style` 可选 `list`（逐首列，默认）/ `by_person`（按人聚合）/ `by_name`（只列分享者名，每行形如 `1.张三`）/ `none`（不附）。

---

## 项目维护

### 一键清理运行时产物

```bash
.venv\Scripts\python scripts\cleanup.py          # 预览要删什么，不动手
.venv\Scripts\python scripts\cleanup.py --yes    # 真正执行
.venv\Scripts\python scripts\cleanup.py --yes --db  # 顺带清空 collector.db
```

清理范围：`data/cache/*.img`、`data/cache/render/*.png`、`data/sample/*.png`、`**/__pycache__`、`*.log`。
`netease_session.json`、`config.yaml`、`.env` 写死在保护名单里，脚本永远不碰。

日常其实不用手动跑——插件自带缓存回收（见下节）。这个脚本主要用于打包上传服务器前瘦身。

### git 卫生检查

如果仓库是在早期初始化的，`.env`、`data/netease_session.json`、`.pyc` 可能已经被跟踪进版本库。
**cookie 进 git 等于账号泄露**，务必检查：

```bash
git ls-files | grep -E "\.env$|netease_session|collector\.db|\.pyc$"
```

有输出就执行下面这段，把它们从索引里摘掉（磁盘文件保留）：

```bash
git rm -r --cached .env data/ -q
git rm -r --cached "*.pyc" -q
git rm -r --cached src/plugins/music_collector/__pycache__ -q
git commit -m "chore: 停止跟踪密钥与运行时产物"
```

如果这些内容**已经推送到过远程仓库**，仅仅 `rm --cached` 不够 —— 历史里还在。
此时应当立刻在网易云退出登录使旧 `MUSIC_U` 失效，重新 `/music cookie`，并考虑用
`git filter-repo` 清洗历史或直接重建仓库。

---

## 清理已收集歌曲（三种方式）

收集到的歌曲存在 `data/collector.db`，支持三种清理：

1. **归档后自动清**：`/music delauto on` —— 建歌单成功后自动清空本期榜单（默认关闭，避免误删）
2. **手动批量清**：`/music del 1-5`、`/music del 2 4 7`、`/music del all`、`/music del window <key>`
3. **定时清**：`/music prune on` 开启，每天 `prune at` 时刻删除早于「现在 - `prune days`」的历史记录

> 「清理已收集歌曲」与「缓存图片清理」是两回事：前者删数据库里的歌曲，后者删 `data/cache/` 的图片。

---

## 缓存图片自动回收

`cache` 配置项控制封面（`data/cache/*.img`）与长图（`render/*.png`）的回收，采用**按天数 + 保留个数**双规则并集：

```yaml
cache:
  enabled: true
  keep_days: 3             # 超过 3 天的删（<=0 表示不按时间清）
  max_render_files: 60     # 长图最多保留 60 个（保留最新）
  max_cover_files: 400     # 封面最多保留 400 个
  clean_at: "04:30"        # 每天定点清理
  clean_on_start: true     # 启动时清一次
  clean_after_render: true # 每次渲染后清一次
```

手动触发：`/music clean [天数]`。

---

## 配置项速览（`data/config.yaml`）

完整带注释的版本见 `config.example.yaml`。

```yaml
enabled: true
collect_override: auto     # auto 按时间表 / on 强制收 / off 强制停（测试用）
groups: []                 # 生效群号，留空为所有群
reply_card: true
notify_duplicate: true

window:
  mode: weekly             # weekly / daily / once
  timezone: Asia/Shanghai
  archive_same_as_end: true  # 归档时刻 = 结束收集时刻，只跑一个任务
  weekly:
    start: MON 00:00
    summary: SUN 22:00
    end: SUN 22:30
    archive: SUN 22:30

playlist:
  name_template: "群歌单 {window}"
  description_template: "由 QQ 群 {group} 在 {window} 期间收集，共 {count} 首。"
  include_sharers: true
  sharer_style: list       # list / by_person / by_name / none
  seq: 1
  seq_auto_increment: true
  pending_name: ''         # 一次性歌单名，用完自动清空
  cross_platform_match: true
  strict_match: true
  desc_retry: 3            # 简介写入重试次数（网易云有频控）
  desc_retry_minutes: 30   # 定时补写间隔；<=0 关闭
  emoji_style: text        # text 转中文词 / strip 删掉 / keep 原样
  desc_show_artist: true
  desc_blank_line: false

card:
  mode: native             # native 原生 / custom 自定义 / off 只发文字
  fallback_custom: true
  fallback_text: true
  fallback_cover: true
  failure_threshold: 3     # 同平台连续失败几次熔断；0 = 不熔断
  cooldown_minutes: 10

intro:
  enabled: true
  text: "你好 {nick}，我是群音乐收集助手 🎵 ..."
  cooldown: 10             # 同群冷却秒数，0 = 不限频
  at_sender: true
  skip_commands: true
  skip_music: true
  always_reply: true       # 非收集期也回应

render:
  theme: dark              # light / dark
  max_items_per_image: 40
  show_cover: true
  font_path: null          # 留空自动探测系统中文字体

cache:
  enabled: true
  keep_days: 3

clear:
  after_archive: false
  scheduled_enabled: false
  keep_days: 30
  prune_at: "05:00"
```

---

## 测试

全部是可直接执行的断言脚本，不依赖 pytest runner：

```bash
.venv\Scripts\python tests\run_all.py            # 全跑，输出通过/失败汇总
.venv\Scripts\python tests\run_all.py naming     # 只跑名字含 naming 的
.venv\Scripts\python tests\run_all.py -v         # 失败时打印完整输出
```

也可以单独跑某个脚本：

```bash
set PYTHONDONTWRITEBYTECODE=1
.venv\Scripts\python tests\test_offline.py
```

> 插件包导入时会 `require("nonebot_plugin_apscheduler")`，所以测试脚本里必须先
> `nonebot.init(driver="~fastapi")` 才能 import 插件模块 —— 新增测试时照抄现有脚本的开头即可。
> 部分脚本会往 `data/sample/`、临时目录写文件，运行环境需对项目目录有写权限。

---

## 部署到服务器

> 完整「从零到运行」的步骤（Docker / systemd 两条路线、NapCat 配置、验证、更新流程）
> 见 **[`deploy/README.md`](deploy/README.md)**。仓库已自带 **`Dockerfile`**、**`docker-compose.yml`**，
> 以及裸机用的 **`deploy/start.sh`** 与 **`deploy/qq-music-collector.service`**。

这个机器人吃的资源很少（常驻约 150–250MB 内存，几乎不占 CPU），**1 核 1G 的小机器够跑，
但建议 1 核 2G**——瓶颈在 NapCat 那一侧（Node + QQ 协议端）。

要点：

1. **NapCat 与机器人必须能互相访问**（同机最省事）。机器人本身**不会连 QQ**，
   真正登录 QQ 的是 NapCat（或 Lagrange.OneBot）协议端，得单独装。别把 WS 端口暴露到公网。
2. **时区大坑**：服务器多为 UTC，务必确认 `window.timezone: Asia/Shanghai`，否则定时全偏 8 小时。
3. **进程守护**：Docker 用 `restart: unless-stopped`；裸机用 systemd（`deploy/qq-music-collector.service`）。
4. **字体**：纯净 Linux 镜像常没有中文字体，长图会变方块。Docker 镜像已内置中文字体；
   裸机 Debian/Ubuntu 用 `apt install fonts-noto-cjk`；RHEL/CentOS/Rocky/Alma（dnf 系）用
   `dnf install google-noto-sans-cjk-ttc-fonts`（或 `dnf install wqy-zenhei`）；
   找不到也可把字体路径写进 `render.font_path`（`render.py` 会自动扫描系统字体目录兜底）。

### 方式一：Docker（NapCat 也是 Docker 时首选）

```bash
cp .env.example .env && nano .env      # 填 SUPERUSERS / ONEBOT_ACCESS_TOKEN
docker compose up -d --build
docker compose logs -f bot
```

⚠️ **容器网络坑**：NapCat 容器里的 `127.0.0.1` 指向它自己。反向 WebSocket 必须填
`ws://qq-music-bot:8080/onebot/v11/ws`（两个容器同网络时），三种网络场景的处理见
[`deploy/README.md`](deploy/README.md)。

更新：`git pull && docker compose up -d --build`。

### 方式二：裸机三步（细节见 deploy/README.md）

```bash
# 1) 装环境 + 依赖（start.sh 首次会自动建 venv）
#    Debian/Ubuntu:   sudo apt install -y python3 python3-venv fonts-noto-cjk
#    RHEL/CentOS/Rocky/Alma (dnf):  sudo dnf install -y python3 python3-pip google-noto-sans-cjk-ttc-fonts
sudo apt install -y python3 python3-venv fonts-noto-cjk
cp .env.example .env && nano .env          # 填 SUPERUSERS / ONEBOT_ACCESS_TOKEN

# 2) 同机装好 NapCat，反向 WS 指向 ws://127.0.0.1:8080/onebot/v11/ws 并扫码登录小号
#    NapCat 官方一键安装：
#    bash <(curl -L https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh)

# 3) 用 systemd 守护启动
sudo cp deploy/qq-music-collector.service /etc/systemd/system/
sudo nano /etc/systemd/system/qq-music-collector.service   # 改 User= 和路径
sudo systemctl daemon-reload && sudo systemctl enable --now qq-music-collector
journalctl -u qq-music-collector -f
```

### 以后更新代码

```bash
cd /opt/qq-music-collector
git pull
.venv/bin/python -m pip install -r requirements.txt   # 仅依赖有变时才需要
sudo systemctl restart qq-music-collector
```

`.env`、`data/`（含 `config.yaml` / `collector.db` / `netease_session.json`）都不进版本库，
更新不会动你的配置与数据。

---

## 注意事项

1. **weapi 风控**：网易云 weapi 接口在某些网络环境可能返回空响应，导致自动建歌单失败。遇到时用 `/music export` 导出文本手动建歌单。
2. **简介写入**：归档时通过 `playlist/update` 接口写入简介并保留歌单名（`playlist/desc/update` 已下线）。该接口有频控，失败会自动入队，由定时任务补写，也可手动 `/music descfix`。
3. **简介里的表情**：网易云简介按 utf8(3 字节) 存储，emoji 是 4 字节，带 emoji 的昵称会**整条写不进去**。默认 `emoji_style: text` 会把常见表情转成中文词（🎵→`[音符]`），不认识的 4 字节字符直接丢弃。
4. **音乐卡片签名服务**：协议端发**原生**音乐卡片时要向外部签名服务换取 ArkShare 结构，那个服务经常 500，日志表现为：

   ```
   [音乐卡片签名失败] 签名服务请求出错! Unexpected status code: 500
   Error: 消息体无法解析, 请检查是否发送了不支持的消息类型
   ```

   插件对此做了三级降级，**信息一定不会丢**：

   | 级别 | 方式 | 是否依赖签名服务 |
   |------|------|------------------|
   | 1 | 平台原生卡片 | 是（会 500） |
   | 2 | 自定义卡片（自拼标题/封面/跳转链接） | 否 |
   | 3 | 文字兜底（歌名 / 歌手 / 专辑 / 可点击链接 + 封面图） | 否 |

   同一平台连续失败 3 次会**熔断** 10 分钟，冷却期内直接走文字兜底，不再每首歌白等一次超时。
   长期不可用就 `/music card custom` 一劳永逸，或在 NapCat 配置里换一个可用的 `musicSignUrl`。

5. **@ 检测**：OneBot V11 适配器会把开头/结尾的 `@机器人` 从消息段里摘掉，所以判断是否被 @ 一律以 `event.to_me` 为准，不能只遍历消息段找 at。
6. **调度撞点**：`/music set time` 会把 summary / end / archive 对齐到同一时刻。调度层已做撞点去重 + 运行期幂等锁，同一时刻只播报一次榜单（归档播报已含最终榜单）。
7. **跨平台匹配**：非网易云来源会在网易云搜索匹配；严格模式要求歌名与歌手都对得上，关闭后命中率高但可能加错版本。
8. **消息风控**：长图、@ 用户、音乐卡片都可能触发 QQ 风控，建议先在测试群验证。
9. **开始收录提醒**：广播对象优先取 `groups` / `report_groups`；都为空则退回「曾经收集过的群」。全新群收不到首条提醒，建议在配置里填上目标群号。
10. **非收集期静默**：收集窗口关闭期间，群里发的音乐链接不会收到任何回应（不解析、不入榜），但 @ 机器人仍会回自我介绍（受 `intro.always_reply` 控制）。

---

## 行为速查

| 场景 | 机器人行为 |
|------|-----------|
| 收集期内分享音乐链接 | @ 分享者，回复歌曲名片并收录入榜 |
| 卡片签名服务 500 | 自动降级：自定义卡片 → 文字兜底，连续失败则熔断 10 分钟 |
| 收集期内重复分享 | 提示已在榜单第几位（首发者） |
| 收集期内分享非音乐 / 无法解析 | 轻量提示「已跳过收录」，不入榜 |
| **收集期外分享音乐链接** | **静默，不解析、不回应** |
| 群里 @ 机器人（任何内容） | 回复自我介绍 + `/music help` 提示 |
| 汇总播报与结束/归档同一时刻 | 只播报一次 |
| 窗口开启时刻 | 向目标群广播「开始收录」提醒 |
| 归档时某首歌匹配不到网易云 | 歌单简介与归档结果里标注「分享者 - 歌名」待手动补 |
| 简介写入被频控 | 入队，由定时任务补写，也可 `/music descfix` 手动触发 |
