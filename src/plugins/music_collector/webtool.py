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

三处细节是照着 WorkBuddy 内置 WebFetch 的本地管线补的（那份实现见
``webfetch-local-pipeline.mjs``），因为它们确实解决了真问题：

- **非文本资源要说明白**（:func:`classify_resource_kind`）：图片 / PDF / 音视频
  以前统一返回 ``None``，模型只知道「抓不到」，会反复重试或者怪网络。现在明确
  告诉它「这是图片，别抓了」；
- **meta 兜底**（:func:`extract_meta_content`）：B 站、知乎这类页面的正文是 JS
  渲染的，trafilatura 只抽出标题（实测 31 字），以前会被 ``MIN_CHARS`` 判成没抓到。
  可 ``<meta description>`` 里明明写着简介和播放量。抽不出正文就退回 meta；
- **15 分钟缓存**：群里同一个链接常被几个人先后贴上，模型也可能在两轮里抓两次。
  同一 URL 十五分钟内只真抓一次。

刻意**没有**照搬的两点：turndown 转 markdown（B 站实测 6678 字里九成是导航和
「接下来播放」的推荐位，比纯文本更吵）、跨主机重定向直接报错（我们的逐跳复核
已经把安全性握在自己手里，没必要因此失败）。
"""

import asyncio
import ipaddress
import re
import socket
import time
from collections import OrderedDict
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

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
#: 抽出来的正文短于这个长度就当没抓到——多半是登录墙、JS 空壳或错误页。
#: 注意：先走 :func:`extract_meta_content` 兜一次，兜不到才算没抓到
MIN_CHARS = 40
#: 最多跟几跳重定向（每跳都要重新做一次公网校验）
MAX_REDIRECTS = 3

#: 缓存多久（秒）。0 = 不缓存
CACHE_TTL = 900.0
#: 缓存最多存多少条 URL
CACHE_MAX = 64
#: 缓存里单条正文最多存多少字（再长就截，反正 fetch_chars 默认才 2000）
CACHE_TEXT_LIMIT = 20000

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}

#: 会按「有没有设备 cookie」判断爬虫的站点。**同一个机房 IP**，带一个随机的
#: ``buvid3`` 就是 200，不带就是 412 —— 实测阿里云出口访问 B 站，光换 UA 没用，
#: 加上这个 cookie 立刻正常。值本来是站点下发的设备 id，首次访问者也没有，
#: 我们自己造一个，只为让请求看起来像普通浏览器第一次来。
_DEVICE_COOKIE_HOSTS = ("bilibili.com",)
#: 进程内复用的设备 cookie（同一次访问里换值反而更像异常）
_DEVICE_COOKIE: str = ""


def _buvid3() -> str:
    global _DEVICE_COOKIE
    if not _DEVICE_COOKIE:
        import uuid

        _DEVICE_COOKIE = f"buvid3={uuid.uuid4().hex.upper()}infoc"
    return _DEVICE_COOKIE


def headers_for(url: str) -> dict:
    """按目标站点补请求头。

    目前只对 B 站补 ``Referer`` + 设备 cookie：它的 412 不是「你被封了 IP」那么简单，
    裸请求（哪怕 UA 正常）一律 412，补上这两样就能拿到正常页面。
    其他站点保持原样，免得给所有网站都带一个莫名其妙的 cookie。
    """
    headers = dict(_HEADERS)
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return headers
    for base in _DEVICE_COOKIE_HOSTS:
        if host == base or host.endswith("." + base):
            headers["Referer"] = f"https://{base}/"
            headers["Origin"] = f"https://{base}"
            headers["Cookie"] = _buvid3()
            break
    return headers

#: 明确是「二进制 / 文档」的 Content-Type，不算网页。
#: 和 ``image/*`` / ``video/*`` / ``audio/*`` 一起归到非文本
_BINARY_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-gzip",
        "application/x-tar",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/vnd.rar",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)

#: 这些状态码表示「站点拒绝抓取」，不是「地址不存在」。区别要说给模型听：
#: 前者别重试、换个来源；后者该老实说页面没了
_BLOCKED_STATUS = frozenset({401, 403, 407, 412, 429, 451})

#: Content-Type → 扩展名（只用于提示语，不落盘）
_EXT_BY_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
}


def available() -> bool:
    """抓取能力是否就绪（trafilatura 装好了没）。网页端状态区会显示这个。"""
    return _extract is not None


# ---------------------------------------------------------------- 资源分类


def normalize_content_type(content_type: str) -> str:
    """``text/html; charset=utf-8`` → ``text/html``（小写）。"""
    return (content_type or "").split(";")[0].strip().lower()


def classify_resource_kind(content_type: str) -> str:
    """把 Content-Type 归成 ``text`` / ``image`` / ``binary``。

    空值按 ``text`` 处理：很多站点压根不发 Content-Type，先当下载看看。
    """
    ct = normalize_content_type(content_type)
    if not ct:
        return "text"
    if ct.startswith("image/"):
        return "image"
    if ct.startswith(("video/", "audio/")) or ct in _BINARY_TYPES:
        return "binary"
    return "text"


def suggested_extension(content_type: str) -> str:
    """给这个 Content-Type 猜个扩展名（没有就空串）。"""
    ct = normalize_content_type(content_type)
    if ct in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[ct]
    if ct.startswith("image/"):
        sub = ct[6:]
        if sub and all(ch.isalnum() or ch in ".+-" for ch in sub):
            return f".{sub}"
    return ""


def non_text_guidance(url: str, content_type: str, kind: str) -> str:
    """非文本资源回给模型的话。

    **重点是「别再抓了」**。以前这里返回 ``None``，模型收到的是笼统的「抓不到」，
    于是它会换着法子重试同一个图片地址，或者跟群友说「网络打不开」——两种都不对。
    """
    ct = normalize_content_type(content_type) or kind
    ext = suggested_extension(content_type) or (".png" if kind == "image" else "")
    detail = f"Content-Type: {ct}" + (f"，差不多是 {ext} 文件" if ext else "")
    if kind == "image":
        return (
            f"这个地址是一张图片（{detail}），抓下来没有可读的文字。\n"
            "**不要重复抓同一个地址**。想知道图里是什么就让发图的人说一下；"
            "你可以直接说你看不到图片里的内容，不要猜。"
        )
    return (
        f"这个地址是文件或音视频（{detail}），不是网页，抓不到正文。\n"
        "**不要重复抓同一个地址**。如果是 PDF / 文档，请发的人把关键内容贴成文字；"
        "视频和音频同理，你读不了里面的内容。"
    )


def blocked_guidance(url: str, status: int) -> str:
    """站点明确拒绝抓取（412 / 403 / 429 …）时回给模型的话。

    和「地址不存在」要分开说：这种**不是地址写错**，重试也没用，而且很多站
    （B 站、知乎）对机房 IP 就是这个反应。
    """
    hint = "（更像限流，隔几分钟再试可能有戏）" if status == 429 else ""
    return (
        f"{url} 这个站点拒绝了抓取（HTTP {status}）{hint}。\n"
        "这**不是地址写错**，多半是开了反爬或拦机房 IP，**反复重试也没用**。\n"
        "可以告诉用户你看不了这个站，请他把关键内容贴成文字；"
        "或者换个来源（别的地方也讲过同一件事）。不要编造里面的内容。"
    )


# ---------------------------------------------------------------- meta 兜底

_META_PATTERNS = (
    ("Title", r"<title[^>]*>([^<]+)</title>"),
    ("Description", r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"']([^\"']+)[\"']"),
    ("Description", r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+name=[\"']description[\"']"),
    ("OG Title", r"<meta[^>]+(?:property|name)=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)[\"']"),
    ("OG Title", r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']og:title[\"']"),
    (
        "OG Description",
        r"<meta[^>]+(?:property|name)=[\"']og:description[\"'][^>]+content=[\"']([^\"']+)[\"']",
    ),
    (
        "OG Description",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']og:description[\"']",
    ),
    ("Keywords", r"<meta[^>]+name=[\"']keywords[\"'][^>]+content=[\"']([^\"']+)[\"']"),
    ("Keywords", r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+name=[\"']keywords[\"']"),
)


def extract_meta_content(html: str) -> str:
    """正文抽不出来时，退而取页面自己的元信息。取不到返回空串。

    拼成 ``Title: … / Description: …`` 这样的多行文本。**重复内容只留一份**：
    很多站点 ``description`` 和 ``og:description`` 完全一样，两个都列出来除了费
    token 没别的用。
    """
    if not html:
        return ""
    seen: set[str] = set()
    lines: list[str] = []
    for label, pattern in _META_PATTERNS:
        try:
            match = re.search(pattern, html, re.I | re.S)
        except re.error:  # pragma: no cover — 模式是写死的，不该发生
            continue
        if not match:
            continue
        value = " ".join((match.group(1) or "").split())
        if not value or value in seen:
            continue
        seen.add(value)
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------- URL / 缓存


def normalize_url(url: str) -> str:
    """缓存键：小写协议与主机、去掉 fragment 和尾斜杠。

    ⚠️ **保留 query**，这点和原版不一样。原版为了「同一个页面」把 ``?…`` 整个丢掉，
    但大量站点的内容就在 query 里（``?id=123``、``?p=2``），丢了会把两个不同页面
    当成同一个，缓存命中后返回错误内容。宁可少命中，不能返回错的。
    """
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return raw
    if not scheme or not host:
        return raw
    default = 80 if scheme == "http" else 443
    if ":" in host and not host.startswith("["):  # IPv6 字面量要加回方括号
        host = f"[{host}]"
    netloc = host if port in (None, default) else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


#: 缓存：normalize_url → (过期时刻, 结果)。用 OrderedDict 当 LRU
_CACHE: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()


def _cache_get(key: str) -> Optional[dict]:
    entry = _CACHE.get(key)
    if not entry:
        return None
    expire_at, payload = entry
    if expire_at < time.time():
        _CACHE.pop(key, None)
        return None
    _CACHE.move_to_end(key)
    return dict(payload)


def _cache_put(key: str, payload: dict, ttl: float = 0.0) -> None:
    """存一条缓存。``ttl`` <= 0 用默认的 :data:`CACHE_TTL`。"""
    ttl = float(ttl) if ttl and ttl > 0 else CACHE_TTL
    if ttl <= 0:
        return
    stored = dict(payload)
    text = stored.get("text") or ""
    if len(text) > CACHE_TEXT_LIMIT:
        stored["text"] = text[:CACHE_TEXT_LIMIT]
    _CACHE[key] = (time.time() + ttl, stored)
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_MAX:
        _CACHE.popitem(last=False)


def clear_cache() -> None:
    """清空缓存（网页端不用，测试与排障用）。"""
    _CACHE.clear()


def cache_size() -> int:
    return len(_CACHE)


# ---------------------------------------------------------------- SSRF


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


# ---------------------------------------------------------------- 抽取


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


def clip_text(text: str, max_chars: int) -> str:
    """按字数截断并加省略号（``max_chars <= 0`` 表示不截）。"""
    if max_chars and max_chars > 0 and len(text) > max_chars:
        return text[:max_chars].rstrip() + "…"
    return text


async def fetch_page(
    url: str,
    *,
    timeout: float = 15.0,
    max_chars: int = 2000,
    allow_private: bool = False,
    cache_minutes: float = 15.0,
) -> Optional[dict]:
    """抓一个网页，返回一页的抓取结果；真的抓不到返回 ``None``。

    返回的 dict：

    - ``url``：跟完重定向后的最终地址
    - ``title`` / ``text``：标题与正文（非文本资源时都是空）
    - ``kind``：``text`` / ``image`` / ``binary`` / ``blocked``（站点拒绝抓取）
    - ``content_type``：原始 Content-Type（提示语要用）
    - ``status``：仅 ``kind="blocked"`` 时有，HTTP 状态码
    - ``source``：``extract``（trafilatura 抽到正文）/ ``meta``（退回页面元信息）/
      ``cache``（命中缓存）
    - ``cached``：是否来自缓存

    ``allow_private`` 只给测试用（本机起 http.server 验证解析与 SSRF 逻辑），
    线上调用一律走默认值。
    """
    current = (url or "").strip()
    if not url_is_fetchable(current):
        return None

    # ⚠️ SSRF 校验必须在查缓存**之前**：缓存里存的是「上次抓成功的内容」，
    # 如果先命中缓存就直接返回，就绕过了「这个地址能不能抓」这道闸。
    # （测试里用 allow_private 抓本机时会真的写进缓存，正是靠这个顺序才拦得住）
    host = urlsplit(current).hostname or ""
    if not allow_private and not await host_is_public(host):
        logger.warning(f"[web] 拒绝抓取内网/非法地址: {current[:80]}")
        return None

    key = normalize_url(current)
    cache_ttl = float(cache_minutes) * 60.0 if cache_minutes and cache_minutes > 0 else 0.0
    if cache_ttl > 0:
        hit = _cache_get(key)
        if hit:
            hit["text"] = clip_text(hit.get("text") or "", max_chars)
            hit["cached"] = True
            hit["source"] = "cache"
            logger.debug(f"[web] 命中缓存（{cache_minutes:g} 分钟内）: {current[:80]}")
            return hit

    ctype = ""
    try:
        async with httpx.AsyncClient(
            timeout=float(timeout), headers=headers_for(current), follow_redirects=False
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
                    ctype = (resp.headers.get("content-type") or "").lower()
                    if resp.status_code >= 400:
                        # 412 / 403 / 429 是「对方不让我抓」，不是地址写错。这个区别
                        # 对模型很重要：前者别重试、换来源；后者该说页面不存在
                        if resp.status_code in _BLOCKED_STATUS:
                            logger.debug(
                                f"[web] 被站点拒绝 {resp.status_code}: {current[:80]}"
                            )
                            return {
                                "url": current,
                                "title": "",
                                "text": "",
                                "kind": "blocked",
                                "status": resp.status_code,
                                "content_type": ctype,
                                "source": "extract",
                                "cached": False,
                            }
                        logger.debug(f"[web] 抓取失败 {resp.status_code}: {current[:80]}")
                        return None
                    # 图片 / PDF / 音视频：**读不了**，但要让模型知道「读不了的是这种
                    # 东西」，而不是笼统的「抓不到」——后者会让它反复重试同一地址
                    kind = classify_resource_kind(ctype)
                    if kind != "text":
                        logger.debug(f"[web] 非文本资源（{ctype}），跳过: {current[:80]}")
                        return {
                            "url": current,
                            "title": "",
                            "text": "",
                            "kind": kind,
                            "content_type": ctype,
                            "source": "extract",
                            "cached": False,
                        }
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

    html = _decode(raw)
    title, text = extract_content(html, current)
    source = "extract"
    if len(text) < MIN_CHARS:
        # 正文抽不出来（JS 渲染 / 登录墙 / 视频页）→ 退回页面元信息。
        # B 站视频页就是典型：trafilatura 只抽出标题 31 字，而 meta description
        # 里写着简介和播放量，够回答「这视频讲什么」了
        meta = extract_meta_content(html)
        if len(meta) >= MIN_CHARS:
            logger.debug(f"[web] 正文过短（{len(text)} 字），改用页面元信息: {current[:80]}")
            text = meta
            source = "meta"
            if not title:
                first = meta.splitlines()[0] if meta else ""
                title = first.split(":", 1)[-1].strip()
        else:
            logger.debug(f"[web] 正文太短（{len(text)} 字），按没抓到处理: {current[:80]}")
            return None

    payload = {
        "url": current,
        "title": title[:120],
        "text": text,
        "kind": "text",
        "content_type": ctype,
        "source": source,
        "cached": False,
    }
    if cache_ttl > 0:
        _cache_put(key, payload, cache_ttl)
    payload["text"] = clip_text(text, max_chars)
    return payload
