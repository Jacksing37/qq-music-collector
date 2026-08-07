"""音乐链接识别器。

群里分享音乐主要有三种形态，这里全部覆盖：
1. 纯文本链接（含各平台短链，需要 302 展开）
2. 小程序 / 结构化分享卡片（OneBot 的 ``json`` 消息段）
3. 音乐消息段（OneBot 的 ``music`` 消息段）
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

import httpx

from .models import MusicLink

# ---------------------------------------------------------------- URL 提取

_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff\"'<>，。、）】\]]+")

# 结尾常见的粘连标点，需要剥掉
_TRAILING_JUNK = ".,;:!?)}]>\u3002\uff0c\uff09"

# 需要跟随 302 才能拿到真实地址的短链域名
SHORT_LINK_HOSTS = {
    "163cn.tv",
    "c6.y.qq.com",
    "c.y.qq.com",
    "url.cn",
    "t1.kugou.com",
    "v.douyin.com",
    "m.tb.cn",
}

# (平台, 字段名, 正则)
_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    # 网易云音乐
    ("netease", "song_id", re.compile(r"music\.163\.com/(?:#/)?(?:m/)?song/?\?[^#\s]*\bid=(\d+)", re.I)),
    ("netease", "song_id", re.compile(r"music\.163\.com/(?:#/)?(?:m/)?song/(\d+)", re.I)),
    ("netease", "song_id", re.compile(r"music\.163\.com/song/media/outer/url\?id=(\d+)", re.I)),
    # QQ 音乐
    ("qq", "song_mid", re.compile(r"y\.qq\.com/n/ryqq/songDetail/([0-9A-Za-z]+)", re.I)),
    ("qq", "song_mid", re.compile(r"y\.qq\.com/n/yqq/song/([0-9A-Za-z]+)\.html", re.I)),
    ("qq", "song_mid", re.compile(r"[?&]songmid=([0-9A-Za-z]+)", re.I)),
    ("qq", "song_mid", re.compile(r"[?&]songMid=([0-9A-Za-z]+)")),
    ("qq", "song_id", re.compile(r"[?&]songid=(\d+)", re.I)),
    # 酷狗
    ("kugou", "song_hash", re.compile(r"kugou\.com/[^\s]*[#?&]hash=([0-9A-Fa-f]{32})", re.I)),
    ("kugou", "song_hash", re.compile(r"kugou\.com/mixsong/([0-9A-Za-z]+)", re.I)),
    # 酷我
    ("kuwo", "song_id", re.compile(r"kuwo\.cn/(?:newh5app/)?play_detail/(\d+)", re.I)),
    ("kuwo", "song_id", re.compile(r"kuwo\.cn/yinyue/(\d+)", re.I)),
    # 汽水音乐
    ("qishui", "song_id", re.compile(r"qishui\.douyin\.com/s/([0-9A-Za-z_-]+)", re.I)),
]

# 只要域名命中就认为是音乐链接（用于短链展开后仍解析不出 id 的兜底）
_HOST_HINTS: list[tuple[str, str]] = [
    ("music.163.com", "netease"),
    ("y.music.163.com", "netease"),
    ("163cn.tv", "netease"),
    ("y.qq.com", "qq"),
    ("kugou.com", "kugou"),
    ("kuwo.cn", "kuwo"),
    ("qishui.douyin.com", "qishui"),
]


def _clean_url(url: str) -> str:
    return url.rstrip(_TRAILING_JUNK)


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", url, re.I)
    return m.group(1).lower() if m else ""


def is_short_link(url: str) -> bool:
    return _host_of(url) in SHORT_LINK_HOSTS


def match_url(url: str) -> Optional[MusicLink]:
    """把单个 URL 匹配成 MusicLink，匹配不到返回 None。"""
    url = _clean_url(url)
    for platform, field, pattern in _PATTERNS:
        m = pattern.search(url)
        if m:
            link = MusicLink(platform=platform, raw=url)
            setattr(link, field, m.group(1))
            return link
    host = _host_of(url)
    for host_frag, platform in _HOST_HINTS:
        if host == host_frag or host.endswith("." + host_frag):
            return MusicLink(platform=platform, raw=url)
    return None


def extract_urls(text: str) -> list[str]:
    return [_clean_url(u) for u in _URL_RE.findall(text or "")]


def extract_from_text(text: str) -> list[MusicLink]:
    """从纯文本里抽取音乐链接。"""
    results: list[MusicLink] = []
    for url in extract_urls(text):
        link = match_url(url)
        if link:
            results.append(link)
    return results


# ---------------------------------------------------------------- 卡片解析

_HINT_TITLE_KEYS = ("title", "musicTitle", "song_name")
_HINT_DESC_KEYS = ("desc", "singer", "author", "summary")
_HINT_COVER_KEYS = ("preview", "cover", "picUrl", "image", "icon", "source_icon")


def _walk_strings(node: Any) -> Iterable[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_strings(value)


def _first_key(node: Any, keys: tuple[str, ...]) -> Optional[str]:
    """深度优先找第一个非空的目标键。"""
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in node.values():
            found = _first_key(value, keys)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _first_key(value, keys)
            if found:
                return found
    return None


def extract_from_card(data: Any) -> list[MusicLink]:
    """从分享卡片 JSON 中抽取音乐链接。

    不假设具体的卡片结构（structmsg / miniapp_01 / detail_1 各不相同），
    直接递归扫描所有字符串找 URL，命中平台规则就算数。
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(data, (dict, list)):
        return []

    hint_title = _first_key(data, _HINT_TITLE_KEYS)
    hint_artist = _first_key(data, _HINT_DESC_KEYS)
    hint_cover = _first_key(data, _HINT_COVER_KEYS)

    seen: set[str] = set()
    results: list[MusicLink] = []
    for raw in _walk_strings(data):
        # 卡片里的 url 常被转义成 \/ 形式
        candidate = raw.replace("\\/", "/")
        for url in extract_urls(candidate):
            link = match_url(url)
            if not link or link.key in seen:
                continue
            seen.add(link.key)
            link.hint_title = hint_title
            link.hint_artist = hint_artist
            link.hint_cover = hint_cover if hint_cover and hint_cover.startswith("http") else None
            results.append(link)
    return results


# ---------------------------------------------------------------- 短链展开

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


async def resolve_short_link(url: str, timeout: float = 8.0) -> str:
    """跟随重定向拿到最终地址，失败时原样返回。"""
    headers = {"User-Agent": _UA}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, headers=headers
        ) as client:
            try:
                resp = await client.head(url)
                # 部分服务器对 HEAD 返回 405，改用 GET
                if resp.status_code >= 400:
                    resp = await client.get(url)
            except httpx.HTTPError:
                resp = await client.get(url)
            return str(resp.url)
    except Exception:
        return url


async def expand_links(links: list[MusicLink]) -> list[MusicLink]:
    """对缺少 id 的短链做一次 302 展开并重新匹配。"""
    expanded: list[MusicLink] = []
    for link in links:
        # 没解析出 id 的一律尝试展开：短链域名之外，长链带跳转参数的情况也常见
        needs_expand = link.song_id is None and link.song_mid is None and link.song_hash is None
        if needs_expand:
            final_url = await resolve_short_link(link.raw)
            if final_url != link.raw:
                re_matched = match_url(final_url)
                if re_matched:
                    re_matched.hint_title = link.hint_title
                    re_matched.hint_artist = link.hint_artist
                    re_matched.hint_cover = link.hint_cover
                    expanded.append(re_matched)
                    continue
                link.raw = final_url
        expanded.append(link)
    return expanded


# ---------------------------------------------------------------- 消息级入口


async def detect_from_segments(segments: Iterable[dict]) -> list[MusicLink]:
    """输入 OneBot 消息段列表（dict 形式），输出去重后的音乐链接。

    抽成纯 dict 输入是为了方便离线测试，不依赖 nonebot 运行时。
    """
    found: list[MusicLink] = []
    for seg in segments:
        seg_type = seg.get("type")
        data = seg.get("data") or {}
        if seg_type == "text":
            found.extend(extract_from_text(str(data.get("text", ""))))
        elif seg_type == "json":
            found.extend(extract_from_card(data.get("data")))
        elif seg_type == "music":
            platform_map = {"163": "netease", "qq": "qq", "kugou": "kugou", "kuwo": "kuwo"}
            mtype = str(data.get("type", ""))
            if mtype in platform_map:
                link = MusicLink(platform=platform_map[mtype], raw=str(data.get("url", "")))
                link.song_id = str(data.get("id")) if data.get("id") else None
                found.append(link)
            elif data.get("url"):
                link = match_url(str(data["url"]))
                if link:
                    link.hint_title = data.get("title")
                    link.hint_artist = data.get("content")
                    link.hint_cover = data.get("image")
                    found.append(link)
        elif seg_type == "forward":
            # 合并转发内部内容由调用方展开后再传进来
            continue

    if not found:
        return []

    found = await expand_links(found)

    # 去重：同一条消息里同一首歌只算一次
    unique: list[MusicLink] = []
    seen: set[tuple[str, str]] = set()
    for link in found:
        sig = (link.platform, link.key)
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(link)
    return unique
