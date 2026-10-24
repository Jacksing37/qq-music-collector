"""主动搭话（banter）：勿扰时段解析、随机排期、素材筛选、端到端发送。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_banter.py

**绝不碰真实 data/**：数据库用临时文件；``service.store`` 用完即还原。
机器人对象是假的，``llm_bot.chat`` 也打成桩——这个测试不该花一分钱、也不该联网。
"""

import asyncio
import sys
import tempfile
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
    section("1. banter_minutes / banter_quiet 解析")
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
    cfg.banter_minutes = "30-60"
    cfg.banter_quiet = []
    now = datetime(2026, 10, 24, 10, 0)
    gaps = []
    for _ in range(200):
        when = bt.next_run_at(cfg, now)
        gaps.append((when - now).total_seconds() / 60.0)
    check("200 次全部落在 30~60 分钟", all(30 <= g <= 60 for g in gaps),
          f"min={min(gaps):.1f} max={max(gaps):.1f}")
    check("确实是随机的（不是每次都一样）", len({round(g, 1) for g in gaps}) > 100)

    cfg.banter_minutes = "10-10"
    check("写固定值 10-10 就是等距", all(
        abs((bt.next_run_at(cfg, now) - now).total_seconds() / 60.0 - 10) < 1e-6
        for _ in range(5)))

    cfg.banter_minutes = "30-60"
    cfg.banter_quiet = ["07:00-09:00"]
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
    fake = FakeBot()
    cfg = config_manager.config.llm
    saved = (cfg.banter, cfg.banter_groups, cfg.banter_quiet,
             cfg.banter_daily_max, cfg.banter_fresh_minutes, cfg.enabled)

    async def fake_chat(group_id, user_id, nick, text, probe, **kw):
        fake.calls = getattr(fake, "calls", [])
        fake.calls.append({"gid": group_id, "uid": user_id, "nick": nick,
                           "text": text, "tools": probe.tools,
                           "cooldown": probe.cooldown,
                           "history_chars": probe.history_chars,
                           "context": kw.get("context")})
        return "是啊，挺晒的，记得带伞", ""

    try:
        service.store = store
        bt.Bot = FakeBot               # type: ignore[assignment]
        bt.nonebot.get_bot = lambda: fake  # type: ignore[assignment]
        bt.llm_bot.chat = fake_chat    # type: ignore[assignment]
        bt.reset_quota()

        # ---- 关着开关：一声不响 ----
        cfg.enabled = True
        cfg.banter = False
        cfg.banter_quiet = []
        res = asyncio.run(bt.run_banter())
        check("开关关着 → off", res["status"] == "off", f"{res}")
        check("开关关着不会发消息", fake.sent == [])

        # ---- 兜底：最近的太短 ----
        cfg.banter = True
        cfg.banter_groups = [GID]
        asyncio.run(store.append_chat(GID, 42, "小明", "user", "嗯", kind="listen"))
        res = asyncio.run(bt.run_banter())
        check("最近一条太短 → 不发", res["sent"] == 0, f"{res}")

        # ---- 正常：接住最近一句 ----
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

        # ---- 每日上限 ----
        cfg.banter_daily_max = 1
        bt.reset_quota()
        asyncio.run(bt.run_banter())
        before = len(fake.sent)
        res = asyncio.run(bt.run_banter())
        check("达到每日上限后不再发", res["sent"] == 0 and len(fake.sent) == before, f"{res}")

        # ---- 消息太旧 ----
        cfg.banter_daily_max = 0
        bt.reset_quota()
        store2 = _setup_store(tmp / "old")
        service.store = store2
        asyncio.run(store2.append_chat(
            GID, 44, "小刚", "user", "两小时前的闲聊内容", kind="listen",
            created_at=datetime.now().timestamp() - 7200,
        ))
        cfg.banter_fresh_minutes = 20
        res = asyncio.run(bt.run_banter())
        check("两小时前的消息不接", res["sent"] == 0, f"{res}")

        # ---- 勿扰时段 ----
        cfg.banter_quiet = ["00:00-23:59"]
        res = asyncio.run(bt.run_banter())
        check("当前时刻落在勿扰段 → quiet", res["status"] == "quiet", f"{res}")

        # ---- 模型失败：不写库、不发消息 ----
        cfg.banter_quiet = []
        bt.reset_quota()
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
        (cfg.banter, cfg.banter_groups, cfg.banter_quiet,
         cfg.banter_daily_max, cfg.banter_fresh_minutes, cfg.enabled) = saved


def test_store_latest(tmp: Path) -> None:
    section("6. store.latest_chat")
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


def test_defaults() -> None:
    section("7. 配置默认值（改默认值时要同步改这里）")
    cfg = LLMConfig()
    check("主动搭话默认关（吵不吵由用户决定）", cfg.banter is False)
    check("默认间隔 30-60", cfg.banter_minutes == "30-60")
    check("默认勿扰只有一段且是夜间", cfg.banter_quiet == ["00:00-08:00"])
    check("默认不限群", cfg.banter_groups == [])
    check("默认不限次数", cfg.banter_daily_max == 0)
    check("默认 20 分钟内的才接", cfg.banter_fresh_minutes == 20.0)

    check("示例配置里也有这几项", _example_has("banter_minutes"))
    check("示例配置写了多段勿扰的写法", _example_has("banter_quiet"))


def _example_has(key: str) -> bool:
    example = ROOT / "config.example.yaml"
    if not example.exists():
        return False
    return key in example.read_text(encoding="utf-8")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="banter_"))
    test_parse()
    test_quiet_end()
    test_next_run()
    test_worth_reply()
    test_run(tmp)
    test_store_latest(tmp)
    test_defaults()
    print(f"\n通过 {PASSED} 项，失败 {FAILED} 项")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
