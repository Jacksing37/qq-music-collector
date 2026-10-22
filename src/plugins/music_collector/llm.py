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

from .config import LLMConfig

logger = logging.getLogger("music_collector.llm")

#: 单条历史消息的字符上限（防止某条超长消息长期占着上下文窗口）
_HISTORY_CHAR_LIMIT = 600

#: 系统提示词字符上限。人设写得再长也不该吃掉半个上下文窗口。
_SYSTEM_CHAR_LIMIT = 4000

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


def system_text(cfg: LLMConfig) -> str:
    """带长度保护的系统提示词（人设再长也不会挤占上下文）。"""
    prompt = (cfg.system_prompt or "").strip()
    if len(prompt) <= _SYSTEM_CHAR_LIMIT:
        return prompt
    logger.warning(
        f"[llm] system_prompt 过长（{len(prompt)} 字），已截断到 {_SYSTEM_CHAR_LIMIT} 字"
    )
    return prompt[:_SYSTEM_CHAR_LIMIT]


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
    # OpenAI 官方与 Azure：reasoning_effort
    if "openai.com" in host or "openai.azure.com" in host:
        return "reasoning_effort"
    if model.startswith(_REASONING_PREFIXES):
        return "reasoning_effort"
    # DeepSeek / Moonshot 等：靠模型名区分思考，没有额外参数可发
    return "none"


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
    """
    if cfg.thinking in ("auto", "off"):
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


def build_payload(messages: list[dict], cfg: LLMConfig) -> dict:
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
    # 高级兜底放最后：用户写的键可以覆盖上面任何一项（包括思考参数）
    payload.update(parse_extra_body(cfg.extra_body))
    return payload


def payload_preview(cfg: LLMConfig) -> dict:
    """给网页端的状态预览：实际会发出去的参数（不含 Key，messages 只留占位）。"""
    try:
        payload = build_payload([{"role": "user", "content": "…"}], cfg)
    except Exception as exc:  # noqa: BLE001 — 预览失败不该让整页 500
        return {"error": str(exc)}
    payload.pop("messages", None)
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
    content = message.get("content")
    if isinstance(content, list):  # 少数实现返回分段数组
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    text = (content or "").strip()
    if text:
        return text
    # 推理模型把“想”和“说”分开：只拿到思考内容说明额度被思考吃光了
    if message.get("reasoning_content") or message.get("reasoning"):
        raise RuntimeError(
            "模型只产出了思考内容、没有正式回答（多半是最大回复 token 太小，"
            "把「最大回复 token」调大，或把思考档位调低）"
        )
    if (choice.get("finish_reason") or "") == "length":
        raise RuntimeError("回复被 max_tokens 截断（没有任何正文），请调大最大回复 token")
    raise RuntimeError("模型返回了空内容")


class LLMBot:
    """大模型回复器（进程内单例，按群维护上下文与并发锁）。"""

    def __init__(self) -> None:
        #: 群号 -> 最近几轮 [{"role","content"}]
        self._history: dict[int, deque[dict]] = {}
        #: 群号 -> 锁：同一个群的并发 @ 排队，别把上下文搅乱
        self._locks: dict[int, asyncio.Lock] = {}
        #: (群号, 用户) -> 上次真正回复的 monotonic 时刻，用于冷却
        self._last_reply: dict[tuple[int, int], float] = {}
        #: 最近一次失败原因（网页端「测试连接」与日志排查用）
        self.last_error: str = ""

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
        """各群的上下文占用：条数 + 字符数（判断「快爆了没」的直观指标）。"""
        return {
            gid: {"msgs": len(msgs), "chars": sum(len(m.get("content", "")) for m in msgs)}
            for gid, msgs in self._history.items()
            if msgs
        }

    def _history_within(self, group_id: int, max_chars: int) -> list[dict]:
        """取该群上下文，并按**字符预算**从最旧的成对丢弃。

        只按轮数（``history_rounds``）限制是不够的：20 轮 × 单条 600 字 ≈ 一万多字，
        小窗口模型（8k/16k）直接 400。这里再做一层总量裁剪，双保险。
        裁剪只作用于本次请求的副本，不动 deque——把配置调大后旧上下文还能回来。
        """
        msgs = self.history(group_id)
        if max_chars <= 0 or not msgs:
            return msgs
        total = sum(len(m.get("content", "")) for m in msgs)
        while len(msgs) > 2 and total > max_chars:
            total -= len(msgs.pop(0).get("content", ""))
            if msgs:
                total -= len(msgs.pop(0).get("content", ""))
        return msgs

    def clear_history(self, group_id: Optional[int] = None) -> int:
        """清空上下文：传群号只清该群，不传清全部。返回清掉的群数。"""
        if group_id is None:
            n = len(self._history)
            self._history.clear()
            return n
        return 1 if self._history.pop(int(group_id), None) is not None else 0

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

    async def _post_chat(self, messages: list[dict], cfg: LLMConfig) -> str:
        """真正发一次请求并返回纯文本回复。失败抛 ``RuntimeError``（带可读原因）。

        单独拆出来是为了测试能直接 patch 它，不必去劫持 httpx。
        """
        url = normalize_endpoint(cfg.base_url)
        if not url:
            raise RuntimeError("未填写接口地址（base_url）")
        payload = build_payload(messages, cfg)
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
        return extract_reply(data)

    # ------------------------------------------------------------ 对外

    async def chat(
        self,
        group_id: int,
        user_id: int,
        nick: str,
        text: str,
        cfg: LLMConfig,
    ) -> tuple[Optional[str], str]:
        """生成一条回复。

        返回 ``(回复文本, 原因)``：

        - ``("文字", "")``  —— 成功
        - ``(None, "cooldown")`` —— 该用户还在冷却期，调用方**什么都别发**
        - ``(None, "empty")`` —— 消息里没有可回答的文字
        - ``(None, "error: …")`` —— 调用失败，调用方可决定是否退回自我介绍
        """
        question = (text or "").strip()
        if not question:
            return None, "empty"
        if cfg.max_input_chars > 0 and len(question) > cfg.max_input_chars:
            question = question[: cfg.max_input_chars].rstrip() + "…"

        # 昵称进 prompt 有助于模型称呼提问者；顺带做一次长度保护
        speaker = (nick or str(user_id)).strip()[:_HISTORY_CHAR_LIMIT]

        left = self.cooldown_left(group_id, user_id, cfg.cooldown)
        if left > 0:
            # 群里表现为「完全没反应」，必须留痕：否则分不清是限流还是压根没触发
            logger.info(
                f"[llm] 群{group_id} {speaker}({user_id}) 冷却中"
                f"（还剩 {left:.0f}s / 共 {cfg.cooldown}s）→ 静默跳过"
            )
            return None, "cooldown"

        async with self._lock(group_id):
            system = system_text(cfg)
            messages: list[dict] = []
            if system:
                messages.append({"role": "system", "content": system})
            hist: list[dict] = []
            if cfg.history_rounds > 0:
                hist = self._history_within(group_id, cfg.history_chars)
                messages.extend(hist)
            question_msg = {"role": "user", "content": f"{speaker}：{question}"}
            messages.append(question_msg)

            try:
                reply = await self._post_chat(messages, cfg)
            except Exception as exc:  # noqa: BLE001 — 任何失败都只记原因
                # 上下文真的超窗时，别把「机器人坏了」暴露给群友：丢掉历史再试一次。
                # 只对「带了历史」的情况重试——没有历史还超窗，说明单条提问/系统提示
                # 就超了，重试也没用。
                if not hist or not is_context_overflow(str(exc)):
                    self.last_error = str(exc)
                    logger.warning(f"[llm] 群{group_id} 生成失败: {exc}")
                    return None, f"error: {exc}"
                dropped = len(hist)
                dropped_chars = sum(len(m.get("content", "")) for m in hist)
                self.clear_history(group_id)
                logger.warning(
                    f"[llm] 群{group_id} 上下文超窗（丢弃 {dropped} 条 / {dropped_chars} 字）→ 不带历史重试"
                )
                retry: list[dict] = []
                if system:
                    retry.append({"role": "system", "content": system})
                retry.append(question_msg)
                try:
                    reply = await self._post_chat(retry, cfg)
                except Exception as exc2:  # noqa: BLE001
                    self.last_error = str(exc2)
                    logger.warning(f"[llm] 群{group_id} 丢弃上下文后仍失败: {exc2}")
                    return None, f"error: {exc2}"
                logger.info(f"[llm] 群{group_id} 丢弃上下文后重试成功")

            self.last_error = ""
            if cfg.max_reply_chars > 0 and len(reply) > cfg.max_reply_chars:
                reply = reply[: cfg.max_reply_chars].rstrip() + "…"

            # 只有成功才写上下文：失败时留着半个来回，下一轮会重复问同一句
            if cfg.history_rounds > 0:
                hist = self._history.setdefault(group_id, deque())
                hist.append({"role": "user", "content": messages[-1]["content"][:_HISTORY_CHAR_LIMIT]})
                hist.append({"role": "assistant", "content": reply[:_HISTORY_CHAR_LIMIT]})
                keep = max(2, cfg.history_rounds * 2)
                while len(hist) > keep:
                    hist.popleft()

        self._last_reply[(int(group_id), int(user_id))] = time.monotonic()
        logger.info(
            f"[llm] 群{group_id} {speaker}({user_id}) 提问 {len(question)} 字 → "
            f"回复 {len(reply)} 字（model={cfg.model}）"
        )
        return reply, ""

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
            reply = await self._post_chat(messages, probe)
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
