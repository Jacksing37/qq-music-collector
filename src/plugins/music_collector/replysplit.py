"""把一条模型回复拆成多条短消息，按真人节奏发出。

为什么要单独成一个模块：拆条的顺序**错一次就全错**——必须先逐段清理、再合并超限段。
反过来的话，被合并的那些段接到「……」后面，它们的开头就不再是行首，
前缀清理会失效（残留的前缀会原样发给群友）。顺序约束只有在纯函数里才测得动。

分条规则（2026-11-06 定稿：**只认空行**）：

1. **空行就是分条**（连续两个及以上换行算一个分隔）。模型想分几条就打几个空行——
   它自己的断句意图是唯一依据，本模块不替它判断「这句该不该断」；
2. 单行内的换行、逗号、句号、问号、感叹号**一律不切**，原样保留在该条消息里；
3. 每段清掉行首残留的时间戳前缀（``[2026-11-05 12:30]`` 这类），仅此一项清理；
4. 条数超 ``max_parts`` 的，多出来的用「……」并到最后一条（只合并、不丢内容）。

为什么退回「只看空行」：一度试过按句末标点、再按逗号切（想让 45 字长句也拆成 5 条），
实践下来切得太碎——半句话一条、emoji 掉队、正常逗号被切开，读起来像手滑。
现在把「什么时候停」这件事完全交回模型，发送层只负责按它给的空行分。

规则来源（均为"借鉴规则、自行实现"）：「模型用空行自控分条」「超过上限用「……」合并」
移植自 qq-companion-bot（MIT, ruoqian-0205）。

本模块不依赖 nonebot / httpx，纯字符串处理，可直接单测。
"""

from __future__ import annotations

import random
import re

__all__ = ["split_reply", "between_delay", "clean_part"]

#: 空行分条：连续两个及以上换行（允许行尾空白 / Windows 换行）算一个分隔。
#: **单个换行不算**——那是同一条消息里的折行，切了就是半句话一条。
_PARA_SPLIT = re.compile(r"(?:\r?\n[ \t]*){2,}")
#: 区间字符串，如 "0.5-1.5" / "0.5~1.5"
_RANGE_RE = re.compile(r"\A\s*(\d+(?:\.\d+)?)\s*[-~～]\s*(\d+(?:\.\d+)?)\s*\Z")
#: 行首时间戳：[2026-11-05 12:30 周三] / [2026-11-05]
_TIME_PREFIX = re.compile(r"\A\s*\[[0-9]{4}-[0-9]{2}-[0-9]{2}[^\]]{0,24}\]\s*")
#: 行首纯时刻：[12:30] / [12:30:05]
_TIME_ONLY = re.compile(r"\A\s*\[[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?\]\s*")

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

    parts: list[str] = []
    for chunk in _PARA_SPLIT.split(raw):
        piece = clean_part(chunk)
        if piece:
            parts.append(piece)
    if not parts:
        # 全是空行（不会真出现）：至少别把内容吞掉
        return [raw]

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


def _cap(parts: list[str], max_parts: int) -> list[str]:
    """条数超限时，多余部分合并成最后一条。"""
    limit = max(1, int(max_parts or 1))
    if len(parts) <= limit:
        return parts
    return parts[: limit - 1] + [MERGE_JOINER.join(parts[limit - 1 :])]
