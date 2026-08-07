"""QQ 音乐 Provider。

解析链路（逐级兜底，任一级成功就返回）：
1. musicu.fcg 详情接口（song_mid / song_id 任一可用）
2. 老版 fcg_play_single_song.fcg（对部分 mid 兼容性更好）
3. 分享页 HTML：先从页面抠出 songmid 再回到第 1 步
4. 用卡片带来的歌名+歌手走 QQ 音乐搜索接口反查
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..models import MusicLink, Song
from .base import Provider

_API = "https://u.y.qq.com/cgi-bin/musicu.fcg"
_LEGACY_API = "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg"
_SEARCH_API = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
_HEADERS = {"Referer": "https://y.qq.com/", "Content-Type": "application/json"}
_PLAIN_HEADERS = {"Referer": "https://y.qq.com/"}
_COVER_TPL = "https://y.gtimg.cn/music/photo_new/T002R300x300M000{mid}.jpg"
_SINGER_COVER_TPL = "https://y.gtimg.cn/music/photo_new/T001R300x300M000{mid}.jpg"

_QQ_MID = r"[0-9A-Za-z]{10,20}"
_PAGE_MID_RE = re.compile(rf'songmid["\'\s:=]+({_QQ_MID})', re.I)
_PAGE_MID_RE2 = re.compile(rf'"mid"\s*:\s*"({_QQ_MID})"')
_JSONP_RE = re.compile(r"^[^(]*\((.*)\)[^)]*$", re.S)


def _singers(node: Any) -> str:
    items = node or []
    return "、".join(str(s.get("name", "")).strip() for s in items if s.get("name"))


class QQMusicProvider(Provider):
    platform = "qq"

    # ------------------------------------------------------------ 对外入口

    async def resolve(self, link: MusicLink) -> Optional[Song]:
        mid = (link.song_mid or "").strip() or None
        sid = (link.song_id or "").strip() or None

        # 1) 详情接口
        for param in self._detail_params(mid, sid):
            song = await self._by_detail(param, link)
            if song:
                return song

        # 2) 老接口
        if mid:
            song = await self._by_legacy(mid, link)
            if song:
                return song

        # 3) 页面抠 mid 再来一次
        page_mid = await self._mid_from_page(link.raw)
        if page_mid and page_mid != mid:
            song = await self._by_detail({"song_mid": page_mid}, link)
            if song:
                return song
            song = await self._by_legacy(page_mid, link)
            if song:
                return song

        # 4) 用卡片信息搜索反查
        return await self._by_search(link)

    # ------------------------------------------------------------ 各级实现

    @staticmethod
    def _detail_params(mid: Optional[str], sid: Optional[str]) -> list[dict[str, object]]:
        params: list[dict[str, object]] = []
        if mid:
            params.append({"song_mid": mid})
        if sid and sid.isdigit():
            params.append({"song_id": int(sid)})
        return params

    async def _by_detail(self, param: dict[str, object], link: MusicLink) -> Optional[Song]:
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
        return self._track_to_song(track, link)

    async def _by_legacy(self, mid: str, link: MusicLink) -> Optional[Song]:
        url = f"{_LEGACY_API}?songmid={mid}&platform=yqq&format=json"
        try:
            text = await self.fetch_text(url, headers=_PLAIN_HEADERS)
            data = json.loads(_strip_jsonp(text))
        except Exception:
            return None
        items = data.get("data") or []
        if not items:
            return None
        item = items[0]
        album = item.get("album") or {}
        name = str(item.get("songname") or item.get("name") or "").strip()
        if not name:
            return None
        album_mid = str(album.get("mid") or item.get("albummid") or "")
        song_mid = str(item.get("songmid") or item.get("mid") or mid)
        numeric = str(item.get("songid") or item.get("id") or "")
        return Song(
            platform="qq",
            song_id=numeric or song_mid,
            title=name,
            artists=_singers(item.get("singer")),
            album=str(album.get("name") or item.get("albumname") or "").strip(),
            cover=_COVER_TPL.format(mid=album_mid) if album_mid else "",
            url=f"https://y.qq.com/n/ryqq/songDetail/{song_mid}",
            duration=int(item.get("interval") or 0),
        )

    async def _mid_from_page(self, raw_url: str) -> Optional[str]:
        if not raw_url or not raw_url.startswith("http"):
            return None
        try:
            html_text = await self.fetch_text(raw_url, headers=_PLAIN_HEADERS)
        except Exception:
            return None
        for pattern in (_PAGE_MID_RE, _PAGE_MID_RE2):
            m = pattern.search(html_text)
            if m:
                return m.group(1)
        return None

    async def _by_search(self, link: MusicLink) -> Optional[Song]:
        keyword = " ".join(
            p for p in ((link.hint_title or "").strip(), (link.hint_artist or "").strip()) if p
        ).strip()
        if not keyword:
            return None
        params = {
            "w": keyword, "format": "json", "p": 1, "n": 5,
            "cr": 1, "t": 0, "aggr": 1, "lossless": 0, "new_json": 1,
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        try:
            text = await self.fetch_text(f"{_SEARCH_API}?{query}", headers=_PLAIN_HEADERS)
            data = json.loads(_strip_jsonp(text))
        except Exception:
            return None
        items = (((data.get("data") or {}).get("song") or {}).get("list")) or []
        if not items:
            return None
        return self._track_to_song(items[0], link)

    # ------------------------------------------------------------ 公共转换

    def _track_to_song(self, track: dict, link: MusicLink) -> Optional[Song]:
        if not isinstance(track, dict):
            return None
        name = str(track.get("name") or track.get("songname") or "").strip()
        if not name:
            return None
        album = track.get("album") or {}
        album_mid = str(album.get("mid") or track.get("albummid") or "")
        mid = str(track.get("mid") or track.get("songmid") or link.song_mid or "")
        # 优先用数字 id 作为主键：QQ 音乐卡片 [CQ:music,type=qq,id=] 只认数字 id
        numeric_id = str(track.get("id") or track.get("songid") or link.song_id or "")
        cover = ""
        if album_mid:
            cover = _COVER_TPL.format(mid=album_mid)
        else:
            singer_mid = ""
            for s in track.get("singer") or []:
                if s.get("mid"):
                    singer_mid = str(s["mid"])
                    break
            if singer_mid:
                cover = _SINGER_COVER_TPL.format(mid=singer_mid)
        return Song(
            platform="qq",
            song_id=numeric_id or mid,
            title=name,
            artists=_singers(track.get("singer")),
            album=str(album.get("name") or track.get("albumname") or "").strip(),
            cover=cover,
            url=f"https://y.qq.com/n/ryqq/songDetail/{mid}" if mid else link.raw,
            duration=int(track.get("interval") or 0),
        )


def _strip_jsonp(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("{") or text.startswith("["):
        return text
    m = _JSONP_RE.match(text)
    return m.group(1) if m else text
