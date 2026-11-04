"""日预算闸门：今天已经在模型上花了多少钱，够了就别再进模型。

为什么要有：:mod:`llmtrace` 只把每次调用的 ``usage`` 记进内存环形队列（200 条），
**没有当日累计、没有单价、没有任何闸门**。中转挂了狂重试、或者模型抽风把工具轮
刷满时，唯一的反馈是月底的账单——而那时候群里早就被刷爆了。

四条设计：

1. **进程内、不落盘**。「今天花了多少」是个当日指标，重启后重新数一遍并不冤
   （一天重启几次也就是多花几分钱）；反过来，落盘就得处理文件损坏、时区漂移、
   多进程写冲突，代价远大于收益。
2. **跨天惰性清零，不起定时任务**。内部记住「当前统计的是哪一天」，
   :func:`spent_today` / :func:`exceeded` / :func:`stats` 一发现日期变了就清空。
   定时任务要处理「进程正好在零点重启」「任务被 reload 掉」这些边界，而这件
   事的正确做法本来就是「读的时候对一下日期」。
3. **单价与预算默认 0 = 完全不启用**。默认配置下 :func:`exceeded` 恒 ``False``，
   行为与改造前逐字一致；只有真的填了 ``daily_budget`` 才会拦住调用。
4. **记账不能反过来打断回话**。:func:`note` 收到 ``None`` / 空 dict / 缺字段
   一律安静返回：用量字段各家兼容层写法不一，记账失败只该少一个数字，不该让
   一条群消息处理崩掉。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

__all__ = [
    "note",
    "spent_today",
    "exceeded",
    "fallback_message",
    "reset",
    "stats",
]

#: 当日累计。``day`` 是这份数字属于哪一天（``YYYY-MM-DD``，本地日期）。
#:
#: 三个数字放在一个 dict 里而不是三个全局变量：清零时必须一起清，分开写迟早
#: 会出现「token 清了、钱没清」这种对不上的状态。
_STATE: dict = {"day": "", "prompt": 0, "completion": 0, "spent": 0.0}

#: :func:`note` 可能被并发链路调用（多群同时在回话），累加必须串行。
_LOCK = threading.Lock()


def _today(now: Optional[datetime] = None) -> str:
    """本地日期字符串。``now`` 可注入，测试不必真的改系统时钟（同 banter._today）。"""
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _roll(now: Optional[datetime] = None) -> None:
    """日期变了就把累计清零。**调用方必须已经持有** :data:`_LOCK`。"""
    day = _today(now)
    if _STATE["day"] != day:
        _STATE["day"] = day
        _STATE["prompt"] = 0
        _STATE["completion"] = 0
        _STATE["spent"] = 0.0


def note(usage: dict | None, cfg) -> None:
    """记一次调用的 token 用量，并按单价折算成钱（元）。

    ``usage`` 就是 OpenAI 兼容响应里那段 ``{"prompt_tokens":…, "completion_tokens":…}``。
    缺失（``None`` / 空 dict / 没有这两个键）时什么都不做——**不记账不是错误**：
    有的中转压根不回 usage，这时候拦住它只会让机器人哑掉。

    折算公式写在一处（``prompt/1e6*price_in + completion/1e6*price_out``）：
    单价单位是「元 / 百万 token」，跟各家官网的报价口径一致，填的时候不用换算。
    """
    if not usage:
        return
    try:
        prompt = max(0, int(usage.get("prompt_tokens") or 0))
        completion = max(0, int(usage.get("completion_tokens") or 0))
    except (AttributeError, TypeError, ValueError):
        # usage 不是 dict（兼容层偶尔塞个字符串 / 列表），或者字段不是数字
        return
    if prompt <= 0 and completion <= 0:
        return
    try:
        price_in = float(getattr(cfg, "price_in", 0.0) or 0.0)
        price_out = float(getattr(cfg, "price_out", 0.0) or 0.0)
    except (TypeError, ValueError):
        price_in = price_out = 0.0
    cost = prompt / 1e6 * price_in + completion / 1e6 * price_out
    with _LOCK:
        _roll()
        _STATE["prompt"] += prompt
        _STATE["completion"] += completion
        _STATE["spent"] += cost


def spent_today(now: Optional[datetime] = None) -> float:
    """今天已经花了多少钱（元）。

    读的时候顺手跨天清零——这就是「自动清零」的全部实现，没有定时任务。
    """
    with _LOCK:
        _roll(now)
        return float(_STATE["spent"])


def exceeded(cfg, now: Optional[datetime] = None) -> bool:
    """今天的预算用完了吗。

    ``cfg.daily_budget <= 0`` 表示不限，恒 ``False``（默认值，也是回滚开关）。

    边界取 ``>=``：**刚好花到上限就算用尽**。理由很简单——再发一个请求就一定
    超了，而超出去的那一笔没有任何机制能追回来。
    """
    try:
        limit = float(getattr(cfg, "daily_budget", 0.0) or 0.0)
    except (TypeError, ValueError):
        limit = 0.0
    if limit <= 0:
        return False
    return spent_today(now) >= limit


def fallback_message() -> str:
    """超预算时对群里说的话。**不暴露原因**（不说「预算用尽」，更不报数字）。

    单独抽成函数而不是写死在一处：这句话是「产品口径」，改一次就该处处生效，
    也不该让调用方各写一份「我这会儿有点卡」。
    """
    return "我这会儿有点卡，等下再回你哈"


def reset() -> None:
    """清空累计（测试用；也想立刻重新数一遍时可以手动调）。"""
    with _LOCK:
        _STATE["day"] = ""
        _STATE["prompt"] = 0
        _STATE["completion"] = 0
        _STATE["spent"] = 0.0


def stats() -> dict:
    """当日快照（调试 / 测试断言用，**不参与任何业务判断**）。"""
    with _LOCK:
        _roll()
        return {
            "day": _STATE["day"],
            "prompt_tokens": int(_STATE["prompt"]),
            "completion_tokens": int(_STATE["completion"]),
            "tokens": int(_STATE["prompt"]) + int(_STATE["completion"]),
            "spent": float(_STATE["spent"]),
        }
