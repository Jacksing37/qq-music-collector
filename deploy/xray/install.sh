#!/bin/bash
# 安装 Xray（VLESS REALITY 客户端）到本机，仅监听回环，做「国内直连 / 境外走代理」分流。
#
# 用法：
#   sudo bash deploy/xray/install.sh config.json [Xray-linux-64.zip]
#
#   config.json       真实配置（由 config.example.json 填好凭据后得到，**不要提交到仓库**）
#   Xray-linux-64.zip 可选，离线包路径；不给则自动从镜像下载最新版
#
# 特性：
#   - 只装二进制/地理数据/配置，不碰机器人代码
#   - 幂等：重复执行只会覆盖 xray 与配置，unit 用 SYSTEMD_UNIT 控制是否重写
set -u

CFG=${1:-}
PKG=${2:-}
ZIP_URL_BASE="https://gh-proxy.com/https://github.com/XTLS/Xray-core/releases/download"

if [ -z "$CFG" ] || [ ! -f "$CFG" ]; then
  echo "用法: sudo bash $0 <config.json> [Xray-linux-64.zip]"
  exit 1
fi
if [ "$(id -u)" != "0" ]; then echo "需要 root"; exit 1; fi

echo "== 1. 准备离线包"
if [ -z "$PKG" ] || [ ! -f "$PKG" ]; then
  echo "未提供本地包，从镜像取最新版"
  TAG=$(curl -s -m 30 "https://gh-proxy.com/https://api.github.com/repos/XTLS/Xray-core/releases/latest" \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)
  [ -z "$TAG" ] && { echo "取版本号失败，请手动下载 Xray-linux-64.zip 后作为第二个参数传入"; exit 1; }
  PKG=/tmp/Xray-linux-64.zip
  echo "版本 $TAG → $PKG"
  curl -sL -m 300 -o "$PKG" "$ZIP_URL_BASE/$TAG/Xray-linux-64.zip" || { echo "下载失败"; exit 1; }
fi
ls -lh "$PKG"

echo "== 2. 解包"
rm -rf /tmp/xray-pkg && mkdir -p /tmp/xray-pkg
PY=$(command -v python3 || echo /opt/qq-music-collector/.venv/bin/python)
"$PY" - "$PKG" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
z.extractall("/tmp/xray-pkg")
print([i.filename for i in z.infolist()])
PY

echo "== 3. 安装"
mkdir -p /usr/local/etc/xray /usr/local/share/xray
install -m 755 /tmp/xray-pkg/xray /usr/local/bin/xray
install -m 644 /tmp/xray-pkg/geoip.dat /usr/local/share/xray/geoip.dat
install -m 644 /tmp/xray-pkg/geosite.dat /usr/local/share/xray/geosite.dat
install -m 600 "$CFG" /usr/local/etc/xray/config.json
/usr/local/bin/xray version | head -2

echo "== 4. systemd 单元"
if [ "${SYSTEMD_UNIT:-1}" = "1" ]; then
  cp deploy/xray/xray-proxy.service /etc/systemd/system/xray-proxy.service 2>/dev/null \
    || cp "$(dirname "$0")/xray-proxy.service" /etc/systemd/system/xray-proxy.service
  systemctl daemon-reload
fi

echo "== 5. 配置自检"
/usr/local/bin/xray run -test -c /usr/local/etc/xray/config.json 2>&1 | tail -3

echo "== 6. 启动"
systemctl enable xray-proxy >/dev/null 2>&1
systemctl restart xray-proxy
sleep 3
systemctl is-active xray-proxy

echo "== 7. 验证"
curl -s -m 20 -x http://127.0.0.1:10809 https://ipinfo.io/json || echo "代理出口检测失败"
echo
curl -s -m 15 -x http://127.0.0.1:10809 -o /dev/null -w "music.163.com -> %{http_code}（应为 200，走直连）\n" https://music.163.com
