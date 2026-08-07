"""酷狗 / 酷我 / 汽水音乐 Provider。

这几家没有稳定的公开接口，策略是：
优先打专用接口，失败则回落到页面 og:title 解析。
"""

from __future__ import annotations

import random
import string
from typing import Optional

from ..models import MusicLink, Song
from .base import Provider, parse_page_title


def _rand_mid() -> str:
    return "".join(random.choice(string.hexdigits.lower()) for _ in range(32))


class KugouProvider(Provider):
    platform = "kugou"

    async def resolve(self, link: MusicLink) -> Optional[Song]:
        if link.song_hash:
            song = await self._by_hash(link.song_hash)
            if song:
                return song
        return await _resolve_by_page(link)

    async def _by_hash(self, song_hash: str) -> Optional[Song]:
        url = f"https://wwwapi.kugou.com/yy/index.php?r=play/getdata&hash={song_hash}"
        headers = {
            "Referer": "https://www.kugou.com/",
            "Cookie": f"kg_mid={_rand_mid()}",
        }
        try:
            payload = await self.fetch_json(url, headers=headers)
        except Exception:
            return None
        data = payload.get("data") or {}
        name = str(data.get("song_name") or data.get("audio_name") or "").strip()
        if not name:
            return None
        return Song(
            platform="kugou",
            song_id=song_hash,
            title=name,
            artists=str(data.get("author_name") or "").strip(),
            album=str(data.get("album_name") or "").strip(),
            cover=str(data.get("img") or ""),
            url=f"https://www.kugou.com/song/#hash={song_hash}",
            duration=int(data.get("timelength") or 0) // 1000,
        )


class KuwoProvider(Provider):
    platform = "kuwo"

    async def resolve(self, link: MusicLink) -> Optional[Song]:
        if link.song_id:
            song = await self._by_id(link.song_id)
            if song:
                return song
        return await _resolve_by_page(link)

    async def _by_id(self, song_id: str) -> Optional[Song]:
        url = f"https://www.kuwo.cn/api/www/music/musicInfo?mid={song_id}&httpsStatus=1"
        headers = {
            "Referer": f"https://www.kuwo.cn/play_detail/{song_id}",
            "csrf": "MUSICKUWO",
            "Cookie": "kw_token=MUSICKUWO",
        }
        try:
            payload = await self.fetch_json(url, headers=headers)
        except Exception:
            return None
        data = payload.get("data") or {}
        name = str(data.get("name") or "").strip()
        if not name:
            return None
        return Song(
            platform="kuwo",
            song_id=str(data.get("rid") or song_id),
            title=name,
            artists=str(data.get("artist") or "").strip(),
            album=str(data.get("album") or "").strip(),
            cover=str(data.get("pic") or ""),
            url=f"https://www.kuwo.cn/play_detail/{song_id}",
            duration=int(data.get("duration") or 0),
        )


class PageProvider(Provider):
    """兜底：只靠页面 meta 标签解析（汽水音乐等）。"""

    platform = "qishui"

    async def resolve(self, link: MusicLink) -> Optional[Song]:
        return await _resolve_by_page(link)


async def _resolve_by_page(link: MusicLink) -> Optional[Song]:
    try:
        html_text = await Provider.fetch_text(link.raw)
    except Exception:
        return None
    title, artist, cover = parse_page_title(html_text)
    if not title:
        return None
    return Song(
        platform=link.platform,
        song_id=link.key,
        title=title,
        artists=artist or (link.hint_artist or ""),
        cover=cover or (link.hint_cover or ""),
        url=link.raw,
    )
