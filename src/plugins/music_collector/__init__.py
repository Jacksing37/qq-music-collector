"""QQ 群音乐分享收集机器人。

自动识别群里分享的各平台音乐链接 / 卡片，@ 分享者回发歌曲名片，
按时序维护榜单（文字 + 长图），并在设定时刻自动建网易云歌单归档。
"""

from __future__ import annotations

import time
from datetime import datetime

from nonebot import get_driver, on_message, on_notice, require
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    GROUP_ADMIN,
    GROUP_OWNER,
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PokeNotifyEvent,
)
from nonebot.log import logger
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

require("nonebot_plugin_apscheduler")

from .bot_utils import (  # noqa: E402
    describe_forward,
    message_preview,
    quote_prefix_and_images,
    read_message,
    safe_send_group,
    send_music_card,
    split_text,
    trace_messages_enabled,
    trace_out,
)
from .detector import (  # noqa: E402
    extract_from_card,
    extract_from_text,
    extract_from_xml,
    extract_urls,
)
from .llm import disabled_reason as llm_disabled_reason  # noqa: E402
from .llm import clip_tail, llm_bot  # noqa: E402
from . import banter as banter_mod  # noqa: E402  被动回复后要重排搭话计时
from . import memory as memory_mod  # noqa: E402
from .models import PLATFORM_NAMES, Song  # noqa: E402
from .naming import build_context, render_template, resolve_alias  # noqa: E402
from .scheduler import reload_jobs  # noqa: E402
from .service import service  # noqa: E402
from .store import MASTER_KEY  # noqa: E402

from . import commands as _commands  # noqa: E402,F401  仅为注册命令
from . import webui as _webui  # noqa: E402,F401  配置管理 Web UI

__plugin_meta__ = PluginMetadata(
    name="群音乐收集",
    description="收集群内分享的音乐链接，定时汇总成榜单并自动建网易云歌单",
    usage="发送 /music help 查看命令",
)

driver = get_driver()


@driver.on_startup
async def _startup() -> None:
    await service.setup()
    # 把持久化后端交给大模型：上下文从此跨重启保留（库里那份才是真相）
    llm_bot.set_store(service.store)
    ok, info = reload_jobs()
    logger.info(f"[music] 插件初始化完成，定时任务{'已注册' if ok else '注册失败'}\n{info}")
    _webui.register_webui()


# ---------------------------------------------------------------- 消息监听


async def _looks_like_music(event: GroupMessageEvent) -> bool:
    """粗筛，避免每条群消息都走一遍完整解析。"""
    for seg in event.message:
        if seg.type in ("json", "music", "xml"):
            return True
        if seg.type == "text" and "http" in str(seg.data.get("text", "")):
            return True
    return False


def _has_music_link(event: GroupMessageEvent) -> bool:
    """消息里有没有**真正能识别的**音乐链接（本地正则判定，不联网）。

    与 ``_looks_like_music`` 的分工：

    - ``_looks_like_music`` 是**粗筛**——「带 http 就算」，只用来省掉全量解析，
      拿它当「是不是音乐」用会把所有网页链接都误判成音乐；
    - 这个函数是**准判**——交给 ``detector`` 的平台识别（QQ 音乐 / 网易云 /
      酷狗…的域名与卡片结构）。

    用途：@ 机器人时决定「交给收集流程还是交给大模型」，以及分享链接时决定
    「这条是不是该让模型接话的非音乐链接」。
    """
    for seg in event.message:
        if seg.type == "music":
            return True
        if seg.type == "json":
            try:
                if extract_from_card(seg.data.get("data")):
                    return True
            except Exception:  # 脏卡片不该让整条消息掉进异常
                continue
        elif seg.type == "xml":
            try:
                if extract_from_xml(str(seg.data.get("data") or "")):
                    return True
            except Exception:
                continue
        elif seg.type == "text":
            try:
                if extract_from_text(str(seg.data.get("text") or "")):
                    return True
            except Exception:
                continue
    return False


music_listener = on_message(rule=Rule(_looks_like_music), priority=99, block=False)

# 收到消息先留一条痕（priority=1、block=False，只观察不影响其它 matcher）。
# 想关掉就到「配置 → 运行日志」里把 logs.trace_messages 关掉。
trace_listener = on_message(priority=1, block=False)


@trace_listener.handle()
async def _trace_incoming(bot: Bot, event: Event) -> None:
    if not trace_messages_enabled():
        return
    group_id = getattr(event, "group_id", None)
    sender = getattr(event, "sender", None)
    if sender is not None:
        nick = getattr(sender, "card", "") or getattr(sender, "nickname", "") or str(event.user_id)
    else:
        nick = str(event.user_id)
    where = f"群{group_id}" if group_id else "私聊"
    logger.info(
        f"[music] ← {where} {nick}({event.user_id}): {message_preview(event.message)}"
    )


# ----------------------------------------------------- 旁听：给「记住每个人」攒素材

# priority=2、block=False：只观察，绝不回复、也绝不改变任何流程走向。
# 与 trace_listener 分开是有意的——trace 只写日志（可以关），旁听要落库，
# 两者开关不同、失败后果也不同。
listen_listener = on_message(priority=2, block=False)

#: (群号, QQ号) -> 上次更新档案活跃度的 monotonic 时刻。
#: 每条消息都写一次盘没必要，10 分钟一次足够反映「最近活跃」
_touch_at: dict[tuple[int, str], float] = {}
_TOUCH_INTERVAL = 600.0


def memory_goal(gid: int) -> tuple[bool, object]:
    """记忆对该群是否生效。返回 ``(是否旁听, memory 配置)``。

    四个条件缺一不可：记忆总开关、旁听开关、大模型开着（没有模型就没有整理）、
    群在白名单里（``memory.listen_groups`` 优先，留空就看 ``llm.groups``）。
    """
    app_cfg = service.cfg(gid)
    mem = app_cfg.memory
    if not (mem.enabled and mem.listen):
        return False, mem
    if not app_cfg.llm.enabled:
        return False, mem
    groups = list(mem.listen_groups or app_cfg.llm.groups or [])
    if groups and gid not in groups:
        return False, mem
    return True, mem


@listen_listener.handle()
async def _listen_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    """把群消息记进 ``chat_log(kind='listen')``，并更新说话人的活跃度。

    **只落库，不回复**。它解决的是「机器人只在被 @ 时才看得见群里的话」这个问题：
    没有这份素材，「记住每个人」就只能靠运气。

    失败一律静默：磁盘满、目录没权限都不该让群里出现报错。
    """
    gid = int(getattr(event, "group_id", 0) or 0)
    uid = int(getattr(event, "user_id", 0) or 0)
    if not gid or not uid or uid == int(getattr(event, "self_id", 0) or 0):
        return
    ok, mem = memory_goal(gid)
    if not ok:
        return
    text = (event.get_plaintext() or "").strip()
    # 纯图片 / 表情 / 卡片没有可读文字，记下来也没用
    if not text:
        return
    # 命令不记："/music list" 这类不能反映「这个人平时聊什么」
    if text.lower().startswith(_COMMAND_PREFIXES):
        return

    app_cfg = service.cfg(gid)
    item = max(200, int(app_cfg.llm.history_item_chars))
    nick = _sender_nick(event)
    try:
        await service.store.append_chat(
            gid, uid, nick, "user", text[:item], kind="listen"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[memory] 群{gid} 旁听落库失败: {exc}")
        return

    key = (gid, str(uid))
    now = time.monotonic()
    if now - _touch_at.get(key, 0.0) < _TOUCH_INTERVAL:
        return
    _touch_at[key] = now
    try:
        created = memory_mod.touch(mem, gid, uid, nick)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[memory] 群{gid} 更新活跃度失败: {exc}")
        return
    if created:
        logger.info(f"[memory] 群{gid} 新成员进档：{nick}({uid})")


# ----------------------------------------------------- 被 @ 时回复自我介绍

# 群 -> 上次回复自我介绍的时间戳，用于冷却
_intro_last_sent: dict[int, float] = {}

_COMMAND_PREFIXES = ("/", "#", "!", "！", "／")
_COMMAND_WORDS = ("music", "音乐")


async def _at_bot(event: GroupMessageEvent) -> bool:
    """消息是否 @ 了本机器人。

    坑点：OneBot V11 适配器会把**开头/结尾**的 @机器人 段从 event.message 里
    摘掉，同时把 event.to_me 置为 True。所以只遍历 message 找 at 段，
    在最常见的「@机器人 说点什么」场景下永远匹配不到——这就是之前 @ 没反应的原因。
    正确做法是以 to_me 为准，再对夹在中间的 @ 做一次兜底扫描。
    """
    if event.to_me:
        return True
    self_id = str(event.self_id)
    return any(
        seg.type == "at" and str(seg.data.get("qq", "")) == self_id
        for seg in event.message
    )


def _is_command_message(event: GroupMessageEvent) -> bool:
    text = event.message.extract_plain_text().strip()
    if not text:
        return False
    if text.startswith(_COMMAND_PREFIXES):
        return True
    return text.split()[0].lower() in _COMMAND_WORDS


async def _build_intro(event: GroupMessageEvent) -> str:
    """渲染自我介绍文案，支持命名占位符 + {nick}/{count}/{state}/{playlist}。"""
    group_id = event.group_id
    cfg = service.cfg(group_id)
    state = service.current_window(group_id)
    try:
        count = await service.store.count(group_id, state.key)
    except Exception:
        count = 0
    nick = event.sender.card or event.sender.nickname or str(event.user_id)

    context = build_context(
        group_id=group_id,
        window_label=state.label,
        start_at=state.start_at,
        end_at=state.archive_at,
        count=count,
        total=count,
        seq=cfg.playlist.seq,
        songs=[],
    )
    context["nick"] = nick
    context["state"] = "收集中" if state.collecting else "未在收集期"
    context["playlist"] = render_template(
        cfg.playlist.pending_name or cfg.playlist.name_template, context
    )
    return render_template(cfg.intro.text, context)


at_listener = on_message(rule=Rule(_at_bot), priority=5, block=False)


def _llm_skip(event: GroupMessageEvent, reason: str, level: str = "debug") -> bool:
    """没走大模型时留一条痕，然后返回 False（交给自我介绍兜底）。

    等级分两档，靠 ``level`` 指定：

    - **INFO**：开了大模型却没生效（缺项、群不在白名单）——「明明开了怎么不回」
      是最难查的一种，默认配置等级（INFO）就能看到。
    - **DEBUG**：本来就不该走（没启用、命令、音乐分享、只 @ 没说话）——都是正常
      流量，用 INFO 会把日志页刷满；真要看时把「配置 → 运行日志 → 日志记录等级」
      临时调成 DEBUG。
    """
    log = logger.info if str(level).lower() == "info" else logger.debug
    log(f"[llm] 群{event.group_id} 未走大模型：{reason}")
    return False


def _llm_gate(
    event: object = None, *, group_id: int | None = None
) -> tuple[object, str]:
    """过三道门，返回 ``(llm_cfg, 原因)``；原因非空表示不该走大模型。

    三道门分别是：``llm.enabled`` + 必填项 + ``llm.groups`` 群白名单（由
    ``llm.disabled_reason`` 统一判定）、插件总开关、插件生效群号。

    三个触发点（@ 机器人 / 被戳一戳 / 分享了非音乐链接）共用这一套门槛，
    免得某个入口漏掉一个开关、变成「明明关了还在说话」。
    """
    gid = int(group_id if group_id is not None else getattr(event, "group_id", 0) or 0)
    app_cfg = service.cfg(gid)
    llm_cfg = app_cfg.llm
    why = llm_disabled_reason(llm_cfg, gid)
    if why:
        return llm_cfg, why
    if not app_cfg.enabled:
        return llm_cfg, "插件总开关关闭"
    if app_cfg.groups and gid not in app_cfg.groups:
        return llm_cfg, "本群不在插件生效列表"
    return llm_cfg, ""


def _sender_nick(event: object) -> str:
    """从事件里取一个能称呼的昵称（群名片 > 昵称 > QQ 号）。

    戳一戳的 notify 事件**没有 sender 字段**，只能退回 QQ 号——所以这里用
    ``getattr`` 而不是直接取属性，两种事件都能用。
    """
    sender = getattr(event, "sender", None)
    if sender is not None:
        nick = getattr(sender, "card", "") or getattr(sender, "nickname", "")
        if nick:
            return str(nick)
    return str(getattr(event, "user_id", "") or "")


async def _llm_context(bot: Bot, event: object, llm_cfg, text: str) -> str:
    """准备模型「自己看不到」的那部分内容：合并转发的聊天记录、链接背后的网页正文。

    两件事各有开关，**失败一律静默降级**（抓不到就当没有）：外链打不开是常态，
    不该变成「群里没回应」。返回的文本只作用于这一次请求，不写进上下文历史。

    只在真要回话的触发点调用（@机器人 / 非音乐链接），不扫全群消息——
    那样既是流量负担，也容易被站点拉黑。
    """
    blocks: list[str] = []
    message = getattr(event, "message", None)
    reply = getattr(event, "reply", None)
    reply_message = getattr(reply, "message", None) if reply is not None else None

    # ① 合并转发的聊天记录：群友「转发了一段聊天」再问「他们说的是啥」。
    # 也要看被引用的那条——常见操作是「引用一条转发记录 + 问这是啥」，
    # 此时转发内容在 event.reply 里，不在当前消息里。
    if llm_cfg.read_forward:
        for label, source in (
            ("群友转发的聊天记录", message),
            ("被引用的聊天记录", reply_message),
        ):
            if source is None:
                continue
            try:
                record = await describe_forward(
                    bot,
                    source,
                    limit=llm_cfg.forward_limit,
                    max_chars=llm_cfg.forward_chars,
                )
            except Exception as exc:  # noqa: BLE001 — 读不出来就当没有这段
                record = ""
                logger.debug(f"[llm] 读取聊天记录失败: {exc}")
            if not record:
                continue
            blocks.append(f"【{label}】\n{record}")
            rows = record.count("\n") + 1
            logger.info(
                f"[llm] 群{getattr(event, 'group_id', '?')} 附带聊天记录 {rows} 条"
                f"（{len(record)} 字）"
            )

    return "\n\n".join(blocks)


#: 背景的**硬上限**。``with_context`` 总共只放 4000 字且超了截**开头**，
#: 而背景是正序（最旧在前）——靠它裁会把最新那几句扔掉，所以背景自己先截好。
#: 取 2500 是给「转发记录 / 网页正文」留出空间；``bg_chat_chars`` 配得更大也只给这么多。
_BG_CHAR_LIMIT = 2500


def _drop_pending_question(rows: list[dict], event: object, text: str) -> list[dict]:
    """把「当前这条提问」从背景里剔掉。

    群里 @ 它的话会被记两遍：旁听器落一条 ``listen``（背景取的就是这张表），
    ``chat()`` 又落一条 ``chat``。于是背景里会冒出和提问一字不差的一句，而背景
    开头明确写着「不是在跟你说话」——同一句既是「别人的闲聊」又是「问你的」，
    模型只能挑一个信，答出来就前后不搭。

    只按「同一个人 + 正文完全相同」剔除，宁可漏也别误删别人的话。
    """
    want = (text or "").strip()
    uid = int(getattr(event, "user_id", 0) or 0)
    if not want or not uid:
        return rows
    return [
        r
        for r in rows
        if not (
            int(r.get("user_id") or 0) == uid
            and str(r.get("content") or "").strip() == want
        )
    ]


async def _llm_background(event: object, llm_cfg, text: str = "") -> str:
    """机器人上次说话之后、群里聊的那一段（它没参与过的那截）。

    **为什么需要它**：群友闲聊存进 ``kind='listen'``，而问答历史是
    ``kind='chat'``，两条通道分开。所以单纯 @ 它一句「刚才在聊啥」，它其实
    一无所知——问出这种「答不上来」的根源在这里，不是模型不行。

    ``text`` 是当前这条提问的正文，用来把它自己从背景里剔掉（见
    :func:`_drop_pending_question`）。

    只查库、不发请求；任何异常都当「没有背景」，绝不能让 @ 的回复卡住。
    """
    if not getattr(llm_cfg, "bg_chat", False):
        return ""
    gid = int(getattr(event, "group_id", 0) or 0)
    if not gid:
        return ""
    max_rows = max(1, int(llm_cfg.bg_chat_max))
    try:
        last = await service.store.last_bot_chat(gid)
        rows = await service.store.recent_chat(
            gid,
            after_id=int((last or {}).get("id") or 0),
            limit=max_rows,
            max_chars=int(llm_cfg.bg_chat_chars),
        )
        rows = _drop_pending_question(rows, event, text)
        # 界限之后不够看就往前补。刚回完一句又被 @ 时，界限之后只剩这句提问
        # 本身（剔除后为空），模型于是完全接不上前面在聊什么——群里看到的就是
        # 「上一句还答得好好的，下一句突然问我在说什么」。
        need = min(int(llm_cfg.bg_chat_min_rows), max_rows)
        if need > 0 and len(rows) < need:
            tail = await service.store.recent_chat(
                gid, after_id=0, limit=max_rows,
                max_chars=int(llm_cfg.bg_chat_chars),
            )
            tail = _drop_pending_question(tail, event, text)
            # 只往前补，别把界限之后那些重复一遍
            first_id = int(rows[0]["id"]) if rows else 0
            older = [r for r in tail if not first_id or int(r["id"]) < first_id]
            if older:
                rows = older[-(need - len(rows)):] + rows
    except Exception as exc:  # noqa: BLE001 — 背景拿不到也要正常回答
        logger.debug(f"[llm] 群{gid} 取群聊背景失败: {exc}")
        return ""
    if not rows:
        return ""

    # 太久以前的不算「刚才在聊」：它可能几小时没说话，中间的全捞进来也没用
    cutoff = time.time() - float(llm_cfg.bg_chat_minutes) * 60
    rows = [r for r in rows if float(r.get("created_at") or 0) >= cutoff]
    if not rows:
        return ""

    tz = service.resolver.tz
    lines: list[str] = []
    for row in rows:
        try:
            hm = datetime.fromtimestamp(float(row["created_at"]), tz).strftime("%H:%M")
        except (KeyError, TypeError, ValueError, OSError):
            hm = "--:--"
        body = str(row.get("content") or "").strip().replace("\n", " ")
        lines.append(f'[{hm}] {row.get("nick") or "群友"}({row.get("user_id") or 0})：{body}')

    # 尊重用户的 bg_chat_chars，但不超过硬上限（超了会把真正的提问挤出 context）
    budget = max(200, min(int(llm_cfg.bg_chat_chars), _BG_CHAR_LIMIT))
    text = clip_tail("\n".join(lines), budget)
    logger.debug(f"[llm] 群{gid} 附带群聊背景 {len(rows)} 条 / {len(text)} 字")
    # 这句说明不能省：不写的话模型会以为有人在跟它说「这家锅贴真不错」，
    # 或者以为自己该接话
    return "【群里刚才在聊的，不是在跟你说话，判断语境用】\n" + text


async def _llm_answer(
    bot: Bot,
    event: object,
    llm_cfg,
    text: str,
    images: list[str] | None = None,
    quote: str = "",
    context: str = "",
) -> tuple[bool, str]:
    """让模型答一句并发到群里，返回 ``(是否已发出, 失败原因)``。

    ``event`` 只需要有 ``group_id`` / ``user_id``（群消息事件与戳一戳事件都满足），
    三个触发点的发送逻辑因此完全一致：@ 提问者（按配置）、超长自动分条。
    """
    group_id = int(getattr(event, "group_id", 0) or 0)
    user_id = int(getattr(event, "user_id", 0) or 0)
    # 群友闲聊不进问答历史，这里把那一段补上（走 background，不进历史、不占历史预算）
    background = await _llm_background(event, llm_cfg, text)
    reply, reason = await llm_bot.chat(
        group_id,
        user_id,
        _sender_nick(event),
        text,
        llm_cfg,
        images=list(images or []),
        quote=quote,
        context=context,
        # 单独传而不是并进 context：日志页要能还原「它当时读到了哪几句群聊」
        background=background,
        # 记忆配置挂在 AppConfig 顶层（memory: 段），llm_cfg 里没有它
        mem=service.cfg(group_id).memory if group_id else None,
    )
    if not reply:
        return False, reason
    parts = split_text(reply)
    first = Message()
    if llm_cfg.at_sender:
        first += MessageSegment.at(user_id)
    first += MessageSegment.text(" " + parts[0])
    await safe_send_group(bot, group_id, first)
    for extra in parts[1:]:
        await safe_send_group(bot, group_id, Message(MessageSegment.text(extra)))
    # 它刚在群里说过话 → 主动搭话的计时重新起算。否则排到的那一轮可能紧接着
    # 就自己冒泡，群里看着像抢话；而且刚说完这一两句，可接的素材也还没跟上
    banter_mod.note_reply(group_id)
    return True, ""


async def _try_llm_reply(bot: Bot, event: GroupMessageEvent) -> bool:
    """用大模型回答「@机器人 + 一句话」。

    返回 ``True`` 表示这条消息已经处理完（含「冷却中故意静默」），上层不要再发
    自我介绍；返回 ``False`` 表示该走内置自我介绍。

    门槛与自我介绍保持同一套（不抢命令、不抢**真的**音乐链接），另外多一道
    ``llm.enabled`` + ``llm.groups`` 的群白名单。注意「带链接」本身不再是跳过
    理由：@ 机器人 + 一个普通网页链接，现在是该模型答的。
    """
    llm_cfg, why = _llm_gate(event)
    if why:
        # 没启用是常态（回自我介绍，符合预期）；开了却走不通才是要排查的
        return _llm_skip(event, why, "debug" if not llm_cfg.enabled else "info")
    if _is_command_message(event):
        return _llm_skip(event, "是命令消息，交给 /music 处理")
    if _has_music_link(event):
        return _llm_skip(event, "是音乐分享，交给收集流程")

    # 把消息段全转成模型能读的：文本原样、图/表情/语音留方括号标记，图片链接单独带出。
    # 跳过 @机器人自己——「只 @ 了机器人、什么都没说」不该被当成一句提问。
    self_ids = {str(event.self_id)}
    text, images = read_message(event.message, self_ids)
    # 引用回复：把被引用的那条压成一句前缀（OneBot 已塞进 event.reply，不用再调 get_msg）。
    # 被引用消息里的图片也一并带出——只给 [图片] 占位符的话，模型会回「图我这边没瞅见」。
    quote = ""
    quote_images: list[str] = []
    if llm_cfg.read_reply:
        quoted = getattr(event, "reply", None)
        if quoted is not None:
            rep_sender = getattr(quoted, "sender", None)
            who = ""
            if rep_sender is not None:
                who = (
                    getattr(rep_sender, "card", "")
                    or getattr(rep_sender, "nickname", "")
                    or ""
                )
            if not who:
                who = str(getattr(quoted, "user_id", "") or "")
            quote, quote_images = quote_prefix_and_images(
                quoted.message, who, llm_cfg.reply_chars, self_ids
            )
    if not text and not images and not quote:
        return _llm_skip(event, "没有可读内容（只有 @ 或空消息）")

    # 当前这条消息的图优先占额度，剩下的才轮到被引用的图（max_images 在 chat 里统一切）
    pics = list(images)
    for url in quote_images:
        if url not in pics:
            pics.append(url)

    # 转发记录 / 链接网页：模型原生看不到，得我们先取回来
    context = await _llm_context(bot, event, llm_cfg, text)

    ok, reason = await _llm_answer(bot, event, llm_cfg, text, pics, quote, context)
    if ok:
        return True
    if reason == "cooldown":
        # 冷却中保持安静，别再用自我介绍补一条（否则「限流」变「刷屏」）。
        # 冷却本身已在 llm.chat 里记了 INFO，这里不再重复。
        return True
    if reason == "empty":
        return False
    # 调用失败：按配置退回自我介绍，或干脆静默
    if not llm_cfg.fallback_intro:
        logger.debug(f"[llm] 群{event.group_id} 调用失败且未开兜底 → 保持静默")
        return True
    return False


async def _reply_intro(bot: Bot, event: GroupMessageEvent) -> None:
    """内置自我介绍（@ 但没有触发大模型回复时的兜底）。"""
    cfg = service.cfg(event.group_id).intro
    if not cfg.enabled:
        return
    # 收集功能关掉时默认仍然自我介绍（否则用户会以为机器人挂了）
    if not cfg.always_reply and not service.group_enabled(event.group_id):
        return
    if cfg.skip_commands and _is_command_message(event):
        return
    if cfg.skip_music and await _looks_like_music(event):
        return

    now = time.monotonic()
    if cfg.cooldown > 0:
        last = _intro_last_sent.get(event.group_id, 0.0)
        if now - last < cfg.cooldown:
            return
    _intro_last_sent[event.group_id] = now

    try:
        text = await _build_intro(event)
    except Exception as exc:
        logger.warning(f"[music] 自我介绍渲染失败: {exc}")
        return
    if not text.strip():
        return

    message = Message()
    if cfg.at_sender:
        message += MessageSegment.at(event.user_id)
    message += MessageSegment.text(text)
    await safe_send_group(bot, event.group_id, message)
    # 兜底的自我介绍也算「它刚说过话」，同样把搭话计时重算
    banter_mod.note_reply(event.group_id)


@at_listener.handle()
async def handle_at(bot: Bot, event: GroupMessageEvent) -> None:
    # 大模型优先；它没接（未启用 / 失败且允许兜底）才发自我介绍
    if await _try_llm_reply(bot, event):
        return
    await _reply_intro(bot, event)


# ------------------------------------------------------------ 被戳一戳时回一句
#
# 群友「双击头像」= OneBot 的 notify/poke 事件：``user_id`` 是谁戳的，
# ``target_id`` 是戳了谁。只有戳到机器人自己才接（戳别人跟它没关系），
# 且只接群里的（上下文按群维护，私聊没有可聊的上下文）。


async def _poked_me(bot: Bot, event: PokeNotifyEvent) -> bool:
    """戳一戳的对象是不是机器人自己。"""
    return str(event.target_id) == str(event.self_id)


poke_listener = on_notice(rule=Rule(_poked_me), priority=5, block=False)


@poke_listener.handle()
async def handle_poke(bot: Bot, event: PokeNotifyEvent) -> None:
    group_id = event.group_id
    if group_id is None:
        return
    llm_cfg, why = _llm_gate(group_id=int(group_id))
    if why:
        # 没启用是常态，只留 DEBUG；开了却走不通时 llm.disabled_reason 的原因照样记下来
        logger.debug(f"[llm] 群{group_id} 被戳了戳但没回复：{why}")
        return
    if not llm_cfg.poke_reply:
        logger.debug(f"[llm] 群{group_id} 被戳了戳，但「戳一戳也回复」关着")
        return

    prompt = (llm_cfg.poke_prompt or "").strip() or "（戳了戳你）"
    logger.info(
        f"[llm] 群{group_id} {_sender_nick(event)}({event.user_id}) 戳了戳机器人 → 交给大模型"
    )
    # 冷却中 chat 会返回 cooldown 并静默，这里什么都不用补
    ok, reason = await _llm_answer(bot, event, llm_cfg, prompt)
    if not ok and reason not in ("cooldown", "empty"):
        logger.debug(f"[llm] 群{group_id} 戳一戳回复失败：{reason}")


def _format_accept(song: Song, index: int, unmatched_notice: str = "") -> str:
    """内置（默认）收录回复格式。自定义模板关闭时用它。"""
    lines = [f" 已收录 · 本期第 {index} 首", song.title]
    if song.artists:
        lines.append(f"歌手: {song.artists}")
    if song.album:
        lines.append(f"专辑: {song.album}")
    lines.append(f"来源: {song.platform_name}")
    if unmatched_notice:
        lines.append(unmatched_notice)
    return "\n".join(lines)


def _song_detail_block(song: Song) -> str:
    """{song} 占位符：歌曲详情块。"""
    lines = [song.title]
    if song.artists:
        lines.append(f"歌手: {song.artists}")
    if song.album:
        lines.append(f"专辑: {song.album}")
    lines.append(f"来源: {song.platform_name}")
    if song.duration > 0:
        lines.append(f"时长: {song.duration_text}")
    return "\n".join(lines)


async def _playlist_placeholder(group_id: int, window_key: str) -> str:
    """{playlist} 占位符：当前群当前窗口的网易云歌单（名称 + 链接）。

    本期还没归档时用 ``reply.playlist_empty_text`` 代替。
    """
    cfg = service.cfg(group_id).reply
    try:
        arch = await service.store.get_archive(group_id, window_key)
    except Exception as exc:  # 查库异常不该打断回复
        logger.warning(f"[music] 读取本期歌单失败: {exc}")
        arch = None
    url = str((arch or {}).get("playlist_url") or "")
    pid = str((arch or {}).get("playlist_id") or "")
    if not url and pid.isdigit():
        url = service.netease.playlist_url(int(pid))
    if not url:
        return cfg.playlist_empty_text
    return url


def _fmt_dup_date(ts: float) -> str:
    """把 Unix 时间戳格式化成 YY/MM/DD（本地时区），用于总库重复提示。"""
    if not ts:
        return "—"
    t = time.localtime(ts)
    return f"{t.tm_year % 100:02d}/{t.tm_mon:02d}/{t.tm_mday:02d}"


async def build_master_dup_text(
    song: Song, index: int, group_id: int, sharer_name: str
) -> str:
    """生成总库跨窗口重复提示文案（模板见 config.master.notify_template）。

    占位符：{title}歌名 {artists}歌手 {platform}来源 {sharer}本次分享者
    {who}总库首发者 {index}总库序号 {count}总库总数 {window}当前窗口
    {period}首发所在期号（来源窗口） {date}首发分享日期(YY/MM/DD)。
    """
    cfg = service.cfg(group_id).master
    aliases = service.cfg(group_id).playlist.sharer_aliases
    # 从歌单导入进总库的歌（src_window="import"）没有分享者信息（sharer_id=0、
    # 昵称为空），直接拼 str(0) 会渲染成「首发: 0」；退回一句可读的占位文案。
    who_raw = (song.sharer_name or "").strip()
    if not who_raw and song.sharer_id:
        who_raw = str(song.sharer_id)
    who = resolve_alias(who_raw, song.sharer_id, aliases) if who_raw else "未知（总库导入）"
    nick = resolve_alias(sharer_name, 0, aliases)
    try:
        count = await service.store.count(group_id, MASTER_KEY)
    except Exception:
        count = index
    state = service.current_window(group_id)
    context = {
        "title": song.title,
        "artists": song.artists,
        "platform": song.platform_name,
        "sharer": nick,
        "who": who,
        "index": str(index),
        "count": str(count),
        "window": state.label,
        "period": getattr(song, "src_window", "") or "",
        "date": _fmt_dup_date(song.created_at),
    }
    return render_template(cfg.notify_template, context)


async def build_accept_text(
    song: Song, index: int, group_id: int, unmatched: bool = False
) -> str:
    """生成收录回复文案（自定义模板开启时走模板，否则用内置格式）。

    ``unmatched=True`` 表示这首歌已在网易云搜过、确认搜不到（不会进本期歌单），
    此时按 ``reply.unmatched_text`` 渲染一条提示，**并入同一条消息**，不额外刷屏。
    """
    cfg = service.cfg(group_id).reply
    state = service.current_window(group_id)
    try:
        count = await service.store.count(group_id, state.key)
    except Exception:
        count = index
    nick = resolve_alias(
        song.sharer_name or str(song.sharer_id), song.sharer_id,
        service.cfg(group_id).playlist.sharer_aliases,
    )
    notice = ""
    if unmatched:
        notice = render_template(cfg.unmatched_text, {
            "title": song.title,
            "artists": song.artists,
            "platform": song.platform_name,
            "nick": nick,
            "index": str(index),
            "count": str(count),
            "window": state.label,
        }).strip()
    context = {
        "index": str(index),
        "nick": nick,
        "title": song.title,
        "artists": song.artists,
        "album": song.album,
        "platform": song.platform_name,
        "url": song.url,
        "duration": song.duration_text,
        "artists_line": f"歌手: {song.artists}\n" if song.artists else "",
        "album_line": f"专辑: {song.album}\n" if song.album else "",
        # 无法匹配的提示整行；匹配成功时整行为空，模板里不留痕迹
        "unmatched_line": f"{notice}\n" if notice else "",
        "song": _song_detail_block(song),
        "playlist": await _playlist_placeholder(group_id, state.key),
        "count": str(count),
        "window": state.label,
    }
    if not cfg.enabled:
        return _format_accept(song, index, notice)
    text = render_template(cfg.accept_text, context)
    # 老配置的 accept_text 里没有 {unmatched_line}：把提示补在末尾，
    # 保证「无法匹配」这条信息不会因为用户没改模板而消失。
    if notice and "{unmatched_line}" not in (cfg.accept_text or ""):
        text = f"{text.rstrip()}\n{notice}"
    return text.rstrip()


async def build_sharer_limit_text(song: Song, first: dict, group_id: int) -> str:
    """同一用户本期重复分享时的提示文案（模板见 ``reply.sharer_limit_text``）。

    占位符：
      {nick}      本次分享者（已套昵称映射）
      {title}     其本期**首发**的歌名   {artists} 首发歌歌手
      {platform}  首发歌来源平台
      {index}     首发歌在本期的序号（首发那首没进榜时为 —）
      {count}     本期已收录首数         {window} 窗口文案
    """
    cfg = service.cfg(group_id).reply
    aliases = service.cfg(group_id).playlist.sharer_aliases
    state = service.current_window(group_id)
    nick = resolve_alias(
        song.sharer_name or str(song.sharer_id), song.sharer_id, aliases
    )
    try:
        count = await service.store.count(group_id, state.key)
    except Exception:
        count = 0

    # 首发占位记录的平台存的是原始 key（qq / kugou…），展示前转成中文名
    raw_platform = str((first or {}).get("platform") or "")
    index_text = "—"
    try:
        first_song = await service.store.first_song_of_sharer(
            group_id, state.key, song.sharer_id
        )
        if first_song is not None and first_song.row_id is not None:
            pos = await service.store.position_of(group_id, state.key, first_song.row_id)
            if pos:
                index_text = str(pos)
    except Exception:  # 查库异常不该让提示发不出去
        pass

    context = {
        "nick": nick,
        "title": str((first or {}).get("title") or "（未知歌曲）"),
        "artists": str((first or {}).get("artists") or ""),
        "platform": PLATFORM_NAMES.get(raw_platform, raw_platform),
        "index": index_text,
        "count": str(count),
        "window": state.label,
    }
    return render_template(cfg.sharer_limit_text, context)


async def _reply_song(
    bot: Bot, event: GroupMessageEvent, text: str, song: Song,
    with_card: bool = True, track: bool = False,
) -> None:
    """@分享者 + 文字说明，随后单独补一条音乐卡片（失败自动降级为文字）。

    ``track=True`` 时把这条消息的 id 登记进 ``song_notices``：群里**引用**它 +
    贴网易云链接，就能回填这首歌的匹配（见下面的 ``handle_match_reply``）。
    """
    msg = Message(MessageSegment.at(event.user_id)) + MessageSegment.text(text)
    try:
        resp = await bot.send(event, msg)
    except Exception as exc:
        logger.warning(f"[music] 回复失败: {exc}")
        return
    trace_out(message_preview(msg), f"群{event.group_id}")
    if track:
        await _remember_notice(resp, event.group_id, song)
    if not with_card:
        return
    # 卡片单独发一条：签名服务挂掉时内部会自动降到自定义卡片 / 文字兜底
    way = await send_music_card(bot, event, song, service.cfg(event.group_id).card)
    trace_out(f"卡片《{song.title}》[{way}]", f"群{event.group_id}")
    logger.debug(f"[music] 《{song.title}》卡片发送方式: {way}")


def _message_id_of(resp: object) -> str:
    """从 OneBot ``send`` 的返回里取消息 id（不同实现给 dict 或类 dict 对象）。"""
    if isinstance(resp, dict):
        mid = resp.get("message_id")
    else:
        mid = getattr(resp, "message_id", None)
    return "" if mid is None else str(mid)


async def _remember_notice(resp: object, group_id: int, song: Song) -> None:
    """登记「刚发出的这条提示消息」对应库里的哪首歌，供群里引用时反查。

    只在 ``song.row_id`` 有值（确实入库了）时记；任何异常都只记 debug 日志——
    这是锦上添花的能力，绝不能因为它失败而影响正常回复。
    """
    mid = _message_id_of(resp)
    if not mid or song is None or song.row_id is None:
        return
    try:
        await service.store.record_song_notice(
            mid, group_id, song.window_key, song.row_id
        )
    except Exception as exc:
        logger.debug(f"[music] 登记提示消息映射失败: {exc}")


# ------------------------------------------------ 引用「没法匹配」的提示来指定匹配
#
# 场景：机器人回「⚠️ 这首在网易云没搜到…」后，分享者（或管理员）在群里**引用那条
# 消息**、贴上正确的网易云歌曲链接 —— 就把这首歌绑定到该链接。
# OneBot V11 适配器收到引用时会把被引用消息取回来塞进 ``event.reply``（含
# ``message_id``），我们靠 ``song_notices`` 表把它反查成库里的歌。
# 本响应器 ``block=True`` 且优先级高于分享监听（99），所以这条链接不会被
# ``_looks_like_music`` 当成一次新分享重复收录。

#: 消息里出现这些片段就认为「带网易云链接」，再交给 service 精确解析
_NETEASE_LINK_HINTS = ("music.163.com", "163cn.tv")


async def _is_admin(bot: Bot, event: GroupMessageEvent) -> bool:
    """超管 / 群管理员 / 群主。"""
    if await SUPERUSER(bot, event):
        return True
    return await GROUP_ADMIN(bot, event) or await GROUP_OWNER(bot, event)


def _reply_match_text(event: GroupMessageEvent) -> str:
    """取消息的纯文本（网易云链接常混在文字里）。"""
    return event.message.extract_plain_text()


async def _looks_like_match_reply(bot: Bot, event: GroupMessageEvent) -> bool:
    """规则：引用了机器人某条歌曲提示 + 消息里带网易云链接 + 功能开着。"""
    reply = getattr(event, "reply", None)
    if reply is None:
        return False
    if not service.cfg(event.group_id).playlist.reply_match:
        return False
    text = _reply_match_text(event)
    if not any(h in text for h in _NETEASE_LINK_HINTS):
        return False
    notice = await service.store.resolve_song_notice(
        getattr(reply, "message_id", ""), event.group_id
    )
    return notice is not None


match_reply_listener = on_message(rule=Rule(_looks_like_match_reply), priority=4, block=True)


@match_reply_listener.handle()
async def handle_match_reply(bot: Bot, event: GroupMessageEvent) -> None:
    group_id = event.group_id
    reply = getattr(event, "reply", None)
    notice = await service.store.resolve_song_notice(
        getattr(reply, "message_id", ""), group_id
    )
    if notice is None:  # 规则与处理之间被清理掉的竞态，静默放过
        return
    try:
        song = await service.store.get_song_by_row(int(notice["row_id"]))
    except Exception:
        song = None
    if song is None or song.row_id is None:
        return

    cfg = service.cfg(group_id)
    nick = event.sender.card or event.sender.nickname or str(event.user_id)
    context = {
        "nick": nick,
        "title": song.title,
        "artists": song.artists,
        "matched_title": "",
        "matched_artists": "",
        "reason": "",
        "window": service.current_window(group_id).label,
    }

    # 权限：管理员 或 这首歌的分享者；其余人回提示（本条消息被 block，不会当新分享收录）
    allowed = (
        await _is_admin(bot, event) or int(event.user_id) == int(song.sharer_id or 0)
    )
    if not allowed:
        text = render_template(cfg.reply.match_deny_text, context)
        await _reply_song(bot, event, text, song, with_card=False)
        return

    if song.netease_id:
        context["reason"] = "这首歌已经匹配到网易云了，无需再指定"
        text = render_template(cfg.reply.match_fail_text, context)
        await _reply_song(bot, event, text, song, with_card=False)
        return

    result = await service.match_unmatched_by_row(song.row_id, _reply_match_text(event))
    if not result.get("ok"):
        context["reason"] = str(result.get("message") or "未知原因")
        text = render_template(cfg.reply.match_fail_text, context)
        await _reply_song(bot, event, text, song, with_card=False)
        return

    matched = result.get("song")
    context["matched_title"] = getattr(matched, "title", "") or song.title
    context["matched_artists"] = getattr(matched, "artists", "") or ""
    text = render_template(cfg.reply.match_ok_text, context)
    await _reply_song(bot, event, text, song, with_card=False)
    logger.info(
        f"[music] 群{group_id} {nick}({event.user_id}) 引用回填匹配："
        f"《{song.title}》-> {getattr(matched, 'netease_id', '')}"
    )
    await _sync_after_match(group_id, matched)


async def _sync_after_match(group_id: int, song: Song) -> None:
    """匹配成功后把这首补进本期歌单（当前窗口仍在收集期、且开了「分享即归档」时）。

    后台执行、失败只记日志，不阻塞回复。引用的是旧窗口的歌时不动歌单。
    """
    try:
        cfg = service.cfg(group_id)
        state = service.current_window(group_id)
        if not (cfg.playlist.auto_archive_on_share and state.collecting):
            return
        if song is None or song.window_key != state.key:
            return
        service._spawn_bg(service.auto_archive_songs(group_id, state, [song]))
    except Exception as exc:
        logger.debug(f"[music] 回填匹配后同步歌单失败: {exc}")


async def _reply_llm_link(bot: Bot, event: GroupMessageEvent) -> None:
    """消息里的链接**不是音乐平台的**时，交给大模型接一句。

    ⚠️ **默认关闭**（``llm.reply_link=False``）：群里甩网页 / 视频 / 文章链接时，
    没 @ 机器人就一声不吭——这是群友反馈「收集窗口期间乱插话」后改的默认值。
    想要「任何链接都主动接一句」的老行为，把 ``llm.reply_link`` 打开。

    几道保险（缺一不可）：

    - ``llm.reply_link`` 关着就直接返回（默认如此）；
    - 消息里必须真有 http 链接——纯图片、纯卡片没有什么可聊的；
    - **真·音乐链接一律不碰**（``_has_music_link``）：不在收集期时
      ``handle_segments`` 会直接返回空结果，这里的判定才不会把歌当成普通链接；
    - **@ 过机器人的不碰**：那条走 ``at_listener``（优先级更高），否则会回两遍；
    - 命令不碰；不在生效群 / 没填全接口，静默。
    """
    llm_cfg, why = _llm_gate(event)
    if why or not llm_cfg.reply_link:
        return
    if await _at_bot(event) or _is_command_message(event):
        return
    if _has_music_link(event):
        return
    text, images = read_message(event.message, {str(event.self_id)})
    if not extract_urls(text):
        return  # 没有链接（或链接在卡片里读不出来）→ 没什么可接的
    hint = (llm_cfg.link_hint or "").strip()
    question = f"{text}\n{hint}" if hint else text
    # 链接正文：这一步才是「真看了」和「只看标题瞎猜」的分界线
    context = await _llm_context(bot, event, llm_cfg, text)
    ok, reason = await _llm_answer(bot, event, llm_cfg, question, images, context=context)
    if ok:
        logger.info(f"[llm] 群{event.group_id} 非音乐链接 → 已交给大模型接话")
    elif reason not in ("cooldown", "empty"):
        logger.debug(f"[llm] 群{event.group_id} 非音乐链接接话失败：{reason}")


@music_listener.handle()
async def handle_music_share(bot: Bot, event: GroupMessageEvent) -> None:
    group_id = event.group_id
    if not service.group_enabled(group_id):
        return

    segments = [{"type": seg.type, "data": dict(seg.data)} for seg in event.message]
    sharer_name = event.sender.card or event.sender.nickname or str(event.user_id)

    try:
        result = await service.handle_segments(group_id, segments, event.user_id, sharer_name)
    except Exception as exc:
        logger.exception(f"[music] 处理消息异常: {exc}")
        return

    if not result.any_music:
        # 认不出音乐时过去是直接静默返回。带链接的普通网页进到这里，
        # 让大模型接一句（真音乐链接、@ 过的、命令都由 _reply_llm_link 内部挡掉）。
        await _reply_llm_link(bot, event)
        return

    logger.info(
        f"[music] 分享处理 · 群{group_id} {sharer_name}({event.user_id}) · "
        f"收录 {len(result.accepted)} / 同窗重复 {len(result.duplicated)} / "
        f"已分享过 {len(result.sharer_limited)} / 未识别 {len(result.unidentified)}"
    )

    cfg = service.cfg(group_id)

    # 文字 @+提示始终发送；卡片是否回发由 reply_card 单独控制
    # 无法匹配到网易云的歌：提示并入收录消息同一条，不额外刷屏
    unmatched_ids = {id(song) for song in result.unmatched}
    for song in result.accepted:
        index = result.index_of.get(id(song), 0)
        is_unmatched = id(song) in unmatched_ids
        try:
            text = await build_accept_text(
                song, index, group_id, unmatched=is_unmatched
            )
        except Exception as exc:
            logger.warning(f"[music] 收录回复渲染失败，回退内置格式: {exc}")
            text = _format_accept(
                song, index,
                cfg.reply.unmatched_text.strip() if is_unmatched else "",
            )
        await _reply_song(bot, event, text, song, with_card=cfg.reply_card, track=True)

    # 同一用户本期已经分享过：只收录第一首，其余回一条可自定义的提示
    for song, first in result.sharer_limited:
        try:
            text = await build_sharer_limit_text(song, first, group_id)
        except Exception as exc:
            logger.warning(f"[music] 同用户限一首提示渲染失败，回退内置格式: {exc}")
            text = (
                f" 本期你已经分享过《{(first or {}).get('title') or '歌曲'}》了，"
                "要更换的话请找管理员"
            )
        # 被拦下的歌不入榜，发卡片只会让人误以为收录了，所以只发文字
        await _reply_song(bot, event, text, song, with_card=False)

    if cfg.notify_duplicate:
        for song in result.duplicated:
            index = result.index_of.get(id(song), 0)
            who = resolve_alias(song.sharer_name or str(song.sharer_id), song.sharer_id, cfg.playlist.sharer_aliases)
            text = f" 这首《{song.title}》已经在榜单第 {index} 位了（首发: {who}）"
            await _reply_song(bot, event, text, song, with_card=cfg.reply_card)

    # 总库跨窗口重复提示：仅当总库已存在该歌（曾在不同窗口被分享过），
    # 与上方同窗口重复提示互不冲突、不重复刷屏。
    if cfg.master.enabled and cfg.master.compare_on_share and result.master_duplicated:
        for song in result.master_duplicated:
            index = result.master_index_of.get(id(song), 0)
            try:
                text = await build_master_dup_text(song, index, group_id, sharer_name)
            except Exception as exc:
                logger.warning(f"[music] 总库重复提示渲染失败，回退内置格式: {exc}")
                who = (song.sharer_name or "").strip() or (
                    str(song.sharer_id) if song.sharer_id else "未知（总库导入）"
                )
                period = getattr(song, "src_window", "") or ""
                where = "" if period == "import" else (f"（{period} 期）" if period else "")
                text = (
                    f" 这首《{song.title}》{where}{_fmt_dup_date(song.created_at)}，"
                    f"由 {who} 分享过了哟"
                )
            await _reply_song(bot, event, text, song, with_card=cfg.reply_card)

    if result.unidentified:
        tip = " 这条分享没识别成音乐，已跳过收录（若确实是音乐链接，换种方式再发一次试试）"
        for song in result.unidentified:
            # 没识别出来的没有歌曲信息，发卡片只会再触发一次签名失败，直接跳过
            await _reply_song(bot, event, tip, song, with_card=False)
