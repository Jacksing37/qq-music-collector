"""把网页链接抓成模型能读的文字。

只做一件事：给一个 http(s) 链接，尽量拿回「标题 + 正文纯文本」。
拿不到就返回 ``None``——抓网页是锦上添花，**任何失败都不该让群里没回应**，
所以这里所有异常都自己吞掉，只留一条 DEBUG/WARNING 日志。

安全：这是「让模型的内容去访问网络」，必须防 SSRF——
只允许 http/https 的公网地址，禁内网 / 环回 / 链路本地，重定向也逐跳检查
（否则 ``http://evil/redirect -> http://169.254.169.254/`` 就能读到云元数据）。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import ipaddress
import re
import socket
from typing import Optional
from urllib.parse import urlsplit

import httpx
from nonebot.log import logger

#: 单页最多下载这么多字节（防超大页面把内存和带宽吃光）。
#: 别调回 512KB：百度首页这类内联 CSS/JS 极多的页面正文在 500KB 之后，
#: 截断点落在 <style> 里既会漏 CSS、又会让正文一个字不剩（线上实测）。
MAX_BYTES = 1024 * 1024
#: 正文短于这个长度就当没抓到——多半是登录墙、JS 渲染的空壳或错误页
MIN_CHARS = 40
#: 最多跟几跳重定向（每跳都要重新做一次公网校验）
MAX_REDIRECTS = 3

#: 有些站点看到默认 UA 直接给 403，伪装成普通浏览器
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}

_RE_SCRIPT = re.compile(r"<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>", re.I | re.S)
#: 「有开无闭」的残块：页面被体积上限截断时最后一个 <style>/<script> 往往没有
#: 闭合标签，上面那对成对剥离会**整体失配**，于是 CSS / JS 原封不动漏进正文。
#: 截断点之后本来也没有正文可读，直接连同残余内容一起砍掉最干净。
_RE_ORPHAN = re.compile(r"<(script|style|noscript|svg|iframe)\b[^>]*>.*\Z", re.I | re.S)
_RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_RE_OG = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:(title|description)["\'][^>]*content=["\'](.*?)["\']',
    re.I | re.S,
)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_BLOCK = re.compile(
    r"</?(?:p|div|br|li|ul|ol|tr|td|th|table|h[1-6]|section|article|header|footer"
    r"|blockquote|pre|figure|figcaption)[^>]*>",
    re.I,
)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_SPACES = re.compile(r"[ \t\u00a0\u3000]+")
_RE_MANY_NL = re.compile(r"\n{3,}")


def is_public_host(host: str) -> bool:
    """主机名解析出来的**每一个**地址都必须是公网地址。

    只查字面量 IP 不够：``localtest.me`` 这种域名会解析到 127.0.0.1，
    ``nip.io`` 之类还能拼出 10.x。所以要么解析，要么拒绝。
    """
    name = (host or "").strip().strip("[]")
    if not name:
        return False
    try:
        infos = socket.getaddrinfo(name, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


async def host_is_public(host: str) -> bool:
    """``is_public_host`` 的异步版（DNS 查询丢到线程池，别卡住事件循环）。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, is_public_host, host)


def url_is_fetchable(url: str) -> bool:
    """粗筛：只放行 http/https 且带主机名的地址（不做 DNS）。"""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def html_to_text(raw: str, max_chars: int = 1500) -> tuple[str, str]:
    """HTML → ``(标题, 正文纯文本)``。不引第三方解析库，够用就行。"""
    title = ""
    og = _RE_OG.search(raw)
    if og and og.group(1).lower() == "title":
        title = html_lib.unescape(og.group(2))
    if not title:
        m = _RE_TITLE.search(raw)
        if m:
            title = html_lib.unescape(m.group(1))
    title = _RE_SPACES.sub(" ", title).strip()[:120]

    body = _RE_COMMENT.sub(" ", raw)
    body = _RE_SCRIPT.sub(" ", body)
    body = _RE_ORPHAN.sub(" ", body)
    body = _RE_BLOCK.sub("\n", body)
    body = _RE_TAG.sub(" ", body)
    body = html_lib.unescape(body)
    # 二次清理：不少站点把整段 HTML **转义**后塞进 JSON / 属性里（百度就这样），
    # 反转义之后才会「长出」<style>/<div>，一轮剥离清不掉，得再来一遍。
    body = _RE_SCRIPT.sub(" ", body)
    body = _RE_ORPHAN.sub(" ", body)
    body = _RE_BLOCK.sub("\n", body)
    body = _RE_TAG.sub(" ", body)
    body = html_lib.unescape(body)
    body = _RE_SPACES.sub(" ", body)
    lines = [ln.strip() for ln in body.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    text = _RE_MANY_NL.sub("\n\n", text).strip()

    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return title, text


def _decode(raw: bytes) -> str:
    """按常见中文编码依次尝试（很多老站点是 GBK，硬按 utf-8 会全是乱码）。"""
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


async def fetch_page(
    url: str,
    *,
    timeout: float = 8.0,
    max_chars: int = 1500,
    allow_private: bool = False,
) -> Optional[dict]:
    """抓一个网页，返回 ``{"url","title","text"}``；抓不到返回 ``None``。

    ``allow_private`` 只给测试用（本机起 http.server 验证解析逻辑），
    线上调用一律走默认值。
    """
    current = (url or "").strip()
    if not url_is_fetchable(current):
        return None

    try:
        async with httpx.AsyncClient(
            timeout=float(timeout), headers=_HEADERS, follow_redirects=False
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                host = urlsplit(current).hostname or ""
                if not allow_private and not await host_is_public(host):
                    logger.warning(f"[web] 拒绝抓取内网/非法地址: {current[:80]}")
                    return None
                async with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        target = resp.headers.get("location") or ""
                        current = str(httpx.URL(current).join(target))
                        if not url_is_fetchable(current):
                            return None
                        continue  # 下一跳重新做公网校验
                    if resp.status_code >= 400:
                        logger.debug(f"[web] 抓取失败 {resp.status_code}: {current[:80]}")
                        return None
                    ctype = (resp.headers.get("content-type") or "").lower()
                    # 只吃文本：图片 / 视频 / 压缩包 / 文件下载一律不碰
                    if ctype and not any(k in ctype for k in ("html", "text/plain", "xml")):
                        logger.debug(f"[web] 不是网页（{ctype}），跳过: {current[:80]}")
                        return None
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= MAX_BYTES:
                            break
                    raw = b"".join(chunks)
                    break
            else:
                logger.debug(f"[web] 重定向过多: {url[:80]}")
                return None
    except httpx.TimeoutException:
        logger.debug(f"[web] 抓取超时（{timeout:g}s）: {current[:80]}")
        return None
    except httpx.HTTPError as exc:
        logger.debug(f"[web] 抓取出错: {exc}")
        return None

    title, text = html_to_text(_decode(raw), max_chars)
    if len(text) < MIN_CHARS:
        logger.debug(f"[web] 正文太短（{len(text)} 字），当没抓到: {current[:80]}")
        return None
    return {"url": current, "title": title or current, "text": text}


async def fetch_pages(
    urls: list[str],
    *,
    limit: int = 2,
    timeout: float = 8.0,
    max_chars: int = 1500,
) -> list[dict]:
    """并发抓前 ``limit`` 个链接（默认 2 个），失败的自动跳过。"""
    if limit <= 0:
        return []
    unique: list[str] = []
    seen_hosts: set[str] = set()
    for url in dict.fromkeys(urls or []):
        if not url_is_fetchable(url):
            continue
        # 同一站点只抓一次：一条消息里贴 3 个同站链接不该各占一份配额
        host = (urlsplit(url).hostname or "").lower()
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        unique.append(url)
        if len(unique) >= limit:
            break
    if not unique:
        return []
    results = await asyncio.gather(
        *(fetch_page(u, timeout=timeout, max_chars=max_chars) for u in unique),
        return_exceptions=True,
    )
    return [r for r in results if isinstance(r, dict)]
