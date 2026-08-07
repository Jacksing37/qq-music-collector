"""音乐链接识别器。

群里分享音乐主要有四种形态，这里全部覆盖：
1. 纯文本链接（含各平台短链，需要 302 展开）
2. 小程序 / 结构化分享卡片（OneBot 的 ``json`` 消息段）
3. 旧版 XML 结构化卡片（OneBot 的 ``xml`` 消息段）
4. 音乐消息段（OneBot 的 ``music`` 消息段）
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .models import MusicLink

try:  # 运行在 nonebot 里用它的 logger，离线测试时退化成标准库
    from nonebot.log import logger as _nb_logger

    logger: Any = _nb_logger
except Exception:  # pragma: no cover
    logger = logging.getLogger("music_collector.detector")

#: 打开后会把每一步识别过程写进日志，排查"分享了却没反应"时很有用
DEBUG = False


def set_debug(enabled: bool) -> None:
    global DEBUG
    DEBUG = enabled


def _dbg(msg: str) -> None:
    if DEBUG:
        logger.info(f"[music/detect] {msg}")


# ---------------------------------------------------------------- URL 提取

_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff\"'<>，。、）】\]\\]+")

# 结尾常见的粘连标点，需要剥掉
_TRAILING_JUNK = ".,;:!?)}]>\u3002\uff0c\uff09"

# 需要跟随 302 才能拿到真实地址的短链域名
SHORT_LINK_HOSTS = {
    "163cn.tv",
    "c6.y.qq.com",
    "c.y.qq.com",
    "c7.y.qq.com",
    "url.cn",
    "t1.kugou.com",
    "t2.kugou.com",
    "v.douyin.com",
    "m.tb.cn",
    "w.url.cn",
}

# QQ 音乐的 mid 是 14 位 base62，这里放宽到 10~20 位
_QQ_MID = r"[0-9A-Za-z]{10,20}"

# (平台, 字段名, 正则)
_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    # ---- 网易云音乐 ----
    ("netease", "song_id", re.compile(r"music\.163\.com/(?:#/)?(?:m/)?song/?\?[^#\s]*\bid=(\d+)", re.I)),
    ("netease", "song_id", re.compile(r"music\.163\.com/(?:#/)?(?:m/)?song/(\d+)", re.I)),
    ("netease", "song_id", re.compile(r"music\.163\.com/song/media/outer/url\?id=(\d+)", re.I)),
    # ---- QQ 音乐（按可信度从高到低）----
    ("qq", "song_mid", re.compile(rf"y\.qq\.com/n/ryqq/songDetail/({_QQ_MID})", re.I)),
    ("qq", "song_mid", re.compile(rf"y\.qq\.com/n/yqq/song/({_QQ_MID})\.html", re.I)),
    ("qq", "song_mid", re.compile(rf"y\.qq\.com/n/m/detail/(?:taoge|song)/[^/]*/({_QQ_MID})", re.I)),
    ("qq", "song_mid", re.compile(rf"[?&#]songmid=({_QQ_MID})", re.I)),
    ("qq", "song_mid", re.compile(rf"[?&#]songMid=({_QQ_MID})")),
    ("qq", "song_mid", re.compile(rf"[?&#]mid=({_QQ_MID})", re.I)),
    ("qq", "song_id", re.compile(r"[?&#]songid=(\d+)", re.I)),
    ("qq", "song_id", re.compile(r"[?&#]songId=(\d+)")),
    # ---- 酷狗 ----
    ("kugou", "song_hash", re.compile(r"kugou\.com/[^\s]*[#?&]hash=([0-9A-Fa-f]{32})", re.I)),
    ("kugou", "song_hash", re.compile(r"kugou\.com/mixsong/([0-9A-Za-z]+)", re.I)),
    ("kugou", "song_hash", re.compile(r"kugou\.com/song/[^\s]*#hash=([0-9A-Fa-f]{32})", re.I)),
    # ---- 酷我 ----
    ("kuwo", "song_id", re.compile(r"kuwo\.cn/(?:newh5app/)?play_detail/(\d+)", re.I)),
    ("kuwo", "song_id", re.compile(r"kuwo\.cn/yinyue/(\d+)", re.I)),
    # ---- 汽水音乐 ----
    ("qishui", "song_id", re.compile(r"qishui\.douyin\.com/s/([0-9A-Za-z_-]+)", re.I)),
]

# 只要域名命中就认为是音乐链接（用于短链展开后仍解析不出 id 的兜底）
_HOST_HINTS: list[tuple[str, str]] = [
    ("music.163.com", "netease"),
    ("y.music.163.com", "netease"),
    ("163cn.tv", "netease"),
    ("y.qq.com", "qq"),
    ("qqmusic.qq.com", "qq"),
    ("i.y.qq.com", "qq"),
    ("kugou.com", "kugou"),
    ("kuwo.cn", "kuwo"),
    ("qishui.douyin.com", "qishui"),
]

# 这些域名下的路径纯属素材/首页，不算分享链接
_IGNORE_PATHS = {"", "/", "/index.html", "/portal", "/n/ryqq", "/n/yqq"}


def _clean_url(url: str) -> str:
    return url.rstrip(_TRAILING_JUNK)


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", url, re.I)
    return m.group(1).lower() if m else ""


def is_short_link(url: str) -> bool:
    return _host_of(url) in SHORT_LINK_HOSTS


def _is_meaningful(url: str) -> bool:
    """域名兜底时排除首页 / 无参数的空链接。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.query or parsed.fragment:
        return True
    return parsed.path.rstrip("/") not in _IGNORE_PATHS


def _platform_of_host(host: str) -> Optional[str]:
    for host_frag, platform in _HOST_HINTS:
        if host == host_frag or host.endswith("." + host_frag):
            return platform
    return None


def match_url(url: str) -> Optional[MusicLink]:
    """把单个 URL 匹配成 MusicLink，匹配不到返回 None。

    先按域名判断平台，再只用该平台的规则去抠 id —— 避免 ``mid=`` /
    ``songid=`` 这类泛化参数把别家链接错认成 QQ 音乐。
    """
    url = _clean_url(url)
    # 分享链里常把真实地址塞在 url= / jumpUrl= 参数中，先剥一层
    for candidate in (url, *_inner_urls(url)):
        scope = _platform_of_host(_host_of(candidate))
        for platform, field, pattern in _PATTERNS:
            if scope is not None and platform != scope:
                continue
            m = pattern.search(candidate)
            if m:
                link = MusicLink(platform=platform, raw=url)
                setattr(link, field, m.group(1))
                return link
    host_platform = _platform_of_host(_host_of(url))
    if host_platform and _is_meaningful(url):
        return MusicLink(platform=host_platform, raw=url)
    return None


def _inner_urls(url: str) -> list[str]:
    """取出 query 里被 URL 编码的嵌套地址。"""
    try:
        query = urlparse(url).query
    except ValueError:
        return []
    if not query:
        return []
    out: list[str] = []
    for key, values in parse_qs(query, keep_blank_values=False).items():
        if key.lower() not in ("url", "jumpurl", "target", "redirect", "link", "src"):
            continue
        for value in values:
            decoded = unquote(value)
            if decoded.startswith("http"):
                out.append(decoded)
    return out


def extract_urls(text: str) -> list[str]:
    text = (text or "").replace("\\/", "/").replace("&amp;", "&")
    return [_clean_url(u) for u in _URL_RE.findall(text)]


def extract_from_text(text: str) -> list[MusicLink]:
    """从纯文本里抽取音乐链接。"""
    results: list[MusicLink] = []
    for url in extract_urls(text):
        link = match_url(url)
        if link:
            results.append(link)
    return results


# ---------------------------------------------------------------- 卡片解析

_HINT_TITLE_KEYS = ("title", "musicTitle", "song_name", "songName", "brief")
_HINT_DESC_KEYS = ("desc", "singer", "author", "summary", "singerName")
_HINT_COVER_KEYS = ("preview", "cover", "picUrl", "image", "icon", "source_icon")

# 卡片里这些字段是音频直链/素材图，不能当分享链接
_NOISE_URL_RE = re.compile(
    r"\.(?:mp3|m4a|flac|ogg|wav|jpg|jpeg|png|gif|webp|ico|css|js)(?:$|\?)", re.I
)


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


def _rank(link: MusicLink) -> int:
    """带 id 的链接比只认出域名的兜底链接更可信。"""
    return 0 if (link.song_mid or link.song_id or link.song_hash) else 1


def extract_from_card(data: Any) -> list[MusicLink]:
    """从分享卡片 JSON 中抽取音乐链接。

    不假设具体的卡片结构（structmsg / miniapp_01 / detail_1 各不相同），
    直接递归扫描所有字符串找 URL，命中平台规则就算数。
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            # 不是合法 JSON 就当纯文本扫一遍，总比直接丢掉强
            return extract_from_text(data)
    if not isinstance(data, (dict, list)):
        return []

    hint_title = _first_key(data, _HINT_TITLE_KEYS)
    hint_artist = _first_key(data, _HINT_DESC_KEYS)
    hint_cover = _first_key(data, _HINT_COVER_KEYS)

    seen: set[str] = set()
    results: list[MusicLink] = []
    for raw in _walk_strings(data):
        candidate = raw.replace("\\/", "/")
        for url in extract_urls(candidate):
            if _NOISE_URL_RE.search(url):
                continue
            link = match_url(url)
            if not link or link.key in seen:
                continue
            seen.add(link.key)
            link.hint_title = hint_title
            link.hint_artist = hint_artist
            link.hint_cover = hint_cover if hint_cover and hint_cover.startswith("http") else None
            results.append(link)

    # 同一张卡片里若既有精确链接又有域名兜底，只留精确的
    if any(_rank(l) == 0 for l in results):
        results = [l for l in results if _rank(l) == 0]
    return results


def extract_from_xml(raw: str) -> list[MusicLink]:
    """旧版 XML structmsg 卡片：属性里塞着 url / brief。"""
    if not raw:
        return []
    text = raw.replace("&amp;", "&").replace("&quot;", '"')
    hint_title = None
    m = re.search(r'brief="\[?[^\]"]*\]?([^"]*)"', text)
    if m:
        hint_title = m.group(1).strip() or None

    results: list[MusicLink] = []
    seen: set[str] = set()
    for url in extract_urls(text):
        if _NOISE_URL_RE.search(url):
            continue
        link = match_url(url)
        if not link or link.key in seen:
            continue
        seen.add(link.key)
        link.hint_title = hint_title
        results.append(link)
    if any(_rank(l) == 0 for l in results):
        results = [l for l in results if _rank(l) == 0]
    return results


# ---------------------------------------------------------------- 短链展开

_UA_PC = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# 页面里 JS/meta 跳转的目标
_META_REFRESH_RE = re.compile(r'http-equiv=["\']refresh["\'][^>]*url=([^"\'>\s]+)', re.I)
_JS_LOCATION_RE = re.compile(r'(?:location\.(?:href|replace\()|window\.location\s*=)\s*["\']([^"\']+)', re.I)
# 页面 HTML 里直接暴露的 songmid
_PAGE_MID_RE = re.compile(rf'songmid["\'\s:=]+({_QQ_MID})', re.I)
_PAGE_MID_RE2 = re.compile(rf'"mid"\s*:\s*"({_QQ_MID})"')


async def resolve_short_link(url: str, timeout: float = 10.0) -> tuple[str, str]:
    """跟随重定向拿到最终地址。返回 (最终URL, 页面正文)。失败时原样返回。"""
    headers = {"User-Agent": _UA_MOBILE, "Accept": "text/html,*/*"}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, headers=headers
        ) as client:
            resp = await client.get(url)
            final = str(resp.url)
            body = ""
            ctype = resp.headers.get("content-type", "")
            if "html" in ctype or "text" in ctype:
                body = resp.text[:200_000]
            # 页面级跳转（meta refresh / JS）再追一跳
            for pattern in (_META_REFRESH_RE, _JS_LOCATION_RE):
                m = pattern.search(body)
                if m:
                    nxt = m.group(1).strip().replace("&amp;", "&")
                    if nxt.startswith("http") and nxt != final:
                        _dbg(f"页面跳转 {final} -> {nxt}")
                        return nxt, body
            return final, body
    except Exception as exc:
        _dbg(f"短链展开失败 {url}: {type(exc).__name__} {exc}")
        return url, ""


def _mid_from_page(body: str) -> Optional[str]:
    for pattern in (_PAGE_MID_RE, _PAGE_MID_RE2):
        m = pattern.search(body or "")
        if m:
            return m.group(1)
    return None


async def expand_links(links: list[MusicLink]) -> list[MusicLink]:
    """对缺少 id 的链接做一次跳转展开并重新匹配。"""
    expanded: list[MusicLink] = []
    for link in links:
        needs_expand = link.song_id is None and link.song_mid is None and link.song_hash is None
        if not needs_expand:
            expanded.append(link)
            continue

        _dbg(f"需要展开: {link.raw}")
        final_url, body = await resolve_short_link(link.raw)
        re_matched = match_url(final_url) if final_url != link.raw else None
        if re_matched and (re_matched.song_id or re_matched.song_mid or re_matched.song_hash):
            re_matched.hint_title = link.hint_title
            re_matched.hint_artist = link.hint_artist
            re_matched.hint_cover = link.hint_cover
            _dbg(f"展开成功 -> {final_url} key={re_matched.key}")
            expanded.append(re_matched)
            continue

        if final_url != link.raw:
            link.raw = final_url
            if re_matched:
                link.platform = re_matched.platform

        # 最后一招：从页面正文里抠 songmid
        if link.platform == "qq":
            mid = _mid_from_page(body)
            if mid:
                link.song_mid = mid
                _dbg(f"从页面正文取到 songmid={mid}")
        _dbg(f"展开结果: platform={link.platform} key={link.key}")
        expanded.append(link)
    return expanded


# ---------------------------------------------------------------- 消息级入口


_MUSIC_TYPE_MAP = {"163": "netease", "qq": "qq", "kugou": "kugou", "kuwo": "kuwo"}


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
        elif seg_type == "xml":
            found.extend(extract_from_xml(str(data.get("data") or data.get("xml") or "")))
        elif seg_type == "music":
            mtype = str(data.get("type", ""))
            if mtype in _MUSIC_TYPE_MAP:
                link = MusicLink(platform=_MUSIC_TYPE_MAP[mtype], raw=str(data.get("url", "")))
                link.song_id = str(data.get("id")) if data.get("id") else None
                link.hint_title = data.get("title")
                link.hint_artist = data.get("content")
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
        elif seg_type not in ("at", "face", "image", "reply", "record", "video"):
            # 协议端可能给出未知段类型（如 NapCat 的 markdown / longmsg），兜底扫一遍
            found.extend(extract_from_card(data))

    if DEBUG:
        _dbg(f"原始命中 {len(found)} 条: {[(l.platform, l.key) for l in found]}")
    if not found:
        return []

    found = await expand_links(found)

    # 去重：同一条消息里同一首歌只算一次，优先保留带 id 的
    found.sort(key=_rank)
    unique: list[MusicLink] = []
    seen: set[tuple[str, str]] = set()
    for link in found:
        sig = (link.platform, link.key)
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(link)

    # 若同平台既有精确链接又有兜底链接，丢掉兜底的（多半是卡片里的首页地址）
    precise_platforms = {l.platform for l in unique if _rank(l) == 0}
    unique = [l for l in unique if _rank(l) == 0 or l.platform not in precise_platforms]

    if DEBUG:
        _dbg(f"最终结果 {len(unique)} 条: {[(l.platform, l.key) for l in unique]}")
    return unique


async def diagnose(text: str) -> str:
    """给 /music parse 命令用：把识别过程逐步讲清楚。"""
    lines: list[str] = []
    urls = extract_urls(text)
    lines.append(f"1) 抽到 {len(urls)} 个 URL")
    for u in urls[:5]:
        lines.append(f"   {u}")
    raw_links = extract_from_text(text)
    lines.append(f"2) 命中音乐平台 {len(raw_links)} 条")
    for l in raw_links[:5]:
        lines.append(f"   [{l.platform}] key={l.key}")
    if not raw_links:
        lines.append("   -> 没有任何链接命中平台规则")
        return "\n".join(lines)
    expanded = await expand_links(raw_links)
    lines.append("3) 展开后")
    for l in expanded[:5]:
        has_id = bool(l.song_mid or l.song_id or l.song_hash)
        lines.append(f"   [{l.platform}] key={l.key} 有ID={'是' if has_id else '否'}")
        lines.append(f"   最终URL: {l.raw[:100]}")
    return "\n".join(lines)
