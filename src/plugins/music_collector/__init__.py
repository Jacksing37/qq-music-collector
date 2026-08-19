"""QQ 群音乐分享收集机器人。

自动识别群里分享的各平台音乐链接 / 卡片，@ 分享者回发歌曲名片，
按时序维护榜单（文字 + 长图），并在设定时刻自动建网易云歌单归档。
"""

from __future__ import annotations

import time

from nonebot import get_driver, on_message, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

require("nonebot_plugin_apscheduler")

from .bot_utils import safe_send_group, send_music_card  # noqa: E402
from .models import Song  # noqa: E402
from .naming import build_context, render_template  # noqa: E402
from .scheduler import reload_jobs  # noqa: E402
from .service import service  # noqa: E402

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
    cfg = service.config
    state = service.current_window()
    group_id = event.group_id
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
    cfg = service.config.intro
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


def _format_accept(song: Song, index: int) -> str:
    lines = [f" 已收录 · 本期第 {index} 首", song.title]
    if song.artists:
        lines.append(f"歌手: {song.artists}")
    if song.album:
        lines.append(f"专辑: {song.album}")
    lines.append(f"来源: {song.platform_name}")
    return "\n".join(lines)


async def _reply_song(
    bot: Bot, event: GroupMessageEvent, text: str, song: Song, with_card: bool = True
) -> None:
    """@分享者 + 文字说明，随后单独补一条音乐卡片（失败自动降级为文字）。"""
    msg = Message(MessageSegment.at(event.user_id)) + MessageSegment.text(text)
    try:
        await bot.send(event, msg)
    except Exception as exc:
        logger.warning(f"[music] 回复失败: {exc}")
        return
    if not with_card:
        return
    # 卡片单独发一条：签名服务挂掉时内部会自动降到自定义卡片 / 文字兜底
    way = await send_music_card(bot, event, song, service.config.card)
    logger.debug(f"[music] 《{song.title}》卡片发送方式: {way}")


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

    cfg = service.config

    # 文字 @+提示始终发送；卡片是否回发由 reply_card 单独控制
    for song in result.accepted:
        index = result.index_of.get(id(song), 0)
        await _reply_song(bot, event, _format_accept(song, index), song, with_card=cfg.reply_card)

    if cfg.notify_duplicate:
        for song in result.duplicated:
            index = result.index_of.get(id(song), 0)
            who = song.sharer_name or str(song.sharer_id)
            text = f" 这首《{song.title}》已经在榜单第 {index} 位了（首发: {who}）"
            await _reply_song(bot, event, text, song, with_card=cfg.reply_card)

    if result.unidentified:
        tip = " 这条分享没识别成音乐，已跳过收录（若确实是音乐链接，换种方式再发一次试试）"
        for song in result.unidentified:
            # 没识别出来的没有歌曲信息，发卡片只会再触发一次签名失败，直接跳过
            await _reply_song(bot, event, tip, song, with_card=False)
