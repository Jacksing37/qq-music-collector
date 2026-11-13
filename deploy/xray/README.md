# 境外代理（Xray · VLESS REALITY）

服务器本机跑一个 Xray 客户端，给机器人一条出境通道。**只监听回环**，公网访问不到。

- `127.0.0.1:10809` — HTTP 代理（**实际在用的就是这个**）
- `127.0.0.1:10808` — SOCKS5（保留，见下方警告，别用在环境变量里）

分流在 Xray 内部完成：`geoip:cn` / `geosite:cn` → 直连，其余 → 代理。
所以网易云、QQ 音乐、本机 `127.0.0.1:8790` 中转都不受影响。

---

## 安装

```bash
# 1. 用模板填好自己的节点凭据（真实文件不要进 git）
cp deploy/xray/config.example.json /tmp/xray.json
vim /tmp/xray.json      # 改 address / port / id / publicKey / shortId / serverName

# 2. 装（第二个参数可给离线包，服务器连不上 GitHub 时很有用）
sudo bash deploy/xray/install.sh /tmp/xray.json [Xray-linux-64.zip]
```

脚本会装到：

| 路径 | 内容 |
|---|---|
| `/usr/local/bin/xray` | 二进制 |
| `/usr/local/share/xray/` | `geoip.dat` / `geosite.dat` |
| `/usr/local/etc/xray/config.json` | 配置（权限 600） |
| `/etc/systemd/system/xray-proxy.service` | 守护单元 |

拿到离线包的办法（服务器/本机都可能连不上 GitHub）：

```bash
curl -sL -o Xray-linux-64.zip \
  "https://gh-proxy.com/https://github.com/XTLS/Xray-core/releases/download/<TAG>/Xray-linux-64.zip"
# TAG 可以从 curl "https://gh-proxy.com/https://api.github.com/repos/XTLS/Xray-core/releases/latest" 取
```

---

## 让机器人用上它

**不要改代码**，用 systemd 环境变量：

```bash
cat >/opt/qq-music-collector/proxy.env <<'EOF'
HTTP_PROXY=http://127.0.0.1:10809
HTTPS_PROXY=http://127.0.0.1:10809
NO_PROXY=127.0.0.1,localhost,::1,100.100.100.200
EOF
```

单元里加一行（`deploy/qq-music-collector.service` 已经带好）：

```ini
EnvironmentFile=-/opt/qq-music-collector/proxy.env
```

这样 LLM 请求、网页抓取、联网搜索、技能脚本全都自动走代理，不用逐个改 `httpx.AsyncClient`。

**关代理**：注释掉 `proxy.env` 里的两行 → `systemctl daemon-reload && systemctl restart qq-music-collector`。

---

## 三个坑（都踩过）

**1. 绝对不要设 `ALL_PROXY=socks5://…`。**
venv 里没装 `socksio`，httpx 一读到 socks 代理就 `ImportError`，**所有**请求（包括国内的）全部失败。
只设 `HTTP_PROXY` / `HTTPS_PROXY`。

**2. 香港节点访问 OpenAI / Anthropic 会被拒。**
`api.openai.com` 返回：

```json
{"error":{"code":"unsupported_country_region_territory","message":"Country, region, or territory not supported"}}
```

TLS 能握手、证书也正确，是出口位置风控。要直连这两家得换日本 / 新加坡 / 美国节点。
一般站点正常：GitHub、Google、DuckDuckGo、Bing、Telegram。

**3. 代理是单点。**
机器人所有出网都先连 `127.0.0.1:10809`，Xray 挂了连国内接口都会失败。
所以单元里用 `Restart=always`，并且 `qq-music-collector.service` 用
`Wants=` + `After=xray-proxy.service` 保证先起代理（`Wants` 是软依赖，代理起不来机器人照样能启动）。

---

## 排查

```bash
systemctl status xray-proxy
journalctl -u xray-proxy -n 50

# 出口 IP（应该是节点 IP）
curl -s -x http://127.0.0.1:10809 https://ipinfo.io/ip

# 国内是否仍直连
curl -s -x http://127.0.0.1:10809 -o /dev/null -w "%{http_code}\n" https://music.163.com
```
