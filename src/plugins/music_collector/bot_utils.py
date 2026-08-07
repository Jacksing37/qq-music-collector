"""OneBot 消息构造与发送辅助。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.log import logger

from .models import Song

# QQ 单条文本消息过长容易被截断，超过就拆分
MAX_TEXT_LEN = 1500


def music_card(song: Song) -> Optional[MessageSegment]:
    """构造平台原生音乐卡片。拿不到合适的 id 时返回 None。"""
    if song.platform == "netease" and song.song_id.isdigit():
        return MessageSegment.music("163", int(song.song_id))
    if song.platform == "qq" and song.song_id.isdigit():
        return MessageSegment.music("qq", int(song.song_id))
    if song.platform == "kugou" and song.song_id.isdigit():
        return MessageSegment.music("kugou", int(song.song_id))
    if song.platform == "kuwo" and song.song_id.isdigit():
        return MessageSegment.music("kuwo", int(song.song_id))
    return None


def image_segment(path: Path) -> Optional[MessageSegment]:
    """读成 bytes 再发，避免协议端与机器人不在同一台机器时 file:// 失效。"""
    try:
        return MessageSegment.image(path.read_bytes())
    except OSError as exc:
        logger.warning(f"[music] 读取图片失败 {path}: {exc}")
        return None


def split_text(text: str, limit: int = MAX_TEXT_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buffer: list[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) + 1 > limit and buffer:
            chunks.append("\n".join(buffer))
            buffer, size = [], 0
        buffer.append(line)
        size += len(line) + 1
    if buffer:
        chunks.append("\n".join(buffer))
    return chunks


async def safe_send_group(bot: Bot, group_id: int, message: Message | str) -> bool:
    try:
        await bot.send_group_msg(group_id=group_id, message=message)
        return True
    except Exception as exc:
        logger.warning(f"[music] 发送群消息失败 group={group_id}: {exc}")
        return False


async def send_report(
    bot: Bot, group_id: int, text: str, images: Sequence[Path]
) -> None:
    """发送榜单：文字分段 + 图片逐张。"""
    for chunk in split_text(text):
        await safe_send_group(bot, group_id, Message(chunk))
    for path in images:
        seg = image_segment(path)
        if seg is not None:
            await safe_send_group(bot, group_id, Message(seg))
