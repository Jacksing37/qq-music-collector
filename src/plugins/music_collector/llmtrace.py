"""LLM 调用记录：一次问答一条，回答「问了什么 / 经过了什么 / 回了什么」。

WebUI「运行日志」页的 **🧠 LLM 调用** 来源就用这个数据。

**为什么不塞进 :mod:`logbuffer`**

- 那边是**文本行**。一次问答要摊成十几行才说得清（提问、历史、工具往返、降级、
  回复、耗时），混在 nonebot / uvicorn 的刷屏里根本找不着；
- prompt 动辄几万字，打成一行会把整个环形缓冲挤掉；
- 而且插件内的 stdlib logger 经 nonebot 的 ``_log_patcher`` 会被**归一成插件名**
  （``music_collector``），拿不到 ``music_collector.llm`` 这一层——想「只看 llm」
  靠 logger 名是过滤不出来的。

所以这里按「一次调用 = 一条记录、字段分开存」：前端能折叠展示，也能只读自己要的字段，
出问题时能直接看出「是没进模型，还是模型回了空，还是中间被降级重试吃掉了一轮」。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Optional

#: 保留多少次调用。一次问答一条，200 条够回溯一两天的排查
DEFAULT_KEEP = 200

#: 单个长字段（提问 / 回复 / 思考）最多存多少字。
#: 不截的话，一个百万字上下文的请求能把整个缓冲撑到几十 MB。
MAX_FIELD = 6000

#: 一次调用最多记多少条「经过」（工具往返 + 降级重试），防止极端情况刷屏
MAX_STEPS = 24


class TraceBuffer:
    """定长环形缓冲，线程安全（和 logbuffer 一个套路，只是结构化的）。"""

    def __init__(self, keep: int = DEFAULT_KEEP) -> None:
        self._lock = threading.Lock()
        self._buf: deque[dict[str, Any]] = deque(maxlen=max(10, int(keep)))
        self._seq = 0

    def start(
        self,
        group_id: int,
        user_id: int,
        speaker: str,
        cfg: Any,
        question: str = "",
        kind: str = "chat",
        background: str = "",
    ) -> dict[str, Any]:
        """开一条记录。返回的 dict 由调用方往里填，最后交给 :meth:`finish`。

        ``kind`` 区分用途：``chat`` 是正常问答，``judge`` 是主动搭话前的判官预检——
        后者也要留痕，否则「它为什么一直不说话」只能靠猜。

        ``background`` 是这次请求附带的群聊背景（它上次说话之后大家在聊什么）。
        单独存一份而不是混进步骤里：日志页要能看见「它当时到底读到了哪几句」，
        只写一句「附参考资料 358 字」根本判断不出它是不是理解错了上下文。
        """
        with self._lock:
            self._seq += 1
            seq = self._seq
        now = time.time()
        return {
            "id": seq,
            "kind": str(kind or "chat"),
            "ts": now,
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "group_id": int(group_id or 0),
            "user_id": int(user_id or 0),
            "speaker": str(speaker or "")[:64],
            "model": str(getattr(cfg, "model", "") or ""),
            "thinking": str(getattr(cfg, "thinking", "") or ""),
            "tools_on": bool(getattr(cfg, "tools", False)),
            "question": str(question or "")[:MAX_FIELD],
            "background": str(background or "")[:MAX_FIELD],
            "reply": "",
            "reasoning": "",
            "system_chars": 0,
            "history_msgs": 0,
            "history_chars": 0,
            "tool_schemas": 0,
            "tool_calls": 0,
            "steps": [],
            "elapsed_ms": 0,
            "ok": False,
            "error": "",
            "usage": {},
        }

    @staticmethod
    def step(entry: Optional[dict], text: str) -> None:
        """记一条「经过了什么」：工具往返、降级重试、记忆注入……"""
        if entry is None:
            return
        steps = entry.setdefault("steps", [])
        if len(steps) < MAX_STEPS:
            steps.append(str(text)[:300])
        elif len(steps) == MAX_STEPS:
            steps.append("…（后续步骤省略）")

    def finish(
        self,
        entry: dict,
        *,
        reply: str = "",
        reasoning: str = "",
        error: str = "",
        elapsed_ms: float = 0.0,
        usage: Optional[dict] = None,
    ) -> None:
        """收口并归档。失败也要归档——「为什么没回」比「回了什么」更需要看。"""
        entry["reply"] = str(reply or "")[:MAX_FIELD]
        entry["reasoning"] = str(reasoning or "")[:MAX_FIELD]
        entry["error"] = str(error or "")[:500]
        entry["elapsed_ms"] = int(max(0.0, float(elapsed_ms)))
        entry["ok"] = bool(reply) and not error
        if usage:
            try:
                entry["usage"] = {
                    k: int(v) for k, v in dict(usage).items()
                    if isinstance(v, (int, float))
                }
            except (TypeError, ValueError):
                entry["usage"] = {}
        with self._lock:
            self._buf.append(entry)

    # ------------------------------------------------------------ 读取

    def snapshot(
        self,
        *,
        limit: int = 50,
        query: str = "",
        group_id: int = 0,
        only_failed: bool = False,
    ) -> list[dict[str, Any]]:
        """取最近的若干条（**新的在前**，和看日志的直觉一致）。"""
        with self._lock:
            items = list(self._buf)
        if group_id:
            items = [i for i in items if i["group_id"] == int(group_id)]
        if only_failed:
            items = [i for i in items if not i["ok"]]
        q = (query or "").strip().lower()
        if q:
            items = [
                i for i in items
                if q in (i.get("question") or "").lower()
                or q in (i.get("reply") or "").lower()
                or q in (i.get("speaker") or "").lower()
                or q in (i.get("error") or "").lower()
                or q in (i.get("background") or "").lower()
                or any(q in s.lower() for s in (i.get("steps") or []))
            ]
        items.reverse()
        if limit and int(limit) > 0:
            items = items[: int(limit)]
        return items

    def clear(self) -> int:
        with self._lock:
            n = len(self._buf)
            self._buf.clear()
        return n

    def stats(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._buf)
        succeeded = sum(1 for i in items if i["ok"])
        return {
            "size": len(items),
            "capacity": self._buf.maxlen,
            "total": self._seq,
            # 刻意不叫 ``ok``：接口响应体顶层已经有一个「请求成功」的 ok，
            # 撞名之后 ``{**stats}`` 会把外面的 ok 覆盖成数字 0，前端判断全错
            "succeeded": succeeded,
            "failed": len(items) - succeeded,
        }


# ---------------------------------------------------------------- 模块级单例

buffer = TraceBuffer()


def start(
    group_id: int,
    user_id: int,
    speaker: str,
    cfg: Any,
    question: str = "",
    kind: str = "chat",
    background: str = "",
) -> dict:
    return buffer.start(
        group_id, user_id, speaker, cfg, question, kind=kind, background=background
    )


def step(entry: Optional[dict], text: str) -> None:
    buffer.step(entry, text)


def finish(entry: dict, **kwargs: Any) -> None:
    buffer.finish(entry, **kwargs)


def snapshot(**kwargs: Any) -> list[dict[str, Any]]:
    return buffer.snapshot(**kwargs)


def clear() -> int:
    return buffer.clear()


def stats() -> dict[str, Any]:
    return buffer.stats()


__all__ = [
    "DEFAULT_KEEP",
    "MAX_FIELD",
    "TraceBuffer",
    "buffer",
    "clear",
    "finish",
    "snapshot",
    "start",
    "stats",
    "step",
]
