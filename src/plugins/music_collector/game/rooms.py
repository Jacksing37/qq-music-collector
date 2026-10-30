"""房间与对局状态机（纯内存，不碰 nonebot、不碰数据库）。

设计要点，都是踩过坑才这么写的：

1. **答案留在服务端**。``state()`` 只输出题目（歌名/封面/播放 id）和候选池，
   不下发 ``sharer``。否则前端 F12 一看就知道答案，游戏就没意义了。
2. **心跳不算动作**。``last_active`` 只在 加入/选源/贴歌/删歌/开始/提交/转让
   这些业务动作时更新；WS 的 ping 与拉快照都不更新。否则 WS 每 30 秒 ping 一次，
   房间永远不会被回收。
3. **房主 = 在座玩家里 ``seq`` 最小的那位**。用户说的「房主退出后第二个进来的人接任」
   就是这条，用加入序号判定，不做数组下标——转让之后下标会乱。
4. **断线重连靠 player token**。同一个 QQ 带对 token 就恢复原身份与已提交答案。
"""

from __future__ import annotations

import asyncio
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

#: 房间号字符集：去掉了 0/O/1/I 这类容易看错的
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

PHASE_LOBBY = "lobby"        # 等人
PHASE_SETUP = "setup"        # 选题库 / 现场贴歌 / 预览
PHASE_PLAYING = "playing"    # 全员作答
PHASE_SETTLED = "settled"    # 已开牌

PHASE_LABELS = {
    PHASE_LOBBY: "等待开始",
    PHASE_SETUP: "准备中",
    PHASE_PLAYING: "作答中",
    PHASE_SETTLED: "已结算",
}


@dataclass
class Card:
    """一道题。``uid`` 只在房间内唯一，答题记录用它当键（不是列表下标，
    因为中途删歌会让下标错位）。"""

    uid: int
    title: str
    artists: str = ""
    cover: str = ""
    url: str = ""
    platform: str = ""
    song_id: str = ""
    netease_id: Optional[str] = None
    duration: int = 0
    sharer_qq: int = 0
    sharer_name: str = ""

    @property
    def iframe_id(self) -> Optional[str]:
        """网易云 iframe 要的歌曲 id；匹配不到就返回 None（前端退化成封面+外链）。"""
        if self.netease_id:
            return self.netease_id
        if self.platform == "netease" and self.song_id:
            return self.song_id
        return None

    def public(self, idx: int) -> dict:
        """给前端看的样子——**故意不含 sharer 字段**。"""
        return {
            "uid": self.uid,
            "idx": idx,
            "title": self.title,
            "artists": self.artists,
            "cover": self.cover,
            "url": self.url,
            "platform": self.platform,
            "netease_id": self.iframe_id,
            "duration": self.duration,
        }


@dataclass
class Player:
    qq: int
    nick: str
    token: str
    seq: int
    joined_at: float = field(default_factory=time.time)
    answers: dict[int, list[int]] = field(default_factory=dict)
    online: bool = False
    score: int = 0
    hits: int = 0
    #: 现场收集的限流用：最近若干次贴歌的时刻
    stamps: list[float] = field(default_factory=list)


@dataclass
class Room:
    code: str
    mode: str                     # multi / single
    host_qq: int
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    phase: str = PHASE_LOBBY
    players: dict[int, Player] = field(default_factory=dict)
    cards: list[Card] = field(default_factory=list)
    source: Optional[dict] = None   # {"kind":"window","group_id":..,"window_key":..}
    label: str = ""
    result: Optional[dict] = None
    archive: Optional[dict] = None
    uid_seq: int = 0
    seq_seq: int = 0
    #: 房间级锁：同一房间的动作串行执行，不同房间互不阻塞。
    #: 现场贴歌要联网解析（可能好几秒），用一把全局锁会把别的房间一起卡住。
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ---------------------------------------------------------------- 成员

    def host(self) -> Optional[Player]:
        return self.players.get(self.host_qq)

    def ordered(self) -> list[Player]:
        return sorted(self.players.values(), key=lambda p: p.seq)

    def next_host(self) -> Optional[int]:
        """接任房主的人：在座玩家里最早加入的（不含即将离开的那位）。"""
        rest = sorted(self.players.values(), key=lambda p: p.seq)
        return rest[0].qq if rest else None

    def touch(self) -> None:
        """记一次业务动作。心跳不调用这里。"""
        self.last_active = time.time()

    def reset_round(self) -> None:
        """开下一局：保留题库来源，清空作答与结算。"""
        self.phase = PHASE_SETUP if self.cards else PHASE_LOBBY
        self.result = None
        self.archive = None
        for p in self.players.values():
            p.answers = {}
            p.score = 0
            p.hits = 0
        self.touch()

    # ---------------------------------------------------------------- 题目

    def add_card(self, card: Card) -> bool:
        """加一首题。同歌重复（按平台 id）或跨平台同曲（按歌名+歌手）都跳过。"""
        if any(
            c.platform == card.platform and c.song_id == card.song_id for c in self.cards
        ):
            return False
        key = _song_key(card.title, card.artists)
        if any(_song_key(c.title, c.artists) == key for c in self.cards):
            return False
        self.uid_seq += 1
        card.uid = self.uid_seq
        self.cards.append(card)
        self.touch()
        return True

    def remove_card(self, uid: int) -> bool:
        before = len(self.cards)
        self.cards = [c for c in self.cards if c.uid != uid]
        for p in self.players.values():
            p.answers.pop(uid, None)
        if len(self.cards) != before:
            self.touch()
            return True
        return False

    def candidates(self) -> list[dict]:
        """候选池：本题库全部分享者，去重后按首次出现排序。

        这是全局同一份清单（每题都一样），所以它本身不泄露「哪首歌是谁的」。
        """
        seen: dict[int, dict] = {}
        for card in self.cards:
            if card.sharer_qq and card.sharer_qq not in seen:
                seen[card.sharer_qq] = {
                    "qq": card.sharer_qq,
                    "nick": card.sharer_name or str(card.sharer_qq),
                }
        return list(seen.values())

    # ---------------------------------------------------------------- 作答

    def submit(self, qq: int, answers: dict[int, list[int]], slots: int) -> None:
        player = self.players[qq]
        valid_uids = {c.uid for c in self.cards}
        cleaned: dict[int, list[int]] = {}
        for raw_uid, picks in answers.items():
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue
            if uid not in valid_uids or not isinstance(picks, list):
                continue
            row: list[int] = []
            # 先去重再截断：前端若传来 [A,A,B,C] 这种，直接切前三个会丢掉 C
            for q in picks:
                try:
                    q = int(q)
                except (TypeError, ValueError):
                    continue
                if q not in row:
                    row.append(q)
            cleaned[uid] = row[:slots]
        player.answers = cleaned
        self.touch()

    def all_submitted(self) -> bool:
        return all(p.answers for p in self.players.values()) and bool(self.players)

    # ---------------------------------------------------------------- 结算

    def settle(self, slots: int, scores: list[int]) -> dict:
        """算分并生成本局结果。

        计分：某题的**真实分享者**排在第 i 位（0 起）→ 得 ``scores[i]`` 分；
        没排进前三 / 没作答 → 0 分。总分就是各题相加。
        """
        for card in self.cards:
            for p in self.players.values():
                picks = p.answers.get(card.uid) or []
                if card.sharer_qq and card.sharer_qq in picks:
                    idx = picks.index(card.sharer_qq)
                    if idx < len(scores):
                        p.score += scores[idx]
                        p.hits += 1

        ranking = _ranking(list(self.players.values()))
        details = []
        for i, card in enumerate(self.cards):
            picks = []
            for p in self.ordered():
                guesses = p.answers.get(card.uid) or []
                idx = -1
                gained = 0
                if card.sharer_qq and card.sharer_qq in guesses:
                    idx = guesses.index(card.sharer_qq)
                    if idx < len(scores):
                        gained = scores[idx]
                picks.append({
                    "qq": p.qq,
                    "nick": p.nick,
                    "guesses": [
                        {"qq": q, "nick": self.nick_of(q)} for q in guesses
                    ],
                    "hit": idx,
                    "score": gained,
                })
            details.append({
                "uid": card.uid,
                "idx": i,
                "title": card.title,
                "artists": card.artists,
                "cover": card.cover,
                "netease_id": card.iframe_id,
                "sharer": {"qq": card.sharer_qq, "nick": self.nick_of(card.sharer_qq)},
                "picks": picks,
            })

        self.result = {"ranking": ranking, "details": details}
        self.phase = PHASE_SETTLED
        self.touch()
        return self.result

    def nick_of(self, qq: int) -> str:
        p = self.players.get(qq)
        if p is not None:
            return p.nick
        for card in self.cards:
            if card.sharer_qq == qq:
                return card.sharer_name or str(qq)
        return str(qq)


def _ranking(players: list[Player]) -> list[dict]:
    """分数降序，同分并列同名次（1,2,2,4），不按提交时间破平。"""
    rows = sorted(players, key=lambda p: (-p.score, p.seq))
    out: list[dict] = []
    last_score: Optional[int] = None
    last_rank = 0
    for i, p in enumerate(rows):
        if p.score != last_score:
            last_rank = i + 1
            last_score = p.score
        out.append({
            "qq": p.qq,
            "nick": p.nick,
            "score": p.score,
            "hits": p.hits,
            "rank": last_rank,
        })
    return out


def _song_key(title: str, artists: str) -> str:
    """跨平台去重用的键：歌名 + 首位歌手，去掉空格/括号/大小写差异。

    同一首歌在网易云和 QQ 音乐被两个人分享时，如果只按平台 id 去重，就会变成
    两道答案不同的题——玩家看到两个一模一样的卡片却要给出不同答案，直接懵。
    """
    import re

    def norm(text: str) -> str:
        text = (text or "").lower()
        text = re.sub(r"[\(（\[【].*?[\)）\]】]", "", text)
        text = re.sub(r"[\s\-_/、,，.。!！?？'\"“”’]+", "", text)
        return text

    first_artist = re.split(r"[/、,，&]| feat\.? ", artists or "", maxsplit=1)[0]
    return f"{norm(title)}|{norm(first_artist)}"


def new_code(existing: Callable[[str], bool]) -> str:
    """生成一个没被占用的房间号。"""
    for _ in range(50):
        code = "".join(random.choice(_CODE_ALPHABET) for _ in range(6))
        if not existing(code):
            return code
    raise RuntimeError("房间号生成失败（房间太多了）")


def new_token() -> str:
    return secrets.token_urlsafe(24)


class RoomError(Exception):
    """业务错误，带一条可以直接显示给玩家的话。"""


class RoomManager:
    """全部房间的容器。一个进程一个实例。"""

    def __init__(self, cfg_fn: Callable[[], object]) -> None:
        self._rooms: dict[str, Room] = {}
        self._cfg = cfg_fn

    # ---------------------------------------------------------------- 基础

    def get(self, code: str) -> Room:
        room = self._rooms.get((code or "").strip().upper())
        if room is None:
            raise RoomError("房间不存在或已解散")
        return room

    def find(self, code: str) -> Optional[Room]:
        return self._rooms.get((code or "").strip().upper())

    def count(self) -> int:
        return len(self._rooms)

    def create(self, qq: int, nick: str, mode: str) -> tuple[Room, Player]:
        code = new_code(lambda c: c in self._rooms)
        room = Room(code=code, mode=mode, host_qq=qq)
        self._rooms[code] = room
        player = self.join(room, qq, nick, host=True)
        if mode == "single":
            room.phase = PHASE_SETUP
        return room, player

    def join(self, room: Room, qq: int, nick: str, host: bool = False) -> Player:
        existing = room.players.get(qq)
        if existing is not None:
            # 同一 QQ 再进来：不是新人，是重连。token 保持不变，答案也不清。
            existing.online = True
            if nick:
                existing.nick = nick
            return existing
        cfg = self._cfg()
        if len(room.players) >= int(getattr(cfg, "max_players", 20)):
            raise RoomError(f"房间满了（上限 {getattr(cfg, 'max_players', 20)} 人）")
        room.seq_seq += 1
        player = Player(qq=qq, nick=nick or str(qq), token=new_token(), seq=room.seq_seq, online=True)
        room.players[qq] = player
        if host or room.host_qq not in room.players:
            room.host_qq = qq
        room.touch()
        return player

    def leave(self, room: Room, qq: int) -> bool:
        """离开房间。返回房间是否已空（空房由调用方回收）。"""
        player = room.players.pop(qq, None)
        if player is None:
            return not room.players
        room.touch()
        if not room.players:
            return True
        if room.host_qq == qq:
            nxt = room.next_host()
            if nxt is not None:
                room.host_qq = nxt
        # 人走了一条答案，「全员提交」的判定要重来
        return False

    def transfer(self, room: Room, actor: int, target: int) -> None:
        if room.host_qq != actor:
            raise RoomError("只有房主可以转让")
        if target not in room.players:
            raise RoomError("对方不在房间里")
        room.host_qq = target
        room.touch()

    def drop(self, code: str) -> None:
        self._rooms.pop((code or "").upper(), None)

    def reap(self) -> list[str]:
        """回收房间：空房立刻回收，超过 ``room_idle_minutes`` 无业务动作的也回收。

        返回被回收的房间号（调用方据此通知还在连着的 WS）。
        """
        cfg = self._cfg()
        idle = float(getattr(cfg, "room_idle_minutes", 60)) * 60
        now = time.time()
        dead = [
            code for code, room in self._rooms.items()
            if not room.players or now - room.last_active > idle
        ]
        for code in dead:
            self._rooms.pop(code, None)
        return dead

    # ---------------------------------------------------------------- 快照

    def state(self, room: Room, viewer: int) -> dict:
        """给某个玩家看的房间快照。

        **不含任何题的 sharer 字段**；真实答案只在 ``phase == settled`` 的
        ``result`` 里出现。
        """
        cfg = self._cfg()
        slots = int(getattr(cfg, "guess_slots", 3))
        scores = list(getattr(cfg, "scores", [3, 2, 1]))
        me = room.players.get(viewer)
        submitted = sum(1 for p in room.players.values() if p.answers)
        payload = {
            "code": room.code,
            "mode": room.mode,
            "phase": room.phase,
            "phase_label": PHASE_LABELS.get(room.phase, room.phase),
            "host_qq": room.host_qq,
            "is_host": room.host_qq == viewer,
            "me": {"qq": viewer, "nick": me.nick if me else ""},
            "players": [
                {
                    "qq": p.qq,
                    "nick": p.nick,
                    "online": p.online,
                    "submitted": bool(p.answers),
                    "score": p.score,
                    "is_host": p.qq == room.host_qq,
                    "is_me": p.qq == viewer,
                }
                for p in room.ordered()
            ],
            "source": room.source,
            "label": room.label,
            "slots": slots,
            "scores": scores,
            "candidates": room.candidates(),
            "cards": [c.public(i) for i, c in enumerate(room.cards)],
            "my_answers": {
                str(uid): picks for uid, picks in (me.answers.items() if me else [])
            },
            "submitted": submitted,
            "total": len(room.players),
            "archive": room.archive,
        }
        if room.phase == PHASE_SETTLED and room.result is not None:
            payload["result"] = room.result
        return payload
