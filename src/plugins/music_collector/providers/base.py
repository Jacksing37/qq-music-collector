"""Provider 抽象：把 MusicLink 解析成 Song。"""

from __future__ import annotations

import abc
import html
import re
from typing import Optional

import httpx

from ..models import MusicLink, Song

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TIMEOUT = 12.0


class Provider(abc.ABC):
    platform: str = ""

    @abc.abstractmethod
    async def resolve(self, link: MusicLink) -> Optional[Song]:
        """解析元数据。返回 None 表示解析失败，由上层用 hint 兜底。"""

    @staticmethod
    async def fetch_text(url: str, headers: Optional[dict] = None) -> str:
        merged = {"User-Agent": UA}
        if headers:
            merged.update(headers)
        async with httpx.AsyncClient(
            timeout=TIMEOUT, headers=merged, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text

    @staticmethod
    async def fetch_json(
        url: str, headers: Optional[dict] = None, json_body: Optional[dict] = None
    ) -> dict:
        merged = {"User-Agent": UA}
        if headers:
            merged.update(headers)
        async with httpx.AsyncClient(
            timeout=TIMEOUT, headers=merged, follow_redirects=True
        ) as client:
            if json_body is None:
                resp = await client.get(url)
            else:
                resp = await client.post(url, json=json_body)
            resp.raise_for_status()
            return resp.json()


_OG_TITLE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# 页面标题里常见的站点后缀，需要剥掉
_SITE_SUFFIXES = (
    "_酷我音乐", "-酷我音乐", "_酷狗音乐", "-酷狗音乐", "_网易云音乐", "-网易云音乐",
    "_QQ音乐", "-QQ音乐", "_汽水音乐", "-汽水音乐", "-单曲", "_单曲", "在线试听",
)


def parse_page_title(html_text: str) -> tuple[str, str, str]:
    """从 HTML 里粗解析出 (歌名, 歌手, 封面)。"""
    title = ""
    m = _OG_TITLE_RE.search(html_text)
    if m:
        title = m.group(1)
    else:
        m = _TITLE_RE.search(html_text)
        if m:
            title = m.group(1)
    title = html.unescape(title).strip()
    for suffix in _SITE_SUFFIXES:
        title = title.replace(suffix, "")
    title = title.strip(" -_|")

    artist = ""
    for sep in (" - ", "-", " – ", "_"):
        if sep in title:
            head, _, tail = title.partition(sep)
            if head.strip() and tail.strip():
                title, artist = head.strip(), tail.strip()
                break

    cover = ""
    m = _OG_IMAGE_RE.search(html_text)
    if m:
        cover = html.unescape(m.group(1)).strip()
    return title, artist, cover


def fallback_song(link: MusicLink) -> Song:
    """所有解析手段都失败时，用卡片自带信息或链接本身兜底，保证不丢记录。"""
    title = (link.hint_title or "").strip() or "未识别歌曲"
    return Song(
        platform=link.platform,
        song_id=link.key,
        title=title,
        artists=(link.hint_artist or "").strip(),
        cover=link.hint_cover or "",
        url=link.raw,
    )
