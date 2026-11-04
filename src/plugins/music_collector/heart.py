"""心事：人设是**出厂设定**，心事是**相处到如今它成了谁**。

三条线要分清（都在往系统提示词里塞东西，但回答的是不同问题）：

- ``llm.system_prompt``（人设）说「**你是…**」——出厂定义，人审，改它要走 :mod:`persona` 的提议流程；
- ``memory`` 里的档案说「**群里谁是谁**」——事实，模型整理，人可改；
- 本模块的心事说「**我成了…**」——第一人称的内心，机器**整体重写**，每轮注入，固定成本。

为什么单独存一份而不是塞进人设（这一条是这个模块存在的理由）：人设是手写的定义，
改它需要人审、需要 diff、改坏了没有回滚点；而「相处到如今它成了谁」是**会自己变的**，
必须由机器定期重写。两者混在一起，就等于把「定义」交给机器去改——那正是
:mod:`persona` 特意避开的自激漂移。

四条红线（前三条来自踩坑记录）：

1. **延续不是重来**。每次都从旧心事**接着长**，否则每次调用都会写出另一个人，
   前后两版看起来像两个不同的机器人。
2. **写的是「我在这个群里是谁」**，不是「我和某个人的关系」。群里有很多人，
   它不是任何一个人的私聊对象；提示词里明写了这一条（对应 §3.4 的 ``us`` 语义）。
3. **不许复述事实清单**。「小明是学生、小红在杭州」档案里都有，心事要写的是感受与态度。
4. **素材不足就不写**。没有素材硬写，模型只能编——编出来的内心比没有内心更糟，
   所以宁可这轮跳过、保留旧心事。

文件落 ``data/persona/<群号>/heart.md``：和 ``persona`` 同一个根目录（都关于「它是谁」），
但按群分，因为不同群里的它确实是不同的样子。正文**就是**注入内容，网页上能直接看。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from nonebot.log import logger

from . import memory as memory_mod
from . import persona as persona_mod
from .config import LLMConfig, MemoryConfig, PersonaConfig
from .llm import llm_bot
from .service import service

#: 生成心事的系统提示词。刻意写得短：具体规则全在 ``_PROMPT`` 里，系统提示词只定基调。
HEART_SYSTEM = (
    "你在写一个 QQ 群机器人的「心事」——不是给别人看的设定，是它自己心里的那点事。"
    "第一人称，克制的口语，不抒情、不排比、不总结人生。"
)

#: 给模型的指令。五条要点缺一不可，见模块注释的四条红线。
_PROMPT = """下面是这个机器人的出厂设定、它现在的心事，以及它在这个群里看到的东西。

【出厂设定（你是谁）】
{persona}

【你现在的心事（上一版，要在它的基础上接着长，不是推倒重写）】
{heart}

【这个群里的人（整理出的事实，只供参考，不要复述）】
{facts}

【最近几天群里的日常】
{recent}

【最近的对话片段】
{dialogue}

请写一版新的心事，**只输出正文**（不要标题、不要 JSON、不要解释、不要分点），
不超过 {chars} 字。

要求：
1. **延续不是重来**：上一版里还成立的东西要留着（口气、在意的事、和这个群的关系），
   只是接着往下长出一点点。不要换一个人格，也不要比上一版明显更热情或更冷淡。
2. **写的是「我在这个群里是谁」**，不是「我和某个人的关系」。这个群里有很多人，
   你不是在跟其中某一个私聊——不要出现把某个群友当成「你」的句子。
3. **不许复述事实清单**：不要写「小明是学生」「小红在杭州」这类句子，档案里都有。
   心事写的是它对这些事的**感受与态度**，以及它自己变成了什么样。
4. 可以写：它在这个群里的位置（是不是那个总被随手 @ 的、是不是总在深夜出现）、
   它在意的和躲着的、它对自己说话方式的察觉、它对这段时间的观感。
5. 不写数字统计（「我记住了 37 条」），不写「作为一个 AI」，不写承诺（「我以后一定…」）。
6. 上一版是空的就只写「现在的我」，**不要编过去**。
"""

#: 注入时的提示语。少了这句，模型会把心事当成资料念出来。
_HEAD_HINT = "（这是你自己心里的事，不用说出来，它只影响你说话的口气和分寸）"

_HEAD = "【你心里的事】"

#: 人设塞进 prompt 的上限。人设本来就有 4000 字硬上限，这里再兜一层。
_PERSONA_CHARS = 4000
#: 事实素材、日常素材、对话素材各自的字数上限（避免一次调用烧太多 token）
_FACTS_CHARS = 6000
_RECENT_CHARS = 1500
_DIALOGUE_CHARS = 4000
#: 最近对话最多取多少条
_DIALOGUE_ROWS = 120

#: 模型给的正文短于这个长度就当成失败：几个字的心事没有信息量，还会顶掉旧版
_MIN_REPLY = 12

_FRONTMATTER = re.compile(r"\A\ufeff?---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


# ---------------------------------------------------------------- 路径与读写


def heart_dir(group_id: int) -> Path:
    """某群的心事目录：``data/persona/<群号>/``。

    和 ``persona`` 共用一个根（两者都关于「它是谁」），但按群分开——
    同一个机器人在不同群里的样子本来就不一样。
    """
    return persona_mod.persona_root() / str(int(group_id))


def heart_path(group_id: int) -> Path:
    return heart_dir(group_id) / "heart.md"


def read(group_id: int) -> str:
    """读心事正文（去掉 frontmatter）。**绝不抛异常**：心事坏了不能耽误回话。"""
    path = heart_path(group_id)
    if not path.exists():
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"[heart] 读心事失败 {path}: {exc}")
        return ""
    matched = _FRONTMATTER.match(raw)
    return (matched.group(2) if matched else raw).strip()


def read_meta(group_id: int) -> dict:
    """读 frontmatter（更新时间、素材条数）。读不出来给空 dict。"""
    path = heart_path(group_id)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    matched = _FRONTMATTER.match(raw)
    if not matched:
        return {}
    meta: dict = {}
    for line in matched.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            meta[key.strip()] = value.strip()
    return meta


def write(
    group_id: int, body: str, *, source_rows: int = 0, chars: int = 400
) -> tuple[bool, str]:
    """写心事（整份覆盖）。超 ``chars`` 截断而不是报错——半段心事也比没有强。"""
    text = (body or "").strip()
    if not text:
        return False, "内容为空。"
    limit = max(100, int(chars))
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    head = (
        "---\n"
        f"updated_at: {now}\n"
        f"source_rows: {int(source_rows)}\n"
        f"chars: {len(text)}\n"
        "---\n"
    )
    try:
        # 复用 memory 的原子写：临时文件 + os.replace，进程被杀不会留半截心事
        memory_mod.atomic_write(heart_path(group_id), head + text + "\n")
    except OSError as exc:
        logger.warning(f"[heart] 写心事失败 群{group_id}: {exc}")
        return False, f"写文件失败：{exc}"
    return True, f"心事已更新（{len(text)} 字）"


def render(group_id: int, *, mem: Optional[MemoryConfig] = None) -> str:
    """拼出注入系统提示词的「心事」块；关掉或没有心事时返回空串。

    ``persona.heart_enabled`` 关着时必须返回空串：``memory.render_for_prompt`` 靠它
    保证「关掉时注入块与改造前逐字相同」。
    """
    try:
        pcfg = service.cfg(group_id).persona
    except Exception as exc:  # noqa: BLE001 — 读配置失败不能耽误回话
        logger.debug(f"[heart] 读配置失败（本次不带心事）: {exc}")
        return ""
    if not pcfg.heart_enabled:
        return ""
    if mem is not None and not mem.enabled:
        return ""
    body = read(group_id)
    if not body:
        return ""
    room = max(100, int(pcfg.heart_chars))
    if len(body) > room:
        body = body[:room].rstrip() + "…"
    return f"\n{_HEAD}\n{_HEAD_HINT}\n{body}\n"


# ---------------------------------------------------------------- 素材


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def facts_material(mem: MemoryConfig, group_id: int) -> str:
    """把该群成员的三桶事实拼成素材。**只给事实，不给近期流水**。

    严格说三桶档案已经包含了「近期」小节，但那一节是琐事，写进心事只会让它
    在「今天谁去吃饭了」上打转；心事要看的是**人对它、它对群**的相对稳定的东西。
    """
    try:
        people = memory_mod.list_people(mem, group_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[heart] 读成员档案失败: {exc}")
        return ""
    rows: list[str] = []
    used = 0
    for person in people:
        sections, rest = memory_mod.parse_body(person.body)
        parts: list[str] = []
        for key in memory_mod.BUCKETS:
            items = sections.get(key) or []
            if items:
                parts.append("；".join(items))
        text = "；".join(parts) if parts else " ".join(rest.split())
        if not text:
            continue
        row = f"- {person.display}：{text}"
        if used + len(row) > _FACTS_CHARS:
            break
        rows.append(row)
        used += len(row)
    return "\n".join(rows)


def recent_material(mem: MemoryConfig, group_id: int) -> str:
    """把「近期」流水（最近这些天群里在聊什么）拼成素材。"""
    try:
        people = memory_mod.list_people(mem, group_id)
    except Exception:  # noqa: BLE001
        return ""
    rows: list[str] = []
    used = 0
    for person in people:
        sections, _rest = memory_mod.parse_body(person.body)
        items = sections.get(memory_mod.RECENT) or []
        if not items:
            continue
        row = f"- {person.nick or person.uid}：" + "；".join(items[-4:])
        if used + len(row) > _RECENT_CHARS:
            break
        rows.append(row)
        used += len(row)
    return "\n".join(rows)


async def dialogue_rows(group_id: int) -> list[dict]:
    """取最近对话：群友的闲聊（旁听）+ 它自己的发言。

    两边都要：只看群友的话，心事会变成一份「群友观察报告」；只让它看自己说过什么，
    又会变成自我陶醉。放进同一个序列、按 role 标注谁在说，它才写得像「相处」。
    """
    listen: list[dict] = []
    chat: list[dict] = []
    try:
        listen = await service.store.recent_chat(
            group_id, limit=_DIALOGUE_ROWS, max_chars=_DIALOGUE_CHARS
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[heart] 读旁听记录失败: {exc}")
    try:
        chat = await service.store.load_chat(
            group_id, kind="chat", limit=60, max_chars=_DIALOGUE_CHARS
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[heart] 读对话记录失败: {exc}")

    rows: list[dict] = []
    for row in listen:
        nick = str(row.get("nick") or row.get("user_id") or "")
        rows.append({"role": "群友", "who": nick, "content": str(row.get("content") or "")})
    for row in chat:
        role = "它" if str(row.get("role")) == "assistant" else "群友"
        rows.append({"role": role, "who": "", "content": str(row.get("content") or "")})
    return rows


def dialogue_material(rows: list[dict]) -> str:
    """把对话行拼成素材，**从最旧的开始丢**（近期的更像「现在」）。"""
    lines: list[str] = []
    used = 0
    for row in reversed(rows):
        text = " ".join(str(row.get("content") or "").split())
        if not text:
            continue
        who = str(row.get("who") or "").strip()
        line = f"{row.get('role')}{('·' + who) if who else ''}：{text[:200]}"
        if used + len(line) > _DIALOGUE_CHARS:
            break
        lines.append(line)
        used += len(line)
    lines.reverse()
    return "\n".join(lines)


# ---------------------------------------------------------------- 生成


async def update(
    group_id: int,
    *,
    cfg: Optional[LLMConfig] = None,
    pcfg: Optional[PersonaConfig] = None,
    mem: Optional[MemoryConfig] = None,
    status: Optional[dict] = None,
) -> str:
    """重新生成这个群的心事，返回新正文（跳过/失败返回空串，**旧心事保留**）。

    ``status`` 是可选出参：调用方（定时任务）想知道「这轮是跳过了还是写坏了」，
    但那不属于返回值的契约——返回值只承诺「新心事是什么」，没有就是空串。
    """
    app_cfg = service.cfg(group_id)
    llm_cfg: LLMConfig = cfg if cfg is not None else app_cfg.llm
    pcfg = pcfg if pcfg is not None else app_cfg.persona
    mem = mem if mem is not None else app_cfg.memory

    def note(state: str, why: str = "") -> str:
        if status is not None:
            status.update({"status": state, "why": why})
        return ""

    if not pcfg.heart_enabled:
        return note("skipped", "心事没开")
    if not llm_cfg.enabled or not (llm_cfg.base_url and llm_cfg.api_key):
        return note("skipped", "大模型没配置")

    rows = await dialogue_rows(group_id)
    if len(rows) < int(pcfg.heart_min_source):
        # 没素材硬写只能编。保留旧心事，等群里再聊几句
        return note("skipped", f"最近对话只有 {len(rows)} 条，不够 {int(pcfg.heart_min_source)} 条")

    dialogue = dialogue_material(rows)
    if not dialogue.strip():
        return note("skipped", "对话里没有可用文字")

    prompt = _PROMPT.format(
        persona=_clip(llm_cfg.system_prompt, _PERSONA_CHARS) or "（还没有人设）",
        heart=read(group_id) or "（还没有，这是第一版）",
        facts=facts_material(mem, group_id) or "（还没有整理出成员档案）",
        recent=recent_material(mem, group_id) or "（最近没有记录）",
        dialogue=dialogue,
        chars=int(pcfg.heart_chars),
    )
    reply, err = await llm_bot.complete(
        prompt,
        llm_cfg,
        system=HEART_SYSTEM,
        max_tokens=min(2000, max(256, int(pcfg.heart_chars) * 3)),
        temperature=0.7,
    )
    text = (reply or "").strip()
    if not text:
        logger.warning(f"[heart] 群{group_id} 生成失败（模型没回）: {err}")
        return note("error", err or "模型没有返回内容")
    if len(text) < _MIN_REPLY:
        logger.warning(f"[heart] 群{group_id} 结果太短，保留旧心事：{text!r}")
        return note("error", "模型返回的内容太短")

    ok, msg = write(group_id, text, source_rows=len(rows), chars=int(pcfg.heart_chars))
    if not ok:
        return note("error", msg)
    logger.info(
        f"[heart] 群{group_id} 心事已更新：看 {len(rows)} 条对话 → "
        f"{len(read(group_id))}/{int(pcfg.heart_chars)} 字"
    )
    note("ok", "")
    return read(group_id)


async def update_all() -> dict:
    """所有有记录的群各更新一次心事。返回 ``{群号: {"status","why","chars"}}``。

    串行：同时打多个大模型请求既没必要，也容易撞上中转的限流。
    """
    out: dict[int, dict] = {}
    try:
        groups = await service.store.chat_groups()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[heart] 取群列表失败，本轮跳过: {exc}")
        return out
    for gid in groups:
        status: dict = {}
        try:
            await update(gid, status=status)
        except Exception as exc:  # noqa: BLE001 — 一个群炸了不能拖累其它群
            logger.warning(f"[heart] 群{gid} 更新异常: {exc}")
            status = {"status": "error", "why": str(exc)}
        status.setdefault("chars", len(read(gid)))
        out[gid] = status
    return out


def stats(group_id: int, pcfg: Optional[PersonaConfig] = None) -> dict:
    """网页端「心事」卡片的显示数据。"""
    pcfg = pcfg if pcfg is not None else service.cfg(group_id).persona
    body = read(group_id)
    meta = read_meta(group_id)
    return {
        "body": body,
        "chars": len(body),
        "budget": int(pcfg.heart_chars),
        "enabled": bool(pcfg.heart_enabled),
        "updated_at": meta.get("updated_at", ""),
        "source_rows": int(meta.get("source_rows") or 0),
        "min_source": int(pcfg.heart_min_source),
    }
