"""网易云音乐 Provider。"""

from __future__ import annotations

from typing import Any, Optional

from ..models import MusicLink, Song
from ..netease_api import NeteaseAPI
from .base import Provider


def song_from_payload(item: dict[str, Any]) -> Song:
    """把网易云 /v3/song/detail 或 cloudsearch 的单曲结构转成 Song。"""
    artists = "、".join(
        a.get("name", "") for a in (item.get("ar") or item.get("artists") or []) if a.get("name")
    )
    album_obj = item.get("al") or item.get("album") or {}
    duration_ms = item.get("dt") or item.get("duration") or 0
    song_id = str(item.get("id", ""))
    return Song(
        platform="netease",
        song_id=song_id,
        title=str(item.get("name", "")).strip(),
        artists=artists,
        album=str(album_obj.get("name", "")).strip(),
        cover=str(album_obj.get("picUrl") or album_obj.get("img1v1Url") or ""),
        url=f"https://music.163.com/#/song?id={song_id}",
        duration=int(duration_ms) // 1000,
        netease_id=song_id,
        matched=True,
    )


class NeteaseProvider(Provider):
    platform = "netease"

    def __init__(self, api: NeteaseAPI) -> None:
        self.api = api

    async def resolve(self, link: MusicLink) -> Optional[Song]:
        if not link.song_id:
            return None
        try:
            songs = await self.api.song_detail([link.song_id])
        except Exception:
            return None
        if not songs:
            return None
        song = song_from_payload(songs[0])
        return song if song.title else None
