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
        self.unidentified: list[Song] = []  # 没认出来的链接，不入榜
        self.index_of: dict[int, int] = {}  # id(song) -> 榜单序号

    @property
    def any_music(self) -> bool:
        return bool(self.accepted or self.duplicated or self.unidentified)


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
        """当前窗口状态；手动开关（collect_override）优先于时间表。"""
        state = self.resolver.resolve()
        override = getattr(self.config, "collect_override", "auto")
        if override == "on":
            state.collecting = True
            state.override = "手动强制开启"
        elif override == "off":
            state.collecting = False
            state.override = "手动强制关闭"
        return state

    def set_collect_override(self, value: str) -> str:
        """设置手动开关，返回人类可读说明。"""
        if value not in ("auto", "on", "off"):
            raise ValueError("collect_override 只能是 auto / on / off")
        config_manager.update("collect_override", value)
        return {
            "auto": "已恢复按时间表自动收集",
            "on": "已手动开启收集（无视时间窗口）",
            "off": "已手动关闭收集（无视时间窗口）",
        }[value]

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
        state = self.current_window()
        # 不在收集期：静默处理，不解析、不回应
        if not state.collecting:
            return result

        links: list[MusicLink] = await detect_from_segments(segments)
        if not links:
            return result

        for link in links:
            song = await self.providers.resolve(link)
            song.sharer_id = sharer_id
            song.sharer_name = sharer_name

            # 没认出来的链接不入榜（如解析失败、非音乐页面）
            if not song.title or song.title == "未识别歌曲":
                result.unidentified.append(song)
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
            end_at=state.end_at or state.archive_at,
            count=len(songs),
            total=len(songs),
            seq=cfg.seq,
            songs=songs,
            emoji_style=cfg.emoji_style,
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
            end_at=state.end_at or state.archive_at,
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

    # ------------------------------------------------------------ 简介补写

    async def retry_pending_desc(
        self, group_id: Optional[int] = None
    ) -> tuple[int, int]:
        """重试所有待补写的歌单简介，返回 (成功数, 失败数)。"""
        pending = await self.store.list_pending_desc(group_id)
        ok = failed = 0
        for row in pending:
            playlist_id = str(row.get("playlist_id") or "")
            if not playlist_id.isdigit():
                await self.store.drop_pending_desc(playlist_id)
                continue
            success, note = await self.netease.update_description(
                int(playlist_id),
                row.get("description") or "",
                name=row.get("playlist_name") or "",
            )
            if success:
                await self.store.drop_pending_desc(playlist_id)
                ok += 1
            else:
                await self.store.save_pending_desc(
                    playlist_id,
                    row.get("playlist_name") or "",
                    int(row.get("group_id") or 0),
                    row.get("description") or "",
                    note,
                )
                failed += 1
        return ok, failed

    async def pending_desc_list(self, group_id: Optional[int] = None) -> list[dict]:
        return await self.store.list_pending_desc(group_id)

    async def rebuild_description(
        self, group_id: int, window: Optional[WindowState] = None
    ) -> str:
        """按当前配置重新生成一份简介文本（用于手动补写 / 预览）。"""
        from .naming import (
            build_sharer_lines,
            build_song_lines,
            fit_description,
        )

        state = window or self.current_window()
        songs = await self.store.list_songs(group_id, state.key)
        cfg = self.config.playlist
        context = build_context(
            group_id=group_id,
            window_label=state.label,
            start_at=state.start_at,
            end_at=state.end_at or state.archive_at,
            count=len(songs),
            total=len(songs),
            seq=cfg.seq,
            songs=songs,
            emoji_style=cfg.emoji_style,
        )
        header = render_template(cfg.description_template, context)
        body: list[str] = []
        if cfg.include_sharers and cfg.sharer_style != "none":
            if cfg.sharer_style == "by_person":
                body = build_sharer_lines(
                    songs,
                    emoji_style=cfg.emoji_style,
                    show_artist=cfg.desc_show_artist,
                    blank_line=cfg.desc_blank_line,
                )
            else:
                body = build_song_lines(
                    songs,
                    emoji_style=cfg.emoji_style,
                    show_artist=cfg.desc_show_artist,
                )
        return fit_description(header, body)

    async def push_description(
        self, playlist_id: int, desc: str, name: str = "", group_id: int = 0
    ) -> tuple[bool, str]:
        """把指定简介写到指定歌单（失败自动入队）。"""
        return await self.archiver.write_description(
            playlist_id, desc, name=name, group_id=group_id,
            retries=self.config.playlist.desc_retry,
        )

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
