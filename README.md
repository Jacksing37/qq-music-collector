# QQ 群音乐分享收集机器人

自动识别群里分享的网易云 / QQ 音乐 / 酷狗 / 酷我 / 汽水音乐 / Apple Music 等链接和卡片，@ 分享者并回发歌曲名片，按时间顺序整理榜单（文字 + 长图），并在设定时刻自动归档到网易云歌单。归档后的歌单**自动写入简介**（开头模板 + 「谁分享了什么歌」清单，一首一行）。

## 功能特性

- **自动识别**：链接 / JSON 卡片 / XML 卡片 / 短链跳转，含 QQ 音乐 mid、网易云数字 id 等
- **即时反馈**：识别后 @ 分享者并回发歌曲名片（卡片失败三级降级，信息不丢）
- **榜单整理**：文字 + 长图，按发送先后排序，支持中文标题渲染
- **定时归档**：到设定时刻在网易云新建歌单并加入全部歌曲，歌单名与简介均可自定义
- **同窗口复用追加**：同一窗口归档过一次后再次归档（如手动 `/music archive` 补发），会**复用已有歌单追加新歌**，不会重复建歌单、不消耗期号
- **分享即归档**：可开 `auto_archive_on_share` 开关，每收到一批新歌立即自动追加进当前窗口歌单（静默执行，结果只记日志）
- **简介自动生成**：开头模板 + 逐首「X 分享《歌名》- 歌手」或按人聚合，一首一行，超 1000 字自动截断
- **表情安全**：昵称 / 歌名里的 emoji 自动转中文词，绕开网易云简介 utf8(3 字节) 存储限制
- **未识别不入榜**：解析失败的链接不会污染榜单，仅给分享者一条轻量提示
- **未匹配标注分享者**：归档时没能匹配到网易云的歌曲，会在简介与归档结果里附上「分享者 - 歌名」
- **被 @ 即自我介绍**：任何时候 @ 机器人都会回应，文案可自定义（支持占位符 + 冷却）
- **手动开关收集**：`/music collect on|off` 临时覆盖时间表，方便随时测试
- **全时间可配置**：每周 / 每日 / 单次，星期与时刻均可改，群内命令即时生效，撞点自动去重
- **分享者昵称映射**：可在网页端「昵称映射」独立页设置 `原昵称=显示名` 或 `QQ号码=显示名`（如 `菜老名=Jacksing` 或 `123456789=Jacksing`），网易云简介 / 群内文字榜单 / WebUI 表格里的分享者名字都会替换成显示名，但数据库仍保留原始昵称（昵称优先于 QQ 号码匹配）
- **总库（跨窗口去重）**：开启后每首新分享会同时进入群级「总库」，跨窗口去重；分享已存在于总库的歌会提示重复（仅跨窗口，与同窗口「重复提醒」互不冲突）。总库可在网页端独立增删改 / 拖拽排序 / 同步到歌单，并独立归档成一个大歌单，功能与正常收集一致

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
# 编辑 .env，至少配置 SUPERUSERS（超管 QQ 号），建议设置 MUSIC_WEBUI_TOKEN
```

### 3. 安装依赖并启动

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python bot.py
```

首次运行自动把 `config.example.yaml` 复制为 `data/config.yaml`，之后用群内命令修改即可，无需重启。

> 依赖装漏 `nonebot-plugin-apscheduler` 会在启动时报 `ModuleNotFoundError`。
> 注意始终用 `.venv\Scripts\python`，别用系统 Python。

### 4. 网易云登录（建歌单必需）

1. 浏览器打开 https://music.163.com 并登录
2. F12 → Network，刷新页面，找任意 `music.163.com` 请求
3. 在请求头 Cookie 中找到 `MUSIC_U=xxxxx`
4. **私聊**机器人发送：`/music cookie xxxxxx`

> ⚠️ `.env`、`data/`（含 `config.yaml` / `collector.db` / `netease_session.json`）均已被 `.gitignore` 忽略，切勿提交，否则账号凭证会泄露。

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
/music replycard on|off       是否回发音乐卡片（@+文字提示始终发送）
/music archive [歌单名]        立即归档建歌单（已归档过的窗口=复用追加）
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
- **总库管理页**：面板左侧「📚 总库」进入，按群展示该群总库歌曲（与收集管理页同样的编辑 / 拖拽排序 / 匹配 / 增删 / 同步 / 归档能力），每个群卡片附其总库歌单链接；顶部「汇总现有窗口到总库」可一键把历史各窗口歌曲回填进总库。总库相关开关（是否启用、分享是否对比、重复提示文案、归档模板等）在「⚙ 配置」的「总库」分组里设置。
- **昵称映射独立页**：面板顶部「✏ 昵称映射」按钮（或直访 `http://<机器人IP>:8080/music-admin/aliases`）
  打开一个专页，每行写一条 `原昵称=显示名`，带实时预览；保存后网易云简介、
  群内文字榜单、WebUI 表格里的分享者名字都会替换，仅做展示，不改入库。

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

### 归档复用与分享即归档

- **同窗口复用**：同一窗口（如 `W20260824-0000`）归档过一次后再归档，会**复用已有歌单**只追加新歌，不会新建歌单；期号 `seq` 与一次性歌单名（`pending_name`）只在**新建歌单**时消耗。没有新歌时返回「歌单已是最新」。
- **分享即归档**：配置 `playlist.auto_archive_on_share: true`（默认 `false`）后，每收到一批新歌分享会立即自动追加进当前窗口歌单，跨平台来源会自动匹配网易云曲库。该行为**静默执行**（结果只在日志记录，不额外刷屏），适合"边分享边建歌单"的实时场景；未登录网易云时自动跳过。

### 简介补写

写简介撞上网易云频控时会自动入队，由定时任务（`desc_retry_minutes`）或 `/music descfix` 补写。

补写时**按当前数据重新生成**简介，而不是重推存档的旧文本——否则期间删过歌、改过昵称映射、或歌单后来又追加了新歌，补上去的清单就会和歌单对不上。数据优先级：

1. 该窗口当前的收集记录（反映后续删歌 / 昵称映射变更）
2. 归档当时的快照（窗口被清空时用它兜底，清单不会变空）
3. 存档的旧文本（两者都没有时的最后退路）

### 收录回复模板

`reply.enabled: true` 后用 `reply.accept_text` 自定义「已收录」那条回复，群命令 `/music reply` 可随时改。

| 占位符 | 含义 |
|--------|------|
| `{index}` | 本期序号 |
| `{nick}` | 分享者（已套昵称映射） |
| `{title}` `{artists}` `{album}` | 歌名 / 歌手 / 专辑 |
| `{platform}` `{url}` `{duration}` | 来源平台 / 链接 / 时长 |
| `{artists_line}` `{album_line}` | 整行「歌手: xxx」「专辑: xxx」，无值时整行消失 |
| `{song}` | 歌曲详情块（歌名 + 歌手/专辑/来源/时长） |
| `{playlist}` | 当前群当前窗口的网易云歌单链接，未归档时用 `playlist_empty_text` 代替 |
| `{count}` `{window}` | 本期已收录首数 / 窗口文案 |

---

## 测试

全集为可直接执行的断言脚本，不依赖 pytest runner：

```bash
.venv\Scripts\python tests\run_all.py            # 全跑，输出通过/失败汇总
.venv\Scripts\python tests\run_all.py naming     # 只跑名字含 naming 的
.venv\Scripts\python tests\run_all.py -v         # 失败时打印完整输出
```

> 插件包导入时会 `require("nonebot_plugin_apscheduler")`，所以测试脚本里必须先
> `nonebot.init(driver="~fastapi")` 才能 import 插件模块 —— 新增测试时照抄现有脚本的开头即可。

---

## 部署到服务器

> 完整「从零到运行」步骤（Docker / systemd 两条路线、NapCat 配置、验证、更新流程）见 **[`deploy/README.md`](deploy/README.md)**。
> 仓库已自带 **`Dockerfile`**、**`docker-compose.yml`**，以及裸机用的 **`deploy/start.sh`** 与 **`deploy/qq-music-collector.service`**。

要点：

1. **NapCat 与机器人必须能互相访问**（同机最省事）。机器人本身**不会连 QQ**，真正登录 QQ 的是 NapCat 协议端，得单独装；别把 WS 端口暴露到公网。
2. **时区大坑**：服务器多为 UTC，务必确认 `window.timezone: Asia/Shanghai`，否则定时全偏 8 小时。
3. **进程守护**：Docker 用 `restart: unless-stopped`；裸机用 systemd。
4. **字体**：纯净 Linux 镜像常没有中文字体，长图会变方块。Docker 镜像已内置中文字体；
   裸机 Debian/Ubuntu 用 `apt install fonts-noto-cjk`；RHEL/CentOS/Rocky/Alma（dnf 系）用
   `dnf install google-noto-sans-cjk-ttc-fonts`（或 `wqy-zenhei`）。
   **泰文**：歌单图片里若出现泰文昵称/歌名，主字体（CJK）不含泰文，需单独装泰文字体——
   Debian/Ubuntu：`apt install fonts-noto-sans-thai`；dnf 系：`dnf install google-noto-sans-thai-fonts`。
   装好后渲染会自动探测到，无需改配置；也可在 `render.thai_font_path` 显式指定。

### 方式一：Docker（NapCat 也是 Docker 时首选）

```bash
cp .env.example .env && nano .env      # 填 SUPERUSERS / ONEBOT_ACCESS_TOKEN
docker compose up -d --build
docker compose logs -f bot
```

⚠️ **容器网络坑**：NapCat 容器里的 `127.0.0.1` 指向它自己。反向 WebSocket 必须填
`ws://qq-music-bot:8080/onebot/v11/ws`（两个容器同网络时），三种网络场景见 [`deploy/README.md`](deploy/README.md)。

### 方式二：裸机三步（细节见 deploy/README.md）

```bash
sudo apt install -y python3 python3-venv fonts-noto-cjk
cp .env.example .env && nano .env          # 填 SUPERUSERS / ONEBOT_ACCESS_TOKEN
# 同机装好 NapCat，反向 WS 指向 ws://127.0.0.1:8080/onebot/v11/ws 并扫码登录
sudo cp deploy/qq-music-collector.service /etc/systemd/system/
sudo nano /etc/systemd/system/qq-music-collector.service   # 改 User= 和路径
sudo systemctl daemon-reload && sudo systemctl enable --now qq-music-collector
journalctl -u qq-music-collector -f
```

更新：`git pull && sudo systemctl restart qq-music-collector`。`.env` 与 `data/` 不进版本库，更新不动你的配置与数据。

---

## 注意事项

1. **weapi 风控**：网易云 weapi 接口在某些网络环境可能返回空响应，导致自动建歌单失败。遇到时用 `/music export` 导出文本手动建歌单。
2. **简介写入**：归档时通过 `playlist/update` 接口写入简介并保留歌单名（该接口有频控，失败会自动入队由定时任务补写，也可手动 `/music descfix`）。
3. **简介里的表情**：网易云简介按 utf8(3 字节) 存储，emoji 是 4 字节，带 emoji 的昵称会整条写不进去。默认 `emoji_style: text` 会把常见表情转成中文词（🎵→`[音符]`），不认识的 4 字节字符直接丢弃。
4. **音乐卡片签名服务**：协议端发原生音乐卡片时要向外部签名服务换 ArkShare 结构，那个服务经常 500。插件做了三级降级（原生卡片 → 自定义卡片 → 文字兜底），信息一定不会丢；同一平台连续失败 3 次会熔断 10 分钟，冷却期内直接走文字兜底。长期不可用就 `/music card custom`，或在 NapCat 配置里换可用的 `musicSignUrl`。
5. **跨平台匹配**：非网易云来源会在网易云搜索匹配；严格模式要求歌名与歌手都对得上，关闭后命中率高但可能加错版本。
6. **消息风控**：长图、@ 用户、音乐卡片都可能触发 QQ 风控，建议先在测试群验证。
