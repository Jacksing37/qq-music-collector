"""猜歌游戏与收集主流程之间的桥：取题目、现场解析、归档。

这里的每一个动作都尽量复用机器人已有的能力（``detector`` / ``providers`` /
``archiver`` / 网易云匹配），而不是另写一套——题目要能播、要能归档，靠的就是
这些现成的实现。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from ..models import Song
from ..service import service as collect_service
from .rooms import Card, Room, RoomError

try:
    from nonebot.log import logger as _log
except Exception:  # pragma: no cover - 离线测试退化成标准库
    import logging

    _log = logging.getLogger("music_collector.game")

#: 一次请求最多解析几首（防止有人一口气贴 200 条把解析器打满）
MAX_LINKS_PER_REQUEST = 10
#: 现场收集允许的最多题量，避免房间把内存撑爆
MAX_CARDS = 200
#: 进房间时最多给几首歌补做网易云匹配（每首约 1 秒，太多会让「选中窗口」卡很久）
MAX_MATCH_PER_ROOM = 30


# ------------------------------------------------------------------ 选题库


async def list_windows(game_store) -> list[dict]:
    """可选的历史收集窗口（群 → 窗口），带一个能看懂的时间标签。

    ``game_store`` 由调用方传入（router 持有那个实例）—— 这里只借它的
    ``window_stats`` 读一眼 ``songs`` 表，不自己再造一个连接。
    """
    rows = await game_store.window_stats()
    out: list[dict] = []
    for r in rows:
        first = _fmt_ts(r.get("first_at"))
        last = _fmt_ts(r.get("last_at"))
        span = first if not last or first == last else f"{first} ~ {last}"
        out.append({
            "group_id": int(r["group_id"]),
            "window_key": str(r["window_key"]),
            "count": int(r["n"]),
            "sharers": int(r.get("sharers") or 0),
            "label": span,
            "text": f"群 {r['group_id']} · {span} · {int(r['n'])} 首",
        })
    return out


def _fmt_ts(value: object) -> str:
    try:
        ts = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


async def cards_from_window(group_id: int, window_key: str) -> list[Card]:
    """把某个历史窗口的歌搬成题目卡片。

    分享者信息在这里进内存（成为答案），但**不会**出现在下发给前端的快照里。

    顺手给「还没匹配到网易云」的歌补一次匹配：没有 ``netease_id`` 就没有播放器，
    卡片上只能干看歌名。历史窗口里这类歌多半是 QQ 音乐 / 酷狗的分享（线上实测
    1197 首里 10 首），补一次能救回来大半。匹配成功的写回库里，第二次进就快了。
    """
    songs = await _store().list_songs(group_id, window_key)
    pl = collect_service.cfg(group_id or None).playlist
    need = [s for s in songs if not s.netease_id and s.platform != "netease"]
    matched = 0
    for song in need[:MAX_MATCH_PER_ROOM]:
        try:
            nid = await collect_service.archiver.match_netease_id(song, pl)
        except Exception:  # noqa: BLE001 - 匹配失败就退化成外链，不算错
            nid = None
        if nid:
            song.netease_id = nid
            song.matched = True
            matched += 1
            if song.row_id is not None:
                try:
                    await _store().mark_matched(song.row_id, nid)
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.sleep(0.3)  # 网易云搜索有频控，别连发

    cards: list[Card] = []
    for s in songs:
        cards.append(Card(
            uid=0,
            title=s.title,
            artists=s.artists,
            cover=s.cover,
            url=s.url,
            platform=s.platform,
            song_id=s.song_id,
            netease_id=s.netease_id,
            duration=s.duration,
            sharer_qq=int(s.sharer_id or 0),
            sharer_name=s.sharer_name,
        ))
    if matched:
        _log(f"窗口 {group_id}/{window_key} 补匹配网易云 {matched} 首")
    await tag_vip(cards)
    return cards


async def tag_vip(cards: list[Card]) -> int:
    """批量问网易云要一次歌曲详情，标出 VIP / 付费专辑 / 已下架的曲目。

    为什么值得多这一次请求：内嵌播放器**不带登录态**，这类歌必然放不出声，玩家
    只会以为页面坏了。一次 ``song_detail`` 最多能带几百个 id，摊到每首歌上几乎免费。
    详情拿不到就当作不知道（不标），绝不因为没有标记而挡住答题。
    """
    ids = [c.iframe_id for c in cards if c.iframe_id]
    if not ids:
        return 0
    uniq = list(dict.fromkeys(ids))
    try:
        rows = await collect_service.netease.song_detail(uniq)
    except Exception as exc:  # noqa: BLE001 - 标记失败不影响出题
        _log.warning(f"查网易云歌曲权限失败：{type(exc).__name__}")
        return 0
    fee = {str(r.get("id") or ""): r.get("fee") for r in rows or []}
    status = {str(r.get("id") or ""): r.get("st") for r in rows or []}
    n = 0
    for card in cards:
        key = str(card.iframe_id or "")
        if key not in fee:
            continue
        f = fee[key]
        try:
            down = int(status.get(key) or 0) < 0
        except (TypeError, ValueError):
            down = False
        # fee: 0=免费 1=VIP 4=付费专辑 8=低音质免费（试听）
        card.vip = bool(down or f in (1, 4))
        if card.vip:
            n += 1
    return n


# ------------------------------------------------------------------ 现场收集


async def add_live_songs(room: Room, qq: int, nick: str, text: str) -> tuple[int, str]:
    """解析一段粘贴进来的文本，把识别到的歌加成题目。

    返回 ``(新增题数, 给玩家的一句话)``。解析失败不算错——玩家一次贴好几条，
    中间混几句寒暄很正常。
    """
    from .. import detector

    cfg = _game_cfg()
    player = room.players.get(qq)
    if player is None:
        raise RoomError("你不在这个房间里")
    if len(room.cards) >= MAX_CARDS:
        return 0, f"题量已达上限（{MAX_CARDS} 首），先开始游戏吧"

    # 限流：每人每分钟最多贴 N 次，防止把解析器当免费代理刷
    now = time.time()
    player.stamps = [t for t in player.stamps if now - t < 60]
    limit = int(getattr(cfg, "live_limit_per_min", 10))
    if len(player.stamps) >= limit:
        return 0, f"贴得太快了，每分钟最多 {limit} 次"
    player.stamps.append(now)

    links = detector.extract_from_text(text or "")
    if not links:
        return 0, "这段文本里没认出音乐链接（支持网易云 / QQ音乐 / 酷狗 / 酷我）"
    links = links[:MAX_LINKS_PER_REQUEST]
    links = await detector.expand_links(links)

    pl = collect_service.cfg(None).playlist
    added = 0
    dup = 0
    failed = 0
    new_cards: list[Card] = []
    for link in links:
        try:
            song = await collect_service.providers.resolve(link)
        except Exception:  # noqa: BLE001 - 单条失败不该让整次粘贴失败
            failed += 1
            continue
        if not song.title or song.title == "未识别歌曲":
            failed += 1
            continue
        # 非网易云的歌要匹配一次网易云，否则卡片里没有播放器
        if song.platform != "netease" and pl.cross_platform_match and not song.netease_id:
            try:
                song.netease_id = await collect_service.archiver.match_netease_id(song, pl)
            except Exception:  # noqa: BLE001 - 匹配失败就退化成封面+外链
                song.netease_id = None
        card = Card(
            uid=0,
            title=song.title,
            artists=song.artists,
            cover=song.cover,
            url=song.url,
            platform=song.platform,
            song_id=song.song_id,
            netease_id=song.netease_id,
            duration=song.duration,
            sharer_qq=qq,
            sharer_name=nick or player.nick,
        )
        if room.add_card(card):
            added += 1
            new_cards.append(card)
        else:
            dup += 1

    if new_cards:
        await tag_vip(new_cards)

    parts = [f"新增 {added} 首"]
    if dup:
        parts.append(f"重复 {dup} 首已跳过")
    if failed:
        parts.append(f"{failed} 条没解析出来")
    return added, "，".join(parts)


# ------------------------------------------------------------------ 归档


async def archive_room(room: Room) -> dict:
    """把房间里的题（现场收集的那批）归档成网易云歌单。

    复用收集机器人的 ``Archiver``：同一套匹配、命名模板、简介清单（谁分享了什么），
    所以这里的歌单和群里收集出来的长得一样。窗口键用 ``GAME-<房间号>``，
    与 ``W/D/O`` 开头的收集窗口互不干扰。
    """
    if not room.cards:
        return {"ok": False, "message": "还没有曲目可以归档"}
    songs: list[Song] = []
    for card in room.cards:
        songs.append(Song(
            platform=card.platform,
            song_id=card.song_id,
            title=card.title,
            artists=card.artists,
            cover=card.cover,
            url=card.url,
            duration=card.duration,
            sharer_id=card.sharer_qq,
            sharer_name=card.sharer_name,
            netease_id=card.netease_id,
            matched=bool(card.netease_id),
        ))

    gid = 0
    if room.source and room.source.get("kind") == "window":
        try:
            gid = int(room.source.get("group_id") or 0)
        except (TypeError, ValueError):
            gid = 0
    pl = collect_service.cfg(gid or None).playlist

    label = room.label or f"猜歌 {room.code}"
    # 歌单名不套 playlist.name_template：那个模板是给「本期群歌单」设计的，
    # 套到游戏上会得到「群歌单 现场收集」这种读起来别扭的名字。这里直接给个
    # 「猜歌 10-30 19:34 现场收集」，一眼看得出是哪一局。
    name = f"猜歌 {datetime.now():%m-%d %H:%M} {label}"

    report = await collect_service.archiver.archive(
        gid,
        f"GAME-{room.code}",
        label,
        songs,
        pl,
        start_at=None,
        end_at=None,
        name_override=name,
        desc_songs=songs,
    )
    payload = {
        "ok": bool(report.ok),
        "message": report.message,
        "playlist_id": report.playlist_id,
        "url": report.playlist_url,
        "name": report.playlist_name or name,
        "total": report.total,
        "added": report.added,
        "desc_ok": report.desc_ok,
        "unmatched": [s.title for s in report.unmatched],
        "at": time.time(),
    }
    room.archive = payload
    room.touch()
    return payload


# ------------------------------------------------------------------ 内部


def _store():
    return collect_service.store


def _game_cfg():
    from ..config import config_manager

    return config_manager.config.game


__all__ = [
    "list_windows",
    "cards_from_window",
    "add_live_songs",
    "archive_room",
    "MAX_CARDS",
]
