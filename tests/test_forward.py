"""合并转发的聊天记录，以及「资料只作用于当次请求」这条约定。

覆盖：

1. ``forward`` 段要调 ``get_forward_msg``（参数名 ``message_id`` / ``id`` 两种都要能试）、
   内联 ``node`` 段、嵌套只展开一层、条数与字数上限；
2. ``with_context``：字符串与 vision 数组两种 content 都能拼，且有自己的字数上限；
3. ``chat(context=…)``：资料进请求但**不进上下文历史**（否则越聊越贵）；
4. ``_llm_context``：只取聊天记录，开关关掉就是空；
5. 表单 / 示例配置 / README 落地。

网页链接**不再自动抓取**——改由模型按需调用 ``fetch_url`` 工具，
相关测试在 ``tests/test_tools.py``。

运行: python tests/test_forward.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))
os.environ.setdefault("MUSIC_DATA_DIR", tempfile.mkdtemp(prefix="music-test-fwd-"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from nonebot.adapters.onebot.v11 import MessageSegment  # noqa: E402

from music_collector.bot_utils import (  # noqa: E402
    describe_forward,
    read_forward_nodes,
    render_forward,
)
from music_collector.config import LLMConfig  # noqa: E402
from music_collector.llm import (  # noqa: E402
    CONTEXT_CHAR_LIMIT,
    LLMBot,
    content_chars,
    with_context,
)

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


def seg(kind: str, **data) -> MessageSegment:
    return MessageSegment(kind, data)


class ForwardBot:
    """只实现 ``call_api``：模拟 get_forward_msg 的两种参数名。"""

    def __init__(self, payload=None, fail_first: bool = True) -> None:
        self.payload = payload if payload is not None else {"messages": []}
        self.fail_first = fail_first
        self.calls: list[dict] = []

    async def call_api(self, api: str, **kwargs):
        self.calls.append({"api": api, **kwargs})
        if api != "get_forward_msg":
            raise RuntimeError(f"unexpected api {api}")
        # go-cqhttp 系用 message_id、部分实现用 id：先试前者，失败再换后者
        if self.fail_first and "message_id" in kwargs:
            raise RuntimeError("unsupported param message_id")
        return self.payload


class _AlwaysFail:
    async def call_api(self, api: str, **kwargs):
        raise RuntimeError("boom")


def test_forward() -> None:
    print("\n[1] 合并转发（聊天记录）")
    payload = {
        "messages": [
            {"sender": {"card": "张三", "nickname": "zs"}, "content": [{"type": "text", "data": {"text": "明天几点出发"}}]},
            {"sender": {"nickname": "李四"}, "content": [
                {"type": "text", "data": {"text": "八点"}},
                {"type": "image", "data": {"url": "http://example.com/a.jpg"}},
            ]},
        ]
    }

    async def run():
        bot = ForwardBot(payload, fail_first=True)
        text = await describe_forward(
            bot, [seg("forward", id="fwd-1")], limit=30, max_chars=2000
        )
        return bot, text

    bot, text = asyncio.run(run())
    check("展开成「昵称：内容」",
          text.splitlines()[:2] == ["张三：明天几点出发", "李四：八点[图片]"], text)
    check("get_forward_msg 走了两次（message_id 失败后换 id）", len(bot.calls) == 2, str(bot.calls))
    check("退回参数名是 id", "id" in bot.calls[-1], str(bot.calls[-1]))

    async def inline():
        bot = ForwardBot()
        return await describe_forward(
            bot,
            [seg("node", nickname="王五", content=[{"type": "text", "data": {"text": "内联内容"}}])],
        )

    check("内联 node 段也能读（不调接口）", asyncio.run(inline()) == "王五：内联内容")

    async def nested():
        bot = ForwardBot({"messages": [
            {"sender": {"nickname": "甲"}, "content": [
                {"type": "text", "data": {"text": "看这个"}},
                {"type": "forward", "data": {"id": "inner"}},
            ]},
        ]})
        return await describe_forward(bot, [seg("forward", id="outer")], limit=10)

    nested_text = asyncio.run(nested())
    check("嵌套转发只展开一层", "[聊天记录]" in nested_text and "inner" not in nested_text, nested_text)

    nodes = [{"who": f"人{i}", "content": [{"type": "text", "data": {"text": f"第{i}句"}}]}
             for i in range(5)]
    cut_by_limit = render_forward(nodes, limit=2)
    check("条数上限生效，并标出剩余条数",
          cut_by_limit.splitlines()[:2] == ["人0：第0句", "人1：第1句"]
          and cut_by_limit.splitlines()[-1] == "…（还有 3 条未展开）", cut_by_limit)
    long_nodes = [{"who": "甲", "content": [{"type": "text", "data": {"text": "字" * 300}}]},
                  {"who": "乙", "content": [{"type": "text", "data": {"text": "短"}}]}]
    cut = render_forward(long_nodes, limit=10, max_chars=100)
    check("字数上限生效并标出剩余条数", "还有 2 条未展开" in cut, cut[-30:])
    check("没有转发段 → 空串",
          asyncio.run(describe_forward(ForwardBot(), [seg("text", text="普通消息")])) == "")
    check("接口报错不影响整体（返回空）",
          asyncio.run(read_forward_nodes(_AlwaysFail(), [seg("forward", id="x")])) == [])


def test_context_merge() -> None:
    print("\n[2] 参考资料拼接")
    check("空资料不改动 content", with_context("问题", "") == "问题")
    got = with_context("问题", "【转发记录】正文")
    check("资料在问题前面", got.startswith("【转发记录】") and got.endswith("问题"), got)

    vision = [{"type": "text", "text": "问题"}, {"type": "image_url", "image_url": {"url": "u"}}]
    merged = with_context(vision, "资料")
    check("vision 数组：资料并进第一段文字", merged[0]["text"].startswith("资料") and len(merged) == 2, str(merged))
    check("vision 数组：图片没被动", merged[1]["type"] == "image_url", str(merged))

    huge = with_context("问题", "字" * (CONTEXT_CHAR_LIMIT + 500))
    check("资料有自己的字数上限", len(huge) <= CONTEXT_CHAR_LIMIT + 10, str(len(huge)))
    check("content_chars 仍然只算文字", content_chars(merged) > 0)


def test_chat_context() -> None:
    print("\n[3] chat(context=…)：进请求、不进历史")
    gid = 8601

    async def run(cfg: LLMConfig, images=None):
        bot = LLMBot()
        captured: dict = {}

        async def fake_post(messages, cfg_, tools=None):
            captured["messages"] = messages
            return "好的"

        bot._post_chat = fake_post  # type: ignore[method-assign]
        reply, reason = await bot.chat(
            gid, 9001, "张三", "这段记录说啥", cfg,
            images=images, context="【群友转发的聊天记录】\n张三：明天八点集合",
        )
        return bot, captured.get("messages") or [], reply, reason

    cfg = LLMConfig(cooldown=0)
    bot, msgs, reply, reason = asyncio.run(run(cfg))
    last = msgs[-1]["content"]
    check("资料进了最后一次请求", "明天八点集合" in str(last), str(last))
    check("提问也在（资料在前）", str(last).index("明天八点集合") < str(last).index("这段记录说啥"), str(last))
    check("回复正常", reply == "好的" and reason == "", f"{reply}/{reason}")
    hist = bot.history(gid)
    check("上下文历史里没有聊天记录正文",
          all("明天八点集合" not in str(m.get("content")) for m in hist), str(hist))
    check("上下文历史照旧存问答对", len(hist) == 2 and "这段记录说啥" in hist[0]["content"], str(hist))

    bot2, msgs2, _, _ = asyncio.run(run(LLMConfig(cooldown=0, vision=True),
                                       images=["http://example.com/x.jpg"]))
    last2 = msgs2[-1]["content"]
    check("带图时资料并进文字段", isinstance(last2, list) and "明天八点集合" in last2[0]["text"], str(last2))
    check("带图时图片段还在", any(p.get("type") == "image_url" for p in last2), str(last2))


def test_llm_context() -> None:
    print("\n[4] _llm_context：只取聊天记录，不碰网页")
    import types

    import music_collector as mc

    event = types.SimpleNamespace(
        group_id=8701,
        message=[
            seg("text", text="看这个 https://example.com/a"),
            seg("forward", id="fwd-9"),
        ],
    )
    bot = ForwardBot({"messages": [{"sender": {"nickname": "甲"},
                                    "content": [{"type": "text", "data": {"text": "记录内容"}}]}]})

    ctx = asyncio.run(mc._llm_context(bot, event, LLMConfig(), "看这个 https://example.com/a"))
    check("聊天记录进了资料", "【群友转发的聊天记录】" in ctx and "记录内容" in ctx, ctx)
    check("链接不再被自动抓（资料里没有网页内容）", "网页内容" not in ctx, ctx)

    off = asyncio.run(mc._llm_context(
        bot, event, LLMConfig(read_forward=False), "随便说说"))
    check("开关关掉 → 空资料", off == "", off)

    # 「引用一条转发记录」再提问，内容在 event.reply 里
    quoted_ev = types.SimpleNamespace(
        group_id=8703,
        message=[seg("text", text="这是啥意思")],
        reply=types.SimpleNamespace(message=[seg("forward", id="fwd-r")]),
    )
    quoted_ctx = asyncio.run(mc._llm_context(bot, quoted_ev, LLMConfig(), "这是啥意思"))
    check("被引用的转发记录也会展开", "【被引用的聊天记录】" in quoted_ctx, quoted_ctx)


def test_form_and_docs() -> None:
    print("\n[5] 表单 / 示例配置 / README 落地")
    from music_collector.config import AppConfig
    from music_collector.webui import KEY_INDEX

    for key in ("llm.read_forward", "llm.forward_limit", "llm.forward_chars",
                "llm.tools", "llm.tool_rounds", "llm.web_tool", "llm.skills",
                "llm.skills_dir", "llm.skill_scripts"):
        check(f"表单里有 {key}", key in KEY_INDEX, str(list(KEY_INDEX)[:3]))
    for key in ("llm.read_link", "llm.link_max", "llm.link_chars", "llm.link_timeout"):
        check(f"旧字段已移除 {key}", key not in KEY_INDEX)

    cfg = AppConfig()
    check("默认开着聊天记录", cfg.llm.read_forward is True)
    check("默认开着工具与技能", cfg.llm.tools is True and cfg.llm.skills is True)
    check("脚本执行默认关闭", cfg.llm.skill_scripts is False)
    check("默认上限符合约定",
          cfg.llm.forward_limit == 30 and cfg.llm.forward_chars == 2000
          and cfg.llm.tool_rounds == 5,
          str((cfg.llm.forward_limit, cfg.llm.forward_chars, cfg.llm.tool_rounds)))

    sample = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    for key in ("read_forward", "forward_limit", "tool_rounds", "skill_scripts"):
        check(f"示例配置写了 {key}", key in sample)
    for key in ("read_link", "link_max"):
        check(f"示例配置不再有 {key}", key not in sample)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for key in ("llm.read_forward", "get_forward_msg", "fetch_url", "SKILL.md", "SSRF"):
        check(f"README 写了 {key}", key in readme)


def main() -> None:
    test_forward()
    test_context_merge()
    test_chat_context()
    test_llm_context()
    test_form_and_docs()

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
