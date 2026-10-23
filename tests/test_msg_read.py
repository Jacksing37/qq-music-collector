"""群友发的东西能不能被模型读到：消息段解析 / 引用回复 / 图片 / 降级。

覆盖：
1. ``read_message``：文本、@、图片（有/无链接）、表情、表情包、语音、文件、视频、
   混合消息的先后顺序、空消息；
2. ``quote_prefix``：正常、空内容、超长截断、``limit=0``、没昵称的兜底；
3. ``build_question_content`` / ``content_chars`` / ``without_images``：
   无图给字符串、有图给 OpenAI vision 数组，多模态数组按文字部分计字符；
4. ``is_vision_unsupported``：各家「不吃图」的措辞都要认，401 与超窗不能误判；
5. ``LLMBot.chat``：``vision`` 开关、``max_images`` 上限、带图被拒后自动去图重试
   （且**只试一次**）、历史里只留 ``[图片]`` 不含 URL、引用进请求；
6. 配置与表单：四个新键的默认值、``KEY_INDEX``、「机器人」页 schema、示例配置；
7. 接线：只发图不打字也能触发大模型；``read_reply=false`` 时不读引用内容。

运行: python tests/test_msg_read.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))
# 独立跑时也要隔离数据目录，绝不碰真实 data/
os.environ.setdefault("MUSIC_DATA_DIR", tempfile.mkdtemp(prefix="music-test-msg-"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment  # noqa: E402
from nonebot.adapters.onebot.v11.event import Sender  # noqa: E402

from music_collector import handle_at  # noqa: E402
from music_collector.bot_utils import (  # noqa: E402
    quote_prefix,
    quote_prefix_and_images,
    read_message,
)
from music_collector.config import LLMConfig, config_manager  # noqa: E402
from music_collector.llm import (  # noqa: E402
    LLMBot,
    build_question_content,
    content_chars,
    is_vision_unsupported,
    without_images,
)
from music_collector.service import service  # noqa: E402
from music_collector.store import Store  # noqa: E402
from music_collector.webui import BOT_SCHEMA, KEY_INDEX  # noqa: E402

PASSED = 0
FAILED = 0

GID = 8301
IMG1 = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=aaa"
IMG2 = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=bbb"


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


def image_seg(url: str = "", file: str = "abc.image") -> MessageSegment:
    data = {"file": file}
    if url:
        data["url"] = url
    return MessageSegment("image", data)


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
        message_id=200,
        message=msg,
        original_message=msg,
        raw_message=str(msg),
        font=0,
        sender=Sender(user_id=user_id, nickname="张三", card="张三"),
        group_id=group_id,
        anonymous=None,
    )


class FakeReply:
    """鸭子类型的 ``event.reply``：只提供代码真正读的字段。"""

    class sender:  # noqa: N801 — 故意模拟 ReplySender
        user_id = 7
        nickname = "李四"
        card = "李四"

    user_id = 7

    def __init__(self, content) -> None:
        # 既接受纯文字，也接受整条 Message（带图的那种引用）
        self.message = Message(content) if isinstance(content, str) else content


def attach_reply(event: GroupMessageEvent, content) -> None:
    """把伪造的引用塞进事件（绕过 pydantic 校验）。"""
    event.__dict__["reply"] = FakeReply(content)


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
    cfg.history_rounds = 3
    cfg.history_chars = 3000
    cfg.cooldown = 0
    cfg.max_input_chars = 500
    cfg.max_reply_chars = 800
    cfg.at_sender = False
    cfg.fallback_intro = True
    cfg.read_reply = True
    cfg.reply_chars = 200
    cfg.vision = False
    cfg.max_images = 2
    for k, v in over.items():
        setattr(cfg, k, v)


# ---------------------------------------------------------------- 1. read_message


def test_read_message() -> None:
    print("\n[1] read_message：把消息段转成模型能读的文字")

    text, images = read_message(Message(seg("text", text="这首歌好听")))
    check("纯文本原样", text == "这首歌好听" and images == [], f"{text} / {images}")

    text, images = read_message(
        Message(seg("text", text="看图") + image_seg(IMG1))
    )
    check("图片留 [图片] 标记", text == "看图 [图片]" or text == "看图[图片]", repr(text))
    check("图片链接被收出", images == [IMG1], str(images))

    text, images = read_message(Message(image_seg(file="abc.image")))
    check("图没链接时只留标记", text == "[图片]" and images == [], f"{text} / {images}")

    # 个别实现把链接放在 file 里
    text, images = read_message(Message(image_seg(url="", file="https://x.com/a.jpg")))
    check("file 里是链接时也能收", images == ["https://x.com/a.jpg"], str(images))

    text, images = read_message(
        Message(image_seg(IMG1) + image_seg(IMG1) + image_seg(IMG2))
    )
    check("同一张图只收一次", images == [IMG1, IMG2], str(images))
    check("三张图都留标记", text.count("[图片]") == 3, repr(text))

    text, _ = read_message(Message(seg("at", qq="123", name="张三") + seg("text", text=" 你好")))
    check("@ 段变成 @昵称", "@张三" in text and "你好" in text, repr(text))

    text, _ = read_message(Message(seg("at", qq="123")))
    check("没有昵称时用 QQ 号", text.strip() == "@123", repr(text))

    text, _ = read_message(Message(seg("at", qq="999", name="机器人")), {"999"})
    check("skip_at 指定的 @ 被丢掉", text == "", repr(text))

    text, _ = read_message(
        Message(seg("at", qq="999", name="机器人") + seg("text", text=" 你好")), {"999"}
    )
    check("@机器人 + 文字时只留文字", text == "你好", repr(text))

    text, _ = read_message(Message(seg("face", id="1")))
    check("表情 → [表情]", text == "[表情]", repr(text))

    text, _ = read_message(Message(seg("mface", summary="笑哭")))
    check("表情包带文字", text == "[表情包: 笑哭]", repr(text))

    text, _ = read_message(Message(seg("mface", emoji_id="x")))
    check("表情包无文字", text == "[表情包]", repr(text))

    text, _ = read_message(Message(seg("record", file="a.amr")))
    check("语音 → [语音]", text == "[语音]", repr(text))

    text, _ = read_message(Message(seg("file", name="报告.pdf")))
    check("文件带文件名", text == "[文件: 报告.pdf]", repr(text))

    text, _ = read_message(Message(seg("video", file="v.mp4")))
    check("视频 → [视频]", text == "[视频]", repr(text))

    text, images = read_message(
        Message(
            seg("text", text="前面")
            + image_seg(IMG1)
            + seg("face", id="1")
            + seg("text", text="后面")
        )
    )
    check("混合消息顺序不乱", text == "前面[图片][表情]后面", repr(text))
    check("混合消息仍能取到图", images == [IMG1], str(images))

    text, images = read_message("  多   个   空格  ")
    check("字符串入参也压空白", text == "多 个 空格" and images == [], repr(text))

    text, images = read_message(Message(""))
    check("空消息", text == "" and images == [], f"{text!r} / {images}")


# ---------------------------------------------------------------- 2. quote_prefix


def test_quote_prefix() -> None:
    print("\n[2] quote_prefix：被引用的消息压成一句前缀")

    got = quote_prefix(Message("这首歌是不是周杰伦的"), "张三")
    check("引用格式", got == "（引用 张三 的消息：这首歌是不是周杰伦的）", got)

    check("空内容不给前缀", quote_prefix(Message(""), "张三") == "")

    long_text = "字" * 400
    got = quote_prefix(Message(long_text), "张三", 50)
    check("超长截断到上限", len(got) <= len("（引用 张三 的消息：）") + 50, str(len(got)))
    check("截断带省略号", got.endswith("…）"), got[-6:])

    got = quote_prefix(Message(long_text), "张三", 0)
    check("limit=0 不截断", long_text in got, str(len(got)))

    got = quote_prefix(Message("原话"), "")
    check("没昵称时用「对方」", "对方" in got, got)

    got = quote_prefix(Message(image_seg(IMG1)), "张三")
    check("引用里只有图也给标记", "（引用 张三 的消息：[图片]）" == got, got)

    # 关键：引用里带图时，图片链接必须一起带出去，否则模型只会看到 [图片]
    prefix, imgs = quote_prefix_and_images(
        Message(seg("text", text="这张") + image_seg(IMG1)), "张三"
    )
    check("带图引用：前缀照旧有 [图片]", "[图片]" in prefix, prefix)
    check("带图引用：图片链接被带出", imgs == [IMG1], str(imgs))

    prefix, imgs = quote_prefix_and_images(Message(image_seg(IMG2)), "张三")
    check("只有图的引用也返回链接", imgs == [IMG2], str(imgs))

    prefix, imgs = quote_prefix_and_images(Message("纯文字"), "张三")
    check("纯文字引用没有图", imgs == [], str(imgs))

    prefix, imgs = quote_prefix_and_images(Message(""), "张三")
    check("空引用返回空前缀与空图", prefix == "" and imgs == [], f"{prefix!r} / {imgs}")

    check("quote_prefix 仍只返回字符串",
          isinstance(quote_prefix(Message("原话"), "张三"), str))


# ---------------------------------------------------------------- 3. content 工具


def test_content_helpers() -> None:
    print("\n[3] build_question_content / content_chars / without_images")

    plain = build_question_content("张三：在吗", [])
    check("无图时是纯字符串", isinstance(plain, str) and plain == "张三：在吗", repr(plain))

    rich = build_question_content("张三：看图 [图片]", [IMG1, IMG2])
    check("有图时是数组", isinstance(rich, list) and len(rich) == 3, str(rich))
    check("首项是文字", rich[0] == {"type": "text", "text": "张三：看图 [图片]"}, str(rich[0]))
    check("其余是 image_url", all(p["type"] == "image_url" for p in rich[1:]), str(rich))
    check("图片地址带在 url 里", rich[1]["image_url"]["url"] == IMG1, str(rich[1]))

    check("content_chars 认字符串", content_chars("abc") == 3)
    check("content_chars 只算文字部分", content_chars(rich) == len("张三：看图 [图片]"),
          str(content_chars(rich)))
    check("content_chars 容忍其它类型", content_chars(None) == 0)

    msgs = [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": rich},
    ]
    stripped = without_images(msgs, "张三：看图 [图片]")
    check("去图后条数不变", len(stripped) == 2, str(len(stripped)))
    check("去图后最后一条是字符串", isinstance(stripped[-1]["content"], str),
          repr(stripped[-1]["content"]))
    check("system 原样保留", stripped[0]["content"] == "系统")


# ---------------------------------------------------------------- 4. 不吃图判定


def test_vision_unsupported() -> None:
    print("\n[4] is_vision_unsupported：哪些报错算「模型不吃图」")

    for msg in (
        'HTTP 400: {"error": "Invalid content type: image_url"}',
        "HTTP 400: this model does not support image input",
        "HTTP 400: multimodal is not supported for this model",
        "HTTP 400: content must be a string",
        "HTTP 400: 该模型不支持图片输入",
        "HTTP 400: 仅支持文本输入",
        # 图片拉不下来（引用旧消息时链接签名过期）也算「这张图没法用」，一样降级
        "HTTP 400: failed to download image from url",
        "HTTP 400: 无法下载图片，链接可能已过期",
    ):
        check(f"认得出：{msg[:36]}…", is_vision_unsupported(msg) is True, msg)

    for msg in (
        "HTTP 401: invalid api key",
        "HTTP 400: maximum context length exceeded",
        "网络错误: All connection attempts failed",
        "HTTP 500: boom",
    ):
        check(f"不误判：{msg[:30]}…", is_vision_unsupported(msg) is False, msg)


# ---------------------------------------------------------------- 5. chat 多模态


def make_bot(reply_text: str = "好", *, fail=None) -> tuple[LLMBot, list]:
    """造一个 LLMBot，``_post_chat`` 换成记录请求的桩。"""
    bot = LLMBot()
    calls: list[list[dict]] = []

    async def fake_post(messages, cfg):
        calls.append([dict(m) for m in messages])
        if fail is not None:
            raised = fail(messages)
            if raised:
                raise RuntimeError(raised)
        return reply_text

    bot._post_chat = fake_post  # type: ignore[method-assign]
    return bot, calls


async def test_chat_vision() -> None:
    print("\n[5] chat：vision 开关、max_images、降级重试、历史不存 URL")

    q = "这图里是什么 [图片]"
    image = f"张三：{q}"

    # (a) vision 关 → 只发字符串，图片被忽略
    bot, calls = make_bot("看到了吗")
    cfg = LLMConfig(history_rounds=0, cooldown=0, vision=False)
    reply, reason = await bot.chat(8001, 1, "张三", q, cfg, images=[IMG1])
    sent = calls[-1][-1]["content"]
    check("vision 关时 content 是字符串", isinstance(sent, str), repr(sent))
    check("vision 关时图片不进请求", IMG1 not in str(sent), sent)
    check("vision 关仍能正常回复", reply == "看到了吗" and reason == "", f"{reply}/{reason}")

    # (b) vision 开 → 数组格式
    bot, calls = make_bot("是一只鲸鱼")
    cfg = LLMConfig(history_rounds=0, cooldown=0, vision=True, max_images=2)
    reply, _ = await bot.chat(8002, 1, "张三", q, cfg, images=[IMG1, IMG2])
    sent = calls[-1][-1]["content"]
    check("vision 开时 content 是数组", isinstance(sent, list) and len(sent) == 3, str(sent))
    check("文字部分是完整提问", sent[0]["text"] == image, sent[0]["text"])
    check("两张图都带上", [p["image_url"]["url"] for p in sent[1:]] == [IMG1, IMG2], str(sent))

    # (c) max_images 上限
    bot, calls = make_bot("嗯")
    cfg = LLMConfig(history_rounds=0, cooldown=0, vision=True, max_images=1)
    await bot.chat(8003, 1, "张三", q, cfg, images=[IMG1, IMG2])
    sent = calls[-1][-1]["content"]
    check("max_images=1 只带一张", len(sent) == 2, str(len(sent)))

    bot, calls = make_bot("嗯")
    cfg = LLMConfig(history_rounds=0, cooldown=0, vision=True, max_images=0)
    await bot.chat(8004, 1, "张三", q, cfg, images=[IMG1])
    sent = calls[-1][-1]["content"]
    check("max_images=0 一张都不带", isinstance(sent, str), repr(sent))

    # (d) 上游不认图 → 自动去图重试一次
    def reject_images(messages):
        return "HTTP 400: this model does not support image input" if isinstance(
            messages[-1]["content"], list
        ) else ""

    bot, calls = make_bot("那我就文字作答", fail=reject_images)
    cfg = LLMConfig(history_rounds=0, cooldown=0, vision=True)
    reply, reason = await bot.chat(8005, 1, "张三", q, cfg, images=[IMG1])
    check("去图后重试成功", reply == "那我就文字作答" and reason == "", f"{reply}/{reason}")
    check("一共请求两次", len(calls) == 2, str(len(calls)))
    check("第二次是纯文本", isinstance(calls[-1][-1]["content"], str),
          repr(calls[-1][-1]["content"]))

    # (e) 去图也救不回来 → 只报错，不无限重试
    def always(messages):
        return "HTTP 400: image_url is not supported"

    bot, calls = make_bot("不会到这里", fail=always)
    cfg = LLMConfig(history_rounds=0, cooldown=0, vision=True)
    reply, reason = await bot.chat(8006, 1, "张三", q, cfg, images=[IMG1])
    check("彻底失败时返回 None", reply is None and reason.startswith("error"), f"{reply}/{reason}")
    check("降级只试一次（共 2 次请求）", len(calls) == 2, str(len(calls)))

    # (f) 历史里只有 [图片]，不含会被烧掉的链接
    bot, calls = make_bot("记住了")
    cfg = LLMConfig(history_rounds=3, cooldown=0, vision=True)
    await bot.chat(8007, 1, "张三", q, cfg, images=[IMG1])
    hist = list(bot._history.get(8007, []))
    check("历史写了两条", len(hist) == 2, str(len(hist)))
    check("历史里没有图片链接", all(IMG1 not in str(m["content"]) for m in hist), str(hist))
    check("历史里保留 [图片] 标记", "[图片]" in hist[0]["content"], hist[0]["content"])

    # (g) 引用前缀进了请求，也进了历史
    bot, calls = make_bot("是周杰伦的")
    cfg = LLMConfig(history_rounds=3, cooldown=0)
    await bot.chat(8008, 1, "张三", "那这个呢", cfg, quote="（引用 李四 的消息：原唱是谁）")
    sent = calls[-1][-1]["content"]
    check("引用在提问前面", sent.startswith("（引用 李四 的消息：原唱是谁）"), repr(sent[:40]))
    check("提问仍在", "张三：那这个呢" in sent, repr(sent))
    check("引用写进历史", "原唱是谁" in bot._history[8008][0]["content"],
          bot._history[8008][0]["content"])

    # (h) 啥都没有 → empty（调用方据此退回自我介绍）
    bot, calls = make_bot("不该被调用")
    reply, reason = await bot.chat(8009, 1, "张三", "", LLMConfig(cooldown=0))
    check("没文字没图 → empty", reply is None and reason == "empty", f"{reply}/{reason}")
    check("empty 时不发请求", calls == [], str(calls))

    bot, calls = make_bot("不该被调用")
    reply, reason = await bot.chat(
        8010, 1, "张三", "", LLMConfig(cooldown=0, vision=True), images=[]
    )
    check("开了视觉但没图也没字 → empty", reply is None and reason == "empty", f"{reply}/{reason}")

    # (i) 历史里的字符统计不因多模态数组而崩
    bot, _ = make_bot("好")
    cfg = LLMConfig(history_rounds=3, cooldown=0, vision=True)
    await bot.chat(8011, 1, "张三", q, cfg, images=[IMG1])
    stats = bot.history_stats()
    check("history_stats 对多模态不崩", stats[8011]["msgs"] == 2, str(stats.get(8011)))
    check("统计只算文字", stats[8011]["chars"] < 60, str(stats.get(8011)))


# ---------------------------------------------------------------- 6. 配置与表单


def test_config_and_form() -> None:
    print("\n[6] 配置默认值与「机器人」页表单")

    cfg = LLMConfig()
    check("read_reply 默认开", cfg.read_reply is True)
    check("reply_chars 默认 200", cfg.reply_chars == 200)
    check("vision 默认关", cfg.vision is False)
    check("max_images 默认 2", cfg.max_images == 2)

    for key in ("llm.read_reply", "llm.reply_chars", "llm.vision", "llm.max_images"):
        check(f"KEY_INDEX 含 {key}", key in KEY_INDEX)
        desc = KEY_INDEX[key]
        check(f"{key} 有中文标签", bool(desc.get("label")), str(desc))
        check(f"{key} 有说明", bool(desc.get("hint")), str(desc))

    check("vision 在表单里是开关", KEY_INDEX["llm.vision"]["type"] == "bool",
          str(KEY_INDEX["llm.vision"]["type"]))

    fields = {f["key"] for sec in BOT_SCHEMA for f in sec.get("fields", [])}
    for key in ("llm.read_reply", "llm.reply_chars", "llm.vision", "llm.max_images"):
        check(f"机器人页含 {key}", key in fields)

    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    for key in ("read_reply", "reply_chars", "vision", "max_images"):
        check(f"示例配置含 {key}", f"{key}:" in example)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("README 提到看图需要视觉模型", "视觉模型" in readme)
    check("README 有 llm.vision", "llm.vision" in readme)


# ---------------------------------------------------------------- 7. 接线


async def test_wiring() -> None:
    print("\n[7] 接线：纯图片能触发、read_reply 关掉时不读引用")
    import music_collector as mc

    tmp = Path(tempfile.mkdtemp())
    service.store = Store(tmp / "collector.db")
    await service.store.init()
    config_manager.config.intro.cooldown = 0
    config_manager.config.intro.enabled = True

    reset_llm()
    bot_calls: list = []
    stub = make_bot("图我收到了")[0]
    original = stub._post_chat

    async def spy(messages, cfg):
        bot_calls.append([dict(m) for m in messages])
        return await original(messages, cfg)

    stub._post_chat = spy  # type: ignore[method-assign]
    mc.llm_bot = stub

    # (a) 只发一张图、一个字不打 → 也要走进大模型
    sender = StubBot()
    await handle_at(sender, make_event(image_seg(IMG1), user_id=3001))
    check("纯图片能触发大模型", len(sender.sent) == 1 and "图我收到了" in sender.sent[0],
          str(sender.sent))
    check("请求里带 [图片] 标记", "[图片]" in str(bot_calls[-1][-1]["content"]),
          str(bot_calls[-1][-1]["content"]))

    # (b) 引用 → 内容进请求
    bot_calls.clear()
    ev = make_event([seg("text", text="那这个呢")], user_id=3002)
    attach_reply(ev, "原唱到底是谁")
    sender = StubBot()
    await handle_at(sender, ev)
    check("引用内容进请求", "原唱到底是谁" in str(bot_calls[-1][-1]["content"]),
          str(bot_calls[-1][-1]["content"]))

    # (c) 关掉 read_reply → 不读引用
    reset_llm(read_reply=False)
    bot_calls.clear()
    ev = make_event([seg("text", text="那这个呢")], user_id=3003)
    attach_reply(ev, "不该出现的原文")
    sender = StubBot()
    await handle_at(sender, ev)
    check("关闭后引用不进请求", "不该出现的原文" not in str(bot_calls[-1][-1]["content"]),
          str(bot_calls[-1][-1]["content"]))

    # (d) 既没文字也没内容（只有 @）→ 不调大模型
    reset_llm()
    bot_calls.clear()
    sender = StubBot()
    await handle_at(sender, make_event([seg("at", qq="1", name="机器人")], user_id=3004))
    check("空消息不进大模型", bot_calls == [], str(bot_calls))
    check("退回自我介绍", len(sender.sent) == 1 and "群音乐收集助手" in sender.sent[0],
          str(sender.sent))


def _urls_in(content) -> list[str]:
    """从请求内容里抠出所有 image_url（纯字符串时返回空）。"""
    if not isinstance(content, list):
        return []
    return [
        str(p.get("image_url", {}).get("url"))
        for p in content
        if isinstance(p, dict) and p.get("type") == "image_url"
    ]


async def test_quote_images() -> None:
    """引用一张图问「什么意思」——图必须真的发出去（这是线上翻车过的场景）。"""
    print("\n[8] 引用里的图片也要发给模型")
    import music_collector as mc

    tmp = Path(tempfile.mkdtemp())
    service.store = Store(tmp / "collector.db")
    await service.store.init()
    config_manager.config.intro.cooldown = 0
    config_manager.config.intro.enabled = True

    reset_llm(vision=True)
    bot_calls: list = []
    stub = make_bot("看到了")[0]
    original = stub._post_chat

    async def spy(messages, cfg):
        bot_calls.append([dict(m) for m in messages])
        return await original(messages, cfg)

    stub._post_chat = spy  # type: ignore[method-assign]
    mc.llm_bot = stub

    # (a) 引用一张图 + 提问 → 图进请求
    ev = make_event([seg("text", text="什么意思")], user_id=3101)
    attach_reply(ev, Message(image_seg(IMG2)))
    await handle_at(StubBot(), ev)
    urls = _urls_in(bot_calls[-1][-1]["content"])
    check("引用里的图进了请求", urls == [IMG2], str(urls))
    check("前缀里仍有 [图片] 占位", "[图片]" in str(bot_calls[-1][-1]["content"]),
          str(bot_calls[-1][-1]["content"])[:120])

    # (b) 当前消息的图优先占额度（max_images=1 时留当前的、不留引用的）
    reset_llm(vision=True, max_images=1)
    bot_calls.clear()
    ev = make_event([seg("text", text="对比一下"), image_seg(IMG1)], user_id=3102)
    attach_reply(ev, Message(image_seg(IMG2)))
    await handle_at(StubBot(), ev)
    urls = _urls_in(bot_calls[-1][-1]["content"])
    check("当前消息的图优先", urls == [IMG1], str(urls))

    # (c) 额度够时两张都发
    reset_llm(vision=True, max_images=2)
    bot_calls.clear()
    ev = make_event([seg("text", text="对比一下"), image_seg(IMG1)], user_id=3103)
    attach_reply(ev, Message(image_seg(IMG2)))
    await handle_at(StubBot(), ev)
    urls = _urls_in(bot_calls[-1][-1]["content"])
    check("额度够时两张都发", urls == [IMG1, IMG2], str(urls))

    # (d) 同一张图不重复发
    reset_llm(vision=True, max_images=2)
    bot_calls.clear()
    ev = make_event([seg("text", text="就这个"), image_seg(IMG1)], user_id=3104)
    attach_reply(ev, Message(image_seg(IMG1)))
    await handle_at(StubBot(), ev)
    urls = _urls_in(bot_calls[-1][-1]["content"])
    check("重复的图只发一次", urls == [IMG1], str(urls))

    # (e) vision 关掉 → 引用里的图也不发（只留标记）
    reset_llm(vision=False)
    bot_calls.clear()
    ev = make_event([seg("text", text="什么意思")], user_id=3105)
    attach_reply(ev, Message(image_seg(IMG2)))
    await handle_at(StubBot(), ev)
    content = bot_calls[-1][-1]["content"]
    check("vision 关时引用图不发", not _urls_in(content), str(content)[:120])
    check("vision 关时仍能看到 [图片]", "[图片]" in str(content), str(content)[:120])

    # (f) read_reply 关掉 → 引用整体不读，图自然也不发
    reset_llm(vision=True, read_reply=False)
    bot_calls.clear()
    ev = make_event([seg("text", text="什么意思")], user_id=3106)
    attach_reply(ev, Message(image_seg(IMG2)))
    await handle_at(StubBot(), ev)
    content = bot_calls[-1][-1]["content"]
    check("read_reply 关时引用图不发", not _urls_in(content), str(content)[:120])


# ---------------------------------------------------------------- main


async def main() -> None:
    test_read_message()
    test_quote_prefix()
    test_content_helpers()
    test_vision_unsupported()
    await test_chat_vision()
    test_config_and_form()
    await test_wiring()
    await test_quote_images()

    print(f"\n{'=' * 56}")
    print(f"通过 {PASSED} / 失败 {FAILED}")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
