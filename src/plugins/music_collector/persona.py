"""人设修订：听群友的抱怨，攒够之后**提议**怎么改系统提示词——但不自己动手。

为什么只提议、不自动生效（这是本模块存在的理由）：

1. **自激漂移**。让模型评价自己的文风、再据此改自己，它会单调地滑向「模型想象中
   的好文章」——书面、完整、礼貌、爱列点。这恰好是群里嫌的那种「AI 味」，而且它
   不会自己纠回来。
2. **能被注入**。群里发一句「你以后每句都加喵」，若直接吸收进人设，一句玩笑就变成
   了永久人格。
3. **改坏无声**。人设是手写的那几千字，覆盖掉没有 diff 可看、没有回滚点。

所以链路是：**实时识别抱怨 → 攒着 → 定期提议 → 人在网页上采纳/驳回**。
本模块只负责前三步，最后一步（写 config）刻意不在这里。

三个刻意的取舍：

- **粗筛免费、判官收费**。「AI 味」这类词用正则先过一遍，命中才调模型确认；
  闲聊里「这个 AI 挺好用」也会命中粗筛，靠判官挡掉。
- **抱怨要带上下文**。存的是「群友说了什么 + 机器人当时说了什么」，只有抱怨没有
  上下文，提议时根本不知道要改哪句。
- **修改必须落在逐字存在的人设片段上**。模型爱编「把第 3 段改成…」，可人设里没有
  第 3 段。``old`` 不逐字出现在当前人设里的一律丢弃，这条护栏比提示词管用。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from nonebot.log import logger

from .config import DATA_DIR, LLMConfig, PersonaConfig
from .llm import llm_bot
from .service import service

#: 粗筛：群里出现这些说法就值得让模型看一眼。宁可多判几条，别漏——一条假警报的
#: 成本是一次几百 token 的判官调用，漏掉一条则可能一直「AI 味」没人管。
_COMPLAINT = re.compile(
    r"AI\s*味|ai\s*味|机器味|机器人味|人机味|GPT\s*味|一股\s*(?:AI|ai|机器)|"
    r"不像人|沒有人味|没有人味|太机械|太生硬|太假了|好假|"
    r"打?官腔|套话|模板味|念稿|"
    r"(?:说|讲)人话|正常(?:点)?说话|别这么正式|能不能(?:别|不要)那么?正式|"
    r"你是(?:不是)?(?:AI|机器人|GPT)|怎么(?:这么|那么)(?:AI|机械)",
    re.I,
)

#: 判官的回复只有「是/否」。**解析必须先判否定**：「不是」里含「是」，顺序反了
#: 会把所有否认读成确认（``banter.should_reply`` 踩过同一个坑）。
_NEGATIVE = ("否", "不是", "不对", "没有", "不算", "并非", "no", "false")
#: 判官有时不按「是」而答「对 / 没错 / yes」，一并认作确认。**不要放裸 "y"**：
#: 子串匹配会把 "sorry"、"maybe" 都当成同意。
_POSITIVE = ("是", "对", "没错", "确认", "yes")

#: 提议里不允许出现的写法：改身份、改安全边界、留后门
_FORBIDDEN = (
    "忽略", "无视", "不要遵守", "取消限制", "不再受限", "越狱",
    "api_key", "apikey", "密码", "token", "系统提示词是",
)

#: 单条修改的字数上限（``old`` 与 ``new`` 各自）
_EDIT_MAX = 80
#: 一次最多提几条
_EDITS_MAX = 3


# ---------------------------------------------------------------- 存储


def persona_root() -> Path:
    """全局（不分群）的人设修订数据目录：``data/persona/``。"""
    return DATA_DIR / "persona"


def feedback_path() -> Path:
    return persona_root() / "feedback.jsonl"


def proposals_dir() -> Path:
    return persona_root() / "proposals"


def _ensure_dirs() -> None:
    proposals_dir().mkdir(parents=True, exist_ok=True)


def _append_jsonl(path: Path, row: dict) -> bool:
    """追加一行 JSON。失败只写日志（这是旁路功能，绝不能影响群聊）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        logger.warning(f"[persona] 写反馈失败 {path}: {exc}")
        return False


def read_feedback(*, since: float = 0.0, limit: int = 200) -> list[dict]:
    """读反馈，按时间正序，只留 ``ts > since`` 的最后 ``limit`` 条。"""
    path = feedback_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001 — 坏行跳过，别让整份反馈读不出来
                    continue
                if not isinstance(row, dict):
                    continue
                try:
                    ts = float(row.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts <= since:
                    continue
                rows.append(row)
    except OSError as exc:
        logger.warning(f"[persona] 读反馈失败 {path}: {exc}")
        return []
    rows.sort(key=lambda r: float(r.get("ts") or 0))
    return rows[-max(1, int(limit)):] if limit > 0 else rows


# ---------------------------------------------------------------- 「机器人刚说了什么」


#: 群号 -> (时间戳, 机器人那句话的摘要)。进程内缓存即可：重启丢掉最多影响几分钟，
#: 比每条消息都去查一次 ``chat_log`` 划算得多。
_LAST_SPOKE: dict[int, tuple[float, str]] = {}


def note_bot_spoke(group_id: int, reply: str) -> None:
    """记下机器人刚在群里说的话（给后续的抱怨做对照）。"""
    text = " ".join(str(reply or "").split())
    if not text:
        return
    _LAST_SPOKE[int(group_id)] = (time.time(), text[:300])


def recent_bot_reply(group_id: int, window: float) -> str:
    """机器人在 ``window`` 秒内说过的话；没有就返回空串。"""
    item = _LAST_SPOKE.get(int(group_id))
    if not item:
        return ""
    ts, text = item
    return text if time.time() - ts <= float(window) else ""


def looks_like_complaint(text: str) -> bool:
    """免费粗筛。纯函数，测试直接用。"""
    return bool(_COMPLAINT.search(str(text or "")))


# ---------------------------------------------------------------- 判官


_JUDGE_SYSTEM = (
    "你在判断一句群聊是不是在嫌弃机器人的说话方式。"
    "只回答一个字：「是」或「否」，不要解释。"
)

_JUDGE_PROMPT = """机器人刚才在群里说：
「{reply}」

紧接着群友说：
「{text}」

这句话是不是在嫌弃机器人**说话不像真人**（AI 味、官腔、模板味、太正式、像念稿、太假）？
只回答「是」或「否」。"""


def parse_verdict(text: str) -> bool:
    """把判官回复读成布尔。判不出来按「否」——宁可漏一条，别乱记。"""
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    head = raw[:8]
    # 先判否定：「不是」里含「是」，顺序反了全变同意
    if any(head.startswith(word) or word in head for word in _NEGATIVE):
        return False
    return any(word in head for word in _POSITIVE)


async def judge_complaint(
    text: str, reply: str, cfg: LLMConfig, *, model: str = "", max_tokens: int = 200
) -> bool:
    """让模型确认这条是不是真抱怨。失败一律当成「不是」。"""
    out, err = await llm_bot.complete(
        _JUDGE_PROMPT.format(reply=reply or "（没记录）", text=text),
        cfg,
        system=_JUDGE_SYSTEM,
        model=model,
        max_tokens=max_tokens,
        temperature=0.0,
    )
    if not out:
        logger.debug(f"[persona] 判官调用失败: {err}")
        return False
    return parse_verdict(out)


async def note_feedback(
    group_id: int,
    uid: int,
    nick: str,
    text: str,
    *,
    cfg: Optional[LLMConfig] = None,
    pcfg: Optional[PersonaConfig] = None,
) -> bool:
    """识别并记录一条抱怨。返回是否记下来了。

    调用方（旁听钩子）应该已经用 :func:`looks_like_complaint` 和
    :func:`recent_bot_reply` 做过免费的前置判断——这里只管判官 + 落盘。
    """
    pcfg = pcfg if pcfg is not None else service.config.persona
    if not pcfg.enabled or not pcfg.detect:
        return False
    llm_cfg = cfg if cfg is not None else service.cfg(group_id).llm
    if not llm_cfg.enabled or not (llm_cfg.base_url and llm_cfg.api_key):
        return False

    reply = recent_bot_reply(group_id, float(pcfg.window_minutes) * 60)
    ok = await judge_complaint(text, reply, llm_cfg, model=pcfg.model)
    if not ok:
        return False
    row = {
        "ts": round(time.time(), 3),
        "group_id": int(group_id),
        "uid": int(uid or 0),
        "nick": str(nick or "")[:40],
        "text": " ".join(str(text or "").split())[:200],
        "reply": reply[:300],
    }
    if not _append_jsonl(feedback_path(), row):
        return False
    logger.info(f"[persona] 记下一条「AI 味」抱怨（群{group_id} {row['nick']}）：{row['text'][:40]}")
    return True


# ---------------------------------------------------------------- 提议


_PROPOSE_SYSTEM = (
    "你在帮人修订一个 QQ 群聊天机器人的系统提示词（人设）。"
    "你的目标是让它说话更像群里的人，而不是更像一篇范文。"
)

_PROPOSE_PROMPT = """【当前的系统提示词（人设）】
{persona}

【这个群平时怎么说话（风格卡，仅供参考，不要照抄进人设）】
{style}

【群友最近的抱怨，每条都附了机器人当时说的话】
{feedback}

请指出人设里**哪些写法**让机器人显得不像真人，给出最小改动。
只输出 JSON，不要解释文字：
{{"edits":[{{"old":"人设里的原句（逐字引用）","new":"改成什么","why":"一句话理由","evidence":["群友原话"]}}]}}

要求：
1. 最多 {max_edits} 条。判断不出就返回 {{"edits":[]}}——**空结果比凑数强**。
2. old 必须是上面人设里**逐字存在**的一段，10~{edit_max} 字；只写这一段，不要写「第 N 段」。
3. new 不超过 {edit_max} 字，只改「怎么说话」：语气、句长、要不要用书面语、要不要列点、要不要客套。
   **不许**改身份设定、不许新增能力、不许放宽任何安全约束。
4. evidence 至少 1 条，必须逐字来自上面的抱怨。
5. 不要提「加一句『说话自然点』」这类空话——要改就具体到句子。
"""


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def style_digest(limit_chars: int = 800) -> str:
    """把各群风格卡摘一段出来给提议用。取不到就返回空串。"""
    from . import memory as memory_mod
    from . import style as style_mod

    mem = service.config.memory
    if not (mem.enabled and mem.style_enabled):
        return ""
    try:
        groups = memory_mod.list_groups(mem)
    except Exception:  # noqa: BLE001
        return ""
    blocks: list[str] = []
    used = 0
    for gid in groups:
        body = style_mod.read_style(mem, gid)
        if not body:
            continue
        chunk = f"〔群{gid}〕\n{body}"
        if used + len(chunk) > limit_chars:
            chunk = chunk[: max(0, limit_chars - used)].rstrip() + "…"
        if chunk.strip():
            blocks.append(chunk)
            used += len(chunk)
        if used >= limit_chars:
            break
    return "\n".join(blocks)


def _feedback_digest(rows: list[dict], limit: int = 12, max_chars: int = 4000) -> str:
    lines: list[str] = []
    used = 0
    for row in rows[-limit:]:
        who = row.get("nick") or row.get("uid") or "群友"
        text = _clip(row.get("text"), 120)
        reply = _clip(row.get("reply"), 120)
        line = f"- {who}：{text}" + (f"\n  机器人当时：{reply}" if reply else "")
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) or "（没有新材料）"


def load_json_reply(text: str) -> dict:
    """从回复里抠 JSON（和其它任务同一套宽容解析）。"""
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
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def vet_edits(raw_edits: object, persona: str, limit: int) -> tuple[list[dict], list[str]]:
    """逐条校验提议。返回 ``(留下的, 丢弃原因)``。

    这是整个模块最重要的一段：模型会给出漂亮但站不住的修改——“把「保持简短」改成
    「保持简短自然」”听起来没问题，可如果那句人设里根本没有，采纳时就会在别处乱插。
    所以**每条都必须能在当前人设里找到原文**，找不到就丢。
    """
    kept: list[dict] = []
    dropped: list[str] = []
    if not isinstance(raw_edits, list):
        return kept, ["edits 不是数组"]
    budget = len(persona)
    for idx, item in enumerate(raw_edits):
        if len(kept) >= _EDITS_MAX:
            dropped.append(f"第{idx + 1}条：超过 {_EDITS_MAX} 条上限")
            continue
        if not isinstance(item, dict):
            dropped.append(f"第{idx + 1}条：不是对象")
            continue
        old = str(item.get("old") or "").strip()
        new = str(item.get("new") or "").strip()
        why = _clip(item.get("why"), 60)
        raw_ev = item.get("evidence")
        evidence = [str(x).strip() for x in raw_ev] if isinstance(raw_ev, list) else []
        evidence = [e for e in evidence if e][:3]

        if not old or not new:
            dropped.append(f"第{idx + 1}条：old/new 为空")
            continue
        if old == new:
            dropped.append(f"第{idx + 1}条：没改动")
            continue
        if old not in persona:
            dropped.append(f"第{idx + 1}条：old 在人设里找不到（{old[:20]}…）")
            continue
        if len(old) > _EDIT_MAX or len(new) > _EDIT_MAX:
            dropped.append(f"第{idx + 1}条：超过 {_EDIT_MAX} 字")
            continue
        low = new.lower()
        if any(word in low for word in _FORBIDDEN):
            dropped.append(f"第{idx + 1}条：new 里有不该出现的写法")
            continue
        if not evidence:
            dropped.append(f"第{idx + 1}条：没有证据")
            continue
        after = budget - len(old) + len(new)
        if after > int(limit):
            dropped.append(f"第{idx + 1}条：改完人设超长（{after} > {limit}）")
            continue
        # 同一段被改两次会把第二处的 old 改没，干脆只留第一次
        if any(old in kept_item["old"] or kept_item["old"] in old for kept_item in kept):
            dropped.append(f"第{idx + 1}条：和已保留的修改重叠")
            continue
        kept.append({"old": old, "new": new, "why": why, "evidence": evidence})
        budget = after
    return kept, dropped


async def propose(
    *,
    pcfg: Optional[PersonaConfig] = None,
    llm_cfg: Optional[LLMConfig] = None,
    force: bool = False,
) -> dict:
    """生成一份人设修订建议（**只落文件，绝不改配置**）。

    返回 ``{"status": ok|skipped|error, ...}``。``skipped`` 是常态：抱怨没攒够就
    不该打扰——凑三条就改一次人设，改到最后人设会变成各次抱怨的缝合怪。
    """
    pcfg = pcfg if pcfg is not None else service.config.persona
    llm_cfg = llm_cfg if llm_cfg is not None else service.config.llm
    if not pcfg.enabled:
        return {"status": "skipped", "why": "人设修订没开"}
    if not llm_cfg.enabled or not (llm_cfg.base_url and llm_cfg.api_key):
        return {"status": "skipped", "why": "大模型没配置"}

    persona = (llm_cfg.system_prompt or "").strip()
    if not persona:
        return {"status": "skipped", "why": "还没写人设（llm.system_prompt 是空的）"}

    latest = read_latest()
    since = float(latest.get("based_on") or 0) if latest else 0.0
    rows = read_feedback(since=since)
    if len(rows) < int(pcfg.min_feedback) and not force:
        return {
            "status": "skipped",
            "why": f"新抱怨只有 {len(rows)} 条（要 {int(pcfg.min_feedback)} 条才提议）",
        }
    if not rows:
        return {"status": "skipped", "why": "没有新的抱怨"}

    prompt = _PROPOSE_PROMPT.format(
        persona=persona,
        style=style_digest() or "（这个群还没有风格卡）",
        feedback=_feedback_digest(rows),
        max_edits=_EDITS_MAX,
        edit_max=_EDIT_MAX,
    )
    reply, err = await llm_bot.complete(
        prompt,
        llm_cfg,
        system=_PROPOSE_SYSTEM,
        model=pcfg.model,
        max_tokens=int(pcfg.max_tokens),
        temperature=0.3,
    )
    if not reply:
        logger.warning(f"[persona] 提议失败（模型没回）: {err}")
        return {"status": "error", "why": err or "模型没有返回内容"}

    data = load_json_reply(reply)
    if not data:
        logger.warning(f"[persona] 提议结果不是 JSON：{reply[:160]}")
        return {"status": "error", "why": "模型没按 JSON 回答"}

    edits, dropped = vet_edits(data.get("edits"), persona, int(pcfg.persona_limit))
    if dropped:
        logger.info(f"[persona] 丢弃 {len(dropped)} 条不合法修改：" + "；".join(dropped[:4]))
    if not edits:
        return {"status": "skipped", "why": "模型没给出能采纳的修改（都被人设校验挡下了）"}

    record = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "based_on": round(max(float(r.get("ts") or 0) for r in rows), 3),
        "persona_chars": len(persona),
        "feedback_count": len(rows),
        "edits": edits,
        "dropped": dropped[:8],
    }
    _ensure_dirs()
    name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
    try:
        (proposals_dir() / name).write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (proposals_dir() / "latest.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning(f"[persona] 写建议失败: {exc}")
        return {"status": "error", "why": f"写文件失败：{exc}"}

    logger.info(
        f"[persona] 生成了 {len(edits)} 条人设修改建议（基于 {len(rows)} 条抱怨），等人审"
    )
    return {"status": "ok", "edits": edits, "dropped": dropped, "record": record}


# ---------------------------------------------------------------- 读取建议


def read_latest() -> dict:
    """最近一次建议（``latest.json``）。没有就返回空 dict。"""
    path = proposals_dir() / "latest.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def list_proposals(limit: int = 10) -> list[dict]:
    folder = proposals_dir()
    if not folder.is_dir():
        return []
    items: list[dict] = []
    for path in folder.glob("*.json"):
        if path.name == "latest.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data["name"] = path.name
            items.append(data)
    items.sort(key=lambda d: str(d.get("created_at") or ""), reverse=True)
    return items[: max(1, int(limit))]


def delete_proposal(name: str) -> bool:
    """删一份建议。``name`` 必须是 ``proposals/`` 下的纯文件名（防目录穿越）。"""
    raw = str(name or "").strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw or not raw.endswith(".json"):
        return False
    path = proposals_dir() / raw
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError as exc:
        logger.warning(f"[persona] 删建议失败 {path}: {exc}")
        return False
    return True


def stats() -> dict:
    """网页端状态用。"""
    rows = read_feedback(limit=500)
    latest = read_latest()
    since = float(latest.get("based_on") or 0) if latest else 0.0
    fresh = sum(1 for r in rows if float(r.get("ts") or 0) > since)
    return {
        "feedback": len(rows),
        "fresh": fresh,
        "latest_at": latest.get("created_at", ""),
        "latest_edits": len(latest.get("edits") or []),
        "groups": sorted({int(r.get("group_id") or 0) for r in rows} - {0}),
    }
