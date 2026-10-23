"""把群聊喂给模型，让它自己整理出「每个人是什么样的」，写进档案。

这是「主动记忆」的那一半：``remember`` 工具要等模型在对话中想起来才用，
而这里每隔几小时扫一遍**旁听到的群聊**，让模型主动归纳。

三条设计红线：

1. **只写本次素材里出现过的人**。模型偶尔会编一个 QQ 号出来，写进去就是
   一条查无此人的幽灵档案——所以 uid 必须出现在这次喂进去的消息里。
2. **解析失败不消耗素材**。整理结果不是合法 JSON 时不标记 ``digested``，
   下一轮还会重试；否则一次模型抽风就把那段聊天永久丢掉了。
3. **失败绝不打扰群里**。这是后台任务，出问题只写日志。
"""

from __future__ import annotations

import json
import re
import time
from typing import Optional

from nonebot.log import logger

from . import memory as memory_mod
from .config import LLMConfig, MemoryConfig
from .llm import llm_bot
from .service import service

#: 整理任务的系统提示词。**刻意不给人设**：这里要的是冷静的观察员，
#: 不是群里那个会接梗的机器人。
DIGEST_SYSTEM = (
    "你在整理一个 QQ 群的成员档案。你的任务是从聊天记录里读出每个人稳定的特征，"
    "结构化输出，供以后对话时参考。只依据给定材料，不推测、不补全、不评价。"
)

#: 给模型的指令。措辞要具体到「什么算稳定特征」，否则它会记一堆「今天吃了面」
_PROMPT = """下面是 QQ 群 {gid} 最近的一段聊天记录，以及我手上已有的档案摘要。

【已有档案】
{known}

【聊天记录】
{material}

请归纳出每个人的**稳定特征**，只输出 JSON，不要任何解释文字：
{{"people":[{{"uid":"QQ号","nick":"昵称","facts":["一句事实"]}}],"group":["群层面的共识或黑话"]}}

要求：
1. 只写材料里能直接看出来的，不要推测、不要编造；拿不准就不写。
2. 只写稳定特征：身份、职业或学业、所在地、兴趣、口味偏好、说话习惯、
   常用梗、和群里其他人的关系。不写「今天吃了什么」这类一次性的事。
3. 不写密码、手机号、住址、身份证号、真实姓名等敏感信息。
4. 每条事实一句话、20 字以内、陈述句、省略主语（写「喜欢周杰伦」，不写「小明喜欢周杰伦」）。
5. 已有档案里的事实不要重复；只写新的，或对旧事实的更正。
6. 一个人最多 5 条。没有任何可写的内容就返回 {{"people":[],"group":[]}}。
"""

#: 已知档案塞进 prompt 的上限（按人裁剪，避免整理请求本身撑爆上下文）
KNOWN_CHARS = 12000


def parse_reply(text: str) -> dict:
    """从模型回复里抠出 JSON。抠不出来返回空 dict（调用方据此判定「失败」）。

    兼容三种常见形态：纯 JSON、`` ```json `` 包裹、前后带解释文字。
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


def known_digest(people: list, budget: int = KNOWN_CHARS) -> str:
    """把已有档案压成「谁：摘要」，按最近活跃倒序装到预算用完为止。"""
    rows: list[str] = []
    used = 0
    for person in people:
        row = f"- {person.display}：{person.summary(150)}"
        if used + len(row) > budget:
            break
        rows.append(row)
        used += len(row)
    if not rows:
        return "（还没有任何档案）"
    if len(rows) < len(people):
        rows.append(f"（另有 {len(people) - len(rows)} 人未列出）")
    return "\n".join(rows)


async def digest_group(group_id: int, *, mem: Optional[MemoryConfig] = None, force: bool = False) -> dict:
    """整理一个群。返回统计字典（含 ``status``，供调度日志与网页端展示）。

    ``status`` 取值：``ok`` / ``skipped``（没新素材或没开）/ ``error``（模型或解析失败）。
    """
    app_cfg = service.cfg(group_id)
    cfg_mem = mem if mem is not None else app_cfg.memory
    llm_cfg: LLMConfig = app_cfg.llm
    if not cfg_mem.enabled:
        return {"status": "skipped", "why": "记忆功能没开"}
    if not llm_cfg.enabled or not (llm_cfg.base_url and llm_cfg.api_key):
        return {"status": "skipped", "why": "大模型没配置"}

    since = 0.0
    if cfg_mem.keep_days > 0:
        since = time.time() - float(cfg_mem.keep_days) * 86400
    rows = await service.store.pending_digest(
        group_id, limit=max(20, int(cfg_mem.digest_msgs)), since=since
    )
    if not rows:
        return {"status": "skipped", "why": "没有新消息"}

    material = memory_mod.digest_source(rows, max_chars=max(1000, int(cfg_mem.digest_chars)))
    if not material.strip():
        return {"status": "skipped", "why": "消息里没有可用文字"}

    people = memory_mod.list_people(cfg_mem, group_id)
    prompt = _PROMPT.format(
        gid=group_id, known=known_digest(people), material=material
    )
    reply, err = await llm_bot.complete(
        prompt,
        llm_cfg,
        system=DIGEST_SYSTEM,
        model=cfg_mem.digest_model,
        max_tokens=int(cfg_mem.digest_max_tokens),
        temperature=0.2,
    )
    if not reply:
        logger.warning(f"[memory] 群{group_id} 整理失败（模型没回）: {err}")
        return {"status": "error", "why": err or "模型没有返回内容"}

    data = parse_reply(reply)
    if not data:
        logger.warning(
            f"[memory] 群{group_id} 整理结果不是 JSON，本轮不消耗素材：{reply[:160]}"
        )
        return {"status": "error", "why": "模型没按 JSON 回答"}

    # 只认「这次材料里真的出现过的人」，防止模型编 QQ 号写出幽灵档案
    allowed = {str(r.get("user_id") or "") for r in rows}
    allowed.discard("")
    wrote_people = 0
    wrote_facts = 0
    for item in data.get("people") or []:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("uid") or "").strip()
        if uid not in allowed:
            logger.info(f"[memory] 群{group_id} 跳过材料里没出现过的 uid={uid!r}")
            continue
        nick = str(item.get("nick") or "").strip()[:40]
        facts = item.get("facts") or []
        if isinstance(facts, str):
            facts = [facts]
        touched = False
        for fact in facts[:8]:
            text = str(fact or "").strip()
            if not text:
                continue
            ok, _msg = memory_mod.append_note(cfg_mem, group_id, uid, text, nick)
            if ok:
                wrote_facts += 1
                touched = True
        if touched:
            wrote_people += 1

    group_lines = data.get("group") or []
    if isinstance(group_lines, str):
        group_lines = [group_lines]
    group_added, _ = memory_mod.append_group_note(
        cfg_mem, group_id, [str(x) for x in group_lines][:10]
    )

    # 走到这里才标记已整理：模型失败 / JSON 坏了都要留着重试
    last_id = max(int(r.get("id") or 0) for r in rows)
    marked = await service.store.mark_digested(group_id, last_id)
    logger.info(
        f"[memory] 群{group_id} 整理完成：看 {len(rows)} 条消息 → "
        f"{wrote_people} 人 / {wrote_facts} 条事实 / 群概况 {group_added} 条"
        f"（标记 {marked} 条为已整理）"
    )
    return {
        "status": "ok",
        "rows": len(rows),
        "people": wrote_people,
        "facts": wrote_facts,
        "group_facts": group_added,
        "marked": marked,
    }


async def digest_all(*, force: bool = False) -> dict:
    """遍历所有有记录的群各整理一次。返回 ``{群号: 结果}``。

    串行执行：同时打多个大模型请求既没必要，也容易撞上中转的限流。
    """
    out: dict[int, dict] = {}
    try:
        groups = await service.store.chat_groups()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[memory] 取群列表失败，本次整理跳过: {exc}")
        return out
    for gid in groups:
        try:
            out[gid] = await digest_group(gid, force=force)
        except Exception as exc:  # noqa: BLE001 — 一个群炸了不能拖累其它群
            logger.warning(f"[memory] 群{gid} 整理异常: {exc}")
            out[gid] = {"status": "error", "why": str(exc)}
    return out
