"""验证 speak 的说话时机模型：概率单调性、@ 恒真、判定先于记账、衰减、窗口只用一次。

关键约束（对应设计文档 `docs/聊天体验拼装设计.md` §3.2 / §5 P2 / §6）：

- **先判定、后记账**：第一条冷群消息的 p 必须就是 `speak_p_max`——如果先记账，
  它会先把自己的热度抬起来、把概率压下去，而第一条恰恰是最该捧场的那条
- **自己发言后 engage 下降**：`×= speak_engage_self_damp`。让它上升就是正反馈，
  冷群里会演变成刷屏
- `speak_enabled=False` / `speak_dry_run=True` 都必须恒放行（关掉就是旧行为）
- 「紧跟它发言」的窗口**只用一次**，补量随 engage 存量递减（防无限对聊）

时间一律靠替换 `speak._now()` 推进，**不真 sleep**；随机数靠 `decide_speak(roll=…)`
或替换 `speak._rand()` 固定，**不统计频率**（那种写法又慢又飘）。
"""

import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")  # 触发插件加载（__init__ 会 require apscheduler）

from music_collector import speak  # noqa: E402
from music_collector.config import LLMConfig  # noqa: E402


# ---------------------------------------------------------------- 测试脚手架


class Clock:
    """可手动推进的时钟，用来替换 ``speak._now()``。

    真的 ``sleep`` 会让回归慢几十秒，而且必然偶尔飘一两个浮点边界。
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


@contextmanager
def sandbox(clock: Clock, **over):
    """把 speak 的配置源与时间源换成可控版本，并在结束后还原。

    ``speak`` 只从这里两处取外部输入（``_cfg`` / ``_now``），所以替换这两个
    入口就等于把整个模块放进沙箱，不需要打桩 nonebot 或 service。
    """
    cfg = LLMConfig(**over)
    old_cfg, old_now = speak._cfg, speak._now
    speak._cfg = lambda: cfg          # type: ignore[assignment]
    speak._now = clock               # type: ignore[assignment]
    speak.reset()
    try:
        yield cfg
    finally:
        speak._cfg = old_cfg         # type: ignore[assignment]
        speak._now = old_now         # type: ignore[assignment]
        speak.reset()


def state(gid: int) -> dict:
    return speak.stats()["groups"][gid]


# ---------------------------------------------------------------- 1. 冷群高 / 刷屏低


def test_cold_group_is_likely_and_busy_group_is_quiet() -> None:
    clock = Clock()
    with sandbox(clock, speak_enabled=True) as cfg:
        gid = 1001
        # 一条消息都没有 = 真死群 → 概率就是天花板 p_max
        seq = [speak.probability(gid, mentioned=False, text="", nick="")]
        assert abs(seq[0] - cfg.speak_p_max) < 1e-9, seq[0]

        # 连续灌消息：热度单调上升、概率单调下降
        for _ in range(40):
            speak.note_group_message(gid)
            seq.append(speak.probability(gid, mentioned=False, text="", nick=""))
        assert all(b < a for a, b in zip(seq, seq[1:])), "概率必须随热度单调下降"

        # 继续刷到饱和。到地板附近时 (1-fast)*(1-slow) 已经小于浮点精度，
        # 概率在数值上不再变小，这里只要求「不上升」——否则断言本身在考浮点
        for _ in range(200):
            speak.note_group_message(gid)
            seq.append(speak.probability(gid, mentioned=False, text="", nick=""))
        assert all(b <= a for a, b in zip(seq, seq[1:])), "概率不许回升"
        assert seq[-1] < cfg.speak_p_min + 0.02, f"刷屏之后应接近 p_min: {seq[-1]}"
        assert seq[-1] >= cfg.speak_p_min - 1e-9, seq[-1]

        # 热度本身也是单调上升的（边际递减，永远到不了 1）
        hot = state(gid)
        assert 0.99 < hot["fast"] < 1.0, hot
        assert 0.99 < hot["slow"] < 1.0, hot


# ---------------------------------------------------------------- 2. 被 @ 恒 1.0


def test_mentioned_is_always_one_and_readonly() -> None:
    clock = Clock()
    with sandbox(clock, speak_enabled=True) as cfg:
        gid = 1002
        speak.note_group_message(gid)
        before = speak.stats()

        p = speak.probability(gid, mentioned=True, text="在吗", nick="小明")
        assert p == 1.0, p
        assert speak.stats() == before, "被 @ 的判定不许改任何状态（不走 ambient/engage）"

        # 两个恒真入口也都不掷骰子、不记账
        roll_never = 0.999999
        for kind in ("at", "poke"):
            ok, pk = speak.decide_speak(gid, kind, "在吗", "小明", roll=roll_never)
            assert ok is True and pk == 1.0, (kind, ok, pk)
        assert speak.stats() == before, "@ / 戳一戳不该改状态"

        # 就算热度爆表、engage 为 0，被 @ 也还是 1.0
        for _ in range(200):
            speak.note_group_message(gid)
        assert speak.probability(gid, mentioned=True, text="", nick="") == 1.0
        assert speak.probability(gid, mentioned=False, text="", nick="") < cfg.speak_p_min + 0.01


# ---------------------------------------------------------------- 3. 判定先于记账


def test_judge_before_record() -> None:
    """同一个冷群：第一条的 p 必须**正好**是 p_max，第二条才被第一条压低。

    这条锁的就是「先判定、后记账」：如果实现反过来（先 note_group_message
    再算 p），第一条会被自己抬起来的热度压一次，p 就**到不了** p_max，
    而第一条恰恰是最该捧场的那条。
    """
    clock = Clock()
    with sandbox(clock, speak_enabled=True) as cfg:
        gid = 1003
        text, nick = "看这个 https://example.com/abc", "小明"

        # 完全照 __init__._listen_group_message 的顺序走一遍：
        # 判定（概率门）→ 记账
        p1 = speak.decide_speak(gid, "link", text, nick, roll=0.0)[1]
        if not speak.note_engage(gid, mentioned=False, text=text, nick=nick):
            speak.note_group_message(gid)

        p2 = speak.decide_speak(gid, "link", text, nick, roll=0.0)[1]

        assert abs(p1 - cfg.speak_p_max) < 1e-9, f"第一条必须还是冷群概率: {p1}"
        assert p2 < p1, f"第二条判定前已记过账，p 必须更低: {p1} vs {p2}"


# ---------------------------------------------------------------- 4. 衰减


def test_decay_follows_half_life() -> None:
    clock = Clock()
    with sandbox(clock, speak_enabled=True) as cfg:
        gid = 1004
        speak.note_group_message(gid)
        fast0 = state(gid)["fast"]
        slow0 = state(gid)["slow"]
        assert fast0 > 0.14 and slow0 > 0.14, (fast0, slow0)

        # 恰好一个 fast 半衰期 → 减半；两个 → 剩四分之一
        clock.advance(cfg.speak_heat_fast_half_life)
        fast1 = state(gid)["fast"]
        assert abs(fast1 - fast0 / 2) < 1e-9, (fast0, fast1)
        clock.advance(cfg.speak_heat_fast_half_life)
        fast2 = state(gid)["fast"]
        assert abs(fast2 - fast0 / 4) < 1e-9, (fast0, fast2)

        # 慢窗口按自己的半衰期走（同一个 dt 下衰减得慢得多）
        slow_now = state(gid)["slow"]
        expect = slow0 * 0.5 ** (2 * cfg.speak_heat_fast_half_life / cfg.speak_heat_slow_half_life)
        assert abs(slow_now - expect) < 1e-9, (slow0, slow_now, expect)

        # 时间不推进时反复读，值不变（惰性结算不能自己漂）
        assert state(gid)["fast"] == state(gid)["fast"]


def test_engage_decays_faster_than_heat() -> None:
    clock = Clock()
    with sandbox(clock, speak_enabled=True) as cfg:
        gid = 1005
        speak.note_engage(gid, mentioned=True, text="", nick="")
        assert state(gid)["engage"] == 1.0
        clock.advance(cfg.speak_engage_half_life)
        assert abs(state(gid)["engage"] - 0.5) < 1e-9, state(gid)["engage"]
        clock.advance(cfg.speak_engage_half_life)
        assert abs(state(gid)["engage"] - 0.25) < 1e-9, state(gid)["engage"]


# ---------------------------------------------------------------- 5. 自己发言


def test_bot_spoke_damps_engage_and_lifts_only_fast() -> None:
    clock = Clock()
    with sandbox(clock, speak_enabled=True) as cfg:
        gid = 1006
        speak.note_engage(gid, mentioned=True, text="", nick="")
        before = state(gid)
        assert before["engage"] == 1.0
        assert before["slow"] == 0.0 and before["fast"] == 0.0

        speak.note_bot_spoke(gid)
        after = state(gid)

        # engage 必须**下降**：让它上升就是正反馈，冷群里必然演变成刷屏
        assert after["engage"] < before["engage"], (before, after)
        assert abs(after["engage"] - cfg.speak_engage_self_damp) < 1e-9, after["engage"]
        # 只抬 fast；slow 一动不动（「我刚说过话」不是「这个群有人气」的证据）
        assert after["fast"] > before["fast"], (before, after)
        assert after["fast"] == cfg.speak_self_bump, after["fast"]
        assert after["slow"] == 0.0, f"慢窗口被自己发言污染了: {after['slow']}"


# ---------------------------------------------------------------- 6. 紧跟窗口只用一次


def test_followup_window_is_consumed_once() -> None:
    clock = Clock()
    with sandbox(clock, speak_enabled=True) as cfg:
        gid = 1007
        speak.note_bot_spoke(gid)
        assert speak.note_engage(gid, mentioned=False, text="对", nick="小明") is True
        # 同一条窗口内的第二条：不算在跟它说话（窗口用完即清）
        assert speak.note_engage(gid, mentioned=False, text="对", nick="小红") is False
        assert speak.note_engage(gid, mentioned=False, text="对", nick="小刚") is False

        # 窗口过期之后也不再算
        speak.note_bot_spoke(gid)
        clock.advance(cfg.speak_engage_followup_window + 1.0)
        assert speak.note_engage(gid, mentioned=False, text="对", nick="小明") is False


# ---------------------------------------------------------------- 7. 紧跟补量递减


def test_followup_bump_shrinks_as_engage_fills() -> None:
    """`room = 1 - engage` 越小补得越少。不减的话「衰减 ↔ 补满」会死循环：
    它说一句 → 对方回一句 → 窗口内又补满 → 它又说……跟一个群友无限对聊。
    """
    clock = Clock()
    with sandbox(clock, speak_enabled=True) as cfg:
        # 基线：engage 从 0 开始，紧跟一次 → 补到约 up_followup（0.3）
        cold = 1008
        speak.note_bot_spoke(cold)
        assert speak.note_engage(cold, mentioned=False, text="嗯", nick="小明") is True
        bump_cold = state(cold)["engage"]
        assert abs(bump_cold - cfg.speak_engage_up_followup) < 1e-6, bump_cold

        # engage 已经接近 1：同样紧跟一次，几乎补不动
        full = 1009
        speak.note_engage(full, mentioned=True, text="", nick="")   # 补满
        speak.note_bot_spoke(full)                                   # 说完打折、开窗口
        speak._STATE[full].engage = 0.97                             # 摆到接近饱和
        speak._STATE[full].bot_at = clock.t                          # 窗口刚打开
        before = state(full)["engage"]
        assert speak.note_engage(full, mentioned=False, text="嗯", nick="小明") is True
        bump_full = state(full)["engage"] - before
        assert bump_full > 0, bump_full
        assert bump_full < bump_cold * 0.05, f"存量越满补得越少: {bump_full} vs {bump_cold}"


# ---------------------------------------------------------------- 8. 关键词


def test_keyword_paths() -> None:
    clock = Clock()
    with sandbox(clock, speak_enabled=True, speak_keywords=["小助手"]) as cfg:
        # 正文命中 → 抬 engage，而且算「在跟它说话」（调用方不会再去抬群热度）
        g1 = 1010
        assert speak.note_engage(g1, mentioned=False, text="小助手你在吗", nick="小明") is True
        assert state(g1)["engage"] > 0
        assert abs(state(g1)["engage"] - cfg.speak_engage_keyword) < 1e-9

        # 昵称命中 → 抬 engage，但**不算**在跟它说话（那是别人起的名字，
        # 不是对着它说），所以要返回 False，让调用方照常记一条群闲聊
        g2 = 1011
        assert speak.note_engage(g2, mentioned=False, text="今天好热", nick="小助手他爸") is False
        assert state(g2)["engage"] > 0
        assert state(g2)["fast"] == 0.0, "note_engage 自己从不抬热度"

        # 都没命中 → 什么都不抬
        g3 = 1013
        assert speak.note_engage(g3, mentioned=False, text="今天好热", nick="小明") is False
        assert state(g3)["engage"] == 0.0

        # 关键词表留空 = 完全不启用这套判定
        g4 = 1014
        assert speak.note_engage(g4, mentioned=False, text="小助手你在吗", nick="小明") is True
        speak.reset()
        with sandbox(clock, speak_enabled=True, speak_keywords=[]) as cfg2:
            assert cfg2.speak_keywords == []
            assert speak.note_engage(1015, mentioned=False, text="小助手你在吗", nick="小明") is False
            assert state(1015)["engage"] == 0.0


# ---------------------------------------------------------------- 9. dry_run 语义


def test_dry_run_never_blocks_but_real_mode_does() -> None:
    clock = Clock()
    # 干跑：p 算到 0.0 也照样放行（只记日志）
    with sandbox(clock, speak_enabled=True, speak_dry_run=True,
                 speak_p_max=0.0, speak_p_min=0.0):
        ok, p = speak.decide_speak(1016, "link", "", "", roll=0.999999)
        assert ok is True, "干跑不许改变任何实际行为"
        assert p == 0.0, p

    # 真接管：同样 p=0.0 → 拦下
    with sandbox(clock, speak_enabled=True, speak_dry_run=False,
                 speak_p_max=0.0, speak_p_min=0.0):
        ok, p = speak.decide_speak(1016, "link", "", "", roll=0.0)
        assert ok is False, "接管后 p=0 必须拦"
        assert p == 0.0, p

    # 边界：p=0.5 时 roll < p 放行、roll >= p 拦下（比较是严格小于）
    with sandbox(clock, speak_enabled=True, speak_dry_run=False,
                 speak_p_max=0.5, speak_p_min=0.0):
        assert speak.decide_speak(1017, "link", "", "", roll=0.4999)[0] is True
        assert speak.decide_speak(1017, "link", "", "", roll=0.5)[0] is False

        # 不传 roll 时走模块级 _rand()：替换它同样可复现（真正上线用的就是这条路）
        old_rand = speak._rand
        try:
            speak._rand = lambda: 0.0    # type: ignore[assignment]
            assert speak.decide_speak(1017, "link", "", "")[0] is True
            speak._rand = lambda: 0.9    # type: ignore[assignment]
            assert speak.decide_speak(1017, "link", "", "")[0] is False
        finally:
            speak._rand = old_rand       # type: ignore[assignment]


# ---------------------------------------------------------------- 10. 关掉开关


def test_disabled_is_passthrough() -> None:
    clock = Clock()
    with sandbox(clock) as cfg:          # speak_enabled 默认 False
        assert cfg.speak_enabled is False
        assert cfg.speak_dry_run is True
        for kind in ("link", "banter", "at", "poke"):
            ok, p = speak.decide_speak(1020, kind, "随便一句话", "小明", roll=0.999999)
            assert ok is True, f"关掉开关后 {kind} 必须恒放行"
            assert p == 1.0, (kind, p)
        # 一次都不该碰到概率：连热度都没有也不影响结论
        assert speak.stats()["count"] == 0


def test_accounting_runs_after_every_trigger() -> None:
    """记账监听器的优先级必须**大于所有触发点**——这是「判定先于记账」的接线保证。

    触发点：@ 是 priority=5，非音乐链接在 ``music_listener`` 里是 99。
    若记账挤进旁听链路（priority=2），同一条带链接的消息会先把自己热度抬起来、
    再拿被自己抬高的热度算概率——死群第一条链接的 p 从 0.70 掉到 0.52，
    而第一条恰恰是最该接的那条（见 ``speak.py`` 模块头的铁律 1）。
    """
    import music_collector as mc

    account = getattr(mc, "speak_listener", None)
    assert account is not None, "缺少说话时机的记账监听器 speak_listener"
    for name in ("music_listener", "at_listener", "match_reply_listener", "listen_listener"):
        matcher = getattr(mc, name, None)
        if matcher is None:
            continue
        assert int(account.priority) > int(matcher.priority), (
            f"记账监听器优先级必须大于 {name}"
            f"（{account.priority} vs {matcher.priority}）"
        )


if __name__ == "__main__":
    test_cold_group_is_likely_and_busy_group_is_quiet()
    test_mentioned_is_always_one_and_readonly()
    test_judge_before_record()
    test_decay_follows_half_life()
    test_engage_decays_faster_than_heat()
    test_bot_spoke_damps_engage_and_lifts_only_fast()
    test_followup_window_is_consumed_once()
    test_followup_bump_shrinks_as_engage_fills()
    test_keyword_paths()
    test_dry_run_never_blocks_but_real_mode_does()
    test_disabled_is_passthrough()
    test_accounting_runs_after_every_trigger()
    print("speak tests OK")
