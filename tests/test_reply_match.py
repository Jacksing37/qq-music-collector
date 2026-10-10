"""「引用『没法匹配』的提示 + 贴网易云链接 = 手动指定匹配」的回归测试。

覆盖：
1. ``Store``：``song_notices`` 登记 / 反查 / 同群过滤 / ``get_song_by_row``，
   ``prune_old`` 会把过期映射一起清掉；
2. ``service.match_unmatched_by_row``：成功绑定；已匹配过 / 总库 / 歌已不在 一律拒绝；
3. ``_message_id_of``：dict / 类 dict / 取不到；
4. 规则 ``_looks_like_match_reply``：没引用 / 没网易云链接 / 引用消息没登记过 /
   功能开关关掉，都不该命中；
5. handler ``handle_match_reply``：无权限只发提示不改库；分享者本人 / 群管 / 群主
   可以改；已匹配过给可读提示；成功后 ``netease_id`` 落库。

运行: python tests/test_reply_match.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message  # noqa: E402
from nonebot.adapters.onebot.v11.event import Reply, Sender  # noqa: E402

from music_collector import (  # noqa: E402
    _looks_like_match_reply,
    _message_id_of,
    handle_match_reply,
)
from music_collector.archiver import Archiver  # noqa: E402
from music_collector.config import config_manager  # noqa: E402
from music_collector.models import Song  # noqa: E402
from music_collector.service import service  # noqa: E402
from music_collector.store import MASTER_KEY, Store  # noqa: E402

PASSED = 0
FAILED = 0

GID = 7001
WK = "W20261010-1200"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


# ---------------------------------------------------------------- 替身


class StubNetease:
    """够用的网易云桩：``song_detail`` 固定回一首「正确的歌」。"""

    logged_in = True

    def __init__(self) -> None:
        self.detail_calls: list[list[str]] = []

    async def song_detail(self, ids):
        self.detail_calls.append(list(ids))
        return [{
            "name": "正确的歌",
            "artists": [{"name": "正确歌手"}],
            "album": {"name": "正确专辑"},
        }]

    async def search_songs(self, keyword, limit=10):
        return []

    async def create_playlist(self, name, privacy=False):
        return 123456

    async def add_tracks(self, playlist_id, batch):
        return {"code": 200}

    async def remove_tracks(self, playlist_id, batch):
        return {"code": 200}

    async def update_description(self, playlist_id, desc, name=""):
        return True, "ok"

    def playlist_url(self, playlist_id):
        return f"https://music.163.com/#/playlist?id={playlist_id}"


class _FakeAdapter:
    def get_name(self) -> str:
        return "OneBot V11"


class FakeBot:
    def __init__(self, self_id: int = 1) -> None:
        self.self_id = self_id
        self.adapter = _FakeAdapter()
        self.config = types.SimpleNamespace(superusers=set())
        self.sent: list[Message] = []

    async def send(self, event, message, **_kw):
        self.sent.append(message)
        return {"message_id": 9000 + len(self.sent)}


def make_event(
    *,
    user_id: int = 42,
    role: str = "member",
    text: str = "",
    reply_to: int | None = 99,
    group_id: int = GID,
) -> GroupMessageEvent:
    event = GroupMessageEvent(
        time=0,
        self_id=1,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=200,
        message=Message(text),
        original_message=Message(text),
        raw_message=text,
        font=0,
        sender=Sender(user_id=user_id, nickname="张三", role=role),
        group_id=group_id,
        anonymous=None,
    )
    if reply_to is not None:
        event.reply = Reply(
            time=0,
            message_type="group",
            message_id=reply_to,
            real_id=reply_to,
            sender=Sender(user_id=1, nickname="bot", role="member"),
            message=Message("已收录"),
        )
    return event


LINK = "https://music.163.com/song?id=123456"


def _setup(tmp: Path) -> tuple[Store, StubNetease]:
    store = Store(tmp / "reply.db")
    api = StubNetease()
    # 装在**单例**上：__init__.py 里的 handler 读的就是这个 service
    service.store = store
    service.netease = api
    service.archiver = Archiver(api, store)
    config_manager.path = tmp / "config.yaml"
    config_manager.load()
    return store, api


async def _add_song(
    store: Store, *, title: str = "原曲", netease_id=None, sharer_id: int = 42,
    window_key: str = WK,
) -> Song:
    song = Song(
        platform="qq", song_id=f"QQ-{title}", title=title, artists="原唱",
        sharer_id=sharer_id, sharer_name="张三", netease_id=netease_id,
        matched=bool(netease_id),
    )
    _inserted, stored = await store.add_song(GID, window_key, song)
    return stored


# ---------------------------------------------------------------- 1. store


async def test_store_notice() -> None:
    print("\n[A] song_notices 登记 / 反查 / 清理")
    tmp = Path(tempfile.mkdtemp())
    store, _api = _setup(tmp)
    await store.init()

    song = await _add_song(store)
    check("入库后有 row_id", song.row_id is not None, str(song.row_id))

    await store.record_song_notice("msg-1", GID, WK, song.row_id)
    notice = await store.resolve_song_notice("msg-1", GID)
    check("能反查到映射", notice is not None)
    check("映射 row_id 正确", notice and int(notice["row_id"]) == song.row_id)
    check("映射 window_key 正确", notice and notice["window_key"] == WK)

    check("换个群查不到", await store.resolve_song_notice("msg-1", 9999) is None)
    check("不存在的 id 查不到", await store.resolve_song_notice("nope", GID) is None)
    check("空 id 查不到", await store.resolve_song_notice("", GID) is None)

    # 同一首歌的卡片那条也要能登记（各自一行）
    await store.record_song_notice("msg-2", GID, WK, song.row_id)
    check("同一首歌可登记多条消息", await store.resolve_song_notice("msg-2", GID) is not None)
    check("空 message_id 不登记", await store.resolve_song_notice("", GID) is None)

    got = await store.get_song_by_row(song.row_id)
    check("get_song_by_row 命中", got is not None and got.title == "原曲")
    check("get_song_by_row 不存在返回 None", await store.get_song_by_row(999999) is None)

    # prune_old：早于阈值的映射被清掉
    async with __import__("aiosqlite").connect(store.db_path) as db:
        await db.execute("UPDATE song_notices SET created_at = 1")
        await db.commit()
    await store.prune_old(1e12)
    check("prune_old 清掉过期映射", await store.resolve_song_notice("msg-1", GID) is None)


# ---------------------------------------------------------------- 2. service


async def test_match_unmatched_by_row() -> None:
    print("\n[B] service.match_unmatched_by_row")
    tmp = Path(tempfile.mkdtemp())
    store, api = _setup(tmp)
    await store.init()

    song = await _add_song(store, title="搜不到的歌")
    res = await service.match_unmatched_by_row(song.row_id, LINK)
    check("未匹配的歌能绑定", res.get("ok") is True, str(res))
    check("返回带 song 对象", isinstance(res.get("song"), Song))
    check("netease_id 写对了", res["song"].netease_id == "123456")
    check("调了 song_detail 拉详情", api.detail_calls == [["123456"]])
    check("歌名被详情覆盖", res["song"].title == "正确的歌", res["song"].title)
    refreshed = await store.get_song_by_row(song.row_id)
    check("落库了 netease_id", refreshed.netease_id == "123456")
    check("落库了 matched", refreshed.matched is True)

    # 已经匹配过的：拒绝，且不覆盖
    res2 = await service.match_unmatched_by_row(song.row_id, "https://music.163.com/song?id=999")
    check("已匹配的歌拒绝再指定", res2.get("ok") is False, str(res2))
    check("已匹配的歌没被改", (await store.get_song_by_row(song.row_id)).netease_id == "123456")

    # 总库里的歌：拒绝
    master = await _add_song(store, title="总库歌", window_key=MASTER_KEY)
    res3 = await service.match_unmatched_by_row(master.row_id, LINK)
    check("总库记录拒绝", res3.get("ok") is False and "总库" in res3["message"], str(res3))

    # 链接解析不出
    plain = await _add_song(store, title="第二首")
    res4 = await service.match_unmatched_by_row(plain.row_id, "这不是链接")
    check("解析不出 id 时报错", res4.get("ok") is False, str(res4))

    # 歌不在了
    res5 = await service.match_unmatched_by_row(999999, LINK)
    check("歌不存在时报错", res5.get("ok") is False, str(res5))


# ---------------------------------------------------------------- 3. 小工具


def test_message_id_of() -> None:
    print("\n[C] _message_id_of")
    check("dict 取值", _message_id_of({"message_id": 123}) == "123")
    check("对象属性取值", _message_id_of(types.SimpleNamespace(message_id=456)) == "456")
    check("None 返回空串", _message_id_of(None) == "")
    check("无字段返回空串", _message_id_of({}) == "")
    check("值为 None 返回空串", _message_id_of({"message_id": None}) == "")


# ---------------------------------------------------------------- 4. 规则


async def test_rule() -> None:
    print("\n[D] 规则 _looks_like_match_reply")
    tmp = Path(tempfile.mkdtemp())
    store, _api = _setup(tmp)
    await store.init()
    song = await _add_song(store)
    await store.record_song_notice("99", GID, WK, song.row_id)
    bot = FakeBot()

    check("没引用 → 不命中", await _looks_like_match_reply(bot, make_event(text=LINK, reply_to=None)) is False)
    check("引用但没网易云链接 → 不命中", await _looks_like_match_reply(bot, make_event(text="看看这首")) is False)
    check(
        "引用 + 链接但引用消息没登记过 → 不命中",
        await _looks_like_match_reply(bot, make_event(text=LINK, reply_to=1234)) is False,
    )
    check("引用 + 链接 + 已登记 → 命中", await _looks_like_match_reply(bot, make_event(text=LINK)) is True)
    check(
        "链接混在文字里也命中",
        await _looks_like_match_reply(bot, make_event(text=f"是这首 {LINK} 谢谢")) is True,
    )

    config_manager.config.playlist.reply_match = False
    check("开关关掉 → 不命中", await _looks_like_match_reply(bot, make_event(text=LINK)) is False)
    config_manager.config.playlist.reply_match = True


# ---------------------------------------------------------------- 5. handler


async def test_handler() -> None:
    print("\n[E] handle_match_reply")
    tmp = Path(tempfile.mkdtemp())
    store, _api = _setup(tmp)
    await store.init()
    bot = FakeBot()

    # ---- 无权限：既不是管理员也不是分享者
    song = await _add_song(store, title="搜不到的歌", sharer_id=42)
    await store.record_song_notice("99", GID, WK, song.row_id)
    await handle_match_reply(bot, make_event(user_id=999, role="member", text=LINK))
    check("无权限：没改库", (await store.get_song_by_row(song.row_id)).netease_id is None)
    check("无权限：发了提示", len(bot.sent) == 1, str(len(bot.sent)))
    check("无权限：提示是「只有管理员或…」", "管理员" in bot.sent[-1].extract_plain_text())

    # ---- 分享者本人：可以改
    bot.sent.clear()
    await handle_match_reply(bot, make_event(user_id=42, role="member", text=LINK))
    refreshed = await store.get_song_by_row(song.row_id)
    check("分享者：netease_id 落库", refreshed.netease_id == "123456", str(refreshed.netease_id))
    check("分享者：歌名被覆盖", refreshed.title == "正确的歌", refreshed.title)
    check("分享者：回执含「已把」", "已把" in bot.sent[-1].extract_plain_text(), bot.sent[-1].extract_plain_text())

    # ---- 已匹配过：给可读提示、不覆盖
    bot.sent.clear()
    await handle_match_reply(bot, make_event(user_id=42, role="member", text="https://music.163.com/song?id=777"))
    check("已匹配：没被覆盖", (await store.get_song_by_row(song.row_id)).netease_id == "123456")
    check("已匹配：提示可读", "已经匹配" in bot.sent[-1].extract_plain_text(), bot.sent[-1].extract_plain_text())

    # ---- 群管理员（非分享者）：也能改
    song2 = await _add_song(store, title="别人的歌", sharer_id=777)
    await store.record_song_notice("98", GID, WK, song2.row_id)
    bot.sent.clear()
    await handle_match_reply(bot, make_event(user_id=10086, role="admin", text=LINK, reply_to=98))
    check("群管：能改别人的歌", (await store.get_song_by_row(song2.row_id)).netease_id == "123456")

    # ---- 群主：也能改
    song3 = await _add_song(store, title="第三首", sharer_id=888)
    await store.record_song_notice("97", GID, WK, song3.row_id)
    await handle_match_reply(bot, make_event(user_id=10087, role="owner", text=LINK, reply_to=97))
    check("群主：能改", (await store.get_song_by_row(song3.row_id)).netease_id == "123456")

    # ---- 引用消息没登记（竞态/被清理）→ 静默返回
    bot.sent.clear()
    before = len(bot.sent)
    await handle_match_reply(bot, make_event(user_id=42, role="member", text=LINK, reply_to=12345))
    check("查不到映射时静默返回", len(bot.sent) == before)


async def main() -> None:
    await test_store_notice()
    test_message_id_of()
    await test_match_unmatched_by_row()
    await test_rule()
    await test_handler()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
