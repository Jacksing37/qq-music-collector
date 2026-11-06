"""「还在查」慢提示的离线测试：只测行为，不联网、不发真消息。

被测对象是 ``music_collector._slow_hint``（`__init__.py`）——模型跑太久时先发一句安抚。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["MUSIC_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="qcmc-hint-")))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

import music_collector as mc  # noqa: E402
from music_collector.config import LLMConfig  # noqa: E402

checks: list[tuple[str, bool, str]] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    checks.append((label, bool(cond), extra))
    print(f"  {'[PASS]' if cond else '[FAIL]'} {label}" + ("" if cond else f"   ← {extra}"))


class FakeBot:
    """只实现 safe_send_group 用到的那个方法。"""

    def __init__(self, fail: bool = False):
        self.sent: list[tuple[int, object]] = []
        self.fail = fail

    async def send_group_msg(self, group_id: int, message):
        if self.fail:
            raise RuntimeError("模拟发送失败")
        self.sent.append((group_id, message))
        return {"message_id": 1}


async def run_hint(bot, group_id, seconds, text="", most=1, cancel_after=None):
    cfg = LLMConfig(tools=True, slow_hint_seconds=seconds, slow_hint_text=text,
                    slow_hint_max=most)
    task = asyncio.create_task(mc._slow_hint(bot, group_id, cfg))
    if cancel_after is not None:
        await asyncio.sleep(cancel_after)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    else:
        await task
    return bot.sent


async def main() -> int:
    print("== 配置默认值 ==")
    cfg = LLMConfig()
    check("slow_hint_seconds 默认 12", float(cfg.slow_hint_seconds) == 12.0, str(cfg.slow_hint_seconds))
    check("slow_hint_text 默认空（用内置那句）", str(cfg.slow_hint_text) == "", repr(cfg.slow_hint_text))
    check("slow_hint_max 默认 1", int(cfg.slow_hint_max) == 1, str(cfg.slow_hint_max))
    check("内置提示语非空", bool(mc.SLOW_HINT_DEFAULT.strip()), mc.SLOW_HINT_DEFAULT)

    print("== 0 秒 = 完全关掉 ==")
    bot = FakeBot()
    sent = await run_hint(bot, 1053096807, 0.0)
    check("seconds=0 时一条都不发", sent == [], str(sent))

    print("== 超时后发一条（用内置文案）==")
    bot = FakeBot()
    sent = await run_hint(bot, 1053096807, 0.05)
    check("发且只发 1 条", len(sent) == 1, str(len(sent)))
    check("发到正确的群", sent and sent[0][0] == 1053096807, str(sent[:1]))
    check("用的是内置那句", sent and str(sent[0][1]) == mc.SLOW_HINT_DEFAULT, str(sent[:1]))

    print("== 自定义文案 ==")
    bot = FakeBot()
    sent = await run_hint(bot, 123, 0.05, text="  稍等，正在翻资料呢  ")
    check("自定义文案生效且去空白", sent and str(sent[0][1]) == "稍等，正在翻资料呢", str(sent[:1]))

    print("== 最多几条 ==")
    bot = FakeBot()
    sent = await run_hint(bot, 123, 0.03, most=3)
    check("slow_hint_max=3 发 3 条", len(sent) == 3, str(len(sent)))

    print("== 快速返回时不该发（模拟调用方 cancel）==")
    bot = FakeBot()
    sent = await run_hint(bot, 123, 0.5, cancel_after=0.05)
    check("计时器被取消 → 一条都不发", sent == [], str(sent))

    print("== 没有群号（私聊 / 戳一戳）不发 ==")
    bot = FakeBot()
    sent = await run_hint(bot, 0, 0.03)
    check("group_id=0 时一条都不发", sent == [], str(sent))

    print("== 发送失败就收手，不重试刷屏 ==")
    bot = FakeBot(fail=True)
    sent = await run_hint(bot, 123, 0.03, most=3)
    check("发送失败时不抛异常且不再重试", sent == [], str(sent))

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
