"""``say``（模型主动插话）的离线测试：不发真消息、不联网。

测三件事：
1. 注册契约——只有「开关开着 + 这个上下文真能发出去」时才给模型这个工具；
2. 工具行为——空文本、超长截断、条数上限、发不出去、没有回调；
3. 与慢提示的关系在触发层（`_llm_answer` 里 cancel 计时器），这里只覆盖工具侧。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["MUSIC_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="qcmc-say-")))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import tools as tl  # noqa: E402
from music_collector.config import LLMConfig  # noqa: E402

checks: list[tuple[str, bool, str]] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    checks.append((label, bool(cond), extra))
    print(f"  {'[PASS]' if cond else '[FAIL]'} {label}" + ("" if cond else f"   ← {extra}"))


def names(cfg, group_id=0, note=None):
    return [t.name for t in tl.build(cfg, group_id=group_id, note=note)[0]]


async def main() -> int:
    print("== 配置默认值 ==")
    cfg = LLMConfig()
    check("say_tool 默认开", cfg.say_tool is True, str(cfg.say_tool))
    check("say_max 默认 1", int(cfg.say_max) == 1, str(cfg.say_max))
    check("SAY_MAX_CHARS 是 60", tl.SAY_MAX_CHARS == 60, str(tl.SAY_MAX_CHARS))

    print("== 注册契约 ==")
    sent: list[str] = []

    async def note(text: str) -> bool:
        sent.append(text)
        return True

    check("say_tool=False → 没有 say", "say" not in names(LLMConfig(say_tool=False), note=note))
    check("say_max=0 → 没有 say（等于关掉）", "say" not in names(LLMConfig(say_max=0), note=note))
    check("没有 note 回调（网页测试/离线）→ 没有 say",
          "say" not in names(LLMConfig(), note=None))
    check("总闸 tools=False → 什么都没有", names(LLMConfig(tools=False), note=note) == [])
    check("正常情况有 say", "say" in names(LLMConfig(), note=note))
    check("say 在 ALLOWED_TOOL_NAMES 里", "say" in tl.ALLOWED_TOOL_NAMES)

    print("== 工具行为（走 dispatch，和线上同一条路）==")
    cfg1 = LLMConfig(tools=True, say_tool=True, say_max=1, web_tool=False)
    tools, ctx = tl.build(cfg1, group_id=123, note=note)

    out = await tl.dispatch(tools, "say", {"text": "  我去翻一下资料，稍等～  "}, ctx)
    check("发出去了一条", len(sent) == 1, str(sent))
    check("首尾空白被清掉", sent and sent[0] == "我去翻一下资料，稍等～", str(sent[:1]))
    check("回填里告诉模型『已经发出去了』", "已经发出去" in out, out[:60])
    check("用满 1 次后 note_used=1", ctx.note_used == 1, str(ctx.note_used))

    sent.clear()
    out = await tl.dispatch(tools, "say", {"text": "再说一句"}, ctx)
    check("超过上限时不再发（say_max=1）", sent == [], str(sent))
    check("并且明确告诉模型别再发", "上限" in out or "别再发" in out, out[:60])

    print("== 空 / 超长 / 没有回调 ==")
    tools2, ctx2 = tl.build(LLMConfig(tools=True, say_tool=True, say_max=3, web_tool=False),
                            group_id=123, note=note)
    sent.clear()
    out = await tl.dispatch(tools2, "say", {"text": "   "}, ctx2)
    check("空文本被拒", sent == [] and "不能为空" in out, out[:60])

    sent.clear()
    long_text = "很长的一句话" * 30
    out = await tl.dispatch(tools2, "say", {"text": long_text}, ctx2)
    check("超长被截断到 60 字（含省略号）",
          bool(sent) and len(sent[0]) <= tl.SAY_MAX_CHARS + 1, str(len(sent[0]) if sent else -1))
    check("截断后仍算成功", "已经发出去" in out, out[:60])

    print("== 发送失败时不许重试 ==")
    async def fail_note(text: str) -> bool:
        return False

    tools3, ctx3 = tl.build(LLMConfig(tools=True, say_tool=True, say_max=3, web_tool=False),
                            group_id=123, note=fail_note)
    out = await tl.dispatch(tools3, "say", {"text": "试着说一句"}, ctx3)
    check("发不出去时回一句『别重试』", "没发出去" in out and "别" in out, out[:60])
    check("失败不消耗配额", ctx3.note_used == 0, str(ctx3.note_used))

    print("== 没有回调的上下文里调用（防御性）==")
    tools4, ctx4 = tl.build(LLMConfig(tools=True, say_tool=True, say_max=3, web_tool=False),
                            group_id=123, note=None)
    out = await tl.dispatch(tools4, "say", {"text": "喂"}, ctx4)
    check("没注册时 dispatch 回「没有这个工具」（第一道闸）", "没有这个工具" in out, out[:60])
    # 直接打 handler：就算以后有人绕过 build 直接塞工具进来，也不能崩
    out = await tl._handle_say({"text": "喂"}, ctx4)
    check("handler 自己也有兜底：没有回调时优雅拒绝", "没法主动发消息" in out, out[:60])

    print()
    bad = [c for c in checks if not c[1]]
    if bad:
        print(f"失败 {len(bad)} 项：")
        for label, _, extra in bad:
            print(f"  - {label} {extra}")
        return 1
    print(f"通过 {len(checks)} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
