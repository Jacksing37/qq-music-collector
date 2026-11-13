# Cloudflare 中转方案（**未执行**，2026-11-13 存档）

> 状态：方案备用。写这份文档时只做了探测，**没有部署任何 Worker，也没有改机器人配置**。

## 1. 为什么要这个

现有出境代理（Xray → `154.49.152.15:20040`，香港）在服务器上不可用：

| 测试项 | 结果 |
|---|---|
| 服务器 → 节点 `ping -c50` | **丢包 58%**，RTT 153ms（mdev 仅 2.4ms） |
| TCP 20040 握手 12 次 | 成功 10 / 失败 2，成功时 1167~1179ms |
| 经代理访问 google | `tls=26.4s`，`ttfb=30.4s` |
| 经代理下 1MB | 90 秒只到 283KB（**3 KB/s**） |
| 本机（家庭宽带）→ 同一节点 | TCP 握手 363~449ms，5 次全成功 |

RTT 稳定但丢包固定比例 → **阿里云出口到该 IP 被 QoS 限速**，不是节点故障，也不是拥塞。
后果：`lens` 引擎的浏览器加载谷歌必然超时，无论超时调到多少都跑不通。

而服务器到 Cloudflare 非常好：

| 测试项 | 结果 |
|---|---|
| `ping 1.1.1.1` ×20 | **0% 丢包**，RTT 53ms |
| 直连 `speed.cloudflare.com` 1MB | **2.06s，485 KB/s** |
| CF 首页 TLS / TTFB | 0.45s / 0.63s |

**所以思路是：把出口从坏掉的香港节点换成 Cloudflare。**

## 2. 已经排除的：WARP

装过 `wgcf` + `wireguard-tools`（内核 5.10 支持，能起隧道），但：

```
transfer: 0 B received, 3.04 KiB sent
```

发出 3 KB，回程 0，ping WARP 内网 100% 丢包。**WARP 走 UDP 2408，这个协议在境内被阻断**，
配置层面无解。

WARP 部署的正确姿势（万一以后要用，避免机器失联）记在这里：

```ini
# /opt/warp/wgcf.conf —— 由 wgcf generate 生成后改两处
[Interface]
PrivateKey = ...
Address = 172.16.0.2/32
Table = off                 # ← 必须：不加默认路由
# DNS = 1.1.1.1             # ← 必须删：否则 wg-quick 会改 /etc/resolv.conf

[Peer]
PublicKey = bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=
Endpoint = engage.cloudflareclient.com:2408
AllowedIPs = 0.0.0.0/0, ::/0
```

```bash
wg-quick up /opt/warp/wgcf.conf          # 文件名必须等于接口名
ip route add default dev wgcf table 100
ip rule add from 172.16.0.2 table 100    # 只让源地址=warp 的流量走隧道
curl --interface wgcf https://ipinfo.io/ip
```

## 3. 方案 A：Workers 做 HTTP 转发（简单，但不解决 lens）

**能做什么**：机器人抓境外网页、调用境外 LLM API（OpenAI / Anthropic / Gemini 的 HTTP 请求）。
**不能做什么**：`lens` 需要真实浏览器跑 TCP，Workers 只转 HTTP，救不了。

思路：部署一个 Worker，把收到的请求 `fetch` 到目标 URL 后原样返回。

```
机器人（httpx, 带自定义 header）→ https://proxy.olgroup.site/?url=<目标>
                                       ↓
                                   Worker fetch → 目标站
```

要点：

- Worker 里白名单目标域名（防止变成开放代理被人白嫖）
- 透传 GET/POST body、状态码、`content-type`
- 机器人侧不需要改代码：`webtool` / `llm` 都可以通过一个「前置 URL」拼接，
  或者在 `httpx` 层加一个自定义 transport

**工作量**：Worker 代码 ~80 行 + 部署 10 分钟 + 机器人侧接入 30 分钟。

## 4. 方案 B：Workers + VLESS over WebSocket（推荐，lens 可用）

原理：Worker 充当 VLESS 服务端，用 **WebSocket 承载 VLESS 的 TCP 流**，xray 客户端连
`wss://<自定义域名>/<path>`。这样出口流量走 CF 的 CDN（443 TCP），绕开被限速的香港链路。

```
xray(服务器) --wss--> Cloudflare CDN --> Worker(VLESS) --> 目标站
```

### 4.1 前置条件

| 条件 | 状态 |
|---|---|
| 域名托管在 Cloudflare | ✅ `olgroup.site` |
| 走**自定义域名**而不是 `*.workers.dev` | ⚠️ 必须。`workers.dev` 在境内 DNS 被污染，解析不到 |
| 一个 UUID 作为认证 | 现场生成 |
| CF API Token（Zone:DNS Edit + Workers Scripts Edit） | 需要授权，或手动在 dashboard 操作 |

### 4.2 部署步骤（待执行）

1. **生成 UUID**：`python -c "import uuid;print(uuid.uuid4())"`
2. **部署 Worker**：用社区成熟实现（`zizifn/edgetunnel` 一类），把 UUID 写进环境变量
   - 关键参数：`USER_ID`（UUID）、`PROXYIP`（可选，Worker 回源用的反代 IP，
     不填则默认走 CF 的 fetch）
3. **绑定自定义域名**：在 Worker 设置里加 `proxy.olgroup.site`（CF 会自动建 DNS 记录并签证书）
   - ⚠️ 不要用 `*.workers.dev`，境内解析不了
4. **验证 Worker 存活**：`curl https://proxy.olgroup.site/<path>` 应返回 400/升级提示
5. **改 xray 出站**（`/usr/local/etc/xray/config.json`）：

```json
{
  "tag": "cfproxy",
  "protocol": "vless",
  "settings": {
    "vnext": [{
      "address": "proxy.olgroup.site",
      "port": 443,
      "users": [{ "id": "<UUID>", "encryption": "none" }]
    }]
  },
  "streamSettings": {
    "network": "ws",
    "security": "tls",
    "tlsSettings": { "serverName": "proxy.olgroup.site", "fingerprint": "chrome" },
    "wsSettings": { "path": "/<path>", "headers": { "Host": "proxy.olgroup.site" } }
  }
}
```

6. **routing 把 `proxy` 出站的 tag 换成 `cfproxy`**，`systemctl restart xray-proxy`
7. **验证**：
   - `curl -x http://127.0.0.1:10809 https://ipinfo.io/ip` 应返回 Cloudflare 的 IP
   - `curl -x http://127.0.0.1:10809 -o /dev/null -w "%{speed_download}" https://speed.cloudflare.com/__down?bytes=1000000`
     期望 ≥ 300 KB/s（对比现在 3 KB/s）
   - 最后再开 `lens` 跑一次 `fish-find-chicken`

### 4.3 预期效果

- `lens`：页面加载从 70 秒超时 → 应在 10 秒内完成
- OpenAI：**仍然会被拒**——CF 的出口 IP 是共享的，OpenAI 对 CF 段普遍判定 `unsupported_country_region_territory`
  或直接 403。这条别指望
- 抓网页/API：正常

## 5. 风险与代价

| 风险 | 说明 |
|---|---|
| CF 免费额度 | 10 万请求/天。`lens` 一次查询几十个请求，个人用够；群大了会超 |
| **违反 CF 条款** | Cloudflare 明确禁止用 Workers 承载大量非 HTTP 代理流量。流量大可能封号，**牵连 `olgroup.site` 整个域名** |
| 出口 IP 是共享的 | 目标站看到的是 CF 的 IP，反爬严格的站会直接给验证码 |
| 延迟 | 多了一跳 CF 回源，RTT 比直连节点高，但胜在稳定 |

## 6. 回滚

方案 B 的所有改动都在两处，回滚 5 分钟：

```bash
# 1. xray 出站换回原来的 vless reality 节点
cp /usr/local/etc/xray/config.json.bak-* /usr/local/etc/xray/config.json
systemctl restart xray-proxy

# 2. CF 侧：Worker 解绑自定义域名 / 删除 Worker（不影响域名其他解析）
```

## 7. 当前状态

- 香港节点仍在用（`xray-proxy.service` 正常运行），只是慢；机器人访问境内不受影响
- `fish-find-chicken` 插件**已按你的要求回滚移除**，本方案若落地再重新装
- 本文档只是方案，**未部署任何 Cloudflare 资源**
