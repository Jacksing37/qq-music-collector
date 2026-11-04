"""说话时机：这一条消息，我该不该开口。

回答的是一个和「说什么」完全正交的问题——**此刻**插一句自然吗。改造前四个入口
（@ / 戳一戳 / 非音乐链接 / 定时搭话）都只有「开/关」两档，群是死是活、是不是正在
刷屏，行为一模一样。这里补上两个可衰减的状态，把它们折成一个概率：

- ``heat_fast`` / ``heat_slow``：群里有多热闹。快窗口（默认 120 秒）回答「刚才有人
  说话吗」，慢窗口（默认 1800 秒）回答「这个群今天有没有人气」。分两个是有意的：
  两个都为 0 才是真死群，那时候最该捧场；只要有一个高，就该让位。
- ``engage``：有没有人在跟我说话。被 @、紧跟它发言、正文命中关键词都抬它。
  它和 heat 的方向**相反**：heat 高 = 别插嘴，engage 高 = 该我接一句。

两条铁律（错一条这个模型就是负优化）：

1. **先判定、后记账**。算这一条该不该回，必须用此前积累的状态；判定完再更新。
   反过来的话，死群里的第一条消息会先把自己的热度抬起来、把自己的概率压下去——
   而第一条恰恰是最该捧场的那条。
2. **自己发言后 ``engage`` 必须下降**（``×= speak_engage_self_damp``）。
   若让它上升就是正反馈：「越说越想说」的资格越来越高，冷群里会演变成刷屏。
   这也是它取代「连发上限 / 冷却」这类硬规则的地方——不靠计数拦截，靠资格衰减。

衰减不跑定时任务，读的时候按距上次的时间惰性补算（``0.5 ** (dt / 半衰期)``）：
进程里没有任何后台线程，也不需要持久化——「群里现在的气氛」本来只有几分钟的
保质期，存到磁盘上再读回来反而更错。

抬升一律走边际递减 ``v += amount * (1 - v)``：永远到不了 1，也就永远还有余量。
线性累加会顶满，之后所有输入都解析成同一个值，模型就失去了分辨力。

只借用 nonebot 的 ``logger``（干跑日志要进运行日志页），不碰 event / bot / 网络，
状态全在进程内，可直接单测。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Optional

from nonebot.log import logger

from .config import LLMConfig, config_manager

__all__ = [
    "note_group_message",
    "note_bot_spoke",
    "note_engage",
    "probability",
    "decide_speak",
    "reset",
    "stats",
]


@dataclass
class _Group:
    """一个群的三个状态值 + 两个时刻。

    **三个值共用一个结算时刻是有意的**：它们的半衰期各不相同（快 120 / 慢 1800 /
    engage 90 秒），但只要每次都一起结算，dt 就是同一个。拆成三份各自带时间戳，
    任何一处漏更新都会让另一个窗口按错误的 dt 衰减——而那种错误在测试里几乎看不
    出来，线上只表现为「最近怎么不太说话」。
    """

    fast: float = 0.0
    slow: float = 0.0
    engage: float = 0.0
    #: 上次结算的时刻（:func:`_now` 的秒数）
    at: float = 0.0
    #: 机器人上次发言的时刻；0.0 = 没有（「紧跟」窗口的起点）
    bot_at: float = 0.0


#: 群号 -> 状态。进程内、重启即清零：这份数据描述的是「此时此刻的群气氛」。
_STATE: dict[int, _Group] = {}


# ---------------------------------------------------------------- 时间与配置


def _now() -> float:
    """当前时刻（秒，单调）。

    **所有读时间的代码都必须过这里**：测试靠 monkeypatch 它来验证衰减，而不是
    真的 sleep（真 sleep 会让回归慢几十秒，还必然偶尔飘）。
    """
    return time.monotonic()


def _rand() -> float:
    """取一个 [0, 1) 的随机数。**唯一**的随机入口。

    测试靠替换它（或给 :func:`decide_speak` 传 ``roll``）让判定可复现——
    「跑 1000 次统计频率」那种写法又慢又飘，不该出现在回归里。
    """
    return random.random()


def _cfg() -> LLMConfig:
    """当前生效的 ``llm`` 配置。

    直接读 ``config_manager`` 而不是 ``service.cfg(gid)``：``llm`` 整段是进程级
    唯一的（``config._GLOBAL_ONLY_SECTIONS`` 里锁着，不允许按群覆盖），群号在这里
    拿不到任何额外信息。
    """
    return config_manager.config.llm


# ---------------------------------------------------------------- 数学


def _decay(value: float, dt: float, half_life: float) -> float:
    """指数衰减：经过 ``dt`` 秒后剩 ``0.5 ** (dt / 半衰期)``。

    ``half_life <= 0`` 时退化成「立刻衰减到 0」而不是抛 ZeroDivisionError：
    配置项有 ``gt=0`` 校验，但这份代码也会被测试和热载路径调用，不值得为此崩掉
    一条群消息的处理。
    """
    if dt <= 0 or value <= 0:
        return value
    if not half_life or half_life <= 0:
        return 0.0
    return value * 0.5 ** (dt / half_life)


def _raise(value: float, amount: float) -> float:
    """边际递减抬升：``v += amount * (1 - v)``，永远到不了 1。"""
    return min(1.0, value + float(amount) * (1.0 - value))


# ---------------------------------------------------------------- 状态结算


def _settle(gid: int, cfg: LLMConfig, now: float) -> _Group:
    """取出该群状态，并按距上次的时间把三个值补算到「现在」。

    新建的记录 ``at`` 就是现在，所以第一次读到的全是 0——冷群。
    """
    key = int(gid)
    group = _STATE.get(key)
    if group is None:
        group = _Group(at=now)
        _STATE[key] = group
        return group
    dt = now - group.at
    if dt > 0:
        group.fast = _decay(group.fast, dt, float(cfg.speak_heat_fast_half_life))
        group.slow = _decay(group.slow, dt, float(cfg.speak_heat_slow_half_life))
        group.engage = _decay(group.engage, dt, float(cfg.speak_engage_half_life))
    group.at = now
    return group


def _peek(gid: int, cfg: LLMConfig, now: float) -> tuple[float, float, float, float]:
    """只读版结算：返回 ``(fast, slow, engage, 距它上次发言的秒数)``。

    **不写回状态**——:func:`probability` 的契约是只读：「算一下这条会不会回」
    不能有副作用，否则同一秒内问两次会得到两个答案。
    """
    group = _STATE.get(int(gid))
    if group is None:
        return 0.0, 0.0, 0.0, float("inf")
    dt = max(0.0, now - group.at)
    fast = _decay(group.fast, dt, float(cfg.speak_heat_fast_half_life))
    slow = _decay(group.slow, dt, float(cfg.speak_heat_slow_half_life))
    engage = _decay(group.engage, dt, float(cfg.speak_engage_half_life))
    since_bot = float("inf") if group.bot_at <= 0 else max(0.0, now - group.bot_at)
    return fast, slow, engage, since_bot


def _keyword_hit(cfg: LLMConfig, text: str, nick: str) -> tuple[bool, bool]:
    """``(正文命中, 昵称命中)``。关键词留空 = 这套判定完全不启用。

    大小写都压成小写再比：关键词里写 ``Bot``、群里打成 ``bot`` 是同一件事。
    """
    words = [str(w).strip().lower() for w in (cfg.speak_keywords or [])]
    words = [w for w in words if w]
    if not words:
        return False, False
    body = str(text or "").lower()
    name = str(nick or "").lower()
    return any(w in body for w in words), any(w in name for w in words)


# ---------------------------------------------------------------- 记账


def note_group_message(gid: int) -> None:
    """群友之间在聊 → 抬 ``fast`` 和 ``slow``。

    只在「这条不是在跟机器人说话」时才该被调用（见 :func:`note_engage` 的返回值）：
    对着它说的一条消息，说明不了这个群有多热闹。

    开关关着也照常记账（几乎零成本）。这样把 ``speak_enabled`` 打开的那一刻，
    它脑子里已经有「这个群现在热不热」，而不是从冷启动开始瞎热一句。
    """
    if not gid:
        return
    cfg = _cfg()
    group = _settle(gid, cfg, _now())
    per = float(cfg.speak_heat_per_message)
    group.fast = _raise(group.fast, per)
    group.slow = _raise(group.slow, per)


def note_bot_spoke(gid: int) -> None:
    """机器人说话了：只抬 ``fast``；``engage`` 打折；重置「紧跟」窗口。

    **``slow`` 一动不动**：「我刚说过话」是短期事件，不该被算成「这个群有人气」的
    长期证据。挪到 slow 上是双重错误——既让它的发言把整晚的热度坐实，又让后面的
    概率被自己压死，冷群里它会越来越不敢说话。

    ``engage`` 是**下降**（不是上升）：让它「越说越有资格说」是正反馈，冷群里必然
    演变成刷屏。这条是取代「连发上限 / 冷却」这类硬规则的关键机制。
    """
    if not gid:
        return
    cfg = _cfg()
    now = _now()
    group = _settle(gid, cfg, now)
    group.fast = _raise(group.fast, float(cfg.speak_self_bump))
    damp = float(cfg.speak_engage_self_damp)
    group.engage = min(1.0, max(0.0, group.engage * damp))
    # 它刚说完 → 接下来几分钟内有人接话，就是「在跟它说话」。窗口由 note_engage 消费
    group.bot_at = now


def note_engage(gid: int, *, mentioned: bool, text: str, nick: str) -> bool:
    """按这条群消息更新 ``engage``，返回「这条是不是在跟机器人说话」。

    返回 ``True`` 的三条路：@ 了它、紧跟它刚才那句、正文命中关键词。
    返回 ``False`` 时调用方**还会**调 :func:`note_group_message`——那不是「在跟我
    说话」，就是群友之间的正常闲聊，该抬的是群热度。

    命中**昵称**里的关键词不算「在跟我说话」：那是别人起的名字（「小助手他爸」），
    不是对着它说。但 ``engage`` 仍然抬一点——一条带着它名字的消息摆在眼前，
    接一句总比装没看见自然。

    顺序上这些判定是互斥的，从强到弱：@ > 正文关键词 > 昵称关键词 > 紧跟窗口。
    """
    if not gid:
        return False
    cfg = _cfg()
    now = _now()
    group = _settle(gid, cfg, now)
    hit_text, hit_nick = _keyword_hit(cfg, text, nick)

    if mentioned:
        group.engage = _raise(group.engage, float(cfg.speak_engage_up))
        # 用掉紧跟窗口：同一条消息不该既算「@ 了」又让下一条再算一次「紧跟」
        group.bot_at = 0.0
        return True

    if hit_text:
        group.engage = _raise(group.engage, float(cfg.speak_engage_keyword))
        group.bot_at = 0.0
        return True

    if hit_nick:
        group.engage = _raise(group.engage, float(cfg.speak_engage_keyword))
        return False

    window = float(cfg.speak_engage_followup_window)
    if window > 0 and group.bot_at > 0 and (now - group.bot_at) <= window:
        # 补量随 engage 存量**递减**：``room = 1 - engage`` 越小补得越少。
        # 不减的话，「衰减 ↔ 补满」会形成死循环——它说一句、对方回一句、
        # 窗口内又补满，于是它跟一个群友无限对聊下去。
        room = 1.0 - group.engage
        if room > 0.02:
            amount = float(cfg.speak_engage_up_followup) * (0.35 + 0.65 * room)
            group.engage = _raise(group.engage, amount)
        # 窗口只用一次（用完即清），否则窗口内连续几条都会被算成在跟它说话
        group.bot_at = 0.0
        return True

    return False


# ---------------------------------------------------------------- 判定


def probability(gid: int, *, mentioned: bool, text: str, nick: str) -> float:
    """这一条该不该开口的概率。**只读**：不修改任何状态。

    被 @ 是独立分支，恒 1.0，连 ambient / engage 都不算：@ 就是「叫你了」，没有
    讨论余地；顺带保证这条最热的路径上不产生任何副作用。

    剩下两条取最大值而不是加权：

    - ``p_ambient``：没人叫它的时候，看群有多冷清（越冷越该捧场）；
    - ``p_engage``：有人在跟它说话，就直接按 engage 的存量给资格。

    两者取 max 是因为它们的含义互斥——「热闹所以让位」和「在跟我说话所以该接」
    不该互相抵消。
    """
    if mentioned:
        return 1.0
    cfg = _cfg()
    fast, slow, engage, _since_bot = _peek(int(gid or 0), cfg, _now())
    p_max = float(cfg.speak_p_max)
    p_min = float(cfg.speak_p_min)
    # 两个窗口都为 0 才是真死群，所以是乘积：(1-fast)*(1-slow)。
    # 用 max(fast, slow) 的话，一次「刚才有人说话」就足以把概率压到最低档，
    # 而这个群其实一整天都死气沉沉——那正是最该接话的时候
    p_ambient = p_min + (p_max - p_min) * (1.0 - fast) * (1.0 - slow)
    p_engage = float(cfg.speak_engage_peak) * engage
    return max(p_ambient, p_engage)


def decide_speak(
    gid: int,
    kind: str,
    text: str = "",
    nick: str = "",
    *,
    roll: Optional[float] = None,
) -> tuple[bool, float]:
    """返回 ``(是否开口, 本次概率)``。``kind`` ∈ ``{"at", "poke", "link", "banter"}``。

    三条短路，优先级从高到低：

    - ``speak_enabled`` 关着 → 恒 ``True``。**关掉就是改造前的行为**，一个字节
      都不变（这也是回滚开关）。
    - ``at`` / ``poke`` → 恒 ``True``：被叫了就该应一声，不掷骰子。
    - ``speak_dry_run`` 开着 → 照常算 p、照常打日志，但恒 ``True``：灰度观察期
      只记录不拦截，先看一周概率分布合不合理，再让它真的接管。

    ``roll`` 只给测试用：传固定值就能断言「p 边界上到底拦不拦」，不必靠跑一千次
    统计频率（那种写法既慢又会随机飘）。
    """
    cfg = _cfg()
    if not cfg.speak_enabled:
        return True, 1.0
    if kind in ("at", "poke"):
        return True, 1.0
    p = probability(gid, mentioned=False, text=text, nick=nick)
    if cfg.speak_dry_run:
        # 灰度期这行日志就是全部产出：一周后拿它统计「本该回多少、实际回多少」
        logger.info(f"[speak] 群{gid} kind={kind} p={p:.3f} → 干跑不拦")
        return True, p
    value = _rand() if roll is None else float(roll)
    ok = value < p
    # 放行记 INFO（这是群里真会发生的事），拦下记 DEBUG：热群里每来一条链接都拦，
    # INFO 会把运行日志页刷满，排查别的问题时反而看不见
    log = logger.info if ok else logger.debug
    log(f"[speak] 群{gid} kind={kind} p={p:.3f} roll={value:.3f} → {'放行' if ok else '拦下'}")
    return ok, p


# ---------------------------------------------------------------- 维护


def reset() -> None:
    """清空所有状态。测试要用；配置热载后想从冷启动重新观察也可以调。"""
    _STATE.clear()


def stats() -> dict:
    """当前状态快照（调试 / 测试断言用，**不参与任何业务判断**）。

    值是按「现在」衰减过的，不是上次写入时的原值——否则想在测试里断言
    ``0.5 ** k`` 还得自己再算一遍，等于把被测逻辑抄进测试。
    """
    cfg = _cfg()
    now = _now()
    groups: dict[int, dict] = {}
    for gid in sorted(_STATE):
        fast, slow, engage, since_bot = _peek(gid, cfg, now)
        groups[gid] = {
            "fast": fast,
            "slow": slow,
            "engage": engage,
            # 没有过发言时给 0 而不是 inf，方便直接打印与比较
            "since_bot": 0.0 if since_bot == float("inf") else since_bot,
        }
    return {
        "count": len(groups),
        "groups": groups,
        "enabled": bool(cfg.speak_enabled),
        "dry_run": bool(cfg.speak_dry_run),
    }
