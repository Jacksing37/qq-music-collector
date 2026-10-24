"""主动搭话：没人叫它，也隔一阵自己在群里冒个泡。

和另外三个触发点的区别值得先说清楚：

- ``@ 机器人`` / ``戳一戳`` / 非音乐链接 —— 都是「群里发生了某件事」才回；
- 这里是**定时**的：非勿扰时段里，每隔一段随机时间挑一个点，
  把群里「上次搭话之后」聊的那一段接过去。

几道闸（缺一不可）：

- ``llm.banter`` 总开关 + ``llm.enabled`` + 生效群；
- 勿扰时段 ``llm.banter_quiet``：**可配多段**，支持跨午夜（``23:00-08:00``）；
- 没有新消息（游标之后一片空白）就不接；
- 批次里最后一条太旧（``banter_fresh_minutes``）就不接——半夜三点突然回一句
  两小时前的话，比不说话还怪；
- 太短（「哈哈」「+1」）、纯标点、命令一律不从当「接的对象」；
- **判官**（``banter_judge``）：先问一句「此刻插嘴自然吗」，吵架、严肃话题、
  别人正在对话、对话已收尾……说不该就闭嘴。判不出来也按「不接」；
- 每群每天上限 ``banter_daily_max``（``0`` = 不限）。

**素材是一段而不是一条**：从 ``_LAST_ID``（上次搭话处理到的 ``chat_log.id``）往后取，
所以读到的是「这段时间发生了什么事」。游标顺便解决了「同一句被接两次」。
判官说「不接」时**也推进游标**，否则下一轮还是这批消息，会死锁在反复白跑。

素材取自 ``chat_log(kind='listen')``，也就是旁听器攒下来的群聊。这样做的好处是
不额外占存储，代价是**依赖 ``memory.listen``**：关掉旁听就没素材，此时
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
from . import llmtrace as llmtrace_mod
from .llm import clip_tail, llm_bot
from .service import service

#: 主动搭话能带多少字的历史。**不跟 ``llm.history_chars`` 走**：
#: 那个值可能配到上百万字（真人提问时值得），但主动搭话只要「知道最近聊了啥」，
#: 带全部历史等于每句话花一块多钱。12000 字约 6000 token，够看最近几十轮。
#:
#: 注意它和「批次素材」（``banter_batch_chars``）不是一回事：历史里是
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

#: 判官的内置标准（``llm.banter_judge_hint`` 留空时用它）。
#: 之所以做成可配置，是因为「什么话题不该插嘴」只有群主知道——有的群能开玩笑，
#: 有的群在聊工作，写死在代码里必然有一半场景不合适。
DEFAULT_JUDGE_HINT = (
    "不要接：争吵或阴阳怪气；严肃话题（生病、去世、家人、成绩、钱、感情）；"
    "明显是在问某个人、或几个人正在对话；纯表情、复读、单字；广告拉人；"
    "对话已经收尾（「好的」「收到」「晚安」）。\n"
    "可以接：泛泛的感慨或吐槽、玩梗、没人接的提问、在聊某个具体话题。\n"
    "不确定时选「不接」。"
)

#: 判官的 system。**只要两个字**——判定结果要好解析，解释反而会污染输出。
JUDGE_SYSTEM = (
    "你在帮一个群聊机器人决定要不要插话。"
    "只输出「接」或「不接」两个字，不要解释、不要标点、不要任何别的字。"
)

#: 群号 -> 上次搭话处理到的 ``chat_log.id``。放内存就够了：重启后最多把最近
#: ``banter_batch_max`` 条再看一遍（可能重复接一小段），不值得为它建表。
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


# ---------------------------------------------------------------- 判官（该不该插嘴）


def judge_prompt(material: str, hint: str) -> str:
    return f"群里刚才的对话：\n{material}\n\n判定标准：\n{hint}\n\n此刻插一句话，自然吗？"


def parse_verdict(text: str) -> bool:
    """把判官的回答读成「接不接」。

    **必须先判否定词**——中文里「不接」本身就含「接」字，先判肯定的话
    会把所有拒绝都读成同意，那这个功能就是反的。解析不出来一律按不接。
    """
    body = (text or "").strip()
    if not body:
        return False
    for no in ("不接", "别接", "不要接", "不插", "别说", "闭嘴"):
        if no in body:
            return False
    return "接" in body


async def should_reply(
    cfg: LLMConfig, material: str, group_id: int = 0
) -> tuple[bool, str]:
    """问判官「此刻插一句自然吗」。返回 ``(接不接, 原因)``。

    走 ``llm.complete``：一次性、不写历史、不占冷却。**这点是关键**——如果让
    主模型自己输出「SKIP」，那次请求已经被 ``chat()`` 写进历史了，下次真人提问
    上下文里会躺着一条「助手: SKIP」。

    判不出来（空正文 / 报错 / 回一堆废话）一律按**不接**：识相的意思是宁缺毋滥，
    静默最多是少说一句，接错话是社死。

    每次判定都记进 :mod:`llmtrace`（``kind="judge"``）：否则「开了主动搭话却一直
    没动静」只能靠猜，看不出是没素材还是判官一路拦。
    """
    hint = (cfg.banter_judge_hint or "").strip() or DEFAULT_JUDGE_HINT
    # 兜一道：万一有人往判定标准里贴了几万字，每次判官请求都得带上它
    hint = hint[:3000]
    body = clip_tail(material, int(cfg.banter_judge_chars))

    trace = llmtrace_mod.start(group_id, 0, "判官", cfg, question=body, kind="judge")
    started = time.monotonic()
    verdict, why = await llm_bot.complete(
        judge_prompt(body, hint),
        cfg,
        system=JUDGE_SYSTEM,
        model=(cfg.banter_judge_model or "").strip(),
        # 只要两个字，但别卡太死：有的模型会带个标点或先说半句，
        # 8 个 token 容易把「不接」本身也截掉，那就变成「判不出来」了
        max_tokens=16,
        temperature=0.0,
    )
    if not verdict:
        logger.warning(f"[banter] 判官没回话（{why}）→ 按不接处理")
        llmtrace_mod.step(trace, f"⚠️ 判官没有回话（{why}）→ 按不接")
        llmtrace_mod.finish(
            trace, error=f"judge_empty({why})",
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
        return False, f"judge_empty({why})"

    ok = parse_verdict(verdict)
    llmtrace_mod.step(trace, f"判定：{'接' if ok else '不接'}")
    llmtrace_mod.finish(
        trace, reply=verdict.strip(),
        elapsed_ms=(time.monotonic() - started) * 1000,
    )
    return ok, f"判官「{verdict.strip()[:8]}」"


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
    - ``no_seed`` —— 所有群都没有可接的素材（没新消息 / 太旧 / 全是短句）；
    - ``ok`` —— 至少发出去一条。

    摘要里还带 ``skipped``（跳过的群数）和 ``declined``（判官说别插嘴的群数）——
    后者为 0 而 ``sent`` 也为 0 时，说明是「没素材」而不是「识相闭嘴」，
    排查起来不至于混为一谈。
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
    declined = 0
    fresh = max(60.0, llm_cfg.banter_fresh_minutes * 60.0)
    for gid in groups:
        if not _quota_left(gid, day, llm_cfg.banter_daily_max):
            skipped += 1
            continue

        # 取「上次搭话之后」的**所有**群消息，而不是只看最新一条：
        # 只给一句，模型只能机械附和；给一段，它才接得住话题。
        try:
            rows = await service.store.recent_chat(
                gid,
                after_id=_LAST_ID.get(gid, 0),
                limit=int(llm_cfg.banter_batch_max),
                max_chars=int(llm_cfg.banter_batch_chars),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[banter] 群{gid} 取最近消息失败: {exc}")
            continue
        if not rows:
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
            logger.info(f"[banter] 群{gid} 最后一条已是 {age / 60:.0f} 分钟前，这批凉了，跳过")
            skipped += 1
            continue

        # 先做免费的粗筛：整批都够不上（全是「嗯」「哈哈」「/命令」）就直接跳过，
        # 省一次判官调用。判官只负责「语义上该不该说」，不管这种机械过滤。
        idx = -1
        for i in range(len(rows) - 1, -1, -1):
            if worth_reply(str(rows[i].get("content") or "")):
                idx = i
                break
        if idx < 0:
            _advance(gid, last_id)
            skipped += 1
            continue

        material = format_batch(rows, service.resolver.tz)

        # 判官再说该不该插嘴。判「不接」**也要推进游标**——否则下一轮还是这批
        # 消息，判官大概率还说不接，就死锁在那儿反复白跑。
        if llm_cfg.banter_judge:
            ok, verdict_why = await should_reply(llm_cfg, material, gid)
            if not ok:
                _advance(gid, last_id)
                declined += 1
                logger.info(
                    f"[banter] 群{gid} 决定不插嘴（{verdict_why}）："
                    f"{len(rows)} 条 / {len(material)} 字"
                )
                skipped += 1
                continue

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
            context=context,
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

    return {
        "status": "ok" if sent else "no_seed",
        "sent": sent,
        "skipped": skipped,
        "declined": declined,
    }
