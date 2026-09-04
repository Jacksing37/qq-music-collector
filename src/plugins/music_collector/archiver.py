"""归档器：把收集到的歌曲写进网易云新建歌单。

非网易云来源的歌曲会先在网易云做一次搜索匹配，匹配不上的会在报告里单独列出。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from datetime import datetime

from .config import PlaylistConfig
from .models import Song
from .naming import (
    build_context,
    build_name_lines,
    build_sharer_lines,
    build_song_lines,
    fit_description,
    render_template,
    resolve_alias,
)
from .netease_api import NeteaseAPI, NeteaseError
from .store import Store

try:  # 插件内用 nonebot logger，离线测试退回标准库
    from nonebot.log import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("music_collector.archiver")

# 搜索限速，避免触发风控
_SEARCH_INTERVAL = 0.35

_BRACKET_RE = re.compile(r"[（(\[【｛{].*?[)）\]】｝}]")
_NOISE_RE = re.compile(r"[\s\-_·・,，.。'\"’“”!！?？/\\|~～]+")
_ARTIST_SPLIT_RE = re.compile(r"[、/&,，×xX＆]|\bfeat\.?\b|\bft\.?\b", re.I)


def normalize_title(text: str) -> str:
    text = (text or "").lower()
    text = _BRACKET_RE.sub("", text)
    return _NOISE_RE.sub("", text)


def artist_set(text: str) -> set[str]:
    parts = _ARTIST_SPLIT_RE.split(text or "")
    return {normalize_title(p) for p in parts if normalize_title(p)}


def score_candidate(song: Song, candidate: dict) -> int:
    """给搜索结果打分，越高越可信。"""
    cand_title = str(candidate.get("name", ""))
    cand_artists = "、".join(
        a.get("name", "") for a in (candidate.get("ar") or candidate.get("artists") or [])
    )
    cand_duration = int(candidate.get("dt") or candidate.get("duration") or 0) // 1000

    score = 0
    src_t, dst_t = normalize_title(song.title), normalize_title(cand_title)
    if src_t and src_t == dst_t:
        score += 100
    elif src_t and (src_t in dst_t or dst_t in src_t):
        score += 55

    src_a, dst_a = artist_set(song.artists), artist_set(cand_artists)
    if src_a and dst_a:
        if src_a & dst_a:
            score += 60
        elif any(a in b or b in a for a in src_a for b in dst_a):
            score += 30
    elif not src_a:
        score += 10  # 原歌手未知，不惩罚

    if song.duration and cand_duration and abs(song.duration - cand_duration) <= 5:
        score += 20
    return score


def is_acceptable(song: Song, candidate: dict, strict: bool) -> bool:
    src_t = normalize_title(song.title)
    dst_t = normalize_title(str(candidate.get("name", "")))
    if not src_t or not dst_t:
        return False
    if strict:
        if src_t != dst_t:
            return False
        src_a = artist_set(song.artists)
        if not src_a:
            return True
        cand_artists = "、".join(
            a.get("name", "") for a in (candidate.get("ar") or candidate.get("artists") or [])
        )
        dst_a = artist_set(cand_artists)
        if not dst_a:
            # 搜索接口没带回歌手信息时无法核对，标题已精确相等即接受
            return True
        return bool(src_a & dst_a) or any(a in b or b in a for a in src_a for b in dst_a)
    return src_t == dst_t or src_t in dst_t or dst_t in src_t


def _pick_best(
    candidates: list[dict], song: Song, *, strict: Optional[bool]
) -> tuple[Optional[dict], int]:
    """从搜索结果里挑分数最高的候选。

    strict=True   歌名精确相等 + 歌手有交集（配置默认）
    strict=False  歌名相等或互相包含（宽松）
    strict=None   只要求歌名精确相等 —— 跨语言歌手名（Apple 英文名 vs
                  网易云中文名）或歌手信息缺失时的兜底，靠 score 的
                  时长加分偏向原版。
    """
    src_t = normalize_title(song.title)
    best: Optional[dict] = None
    best_score = -1
    for cand in candidates:
        dst_t = normalize_title(str(cand.get("name", "")))
        if not dst_t:
            continue
        if strict is True:
            if not is_acceptable(song, cand, True):
                continue
        elif strict is False:
            if not is_acceptable(song, cand, False):
                continue
        else:  # strict is None：只认歌名完全一致
            if dst_t != src_t:
                continue
        s = score_candidate(song, cand)
        if s > best_score:
            best, best_score = cand, s
    return best, best_score


_SNAPSHOT_FIELDS = (
    "platform", "song_id", "title", "artists", "album", "duration",
    "sharer_id", "sharer_name", "netease_id", "created_at",
)


def snapshot_payload(
    window_key: str,
    window_label: str,
    start_at: Optional[datetime],
    end_at: Optional[datetime],
    songs: Sequence[Song],
) -> dict:
    """归档当时的上下文快照，用于简介补写时按同一批歌曲重建。"""
    return {
        "window_key": window_key,
        "label": window_label,
        "start_at": start_at.timestamp() if start_at else None,
        "end_at": end_at.timestamp() if end_at else None,
        "songs": [
            {f: getattr(s, f) for f in _SNAPSHOT_FIELDS} for s in songs
        ],
    }


def songs_from_snapshot(payload: object) -> list[Song]:
    """把快照里的歌曲还原成 Song（字段缺失/多余都容错）。"""
    if not isinstance(payload, dict):
        return []
    out: list[Song] = []
    for raw in payload.get("songs") or []:
        if not isinstance(raw, dict):
            continue
        data = {k: v for k, v in raw.items() if k in _SNAPSHOT_FIELDS}
        if not data.get("title"):
            continue
        out.append(Song(**data))
    return out


@dataclass
class ArchiveReport:
    ok: bool = False
    message: str = ""
    playlist_id: Optional[int] = None
    playlist_url: Optional[str] = None
    playlist_name: str = ""
    total: int = 0
    added: int = 0
    unmatched: list[Song] = field(default_factory=list)
    #: 本次是否新建了歌单；False 表示复用了当前窗口已存在的歌单追加
    created_new: bool = True
    #: 简介是否成功写入网易云
    desc_ok: bool = False
    #: 简介写入失败原因（成功时是命中的通道名）
    desc_note: str = ""
    #: 实际生成的简介文本，失败时可用于手动补写
    description: str = ""

    def summary(self, aliases: Optional[dict[str, str]] = None) -> str:
        if not self.ok:
            return f"归档失败：{self.message}"
        if self.added <= 0 and not self.created_new:
            return (
                f"当前窗口的歌单已是最新（{self.playlist_name}）\n"
                f"链接: {self.playlist_url}\n没有需要追加的新歌。"
            )
        lines = [
            f"歌单{'已更新' if not self.created_new else '已生成'}：{self.playlist_name}",
            f"链接: {self.playlist_url}",
            f"收录 {self.added}/{self.total} 首",
        ]
        if self.description:
            lines.append(
                "简介（含分享清单）已写入 ✅"
                if self.desc_ok
                else f"简介写入失败 ⚠️（{self.desc_note}）\n"
                     f"已存入待补写队列，稍后自动重试，也可手动执行 /music descfix"
            )
        if self.unmatched:
            lines.append(f"以下 {len(self.unmatched)} 首在网易云没匹配到，需要手动处理：")
            for s in self.unmatched[:15]:
                raw = s.sharer_name or (str(s.sharer_id) if s.sharer_id else "匿名")
                sharer = resolve_alias(raw, s.sharer_id, aliases)
                artist = s.artists or "未知歌手"
                lines.append(
                    f"  · {sharer} 分享《{s.title}》- {artist}（{s.platform_name}）"
                )
            if len(self.unmatched) > 15:
                lines.append(f"  …… 还有 {len(self.unmatched) - 15} 首")
        return "\n".join(lines)


class Archiver:
    def __init__(self, api: NeteaseAPI, store: Store) -> None:
        self.api = api
        self.store = store
        #: 串行化归档写歌单，防止自动归档 / 手动 / 定时同时触发时网易云频控或顺序错乱
        self._lock = asyncio.Lock()

    async def write_description(
        self,
        playlist_id: int,
        desc: str,
        *,
        name: str = "",
        group_id: int = 0,
        retries: int = 3,
        window_key: Optional[str] = None,
        snapshot: Optional[dict] = None,
    ) -> tuple[bool, str]:
        """写简介 + 退避重试；仍失败则入队等待补写。

        ``window_key`` / ``snapshot`` 会随队列一起存下来，让补写时能按当前数据
        （或归档时的快照）重新生成简介，而不是直接重推这段旧文本。
        """
        note = "未尝试"
        for attempt in range(1, max(1, retries) + 1):
            ok, note = await self.api.update_description(playlist_id, desc, name=name)
            if ok:
                await self.store.drop_pending_desc(str(playlist_id))
                return True, note
            if attempt < max(1, retries):
                await asyncio.sleep(min(3 * attempt, 10))
        await self.store.save_pending_desc(
            str(playlist_id), name, group_id, desc, note,
            window_key=window_key, snapshot=snapshot,
        )
        return False, note

    async def reorder_to_match(
        self,
        playlist_id: int,
        songs: Sequence[Song],
        merged_ids: set[str],
    ) -> None:
        """把歌单里已有的曲目重排成与 ``songs`` 一致的顺序，使歌单与简介顺序对齐。

        - 仅当 NeteaseAPI 提供重排接口时执行；测试 stub 无该方法则直接跳过，
          不影响归档主流程。
        - 以「当前歌单实际曲目」为基准：清单曲目按 ``songs`` 顺序排列在前，
          清单外（如用户手动加的歌）原样附加在后，绝不会误删歌单里的歌。
        - 任何异常都不影响归档，仅记日志。
        """
        try:
            if not hasattr(self.api, "reorder_tracks") or not hasattr(
                self.api, "playlist_track_ids"
            ):
                return
            current = await self.api.playlist_track_ids(playlist_id)
            if not current:
                return
            merged = {str(i) for i in merged_ids}
            canonical: list[str] = [
                str(s.netease_id) for s in songs
                if s.netease_id and str(s.netease_id) in merged
            ]
            canon_set = set(canonical)
            extra = [cid for cid in current if cid not in canon_set]
            desired = canonical + extra
            if desired == current:
                return
            await self.api.reorder_tracks(playlist_id, desired)
        except Exception as exc:
            logger.warning(f"[music] 歌单重排失败 playlist={playlist_id}: {exc}")

    async def match_netease_id(self, song: Song, cfg: PlaylistConfig) -> Optional[str]:
        """为一首歌找到对应的网易云歌曲 id。

        两级匹配：先按配置的严格/宽松规则选（歌名+歌手都吻合）；
        仍无结果时降级为「歌名完全一致」里分数最高的——覆盖 Apple Music
        歌手名跨语言（Kenshi Yonezu ↔ 米津玄師）等场景。
        """
        if song.platform == "netease" and song.song_id.isdigit():
            return song.song_id
        if song.netease_id:
            return song.netease_id
        if not cfg.cross_platform_match:
            return None

        keyword = f"{song.title} {song.artists}".strip()
        if not keyword or song.title == "未识别歌曲":
            return None
        try:
            candidates = await self.api.search_songs(keyword, limit=20)
        except Exception:
            return None
        if not candidates:
            return None

        # 第一级：按配置的严格/宽松规则选（歌名+歌手都吻合）
        best, _ = _pick_best(candidates, song, strict=cfg.strict_match)
        if best is None:
            # 第二级兜底：只按歌名完全一致选。解决 Apple Music 歌手名是
            # 英文（Kenshi Yonezu）而网易云是中文（米津玄師）等跨语言差异，
            # 以及候选没带回歌手信息导致第一级全被拒的场景。
            best, _ = _pick_best(candidates, song, strict=None)
        return str(best.get("id")) if best else None

    async def archive(
        self,
        group_id: int,
        window_key: str,
        window_label: str,
        songs: Sequence[Song],
        cfg: PlaylistConfig,
        *,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        name_override: str = "",
        desc_songs: Optional[Sequence[Song]] = None,
    ) -> ArchiveReport:
        """把一批歌曲写进当前窗口的歌单。

        - 窗口已归档过（archives 表有 playlist_id）时**复用**原歌单，只追加
          还没收录过的歌，不新建、不消耗 pending_name / 期号。
        - 简介始终按 ``desc_songs``（缺省为本次 songs 全量）重写，保证
          「谁分享了什么」清单与歌单内容一致。
        """
        async with self._lock:
            return await self._archive_locked(
                group_id, window_key, window_label, songs, cfg,
                start_at=start_at, end_at=end_at, name_override=name_override,
                desc_songs=desc_songs,
            )

    async def _archive_locked(
        self,
        group_id: int,
        window_key: str,
        window_label: str,
        songs: Sequence[Song],
        cfg: PlaylistConfig,
        *,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        name_override: str = "",
        desc_songs: Optional[Sequence[Song]] = None,
    ) -> ArchiveReport:
        report = ArchiveReport(total=len(songs))
        if not songs:
            report.message = "该窗口没有收集到歌曲，跳过建歌单"
            return report
        if not self.api.logged_in:
            report.message = "网易云未登录，请管理员先私聊机器人执行 /music cookie <MUSIC_U>"
            return report

        # 0. 查当前窗口是否已归档过：有则复用歌单，只追加新歌
        existing = await self.store.get_archive(group_id, window_key)
        reused_playlist_id: Optional[int] = None
        added_set: set[str] = set()
        if existing and str(existing.get("playlist_id") or "").isdigit():
            reused_playlist_id = int(existing["playlist_id"])
            added_set = set(existing.get("added_ids") or set())
            report.created_new = False

        # 1. 逐首匹配 id
        matched_pairs: list[tuple[str, Song]] = []
        for song in songs:
            netease_id = await self.match_netease_id(song, cfg)
            if netease_id:
                matched_pairs.append((netease_id, song))
                if song.row_id is not None and song.netease_id != netease_id:
                    await self.store.mark_matched(song.row_id, netease_id)
            else:
                report.unmatched.append(song)
            if song.platform != "netease":
                await asyncio.sleep(_SEARCH_INTERVAL)

        # 保持先后顺序的同时去重
        ordered_unique: list[str] = []
        seen: set[str] = set()
        for tid, song in matched_pairs:
            if tid not in seen:
                seen.add(tid)
                ordered_unique.append(tid)

        if not ordered_unique:
            report.message = "没有任何歌曲能匹配到网易云曲库"
            return report

        # 2. 与歌单内已有歌曲求差集：只处理真正的新歌
        new_ids = [tid for tid in ordered_unique if tid not in added_set]

        # 新建歌单（仅首次归档时）
        if reused_playlist_id is None:
            context = build_context(
                group_id=group_id,
                window_label=window_label,
                start_at=start_at,
                end_at=end_at,
                count=len(ordered_unique),
                total=len(songs),
                seq=cfg.seq,
                songs=songs,
                emoji_style=cfg.emoji_style,
                aliases=cfg.sharer_aliases,
            )
            name = (name_override or cfg.pending_name or cfg.name_template).strip()
            name = render_template(name, context) or f"群歌单 {window_label}"
            report.playlist_name = name
            try:
                playlist_id = await self.api.create_playlist(name, cfg.privacy)
            except NeteaseError as exc:
                report.message = exc.message
                return report
            except Exception as exc:
                report.message = f"创建歌单异常: {exc}"
                return report
            report.created_new = True
        else:
            playlist_id = reused_playlist_id
            # 复用场景不重新生成歌单名；archives 表不存名字，用窗口标签兜底展示
            report.playlist_name = f"群歌单 {window_label}"

        if not new_ids:
            # 没有新歌可追加：简介仍按当前窗口全量重写一次（保持清单最新）
            report.playlist_url = self.api.playlist_url(playlist_id)
            report.added = 0
            await self._write_description_full(
                report, group_id, window_key, window_label, songs,
                cfg, playlist_id, start_at=start_at, end_at=end_at,
                desc_songs=desc_songs,
            )
            report.ok = True
            report.playlist_id = playlist_id
            await self.store.record_archive(
                group_id, window_key, str(playlist_id), report.playlist_url,
                report.total, report.added, len(report.unmatched),
                added_ids=sorted(added_set),
            )
            return report

        # 3. 分批加歌（复用已有歌单 = 追加；新歌单 = 初始填充）
        # 网易云 add 接口会把整批歌曲「倒序」插到歌单顶部，直接按原序提交会导致
        # 最终歌单顺序和简介清单相反。所以这里把顺序整体反转后再提交，抵消它的倒序。
        add_order = list(reversed(new_ids))
        added = 0
        for i in range(0, len(add_order), cfg.batch_size):
            batch = add_order[i:i + cfg.batch_size]
            try:
                await self.api.add_tracks(playlist_id, batch)
                added += len(batch)
            except Exception:
                # 单批失败不影响其余批次
                pass
            await asyncio.sleep(0.5)
        merged_ids = added_set | set(new_ids)

        # 增量追加会把新歌顶到歌单顶部，导致「歌单顺序」与「简介顺序」相反；
        # 这里按简介（desc_songs）的顺序把歌单重排回一致。复用场景（reused 非 None）
        # 才走到这里，新建歌单的情况由上方整批反转保证正序，无需重排。
        if reused_playlist_id is not None:
            listed = desc_songs if desc_songs is not None else songs
            await self.reorder_to_match(playlist_id, listed, merged_ids)

        report.ok = True
        report.playlist_id = playlist_id
        report.playlist_url = self.api.playlist_url(playlist_id)
        report.added = added

        await self._write_description_full(
            report, group_id, window_key, window_label, songs,
            cfg, playlist_id, start_at=start_at, end_at=end_at,
            desc_songs=desc_songs, total_in_playlist=len(merged_ids),
        )

        await self.store.record_archive(
            group_id, window_key, str(playlist_id), report.playlist_url,
            report.total, added, len(report.unmatched),
            added_ids=sorted(merged_ids),
        )
        return report

    async def _write_description_full(
        self,
        report: ArchiveReport,
        group_id: int,
        window_key: str,
        window_label: str,
        songs: Sequence[Song],
        cfg: PlaylistConfig,
        playlist_id: int,
        *,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        desc_songs: Optional[Sequence[Song]] = None,
        total_in_playlist: Optional[int] = None,
    ) -> None:
        """生成并写入歌单简介。

        ``desc_songs`` 缺省时用本次 ``songs``；自动归档（单曲）场景下由调用方
        传入窗口全量，保证简介清单完整。
        """
        listed = desc_songs if desc_songs is not None else songs
        context = build_context(
            group_id=group_id,
            window_label=window_label,
            start_at=start_at,
            end_at=end_at,
            count=total_in_playlist if total_in_playlist is not None else len(listed),
            total=len(listed),
            seq=cfg.seq,
            songs=listed,
            emoji_style=cfg.emoji_style,
            aliases=cfg.sharer_aliases,
        )
        header = render_template(cfg.description_template, context)
        body_lines: list[str] = []
        if cfg.include_sharers and cfg.sharer_style != "none":
            if cfg.sharer_style == "by_person":
                body_lines = build_sharer_lines(
                    listed,
                    emoji_style=cfg.emoji_style,
                    show_artist=cfg.desc_show_artist,
                    blank_line=cfg.desc_blank_line,
                    aliases=cfg.sharer_aliases,
                )
            elif cfg.sharer_style == "by_name":
                body_lines = build_name_lines(
                    listed, emoji_style=cfg.emoji_style, aliases=cfg.sharer_aliases
                )
            else:
                body_lines = build_song_lines(
                    listed,
                    emoji_style=cfg.emoji_style,
                    show_artist=cfg.desc_show_artist,
                    aliases=cfg.sharer_aliases,
                )
        if report.unmatched:
            body_lines.append("以下歌曲在网易云未匹配到，未收录（含分享者方便查找）：")
            body_lines += build_song_lines(
                report.unmatched,
                with_platform=True,
                emoji_style=cfg.emoji_style,
                show_artist=cfg.desc_show_artist,
                aliases=cfg.sharer_aliases,
            )
        desc = fit_description(header, body_lines)
        report.description = desc
        report.desc_ok, report.desc_note = await self.write_description(
            playlist_id, desc, name=report.playlist_name, group_id=group_id,
            retries=cfg.desc_retry,
            window_key=window_key,
            snapshot=snapshot_payload(window_key, window_label, start_at, end_at, listed),
        )
