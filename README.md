# QQ 群音乐分享收集机器人

自动识别群里分享的网易云 / QQ 音乐 / 酷狗 / 酷我 / 汽水音乐等链接和卡片，@ 分享者并回发歌曲名片，按时间顺序整理榜单（文字 + 长图），并在设定时刻自动归档到网易云歌单。

## 已支持的音乐平台

- 网易云音乐
- QQ 音乐
- 酷狗音乐
- 酷我音乐
- 汽水音乐

## 技术栈

- 机器人框架：[NoneBot2](https://github.com/nonebot/nonebot2) + OneBot v11 Adapter
- 协议端：[NapCat](https://github.com/NapNeko/NapCatQQ)（推荐）或 Lagrange.OneBot
- 数据库：SQLite（零部署）
- 渲染：Pillow（无需浏览器）
- 任务调度：APScheduler（内置在 NoneBot2 中）

## 快速启动

### 1. 准备 NapCat

下载 NapCat 并配置 QQ 号，确保反向 WebSocket 指向本机器人地址：

```json
{
  "url": "ws://127.0.0.1:8080/onebot/v11/ws"
}
```

### 2. 配置环境

```bash
cp .env.example .env
# 编辑 .env，至少配置 SUPERUSERS
```

### 3. 安装依赖并启动

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python bot.py
```

首次运行会自动创建 `data/config.yaml`，之后可用群内命令修改，无需重启。

## 群内命令

```
/music help           显示命令帮助
/music list            当前窗口榜单（文字 + 长图）
/music count           已收集数量
/music window          查看时间窗口配置
/music status          运行状态

/music mode weekly|daily|once      切换循环模式
/music set start   <时间点>          设置开始收集
/music set summary <时间点>          设置汇总播报
/music set archive <时间点>          设置归档建歌单
/music set tz      <时区>            设置时区
/music name <模板>                  设置歌单命名模板
/music on | off                      开关收集
/music del <序号>                    从榜单删除某首

/music cookie <MUSIC_U>             设置网易云登录凭证（私聊）
/music archive                      立即归档并建网易云歌单
/music export                       导出榜单文本（备用）
```

### 时间格式

- weekly: `MON 20:00` 或 `周一 20:00`
- daily : `23:00`
- once  : `2026-08-10 00:00`

## 网易云登录说明

自动建歌单需要网易云登录。请按以下步骤获取 `MUSIC_U`：

1. 用浏览器打开 https://music.163.com 并登录
2. 按 F12 打开开发者工具 → Network（网络）
3. 刷新页面，找到任意 `music.163.com` 请求
4. 在请求头 Cookie 中找到 `MUSIC_U=xxxxx` 这一段
5. 私聊机器人发送：`/music cookie xxxxxx`

凭证会持久化到 `data/netease_session.json`，重启后仍然有效。

## 注意事项

1. **weapi 风控**：网易云 weapi 接口在某些网络环境下可能返回空响应，导致自动建歌单失败。若遇到这种情况，请使用 `/music export` 导出歌单文本后手动创建歌单。
2. **QQ 音乐卡片**：部分 QQ 音乐分享拿不到数字 ID，此时会降级发送文字 + 封面。
3. **跨平台匹配**：非网易云平台分享会在网易云搜索匹配，严格模式下要求歌名和歌手都对得上；关闭严格模式会提高命中率，但可能加入错误版本。
4. **消息风控**：长图榜单、@ 用户和音乐卡片都可能触发 QQ 风控，建议先在测试群验证。

## 配置项

所有配置集中在 `data/config.yaml`，首次启动会自动生成。关键项：

```yaml
enabled: true
groups: []                 # 生效群号，留空为所有群
window:
  mode: weekly             # weekly / daily / once
  timezone: Asia/Shanghai
  reply_outside_window: true
playlist:
  name_template: "群歌单 {window}"
  cross_platform_match: true
  strict_match: true
render:
  theme: dark
  max_items_per_image: 40
```

## 项目结构

```
qq-music-collector/
├── bot.py                  # NoneBot2 启动入口
├── config.example.yaml     # 配置模板
├── requirements.txt
├── src/
│   └── plugins/
│       └── music_collector/
│           ├── __init__.py      # 插件入口 + 消息监听
│           ├── commands.py      # 群内命令
│           ├── scheduler.py     # 定时任务
│           ├── service.py       # 服务层
│           ├── detector.py      # 链接识别
│           ├── window.py        # 时间窗口
│           ├── render.py        # 长图渲染
│           ├── archiver.py      # 网易云归档
│           ├── netease_api.py   # 网易云 API
│           ├── store.py         # SQLite 收集池
│           ├── providers/       # 各平台元数据解析
│           └── models.py        # 数据模型
└── tests/
    └── test_offline.py      # 离线测试
```
