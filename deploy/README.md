# 服务器部署指南（Linux VPS）

机器人本身只处理消息，**真正连 QQ 的是 NapCat 协议端**，两者必须能互相访问。

资源占用：机器人常驻约 150–250MB，NapCat（Node + QQ 协议端）建议再留 1–2G，
所以 **1 核 2G 最稳**，1 核 1G 偏紧。

两条路线，按你的 NapCat 形态选：

| 你的情况 | 走哪条 |
|---------|--------|
| NapCat 是 Docker | [路线一 · Docker](#路线一docker推荐) |
| NapCat 是裸机安装 | [路线二 · systemd](#路线二裸机--systemd) |

---

## 0. 准备

- 一台 Linux 服务器（Ubuntu / Debian 推荐）
- 一个**专门的 QQ 小号**（协议端有风控风险，别用大号）
- SSH 能登上去

---

# 路线一：Docker（推荐）

## 1. 上传代码

```bash
# 本地先瘦身，别把几十兆缓存图传上去
python scripts/cleanup.py --yes

# 服务器上
git clone <你的仓库地址> /opt/qq-music-collector
cd /opt/qq-music-collector
```

## 2. 配置 .env

```bash
cp .env.example .env
nano .env
```

必改两项：

- `SUPERUSERS`：你的超管 QQ 号，如 `["123456789"]`
- `ONEBOT_ACCESS_TOKEN`：随便设一个字符串，NapCat 那边要填一样的

`HOST` 不用管，`docker-compose.yml` 会强制覆盖成 `0.0.0.0`
（容器里若监听 127.0.0.1，NapCat 容器永远连不进来）。

## 3. 理清网络（最容易踩的坑）

> **NapCat 容器里的 `127.0.0.1` 指向 NapCat 自己，不是你的机器人。**
> 反向 WS 填 `ws://127.0.0.1:8080/...` 一定连不上。

按你的实际情况三选一：

### 场景 A：让本 compose 把 NapCat 一起拉起来（最省事）

`docker-compose.yml` 里已经带了 napcat 服务，直接用。
两个容器在同一个 `botnet` 网络，反向 WS 地址：

```
ws://qq-music-bot:8080/onebot/v11/ws
```

### 场景 B：NapCat 已经在单独跑，不想动它

注释掉 `docker-compose.yml` 里的整个 `napcat:` 段，起完机器人后把
NapCat 容器接进同一网络：

```bash
docker compose up -d --build
docker network connect qq-music-collector_botnet <你的napcat容器名>
```

反向 WS 地址同样填：

```
ws://qq-music-bot:8080/onebot/v11/ws
```

网络名不确定就 `docker network ls | grep botnet` 看一眼。

### 场景 C：NapCat 用了 `network_mode: host`

那它和宿主机共用网络栈。把机器人也改成 host 模式即可
（在 `docker-compose.yml` 的 bot 服务下加 `network_mode: host`，
并删掉它的 `networks:` 段），反向 WS 填：

```
ws://127.0.0.1:8080/onebot/v11/ws
```

### 场景 D：机器人原生跑在宿主机，NapCat 在 Docker（混合部署，最常见）

不想把 Python 塞进 Docker、只想让 NapCat 用现成容器时走这条。
关键点只有两个：机器人必须监听 `0.0.0.0`，NapCat 反向 WS 指向 `host.docker.internal`。

1. `.env` 里把监听地址改成 `0.0.0.0`（默认模板是 `127.0.0.1`，容器连不进来会报
   `ECONNREFUSED 172.17.0.1:8080`）：

   ```ini
   HOST=0.0.0.0
   ```

2. 原生启动机器人（任选一种，见上方的「极简三步 / systemd」）：

   ```bash
   bash deploy/start.sh
   # 或 systemd： sudo systemctl enable --now qq-music-collector
   ```

3. 在 NapCat WebUI 新增**反向 WebSocket**，地址填：

   ```json
   {
     "url": "ws://host.docker.internal:8080/onebot/v11/ws",
     "token": "<和 .env 里的 ONEBOT_ACCESS_TOKEN 完全一致>"
   }
   ```

4. **Linux 原生 Docker 引擎注意**：`host.docker.internal` 默认不被解析，
   需要给 NapCat 容器加一行 host 映射（二选一）：
   - 命令行：`docker run ... --add-host=host.docker.internal:host-gateway ...`
   - compose：在 napcat 服务下加 `extra_hosts: ["host.docker.internal:host-gateway"]`
   （Docker Desktop / Windows / Mac 自带该域名，可跳过。）

5. **安全提示**：`HOST=0.0.0.0` 会让 8080 监听在所有网卡上。
   务必保持 `ONEBOT_ACCESS_TOKEN` 非空，并靠防火墙/安全组只放行必要端口，
   别把 8080 暴露在公网。
   - 本项目还内置了配置管理网页 `http://<IP>:8080/music-admin`，它**只能靠**
     `.env` 里的 `MUSIC_WEBUI_TOKEN` 保护（未设置则每次启动随机生成并打印在日志）。
     上线前务必设一个强令牌，且**不要**对公网开放 8080。

## 4. 启动

```bash
docker compose up -d --build
docker compose logs -f bot
```

看到 `Running NoneBot... Loaded adapters: OneBot V11` 就说明机器人起来了。

## 5. 配置 NapCat

打开 NapCat WebUI（默认 `http://127.0.0.1:6099`，服务器上建议走 SSH 隧道：
`ssh -L 6099:127.0.0.1:6099 user@server`，然后本地浏览器开 `http://127.0.0.1:6099`）。

扫码登录机器人小号，然后新增一个**反向 WebSocket**：

```json
{
  "url": "ws://qq-music-bot:8080/onebot/v11/ws",
  "token": "<和 .env 里的 ONEBOT_ACCESS_TOKEN 完全一致>"
}
```

保存后回看机器人日志，出现 `WebSocket Connection from ... Accepted!` 即连通。

## 6. 登录网易云（建歌单必需）

私聊机器人：

```
/music cookie <MUSIC_U>
```

获取方式：浏览器登录 music.163.com → F12 → Network → 刷新 → 找任意
music.163.com 请求 → 请求头 Cookie 里的 `MUSIC_U=xxxxx`。**别在群里发。**

## 7. 日常运维

```bash
docker compose ps                  # 看状态
docker compose logs -f bot         # 跟日志
docker compose restart bot         # 重启
docker compose down                # 停止

# 更新代码
git pull && docker compose up -d --build
```

`./data`（config.yaml、collector.db、netease_session.json、缓存图）挂在宿主机，
重建镜像不会丢配置和数据。

---

# 路线二：裸机 + systemd

## 1. 环境

```bash
# Debian/Ubuntu:
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl fonts-noto-cjk

# RHEL / CentOS / Rocky / Alma（dnf 系）:
sudo dnf install -y python3 python3-pip git curl google-noto-sans-cjk-ttc-fonts
# 备选：sudo dnf install -y wqy-zenhei

cd /opt/qq-music-collector
```

中文字体（`fonts-noto-cjk` / `google-noto-sans-cjk-ttc-fonts` / `wqy-zenhei` 任一即可）是为了
长图里的中文不变方块。`render.py` 会自动扫描系统字体目录兜底，实在找不到再配 `render.font_path`。

## 2. 配置

```bash
cp .env.example .env
nano .env     # 改 SUPERUSERS 和 ONEBOT_ACCESS_TOKEN
```

`data/config.yaml` 首次运行自动从 `config.example.yaml` 生成，不用手建。

> ⚠️ **时区大坑**：服务器默认 UTC。确认 `data/config.yaml` 里
> `window.timezone: Asia/Shanghai`，否则所有定时会整体偏 8 小时。

## 3. 安装 NapCat

```bash
bash <(curl -L https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh)
```

反向 WS 填 `ws://127.0.0.1:8080/onebot/v11/ws`，token 与 `.env` 一致。

> 备选协议端：**Lagrange.OneBot**（Go 实现，更省内存，适合 1G 小机）。

## 4. 启动

```bash
sudo cp deploy/qq-music-collector.service /etc/systemd/system/
sudo nano /etc/systemd/system/qq-music-collector.service   # 改 User= 和路径
sudo systemctl daemon-reload
sudo systemctl enable --now qq-music-collector
journalctl -u qq-music-collector -f
```

`start.sh` 会自动建 venv 并装依赖，首次启动多花一两分钟属正常。

更新代码：

```bash
git pull
.venv/bin/python -m pip install -r requirements.txt   # 依赖有变时才需要
sudo systemctl restart qq-music-collector
```

---

## 常见问题

| 现象 | 排查 |
|------|------|
| NapCat 一直连不上（Docker） | 反向 WS 别填 `127.0.0.1`，填 `ws://qq-music-bot:8080/onebot/v11/ws`；确认两个容器在同一网络 `docker network inspect <网络名>` |
| 连上又立刻断开 | token 不一致，`.env` 的 `ONEBOT_ACCESS_TOKEN` 和 NapCat 里必须一模一样 |
| 容器起来但没日志输出 | `docker compose logs bot`；若卡在装依赖是首次构建，等它 |
| 定时全错乱（差 8 小时） | 容器已设 `TZ=Asia/Shanghai`；再确认 `data/config.yaml` 的 `window.timezone` |
| 长图中文变方块 | 镜像已内置中文字体；裸机 Debian/Ubuntu `apt install fonts-noto-cjk`，dnf 系 `dnf install google-noto-sans-cjk-ttc-fonts`（或 `wqy-zenhei`），或配 `render.font_path` |
| 卡片签名服务 500 | `/music card custom` 一劳永逸 |
| 群里丢链接没反应 | NapCat 没连上 / 没 @ 机器人 / 不在收集窗口 |
| 收不到「开始收录」广播 | 在 `data/config.yaml` 的 `groups` 填上目标群号 |
| 权限报错 `Permission denied: data/` | 容器以 root 写盘，宿主机侧执行 `sudo chown -R $USER ./data` 即可 |
| `docker compose` 报 variable is not set | `.env` 里的值含 `$`（compose 会当变量插值），把 `$` 写成 `$$` 转义 |
| 改了 `.env` 不生效 | `docker compose up -d` 重建容器，`restart` 不会重新读 env_file |
