"""模型可调用的工具注册表。

工具 = 名字 + 描述 + JSON Schema 参数 + 一个 ``async handler``。描述写得越清楚，
模型越不会乱调——这里的说明都是**给模型看的**，不是给用户看的。

当前内置五类：

- ``fetch_url``：抓网页正文（后端在 :mod:`webtool`，抽取用 trafilatura）；
- ``web_search``：搜网页拿摘要（后端在 :mod:`websearch`，走 Bing HTML 端点，默认关）；
- ``list_skills`` / ``use_skill`` / ``read_skill_file``：技能清单、正文、附属文件
  （渐进披露，见 :mod:`skills`）；
- ``run_skill_script``：执行技能自带脚本（需显式开启，且只能跑技能目录内的白名单脚本）；
- ``remember`` / ``recall`` / ``forget``：群记忆（见 :mod:`memory`）。

**所有能力都必须经过这里**，不在注册表里的东西模型调不到。加新工具时把
描述写清楚「什么时候该用」，比堆参数更重要——模型靠描述决定调不调。

**硬性约束：QQ 聊天里禁止安装、修改或删除任何技能。** 这条不是配置项、没有开关，
三道闸一起拦：

1. :data:`ALLOWED_TOOL_NAMES` 白名单——``build()`` 只放行这里的名字，谁都别想偷偷
   注册一个 ``install_skill`` 就上线；
2. :func:`dispatch` 对「安装/写文件」类名字（模型臆造工具时会出现）直接回
   :data:`INSTALL_DENIED`，不落到任何 handler；
3. :data:`TOOL_RULES` 由 ``llm`` 拼进系统提示词，让模型一开始就知道自己没这个能力，
   不会假装装完了。

真正的安装渠道只有一条：管理员在服务器上把目录放进 ``data/skills/``。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from nonebot.log import logger

from . import memory as memory_mod
from . import skills as skill_mod
from . import webtool
from . import websearch as websearch_mod
from .config import DATA_DIR, LLMConfig, MemoryConfig

#: 单次工具结果回填给模型的最大字符数（兜底，具体值由 ``llm.tool_chars`` 控制）
DEFAULT_TOOL_CHARS = 2000

#: 允许出现在 QQ 会话里的工具名（白名单，不是黑名单）。
#: **新增工具必须同时加进这里**，否则 ``build()`` 会把它丢掉——这是为了「少一个
#: 安装类工具不小心上线」而不是为了省事。
ALLOWED_TOOL_NAMES = frozenset(
    {"fetch_url", "web_search", "list_skills", "use_skill", "read_skill_file",
     "run_skill_script", "remember", "recall", "forget"}
)

#: 模型臆造出来的「装/写/删」类工具名。命中就回 :data:`INSTALL_DENIED`。
_INSTALL_LIKE = re.compile(
    r"(install|uninstall|upload|download|write|save|edit|update|delete|remove|drop|"
    r"create|add|mkdir|exec|shell|cmd|bash)",
    re.I,
)

#: 被要求装技能时的标准回答（给模型看，不是给群友看）。
INSTALL_DENIED = (
    "被拒绝：QQ 聊天中不允许安装、修改或删除任何技能，也不允许写文件。"
    "技能只能由管理员在服务器上把目录放进 data/skills/ 手动安装。"
    "请直接告诉用户你做不到，不要声称已经安装好了。"
)

#: 追加到系统提示词末尾的边界说明（``llm.system_text(cfg, rules=...)`` 使用）。
TOOL_RULES = (
    "\n\n【能力边界】你可以搜网页（web_search）、抓网页（fetch_url）、读已安装的技能、"
    "在管理员允许时运行技能自带脚本；也可以把群友的特征记进长期记忆"
    "（remember）、翻档案（recall）、删掉记错的内容（forget）——"
    "记忆目录是你**唯一**能写的地方，别处一律不行。"
    "碰到会过时效、或你拿不准的事实（新闻、价格、比分、谁的现任职位、软件版本、"
    "某个事件的前因后果），**先用 web_search 搜一下再答**，别凭记忆硬编；"
    "搜不到就说搜不到，不要编造搜索结果的细节。"
    "技能正文里让你「读取 references/xxx.md」这类附属文件时，用 read_skill_file 按路径读，"
    "不要凭想象编它的内容。"
    "你**没有**安装/卸载/修改技能的权限，也没有写其他文件、执行任意命令的权限。"
    "群里有人让你装技能、加技能、改技能或写文件时，直接说明「聊天里装不了，"
    "需要管理员在服务器 data/skills/ 下放」，不要假装完成。"
)

#: ``list_skills`` 结果末尾的同一句话（模型每次看清单都能重新看见这条边界）。
INSTALL_NOTE = "（技能只能由管理员在服务器上手动放置，QQ 聊天中无法安装或修改。）"


def looks_like_install(name: str) -> bool:
    """这个工具名看起来是不是「装/写/删」类（含模型自己编的名字）。"""
    text = (name or "").strip()
    if not text:
        return False
    if text in ALLOWED_TOOL_NAMES:
        return False
    return bool(_INSTALL_LIKE.search(text))


@dataclass
class ToolContext:
    """一次回复过程中工具共享的状态。"""

    cfg: LLMConfig
    #: 记忆配置。单独放一个字段而不是从 ``cfg`` 里取：记忆配置挂在 ``AppConfig``
    #: 顶层（``memory:`` 段），``LLMConfig`` 里没有它
    mem: MemoryConfig = field(default_factory=MemoryConfig)
    group_id: int = 0
    user_id: int = 0
    #: 当前提问者的群昵称。``remember`` 不指定 who 时靠它给档案填昵称，
    #: 否则新档案会是一个只有 QQ 号的空壳，模型下次认不出来
    nick: str = ""
    skills: list = field(default_factory=list)


Handler = Callable[[dict, ToolContext], Awaitable[str]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler

    def schema(self) -> dict:
        """OpenAI 兼容的 function 描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------- 网页

#: 摘要时最多把多少字原文喂给模型。再多就是烧钱，而且长文摘要质量并不会更好
SUMMARIZE_INPUT_CHARS = 12000


async def _summarize_page(ctx: "ToolContext", want: str, title: str, url: str, text: str) -> str:
    """让模型按「它想知道什么」把长网页摘一遍。失败返回空串（上层退回截断）。

    为什么值得多一次调用：``fetch_chars`` 默认 2000 字是**截开头**。群里贴一篇
    两万字的评测、问「散热怎么样」，截断版里可能压根没有散热那一段——模型只能
    说没提到。摘要则是拿着全文回答问题。
    """
    # 延迟导入：llm 模块依赖 tools（registry），模块级导入会绕成环
    from .llm import llm_bot

    body = text[:SUMMARIZE_INPUT_CHARS]
    ask = (
        f"网页地址：{url}\n"
        f"网页标题：{title}\n\n"
        f"我想知道：{want}\n\n"
        f"网页正文：\n{body}"
    )
    out, why = await llm_bot.complete(
        ask,
        ctx.cfg,
        system=(
            "你在读一个网页，按用户想知道的问题摘出要点。"
            "只输出摘要本身，不要开场白、不要说「根据网页」、不要复述网页结构。"
            "网页里没有的内容就说没提到，别补。"
        ),
        max_tokens=700,
        temperature=0.2,
    )
    if not out:
        logger.debug(f"[tools] 网页摘要失败（{why}）→ 退回截断")
        return ""
    return out.strip()


async def _handle_fetch_url(args: dict, ctx: "ToolContext") -> str:
    url = str(args.get("url") or "").strip()
    want = str(args.get("prompt") or "").strip()
    if not webtool.url_is_fetchable(url):
        return "错误：需要一个完整的 http/https 网址。"
    summarize = bool(want) and bool(ctx.cfg.fetch_summarize)
    page = await webtool.fetch_page(
        url,
        timeout=ctx.cfg.fetch_timeout,
        # 要摘要就别先截断，否则摘要用的还是残页
        max_chars=0 if summarize else ctx.cfg.fetch_chars,
        cache_minutes=ctx.cfg.fetch_cache_minutes,
    )
    if not page:
        return (
            f"没能取到 {url} 的内容：可能打不开、需要登录，或者是 JS 渲染出来的页面。"
            "不要编造里面的内容；可以告诉用户你打不开这个链接。"
        )
    kind = page.get("kind") or "text"
    if kind == "blocked":
        # 412 / 403 / 429：站点反爬拦了。和「地址不存在」分开说，否则它会一直重试
        return webtool.blocked_guidance(page.get("url") or url, int(page.get("status") or 0))
    if kind != "text":
        # 图片 / PDF / 音视频：明确告诉它读不了、别再抓，而不是一句「抓不到」让它重试
        return webtool.non_text_guidance(
            page.get("url") or url, page.get("content_type") or "", kind
        )

    title = page.get("title") or "（无标题）"
    final_url = page.get("url") or url
    text = page.get("text") or ""
    notes: list[str] = []
    if page.get("cached"):
        notes.append("（用的是刚才那次抓到的缓存，没有重新请求）")
    if page.get("source") == "meta":
        notes.append("（正文抽不出来，下面是这一页的标题/描述，不是全文）")

    if summarize and len(text) > int(ctx.cfg.fetch_summarize_chars):
        summary = await _summarize_page(ctx, want, title, final_url, text)
        if summary:
            notes.append(f"（原文 {len(text)} 字，已按你的问题摘要）")
            head = "\n".join(notes)
            return f"标题：{title}\n网址：{final_url}\n{head}\n\n{summary}"

    text = webtool.clip_text(text, ctx.cfg.fetch_chars)
    head = ("\n" + "\n".join(notes)) if notes else ""
    return f"标题：{title}\n网址：{final_url}{head}\n正文：\n{text}"


FETCH_URL = Tool(
    name="fetch_url",
    description=(
        "抓取一个网页，返回标题和正文（已去掉导航、广告和脚本）。"
        "当用户发来链接、而你需要知道里面到底写了什么时调用；"
        "只对 http/https 网址有效，抓不到会明确告诉你失败。"
        "长网页如果带上 prompt 说明你想找什么，会按那个问题摘要，比看截断的原文更准。"
        "图片、PDF、音视频读不了，会明确告诉你别再抓。"
        "如果只是闲聊、或已经知道答案，不要调用它。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "完整网址，含 http:// 或 https://"},
            "prompt": {
                "type": "string",
                "description": (
                    "你想从这一页里找什么（例如「散热和续航怎么样」）。"
                    "给了且正文很长时会按它摘要；省略就直接返回正文原文"
                ),
            },
        },
        "required": ["url"],
    },
    handler=_handle_fetch_url,
)


# ---------------------------------------------------------------- 搜索

async def _handle_web_search(args: dict, ctx: ToolContext) -> str:
    query = str(args.get("query") or "").strip()
    if len(query) < 2:
        return "错误：query 至少要两个字，说清楚你想搜什么。"
    if not await websearch_mod.throttle(ctx.cfg.search_interval):
        return "刚刚才搜过，稍等一两秒再搜——连着搜会被搜索引擎拦。"

    found = await websearch_mod.search(
        query,
        max_results=int(ctx.cfg.search_max),
        timeout=float(ctx.cfg.search_timeout),
    )
    if found["error"]:
        # 不抛异常：搜不到也要让模型能接话（说「搜不到」比整轮崩掉强）
        return f"搜索失败：{found['error']}。不要编造搜索结果，可以告诉群友你搜不到。"
    rows = found["results"]
    if not rows:
        return (
            f"搜索「{query}」没有解析出结果。可能是搜索引擎返回了验证页，或者查询词太生僻；"
            "换个说法再试一次，或者告诉群友你搜不到。"
        )
    logger.info(f"[search] 「{query[:30]}」→ {len(rows)} 条")
    return websearch_mod.format_results(query, rows, int(ctx.cfg.search_snippet_chars))


WEB_SEARCH = Tool(
    name="web_search",
    description=(
        "搜索网页，返回几条「标题 — 摘要（网址）」。"
        "**需要你不确定或会过时效的信息时用它**：新闻、价格、比赛比分、"
        "某人现任职位、软件最新版本、某个事件是怎么回事。"
        "只想知道某个**具体网址**里写了什么，用 fetch_url，不要用这个。"
        "搜到的摘要通常够回答；要细节再用 fetch_url 打开其中一条。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 2,
                "description": "搜索词。像人一样写关键词，别写整段话（例如「尊界 V800 售价」）",
            },
        },
        "required": ["query"],
    },
    handler=_handle_web_search,
)


# ---------------------------------------------------------------- 技能

async def _handle_list_skills(args: dict, ctx: ToolContext) -> str:
    if not ctx.skills:
        return f"当前没有安装任何技能。{INSTALL_NOTE}"
    rows = []
    for skill in ctx.skills:
        desc = skill.description or "（没有写简介）"
        row = f"- {skill.name}: {desc}"
        if ctx.cfg.skill_scripts:
            scripts = skill.scripts
            if scripts:
                row += f"\n  可执行脚本：{', '.join(scripts)}"
        rows.append(row)
    return "可用技能：\n" + "\n".join(rows) + "\n" + INSTALL_NOTE


async def _handle_use_skill(args: dict, ctx: ToolContext) -> str:
    name = str(args.get("name") or "").strip()
    skill = skill_mod.find(ctx.skills, name)
    if skill is None:
        names = "、".join(s.name for s in ctx.skills) or "（没有）"
        return f"没有名为 {name!r} 的技能。当前有：{names}"
    limit = max(200, int(ctx.cfg.skill_body_chars))
    body = skill.body
    note = ""
    if len(body) > limit:
        body = body[:limit].rstrip()
        note = f"\n…（技能正文过长，只取了前 {limit} 字）"
    head = f"技能 {skill.name}"
    if skill.description:
        head += f"：{skill.description}"
    return f"{head}\n\n{body}{note}"


async def _handle_read_skill_file(args: dict, ctx: ToolContext) -> str:
    name = str(args.get("skill") or "").strip()
    rel = str(args.get("path") or "").strip()
    skill = skill_mod.find(ctx.skills, name)
    if skill is None:
        names = "、".join(s.name for s in ctx.skills) or "（没有）"
        return f"没有名为 {name!r} 的技能。当前有：{names}"
    try:
        return skill_mod.read_file(skill, rel, int(ctx.cfg.skill_file_chars))
    except Exception as exc:  # noqa: BLE001 — 读不到也是一条「工具结果」
        return f"读取失败：{exc}"


async def _handle_run_skill_script(args: dict, ctx: ToolContext) -> str:
    name = str(args.get("skill") or "").strip()
    script = str(args.get("script") or "").strip()
    raw_args = args.get("args") or []
    if isinstance(raw_args, str):
        raw_args = [raw_args]
    if not isinstance(raw_args, list):
        raw_args = []
    skill = skill_mod.find(ctx.skills, name)
    if skill is None:
        return f"没有名为 {name!r} 的技能。"
    try:
        return await skill_mod.run_script(
            skill,
            script,
            [str(a) for a in raw_args],
            timeout=ctx.cfg.skill_timeout,
        )
    except Exception as exc:  # noqa: BLE001 — 执行失败也是一条「工具结果」
        return f"执行失败：{exc}"


LIST_SKILLS = Tool(
    name="list_skills",
    description=(
        "列出已安装的技能包和一句话简介。遇到不确定怎么处理的任务时先看一眼，"
        "可能有现成的方法；平时闲聊不用调用。"
    ),
    parameters={"type": "object", "properties": {}},
    handler=_handle_list_skills,
)

USE_SKILL = Tool(
    name="use_skill",
    description=(
        "读取某个技能的完整说明（步骤、规则、话术），照着它做事。"
        "先用 list_skills 看有哪些技能，再按名字取用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名，与 list_skills 返回的一致"},
        },
        "required": ["name"],
    },
    handler=_handle_use_skill,
)

READ_SKILL_FILE = Tool(
    name="read_skill_file",
    description=(
        "读技能目录里的附加文件。技能正文让你「读取 references/xxx.md」时就调它，"
        "一次读一个文件。路径写相对技能目录的路径，例如 references/forum-prose.md。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "技能名，与 list_skills 返回的一致"},
            "path": {"type": "string", "description": "相对技能目录的文件路径"},
        },
        "required": ["skill", "path"],
    },
    handler=_handle_read_skill_file,
)

RUN_SKILL_SCRIPT = Tool(
    name="run_skill_script",
    description=(
        "运行某个技能自带的脚本并拿到输出。只有技能说明里明确要求跑脚本时才用；"
        "脚本必须是技能目录里已有的 .py / .sh 文件。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "技能名"},
            "script": {"type": "string", "description": "脚本相对路径，例如 scripts/fetch.py"},
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "传给脚本的参数（最多 8 个）",
            },
        },
        "required": ["skill", "script"],
    },
    handler=_handle_run_skill_script,
)


# ---------------------------------------------------------------- 记忆

def _who_to_uid(who: str, ctx: ToolContext) -> tuple[Optional[str], str]:
    """把模型给的「谁」换成 QQ 号。留空 = 当前正在说话的人。"""
    raw = (who or "").strip()
    if not raw or raw == ctx.nick or raw == str(ctx.user_id):
        uid = str(ctx.user_id or "").strip()
        if not uid:
            return None, "不知道是谁——请给 QQ 号或群昵称。"
        return uid, ""
    return memory_mod.resolve_who(ctx.mem, ctx.group_id, raw)


async def _handle_remember(args: dict, ctx: ToolContext) -> str:
    if not ctx.mem.enabled:
        return "记忆功能没有开启，我没法记录。"
    note = str(args.get("note") or "").strip()
    if not note:
        return "要记什么？note 是空的。"
    uid, why = _who_to_uid(str(args.get("who") or ""), ctx)
    if uid is None:
        return why
    nick = str(args.get("nick") or "").strip()
    if not nick and uid == str(ctx.user_id):
        nick = ctx.nick          # 记当前说话的人时顺手把昵称补上
    ok, msg = memory_mod.append_note(ctx.mem, ctx.group_id, uid, note, nick)
    if not ok:
        return msg
    person = memory_mod.load_person(ctx.mem, ctx.group_id, uid)
    tail = f"\n这条档案现在是：{person.summary(300)}" if person else ""
    return msg + tail


async def _handle_recall(args: dict, ctx: ToolContext) -> str:
    if not ctx.mem.enabled:
        return "记忆功能没有开启。"
    who = str(args.get("who") or "").strip()
    if not who:
        people = memory_mod.list_people(ctx.mem, ctx.group_id)
        group_text = memory_mod.read_group_note(ctx.mem, ctx.group_id)
        parts: list[str] = []
        if group_text:
            parts.append("【群概况】\n" + group_text)
        if people:
            rows = [f"- {p.display}：{p.summary()}" for p in people]
            parts.append(f"【群成员档案】共 {len(people)} 人：\n" + "\n".join(rows))
        return "\n\n".join(parts) if parts else "这个群还没有任何记忆。"
    uid, why = _who_to_uid(who, ctx)
    if uid is None:
        return why
    person = memory_mod.load_person(ctx.mem, ctx.group_id, uid)
    if person is None:
        return f"还没有 {uid} 的档案。"
    head = (
        f"{person.display}｜首次 {person.first_seen or '?'}｜"
        f"最后 {person.last_seen or '?'}｜消息 {person.msgs} 条"
    )
    return f"{head}\n\n{person.body or '（档案是空的）'}"


async def _handle_forget(args: dict, ctx: ToolContext) -> str:
    if not ctx.mem.enabled:
        return "记忆功能没有开启。"
    keyword = str(args.get("keyword") or "").strip()
    if not keyword:
        return "要删哪一条？给我一个关键词。"
    uid, why = _who_to_uid(str(args.get("who") or ""), ctx)
    if uid is None:
        return why
    _removed, msg = memory_mod.forget_note(ctx.mem, ctx.group_id, uid, keyword)
    return msg


REMEMBER = Tool(
    name="remember",
    description=(
        "把关于某个群友的**长期事实**记进档案，跨对话、跨重启都不会忘。"
        "当对方说到自己的偏好、身份、经历、习惯、正在做的事，或者你判断「以后用得上」时调用。"
        "一次记一条，写成一句陈述句（例如「喜欢周杰伦，常点《七里香》」「在杭州读大三」）。"
        "不要记寒暄、玩笑、一次性的安排，也不要记密码、手机号、住址这类敏感信息。"
        "不填 who 就是记当前正在说话的人。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "note": {"type": "string", "description": "要记住的内容，一句话说清，别写废话"},
            "who": {
                "type": "string",
                "description": "记谁：QQ 号或群昵称。不填＝当前提问的人",
            },
            "nick": {"type": "string", "description": "对方怎么称呼自己（可选，仅首次建档时需要）"},
        },
        "required": ["note"],
    },
    handler=_handle_remember,
)

RECALL = Tool(
    name="recall",
    description=(
        "查记忆档案。不填 who 返回全群一览（每个人一行摘要）；"
        "填了 who 返回那个人的完整档案。拿不准某人的情况、或想确认自己记过什么时先查再答。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "who": {"type": "string", "description": "QQ 号或群昵称；不填＝看全群一览"},
        },
    },
    handler=_handle_recall,
)

FORGET = Tool(
    name="forget",
    description=(
        "从档案里删掉记错或过时的内容。keyword 是那条记录里的关键词，"
        "包含它的记录都会被删——所以关键词要具体一点。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "要删的记录里包含的词"},
            "who": {"type": "string", "description": "QQ 号或群昵称；不填＝当前提问的人"},
        },
        "required": ["keyword"],
    },
    handler=_handle_forget,
)


# ---------------------------------------------------------------- 组装

def skills_root(cfg: LLMConfig) -> Path:
    """技能目录：``llm.skills_dir`` 没填就用 ``data/skills``。"""
    raw = (cfg.skills_dir or "").strip()
    if not raw:
        return DATA_DIR / "skills"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (DATA_DIR / path)


def build(
    cfg: LLMConfig,
    group_id: int = 0,
    user_id: int = 0,
    nick: str = "",
    mem: Optional[MemoryConfig] = None,
) -> tuple[list[Tool], ToolContext]:
    """按配置组装这次请求可用的工具，以及共享上下文。

    每个开关都独立：``tools`` 是总闸，``web_tool`` / ``skills`` / ``memory``
    再各自控制，这样关掉技能不用连抓网页一起关。

    ``nick`` 只给 ``remember`` 用：模型记「当前这个人」时要顺手把昵称写进档案，
    否则新档案会是一个只有 QQ 号的空壳，下次认不出来。

    ``mem`` 不传就用默认值（``enabled=True`` + ``data/memory``）——测试和离线
    调用不必为了拿到记忆配置去构造整个 ``AppConfig``。

    返回前还会按 :data:`ALLOWED_TOOL_NAMES` 再过滤一遍——就算以后有人往这里塞了
    一个「安装技能」的工具，它也发不到 QQ 会话里。
    """
    memory_cfg = mem if mem is not None else MemoryConfig()
    ctx = ToolContext(
        cfg=cfg,
        mem=memory_cfg,
        group_id=int(group_id),
        user_id=int(user_id),
        nick=str(nick or ""),
    )
    tools: list[Tool] = []
    if not cfg.tools:
        return tools, ctx
    if cfg.web_tool:
        tools.append(FETCH_URL)
    if cfg.web_tool and cfg.web_search:
        # 搜索依赖「打开网页」的能力（摘要不够时再去 fetch_url 读细节），
        # 所以 web_tool 关掉时搜索也一起不给——否则模型搜到网址却打不开
        tools.append(WEB_SEARCH)
    if memory_cfg.enabled:
        tools.extend([REMEMBER, RECALL, FORGET])
    if cfg.skills:
        ctx.skills = skill_mod.discover(skills_root(cfg))
        if ctx.skills:
            tools.append(LIST_SKILLS)
            tools.append(USE_SKILL)
            tools.append(READ_SKILL_FILE)
            if cfg.skill_scripts:
                tools.append(RUN_SKILL_SCRIPT)
    return [t for t in tools if t.name in ALLOWED_TOOL_NAMES], ctx


def schemas(tools: list[Tool]) -> list[dict]:
    """转成请求体里的 ``tools`` 字段。"""
    return [t.schema() for t in tools]


async def dispatch(
    tools: list[Tool], name: str, args: dict, ctx: ToolContext, max_chars: Optional[int] = None
) -> str:
    """执行一次工具调用，返回回填给模型的文本。**永远不抛异常**。"""
    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        # 模型偶尔会自己编一个 install_skill / write_file 出来。这不只是「没有这个
        # 工具」——要让它记住聊天里装不了技能，所以单独回一句明确的拒绝。
        if looks_like_install(name):
            logger.warning(f"[tool] 群{ctx.group_id} 模型试图调用安装/写入类工具 {name!r} → 已拒绝")
            return INSTALL_DENIED
        return f"没有这个工具：{name}"
    try:
        out = await tool.handler(args or {}, ctx)
    except Exception as exc:  # noqa: BLE001 — 工具挂了也要让对话继续
        out = f"工具执行出错：{exc}"
    text = str(out or "").strip() or "（没有返回内容）"
    limit = DEFAULT_TOOL_CHARS if max_chars is None else int(max_chars)
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip() + f"…（结果过长，已截断，原 {len(text)} 字）"
    return text
