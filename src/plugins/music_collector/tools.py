"""模型可调用的工具注册表。

工具 = 名字 + 描述 + JSON Schema 参数 + 一个 ``async handler``。描述写得越清楚，
模型越不会乱调——这里的说明都是**给模型看的**，不是给用户看的。

当前内置四类：

- ``fetch_url``：抓网页正文（后端在 :mod:`webtool`，抽取用 trafilatura）；
- ``list_skills`` / ``use_skill``：技能清单与正文（渐进披露，见 :mod:`skills`）；
- ``run_skill_script``：执行技能自带脚本（需显式开启，且只能跑技能目录内的白名单脚本）。

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

from . import skills as skill_mod
from . import webtool
from .config import DATA_DIR, LLMConfig

#: 单次工具结果回填给模型的最大字符数（兜底，具体值由 ``llm.tool_chars`` 控制）
DEFAULT_TOOL_CHARS = 2000

#: 允许出现在 QQ 会话里的工具名（白名单，不是黑名单）。
#: **新增工具必须同时加进这里**，否则 ``build()`` 会把它丢掉——这是为了「少一个
#: 安装类工具不小心上线」而不是为了省事。
ALLOWED_TOOL_NAMES = frozenset({"fetch_url", "list_skills", "use_skill", "run_skill_script"})

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
    "\n\n【能力边界】你可以抓网页（fetch_url）、读已安装的技能、"
    "在管理员允许时运行技能自带脚本；"
    "但你**没有**安装/卸载/修改技能的权限，也没有写文件、执行任意命令的权限。"
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
    group_id: int = 0
    user_id: int = 0
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

async def _handle_fetch_url(args: dict, ctx: ToolContext) -> str:
    url = str(args.get("url") or "").strip()
    if not webtool.url_is_fetchable(url):
        return "错误：需要一个完整的 http/https 网址。"
    page = await webtool.fetch_page(
        url,
        timeout=ctx.cfg.fetch_timeout,
        max_chars=ctx.cfg.fetch_chars,
    )
    if not page:
        return (
            f"没能取到 {url} 的内容：可能打不开、需要登录，或者是 JS 渲染出来的页面。"
            "不要编造里面的内容；可以告诉用户你打不开这个链接。"
        )
    title = page.get("title") or "（无标题）"
    return f"标题：{title}\n网址：{page['url']}\n正文：\n{page['text']}"


FETCH_URL = Tool(
    name="fetch_url",
    description=(
        "抓取一个网页，返回标题和正文（已去掉导航、广告和脚本）。"
        "当用户发来链接、而你需要知道里面到底写了什么时调用；"
        "只对 http/https 网址有效，抓不到会明确告诉你失败。"
        "如果只是闲聊、或已经知道答案，不要调用它。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "完整网址，含 http:// 或 https://"},
        },
        "required": ["url"],
    },
    handler=_handle_fetch_url,
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


# ---------------------------------------------------------------- 组装

def skills_root(cfg: LLMConfig) -> Path:
    """技能目录：``llm.skills_dir`` 没填就用 ``data/skills``。"""
    raw = (cfg.skills_dir or "").strip()
    if not raw:
        return DATA_DIR / "skills"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (DATA_DIR / path)


def build(cfg: LLMConfig, group_id: int = 0, user_id: int = 0) -> tuple[list[Tool], ToolContext]:
    """按配置组装这次请求可用的工具，以及共享上下文。

    每个开关都独立：``tools`` 是总闸，``web_tool`` / ``skills`` 再各自控制，
    这样关掉技能不用连抓网页一起关。

    返回前还会按 :data:`ALLOWED_TOOL_NAMES` 再过滤一遍——就算以后有人往这里塞了
    一个「安装技能」的工具，它也发不到 QQ 会话里。
    """
    ctx = ToolContext(cfg=cfg, group_id=int(group_id), user_id=int(user_id))
    tools: list[Tool] = []
    if not cfg.tools:
        return tools, ctx
    if cfg.web_tool:
        tools.append(FETCH_URL)
    if cfg.skills:
        ctx.skills = skill_mod.discover(skills_root(cfg))
        if ctx.skills:
            tools.append(LIST_SKILLS)
            tools.append(USE_SKILL)
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
