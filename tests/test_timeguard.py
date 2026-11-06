"""验证 timeguard 的时间感兜底：白天才拦、改不好退回词表、夜里一动不动。

关键约束（对应设计文档 `docs/聊天体验拼装设计.md` §5 P5.2）：

- **夜间（6 点前 / 20 点后）绝不拦**：那时候说「晚安」是对的，拦下来才是 bug
- 18:00–20:00 是「傍晚」不是「晚上」（项目里已经踩过「18 点说大晚上」这个坑）
- **改写失败（空串 / 仍带夜间词 / 抛异常）一律退回 `hard_replace`**，绝不抛出
- `enabled=False` 必须原样返回——关掉开关就是零行为变化

改写靠注入的桩 `complete`，**不联网、不 sleep**：这一层要验的是「什么时候该调、
调了结果怎么用」，不是模型写得好不好。
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")  # 触发插件加载（__init__ 会 require apscheduler）

from music_collector import timeguard as T  # noqa: E402


# ---------------------------------------------------------------- 测试脚手架


class Stub:
    """可注入的补全桩：记调用次数、返回固定内容、可模拟抛异常。

    契约与 ``llm_bot.complete`` 一致：``await comp(prompt, cfg, **kwargs) -> (正文, 错误)``。
    """

    def __init__(self, reply: str = "", error: str = "", boom: Exception | None = None):
        self.reply = reply
        self.error = error
        self.boom = boom
        self.calls = 0
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, cfg, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        if self.boom is not None:
            raise self.boom
        return self.reply, self.error


def at(hour: int, minute: int = 0) -> datetime:
    """造一个固定时刻（不带时区：被测代码只读 ``hour``）。"""
    return datetime(2026, 11, 5, hour, minute)


def run(text: str, now: datetime, stub: Stub, *, enabled: bool = True) -> str:
    return asyncio.run(T.fix(text, now, complete=stub, enabled=enabled))


# ---------------------------------------------------------------- 1. 白天该改


def test_daytime_night_word_is_rewritten() -> None:
    """白天 + 夜间词 → 调一次 complete，用它的结果。"""
    stub = Stub(reply="今天早点休息，别熬夜")
    raw = "今晚早点睡，别熬夜"
    out = run(raw, at(9), stub)
    assert out == "今天早点休息，别熬夜", out
    assert stub.calls == 1, stub.calls
    # 改写指令里必须告诉它现在几点、哪个时段，并列出禁用词（否则它无从改起）
    prompt = stub.prompts[0]
    assert "09:00" in prompt and "上午" in prompt, prompt
    assert "晚安" in prompt and "早点睡" in prompt, prompt


def test_daytime_without_night_word_never_calls_model() -> None:
    """白天但没有夜间词 → 一次都不调（这是省钱的关键，不能靠模型判断）。"""
    stub = Stub(reply="随便什么")
    raw = "上午好，今天听点什么"
    out = run(raw, at(10), stub)
    assert out == raw, out
    assert stub.calls == 0, stub.calls


# ---------------------------------------------------------------- 2. 夜间不动


def test_night_is_untouched() -> None:
    """夜里（22:00 / 凌晨 3:00）说「晚安」是对的 → 不调、原样返回。"""
    for hour in (22, 3):
        stub = Stub(reply="（不该被用到）")
        raw = "晚安，早点睡"
        out = run(raw, at(hour), stub)
        assert out == raw, (hour, out)
        assert stub.calls == 0, (hour, stub.calls)


def test_enabled_false_is_verbatim_passthrough() -> None:
    """关掉开关 = 旧行为：连正则都不跑，原样返回。"""
    stub = Stub(reply="改写后的")
    raw = "今晚早点睡"
    out = run(raw, at(9), stub, enabled=False)
    assert out == raw, out
    assert stub.calls == 0, stub.calls


def test_needs_fix_boundaries() -> None:
    """白天区间是 ``[6, 20)``：6 点整要拦，20 点整不拦。"""
    text = "今晚早点睡"
    assert T.needs_fix(text, at(6)) is True
    assert T.needs_fix(text, at(19, 59)) is True
    assert T.needs_fix(text, at(20)) is False
    assert T.needs_fix(text, at(5, 59)) is False
    assert T.needs_fix("上午好", at(9)) is False


# ---------------------------------------------------------------- 3. 改写失败兜底


def test_rewrite_still_dirty_falls_back_to_word_table() -> None:
    """改写结果仍含夜间词 → 不能用，退回 hard_replace。"""
    stub = Stub(reply="今晚早点休息吧")  # 换了个说法，但「今晚」还在
    raw = "今晚早点睡"
    out = run(raw, at(9), stub)
    assert out == T.hard_replace(raw), out
    assert out == "今天早点休息", out
    assert "今晚" not in out


def test_empty_rewrite_falls_back_to_word_table() -> None:
    """改写返回空串（超时 / 被截断 / 模型啥也没说）→ 退回 hard_replace。"""
    stub = Stub(reply="   ")
    raw = "晚安啦"
    out = run(raw, at(9), stub)
    assert out == T.hard_replace(raw) == "回头聊啦", out


def test_complete_exception_falls_back_instead_of_raising() -> None:
    """complete 抛异常 → 不抛出，退回 hard_replace（兜底层绝不能把回话打断）。"""
    stub = Stub(boom=RuntimeError("中转挂了"))
    raw = "该睡了，明早还要上班"
    out = run(raw, at(9), stub)
    assert out == T.hard_replace(raw) == "别太累，明早还要上班", out
    assert "该睡了" not in out


# ---------------------------------------------------------------- 3.5 换行结构必须保住


def test_multiline_keeps_line_breaks_and_splits_still_work() -> None:
    """2026-11-06 线上翻的车：整条丢给模型改写 → 换行被压成空格 → 拆条看到单行。

    现在只改**含夜间用语的那一行**，其余行一字不动，段数与段内容都不变。
    """
    raw = "哈？火山罐挨着人质\n\n那今晚开个自定义试试不就知道了\n\n人质别放得太挤~🐳"
    stub = Stub(reply="那今天下午开个自定义试试不就知道了")
    out = run(raw, at(9), stub)
    assert out == "哈？火山罐挨着人质\n\n那今天下午开个自定义试试不就知道了\n\n人质别放得太挤~🐳", out
    assert out.count("\n") == raw.count("\n"), "换行结构必须原样保留"
    assert stub.calls == 1, stub.calls            # 只改一行就只调一次
    # 送进改写的是**那一行**，不是整条回复
    assert "哈？" not in stub.prompts[0], stub.prompts[0][-200:]


def test_multiline_fallback_also_keeps_line_breaks() -> None:
    """改写失败退词表时也只换那句的词，别把整段的换行吃掉。"""
    stub = Stub(boom=RuntimeError("中转挂了"))
    raw = "先看阵容\n今晚再说"
    out = run(raw, at(9), stub)
    assert out == "先看阵容\n今天再说", out


def test_multiline_only_dirty_lines_cost_calls() -> None:
    """几行都没问题就一次也不调；夜间词在第三行就只调一次。"""
    stub = Stub(reply="（不该被用到）")
    assert run("第一行\n第二行\n第三行", at(9), stub) == "第一行\n第二行\n第三行"
    assert stub.calls == 0, stub.calls

    stub2 = Stub(reply="今天再说")
    out = run("第一行\n第二行\n今晚再说", at(9), stub2)
    assert out == "第一行\n第二行\n今天再说", out
    assert stub2.calls == 1, stub2.calls


def test_rewritten_line_is_single_line_even_if_model_returns_many() -> None:
    """模型不听话吐了多行 → 只留第一行，结构不能由它决定。"""
    stub = Stub(reply="第一行新说法\n第二行不该出现在输出里")
    out = run("今晚早点睡", at(9), stub)
    assert out == "第一行新说法", out


# ---------------------------------------------------------------- 4. 词表


def test_hard_replace_covers_four_words() -> None:
    """四条词表各验一次（这是改写彻底失败时群里唯一会看到的东西）。"""
    assert T.hard_replace("今晚听什么") == "今天听什么"
    assert T.hard_replace("晚安，明天聊") == "回头聊，明天聊"
    assert T.hard_replace("早点睡吧") == "早点休息吧"
    assert T.hard_replace("该睡了") == "别太累"


def test_hard_replace_leaves_other_text_alone() -> None:
    assert T.hard_replace("上午好，今天听点什么") == "上午好，今天听点什么"
    assert T.hard_replace("") == ""


# ---------------------------------------------------------------- 5. 时段


def test_slot_of_boundaries() -> None:
    """18–20 点必须是「傍晚」——18 点说「大晚上」就是线上翻过的那次车。"""
    assert T.slot_of(5) == "凌晨"
    assert T.slot_of(6) == "上午"
    assert T.slot_of(11) == "上午"
    assert T.slot_of(12) == "中午"
    assert T.slot_of(14) == "下午"
    assert T.slot_of(18) == "傍晚"
    assert T.slot_of(20) == "晚上"


if __name__ == "__main__":
    test_daytime_night_word_is_rewritten()
    test_daytime_without_night_word_never_calls_model()
    test_night_is_untouched()
    test_enabled_false_is_verbatim_passthrough()
    test_needs_fix_boundaries()
    test_rewrite_still_dirty_falls_back_to_word_table()
    test_empty_rewrite_falls_back_to_word_table()
    test_complete_exception_falls_back_instead_of_raising()
    test_multiline_keeps_line_breaks_and_splits_still_work()
    test_multiline_fallback_also_keeps_line_breaks()
    test_multiline_only_dirty_lines_cost_calls()
    test_rewritten_line_is_single_line_even_if_model_returns_many()
    test_hard_replace_covers_four_words()
    test_hard_replace_leaves_other_text_alone()
    test_slot_of_boundaries()
    print("timeguard tests OK")
