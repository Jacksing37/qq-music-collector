"""回复里「@ 谁」的候选集、唯一匹配与校验。

**只走工具**：模型必须调 ``mention`` 工具点人。正文里手写的 ``@某某`` 一律当普通
文字发出去 —— 用户明确要求「不解析正文」，因为那种兜底最容易误 @ 到不相干的人。

**「拿不准就不 @」是代码裁决的，不是靠模型自觉**。实现方式是把「可以 @ 谁」收敛成
一份**小名单**，名单外的人模型知道名字也点不动：

1. 这份名单**只装本次上下文里出现过的人**——
   - 本条消息的发送者（在问你）
   - 被引用那条的发送者（你引用的就是他说的）
   - 本条 / 被引用消息里被 @ 的人
   - 本次正文里被点名的群档案（「让 XX 回答」这种）
   群档案里其他无关的人**不进来**：模型从记忆里翻出一个名字也 @ 不出去。
2. 目标必须**唯一可判定**：昵称命中多人又没有 QQ 号 → 直接拒绝，把候选回给它选。
3. 一次最多 ``llm.mention_max`` 个；禁止 @ 机器人自己、禁止 @ 全体成员。
4. 拒绝时把「能 @ 的只有这些人」一起回给模型，让它自己改，而不是静默失败。

为什么这么保守：QQ 的 @ 会红点 + 提示音，**误 @ 的代价（把不相干的人拽进对话）
远大于漏 @**（漏了还有 ``llm.at_sender`` 兜底 @ 提问者）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

#: @ 全体成员 / @ 自己 这类特殊值，永远不让过
_FORBIDDEN_QQ = {0, -1}


@dataclass(frozen=True)
class Target:
    """一个「可以 @」的候选。``why`` 是给模型看的出处，它靠这个判断该不该点。"""

    qq: int
    nick: str = ""
    aliases: tuple[str, ...] = ()
    why: str = ""

    @property
    def display(self) -> str:
        """给模型看的写法：``Luvu53lfplz(1778690479)``。"""
        return f"{self.nick}({self.qq})" if self.nick else str(self.qq)

    def names(self) -> list[str]:
        """能被用来指他的所有名字（昵称 + 别名），去空去重。"""
        out: list[str] = []
        for name in (self.nick, *self.aliases):
            text = (name or "").strip()
            if text and text not in out:
                out.append(text)
        return out


def _norm(text: str) -> str:
    """比对名字用：去空白、忽略大小写与全半角差异。"""
    import unicodedata

    return unicodedata.normalize("NFKC", str(text or "")).strip().casefold()


def _add(out: dict[int, Target], qq: int, nick: str, why: str, aliases: Iterable[str] = ()) -> None:
    """把一个人加进候选集；同一个 QQ 只保留第一次的出处（先到的更具体）。"""
    try:
        qq = int(qq)
    except (TypeError, ValueError):
        return
    if qq <= 0 or qq in _FORBIDDEN_QQ:
        return
    if qq in out:
        return
    out[qq] = Target(
        qq=qq,
        nick=str(nick or "").strip(),
        aliases=tuple(str(a).strip() for a in aliases if str(a or "").strip()),
        why=str(why or "").strip(),
    )


def build_targets(
    *,
    sender_qq: int = 0,
    sender_nick: str = "",
    quote_qq: int = 0,
    quote_nick: str = "",
    at_qqs: Iterable[tuple[int, str]] = (),
    text: str = "",
    people: Iterable[dict] = (),
    self_qq: int = 0,
    limit: int = 12,
) -> list[Target]:
    """组装本次「可 @ 名单」。

    ``at_qqs`` 是 ``(qq, 昵称)``；``people`` 是群档案（``{"qq","nick","aliases"}``），
    只有**名字在 ``text`` 里出现过**的才会被收进来 —— 这就是「正文里被点名」的那类。
    ``self_qq`` 是机器人自己，永远排除。
    """
    out: dict[int, Target] = {}
    chat_text = _norm(text)
    # 档案索引：发送者/被引用者/被 @ 的人常常也有档案，把别名一起带上，
    # 模型才能用「老名」这种日常叫法点人
    pindex: dict[int, dict] = {}
    for person in people or ():
        try:
            qq = int(person.get("qq") or 0)
        except (TypeError, ValueError):
            continue
        if qq:
            pindex.setdefault(qq, person)

    def aliases_of(qq: int) -> tuple[str, ...]:
        person = pindex.get(int(qq or 0)) or {}
        return tuple(str(a).strip() for a in (person.get("aliases") or []) if str(a or "").strip())

    if sender_qq and int(sender_qq) != int(self_qq or 0):
        _add(out, sender_qq, sender_nick, "在问你", aliases_of(sender_qq))
    if quote_qq and int(quote_qq) != int(self_qq or 0):
        _add(out, quote_qq, quote_nick, "你引用的那条就是他说的", aliases_of(quote_qq))
    for qq, nick in list(at_qqs or ()):
        if int(qq or 0) == int(self_qq or 0):
            continue
        _add(out, qq, nick, "这条消息里 @ 了他", aliases_of(qq))

    # 正文里被点名的群档案：名字要够长（1 个字的名字太容易误命中）
    for person in people or ():
        qq = int(person.get("qq") or 0)
        if not qq or qq == int(self_qq or 0) or qq in out:
            continue
        nick = str(person.get("nick") or "").strip()
        aliases = person.get("aliases") or []
        hit = ""
        for name in [nick, *[str(a) for a in aliases]]:
            key = _norm(name)
            if len(key) >= 2 and key in chat_text:
                hit = name
                break
        if hit:
            _add(out, qq, nick or hit, "正文里提到了他", aliases)

    targets = list(out.values())
    return targets[: max(1, int(limit))]


def resolve(who: str, targets: list[Target]) -> tuple[Optional[int], str]:
    """把模型给的 ``who`` 解析成 QQ。返回 ``(qq, 错误说明)``，成功时错误说明为空。

    认三种写法：``Luvu53lfplz`` / ``Luvu53lfplz(1778690479)`` / ``1778690479``。
    """
    raw = (who or "").strip()
    if not raw:
        return None, "没说要 @ 谁"
    by_qq = {t.qq: t for t in targets}

    # ① 纯 QQ 号
    digits = raw.strip("()（）@ ")
    if digits.isdigit():
        qq = int(digits)
        if qq in by_qq:
            return qq, ""
        return None, f"这条消息里没有可 @ 的 {qq}（不是本人/不在本次上下文里）"

    # ② 昵称(QQ) 形式
    normalised = raw.replace("（", "(").replace("）", ")")
    if normalised.endswith(")") and "(" in normalised:
        head, _, tail = normalised.rpartition("(")
        inner = tail.rstrip(")")
        if inner.isdigit():
            qq = int(inner)
            target = by_qq.get(qq)
            if target is None:
                return None, f"这条消息里没有可 @ 的 {inner}"
            if head.strip() and _norm(head) not in [_norm(n) for n in target.names()]:
                return None, f"{inner} 的昵称对不上（是 {target.display}），确认一下再 @"
            return qq, ""

    # ③ 按名字匹配
    want = _norm(raw.lstrip("@"))
    hits = [t for t in targets if want in [_norm(n) for n in t.names()]]
    if not hits:
        return None, f"没有叫「{raw}」的人可以 @"
    if len(hits) > 1:
        names = "、".join(t.display for t in hits)
        return None, f"「{raw}」对上好几个人：{names}。用 昵称(QQ号) 指定，比如 {hits[0].display}"
    return hits[0].qq, ""


def render_hint(targets: list[Target]) -> str:
    """给模型看的「这次能 @ 的人」清单；没人可 @ 时返回空串。"""
    if not targets:
        return ""
    rows = [f"- {t.display}（{t.why}）" if t.why else f"- {t.display}" for t in targets]
    return (
        "【这次能 @ 的人】\n"
        + "\n".join(rows)
        + "\n（如果这条回复是在回应或评价上面某个人、或者他需要看到你的回答，"
        "先调 mention 点他；名单外的名字点不到，也不许自己写「@某某」的文字。）"
    )


def strip_leading_at(text: str, names: Iterable[str]) -> tuple[str, str]:
    """把正文**开头**那串手写的 ``@昵称`` 去掉，返回 ``(新正文, 被去掉的名字)``。

    为什么要去：模型很爱在开头写一个 ``@某某``（纯文字），而发送时我们又可能真的
    给同一个人加了 at 段——两样叠在一起，群里就是「同一个人被 @ 两次」（线上真发生过：
    21:54 那条「[真@菜老名] @菜老名 发这种装可爱的表情包…」）。

    只在**开头**去，且只去名字对得上 ``names``（这次真要 at 的人）的那些：模型在句子
    中间提别人、或 @ 一个我们没打算 at 的人，都不动。
    """
    body = str(text or "")
    cand = [str(n).strip() for n in (names or ()) if str(n or "").strip()]
    if not cand:
        return body, ""
    dropped: list[str] = []
    for _ in range(3):                       # 最多连剥三个（「@A @B 正文」）
        stripped = body.lstrip()
        if not stripped.startswith("@"):
            break
        rest = stripped[1:]
        hit = ""
        for name in cand:
            # 按原串切同样长度再比，避免全半角/大小写差异把切片位置弄错
            if len(rest) >= len(name) and _norm(rest[: len(name)]) == _norm(name):
                if len(name) > len(hit):
                    hit = name
        if not hit:
            break
        body = rest[len(hit):].lstrip(" ：:，,、")
        # 「@菜老名(2531546239) 正文」这种带 QQ 的写法：括号那一截也一起剥
        body = re.sub(r"^[（(]\s*@?\s*\d{5,12}\s*[)）]", "", body).lstrip(" ：:，,、")
        if hit not in dropped:
            dropped.append(hit)
    return body, "、".join(dropped)

def summary(targets: list[Target], qqs: Iterable[int]) -> str:
    """把已记下的 QQ 变成一句人话（日志 / 追踪用）。"""
    by_qq = {t.qq: t for t in targets}
    return "、".join(
        (by_qq[q].display if q in by_qq else str(q)) for q in qqs
    )
