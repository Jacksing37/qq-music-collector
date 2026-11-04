"""把一条模型回复拆成多条短消息，按真人节奏发出。

为什么单独成一个模块：拆条的顺序**错一次就全错**——必须先逐段清理、再合并超限段。
反过来的话，被合并的那些段接到「……」后面，它们的开头就不再是行首，
前缀清理会失效（残留的前缀会原样发给群友）。顺序约束只有在纯函数里才测得动。

规则来源（均为"借鉴规则、自行实现"）：

- 「模型用换行自控分条」「超过上限用「……」合并到最后一条」「≤6 字碎片并回前一条」
  移植自 qq-companion-bot（MIT, ruoqian-0205）。
- 「无换行时按句末标点兜底切分、再按逗号二次拆」的思路来自 Warm-Isle 的
  ``ReplyComposer``（AGPL-3.0，**仅借鉴规则，未复制代码**）。

本模块不依赖 nonebot / httpx，纯字符串处理，可直接单测。
"""

from __future__ import annotations

import random
import re

__all__ = ["split_reply", "between_delay", "clean_part"]

#: 句末标点：切分点保留在前一段末尾
_SENT_SPLIT = re.compile(r"(?<=[。！？!?~～…])")
#: 换行分条（连续多个换行算一个分隔）
_LINE_SPLIT = re.compile(r"\n+")
#: 区间字符串，如 "0.5-1.5" / "0.5~1.5"
_RANGE_RE = re.compile(r"\A\s*(\d+(?:\.\d+)?)\s*[-~～]\s*(\d+(?:\.\d+)?)\s*\Z")
#: 行首时间戳：[2026-11-05 12:30 周三] / [2026-11-05]
_TIME_PREFIX = re.compile(r"\A\s*\[[0-9]{4}-[0-9]{2}-[0-9]{2}[^\]]{0,24}\]\s*")
#: 行首纯时刻：[12:30] / [12:30:05]
_TIME_ONLY = re.compile(r"\A\s*\[[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?\]\s*")

#: ≤ 这个长度的片段并回前一条，避免「嗯。」「啊。」连发
FRAGMENT_MAX = 6
#: 没有换行、且超过这个长度才做标点兜底切分（短回复不该被切）
SOFT_SPLIT_AT = 60
#: 超限合并时用的连接符
MERGE_JOINER = "……"


def clean_part(text: str) -> str:
    """清掉一段开头的残留时间戳并去掉首尾空白。

    只处理**行首**的时间戳方括号。刻意不剥「昵称：」这类前缀——正常回复里
    「注意：」「顺便说一下：」都是合法开头，误删比不删更糟。真正需要剥的
    「行首 @昵称」由 :func:`mention.strip_leading_at` 负责。
    """
    s = _TIME_PREFIX.sub("", str(text or ""))
    s = _TIME_ONLY.sub("", s)
    return s.strip()


def split_reply(text: str, *, enabled: bool = True, max_parts: int = 10) -> list[str]:
    """把回复拆成要依次发出的若干段，至少返回一段（空输入返回空列表）。

    ``enabled`` 为 ``False`` 时**原样一条返回**：不清理、不切分。关掉开关就必须
    与改造前逐字一致——"顺手清掉时间戳"听着无害，但那已经是行为变化，
    而且要清理就该在打开功能时一起生效，不能借默认路径偷偷改输出。

    ``max_parts`` 是发送条数上限，超出部分用 :data:`MERGE_JOINER` 合并到最后一条
    （越拆越多会让群里刷屏，而且一条回复拆成十几段并不像真人）。
    """
    raw = str(text or "")
    if not raw.strip():
        return []

    if not enabled:
        return [raw]

    parts = [p for p in (clean_part(line) for line in _LINE_SPLIT.split(raw)) if p]
    if not parts:
        return []

    # 只有一段（模型没给换行）时才退而用标点切；模型给了换行就以它为准。
    # 碎片回并**只作用于算法切分的结果**：中文里「第一句」才 3 个字，若对模型
    # 主动给的换行也做回并，整条回复会被吞成一句——模型断的句是它想好的停顿。
    if len(parts) == 1:
        parts = _merge_fragments(_split_by_punct(parts[0]))

    return _cap(parts, max_parts)


def between_delay(interval: str) -> float:
    """把 ``"0.5-1.5"`` 解析成随机秒数；解析失败返回 0（宁可快，不可卡住不发）。"""
    m = _RANGE_RE.match(str(interval or ""))
    if not m:
        return 0.0
    low, high = float(m.group(1)), float(m.group(2))
    if low > high:
        low, high = high, low
    if high <= 0:
        return 0.0
    return random.uniform(low, high)


def _split_by_punct(text: str) -> list[str]:
    """无换行时的兜底：先按句末标点切，仍只有一段再尝试在第一个逗号处断开。"""
    if len(text) <= SOFT_SPLIT_AT:
        return [text]
    pieces = [p for p in (seg.strip() for seg in _SENT_SPLIT.split(text)) if p]
    if len(pieces) > 1:
        return pieces
    return _split_at_comma(text)


def _split_at_comma(text: str) -> list[str]:
    """在第一个逗号处断成两条（真人说话听到一半会停一下）。

    左右各要够长（≥5 / ≥4 字）才切——否则会把「行吧，那就这样」切得很碎。
    """
    idx = -1
    for i, ch in enumerate(text):
        if ch in "，,":
            idx = i
            break
    if idx < 0:
        return [text]
    left = text[:idx].strip()
    right = text[idx + 1 :].strip()
    if len(left) < 5 or len(right) < 4:
        return [text]
    return [left, right]


def _merge_fragments(parts: list[str]) -> list[str]:
    """把过短的片段并回前一条（「嗯。」结尾这类不该单独发一条）。

    只用于 :func:`_split_by_punct` 的结果——算法切分可能在末尾留一个碎块，
    而模型自己用换行断的句是它有意为之，必须原样保留。
    """
    merged: list[str] = []
    for part in parts:
        if merged and len(part) <= FRAGMENT_MAX:
            merged[-1] += part
        else:
            merged.append(part)
    return merged or parts


def _cap(parts: list[str], max_parts: int) -> list[str]:
    """条数超限时，多余部分合并成最后一条。"""
    limit = max(1, int(max_parts or 1))
    if len(parts) <= limit:
        return parts
    return parts[: limit - 1] + [MERGE_JOINER.join(parts[limit - 1 :])]
