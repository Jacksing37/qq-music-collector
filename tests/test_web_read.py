"""网页抓取与合并转发的读取。

覆盖：

1. ``html_to_text``：标题（og:title / <title>）、去 script/style、块级标签换行、
   实体反转义、超长截断；
2. SSRF 防护：环回 / 私网 / 链路本地 / 保留地址一律拒绝，公网 IP 放行；
3. ``fetch_page``：正常网页能拿到标题+正文；404、图片、内网地址一律返回 None；
   超时/连接失败不抛异常；
4. ``fetch_pages``：条数上限、同站点只抓一次；
5. 合并转发：``forward`` 段要调 ``get_forward_msg``（参数名 message_id / id 两种都要能试）、
   内联 ``node`` 段、嵌套只展开一层、条数与字数上限；
6. ``with_context``：字符串与 vision 数组两种 content 都能拼，且有自己的字数上限；
7. ``chat(context=…)``：资料进请求但**不进上下文历史**（否则越聊越贵）。

运行: python tests/test_web_read.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))
os.environ.setdefault("MUSIC_DATA_DIR", tempfile.mkdtemp(prefix="music-test-web-"))

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
from music_collector.webpage import (  # noqa: E402
    fetch_page,
    fetch_pages,
    html_to_text,
    is_public_host,
    url_is_fetchable,
)

PASSED = 0
FAILED = 0

PAGE_HTML = """<!doctype html>
<html><head>
<title>面条的十种做法 - 美食站</title>
<style>body{color:red}</style>
<script>var a = "<p>不该出现</p>";</script>
</head>
<body>
<article>
  <h1>面条的十种做法</h1>
  <p>第一种：把水烧开，下面条，煮三分钟。</p>
  <p>第二种：冷面过冰水，拌芝麻酱 &amp; 黄瓜丝。</p>
  <ul><li>要点一：水要滚</li><li>要点二：别盖锅盖</li></ul>
</article>
</body></html>
"""


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


class _Handler(BaseHTTPRequestHandler):
    """本机测试服务器：用来验证真实的 HTTP 抓取路径（不联网）。"""

    def log_message(self, *args) -> None:  # 别刷屏
        pass

    def do_GET(self) -> None:  # noqa: N802 — 标准库要求这个大小写
        if self.path == "/":
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/short":
            body = "太短".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/image":
            body = b"\x89PNG\r\n\x1a\n" + b"0" * 100
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            body = b"<html><body>not found</body></html>"
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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


def test_html_to_text() -> None:
    print("\n[1] HTML → 标题 + 正文")
    title, text = html_to_text(PAGE_HTML, max_chars=1000)
    check("取出 <title>", "面条的十种做法" in title, title)
    check("正文包含段落内容", "把水烧开" in text, text)
    check("script 里的假标签不会被当正文", "不该出现" not in text, text)
    check("style 不进正文", "color:red" not in text, text)
    check("标签被去掉、实体反转义", "<p>" not in text and "&amp;" not in text, text)
    check("块级标签变行（不是全挤一行）", text.count("\n") >= 3, text)

    og = '<meta property="og:title" content="OG 标题"/>' + PAGE_HTML
    check("优先用 og:title", html_to_text(og)[0] == "OG 标题", html_to_text(og)[0])

    _, cut = html_to_text("<p>" + "字" * 500 + "</p>", max_chars=100)
    check("超长截断并加省略号", len(cut) <= 101 and cut.endswith("…"), cut[-10:])

    # 真实踩过的坑：百度把整段 HTML 转义后塞在 JSON 里，反转义后 `<style>` 会「复活」
    escaped = ("<p>正文开始</p><script>var s = \"&lt;style&gt;body{color:red}&lt;/style&gt;\""
               "</script><div>正文结尾</div>")
    _, txt = html_to_text(escaped)
    check("转义过的标签不会在反转义后复活",
          "<style" not in txt and "color:red" not in txt, txt)
    check("正文没被误删", "正文开始" in txt and "正文结尾" in txt, txt)


def test_ssrf_guard() -> None:
    print("\n[2] SSRF 防护：只放行公网地址")
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "192.168.1.1",
                 "172.16.0.9", "169.254.169.254", "::1", "0.0.0.0", ""):
        check(f"拒绝 {host or '(空)'}", is_public_host(host) is False)
    for host in ("8.8.8.8", "1.1.1.1"):
        check(f"放行 {host}", is_public_host(host) is True)
    check("非 http(s) 一律不抓", url_is_fetchable("file:///etc/passwd") is False)
    check("ftp 不抓", url_is_fetchable("ftp://example.com/a") is False)
    check("正常链接可抓", url_is_fetchable("https://example.com/a") is True)


def test_fetch_page(port: int) -> None:
    print("\n[3] 抓取真实页面（本机 http.server）")
    base = f"http://127.0.0.1:{port}"

    page = asyncio.run(fetch_page(base + "/", timeout=5, max_chars=800, allow_private=True))
    check("正常网页拿到标题", page is not None and "面条" in page["title"], str(page))
    check("正文抓到了", page is not None and "煮三分钟" in page["text"], str(page.get("text")))

    check("404 返回 None",
          asyncio.run(fetch_page(base + "/404", timeout=5, allow_private=True)) is None)
    check("图片（非文本）返回 None",
          asyncio.run(fetch_page(base + "/image", timeout=5, allow_private=True)) is None)
    check("正文太短（登录墙/空壳）返回 None",
          asyncio.run(fetch_page(base + "/short", timeout=5, allow_private=True)) is None)
    check("内网地址在默认参数下被拒（没开 allow_private）",
          asyncio.run(fetch_page(base + "/", timeout=5)) is None)
    check("非 http(s) 直接 None", asyncio.run(fetch_page("file:///etc/passwd")) is None)

    red = asyncio.run(fetch_page(base + "/redirect", timeout=5, allow_private=True))
    check("跟随重定向（逐跳校验）", red is not None and "面条" in red["title"], str(red))

    check("空列表不报错且不请求", asyncio.run(fetch_pages([])) == [])
    pages = asyncio.run(_fetch_local_pages(base))
    check("同站点只抓一次（limit=2 也只返回 1 篇）", len(pages) == 1, str(len(pages)))
    mixed = asyncio.run(_fetch_local_pages(base, extra_host=True))
    check("去重之后再取 limit（不会因为同站重复而饿死别的站）",
          len(mixed) == 2, str([p["url"] for p in mixed]))


async def _fetch_local_pages(base: str, extra_host: bool = False) -> list[dict]:
    """``fetch_pages`` 没有 allow_private，测试里临时给它一个本机入口。"""
    from music_collector import webpage

    original = webpage.fetch_page

    async def patched(url, **kwargs):
        kwargs["allow_private"] = True
        return await original(url, **kwargs)

    webpage.fetch_page = patched
    try:
        urls = [base + "/", base + "/?x=1", base + "/y"]
        if extra_host:
            # localhost 与 127.0.0.1 指向同一台机器，但主机名不同 → 应算两个站点
            urls = [base + "/", base + "/?x=1", base.replace("127.0.0.1", "localhost") + "/"]
        return await fetch_pages(urls, limit=2, timeout=5)
    finally:
        webpage.fetch_page = original


def test_forward() -> None:
    print("\n[4] 合并转发（聊天记录）")
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


class _AlwaysFail:
    async def call_api(self, api: str, **kwargs):
        raise RuntimeError("boom")


def test_context_merge() -> None:
    print("\n[5] 参考资料拼接")
    check("空资料不改动 content", with_context("问题", "") == "问题")
    got = with_context("问题", "【网页】正文")
    check("资料在问题前面", got.startswith("【网页】") and got.endswith("问题"), got)

    vision = [{"type": "text", "text": "问题"}, {"type": "image_url", "image_url": {"url": "u"}}]
    merged = with_context(vision, "资料")
    check("vision 数组：资料并进第一段文字", merged[0]["text"].startswith("资料") and len(merged) == 2, str(merged))
    check("vision 数组：图片没被动", merged[1]["type"] == "image_url", str(merged))

    huge = with_context("问题", "字" * (CONTEXT_CHAR_LIMIT + 500))
    check("资料有自己的字数上限", len(huge) <= CONTEXT_CHAR_LIMIT + 10, str(len(huge)))
    check("content_chars 仍然只算文字", content_chars(merged) > 0)


def test_chat_context() -> None:
    print("\n[6] chat(context=…)：进请求、不进历史")
    gid = 8601

    async def run(cfg: LLMConfig, images=None):
        bot = LLMBot()
        captured: dict = {}

        async def fake_post(messages, cfg_):
            captured["messages"] = messages
            return "好的"

        bot._post_chat = fake_post  # type: ignore[method-assign]
        reply, reason = await bot.chat(
            gid, 9001, "张三", "这个链接说啥", cfg,
            images=images, context="【网页内容：面条做法】\n水烧开下面条",
        )
        return bot, captured.get("messages") or [], reply, reason

    cfg = LLMConfig(cooldown=0)
    bot, msgs, reply, reason = asyncio.run(run(cfg))
    last = msgs[-1]["content"]
    check("资料进了最后一次请求", "水烧开下面条" in str(last), str(last))
    check("提问也在（资料在前）", str(last).index("水烧开下面条") < str(last).index("这个链接说啥"), str(last))
    check("回复正常", reply == "好的" and reason == "", f"{reply}/{reason}")
    hist = bot.history(gid)
    check("上下文历史里没有网页正文",
          all("水烧开下面条" not in str(m.get("content")) for m in hist), str(hist))
    check("上下文历史照旧存问答对", len(hist) == 2 and "这个链接说啥" in hist[0]["content"], str(hist))

    bot2, msgs2, _, _ = asyncio.run(run(LLMConfig(cooldown=0, vision=True),
                                       images=["http://example.com/x.jpg"]))
    last2 = msgs2[-1]["content"]
    check("带图时资料并进文字段", isinstance(last2, list) and "水烧开下面条" in last2[0]["text"], str(last2))
    check("带图时图片段还在", any(p.get("type") == "image_url" for p in last2), str(last2))


def test_llm_context() -> None:
    print("\n[7] _llm_context：只取该取的，抓不到也不炸")
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

    calls: list[list[str]] = []
    original = mc.fetch_pages

    async def fake_pages(urls, **kwargs):
        calls.append(list(urls))
        return [{"url": urls[0], "title": "示例标题", "text": "网页正文内容"}]

    mc.fetch_pages = fake_pages
    try:
        ctx = asyncio.run(mc._llm_context(bot, event, LLMConfig(), "看这个 https://example.com/a"))
        check("聊天记录进了资料", "【群友转发的聊天记录】" in ctx and "记录内容" in ctx, ctx)
        check("网页正文进了资料", "【网页内容：示例标题】" in ctx and "网页正文内容" in ctx, ctx)
        check("记录在网页前面", ctx.index("聊天记录") < ctx.index("网页内容"), ctx)

        off = asyncio.run(mc._llm_context(
            bot, event, LLMConfig(read_forward=False, read_link=False), "随便说说"))
        check("两个开关都关 → 空资料", off == "", off)

        music_ev = types.SimpleNamespace(
            group_id=8702, message=[seg("text", text="https://music.163.com/song?id=123")])
        calls.clear()
        none_ctx = asyncio.run(mc._llm_context(ForwardBot(), music_ev, LLMConfig(),
                                               "https://music.163.com/song?id=123"))
        check("音乐链接不去抓（省一次请求）", calls == [] and none_ctx == "", str(calls))

        async def no_pages(urls, **kwargs):
            calls.append(list(urls))
            return []

        mc.fetch_pages = no_pages
        calls.clear()
        fail_ctx = asyncio.run(mc._llm_context(bot, event, LLMConfig(), "https://example.com/b"))
        check("抓不到时告诉模型「打不开」", "没能抓取成功" in fail_ctx, fail_ctx)

        # 「引用一条转发记录 / 引用一条带链接的消息」再提问，内容都在 event.reply 里
        mc.fetch_pages = fake_pages
        quoted_ev = types.SimpleNamespace(
            group_id=8703,
            message=[seg("text", text="这是啥意思")],
            reply=types.SimpleNamespace(message=[
                seg("forward", id="fwd-r"),
            ]),
        )
        quoted_ctx = asyncio.run(mc._llm_context(bot, quoted_ev, LLMConfig(), "这是啥意思"))
        check("被引用的转发记录也会展开", "【被引用的聊天记录】" in quoted_ctx, quoted_ctx)

        calls.clear()
        link_ev = types.SimpleNamespace(
            group_id=8704,
            message=[seg("text", text="这文章讲啥")],
            reply=types.SimpleNamespace(message=[seg("text", text="https://example.com/art")]),
        )
        asyncio.run(mc._llm_context(ForwardBot(), link_ev, LLMConfig(), "这文章讲啥"))
        check("被引用消息里的链接也会抓", calls and "example.com/art" in calls[0][0], str(calls))
    finally:
        mc.fetch_pages = original


def test_form_and_docs() -> None:
    print("\n[8] 表单 / 示例配置 / README 落地")
    from music_collector.config import AppConfig
    from music_collector.webui import KEY_INDEX

    keys = ("llm.read_link", "llm.link_max", "llm.link_chars", "llm.link_timeout",
            "llm.read_forward", "llm.forward_limit", "llm.forward_chars")
    for key in keys:
        check(f"表单里有 {key}", key in KEY_INDEX, str(list(KEY_INDEX)[:3]))

    cfg = AppConfig()
    check("默认开着网页抓取", cfg.llm.read_link is True)
    check("默认开着聊天记录", cfg.llm.read_forward is True)
    check("默认上限符合约定",
          cfg.llm.link_max == 2 and cfg.llm.link_chars == 1500
          and cfg.llm.forward_limit == 30 and cfg.llm.forward_chars == 2000,
          str((cfg.llm.link_max, cfg.llm.link_chars, cfg.llm.forward_limit, cfg.llm.forward_chars)))

    sample = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    for key in ("read_link", "link_max", "read_forward", "forward_limit"):
        check(f"示例配置写了 {key}", key in sample)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for key in ("llm.read_link", "llm.read_forward", "get_forward_msg", "SSRF"):
        check(f"README 写了 {key}", key in readme)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        test_html_to_text()
        test_ssrf_guard()
        test_fetch_page(port)
        test_forward()
        test_context_merge()
        test_chat_context()
        test_llm_context()
        test_form_and_docs()
    finally:
        server.shutdown()
        server.server_close()

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
