"""主动搭话：没人叫它，也隔一阵自己在群里冒个泡。

和另外三个触发点的区别值得先说清楚：

- ``@ 机器人`` / ``戳一戳`` / 非音乐链接 —— 都是「群里发生了某件事」才回；
- 这里是**定时**的：非勿扰时段里按 ``speak_check_minutes`` 取一个点，
  把群里「上次搭话之后」聊的那一段接过去。

**要不要开口由 ``speak`` 的概率模型决定**（2026-11-05 合并）：同一套 heat/engage
状态既管「非音乐链接要不要接」，也管这里。原来另有一套「随机间隔 + 每日上限 +
LLM 判官」的独立逻辑，现在只保留调度必需的部分：

- ``llm.speak_proactive`` 总开关 + ``llm.enabled`` + ``llm.groups`` 生效群；
- 勿扰时段 ``llm.speak_quiet``：**可配多段**，支持跨午夜（``23:00-08:00``）；
- 没有新消息（游标之后一片空白）就不接；
- 批次里最后一条太旧（``speak_fresh_minutes``）就不接——半夜三点突然回一句
  两小时前的话，比不说话还怪；
- 太短（「哈哈」「+1」）、纯标点、命令一律不当「接的对象」；
- **概率门** ``speak.decide_speak(gid, "banter")``：群正热闹、或刚聊完，这一轮就
  不冒泡；``llm.speak_enabled`` 关着时它恒放行（零行为变化）；
- 每群每天上限 ``speak_daily_max``（``0`` = 不限）。

⚠️ **LLM 判官已删除**（2026-11-05）：以前每轮多花一次模型调用问「此刻插嘴自然吗」，
现在频率完全交给概率模型；语义质量靠 ``BANTER_HINT`` 与概率门共同兜。

**素材是一段而不是一条**：从 ``_LAST_ID``（上次处理到的 ``chat_log.id``）往后取，
读到的是「这段时间发生了什么事」。游标顺便解决「同一句被接两次」；跳过时也推进游标，
否则下一轮还是这批消息，会死锁在反复白跑。

素材取自 ``chat_log(kind='listen')``，**依赖 ``memory.listen``**：关掉旁听就没素材，
:func:`run_banter` 会在日志里说明原因，而不是悄悄什么都不做。
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import nonebot
from nonebot.adapters.onebot.v11 import Bot, Message
from nonebot.log import logger

from .bot_utils import safe_send_group
from .config import LLMConfig
from . import speak as speak_mod
from .llm import clip_tail, llm_bot
from .service import service

#: 主动搭话能带多少字的历史。**不跟 ``llm.history_chars`` 走**：
#: 那个值可能配到上百万字（真人提问时值得），但主动搭话只要「知道最近聊了啥」，
#: 带全部历史等于每句话花一块多钱。12000 字约 6000 token，够看最近几十轮。
#:
#: 注意它和「批次素材」（``speak_batch_chars``）不是一回事：历史里是
#: ``kind='chat'``（跟机器人的问答），素材是 ``kind='listen'``（群友闲聊），
#: 两者互补，不重复。
BANTER_HISTORY_CHARS = 12000

#: 主动搭话的回复上限。冒泡不该写小作文。
BANTER_REPLY_CHARS = 120

#: 拼给模型的「背景素材」总预算。比 ``llm.CONTEXT_CHAR_LIMIT``（4000）略小，
#: 留点余量给说明文字——那个上限是**截开头**的，靠它裁剪会把最新的话扔掉。
_CONTEXT_BUDGET = 3900

#: 送给模型的说明。走 ``chat(context=...)`` 传，因此**不进对话历史**——
#: 否则「你是在主动接话」会一直挂在上下文里，真人提问时模型还以为又该自己找话。
BANTER_HINT = (
    "（下面是群里刚才的一段对话，没有人在跟你说话。"
    "你顺着最后那句话主动接一句：20~60 字，像群友随口搭话。"
    "不要 @ 任何人、不要引用、不要连发好几条、不要解释你在做什么、也别追问一堆问题。"
    "前面那些是背景，挑最自然的那个点接，不用逐句回应。）"
)

#: 群号 -> 上次搭话处理到的 ``chat_log.id``。放内存就够了：重启后最多把最近
#: ``speak_batch_max`` 条再看一遍（可能重复接一小段），不值得为它建表。
#:
#: 它同时解决「同一句被接两次」——推进游标之后，接过的那批不会再被捞出来。
_LAST_ID: dict[int, int] = {}

_RANGE_RE = re.compile(r"\A\s*(\d+(?:\.\d+)?)\s*[-~～]\s*(\d+(?:\.\d+)?)\s*\Z")
_SPAN_RE = re.compile(r"\A\s*(\d{1,2}):(\d{2})\s*[-~～]\s*(\d{1,2}):(\d{2})\s*\Z")

#: 一轮最多处理几个群。群多的时候一轮挨个调模型会拖到几分钟、账单也难看，
#: 所以随机挑几个就够了——反正下一轮还有机会轮到别人。
MAX_GROUPS_PER_ROUND = 3

#: (群号, 'YYYY-MM-DD') -> 今天已经搭了几次。放内存就够：重启清零最多多冒一次泡，
#: 不值得为它建表。
_DAILY: dict[tuple[int, str], int] = {}

#: 群号 -> 这一刻之前不许再主动搭话（epoch 秒）。
#:
#: **为什么需要**：它刚被 @ 完、在群里答了一句，紧接着又自己冒个泡，看着像抢话；
#: 而且它刚说完那么一两句，可接的素材也还没跟上。所以被动回复完就把这个群的
#: 计时**重新起算**（见 :func:`note_reply`），间隔仍取 ``speak_check_minutes`` 那一段。
_NEXT_AT: dict[int, float] = {}


# ---------------------------------------------------------------- 配置解析


def parse_range(text: str, default: tuple[float, float] = (30.0, 60.0)) -> tuple[float, float]:
    """解析 ``"30-60"`` 这种区间（单位：分钟）。写错就用默认值，不抛异常。

    配置项写错不该让定时任务整个不注册——那会让「功能明明开了却没动静」
    变成最难查的一类故障。这里退回默认值并留一条 WARNING。
    """
    m = _RANGE_RE.match(str(text or ""))
    if not m:
        if str(text or "").strip():
            logger.warning(f"[banter] speak_check_minutes 格式应为 `30-60`，收到 {text!r} → 用默认 30-60")
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
    lo, hi = parse_range(cfg.speak_check_minutes)
    spans = parse_quiet(cfg.speak_quiet)
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


# ---------------------------------------------------------------- 批次素材


def format_batch(rows: list[dict], tz) -> str:
    """把一批旁听消息拼成「一段对话」。

    带时间戳和昵称，是为了让模型看出「谁在跟谁说、隔了多久」——没有这些，
    几十行文字糊在一起，它会把不相干的两句话当成对话的上下句。
    """
    lines: list[str] = []
    for row in rows:
        try:
            ts = datetime.fromtimestamp(float(row["created_at"]), tz).strftime("%H:%M")
        except (KeyError, TypeError, ValueError, OSError):
            ts = "--:--"
        nick = (str(row.get("nick") or "").strip() or "群友")[:20]
        uid = row.get("user_id") or 0
        # 换行压成空格：一条消息占一行，模型才容易数清「有几个人在说话」
        body = str(row.get("content") or "").strip().replace("\n", " ")
        lines.append(f"[{ts}] {nick}({uid})：{body}")
    return "\n".join(lines)


def _advance(group_id: int, last_id: int) -> None:
    """把游标推到 ``last_id``（只前进不后退）。"""
    if last_id > _LAST_ID.get(int(group_id), 0):
        _LAST_ID[int(group_id)] = int(last_id)


def reset_cursor(group_id: Optional[int] = None) -> None:
    """清掉搭话游标（测试用；也方便以后加「重新开始」的按钮）。"""
    if group_id is None:
        _LAST_ID.clear()
    else:
        _LAST_ID.pop(int(group_id), None)


def target_groups(cfg: LLMConfig) -> list[int]:
    """这次该在哪些群开口：直接用 ``llm.groups``（留空 = 所有旁听过的群）。

    合并进 speak 之后不再单独有个「搭话群」旋钮——两套群白名单只会让人改错。
    """
    return [int(g) for g in (cfg.groups or [])]


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


# ---------------------------------------------------------------- 计时

def _gap_minutes(cfg: LLMConfig) -> float:
    """一次随机间隔（分钟）。和 :func:`next_run_at` 取同一段区间。"""
    lo, hi = parse_range(cfg.speak_check_minutes)
    return random.uniform(lo, hi)


def _set_gate(group_id: int, minutes: float) -> None:
    _NEXT_AT[int(group_id)] = time.time() + max(0.0, minutes) * 60.0


def note_reply(group_id: int) -> None:
    """被动回复完，把这个群的搭话计时**重新起算**，并把定时任务挪到那之后。

    三个被动入口（@ / 戳一戳 / 非音乐链接）都走 ``_llm_answer``，所以调用点只
    需要那一处。间隔仍取 ``speak_check_minutes``——「重新计算」的意思是「从现在起再等
    一个间隔」，不是把下一次提前。

    不这样做的话：它 22:00 刚答完人，22:01 又被排到的那一轮捞起来自己冒泡，
    群里看着就是「抢话」；而且它刚说完那一两句，可接的素材也还没跟上。
    """
    gid = int(group_id or 0)
    if not gid:
        return
    cfg = service.config.llm
    # 合并进 speak 后「被动回复后重算计时」恒为开（原来的 banter_reset_on_reply 旋钮
    # 已删）：它防的是「刚答完又自己冒泡」这种抢话，没有关掉的理由。
    if not cfg.speak_proactive:
        return
    gap = _gap_minutes(cfg)
    _set_gate(gid, gap)
    logger.debug(f"[banter] 群{gid} 被动回复完，搭话计时重新起算（{gap:.0f} 分钟后）")
    # 定时任务得跟着挪：它原本可能几分钟后就响，那时这个群还在冷却里，会白跑一轮
    try:
        from .scheduler import reschedule_banter  # 延迟导入：scheduler 模块级依赖本模块

        reschedule_banter()
    except Exception as exc:  # noqa: BLE001 — 排不上最多晚一轮，绝不能影响回复
        logger.warning(f"[banter] 重新排期失败: {exc}")


def next_gate_at(now: Optional[datetime] = None) -> Optional[datetime]:
    """最早一个「解禁」的时刻；没有任何群在冷却里就返回 ``None``。"""
    tz = service.resolver.tz
    now = now or datetime.now(tz)
    future = [v for v in _NEXT_AT.values() if v > now.timestamp()]
    if not future:
        return None
    return datetime.fromtimestamp(min(future), tz)


def waiting(group_id: int, now: Optional[float] = None) -> bool:
    """这个群是不是还在「刚回过话」的冷却里。"""
    due = _NEXT_AT.get(int(group_id or 0))
    return bool(due and (time.time() if now is None else now) < due)


def reset_schedule() -> None:
    """清空计时（网页端改配置 / 测试用）。"""
    _NEXT_AT.clear()


# ---------------------------------------------------------------- 主流程


async def run_banter() -> dict:
    """跑一轮主动搭话。返回一份可断言的摘要，**不抛异常**。

    返回的 ``status`` 取值：

    - ``off`` —— 总开关关着 / ``llm.enabled`` 关着；
    - ``quiet`` —— 此刻仍在勿扰时段；
    - ``no_group`` —— 没有任何可搭话的群；
    - ``empty`` / ``stale`` / ``gate`` / ``short`` / ``quota`` / ``cooldown`` ——
      一个群都没发出去时，取**最主要的原因**：没新消息 / 素材太旧 / 概率门拦下 /
      整批不够格 / 当天配额用尽 / 刚回过话还在冷却；
    - ``ok`` —— 至少发出去一条。

    摘要里还带 ``skipped``（跳过的群数）：``sent`` 为 0 时看它就知道是没素材
    还是被概率门/勿扰/上限挡掉了（日志里各有对应的一句）。
    """
    llm_cfg = service.config.llm
    if not llm_cfg.speak_proactive or not llm_cfg.enabled:
        return {"status": "off", "sent": 0}
    # 素材只来自旁听。旁听关着就没话可接——这里直接说明，免得每半小时跑一次
    # 只在日志里留一句泛泛的状态，看的人以为出了 bug
    if not service.config.memory.listen:
        logger.info("[banter] 群记忆的「旁听群消息」没开，没有素材可用，本轮跳过")
        return {"status": "no_listen", "sent": 0}

    now = datetime.now(service.resolver.tz)
    if quiet_end(now, parse_quiet(llm_cfg.speak_quiet)) is not None:
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
    # 为什么没说话，分门别类记下来。以前统一返回 ``no_seed``，结果
    # 「群里没人说话」「素材太旧」「概率门拦下」「配额用尽」在日志里长得一模一样，
    # 排查「怎么一句都不说」时只能靠猜（2026-11-05 就是这么踩的）
    reasons = {"quota": 0, "cooldown": 0, "empty": 0, "stale": 0, "gate": 0, "short": 0}
    fresh = max(60.0, llm_cfg.speak_fresh_minutes * 60.0)
    for gid in groups:
        if not _quota_left(gid, day, llm_cfg.speak_daily_max):
            reasons["quota"] += 1
            skipped += 1
            continue
        # 刚在群里回过话，这一轮先不插嘴——计时已按被动回复那一刻重算过
        if waiting(gid):
            reasons["cooldown"] += 1
            skipped += 1
            continue

        # 取「上次搭话之后」的**所有**群消息，而不是只看最新一条：
        # 只给一句，模型只能机械附和；给一段，它才接得住话题。
        try:
            rows = await service.store.recent_chat(
                gid,
                after_id=_LAST_ID.get(gid, 0),
                limit=int(llm_cfg.speak_batch_max),
                max_chars=int(llm_cfg.speak_batch_chars),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[banter] 群{gid} 取最近消息失败: {exc}")
            continue
        if not rows:
            logger.info(f"[banter] 群{gid} 上次开口之后没有新消息")
            reasons["empty"] += 1
            skipped += 1
            continue

        last_id = int(rows[-1].get("id") or 0)
        try:
            age = time.time() - float(rows[-1]["created_at"])
        except (KeyError, TypeError, ValueError):
            age = 0.0
        # 判的是**最后一条**而不是第一条：聊了半小时的对话，只要刚还在继续就值得接
        if age > fresh:
            _advance(gid, last_id)
            logger.info(
                f"[banter] 群{gid} 最后一条已是 {age / 60:.0f} 分钟前"
                f"（阈值 {llm_cfg.speak_fresh_minutes:.0f} 分钟），这批凉了，跳过"
            )
            reasons["stale"] += 1
            skipped += 1
            continue

        # 说话时机：概率门排在**最前面**。它替代的是「定时器 + 随机间隔」这一层——
        # 群里正热闹、或刚才那阵子已经聊完了，这一轮就不冒泡。
        # 概率门之后还剩一道**免费粗筛**（太长/命令/纯符号）：不需要模型、几乎零成本。
        # LLM 判官已于 2026-11-05 删除——频率交给概率，别再多花一次调用。
        #
        # 素材取最后一条：它才是「现在的话题」，也是 age / fresh 判定的那一条
        speak_ok, _speak_p = speak_mod.decide_speak(
            gid,
            "banter",
            str(rows[-1].get("content") or ""),
            str(rows[-1].get("nick") or ""),
        )
        if not speak_ok:
            # 拦下原本只记 DEBUG（热群里链接太多怕刷屏），但主动搭话每 30~60 分钟
            # 才一次，必须看得见——这是「群里怎么一句都不说」最常见的原因。
            # ⚠️ 只报 p，别解释成「群正热闹」：p 高（如 0.7）恰恰说明群很冷、
            # 只是这次掷骰没过。上一次就是被这句误导的。
            logger.info(f"[banter] 群{gid} 说话时机掷骰未过（p={_speak_p:.2f}）")
            reasons["gate"] += 1
            skipped += 1
            continue

        # 先做免费的粗筛：整批都够不上（全是「嗯」「哈哈」「/命令」）就直接跳过。
        idx = -1
        for i in range(len(rows) - 1, -1, -1):
            if worth_reply(str(rows[i].get("content") or "")):
                idx = i
                break
        if idx < 0:
            logger.info(f"[banter] 群{gid} 整批都不够格（太短/纯符号/命令）")
            _advance(gid, last_id)
            reasons["short"] += 1
            skipped += 1
            continue

        material = format_batch(rows, service.resolver.tz)

        # 接的是「最后一条有内容的」，前面那些当背景。
        # 这样历史里存的是一句自然的话，而不是一整段带时间戳的群聊记录。
        text = str(rows[idx].get("content") or "").strip()
        earlier_text = format_batch(rows[:idx], service.resolver.tz) if idx > 0 else ""
        # 预算要自己算：``with_context`` 超长时截的是**开头**（保留前 4000 字），
        # 而素材是正序、最旧的在前面——照它截会把最新的那段扔掉，正好接不上。
        # 所以先给 BANTER_HINT 留位置，再把背景按「保留最近」截到剩余预算里。
        earlier = clip_tail(earlier_text, max(0, _CONTEXT_BUDGET - len(BANTER_HINT) - 40))
        context = BANTER_HINT + (f"\n\n刚才群里还在聊这些：\n{earlier}" if earlier else "")

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
        uid = int(rows[idx].get("user_id") or 0)
        nick = rows[idx].get("nick") or ""
        reply, why = await llm_bot.chat(
            gid, uid, nick, text, probe,
            # 走 background 而不是 context：日志页要能看见这段素材（它当时读到哪几句），
            # 只写「附参考资料 600 字」没法判断接错话是素材问题还是模型想歪了
            background=context,
            mem=service.cfg(gid).memory,
        )
        if not reply:
            logger.info(f"[banter] 群{gid} 未生成（{why}），跳过")
            skipped += 1
            continue
        if await safe_send_group(bot, gid, Message(reply)):
            _bump(gid, day)
            # 发出去了才推进游标；发送失败不推，下一轮还能再试这批
            _advance(gid, last_id)
            sent += 1
            logger.info(
                f"[banter] 群{gid} 接住 {nick}({uid}) 的「{text[:20]}」"
                f"（这批 {len(rows)} 条）→ 回复 {len(reply)} 字"
            )
        else:
            skipped += 1

    if sent:
        status = "ok"
    elif any(reasons.values()):
        # 取最主要的原因，日志里那行「本轮没发（gate）」就直接说明问题在哪
        status = max(reasons, key=lambda k: reasons[k])
    else:
        status = "ok"
    return {
        "status": status,
        "sent": sent,
        "skipped": skipped,
        "why": reasons,
    }
