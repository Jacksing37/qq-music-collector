"""服务层：把配置、存储、解析、渲染、归档装配成一组高层操作。

这一层不依赖 nonebot，方便离线测试。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from .archiver import Archiver, ArchiveReport, songs_from_snapshot
from .cache import CleanResult, clean_caches
from .config import (
    CACHE_DIR,
    DB_PATH,
    NETEASE_SESSION_PATH,
    AppConfig,
    PlaylistConfig,
    config_manager,
)
from . import detector
from .detector import detect_from_segments
from .models import MusicLink, Song
from .naming import build_context, render_template
from .netease_api import NeteaseAPI
from .providers import ProviderRegistry
from .render import build_text_list, render_song_list
from .store import MASTER_KEY, Store
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
        self.duplicated: list[Song] = []    # 同窗口重复分享
        self.master_duplicated: list[Song] = []  # 跨窗口重复（总库已存在）
        self.unidentified: list[Song] = []  # 没认出来的链接，不入榜
        self.index_of: dict[int, int] = {}        # id(song) -> 本窗口榜单序号
        self.master_index_of: dict[int, int] = {}  # id(song) -> 总库序号

    @property
    def any_music(self) -> bool:
        return bool(self.accepted or self.duplicated or self.unidentified)


class CollectorService:
    def __init__(self) -> None:
        self.store = Store(DB_PATH)
        self.netease = NeteaseAPI(NETEASE_SESSION_PATH)
        self.providers = ProviderRegistry(self.netease)
        self.archiver = Archiver(self.netease, self.store)
        # 后台任务集合：分享即归档等异步副作用放这里跑，避免阻塞消息回复
        self._bg_tasks: set[asyncio.Task] = set()

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

            # 总库：启用后先判定该歌是否已存在于群级总库（跨窗口去重）。
            # 若总库早已存在，则不再写入当前窗口，避免同一首歌在多个窗口重复收录，
            # 仅记录为跨窗口重复提示（与同窗口重复互不冲突、不重复刷屏）。
            master_dup_stored = None
            already_in_master = False
            if self.config.master.enabled:
                m_inserted, m_stored = await self.store.add_song(
                    group_id, MASTER_KEY, song, src_window=state.key
                )
                if not m_inserted:
                    already_in_master = True
                    master_dup_stored = m_stored

            if already_in_master:
                # 已在总库（跨窗口重复）：不再写入当前窗口（避免重复计数）。
                # 但若当前窗口此前也已收录过这首歌，仍记为同窗口重复，走 notify_duplicate，
                # 否则开启总库后「同一首歌重复分享」会完全静默、不发任何提示。
                win_stored = await self.store.find_in_window(group_id, state.key, song)
                if win_stored is not None:
                    result.duplicated.append(win_stored)
                else:
                    result.master_duplicated.append(master_dup_stored)
                continue

            inserted, stored = await self.store.add_song(group_id, state.key, song)
            if inserted:
                result.accepted.append(stored)
            else:
                result.duplicated.append(stored)
            if master_dup_stored is not None:
                result.master_duplicated.append(master_dup_stored)

        for song in result.accepted + result.duplicated:
            if song.row_id is not None:
                result.index_of[id(song)] = await self.store.position_of(
                    group_id, state.key, song.row_id
                )
        for song in result.master_duplicated:
            if song.row_id is not None:
                result.master_index_of[id(song)] = await self.store.position_of(
                    group_id, MASTER_KEY, song.row_id
                )

        # 分享即归档：把本批新收录的歌增量同步到当前窗口歌单。
        # 改为后台执行——归档要对整窗口重新匹配+重排歌单，歌曲多时会很慢，
        # 不能阻塞消息回复（否则表现为"机器人无反应"）。失败只记日志。
        if result.accepted and self.config.playlist.auto_archive_on_share:
            self._spawn_bg(self.auto_archive_songs(group_id, state, result.accepted))
        # 总库分享即归档：仅把新歌增量追加到总库歌单顶部（后台执行，不阻塞回复）
        if result.accepted and self.config.master.enabled and self.config.master.auto_archive:
            self._spawn_bg(self.auto_archive_master(group_id, result.accepted))
        return result

    def _spawn_bg(self, coro) -> None:
        """把协程丢到后台执行，不阻塞当前消息处理；异常只记日志，不向上抛。

        离线测试（无事件循环）时静默跳过，由调用方自行 try/except。
        """
        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:
            return
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _master_state(self) -> WindowState:
        """构造总库用的窗口状态（歌单命名/简介用，无需真实时间区间）。"""
        return WindowState(
            mode="master",
            key=MASTER_KEY,
            label="总库",
            collecting=True,
            start_at=None,
            archive_at=None,
        )

    def _master_playlist_cfg(self) -> "PlaylistConfig":
        """把扁平的 master 配置组装成归档器需要的 PlaylistConfig。

        master 段为了网页端能平铺展示，字段是扁平的；归档器严格依赖
        ``PlaylistConfig``，这里在调用前临时拼一个。
        """
        m = self.config.master
        return PlaylistConfig(
            name_template=m.name_template,
            description_template=m.description_template,
            include_sharers=m.include_sharers,
            sharer_style=m.sharer_style,
            seq=m.seq,
            seq_auto_increment=m.seq_auto_increment,
            pending_name=m.pending_name,
            privacy=m.privacy,
            cross_platform_match=m.cross_platform_match,
            strict_match=m.strict_match,
            batch_size=m.batch_size,
            desc_retry=m.desc_retry,
            desc_retry_minutes=m.desc_retry_minutes,
            emoji_style=m.emoji_style,
            desc_show_artist=m.desc_show_artist,
            desc_blank_line=m.desc_blank_line,
            sharer_aliases=m.sharer_aliases,
        )

    async def auto_archive_master(self, group_id: int, new_songs: Optional[Sequence[Song]] = None) -> None:
        """把总库增量归档到总库歌单（静默/后台执行，不刷屏）。

        - 总库歌单已存在：仅把本次新分享的歌追加到顶部（总库 newest_first，新歌天然在顶），
          不再对整库重排，避免总库变大后每次分享都触发上千次接口调用被限流。
        - 总库歌单尚不存在（首次）：走全量归档建歌单。
        """
        try:
            cfg = self._master_playlist_cfg()
            existing = await self.store.get_archive(group_id, MASTER_KEY)
            if existing and str(existing.get("playlist_id") or "").isdigit() and new_songs:
                raw = (existing or {}).get("added_ids") or []
                # get_archive 返回的 added_ids 可能是 list/set（已解析）或 json 字符串
                prev_ids = json.loads(raw) if isinstance(raw, str) else list(raw)
                await self._append_master_playlist(
                    group_id, list(new_songs), cfg, int(existing["playlist_id"]), prev_ids
                )
                return
            all_songs = await self.store.list_songs(group_id, MASTER_KEY, newest_first=True)
            report = await self.archiver.archive(
                group_id, MASTER_KEY, "总库", all_songs, cfg,
                start_at=None, end_at=None, desc_songs=all_songs,
            )
            if report.ok:
                logger.info(
                    f"[music] 总库分享即归档 group={group_id} "
                    f"{'复用歌单追加' if not report.created_new else '新建歌单'} "
                    f"{report.added} 首（总库共 {len(all_songs)} 首）"
                )
            else:
                logger.warning(f"[music] 总库分享即归档失败 group={group_id}: {report.message}")
        except Exception as exc:
            logger.warning(f"[music] 总库分享即归档异常 group={group_id}: {type(exc).__name__} {exc}")

    async def _append_master_playlist(
        self,
        group_id: int,
        new_songs: list[Song],
        cfg: "PlaylistConfig",
        playlist_id: int,
        prev_ids: Sequence[str],
    ) -> None:
        """增量把新歌追加到总库歌单顶部（用于分享即归档，避免整库重排）。

        总库是 newest_first，新分享的歌天然应在最上面；直接按正序追加到顶部即可，
        无需把整库曲目移除再重加（那在总库很大时是上千次接口调用、极易被限流）。
        简介仍按全量重写，保证清单与歌单顺序一致。
        """
        pairs: list[tuple[str, Song]] = []
        for s in new_songs:
            if s.netease_id:
                pairs.append((s.netease_id, s))
                continue
            nid = await self.archiver.match_netease_id(s, cfg)
            if nid:
                s.netease_id = nid
                s.matched = True
                if s.row_id is not None:
                    await self.store.mark_matched(s.row_id, nid)
                pairs.append((nid, s))
            # 未匹配的非网易云歌不进歌单
        seen: set[str] = set()
        ordered: list[str] = []
        prev_set = set(prev_ids)
        for tid, _ in pairs:
            if tid in prev_set or tid in seen:
                continue
            seen.add(tid)
            ordered.append(tid)
        if not ordered:
            logger.info(f"[music] 总库分享即归档(增量) group={group_id} 本次无新匹配歌，跳过")
            return
        # add_tracks 会把整批倒序插到顶部，故反转提交后顶部即 ordered 正序（newest_first）
        add_order = list(reversed(ordered))
        added = 0
        for i in range(0, len(add_order), cfg.batch_size):
            batch = add_order[i:i + cfg.batch_size]
            try:
                await self.netease.add_tracks(playlist_id, batch)
                added += len(batch)
            except Exception:
                pass
            await asyncio.sleep(0.5)
        merged = sorted(prev_set | set(ordered))
        all_songs = await self.store.list_songs(group_id, MASTER_KEY, newest_first=True)
        report = ArchiveReport(total=len(all_songs))
        report.playlist_id = playlist_id
        report.playlist_url = self.netease.playlist_url(playlist_id)
        report.added = added
        await self.archiver._write_description_full(
            report, group_id, MASTER_KEY, "总库", all_songs, cfg, playlist_id,
            start_at=None, end_at=None, desc_songs=all_songs,
        )
        await self.store.record_archive(
            group_id, MASTER_KEY, str(playlist_id), report.playlist_url,
            len(all_songs), added, 0, added_ids=merged,
        )
        logger.info(
            f"[music] 总库分享即归档(增量) group={group_id} 新增 {added} 首"
            f"（总库共 {len(all_songs)} 首）"
        )

    async def aggregate_to_master(self, group_id: int) -> int:
        """把该群所有窗口的歌曲一键汇总进总库（跨窗口去重）。返回新增进总库的条数。"""
        windows = await self.store.windows_with_counts(group_id)
        added = 0
        for wk, _ in windows:
            songs = await self.store.list_songs(group_id, wk)
            for s in songs:
                song = Song(
                    platform=s.platform, song_id=s.song_id, title=s.title,
                    artists=s.artists, album=s.album, cover=s.cover, url=s.url,
                    duration=s.duration, sharer_id=s.sharer_id, sharer_name=s.sharer_name,
                    netease_id=s.netease_id, matched=s.matched, created_at=s.created_at,
                )
                ins, _ = await self.store.add_song(
                    group_id, MASTER_KEY, song, src_window=wk
                )
                if ins:
                    added += 1
        return added

    async def aggregate_window_to_master(
        self, group_id: int, window_key: str
    ) -> int:
        """把指定窗口某群的歌曲去重汇总进总库。返回新增进总库的条数。

        与 ``aggregate_to_master``（汇总该群*所有*窗口）不同，这里只处理单个窗口，
        供收集管理页「汇总到总库」按钮按当前窗口增量归档使用；不改动当前窗口。
        """
        songs = await self.store.list_songs(group_id, window_key)
        added = 0
        for s in songs:
            song = Song(
                platform=s.platform, song_id=s.song_id, title=s.title,
                artists=s.artists, album=s.album, cover=s.cover, url=s.url,
                duration=s.duration, sharer_id=s.sharer_id, sharer_name=s.sharer_name,
                netease_id=s.netease_id, matched=s.matched, created_at=s.created_at,
            )
            ins, _ = await self.store.add_song(
                group_id, MASTER_KEY, song, src_window=window_key
            )
            if ins:
                added += 1
        return added

    _PLAYLIST_RE = re.compile(
        r"music\.163\.com/(?:#/)?(?:m/)?playlist[/\?]?[^\s#]*?id=(\d+)", re.I
    )
    _PLAYLIST_RE2 = re.compile(r"music\.163\.com/playlist/(\d+)", re.I)

    @classmethod
    def _extract_playlist_id(cls, text: str) -> Optional[str]:
        """从文本（歌单链接或纯数字）里提取网易云歌单 id。"""
        if not text:
            return None
        m = cls._PLAYLIST_RE.search(text) or cls._PLAYLIST_RE2.search(text)
        if m:
            return m.group(1)
        s = text.strip()
        if s.isdigit():
            return s
        return None

    async def _song_details_bulk(self, ids: list[str]) -> dict[str, dict]:
        """批量拉歌曲详情（网易云一次有限制，按 200 分批），返回 id->detail。"""
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 200):
            batch = ids[i:i + 200]
            try:
                dets = await self.netease.song_detail(batch)
            except Exception:
                dets = []
            for d in dets:
                did = d.get("id")
                if did is not None:
                    out[str(did)] = d
        return out

    async def import_playlist_to_master(self, group_id: int, playlist_url: str) -> dict:
        """从网易云歌单链接解析全部曲目，导入到该群总库（跨窗口去重）。

        带 WebUI 入口（总库页「从歌单导入总库」）。导入的歌统一标记 platform=netease、
        netease_id=自身 id、matched=True，并按导入时刻写入 created_at（越新越靠前）。
        """
        pid = self._extract_playlist_id(playlist_url)
        if not pid:
            return {"ok": False, "message": "无法从链接解析出歌单 id"}
        if not self.netease.logged_in:
            return {"ok": False, "message": "网易云未登录，请先登录后再导入"}
        try:
            ids = await self.netease.playlist_track_ids(int(pid))
        except Exception as exc:
            return {"ok": False, "message": f"读取歌单曲目失败: {exc}"}
        if not ids:
            return {"ok": False, "message": "歌单为空或读取失败（可能不存在/无权限）"}
        details = await self._song_details_bulk([str(i) for i in ids])
        added = total = 0
        added_row_ids: list[int] = []
        for cid in ids:
            d = details.get(str(cid))
            if not d or not d.get("name"):
                continue
            total += 1
            arts = d.get("artists") or d.get("ar") or []
            artists = " / ".join(a.get("name", "") for a in arts)
            album = (d.get("album") or {}).get("name", "") or ""
            dur = int(d.get("duration") or d.get("dt") or 0) // 1000
            song = Song(
                platform="netease",
                song_id=str(cid),
                title=d["name"],
                artists=artists,
                album=album,
                netease_id=str(cid),
                matched=True,
                duration=dur,
                created_at=time.time(),
            )
            ins, stored = await self.store.add_song(
                group_id, MASTER_KEY, song, src_window="import"
            )
            if ins:
                added += 1
                if stored.row_id is not None:
                    added_row_ids.append(stored.row_id)
        result = {
            "ok": True,
            "added": added,
            "total": total,
            "message": f"已导入 {added}/{total} 首到总库"
            + ("" if added == total else f"（{total - added} 首已存在或信息缺失跳过）"),
        }
        if added_row_ids:
            hid = await self.store.record_import(
                group_id, MASTER_KEY, pid, playlist_url, total, added, added_row_ids
            )
            result["history_id"] = hid
        return result

    async def undo_master_import(
        self, group_id: int, history_id: Optional[int] = None
    ) -> dict:
        """撤回总库歌单导入。

        - 指定 ``history_id``：撤回该次具体导入；
        - 不指定：撤回该群**最近一次**未撤回的导入。
        撤回只删除本次新加入总库的歌曲（不影响导入前已存在或后来手动添加的歌），
        且不会自动改动已生成的网易云歌单（如需同步移除请手动「同步全部歌单」）。
        """
        if history_id is not None:
            return await self.store.undo_import(history_id)
        recs = await self.store.list_imports(group_id, MASTER_KEY, limit=1)
        if not recs:
            return {"ok": False, "message": "该群没有可撤回的导入记录"}
        return await self.store.undo_import(recs[0]["id"])

    async def run_master_archive(
        self, group_id: int, name_override: str = ""
    ) -> ArchiveReport:
        """把总库归档到网易云歌单（与正常收集一致：复用/新建、简介清单、期号等）。"""
        songs = await self.store.list_songs(group_id, MASTER_KEY, newest_first=True)
        cfg = self._master_playlist_cfg()
        report = await self.archiver.archive(
            group_id, MASTER_KEY, "总库", songs, cfg,
            start_at=None, end_at=None, name_override=name_override,
        )
        if report.ok and report.created_new:
            if cfg.pending_name and not name_override:
                config_manager.update("master.pending_name", "")
            if cfg.seq_auto_increment:
                config_manager.update("master.seq", cfg.seq + 1)
        return report

    async def sync_master_playlist(self, group_id: int) -> dict:
        """全量同步总库到总库歌单：增 + 删 + 简介（与正常收集一致）。"""
        cfg = self._master_playlist_cfg()
        songs = await self.store.list_songs(group_id, MASTER_KEY, newest_first=True)
        # 同步前先对总库内全部歌做跨平台匹配，确保非网易云来源的歌也能进歌单
        await self._ensure_matched(songs, cfg)
        arch = await self.store.get_archive(group_id, MASTER_KEY)

        if not arch or not str(arch.get("playlist_id") or ""):
            report = await self.archiver.archive(
                group_id, MASTER_KEY, "总库", songs, cfg,
                start_at=None, end_at=None,
            )
            if not report.ok:
                return {"ok": False, "message": report.message or "建歌单失败"}
            if report.created_new:
                if cfg.pending_name:
                    config_manager.update("master.pending_name", "")
                if cfg.seq_auto_increment:
                    config_manager.update("master.seq", cfg.seq + 1)
            arch = await self.store.get_archive(group_id, MASTER_KEY)
            if not arch:
                return {"ok": False, "message": "建歌单后未读到归档记录"}

        playlist_id = int(arch["playlist_id"])
        current_ids = [int(s.netease_id) for s in songs if s.netease_id]
        added_ids = set(int(x) for x in (arch.get("added_ids") or set()) if str(x).isdigit())
        to_add = [i for i in current_ids if i not in added_ids]
        to_remove = [i for i in added_ids if i not in set(current_ids)]

        added_n = removed_n = 0
        try:
            if to_add:
                await self.netease.add_tracks(playlist_id, [str(i) for i in to_add])
                added_n = len(to_add)
            if to_remove:
                await self.netease.remove_tracks(playlist_id, [str(i) for i in to_remove])
                removed_n = len(to_remove)
        except Exception as exc:
            return {"ok": False, "message": f"歌单增删失败: {exc}"}

        new_added = (added_ids | set(to_add)) - set(to_remove)
        await self.store.record_archive(
            group_id, MASTER_KEY, str(playlist_id), arch.get("playlist_url"),
            total=len(songs), added=len(new_added), failed=0,
            added_ids=[str(i) for i in new_added],
        )

        # 同步后按总库顺序重排歌单，保证歌单顺序与简介一致
        await self.archiver.reorder_to_match(
            playlist_id, songs, {str(i) for i in new_added},
        )

        desc = await self._render_description(
            songs, group_id, "总库", self._master_playlist_cfg(),
            start_at=None, end_at=None, count=len(songs),
        )
        desc_ok, desc_note = await self.push_description(
            playlist_id, desc, name="", group_id=group_id
        )
        return {
            "ok": True,
            "added": added_n,
            "removed": removed_n,
            "desc_ok": desc_ok,
            "playlist_id": playlist_id,
            "playlist_url": self.netease.playlist_url(playlist_id),
            "message": f"已同步总库：新增 {added_n} / 移除 {removed_n} 首"
            + ("" if desc_ok else "（简介写入待补写）"),
        }

    async def preview_master_name(self, group_id: int) -> str:
        songs = await self.store.list_songs(group_id, MASTER_KEY, newest_first=True)
        m = self.config.master
        context = build_context(
            group_id=group_id, window_label="总库",
            start_at=None, end_at=None,
            count=len(songs), total=len(songs), seq=m.seq, songs=songs,
            emoji_style=m.emoji_style, aliases=m.sharer_aliases,
        )
        return render_template(m.pending_name or m.name_template, context)

    async def preview_master_description(self, group_id: int) -> str:
        songs = await self.store.list_songs(group_id, MASTER_KEY, newest_first=True)
        return await self._render_description(
            songs, group_id, "总库", self._master_playlist_cfg(),
            start_at=None, end_at=None, count=len(songs),
        )

    async def _ensure_matched(self, songs: Sequence[Song], cfg) -> None:
        """为没有 ``netease_id`` 的歌做跨平台匹配，并落库（原地更新 ``song.netease_id``）。

        同步 / 分享即归档复用歌单时，需要保证窗口里所有歌（含历史收录、之前没匹配上的
        非网易云歌）都能被匹配进网易云歌单，而不只是当次新分享的那几首。
        """
        for s in songs:
            if s.netease_id or s.row_id is None:
                continue
            nid = await self.archiver.match_netease_id(s, cfg)
            if nid:
                s.netease_id = nid
                s.matched = True
                await self.store.mark_matched(s.row_id, nid)

    async def auto_archive_songs(
        self, group_id: int, state: WindowState, songs: Sequence[Song]
    ) -> None:
        """把一批新分享的歌增量归档到当前窗口歌单。

        复用同一窗口已建的歌单（不会新建、不消耗期号）；失败只记日志，
        不打断分享回复流程。会先对窗口内全部歌做跨平台匹配，确保非网易云的歌
        也能被加进歌单。
        """
        try:
            cfg = self.config.playlist
            all_songs = await self.store.list_songs(group_id, state.key)
            await self._ensure_matched(all_songs, cfg)
            report = await self.archiver.archive(
                group_id, state.key, state.label, all_songs, cfg,
                start_at=state.start_at,
                end_at=state.end_at or state.archive_at,
                desc_songs=all_songs,
            )
            if report.ok:
                logger.info(
                    f"[music] 分享即归档 group={group_id} window={state.key} "
                    f"{'复用歌单追加' if not report.created_new else '新建歌单'} "
                    f"{report.added} 首（总收录 {len(all_songs)} 首）"
                )
            else:
                logger.warning(f"[music] 分享即归档失败 group={group_id}: {report.message}")
        except Exception as exc:
            logger.warning(f"[music] 分享即归档异常 group={group_id}: {type(exc).__name__} {exc}")

    # ------------------------------------------------------------ 榜单

    async def build_report(
        self, group_id: int, window: Optional[WindowState] = None
    ) -> tuple[str, list[Path], list[Song]]:
        state = window or self.current_window()
        songs = await self.store.list_songs(group_id, state.key)
        title = f"群音乐收藏榜 · {state.label}"
        text = build_text_list(songs, title, aliases=self.config.playlist.sharer_aliases)
        images: list[Path] = []
        if songs:
            subtitle = f"共 {len(songs)} 首 · 窗口 {state.label}"
            images = await render_song_list(
                songs, title, subtitle, self.config.render, CACHE_DIR / "render",
                aliases=self.config.playlist.sharer_aliases,
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
            aliases=cfg.sharer_aliases,
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
            # 只有「新建歌单」才消耗一次性歌单名 / 自增期号；
            # 复用已有歌单追加时不改动命名与期号。
            if report.created_new:
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
        """重试所有待补写的歌单简介，返回 (成功数, 失败数)。

        补写时**重新生成**简介而不是重推存档的旧文本，否则期间删歌 / 改昵称
        映射 / 歌单又追加了新歌，都会让补写上去的清单与歌单内容对不上。
        """
        pending = await self.store.list_pending_desc(group_id)
        ok = failed = 0
        for row in pending:
            playlist_id = str(row.get("playlist_id") or "")
            if not playlist_id.isdigit():
                await self.store.drop_pending_desc(playlist_id)
                continue
            desc = await self._description_for_pending(row, playlist_id)
            success, note = await self.netease.update_description(
                int(playlist_id),
                desc,
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
                    desc,
                    note,
                )
                failed += 1
        return ok, failed

    async def _description_for_pending(self, row: dict, playlist_id: str) -> str:
        """为一条待补写记录生成简介文本。

        数据优先级：
        1. 该窗口当前的收集记录（反映删歌 / 改昵称映射等后续变更）
        2. 归档当时存的快照（窗口数据被清空时用它兜底）
        3. 存档的旧文本（两者都没有时的最后退路）
        """
        group_id = int(row.get("group_id") or 0)
        window_key = str(row.get("window_key") or "")
        try:
            snapshot = json.loads(row.get("snapshot") or "{}")
        except (TypeError, json.JSONDecodeError):
            snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}

        songs: list[Song] = []
        if group_id and window_key:
            songs = await self.store.list_songs(group_id, window_key)
        if not songs:
            songs = songs_from_snapshot(snapshot)
        if not songs:
            return str(row.get("description") or "")

        # 收录数对齐歌单实际内容，保证「共 N 首」与实际一致
        count = len(songs)
        try:
            arch = await self.store.get_archive_by_playlist(playlist_id)
        except Exception:
            arch = None
        if arch and arch.get("added_ids"):
            count = len(arch["added_ids"])

        tz = self.resolver.tz
        start_at = self._ts_to_dt(snapshot.get("start_at"), tz)
        end_at = self._ts_to_dt(snapshot.get("end_at"), tz)
        return await self._render_description(
            songs,
            group_id,
            str(snapshot.get("label") or window_key or ""),
            self.config.playlist,
            start_at=start_at,
            end_at=end_at,
            count=count,
        )

    @staticmethod
    def _ts_to_dt(value: object, tz):
        """快照里的时间戳还原成带时区的 datetime。"""
        if not value:
            return None
        try:
            return datetime.fromtimestamp(float(value), tz)
        except (TypeError, ValueError):
            return None

    async def _render_description(
        self,
        songs: list[Song],
        group_id: int,
        window_label: str,
        cfg,
        *,
        start_at=None,
        end_at=None,
        count: Optional[int] = None,
    ) -> str:
        """按当前配置渲染一份简介（歌单名 + 分享清单）。

        ``count`` 用于「共 N 首」；缺省时按歌曲条数。
        """
        from .naming import (
            build_name_lines,
            build_sharer_lines,
            build_song_lines,
            fit_description,
        )

        context = build_context(
            group_id=group_id,
            window_label=window_label,
            start_at=start_at,
            end_at=end_at,
            count=count if count is not None else len(songs),
            total=len(songs),
            seq=cfg.seq,
            songs=songs,
            emoji_style=cfg.emoji_style,
            aliases=cfg.sharer_aliases,
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
                    aliases=cfg.sharer_aliases,
                )
            elif cfg.sharer_style == "by_name":
                body = build_name_lines(
                    songs, emoji_style=cfg.emoji_style, aliases=cfg.sharer_aliases
                )
            else:
                body = build_song_lines(
                    songs,
                    emoji_style=cfg.emoji_style,
                    show_artist=cfg.desc_show_artist,
                    aliases=cfg.sharer_aliases,
                )
        return fit_description(header, body)

    async def pending_desc_list(self, group_id: Optional[int] = None) -> list[dict]:
        return await self.store.list_pending_desc(group_id)

    async def rebuild_description(
        self, group_id: int, window: Optional[WindowState] = None
    ) -> str:
        """按当前配置重新生成一份简介文本（用于手动补写 / 预览）。"""
        state = window or self.current_window()
        songs = await self.store.list_songs(group_id, state.key)
        return await self._render_description(
            songs,
            group_id,
            state.label,
            self.config.playlist,
            start_at=state.start_at,
            end_at=state.end_at or state.archive_at,
        )

    async def push_description(
        self, playlist_id: int, desc: str, name: str = "", group_id: int = 0
    ) -> tuple[bool, str]:
        """把指定简介写到指定歌单（失败自动入队）。"""
        return await self.archiver.write_description(
            playlist_id, desc, name=name, group_id=group_id,
            retries=self.config.playlist.desc_retry,
        )

    # ------------------------------------------------------------ 网页端手动收集管理

    _NETEASE_RE = re.compile(
        r"music\.163\.com/(?:#/)?(?:m/)?song/?\?(?:[^#\s]*&)?id=(\d+)", re.I
    )
    _NETEASE_RE2 = re.compile(
        r"music\.163\.com/(?:#/)?(?:m/)?song/(\d+)", re.I
    )

    @staticmethod
    def _extract_netease_id(text: str) -> Optional[str]:
        """从一段文本（链接或纯数字）里提取网易云歌曲 id。"""
        if not text:
            return None
        m = service._NETEASE_RE.search(text) or service._NETEASE_RE2.search(text)
        if m:
            return m.group(1)
        s = text.strip()
        if s.isdigit():
            return s
        return None

    async def _expand_short_link(self, url: str) -> Optional[str]:
        """展开 163cn.tv 这类短链，拿到最终带 song id 的 URL。"""
        try:
            import httpx
        except Exception:
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url.strip())
                return str(resp.url)
        except Exception:
            return None

    async def manual_add_song(
        self, group_id: int, window_key: str, payload: dict
    ) -> dict:
        """网页端手动添加一首歌到指定群/窗口。

        ``payload`` 含 platform / song_id / title / artists / sharer_name /
        sharer_id / url。网易云来源且给了 song_id 但没给歌名时，自动拉详情补全。
        """
        platform = str(payload.get("platform") or "netease")
        song_id = str(payload.get("song_id") or "").strip()
        title = (payload.get("title") or "").strip()
        artists = (payload.get("artists") or "").strip()
        sharer_name = (payload.get("sharer_name") or "").strip() or "手动添加"
        try:
            sharer_id = int(payload.get("sharer_id") or 0)
        except (TypeError, ValueError):
            sharer_id = 0
        url = (payload.get("url") or "").strip()

        if platform == "netease" and song_id and not title:
            try:
                details = await self.netease.song_detail([song_id])
            except Exception:
                details = []
            if details:
                d = details[0]
                title = d.get("name") or title
                arts = d.get("artists") or []
                artists = " / ".join(a.get("name", "") for a in arts) or artists
                url = url or (d.get("url") or "")

        if not song_id:
            return {"ok": False, "message": "缺少歌曲 id（网易云为数字 id）"}
        song = Song(
            platform=platform,
            song_id=song_id,
            title=title or "未命名歌曲",
            artists=artists,
            sharer_name=sharer_name,
            sharer_id=sharer_id,
            url=url,
            created_at=time.time(),
        )
        inserted, stored = await self.store.add_song(group_id, window_key, song)
        return {
            "ok": True,
            "inserted": inserted,
            "song": {
                "title": stored.title,
                "artists": stored.artists,
                "netease_id": stored.netease_id,
                "matched": stored.matched,
            },
            "message": "已添加" if inserted else "该歌曲已存在（未重复添加）",
        }

    async def edit_song(
        self, group_id: int, window_key: str, index: int, payload: dict
    ) -> dict:
        """网页端编辑一条已收集歌曲：歌名 / 歌手 / 分享者 / 原链接 / 匹配链接。

        - ``url``：原平台链接（如 QQ音乐分享链接），直接存文本。
        - ``netease_link``：网易云歌曲链接；仅当提供有效链接时才重新匹配，
          并把歌名/歌手/专辑覆盖为匹配结果（与手动匹配语义一致）。
        """
        song = await self.store.get_song_by_index(
            group_id, window_key, index, newest_first=(window_key == MASTER_KEY)
        )
        if song is None or song.row_id is None:
            return {"ok": False, "message": "序号无效或歌曲不存在"}
        upd: dict = {}
        if payload.get("title") is not None:
            upd["title"] = str(payload["title"]).strip()
        if payload.get("artists") is not None:
            upd["artists"] = str(payload["artists"]).strip()
        if payload.get("sharer_name") is not None:
            upd["sharer_name"] = str(payload["sharer_name"]).strip()
        if payload.get("sharer_id") is not None:
            try:
                upd["sharer_id"] = int(payload["sharer_id"])
            except (TypeError, ValueError):
                pass
        if payload.get("url") is not None:
            upd["url"] = str(payload["url"]).strip()
        # 匹配链接：仅在提供了有效链接时才重新匹配
        netease_link = payload.get("netease_link")
        if netease_link is not None and str(netease_link).strip():
            try:
                sid, title, artists, album = await self._resolve_netease_link(
                    netease_link,
                    fallback_title=song.title,
                    fallback_artists=song.artists,
                    fallback_album=song.album,
                )
            except ValueError as exc:
                return {"ok": False, "message": str(exc)}
            upd["netease_id"] = sid
            upd["matched"] = 1
            upd["title"] = title
            upd["artists"] = artists
            upd["album"] = album
        if not upd:
            return {"ok": False, "message": "没有可修改的字段"}
        await self.store.update_song_meta(song.row_id, **upd)
        return {"ok": True, "message": "已保存修改"}

    async def _resolve_netease_link(
        self, link: str, *, fallback_title: str = "", fallback_artists: str = "",
        fallback_album: str = "",
    ) -> tuple[str, str, str, str]:
        """解析网易云链接 -> (song_id, title, artists, album)。

        解析不到 id 抛 ValueError；拿到 id 后拉详情补全标题/歌手/专辑，
        拉不到详情时回退到传入的 fallback（保持原值）。match_song 与 edit_song 共用。
        """
        sid = self._extract_netease_id(link)
        if not sid and "http" in (link or ""):
            expanded = await self._expand_short_link(link)
            if expanded:
                sid = self._extract_netease_id(expanded)
        if not sid:
            raise ValueError("无法从链接解析出网易云歌曲 id")
        title, artists, album = fallback_title, fallback_artists, fallback_album
        try:
            details = await self.netease.song_detail([sid])
        except Exception:
            details = []
        if details:
            d = details[0]
            if d.get("name"):
                title = d["name"]
            arts = d.get("artists") or []
            if arts:
                artists = " / ".join(a.get("name", "") for a in arts)
            album = (d.get("album") or {}).get("name", "") or album
        return sid, title, artists, album

    async def match_song(
        self, group_id: int, window_key: str, index: int, netease_link: str
    ) -> dict:
        """网页端手动匹配：把一条已收集歌曲绑定到粘贴的网易云链接（正确的歌）。

        解析链接 → 取 song_id → 拉详情补全标题/歌手 → 写 netease_id + matched。
        """
        song = await self.store.get_song_by_index(
            group_id, window_key, index, newest_first=(window_key == MASTER_KEY)
        )
        if song is None or song.row_id is None:
            return {"ok": False, "message": "序号无效或歌曲不存在"}

        try:
            sid, title, artists, album = await self._resolve_netease_link(
                netease_link,
                fallback_title=song.title,
                fallback_artists=song.artists,
                fallback_album=song.album,
            )
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}

        await self.store.update_song_meta(
            song.row_id,
            netease_id=sid,
            matched=1,
            title=title,
            artists=artists,
            album=album,
        )
        return {
            "ok": True,
            "message": f"已绑定网易云歌曲 {sid}",
            "song": {"title": title, "artists": artists, "netease_id": sid, "matched": True},
        }

    async def reorder_songs(
        self, group_id: int, window_key: str, ordered_indices: list[int]
    ) -> dict:
        """网页端排序：``ordered_indices`` 是当前显示顺序（1 基）重新排列后的完整列表。"""
        songs = await self.store.list_songs(group_id, window_key)
        by_index = {i + 1: s.row_id for i, s in enumerate(songs) if s.row_id is not None}
        ordered_row_ids = [by_index[i] for i in ordered_indices if i in by_index]
        if not ordered_row_ids:
            return {"ok": False, "message": "排序列表为空或无效"}
        n = await self.store.set_song_order(group_id, window_key, ordered_row_ids)
        return {"ok": True, "count": n, "message": f"已重排 {n} 首"}

    async def sync_playlist(self, group_id: int, window: Optional[WindowState] = None) -> dict:
        """全量同步当前窗口到歌单：增 + 删 + 简介。

        - 尚无歌单：先建歌单并加入全部（不触发「归档后清空」）。
        - 已有歌单：计算窗口有/歌单无(to_add) 与 歌单有/窗口无(to_remove) 做对账，
          调 add/remove，重写 added_ids，再重写简介。
        """
        state = window or self.current_window()
        cfg = self.config.playlist
        songs = await self.store.list_songs(group_id, state.key)
        # 同步前先对窗口内全部歌做跨平台匹配，确保非网易云来源的歌也能进歌单
        await self._ensure_matched(songs, cfg)
        arch = await self.store.get_archive(group_id, state.key)

        if not arch or not str(arch.get("playlist_id") or ""):
            report = await self.archiver.archive(
                group_id, state.key, state.label, songs, cfg,
                start_at=state.start_at,
                end_at=state.end_at or state.archive_at,
            )
            if not report.ok:
                return {"ok": False, "message": report.message or "建歌单失败"}
            if report.created_new:
                if cfg.pending_name:
                    config_manager.update("playlist.pending_name", "")
                if cfg.seq_auto_increment:
                    config_manager.update("playlist.seq", cfg.seq + 1)
            arch = await self.store.get_archive(group_id, state.key)
            if not arch:
                return {"ok": False, "message": "建歌单后未读到归档记录"}

        playlist_id = int(arch["playlist_id"])
        current_ids = [int(s.netease_id) for s in songs if s.netease_id]
        added_ids = set(int(x) for x in (arch.get("added_ids") or set()) if str(x).isdigit())
        to_add = [i for i in current_ids if i not in added_ids]
        to_remove = [i for i in added_ids if i not in set(current_ids)]

        added_n = removed_n = 0
        try:
            if to_add:
                await self.netease.add_tracks(playlist_id, [str(i) for i in to_add])
                added_n = len(to_add)
            if to_remove:
                await self.netease.remove_tracks(playlist_id, [str(i) for i in to_remove])
                removed_n = len(to_remove)
        except Exception as exc:
            return {"ok": False, "message": f"歌单增删失败: {exc}"}

        new_added = (added_ids | set(to_add)) - set(to_remove)
        await self.store.record_archive(
            group_id, state.key, str(playlist_id), arch.get("playlist_url"),
            total=len(songs), added=len(new_added), failed=0,
            added_ids=[str(i) for i in new_added],
        )

        # 同步后按窗口顺序重排歌单，保证歌单顺序与简介/清单一致
        await self.archiver.reorder_to_match(
            playlist_id, songs, {str(i) for i in new_added},
        )

        desc = await self.rebuild_description(group_id, state)
        desc_ok, desc_note = await self.push_description(
            playlist_id, desc, name="", group_id=group_id
        )
        return {
            "ok": True,
            "added": added_n,
            "removed": removed_n,
            "desc_ok": desc_ok,
            "playlist_id": playlist_id,
            "playlist_url": self.netease.playlist_url(playlist_id),
            "message": f"已同步：新增 {added_n} / 移除 {removed_n} 首"
            + ("" if desc_ok else "（简介写入待补写）"),
        }

    # ------------------------------------------------------------ 清理收集数据

    async def clear_window(self, group_id: int, window_key: str) -> int:
        """清空某个群在某个窗口下的全部已收集歌曲。"""
        return await self.store.delete_window(group_id, window_key)

    async def clear_indices(
        self, group_id: int, window_key: str, indices: Sequence[int]
    ) -> int:
        """按序号批量删除已收集歌曲。"""
        # 总库视图是 newest_first 渲染的，序号→row_id 映射必须与之同序，否则删错歌
        return await self.store.delete_songs_by_indices(
            group_id, window_key, indices, newest_first=(window_key == MASTER_KEY)
        )

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
