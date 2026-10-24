"""主动搭话：没人叫它，也隔一阵自己在群里冒个泡。

和另外三个触发点的区别值得先说清楚：

- ``@ 机器人`` / ``戳一戳`` / 非音乐链接 —— 都是「群里发生了某件事」才回；
- 这里是**定时**的：非勿扰时段里，每隔一段随机时间挑一个点，
  把群里最近的一句话接过去。

几道闸（缺一不可）：

- ``llm.banter`` 总开关 + ``llm.enabled`` + 生效群；
- 勿扰时段 ``llm.banter_quiet``：**可配多段**，支持跨午夜（``23:00-08:00``）；
- 最近一条消息太旧（``banter_fresh_minutes``）就不接——半夜三点突然回一句
  两小时前的话，比不说话还怪；
- 太短（「哈哈」「+1」）、纯标点、命令一律不接；
- 每群每天上限 ``banter_daily_max``（``0`` = 不限）。

素材取自 ``chat_log(kind='listen')``，也就是旁听器攒下来的群聊。这样做的好处是
不额外占存储，代价是**依赖 ``memory.listen``**：关掉旁听就没素材，此时
:func:`run_banter` 会在日志里说明原因，而不是悄悄什么都不做。
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from typing import Optional

import nonebot
from nonebot.adapters.onebot.v11 import Bot, Message
from nonebot.log import logger

from .bot_utils import safe_send_group
from .config import LLMConfig
from .llm import llm_bot
from .service import service

#: 主动搭话能带多少字的历史。**不跟 ``llm.history_chars`` 走**：
#: 那个值可能配到上百万字（真人提问时值得），但主动搭话只要「知道最近聊了啥」，
#: 带全部历史等于每句话花一块多钱。12000 字约 6000 token，够看最近几十轮。
BANTER_HISTORY_CHARS = 12000

#: 主动搭话的回复上限。冒泡不该写小作文。
BANTER_REPLY_CHARS = 120

#: 送给模型的说明。走 ``chat(context=...)`` 传，因此**不进对话历史**——
#: 否则「你是在主动接话」会一直挂在上下文里，真人提问时模型还以为又该自己找话。
BANTER_HINT = (
    "（这是群里刚才的一句话，没有人在跟你说话。"
    "你主动顺口接一句，20~60 字，像群友随口搭话："
    "不要 @ 任何人、不要引用、不要连发好几条、不要解释你在做什么、"
    "也别追问一堆问题。实在接不上就聊点跟它沾边的。）"
)

_RANGE_RE = re.compile(r"\A\s*(\d+(?:\.\d+)?)\s*[-~～]\s*(\d+(?:\.\d+)?)\s*\Z")
_SPAN_RE = re.compile(r"\A\s*(\d{1,2}):(\d{2})\s*[-~～]\s*(\d{1,2}):(\d{2})\s*\Z")

#: 一轮最多处理几个群。群多的时候一轮挨个调模型会拖到几分钟、账单也难看，
#: 所以随机挑几个就够了——反正下一轮还有机会轮到别人。
MAX_GROUPS_PER_ROUND = 3

#: (群号, 'YYYY-MM-DD') -> 今天已经搭了几次。放内存就够：重启清零最多多冒一次泡，
#: 不值得为它建表。
_DAILY: dict[tuple[int, str], int] = {}


# ---------------------------------------------------------------- 配置解析


def parse_range(text: str, default: tuple[float, float] = (30.0, 60.0)) -> tuple[float, float]:
    """解析 ``"30-60"`` 这种区间（单位：分钟）。写错就用默认值，不抛异常。

    配置项写错不该让定时任务整个不注册——那会让「功能明明开了却没动静」
    变成最难查的一类故障。这里退回默认值并留一条 WARNING。
    """
    m = _RANGE_RE.match(str(text or ""))
    if not m:
        if str(text or "").strip():
            logger.warning(f"[banter] banter_minutes 格式应为 `30-60`，收到 {text!r} → 用默认 30-60")
        return default
    lo, hi = float(m.group(1)), float(m.group(2))
    if lo <= 0 or hi <= 0:
        return default
    return (lo, hi) if lo <= hi else (hi, lo)


def parse_span(text: str) -> Optional[tuple[int, int]]:
    """解析 ``"00:00-08:00"`` 成分钟数区间 ``(0, 480)``。不合法返回 ``None``。"""
    m = _SPAN_RE.match(str(text or ""))
    if not m:
        return None
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None
    a, b = h1 * 60 + m1, h2 * 60 + m2
    # 起止相同等于「空区间」而不是「全天」——写成 08:00-08:00 的人多半是笔误，
    # 按全天处理会直接把功能关死，反而更难发现
    return None if a == b else (a, b)


def parse_quiet(items: Optional[list[str]]) -> list[tuple[int, int]]:
    """把多段勿扰时段解析成分钟区间列表，丢掉写错的那些。"""
    spans: list[tuple[int, int]] = []
    for raw in items or []:
        span = parse_span(raw)
        if span is None:
            if str(raw or "").strip():
                logger.warning(f"[banter] 勿扰时段格式应为 `00:00-08:00`，忽略 {raw!r}")
            continue
        spans.append(span)
    return spans


def quiet_end(now: datetime, spans: list[tuple[int, int]]) -> Optional[datetime]:
    """``now`` 若落在某个勿扰段里，返回**该段结束**的时刻；否则 ``None``。

    跨午夜（``23:00-08:00``）时分两种情况：已经过了 23:00 → 结束在明天早上；
    还在 08:00 之前 → 结束在今天早上。
    """
    minute = now.hour * 60 + now.minute
    for a, b in spans:
        if a < b:
            if a <= minute < b:
                return now.replace(hour=b // 60, minute=b % 60, second=0, microsecond=0)
        else:
            if minute >= a:
                return (now + timedelta(days=1)).replace(
                    hour=b // 60, minute=b % 60, second=0, microsecond=0
                )
            if minute < b:
                return now.replace(hour=b // 60, minute=b % 60, second=0, microsecond=0)
    return None


def next_run_at(cfg: LLMConfig, now: datetime) -> datetime:
    """下一次搭话的时刻：``now`` + 随机间隔；落进勿扰段就顺延到该段结束。

    为什么要「顺延」而不是「到点发现是勿扰就不说」：后者会让任务在夜里每
    30~60 分钟空转一次，白天的时间点也跟着漂移。顺延之后，夜里那次直接排到
    早上，既不空转，醒来后的第一次也不会正好卡在勿扰结束的同一分钟。
    """
    lo, hi = parse_range(cfg.banter_minutes)
    spans = parse_quiet(cfg.banter_quiet)
    when = now
    for _ in range(12):
        when = when + timedelta(minutes=random.uniform(lo, hi))
        end = quiet_end(when, spans)
        if end is None:
            return when
        # 顺延到勿扰结束后再随机 0~10 分钟，避免每天固定在 08:00 整冒泡
        when = end + timedelta(minutes=random.uniform(0.0, 10.0))
        if quiet_end(when, spans) is None:
            return when
    return when


# ---------------------------------------------------------------- 素材筛选


def worth_reply(text: str) -> bool:
    """这句话值不值得接。太短、纯标点、命令都不值得。"""
    body = (text or "").strip()
    if len(body) < 2:
        return False
    # 命令不接："/music list" 这种是给人看的，不是聊天
    if body.lower().startswith(("/", "#", "!", "！", "／")):
        return False
    # 纯标点 / 纯表情字符（「哈哈」这类有汉字，会保留）
    if not any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in body):
        return False
    return True


def target_groups(cfg: LLMConfig) -> list[int]:
    """这次该在哪些群搭话：``banter_groups`` → ``llm.groups`` → 所有旁听过的群。"""
    for candidate in (cfg.banter_groups, cfg.groups):
        if candidate:
            return [int(g) for g in candidate]
    return []


def _today(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _quota_left(gid: int, day: str, limit: int) -> bool:
    if limit <= 0:
        return True
    return _DAILY.get((gid, day), 0) < limit


def _bump(gid: int, day: str) -> None:
    _DAILY[(gid, day)] = _DAILY.get((gid, day), 0) + 1
    # 顺手清掉非今天的计数，免得长期运行后这个 dict 越攒越大
    for key in [k for k in _DAILY if k[1] != day]:
        _DAILY.pop(key, None)


def reset_quota() -> None:
    """清空每日计数（网页端手动触发 / 测试用）。"""
    _DAILY.clear()


# ---------------------------------------------------------------- 主流程


async def run_banter() -> dict:
    """跑一轮主动搭话。返回一份可断言的摘要，**不抛异常**。

    返回的 ``status`` 取值：

    - ``off`` —— 总开关关着 / ``llm.enabled`` 关着；
    - ``quiet`` —— 此刻仍在勿扰时段；
    - ``no_group`` —— 没有任何可搭话的群；
    - ``no_seed`` —— 所有群都没有新鲜素材；
    - ``ok`` —— 至少发出去一条。
    """
    llm_cfg = service.config.llm
    if not llm_cfg.banter or not llm_cfg.enabled:
        return {"status": "off", "sent": 0}
    # 素材只来自旁听。旁听关着就没话可接——这里直接说明，免得每半小时跑一次
    # 只在日志里留一句「no_seed」，看的人以为出了 bug
    if not service.config.memory.listen:
        logger.info("[banter] 群记忆的「旁听群消息」没开，没有素材可用，本轮跳过")
        return {"status": "no_listen", "sent": 0}

    now = datetime.now(service.resolver.tz)
    if quiet_end(now, parse_quiet(llm_cfg.banter_quiet)) is not None:
        # next_run_at 已经避开了勿扰段，能走到这里说明配置刚被改过
        logger.info("[banter] 此刻在勿扰时段内，本轮跳过")
        return {"status": "quiet", "sent": 0}

    groups = target_groups(llm_cfg)
    if not groups:
        try:
            groups = await service.store.chat_groups()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[banter] 读取群列表失败: {exc}")
            return {"status": "no_group", "sent": 0}
    if not groups:
        return {"status": "no_group", "sent": 0}
    if len(groups) > MAX_GROUPS_PER_ROUND:
        groups = random.sample(groups, k=MAX_GROUPS_PER_ROUND)

    try:
        bot = nonebot.get_bot()
    except (ValueError, KeyError):
        logger.info("[banter] 当前没有已连接的机器人，本轮跳过")
        return {"status": "off", "sent": 0}
    if not isinstance(bot, Bot):
        return {"status": "off", "sent": 0}

    day = _today(now)
    sent = 0
    skipped = 0
    for gid in groups:
        if not _quota_left(gid, day, llm_cfg.banter_daily_max):
            skipped += 1
            continue
        try:
            row = await service.store.latest_chat(
                gid, max_age=max(60.0, llm_cfg.banter_fresh_minutes * 60.0)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[banter] 群{gid} 取最近消息失败: {exc}")
            continue
        if not row:
            skipped += 1
            continue
        text = (row.get("content") or "").strip()
        if not worth_reply(text):
            skipped += 1
            continue

        # 只改这一次请求的副本：工具关掉（省 900 token 且不该联网）、冷却清零
        # （不占被搭话者的额度，否则他紧接着 @ 机器人会被静默）、
        # 历史压到 12000 字（见 BANTER_HISTORY_CHARS）
        probe = llm_cfg.model_copy(
            update={
                "tools": False,
                "cooldown": 0,
                "max_reply_chars": BANTER_REPLY_CHARS,
                "history_chars": min(llm_cfg.history_chars or BANTER_HISTORY_CHARS,
                                     BANTER_HISTORY_CHARS),
            }
        )
        uid = int(row.get("user_id") or 0)
        nick = row.get("nick") or ""
        reply, why = await llm_bot.chat(
            gid, uid, nick, text, probe,
            context=BANTER_HINT,
            mem=service.cfg(gid).memory,
        )
        if not reply:
            logger.info(f"[banter] 群{gid} 未生成（{why}），跳过")
            skipped += 1
            continue
        if await safe_send_group(bot, gid, Message(reply)):
            _bump(gid, day)
            sent += 1
            logger.info(
                f"[banter] 群{gid} 接住 {nick}({uid}) 的「{text[:20]}」→ 回复 {len(reply)} 字"
            )
        else:
            skipped += 1

    return {"status": "ok" if sent else "no_seed", "sent": sent, "skipped": skipped}
