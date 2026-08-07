"""QQ 音乐 Provider（走公开的 musicu.fcg 聚合接口，无需登录）。"""

from __future__ import annotations

from typing import Optional

from ..models import MusicLink, Song
from .base import Provider

_API = "https://u.y.qq.com/cgi-bin/musicu.fcg"
_HEADERS = {"Referer": "https://y.qq.com/", "Content-Type": "application/json"}
_COVER_TPL = "https://y.gtimg.cn/music/photo_new/T002R300x300M000{mid}.jpg"


class QQMusicProvider(Provider):
    platform = "qq"

    async def resolve(self, link: MusicLink) -> Optional[Song]:
        param: dict[str, object]
        if link.song_mid:
            param = {"song_mid": link.song_mid}
        elif link.song_id:
            param = {"song_id": int(link.song_id)}
        else:
            return None

        body = {
            "comm": {"ct": 24, "cv": 0},
            "songinfo": {
                "method": "get_song_detail_yqq",
                "module": "music.pf_song_detail_svr",
                "param": param,
            },
        }
        try:
            data = await self.fetch_json(_API, headers=_HEADERS, json_body=body)
        except Exception:
            return None

        node = (data.get("songinfo") or {}).get("data") or {}
        track = node.get("track_info") or {}
        name = str(track.get("name") or "").strip()
        if not name:
            return None

        singers = "、".join(
            s.get("name", "") for s in (track.get("singer") or []) if s.get("name")
        )
        album = track.get("album") or {}
        album_mid = str(album.get("mid") or "")
        mid = str(track.get("mid") or link.song_mid or "")
        # 优先用数字 id 作为主键：QQ 音乐卡片 [CQ:music,type=qq,id=] 只认数字 id
        numeric_id = str(track.get("id") or link.song_id or "")
        return Song(
            platform="qq",
            song_id=numeric_id or mid,
            title=name,
            artists=singers,
            album=str(album.get("name") or "").strip(),
            cover=_COVER_TPL.format(mid=album_mid) if album_mid else "",
            url=f"https://y.qq.com/n/ryqq/songDetail/{mid}" if mid else link.raw,
            duration=int(track.get("interval") or 0),
        )
