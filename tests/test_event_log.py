"""业务事件日志测试。

需求背景：网页端「日志」页以前只能看到协议端（``nonebot`` / OneBot 适配器）与
uvicorn 的访问日志在刷屏，**看不到机器人自己在干什么**。三处根因：

1. 插件里 archiver / service / config / detector / cache / netease_api 用的是
   ``logging.getLogger("music_collector.xxx")``，而 nonebot **没有全局的
   stdlib→loguru 桥**（只给 uvicorn / apscheduler 各自接了），stdlib root 默认又是
   WARNING，于是这些 INFO 日志既到不了 loguru、也进不了内存缓冲。→ ``logbuffer``
   现在自己装一个桥（幂等）。
2. 改配置没有留痕。→ ``config_manager.update`` 统一记一条「改了哪个键、从什么变成什么」，
   密码类脱敏、值没变不记。
3. 收发消息没有留痕。→ ``bot_utils.message_preview`` / ``trace_out`` 把消息压成一行，
   受 ``logs.trace_messages`` 开关控制。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_event_log.py
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from nonebot.adapters.onebot.v11 import Message, MessageSegment  # noqa: E402

from music_collector import logbuffer  # noqa: E402
from music_collector.bot_utils import message_preview, trace_out  # noqa: E402
from music_collector.config import config_manager  # noqa: E402
from music_collector.service import service  # noqa: E402

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


def _fresh_config():
    """把配置指到临时目录并加载，避免碰到真实的 data/config.yaml。"""
    tmp = Path(tempfile.mkdtemp())
    config_manager.path = tmp / "config.yaml"
    config_manager.load()
    return tmp


def _messages() -> list[str]:
    return [i["message"] for i in logbuffer.buffer.snapshot()]


# ------------------------------------------------------- 1. stdlib 桥接


def test_stdlib_bridge_captures_plugin_logging():
    print("\n[A] stdlib logging 的日志能进缓冲（否则看不到机器人在干什么）")
    logbuffer.uninstall()
    logbuffer.buffer.clear()
    logbuffer.install(lines=500, level="INFO")

    check("桥已装上", logbuffer.stdlib_bridge_installed())
    check("重复安装不再装第二个 handler", logbuffer._install_stdlib_bridge() is False)

    logbuffer.buffer.clear()
    # archiver / service / config 这些模块就是这么写日志的
    logging.getLogger("music_collector.archiver").info("[music] 归档-marker")
    logging.getLogger("music_collector.service").warning("[music] 警告-marker")

    msgs = _messages()
    check("INFO 日志被捕获", any("归档-marker" in m for m in msgs), str(msgs))
    check("WARNING 日志被捕获", any("警告-marker" in m for m in msgs), str(msgs))
    # 日志页「只看机器人」是按 logger 名筛选的：nonebot 的 _log_patcher 会把
    # music_collector.archiver 这类模块名归一成插件包名，所以 BOT_LOGGER 必须等于包名。
    # （本文件里调用 logging 的是 __main__，所以这里只校验包名这个不变量。）
    import music_collector

    check("BOT_LOGGER 就是插件包名", logbuffer.BOT_LOGGER == music_collector.__name__,
          logbuffer.BOT_LOGGER)
    names = {i["name"] for i in logbuffer.buffer.snapshot()}
    check("logger 名不为空", all(names), str(names))
    # 同一条日志只能出现一次（propagate=False，没有二次转发）
    check("没有重复记录", len([m for m in msgs if "归档-marker" in m]) == 1, str(msgs))


# ------------------------------------------------------- 2. 配置变更留痕


def test_config_update_is_logged_and_masked():
    print("\n[B] 改设置留痕：记到日志页，且密码类脱敏")
    _fresh_config()
    logbuffer.buffer.clear()

    config_manager.update("playlist.seq", 123)
    config_manager.update("netease.password_md5", "a" * 32)
    config_manager.update("netease.auto_relogin", False)

    msgs = _messages()
    joined = "\n".join(msgs)
    check("期号修改被记录", "playlist.seq" in joined and "123" in joined, joined)
    check("布尔值可读（不是 True/False）", "auto_relogin: true → false" in joined, joined)
    check("密码类打码", "******" in joined and "a" * 32 not in joined, joined)
    check("普通键不被打码", "playlist.seq: " in joined and "******" not in joined.split("playlist.seq")[1][:20], joined)


def test_config_update_skips_unchanged():
    print("\n[C] 值没变就不记（避免刷屏）")
    _fresh_config()
    logbuffer.buffer.clear()

    config_manager.update("playlist.seq", 55)
    config_manager.update("playlist.seq", 55)      # 第二次同值
    hits = [m for m in _messages() if "playlist.seq" in m]
    check("同值只记一次", len(hits) == 1, str(hits))


# ------------------------------------------------------- 3. 收发消息留痕


def test_message_preview():
    print("\n[D] 消息摘要：文本 + 非文本段标记，长文本截断")
    check("纯文本", message_preview("你好") == "你好", message_preview("你好"))
    check("折行压平", message_preview("第一行\n第二行") == "第一行 第二行",
          message_preview("第一行\n第二行"))

    msg = Message(
        MessageSegment.at(123456)
        + MessageSegment.text("看看这首")
        + MessageSegment.image("https://x/y.jpg")
    )
    preview = message_preview(msg)
    check("带 @ 与图片标记", preview.startswith("[@][图片]"), preview)
    check("保留正文", "看看这首" in preview, preview)

    check("空消息有兜底", message_preview("") == "（空消息）", message_preview(""))
    long_preview = message_preview("字" * 500)
    check("超长截断", len(long_preview) <= 160 and long_preview.endswith("…"), str(len(long_preview)))


def test_trace_out_respects_switch():
    print("\n[E] 收发消息留痕受 logs.trace_messages 开关控制")
    _fresh_config()
    logbuffer.buffer.clear()

    config_manager.config.logs.trace_messages = True
    trace_out("收录 1 首", "群100")
    check("开启时记录发出的消息", any("→ 群100 收录 1 首" in m for m in _messages()),
          str(_messages()))

    logbuffer.buffer.clear()
    config_manager.config.logs.trace_messages = False
    trace_out("不该出现", "群100")
    check("关闭后不再记录", _messages() == [], str(_messages()))

    config_manager.config.logs.trace_messages = True   # 复位，别影响其它用例


def test_schema_and_config_example_expose_switch():
    print("\n[F] 开关在配置页与模板里都有，且带中文标签")
    from music_collector import webui

    keys = {f["key"] for s in webui.SCHEMA for f in s["fields"]}
    check("schema 里有 logs.trace_messages", "logs.trace_messages" in keys)
    label = webui.KEY_INDEX["logs.trace_messages"]["label"]
    check("有中文标签", any("\u4e00" <= ch <= "\u9fff" for ch in label), label)

    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    check("config.example.yaml 里有该项", "trace_messages:" in example)


def main() -> None:
    test_stdlib_bridge_captures_plugin_logging()
    test_config_update_is_logged_and_masked()
    test_config_update_skips_unchanged()
    test_message_preview()
    test_trace_out_respects_switch()
    test_schema_and_config_example_expose_switch()
    logbuffer.uninstall()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
