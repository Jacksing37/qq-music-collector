"""``web_search`` 后端：解析、格式化、失败处理、限流、开关接线。

**不联网**：Bing 的 HTML 全部是这里手写的固定片段，请求一律打桩。真联网的验证
在 ``tests/manual_search.py``（要手动跑，别放进回归——会被反爬、也不该每次 CI 都去打人家）。

运行: MUSIC_DATA_DIR=$(mktemp -d) python tests/test_websearch.py
"""

import asyncio
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

import httpx  # noqa: E402

from music_collector import tools as tl  # noqa: E402
from music_collector import websearch as ws  # noqa: E402
from music_collector.config import LLMConfig, MemoryConfig  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"  →  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")


#: 照着真实 Bing 结果页裁的一个片段，特意留了几个坑：
#: - 第一个 ``li`` 带 ``data-id`` 属性（正则不能要求紧跟 ``>``）
#: - 第二个的链接是 ``bing.com/ck/a?...&u=a1<base64>`` 跳转
#: - 摘要里有 ``&amp;`` 和 ``&#39;`` 实体
#: - 第四个是 ``javascript:`` 链接，必须丢掉
#: - 第五个没有 ``<h2>``，必须跳过
SAMPLE = """
<ol id="b_results">
<li class="b_algo" data-id="1">
  <h2><a href="https://example.com/a?x=1&amp;y=2">尊界 V800 售价公布</a></h2>
  <p>官方指导价 78.8 万元起，&#39;增程版&#39;同步开售 &amp; 交付。</p>
</li>
<li class="b_algo">
  <h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=abc&amp;u=a1aHR0cHM6Ly9uZXdzLmV4YW1wbGUuY29tL2JyYWtl">刹车事件调查结果</a></h2>
  <p>第三方机构已完成检测。</p>
</li>
<li class="b_algo">
  <h2><a href="//auto.example.cn/v800">第三方评测</a></h2>
  <p>实测百公里加速 <b>3.9</b> 秒。</p>
</li>
<li class="b_algo">
  <h2><a href="javascript:void(0)">恶意链接</a></h2>
  <p>不该出现</p>
</li>
<li class="b_algo">
  <span>没有 h2，跳过</span>
</li>
</ol>
"""


def test_parse() -> None:
    section("1. 解析 Bing 结果页")
    rows = ws._parse_bing(SAMPLE)
    check("解析出 3 条（javascript 与缺 h2 的被丢掉）", len(rows) == 3, str(len(rows)))
    check("标题正确", rows[0]["title"] == "尊界 V800 售价公布", rows[0]["title"])
    check("带 data-id 的 li 也能匹配", rows[0]["url"].startswith("https://example.com/a"),
          rows[0]["url"])
    check("URL 里的 &amp; 还原成 &", "&y=2" in rows[0]["url"], rows[0]["url"])
    check("摘要去掉标签", "<b>" not in rows[2]["snippet"], rows[2]["snippet"])
    check("摘要实体还原", "'" in rows[0]["snippet"] and "&" in rows[0]["snippet"],
          rows[0]["snippet"])

    check("ck/a 跳转解出真实地址",
          rows[1]["url"] == "https://news.example.com/brake", rows[1]["url"])
    check("// 开头补成 https", rows[2]["url"] == "https://auto.example.cn/v800", rows[2]["url"])

    check("空 HTML → 0 条", ws._parse_bing("") == [])
    check("完全不相干的 HTML → 0 条（Bing 改版就是这样）",
          ws._parse_bing("<html><body><div>验证一下</div></body></html>") == [])


def test_real_url() -> None:
    section("2. 跳转链接与实体")
    real = "https://a.example.com/x?y=1"
    token = ws.base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
    wrapped = f"https://www.bing.com/ck/a?p=zz&u=a1{token}"
    check("base64url 解出真实地址", ws._real_url(wrapped) == real, ws._real_url(wrapped))
    check("普通地址原样返回", ws._real_url("https://a.com/b") == "https://a.com/b")
    check("空串返回空串", ws._real_url("") == "")
    check("解不出来的 ck/a 退回原链接",
          ws._real_url("https://www.bing.com/ck/a?u=a1!!!").startswith("https://www.bing.com"),
          ws._real_url("https://www.bing.com/ck/a?u=a1!!!"))
    check("实体解码：&hellip; / &mdash;",
          ws.decode_entities("a&hellip;b&mdash;c") == "a…b—c",
          ws.decode_entities("a&hellip;b&mdash;c"))
    check("实体解码：数字实体", ws.decode_entities("&#65;&#66;") == "AB",
          ws.decode_entities("&#65;&#66;"))


def test_format() -> None:
    section("3. 拼给模型的文本")
    rows = [
        {"title": "标题A", "url": "https://a.com/1", "snippet": "  摘要   A  "},
        {"title": "标题B", "url": "https://b.com/2", "snippet": "很长的摘要" * 50},
    ]
    text = ws.format_results("测试词", rows, 20)
    check("首行报条数", "共 2 条" in text.splitlines()[0], text.splitlines()[0])
    check("一行一条（不用原版 markdown 小标题）", "## " not in text, text[:60])
    check("带标题与网址", "标题A" in text and "https://a.com/1" in text, text[:120])
    check("摘要去多余空白", "摘要 A" in text, text[:120])
    check("超长摘要被截断并加省略号", "…" in text, text[:200])
    check("提醒可以用 fetch_url 看细节", "fetch_url" in text, text[-60:])

    only = ws.format_results("q", [{"title": "T", "url": "u", "snippet": ""}], 200)
    check("没有摘要时不留空破折号", "T（u）" in only, only)


def test_search_errors() -> None:
    section("4. search() 的失败路径（必须不抛异常）")
    original = ws._search_bing

    async def boom_timeout(*a, **k):
        raise httpx.TimeoutException("timeout")

    async def boom_429(*a, **k):
        req = httpx.Request("GET", "https://www.bing.com/search")
        raise httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req))

    async def boom_other(*a, **k):
        raise RuntimeError("dns 挂了")

    async def fine(*a, **k):
        return [
            {"title": f"随便 t{i}", "url": f"https://x.com/{i}", "snippet": "s"}
            for i in range(30)
        ]

    try:
        ws._search_bing = boom_timeout  # type: ignore[assignment]
        r = asyncio.run(ws.search("随便", max_results=3))
        check("超时 → error 里有「超时」", "超时" in r["error"], r["error"])
        check("超时 → results 为空", r["results"] == [])

        ws._search_bing = boom_429  # type: ignore[assignment]
        r = asyncio.run(ws.search("随便"))
        check("429 → error 里带 HTTP 429", "429" in r["error"], r["error"])
        check("429 → 提示可能被限流", "限流" in r["error"], r["error"])

        ws._search_bing = boom_other  # type: ignore[assignment]
        r = asyncio.run(ws.search("随便"))
        check("其它异常 → 也变成一句 error", "失败" in r["error"], r["error"])

        ws._search_bing = fine  # type: ignore[assignment]
        r = asyncio.run(ws.search("随便", max_results=3))
        check("成功 → 截到 max_results", len(r["results"]) == 3, str(len(r["results"])))
        check("成功 → error 是空串", r["error"] == "", r["error"])

        r = asyncio.run(ws.search("随便", max_results=99))
        check("条数超上限被压到 MAX_RESULTS", len(r["results"]) == ws.MAX_RESULTS,
              str(len(r["results"])))

        r = asyncio.run(ws.search("随便", engine="google"))
        check("不支持的引擎 → error", "不支持" in r["error"], r["error"])
    finally:
        ws._search_bing = original  # type: ignore[assignment]


def test_throttle() -> None:
    section("5. 限流")

    async def case() -> None:
        # (a) 间隔 0.3 秒：第二次必须等
        ws.reset_throttle()
        t0 = time.monotonic()
        check("第一次放行", await ws.throttle(0.3) is True)
        await ws.throttle(0.3)
        elapsed = time.monotonic() - t0
        check("第二次真的等了（≥0.2s）", elapsed >= 0.2, f"{elapsed:.3f}s")

        # (b) interval=0：不等
        ws.reset_throttle()
        t1 = time.monotonic()
        for _ in range(3):
            await ws.throttle(0)
        check("间隔 0 时连搜三次几乎不耗时", time.monotonic() - t1 < 0.2,
              f"{time.monotonic() - t1:.3f}s")

        # (c) 要等太久 → 直接拒绝，不卡住回复
        ws._next_at = asyncio.get_running_loop().time() + 100
        check("等太久 → 返回 False（让模型说稍后再搜）", await ws.throttle(1.0) is False)
        ws.reset_throttle()

    asyncio.run(case())


def test_off_topic() -> None:
    section("6. 跑偏检测（Bing 改写查询时会把无关结果当答案）")

    check("取实词：中文片段 + 英文数字",
          ws.query_tokens("尊界V800 刹车事件") == ["尊界", "V800", "刹车事件"],
          str(ws.query_tokens("尊界V800 刹车事件")))
    check("纯数字不算实词（7 会在 2026 里误命中）",
          ws.query_tokens("2026 价格") == ["价格"], str(ws.query_tokens("2026 价格")))
    check("重复的实词只留一次",
          ws.query_tokens("尊界 尊界") == ["尊界"], str(ws.query_tokens("尊界 尊界")))

    right = [{"title": "尊界V800刹车踏板断裂事件", "url": "https://a.com", "snippet": "懂车帝测评"}]
    wrong = [{"title": "尊（汉语文字）_百度百科", "url": "https://b.com", "snippet": "尊的本义指盛酒的器具"}]
    tokens = ws.query_tokens("尊界V800 刹车事件")
    check("结果提到实词 → 不算跑偏", ws.looks_off_topic(right, tokens) is False)
    check("结果只有「尊」字 → 判定跑偏", ws.looks_off_topic(wrong, tokens) is True)
    check("空结果不判跑偏（那是另一条路径）", ws.looks_off_topic([], tokens) is False)
    check("没有实词时也不判（例如查纯数字）",
          ws.looks_off_topic(wrong, ws.query_tokens("2026")) is False)

    check("简化：去掉「事件」",
          ws.simplify_query("尊界V800 刹车事件") == "尊界V800 刹车",
          ws.simplify_query("尊界V800 刹车事件"))
    check("简化：去掉「怎么回事」",
          ws.simplify_query("尊界V800 刹车怎么回事") == "尊界V800 刹车",
          ws.simplify_query("尊界V800 刹车怎么回事"))
    check("没有可去的词 → 返回空串（表示不用重试）",
          ws.simplify_query("尊界V800 售价") == "", ws.simplify_query("尊界V800 售价"))

    # ---- 端到端：跑偏 → 简化后重试 ----
    calls: list[str] = []

    async def bing(query, count, timeout):
        calls.append(query)
        if "事件" in query:
            return list(wrong)
        return list(right)

    original = ws._search_bing
    try:
        ws._search_bing = bing  # type: ignore[assignment]
        r = asyncio.run(ws.search("尊界V800 刹车事件", max_results=3))
        check("跑偏后重试拿到了正确结果", len(r["results"]) == 1 and "尊界" in r["results"][0]["title"],
              str(r))
        check("真的重试了一次（两次请求）", len(calls) == 2, str(calls))
        check("重试用的是简化后的查询", calls[1] == "尊界V800 刹车", str(calls))

        # ---- 端到端：重试也跑偏 → 宁可说没搜到 ----
        calls.clear()

        async def always_off(query, count, timeout):
            calls.append(query)
            return list(wrong)

        ws._search_bing = always_off  # type: ignore[assignment]
        r = asyncio.run(ws.search("尊界V800 刹车事件", max_results=3))
        check("两次都跑偏 → 不返回无关结果", r["results"] == [], str(r["results"]))
        check("两次都跑偏 → error 说明没搜到相关内容", "没搜到相关内容" in r["error"], r["error"])

        # ---- 正常查询不该多打一次请求 ----
        calls.clear()

        async def ok(query, count, timeout):
            calls.append(query)
            return list(right)

        ws._search_bing = ok  # type: ignore[assignment]
        r = asyncio.run(ws.search("尊界V800 刹车", max_results=3))
        check("结果正确时只请求一次", len(calls) == 1, str(calls))
        check("结果原样返回", len(r["results"]) == 1, str(len(r["results"])))
    finally:
        ws._search_bing = original  # type: ignore[assignment]


def test_build() -> None:
    section("7. 开关接线（build）")
    mem = MemoryConfig(enabled=False)
    cfg = LLMConfig(tools=True, web_tool=True, web_search=False, skills=False)
    names = [t.name for t in tl.build(cfg, mem=mem)[0]]
    check("默认不给 web_search", "web_search" not in names, str(names))
    check("fetch_url 仍然有", "fetch_url" in names, str(names))

    cfg = LLMConfig(tools=True, web_tool=True, web_search=True, skills=False)
    names = [t.name for t in tl.build(cfg, mem=mem)[0]]
    check("开了就有 web_search", "web_search" in names, str(names))

    cfg = LLMConfig(tools=True, web_tool=False, web_search=True, skills=False)
    names = [t.name for t in tl.build(cfg, mem=mem)[0]]
    check("web_tool 关掉时搜索也一起不给（搜到也打不开）",
          "web_search" not in names and "fetch_url" not in names, str(names))

    cfg = LLMConfig(tools=False, web_tool=True, web_search=True, skills=False)
    names = [t.name for t in tl.build(cfg, mem=mem)[0]]
    check("工具总闸关掉 → 一个都没有", names == [], str(names))

    check("web_search 在白名单里", "web_search" in tl.ALLOWED_TOOL_NAMES,
          str(sorted(tl.ALLOWED_TOOL_NAMES)))
    check("白名单共 9 个（新增必须显式加）", len(tl.ALLOWED_TOOL_NAMES) == 9,
          str(len(tl.ALLOWED_TOOL_NAMES)))


def test_defaults() -> None:
    section("8. 配置默认值（改默认值时要同步改这里）")
    cfg = LLMConfig()
    check("搜索默认关（避免没确认就吃验证码）", cfg.web_search is False)
    check("默认 5 条", cfg.search_max == 5)
    check("默认每条摘要 200 字", cfg.search_snippet_chars == 200)
    check("默认超时 20 秒", cfg.search_timeout == 20.0)
    check("默认间隔 1 秒", cfg.search_interval == 1.0)

    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    for key in ("web_search", "search_max", "search_snippet_chars",
                "search_timeout", "search_interval"):
        check(f"示例配置里有 {key}", key in example)


def test_tool_handler() -> None:
    section("9. 工具 handler 的三条失败文案")

    async def case() -> None:
        cfg = LLMConfig(web_search=True, search_interval=0)
        ctx = tl.build(cfg, mem=MemoryConfig(enabled=False))[1]

        out = await tl.dispatch(tl.build(cfg, mem=MemoryConfig(enabled=False))[0],
                                "web_search", {"query": "x"}, ctx)
        check("一个字 → 要求说清楚", "错误" in out, out)

        original = ws._search_bing

        async def empty(*a, **k):
            return []

        async def one(*a, **k):
            return [{"title": "尊界V800 报价", "url": "https://a.com", "snippet": "摘要"}]

        try:
            ws._search_bing = empty  # type: ignore[assignment]
            out = await tl.dispatch(tl.build(cfg, mem=MemoryConfig(enabled=False))[0],
                                    "web_search", {"query": "尊界 v800"}, ctx)
            check("0 条 → 明说没搜到、别编", "没有解析出结果" in out, out)

            ws._search_bing = one  # type: ignore[assignment]
            out = await tl.dispatch(tl.build(cfg, mem=MemoryConfig(enabled=False))[0],
                                    "web_search", {"query": "尊界 v800"}, ctx)
            check("有结果 → 拼成给模型的文本", "共 1 条" in out and "https://a.com" in out, out)
        finally:
            ws._search_bing = original  # type: ignore[assignment]

    asyncio.run(case())


def main() -> None:
    test_parse()
    test_real_url()
    test_format()
    test_search_errors()
    test_throttle()
    test_off_topic()
    test_build()
    test_defaults()
    test_tool_handler()
    print(f"\n通过 {PASSED} 项，失败 {FAILED} 项")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
