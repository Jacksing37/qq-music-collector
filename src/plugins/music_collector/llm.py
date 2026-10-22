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


def enabled_for(cfg: LLMConfig, group_id: int) -> bool:
    """该群是否该用大模型回复：开关 + 必填项 + 群白名单（空=所有群）。"""
    if not cfg.enabled:
        return False
    if not (cfg.base_url.strip() and cfg.api_key.strip() and cfg.model.strip()):
        return False
    gid = int(group_id)
    return not cfg.groups or gid in cfg.groups


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
        payload = {
            "model": cfg.model.strip(),
            "messages": messages,
            "temperature": float(cfg.temperature),
            "max_tokens": int(cfg.max_tokens),
            "stream": False,
        }
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
            # 有些兼容层把错误放在 200 的 body 里
            err = data.get("error") or data.get("message") or data.get("detail")
            raise RuntimeError(f"没有 choices: {str(err)[:200] or data}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # 少数实现返回分段数组
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        text = (content or "").strip()
        if not text:
            raise RuntimeError("模型返回了空内容")
        return text

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

        if self.cooldown_left(group_id, user_id, cfg.cooldown) > 0:
            return None, "cooldown"

        # 昵称进 prompt 有助于模型称呼提问者；顺带做一次长度保护
        speaker = (nick or str(user_id)).strip()[:_HISTORY_CHAR_LIMIT]

        async with self._lock(group_id):
            messages: list[dict] = []
            if cfg.system_prompt.strip():
                messages.append({"role": "system", "content": cfg.system_prompt.strip()})
            if cfg.history_rounds > 0:
                messages.extend(self.history(group_id))
            messages.append({"role": "user", "content": f"{speaker}：{question}"})

            try:
                reply = await self._post_chat(messages, cfg)
            except Exception as exc:  # noqa: BLE001 — 任何失败都只记原因
                self.last_error = str(exc)
                logger.warning(f"[llm] 群{group_id} 生成失败: {exc}")
                return None, f"error: {exc}"

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
        if cfg.system_prompt.strip():
            messages.append({"role": "system", "content": cfg.system_prompt.strip()})
        messages.append({"role": "user", "content": prompt})
        started = time.monotonic()
        # 测试时临时收窄 max_tokens，省点钱
        probe = cfg.model_copy(update={"max_tokens": min(int(cfg.max_tokens), 128), "temperature": 0.3})
        try:
            reply = await self._post_chat(messages, probe)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return False, str(exc)
        self.last_error = ""
        elapsed = (time.monotonic() - started) * 1000
        return True, f"{reply}（{elapsed:.0f} ms，共 {len(reply)} 字）"


llm_bot = LLMBot()
