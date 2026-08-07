"""服务层：把配置、存储、解析、渲染、归档装配成一组高层操作。

这一层不依赖 nonebot，方便离线测试。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Sequence

from .archiver import Archiver, ArchiveReport
from .cache import CleanResult, clean_caches
from .config import CACHE_DIR, DB_PATH, NETEASE_SESSION_PATH, AppConfig, config_manager
from . import detector
from .detector import detect_from_segments
from .models import MusicLink, Song
from .naming import build_context, render_template
from .netease_api import NeteaseAPI
from .providers import ProviderRegistry
from .render import build_text_list, render_song_list
from .store import Store
from .window import WindowResolver, WindowState

try:
    from nonebot.log import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("music_collector.service")


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
        detector.set_debug(self.config.debug_detect)
        await self.store.init()
        if self.config.cache.enabled and self.config.cache.clean_on_start:
            self.clean_cache()

    def group_enabled(self, group_id: int) -> bool:
        cfg = self.config
        if not cfg.enabled:
            return False
        return not cfg.groups or group_id in cfg.groups

    def current_window(self) -> WindowState:
        return self.resolver.resolve()

    # ------------------------------------------------------------ 缓存

    def clean_cache(self, keep_days: Optional[float] = None) -> CleanResult:
        cfg = self.config.cache
        return clean_caches(
            CACHE_DIR,
            keep_days if keep_days is not None else cfg.keep_days,
            cfg.max_render_files,
            cfg.max_cover_files,
        )

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
            cache_cfg = self.config.cache
            if cache_cfg.enabled and cache_cfg.clean_after_render:
                self.clean_cache()
        return text, images, songs

    # ------------------------------------------------------------ 归档

    async def preview_playlist_name(
        self, group_id: int, window: Optional[WindowState] = None
    ) -> str:
        """按当前配置预览歌单名，方便群里确认再归档。"""
        state = window or self.current_window()
        songs = await self.store.list_songs(group_id, state.key)
        cfg = self.config.playlist
        context = build_context(
            group_id=group_id,
            window_label=state.label,
            start_at=state.start_at,
            end_at=state.archive_at,
            count=len(songs),
            total=len(songs),
            seq=cfg.seq,
            songs=songs,
        )
        return render_template(cfg.pending_name or cfg.name_template, context)

    async def run_archive(
        self,
        group_id: int,
        window: Optional[WindowState] = None,
        name_override: str = "",
    ) -> ArchiveReport:
        state = window or self.current_window()
        songs = await self.store.list_songs(group_id, state.key)
        cfg = self.config.playlist
        report = await self.archiver.archive(
            group_id, state.key, state.label, songs, cfg,
            start_at=state.start_at,
            end_at=state.archive_at,
            name_override=name_override,
        )
        if report.ok:
            # 一次性歌单名用完即弃；期号自增
            if cfg.pending_name and not name_override:
                config_manager.update("playlist.pending_name", "")
            if cfg.seq_auto_increment:
                config_manager.update("playlist.seq", cfg.seq + 1)
            # 归档（结束收集）后自动清空本期已收集歌曲
            if self.config.clear.after_archive:
                removed = await self.store.delete_window(group_id, state.key)
                logger.info(f"[music] 归档后已自动清空本期 {removed} 首")
        return report

    # ------------------------------------------------------------ 清理收集数据

    async def clear_window(self, group_id: int, window_key: str) -> int:
        """清空某个群在某个窗口下的全部已收集歌曲。"""
        return await self.store.delete_window(group_id, window_key)

    async def clear_indices(
        self, group_id: int, window_key: str, indices: Sequence[int]
    ) -> int:
        """按序号批量删除已收集歌曲。"""
        return await self.store.delete_songs_by_indices(group_id, window_key, indices)

    async def prune_old(self, keep_days: float) -> int:
        """删除早于 now - keep_days 天的收集记录。"""
        before = time.time() - keep_days * 86400
        return await self.store.prune_old(before)

    async def windows_with_counts(self, group_id: Optional[int] = None) -> list[tuple[str, int]]:
        """列出各窗口及其歌曲数，供手动清理选择。"""
        return await self.store.windows_with_counts(group_id)

    async def target_groups(self, window_key: str) -> list[int]:
        """定时任务要处理哪些群：优先配置白名单，否则取有数据的群。"""
        if self.config.groups:
            return list(self.config.groups)
        return await self.store.groups_in_window(window_key)


service = CollectorService()
