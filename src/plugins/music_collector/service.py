"""服务层：把配置、存储、解析、渲染、归档装配成一组高层操作。

这一层不依赖 nonebot，方便离线测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from .archiver import Archiver, ArchiveReport
from .config import CACHE_DIR, DB_PATH, NETEASE_SESSION_PATH, AppConfig, config_manager
from .detector import detect_from_segments
from .models import MusicLink, Song
from .netease_api import NeteaseAPI
from .providers import ProviderRegistry
from .render import build_text_list, render_song_list
from .store import Store
from .window import WindowResolver, WindowState


class CollectResult:
    """一条消息的处理结果。"""

    def __init__(self) -> None:
        self.accepted: list[Song] = []      # 新收录
        self.duplicated: list[Song] = []    # 重复分享
        self.outside_window: list[Song] = []  # 不在收集期，仅回卡片
        self.index_of: dict[int, int] = {}  # id(song) -> 榜单序号

    @property
    def any_music(self) -> bool:
        return bool(self.accepted or self.duplicated or self.outside_window)


class CollectorService:
    def __init__(self) -> None:
        self.store = Store(DB_PATH)
        self.netease = NeteaseAPI(NETEASE_SESSION_PATH)
        self.providers = ProviderRegistry(self.netease)
        self.archiver = Archiver(self.netease, self.store)

    # ------------------------------------------------------------ 基础

    @property
    def config(self) -> AppConfig:
        return config_manager.config

    @property
    def resolver(self) -> WindowResolver:
        # 构造很轻，每次重建以便配置热更新后立即生效
        return WindowResolver(self.config.window)

    async def setup(self) -> None:
        config_manager.load()
        await self.store.init()

    def group_enabled(self, group_id: int) -> bool:
        cfg = self.config
        if not cfg.enabled:
            return False
        return not cfg.groups or group_id in cfg.groups

    def current_window(self) -> WindowState:
        return self.resolver.resolve()

    # ------------------------------------------------------------ 收集

    async def handle_segments(
        self,
        group_id: int,
        segments: Sequence[dict],
        sharer_id: int,
        sharer_name: str,
    ) -> CollectResult:
        result = CollectResult()
        links: list[MusicLink] = await detect_from_segments(segments)
        if not links:
            return result

        state = self.current_window()
        for link in links:
            song = await self.providers.resolve(link)
            song.sharer_id = sharer_id
            song.sharer_name = sharer_name

            if not state.collecting:
                result.outside_window.append(song)
                continue

            inserted, stored = await self.store.add_song(group_id, state.key, song)
            if inserted:
                result.accepted.append(stored)
            else:
                result.duplicated.append(stored)

        for song in result.accepted + result.duplicated:
            if song.row_id is not None:
                result.index_of[id(song)] = await self.store.position_of(
                    group_id, state.key, song.row_id
                )
        return result

    # ------------------------------------------------------------ 榜单

    async def build_report(
        self, group_id: int, window: Optional[WindowState] = None
    ) -> tuple[str, list[Path], list[Song]]:
        state = window or self.current_window()
        songs = await self.store.list_songs(group_id, state.key)
        title = f"群音乐收藏榜 · {state.label}"
        text = build_text_list(songs, title)
        images: list[Path] = []
        if songs:
            subtitle = f"共 {len(songs)} 首 · 窗口 {state.label}"
            images = await render_song_list(
                songs, title, subtitle, self.config.render, CACHE_DIR / "render"
            )
        return text, images, songs

    # ------------------------------------------------------------ 归档

    async def run_archive(
        self, group_id: int, window: Optional[WindowState] = None
    ) -> ArchiveReport:
        state = window or self.current_window()
        songs = await self.store.list_songs(group_id, state.key)
        return await self.archiver.archive(
            group_id, state.key, state.label, songs, self.config.playlist
        )

    async def target_groups(self, window_key: str) -> list[int]:
        """定时任务要处理哪些群：优先配置白名单，否则取有数据的群。"""
        if self.config.groups:
            return list(self.config.groups)
        return await self.store.groups_in_window(window_key)


service = CollectorService()
