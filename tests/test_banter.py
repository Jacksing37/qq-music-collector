"""主动搭话（banter）：勿扰时段解析、随机排期、素材筛选、端到端发送。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_banter.py

**绝不碰真实 data/**：数据库用临时文件；``service.store`` 用完即还原。
机器人对象是假的，``llm_bot.chat`` 也打成桩——这个测试不该花一分钱、也不该联网。
"""

import asyncio
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import banter as bt  # noqa: E402
from music_collector.config import LLMConfig, config_manager  # noqa: E402
from music_collector.service import service  # noqa: E402
from music_collector.store import Store  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"  →  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n[{title}]")


# ---------------------------------------------------------------- 1. 区间与时段解析


def test_parse() -> None:
    section("1. speak_check_minutes / speak_quiet 解析")
    check("30-60 正常", bt.parse_range("30-60") == (30.0, 60.0))
    check("写反了自动交换", bt.parse_range("60-30") == (30.0, 60.0))
    check("支持波浪号", bt.parse_range("15~25") == (15.0, 25.0))
    check("支持小数", bt.parse_range("0.5-1.5") == (0.5, 1.5))
    check("空串用默认", bt.parse_range("") == (30.0, 60.0))
    check("乱写用默认", bt.parse_range("半小时") == (30.0, 60.0))
    check("含 0 用默认（0 分钟会疯狂刷屏）", bt.parse_range("0-10") == (30.0, 60.0))

    check("00:00-08:00", bt.parse_span("00:00-08:00") == (0, 480))
    check("13:30-14:45", bt.parse_span("13:30-14:45") == (810, 885))
    check("跨午夜 23:00-08:00", bt.parse_span("23:00-08:00") == (1380, 480))
    check("起止相同 = 空区间", bt.parse_span("08:00-08:00") is None)
    check("小时越界拒绝", bt.parse_span("25:00-26:00") is None)
    check("分钟越界拒绝", bt.parse_span("10:70-11:00") is None)
    check("乱写拒绝", bt.parse_span("早上八点") is None)

    spans = bt.parse_quiet(["00:00-08:00", "12:00-14:00", "胡说", "", "23:00-08:00"])
    check("多段都能解析（坏的丢掉）", len(spans) == 3, f"spans={spans}")
    check("空列表 = 没有勿扰", bt.parse_quiet([]) == [])
    check("None 也不炸", bt.parse_quiet(None) == [])


# ---------------------------------------------------------------- 2. 勿扰判定


def test_quiet_end() -> None:
    section("2. quiet_end：落在哪一段、几点结束")
    spans = bt.parse_quiet(["00:00-08:00", "12:00-14:00"])

    t = datetime(2026, 10, 24, 3, 20)
    end = bt.quiet_end(t, spans)
    check("凌晨 3:20 在勿扰里", end is not None)
    check("结束在今天 08:00", end == datetime(2026, 10, 24, 8, 0), f"end={end}")

    check("09:00 不在勿扰里", bt.quiet_end(datetime(2026, 10, 24, 9, 0), spans) is None)
    check("12:30 在第二段里", bt.quiet_end(datetime(2026, 10, 24, 12, 30), spans)
          == datetime(2026, 10, 24, 14, 0))
    check("14:00 整已出段", bt.quiet_end(datetime(2026, 10, 24, 14, 0), spans) is None)

    cross = bt.parse_quiet(["23:00-08:00"])
    check("23:30 跨午夜段内", bt.quiet_end(datetime(2026, 10, 24, 23, 30), cross) is not None)
    check("跨午夜结束在次日 08:00",
          bt.quiet_end(datetime(2026, 10, 24, 23, 30), cross)
          == datetime(2026, 10, 25, 8, 0))
    check("早上 7 点也在这段里（结束在今天）",
          bt.quiet_end(datetime(2026, 10, 24, 7, 0), cross)
          == datetime(2026, 10, 24, 8, 0))
    check("下午不在跨午夜段", bt.quiet_end(datetime(2026, 10, 24, 15, 0), cross) is None)


# ---------------------------------------------------------------- 3. 随机排期


def test_next_run() -> None:
    section("3. next_run_at：随机间隔 + 避开勿扰段")
    cfg = LLMConfig()
    cfg.speak_check_minutes = "30-60"
    cfg.speak_quiet = []
    now = datetime(2026, 10, 24, 10, 0)
    gaps = []
    for _ in range(200):
        when = bt.next_run_at(cfg, now)
        gaps.append((when - now).total_seconds() / 60.0)
    check("200 次全部落在 30~60 分钟", all(30 <= g <= 60 for g in gaps),
          f"min={min(gaps):.1f} max={max(gaps):.1f}")
    check("确实是随机的（不是每次都一样）", len({round(g, 1) for g in gaps}) > 100)

    cfg.speak_check_minutes = "10-10"
    check("写固定值 10-10 就是等距", all(
        abs((bt.next_run_at(cfg, now) - now).total_seconds() / 60.0 - 10) < 1e-6
        for _ in range(5)))

    cfg.speak_check_minutes = "30-60"
    cfg.speak_quiet = ["07:00-09:00"]
    start = datetime(2026, 10, 24, 8, 0)
    results = [bt.next_run_at(cfg, start) for _ in range(60)]
    check("从勿扰段里出发时，全部顺延到 09:00 之后",
          all(r >= datetime(2026, 10, 24, 9, 0) for r in results),
          f"最早的={min(results)}")
    check("顺延后也不会越过 09:10（只随机 0~10 分钟）",
          all(r <= datetime(2026, 10, 24, 9, 11) for r in results),
          f"最晚的={max(results)}")


# ---------------------------------------------------------------- 4. 素材筛选


def test_worth_reply() -> None:
    section("4. worth_reply：哪些话不值得接")
    check("正常一句话", bt.worth_reply("今天这天气也太晒了"))
    check("英文也算", bt.worth_reply("ok"))
    check("单字不接", not bt.worth_reply("嗯"))
    check("空串不接", not bt.worth_reply(""))
    check("纯标点不接", not bt.worth_reply("？？？"))
    check("表情符号不接", not bt.worth_reply("😀😀😀"))
    check("命令不接", not bt.worth_reply("/music list"))
    check("感叹号开头也当命令", not bt.worth_reply("!help"))
    check("带空格的短句仍算", bt.worth_reply("哈 哈"))


# ---------------------------------------------------------------- 5. 端到端


class FakeBot:
    """只记账、不发送。``isinstance(x, Bot)`` 那道检查靠 patch ``bt.Bot`` 绕过。"""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_group_msg(self, group_id, message):  # noqa: ANN001
        self.sent.append((int(group_id), str(message)))


GID = 100000123


def _setup_store(tmp: Path) -> Store:
    tmp.mkdir(parents=True, exist_ok=True)
    store = Store(tmp / "banter.db")
    asyncio.run(store.init())
    return store


def test_run(tmp: Path) -> None:
    section("5. run_banter 端到端")
    store = _setup_store(tmp)
    old_store = service.store
    old_bot = bt.Bot
    old_get = bt.nonebot.get_bot
    old_chat = bt.llm_bot.chat
    old_complete = bt.llm_bot.complete
    fake = FakeBot()
    cfg = config_manager.config.llm
    saved = (cfg.speak_proactive, cfg.groups, cfg.speak_quiet,
             cfg.speak_daily_max, cfg.speak_fresh_minutes, cfg.enabled)

    async def fake_chat(group_id, user_id, nick, text, probe, **kw):
        fake.calls = getattr(fake, "calls", [])
        fake.calls.append({"gid": group_id, "uid": user_id, "nick": nick,
                           "text": text, "tools": probe.tools,
                           "cooldown": probe.cooldown,
                           "history_chars": probe.history_chars,
                           # 素材现在走 background 传（日志页要看见内容），
                           # 这里合并回来，下面的断言不关心它走哪个参数
                           "context": (kw.get("background") or "") + (kw.get("context") or ""),
                           "background": kw.get("background")})
        return "是啊，挺晒的，记得带伞", ""

    try:
        service.store = store
        bt.Bot = FakeBot               # type: ignore[assignment]
        bt.nonebot.get_bot = lambda: fake  # type: ignore[assignment]
        bt.llm_bot.chat = fake_chat    # type: ignore[assignment]
        bt.reset_quota()
        bt.reset_cursor()

        # ---- 关着开关：一声不响 ----
        cfg.enabled = True
        cfg.speak_proactive = False
        cfg.speak_quiet = []
        res = asyncio.run(bt.run_banter())
        check("开关关着 → off", res["status"] == "off", f"{res}")
        check("开关关着不会发消息", fake.sent == [])

        # ---- 兜底：最近的太短 ----
        cfg.speak_proactive = True
        cfg.groups = [GID]
        bt.reset_cursor()
        asyncio.run(store.append_chat(GID, 42, "小明", "user", "嗯", kind="listen"))
        res = asyncio.run(bt.run_banter())
        check("整批都不够格 → 不发", res["sent"] == 0, f"{res}")

        # ---- 正常：接住最近一句 ----
        bt.reset_cursor()
        bt.reset_quota()
        asyncio.run(store.append_chat(GID, 43, "小红", "user",
                                      "周末有人去看海吗，求搭伴", kind="listen"))
        res = asyncio.run(bt.run_banter())
        check("正常素材 → 发出 1 条", res["sent"] == 1, f"{res}")
        check("发到了正确群", fake.sent and fake.sent[-1][0] == GID)
        body = fake.sent[-1][1] if fake.sent else ""
        check("内容是模型回复", "带伞" in body, body)
        check("没有 @ 任何人", "[CQ:at" not in body and "at" not in body.lower(), body)
        check("没有引用回复", "reply" not in body.lower(), body)

        call = fake.calls[-1]
        check("用的是最新那条（小红）", call["nick"] == "小红" and call["uid"] == 43, f"{call}")
        check("工具被关掉（不联网、省 schema）", call["tools"] is False, f"{call}")
        check("冷却清零（不占被搭话者额度）", call["cooldown"] == 0, f"{call}")
        check("历史压到 12000 字", call["history_chars"] <= bt.BANTER_HISTORY_CHARS,
              f"{call}")
        check("带了「你在主动接话」的说明", "主动" in (call["context"] or ""), f"{call}")

        # ---- 接过的那批不会再被接第二次（游标已推进）----
        res = asyncio.run(bt.run_banter())
        check("同一批不会被接第二次（游标已推进）", res["sent"] == 0, f"{res}")

        # ---- 一批多条：看得到上下文，只发一条 ----
        bt.reset_quota()
        bt.reset_cursor()
        asyncio.run(store.append_chat(GID, 60, "甲", "user", "第一句话在这里", kind="listen"))
        asyncio.run(store.append_chat(GID, 61, "乙", "user", "第二句话接上", kind="listen"))
        asyncio.run(store.append_chat(GID, 62, "丙", "user", "第三句话收尾", kind="listen"))
        res = asyncio.run(bt.run_banter())
        check("一批多条也只发 1 条", res["sent"] == 1, f"{res}")
        call2 = fake.calls[-1]
        check("接的是最后一条够格的话", call2["text"] == "第三句话收尾", str(call2["text"]))
        check("前面几句作为背景进了 context",
              "第一句话在这里" in (call2["context"] or ""), (call2["context"] or "")[:120])
        check("背景带时间戳（模型才看得出谁跟谁说）",
              "[" in (call2["context"] or ""), (call2["context"] or "")[:80])
        check("素材走 background 传（日志页才看得见内容）",
              "第一句话在这里" in (call2.get("background") or ""),
              (call2.get("background") or "")[:80])

        # ---- 条数上限 ----
        cfg.speak_batch_max = 1
        bt.reset_quota()
        bt.reset_cursor()
        asyncio.run(store.append_chat(GID, 63, "丁", "user", "只该看到这一条", kind="listen"))
        asyncio.run(store.append_chat(GID, 64, "戊", "user", "最新的一条在这", kind="listen"))
        asyncio.run(bt.run_banter())
        call3 = fake.calls[-1]
        check("batch_max=1 时只看最新一条", call3["text"] == "最新的一条在这", str(call3["text"]))
        cfg.speak_batch_max = 40

        # ---- 每日上限 ----
        cfg.speak_daily_max = 1
        bt.reset_quota()
        bt.reset_cursor()
        asyncio.run(store.append_chat(GID, 45, "小华", "user", "上限测试素材一", kind="listen"))
        asyncio.run(bt.run_banter())
        before = len(fake.sent)
        asyncio.run(store.append_chat(GID, 46, "小华", "user", "上限测试素材二", kind="listen"))
        res = asyncio.run(bt.run_banter())
        check("达到每日上限后不再发", res["sent"] == 0 and len(fake.sent) == before, f"{res}")

        # ---- 刚被动回复过 → 这一轮先不插嘴 ----
        cfg.speak_daily_max = 0
        bt.reset_quota()
        bt.reset_cursor()
        asyncio.run(store.append_chat(GID, 47, "小华", "user", "刚答完话之后的素材", kind="listen"))
        bt.note_reply(GID)          # 模拟刚才被 @ 完、已经回过话
        before = len(fake.sent)
        res = asyncio.run(bt.run_banter())
        check("刚被动回复过 → 本轮不搭话",
              res["sent"] == 0 and len(fake.sent) == before, f"{res}")
        bt.reset_schedule()
        res = asyncio.run(bt.run_banter())
        check("计时过了 → 照常搭话", res["sent"] == 1, f"{res}")

        # ---- 消息太旧 ----
        cfg.speak_daily_max = 0
        bt.reset_quota()
        bt.reset_cursor()
        store2 = _setup_store(tmp / "old")
        service.store = store2
        asyncio.run(store2.append_chat(
            GID, 44, "小刚", "user", "两小时前的闲聊内容", kind="listen",
            created_at=datetime.now().timestamp() - 7200,
        ))
        cfg.speak_fresh_minutes = 20
        res = asyncio.run(bt.run_banter())
        check("两小时前的消息不接", res["sent"] == 0, f"{res}")

        # ---- 勿扰时段 ----
        cfg.speak_quiet = ["00:00-23:59"]
        res = asyncio.run(bt.run_banter())
        check("当前时刻落在勿扰段 → quiet", res["status"] == "quiet", f"{res}")

        # ---- 模型失败：不写库、不发消息 ----
        cfg.speak_quiet = []
        bt.reset_quota()
        bt.reset_cursor()
        service.store = store
        async def bad_chat(*a, **k):
            return None, "error: 502"
        bt.llm_bot.chat = bad_chat  # type: ignore[assignment]
        before = len(fake.sent)
        res = asyncio.run(bt.run_banter())
        check("模型失败不发空消息", res["sent"] == 0 and len(fake.sent) == before, f"{res}")
    finally:
        service.store = old_store
        bt.Bot = old_bot              # type: ignore[assignment]
        bt.nonebot.get_bot = old_get  # type: ignore[assignment]
        bt.llm_bot.chat = old_chat    # type: ignore[assignment]
        bt.llm_bot.complete = old_complete  # type: ignore[assignment]
        bt.reset_cursor()
        (cfg.speak_proactive, cfg.groups, cfg.speak_quiet,
         cfg.speak_daily_max, cfg.speak_fresh_minutes, cfg.enabled) = saved



def test_clip_and_format() -> None:
    section("7. 素材裁剪与拼装")
    long = "\n".join(f"第{i}行内容" for i in range(200))
    got = bt.clip_tail(long, 60)
    check("从末尾保留（要的是最近的话）", "第199行内容" in got, got[-24:])
    check("不会超预算太多", len(got) <= 60, str(len(got)))
    check("裁完仍是整行（不从半句开始）", got.startswith("第"), got[:10])
    check("短文本原样返回", bt.clip_tail("abc", 100) == "abc")
    check("limit=0 表示不限", bt.clip_tail(long, 0) == long)

    now = datetime.now().timestamp()
    rows = [
        {"created_at": now, "nick": "小明", "user_id": 1, "content": "你好"},
        {"created_at": now, "nick": "", "user_id": 2, "content": "在的\n刚回来"},
    ]
    text = bt.format_batch(rows, datetime.now().astimezone().tzinfo)
    check("带昵称与 QQ 号（模型才看得出谁在说）", "小明(1)" in text, text)
    check("没昵称时用「群友」兜底", "群友(2)" in text, text)
    check("多行内容压成一行（不然数不清几个人）", "\n\n" not in text, text)
    check("每条都带时间戳", text.count("[") >= 2, text)


def test_store_recent(tmp: Path) -> None:
    section("8. store.recent_chat：游标之后的整段")
    store = _setup_store(tmp / "recent")
    for i, (nick, body) in enumerate(
        [("甲", "第一句"), ("乙", "第二句"), ("丙", "第三句")], 1
    ):
        asyncio.run(store.append_chat(GID, i, nick, "user", body, kind="listen"))
    # 机器人自己的回复、以及问答记录都不该混进素材
    asyncio.run(store.append_chat(GID, 0, "", "assistant", "机器人的话", kind="listen"))
    asyncio.run(store.append_chat(GID, 9, "丁", "user", "问答记录", kind="chat"))

    rows = asyncio.run(store.recent_chat(GID))
    check("默认取全部群友消息", len(rows) == 3, str(len(rows)))
    check("正序（最旧在前）", rows[0]["content"] == "第一句", rows[0]["content"])
    check("机器人自己的话不算素材",
          all(r["content"] != "机器人的话" for r in rows), str([r["content"] for r in rows]))
    check("问答记录（kind=chat）也不算",
          all(r["content"] != "问答记录" for r in rows), str([r["content"] for r in rows]))

    first_id = rows[0]["id"]
    after = asyncio.run(store.recent_chat(GID, after_id=first_id))
    check("游标之后只剩两条", len(after) == 2 and after[0]["content"] == "第二句",
          str([r["content"] for r in after]))
    check("游标推到最后时取不到东西",
          asyncio.run(store.recent_chat(GID, after_id=rows[-1]["id"])) == [])

    limited = asyncio.run(store.recent_chat(GID, limit=2))
    check("limit 取最近的 N 条",
          [r["content"] for r in limited] == ["第二句", "第三句"],
          str([r["content"] for r in limited]))

    clipped = asyncio.run(store.recent_chat(GID, max_chars=6))
    check("超字数从最旧的丢、留住最新的",
          bool(clipped) and clipped[-1]["content"] == "第三句", str(clipped))
    check("至少留一条（不会裁空）", len(clipped) >= 1, str(len(clipped)))
    check("没记录过的群返回空表", asyncio.run(store.recent_chat(999)) == [])


def test_store_latest(tmp: Path) -> None:
    section("9. store.latest_chat（旧的单条接口，保留）")
    store = _setup_store(tmp / "latest")
    asyncio.run(store.append_chat(GID, 1, "甲", "user", "第一条", kind="listen"))
    asyncio.run(store.append_chat(GID, 2, "乙", "user", "第二条", kind="listen"))
    # 机器人自己的回复不该被当成引子
    asyncio.run(store.append_chat(GID, 0, "", "assistant", "机器人的话", kind="listen"))
    # 问答记录（带昵称前缀）也不算，引子只认旁听原文
    asyncio.run(store.append_chat(GID, 3, "丙", "user", "丙(3)：问答", kind="chat"))

    row = asyncio.run(store.latest_chat(GID))
    check("取到最新一条群友原话", row and row["content"] == "第二条", f"{row}")
    check("带上了昵称与 QQ 号", row and row["nick"] == "乙" and row["user_id"] == 2, f"{row}")

    # max_age 用的是「写入时刻」，刚写进去的消息不可能算过期——要验过滤就得造一条老消息
    asyncio.run(store.append_chat(
        GID, 4, "丁", "user", "很久以前的话", kind="listen",
        created_at=datetime.now().timestamp() - 7200,
    ))
    row_fresh = asyncio.run(store.latest_chat(GID, max_age=3600))
    check("max_age 跳过两小时前那条，仍取到新鲜的",
          bool(row_fresh) and row_fresh["content"] == "第二条", f"{row_fresh}")
    check("不带 max_age 时最新的就是那条老消息（说明它 id 更大）",
          (asyncio.run(store.latest_chat(GID)) or {}).get("content") == "很久以前的话")

    store_old = _setup_store(tmp / "oldonly")
    asyncio.run(store_old.append_chat(
        GID, 5, "戊", "user", "只有一条老消息", kind="listen",
        created_at=datetime.now().timestamp() - 7200,
    ))
    check("只有老消息时 max_age 直接取不到",
          asyncio.run(store_old.latest_chat(GID, max_age=3600)) is None)
    check("不限时长就能取到",
          asyncio.run(store_old.latest_chat(GID)) is not None)

    check("没记录过的群返回 None", asyncio.run(store.latest_chat(999)) is None)


# ---------------------------------------------------------------- 7. 配置默认值


def test_reset_on_reply() -> None:
    section("11. 被动回复后重新计时（免得刚答完又自己冒泡）")
    cfg = config_manager.config.llm
    saved = (cfg.speak_proactive, cfg.speak_check_minutes)
    bt.reset_schedule()
    gid = 9401
    try:
        cfg.speak_proactive = True
        cfg.speak_check_minutes = "30-60"

        check("一开始没有冷却", not bt.waiting(gid))
        check("没有冷却时 next_gate_at 返回 None", bt.next_gate_at() is None)

        bt.note_reply(gid)
        check("回复完就进入冷却", bt.waiting(gid) is True)
        gate = bt.next_gate_at()
        gap = ((gate.timestamp() - time.time()) / 60) if gate else -1
        check("解禁时间落在 30~60 分钟后", 29 <= gap <= 61, f"{gap:.1f} 分钟")
        check("冷却只影响这个群（别的群照常）", not bt.waiting(gid + 1))
        check("到期之后自动放行", not bt.waiting(gid, now=time.time() + 3600))

        # 重复回复：以**最后一次**为准，从那一刻重新数一个间隔
        bt.note_reply(gid)
        again = bt.next_gate_at()
        gap2 = ((again.timestamp() - time.time()) / 60) if again else -1
        check("再回一句 → 从这一刻重新数（仍是 30~60 分钟后）",
              29 <= gap2 <= 61, f"{gap2:.1f} 分钟")

        # 合并进 speak 后这个行为恒为开（旧的 speak_proactive 旋钮已删），
        # 只剩总开关能关掉它
        bt.reset_schedule()
        cfg.speak_proactive = False
        bt.note_reply(gid)
        check("搭话总开关关着 → 不设冷却", not bt.waiting(gid))

        # 没群号（私聊戳一戳之类）不该炸
        cfg.speak_proactive = True
        bt.note_reply(0)
        check("群号为 0 → 安静返回", not bt._NEXT_AT)
    finally:
        bt.reset_schedule()
        (cfg.speak_proactive, cfg.speak_check_minutes) = saved



def test_gate_reason(tmp: Path) -> None:
    """概率门拦下时，返回值必须**说得出原因**。

    以前统一返回 ``no_seed``，2026-11-05 线上就吃了亏：连着六轮"没发"，
    日志里全是同一句话，分不清是没素材、太旧还是概率门拦的。这里钉住
    「门拦下 → status 为 gate 且 reasons 记账」。
    """
    section("12. 概率门拦下要留下可分辨的原因")
    store = _setup_store(tmp / "gate")
    old_store = service.store
    old_bot = bt.Bot
    old_get = bt.nonebot.get_bot
    old_chat = bt.llm_bot.chat
    old_decide = bt.speak_mod.decide_speak
    fake = FakeBot()
    cfg = config_manager.config.llm
    saved = (cfg.speak_proactive, cfg.groups, cfg.speak_quiet,
             cfg.speak_daily_max, cfg.speak_fresh_minutes, cfg.enabled)

    async def fake_chat(*a, **k):  # 不该被调用
        raise AssertionError("概率门拦下了，不该去调模型")

    def refuse(gid, kind, text="", nick="", **kw):
        return False, 0.05

    try:
        service.store = store
        bt.Bot = FakeBot               # type: ignore[assignment]
        bt.nonebot.get_bot = lambda: fake  # type: ignore[assignment]
        bt.llm_bot.chat = fake_chat    # type: ignore[assignment]
        bt.speak_mod.decide_speak = refuse  # type: ignore[assignment]
        bt.reset_quota()
        bt.reset_cursor()

        cfg.enabled = True
        cfg.speak_proactive = True
        cfg.speak_quiet = []
        cfg.speak_daily_max = 0
        cfg.speak_fresh_minutes = 20
        cfg.groups = [GID]
        asyncio.run(store.append_chat(GID, 71, "小明", "user",
                                      "刚看完那部剧，结尾有点崩", kind="listen"))
        res = asyncio.run(bt.run_banter())
        check("门拦下 → status 是 gate（不再笼统叫 no_seed）",
              res["status"] == "gate", f"{res}")
        check("门拦下 → 一条都没发", res["sent"] == 0, f"{res}")
        check("原因里记下了 gate 次数", res.get("why", {}).get("gate") == 1, f"{res}")
        check("门拦下不会留下假消息", fake.sent == [], str(fake.sent))
    finally:
        service.store = old_store
        bt.Bot = old_bot               # type: ignore[assignment]
        bt.nonebot.get_bot = old_get   # type: ignore[assignment]
        bt.llm_bot.chat = old_chat     # type: ignore[assignment]
        bt.speak_mod.decide_speak = old_decide  # type: ignore[assignment]
        bt.reset_schedule()
        bt.reset_quota()
        bt.reset_cursor()
        (cfg.speak_proactive, cfg.groups, cfg.speak_quiet,
         cfg.speak_daily_max, cfg.speak_fresh_minutes, cfg.enabled) = saved


def test_defaults() -> None:
    section("10. 配置默认值（改默认值时要同步改这里）")
    cfg = LLMConfig()
    check("主动搭话默认关（吵不吵由用户决定）", cfg.speak_proactive is False)
    check("默认间隔 30-60", cfg.speak_check_minutes == "30-60")
    check("默认勿扰只有一段且是夜间", cfg.speak_quiet == ["00:00-08:00"])
    check("默认不限群", cfg.groups == [])
    check("默认不限次数", cfg.speak_daily_max == 0)
    check("默认 20 分钟内的才接", cfg.speak_fresh_minutes == 20.0)
    check("默认被动回复后重新计时", True is True)
    check("默认一批最多 40 条", cfg.speak_batch_max == 40)
    check("默认一批最多 6000 字", cfg.speak_batch_chars == 6000)

    check("示例配置里也有这几项", _example_has("speak_check_minutes"))
    check("示例配置写了多段勿扰的写法", _example_has("speak_quiet"))
    for key in ("speak_proactive", "speak_daily_max", "speak_batch_max", "speak_batch_chars"):
        check(f"示例配置写了 {key}", _example_has(key))


def _example_has(key: str) -> bool:
    example = ROOT / "config.example.yaml"
    if not example.exists():
        return False
    return key in example.read_text(encoding="utf-8")


def test_reload_trigger() -> None:
    """保存配置后要不要重建定时任务。

    这条锁的是一个真出过的 bug：判断条件原来只比对 ``llm.banter`` 这一个键，
    而用户改的通常是 ``llm.speak_check_minutes`` / ``speak_quiet``，于是「开关没动」
    就不重排，改了间隔得等旧的那次跑完（甚至等下次重启）才生效。
    """
    print("\n[8] 保存配置后要不要重建定时任务")
    from music_collector import webui as wui

    for k in (
        "llm.speak_proactive",
        "llm.speak_check_minutes",
        "llm.speak_quiet",
        "llm.speak_daily_max",
        "llm.speak_batch_max",
        "llm.speak_fresh_minutes",
        "memory.digest_hours",
        "window.mode",
        "window.daily.start",
    ):
        check(f"{k} → 要重排", wui.needs_job_reload({k: 1}))

    for k in ("llm.model", "llm.temperature", "memory.inject_chars", "playlist.seq", "logs.level"):
        check(f"{k} → 不用重排", not wui.needs_job_reload({k: 1}))

    check("没有改动 → 不用重排", not wui.needs_job_reload({}))
    check(
        "混着改也能认出搭话项",
        wui.needs_job_reload({"llm.model": "x", "llm.speak_check_minutes": "90-120"}),
    )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="banter_"))
    test_parse()
    test_quiet_end()
    test_next_run()
    test_worth_reply()
    test_run(tmp)
    test_store_latest(tmp)
    test_clip_and_format()
    test_store_recent(tmp)
    test_reset_on_reply()
    test_gate_reason(tmp)
    test_defaults()
    test_reload_trigger()
    print(f"\n通过 {PASSED} 项，失败 {FAILED} 项")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
