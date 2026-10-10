"""QQ 群音乐分享收集机器人。

自动识别群里分享的各平台音乐链接 / 卡片，@ 分享者回发歌曲名片，
按时序维护榜单（文字 + 长图），并在设定时刻自动建网易云歌单归档。
"""

from __future__ import annotations

import time

from nonebot import get_driver, on_message, require
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    GROUP_ADMIN,
    GROUP_OWNER,
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.log import logger
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

require("nonebot_plugin_apscheduler")

from .bot_utils import (  # noqa: E402
    message_preview,
    safe_send_group,
    send_music_card,
    trace_messages_enabled,
    trace_out,
)
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


@at_listener.handle()
async def handle_at(bot: Bot, event: GroupMessageEvent) -> None:
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
