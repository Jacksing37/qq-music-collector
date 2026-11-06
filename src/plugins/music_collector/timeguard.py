"""时间感兜底：白天说「晚安」这种错话，改写一次；改不好就按词表硬换。

为什么要有这一层：提示词里早就写了「现在是几点、该用哪个时段的说法」，但那是
**软约束**——模型不听话就没有任何兜底。线上真实发生过的两次：

- 上午 9 点回了一句「今晚早点睡」（群友当场笑它没看表）；
- 18:07 说「别大晚上做决定」，群友直接回「现在才 18 点」。

两个设计点，都是踩过坑才定下来的：

1. **只管 06:00–20:00**。20:00 之后说「晚安」是对的，夜里拦下来纯属捣乱。
   18:00–20:00 算「傍晚」不算「晚上」——这是本项目已经踩过的坑：把 18 点当成
   大晚上，被群友当场纠正。所以边界一律取 ``[6, 20)``，不是「过了 18 点就算晚」。
2. **改写失败必须还有词表兜底**。改写要多花一次模型调用，它会超时、会被限流、
   会在中转挂掉时返回空串或原句；任何一条没兜住，群里看到的就是那句错话。
   所以 :func:`fix` 的契约是「**绝不抛异常**，且最差也要把四条最常见说法
   （今晚 / 晚安 / 早点睡 / 该睡了）换掉」。词表只有四条，兜不住「大晚上」
   这类别的说法——这也正是「改写优先、词表垫底」的顺序理由：能改就改，改不了
   至少不是最常翻车的那几句。

改写用的是一次性低温暖补全（``llm_bot.complete``）：不带历史、不带工具、不占
冷却、默认关思考，正好适合「把一句话换个说法」这种一次性任务。
"""

from __future__ import annotations

import re
from datetime import datetime

from nonebot.log import logger

from .config import LLMConfig, config_manager
from .llm import llm_bot

__all__ = [
    "NIGHT_WORDS",
    "needs_fix",
    "slot_of",
    "hard_replace",
    "fix",
]

#: 夜间用语。命中任意一个就说明这句话在说「晚上」，白天说出来就是错的。
#:
#: 「好梦」和「做个好梦」都列着：后者能被前者匹配到，但显式写出来是为了让
#: 这张表本身好读——扫一眼就知道覆盖了哪些说法，不用在脑子里跑正则。
NIGHT_WORDS = re.compile(r"(晚安|今晚|大晚上|这么晚|早点睡|该睡了|睡吧|好梦|做个好梦)")

#: 白天的起止小时（左闭右开）。20 点之后不算白天，见模块开头的坑。
DAY_START = 6
DAY_END = 20

#: 词表兜底：改写失败时只能按这几个词硬换。
#:
#: 顺序有讲究——**长的、更具体的排前面**。像「该睡了」若排在「睡了」后面
#: （目前没有后者，但以后别加）就会被换掉一半，剩下半句语法上不成立。
#: 这里只收四条最常撞的说法，多一条就多一处误伤：宁可漏换（还能靠改写），
#: 不可错换（把「晚安」改成不通顺的句子）。
_HARD_REPLACE: tuple[tuple[str, str], ...] = (
    ("该睡了", "别太累"),
    ("早点睡", "早点休息"),
    ("晚安", "回头聊"),
    ("今晚", "今天"),
)

#: 改写时明确禁用的词。和 :data:`NIGHT_WORDS` 同一个清单，只是要写成人能读的形式。
_FORBIDDEN = "、".join(
    ["晚安", "今晚", "大晚上", "这么晚", "早点睡", "该睡了", "睡吧", "好梦"]
)

#: 模型爱给改写结果加引号（「今天的汤真好喝。」）。这层壳不能发给群友。
_QUOTES = "「」『』“”\"'‘’"

#: 一次回复里最多改几行。正常就一行带「今晚」；上限只防「整段都在说晚上」时连打十几次补全。
_MAX_LINES = 3


def _cfg() -> LLMConfig:
    """当前生效的 ``llm`` 配置。

    和 :mod:`speak` 一样直接读 ``config_manager``：``llm`` 整段是进程级唯一的
    （``config._GLOBAL_ONLY_SECTIONS`` 锁着，不允许按群覆盖），群号在这里拿不到
    任何额外信息。改写调用只需要接口地址 / Key / 模型名这些全局项。
    """
    return config_manager.config.llm


def needs_fix(text: str, now: datetime) -> bool:
    """这句话在当前时刻说出来是错的吗：白天 + 命中夜间用语。

    白天是 ``[06:00, 20:00)``。夜间（<6 点或 ≥20 点）一律 ``False``——那正是
    说「晚安」的时候，拦它才是 bug。
    """
    if not (DAY_START <= int(now.hour) < DAY_END):
        return False
    return bool(NIGHT_WORDS.search(str(text or "")))


def slot_of(hour: int) -> str:
    """这个小时该说哪个时段。用来告诉模型「现在到底是什么时候」。

    18:00–20:00 是**傍晚**而不是晚上，20:00 之后才算晚上——项目里已经踩过
    「18 点说大晚上」这个坑，边界写死在这里，别再各写一份。
    """
    h = int(hour) % 24
    if h < DAY_START:
        return "凌晨"
    if h < 12:
        return "上午"
    if h < 14:
        return "中午"
    if h < 18:
        return "下午"
    if h < DAY_END:
        return "傍晚"
    return "晚上"


def hard_replace(text: str) -> str:
    """按词表把夜间用语换成白天能说的话（改写失败时的最后一道兜底）。

    只换词、不改语序：这一层的目标是「别让群里看到『晚安』」，不是「写得好看」。
    语句通不通顺由模型那一层负责，这里再动手就有把句子改坏的风险。
    """
    out = str(text or "")
    for word, repl in _HARD_REPLACE:
        out = out.replace(word, repl)
    return out


async def fix(
    text: str,
    now: datetime,
    *,
    complete=None,
    enabled: bool = True,
) -> str:
    """需要时把这句话改写成符合当前时段的说法，**绝不抛异常**。

    ``enabled`` 为 ``False`` 时原样返回（连正则都不跑）：关掉开关就必须与改造前
    逐字一致，这也是回滚开关。

    ``complete`` 是可注入的补全函数，默认 ``llm_bot.complete``。约定签名
    ``await complete(prompt, cfg, *, max_tokens=…, temperature=…) -> (正文, 错误)``，
    测试传桩函数即可，不需要联网。

    三条出口，优先级从高到低：

    1. 不需要修（夜间 / 没有夜间词）→ 原样返回；
    2. 模型改写成功且改写结果干净 → 用模型的；
    3. 改写结果仍带夜间词、是空串、或调用抛异常 → :func:`hard_replace`。

    第 3 条的判断里**必须**再查一遍夜间词：模型完全可能把「今晚」换成「今夜」
    或者干脆把原句抄回来，那不是修好了，是白花一次钱。

    ⚠️ **按行改，换行结构原样保留**（2026-11-06 线上翻的车）：这个函数曾经把
    整条回复丢给模型改写、再整条替换，于是「哈？\\n\\n那句话」被压成一行空格——
    后面的拆条（``llm.reply_split``）看到单行，整条回复就又变成一大段发出去了。
    现在只改**含夜间用语的那一行**，其余行一字不动。
    """
    raw = str(text or "")
    if not enabled or not needs_fix(raw, now):
        return raw

    lines = raw.split("\n")
    # 只挑「说错了」的那几行；上限是为了万一整段都在说晚上，不至于连打十几次补全
    targets = [i for i, ln in enumerate(lines) if needs_fix(ln, now)][:_MAX_LINES]
    if not targets:
        # 夜间词被换行劈成两半这种边角（实际不会出现）：整条按词表换，
        # 至少能保证换行不丢，不会把拆条一起搞坏
        return hard_replace(raw)

    comp = complete if complete is not None else llm_bot.complete
    for i in targets:
        lines[i] = await _rewrite_line(lines[i], now, comp)
    return "\n".join(lines)


async def _rewrite_line(line: str, now: datetime, comp) -> str:
    """改**一行**：模型改写优先，改不好退回词表。永不抛异常、永不返回多行。"""
    try:
        out, err = await comp(
            _rewrite_prompt(line, now),
            _cfg(),
            # 低温暖 + 短上限：这活儿只是「换几个词」，不需要创意也不需要长思考
            max_tokens=200,
            temperature=0.5,
        )
        cleaned = _unwrap(str(out or ""))
        # 模型偶尔会把整段几行一起吐回来：只留第一行，别把结构还给它自己决定
        cleaned = cleaned.split("\n")[0].strip() if cleaned else ""
        if cleaned and not NIGHT_WORDS.search(cleaned):
            logger.info(f"[timeguard] 白天夜间用语已改写：{line[:40]} → {cleaned[:40]}")
            return cleaned
        logger.info(
            f"[timeguard] 改写结果不能用（空或仍含夜间词，err={err or '无'}）→ 退回词表"
        )
    except Exception as exc:  # noqa: BLE001 — 兜底函数绝不能把异常抛给回话链路
        logger.warning(f"[timeguard] 改写调用失败，退回词表兜底: {exc}")
    return hard_replace(line)


def _rewrite_prompt(text: str, now: datetime) -> str:
    """拼一次性的改写指令：告诉它现在几点、哪个时段、禁用哪些词、只要一句话。"""
    return (
        f"现在是 {now:%H:%M}（{slot_of(now.hour)}），**不是晚上**。\n"
        f"下面这句话把时间说错了，请按当前时段改写，意思和语气保持不变：\n"
        f"{text}\n\n"
        f"要求：\n"
        f"1. 不要出现这些词：{_FORBIDDEN}。\n"
        f"2. 只把「晚上」相关的说法换成符合{slot_of(now.hour)}的，别的话题一律别动。\n"
        f"3. 只输出改写后的这一句，不要解释、不要加引号、不要分行。"
    )


def _unwrap(text: str) -> str:
    """去掉首尾空白与模型自带的引号壳。"""
    s = str(text or "").strip()
    while len(s) >= 2 and s[0] in _QUOTES and s[-1] in _QUOTES:
        s = s[1:-1].strip()
    return s
