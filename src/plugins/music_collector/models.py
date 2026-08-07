"""核心数据模型。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# 平台标识 -> 中文名
PLATFORM_NAMES: dict[str, str] = {
    "netease": "网易云音乐",
    "qq": "QQ音乐",
    "kugou": "酷狗音乐",
    "kuwo": "酷我音乐",
    "qishui": "汽水音乐",
    "bilibili": "哔哩哔哩",
    "unknown": "未知平台",
}


@dataclass
class MusicLink:
    """从消息中识别出的一条音乐链接（尚未解析元数据）。"""

    platform: str
    raw: str                      # 原始链接或卡片里的 jumpUrl
    song_id: Optional[str] = None  # 数字 id（网易云/酷我）
    song_mid: Optional[str] = None  # mid（QQ音乐）
    song_hash: Optional[str] = None  # hash（酷狗）
    # 卡片里自带的信息，作为解析失败时的兜底
    hint_title: Optional[str] = None
    hint_artist: Optional[str] = None
    hint_cover: Optional[str] = None

    @property
    def key(self) -> str:
        """平台内唯一键。"""
        return self.song_mid or self.song_id or self.song_hash or self.raw


@dataclass
class Song:
    """解析完成的歌曲信息。"""

    platform: str
    song_id: str                 # 平台内唯一 id（QQ音乐用 mid）
    title: str
    artists: str = ""
    album: str = ""
    cover: str = ""
    url: str = ""
    duration: int = 0            # 秒
    # 收集上下文
    sharer_id: int = 0
    sharer_name: str = ""
    created_at: float = field(default_factory=time.time)
    # 归档结果
    netease_id: Optional[str] = None   # 匹配到的网易云歌曲 id
    matched: bool = False
    # 数据库行号，入库后回填，用于保持分享先后顺序
    row_id: Optional[int] = None

    @property
    def platform_name(self) -> str:
        return PLATFORM_NAMES.get(self.platform, self.platform)

    @property
    def duration_text(self) -> str:
        if self.duration <= 0:
            return "--:--"
        return f"{self.duration // 60:02d}:{self.duration % 60:02d}"

    def display(self) -> str:
        """单行文本展示。"""
        artist = self.artists or "未知歌手"
        return f"{self.title} - {artist}"
