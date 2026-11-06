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

**跨重启留存（2026-11-06）**

``finish()`` 归档时顺手往 ``data/llm_trace.jsonl`` 追加一行 JSON（4MB 轮转，留 1 份备份），
``install()`` 在启动时把这个文件的尾部读回缓冲——否则网页「LLM 调用」页重启就空了，
和日志页一样只能看到本次启动的记录。**只有 ``install()`` 调过才落盘**：没调过（单元测试里
直接 new 一个 ``TraceBuffer``）就纯内存，绝不往真实 ``data/`` 里写东西。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from .config import DATA_DIR

#: 保留多少次调用。一次问答一条，200 条够回溯一两天的排查
DEFAULT_KEEP = 200

#: 单个长字段（提问 / 回复 / 思考）最多存多少字。
#: 不截的话，一个百万字上下文的请求能把整个缓冲撑到几十 MB。
MAX_FIELD = 6000

#: 一次调用最多记多少条「经过」（工具往返 + 降级重试），防止极端情况刷屏
MAX_STEPS = 24

#: 落盘文件名（与 collector.db 同在 data/ 下）
FILE_NAME = "llm_trace.jsonl"
#: 落盘文件多大轮转：一条记录几十 KB，4MB 大致能装几百条
FILE_MAX_BYTES = 4 * 1024 * 1024
#: 回灌时最多读文件尾部多少字节
_FILE_TAIL_BYTES = 8 * 1024 * 1024


class TraceBuffer:
    """定长环形缓冲，线程安全（和 logbuffer 一个套路，只是结构化的）。"""

    def __init__(self, keep: int = DEFAULT_KEEP) -> None:
        self._lock = threading.Lock()
        self._buf: deque[dict[str, Any]] = deque(maxlen=max(10, int(keep)))
        self._seq = 0
        #: 从落盘文件回灌进来的条数（重启后仍能看到的那部分）
        self._restored = 0

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
        # 归档之后再落盘：写文件失败绝不能影响回话（_append 自己吞掉异常）
        _append(entry)

    # ------------------------------------------------------------ 回灌与容量

    def restore(self, items: list[dict[str, Any]]) -> int:
        """把历史记录塞到缓冲区**最前面**，返回实际塞进去的条数。

        只塞「还装得下」的那部分：``deque.extendleft`` 满员时从右端丢，
        会先丢掉刚启动后的新记录——那是最该留的。``_seq`` 要顺延到历史里的最大 id，
        否则新记录会和历史记录撞 id（前端拿 id 当 key）。
        """
        if not items:
            return 0
        with self._lock:
            room = (self._buf.maxlen or 0) - len(self._buf)
            keep = items[-room:] if room > 0 else []
            for item in reversed(keep):
                self._buf.appendleft(item)
            self._restored += len(keep)
            try:
                self._seq = max(
                    [self._seq] + [int(i.get("id") or 0) for i in self._buf]
                )
            except (TypeError, ValueError):
                pass
        return len(keep)

    @property
    def capacity(self) -> int:
        return int(self._buf.maxlen or 0)

    def set_keep(self, keep: int) -> None:
        n = max(10, int(keep))
        with self._lock:
            if self._buf.maxlen != n:
                self._buf = deque(self._buf, maxlen=n)

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
            items = [i for i in items if i.get("group_id") == int(group_id)]
        if only_failed:
            items = [i for i in items if not i.get("ok")]
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
            self._restored = 0
        return n

    def stats(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._buf)
            restored = self._restored
        succeeded = sum(1 for i in items if i.get("ok"))
        return {
            "size": len(items),
            "capacity": self._buf.maxlen,
            "total": self._seq,
            "restored": restored,
            "file": str(_file_path) if _file_path else None,
            # 刻意不叫 ``ok``：接口响应体顶层已经有一个「请求成功」的 ok，
            # 撞名之后 ``{**stats}`` 会把外面的 ok 覆盖成数字 0，前端判断全错
            "succeeded": succeeded,
            "failed": len(items) - succeeded,
        }


# ---------------------------------------------------------------- 模块级单例

buffer = TraceBuffer()

#: 落盘路径；``None`` = 不落盘（``install()`` 没调过，单元测试就走到这里）
_file_path: Optional[Path] = None
#: 是否已经回灌过历史（同一个路径只读一次，重复 install 不会读第二遍）
_loaded = False


def trace_path() -> Optional[Path]:
    """当前落盘文件路径（没装落盘就是 ``None``）。"""
    return _file_path


def _resolve(raw: Any) -> Optional[Path]:
    text = str(raw or "").strip()
    p = Path(text).expanduser() if text else (Path(DATA_DIR) / FILE_NAME)
    return p if p.is_absolute() else (Path(DATA_DIR) / p)


def _append(entry: dict[str, Any]) -> None:
    """把一条记录追加到落盘文件（一行 JSON）。任何异常都吞掉：归档失败不能影响回话。"""
    if _file_path is None:
        return
    try:
        _file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if _file_path.stat().st_size > FILE_MAX_BYTES:
                # 只留一份备份：这份日志是用来排查最近几次的，不是审计留档
                _file_path.replace(_file_path.with_name(_file_path.name + ".1"))
        except OSError:
            pass
        with _file_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 - 落盘永远不许把异常抛给回话链路
        return


#: 回灌时给缺失字段补的默认值。历史文件可能是旧版本写的、也可能被人手改过，
#: 少一个键就让整个统计/过滤炸掉不值得（真踩过：手写的测试行没 ``ok`` → KeyError）。
_FIELD_DEFAULTS: dict[str, Any] = {
    "id": 0,
    "kind": "chat",
    "ts": 0.0,
    "time": "",
    "group_id": 0,
    "user_id": 0,
    "speaker": "",
    "model": "",
    "thinking": "",
    "tools_on": False,
    "question": "",
    "background": "",
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


def _read_history(path: Path, limit: int) -> list[dict[str, Any]]:
    """读落盘文件尾部，返回按时间升序的记录（坏行直接跳过）。

    跨天的记录会把 ``time`` 改成「MM-DD HH:MM:SS」——只留时分秒的话，
    第二天打开页面看到「21:30:05」根本分不清是哪天。
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _FILE_TAIL_BYTES:
                fh.seek(size - _FILE_TAIL_BYTES)
                fh.readline()            # 丢掉可能被切一半的首行
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    today = time.strftime("%Y-%m-%d")
    items: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict) or "ts" not in item:
            continue
        try:
            when = float(item["ts"])
            if time.strftime("%Y-%m-%d", time.localtime(when)) != today:
                item["time"] = time.strftime("%m-%d %H:%M:%S", time.localtime(when))
        except (TypeError, ValueError, OSError):
            pass
        # 补缺失字段（拷贝一份，别让两条记录共用同一个 list / dict）
        for key, default in _FIELD_DEFAULTS.items():
            if key not in item:
                if isinstance(default, list):
                    item[key] = list(default)
                elif isinstance(default, dict):
                    item[key] = dict(default)
                else:
                    item[key] = default
        items.append(item)

    return items[-limit:] if limit > 0 else items


def install(path: Any = None, keep: int = DEFAULT_KEEP) -> dict[str, Any]:
    """启动时调用：定下落盘路径，并把历史记录回灌进缓冲（幂等）。

    ``path`` 留空就用 ``data/llm_trace.jsonl``。**不调用它就不落盘**——
    单元测试直接 new ``TraceBuffer`` 是纯内存的，不会写真实 ``data/``。
    """
    global _file_path, _loaded
    p = _resolve(path)
    if p != _file_path:
        _file_path = p
        _loaded = False
    if keep:
        buffer.set_keep(keep)
    if not _loaded and _file_path is not None:
        buffer.restore(_read_history(_file_path, buffer.capacity))
        _loaded = True
    return buffer.stats()


def reset() -> None:
    """测试用：摘掉落盘与「已回灌」标记（缓冲本身另外 clear）。"""
    global _file_path, _loaded
    _file_path = None
    _loaded = False


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
    "FILE_NAME",
    "FILE_MAX_BYTES",
    "MAX_FIELD",
    "TraceBuffer",
    "buffer",
    "clear",
    "finish",
    "install",
    "reset",
    "snapshot",
    "start",
    "stats",
    "step",
    "trace_path",
]
