"""主动搭话（banter）：事件驱动的门控、勿扰时段、素材筛选、发送链路。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_banter.py

**绝不碰真实 data/**：数据库用临时文件；``service.store`` 用完即还原。
机器人对象是假的，``llm_bot.chat`` 也打成桩——这个测试不该花一分钱、也不该联网。

⚠️ 2026-11-05 起主动搭话是**事件驱动**的（``tick`` 每条消息调一次），
不再有 ``run_banter`` / 随机排期，所以这里也没有「下次几点」的测试。
"""

import asyncio
import sys
import tempfile
import time
from datetime import datetime
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
GID = 9101


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


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        #: >0 时「第 N 条开始发失败」，用来模拟多段回复半路掉线
        self.fail_at: int = 0

    async def send_group_msg(self, *, group_id: int, message) -> dict:
        if self.fail_at and len(self.sent) + 1 >= self.fail_at:
            raise RuntimeError("boom")
        self.sent.append((int(group_id), str(message)))
        return {"message_id": len(self.sent)}


# ---------------------------------------------------------------- 1. 时段解析


def test_span_parse() -> None:
    section("1. 勿扰时段解析")
    check("普通区间", bt.parse_span("00:00-08:00") == (0, 480))
    check("跨午夜", bt.parse_span("23:00-08:00") == (1380, 480))
    check("单数字小时", bt.parse_span("9:30-18:05") == (570, 1085))
    check("波浪号也认", bt.parse_span("23:00～08:00") == (1380, 480))
    check("起止相同当笔误", bt.parse_span("08:00-08:00") is None)
    check("小时越界", bt.parse_span("24:00-08:00") is None)
    check("分钟越界", bt.parse_span("00:60-08:00") is None)
    check("乱写不抛异常", bt.parse_span("晚上") is None and bt.parse_span("") is None)

    parsed = bt.parse_quiet(["00:00-08:00", "乱写", "12:00-14:00"])
    check("多段解析，坏的那段丢掉", parsed == [(0, 480), (720, 840)], str(parsed))
    check("空列表安全", bt.parse_quiet(None) == [] and bt.parse_quiet([]) == [])


def test_quiet_end() -> None:
    section("2. 勿扰段结束时刻（含跨午夜）")
    day = datetime(2026, 11, 5, 3, 0)
    spans = bt.parse_quiet(["00:00-08:00"])
    end = bt.quiet_end(day, spans)
    check("段内返回段尾", end is not None and end.hour == 8 and end.minute == 0, str(end))
    check("段外返回 None", bt.quiet_end(datetime(2026, 11, 5, 9, 0), spans) is None)

    cross = bt.parse_quiet(["23:00-08:00"])
    late = bt.quiet_end(datetime(2026, 11, 5, 23, 30), cross)
    check("跨午夜：已过 23:00 → 结束在明天 08:00",
          late is not None and late.day == 6 and late.hour == 8, str(late))
    early = bt.quiet_end(datetime(2026, 11, 5, 7, 0), cross)
    check("跨午夜：08:00 前 → 结束在今天 08:00",
          early is not None and early.day == 5 and early.hour == 8, str(early))
    check("跨午夜：中午不算勿扰", bt.quiet_end(datetime(2026, 11, 5, 12, 0), cross) is None)


# ---------------------------------------------------------------- 3. 素材筛选


def test_worth_reply() -> None:
    section("3. 哪句话值得接")
    check("正常聊天", bt.worth_reply("今天天气不错，适合出门"))
    check("两字也算", bt.worth_reply("哈哈"))
    check("单字不接", not bt.worth_reply("嗯"))
    check("空不接", not bt.worth_reply("") and not bt.worth_reply("   "))
    check("纯标点不接", not bt.worth_reply("？？？") and not bt.worth_reply("!!!..."))
    check("命令不接", not bt.worth_reply("/music list"))
    check("井号不接", not bt.worth_reply("#话题"))
    check("纯表情符不接", not bt.worth_reply("😂😂😂"))


def test_format_batch() -> None:
    section("4. 背景拼装")
    tz = service.resolver.tz
    ts = datetime(2026, 11, 5, 12, 0, tzinfo=tz).timestamp()
    rows = [
        {"created_at": ts, "nick": "小明", "user_id": 11, "content": "第一行\n第二行"},
        {"created_at": ts, "nick": "", "user_id": 12, "content": "没昵称"},
    ]
    text = bt.format_batch(rows, tz)
    check("带时间戳", "[12:00]" in text, text)
    check("带昵称与号", "小明(11)" in text, text)
    check("换行压平（一条一行）", "第一行 第二行" in text, text)
    check("没昵称给默认名", "群友(12)" in text, text)
    check("空批次空字符串", bt.format_batch([], tz) == "")
    check("时间戳坏了不炸", "坏数据" in bt.format_batch(
        [{"created_at": "x", "nick": "a", "user_id": 1, "content": "坏数据"}], tz))


def test_in_scope() -> None:
    section("5. 生效群范围")
    cfg = LLMConfig()
    cfg.groups = []
    check("留空 = 所有群", bt.in_scope(cfg, 123))
    cfg.groups = [123]
    check("命中", bt.in_scope(cfg, 123))
    check("不在名单里就跳过", not bt.in_scope(cfg, 456))


# ---------------------------------------------------------------- 6. 配额与冷却


def test_quota() -> None:
    section("6. 每日配额")
    bt.reset_quota()
    day = "2026-11-05"
    check("0 = 不限", bt._quota_left(GID, day, 0))
    check("有额度", bt._quota_left(GID, day, 2))
    bt._bump(GID, day)
    check("用掉一次后还剩", bt._quota_left(GID, day, 2))
    bt._bump(GID, day)
    check("用满就没了", not bt._quota_left(GID, day, 2))
    check("别的群不受影响", bt._quota_left(GID + 1, day, 2))
    bt._bump(GID + 1, "2026-11-04")
    check("跨天自动清掉非当天的账", all(k[1] == "2026-11-04" for k in bt._DAILY), str(bt._DAILY))
    bt.reset_quota()
    check("reset 清空", bt._DAILY == {})


def test_cooldown() -> None:
    section("7. 最小间隔 / 被动回复后安静")
    cfg = config_manager.config.llm
    saved = (cfg.speak_proactive, cfg.speak_min_gap_minutes)
    bt.reset_schedule()
    try:
        cfg.speak_proactive = True
        cfg.speak_min_gap_minutes = 20

        check("一开始没有冷却", not bt.waiting(GID))
        check("没冷却时 next_gate_at 是 None", bt.next_gate_at() is None)

        bt.note_reply(GID)
        check("被动回完就进入冷却", bt.waiting(GID) is True)
        gate = bt.next_gate_at()
        gap = ((gate.timestamp() - time.time()) / 60) if gate else -1
        check("冷却就是 min_gap（20 分钟）", 19 <= gap <= 21, f"{gap:.1f} 分钟")
        check("只影响这个群", not bt.waiting(GID + 1))
        check("到期后自动放行", not bt.waiting(GID, now=time.time() + 3600))

        bt.reset_schedule()
        cfg.speak_proactive = False
        bt.note_reply(GID)
        check("总开关关着 → 不设冷却", not bt.waiting(GID))

        cfg.speak_proactive = True
        bt.note_reply(0)
        check("群号 0 安静返回", not bt._NEXT_AT)
    finally:
        bt.reset_schedule()
        (cfg.speak_proactive, cfg.speak_min_gap_minutes) = saved


# ---------------------------------------------------------------- 8. tick 门控


def _setup_store(tmp: Path, name: str = "banter.db") -> Store:
    tmp.mkdir(parents=True, exist_ok=True)
    store = Store(tmp / name)
    asyncio.run(store.init())
    return store


class _Env:
    """临时把 service.store / 模型 / 概率门换成假的，退出时全部还原。"""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.store = _setup_store(tmp)
        self.fake = FakeBot()
        self.calls: list[dict] = []
        self.reply = "是啊，挺晒的，记得带伞"

    def __enter__(self):
        self.old_store = service.store
        self.old_chat = bt.llm_bot.chat
        self.old_decide = bt.speak_mod.decide_speak
        cfg = config_manager.config.llm
        app_cfg = config_manager.config
        self.saved_cfg = (cfg.speak_proactive, cfg.speak_enabled, cfg.enabled, cfg.groups,
                          cfg.speak_quiet, cfg.speak_daily_max, cfg.speak_min_gap_minutes)
        self.saved_listen = app_cfg.memory.listen

        calls = self.calls

        async def fake_chat(group_id, user_id, nick, text, probe, **kw):
            calls.append({
                "gid": group_id, "uid": user_id, "nick": nick, "text": text,
                "tools": probe.tools, "cooldown": probe.cooldown,
                "max_reply_chars": probe.max_reply_chars,
                "background": kw.get("background") or "",
            })
            return self.reply, ""

        service.store = self.store
        bt.llm_bot.chat = fake_chat  # type: ignore[assignment]
        bt.speak_mod.decide_speak = (  # type: ignore[assignment]
            lambda gid, kind, text="", nick="", **k: (True, 0.6)
        )
        cfg.speak_proactive = True
        cfg.speak_enabled = True
        cfg.enabled = True
        cfg.groups = [GID]
        cfg.speak_quiet = []
        cfg.speak_daily_max = 0
        cfg.speak_min_gap_minutes = 20
        app_cfg.memory.listen = True
        bt.reset_quota()
        bt.reset_cursor()
        bt.reset_schedule()
        return self

    def __exit__(self, *exc):
        service.store = self.old_store
        bt.llm_bot.chat = self.old_chat  # type: ignore[assignment]
        bt.speak_mod.decide_speak = self.old_decide  # type: ignore[assignment]
        cfg = config_manager.config.llm
        (cfg.speak_proactive, cfg.speak_enabled, cfg.enabled, cfg.groups,
         cfg.speak_quiet, cfg.speak_daily_max, cfg.speak_min_gap_minutes) = self.saved_cfg
        config_manager.config.memory.listen = self.saved_listen
        bt.reset_quota()
        bt.reset_cursor()
        bt.reset_schedule()
        return False


def test_tick_gates(tmp: Path) -> None:
    section("8. tick 门控（每一道都要说得出原因）")
    with _Env(tmp / "gates") as env:
        cfg = config_manager.config.llm
        bot = env.fake

        cfg.speak_proactive = False
        res = asyncio.run(bt.tick(bot, GID, 11, "小明", "今天好热啊"))
        check("总开关关 → off", res["status"] == "off", str(res))
        cfg.speak_proactive = True

        cfg.speak_enabled = False
        res = asyncio.run(bt.tick(bot, GID, 11, "小明", "今天好热啊"))
        check("概率模型关 → off（没有概率门就是逢话必接，不给开）",
              res["status"] == "off", str(res))
        cfg.speak_enabled = True

        res = asyncio.run(bt.tick(bot, GID + 1, 11, "小明", "今天好热啊"))
        check("不在 llm.groups → no_group", res["status"] == "no_group", str(res))

        config_manager.config.memory.listen = False
        res = asyncio.run(bt.tick(bot, GID, 11, "小明", "今天好热啊"))
        check("没开旁听 → no_listen", res["status"] == "no_listen", str(res))
        config_manager.config.memory.listen = True

        cfg.speak_quiet = ["00:00-23:59"]
        res = asyncio.run(bt.tick(bot, GID, 11, "小明", "今天好热啊"))
        check("勿扰时段 → quiet", res["status"] == "quiet", str(res))
        cfg.speak_quiet = []

        cfg.speak_daily_max = 1
        bt._bump(GID, bt._today(datetime.now(service.resolver.tz)))
        res = asyncio.run(bt.tick(bot, GID, 11, "小明", "今天好热啊"))
        check("今天说够了 → quota", res["status"] == "quota", str(res))
        cfg.speak_daily_max = 0
        bt.reset_quota()

        bt.note_reply(GID)
        res = asyncio.run(bt.tick(bot, GID, 11, "小明", "今天好热啊"))
        check("刚被动回过话 → cooldown", res["status"] == "cooldown", str(res))
        bt.reset_schedule()

        for bad in ("嗯", "？？？", "/music list", ""):
            res = asyncio.run(bt.tick(bot, GID, 11, "小明", bad))
            check(f"「{bad}」不值得接 → short", res["status"] == "short", str(res))

        bt.speak_mod.decide_speak = lambda *a, **k: (False, 0.05)  # type: ignore[assignment]
        res = asyncio.run(bt.tick(bot, GID, 11, "小明", "今天好热啊，不想出门"))
        check("概率门拦下 → gate", res["status"] == "gate", str(res))
        check("拦下时带上概率", res.get("p") == 0.05, str(res))
        check("拦下时一条都没发", bot.sent == [], str(bot.sent))
        check("拦下时不调模型", env.calls == [], str(env.calls))


def test_tick_send(tmp: Path) -> None:
    section("9. tick 正常发送")
    with _Env(tmp / "send") as env:
        bot = env.fake
        store = env.store
        asyncio.run(store.append_chat(GID, 42, "小明", "user", "前面那句是背景", kind="listen"))

        res = asyncio.run(bt.tick(bot, GID, 43, "小红", "周末有人去看海吗，求搭伴"))
        check("发出去了 → ok", res["status"] == "ok", str(res))
        check("发到对的群", len(bot.sent) == 1 and bot.sent[0][0] == GID, str(bot.sent))
        check("发的是模型回复", "带伞" in bot.sent[0][1], str(bot.sent))

        call = env.calls[-1]
        check("接的是触发那句话", call["text"] == "周末有人去看海吗，求搭伴", str(call["text"]))
        check("工具关掉（不联网、省 schema）", call["tools"] is False, str(call))
        check("冷却清零（不占群友额度）", call["cooldown"] == 0, str(call))
        check("回复压到 120 字", call["max_reply_chars"] == bt.BANTER_REPLY_CHARS, str(call))
        check("带「你在主动接话」的说明", "主动" in call["background"], call["background"][:60])
        check("背景里有前一句话", "前面那句是背景" in call["background"],
              call["background"][:120])

        day = bt._today(datetime.now(service.resolver.tz))
        check("记账 +1", bt._DAILY.get((GID, day)) == 1, str(bt._DAILY))
        check("说完进入冷却", bt.waiting(GID) is True)

        res = asyncio.run(bt.tick(bot, GID, 44, "小刚", "我也想去"))
        check("冷却期内再来一条不再开口", res["status"] == "cooldown", str(res))
        check("确实只发了一条", len(bot.sent) == 1, str(bot.sent))

        bt.reset_schedule()
        env.reply = ""
        res = asyncio.run(bt.tick(bot, GID, 45, "小美", "明天还去吗"))
        check("模型没生成 → empty", res["status"] == "empty", str(res))
        check("没生成就不发消息", len(bot.sent) == 1, str(bot.sent))


def test_probe(tmp: Path) -> None:
    section("13. probe()：只读自检，不发送不记账")
    with _Env(tmp / "probe") as env:
        cfg = config_manager.config.llm
        res = bt.probe(GID, "自检", "今天好热啊，不想出门")
        check("过门时返回 ok", res["status"] == "ok", str(res))
        check("带上群号与当日已发数", res.get("group") == GID and res.get("sent_today") == 0, str(res))
        check("带上当前参数快照",
              res.get("min_gap") == 20 and res.get("daily_max") == 0, str(res))
        check("自检不发送", env.fake.sent == [], str(env.fake.sent))
        check("自检不调模型", env.calls == [], str(env.calls))
        check("自检不记账", bt._DAILY == {}, str(bt._DAILY))

        cfg.speak_quiet = ["00:00-23:59"]
        res = bt.probe(GID, "自检", "今天好热啊")
        check("勿扰时段能问出 quiet", res["status"] == "quiet", str(res))
        cfg.speak_quiet = []

        bt.note_reply(GID)
        res = bt.probe(GID, "自检", "今天好热啊")
        check("冷却中能问出 cooldown", res["status"] == "cooldown", str(res))
        check("并给出解禁时刻", res.get("cooling_until") is not None, str(res))
        bt.reset_schedule()
def test_defaults() -> None:
    section("10. 配置默认值（改默认值时要同步改这里）")
    cfg = LLMConfig()
    check("主动搭话默认关（吵不吵由用户决定）", cfg.speak_proactive is False)
    check("默认勿扰只有一段且是夜间", cfg.speak_quiet == ["00:00-08:00"])
    check("默认每天不限", cfg.speak_daily_max == 0)
    check("默认最小间隔 20 分钟", cfg.speak_min_gap_minutes == 20)
    check("默认一次看 40 条", cfg.speak_batch_max == 40)
    check("默认背景 6000 字", cfg.speak_batch_chars == 6000)
    check("轮询时代的旋钮删干净了（事件驱动用不上）",
          not hasattr(cfg, "speak_check_minutes") and not hasattr(cfg, "speak_fresh_minutes"))


def test_reload_trigger() -> None:
    section("11. 改配置要重建定时任务")
    from music_collector import webui as wui

    check("改整点窗口 → 要重排", wui.needs_job_reload({"window.daily.start": "09:00"}))
    check("改 speak 项 → 要重排", wui.needs_job_reload({"llm.speak_daily_max": 3}))
    check("改模型 → 不用重排", not wui.needs_job_reload({"llm.model": "x"}))
    check("没有改动 → 不用重排", not wui.needs_job_reload({}))


def test_store_recent(tmp: Path) -> None:
    section("12. 背景素材的取法（recent_chat）")
    store = _setup_store(tmp / "recent")
    asyncio.run(store.append_chat(GID, 1, "甲", "user", "第一句", kind="listen"))
    asyncio.run(store.append_chat(GID, 2, "乙", "user", "第二句", kind="listen"))
    rows = asyncio.run(store.recent_chat(GID, limit=10))
    check("拿到两条", len(rows) == 2, str(len(rows)))
    check("按时间正序（最旧在前）", rows[0]["content"] == "第一句", str(rows[0]))
    after = asyncio.run(store.recent_chat(GID, after_id=int(rows[0]["id"]), limit=10))
    check("after_id 只取之后的", len(after) == 1 and after[0]["content"] == "第二句", str(after))
    check("limit 生效", len(asyncio.run(store.recent_chat(GID, limit=1))) == 1)


def test_split_send(tmp: Path) -> None:
    section("14. 主动搭话也走拆条（2026-11-06 修：banter 曾绕过 replysplit）")
    with _Env(tmp / "split") as env:
        cfg = config_manager.config.llm
        bot = env.fake
        saved = (cfg.reply_split, cfg.reply_split_max, cfg.reply_split_interval, cfg.time_guard)
        try:
            cfg.reply_split = True
            cfg.reply_split_max = 10
            cfg.reply_split_interval = "0"  # 别在测试里真睡
            env.reply = "第一句话在这\n第二句话在那"

            res = asyncio.run(bt.tick(bot, GID, 51, "小明", "周末有人去看海吗，求搭伴"))
            check("拆成两条发出去", res["status"] == "ok" and len(bot.sent) == 2, str(bot.sent))
            check("第一条 = 模型第一行",
                  len(bot.sent) > 0 and bot.sent[0][1] == "第一句话在这", str(bot.sent))
            check("第二条 = 模型第二行",
                  len(bot.sent) > 1 and bot.sent[1][1] == "第二句话在那", str(bot.sent))
            check("主动搭话不留前导空格",
                  len(bot.sent) > 0 and not bot.sent[0][1].startswith(" "), repr(bot.sent[:1]))
            check("不 @ 任何人", all("[at:" not in t for _, t in bot.sent), str(bot.sent))

            # 关掉开关 → 与改造前逐字一致：一条、换行原样保留
            bt.reset_schedule()
            bot.sent.clear()
            cfg.reply_split = False
            res = asyncio.run(bt.tick(bot, GID, 52, "小红", "周末有人去看海吗，求搭伴"))
            check("reply_split 关 → 原样一条",
                  len(bot.sent) == 1 and bot.sent[0][1] == env.reply, str(bot.sent))

            # 半路发失败：第二段没送到 → 本轮不记账、不推进游标
            bt.reset_schedule()
            bot.sent.clear()
            cfg.reply_split = True
            day = bt._today(datetime.now(service.resolver.tz))
            before = bt._DAILY.get((GID, day), 0)
            bot.fail_at = 2
            res = asyncio.run(bt.tick(bot, GID, 53, "小刚", "周末有人去看海吗，求搭伴"))
            check("第二段失败 → empty", res["status"] == "empty", str(res))
            check("没全送到就不记账", bt._DAILY.get((GID, day), 0) == before, str(bt._DAILY))
            bot.fail_at = 0

            # 时间感兜底：白天冒出一句「今晚」同样要改写（与被动回复同一层）
            bt.reset_schedule()
            bot.sent.clear()
            cfg.reply_split = False
            seen: list[dict] = []
            real_fix = bt.timeguard_mod.fix

            async def fake_fix(text, now, *, complete=None, enabled=True):
                seen.append({"text": text, "enabled": enabled})
                return "改写过的一句话"

            bt.timeguard_mod.fix = fake_fix  # type: ignore[assignment]
            try:
                asyncio.run(bt.tick(bot, GID, 54, "小美", "周末有人去看海吗，求搭伴"))
            finally:
                bt.timeguard_mod.fix = real_fix  # type: ignore[assignment]
            check("主动搭话也过时间感兜底", len(seen) == 1, str(seen))
            check("发出去的是改写后的正文",
                  len(bot.sent) == 1 and bot.sent[0][1] == "改写过的一句话", str(bot.sent))
        finally:
            (cfg.reply_split, cfg.reply_split_max,
             cfg.reply_split_interval, cfg.time_guard) = saved


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="banter_"))
    test_span_parse()
    test_quiet_end()
    test_worth_reply()
    test_format_batch()
    test_in_scope()
    test_quota()
    test_cooldown()
    test_tick_gates(tmp)
    test_tick_send(tmp)
    test_split_send(tmp)
    test_probe(tmp)
    test_defaults()
    test_reload_trigger()
    test_store_recent(tmp)
    print(f"\n通过 {PASSED} 项，失败 {FAILED} 项")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
