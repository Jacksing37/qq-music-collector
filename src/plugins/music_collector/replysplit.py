"""把一条模型回复拆成多条短消息，按真人节奏发出。

为什么要单独成一个模块：拆条的顺序**错一次就全错**——必须先逐段清理、再合并超限段。
反过来的话，被合并的那些段接到「……」后面，它们的开头就不再是行首，
前缀清理会失效（残留的前缀会原样发给群友）。顺序约束只有在纯函数里才测得动。

分条规则（2026-11-06 起「细到逗号」）：

1. **换行必定分条**（连续多个换行算一个分隔）；
2. 每行之内按**句末标点**（``。！？!?…~～``）切，切点留在前一段末尾；
3. 还长的话按**句中标点**（``，,；;``）切——中文聊天里一个逗号就是换一口气，
   真人发 QQ 是「一句一条」，不是整段一条；
4. 切出来的**碎块**（单字 / 纯符号表情，如 ``🐳``）并回上一条，别单独刷屏；
5. 条数超 ``max_parts`` 的，多出来的用「……」并到最后一条（只合并、不丢内容）。

第 3 条是 2026-11-06 的实测反馈改出来的：原来只在「模型给了换行」或「正文 >60 字」
时才切，结果模型用空格断句的 45 字回复整条发出去，群里看着就是一段小作文。

规则来源（均为"借鉴规则、自行实现"）：「模型用换行自控分条」「超过上限用「……」合并」
移植自 qq-companion-bot（MIT, ruoqian-0205）；「按句末标点兜底切分」的思路来自
Warm-Isle 的 ``ReplyComposer``（AGPL-3.0，**仅借鉴规则，未复制代码**）。

本模块不依赖 nonebot / httpx，纯字符串处理，可直接单测。
"""

from __future__ import annotations

import random
import re

__all__ = ["split_reply", "between_delay", "clean_part"]

#: 换行分条（连续多个换行算一个分隔）
_LINE_SPLIT = re.compile(r"\n+")
#: 句末标点：切分点保留在前一段末尾
_SENT_SPLIT = re.compile(r"(?<=[。！？!?…~～])")
#: 句中标点：切分点**丢给** :func:`_tidy` 收尾时去掉（留着就是多余）。
#: 半角逗号后面紧跟数字时不切——``1,000`` 这种千分位不该断成两条消息。
_CLAUSE_SPLIT = re.compile(r"(?<=[，；;])|(?<=,)(?![0-9])")
#: 段末该被丢掉的切点标点（逗号 / 分号是切点本身，不该留在句尾）
_TRAILING_SEP = "，,；;"
#: 区间字符串，如 "0.5-1.5" / "0.5~1.5"
_RANGE_RE = re.compile(r"\A\s*(\d+(?:\.\d+)?)\s*[-~～]\s*(\d+(?:\.\d+)?)\s*\Z")
#: 行首时间戳：[2026-11-05 12:30 周三] / [2026-11-05]
_TIME_PREFIX = re.compile(r"\A\s*\[[0-9]{4}-[0-9]{2}-[0-9]{2}[^\]]{0,24}\]\s*")
#: 行首纯时刻：[12:30] / [12:30:05]
_TIME_ONLY = re.compile(r"\A\s*\[[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?\]\s*")
#: 这段文字里有没有"内容"（汉字 / 字母 / 数字 / 假名 / 谚文）
_HAS_WORD = re.compile(r"[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

#: 短于这个长度的碎块并回前一条（「哈？」2 字要能单独发，「嗯」1 字不行）
MIN_PART_CHARS = 2
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
    for line in _LINE_SPLIT.split(raw):
        parts.extend(_split_line(clean_part(line)))
    if not parts:
        return []

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


def _split_line(line: str) -> list[str]:
    """一行之内：先按句末标点切，再按句中标点切，碎块并回前一条。"""
    out: list[str] = []
    for sentence in _SENT_SPLIT.split(line):
        for clause in _CLAUSE_SPLIT.split(sentence):
            piece = _tidy(clause)
            if not piece:
                continue
            if out and _is_fragment(piece):
                # 碎块直接贴回上一条（「……了~」+「🐳」），不额外补空格
                out[-1] += piece
            else:
                out.append(piece)
    return out


def _tidy(text: str) -> str:
    """去掉首尾空白，以及句尾那个**切点标点**（逗号 / 分号）。"""
    return str(text or "").strip().rstrip(_TRAILING_SEP).strip()


def _is_fragment(text: str) -> bool:
    """太短或纯符号的碎块不该单独成一条消息。

    「哈？」「还问？」这种 2~3 字带标点的短句**是**正常一句，要单独发；
    只有 1 个字（「嗯」）或者整段没有一个文字符号（``🐳``、``——``、``…``）才算碎块。
    """
    s = str(text or "")
    return len(s) < MIN_PART_CHARS or not _HAS_WORD.search(s)


def _cap(parts: list[str], max_parts: int) -> list[str]:
    """条数超限时，多余部分合并成最后一条。"""
    limit = max(1, int(max_parts or 1))
    if len(parts) <= limit:
        return parts
    return parts[: limit - 1] + [MERGE_JOINER.join(parts[limit - 1 :])]
