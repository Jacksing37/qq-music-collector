"""被 @ 时用大模型 API 生成回复（OpenAI 兼容的 ``/chat/completions``）。

设计取舍：

- **只做一件事**：把「谁说了什么」拼成 messages、发请求、拿回复。开关、群白名单、
  模型、温度、上下文轮数、冷却、截断长度全部来自 ``llm`` 配置段，网页端改完立刻
  生效（每次调用都现取配置，不缓存客户端对象）。
- **上下文只放内存**：按群维护最近 N 轮对话（``deque``），进程重启即清空。存库会
  让「一句闲聊」变成永久数据，收益不值。
- **失败不抛给上层**：返回 ``(回复, 原因)``。调用方据此决定「退回自我介绍」还是
  「什么都不发」，避免一次网络抖动在群里刷出报错。
- **OpenAI 兼容**：DeepSeek / Moonshot / 通义 / 智谱 / Ollama / vLLM / one-api
  都走同一套协议，只要 ``base_url`` + ``api_key`` + ``model`` 填对即可。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional
from urllib.parse import urlsplit

import httpx

from . import memory as memory_mod
from . import tools as tools_mod
from .config import LLMConfig, MemoryConfig

logger = logging.getLogger("music_collector.llm")

#: 单条历史消息的字符上限**兜底值**（真正生效的是 ``llm.history_item_chars``）。
#: 以前写死 600 会把人家长回复腰斩，模型下一轮就看不懂自己在说什么了。
_HISTORY_CHAR_LIMIT = 600

#: 系统提示词字符上限。人设写得再长也不该吃掉半个上下文窗口。
#: 公开常量（不带下划线）是有意的：网页端「机器人」页要按同一个数字显示
#: 「已用 x / 4000 字」，两边各写一个魔法数字迟早会对不上。
SYSTEM_CHAR_LIMIT = 4000

#: 服务端报「超窗」时常见的措辞。命中就说明上下文真的爆炸了，
#: 与其把错误抛给用户，不如丢掉历史重试一次。
_CONTEXT_OVERFLOW_MARKS = (
    "context length",
    "context_length",
    "maximum context",
    "context window",
    "too long",
    "too many tokens",
    "reduce the length",
    "prompt is too long",
    "input length",
    "超过最大",
    "上下文长度",
)

#: 思考档位 -> 默认 token 预算。``low`` 取 1024 是为了兼容 Anthropic 风格的
#: 最小预算限制（<1024 会被拒）；``high`` 不给太大，免得一次回复烧掉太多钱。
_THINK_BUDGET = {"low": 1024, "medium": 4096, "high": 12000}

#: 中转层不认 ``tools`` 时的典型报错。命中就去掉工具再试一次——
#: 不做这个降级，一个只转发 messages 的中转会让整个机器人哑掉。
_TOOLS_UNSUPPORTED_MARKS = (
    "tool_choice",
    "tools is not supported",
    "tool is not supported",
    "does not support tool",
    "unsupported parameter: tools",
    "unknown parameter: tools",
    "unrecognized request argument",
    "function calling",
    "不支持工具",
    "tools 字段",
)

#: DeepSeek 的思考强度只认 ``low`` / ``high`` / ``max`` 三档（见官方「思考模式」文档的
#: 映射表：minimal→low、low→low、medium→high、high→high、xhigh→high、max→max、ultra→max）。
#: 所以本地档位要压到这三个值上——直接把 ``medium`` 发过去是没定义的写法。
_DEEPSEEK_EFFORT = {"low": "low", "medium": "high", "high": "high", "max": "max"}

#: 这些模型对 temperature / top_p / penalty **要么拒绝要么忽略**，发出去只会换来
#: 400 或者「参数不生效的困惑」，所以按模型名直接省掉。
_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")
_REASONING_MARKS = ("reasoner", "-r1", "qwq", "thinking")


def normalize_endpoint(base_url: str) -> str:
    """把用户填的 ``base_url`` 补成完整的 chat/completions 地址。

    兼容这几种常见写法（都能用）：

    - ``https://api.openai.com/v1``        -> ``.../v1/chat/completions``
    - ``https://api.openai.com/v1/``       -> 同上（尾部斜杠自动去掉）
    - ``https://api.openai.com``           -> ``.../v1/chat/completions``
    - ``https://x.com/v1/chat/completions`` -> 原样使用

    只有「连路径都没填」（``scheme://host`` 或 ``/``）时才补 ``/v1``；填了别的前缀
    （如 one-api 的 ``/v1``、自建的 ``/api/v3``）就只补 ``/chat/completions``，
    不去动用户的路径。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/chat/completions"):
        return url
    parsed = urlsplit(url)
    if parsed.path in ("", "/"):
        return url + "/v1/chat/completions"
    return url + "/chat/completions"


def is_context_overflow(message: str) -> bool:
    """错误信息是不是「上下文超窗」。

    各家措辞不同（OpenAI 的 ``maximum context length``、通义的 ``input length``、
    中转层直接把原始报错透传），所以按关键词匹配，宁可多认几种也别漏。
    """
    text = (message or "").lower()
    return any(mark in text for mark in _CONTEXT_OVERFLOW_MARKS)


#: 上游「不认图片」的典型措辞。带图请求撞上这些就退化成纯文本重试一次——
#: 这样以后换回不支持视觉的模型也不用改配置。
#: 后几条是「图拉不下来」：引用旧消息时 QQ 图片链接可能已过期签名，中转取不到图会报错。
_VISION_UNSUPPORTED_MARKS = (
    "image_url",
    "image url",
    "image input",
    "vision",
    "multimodal",
    "multi-modal",
    "unsupported image",
    "not support image",
    "does not support image",
    "invalid content",
    "content must be a string",
    "only text",
    "不支持图片",
    "不支持图像",
    "仅支持文本",
    "download",
    "fetch image",
    "invalid image",
    "无法下载",
    "图片下载",
    "图片链接",
)


def is_vision_unsupported(message: str) -> bool:
    """错误信息是不是「这个模型/接口不接受图片」。"""
    text = (message or "").lower()
    return any(mark in text for mark in _VISION_UNSUPPORTED_MARKS)


def is_tools_unsupported(message: str) -> bool:
    """错误信息是不是「这层接口不认 tools / 不支持函数调用」。

    命中就去掉工具再试一次。不做这个降级，一个只转发 ``messages`` 的中转
    会让整个机器人哑掉——而「不能用工具」本来只是少个功能。
    """
    text = (message or "").lower()
    return any(mark in text for mark in _TOOLS_UNSUPPORTED_MARKS)


def build_question_content(text: str, images: list[str]) -> str | list[dict]:
    """拼 ``user`` 消息的 content：没图给字符串，有图给 OpenAI vision 数组。

    无图时坚持用**纯字符串**是有意的：绝大多数兼容接口只认字符串，只有真要带图
    才切成数组，日常对话的行为一点不变。
    """
    if not images:
        return text
    parts: list[dict] = [{"type": "text", "text": text}]
    for url in images:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def content_chars(content) -> int:
    """一条消息内容的字符数；多模态数组只算文字部分（图片本身不占字符预算）。"""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(str(part.get("text") or "")) for part in content if isinstance(part, dict)
        )
    return 0


def message_chars(message: dict) -> int:
    """一条消息实际占用的字符数：正文 + 思考内容。

    思考内容虽然用户看不见，但**确实会发给上游、确实占 token**。裁剪历史时
    只算正文会让「开了思考 + 12 轮」的上下文比预期大一倍。
    """
    total = content_chars(message.get("content"))
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    return total + len(str(reasoning))


def as_message(raw) -> dict:
    """把 ``_post_chat`` 的返回值归一化成 message dict。

    真实实现返回 dict；测试桩历史上返回**字符串**，这里兼容一下，
    免得每个用桩的用例都要跟着改。
    """
    if isinstance(raw, dict):
        return raw
    text = "" if raw is None else str(raw)
    return {"role": "assistant", "content": text}


def message_text(message: dict) -> str:
    """从一条 assistant message 里取正文；取不到就抛带原因的错误。

    注意顺序：**先看 tool_calls**。模型这一轮只想调工具、不说话的
    ``content`` 就是空的，这是正常情况，不能当成「空回复」报错。
    """
    content = message.get("content")
    if isinstance(content, list):  # 少数实现返回分段数组
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    text = (content or "").strip()
    if text:
        return text
    if message.get("tool_calls"):
        return ""
    # 推理模型把“想”和“说”分开：只拿到思考内容说明额度被思考吃光了
    if message.get("reasoning_content") or message.get("reasoning"):
        raise RuntimeError(
            "模型只产出了思考内容、没有正式回答（多半是最大回复 token 太小，"
            "把「最大回复 token」调大，或把思考档位调低）"
        )
    raise RuntimeError("模型返回了空内容")


def parse_tool_arguments(raw) -> dict:
    """``tool_calls[].function.arguments`` 是**字符串**，各家中转的写法还不一样。

    解析失败不抛异常：把原文当成 ``{"raw": ...}`` 塞回去，让工具自己决定怎么处理
    ——因为「参数格式错」不该让整条回复失败。
    """
    if isinstance(raw, dict):
        return raw
    text = (raw or "").strip() if isinstance(raw, str) else ""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return {"_raw": text[:500]}
    return data if isinstance(data, dict) else {"_raw": text[:500]}


#: 「参考资料」（网页正文 / 转发记录）的总字数上限。抓取时各自已经有上限，
#: 这里是最后一道闸：一次请求别因为外链内容把上下文预算吃光。
CONTEXT_CHAR_LIMIT = 4000


def with_context(content, context: str):
    """把参考资料拼到提问前面（保持「资料在前、问题在后」的阅读顺序）。

    ``context`` 只作用于**当次请求**：调用方写上下文历史时仍只存原始提问，
    否则几小时前的网页正文会一直挂在历史里，越聊越贵。
    """
    note = (context or "").strip()
    if not note:
        return content
    if len(note) > CONTEXT_CHAR_LIMIT:
        note = note[:CONTEXT_CHAR_LIMIT].rstrip() + "…"
    if isinstance(content, str):
        return f"{note}\n\n{content}"
    parts = list(content)
    if parts and isinstance(parts[0], dict) and parts[0].get("type") == "text":
        parts[0] = {"type": "text", "text": f"{note}\n\n{parts[0].get('text') or ''}"}
    else:
        parts.insert(0, {"type": "text", "text": note})
    return parts


def without_images(messages: list[dict], text: str) -> list[dict]:
    """把最后一条（user）消息换回纯文本，用于「上游不认图」的降级重试。"""
    head = list(messages[:-1])
    head.append({"role": "user", "content": text})
    return head


def system_text(cfg: LLMConfig, rules: str = "", memory_text: str = "") -> str:
    """带长度保护的系统提示词（人设再长也不会挤占上下文）。

    拼接顺序是 **人设 → 记忆 → 硬规则**，两个理由：

    - ``rules`` 是代码写死的硬规则（见 :data:`tools.TOOL_RULES`），必须排在最后、
      且**拼在截断之后**——用户把人设写满 4000 字也挤不掉「聊天里装不了技能」；
    - ``memory_text`` 是群成员档案（见 :mod:`memory`），它只会随人数增长、
      不该挤占人设位置，也不该盖住能力边界。
    """
    prompt = (cfg.system_prompt or "").strip()
    if len(prompt) > SYSTEM_CHAR_LIMIT:
        logger.warning(
            f"[llm] system_prompt 过长（{len(prompt)} 字），已截断到 {SYSTEM_CHAR_LIMIT} 字"
        )
        prompt = prompt[:SYSTEM_CHAR_LIMIT]
    return prompt + (memory_text or "") + (rules or "")


def is_reasoning_model(model: str) -> bool:
    """是不是「自带思考、不吃采样参数」的模型（o1/o3/gpt-5/reasoner/QwQ…）。

    这类模型传 ``temperature`` 往往直接 400，所以发请求时自动省掉采样参数；
    用户在网页上填的温度对它们不生效，界面上会一并标出来。
    """
    name = (model or "").strip().lower()
    if not name:
        return False
    return name.startswith(_REASONING_PREFIXES) or any(m in name for m in _REASONING_MARKS)


def guess_thinking_style(cfg: LLMConfig) -> str:
    """按 ``base_url`` 的域名和模型名猜这家服务用哪种思考参数写法。

    猜不准也没关系：网页端可以把 ``thinking_style`` 手动固定成某一种。
    猜不出（多数情况是模型本身自带思考、不需要额外参数）就返回 ``none``。
    """
    host = urlsplit(cfg.base_url or "").netloc.lower()
    model = (cfg.model or "").strip().lower()

    # 通义 / 百炼：enable_thinking + thinking_budget
    if "dashscope" in host or "aliyuncs" in host or model.startswith(("qwen", "qwq")):
        return "enable_thinking"
    # Claude 直连或中转：thinking: {type, budget_tokens}
    if "anthropic" in host or "claude" in model:
        return "thinking_object"
    # 智谱 GLM / 硅基流动 / OpenRouter：也用 thinking 对象
    if any(h in host for h in ("bigmodel", "zhipu", "siliconflow", "openrouter")):
        return "thinking_object"
    # DeepSeek：开关用 thinking 对象、强度用 reasoning_effort（两件事分开表达，见官方文档）
    if "deepseek" in host or "deepseek" in model:
        return "deepseek"
    # OpenAI 官方与 Azure：reasoning_effort
    if "openai.com" in host or "openai.azure.com" in host:
        return "reasoning_effort"
    if model.startswith(_REASONING_PREFIXES):
        return "reasoning_effort"
    # 认不出的中转站：靠模型名区分思考，没有额外参数可发
    return "none"


def deepseek_effort(mode: str) -> str:
    """把本地档位映射成 DeepSeek 认的强度值（``medium`` 在那边等价于 ``high``）。"""
    return _DEEPSEEK_EFFORT.get((mode or "").strip(), "high")


def resolve_thinking_style(cfg: LLMConfig) -> str:
    """最终生效的思考参数写法：``auto`` 时走推断，``none`` 时明确不发。"""
    style = (cfg.thinking_style or "auto").strip()
    if style == "auto":
        return guess_thinking_style(cfg)
    return style


def thinking_budget(cfg: LLMConfig) -> int:
    """实际会发出去的思考预算（token）。

    两条约束：① ``0`` = 按档位取默认；② 预算不能吃掉全部 ``max_tokens``，
    否则模型思考完就没配额输出正文（表现为「返回空内容」），所以上限压到
    ``max_tokens`` 减 256。想真正高强度思考，得同时把最大回复 token 调大。

    DeepSeek 不接收 token 预算（只认 low/high/max 三档），这里返回 0，
    免得网页端显示一个「根本不存在的预算」。
    """
    if cfg.thinking in ("auto", "off"):
        return 0
    if resolve_thinking_style(cfg) == "deepseek":
        return 0
    budget = int(cfg.thinking_budget) or _THINK_BUDGET.get(cfg.thinking, 0)
    if budget <= 0:
        return 0
    ceiling = max(256, int(cfg.max_tokens) - 256)
    return min(budget, ceiling)


def thinking_params(cfg: LLMConfig) -> dict:
    """按档位与写法拼出思考相关参数；返回空 dict 表示这次不加任何思考参数。"""
    mode = (cfg.thinking or "auto").strip()
    style = resolve_thinking_style(cfg)
    if mode == "auto" or style == "none":
        return {}
    if style == "reasoning_effort":
        # OpenAI 只有 minimal/low/medium/high，没有「关」，所以 off 时不发参数
        return {} if mode == "off" else {"reasoning_effort": mode}
    if style == "enable_thinking":
        if mode == "off":
            return {"enable_thinking": False}
        params: dict = {"enable_thinking": True}
        budget = thinking_budget(cfg)
        if budget:
            params["thinking_budget"] = budget
        return params
    if style == "deepseek":
        # 官方默认就是「开 + high」，所以「自动」什么都不发（前面已返回）。
        # 关：只有 thinking 对象能表达关闭，reasoning_effort 没有「关」这个值。
        if mode == "off":
            return {"thinking": {"type": "disabled"}}
        # 开：显式打开 + 给强度。**不带 budget_tokens**，DeepSeek 没有这个参数，
        # 只有 Anthropic 系才用（预算在这边由档位表达）。
        return {"thinking": {"type": "enabled"}, "reasoning_effort": deepseek_effort(mode)}
    # thinking_object（Claude / GLM / 部分中转）
    if mode == "off":
        return {"thinking": {"type": "disabled"}}
    return {"thinking": {"type": "enabled", "budget_tokens": thinking_budget(cfg)}}


def parse_extra_body(raw: str) -> dict:
    """把 ``extra_body`` 那段文本解析成 dict；非法 JSON 直接报可读错误。"""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001 — 报给用户看，不用堆栈
        raise RuntimeError(f"extra_body 不是合法 JSON：{exc}") from None
    if not isinstance(data, dict):
        raise RuntimeError('extra_body 必须是一个 JSON 对象，例如 {"top_k": 5}')
    return data


def build_payload(
    messages: list[dict], cfg: LLMConfig, tools: Optional[list[dict]] = None
) -> dict:
    """拼出真正发给模型的请求体（抽出来是为了网页端能预览、测试能断言）。"""
    payload: dict = {
        "model": (cfg.model or "").strip(),
        "messages": messages,
        "stream": False,
        "max_tokens": int(cfg.max_tokens),
    }
    # 推理模型不认采样参数，发了可能直接 400
    if not is_reasoning_model(cfg.model):
        payload["temperature"] = float(cfg.temperature)
        payload["top_p"] = float(cfg.top_p)
        if cfg.presence_penalty:
            payload["presence_penalty"] = float(cfg.presence_penalty)
        if cfg.frequency_penalty:
            payload["frequency_penalty"] = float(cfg.frequency_penalty)
    stops = [s for s in (cfg.stop or []) if str(s).strip()]
    if stops:
        payload["stop"] = stops
    if int(cfg.seed) >= 0:
        payload["seed"] = int(cfg.seed)
    payload.update(thinking_params(cfg))
    if tools:
        # tool_choice=auto：让模型自己决定调不调。写死 "required" 会逼它每句都调工具
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    # 高级兜底放最后：用户写的键可以覆盖上面任何一项（包括思考参数与工具）
    payload.update(parse_extra_body(cfg.extra_body))
    return payload


def payload_preview(cfg: LLMConfig) -> dict:
    """给网页端的状态预览：实际会发出去的参数（不含 Key，messages 只留占位）。

    工具只列名字不列完整 schema——预览区是给人看的，塞二十行 JSON Schema 就没法读了。
    """
    tool_names: list[str] = []
    tool_list: list = []
    try:
        if cfg.tools:
            tool_list, _ = tools_mod.build(cfg)
            tool_names = [t.name for t in tool_list]
        payload = build_payload(
            [{"role": "user", "content": "…"}], cfg, tools_mod.schemas(tool_list) if tool_names else None
        )
    except Exception as exc:  # noqa: BLE001 — 预览失败不该让整页 500
        return {"error": str(exc)}
    payload.pop("messages", None)
    if "tools" in payload:
        payload["tools"] = tool_names
    return payload


def disabled_reason(cfg: LLMConfig, group_id: int) -> str:
    """这个群为什么不走大模型；返回空串表示可以走。

    抽成一个函数是为了让日志能说清**原因**：群里「@ 了没反应」时，只知道
    ``enabled_for`` 返回 False 是没法排查的——未启用、没填全、群不在白名单，
    三种情况处理方式完全不同。
    """
    if not cfg.enabled:
        return "大模型功能未启用"
    if not (cfg.base_url.strip() and cfg.api_key.strip() and cfg.model.strip()):
        return "接口地址 / API Key / 模型名 没填全"
    gid = int(group_id)
    if cfg.groups and gid not in cfg.groups:
        return f"群 {gid} 不在生效列表（当前 {len(cfg.groups)} 个群）"
    return ""


def enabled_for(cfg: LLMConfig, group_id: int) -> bool:
    """该群是否该用大模型回复：开关 + 必填项 + 群白名单（空=所有群）。"""
    return not disabled_reason(cfg, group_id)


def extract_reply(data: dict) -> str:
    """从响应体里取正文。取不到就抛带原因的错误（抽出来方便单测）。"""
    choices = data.get("choices") or []
    if not choices:
        # 有些兼容层把错误放在 200 的 body 里
        err = data.get("error") or data.get("message") or data.get("detail")
        raise RuntimeError(f"没有 choices: {str(err)[:200] or data}")
    choice = choices[0] or {}
    message = choice.get("message") or {}
    try:
        text = message_text(message)
    except RuntimeError:
        # 被 max_tokens 截断时的提示要和「模型啥也没说」区分开，否则排查方向全错
        if (choice.get("finish_reason") or "") == "length":
            raise RuntimeError(
                "回复被 max_tokens 截断（没有任何正文），请调大最大回复 token"
            ) from None
        raise
    if not text:
        raise RuntimeError("模型返回了空内容")
    return text


class LLMBot:
    """大模型回复器（进程内单例，按群维护上下文与并发锁）。"""

    def __init__(self) -> None:
        #: 群号 -> 最近几轮 [{"role","content"}]（内存视图，进程内热数据）
        self._history: dict[int, deque[dict]] = {}
        #: 群号 -> 锁：同一个群的并发 @ 排队，别把上下文搅乱
        self._locks: dict[int, asyncio.Lock] = {}
        #: (群号, 用户) -> 上次真正回复的 monotonic 时刻，用于冷却
        self._last_reply: dict[tuple[int, int], float] = {}
        #: 最近一次失败原因（网页端「测试连接」与日志排查用）
        self.last_error: str = ""
        #: 持久化后端（有 ``load_chat`` / ``append_chat`` 的 store）。注入后
        #: 上下文**跨重启保留**——这就是「持久化上下文」的全部机制
        self._store = None
        #: 已经从库里预热过的群，避免每次请求都读库
        self._history_loaded: set[int] = set()
        #: 已点了「清空上下文」但还没来得及清库的群
        self._pending_clear: set[int] = set()
        #: 一次性清空全部（用户点「清空所有群」时置位，下次任意群 sync 时执行）
        self._clear_all: bool = False

    # ------------------------------------------------------------ 持久化

    def set_store(self, store) -> None:
        """注入持久化后端。**不注入就退化成纯内存**（测试、离线场景）。"""
        self._store = store

    async def _sync_history(self, group_id: int, cfg: LLMConfig) -> None:
        """把该群的历史从库里预热到内存；顺带执行「用户要求的清空」。

        只在**每群第一次**真正发请求时读一次库，之后都走内存——否则每条消息
        都要扫一遍几万行，白白拖慢回复。

        注意顺序：清空标记要在「已加载」判断之前处理，否则用户点了清空之后
        新消息进来会看到旧上下文复活。
        """
        if self._store is None:
            return
        if self._clear_all:
            self._clear_all = False
            self._pending_clear.clear()
            self._history_loaded.clear()
            self._history.clear()
            for gid in await self._store.chat_groups():
                await self._store.clear_chat(gid, kind="chat")
            return
        if group_id in self._pending_clear:
            self._pending_clear.discard(group_id)
            self._history_loaded.discard(group_id)
            await self._store.clear_chat(group_id, kind="chat")
            return
        if group_id in self._history_loaded:
            return
        self._history_loaded.add(group_id)
        try:
            since = 0.0
            if cfg.history_days > 0:
                since = time.time() - float(cfg.history_days) * 86400
            # 预热条数 = 轮数×2，再按**字数预算**反推一道上限，两者取小。
            # 以前这里写死 4000 条（2000 轮），按每条约 50~100 字算只有 20~40 万字，
            # 于是 history_chars 配到 160 万也吃不满 1M 窗口——上限卡在读库这一步。
            # 现在按「平均每条 40 字」保守估算：宁可多读几行（后面 _history_within
            # 还会按字数精裁），也不能让预算在预热阶段就被砍掉。
            # 另留 50000 条硬顶防呆（约 5M 字符、十几 MB 内存），避免配置写错吃爆内存。
            rounds_limit = int(cfg.history_rounds) * 2
            chars_limit = (
                max(2, int(cfg.history_chars) // 40) if cfg.history_chars > 0 else rounds_limit
            )
            limit = max(2, min(rounds_limit, chars_limit, 50000))
            rows = await self._store.load_chat(
                group_id, kind="chat", limit=limit, since=since
            )
        except Exception as exc:  # noqa: BLE001 — 读库失败就用空历史
            logger.warning(f"[llm] 群{group_id} 载入历史失败（按空历史继续）: {exc}")
            return
        if not rows:
            return
        box = self._history.setdefault(group_id, deque())
        box.clear()
        for row in rows:
            box.append(row)
        logger.info(
            f"[llm] 群{group_id} 从库里恢复 {len(rows)} 条历史"
            f"（{sum(message_chars(m) for m in rows)} 字）"
        )

    # ------------------------------------------------------------ 上下文

    def _lock(self, group_id: int) -> asyncio.Lock:
        lock = self._locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[group_id] = lock
        return lock

    def history(self, group_id: int) -> list[dict]:
        """取该群当前上下文（只读拷贝，供网页端展示/测试）。"""
        return [dict(m) for m in self._history.get(group_id, ())]

    def history_sizes(self) -> dict[int, int]:
        """各群当前缓存了多少条上下文消息（网页端状态显示用）。"""
        return {gid: len(msgs) for gid, msgs in self._history.items() if msgs}

    def history_stats(self) -> dict[int, dict[str, int]]:
        """各群的上下文占用：条数 + 字符数（判断「快爆了没」的直观指标）。

        字符数把 ``reasoning_content`` 也算进去——它一样要走请求、一样占 token。
        """
        return {
            gid: {
                "msgs": len(msgs),
                "chars": sum(message_chars(m) for m in msgs),
            }
            for gid, msgs in self._history.items()
            if msgs
        }

    def _history_within(self, group_id: int, max_chars: int) -> list[dict]:
        """取该群上下文，并按**字符预算**从最旧的成对丢弃。

        只按轮数（``history_rounds``）限制是不够的：20 轮 × 单条 600 字 ≈ 一万多字，
        小窗口模型（8k/16k）直接 400。这里再做一层总量裁剪，双保险。
        裁剪只作用于本次请求的副本，不动 deque——把配置调大后旧上下文还能回来。

        历史里只有 ``user`` / ``assistant`` 成对的消息（工具往返不落库，见 ``chat``），
        所以按两条一减不会把「tool_call 和它的结果」拆散。
        """
        msgs = self.history(group_id)
        if max_chars <= 0 or not msgs:
            return msgs
        total = sum(message_chars(m) for m in msgs)
        while len(msgs) > 2 and total > max_chars:
            total -= message_chars(msgs.pop(0))
            if msgs:
                total -= message_chars(msgs.pop(0))
        return msgs

    def clear_history(self, group_id: Optional[int] = None) -> int:
        """清空上下文（内存 + 库）：传群号只清该群，不传清全部。返回清掉的群数。

        库里那份**不能在这里删**：这是个同步方法，网页端点一下就直接返回了。
        这里只记个标记，等下一次真正发请求时由 :meth:`_sync_history` 落库删除。
        """
        if group_id is None:
            n = len(self._history)
            self._history.clear()
            self._history_loaded.clear()
            self._pending_clear.clear()
            self._clear_all = True
            return n
        gid = int(group_id)
        had = self._history.pop(gid, None) is not None
        self._history_loaded.discard(gid)
        self._pending_clear.add(gid)
        return 1 if had or self._store is not None else 0

    def cooldown_left(self, group_id: int, user_id: int, cooldown: int) -> float:
        """距离上次回复还要等几秒；<=0 表示可以回了。"""
        if cooldown <= 0:
            return 0.0
        last = self._last_reply.get((int(group_id), int(user_id)))
        if last is None:
            return 0.0
        left = cooldown - (time.monotonic() - last)
        return left if left > 0 else 0.0

    # ------------------------------------------------------------ HTTP

    async def _post_chat(
        self, messages: list[dict], cfg: LLMConfig, tools: Optional[list[dict]] = None
    ) -> dict:
        """真正发一次请求，返回**整条** assistant message。

        返回整条而不只是正文，是因为工具调用要看 ``tool_calls``、下一轮历史又要
        回传 ``reasoning_content``——只取正文这些信息就全丢了。

        失败抛 ``RuntimeError``（带可读原因）。单独拆出来是为了测试能直接
        patch 它，不必去劫持 httpx；桩返回纯字符串也认（见 :func:`as_message`）。
        """
        url = normalize_endpoint(cfg.base_url)
        if not url:
            raise RuntimeError("未填写接口地址（base_url）")
        payload = build_payload(messages, cfg, tools)
        headers = {
            "Authorization": f"Bearer {cfg.api_key.strip()}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=float(cfg.timeout)) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise RuntimeError(f"请求超时（{cfg.timeout:g}s）") from None
        except httpx.HTTPError as exc:
            raise RuntimeError(f"网络错误: {exc}") from None

        if resp.status_code >= 400:
            # 各家错误体格式不一，尽量把有用的一句捞出来
            detail = (resp.text or "").strip().replace("\n", " ")[:200]
            raise RuntimeError(f"HTTP {resp.status_code}: {detail}")
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError("返回的不是合法 JSON") from None
        choices = data.get("choices") or []
        if not choices:
            err = data.get("error") or data.get("message") or data.get("detail")
            raise RuntimeError(f"没有 choices: {str(err)[:200] or data}")
        message = (choices[0] or {}).get("message") or {}
        if not isinstance(message, dict):
            raise RuntimeError("返回的 message 结构不认识")
        return message

    # ------------------------------------------------------------ 对外

    async def _run_turns(
        self,
        messages: list[dict],
        cfg: LLMConfig,
        tool_list: list,
        tool_ctx,
        tool_schemas: list[dict],
        group_id: int,
    ) -> tuple[str, str, int]:
        """跑「模型 ⇄ 工具」往返，返回 ``(最终正文, 思考内容, 工具调用次数)``。

        两个容易踩的点：

        - **最后一轮不带 tools**。到了 ``tool_rounds`` 上限还在要工具，就只能拿到
          空 content；这里强制最后取一次正文，比直接报「空回复」靠谱。
        - **回填 assistant 消息时必须带 ``reasoning_content``**。DeepSeek 明确说，
          请求带 ``tools`` 时历史里的 ``reasoning_content`` 要完整回传，否则 400。
          这也是为什么历史里要存它（见 ``chat`` 的写库部分）。
        """
        working = [dict(m) for m in messages]
        steps = max(0, int(cfg.tool_rounds))
        used = 0
        for step in range(steps + 1):
            last = step >= steps
            raw = await self._post_chat(
                working, cfg, None if (last or not tool_schemas) else tool_schemas
            )
            msg = as_message(raw)
            calls = msg.get("tool_calls") or []
            if last or not calls:
                return message_text(msg), str(msg.get("reasoning_content") or ""), used

            assistant: dict = {"role": "assistant", "content": msg.get("content") or ""}
            reasoning = msg.get("reasoning_content")
            if reasoning:
                assistant["reasoning_content"] = reasoning
            assistant["tool_calls"] = calls
            working.append(assistant)

            for call in calls:
                fn = (call or {}).get("function") or {}
                name = str(fn.get("name") or "")
                args = parse_tool_arguments(fn.get("arguments"))
                used += 1
                preview = json.dumps(args, ensure_ascii=False)[:120]
                logger.info(f"[llm] 群{group_id} 调用工具 {name} 参数 {preview}")
                result = await tools_mod.dispatch(
                    tool_list, name, args, tool_ctx, cfg.tool_chars
                )
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": str((call or {}).get("id") or f"call_{used}"),
                        "content": result,
                    }
                )
                logger.info(f"[llm] 群{group_id} 工具 {name} 返回 {len(result)} 字")
        return "", "", used  # 循环必然在上面 return，这里只为让类型完整

    async def chat(
        self,
        group_id: int,
        user_id: int,
        nick: str,
        text: str,
        cfg: LLMConfig,
        images: Optional[list[str]] = None,
        quote: str = "",
        context: str = "",
        mem: Optional[MemoryConfig] = None,
    ) -> tuple[Optional[str], str]:
        """生成一条回复。

        ``images`` 是图片链接（``llm.vision`` 关着时会被忽略，只在文字里留
        ``[图片]`` 标记）；``quote`` 是被引用消息压成的一句前缀；
        ``context`` 是**当次**附带的参考资料（网页正文 / 转发记录），
        只拼进这次请求，不写进上下文历史；``mem`` 是记忆配置（不传就用默认值，
        指向 ``data/memory``）。

        返回 ``(回复文本, 原因)``：

        - ``("文字", "")``  —— 成功
        - ``(None, "cooldown")`` —— 该用户还在冷却期，调用方**什么都别发**
        - ``(None, "empty")`` —— 消息里既没有文字也没有图
        - ``(None, "error: …")`` —— 调用失败，调用方可决定是否退回自我介绍
        """
        question = (text or "").strip()
        # 只有真的开了视觉、且模型那边支持，才把图片发出去
        pics = []
        if cfg.vision:
            pics = [u for u in (images or []) if u][: max(0, cfg.max_images)]
        if not question and not pics:
            return None, "empty"
        if cfg.max_input_chars > 0 and len(question) > cfg.max_input_chars:
            question = question[: cfg.max_input_chars].rstrip() + "…"

        # 昵称进 prompt 有助于模型称呼提问者；顺带做一次长度保护
        item_limit = max(200, int(cfg.history_item_chars or _HISTORY_CHAR_LIMIT))
        speaker = (nick or str(user_id)).strip()[:item_limit]

        # 纯文本版本：写历史、以及「上游不认图」降级时都用它，绝不带 URL。
        # 带上 QQ 号是有意的：模型要调 remember 记「这个人」时，必须有稳定标识，
        # 光靠昵称会撞名（群里两个「小明」太常见了）。
        body = f"{speaker}({user_id})：{question}"
        if quote:
            body = f"{quote}\n{body}"

        left = self.cooldown_left(group_id, user_id, cfg.cooldown)
        if left > 0:
            # 群里表现为「完全没反应」，必须留痕：否则分不清是限流还是压根没触发
            logger.info(
                f"[llm] 群{group_id} {speaker}({user_id}) 冷却中"
                f"（还剩 {left:.0f}s / 共 {cfg.cooldown}s）→ 静默跳过"
            )
            return None, "cooldown"

        async with self._lock(group_id):
            memory_cfg = mem if mem is not None else MemoryConfig()
            # 每群第一次发请求时把历史从库里捞回来（重启后也能接上话）
            await self._sync_history(group_id, cfg)
            # 工具一开就是「agent 模式」：能力边界（不许装技能、不许写文件）由代码
            # 追加在系统提示词末尾，用户改人设也删不掉。群成员档案插在人设之后，
            # 硬规则之前——档案再长也盖不掉能力边界。
            system = system_text(
                cfg,
                tools_mod.TOOL_RULES if cfg.tools else "",
                memory_mod.render_for_prompt(memory_cfg, group_id),
            )
            base: list[dict] = []
            if system:
                base.append({"role": "system", "content": system})
            hist: list[dict] = []
            if cfg.history_rounds > 0:
                hist = self._history_within(group_id, cfg.history_chars)
            current = [
                *base,
                *hist,
                {"role": "user", "content": with_context(build_question_content(body, pics), context)},
            ]
            # 降级重试时用的纯文本版本：参考资料的顺序要和上面一致，否则「去掉图片
            # 重试」会把网页正文一起丢掉，模型突然从「知道内容」变成「不知道内容」。
            plain = with_context(body, context)

            # 工具按配置组装。tool_rounds=0 等于「不许调工具」，连 schema 都不发，
            # 省得模型调了却拿不到结果。
            tool_list: list = []
            tool_ctx = None
            tool_schemas: list[dict] = []
            if cfg.tools and cfg.tool_rounds > 0:
                tool_list, tool_ctx = tools_mod.build(
                    cfg, group_id, user_id, speaker, memory_cfg
                )
                tool_schemas = tools_mod.schemas(tool_list)

            allow_drop_images = bool(pics)
            allow_drop_history = bool(hist)
            allow_drop_tools = bool(tool_schemas)
            reply: Optional[str] = None
            reasoning = ""
            used_tools = 0
            while True:
                try:
                    reply, reasoning, used_tools = await self._run_turns(
                        current, cfg, tool_list, tool_ctx, tool_schemas, group_id
                    )
                    break
                except Exception as exc:  # noqa: BLE001 — 任何失败都只记原因
                    err = str(exc)
                    # ① 接口不认 tools（中转只转发 messages / 模型不支持函数调用）：
                    #    去掉工具重试。少了工具只是少个功能，不该让机器人哑掉。
                    if allow_drop_tools and is_tools_unsupported(err):
                        allow_drop_tools = False
                        tool_schemas = []
                        logger.warning(
                            f"[llm] 群{group_id} 接口不接受 tools（{err[:80]}）→ 去掉工具重试"
                        )
                        continue
                    # ② 上游不认图（换了纯文本模型 / 中转层不支持多模态）：去掉图片再试
                    if allow_drop_images and is_vision_unsupported(err):
                        allow_drop_images = False
                        logger.warning(
                            f"[llm] 群{group_id} 上游不接受图片（{err[:80]}）→ 去掉图片重试"
                        )
                        current = without_images(current, plain)
                        continue
                    # ③ 上下文超窗：丢掉历史再试。没带历史还超窗说明单条提问就超了，
                    #    重试也没用，直接报错。
                    if allow_drop_history and is_context_overflow(err):
                        allow_drop_history = False
                        dropped = len(hist)
                        dropped_chars = sum(message_chars(m) for m in hist)
                        self.clear_history(group_id)
                        logger.warning(
                            f"[llm] 群{group_id} 上下文超窗（丢弃 {dropped} 条 / "
                            f"{dropped_chars} 字）→ 不带历史重试"
                        )
                        current = [*base, {"role": "user", "content": current[-1]["content"]}]
                        continue
                    self.last_error = err
                    logger.warning(f"[llm] 群{group_id} 生成失败: {err}")
                    return None, f"error: {err}"

            self.last_error = ""
            if not reply:
                # 工具轮数用尽还在要工具、或者模型干脆不说话：别把空串当成功，
                # 否则群里会出现「@了但什么都没发」而日志显示一切正常。
                err = "模型没有给出正文（工具调用可能达到上限）"
                self.last_error = err
                logger.warning(f"[llm] 群{group_id} {err}（已用工具 {used_tools} 次）")
                return None, f"error: {err}"
            if cfg.max_reply_chars > 0 and len(reply) > cfg.max_reply_chars:
                reply = reply[: cfg.max_reply_chars].rstrip() + "…"

            # 只有成功才写上下文：失败时留着半个来回，下一轮会重复问同一句。
            # 图片一律只记 [图片] 标记——URL 几小时就过期，塞进历史既没用又占预算。
            # **思考内容要留着**：带 tools 的请求若不回传历史 reasoning_content，
            # DeepSeek 会直接 400；不带 tools 时传了也会被忽略，所以存着没有副作用。
            # 工具往返（tool_calls / tool 结果）**不进历史**：它对下一轮对话没有价值，
            # 存进去反而会让「按两条一裁」的历史裁剪拆散 tool_call 与它的结果。
            if cfg.history_rounds > 0:
                hist_box = self._history.setdefault(group_id, deque())
                hist_box.append({"role": "user", "content": body[:item_limit]})
                entry: dict = {"role": "assistant", "content": reply[:item_limit]}
                if reasoning:
                    entry["reasoning_content"] = reasoning[:item_limit]
                hist_box.append(entry)
                keep = max(2, cfg.history_rounds * 2)
                while len(hist_box) > keep:
                    hist_box.popleft()

            # 落库**不受 history_rounds 限制**：即使关了上下文，这些对话仍然是
            # 记忆整理任务的素材。写库失败只记一条日志，绝不拖累「已经把回复发出去了」
            # 这个结果。
            if self._store is not None:
                try:
                    await self._store.append_chat(
                        group_id, user_id, speaker, "user", body[:item_limit], kind="chat"
                    )
                    await self._store.append_chat(
                        group_id, 0, "", "assistant", reply[:item_limit],
                        reasoning[:item_limit], kind="chat",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[llm] 群{group_id} 对话落库失败: {exc}")

        self._last_reply[(int(group_id), int(user_id))] = time.monotonic()
        pics_note = f"，图 {len(pics)} 张" if pics else ""
        ctx_note = f"，资料 {len(context)} 字" if context else ""
        tool_note = f"，工具 {used_tools} 次" if used_tools else ""
        logger.info(
            f"[llm] 群{group_id} {speaker}({user_id}) 提问 {len(question)} 字{pics_note}{ctx_note}"
            f" → 回复 {len(reply)} 字{tool_note}（model={cfg.model}）"
        )
        return reply, ""

    async def complete(
        self,
        prompt: str,
        cfg: LLMConfig,
        *,
        system: str = "",
        model: str = "",
        max_tokens: int = 0,
        temperature: float = 0.3,
    ) -> tuple[str, str]:
        """一次性纯文本补全：不带历史、不带工具、不占冷却、不写上下文。

        专给后台任务用（目前是「整理群成员记忆」）。这类任务要的是模型的阅读
        理解能力，不是聊天人格——所以 ``system`` 由调用方给任务说明，**不拼人设
        也不拼记忆**，免得让它总结别人的时候先把自己的人设演一遍。

        返回 ``(正文, 错误原因)``；失败时正文是空串，不抛异常。
        """
        text = (prompt or "").strip()
        if not text:
            return "", "empty"
        messages: list[dict] = []
        if (system or "").strip():
            messages.append({"role": "system", "content": system.strip()})
        messages.append({"role": "user", "content": text})
        probe = cfg.model_copy(
            update={
                "model": (model or cfg.model),
                "max_tokens": int(max_tokens or cfg.max_tokens),
                "temperature": float(temperature),
                # 整理任务是「读一遍、归纳一下」，不需要长思考：既省钱也省时间
                "thinking": "off",
            }
        )
        try:
            msg = as_message(await self._post_chat(messages, probe))
            return message_text(msg), ""
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[llm] 一次性调用失败（model={probe.model}）: {exc}")
            return "", str(exc)

    async def test(self, cfg: LLMConfig, prompt: str = "用一句话介绍你自己") -> tuple[bool, str]:
        """网页端「测试连接」：发一条最小请求，返回是否成功 + 可读结果。"""
        if not (cfg.base_url.strip() and cfg.api_key.strip() and cfg.model.strip()):
            return False, "接口地址 / API Key / 模型名 还没填全"
        messages = []
        system = system_text(cfg)
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        started = time.monotonic()
        # 测试时收窄 max_tokens 省钱，但**不能太小**：开了思考的模型会把额度全用在
        # 思考上，正文为空 → 明明接口是好的却报错。给 1024 留出回答空间。
        probe = cfg.model_copy(
            update={
                "max_tokens": max(1024, min(int(cfg.max_tokens), 2048)),
                "temperature": 0.3,
            }
        )
        try:
            # 测试连接**不带工具**：这里只想验证「接口通不通、模型回不回话」，
            # 带上 tools 反而会被中转的兼容性问题干扰，排查方向就糊了。
            reply = message_text(as_message(await self._post_chat(messages, probe)))
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            # 网页上闪一下的结果会被关掉，日志里留一份才能事后回溯
            logger.warning(f"[llm] 测试连接失败（model={cfg.model}）: {exc}")
            return False, str(exc)
        self.last_error = ""
        elapsed = (time.monotonic() - started) * 1000
        logger.info(
            f"[llm] 测试连接成功（model={cfg.model}，{elapsed:.0f} ms，回复 {len(reply)} 字）"
        )
        return True, f"{reply}（{elapsed:.0f} ms，共 {len(reply)} 字）"


llm_bot = LLMBot()
