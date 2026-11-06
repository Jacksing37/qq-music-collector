"""真实浏览器（``browse`` 工具）相关契约的**离线**回归。

覆盖四块，全部是纯函数 / 配置 / 元数据断言——**不联网、不连 CDP、不起任何进程**：

0. 环境自检：数据目录必须被 ``MUSIC_DATA_DIR`` 挪到临时目录；
1. ``config.LLMConfig`` 的 ``browser*`` 配置项：默认值（尤其是「默认关」）与上下界；
2. ``webui.FIELD_META`` 里那七条中文文案（标题中文、有说明、且真的进了 schema）；
3. ``browser.py`` 的四条纯函数：``detect_challenge`` / ``parse_allow_hosts`` /
   ``host_matches`` / ``format_report``，用的都是线上真见过的那几种拦截页样本；
4. ``tools.build()`` 的注册契约：总闸、群白名单（``browser_groups``）三条路径。

⚠️ **依赖 ``browser.py`` 实现落地后通过**：写这份脚本时 ``music_collector/browser.py`` 与
``webui.FIELD_META`` 里那七条 ``llm.browser*`` 正在同时开发，落地前跑必然是红的——那是预期
的，别把断言放宽：

- ``browser.py`` 不存在 → 第 3 节每条断言都失败，最后一行会写明 ImportError 原因；
- ``FIELD_META`` 还没补 → 第 2 节 29 条失败（标题仍是 ``Browser Groups`` 这种英文兜底名）。

实现落地后必须全绿；红了就是契约被破坏（十有八九是「写了 FIELD_META 但写在
``SCHEMA = build_schema()`` 之后」——本地查字典全有、线上页面全是英文）。

运行: python tests/test_browser.py       # MUSIC_DATA_DIR 会自分到临时目录
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

#: 回归**绝不能碰真实 data/**：``config`` 在导入时就读这个环境变量，所以必须写在这里。
#: 用 setdefault 而不是硬覆盖：``tests/run_all.py`` 已经给每个子进程发了一个临时目录，
#: 那种情况下直接沿用（同样是临时目录，只是全量回归共用一个，便于排查）。
os.environ.setdefault("MUSIC_DATA_DIR", tempfile.mkdtemp(prefix="music-test-browser-"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from pydantic import ValidationError  # noqa: E402

#: 插件包整体导入兜一层：``tools.py`` 顶部就写死了 ``from . import browser``，所以
#: ``browser.py`` 还没落地时**连 ``tools`` / ``webui`` 都 import 不了**（整包起不来）。
#: 这里把「实现还没落地」变成一句读得懂的失败原因——**不是跳过**：照样 ``sys.exit(1)``，
#: 一条断言都不会被绿色掩盖，只是省掉一段看不懂的 traceback。
try:
    from music_collector import browser as br  # noqa: E402
    from music_collector import tools as tl  # noqa: E402
    from music_collector import webui as W  # noqa: E402
    from music_collector.config import DATA_DIR, LLMConfig  # noqa: E402
except Exception as _exc:  # noqa: BLE001
    print(f"\n导入 music_collector 失败：{type(_exc).__name__}: {_exc}")
    print("失败原因：music_collector/browser.py 还没落地（tools.py / webui.py 现在都依赖它），"
          "本脚本一行断言都跑不了。实现落地后重跑即可")
    sys.exit(1)

PASSED = 0
FAILED = 0
#: 当前小节标题 + 出过错的小节（收尾那行要靠它说清「红在哪一节」）
_SECTION = ""
FAILED_SECTIONS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        if _SECTION and _SECTION not in FAILED_SECTIONS:
            FAILED_SECTIONS.append(_SECTION)
        print(f"  [FAIL] {name}" + (f"  →  {detail}" if detail else ""))


def section(title: str) -> None:
    global _SECTION
    _SECTION = title
    print(f"\n{title}")


def _has_chinese(text: str) -> bool:
    """有没有汉字。用来抓「文案还是英文兜底名」这类问题。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _any(text: str, words: tuple[str, ...]) -> str:
    """返回第一个命中的关键词（空串 = 一个都没命中）。提示语只要求「这类话」，不钉死措辞。"""
    return next((w for w in words if w in (text or "")), "")


# ====================================================================== 0. 环境
def test_env() -> None:
    section("0. 环境自检（不碰真实 data/）")
    raw = os.environ.get("MUSIC_DATA_DIR", "")
    check("MUSIC_DATA_DIR 已设置成绝对路径的临时目录",
          bool(raw) and Path(raw).is_absolute(), repr(raw))
    check("config.DATA_DIR 跟着指到临时目录，不是仓库里的 data/",
          DATA_DIR == Path(raw).expanduser() and DATA_DIR != (ROOT / "data"), str(DATA_DIR))


# ====================================================================== 1. 配置
def test_config_defaults() -> None:
    section("1. config.LLMConfig 的 browser* 配置项")
    cfg = LLMConfig()

    # 「默认关」是安全要求：模型能驱动的浏览器 = 用服务器自己的身份上网（带着全部 cookie）
    check("browser 默认 False（默认关是安全要求）", cfg.browser is False, str(cfg.browser))
    check("browser_groups 默认空列表（不按群限制）", cfg.browser_groups == [],
          str(cfg.browser_groups))
    check("browser_groups 用的是默认工厂（两个实例不共享同一个 list）",
          LLMConfig().browser_groups is not LLMConfig().browser_groups)
    check("browser_cdp 默认 http://127.0.0.1:9222",
          cfg.browser_cdp == "http://127.0.0.1:9222", cfg.browser_cdp)
    check("browser_timeout 默认 30.0 秒", cfg.browser_timeout == 30.0, str(cfg.browser_timeout))
    check("browser_chars 默认 3000 字", cfg.browser_chars == 3000, str(cfg.browser_chars))
    check("browser_allow_hosts 默认空串（= 不限域名）", cfg.browser_allow_hosts == "",
          repr(cfg.browser_allow_hosts))
    check("browser_max_steps 默认 3", cfg.browser_max_steps == 3, str(cfg.browser_max_steps))

    def rejected(**kw) -> bool:
        """pydantic 拒绝返回 True；**只有** ValidationError 算拒绝。"""
        try:
            LLMConfig(**kw)
        except ValidationError:
            return True
        except Exception:  # noqa: BLE001 — 别的异常不算「被校验拦住」
            return False
        return False

    def accepted(**kw) -> bool:
        try:
            LLMConfig(**kw)
        except ValidationError:
            return False
        return True

    # ---- browser_timeout：ge=5, le=120
    check("browser_timeout=5 是合法下界", accepted(browser_timeout=5),
          "5 被拒了，说明下界写错")
    check("browser_timeout=120 是合法上界", accepted(browser_timeout=120),
          "120 被拒了，说明上界写错")
    check("browser_timeout=4.9 越界被拒（<ge=5）", rejected(browser_timeout=4.9),
          "4.9 竟然通过了")
    check("browser_timeout=121 越界被拒（>le=120）", rejected(browser_timeout=121),
          "121 竟然通过了")

    # ---- browser_chars：ge=200, le=12000
    check("browser_chars=200 是合法下界", accepted(browser_chars=200), "200 被拒了")
    check("browser_chars=12000 是合法上界", accepted(browser_chars=12000), "12000 被拒了")
    check("browser_chars=199 越界被拒（<ge=200）", rejected(browser_chars=199),
          "199 竟然通过了")
    check("browser_chars=12001 越界被拒（>le=12000）", rejected(browser_chars=12001),
          "12001 竟然通过了")

    # ---- browser_max_steps：ge=0, le=5（0 = 只读不点，是允许的）
    check("browser_max_steps=0 合法（0 = 只读不点，最安全）", accepted(browser_max_steps=0),
          "0 被拒了——那样就没法只读浏览")
    check("browser_max_steps=5 是合法上界", accepted(browser_max_steps=5), "5 被拒了")
    check("browser_max_steps=-1 越界被拒（<ge=0）", rejected(browser_max_steps=-1),
          "-1 竟然通过了")
    check("browser_max_steps=6 越界被拒（>le=5）", rejected(browser_max_steps=6),
          "6 竟然通过了")


# ====================================================================== 2. FIELD_META
#: 七条必须存在的网页端文案。``(dotted key, 字段名)``——字段名用来算它被自动兜底成什么
#: （``browser_groups`` → ``Browser Groups``），命中兜底名就说明 ``FIELD_META`` 没生效。
_BROWSER_FIELDS: tuple[tuple[str, str], ...] = (
    ("llm.browser", "browser"),
    ("llm.browser_groups", "browser_groups"),
    ("llm.browser_cdp", "browser_cdp"),
    ("llm.browser_timeout", "browser_timeout"),
    ("llm.browser_chars", "browser_chars"),
    ("llm.browser_allow_hosts", "browser_allow_hosts"),
    ("llm.browser_max_steps", "browser_max_steps"),
)


def test_field_meta() -> None:
    """七条 ``llm.browser*`` 必须都在 ``FIELD_META`` 里，且标题是中文。

    写法照 ``tests/test_webui.py::test_bot_card_fields_have_chinese_labels``：除了查
    ``FIELD_META`` 字典本身，还要跟 ``SCHEMA`` 里渲染出来的 ``label`` 对一遍——**只查
    字典是不够的**，元数据写在 ``SCHEMA = build_schema()`` 之后就「字典里有、页面上没有」，
    页面照样显示 ``Browser Groups``（2026-11-05 六条主动搭话的文案就这么翻过车）。
    """
    section("2. webui.FIELD_META 的七条 llm.browser* 中文文案")
    for key, name in _BROWSER_FIELDS:
        meta = W.FIELD_META.get(key)
        check(f"{key} 在 FIELD_META 里", meta is not None, "字典里没有这条")
        if meta is None:
            # 补齐剩下三条的计数：「七个键 × 四条」，红的时候一眼看出少了哪条
            for sub in ("标题是中文", "有中文说明", "已进 SCHEMA 且用的是这条标题"):
                check(f"{key} {sub}", False, "FIELD_META 里没有这条")
            continue

        label = str(meta[0] or "")
        hint = str(meta[1] or "") if len(meta) > 1 else ""
        auto = name.replace("_", " ").title()      # webui._field_desc 的兜底文案
        field = W.KEY_INDEX.get(key) or {}

        check(f"{key} 标题是中文", _has_chinese(label) and label != auto, repr(label))
        check(f"{key} 有中文说明", _has_chinese(hint), repr(hint[:40]))
        check(f"{key} 已进 SCHEMA 且 label 就是 FIELD_META 那条（没写在 build_schema 之后）",
              field.get("label") == label and field.get("label") != auto,
              f"schema label={field.get('label')!r} / meta label={label!r}")

    check("这七条都能在 KEY_INDEX 里查到（说明配置字段真的存在）",
          all(k in W.KEY_INDEX for k, _ in _BROWSER_FIELDS),
          str([k for k, _ in _BROWSER_FIELDS if k not in W.KEY_INDEX]))


# ====================================================================== 3. 纯函数
# ---- 3.1 detect_challenge 的样本（都是线上真见过的拦截页形状，不联网）----
ZHIHU_TITLE = "安全验证 - 知乎"
ZHIHU_URL = "https://www.zhihu.com/account/unhuman?type=U4E3Z1"
ZHIHU_TEXT = "请您登录后查看更多专业优质内容。 登录知乎 · 意见反馈"

CLOUDFLARE_TITLE = "Just a moment..."
CLOUDFLARE_URL = "https://www.cloudflare.com/"
CLOUDFLARE_TEXT = ""

#: 淘宝的滑块页：标题和正文都很「正常」，只有正文里的那几句话暴露它
ALI_TITLE = "淘宝网 - 淘！我喜欢"
ALI_URL = "https://s.taobao.com/search?q=耳机"
ALI_TEXT = "请完成安全验证"

#: 腾讯的滑块页：标题是站点自己的，正文要求拖滑块
TENCENT_TITLE = "QQ音乐-千万正版音乐海量无损曲库"
TENCENT_URL = "https://y.qq.com/n/ryqq/toplist"
TENCENT_TEXT = "请按住左边按钮拖动滑块完成拼图"

#: 百度：整页被重定向到 wappass 的图形验证码中间页
BAIDU_TITLE = "百度安全验证"
BAIDU_URL = "https://wappass.baidu.com/static/captcha/tuxing.html?ak=x&backurl=y"
BAIDU_TEXT = "网络不给力，请稍后重试"

CAPTCHA_TITLE = "安全验证"
CAPTCHA_URL = "https://example.com/login"
CAPTCHA_TEXT = "请输入验证码后继续访问"

BILI_TITLE = "【官方 MV】Never Gonna Give You Up - Rick Astley_哔哩哔哩_bilibili"
BILI_URL = "https://www.bilibili.com/video/BV1htTe6qEU1"
#: 1400 字正常正文：够长、没有任何拦截特征，**必须放行**
BILI_SENTENCE = (
    "今天聊聊这首歌的 MV。1987 年 Rick Astley 的 Never Gonna Give You Up 发行之后，"
    "这支 MV 因为一段舞蹈和一个转身成了互联网上最著名的梗。"
    "画面里他穿着米色风衣，在酒吧里跟着节奏摇摆，镜头来回切换乐队和舞者。"
    "歌词讲的是不放弃一段感情，四四拍的合成器流行曲式到今天听起来依然不过时。"
)
BILI_TEXT = (BILI_SENTENCE * 20)[:1400]

#: 「正文里恰好有『登录』两个字」但页面完全正常——按「宁可不拦」必须放行
NORMAL_LOGIN_TITLE = "如何评价主流音乐 App 的推荐算法 - 知乎"
NORMAL_LOGIN_URL = "https://www.zhihu.com/question/123456789/answer/987654321"
NORMAL_LOGIN_TEXT = ("登录知乎可以看更多，不过这篇回答本身是完整的。" + BILI_SENTENCE * 10)[:1400]


def _challenge(title: str, url: str, text: str) -> tuple[bool, str]:
    """调 ``detect_challenge``，把「模块缺失 / 抛异常」变成一条失败而不是让脚本崩掉。"""
    try:
        got = br.detect_challenge(title, url, text)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if not isinstance(got, str):
        return False, f"返回了 {type(got).__name__}，契约要求 str"
    return True, got


def test_detect_challenge() -> None:
    section("3.1 browser.detect_challenge（'' = 正常页，否则一句中文原因）")
    check("正常页样本正文正好 1400 字（太长/太短都会让「不误判」那条失去意义）",
          len(BILI_TEXT) == 1400, str(len(BILI_TEXT)))

    positives = [
        ("知乎 /account/unhuman 页", ZHIHU_TITLE, ZHIHU_URL, ZHIHU_TEXT),
        ("Cloudflare 的 Just a moment...", CLOUDFLARE_TITLE, CLOUDFLARE_URL, CLOUDFLARE_TEXT),
        ("阿里/淘宝滑块（正文含「请完成安全验证」）", ALI_TITLE, ALI_URL, ALI_TEXT),
        ("腾讯滑块（正文含「拖动滑块」）", TENCENT_TITLE, TENCENT_URL, TENCENT_TEXT),
        ("百度 wappass 中间页", BAIDU_TITLE, BAIDU_URL, BAIDU_TEXT),
        ("验证码页（正文含「验证码」+ 标题含「验证」）", CAPTCHA_TITLE, CAPTCHA_URL, CAPTCHA_TEXT),
    ]
    reasons: list[str] = []
    for label, title, url, text in positives:
        ok, got = _challenge(title, url, text)
        if ok and got.strip():
            reasons.append(got)
        check(f"认出{label}", ok and bool(got.strip()), f"{ok} {got!r}")

    check("命中时给的是一句中文原因（不是 True/False，也不是英文兜底）",
          len(reasons) == len(positives) and all(_has_chinese(r) for r in reasons),
          str(reasons))

    ok, got = _challenge(BILI_TITLE, BILI_URL, BILI_TEXT)
    check("正常 B 站长视频页必须返回 ''（1400 字正文，含「MV」「音乐」也不是拦截页）",
          ok and got == "", f"{ok} {got!r}")

    ok, got = _challenge(NORMAL_LOGIN_TITLE, NORMAL_LOGIN_URL, NORMAL_LOGIN_TEXT)
    check("正文里出现「登录」但标题正常的页面必须返回 ''（宁可不拦，别误判正常页）",
          ok and got == "", f"{ok} {got!r}")

    # 空页面（about:blank）返回空还是非空都算合理，这里不断言，避免跟实现细节耦合。


def test_parse_allow_hosts() -> None:
    section("3.2 browser.parse_allow_hosts（去空、去重、小写）")
    cases = [
        ("a.com, .b.com ,,c.com", ["a.com", ".b.com", "c.com"],
         "契约样例：逗号分隔 + 多余空格 + 空段，顺序保持"),
        ("A.COM,B.com", ["a.com", "b.com"], "大写归一成小写"),
        ("a.com, a.com ,A.com", ["a.com"], "去重（只留第一次出现的那个）"),
        ("", [], "空串 → 空列表"),
        ("  ,  , ", [], "只有空格和逗号 → 空列表"),
        (" example.com ", ["example.com"], "首尾空格要去掉"),
    ]
    for raw, want, label in cases:
        try:
            got = br.parse_allow_hosts(raw)
        except Exception as exc:  # noqa: BLE001
            got = f"{type(exc).__name__}: {exc}"
        check(f"parse_allow_hosts：{label}", got == want, f"{got!r} != {want!r}")


def test_host_matches() -> None:
    section("3.3 browser.host_matches（'' / 空列表 = 不限制）")
    cases = [
        ("example.com", ["example.com"], True, "不带点的精确项命中自身"),
        ("sub.example.com", ["example.com"], False,
         "不带点的精确项只匹配自身，子域不命中"),
        ("example.com", [".example.com"], True, "带点的后缀命中裸域"),
        ("sub.example.com", [".example.com"], True, "带点的后缀命中子域"),
        ("a.b.example.com", [".example.com"], True, "多级子域也命中"),
        ("notexample.com", [".example.com"], False,
         "按点边界比：notexample.com 不是 example.com 的子域"),
        ("evil-example.com", ["example.com"], False, "精确项不能被前缀/后缀骗过"),
        ("blog.example.com", ["a.com", ".example.com"], True, "多条模式命中任意一条即可"),
        ("anything.example", [], True, "白名单为空 = 不限制"),
    ]
    for host, patterns, want, label in cases:
        try:
            got = br.host_matches(host, patterns)
        except Exception as exc:  # noqa: BLE001
            got = f"{type(exc).__name__}: {exc}"
        check(f"host_matches：{label}", got is want, f"{got!r} != {want!r}")

    # 契约里的「'' 或空列表 = 不限制」说的是**白名单**一侧（browser_allow_hosts 解析出来为空，
    # 上面那条已覆盖）。「空 host + 非空白名单」两种读法都讲得通——判不出主机名时拒绝更安全，
    # 所以这里只钉「不抛异常、返回 bool」，不硬猜放行还是拒绝。
    try:
        got = br.host_matches("", ["example.com"])
    except Exception as exc:  # noqa: BLE001
        got = f"{type(exc).__name__}: {exc}"
    check("host_matches：空 host 不抛异常、返回 bool", isinstance(got, bool), repr(got))


# ---- 3.4 format_report ----
HUMAN_REASON = "需要人工过验证"
HUMAN_SHOT = "/tmp/browse/zhihu-unhuman.png"
#: browse() 返回字典的形状（键名固定），needs_human=True 的那种
HUMAN_RES = {
    "ok": True,
    "url": ZHIHU_URL,
    "title": ZHIHU_TITLE,
    "text": "请您登录后查看更多专业优质内容。",
    "chars": 18,
    "needs_human": True,
    "reason": HUMAN_REASON,
    "screenshot": HUMAN_SHOT,
    "steps_done": 0,
    "error": "",
}

ERROR_TEXT = "连接浏览器失败：连接 CDP http://127.0.0.1:9222 被拒绝（浏览器没起来？）"
ERROR_RES = {
    "ok": False,
    "url": "https://www.bilibili.com/video/BV1htTe6qEU1",
    "title": "",
    "text": "",
    "chars": 0,
    "needs_human": False,
    "reason": "",
    "screenshot": "",
    "steps_done": 0,
    "error": ERROR_TEXT,
}

LONG_RES = {
    "ok": True,
    "url": BILI_URL,
    "title": "超长正文页",
    "text": "算" * 9000,
    "chars": 9000,
    "needs_human": False,
    "reason": "",
    "screenshot": "",
    "steps_done": 2,
    "error": "",
}

NORMAL_RES = {
    "ok": True,
    "url": BILI_URL,
    "title": BILI_TITLE,
    "text": BILI_TEXT,
    "chars": len(BILI_TEXT),
    "needs_human": False,
    "reason": "",
    "screenshot": "",
    "steps_done": 1,
    "error": "",
}


def _report(res: dict, **kw) -> tuple[bool, str]:
    try:
        return True, br.format_report(res, **kw)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def test_format_report() -> None:
    section("3.4 browser.format_report（拼给模型看的中文文本）")

    # ---- 要人工过验证
    ok, out = _report(HUMAN_RES)
    check("needs_human：输出里有「验证」二字", ok and "验证" in out, out[:120])
    check("needs_human：输出里有原因", ok and HUMAN_REASON in out, out[:120])
    check("needs_human：输出里有截图路径（模型/人要能看到截图）",
          ok and HUMAN_SHOT in out, out[:160])
    check("needs_human：给出「过完再让我读一次」这类可操作提示",
          ok and bool(_any(out, ("再让我", "再读", "读一次", "再来", "再试", "过完"))),
          out[-160:])

    # ---- 出错
    ok, out = _report(ERROR_RES)
    check("error 非空：输出里带这句 error 原文", ok and ERROR_TEXT in out, out[:160])
    check("error 非空：输出里说明这次没读到东西",
          ok and bool(_any(out, ("失败", "没读到", "打不开", "错误"))), out[:120])

    # ---- 超长正文
    ok, out = _report(LONG_RES, max_chars=500)
    check("正文超上限：输出被截到 500 字量级（不是把 9000 字全塞进去）",
          ok and len(out) <= 1200, f"len={len(out) if ok else out}")
    check("正文超上限：带省略号或「已截断」说明",
          ok and bool(_any(out, ("…", "...", "截断", "省略", "只显示", "超出"))),
          out[-160:] if ok else out)
    ok, out = _report(LONG_RES, max_chars=12000)
    check("上限给够时不截断（max_chars=12000 → 9000 字正文完整给出）",
          ok and len(out) >= 9000, f"len={len(out) if ok else out}")

    # ---- 正常结果
    ok, out = _report(NORMAL_RES)
    check("正常结果：带标题", ok and BILI_TITLE in out, out[:120])
    check("正常结果：带最终 URL（可能跟请求地址不一样）", ok and BILI_URL in out, out[:160])
    check("正常结果：带正文", ok and BILI_TEXT[:60] in out, out[:160])


# ====================================================================== 4. 工具注册
def test_tools_registration() -> None:
    section("4. tools.build() 的 browse 注册契约（总闸 / 群白名单）")

    def names(cfg: LLMConfig, group_id: int = 0) -> list[str]:
        return [t.name for t in tl.build(cfg, group_id=group_id)[0]]

    off = names(LLMConfig(tools=True, web_tool=True, web_search=True, browser=False))
    check("browser=False 时没有 browse（即使 web_tool/web_search 都开着）",
          "browse" not in off, str(off))

    gated = names(LLMConfig(tools=True, browser=True, browser_groups=[123]), group_id=456)
    check("browser_groups=[123] 且 group_id=456 → 没有 browse",
          "browse" not in gated, str(gated))
    check("browser_groups=[123] 且 group_id=123 → 有 browse",
          "browse" in names(LLMConfig(tools=True, browser=True, browser_groups=[123]),
                            group_id=123),
          str(names(LLMConfig(tools=True, browser=True, browser_groups=[123]), group_id=123)))
    check("browser_groups=[] → 不按群限制，有 browse",
          "browse" in names(LLMConfig(tools=True, browser=True, browser_groups=[]), group_id=999),
          str(names(LLMConfig(tools=True, browser=True, browser_groups=[]), group_id=999)))

    all_off = names(LLMConfig(tools=False, browser=True, browser_groups=[]))
    check("总闸 tools=False → 一个工具都没有（browser 也救不回来）",
          all_off == [], str(all_off))

    # build() 返回前还要过 ALLOWED_TOOL_NAMES：名字不在白名单里就永远发不到 QQ 会话
    check("browse 在 ALLOWED_TOOL_NAMES 里（否则 build 会把它过滤掉）",
          "browse" in tl.ALLOWED_TOOL_NAMES, str(sorted(tl.ALLOWED_TOOL_NAMES)))


def main() -> None:
    print(f"临时数据目录 {os.environ.get('MUSIC_DATA_DIR')}（不碰真实 data/）")
    test_env()
    test_config_defaults()
    test_field_meta()
    test_detect_challenge()
    test_parse_allow_hosts()
    test_host_matches()
    test_format_report()
    test_tools_registration()

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        # 最后一行必须是「为什么红」：跑回归的人只看这一行
        print(f"失败原因：{FAILED} 条断言不成立，出问题的小节 → "
              + " / ".join(FAILED_SECTIONS))
        sys.exit(1)


if __name__ == "__main__":
    main()
