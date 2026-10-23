"""大模型什么时候该开口：@ 之外的两个触发点，以及「非音乐链接」的处理。

覆盖：

1. ``_has_music_link``（准判）与 ``_looks_like_music``（粗筛）的分工——
   普通网页链接不能被当成音乐；
2. ``_llm_gate``：三道门（大模型开关 / 插件开关 / 生效群）的返回原因；
3. 「@ 机器人 + 非音乐链接」→ 模型回答（以前会被当成音乐分享直接跳过）；
4. 「非 @ 的普通链接」→ 收集流程 silent 之后交给模型，且带 ``link_hint``；
5. 真·音乐链接**不会**被模型截走（无论 @ 没 @）；
6. 「@ 过机器人的链接」只回一次（at 流程答过后 music 流程不再答）；
7. 戳一戳：戳机器人才回、``poke_reply`` 开关、``llm.enabled`` 关闭、冷却静默；
8. 配置 / 表单 / 示例配置 / README 的落地检查，以及提示词字数上限进了 schema。

运行: python tests/test_llm_triggers.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))
os.environ.setdefault("MUSIC_DATA_DIR", tempfile.mkdtemp(prefix="music-test-trig-"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from nonebot.adapters.onebot.v11 import (  # noqa: E402
    GroupMessageEvent,
    Message,
    MessageSegment,
    PokeNotifyEvent,
)
from nonebot.adapters.onebot.v11.event import Sender  # noqa: E402

from music_collector import (  # noqa: E402
    _has_music_link,
    _llm_gate,
    _looks_like_music,
    _poked_me,
    handle_at,
    handle_music_share,
    handle_poke,
)
from music_collector.config import LLMConfig, config_manager  # noqa: E402
from music_collector.llm import LLMBot, SYSTEM_CHAR_LIMIT  # noqa: E402
from music_collector.service import service  # noqa: E402
from music_collector.store import Store  # noqa: E402
from music_collector.webui import BOT_SCHEMA, KEY_INDEX  # noqa: E402
from music_collector import webui_frontend  # noqa: E402

PASSED = 0
FAILED = 0

GID = 8501
NETEASE_URL = "https://music.163.com/song?id=1974443814&uct2=U2FsdGVkX1"
QQMUSIC_URL = "https://y.qq.com/n/ryqq/songDetail/003OUlho2HcRHC"
PLAIN_URL = "https://www.example.com/posts/how-to-cook-noodles"


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


class StubBot:
    """只实现 ``send_group_msg``，把发出去的消息收集起来。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_group_msg(self, group_id: int, message) -> None:
        self.sent.append(str(message))


def make_event(segments, *, user_id: int = 42, group_id: int = GID) -> GroupMessageEvent:
    msg = segments if isinstance(segments, Message) else Message(segments)
    return GroupMessageEvent(
        time=0,
        self_id=1,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=300,
        message=msg,
        original_message=msg,
        raw_message=str(msg),
        font=0,
        sender=Sender(user_id=user_id, nickname="张三", card="张三"),
        group_id=group_id,
        anonymous=None,
    )


def make_poke(*, user_id: int = 42, target_id: int = 1, group_id=GID) -> PokeNotifyEvent:
    return PokeNotifyEvent(
        time=0,
        self_id=1,
        post_type="notice",
        notice_type="notify",
        sub_type="poke",
        user_id=user_id,
        group_id=group_id,
        target_id=target_id,
    )


def reset_llm(enabled: bool = True, **over) -> None:
    cfg = config_manager.config.llm
    cfg.enabled = enabled
    cfg.groups = []
    cfg.base_url = "https://api.example.com/v1"
    cfg.api_key = "sk-test"
    cfg.model = "test-model"
    cfg.system_prompt = "你是助手"
    cfg.temperature = 0.5
    cfg.max_tokens = 256
    cfg.timeout = 5.0
    cfg.history_rounds = 0
    cfg.history_chars = 3000
    cfg.cooldown = 0
    cfg.max_input_chars = 0
    cfg.max_reply_chars = 800
    cfg.at_sender = False
    cfg.fallback_intro = True
    cfg.read_reply = True
    cfg.reply_chars = 200
    cfg.vision = False
    cfg.max_images = 2
    cfg.poke_reply = True
    cfg.poke_prompt = "（戳了戳你）"
    cfg.reply_link = True
    cfg.link_hint = "（这不是音乐平台的歌曲链接，顺口接一句就行，不用当成点歌）"
    for k, v in over.items():
        setattr(cfg, k, v)


class Stubbed:
    """替身 LLMBot：记录请求、返回固定文本，统一替换 ``music_collector.llm_bot``。"""

    def __init__(self, reply_text: str = "好", *, fail: str = "") -> None:
        self.calls: list[list[dict]] = []
        self.bot = LLMBot()
        self.reply_text = reply_text
        self.fail = fail

        async def fake_post(messages, cfg):
            self.calls.append([dict(m) for m in messages])
            if self.fail:
                raise RuntimeError(self.fail)
            return self.reply_text

        self.bot._post_chat = fake_post  # type: ignore[method-assign]

    def install(self) -> "Stubbed":
        import music_collector as mc

        mc.llm_bot = self.bot
        return self

    def last_text(self) -> str:
        return str(self.calls[-1][-1]["content"]) if self.calls else ""


# ------------------------------------------------------- 1. 音乐链接判定


async def test_link_detection() -> None:
    print("\n[1] _has_music_link 准判 / _looks_like_music 粗筛")

    def text_ev(text: str) -> GroupMessageEvent:
        return make_event([seg("text", text=text)])

    check("网易云链接算音乐", _has_music_link(text_ev(f"分享 {NETEASE_URL}")) is True)
    check("QQ 音乐链接算音乐", _has_music_link(text_ev(QQMUSIC_URL)) is True)
    check("普通网页链接不算音乐", _has_music_link(text_ev(PLAIN_URL)) is False)
    check("纯文字不算音乐", _has_music_link(text_ev("今天吃啥")) is False)

    # 粗筛本来就只负责「省掉全量解析」，会把普通链接也当候选——这正是不能拿它
    # 决定「是不是音乐」的原因（旧代码在 @ 流程里就是用它，导致 @+网页链接被跳过）
    check("粗筛对普通链接返回 True", await _looks_like_music(text_ev(PLAIN_URL)) is True)

    music_card = {
        "app": "com.tencent.music",
        "meta": {
            "music": {
                "title": "晴天",
                "desc": "周杰伦",
                "jumpUrl": "https://y.qq.com/n/ryqq/songDetail/003OUlho2HcRHC",
            }
        },
    }
    check("音乐卡片算音乐",
          _has_music_link(make_event([seg("json", data=music_card)])) is True)
    plain_card = {"app": "com.tencent.miniapp", "meta": {"detail_1": {"title": "某小程序"}}}
    check("普通卡片不算音乐",
          _has_music_link(make_event([seg("json", data=plain_card)])) is False)
    check("music 段算音乐",
          _has_music_link(make_event([seg("music", type="qq", id="1")])) is True)
    check("非音乐 xml 不算",
          _has_music_link(make_event([seg("xml", data="<msg><app>other</app></msg>")])) is False)


# ------------------------------------------------------- 2. 三道门


def test_gate() -> None:
    print("\n[2] _llm_gate：三道门")
    cfg = config_manager.config

    reset_llm(enabled=False)
    got_cfg, why = _llm_gate(group_id=GID)
    check("未启用 → 原因是大模型未启用", "未启用" in why, why)
    check("原因里带得回 llm_cfg", got_cfg is cfg.llm)

    reset_llm()
    cfg.llm.groups = [999999]
    _, why = _llm_gate(group_id=GID)
    check("群不在生效列表 → 拦下", "不在生效列表" in why, why)

    reset_llm()
    cfg.enabled = False
    _, why = _llm_gate(group_id=GID)
    check("插件总开关关 → 拦下", "总开关" in why, why)

    cfg.enabled = True
    cfg.groups = [999999]
    _, why = _llm_gate(group_id=GID)
    check("插件生效列表不含本群 → 拦下", "生效列表" in why, why)

    cfg.groups = []
    _, why = _llm_gate(group_id=GID)
    check("全部满足 → 空原因", why == "", why)


# ------------------------------------------------------- 3. @ + 非音乐链接


async def test_at_with_plain_link() -> None:
    print("\n[3] @ 机器人 + 普通链接：交给模型（不再当音乐跳过）")
    reset_llm()
    stub = Stubbed("这文章讲的是煮面").install()

    ev = make_event([seg("text", text=f"看看这个 {PLAIN_URL}")], user_id=4001)
    bot = StubBot()
    await handle_at(bot, ev)
    check("模型被调用", len(stub.calls) == 1, str(len(stub.calls)))
    check("请求里带上了链接", PLAIN_URL in stub.last_text(), stub.last_text()[:120])
    check("回了这条消息", bot.sent and "这文章讲的是煮面" in bot.sent[0], str(bot.sent))
    check("@ 路径不加 link_hint", "不用当成点歌" not in stub.last_text())

    # 真音乐链接：模型不抢，仍由收集流程负责（此处不在收集期 → 静默；
    # 自我介绍默认也跳过音乐链接，所以群里什么都不发）
    stub = Stubbed("不该被调用").install()
    ev = make_event([seg("text", text=f"{NETEASE_URL}")], user_id=4002)
    bot = StubBot()
    await handle_at(bot, ev)
    check("真音乐链接不调模型", stub.calls == [], str(stub.calls))
    check("真音乐链接也不自我介绍", bot.sent == [], str(bot.sent))


# ------------------------------------------------------- 4. 非 @ 的普通链接


async def test_plain_link_share() -> None:
    print("\n[4] 群里分享普通链接：收集流程静默后交给模型")
    reset_llm()
    stub = Stubbed("这链接是个菜谱").install()

    ev = make_event([seg("text", text=f"https://example.com/a?b=1")], user_id=4101)
    bot = StubBot()
    await handle_music_share(bot, ev)
    check("模型被调用", len(stub.calls) == 1, str(len(stub.calls)))
    check("请求里带 link_hint", "不用当成点歌" in stub.last_text(), stub.last_text()[:160])
    check("回复发到了群里", bot.sent and "这链接是个菜谱" in bot.sent[0], str(bot.sent))

    # 关掉 reply_link → 一声不吭
    reset_llm(reply_link=False)
    stub = Stubbed("不该被调用").install()
    bot = StubBot()
    await handle_music_share(bot, make_event([seg("text", text=PLAIN_URL)], user_id=4102))
    check("reply_link 关掉后不调模型", stub.calls == [], str(stub.calls))
    check("reply_link 关掉后不发消息", bot.sent == [], str(bot.sent))

    # 没有链接的消息（比如纯文字）不该被接话
    reset_llm()
    stub = Stubbed("不该被调用").install()
    bot = StubBot()
    await handle_music_share(bot, make_event([seg("text", text="哈哈哈")], user_id=4103))
    check("没链接不调模型", stub.calls == [], str(stub.calls))

    # 非音乐卡片也不接（读不出可聊的内容）
    stub = Stubbed("不该被调用").install()
    await handle_music_share(
        StubBot(), make_event([seg("json", data={"app": "x", "meta": {}})], user_id=4104)
    )
    check("空卡片不调模型", stub.calls == [], str(stub.calls))

    # 真音乐链接（此处不在收集期，收集流程会早退）→ 不能让模型顶上去
    reset_llm()
    stub = Stubbed("不该被调用").install()
    await handle_music_share(StubBot(), make_event([seg("text", text=QQMUSIC_URL)], user_id=4105))
    check("真音乐链接不被截走", stub.calls == [], str(stub.calls))


async def test_no_double_reply() -> None:
    print("\n[5] @ 过的链接只回一次（at 流程答完，music 流程不再答）")
    reset_llm()
    stub = Stubbed("答一次就够").install()
    ev = make_event(
        Message(seg("at", qq="1", name="机器人") + seg("text", text=f" {PLAIN_URL}")),
        user_id=4201,
    )
    ev.__dict__["to_me"] = True  # OneBot 会把「@机器人」摘掉并置 to_me

    bot = StubBot()
    await handle_at(bot, ev)
    after_at = len(bot.sent)
    check("at 流程答了一次", after_at == 1, str(bot.sent))

    await handle_music_share(bot, ev)
    check("music 流程没有再答", len(bot.sent) == after_at, str(bot.sent))
    check("模型只被调了一次", len(stub.calls) == 1, str(len(stub.calls)))


# ------------------------------------------------------- 6. 戳一戳


async def test_poke() -> None:
    print("\n[6] 戳一戳 → 模型回一句")
    reset_llm()
    stub = Stubbed("别戳我啦").install()

    poke = make_poke(user_id=4301)
    check("戳机器人自己 → 规则命中", await _poked_me(None, poke) is True)
    check("戳别人 → 规则不命中",
          await _poked_me(None, make_poke(target_id=999)) is False)

    bot = StubBot()
    await handle_poke(bot, poke)
    check("戳一戳触发了模型", len(stub.calls) == 1, str(len(stub.calls)))
    check("请求里是 poke_prompt", "戳了戳你" in stub.last_text(), stub.last_text()[:120])
    check("带上了戳的人的称呼", "4301" in stub.last_text(), stub.last_text()[:120])
    check("群里收到了回复", bot.sent and "别戳我啦" in bot.sent[0], str(bot.sent))

    # 冷却：同一人连戳只回一次
    reset_llm(cooldown=60)
    stub = Stubbed("第一次").install()
    bot = StubBot()
    await handle_poke(bot, make_poke(user_id=4302))
    await handle_poke(bot, make_poke(user_id=4302))
    check("冷却中只回一次", len(bot.sent) == 1 and len(stub.calls) == 1, str(bot.sent))

    # 关掉 poke_reply
    reset_llm(poke_reply=False)
    stub = Stubbed("不该被调用").install()
    bot = StubBot()
    await handle_poke(bot, make_poke(user_id=4303))
    check("poke_reply 关掉后不调模型", stub.calls == [], str(stub.calls))
    check("poke_reply 关掉后不发消息", bot.sent == [], str(bot.sent))

    # 大模型没启用
    reset_llm(enabled=False)
    stub = Stubbed("不该被调用").install()
    bot = StubBot()
    await handle_poke(bot, make_poke(user_id=4304))
    check("大模型关掉后戳也不回", stub.calls == [] and bot.sent == [], str(bot.sent))

    # 私聊戳：不接（上下文按群维护）
    reset_llm()
    stub = Stubbed("不该被调用").install()
    bot = StubBot()
    await handle_poke(bot, make_poke(user_id=4305, group_id=None))
    check("私聊戳不接", stub.calls == [] and bot.sent == [], str(bot.sent))

    # 自定义话术
    reset_llm(poke_prompt="（被戳了，问他要干嘛）")
    stub = Stubbed("怎么啦").install()
    await handle_poke(StubBot(), make_poke(user_id=4306))
    check("poke_prompt 可自定义", "问他要干嘛" in stub.last_text(), stub.last_text()[:120])

    # 空白话术 → 退回默认
    reset_llm(poke_prompt="   ")
    stub = Stubbed("嗯？").install()
    await handle_poke(StubBot(), make_poke(user_id=4307))
    check("空白话术退回默认", "戳了戳你" in stub.last_text(), stub.last_text()[:120])


# ------------------------------------------------------- 7. 配置与表单


def test_config_and_form() -> None:
    print("\n[7] 配置默认值 / 表单 / 示例配置 / README")

    cfg = LLMConfig()
    check("poke_reply 默认开", cfg.poke_reply is True)
    check("poke_prompt 默认有内容", bool(cfg.poke_prompt.strip()))
    check("reply_link 默认开", cfg.reply_link is True)
    check("link_hint 默认有内容", "点歌" in cfg.link_hint)

    for key in ("llm.poke_reply", "llm.poke_prompt", "llm.reply_link", "llm.link_hint"):
        desc = KEY_INDEX.get(key)
        check(f"KEY_INDEX 含 {key}", desc is not None, str(desc))
        if desc:
            check(f"{key} 有中文标签", bool(desc.get("label")), str(desc))
            check(f"{key} 有说明", bool(desc.get("hint")), str(desc))

    fields = {f["key"] for sec in BOT_SCHEMA for f in sec.get("fields", [])}
    for key in ("llm.poke_reply", "llm.poke_prompt", "llm.reply_link", "llm.link_hint"):
        check(f"机器人页含 {key}", key in fields)

    check("poke_reply 是开关", KEY_INDEX["llm.poke_reply"]["type"] == "bool")

    # 提示词字数上限进了 schema，前端才能显示「已用 x / 4000 字」
    prompt = KEY_INDEX["llm.system_prompt"]
    check("system_prompt 是多行", prompt["multiline"] is True)
    check("system_prompt 带上限", prompt["max_chars"] == SYSTEM_CHAR_LIMIT == 4000,
          str(prompt.get("max_chars")))
    check("其它字段没有多余上限", KEY_INDEX["llm.model"]["max_chars"] == 0,
          str(KEY_INDEX["llm.model"].get("max_chars")))

    html = webui_frontend.DASHBOARD_HTML
    check("前端渲染字数统计", "已用 ${n} / ${f.max_chars} 字" in html, "计数器缺失")
    check("前端有超限提示样式", ".fcount.over" in html)
    check("机器人页有「附加触发」分组", "附加触发" in html)

    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    for key in ("poke_reply", "poke_prompt", "reply_link", "link_hint"):
        check(f"示例配置含 {key}", f"{key}:" in example)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("README 提到戳一戳", "戳一戳" in readme)
    check("README 提到非音乐链接", "不是音乐链接" in readme or "非音乐链接" in readme)
    check("README 提到 4000 字上限", "4000 字" in readme)


# ------------------------------------------------------- main


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    service.store = Store(tmp / "collector.db")
    await service.store.init()

    cfg = config_manager.config
    cfg.enabled = True
    cfg.groups = []
    cfg.intro.enabled = True
    cfg.intro.cooldown = 0
    # 不在收集期：handle_segments 会早退，链接判定才是唯一变量，也不会联网
    cfg.collect_override = "off"

    await test_link_detection()
    test_gate()
    await test_at_with_plain_link()
    await test_plain_link_share()
    await test_no_double_reply()
    await test_poke()
    test_config_and_form()

    print(f"\n{'=' * 56}")
    print(f"通过 {PASSED} / 失败 {FAILED}")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
