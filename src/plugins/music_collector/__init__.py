"""QQ 群音乐分享收集机器人。

自动识别群里分享的各平台音乐链接 / 卡片，@ 分享者回发歌曲名片，
按时序维护榜单（文字 + 长图），并在设定时刻自动建网易云歌单归档。
"""

from __future__ import annotations

from nonebot import get_driver, on_message, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

require("nonebot_plugin_apscheduler")

from .bot_utils import music_card  # noqa: E402
from .models import Song  # noqa: E402
from .scheduler import reload_jobs  # noqa: E402
from .service import service  # noqa: E402

from . import commands as _commands  # noqa: E402,F401  仅为注册命令

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


def _format_accept(song: Song, index: int) -> str:
    lines = [f" 已收录 · 本期第 {index} 首", song.title]
    if song.artists:
        lines.append(f"歌手: {song.artists}")
    if song.album:
        lines.append(f"专辑: {song.album}")
    lines.append(f"来源: {song.platform_name}")
    return "\n".join(lines)


async def _reply_song(bot: Bot, event: GroupMessageEvent, text: str, song: Song) -> None:
    """@分享者 + 文字说明，随后单独补一条平台原生音乐卡片。"""
    msg = Message(MessageSegment.at(event.user_id)) + MessageSegment.text(text)
    card = music_card(song)
    if card is None and song.cover:
        msg += MessageSegment.image(song.cover)
    try:
        await bot.send(event, msg)
    except Exception as exc:
        logger.warning(f"[music] 回复失败: {exc}")
        return
    if card is not None:
        try:
            # 音乐卡片必须独占一条消息
            await bot.send(event, Message(card))
        except Exception as exc:
            logger.debug(f"[music] 音乐卡片发送失败，降级为纯文本: {exc}")
            if song.cover:
                try:
                    await bot.send(event, Message(MessageSegment.image(song.cover)))
                except Exception:
                    pass


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
    if not cfg.reply_card:
        return

    for song in result.accepted:
        index = result.index_of.get(id(song), 0)
        await _reply_song(bot, event, _format_accept(song, index), song)

    if cfg.notify_duplicate:
        for song in result.duplicated:
            index = result.index_of.get(id(song), 0)
            who = song.sharer_name or str(song.sharer_id)
            text = f" 这首《{song.title}》已经在榜单第 {index} 位了（首发: {who}）"
            await _reply_song(bot, event, text, song)

    if result.outside_window and cfg.window.reply_outside_window:
        song = result.outside_window[0]
        tip = (
            f" 当前不在收集期，这首没有入榜\n"
            f"{song.display()}\n"
            f"发送 /music window 查看收集时间"
        )
        await _reply_song(bot, event, tip, song)
