"""Apple Music Provider。

链接形如 https://music.apple.com/cn/song/<slug>/<track_id> 或
https://music.apple.com/cn/album/<slug>/<album_id>?i=<track_id>（i= 即单曲 id）。

优先走 iTunes Lookup 公开 API（无需鉴权）取元数据，失败回落页面 og:title。
"""

from __future__ import annotations

import re
from typing import Optional

from ..models import MusicLink, Song
from .base import Provider
from .generic import _resolve_by_page

_CC_RE = re.compile(r"music\.apple\.com/([a-z]{2})/", re.I)


class AppleProvider(Provider):
    platform = "apple"

    async def resolve(self, link: MusicLink) -> Optional[Song]:
        if link.song_id:
            song = await self._by_lookup(link.song_id, link.raw)
            if song:
                return song
        return await _resolve_by_page(link)

    async def _by_lookup(self, track_id: str, raw_url: str) -> Optional[Song]:
        """iTunes Lookup API：https://itunes.apple.com/lookup?id=<track_id>"""
        urls = [f"https://itunes.apple.com/lookup?id={track_id}"]
        m = _CC_RE.search(raw_url or "")
        if m:
            urls.append(f"https://itunes.apple.com/lookup?id={track_id}&country={m.group(1)}")
        for url in urls:
            try:
                payload = await self.fetch_json(url)
            except Exception:
                continue
            results = payload.get("results") or []
            if not results:
                continue
            r = results[0]
            name = str(r.get("trackName") or "").strip()
            if not name:
                continue
            cover = str(r.get("artworkUrl100") or "").strip()
            if cover:
                # 100x100bb → 600x600bb 拿大图
                cover = cover.replace("100x100bb", "600x600bb").replace("100x100", "600x600")
            return Song(
                platform="apple",
                song_id=str(r.get("trackId") or track_id),
                title=name,
                artists=str(r.get("artistName") or "").strip(),
                album=str(r.get("collectionName") or "").strip(),
                cover=cover,
                url=raw_url or f"https://music.apple.com/us/song/{track_id}",
                duration=int(r.get("trackTimeMillis") or 0) // 1000,
            )
        return None
