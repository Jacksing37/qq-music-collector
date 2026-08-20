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
    #: 简介是否成功写入网易云
    desc_ok: bool = False
    #: 简介写入失败原因（成功时是命中的通道名）
    desc_note: str = ""
    #: 实际生成的简介文本，失败时可用于手动补写
    description: str = ""

    def summary(self, aliases: Optional[dict[str, str]] = None) -> str:
        if not self.ok:
            return f"归档失败：{self.message}"
        lines = [
            f"歌单已生成：{self.playlist_name}" if self.playlist_name else "歌单已生成",
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

    async def write_description(
        self,
        playlist_id: int,
        desc: str,
        *,
        name: str = "",
        group_id: int = 0,
        retries: int = 3,
    ) -> tuple[bool, str]:
        """写简介 + 退避重试；仍失败则入队等待补写。"""
        note = "未尝试"
        for attempt in range(1, max(1, retries) + 1):
            ok, note = await self.api.update_description(playlist_id, desc, name=name)
            if ok:
                await self.store.drop_pending_desc(str(playlist_id))
                return True, note
            if attempt < max(1, retries):
                await asyncio.sleep(min(3 * attempt, 10))
        await self.store.save_pending_desc(
            str(playlist_id), name, group_id, desc, note
        )
        return False, note

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
    ) -> ArchiveReport:
        report = ArchiveReport(total=len(songs))
        if not songs:
            report.message = "该窗口没有收集到歌曲，跳过建歌单"
            return report
        if not self.api.logged_in:
            report.message = "网易云未登录，请管理员先私聊机器人执行 /music cookie <MUSIC_U>"
            return report

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
        matched_songs: list[Song] = []
        seen: set[str] = set()
        for tid, song in matched_pairs:
            if tid not in seen:
                seen.add(tid)
                ordered_unique.append(tid)
                matched_songs.append(song)

        if not ordered_unique:
            report.message = "没有任何歌曲能匹配到网易云曲库"
            return report

        # 2. 建歌单
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

        # 3. 分批加歌
        # 网易云 add 接口会把整批歌曲「倒序」插到歌单顶部，直接按原序提交会导致
        # 最终歌单顺序和简介清单相反。所以这里把顺序整体反转后再提交，抵消它的倒序。
        add_order = list(reversed(ordered_unique))
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

        # 4. 简介：模板开头 + 「谁分享了什么歌」清单
        context["count"] = str(added)
        header = render_template(cfg.description_template, context)
        body_lines: list[str] = []
        if cfg.include_sharers and cfg.sharer_style != "none":
            listed = matched_songs or list(songs)
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
            playlist_id, desc, name=name, group_id=group_id, retries=cfg.desc_retry
        )

        report.ok = True
        report.playlist_id = playlist_id
        report.playlist_url = self.api.playlist_url(playlist_id)
        report.added = added

        await self.store.record_archive(
            group_id, window_key, str(playlist_id), report.playlist_url,
            report.total, added, len(report.unmatched),
        )
        return report
