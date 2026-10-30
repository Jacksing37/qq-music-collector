"""猜歌小游戏回归：房间状态机 / 计分 / 答案不泄露 / 白名单与验证码。

不连网、不起 HTTP：只验证纯逻辑与数据库读写。真正的页面与接口靠手工过一遍
（``/guess`` 与 ``/music-admin/guess``）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

if not os.environ.get("MUSIC_DATA_DIR"):
    os.environ["MUSIC_DATA_DIR"] = tempfile.mkdtemp(prefix="music-test-")

sys.path.insert(0, ".")

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")  # noqa: E402

from src.plugins.music_collector import webui as W  # noqa: E402
from src.plugins.music_collector.config import AppConfig  # noqa: E402
from src.plugins.music_collector.game import rooms as R  # noqa: E402
from src.plugins.music_collector.game.auth import GameAuth  # noqa: E402
from src.plugins.music_collector.game.store import GameStore  # noqa: E402


class Cfg:
    """GameConfig 的替身：只放测试真正用到的字段。"""

    enabled = True
    whitelist_only = True
    allow_single = True
    max_players = 4
    room_idle_minutes = 60
    code_ttl_minutes = 10
    session_days = 30
    guess_slots = 3
    scores = [3, 2, 1]
    live_limit_per_min = 10
    archive_by_host = True


def _card(title: str, sharer: int, nick: str, platform: str = "netease") -> R.Card:
    return R.Card(
        uid=0, title=title, artists="歌手", platform=platform,
        song_id=f"{platform}-{title}", sharer_qq=sharer, sharer_name=nick,
    )


def _mgr() -> R.RoomManager:
    return R.RoomManager(lambda: Cfg())


# ---------------------------------------------------------------- 计分


def test_scoring_and_ranking():
    mgr = _mgr()
    room, _ = mgr.create(1, "甲", "multi")
    mgr.join(room, 2, "乙")
    room.add_card(_card("A", 111, "阿一"))
    room.add_card(_card("B", 222, "阿二"))
    uid_a, uid_b = room.cards[0].uid, room.cards[1].uid

    room.submit(1, {uid_a: [111, 222], uid_b: [222, 333]}, 3)
    room.submit(2, {uid_a: [222, 111], uid_b: [1, 222]}, 3)
    assert room.all_submitted()
    result = room.settle(3, [3, 2, 1])

    assert room.players[1].score == 6, room.players[1].score
    assert room.players[2].score == 4, room.players[2].score
    assert result["ranking"][0]["qq"] == 1
    assert [r["rank"] for r in result["ranking"]] == [1, 2]
    assert result["details"][0]["sharer"] == {"qq": 111, "nick": "阿一"}


def test_same_score_ties():
    mgr = _mgr()
    room, _ = mgr.create(1, "甲", "multi")
    mgr.join(room, 2, "乙")
    room.add_card(_card("A", 111, "阿一"))
    uid = room.cards[0].uid
    room.submit(1, {uid: [111]}, 3)
    room.submit(2, {uid: [111]}, 3)
    result = room.settle(3, [3, 2, 1])
    assert [r["rank"] for r in result["ranking"]] == [1, 1], result["ranking"]


def test_pick_validation():
    """重复项被去掉、超长被截断、不存在的题号被忽略。"""
    mgr = _mgr()
    room, _ = mgr.create(1, "甲", "multi")
    room.add_card(_card("A", 111, "阿一"))
    uid = room.cards[0].uid
    room.submit(1, {uid: [111, 111, 222, 333, 444], 9999: [111]}, 3)
    assert room.players[1].answers[uid] == [111, 222, 333]
    assert 9999 not in room.players[1].answers


# ---------------------------------------------------------------- 题目


def test_dedupe_same_and_cross_platform():
    mgr = _mgr()
    room, _ = mgr.create(1, "甲", "multi")
    assert room.add_card(_card("A", 111, "阿一"))
    # 同平台同 id
    assert not room.add_card(_card("A", 111, "阿一"))
    # 跨平台同曲：歌名+歌手一样，平台不同
    dup = _card("A", 222, "阿二", platform="qq")
    assert not room.add_card(dup), "跨平台同曲应被合并"
    # 同名但歌手不同 → 两道题
    other = R.Card(uid=0, title="A", artists="别人", platform="netease",
                   song_id="netease-x", sharer_qq=333, sharer_name="阿三")
    assert room.add_card(other)


def test_candidates_dedup():
    mgr = _mgr()
    room, _ = mgr.create(1, "甲", "multi")
    room.add_card(_card("A", 111, "阿一"))
    room.add_card(_card("B", 111, "阿一"))
    room.add_card(_card("C", 222, "阿二"))
    cands = room.candidates()
    assert [c["qq"] for c in cands] == [111, 222], cands


# ---------------------------------------------------------------- 房主 / 回收


def test_host_succession():
    mgr = _mgr()
    room, _ = mgr.create(1, "甲", "multi")
    mgr.join(room, 2, "乙")
    mgr.join(room, 3, "丙")
    assert room.host_qq == 1
    mgr.leave(room, 1)
    assert room.host_qq == 2, "房主退出应由最早加入的剩余玩家接任"
    mgr.transfer(room, 2, 3)
    assert room.host_qq == 3
    mgr.leave(room, 3)
    assert room.host_qq == 2


def test_room_reap():
    mgr = _mgr()
    room, _ = mgr.create(1, "甲", "multi")
    mgr.join(room, 2, "乙")
    room.last_active = time.time() - 61 * 60
    dead = mgr.reap()
    assert room.code in dead


def test_empty_room_reaped():
    mgr = _mgr()
    room, _ = mgr.create(1, "甲", "multi")
    mgr.leave(room, 1)
    assert room.code in mgr.reap()


def test_max_players():
    mgr = _mgr()
    room, _ = mgr.create(1, "甲", "multi")
    mgr.join(room, 2, "乙")
    mgr.join(room, 3, "丙")
    mgr.join(room, 4, "丁")
    try:
        mgr.join(room, 5, "戊")
    except R.RoomError:
        return
    raise AssertionError("超过上限应报错")


# ---------------------------------------------------------------- 答案不泄露


def test_state_hides_answer():
    mgr = _mgr()
    room, _ = mgr.create(1, "甲", "multi")
    room.add_card(_card("秘密歌名", 111, "阿一"))
    for phase in (R.PHASE_LOBBY, R.PHASE_SETUP, R.PHASE_PLAYING):
        room.phase = phase
        state = mgr.state(room, 1)
        blob = json.dumps(state, ensure_ascii=False)
        assert "result" not in state, f"{phase} 阶段不该带结算结果"
        for card in state["cards"]:
            assert "sharer" not in card and "sharer_qq" not in card, card
        assert "111" not in json.dumps(state["cards"], ensure_ascii=False)
        assert "阿一" not in blob or "阿一" in json.dumps(state["candidates"], ensure_ascii=False)

    room.submit(1, {room.cards[0].uid: [111]}, 3)
    state = mgr.state(room, 1)
    room.settle(3, [3, 2, 1])
    state = mgr.state(room, 1)
    assert state["result"]["details"][0]["sharer"]["qq"] == 111


# ---------------------------------------------------------------- 白名单 / 验证码


def test_store_users(tmp: Path):
    async def run():
        st = GameStore(tmp / "g.db")
        await st.init()
        assert await st.add_users([(111, "阿一", 0), (222, "阿二", 0)]) == 2
        # 重复导入不新增，但昵称会被刷新
        assert await st.add_users([(111, "阿一新", 0)]) == 0
        assert (await st.get_user(111))["nick"] == "阿一新"
        assert await st.count_users() == 2
        assert await st.set_enabled(222, False)
        assert await st.count_users() == 1
        assert await st.delete_users([111]) == 1
        assert await st.get_user(111) is None

    asyncio.run(run())


def test_codes_and_sessions(tmp: Path):
    async def run():
        st = GameStore(tmp / "c.db")
        await st.init()
        code = await st.new_code(111, 600)
        assert len(code) == 6 and code.isdigit()
        ok, why = await st.consume_code(111, "000000" if code != "000000" else "111111")
        assert not ok and why == "验证码不对"
        ok, _ = await st.consume_code(111, code)
        assert ok
        ok, why = await st.consume_code(111, code)
        assert not ok and "用过" in why

        # 过期
        expired = await st.new_code(111, -1)
        ok, why = await st.consume_code(111, expired)
        assert not ok and "过期" in why

        # 错满次数作废
        real = await st.new_code(222, 600)
        wrong = "000000" if real != "000000" else "111111"
        for _ in range(5):
            await st.consume_code(222, wrong)
        ok, why = await st.consume_code(222, real)
        assert not ok and "次数过多" in why

        token = await st.new_session(111, "阿一", 30)
        session = await st.get_session(token, 30)
        assert session and session["qq"] == 111
        assert await st.get_session(token, 0) is None, "0 天有效期应立刻失效"
        token2 = await st.new_session(111, "阿一", 30)
        await st.drop_session(token2)
        assert await st.get_session(token2, 30) is None

    asyncio.run(run())


def test_auth_whitelist(tmp: Path):
    async def run():
        st = GameStore(tmp / "a.db")
        await st.init()
        await st.add_users([(111, "阿一", 0)])
        auth = GameAuth(st, lambda: Cfg())
        ok, _ = await auth.check_allowed(111)
        assert ok
        ok, why = await auth.check_allowed(999)
        assert not ok and "名单" in why
        await st.set_enabled(111, False)
        ok, why = await auth.check_allowed(111)
        assert not ok and "停用" in why
        # 没有 bot 在线时不能假称"已发送"
        await st.set_enabled(111, True)
        ok, why = await auth.request_code(111)
        assert not ok and "不在线" in why

    asyncio.run(run())


def test_dm_falls_back_to_group_session(tmp: Path):
    """好友私聊发不出去时，要用群临时会话再试一次（OneBot 的 group_id 参数）。"""
    import src.plugins.music_collector.game.auth as A

    async def run():
        st = GameStore(tmp / "dm.db")
        await st.init()
        await st.add_users([(111, "阿一", 999001)])
        auth = GameAuth(st, lambda: Cfg())
        calls: list[dict] = []

        class Bot:
            def __init__(self, fail_all: bool = False) -> None:
                self.fail_all = fail_all

            async def send_private_msg(self, **kw):
                calls.append(kw)
                if self.fail_all or len(calls) == 1:
                    raise RuntimeError("not friend")
                return {"message_id": 1}

        orig = A._pick_bot
        try:
            A._pick_bot = lambda: Bot()
            ok, msg = await auth.request_code(111)
            assert ok, msg
            assert len(calls) == 2 and calls[1].get("group_id") == 999001, calls

            calls.clear()
            auth._last_code_at.clear()
            A._pick_bot = lambda: Bot(fail_all=True)
            ok, msg = await auth.request_code(111)
            assert not ok and "临时会话" in msg, msg
        finally:
            A._pick_bot = orig

    asyncio.run(run())


def test_parse_user_text():
    from src.plugins.music_collector.game.router import _parse_user_text

    rows = _parse_user_text("123456789 小明\n987654321,阿二\n# 注释\nabc\n10001 主号")
    assert rows == [(123456789, "小明"), (987654321, "阿二"), (10001, "主号")], rows
    # 重复号码只留一条
    assert _parse_user_text("123456789\n123456789 重复") == [(123456789, "")]
    assert _parse_user_text("") == []


def test_list_windows(tmp: Path):
    """选题库列表要读得到 songs 表（曾经把 GameStore 和 Store 搞混，直接 500）。"""
    import sqlite3

    from src.plugins.music_collector.config import DB_PATH
    from src.plugins.music_collector.game import bridge

    async def run():
        store = GameStore(DB_PATH)
        await store.init()
        # songs 表建在收集主库的 schema 里，这里按最小列建一份即可
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS songs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " group_id INTEGER, window_key TEXT, platform TEXT, song_id TEXT,"
                " title TEXT, artists TEXT, album TEXT, cover TEXT, url TEXT,"
                " duration INTEGER, sharer_id INTEGER, sharer_name TEXT, created_at REAL,"
                " netease_id TEXT, matched INTEGER DEFAULT 0, sort_order INTEGER DEFAULT 0,"
                " src_window TEXT DEFAULT '')"
            )
            conn.execute(
                "INSERT INTO songs (group_id, window_key, platform, song_id, title, artists,"
                " sharer_id, sharer_name, created_at) VALUES (900001,'W20260101-0000',"
                "'netease','1','晴天','周杰伦',111,'阿一',?)",
                (time.time(),),
            )
            conn.commit()
        rows = await bridge.list_windows(store)
        assert any(r["group_id"] == 900001 and r["count"] == 1 for r in rows), rows

    asyncio.run(run())


# ---------------------------------------------------------------- 配置 / schema


def test_dispatch_flow(tmp: Path):
    """走一遍 _dispatch 的真实分支（start/submit 曾经因为漏 import 一个常量直接 500）。"""
    from src.plugins.music_collector.game import router as RT

    async def run():
        mgr = _mgr()
        RT.manager = mgr
        RT.store = GameStore(tmp / "d.db")
        await RT.store.init()
        RT.auth = GameAuth(RT.store, lambda: Cfg())
        room, _ = mgr.create(1, "甲", "multi")
        mgr.join(room, 2, "乙")
        room.add_card(_card("A", 111, "阿一"))
        room.source = {"kind": "live"}
        uid = room.cards[0].uid

        msg = await RT._dispatch(room, 1, True, "start", {})
        assert room.phase == R.PHASE_PLAYING, msg
        await RT._dispatch(room, 1, True, "submit", {"answers": {str(uid): [111]}})
        assert room.phase == R.PHASE_PLAYING, "只有一个人交了，不该开牌"
        await RT._dispatch(room, 2, False, "submit", {"answers": {str(uid): [222]}})
        assert room.phase == R.PHASE_SETTLED
        assert room.players[1].score == 3 and room.players[2].score == 0

        # 权限：非房主不能开始 / 不能强制结算
        try:
            await RT._dispatch(room, 2, False, "start", {})
        except R.RoomError:
            pass
        else:
            raise AssertionError("非房主不该能开始游戏")

        # 未知动作
        try:
            await RT._dispatch(room, 1, True, "nope", {})
        except R.RoomError:
            pass
        else:
            raise AssertionError("未知动作应报错")

    asyncio.run(run())


def test_config_defaults():
    cfg = AppConfig().game
    assert cfg.enabled and cfg.whitelist_only and cfg.guess_slots == 3
    assert cfg.scores == [3, 2, 1]
    assert cfg.room_idle_minutes == 60


def test_schema_has_game_section():
    keys = [f["key"] for s in W.SCHEMA for f in s["fields"]]
    for key in ("game.enabled", "game.room_idle_minutes", "game.scores",
                "game.guess_slots", "game.archive_by_host"):
        assert key in keys, f"schema 缺少 {key}"
    assert "game.enabled" not in [s["key"] for s in W.BOT_SCHEMA]
    # 整段是进程级配置，网页「按群」视图要把它灰掉
    assert W.KEY_INDEX["game.enabled"]["global_only"] is True
    assert W.KEY_INDEX["game.scores"]["type"] == "intlist"


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp(prefix="guess-test-"))
    test_scoring_and_ranking()
    test_same_score_ties()
    test_pick_validation()
    test_dedupe_same_and_cross_platform()
    test_candidates_dedup()
    test_host_succession()
    test_room_reap()
    test_empty_room_reaped()
    test_max_players()
    test_state_hides_answer()
    test_store_users(tmp)
    test_codes_and_sessions(tmp)
    test_auth_whitelist(tmp)
    test_dm_falls_back_to_group_session(tmp)
    test_parse_user_text()
    test_list_windows(tmp)
    test_dispatch_flow(tmp)
    test_config_defaults()
    test_schema_has_game_section()
    print("guess tests OK")
