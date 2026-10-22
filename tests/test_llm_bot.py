"""「@机器人 → 大模型回复」的回归测试。

覆盖：
1. ``normalize_endpoint``：各种 base_url 写法都能补成 chat/completions；
2. ``enabled_for``：开关 / 必填项 / 群白名单（空=所有群）；
3. ``LLMBot.chat``：成功、失败、冷却、字数截断、上下文累积与轮数上限、
   ``history_rounds=0`` 时不记上下文；失败**不写**上下文（否则会重复问同一句）；
4. 配置约束：``llm.*`` 是进程级（不能按群覆盖）、配置页不含该段、「机器人」页单独承载；
   ``api_key`` 在日志里打码，而 ``max_tokens`` 不能被 "token" 误伤；
5. ``handle_at`` 的分流：大模型成功就不发自我介绍；关闭/群不匹配 → 回自我介绍；
   ``/music`` 命令两个都不发。

运行: MUSIC_DATA_DIR=$(mktemp -d) python tests/test_llm_bot.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message  # noqa: E402
from nonebot.adapters.onebot.v11.event import Sender  # noqa: E402

from music_collector import handle_at  # noqa: E402
from music_collector.config import (  # noqa: E402
    LLMConfig,
    _mask_value,
    apply_config_value,
    config_manager,
    group_overrides,
    is_group_scopable,
)
from music_collector.llm import (  # noqa: E402
    LLMBot,
    build_payload,
    enabled_for,
    extract_reply,
    guess_thinking_style,
    is_reasoning_model,
    normalize_endpoint,
    parse_extra_body,
    payload_preview,
    resolve_thinking_style,
    thinking_budget,
)
from music_collector.service import service  # noqa: E402
from music_collector.store import Store  # noqa: E402
from music_collector.webui import BOT_SCHEMA, CONFIG_SCHEMA, KEY_INDEX  # noqa: E402

PASSED = 0
FAILED = 0

GID = 8101


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


# ---------------------------------------------------------------- 替身


class StubBot:
    """只实现 ``send_group_msg``，把发出去的消息收集起来。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_group_msg(self, group_id: int, message) -> None:
        self.sent.append(str(message))


def make_event(text: str, *, user_id: int = 42, group_id: int = GID) -> GroupMessageEvent:
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


def make_bot(stub_reply=None, *, error: str = "", calls: list | None = None) -> LLMBot:
    """造一个 LLMBot，``_post_chat`` 用固定回复 / 固定异常替换掉。"""
    bot = LLMBot()
    record = calls if calls is not None else []

    async def fake_post(messages, cfg):
        record.append([dict(m) for m in messages])
        if error:
            raise RuntimeError(error)
        return stub_reply

    bot._post_chat = fake_post  # type: ignore[method-assign]
    return bot


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
    cfg.cooldown = 0
    cfg.max_input_chars = 500
    cfg.max_reply_chars = 800
    cfg.at_sender = False
    cfg.fallback_intro = True
    for k, v in over.items():
        setattr(cfg, k, v)


# ---------------------------------------------------------------- 1. URL 归一


def test_normalize_endpoint() -> None:
    print("\n[1] normalize_endpoint")
    cases = {
        "https://api.openai.com/v1": "https://api.openai.com/v1/chat/completions",
        "https://api.openai.com/v1/": "https://api.openai.com/v1/chat/completions",
        "https://api.openai.com": "https://api.openai.com/v1/chat/completions",
        "https://api.deepseek.com/v1": "https://api.deepseek.com/v1/chat/completions",
        "https://x.com/api/v3": "https://x.com/api/v3/chat/completions",
        "https://x.com/v1/chat/completions": "https://x.com/v1/chat/completions",
        "": "",
        "   ": "",
    }
    for raw, want in cases.items():
        got = normalize_endpoint(raw)
        check(f"{raw!r} -> {want}", got == want, f"got={got!r}")


# ---------------------------------------------------------------- 2. 启用判定


def test_enabled_for() -> None:
    print("\n[2] enabled_for（开关 / 必填 / 群白名单）")
    cfg = config_manager.config.llm
    reset_llm()
    check("填全 + 开启 → 所有群都算启用", enabled_for(cfg, 123) and enabled_for(cfg, 456))
    cfg.groups = [123]
    check("白名单命中 → 启用", enabled_for(cfg, 123))
    check("白名单未命中 → 不启用", not enabled_for(cfg, 456))
    cfg.groups = []
    cfg.enabled = False
    check("总开关关掉 → 不启用", not enabled_for(cfg, 123))
    cfg.enabled = True
    cfg.api_key = "  "
    check("Key 为空 → 不启用（避免无谓报错）", not enabled_for(cfg, 123))
    cfg.api_key = "sk-test"
    cfg.model = ""
    check("模型名为空 → 不启用", not enabled_for(cfg, 123))


# ---------------------------------------------------------------- 3. chat


async def test_chat_success_and_history() -> None:
    print("\n[3] chat 成功 + 上下文")
    reset_llm()
    cfg = config_manager.config.llm
    calls: list = []
    bot = make_bot("你好呀", calls=calls)
    reply, reason = await bot.chat(GID, 42, "张三", "  在吗  ", cfg)
    check("返回回复文本", reply == "你好呀", repr(reply))
    check("reason 为空", reason == "", repr(reason))
    sent = calls[0]
    check("首条是 system", sent[0]["role"] == "system" and sent[0]["content"] == "你是助手")
    check("末条是 user 且带昵称", sent[-1]["role"] == "user" and sent[-1]["content"] == "张三：在吗",
          repr(sent[-1]))
    check("历史记了 2 条（问 + 答）", len(bot.history(GID)) == 2)
    check("history_sizes 有该群", bot.history_sizes().get(GID) == 2)

    # 第二轮：应带上刚才的问答
    await bot.chat(GID, 42, "张三", "第二句", cfg)
    second = calls[1]
    check("第二轮带上下文（system + 2 历史 + 本轮 = 4 条）", len(second) == 4, str(len(second)))
    check("历史顺序正确", second[1]["content"] == "张三：在吗" and second[2]["content"] == "你好呀")

    # 轮数上限：history_rounds=1 时只留最后一轮
    cfg.history_rounds = 1
    for i in range(3):
        await bot.chat(GID, 42, "张三", f"第{i}句", cfg)
    check("history_rounds=1 只留 2 条", len(bot.history(GID)) == 2, str(bot.history(GID)))


async def test_chat_history_off() -> None:
    print("\n[4] history_rounds=0：不带也不记")
    reset_llm(history_rounds=0)
    cfg = config_manager.config.llm
    calls: list = []
    bot = make_bot("好", calls=calls)
    await bot.chat(GID, 42, "张三", "在吗", cfg)
    check("messages 只有 system + user", len(calls[0]) == 2, str(len(calls[0])))
    check("不记上下文", bot.history(GID) == [])


async def test_chat_failure() -> None:
    print("\n[5] chat 失败：返回原因且不污染上下文")
    reset_llm()
    cfg = config_manager.config.llm
    bot = make_bot(error="HTTP 401: invalid api key")
    reply, reason = await bot.chat(GID, 42, "张三", "在吗", cfg)
    check("失败时没有回复", reply is None)
    check("reason 带 error:", reason.startswith("error:"), reason)
    check("原样带出底层原因", "401" in reason, reason)
    check("失败不写上下文", bot.history(GID) == [])
    check("last_error 记录原因", "401" in bot.last_error, bot.last_error)


async def test_chat_cooldown() -> None:
    print("\n[6] 冷却")
    reset_llm(cooldown=60)
    cfg = config_manager.config.llm
    bot = make_bot("第一次")
    r1, _ = await bot.chat(GID, 42, "张三", "在吗", cfg)
    check("第一次正常回", r1 == "第一次")
    r2, reason = await bot.chat(GID, 42, "张三", "又问", cfg)
    check("同一用户立刻再问 → 冷却", r2 is None and reason == "cooldown", reason)
    r3, _ = await bot.chat(GID, 43, "李四", "我问问", cfg)
    check("换个人不受影响", r3 == "第一次")


async def test_chat_truncation() -> None:
    print("\n[7] 字数截断")
    reset_llm(max_input_chars=5, max_reply_chars=4)
    cfg = config_manager.config.llm
    calls: list = []
    bot = make_bot("一二三四五六七八", calls=calls)
    reply, _ = await bot.chat(GID, 42, "张三", "一二三四五六七八九十", cfg)
    sent_user = calls[0][-1]["content"]
    check("提问被截断到 5 字 + 省略号", sent_user.endswith("一二三四五…"), sent_user)
    check("回复被截断到 4 字 + 省略号", reply == "一二三四…", repr(reply))


async def test_chat_empty_and_clear() -> None:
    print("\n[8] 空提问 / 清空上下文")
    reset_llm()
    cfg = config_manager.config.llm
    bot = make_bot("x")
    reply, reason = await bot.chat(GID, 42, "张三", "   ", cfg)
    check("空提问 → empty", reply is None and reason == "empty", reason)
    await bot.chat(GID, 42, "张三", "在吗", cfg)
    check("清空指定群", bot.clear_history(GID) == 1 and bot.history(GID) == [])
    check("清空不存在的群返回 0", bot.clear_history(GID) == 0)
    await bot.chat(GID, 42, "张三", "在吗", cfg)
    check("清空全部", bot.clear_history(None) == 1 and bot.history_sizes() == {})


# ---------------------------------------------------------------- 9. 配置约束


def test_config_and_schema() -> None:
    print("\n[9] 配置约束（只在网页改 / 不能按群覆盖 / 打码）")
    check("llm.enabled 不能按群覆盖", not is_group_scopable("llm.enabled"))
    check("llm.api_key 不能按群覆盖", not is_group_scopable("llm.api_key"))
    check("playlist.seq 仍然可以按群覆盖", is_group_scopable("playlist.seq"))

    reset_llm()
    config_manager.config.llm.model = "global-model"
    # 即使调用方带了 group_id，也必须落到全局层
    layer = apply_config_value(555, "llm.model", "from-group-attempt")
    check("带 group_id 写 llm.* 仍落全局", layer == "global", layer)
    check("全局值被写入", config_manager.config.llm.model == "from-group-attempt")
    check("没有产生按群覆盖", "llm.model" not in group_overrides.get(555))
    config_manager.config.llm.model = "test-model"

    check("配置页不含 llm 段", all(s["key"] != "llm" for s in CONFIG_SCHEMA))
    check("「机器人」页只含 llm 段", [s["key"] for s in BOT_SCHEMA] == ["llm"])
    llm_keys = [k for k in KEY_INDEX if k.startswith("llm.")]
    check("PATCH 仍认得 llm.*（KEY_INDEX 完整）", len(llm_keys) >= 14, str(len(llm_keys)))
    check("api_key 在表单里是密码框", KEY_INDEX["llm.api_key"]["secret"] is True)
    check("api_key 写日志时打码", _mask_value("llm.api_key", "sk-123456") == "******")
    check("max_tokens 不被 token 误伤", _mask_value("llm.max_tokens", 512) == "512",
          _mask_value("llm.max_tokens", 512))


# ---------------------------------------------------------------- 10. 采样 / 思考


def test_sampling_and_thinking() -> None:
    print("\n[10] 采样参数与思考模式")
    msgs = [{"role": "user", "content": "hi"}]

    # (a) 默认：不干预
    p = build_payload(msgs, LLMConfig())
    check("默认不带任何思考参数",
          not ({"reasoning_effort", "enable_thinking", "thinking"} & set(p)), str(p))
    check("默认下发温度与 top_p", p.get("temperature") == 0.7 and p.get("top_p") == 1.0, str(p))
    check("默认不发 seed / stop", "seed" not in p and "stop" not in p, str(p))
    check("0 值惩罚项不发（少发无用参数）",
          "presence_penalty" not in p and "frequency_penalty" not in p, str(p))

    # (b) 推理模型：采样参数一律省掉，否则可能 400
    for name in ("o3-mini", "gpt-5", "deepseek-reasoner", "Qwen3-32B-thinking"):
        model_p = build_payload(msgs, LLMConfig(model=name))
        check(f"{name} 不送 temperature/top_p",
              "temperature" not in model_p and "top_p" not in model_p, str(model_p))
    check("普通模型不会被误判成推理模型", is_reasoning_model("gpt-4o-mini") is False)

    # (c) 写法推断
    for url, model, want in (
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus", "enable_thinking"),
        ("https://api.openai.com/v1", "gpt-4o-mini", "reasoning_effort"),
        ("https://open.bigmodel.cn/api/paas/v4", "glm-4-plus", "thinking_object"),
        ("https://api.deepseek.com/v1", "deepseek-chat", "none"),
        ("https://my-proxy.example.com/v1", "o1-mini", "reasoning_effort"),
    ):
        got = resolve_thinking_style(LLMConfig(base_url=url, model=model))
        check(f"{model} 推断为 {want}", got == want, got)
    check("手选写法时不再推断",
          resolve_thinking_style(LLMConfig(thinking_style="none", model="qwen-plus")) == "none")

    # (d) 档位 -> 预算
    check("auto / off 不给预算",
          thinking_budget(LLMConfig()) == 0 and thinking_budget(LLMConfig(thinking="off")) == 0)
    check("low 默认 1024", thinking_budget(LLMConfig(thinking="low", max_tokens=8000)) == 1024)
    check("medium 默认 4096",
          thinking_budget(LLMConfig(thinking="medium", max_tokens=8000)) == 4096)
    check("high 默认 12000",
          thinking_budget(LLMConfig(thinking="high", max_tokens=32000)) == 12000)
    check("预算被 max_tokens-256 压住",
          thinking_budget(LLMConfig(thinking="high", max_tokens=1000)) == 744,
          str(thinking_budget(LLMConfig(thinking="high", max_tokens=1000))))
    check("手填预算优先于档位默认",
          thinking_budget(LLMConfig(thinking="low", thinking_budget=777, max_tokens=8000)) == 777)

    # (e) enable_thinking 写法（通义 / 百炼）
    p = build_payload(msgs, LLMConfig(thinking="medium", thinking_style="enable_thinking",
                                     max_tokens=8000))
    check("enable_thinking=True + 预算 4096",
          p.get("enable_thinking") is True and p.get("thinking_budget") == 4096, str(p))
    p = build_payload(msgs, LLMConfig(thinking="off", thinking_style="enable_thinking"))
    check("enable_thinking=False 才是真关",
          p.get("enable_thinking") is False and "thinking_budget" not in p, str(p))

    # (f) thinking 对象写法（GLM / Claude / OpenRouter）
    p = build_payload(msgs, LLMConfig(thinking="high", thinking_style="thinking_object",
                                     max_tokens=20000))
    check("thinking 对象带 budget_tokens",
          p.get("thinking") == {"type": "enabled", "budget_tokens": 12000}, str(p))
    p = build_payload(msgs, LLMConfig(thinking="off", thinking_style="thinking_object"))
    check("thinking 对象可显式关闭", p.get("thinking") == {"type": "disabled"}, str(p))

    # (g) reasoning_effort 写法（OpenAI / o 系列）
    p = build_payload(msgs, LLMConfig(thinking="high", thinking_style="reasoning_effort"))
    check("reasoning_effort=high", p.get("reasoning_effort") == "high", str(p))
    p = build_payload(msgs, LLMConfig(thinking="off", thinking_style="reasoning_effort"))
    check("OpenAI 系没有“关闭”，off 时不发参数", "reasoning_effort" not in p, str(p))
    p = build_payload(msgs, LLMConfig(thinking="medium", thinking_style="none"))
    check("style=none 一律不发思考参数",
          not ({"reasoning_effort", "enable_thinking", "thinking"} & set(p)), str(p))

    # (h) extra_body 兜底：能覆盖同名项，也能塞任意私有参数
    p = build_payload(msgs, LLMConfig(extra_body='{"temperature": 0.11, "top_k": 5}'))
    check("extra_body 覆盖同名项", p.get("temperature") == 0.11 and p.get("top_k") == 5, str(p))
    p = build_payload(msgs, LLMConfig(thinking="off",
                                     extra_body='{"thinking": {"type": "enabled"}}'))
    check("extra_body 能盖掉思考参数", p.get("thinking") == {"type": "enabled"}, str(p))
    check("空 extra_body 不报错", parse_extra_body("   ") == {})
    for bad, why in (("{oops", "JSON"), ("[1,2]", "对象")):
        try:
            parse_extra_body(bad)
            msg = "没有报错"
        except RuntimeError as exc:
            msg = str(exc)
        check(f"非法 extra_body 报错（{bad} → 提示含「{why}」）", why in msg, msg)

    # (i) seed / stop / 惩罚
    p = build_payload(msgs, LLMConfig(seed=42, stop=["。", " "], presence_penalty=0.5,
                                     frequency_penalty=-0.3))
    check("seed 下发", p.get("seed") == 42, str(p))
    check("stop 丢掉空白项", p.get("stop") == ["。"], str(p))
    check("惩罚项非 0 才发",
          p.get("presence_penalty") == 0.5 and p.get("frequency_penalty") == -0.3, str(p))

    # (j) 预览：网页端「实际请求参数」
    prev = payload_preview(LLMConfig(thinking="low", thinking_style="enable_thinking",
                                     max_tokens=4000))
    check("预览不含 messages", "messages" not in prev and prev.get("enable_thinking") is True, str(prev))
    check("预览不泄露 Key", "api_key" not in prev and "Authorization" not in prev, str(prev))
    bad_prev = payload_preview(LLMConfig(extra_body="{oops"))
    check("预览遇到非法 JSON 也不炸，返回 error", "error" in bad_prev, str(bad_prev))

    # (k) 响应解析：思考模型「只想不说」要给出能懂的原因
    check("取纯文本", extract_reply({"choices": [{"message": {"content": "你好"}}]}) == "你好")
    check("分段 content 拼接",
          extract_reply({"choices": [{"message": {"content": [{"text": "ab"}, {"text": "cd"}]}}]}) == "abcd")
    for data, kw in (
        ({"choices": [{"message": {"content": "", "reasoning_content": "想了很久"}}]}, "思考内容"),
        ({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}, "截断"),
        ({"error": "invalid api key"}, "choices"),
    ):
        try:
            extract_reply(data)
            msg = "没有报错"
        except RuntimeError as exc:
            msg = str(exc)
        check(f"错误提示含「{kw}」", kw in msg, msg)

    # (l) 表单 / 状态
    for key in ("llm.top_p", "llm.presence_penalty", "llm.frequency_penalty", "llm.stop",
                "llm.seed", "llm.thinking", "llm.thinking_style", "llm.thinking_budget",
                "llm.extra_body"):
        check(f"表单含 {key}", key in KEY_INDEX)
    th = KEY_INDEX["llm.thinking"]
    check("思考模式是下拉框且含 5 档",
          th["type"] == "enum" and set(th["enum"] or []) == {"auto", "off", "low", "medium", "high"},
          str(th))
    check("extra_body 是多行输入", KEY_INDEX["llm.extra_body"]["multiline"] is True)
    check("extra_body 不当成密码框", KEY_INDEX["llm.extra_body"]["secret"] is False)
    check("思考预算不被 token 误伤", _mask_value("llm.thinking_budget", 4096) == "4096")


# ---------------------------------------------------------------- 11. 分流


async def test_handle_at_routing() -> None:
    print("\n[11] handle_at 分流：大模型优先，兜底自我介绍")
    import music_collector as mc

    tmp = Path(tempfile.mkdtemp())
    service.store = Store(tmp / "collector.db")
    await service.store.init()
    # 自我介绍有按群冷却，这组用例全在同一个群里跑，关掉冷却免得互相干扰
    config_manager.config.intro.cooldown = 0
    config_manager.config.intro.enabled = True
    config_manager.config.intro.skip_commands = True

    reset_llm()
    cfg = config_manager.config.llm

    # (a) 大模型成功 → 只发一条，内容是模型回复
    stub = make_bot("我是大模型的回答")
    mc.llm_bot = stub
    bot = StubBot()
    await handle_at(bot, make_event("在吗", user_id=1001))
    check("大模型成功只发 1 条", len(bot.sent) == 1, str(bot.sent))
    check("内容是模型回复", "我是大模型的回答" in (bot.sent[0] if bot.sent else ""), str(bot.sent))
    check("没有混进自我介绍", all("群音乐收集助手" not in s for s in bot.sent))

    # (b) 关闭大模型 → 回自我介绍
    reset_llm(enabled=False)
    bot = StubBot()
    await handle_at(bot, make_event("在吗", user_id=1002))
    check("关闭时回自我介绍", len(bot.sent) == 1 and "群音乐收集助手" in bot.sent[0], str(bot.sent))

    # (c) 群不在白名单 → 回自我介绍
    reset_llm()
    cfg.groups = [999999]
    bot = StubBot()
    await handle_at(bot, make_event("在吗", user_id=1003))
    check("群不在白名单 → 自我介绍", len(bot.sent) == 1 and "群音乐收集助手" in bot.sent[0],
          str(bot.sent))

    # (d) 调用失败 + fallback_intro → 回自我介绍（而不是静默）
    reset_llm()
    cfg.groups = []
    mc.llm_bot = make_bot(error="HTTP 500: boom")
    bot = StubBot()
    await handle_at(bot, make_event("在吗", user_id=1004))
    check("失败且允许兜底 → 自我介绍", len(bot.sent) == 1 and "群音乐收集助手" in bot.sent[0],
          str(bot.sent))

    # (e) 调用失败 + 不兜底 → 完全静默
    reset_llm(fallback_intro=False)
    bot = StubBot()
    await handle_at(bot, make_event("在吗", user_id=1005))
    check("失败且不兜底 → 静默", bot.sent == [], str(bot.sent))

    # (f) 冷却中 → 静默（不能再用自我介绍补一条）
    reset_llm(cooldown=60)
    stub = make_bot("第一次")
    mc.llm_bot = stub
    bot = StubBot()
    await handle_at(bot, make_event("在吗", user_id=1006))
    first = len(bot.sent)
    await handle_at(bot, make_event("再问一次", user_id=1006))
    check("同用户第二次被冷却 → 不追加消息", len(bot.sent) == first == 1, str(bot.sent))

    # (g) /music 命令：大模型与自我介绍都不发（intro.skip_commands 默认开）
    reset_llm()
    mc.llm_bot = make_bot("不该出现")
    bot = StubBot()
    await handle_at(bot, make_event("/music help", user_id=1007))
    check("/music 命令不触发大模型/自我介绍", bot.sent == [], str(bot.sent))

    # (h) 只有 @ 没文字 → 回自我介绍
    reset_llm()
    bot = StubBot()
    await handle_at(bot, make_event("", user_id=1008))
    check("空文本 → 自我介绍", len(bot.sent) == 1 and "群音乐收集助手" in bot.sent[0],
          str(bot.sent))


async def main() -> None:
    test_normalize_endpoint()
    test_enabled_for()
    await test_chat_success_and_history()
    await test_chat_history_off()
    await test_chat_failure()
    await test_chat_cooldown()
    await test_chat_truncation()
    await test_chat_empty_and_clear()
    test_config_and_schema()
    test_sampling_and_thinking()
    await test_handle_at_routing()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
