# 鱼鱼找鸡（coser-image-search）

给一张图反查 coser 的社媒账号。**不是 NoneBot 插件**，是个独立 Node 包，
由 `src/plugins/fish_find/` 这层 Python 包装调起来。

- 上游：`鱼鱼找鸡-插件-v1.0.0.zip`（包名 `coser-image-search`，MIT）
- 装到：`/opt/qq-music-collector/vendor/fish-find-chicken/`
- Python 侧：`src/plugins/fish_find/__init__.py`

## 为什么不用它自带的 OneBot 适配器

`adapters/onebot.mjs` 是给 Node 机器人写的，会自己调 OneBot 接口发消息（先「收到，开始找鸡」
再发结果）。本项目唯一的发送出口是 `bot_utils.send_reply_parts`，绕过它就没有拆条、
没有送达记账。所以 Python 侧改成「取图 → 存临时文件 → `bin/fish.mjs --json` → 只取 `text`」。

## 安装

```bash
cd /opt/qq-music-collector
mkdir -p vendor && rm -rf vendor/fish-find-chicken
unzip -q 鱼鱼找鸡-插件-v1.0.0.zip -d /tmp/fishx
mv "/tmp/fishx/鱼鱼找鸡-1.0.0" vendor/fish-find-chicken

cat > vendor/fish-find-chicken/config.json <<'EOF'
{
  "enabled": true,
  "engines": ["lens", "saucenao", "tracemoe"],
  "timeoutMs": 30000,
  "maxAccounts": 3,
  "strictConsensus": true,
  "proxy": "http://127.0.0.1:10809",
  "browserExe": "/usr/bin/chromium-browser",
  "foundImage": "",
  "sendOnMiss": false
}
EOF
```

`proxy` 指向本机 Xray（见 `deploy/xray/`），只给浏览器用，不动系统代理。

## 两个必须打的补丁（重装后要重做）

**1. `--no-sandbox`（改上游源码）**

服务器以 root 跑，chromium 不加 `--no-sandbox` 会直接退出：

```
ERROR:zygote_host_impl_linux.cc(100)] Running as root without --no-sandbox is not supported.
```

外部看到的现象是「浏览器没起来（等不到 DevToolsActivePort）」。在
`src/fish-find-chicken.js` 的 `startBrowser()` 参数表里，`'--headless=new',` 之后插入：

```js
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
```

（原文件已备份为 `src/fish-find-chicken.js.bak`。）

**2. `NODE_OPTIONS=--experimental-websocket`（Python 侧自动加）**

Node 20 的全局 `WebSocket` 还在实验开关后面，不开就报 `WebSocket is not defined`
（插件用裸 CDP 驱动浏览器，全靠它）。`src/plugins/fish_find` 会检测 node 主版本，
低于 21 才自动追加，不用手工设。

## 引擎现状

| 引擎 | 状态 |
|---|---|
| `saucenao` | 直连可用，2 秒出结果（索引偏动漫图） |
| `tracemoe` | 直连可用（只认动画截屏） |
| `lens` | **能起浏览器，但导航到 `www.google.com` 会超时**（香港节点拉子资源慢），实测 108 秒后放弃 |

`lens` 是最能认真人 coser 的那条路。要它可用，得先让 `www.google.com` 在浏览器里
正常加载（换日本/新加坡节点，或对 gstatic 之类做直连分流）。当前配置下它只是拖时间，
嫌慢就把 `config.json` 的 `engines` 改成 `["saucenao","tracemoe"]`。

## 用法

群里**引用一张图**，发 `/鱼鱼找鸡`（或「鱼鱼找鸡」「找鸡」）。

- 单次超时 240 秒（`FISH_FIND_TIMEOUT` 可覆盖）
- 同一个人 30 秒冷却（防刷 SauceNAO 免费额度）
- 全局串行：同时只有一次查询在跑（每个查询都要开 chromium）
