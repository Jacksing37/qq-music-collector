"""主动搭话：没人叫它，也顺着群里的聊天插一句。

**2026-11-05 起是事件驱动的**：每来一条群消息算一次「现在插一句合不合适」，
而不是「每隔半小时醒来掷一次骰子」。区别很实在：

- 定时轮询醒来时，素材只有两种状态，两头都不好用——刚聊完（概率门让位）、
  或者聊完三四十分钟了（接一句「诈尸」）。中间那个理想窗口很难撞上。
- 事件驱动永远在「刚刚有人说话」的那一刻判定，接的就是眼前这句话。

三件事分开：

- ``speak`` 的概率模型决定**这一刻该不该开口**（群多热闹 / 有没有人在跟它说话）；
- 这里决定**素材与礼貌**：勿扰、每日上限、最小间隔、值不值得接；
- ``llm_bot.chat`` 负责生成。

几道闸（缺一不可）：

- ``llm.speak_proactive`` + ``llm.speak_enabled`` + ``llm.enabled`` 三个开关；
- 群范围 ``llm.groups``（留空 = 所有群）；
- 勿扰时段 ``llm.speak_quiet``：**可配多段**，支持跨午夜（``23:00-08:00``）；
- 每群每天上限 ``llm.speak_daily_max``（``0`` = 不限）；
- 最小间隔 ``llm.speak_min_gap_minutes``：两次主动开口之间至少隔这么久；
  被动回复完也会重新起算（否则刚答完话又自己冒泡，群里看着像抢话）；
- 太短（「哈哈」「+1」）、纯标点、命令一律不当「接的对象」；
- 概率门 ``speak.decide_speak(gid, "banter")``：群正热闹就让位。

⚠️ 被 @ / 戳一戳 / 非音乐链接走各自的路，不经过这里——这三条是「有人叫它」。

**素材是一段而不是一条**：附近消息拼成「一段对话」当背景，模型才知道在聊什么；
接的对象是**触发这次判定的那句话**。素材取自 ``chat_log(kind='listen')``，
**依赖 ``memory.listen``**：关掉旁听就没有背景可用。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Optional

from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger

from . import speak as speak_mod
from . import timeguard as timeguard_mod
from .bot_utils import send_reply_parts
from .config import LLMConfig
from .llm import clip_tail, llm_bot
from .service import service

#: 主动搭话的回复上限。冒泡不该写小作文。
BANTER_REPLY_CHARS = 120

#: 拼给模型的「背景素材」总预算。比 ``llm.CONTEXT_CHAR_LIMIT``（4000）略小，
#: 留点余量给说明文字——那个上限是**截开头**的，靠它裁剪会把最新的话扔掉。
_CONTEXT_BUDGET = 3900

#: 送给模型的说明。走 ``chat(background=...)`` 传，因此**不进对话历史**——
#: 否则「你是在主动接话」会一直挂在上下文里，真人提问时模型还以为又该自己找话。
#:
#: ⚠️ 「想停顿就断句」是 2026-11-06 加的，措辞跟着拆条规则走：发送层**只按空行分条**，
#: 所以这里必须明确要模型用空行隔开，否则它写一段逗号长句、我们一条发出去。
#: 以前这里写的是「不要连发好几条」，和发送层打架（那句是拆条还不存在时定的）。
BANTER_HINT = (
    "（下面是群里刚才的一段对话，没有人在跟你说话。标着「你：」的是你自己之前说的话。"
    "你顺着最后那句话主动接一句：20~60 字，像群友随口搭话。"
    "想停顿就断句——用空行分成几条（一条一句），别写成一大段小作文；"
    "不要 @ 任何人、不要引用、不要解释你在做什么、也别追问一堆问题。"
    "前面那些是背景，挑最自然的那个点接，不用逐句回应。）"
)

#: 群号 -> 上次成功开口时处理到的 ``chat_log.id``，用来限制背景长度
#: （放内存就够：重启后最多多带一段背景，不值得为它建表）
_LAST_ID: dict[int, int] = {}

#: (群号, 日期) -> 今天已经开口几次
_DAILY: dict[tuple[int, str], int] = {}

#: 群号 -> 解禁时间戳（秒）。既管「两次主动开口的间隔」，也管「刚被动回复完别插嘴」
_NEXT_AT: dict[int, float] = {}

#: 群号 -> 正在生成中。防止两条消息同时触发、各生成一条
_BUSY: set[int] = set()

_SPAN_RE = re.compile(r"\A\s*(\d{1,2}):(\d{2})\s*[-~～]\s*(\d{1,2}):(\d{2})\s*\Z")


# ---------------------------------------------------------------- 配置解析


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
        if str(row.get("kind") or "") == "say":
            # 自己发的气泡：标成「你」，不然模型会把它当成别人说的话去接
            lines.append(f"[{ts}] 你：{body}")
            continue
        lines.append(f"[{ts}] {nick}({uid})：{body}")
    return "\n".join(lines)


def _advance(group_id: int, last_id: int) -> None:
    """把游标推到 ``last_id``（只前进不后退）。"""
    if last_id > _LAST_ID.get(int(group_id), 0):
        _LAST_ID[int(group_id)] = int(last_id)


def reset_cursor(group_id: Optional[int] = None) -> None:
    """清掉游标（测试用）。"""
    if group_id is None:
        _LAST_ID.clear()
    else:
        _LAST_ID.pop(int(group_id), None)


def in_scope(cfg: LLMConfig, group_id: int) -> bool:
    """这个群是否在生效范围内：``llm.groups`` 留空 = 所有群。

    不再单独有「搭话群」旋钮——两套群白名单只会让人改错。
    """
    groups = [int(g) for g in (cfg.groups or [])]
    return not groups or int(group_id) in groups


# ---------------------------------------------------------------- 配额


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


# ---------------------------------------------------------------- 冷却


def _gap_seconds(cfg: LLMConfig) -> float:
    """两次主动开口之间至少要隔多久（秒）。"""
    return max(0.0, float(cfg.speak_min_gap_minutes)) * 60.0


def _set_gate(group_id: int, seconds: float) -> None:
    _NEXT_AT[int(group_id)] = time.time() + max(0.0, seconds)


def note_reply(group_id: int) -> None:
    """被动回复完（@ / 戳一戳 / 非音乐链接），先安静一会儿再插话。

    三个被动入口都走 ``_llm_answer``，所以调用点只需要那一处。
    不这样做的话：它刚答完人，紧接着群里又有人说了一句，它 20 秒后又自己冒泡，
    群里看着就是「抢话」；而且刚说完那一两句，可接的素材也还没跟上。
    """
    gid = int(group_id or 0)
    if not gid:
        return
    cfg = service.config.llm
    if not cfg.speak_proactive:
        return
    _set_gate(gid, _gap_seconds(cfg))
    logger.debug(
        f"[banter] 群{gid} 被动回复完，{cfg.speak_min_gap_minutes:.0f} 分钟内不再插话"
    )


def next_gate_at(now: Optional[datetime] = None) -> Optional[datetime]:
    """最早一个「解禁」的时刻；没有任何群在冷却里就返回 ``None``。"""
    tz = service.resolver.tz
    now = now or datetime.now(tz)
    future = [v for v in _NEXT_AT.values() if v > now.timestamp()]
    if not future:
        return None
    return datetime.fromtimestamp(min(future), tz)


def waiting(group_id: int, now: Optional[float] = None) -> bool:
    """这个群是不是还在冷却里，或者正在生成。"""
    gid = int(group_id or 0)
    if gid in _BUSY:
        return True
    due = _NEXT_AT.get(gid)
    return bool(due and (time.time() if now is None else now) < due)


def reset_schedule() -> None:
    """清空冷却（网页端改配置 / 测试用）。"""
    _NEXT_AT.clear()
    _BUSY.clear()


# ---------------------------------------------------------------- 主流程


def _gates(gid: int, nick: str, text: str) -> dict:
    """把所有「便宜的门」+ 概率门过一遍。返回 ``{"status": ...}``，``ok`` 才该发。

    抽出来是为了让网页能问一句「现在插一句会被哪道门挡住」——
    2026-11-05 排查「群里怎么一句都不说」时，缺的就是这个。
    """
    gid = int(gid or 0)
    cfg = service.config.llm

    # ---- 便宜的门先过一遍，一次 API 都不花 ----
    if not (cfg.enabled and cfg.speak_proactive and cfg.speak_enabled):
        return {"status": "off"}
    if not gid or not in_scope(cfg, gid):
        return {"status": "no_group"}
    if not service.config.memory.listen:
        # 没旁听就没有背景可接；说清楚原因，免得看起来像坏了
        logger.debug("[banter] 群记忆的「旁听群消息」没开，没有背景可用")
        return {"status": "no_listen"}
    now = datetime.now(service.resolver.tz)
    if quiet_end(now, parse_quiet(cfg.speak_quiet)) is not None:
        return {"status": "quiet"}
    day = _today(now)
    if not _quota_left(gid, day, cfg.speak_daily_max):
        return {"status": "quota"}
    if waiting(gid):
        return {"status": "cooldown"}
    if not worth_reply(text):
        return {"status": "short"}

    # ---- 概率门：这一刻该不该开口 ----
    speak_ok, p = speak_mod.decide_speak(gid, "banter", text, nick)
    if not speak_ok:
        # 拦下原本只记 DEBUG（热群里链接太多怕刷屏），但主动搭话一次判定很珍贵，
        # 必须看得见。⚠️ 只报 p，别解释成「群正热闹」——p 高恰恰说明群很冷，
        # 只是这次掷骰没过。
        logger.info(f"[banter] 群{gid} 说话时机掷骰未过（p={p:.2f}）")
        return {"status": "gate", "p": p}
    return {"status": "ok", "p": p}


def probe(group_id: int, nick: str = "自检", text: str = "自检：现在插一句会被哪道门挡住") -> dict:
    """**只读**自检：完整过一遍门控（含概率门），但不生成、不发送、不记账。

    注意概率门是**掷骰**的，同一个状态连问两次可能一次 ok 一次 gate——
    要看「理论上限」得连问十几次，或看返回的 ``p``。
    """
    gid = int(group_id or 0)
    try:
        res = dict(_gates(gid, nick, text))
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "why": str(exc)[:120]}
    res["group"] = gid
    res["quiet"] = list(service.config.llm.speak_quiet)
    res["daily_max"] = int(service.config.llm.speak_daily_max)
    res["min_gap"] = float(service.config.llm.speak_min_gap_minutes)
    res["cooling_until"] = (next_gate_at().strftime("%H:%M:%S")
                            if next_gate_at() else None)
    res["sent_today"] = _DAILY.get((gid, _today(datetime.now(service.resolver.tz))), 0)
    return res


async def tick(
    bot: Bot, group_id: int, user_id: int, nick: str, text: str
) -> dict:
    """**每来一条群消息**调一次：现在插一句合不合适？返回可断言的摘要，不抛异常。

    ``status`` 取值：

    - ``off`` —— 三个开关任一关着；
    - ``no_group`` —— 这个群不在 ``llm.groups`` 里；
    - ``no_listen`` —— 没开旁听，没有背景素材；
    - ``quiet`` —— 此刻在勿扰时段；
    - ``quota`` —— 今天这个群说够了；
    - ``cooldown`` —— 刚说过话 / 刚被动回过话 / 正在生成；
    - ``short`` —— 这句话不值得接（太短 / 命令 / 纯符号）；
    - ``gate`` —— 概率门拦下（群正热闹，让位）；
    - ``empty`` —— 生成失败或没发出去；
    - ``ok`` —— 发出去了。
    """
    gid = int(group_id or 0)
    cfg = service.config.llm
    gates = _gates(gid, nick, text)
    if gates["status"] != "ok":
        return gates
    day = _today(datetime.now(service.resolver.tz))

    # 先占位再干活：生成要几秒，期间又来一条消息不能重复开口
    _BUSY.add(gid)
    _set_gate(gid, _gap_seconds(cfg))
    try:
        return await _generate(bot, gid, int(user_id or 0), nick, text, cfg, day)
    except Exception as exc:  # noqa: BLE001 — 冒泡失败绝不能影响别的消息处理
        logger.warning(f"[banter] 群{gid} 生成/发送异常: {exc}")
        return {"status": "empty", "why": str(exc)[:120]}
    finally:
        _BUSY.discard(gid)


async def _generate(
    bot: Bot,
    gid: int,
    uid: int,
    nick: str,
    text: str,
    cfg: LLMConfig,
    day: str,
) -> dict:
    """把「这段对话 + 这句话」交给模型，发出去就记账。"""
    rows: list[dict] = []
    try:
        rows = await service.store.recent_chat(
            gid,
            after_id=_LAST_ID.get(gid, 0),
            limit=int(cfg.speak_batch_max),
            max_chars=int(cfg.speak_batch_chars),
            # 自己之前发的气泡也算「这段对话」的一部分，否则它接话时看不见自己说过什么
            include_self=True,
        )
    except Exception as exc:  # noqa: BLE001 — 没有背景也能接，别因此不发
        logger.warning(f"[banter] 群{gid} 取背景失败（照常接）: {exc}")

    # 背景是「这段对话」，但**接的对象是触发这次判定的那句话**——如果它已经落库
    # （正常情况：旁听监听器 priority=2 先写），就从背景里剔掉，别让模型以为
    # 那句是别人说的。判据是正文相同而不是「最后一行」：消息没落库时（比如测试
    # 直接调 tick）最后一行就是真正的背景，按位置剔会把它一起丢掉。
    bg_rows = rows
    if rows and str(rows[-1].get("content") or "").strip() == text.strip():
        bg_rows = rows[:-1]
    earlier_text = format_batch(bg_rows, service.resolver.tz)
    # 预算自己算：``with_context`` 超长时截的是**开头**，而素材是正序、最旧的在前，
    # 照它截会把最新的那段扔掉，正好接不上
    earlier = clip_tail(earlier_text, max(0, _CONTEXT_BUDGET - len(BANTER_HINT) - 40))
    context = BANTER_HINT + (f"\n\n刚才群里还在聊这些：\n{earlier}" if earlier else "")

    # 只改这一次请求的副本：工具关掉（省 900 token 且不该联网）、冷却清零
    # （不占群友额度，否则他紧接着 @ 机器人会被静默）、回复上限压到 120 字
    probe = cfg.model_copy(
        update={
            "tools": False,
            "cooldown": 0,
            "max_reply_chars": BANTER_REPLY_CHARS,
        }
    )
    reply, why = await llm_bot.chat(
        gid,
        uid,
        nick,
        text,
        probe,
        # 走 background 而不是 context：日志页要能看见这段素材（它当时读到哪几句），
        # 只写「附参考资料 600 字」没法判断接错话是素材问题还是模型想歪了
        background=context,
        mem=service.cfg(gid).memory,
    )
    if not reply:
        logger.info(f"[banter] 群{gid} 未生成（{why}），跳过")
        return {"status": "empty", "why": why}

    # 时间感兜底：白天冒出一句「今晚 / 晚安」同样要改写。放在拆条**之前**——
    # 拆条会按换行/标点切开，之后改的就只是其中一段（与被动回复同一处顺序）
    reply = await timeguard_mod.fix(
        reply, datetime.now(service.resolver.tz), enabled=cfg.time_guard
    )

    # 拆条与发送和被动回复共用一个出口：``llm.reply_split`` 关掉时原样一条，
    # 打开时按换行分条、段间按 ``reply_split_interval`` 停顿。主动搭话不 @ 人，
    # 也不要正文前那个空格（那是被动回答的历史行为）
    delivered, total, parts = await send_reply_parts(
        bot, gid, reply, cfg, pad_first=False
    )
    if delivered < total:
        logger.warning(
            f"[banter] 群{gid} 回复未送达（{delivered}/{total} 段成功），本轮不记账"
        )
        return {"status": "empty", "why": "send_failed"}

    # 自己发的气泡也记一行（kind='say'）：下一轮接话/被 @ 时，背景里能看到它说过什么
    await llm_bot.record_said(gid, parts)

    _bump(gid, day)
    if rows:
        _advance(gid, int(rows[-1].get("id") or 0))
    logger.info(
        f"[banter] 群{gid} 接住 {nick}({uid}) 的「{text[:20]}」"
        f"（背景 {len(rows)} 条）→ 回复 {len(reply)} 字"
        + (f"，分 {total} 条发" if total > 1 else "")
    )
    return {"status": "ok", "reply": reply}


# ---------------------------------------------------------------- 调试


def stats() -> dict:
    """当前内存状态快照（网页 / 测试断言用，不参与任何业务判断）。"""
    return {
        "cursor": dict(_LAST_ID),
        "daily": {f"{g}@{d}": n for (g, d), n in _DAILY.items()},
        "gates": {g: datetime.fromtimestamp(t, service.resolver.tz).strftime("%H:%M:%S")
                  for g, t in _NEXT_AT.items()},
        "busy": sorted(_BUSY),
    }
