"""验证 budget 的当日记账与闸门：折算、默认不限、边界、脏 usage、跨天清零。

关键约束（对应设计文档 `docs/聊天体验拼装设计.md` §5 P5.3）：

- **默认零行为变化**：`daily_budget = 0` 时 `exceeded` 恒 False（关掉开关就是旧行为）
- 折算口径写在一处：`prompt/1e6*price_in + completion/1e6*price_out`
- **跨天自动清零**，靠可注入的 `now` 验证，不真的改系统时钟、不起定时任务
- `usage` 缺失（None / 空 dict / 字段缺失 / 不是 dict）只该少一个数字，**不抛异常**

所有用例内部先 `reset()`：这是一份进程内全局状态，用例之间必须互不影响。
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")  # 触发插件加载（__init__ 会 require apscheduler）

from music_collector import budget  # noqa: E402
from music_collector.config import LLMConfig  # noqa: E402


# ---------------------------------------------------------------- 1. 累计与折算


def test_note_accumulates_and_converts_to_money() -> None:
    """两次调用后 spent_today = 两项之和（按单价手算核对，不抄代码）。"""
    budget.reset()
    cfg = LLMConfig(daily_budget=10.0, price_in=2.0, price_out=8.0)
    budget.note({"prompt_tokens": 1000, "completion_tokens": 500}, cfg)
    budget.note({"prompt_tokens": 2000, "completion_tokens": 250}, cfg)

    # 手算：输入共 3000 token、输出共 750 token，单价分别是 2 / 8 元每百万
    expect = 3000 / 1e6 * 2.0 + 750 / 1e6 * 8.0  # = 0.006 + 0.006 = 0.012
    got = budget.spent_today()
    assert abs(got - expect) < 1e-12, (got, expect)

    st = budget.stats()
    assert st["prompt_tokens"] == 3000, st
    assert st["completion_tokens"] == 750, st
    assert st["tokens"] == 3750, st
    assert st["day"] == datetime.now().strftime("%Y-%m-%d"), st


# ---------------------------------------------------------------- 2. 默认不限


def test_zero_budget_means_unlimited() -> None:
    """``daily_budget = 0`` → 恒 False，花多少都不拦（默认配置下的旧行为）。"""
    budget.reset()
    cfg = LLMConfig()  # 单价与预算都是 0
    budget.note({"prompt_tokens": 10_000_000, "completion_tokens": 10_000_000}, cfg)
    assert budget.exceeded(cfg) is False
    # 单价 0 时只统计 token、不算钱
    assert budget.spent_today() == 0.0
    assert budget.stats()["tokens"] == 20_000_000


# ---------------------------------------------------------------- 3. 边界


def test_exceeded_and_exact_boundary() -> None:
    """没到上限不拦；**刚好等于上限就算用尽**（再发一次必然超）。"""
    budget.reset()
    cfg = LLMConfig(daily_budget=1.0, price_in=1.0)
    # 999_999 token × 1 元/百万 = 0.999999 元 < 1.0
    budget.note({"prompt_tokens": 999_999}, cfg)
    assert budget.exceeded(cfg) is False, budget.spent_today()
    # 再补 1 token → 恰好 1.0 元
    budget.note({"prompt_tokens": 1}, cfg)
    assert abs(budget.spent_today() - 1.0) < 1e-9, budget.spent_today()
    assert budget.exceeded(cfg) is True, "刚好用满也要拦（>= 视为超）"


# ---------------------------------------------------------------- 4. 脏 usage


def test_bad_usage_is_ignored_without_raising() -> None:
    """None / 空 dict / 缺字段 / 非数字 / 不是 dict → 不抛、不改变累计。"""
    budget.reset()
    cfg = LLMConfig(daily_budget=1.0, price_in=1.0, price_out=1.0)
    for bad in (
        None,
        {},
        {"prompt_tokens": None},
        {"prompt_tokens": "", "completion_tokens": ""},
        {"prompt_tokens": "abc"},
        {"unrelated": 3},
        "not-a-dict",
        ["prompt_tokens", 1],
    ):
        budget.note(bad, cfg)
    assert budget.spent_today() == 0.0, budget.spent_today()
    assert budget.stats()["tokens"] == 0, budget.stats()
    # 记账失败之后仍然正常可记
    budget.note({"prompt_tokens": 1_000_000}, cfg)
    assert abs(budget.spent_today() - 1.0) < 1e-9


# ---------------------------------------------------------------- 5. 跨天清零


def test_cross_day_resets_lazily() -> None:
    """注入「明天」后 spent_today 归零——惰性清零，不依赖定时任务、不改系统时钟。"""
    budget.reset()
    cfg = LLMConfig(daily_budget=1.0, price_in=1.0)
    budget.note({"prompt_tokens": 1_000_000}, cfg)
    assert budget.exceeded(cfg) is True

    tomorrow = datetime.now() + timedelta(days=1)
    assert budget.spent_today(tomorrow) == 0.0
    assert budget.exceeded(cfg, tomorrow) is False
    # 清零是真的落在状态上（不是只在这一次读取里假装 0）：读「今天」的快照也是空的
    st = budget.stats()
    assert st["tokens"] == 0, st
    assert st["day"] == datetime.now().strftime("%Y-%m-%d"), st
    # 清零之后重新记账，从 0 开始
    budget.note({"prompt_tokens": 500_000}, cfg)
    assert abs(budget.spent_today() - 0.5) < 1e-9


# ---------------------------------------------------------------- 6. 维护


def test_reset_clears_everything() -> None:
    budget.reset()
    cfg = LLMConfig(daily_budget=1.0, price_in=1.0, price_out=1.0)
    budget.note({"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}, cfg)
    assert budget.spent_today() > 0
    budget.reset()
    assert budget.spent_today() == 0.0
    assert budget.stats()["tokens"] == 0
    assert budget.exceeded(cfg) is False


def test_fallback_message_hides_the_reason() -> None:
    """兜底话术不暴露原因：不能出现「预算 / token / 钱 / 元」这些内部词。"""
    msg = budget.fallback_message()
    assert msg == "我这会儿有点卡，等下再回你哈"
    for word in ("预算", "token", "钱", "元", "限"):
        assert word not in msg, (word, msg)


if __name__ == "__main__":
    test_note_accumulates_and_converts_to_money()
    test_zero_budget_means_unlimited()
    test_exceeded_and_exact_boundary()
    test_bad_usage_is_ignored_without_raising()
    test_cross_day_resets_lazily()
    test_reset_clears_everything()
    test_fallback_message_hides_the_reason()
    print("budget tests OK")
