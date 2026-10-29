"""从群聊里学「这个群平时怎么说话」，写成一张风格卡，注入系统提示词。

和 :mod:`digest` 的分工要分清楚，这是两件不同的事：

- ``digest`` 学的是**事实**：「谁是谁、爱吃什么、和谁熟」。粒度是每人一句陈述句
  （20 字以内、省略主语），它能回答「这个人是谁」，但学不到「这个群怎么说话」。
- ``style`` 学的是**语体**：句长、分行、标点、口头禅，以及最关键的——**一批原话
  样本**。写「语气活泼」对模型几乎没影响，给它看五条「绷不住了属实是」「？你认真
  的吗。。。」它立刻就能对上频道。

三条设计红线（和 ``digest`` 同源）：

1. **整卡重写，不追加**。追加式的风格记忆半年后会变成一堆自相矛盾的形容词；而且
   越写越长，最后把人设挤掉。旧卡轮转备份，留最近 3 版。
2. **失败绝不覆盖旧卡**。模型没回、不是 JSON、样本太少，一律保留原来的卡——一张
   旧卡总比一张空卡强。
3. **只学共性，不学某个人**。素材里不带昵称和 QQ 号，提示词里也明令禁止写人名。
   让机器人带上某位群友的口癖，观感上就是「冒充」，这个界限不能越。

文件落 ``data/memory/<群号>/style.md``：正文**就是**注入内容，所以网页上直接改完
立刻生效，不需要另一套渲染。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from nonebot.log import logger

from .config import LLMConfig, MemoryConfig
from .llm import llm_bot
from .memory import atomic_write, group_dir
from .service import service

#: 蒸馏任务的系统提示词。**刻意不给人设**：这里要的是语言观察员，不是群里那个
#: 会接梗的机器人——让它带着人格去总结语体，它会把「自己理想中的说话方式」写进去。
STYLE_SYSTEM = (
    "你是语言观察员。你的任务是从一段 QQ 群聊天里读出这个群「怎么说话」——"
    "句长、分行、标点、口头禅、接梗方式，而不是「聊了什么」。"
    "只依据给定材料归纳，不推测、不评价、不美化。"
)

#: 给模型的指令。要点：要求「逐字引用」，否则它会自己编一句「像群里会说的话」，
#: 那种伪造样本用来做 few-shot 是有害的——模型会去学一句没人说过的话。
_PROMPT = """下面是 QQ 群 {gid} 最近 {days} 天的部分聊天记录（已去掉链接、命令和重复内容）。
每行开头没有昵称，因为要学的是**这个群整体的说话方式**，不是某个人。

【聊天记录】
{material}

请归纳这个群的说话习惯，只输出 JSON，不要任何解释文字：
{{"length":"句长与分行习惯","punct":"标点与表情习惯","words":["高频词或口头禅"],"avoid":["这个群基本不用的表达"],"samples":["逐字原话"]}}

要求：
1. 只写「怎么说」，不写「聊什么」，不评价内容好坏、不写群友性格。
2. samples 必须是材料里**逐字出现过**的原话，6~10 条，每条 3~40 字。优先挑最能
   代表语感的：口头禅、极短句、接梗方式、标点怪癖。不要改写、不要拼接两条。
3. words 最多 8 个，只收材料里**反复出现**的词；只出现一次的不算口头禅。
4. avoid 写这个群里几乎看不到的表达（比如成套的书面连接词、敬语、客服腔），
   没有就给空数组。
5. 不写人名、昵称、QQ 号、手机号、链接、地址。
6. 材料太短、看不出规律时，对应字段给空字符串或空数组，**不要编**。
"""

#: 一条原话的长度区间。下限 2 是留给「绷」「寄」这类极短梗，上限 60 是因为
#: 长段落反映的是「在讲一件事」而不是语体，混进来会把句长带偏。
MIN_LEN = 2
MAX_LEN = 60

#: 链接、CQ 码、卡片：旁听已经过滤了命令和纯图片，这里再兜一层
_NOISE = re.compile(r"https?://|www\.|\[CQ:|分享单曲|分享歌单|分享视频")
#: 至少得有一个汉字或字母，纯数字/纯标点没有语体信息
_WORDY = re.compile(r"[\u4e00-\u9fa5A-Za-z]")
#: 模型偶尔会给原话加「小明：」前缀，虽然明令禁止；剥掉，别让身份混进样本
_SPEAKER_PREFIX = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9_]{1,8}\s*[：:]\s*")
#: 版本文件保留几份
_KEEP_VERSIONS = 3

#: 一次最多把多少条原话喂给模型（再多人也读不出更多规律，只是烧 token）
_MAX_ITEMS = 400
#: 素材的硬上限字数。约 6k token，足够模型看出语体，又不会慢到超时。
_MAX_MATERIAL_CHARS = 12000
#: 少于此数就这一轮不学。**不能按人数乘配额定**：小群三个人、每人 6 条才 18 条，
#: 按 20 卡就会永远学不到；10 条已经足够看出句长和口癖了。
_MIN_MATERIAL = 10

#: 注入时的固定前缀。约束句不能省：样本是别人的原话，不声明清楚模型会直接复述，
#: 或者用「我说过」的口气引用。
_HEAD_HINT = "（下面是这个群平时怎么说话，照这个口气说；别照搬原话的内容，别声称是某个人说的）"


# ---------------------------------------------------------------- 路径与读写


def style_path(mem: MemoryConfig, group_id: int) -> Path:
    return group_dir(mem, group_id) / "style.md"


def _version_path(mem: MemoryConfig, group_id: int, index: int) -> Path:
    return group_dir(mem, group_id) / f"style.v{index}.md"


_FRONTMATTER = re.compile(r"\A\ufeff?---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


def read_style(mem: MemoryConfig, group_id: int) -> str:
    """读风格卡正文（去掉 frontmatter）。**绝不抛异常**：卡坏了不能耽误回话。"""
    path = style_path(mem, group_id)
    if not path.exists():
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"[style] 读风格卡失败 {path}: {exc}")
        return ""
    match = _FRONTMATTER.match(raw)
    return (match.group(2) if match else raw).strip()


def read_meta(mem: MemoryConfig, group_id: int) -> dict:
    """读 frontmatter（更新时间、素材条数）。读不出来就给空 dict。"""
    path = style_path(mem, group_id)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    match = _FRONTMATTER.match(raw)
    if not match:
        return {}
    meta: dict = {}
    for line in match.group(1).splitlines():
        key, _, val = line.partition(":")
        key = key.strip()
        if key:
            meta[key] = val.strip()
    return meta


def _rotate(mem: MemoryConfig, group_id: int) -> None:
    """把当前卡推进历史（v1 最新），最多留 ``_KEEP_VERSIONS`` 份。"""
    try:
        current = style_path(mem, group_id).read_text(encoding="utf-8")
    except OSError:
        return
    for index in range(_KEEP_VERSIONS - 1, 0, -1):
        src = _version_path(mem, group_id, index)
        if src.exists():
            try:
                atomic_write(_version_path(mem, group_id, index + 1), src.read_text(encoding="utf-8"))
            except OSError:
                pass
    try:
        atomic_write(_version_path(mem, group_id, 1), current)
    except OSError as exc:
        logger.debug(f"[style] 备份旧卡失败（不影响新卡）: {exc}")


def write_style(
    mem: MemoryConfig, group_id: int, body: str, *, source_msgs: int = 0, rotate: bool = True
) -> tuple[bool, str]:
    """写风格卡。``body`` 是注入内容本身，超预算会截断。

    网页上手工编辑也走这里（``rotate=False``，人手改的东西不必进历史版本）。
    """
    text = (body or "").strip()
    if not text:
        return False, "内容为空。"
    limit = max(200, int(mem.style_chars))
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    if rotate and style_path(mem, group_id).exists():
        _rotate(mem, group_id)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    head = (
        "---\n"
        f"updated_at: {now}\n"
        f"source_msgs: {int(source_msgs)}\n"
        f"chars: {len(text)}\n"
        "---\n"
    )
    try:
        atomic_write(style_path(mem, group_id), head + text + "\n")
    except OSError as exc:
        logger.warning(f"[style] 写风格卡失败 群{group_id}: {exc}")
        return False, f"写文件失败：{exc}"
    return True, f"风格卡已更新（{len(text)} 字）"


def render_for_prompt(mem: MemoryConfig, group_id: int) -> str:
    """拼出注入系统提示词的「本群说话习惯」块；没卡/没开就返回空串。

    预算用 ``memory.style_chars``，**独立于 ``inject_chars``**：风格卡不该挤掉成员
    档案，成员档案也不该挤掉它。超预算按字数硬截（卡本身就该短，截到一半也比不带强）。
    """
    if not (mem.enabled and mem.style_enabled):
        return ""
    body = read_style(mem, group_id)
    if not body:
        return ""
    room = max(200, int(mem.style_chars))
    if len(body) > room:
        body = body[:room].rstrip() + "…"
    return f"\n【本群说话习惯】\n{_HEAD_HINT}\n{body}\n"


# ---------------------------------------------------------------- 采样


def _clean_line(text: object) -> str:
    return " ".join(str(text or "").split()).strip()


def eligible(text: object) -> bool:
    """这条消息适不适合当语体素材。纯函数，测试直接用。"""
    line = _clean_line(text)
    if not (MIN_LEN <= len(line) <= MAX_LEN):
        return False
    if _NOISE.search(line):
        return False
    return bool(_WORDY.search(line))


def pick_samples(
    rows: Iterable[dict],
    *,
    per_person: int = 6,
    max_items: int = 240,
    max_chars: int = 16000,
) -> list[dict]:
    """从 ``chat_log`` 行里挑素材，返回**按时间正序**的子集。

    三层过滤：单条合格性（:func:`eligible`）→ 每人配额 → 全群总量/字数上限。

    每人配额不是可有可无的：群里总有一两个人贡献一半消息，不设配额的话素材会被
    他们刷成「这个群说话像那一个人」，学出来的风格卡就是个人口癖合集。
    """
    # 用 uid 分桶，桶内按时间正序
    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in rows:
        if not eligible(row.get("content")):
            continue
        uid = str(row.get("user_id") or "")
        if uid not in buckets:
            buckets[uid] = []
            order.append(uid)
        buckets[uid].append(row)

    picked: list[dict] = []
    for uid in order:
        items = buckets[uid]
        quota = max(1, int(per_person))
        if len(items) <= quota:
            chosen = items
        else:
            # 等间隔抽样，保住首尾：只取「最近的 N 条」会让素材挤在同一场对话里，
            # 学出来的是「那一晚的语境」，不是这个群平时的样子。
            step = len(items) / float(quota)
            indexes = sorted({min(len(items) - 1, int(i * step)) for i in range(quota)})
            chosen = [items[i] for i in indexes]
        picked.extend(chosen)

    picked.sort(key=lambda r: int(r.get("id") or 0))
    # 总量与字数：都从**最旧**开始丢（近期的语体更接近现在）
    while len(picked) > max(1, int(max_items)):
        picked.pop(0)
    total = 0
    kept: list[dict] = []
    for row in reversed(picked):
        line = _clean_line(row.get("content"))
        if total + len(line) > max(1, int(max_chars)):
            break
        kept.append(row)
        total += len(line) + 3
    kept.reverse()
    return kept


def build_material(rows: Iterable[dict], *, max_chars: int = 16000) -> str:
    """把选中的原话拼成素材。**只给原话，不带昵称/QQ 号**。"""
    lines: list[str] = []
    total = 0
    for row in rows:
        line = _clean_line(row.get("content"))
        if not line:
            continue
        if total + len(line) > max_chars:
            break
        lines.append(f"- {line}")
        total += len(line) + 3
    return "\n".join(lines)


# ---------------------------------------------------------------- 解析与渲染


def _loads(text: str) -> dict:
    """从回复里抠 JSON。和 ``digest.parse_reply`` 同一套逻辑，**故意不互相依赖**：
    风格任务不该为了一个 10 行的函数拖上整理任务。
    """
    raw = (text or "").strip()
    if not raw:
        return {}
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001 — 模型输出不该让任务崩掉
        return {}
    return data if isinstance(data, dict) else {}


def _one_line(value: object, limit: int = 120) -> str:
    text = _clean_line(value)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _items(value: object, *, limit: int, item_len: int, strip_prefix: bool = False) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _one_line(item, item_len + 20)
        if strip_prefix:
            text = _SPEAKER_PREFIX.sub("", text).strip()
        if not text or len(text) > item_len:
            continue
        if text in out:
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out


def compose_body(data: dict) -> str:
    """把模型的结构化结果渲染成风格卡正文（也就是注入内容）。"""
    lines: list[str] = []
    length = _one_line(data.get("length"), 80)
    punct = _one_line(data.get("punct"), 80)
    if length:
        lines.append(f"- 句长：{length}")
    if punct:
        lines.append(f"- 标点：{punct}")
    words = _items(data.get("words"), limit=8, item_len=12)
    if words:
        lines.append("- 高频词：" + "、".join(words))
    avoid = _items(data.get("avoid"), limit=6, item_len=20)
    if avoid:
        lines.append("- 少用：" + "、".join(avoid))
    samples = _items(data.get("samples"), limit=10, item_len=MAX_LEN, strip_prefix=True)
    if samples:
        lines.append("")
        lines.append("群友原话（学语感用，别复述内容）：")
        lines.extend(f"{i}. {s}" for i, s in enumerate(samples, 1))
    return "\n".join(lines)


def acceptable(data: dict) -> tuple[bool, str]:
    """这张卡够不够格覆盖旧卡。

    门槛故意压在「至少 3 条逐字原话」：没有样本的风格卡基本等于一段空话，而空话会
    顶掉一张好卡——宁可不更新。
    """
    if not data:
        return False, "不是合法 JSON"
    samples = _items(data.get("samples"), limit=10, item_len=MAX_LEN, strip_prefix=True)
    if len(samples) < 3:
        return False, f"原话样本不足（{len(samples)} 条）"
    if not (_one_line(data.get("length")) or _one_line(data.get("punct"))):
        return False, "句长和标点都没写"
    return True, ""


# ---------------------------------------------------------------- 蒸馏任务


def _since(mem: MemoryConfig) -> float:
    days = float(mem.style_days)
    return time.time() - days * 86400 if days > 0 else 0.0


async def distill_group(
    group_id: int, *, mem: Optional[MemoryConfig] = None, force: bool = False
) -> dict:
    """学一次这个群的说话方式。返回统计字典，``status`` 取 ``ok``/``skipped``/``error``。

    ``force`` 目前只影响日志措辞（网页上手动触发）。采样用的是「最近 N 天」的滚动
    窗口而不是「未整理过的消息」——风格是**慢变量**，重复看同一批消息不浪费，
    反而是好事：卡会随着新消息加入而平滑变化，而不是被一批新消息整个推翻。
    """
    cfg_mem = mem if mem is not None else service.cfg(group_id).memory
    llm_cfg: LLMConfig = service.cfg(group_id).llm
    if not (cfg_mem.enabled and cfg_mem.style_enabled):
        return {"status": "skipped", "why": "风格学习没开"}
    if not llm_cfg.enabled or not (llm_cfg.base_url and llm_cfg.api_key):
        return {"status": "skipped", "why": "大模型没配置"}

    try:
        rows = await service.store.recent_chat(
            group_id, limit=max(200, int(cfg_mem.style_scan)), max_age=_since(cfg_mem)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[style] 群{group_id} 取聊天记录失败: {exc}")
        return {"status": "error", "why": f"读聊天记录失败：{exc}"}
    if not rows:
        return {"status": "skipped", "why": "这段时间没有群消息"}

    picked = pick_samples(
        rows,
        per_person=int(cfg_mem.style_per_person),
        max_items=_MAX_ITEMS,
        max_chars=_MAX_MATERIAL_CHARS,
    )
    if len(picked) < _MIN_MATERIAL:
        return {"status": "skipped", "why": f"可用素材太少（{len(picked)} 条）"}

    material = build_material(picked, max_chars=_MAX_MATERIAL_CHARS)
    prompt = _PROMPT.format(
        gid=group_id, days=int(float(cfg_mem.style_days) or 0), material=material
    )
    reply, err = await llm_bot.complete(
        prompt,
        llm_cfg,
        system=STYLE_SYSTEM,
        model=cfg_mem.style_model,
        max_tokens=int(cfg_mem.style_max_tokens),
        temperature=0.3,
    )
    if not reply:
        logger.warning(f"[style] 群{group_id} 学习失败（模型没回）: {err}")
        return {"status": "error", "why": err or "模型没有返回内容"}

    data = _loads(reply)
    ok, why = acceptable(data)
    if not ok:
        logger.warning(f"[style] 群{group_id} 结果不能用（{why}），保留旧卡：{reply[:160]}")
        return {"status": "error", "why": why}

    body = compose_body(data)
    written, msg = write_style(cfg_mem, group_id, body, source_msgs=len(picked))
    if not written:
        return {"status": "error", "why": msg}
    logger.info(
        f"[style] 群{group_id} 风格卡已更新：看 {len(picked)} 条消息 → {len(body)} 字"
    )
    return {
        "status": "ok",
        "rows": len(picked),
        "chars": len(body),
        "samples": len(_items(data.get("samples"), limit=10, item_len=MAX_LEN)),
    }


async def distill_all(*, force: bool = False) -> dict:
    """所有有记录的群各学一次。串行：同时打多个请求会撞中转限流。"""
    out: dict[int, dict] = {}
    try:
        groups = await service.store.chat_groups()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[style] 取群列表失败，本轮跳过: {exc}")
        return out
    for gid in groups:
        try:
            out[gid] = await distill_group(gid, force=force)
        except Exception as exc:  # noqa: BLE001 — 一个群炸了不能拖累其它群
            logger.warning(f"[style] 群{gid} 学习异常: {exc}")
            out[gid] = {"status": "error", "why": str(exc)}
    return out


def stats(mem: MemoryConfig, group_id: int) -> dict:
    """网页端状态显示用。"""
    body = read_style(mem, group_id)
    meta = read_meta(mem, group_id)
    return {
        "chars": len(body),
        "updated_at": meta.get("updated_at", ""),
        "source_msgs": int(meta.get("source_msgs") or 0),
        "budget": int(mem.style_chars),
        "enabled": bool(mem.enabled and mem.style_enabled),
    }
