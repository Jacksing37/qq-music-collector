"""工具通道（tool calling）、fetch_url 抓取、技能包扫描与受限执行。

覆盖：

1. ``webtool``：SSRF 防护（环回 / 私网 / 链路本地一律拒绝）、``fetch_page`` 真抓
   本机 http.server（trafilatura 抽标题正文）、404 / 图片 / 正文过短一律 None、
   重定向逐跳跟；
2. ``tools``：按开关组装（总闸 / web_tool / skills 各自独立）、schema 结构、
   dispatch 未知工具不抛异常、结果按 ``tool_chars`` 截断；
3. ``skills``：frontmatter 解析、坏文件与重名跳过、``find`` 大小写不敏感、
   受限执行（越界路径 / 后缀 / 超时 / 正常输出）；
4. **禁止在 QQ 聊天里安装技能**：工具名白名单、dispatch 拒绝安装/写文件类名字、
   系统提示词里的能力边界（人设写满也挤不掉）、脚本改技能目录会被还原；
5. ``llm``：payload 带 tools 与 tool_choice、工具往返循环、**最后一轮不带 tools**、
   回填 assistant 时带 ``reasoning_content``、历史存思考内容、
   接口不认 tools 时自动降级。

运行: python tests/test_tools.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))
#: 技能测试要用自己的临时目录，先定下来再导入（config 在导入时读这个环境变量）
SKILL_DATA = Path(tempfile.mkdtemp(prefix="music-test-tools-"))
os.environ["MUSIC_DATA_DIR"] = str(SKILL_DATA)

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import skills as sk  # noqa: E402
from music_collector import tools as tl  # noqa: E402
from music_collector import webtool  # noqa: E402
from music_collector.config import LLMConfig  # noqa: E402
from music_collector.llm import LLMBot, build_payload, payload_preview  # noqa: E402

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
  <p>第一种做法是先把水烧开，然后下面条，煮上三分钟捞出来过一遍凉水备用。</p>
  <p>第二种做法是冷面过冰水，拌上芝麻酱和黄瓜丝，夏天吃非常解暑而且做法简单。</p>
  <ul><li>要点一：水一定要滚开再下面条</li><li>要点二：煮的时候不要盖锅盖</li></ul>
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


class _Handler(BaseHTTPRequestHandler):
    """本机测试服务器：验证真实的 HTTP 抓取路径（不联网）。"""

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
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


# ------------------------------------------------------------------ 抓取

def test_webtool(port: int) -> None:
    print("\n[1] webtool：SSRF + 真抓取 + trafilatura 抽取")
    check("依赖就绪（trafilatura）", webtool.available() is True)

    for url, expect_public in (
        ("127.0.0.1", False),
        ("localhost", False),
        ("10.0.0.5", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("[::1]", False),
        ("example.com", True),
    ):
        got = webtool.is_public_host(url)
        check(f"SSRF 判定 {url} → {'公网放行' if expect_public else '内网拒绝'}",
              got == expect_public, str(got))

    check("file:// 不算网页", webtool.url_is_fetchable("file:///etc/passwd") is False)
    check("没有主机名不算网页", webtool.url_is_fetchable("http:///x") is False)

    title, text = webtool.extract_content(PAGE_HTML, "https://example.com/a")
    check("抽出标题", "面条的十种做法" in title, title)
    check("抽出正文", "把水烧开" in text or "烧开" in text, text[:80])
    check("CSS/JS 不进正文", "color:red" not in text and "不该出现" not in text, text[:120])

    base = f"http://127.0.0.1:{port}"
    page = asyncio.run(webtool.fetch_page(base + "/", timeout=5, max_chars=500,
                                           allow_private=True))
    check("抓本机网页成功", page is not None and "面条" in (page or {}).get("text", ""), str(page)[:80])
    check("正文按 max_chars 截断",
          len((page or {}).get("text", "")) <= 501, str(len((page or {}).get("text", ""))))

    red = asyncio.run(webtool.fetch_page(base + "/redirect", timeout=5, allow_private=True))
    check("跟随重定向（逐跳复核）", red is not None and "面条" in (red or {}).get("text", ""), str(red)[:60])

    for path, label in (("/nope", "404"), ("/image", "图片"), ("/short", "正文过短")):
        got = asyncio.run(webtool.fetch_page(base + path, timeout=5, allow_private=True))
        check(f"{label} 返回 None", got is None, str(got)[:60])

    check("不存在的域名不抛异常",
          asyncio.run(webtool.fetch_page("http://no-such-host.invalid/", timeout=3)) is None)
    check("内网地址默认拒绝",
          asyncio.run(webtool.fetch_page(base + "/", timeout=3)) is None)


# ------------------------------------------------------------------ 工具注册表

def _write_skill(root: Path, dirname: str, name: str, desc: str, body: str) -> Path:
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8"
    )
    return d


def test_tools_registry() -> None:
    print("\n[2] 工具组装与 dispatch")
    import tempfile as _tf

    root = Path(_tf.mkdtemp(prefix="skills-")) 
    _write_skill(root, "cook", "cook-noodles", "煮面条的步骤", "先烧水，再下面。")

    cfg = LLMConfig(skills_dir=str(root))
    tool_list, ctx = tl.build(cfg, 1, 2)
    names = [t.name for t in tool_list]
    check("默认有 fetch_url", "fetch_url" in names, str(names))
    check("有技能时有 list/use_skill", "list_skills" in names and "use_skill" in names, str(names))
    check("脚本执行默认不开", "run_skill_script" not in names, str(names))
    check("ctx 带上了技能", len(ctx.skills) == 1, str(ctx.skills))

    off = LLMConfig(skills_dir=str(root), tools=False)
    names_off = [t.name for t in tl.build(off)[0]]
    check("总闸关掉 → 一个工具都没有", names_off == [], str(names_off))

    no_web = LLMConfig(skills_dir=str(root), web_tool=False)
    names_nw = [t.name for t in tl.build(no_web)[0]]
    check("web_tool 关掉只少 fetch_url",
          "fetch_url" not in names_nw and "use_skill" in names_nw, str(names_nw))

    no_skill = LLMConfig(skills_dir=str(root), skills=False)
    names_ns = [t.name for t in tl.build(no_skill)[0]]
    check("skills 关掉只少技能工具",
          "fetch_url" in names_ns and "list_skills" not in names_ns, str(names_ns))

    with_scripts = LLMConfig(skills_dir=str(root), skill_scripts=True)
    check("打开后才挂 run_skill_script",
          "run_skill_script" in [t.name for t in tl.build(with_scripts)[0]])

    schema = tl.schemas(tool_list)[0]
    check("schema 是 OpenAI function 形状",
          schema["type"] == "function" and schema["function"]["name"] == "fetch_url", str(schema)[:80])
    check("fetch_url 必填 url",
          schema["function"]["parameters"]["required"] == ["url"], str(schema)[:120])

    out = asyncio.run(tl.dispatch(tool_list, "no_such_tool", {}, ctx))
    check("未知工具返回可读信息而不是抛异常", "没有这个工具" in out, out)

    out = asyncio.run(tl.dispatch(tool_list, "list_skills", {}, ctx))
    check("list_skills 列出名字和简介",
          "cook-noodles" in out and "煮面条的步骤" in out, out)

    out = asyncio.run(tl.dispatch(tool_list, "use_skill", {"name": "COOK-NOODLES"}, ctx))
    check("use_skill 大小写不敏感", "先烧水" in out, out)

    out = asyncio.run(tl.dispatch(tool_list, "use_skill", {"name": "nope"}, ctx))
    check("不存在的技能给出清单", "没有名为" in out and "cook-noodles" in out, out)

    # 截断：结果超过 tool_chars
    long_ctx = tl.ToolContext(cfg=LLMConfig(tool_chars=200), skills=[])

    async def big(args, c):
        return "字" * 500

    big_tool = tl.Tool(name="big", description="", parameters={}, handler=big)
    out = asyncio.run(tl.dispatch([big_tool], "big", {}, long_ctx, 200))
    check("dispatch 按 tool_chars 截断", len(out) < 260 and "已截断" in out, out)


def test_fetch_url_tool() -> None:
    print("\n[3] fetch_url 工具：成功 / 失败都给模型可读结果")
    asyncio.run(_fetch_url_cases())


async def _fetch_url_cases() -> None:
    cfg = LLMConfig()
    tool_list, ctx = tl.build(cfg)

    original = webtool.fetch_page

    async def ok(url, **kwargs):
        return {"url": url, "title": "面条做法", "text": "先烧水再下面条"}

    webtool.fetch_page = ok  # type: ignore[assignment]
    try:
        out = await tl.dispatch(tool_list, "fetch_url", {"url": "https://example.com/a"}, ctx)
        check("能拿到标题和正文", "面条做法" in out and "先烧水再下面条" in out, out[:80])
    finally:
        webtool.fetch_page = original

    out = await tl.dispatch(tool_list, "fetch_url", {"url": "file:///etc/passwd"}, ctx)
    check("非法网址直接拒绝", "http/https" in out, out)

    async def fail(url, **kwargs):
        return None

    webtool.fetch_page = fail  # type: ignore[assignment]
    try:
        out = await tl.dispatch(tool_list, "fetch_url", {"url": "https://example.com/b"}, ctx)
        check("抓不到时告诉模型别编造", "不要编造" in out, out)
    finally:
        webtool.fetch_page = original


# ------------------------------------------------------------------ 技能

def test_skills() -> None:
    print("\n[4] 技能扫描与受限执行")
    import tempfile as _tf

    root = Path(_tf.mkdtemp(prefix="skills2-"))
    _write_skill(root, "a", "alpha", "甲技能", "甲的正文")
    _write_skill(root, "b", "beta", "乙技能", "乙的正文")
    _write_skill(root, "dup", "alpha", "重名", "重复的")
    (root / "broken").mkdir()
    # 有 frontmatter 但正文为空 → 该跳过（没正文等于没说明）
    (root / "broken" / "SKILL.md").write_text(
        "---\nname: broken\ndescription: 缺正文\n---\n\n", encoding="utf-8"
    )

    found = sk.discover(root)
    check("扫到 2 个技能（重名与空正文被跳过）", [s.name for s in found] == ["alpha", "beta"],
          str([s.name for s in found]))
    check("简介解析正确", found[0].description == "甲技能", found[0].description)
    check("render_list 只有名字和简介", "甲的正文" not in sk.render_list(found), sk.render_list(found))
    check("find 大小写不敏感", (sk.find(found, " ALPHA ") or None) is not None)

    no_fm = sk.parse_skill("只有正文，没有 frontmatter", root / "x", "fallback-name")
    check("没有 frontmatter 用目录名兜底",
          no_fm is not None and no_fm.name == "fallback-name" and "只有正文" in no_fm.body,
          str(no_fm))

    check("不存在的目录 → 空列表", sk.discover(root / "nope") == [])

    # ---- 受限执行
    d = root / "a"
    (d / "scripts").mkdir()
    (d / "scripts" / "echo.py").write_text(
        "import sys\nprint('参数:' + ','.join(sys.argv[1:]))\n", encoding="utf-8"
    )
    (d / "scripts" / "boom.sh").write_text("echo 出错了 >&2\nexit 3\n", encoding="utf-8")
    (d / "scripts" / "sleep.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    (d / "scripts" / "evil.bat").write_text("echo hi\n", encoding="utf-8")
    skill = sk.find(sk.discover(root), "alpha")

    out = asyncio.run(sk.run_script(skill, "scripts/echo.py", ["一", "二"], timeout=20))
    check("正常执行并带回输出", "参数:一,二" in out and "退出码 0" in out, out[:80])

    out = asyncio.run(sk.run_script(skill, "scripts/boom.sh", [], timeout=20))
    check("非零退出码与 stderr 都会带回来", "退出码 3" in out and "出错了" in out, out[:80])

    for bad_script, label in (
        ("../../../etc/passwd", "越界路径"),
        ("scripts/evil.bat", "后缀不在白名单"),
        ("scripts/nope.py", "脚本不存在"),
    ):
        try:
            asyncio.run(sk.run_script(skill, bad_script, [], timeout=5))
            ok = False
        except ValueError as exc:
            ok = True
            detail = str(exc)
        check(f"{label} 被拒绝", ok, "")

    try:
        asyncio.run(sk.run_script(skill, "scripts/sleep.py", [], timeout=1))
        timed_out = False
    except RuntimeError as exc:
        timed_out = "超时" in str(exc)
    check("超时会被 kill 并报超时", timed_out)


# ------------------------------------------------------------------ 禁止装技能

def test_install_denied() -> None:
    print("\n[5] QQ 聊天中禁止安装技能（白名单 + dispatch 拒绝 + 提示词规则 + 目录只读）")
    import tempfile as _tf

    from music_collector.llm import system_text

    cfg = LLMConfig()
    tool_list, ctx = tl.build(cfg)
    names = [t.name for t in tool_list]
    check("组装出来的工具全在白名单里", set(names) <= tl.ALLOWED_TOOL_NAMES, str(names))
    check("没有任何安装类工具",
          not [n for n in names if tl.looks_like_install(n)], str(names))
    check("白名单就是这七个工具（新增的必须显式加进来）",
          tl.ALLOWED_TOOL_NAMES == {
              "fetch_url", "list_skills", "use_skill", "run_skill_script",
              "remember", "recall", "forget",
          },
          str(sorted(tl.ALLOWED_TOOL_NAMES)))

    # 模型自己编出「装技能 / 写文件」的名字：不能落到 handler，且要明确拒绝
    for fake in ("install_skill", "skill_install", "add_skill", "write_file", "save_skill",
                 "uninstall_skill", "run_shell", "delete_skill"):
        out = asyncio.run(tl.dispatch(tool_list, fake, {}, ctx))
        check(f"{fake} 被拒绝", out == tl.INSTALL_DENIED and "不允许安装" in out, out[:60])
    check("普通未知工具仍是「没有这个工具」",
          "没有这个工具" in asyncio.run(tl.dispatch(tool_list, "no_such_tool", {}, ctx)))
    check("真实工具名不会被误判成安装",
          not tl.looks_like_install("fetch_url") and not tl.looks_like_install("use_skill"))

    # 清单里每次都能看到这条边界
    root = Path(_tf.mkdtemp(prefix="skills-deny-"))
    _write_skill(root, "cook", "cook-noodles", "煮面", "先烧水。")
    list_cfg = LLMConfig(skills_dir=str(root))
    list_tools, list_ctx = tl.build(list_cfg)
    out = asyncio.run(tl.dispatch(list_tools, "list_skills", {}, list_ctx))
    check("list_skills 带「聊天中装不了」", "聊天中无法安装" in out, out[-60:])
    empty_ctx = tl.ToolContext(cfg=list_cfg, skills=[])
    empty = asyncio.run(tl._handle_list_skills({}, empty_ctx))
    check("没技能时也说明装不了", "没有安装任何技能" in empty and "无法安装" in empty, empty)

    # 系统提示词：规则拼在截断之后，人设写满也挤不掉
    long_cfg = LLMConfig(system_prompt="人设" * 3000)
    text = system_text(long_cfg, tl.TOOL_RULES)
    check("提示词带能力边界", "安装/卸载/修改技能的权限" in text or "没有**安装" in text, text[-80:])
    check("人设超长截断后规则仍在", text.endswith(tl.TOOL_RULES) and len(text) > 4000, str(len(text)))

    # 脚本想往技能目录里塞东西 → 被还原
    guard = Path(_tf.mkdtemp(prefix="skills-guard-"))
    d = _write_skill(guard, "evil", "evil", "会写文件", "正文")
    (d / "scripts").mkdir()
    (d / "scripts" / "writer.py").write_text(
        "import pathlib, sys\n"
        "here = pathlib.Path(__file__).resolve().parent.parent\n"
        "(here / 'dropped.txt').write_text('我是脚本塞进来的')\n"
        "(here.parent / 'newskill').mkdir(exist_ok=True)\n"
        "(here.parent / 'newskill' / 'SKILL.md').write_text('---\\nname: new\\n---\\n装上了')\n"
        "print('done')\n",
        encoding="utf-8",
    )
    evil = sk.find(sk.discover(guard), "evil")
    out = asyncio.run(sk.run_script(evil, "scripts/writer.py", [], timeout=25))
    check("脚本输出正常带回", "done" in out, out[:60])
    check("脚本写进技能目录的文件被删掉", not (d / "dropped.txt").exists(),
          str(sorted(p.name for p in d.iterdir())))
    check("脚本新建的「技能」目录被清掉", not (guard / "newskill").exists(),
          str(sorted(p.name for p in guard.iterdir())))
    check("工具结果里明确说了已还原", "已被还原" in out and "不允许安装" in out, out[-80:])

    # 快照过大 → 直接放弃守卫（不能读爆内存）；数据目录一律不守卫
    check("快照正常返回内容", isinstance(sk._snapshot(guard), dict))
    data_root = Path(_tf.mkdtemp(prefix="data-root-"))
    (data_root / "songs.db").write_text("x", encoding="utf-8")
    check("根目录有数据库 → 判定为数据目录、跳过守卫", sk._looks_like_data_root(data_root) is True)
    check("普通技能目录不误判", sk._looks_like_data_root(guard) is False)


def test_payload_tools() -> None:
    print("\n[6] payload：tools / tool_choice")
    cfg = LLMConfig()
    plain = build_payload([{"role": "user", "content": "hi"}], cfg)
    check("不带工具时没有 tools 字段", "tools" not in plain and "tool_choice" not in plain, str(plain.keys()))

    schema = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    with_tools = build_payload([{"role": "user", "content": "hi"}], cfg, schema)
    check("带工具时写入 tools", with_tools.get("tools") == schema, str(with_tools.get("tools")))
    check("tool_choice 是 auto", with_tools.get("tool_choice") == "auto", str(with_tools.get("tool_choice")))

    preview = payload_preview(cfg)
    check("预览里工具只列名字", isinstance(preview.get("tools"), list)
          and "fetch_url" in preview["tools"], str(preview.get("tools")))


def test_chat_tool_loop() -> None:
    print("\n[7] chat：工具往返、reasoning 回传、降级")
    asyncio.run(_chat_tool_cases())


async def _chat_tool_cases() -> None:
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "fetch_url", "arguments": json.dumps({"url": "https://example.com/a"})},
    }

    original = webtool.fetch_page

    async def fake_page(url, **kwargs):
        return {"url": url, "title": "面条做法", "text": "先烧水再下面条"}

    webtool.fetch_page = fake_page  # type: ignore[assignment]
    try:
        calls: list[dict] = []

        async def two_rounds(messages, cfg, tools=None):
            calls.append({"messages": [dict(m) for m in messages], "tools": tools})
            if len(calls) == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "我需要先看这个链接",
                    "tool_calls": [tool_call],
                }
            return {
                "role": "assistant",
                "content": "看完了，是讲面条的",
                "reasoning_content": "整理一下要点",
            }

        bot = LLMBot()
        bot._post_chat = two_rounds  # type: ignore[method-assign]
        cfg = LLMConfig(cooldown=0, tool_rounds=3)
        reply, reason = await bot.chat(8801, 1, "张三", "这个链接讲啥 https://example.com/a", cfg)

        check("最终回复正常", reply == "看完了，是讲面条的" and reason == "", f"{reply}/{reason}")
        check("一共两轮请求", len(calls) == 2, str(len(calls)))
        check("第一轮带 tools", bool(calls[0]["tools"]), str(calls[0]["tools"])[:60])
        check("第二轮也带 tools（还没到上限）", bool(calls[1]["tools"]))

        second = calls[1]["messages"]
        assistant = next(m for m in second if m.get("role") == "assistant")
        check("回填 assistant 时带 reasoning_content",
              assistant.get("reasoning_content") == "我需要先看这个链接", str(assistant))
        check("回填 assistant 时带 tool_calls", bool(assistant.get("tool_calls")), str(assistant)[:80])
        tool_msg = next((m for m in second if m.get("role") == "tool"), None)
        check("工具结果以 role=tool 回填",
              tool_msg is not None and "先烧水再下面条" in tool_msg["content"], str(tool_msg)[:80])
        check("tool_call_id 对得上", (tool_msg or {}).get("tool_call_id") == "call_1", str(tool_msg)[:80])

        hist = bot.history(8801)
        check("历史存了 reasoning_content",
              hist[-1].get("reasoning_content") == "整理一下要点", str(hist[-1]))
        check("历史不含工具往返（只有问答对）", len(hist) == 2, str(len(hist)))

        # ---- 最后一轮不带 tools：tool_rounds=1 时第二次请求必须没有 tools
        calls2: list[dict] = []

        async def always_tool(messages, cfg, tools=None):
            calls2.append({"tools": tools})
            return {"role": "assistant", "content": "",
                    "tool_calls": [tool_call]}

        bot2 = LLMBot()
        bot2._post_chat = always_tool  # type: ignore[method-assign]
        reply2, reason2 = await bot2.chat(8802, 1, "李四", "查一下", LLMConfig(cooldown=0, tool_rounds=1))
        check("到上限后最后一次不带 tools", calls2[-1]["tools"] is None, str(len(calls2)))
        check("满轮工具仍无正文 → 明确报错而不是静默空串",
              reply2 is None and "没有给出正文" in str(reason2), f"{reply2}/{reason2}")

        # ---- 接口不认 tools：去掉工具重试一次
        calls3: list[dict] = []

        async def tools_unsupported(messages, cfg, tools=None):
            calls3.append({"tools": tools})
            if len(calls3) == 1:
                raise RuntimeError("HTTP 400: unknown parameter: tools")
            return {"role": "assistant", "content": "没用工具也答了"}

        bot3 = LLMBot()
        bot3._post_chat = tools_unsupported  # type: ignore[method-assign]
        reply3, reason3 = await bot3.chat(8803, 1, "王五", "你好", LLMConfig(cooldown=0))
        check("tools 不被支持时自动降级重试",
              reply3 == "没用工具也答了" and reason3 == "", f"{reply3}/{reason3}")
        check("降级后第二次不带 tools", calls3[-1]["tools"] is None, str(calls3))
        check("第一次确实带了 tools", bool(calls3[0]["tools"]))
    finally:
        webtool.fetch_page = original  # type: ignore[assignment]


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        test_webtool(port)
        test_tools_registry()
        test_fetch_url_tool()
        test_skills()
        test_install_denied()
        test_payload_tools()
        test_chat_tool_loop()
    finally:
        server.shutdown()
        server.server_close()

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
