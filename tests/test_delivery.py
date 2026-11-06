"""送达回执才落库：发送失败的问答**不许**进 ``chat_log``。

对应设计文档 `docs/聊天体验拼装设计.md` §5「P5 · 5.1 送达回执才落库」。

为什么值得单独测：旧实现是**发送之前**写库。NapCat 掉线 / 被风控时模型会"记得"
自己说过一句话，接着等一个不会来的回应，对话从此错位——而且没有任何日志暴露这件事。
所以这里的断言一律围绕「store 到底被写了几行、写了什么」。

不连网络：``chat()`` 唯一的请求出口是 ``_post_chat``，打桩即可（项目里既有的做法，
见 test_llm_bot.py）；发送层用一个只实现 ``send_group_msg`` 的假 bot，
``fail_at`` 里的序号会抛异常 = NapCat 掉线。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))
# 与 test_llm_bot.py 同样的理由：这些用例会走 service.config，必须把 data/ 挪到临时目录，
# 免得读到、或写出真实的 data/config.yaml。
if not os.environ.get("MUSIC_DATA_DIR"):
    os.environ["MUSIC_DATA_DIR"] = tempfile.mkdtemp(prefix="music-test-data-")

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message  # noqa: E402
from nonebot.adapters.onebot.v11.event import Sender  # noqa: E402

import music_collector as mc  # noqa: E402
from music_collector import timeguard as tg  # noqa: E402
from music_collector.bot_utils import safe_send_group  # noqa: E402
from music_collector.config import LLMConfig  # noqa: E402
from music_collector.llm import LLMBot, llm_bot  # noqa: E402


# ---------------------------------------------------------------- 替身


class FakeStore:
    """只统计「写了几行、每行什么内容」的假 store。

    落库时机是这一轮唯一的观测点，所以不必接真 SQLite：``append_chat`` 记下来就够了。
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def append_chat(
        self,
        group_id,
        user_id=0,
        nick="",
        role="user",
        content="",
        reasoning="",
        kind="chat",
        created_at=None,
    ) -> int:
        self.rows.append({
            "group_id": int(group_id),
            "user_id": int(user_id),
            "nick": nick,
            "role": role,
            "content": content,
            "reasoning": reasoning,
            "kind": kind,
        })
        return len(self.rows)

    # chat() 会拿这几个方法预热历史；空历史 = 这个群从没说过话
    async def load_chat(self, *a, **k):
        return []

    async def chat_groups(self):
        return []

    async def clear_chat(self, *a, **k):
        return 0


class FakeBot:
    """只实现 ``send_group_msg``：成功回一个回执，``fail_at`` 里的序号抛异常。"""

    def __init__(self, fail_at: set[int] | None = None) -> None:
        self.sent: list[str] = []
        self.fail_at = set(fail_at or ())
        self.attempts = 0

    async def send_group_msg(self, group_id: int, message):
        # 按「第几次调用」定位失败，而不是按成功条数——失败那条不推进 sent，
        # 否则一次失败会让后面每一段都跟着算同一条
        self.attempts += 1
        idx = self.attempts
        if idx in self.fail_at:
            raise RuntimeError("NapCat 连接已断开")
        self.sent.append(str(message))
        return {"message_id": 9000 + idx, "message_id_str": str(9000 + idx)}


def make_event(text: str, *, user_id: int = 42, group_id: int = 7700) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=0,
        self_id=1,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=200,
        message=Message(text),
        original_message=Message(text),
        raw_message=text,
        font=0,
        sender=Sender(user_id=user_id, nickname="张三", card="张三"),
        group_id=group_id,
        anonymous=None,
    )


def base_cfg(**over) -> LLMConfig:
    """一份「只回答、不整活」的配置：不冷却、不带工具/历史/视觉、默认不拆条。"""
    fields = dict(
        enabled=True,
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="test-model",
        system_prompt="你是助手",
        cooldown=0,
        tools=False,
        tool_rounds=0,
        history_rounds=0,
        max_input_chars=0,
        max_reply_chars=0,
        at_sender=False,
        reply_split=False,
        # 拆条时默认有 0.5-1.5s 的停顿，测试里关掉（否则每条用例多等几秒）
        reply_split_interval="0",
    )
    fields.update(over)
    return LLMConfig(**fields)


async def answer_once(*, gid: int, reply: str, fail_at=None, cfg=None):
    """跑一遍**真实**的 ``_llm_answer``：请求层与背景查询打桩，发送层是真的。

    返回 ``(store, bot, ok, why)``。打桩的两处：
    - ``llm_bot._post_chat``：chat() 内部唯一发请求的地方（不联网）；
    - ``_llm_background``：要查库拿群聊背景，与本用例无关，省掉。
    """
    cfg = cfg or base_cfg()
    store = FakeStore()
    bot = FakeBot(fail_at=fail_at)
    old_store = llm_bot._store
    old_post = llm_bot._post_chat
    old_bg = mc._llm_background

    async def fake_post(messages, cf, tools=None):
        return reply

    async def no_background(event, llm_cfg, text=""):
        return ""

    llm_bot.set_store(store)
    llm_bot._post_chat = fake_post  # type: ignore[assignment]
    mc._llm_background = no_background  # type: ignore[assignment]
    try:
        ok, why = await mc._llm_answer(bot, make_event("在吗", group_id=gid), cfg, "在吗")
    finally:
        llm_bot._post_chat = old_post  # type: ignore[assignment]
        mc._llm_background = old_bg  # type: ignore[assignment]
        llm_bot.set_store(old_store)
    return store, bot, ok, why


# ---------------------------------------------------------------- 1. 发送层的返回值


def test_safe_send_group_returns_receipt() -> None:
    """拿到回执 → 返回带回执的 dict（调用方靠它确认「真发出去了」）。"""
    rec = asyncio.run(safe_send_group(FakeBot(), 1, Message("你好")))
    assert isinstance(rec, dict), repr(rec)
    assert rec.get("message_id") == 9001, repr(rec)
    assert bool(rec) is True, "回执必须为真——否则旧的 `if not safe_send_group(...)` 会误判成失败"


def test_safe_send_group_none_when_failed() -> None:
    """发送抛异常（NapCat 掉线）→ None。"""
    assert asyncio.run(safe_send_group(FakeBot(fail_at={1}), 1, Message("你好"))) is None


def test_safe_send_group_truthy_when_no_message_id() -> None:
    """上游不回 message_id（旧 NapCat / 中转）也算成功，不能退化成假值。"""

    class Silent:
        async def send_group_msg(self, group_id: int, message):
            return None

    rec = asyncio.run(safe_send_group(Silent(), 1, Message("你好")))
    assert rec is not None and bool(rec), repr(rec)


# ---------------------------------------------------------------- 2. 落库端的形状


def test_persist_turn_writes_two_rows() -> None:
    """写入的两行与改造前逐字一致：user 行带提问者，assistant 行 user_id=0 / nick=""。"""
    store = FakeStore()
    old = llm_bot._store
    llm_bot.set_store(store)
    try:
        asyncio.run(llm_bot.persist_turn(
            7710,
            user_id=42,
            speaker="张三",
            body="张三(42)：在吗",
            reply="在的，你说",
            reasoning="想了想",
        ))
    finally:
        llm_bot.set_store(old)

    assert len(store.rows) == 2, store.rows
    user, asst = store.rows
    assert user["group_id"] == 7710 and user["kind"] == "chat"
    assert user["role"] == "user" and user["user_id"] == 42 and user["nick"] == "张三"
    assert user["content"] == "张三(42)：在吗", user["content"]
    assert asst["role"] == "assistant" and asst["user_id"] == 0 and asst["nick"] == ""
    assert asst["content"] == "在的，你说", asst["content"]
    assert asst["reasoning"] == "想了想", asst["reasoning"]


def test_persist_turn_never_raises() -> None:
    """写库失败只记日志：这时回复已经发出去了，不能让异常冒到调用方。"""

    class BrokenStore:
        async def append_chat(self, *a, **k):
            raise OSError("disk full")

    old = llm_bot._store
    llm_bot.set_store(BrokenStore())
    try:
        asyncio.run(llm_bot.persist_turn(7711, user_id=42, speaker="张三",
                                        body="张三(42)：在吗", reply="在的"))
    finally:
        llm_bot.set_store(old)


# ---------------------------------------------------------------- 3. 端到端：回执 → 落库


async def test_no_delivery_no_memory() -> None:
    """§5.1 的核心：发送失败 → store 一次都不写。"""
    store, bot, ok, why = await answer_once(gid=7720, reply="在的，你说", fail_at={1})
    assert bot.sent == [], bot.sent
    assert store.rows == [], store.rows
    # ok=True 说明这一轮确实生成出了正文（失败发生在发送，不是生成）——
    # 旧实现恰恰是在这个时候就把两行写进去了
    assert ok is True, f"{ok} {why}"


def chat_rows(store):
    """只要 ``kind='chat'`` 的问答行；``kind='say'`` 是「自己发的气泡」旁听记录。"""
    return [r for r in store.rows if r["kind"] == "chat"]


def said_rows(store):
    return [r for r in store.rows if r["kind"] == "say"]


async def test_delivered_writes_two_rows() -> None:
    """送达 → 落两行问答，外加一行「自己发的气泡」（背景要用）。"""
    store, bot, ok, why = await answer_once(gid=7721, reply="在的，你说")
    assert ok and len(bot.sent) == 1, f"{ok} {bot.sent}"
    assert [r["role"] for r in chat_rows(store)] == ["user", "assistant"], store.rows
    user, asst = chat_rows(store)
    assert user["content"] == "张三(42)：在吗", user["content"]
    assert user["user_id"] == 42 and user["nick"] == "张三"
    assert asst["content"] == "在的，你说", asst["content"]
    assert asst["user_id"] == 0 and asst["nick"] == ""
    # 自己发的气泡也记一行（kind='say'，role=assistant，user_id=0）
    said = said_rows(store)
    assert len(said) == 1, store.rows
    assert said[0]["role"] == "assistant" and said[0]["user_id"] == 0, said[0]
    assert said[0]["content"] == "在的，你说", said[0]


async def test_multi_part_all_delivered_writes_whole_reply() -> None:
    """拆成多段全部送达 → 落库；assistant 行存的是**实际发出去的样子**（各段用换行拼回）。"""
    cfg = base_cfg(reply_split=True, reply_split_max=5)
    store, bot, ok, why = await answer_once(
        gid=7722, reply="第一句\n\n第二句\n\n第三句", cfg=cfg
    )
    assert len(bot.sent) == 3, bot.sent
    assert len(chat_rows(store)) == 2, store.rows
    # 原来是原样存「第一句\n\n第二句\n\n第三句」；2026-11-06 起存拼回去的实际气泡，
    # 空行在发送时已被吃掉，记忆里也就不该还留着空行
    assert chat_rows(store)[1]["content"] == "第一句\n第二句\n第三句", chat_rows(store)[1]["content"]
    # 每个气泡各记一行 kind='say'，背景里就能看到它连发了三条
    assert [r["content"] for r in said_rows(store)] == ["第一句", "第二句", "第三句"], store.rows


async def test_multi_part_partial_failure_discards() -> None:
    """多段里只要有一段失败 → 整轮不落库（哪怕另外两段已经发出去了）。"""
    cfg = base_cfg(reply_split=True, reply_split_max=5)
    store, bot, ok, why = await answer_once(
        gid=7723, reply="第一句\n\n第二句\n\n第三句", fail_at={2}, cfg=cfg
    )
    assert len(bot.sent) == 2, bot.sent   # 第 2 段失败，第 3 段仍会尝试（沿用原行为）
    assert store.rows == [], store.rows


# ---------------------------------------------------------------- 3.5 时间感改写不能拆散分段


async def test_timeguard_keeps_line_structure_so_split_still_works() -> None:
    """2026-11-06 线上事故的回归：timeguard 改写把整条压成一行 → 拆条看到单行 → 整条发出去。

    修好以后：只改含「今晚」的那一行，其余行原样保留，拆条照旧把这条回复分成多条。
    时刻用「全天都算白天」的补丁钉死，免得用例随运行时间飘（夜里根本不改写）。
    """
    cfg = base_cfg(reply_split=True, reply_split_max=8, time_guard=True)
    old_complete = llm_bot.complete
    old_span = (tg.DAY_START, tg.DAY_END)

    async def fake_complete(prompt, cf, **kw):
        return "那今天下午开个自定义试试不就知道了", ""

    llm_bot.complete = fake_complete  # type: ignore[assignment]
    tg.DAY_START, tg.DAY_END = 0, 24
    try:
        store, bot, ok, why = await answer_once(
            gid=7724,
            reply="哈？火山罐挨着人质\n\n那今晚开个自定义试试不就知道了\n\n人质别放得太挤~🐳",
            cfg=cfg,
        )
    finally:
        llm_bot.complete = old_complete  # type: ignore[assignment]
        tg.DAY_START, tg.DAY_END = old_span

    sent = [s.strip() for s in bot.sent]
    assert ok and why == "", (ok, why)
    assert sent == [
        "哈？火山罐挨着人质",
        "那今天下午开个自定义试试不就知道了",
        "人质别放得太挤~🐳",
    ], sent
    assert not any("今晚" in s for s in sent), sent
    assert len(chat_rows(store)) == 2, store.rows      # 全段送达 → 落库
    # 记忆里存的是**实际发出去的样子**：timeguard 已经改写过的那一版，
    # 而不是模型原文里的「今晚」——否则下一轮它会以为自己说的是「今晚」
    kept = chat_rows(store)[1]["content"]
    assert "今晚" not in kept, kept
    assert kept == "哈？火山罐挨着人质\n那今天下午开个自定义试试不就知道了\n人质别放得太挤~🐳", kept
    assert [r["content"] for r in said_rows(store)] == sent, store.rows


# ---------------------------------------------------------------- 4. 网页聊天测试路径


async def test_persist_false_writes_nothing() -> None:
    """``persist=False``（webui「聊天测试」）：既不落库，也不回填 body_out。"""
    store = FakeStore()
    old_store = llm_bot._store
    old_post = llm_bot._post_chat

    async def fake_post(messages, cf, tools=None):
        return "测试回复"

    llm_bot.set_store(store)
    llm_bot._post_chat = fake_post  # type: ignore[assignment]
    box: dict = {}
    try:
        reply, why = await llm_bot.chat(
            7730, 0, "网页测试", "你好", base_cfg(), persist=False, body_out=box
        )
    finally:
        llm_bot._post_chat = old_post  # type: ignore[assignment]
        llm_bot.set_store(old_store)
    assert reply == "测试回复" and why == "", f"{reply} {why}"
    assert store.rows == [], store.rows
    assert box == {}, box


# ---------------------------------------------------------------- 5. body_out 不残留


async def test_body_out_untouched_on_failure() -> None:
    """生成失败 → box 保持原样，调用方不可能拿半箱内容去落库。"""
    bot = LLMBot()  # 独立实例，不碰全局单例的上下文

    async def boom(messages, cf, tools=None):
        raise RuntimeError("502 Bad Gateway")

    bot._post_chat = boom  # type: ignore[assignment]
    box: dict = {}
    reply, why = await bot.chat(7731, 42, "张三", "在吗", base_cfg(), body_out=box)
    assert reply is None and why.startswith("error:"), f"{reply} {why}"
    assert box == {}, box


async def test_body_out_replaced_not_merged() -> None:
    """同一个 dict 复用两轮：第二轮整体替换，第一轮的内容一个字都不能留下。"""
    bot = LLMBot()
    replies = ["第一轮回复", "第二轮回复"]

    async def fake_post(messages, cf, tools=None):
        return replies.pop(0)

    bot._post_chat = fake_post  # type: ignore[assignment]
    cfg = base_cfg()
    box: dict = {"stale": "上一轮留下的脏数据"}

    await bot.chat(7732, 42, "张三", "第一问", cfg, body_out=box)
    keys_first = set(box)
    await bot.chat(7732, 42, "张三", "第二问", cfg, body_out=box)

    assert box["user_content"] == "张三(42)：第二问", box.get("user_content")
    assert box["assistant_content"] == "第二轮回复", box.get("assistant_content")
    assert "stale" not in box, box
    assert set(box) == keys_first, f"{sorted(box)} != {sorted(keys_first)}"
    assert {"user_content", "assistant_content", "reasoning", "user_id", "speaker"} <= set(box)


if __name__ == "__main__":
    test_safe_send_group_returns_receipt()
    test_safe_send_group_none_when_failed()
    test_safe_send_group_truthy_when_no_message_id()
    test_persist_turn_writes_two_rows()
    test_persist_turn_never_raises()

    asyncio.run(test_no_delivery_no_memory())
    asyncio.run(test_delivered_writes_two_rows())
    asyncio.run(test_multi_part_all_delivered_writes_whole_reply())
    asyncio.run(test_multi_part_partial_failure_discards())
    asyncio.run(test_timeguard_keeps_line_structure_so_split_still_works())
    asyncio.run(test_persist_false_writes_nothing())
    asyncio.run(test_body_out_untouched_on_failure())
    asyncio.run(test_body_out_replaced_not_merged())

    print("delivery tests OK")
