"""``fetch_url`` 工具的后端：抓一个网页，返回标题 + 正文。

**这里的东西不会被自动调用。** 只有模型在需要时通过 tool calling 主动要求
抓某个 URL（见 :mod:`tools`）。所以不需要「批量抓」「同站点去重」这类策略，
只负责「一个 URL → 一段可读文本」。

正文抽取交给 **trafilatura**（业界通用的正文抽取库），不再手写正则。
手写那套要自己面对转义、截断一半的 `<style>`、内联 JSON 里的 CSS……
每踩一个坑补一条正则，而 trafilatura 已经把这些都处理过了。

安全上仍然自己做 SSRF 防护——这不是能用库解决的问题：只放行 http/https 的
**公网**地址，内网 / 环回 / 链路本地 / 保留段一律拒绝，**重定向逐跳复核**
（不能让 httpx 自己跟，否则一条 `http://内网/redirect` 就能读到云元数据）。
"""

import asyncio
import ipaddress
import socket
from typing import Optional
from urllib.parse import urlsplit

import httpx
from nonebot.log import logger

try:  # 正文抽取库；装了才用，没装就报「抓不到」而不是整个插件起不来
    from trafilatura import extract as _extract
    from trafilatura import extract_metadata as _extract_metadata
except Exception:  # noqa: BLE001 — ImportError / 依赖版本问题都按「没装」处理
    _extract = None
    _extract_metadata = None

#: 单页最多下载多少字节（防超大页面把内存和带宽吃光）
MAX_BYTES = 2 * 1024 * 1024
#: 抽出来的正文短于这个长度就当没抓到——多半是登录墙、JS 空壳或错误页
MIN_CHARS = 40
#: 最多跟几跳重定向（每跳都要重新做一次公网校验）
MAX_REDIRECTS = 3

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}


def available() -> bool:
    """抓取能力是否就绪（trafilatura 装好了没）。网页端状态区会显示这个。"""
    return _extract is not None


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


def _decode(raw: bytes) -> str:
    """按常见中文编码依次尝试（很多老站点是 GBK，硬按 utf-8 会全是乱码）。"""
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_content(html: str, url: str = "") -> tuple[str, str]:
    """HTML → ``(标题, 正文)``；抽不出正文返回 ``("", "")``。

    抽不出来**不是异常**：很多站点是 JS 渲染的空壳，浏览器里能看到、
    抓下来只有一个空 div。这种情况让上层报「抓不到」即可。
    """
    if _extract is None:
        logger.warning("[web] 没装 trafilatura，无法抽取正文（pip install trafilatura）")
        return "", ""
    text = ""
    try:
        text = _extract(
            html,
            url=(url or None),
            include_comments=False,
            include_tables=True,
            deduplicate=True,
            favor_precision=True,
        ) or ""
    except Exception as exc:  # noqa: BLE001 — 抽取失败按「没抓到」处理，不能掀翻调用方
        logger.debug(f"[web] 正文抽取失败: {exc}")
        return "", ""
    title = ""
    try:
        meta = _extract_metadata(html)
        title = (getattr(meta, "title", "") or "") if meta is not None else ""
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[web] 标题抽取失败: {exc}")
    lines = [ln.strip() for ln in text.splitlines()]
    return " ".join(t for t in title.split() if t).strip(), "\n".join(ln for ln in lines if ln)


async def fetch_page(
    url: str,
    *,
    timeout: float = 15.0,
    max_chars: int = 2000,
    allow_private: bool = False,
) -> Optional[dict]:
    """抓一个网页，返回 ``{"url","title","text"}``；抓不到返回 ``None``。

    ``allow_private`` 只给测试用（本机起 http.server 验证解析与 SSRF 逻辑），
    线上调用一律走默认值。
    """
    current = (url or "").strip()
    if not url_is_fetchable(current):
        return None

    try:
        async with httpx.AsyncClient(
            timeout=float(timeout), headers=_HEADERS, follow_redirects=False
        ) as client:
            raw = b""
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

    title, text = extract_content(_decode(raw), current)
    if len(text) < MIN_CHARS:
        logger.debug(f"[web] 正文太短（{len(text)} 字），按没抓到处理: {current[:80]}")
        return None
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return {"url": current, "title": title[:120], "text": text}
