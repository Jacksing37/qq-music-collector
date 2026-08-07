# QQ 群音乐分享收集机器人

自动识别群里分享的网易云 / QQ 音乐 / 酷狗 / 酷我 / 汽水音乐等链接和卡片，@ 分享者并回发歌曲名片，按时间顺序整理榜单（文字 + 长图），并在设定时刻自动归档到网易云歌单。归档后的歌单**自动写入简介**（开头模板 + 「谁分享了什么歌」清单）。

## 功能特性

- **自动识别**：链接 / JSON 卡片 / XML 卡片 / 短链跳转，含 QQ 音乐 mid、网易云数字 id 等
- **即时反馈**：识别后 @ 分享者并回发歌曲名片（解析失败降级为文字 + 封面）
- **榜单整理**：文字 + 长图，按发送先后排序，支持中文标题渲染
- **定时归档**：到设定时刻在网易云新建歌单并加入全部歌曲，歌单名与简介均可自定义
- **简介自动生成**：开头模板 + 逐首「X 分享《歌名》- 歌手」或按人聚合，超 1000 字自动截断
- **未识别不入榜**：解析失败的链接（如非音乐页面、接口超时）不会污染榜单，仅给分享者一条轻量提示
- **未匹配标注分享者**：归档时未能匹配网易云的歌曲，在歌单简介与归档结果里附上「分享者 - 歌名」，**方便手动查找补录**
- **开始收录提醒**：每个收集窗口开启时自动向群内广播「开始收录」提示
- **三种清理方式**（已收集歌曲）：归档后自动清 / 手动批量清 / 定时清
- **缓存图片自动回收**：按天数 + 保留个数双规则清理封面与长图
- **全时间可配置**：每周 / 每日 / 单次，星期与时刻均可改，群内命令即时生效

## 技术栈

- 框架：[NoneBot2](https://github.com/nonebot/nonebot2) + OneBot v11
- 协议端：[NapCat](https://github.com/NapNeko/NapCatQQ)（推荐）/ Lagrange.OneBot
- 存储：SQLite（零部署）
- 渲染：Pillow（无需浏览器）
- 调度：APScheduler（`nonebot_plugin_apscheduler`）

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

首次运行自动创建 `data/config.yaml`，之后用群内命令修改即可，无需重启。

### 4. 网易云登录（建歌单必需）

1. 浏览器打开 https://music.163.com 并登录
2. F12 → Network，刷新页面，找任意 `music.163.com` 请求
3. 在请求头 Cookie 中找到 `MUSIC_U=xxxxx`
4. **私聊**机器人发送：`/music cookie xxxxxx`

凭证持久化到 `data/netease_session.json`，重启有效。

---

## 文件目录结构

```
qq-music-collector/
├── bot.py                  # NoneBot2 启动入口（加载插件 + 驱动）
├── .env                    # ⚠️ 运行配置（含密钥，已被 .gitignore 忽略，勿提交）
├── .env.example            # .env 模板，复制后填值
├── .gitignore              # ⚠️ 保护文件：忽略密钥与运行时数据，防止误提交
├── config.example.yaml     # 配置模板（首次运行复制为 data/config.yaml）
├── requirements.txt        # Python 依赖清单
├── pyproject.toml          # 项目元数据 + 依赖约束
├── README.md               # 本文件
├── src/
│   └── plugins/
│       └── music_collector/
│           ├── __init__.py      # 插件入口 + 群消息监听（识别→@→回卡片）
│           ├── commands.py      # 群内命令（/music 全部子命令）
│           ├── scheduler.py     # 定时任务：开始/汇总/归档/缓存清理/收集清理
│           ├── service.py       # 服务层：编排收集、归档、清理
│           ├── detector.py      # 链接识别：url/json/xml/短链 + 各平台正则
│           ├── window.py        # 时间窗口：weekly/daily/once 解析与匹配
│           ├── render.py        # 长图渲染（Pillow，含中文）
│           ├── archiver.py      # 归档器：匹配网易云 id + 建歌单 + 写简介
│           ├── netease_api.py   # 网易云 API（weapi 加密，自实现，无第三方 SDK）
│           ├── cache.py         # 缓存图片回收（按时间 + 保留个数）
│           ├── naming.py        # 歌单名 / 简介模板引擎（占位符渲染）
│           ├── config.py        # 配置模型 + YAML 落盘 + 热更新
│           ├── store.py         # SQLite 收集池（增删查、匹配记录、归档记录）
│           ├── models.py        # 数据模型：MusicLink / Song
│           ├── bot_utils.py     # 发送辅助（分段、长图上传）
│           └── providers/       # 各平台元数据解析
│               ├── base.py      # Provider 抽象基类
│               ├── netease.py   # 网易云解析
│               └── qqmusic.py   # QQ 音乐解析（4 级回退）
├── data/                   # ⚠️ 运行时数据（多已被 .gitignore 忽略）
│   ├── config.yaml         # 当前生效配置（命令修改后自动写入）
│   ├── collector.db        # 收集到的歌曲数据库（SQLite）
│   ├── netease_session.json  # ⚠️ 网易云登录凭证（忽略，勿提交）
│   ├── cache/              # 封面缓存（*.img，自动回收）
│   └── sample/            # 测试渲染的长图样例（可删，自动忽略）
└── tests/
    ├── test_offline.py     # 离线基础测试（62 项）
    ├── test_features.py    # 命名模板 / 简介 / 缓存清理（20 项）
    ├── test_clear.py       # 清理已收集歌曲三模式（19 项）
    └── test_description.py # 歌单简介写入校验（2 项）
```

### 🛡️ 受保护的文件 / 目录（不要手动删除或提交）

| 路径 | 原因 | 处理 |
|------|------|------|
| `.env` | 含 OneBot 密钥与超管 QQ 号 | 已被 `.gitignore` 忽略；复制自 `.env.example` |
| `data/netease_session.json` | 网易云账号凭证 `MUSIC_U` | 已被忽略；删除后需重新 `/music cookie` |
| `data/collector.db` | 已收集的歌曲数据 | 忽略；删除即丢失所有未归档收集 |
| `data/cache/` | 封面缓存 | 忽略；由缓存清理规则自动管理，可删 |
| `data/sample/` | 测试长图 | 忽略；可随时删除 |
| `.venv/`、`__pycache__/`、`*.pyc` | 依赖与字节码 | 忽略；重建即可 |

> 提交代码前请确认这些文件不会进入 git（`.gitignore` 已覆盖）。如需把 `config.yaml` 纳入版本管理，请自行调整 `.gitignore`。

---

## 群内命令

```
【所有人】
/music list          当前榜单（文字 + 长图）
/music count         已收集数量
/music window        查看时间窗口配置
/music status        运行状态与网易云登录状态
/music preview       预览本期歌单名将是什么

【管理员 · 时间】
/music mode weekly|daily|once       切换循环模式
/music set start   <时间点>          设置开始收集时刻
/music set summary <时间点>          设置汇总播报时刻
/music set archive <时间点>          设置归档建歌单时刻
/music set tz      <时区>            如 Asia/Shanghai

【管理员 · 歌单命名 / 简介】
/music name <模板>                   设置长期歌单命名模板
/music title <名称>                  只对下一次归档生效的歌单名（用完自动清空）
/music seq <数字>                    设置自增期号（Wk.86 里的 86）
/music desc <模板>                   歌单简介开头模板
/music sharer list|by_person|none    简介内分享清单样式
/music archive [歌单名]              立即归档并建歌单
/music export                        导出榜单文本（weapi 不可用时手动建歌单）

【管理员 · 清理已收集歌曲】
/music del <序号|范围|all|window>     删除已收集歌曲
   del 3             删除第 3 首
   del 1-5           批量删除第 1~5 首
   del 2 4 7         批量删除指定序号
   del all           清空本期榜单
   del window        查看可清理的历史窗口
   del window <key>  清空指定窗口
/music delauto on|off                 归档（结束收集）后自动清空本期
/music prune on|off|days|at           定时清理历史收集
   prune on|off          开关定时清理
   prune days <天数>     保留天数（默认 30）
   prune at <HH:MM>      每天执行时刻（默认 05:00）

【管理员 · 缓存 / 调试】
/music clean [天数]                   立即清理图片缓存
/music on | off                       开关收集
/music cookie <MUSIC_U>               设置网易云登录凭证（建议私聊）
/music parse <链接>                   诊断某链接为何没被识别
/music debug on|off                   开关识别过程详细日志
```

### 时间点格式

- weekly: `MON 20:00` 或 `周一 20:00`
- daily : `23:00`
- once  : `2026-08-10 00:00`

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

简介 = `desc` 模板开头 + 「谁分享了什么歌」清单（`sharer list` 逐首 / `by_person` 按人聚合）。清单超 1000 字自动截断并注明省略条数。

---

## 清理已收集歌曲（三种方式）

收集到的歌曲存在 `data/collector.db`，支持三种清理：

1. **归档后自动清**：`/music delauto on` —— 归档建歌单成功后自动清空本期榜单（默认关闭，避免误删）
2. **手动批量清**：`/music del 1-5`、`/music del 2 4 7`、`/music del all`、`/music del window <key>`
3. **定时清**：`/music prune on` 开启，每天 `prune at` 时刻删除早于「现在 - `prune days`」的历史记录

> 注意「清理已收集歌曲」与「缓存图片清理」是两回事：前者删数据库里的歌曲，后者删 `data/cache/` 的图片。

---

## 缓存图片自动回收

`cache` 配置项控制封面（`data/cache/*.img`）与长图（`render/*.png`）的回收，采用**按天数 + 保留个数**双规则并集：

```yaml
cache:
  enabled: true
  keep_days: 3            # 超过 3 天的删（<=0 表示不按时间清）
  max_render_files: 60    # 长图最多保留 60 个（保留最新）
  max_cover_files: 400    # 封面最多保留 400 个
  clean_at: "04:30"       # 每天定点清理
  clean_on_start: true    # 启动时清一次
  clean_after_render: true # 每次渲染后清一次
```

手动触发：`/music clean [天数]`。

---

## 配置项速览（`data/config.yaml`）

```yaml
enabled: true
groups: []                 # 生效群号，留空为所有群
window:
  mode: weekly             # weekly / daily / once
  timezone: Asia/Shanghai
  reply_outside_window: true
playlist:
  name_template: "群歌单 {window}"
  description_template: "由 QQ 群 {group} 在 {window} 期间收集，共 {count} 首。"
  include_sharers: true
  sharer_style: list       # list / by_person / none
  seq: 1
  seq_auto_increment: true
  privacy: false
  cross_platform_match: true
  strict_match: true
  batch_size: 100
render:
  theme: dark
  max_items_per_image: 40
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

```bash
.venv\Scripts\python -m pip install pytest httpx pycryptodome pillow aiosqlite pydantic pyyaml
PYTHONDONTWRITEBYTECODE=1 .venv\Scripts\python tests/test_offline.py
PYTHONDONTWRITEBYTECODE=1 .venv\Scripts\python tests/test_features.py
PYTHONDONTWRITEBYTECODE=1 .venv\Scripts\python tests/test_clear.py
PYTHONDONTWRITEBYTECODE=1 .venv\Scripts\python tests/test_description.py
```

当前覆盖：离线 62 + 功能 20 + 清理 19 + 简介 2 = 103 项，全部通过。

---

## 注意事项

1. **weapi 风控**：网易云 weapi 接口在某些网络环境可能返回空响应，导致自动建歌单失败。遇到时用 `/music export` 导出文本手动建歌单。
2. **简介写入**：归档时通过 `playlist/update` 接口写入简介并保留歌单名；若该接口被风控，简介可能为空，可重试归档。
3. **QQ 音乐卡片**：部分分享拿不到数字 ID，会降级发送文字 + 封面。
4. **跨平台匹配**：非网易云来源会在网易云搜索匹配；严格模式要求歌名与歌手都对得上，关闭后命中率高但可能加错版本。
5. **消息风控**：长图、@ 用户、音乐卡片都可能触发 QQ 风控，建议先在测试群验证。
6. **开始收录提醒**：窗口开启时会向群广播提醒。广播对象优先取 `groups`/`report_groups` 配置；若都为空，则退回到「曾经收集过的群」。全新尚未收集过的群收不到首条提醒，建议在 `data/config.yaml` 的 `groups` 里填上目标群号。
```
