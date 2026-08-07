"""Provider 注册与统一解析入口。"""

from __future__ import annotations

from typing import Optional

from ..models import MusicLink, Song
from ..netease_api import NeteaseAPI
from .base import Provider, fallback_song
from .generic import KugouProvider, KuwoProvider, PageProvider
from .netease import NeteaseProvider, song_from_payload
from .qqmusic import QQMusicProvider

__all__ = [
    "ProviderRegistry",
    "Provider",
    "song_from_payload",
    "fallback_song",
]


class ProviderRegistry:
    def __init__(self, netease_api: NeteaseAPI) -> None:
        page = PageProvider()
        self._providers: dict[str, Provider] = {
            "netease": NeteaseProvider(netease_api),
            "qq": QQMusicProvider(),
            "kugou": KugouProvider(),
            "kuwo": KuwoProvider(),
            "qishui": page,
            "unknown": page,
        }

    async def resolve(self, link: MusicLink) -> Song:
        """解析音乐链接。任何失败都不抛异常，用兜底 Song 保证记录不丢。"""
        provider = self._providers.get(link.platform)
        song: Optional[Song] = None
        if provider is not None:
            try:
                song = await provider.resolve(link)
            except Exception:
                song = None
        if song is None or not song.title:
            song = fallback_song(link)
        # 卡片自带封面质量通常够用，补齐缺失字段
        if not song.cover and link.hint_cover:
            song.cover = link.hint_cover
        if not song.artists and link.hint_artist:
            song.artists = link.hint_artist
        if not song.url:
            song.url = link.raw
        return song
