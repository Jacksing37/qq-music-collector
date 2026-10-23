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


# ---------------------------------------------------------------- 收发消息留痕
#
# 网页端「运行日志」页要能看清「机器人收到了什么、发出去了什么」。这里把消息
# 压成一行摘要（文本 + [图片]/[卡片] 之类的标记）再记日志，避免把整段榜单刷屏。
# 开关是 ``logs.trace_messages``，关掉后不再记录。

_PREVIEW_MARKS: dict[str, str] = {
    "image": "[图片]", "json": "[卡片]", "xml": "[卡片]", "music": "[音乐]",
    "face": "[表情]", "record": "[语音]", "video": "[视频]", "file": "[文件]",
    "at": "[@]", "reply": "[回复]",
}


def message_preview(message: Message | str, limit: int = 160) -> str:
    """把一条消息压成一行摘要，供日志展示。"""
    if isinstance(message, str):
        extra, text = "", message
    else:
        text = message.extract_plain_text()
        seen: list[str] = []
        for seg in message:
            tag = _PREVIEW_MARKS.get(seg.type)
            if tag and tag not in seen:
                seen.append(tag)
        extra = "".join(seen)
    text = " ".join(text.split())          # 折行 / 连续空白压成单个空格
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return (extra + text) or "（空消息）"


# ---------------------------------------------------------------- 给大模型读消息
#
# 「@机器人 + 一句话」之外，群友也会发表情、发图、引用上一条再问。这里把一条消息
# 拆成两部分：**给模型看的文字**（图/表情/语音都变成方括号标记）和**视觉模型能
# 直接读取的图片链接**。没开视觉时标记也不会丢——模型至少知道「对方发了图」。

#: 本身没有可读文本、只留个标记的段
_SEG_MARKS: dict[str, str] = {
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "forward": "[聊天记录]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
}


def _image_url(data: dict) -> str:
    """从 image 段的 data 里找可直接访问的链接；拿不到返回空串。

    OneBot 实现一般给 ``url``（腾讯 CDN，带时效签名，几小时内有效）；
    个别实现把链接放在 ``file`` 里，所以顺带看一眼。
    """
    for key in ("url", "file"):
        value = str(data.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def read_message(
    message: Message | str, skip_at: set[str] | None = None
) -> tuple[str, list[str]]:
    """把一条消息拆成 ``(文字, 图片链接列表)``。

    文字里图片位置保留 ``[图片]``。``llm.vision`` 打开时这些链接会随请求发出去，
    那要求接口背后的模型支持图片输入（视觉模型）；关着就只当占位符。

    ``skip_at`` 是要忽略的 @ 对象（一般是机器人自己的 QQ）——「只 @ 了机器人、
    什么都没说」的消息不该被当成一句提问。
    """
    if isinstance(message, str):
        return " ".join(message.split()), []
    parts: list[str] = []
    images: list[str] = []
    for seg in message:
        kind = seg.type
        data = seg.data or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            qq = str(data.get("qq") or "").strip()
            if skip_at and qq in skip_at:
                continue
            name = str(data.get("name") or qq or "").strip()
            parts.append(f"@{name}" if name else "@")
        elif kind == "image":
            parts.append("[图片]")
            url = _image_url(data)
            if url and url not in images:
                images.append(url)
        elif kind == "mface":
            summary = str(data.get("summary") or "").strip()
            parts.append(f"[表情包: {summary}]" if summary else "[表情包]")
        elif kind == "file":
            filename = str(data.get("name") or data.get("file") or "").strip()
            parts.append(f"[文件: {filename}]" if filename else "[文件]")
        elif kind in _SEG_MARKS:
            parts.append(_SEG_MARKS[kind])
        # reply 段已在 event.reply 里处理；json/xml 卡片对闲聊没信息量，忽略
    return " ".join("".join(parts).split()), images


# ------------------------------------------------------------ 合并转发（聊天记录）
#
# 群友「转发聊天记录」时，OneBot 给的只是一个 ``forward`` 段（带一个 id），
# 内容要再调一次 ``get_forward_msg`` 才拿得到；个别实现直接塞 ``node`` 段。
# 两种都要能读，否则模型只看到 ``[聊天记录]`` 四个字。

#: 一次展开最多几条 / 多少字——聊天记录可以是几百条，不封顶会直接撑爆上下文
FORWARD_LIMIT = 30
FORWARD_CHARS = 2000


def _node_sender(data: dict) -> str:
    """取一条转发消息的发送者昵称（群名片 > 昵称 > QQ 号）。"""
    sender = (data or {}).get("sender") or {}
    if not isinstance(sender, dict):
        return ""
    for key in ("card", "nickname", "user_id"):
        value = str(sender.get(key) or "").strip()
        if value:
            return value
    return ""


def _node_segments(content) -> list:
    """把 ``content`` / ``message`` 归一成消息段列表（可能是字符串，也可能带脏数据）。"""
    if isinstance(content, str):
        return [MessageSegment.text(content)]
    out = []
    for raw in content or []:
        if not isinstance(raw, dict):
            continue
        try:
            out.append(MessageSegment(str(raw.get("type") or "text"), raw.get("data") or {}))
        except Exception:  # 脏段不该让整条聊天记录作废
            continue
    return out


def _forward_nodes(payload) -> list[dict]:
    """把 ``get_forward_msg`` 的返回摊平成 ``[{"who": …, "content": [...]}]``。

    各家返回结构不统一（``messages`` / 直接数组 / 外面再包一层 ``node``），
    所以这里全部容忍，认不出来就返回空——宁可少读，别抛异常。
    """
    if isinstance(payload, dict):
        items = payload.get("messages") or payload.get("message") or []
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    nodes: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        data = item
        if str(item.get("type") or "") == "node" and isinstance(item.get("data"), dict):
            data = item["data"]
        nodes.append(
            {
                "who": _node_sender(data),
                "content": data.get("content", data.get("message", [])),
            }
        )
    return nodes


async def read_forward_nodes(bot: Bot, message: Message | list) -> list[dict]:
    """读出消息里所有合并转发的内容（``forward`` 段需要再调一次接口）。

    嵌套转发**只展开一层**：记录里的记录仍显示成 ``[聊天记录]``，
    否则一条「转发了一堆转发」的消息能无限膨胀。
    """
    nodes: list[dict] = []
    for seg in message:
        kind = getattr(seg, "type", "")
        data = getattr(seg, "data", None) or {}
        if kind == "forward":
            fid = str(data.get("id") or data.get("message_id") or "").strip()
            if not fid:
                continue
            payload = None
            last: Exception | None = None
            for key in ("message_id", "id"):
                try:
                    payload = await bot.call_api("get_forward_msg", **{key: fid})
                    break
                except Exception as exc:  # noqa: BLE001 — 换一种参数名再试
                    last = exc
            if payload is None:
                logger.warning(f"[llm] 读取聊天记录失败（id={fid[:20]}）: {last}")
                continue
            nodes.extend(_forward_nodes(payload))
        elif kind == "node":
            # 内联 node：客户端把内容直接塞在段里，不用再请求
            nodes.append(
                {
                    "who": _node_sender({"sender": data.get("sender") or {}})
                    or str(data.get("nickname") or "").strip(),
                    "content": data.get("content", data.get("message", [])),
                }
            )
    return nodes


def render_forward(
    nodes: list[dict], limit: int = FORWARD_LIMIT, max_chars: int = FORWARD_CHARS
) -> str:
    """把展开后的转发记录渲染成「昵称：内容」多行文本（超长自动收尾）。

    被截断时补一句「还有 N 条未展开」：模型知道「记录不止这些」，才不会
    把半段聊天当成全部、得出「他们没提过这事」这种结论。
    """
    if not nodes:
        return ""
    lines: list[str] = []
    used = 0
    for idx, node in enumerate(nodes):
        if len(lines) >= max(1, limit):
            lines.append(f"…（还有 {len(nodes) - idx} 条未展开）")
            break
        text, _ = read_message(_node_segments(node.get("content")))
        text = text.strip()
        who = (node.get("who") or "").strip()
        line = f"{who}：{text}" if who else text
        if not line.strip("： "):
            continue
        if max_chars > 0 and used + len(line) > max_chars:
            # 半条也别硬塞：直接标出还剩多少条，让模型知道记录被截断了
            lines.append(f"…（还有 {len(nodes) - idx} 条未展开）")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


async def describe_forward(
    bot: Bot,
    message: Message | list,
    limit: int = FORWARD_LIMIT,
    max_chars: int = FORWARD_CHARS,
) -> str:
    """消息里有合并转发就渲染成文本；没有返回空串。"""
    nodes = await read_forward_nodes(bot, message)
    if not nodes:
        return ""
    return render_forward(nodes, limit=limit, max_chars=max_chars)


def quote_prefix_and_images(
    message: Message | str,
    who: str,
    limit: int = 200,
    skip_at: set[str] | None = None,
) -> tuple[str, list[str]]:
    """把被引用的那条消息压成一句前缀，**并带出其中的图片链接**。

    OneBot V11 已经把被引用消息塞进 ``event.reply``，不必再调 ``get_msg``。

    返回 ``(前缀文本, 图片链接列表)``；没有可读内容时返回 ``("", [])``。

    图片链接必须一起返回：只给文字的话，模型只会看到 ``[图片]`` 这个占位符，
    然后回一句「图我这边没瞅见」——群友引用一张图问「什么意思」时就是这么翻车的。
    """
    text, images = read_message(message, skip_at)
    text = text.strip()
    if not text:
        return "", []
    if limit > 0 and len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    name = (who or "").strip() or "对方"
    return f"（引用 {name} 的消息：{text}）", images


def quote_prefix(
    message: Message | str,
    who: str,
    limit: int = 200,
    skip_at: set[str] | None = None,
) -> str:
    """只要文字版前缀（保留旧调用与测试用）；需要图片请用 ``quote_prefix_and_images``。"""
    return quote_prefix_and_images(message, who, limit, skip_at)[0]


def trace_messages_enabled() -> bool:
    """是否记录收发消息（``logs.trace_messages``）。

    延迟导入 ``service``：bot_utils 是更底层的模块，模块级导入会成环。
    """
    try:
        from .service import service

        return bool(getattr(service.config.logs, "trace_messages", True))
    except Exception:  # noqa: BLE001 — 配置尚未就绪时静默跳过，不能影响发消息
        return False


def trace_out(preview: str, where: str = "") -> None:
    """记一条「机器人发出去了什么」。"""
    if trace_messages_enabled():
        logger.info(f"[music] → {where} {preview}".rstrip())


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
    except Exception as exc:
        logger.warning(f"[music] 发送群消息失败 group={group_id}: {exc}")
        return False
    trace_out(message_preview(message), f"群{group_id}")
    return True


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
