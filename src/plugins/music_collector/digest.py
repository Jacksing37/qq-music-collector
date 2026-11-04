"""把群聊喂给模型，让它自己整理出「每个人是什么样的」，写进档案。

这是「主动记忆」的那一半：``remember`` 工具要等模型在对话中想起来才用，
而这里每隔几小时扫一遍**旁听到的群聊**，让模型主动归纳。

两种整理模式，由 ``memory.buckets_enabled`` 选（默认关 = 老的自由文本模式）：

- **扁平模式**（关）：模型回 ``{"people":[{"uid","facts":[...]}]}``，每条事实追加
  进档案正文，行为与改造前逐字相同；
- **三桶模式**（开）：模型回 ``{"people":[{"uid","who":[...],"us":[...],
  "event":[...],"recent_new":[...]}]}``，我们**整份重写**这个人的档案。
  整份重写才是「合并、改写、淘汰旧条目」能做对的前提——追加式永远做不到
  「搬家了就改写那一条，绝不新旧并存」。

设计红线（前三条是改造前就有的，第四条是整份重写引入的）：

1. **只写本次素材里出现过的人**。模型偶尔会编一个 QQ 号出来，写进去就是
   一条查无此人的幽灵档案——所以 uid 必须出现在这次喂进去的消息里。
2. **解析失败不消耗素材**。整理结果不是合法 JSON 时不标记 ``digested``，
   下一轮还会重试；否则一次模型抽风就把那段聊天永久丢掉了。
3. **失败绝不打扰群里**。这是后台任务，出问题只写日志。
4. **整份重写必须先备份、并防误删**。写盘前把旧文件拷一份 ``.bak``；若本次解析出
   的长期条目数**不足既有的半数**，视为疑似误删 → 告警 + 用 ``.bak`` 回滚，
   库内容一个字节都不变。另外同一群同一时刻只允许一次整理在跑（:func:`begin_digest`），
   否则两个任务各基于同一份旧库算一遍，后提交的会覆盖先提交的。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
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

#: 一次整理最多看多少条新消息。以前是配置项 ``memory.digest_msgs``，但它和
#: ``memory.digest_chars`` 限的是同一份素材（条数 / 字数），留两个旋钮只会
#: 改一个忘一个、互相抵消；真正该调的是字数。这里固定一个足够大的值。
DIGEST_MAX_MSGS = 300

# ---------------------------------------------------------------- 三桶模式


#: 三桶模式的系统提示词。和扁平模式一样不给人设，但要额外说清「这是整理不是重写」——
#: 少了这句，模型每次都会照新材料从零写一份，旧条目就全丢。
DIGEST_SYSTEM_BUCKETS = (
    "你在整理一个 QQ 群的成员档案。档案按「他是谁 / 我们之间 / 事件」三个桶存，"
    "你的任务是把这次的聊天记录**并进**已有档案，输出整理后的整份档案。"
    "只依据给定材料，不推测、不补全、不评价。"
)

#: 给模型的三桶指令。判据、合并规则、配额、红线缺一不可：
#: - 没有判据 → 模型把同一条写进两个桶，或者记流水账；
#: - 没有合并规则 → 出现「他住杭州」和「他搬到上海了」并存；
#: - 没有「上限不是目标」→ 模型为了凑满配额开始编；
#: - 没有红线 → 会记「你是不是 AI」这类话题和编造的身世。
_PROMPT_BUCKETS = """下面是 QQ 群 {gid} 最近的一段聊天记录，以及我手上已有的档案。

【已有档案】（这是**整理**：在它的基础上增补与合并，不是从零重写）
{known}

【聊天记录】
{material}

只输出 JSON，不要任何解释文字：
{{"people":[{{"uid":"QQ号","nick":"昵称",
 "who":[{{"c":"是学生，住寝室","seen":"2026-09-14","n":2}}],
 "us":[{{"c":"习惯每天互道晚安","seen":"2026-09-15","n":3}}],
 "event":[{{"c":"9月16日要交作业","seen":"2026-09-15","n":1}}],
 "recent_new":[{{"d":"2026-09-16","c":"中午刚醒，说要去吃饭"}}]}}],
 "group":["群层面的共识或黑话"]}}

每个条目：c = 内容（一句话、40 字以内、陈述句、省略主语）；
seen = 最近一次提到的日期（YYYY-MM-DD）；n = 累计提到了几次（拿不准就写 1）。
确实需要保密的条目加 "sensitive":true，这类条目不会被写进档案。

三个桶怎么分（判据互斥，一条只进一个桶）：
1. who（他是谁）：这个人是什么样的人——身份、城市、家人、长期偏好与雷区、稳定作息。
   判据：**半年后依然为真，且只关于他**。不记：他说过什么、答应过什么。
2. us（我们之间）：群内关系的**状态**——称呼、群内梗与共识、群规、共同约定。
   判据：**描述状态，不是单次的事，而且不带具体日期**。
   ⚠️ 这里的「我们」是**机器人与这个群**，不是「你和我」。绝不能写成把某个群友
   当私聊对象的句子（不写「我们昨天说好……」这种）。
   必须用**状态口吻**，不用债务口吻：✅「习惯每天互道晚安」 ❌「他答应每天说晚安」
   （后者会变成还不完的欠条，以后每轮都想去追讨）。
3. event（事件）：有确定时间点的事。判据：**能说出「哪天」**。
   时间点直接写进句子（✅「9月16日要交作业」），不要另设日期字段。
   模糊时间（「过阵子」「改天」）填不出日期，不要记——它会变成永不失效的幽灵事件。
4. recent_new（近期流水）：最近几天的日常，只写这次**新出现**的
   （中午刚醒、去吃饭、今天加班到十点）。不要重复已有档案里的内容，过期由代码负责。

合并规则（整理，不是重写）：
- 同一件事反复提到 → 合成一条，n 累加，seen 取最近那次；
- 状态被取代（搬家、换工作、换对象）→ **改写那一条**，绝不新旧并存；
- 事件有了结果 → 改成结果（「9月16日要交的作业交了」）；不知道结果就原样保留，不许猜；
- 已有档案里这次材料**没有提到**的条目，原样抄回去，不要删。

配额（按**条目数**算，不是字数）：
- 一个人合计不超过 {max_facts} 条；每桶不超过 who {q_who} / us {q_us} / event {q_event} 条；
- recent_new 不占这个配额，但只写最近 {recent_days} 天的事；
- **配额是上限不是目标**：没到上限不要凑数，更不许编造；超了先删最不重要、最可能过时的。

红线：
- 不许记编造的身世与外部经历（他说自己是谁就是谁，不要替他补全成一个完整的故事）；
- 不许记「你是不是 AI / 机器人 / 程序」这类话题；
- 不写密码、手机号、住址、身份证号、真实姓名；
- **本轮没有新信息，就把已有档案原样输出**，不许借机删条目；
- 只输出**这次聊天记录里出现过的人**，其他人的档案不用输出（它们原样不动）。
"""

#: 顶层键 → 桶。同时认中文标题，是因为模型偶尔会照着小节标题写键名。
_BUCKET_ALIASES: dict[str, str] = {
    "who": "who",
    "他是谁": "who",
    "us": "us",
    "我们之间": "us",
    "event": "event",
    "事件": "event",
    "recent_new": memory_mod.RECENT,
    "recent": memory_mod.RECENT,
    "近期": memory_mod.RECENT,
}

#: 一条条目的字数上限。超了**截断**而不是丢掉：半句话也比一片空白有信息量。
_FACT_CHARS = 40
#: ``## 近期`` 最多留几条（``recent_days`` 按天淘汰之外的第二道闸，防一天刷爆）
_RECENT_MAX = 30
#: 覆盖防护的比例：新条目数 × 2 < 旧条目数 → 判定疑似误删
_COVER_RATIO = 2

#: ``c`` 结尾的「（2026-09-14 提到，2 次）」注解。模型抄回已有档案时常常把注解
#: 一起抄进 ``c``，不剥掉就会一次叠一层。
_ANNOTATION = re.compile(r"[（(]\s*\d{4}-\d{2}-\d{2}[^）)]*[）)]\s*$")
#: ``## 近期`` 条目的日期前缀
_RECENT_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+")


# ---------------------------------------------------------------- 解析


def _json_object(text: str) -> Optional[dict]:
    """从模型回复里抠出 JSON 对象。抠不出来返回 ``None``。

    兼容三种常见形态：纯 JSON、`` ```json `` 包裹、前后带解释文字。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001 — 模型输出不该让任务崩掉
        return None
    return data if isinstance(data, dict) else None


def parse_reply(text: str) -> dict:
    """从模型回复里抠出 JSON。抠不出来返回空 dict（调用方据此判定「失败」）。

    **返回值契约没变**（空 dict = 失败），三桶模式用 :func:`parse_buckets`
    拿到更明确的 ``None``：那个函数要区分「合法 JSON 但不合格式」和「压根不是 JSON」。
    """
    return _json_object(text) or {}


def _one_line(value: object, limit: int = _FACT_CHARS) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _entry(value: object) -> Optional[dict]:
    """把一条条目收敛成 ``{"c","seen","n","sensitive"}``。

    认三种写法：纯字符串（模型偷懒）、``{"c":...}``、以及旧扁平格式的 ``{"fact":...}``。
    空内容返回 ``None``（调用方跳过）。
    """
    if isinstance(value, str):
        text, seen, count, sensitive = value, "", 1, False
    elif isinstance(value, dict):
        text = (
            value.get("c")
            or value.get("content")
            or value.get("text")
            or value.get("fact")
            or ""
        )
        seen = value.get("seen") or value.get("date") or value.get("d") or ""
        try:
            count = int(value.get("n") or value.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        sensitive = bool(value.get("sensitive"))
    else:
        return None
    text = _ANNOTATION.sub("", _one_line(text)).strip()
    if not text:
        return None
    return {
        "c": text,
        "seen": _one_line(seen, 20),
        "n": max(1, min(999, count)),
        "sensitive": sensitive,
    }


def _entries(value: object) -> list[dict]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        entry = _entry(item)
        if entry is not None:
            out.append(entry)
    return out


def parse_buckets(text: str) -> Optional[dict]:
    """解析三桶整理结果。**解析不了返回 ``None``**，调用方据此放弃本次整理。

    为什么是 ``None`` 而不是空 dict：调用方要区分「模型没按格式回」和「模型回了一份
    空库」——前者不该消耗素材（下一轮重试），后者要触发覆盖防护。分析失败时
    **绝不半写**：宁可不整理，也不能把一份坏结果落到磁盘上。

    返回的规范结构::

        {"people": [{"uid","nick","who","us","event","recent","unclassified"}],
         "group": ["群层面的共识"], "form": "buckets" | "flat"}

    兼容两种老形态：整份回复是旧扁平格式 ``{"facts":[...]}``（落到未分类桶），
    以及顶层直接给桶而没有 ``people`` 数组（当单个人的档案处理）。
    """
    data = _json_object(text)
    if data is None:
        return None

    records: list[dict] = []
    raw_people = data.get("people")
    if isinstance(raw_people, list):
        records = [x for x in raw_people if isinstance(x, dict)]
    elif isinstance(raw_people, str):
        records = []
    else:
        # 顶层直接给桶：把整份回复当成一个人的档案（uid 可能写在顶层）
        if any(key in data for key in _BUCKET_ALIASES) or "facts" in data:
            records = [data]

    if not records and "people" not in data and not any(
        key in data for key in _BUCKET_ALIASES
    ):
        # 既没有 people 数组、也没有任何桶键 → 不认识的形状
        return None
    if isinstance(raw_people, list) and not records:
        # people 是空数组：形状对，但一个人都没有。这**不是**解析失败，
        # 交给调用方判定「模型回了空结果」并告警（仍然不写盘、不消耗素材）。
        return {"people": [], "group": _str_list(data.get("group")), "form": "buckets"}

    people: list[dict] = []
    form = "buckets"
    for raw in records:
        record = {
            "uid": str(raw.get("uid") or "").strip(),
            "nick": str(raw.get("nick") or "").strip()[:40],
            "who": [],
            "us": [],
            "event": [],
            memory_mod.RECENT: [],
            memory_mod.UNSORTED: [],
        }
        saw_bucket = False
        for key, bucket in _BUCKET_ALIASES.items():
            if key in raw:
                saw_bucket = True
                record[bucket].extend(_entries(raw[key]))
        # 旧扁平格式：facts 一条都分不清是「他是谁」还是「哪天的事」。
        # **不硬塞进 event**——没有时间点的东西进了事件桶就是永不失效的幽灵事件。
        for key in ("facts", "fact"):
            if key in raw:
                record[memory_mod.UNSORTED].extend(_entries(raw[key]))
        if not saw_bucket:
            form = "flat"
        people.append(record)
    return {"people": people, "group": _str_list(data.get("group")), "form": form}


def _str_list(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()][:10]


def _render_fact(entry: dict) -> str:
    """条目落盘的样子：``内容（2026-09-14 提到，2 次）``。

    日期与次数写在**同一条**里，而不是另开字段：档案是手也能改的 markdown，
    多一个字段就多一处会被手改坏的地方；而「哪天提的」这件事模型读书面句子就能懂。
    """
    text = entry["c"]
    seen = (entry.get("seen") or "").strip()
    count = int(entry.get("n") or 1)
    if not seen:
        return text
    if count > 1:
        return f"{text}（{seen} 提到，{count} 次）"
    return f"{text}（{seen} 提到）"


# ---------------------------------------------------------------- 已知档案


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


def known_buckets(people: list, budget: int = KNOWN_CHARS) -> str:
    """把已有档案按三桶摊开给模型看（三桶模式用）。

    和 :func:`known_digest` 的区别：那边只要「这个人是谁」的一行摘要，够模型判断
    「这条是不是新的」；这边要让模型**把旧条目原样抄回去**，所以必须给它看得见
    每一条原文，否则它会以为旧档案是一条摘要、然后自己重写一遍。
    """
    rows: list[str] = []
    used = 0
    for person in people:
        sections, rest = memory_mod.parse_body(person.body)
        lines = [f"- {person.display}"]
        any_item = False
        for key in (*memory_mod.BUCKETS, memory_mod.UNSORTED):
            items = sections.get(key) or []
            if not items:
                continue
            any_item = True
            title = memory_mod.BUCKET_TITLES.get(key, memory_mod.UNSORTED_TITLE)
            lines.append(f"  {title}：" + "；".join(items))
        recent = sections.get(memory_mod.RECENT) or []
        if recent:
            lines.append("  近期：" + "；".join(recent[-5:]))
        if not any_item and not recent and rest.strip():
            # 旧格式档案：整段正文给模型看，并注明「原样保留即可」
            lines.append("  未分类（旧格式，原样保留即可）：" + " ".join(rest.split()))
        row = "\n".join(lines)
        if used + len(row) > budget:
            rows.append(f"（另有 {len(people) - len(rows)} 人的档案未列出）")
            break
        rows.append(row)
        used += len(row)
    return "\n".join(rows) if rows else "（还没有任何档案）"


# ---------------------------------------------------------------- 同群单例


#: 正在整理的群 → 开始时刻（时刻只用于日志与「卡住多久了」的排查）。
#: 进程内共享：定时任务与网页端「立即整理」在同一个进程里，所以一个标记就够。
_DIGESTING: dict[int, float] = {}


def begin_digest(group_id: int) -> bool:
    """给这个群打上「整理中」标记。已经在整理则返回 ``False``，调用方直接放弃本次。

    标记要覆盖**整个**整理过程，包括锁外的那一次模型调用：真实风险不是文件写一半，
    而是两个任务基于同一份旧库各算一遍，后提交的把先提交的覆盖掉——那等于白跑一次
    还丢一次结果。
    """
    gid = int(group_id)
    if gid in _DIGESTING:
        return False
    _DIGESTING[gid] = time.time()
    return True


def end_digest(group_id: int) -> None:
    """摘掉「整理中」标记。放在 ``finally`` 里，异常路径也要摘。"""
    _DIGESTING.pop(int(group_id), None)


def is_digesting(group_id: int) -> bool:
    return int(group_id) in _DIGESTING


# ---------------------------------------------------------------- 整理


async def digest_group(group_id: int, *, mem: Optional[MemoryConfig] = None, force: bool = False) -> dict:
    """整理一个群。返回统计字典（含 ``status``，供调度日志与网页端展示）。

    ``status`` 取值：``ok`` / ``skipped``（没新素材、没开、正在整理）/
    ``error``（模型或解析失败）/ ``partial``（写了但有人被覆盖防护回滚）。
    """
    app_cfg = service.cfg(group_id)
    cfg_mem = mem if mem is not None else app_cfg.memory
    llm_cfg: LLMConfig = app_cfg.llm
    if not cfg_mem.enabled:
        return {"status": "skipped", "why": "记忆功能没开"}
    if not llm_cfg.enabled or not (llm_cfg.base_url and llm_cfg.api_key):
        return {"status": "skipped", "why": "大模型没配置"}
    if not begin_digest(group_id):
        logger.info(f"[memory] 群{group_id} 正在整理中，本次重复触发直接跳过")
        return {"status": "skipped", "why": "这个群正在整理"}
    try:
        return await _digest_locked(group_id, cfg_mem, llm_cfg, force=force)
    finally:
        end_digest(group_id)


async def _digest_locked(
    group_id: int, cfg_mem: MemoryConfig, llm_cfg: LLMConfig, *, force: bool = False
) -> dict:
    """拿到「整理中」标记之后的正文。拆出来只为让 ``try/finally`` 一眼可见。"""
    since = 0.0
    if cfg_mem.chat_keep_days > 0:
        since = time.time() - float(cfg_mem.chat_keep_days) * 86400
    # 条数上限固定 300：它和 ``digest_chars`` 限的是同一份素材，留两个旋钮只会
    # 改一个忘一个、互相抵消。真正该调的是字数（``digest_chars``）。
    rows = await service.store.pending_digest(
        group_id, limit=DIGEST_MAX_MSGS, since=since
    )
    if not rows:
        return {"status": "skipped", "why": "没有新消息"}

    material = memory_mod.digest_source(rows, max_chars=max(1000, int(cfg_mem.digest_chars)))
    if not material.strip():
        return {"status": "skipped", "why": "消息里没有可用文字"}

    if cfg_mem.buckets_enabled:
        return await _digest_buckets(group_id, cfg_mem, llm_cfg, rows, material)
    return await _digest_flat(group_id, cfg_mem, llm_cfg, rows, material)


async def _digest_flat(
    group_id: int,
    cfg_mem: MemoryConfig,
    llm_cfg: LLMConfig,
    rows: list,
    material: str,
) -> dict:
    """老的自由文本整理：一条条事实追加进正文。**行为与改造前逐字相同。**"""
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


async def _digest_buckets(
    group_id: int,
    cfg_mem: MemoryConfig,
    llm_cfg: LLMConfig,
    rows: list,
    material: str,
) -> dict:
    """三桶整理：整份重写每个人的档案，带备份与覆盖防护。"""
    allowed = {str(r.get("user_id") or "") for r in rows}
    allowed.discard("")
    people = memory_mod.list_people(cfg_mem, group_id)
    # 只把「这次材料里出现过的人」的档案摊给模型：既省上下文，也让输出规模有界
    scope = [p for p in people if p.uid in allowed]
    quotas = _quotas(cfg_mem)
    prompt = _PROMPT_BUCKETS.format(
        gid=group_id,
        known=known_buckets(scope),
        material=material,
        max_facts=int(cfg_mem.l1_max_facts),
        q_who=quotas["who"],
        q_us=quotas["us"],
        q_event=quotas["event"],
        recent_days=int(cfg_mem.recent_days),
    )
    reply, err = await llm_bot.complete(
        prompt,
        llm_cfg,
        system=DIGEST_SYSTEM_BUCKETS,
        model=cfg_mem.digest_model,
        max_tokens=int(cfg_mem.digest_max_tokens),
        temperature=0.2,
    )
    if not reply:
        logger.warning(f"[memory] 群{group_id} 三桶整理失败（模型没回）: {err}")
        return {"status": "error", "why": err or "模型没有返回内容"}

    data = parse_buckets(reply)
    if data is None:
        logger.warning(
            f"[memory] 群{group_id} 三桶结果解析不出来，本轮不消耗素材：{reply[:200]}"
        )
        return {"status": "error", "why": "模型没按 JSON 回答"}
    records = data.get("people") or []
    if not records:
        # 一份空结果意味着「这次什么都没记住」。它既可能是素材太水，也可能是模型抽风；
        # 无论哪种，写入都毫无收益、却要冒着把整库抹成空的风险——所以直接放弃。
        logger.warning(f"[memory] 群{group_id} 三桶结果里一个人都没有，本轮不写也不消耗素材")
        return {"status": "error", "why": "模型返回了空结果"}

    wrote_people = 0
    wrote_facts = 0
    wrote_recent = 0
    sensitive = 0
    rolled: list[str] = []
    for record in records:
        uid = str(record.get("uid") or "").strip()
        if uid not in allowed:
            logger.info(f"[memory] 群{group_id} 跳过材料里没出现过的 uid={uid!r}")
            continue
        res = _write_buckets(cfg_mem, group_id, uid, record)
        if res.get("status") == "ok":
            wrote_people += 1
            wrote_facts += int(res.get("facts") or 0)
            wrote_recent += int(res.get("recent") or 0)
            sensitive += int(res.get("sensitive") or 0)
        elif res.get("status") == "rolled_back":
            rolled.append(uid)
            logger.warning(f"[memory] 群{group_id} {uid} 疑似误删，已用 .bak 回滚：{res.get('why')}")

    group_added, _ = memory_mod.append_group_note(
        cfg_mem, group_id, _str_list(data.get("group"))
    )

    last_id = max(int(r.get("id") or 0) for r in rows)
    marked = await service.store.mark_digested(group_id, last_id)
    logger.info(
        f"[memory] 群{group_id} 三桶整理完成：看 {len(rows)} 条消息 → "
        f"{wrote_people} 人 / 长期条款 {wrote_facts} 条 / 近期 {wrote_recent} 条"
        f" / 群概况 {group_added} 条 / 回滚 {len(rolled)} 人 / 丢弃敏感 {sensitive} 条"
        f"（标记 {marked} 条为已整理）"
    )
    return {
        "status": "ok" if not rolled else "partial",
        "rows": len(rows),
        "people": wrote_people,
        "facts": wrote_facts,
        "recent": wrote_recent,
        "sensitive": sensitive,
        "rolled_back": rolled,
        "group_facts": group_added,
        "marked": marked,
    }


def _quotas(mem: MemoryConfig) -> dict[str, int]:
    """读出各桶配额，坏值一律退回默认（配置被人手改坏时不能让整理整个停摆）。"""
    default = {"who": 20, "us": 24, "event": 16}
    raw = mem.l1_quota if isinstance(mem.l1_quota, dict) else {}
    out: dict[str, int] = {}
    for key, fallback in default.items():
        try:
            value = int((raw or {}).get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        out[key] = value if value > 0 else fallback
    return out


def _trim_bucket(items: list[str], cap: int) -> list[str]:
    """把一桶条目削到 ``cap`` 条：**先删最久没被提到的那几条**。

    为什么不是简单截尾：条目按「旧在前、新在后」排，截尾等于把这一轮刚记住的
    全删掉，只留最老的——正好反了。``seen`` 最旧的才是「最可能已经过时」的。
    没有日期的条目排在最后才删：人工写进去的东西不该被代码优先扔掉。
    """
    if cap < 0 or len(items) <= cap:
        return items

    def seen_of(text: str) -> str:
        matched = re.search(r"[（(]\s*(\d{4}-\d{2}-\d{2})", text)
        return matched.group(1) if matched else "9999-99-99"

    order = sorted(range(len(items)), key=lambda i: (seen_of(items[i]), i))
    drop = set(order[: len(items) - cap])
    return [text for index, text in enumerate(items) if index not in drop]


def _render_entries(new: list[dict]) -> list[str]:
    """把新条目渲染成落盘文本，并按「去掉注解后的内容」去重。

    **刻意不和旧条目求并集**：模型回的就是整理后的整份档案——它被要求把这次材料里
    没提到的旧条目原样抄回去，而「状态被取代就改写那一条」必须能真的把旧句删掉，
    求并集会让新旧并存（「他住杭州」+「他搬到上海了」永远同时出现）。
    漏抄的风险由覆盖防护兜：条目数掉一半以上就整份回滚。
    """
    seen: dict[str, str] = {}
    for entry in new:
        text = _render_fact(entry)
        seen[_ANNOTATION.sub("", text).strip()] = text
    return list(seen.values())


def _prune_recent(items: list[str], keep_days: int) -> list[str]:
    """按天淘汰近期流水：早于 ``今天 - keep_days`` 的条目丢掉。

    时间性淘汰交给代码、语义性淘汰（合并/取代）交给模型——这是分工，不是遗漏。
    没有日期前缀的条目（人手加的）算不出来，**保留**：宁可多留一条，也不替人删字。
    """
    cutoff = datetime.now() - timedelta(days=max(1, int(keep_days)))
    kept: list[str] = []
    seen: set[str] = set()
    for text in items:
        cleaned = " ".join(str(text).split())
        if not cleaned or cleaned in seen:
            continue
        matched = _RECENT_DATE.match(cleaned)
        if matched:
            try:
                stamp = datetime.strptime(matched.group(1), "%Y-%m-%d")
            except ValueError:
                stamp = None
            if stamp is not None and stamp < cutoff:
                continue
        seen.add(cleaned)
        kept.append(cleaned)
    return kept[-_RECENT_MAX:]


def _write_buckets(
    mem: MemoryConfig, group_id: int, uid: str, record: dict
) -> dict:
    """整份重写一个人的档案：备份 → 写入 → 覆盖防护（不足半数就回滚）。"""
    person = memory_mod.load_person(mem, group_id, uid)
    sections, rest = memory_mod.parse_body(person.body if person else "")
    old_facts = sum(len(sections.get(key) or []) for key in memory_mod.BUCKETS)

    quotas = _quotas(mem)
    merged: dict[str, list[str]] = {}
    sensitive = 0
    for key in memory_mod.BUCKETS:
        raw_items = record.get(key) or []
        entries = [e for e in raw_items if not e.get("sensitive")]
        sensitive += len(raw_items) - len(entries)
        merged[key] = _trim_bucket(_render_entries(entries), quotas[key])

    # 合计上限：先削 event（时效最短），再 us，最后 who —— 削掉长期特征最伤
    total = sum(len(merged[key]) for key in memory_mod.BUCKETS)
    for key in ("event", "us", "who"):
        need = total - int(mem.l1_max_facts)
        if need <= 0:
            break
        before = len(merged[key])
        merged[key] = _trim_bucket(merged[key], max(0, before - need))
        total -= before - len(merged[key])

    # 近期流水：旧条目 + 这次新增的，按天淘汰
    recent_old = list(sections.get(memory_mod.RECENT) or [])
    recent_new: list[str] = []
    for entry in record.get(memory_mod.RECENT) or []:
        if entry.get("sensitive"):
            sensitive += 1
            continue
        stamp = (entry.get("seen") or "").strip()
        text = entry["c"]
        recent_new.append(f"{stamp} {text}" if stamp else text)
    recent = _prune_recent(recent_old + recent_new, int(mem.recent_days))

    # 未分类（旧扁平格式 / 人手写的未知小节）：**不参与模型输入，但一定要留在文件里**。
    # 这次如果是旧格式回的（模型给了 facts），新内容也只能落到这里——它分不清是
    # 「他是谁」还是「哪天的事」，硬塞进 event 就是一条没有时间点的幽灵事件。
    fresh_unsorted = _render_entries(record.get(memory_mod.UNSORTED) or [])
    unsorted_items = list(
        dict.fromkeys([*(sections.get(memory_mod.UNSORTED) or []), *fresh_unsorted])
    )
    body = memory_mod.join_body(
        {
            **{key: merged[key] for key in memory_mod.BUCKETS},
            memory_mod.RECENT: recent,
            memory_mod.UNSORTED: unsorted_items,
        },
        rest,
    )
    new_facts = sum(len(merged[key]) for key in memory_mod.BUCKETS)
    if not body.strip():
        # 一条都不剩（模型给的全是敏感条目、或者本来就是空档案）：不建空文件。
        # 空档案在网页上和一整份被抹掉的档案长得一样，不如根本不写。
        return {"status": "skipped", "why": "整理结果为空，没有写入"}

    path = memory_mod._path_for(mem, group_id, uid)
    backup = path.with_name(path.name + ".bak")
    had_file = path.exists()
    if had_file:
        # 没有备份就不写：整份重写是全项目唯一一处「一次可能抹掉很多字」的写盘
        try:
            backup.write_bytes(path.read_bytes())
        except OSError as exc:
            logger.warning(f"[memory] 群{group_id} {uid} 备份失败，本次不写: {exc}")
            return {"status": "error", "why": f"备份失败：{exc}"}

    target = person or memory_mod.Person(uid=uid, first_seen=memory_mod._now())
    if (record.get("nick") or "").strip():
        target.nick = str(record["nick"]).strip()[:40]
    target.last_seen = memory_mod._now()
    target.body = body
    memory_mod.save_person(mem, group_id, target)

    # 覆盖防护：整份重写是全项目唯一一处「一次可能抹掉很多字」的写盘，所以写完之后
    # 还要对一次数。不足既有的一半 → 基本可以断定模型漏抄或抽风（不是真的该删），
    # 用备份整份回滚，库内容一个字节都不变。
    if old_facts and new_facts * _COVER_RATIO < old_facts:
        why = f"条目数从 {old_facts} 掉到 {new_facts}（不足一半），判为疑似误删"
        logger.warning(f"[memory] 群{group_id} {uid} {why}，已用 .bak 回滚，库不变")
        try:
            memory_mod._atomic_write(path, backup.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning(f"[memory] 群{group_id} {uid} 回滚失败（旧文件还在 .bak）: {exc}")
        return {"status": "rolled_back", "why": why, "facts": new_facts}

    return {
        "status": "ok",
        "facts": new_facts,
        "recent": len(recent),
        "sensitive": sensitive,
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
