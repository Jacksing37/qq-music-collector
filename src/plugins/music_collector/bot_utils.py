"""OneBot 消息构造与发送辅助。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Sequence

from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.log import logger

from .models import Song

# QQ 单条文本消息过长容易被截断，超过就拆分
MAX_TEXT_LEN = 1500

# 平台 -> OneBot 原生音乐卡片 type
_NATIVE_TYPES: dict[str, str] = {
    "netease": "163",
    "qq": "qq",
    "kugou": "kugou",
    "kuwo": "kuwo",
}


def music_card(song: Song) -> Optional[MessageSegment]:
    """构造平台原生音乐卡片。拿不到合适的 id 时返回 None。"""
    kind = _NATIVE_TYPES.get(song.platform)
    if kind and song.song_id.isdigit():
        return MessageSegment.music(kind, int(song.song_id))
    return None


def custom_music_card(song: Song) -> Optional[MessageSegment]:
    """构造自定义音乐卡片。

    自定义卡片由我们自己填标题 / 歌手 / 封面 / 跳转链接，协议端不需要向签名
    服务换取 ArkShare 结构，因此在签名服务 500 时通常仍然能发出去。
    没有可跳转链接时返回 None（卡片没链接就没意义了）。
    """
    url = song.url or ""
    if not url.startswith("http"):
        return None
    # 手动拼 data：适配器的 music_custom 会把缺省字段填成 null，
    # 部分协议端对 null 字段直接报「消息体无法解析」，所以空值就不带这个键。
    data: dict[str, str] = {
        "type": "custom",
        "url": url,
        # 没有音频直链就退而用页面地址，点开跳转到平台播放
        "audio": url,
        "title": (song.title or "未知歌曲")[:60],
    }
    content = song.artists or song.platform_name
    if content:
        data["content"] = content[:60]
    if song.cover and song.cover.startswith("http"):
        data["image"] = song.cover
    return MessageSegment("music", data)


def song_fallback_text(song: Song) -> str:
    """卡片发不出去时的纯文字兜底，保证信息不丢。"""
    lines = [f"🎵 {song.title or '未知歌曲'}"]
    if song.artists:
        lines.append(f"歌手：{song.artists}")
    if song.album:
        lines.append(f"专辑：{song.album}")
    lines.append(f"来源：{song.platform_name}")
    if song.url:
        lines.append(song.url)
    return "\n".join(lines)


class CardBreaker:
    """平台级熔断器。

    签名服务一旦挂掉，它对该平台的**所有**歌曲都会挂，没必要每首歌都去等一次
    超时。连续失败到阈值就熔断，冷却期内直接跳过卡片走文字兜底；冷却结束后
    自动放行一次试探，成功即恢复。
    """

    def __init__(self) -> None:
        self._fails: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def blocked(self, key: str, threshold: int, cooldown_minutes: float) -> bool:
        if threshold <= 0:
            return False
        until = self._open_until.get(key, 0.0)
        if until and time.monotonic() < until:
            return True
        if until:
            # 冷却到期：清空计数，放行一次试探
            self._open_until.pop(key, None)
            self._fails[key] = 0
        return False

    def record_fail(self, key: str, threshold: int, cooldown_minutes: float) -> bool:
        """记一次失败，返回本次是否触发熔断。"""
        if threshold <= 0:
            return False
        count = self._fails.get(key, 0) + 1
        self._fails[key] = count
        if count >= threshold:
            self._open_until[key] = time.monotonic() + max(cooldown_minutes, 0.1) * 60
            self._fails[key] = 0
            return True
        return False

    def record_ok(self, key: str) -> None:
        self._fails.pop(key, None)
        self._open_until.pop(key, None)

    def reset(self) -> None:
        self._fails.clear()
        self._open_until.clear()

    def status(self) -> str:
        if not self._fails and not self._open_until:
            return "全部正常"
        now = time.monotonic()
        parts = []
        for key, until in self._open_until.items():
            left = max(0, int(until - now))
            parts.append(f"{key}: 熔断中（{left}s 后重试）")
        for key, count in self._fails.items():
            if key not in self._open_until and count:
                parts.append(f"{key}: 连续失败 {count} 次")
        return "；".join(parts) or "全部正常"


card_breaker = CardBreaker()


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


# ---------------------------------------------------------------- 音乐卡片


async def _try_send(bot: Bot, event, message: Message) -> Optional[str]:
    """发送并把异常转成简短错误串；成功返回 None。"""
    try:
        await bot.send(event, message)
        return None
    except Exception as exc:
        return str(exc) or exc.__class__.__name__


async def send_music_card(bot: Bot, event, song: Song, cfg) -> str:
    """按配置发送音乐卡片，自动降级。返回实际采用的方式，便于日志/诊断。

    降级链（mode=native 时）::

        原生卡片 → 自定义卡片 → 文字兜底（歌名/歌手/链接 + 封面）

    签名服务返回 500 时原生卡片必然失败，此时靠后两级保证信息不丢；
    同一平台连续失败到阈值则熔断，冷却期内直接走文字兜底不再空等。
    """
    mode = getattr(cfg, "mode", "native")
    threshold = int(getattr(cfg, "failure_threshold", 3) or 0)
    cooldown = float(getattr(cfg, "cooldown_minutes", 10) or 0)
    key = song.platform or "unknown"

    attempts: list[tuple[str, MessageSegment]] = []
    if mode != "off" and not card_breaker.blocked(key, threshold, cooldown):
        if mode == "native":
            native = music_card(song)
            if native is not None:
                attempts.append(("原生卡片", native))
            if getattr(cfg, "fallback_custom", True):
                custom = custom_music_card(song)
                if custom is not None:
                    attempts.append(("自定义卡片", custom))
        elif mode == "custom":
            custom = custom_music_card(song)
            if custom is not None:
                attempts.append(("自定义卡片", custom))

    last_error = ""
    for label, seg in attempts:
        # 音乐卡片必须独占一条消息，不能和文字混在同一条里
        error = await _try_send(bot, event, Message(seg))
        if error is None:
            card_breaker.record_ok(key)
            return label
        last_error = error
        logger.warning(f"[music] {label}发送失败（{song.platform_name}）: {error}")

    if attempts:
        if card_breaker.record_fail(key, threshold, cooldown):
            logger.warning(
                f"[music] {song.platform_name} 卡片连续失败已熔断，"
                f"{cooldown:g} 分钟内改用文字兜底（多为签名服务不可用）"
            )

    if not getattr(cfg, "fallback_text", True):
        return "已跳过"

    message = Message(MessageSegment.text(song_fallback_text(song)))
    if getattr(cfg, "fallback_cover", True) and song.cover:
        message += MessageSegment.image(song.cover)
    error = await _try_send(bot, event, message)
    if error is not None:
        logger.warning(f"[music] 文字兜底也发送失败: {error}（前序错误: {last_error}）")
        return "发送失败"
    return "文字兜底"
