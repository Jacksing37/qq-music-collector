"""Web UI 核心逻辑测试：schema 自省 / 值转换 / 原子更新。

不依赖 HTTP，直接验证纯函数与 config_manager 写入路径。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

import nonebot

nonebot.init(driver="~fastapi")  # noqa: E402

from src.plugins.music_collector import webui as W  # noqa: E402
from src.plugins.music_collector import webui_frontend as F  # noqa: E402
from src.plugins.music_collector.config import AppConfig, config_manager  # noqa: E402

#: 手写 HTML 里必须显式闭合的骨架标签。``input``/``br``/``img`` 这类不管。
_STRUCT_TAGS = ("div", "section", "main", "aside", "nav", "ul", "ol",
                "details", "form", "table")


def _scan_dom(text: str):
    """扫 DASHBOARD_HTML 的骨架闭合，返回 ``(配对, 错误)``。

    为什么值得测：一个多余的 ``</div>`` 浏览器不会报错，只是把后面的内容甩到
    ``.page`` 外面。2026-11-04 那次线上 bug 就是这么来的——``#page-chat`` 里多了
    一个 ``</div>``，section 被提前关掉，对话记录和输入框掉到 ``<body>`` 直下，
    于是**每个页面底部都挂着一个聊天输入框**，靠肉眼很难发现。
    """
    body = re.sub(r"<script\b.*?</script>", "", text, flags=re.S | re.I)
    tag_re = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>", re.S)
    stack: list[tuple[str, int]] = []
    pairs: list[tuple[str, int, int, str]] = []
    errors: list[str] = []
    for m in tag_re.finditer(body):
        closing, tag, attrs = m.group(1), m.group(2).lower(), m.group(3) or ""
        if tag not in _STRUCT_TAGS or attrs.rstrip().endswith("/"):
            continue
        if not closing:
            stack.append((tag, m.start()))
            continue
        if not stack:
            errors.append(f"多余的 </{tag}>（第 {m.start()} 字符）")
            continue
        open_tag, pos = stack.pop()
        if open_tag != tag:
            errors.append(f"<{open_tag}>（第 {pos} 字符）被 </{tag}> 关掉")
        pairs.append((open_tag, pos, m.end(), body[pos:m.end()][:200]))
    for tag, pos in stack:
        errors.append(f"没有闭合的 <{tag}>（第 {pos} 字符）")
    return body, pairs, errors


def test_dashboard_html_is_well_formed():
    """骨架必须闭合正确，且每张卡片都在某个 ``.page`` 里（否则会全站可见）。"""
    body, pairs, errors = _scan_dom(F.DASHBOARD_HTML)
    assert not errors, "DASHBOARD_HTML 骨架闭合有问题：\n" + "\n".join(errors[:8])

    pages = [(start, end) for tag, start, end, snippet in pairs
             if tag == "section" and re.search(r'class="[^"]*\bpage\b', snippet[:120])]
    assert len(pages) >= 8, f"页面 section 只认出 {len(pages)} 个"

    cards = [m.start() for m in re.finditer(r'<div[^>]*class="[^"]*\bcard\b', body)]
    leaked = [p for p in cards if not any(a < p < e for a, e in pages)]
    assert not leaked, (
        "这些 card 掉到 .page 外面了（会出现在每一个页面上）："
        + ", ".join(repr(body[p:p + 60]) for p in leaked[:4])
    )

    # 聊天测试页的输入区必须真的在这一页里
    chat = next((s, e) for tag, s, e, snip in pairs
                if tag == "section" and 'id="page-chat"' in snip[:120])
    for eid in ("chatLog", "chatInput", "chatSend", "chatTraceCard", "chatReset"):
        pos = body.find(f'id="{eid}"')
        assert pos != -1, f"找不到 #{eid}"
        assert chat[0] < pos < chat[1], f"#{eid} 跑到 #page-chat 外面去了"


def _all_keys():
    return [f["key"] for s in W.SCHEMA for f in s["fields"]]


def test_build_schema_covers_known_keys():
    keys = _all_keys()
    # 嵌套子模型（window.weekly/daily/once）必须被展开成真正的字段，
    # 而不是只出现一个 window.weekly —— 否则「每周·开始/结束」在网页端改不了
    for expected in ("enabled", "collect_override", "window.mode",
                     "window.weekly.start", "window.weekly.archive",
                     "window.daily.end", "window.once.start",
                     "playlist.sharer_style",
                     "playlist.name_template", "card.mode", "intro.text",
                     "cache.keep_days", "clear.keep_days"):
        assert expected in keys, f"schema 缺少 {expected}"
    assert "window.weekly" not in keys, "嵌套模型不该作为字段直接出现"
    nested = next(f for s in W.SCHEMA for f in s["fields"]
                  if f["key"] == "window.weekly.start")
    assert nested["type"] == "str", "展开后的字段应是真实类型而非 model"


def test_enum_options_detected():
    mode = next(f for s in W.SCHEMA for f in s["fields"] if f["key"] == "window.mode")
    assert mode["type"] == "enum"
    assert set(mode["enum"]) == {"weekly", "daily", "once"}

    sharer = next(f for s in W.SCHEMA for f in s["fields"] if f["key"] == "playlist.sharer_style")
    assert set(sharer["enum"]) == {"list", "by_person", "by_name", "none"}


def test_coerce_value_types():
    assert W.coerce_value("bool", None, "true") is True
    assert W.coerce_value("bool", None, "0") is False
    assert W.coerce_value("int", None, "5") == 5
    assert W.coerce_value("float", None, "1.5") == 1.5
    assert W.coerce_value("intlist", None, "1, 2,3") == [1, 2, 3]
    assert W.coerce_value("enum", ["a", "b"], "a") == "a"
    try:
        W.coerce_value("enum", ["a", "b"], "c")
        raise AssertionError("应当抛错")
    except ValueError:
        pass


def test_apply_updates_atomic(tmp_path: Path):
    orig_path = config_manager.path
    config_manager.path = tmp_path / "config.yaml"
    config_manager.load()

    ok, errs = W.apply_updates({"window.mode": "daily", "playlist.seq": 7})
    assert ok, errs
    assert config_manager.config.window.mode == "daily"
    assert config_manager.config.playlist.seq == 7

    # 非法枚举 -> 整体回滚，已改字段不残留
    ok2, errs2 = W.apply_updates({"playlist.sharer_style": "bogus"})
    assert not ok2
    assert "playlist.sharer_style" in errs2
    assert config_manager.config.playlist.sharer_style == "list"

    config_manager.path = orig_path
    config_manager.load()


def test_coerce_value_map():
    assert W.coerce_value("map", None, {"菜老名": "Jacksing"}) == {"菜老名": "Jacksing"}
    assert W.coerce_value("map", None, "菜老名=Jacksing\n# 注释\n星仔=Star") == {
        "菜老名": "Jacksing", "星仔": "Star"
    }
    assert W.coerce_value("map", None, "") == {}


def test_sharer_aliases_map_type():
    f = W.KEY_INDEX["playlist.sharer_aliases"]
    assert f["type"] == "map"
    # 仍出现在 schema 里（前端 form 渲染时再跳过 map 类型，走独立编辑页）
    assert any(x["key"] == "playlist.sharer_aliases" for s in W.SCHEMA for x in s["fields"])


def test_current_values_map_whole_dict():
    vals = W.current_values()
    assert "playlist.sharer_aliases" in vals
    assert isinstance(vals["playlist.sharer_aliases"], dict)
    # 不应把映射里的每个昵称拍平成独立配置项
    assert "playlist.sharer_aliases.菜老名" not in vals


def test_apply_updates_map(tmp_path: Path):
    orig_path = config_manager.path
    config_manager.path = tmp_path / "config.yaml"
    config_manager.load()
    ok, errs = W.apply_updates({"playlist.sharer_aliases": {"菜老名": "Jacksing"}})
    assert ok, errs
    assert config_manager.config.playlist.sharer_aliases == {"菜老名": "Jacksing"}
    config_manager.path = orig_path
    config_manager.load()


def test_apply_updates_rollback_save_failure_no_500(tmp_path: Path):
    """回滚时写盘失败（文件被锁/只读）也不能抛异常变成 500。"""
    import os
    import stat

    orig_path = config_manager.path
    cfg_file = tmp_path / "config.yaml"
    config_manager.path = cfg_file
    config_manager.load()

    os.chmod(cfg_file, stat.S_IREAD)  # 只读 -> save() 抛 PermissionError
    try:
        ok, errs = W.apply_updates({"playlist.sharer_style": "bogus"})
    finally:
        os.chmod(cfg_file, stat.S_IWRITE)
    assert not ok
    assert "playlist.sharer_style" in errs
    # 内存态已回滚，不残留
    assert config_manager.config.playlist.sharer_style == "list"

    config_manager.path = orig_path
    config_manager.load()


def test_netease_section_and_secret_fields():
    """「网易云登录」分组要在配置页出现，密码字段要按 secret 渲染。"""
    keys = _all_keys()
    for expected in ("netease.auto_relogin", "netease.phone",
                     "netease.password", "netease.password_md5",
                     "netease.countrycode", "netease.relogin_cooldown"):
        assert expected in keys, f"schema 缺少 {expected}"

    sec = next((s for s in W.SCHEMA if s["key"] == "netease"), None)
    assert sec is not None, "缺少 netease 分组"
    assert sec["title"] == "网易云登录", sec["title"]

    assert W.KEY_INDEX["netease.password"]["secret"] is True
    assert W.KEY_INDEX["netease.password_md5"]["secret"] is True
    assert W.KEY_INDEX["netease.phone"]["secret"] is False


def test_chat_test_quote_inputs_clean():
    """「模拟引用」那三个框必须是空默认值、并关掉浏览器自动填充。

    线上反馈：那三个框会自己冒出别处的数据。它们只有 placeholder、没有 name，
    Chrome 自动填充 / 密码管理器 / 表单恢复会把它们当「姓名·电话」填上，
    于是测试带着一段根本没写的引用跑，看 trace 就像「凭空多出来的数据」。
    """
    body = F.DASHBOARD_HTML
    for eid in ("chatQuoteNick", "chatQuoteQq", "chatQuoteText"):
        m = re.search(r'<input[^>]*id="%s"[^>]*>' % eid, body)
        assert m, f"找不到 #{eid}"
        tag = m.group(0)
        assert 'autocomplete="off"' in tag, f"#{eid} 少了 autocomplete=off：{tag}"
        assert "value=" not in tag, f"#{eid} 不该带默认值：{tag}"
    # 只对「下一条」生效：发完必须清掉，否则后面每一句都还带着它
    assert "scrubChatQuote(true)" in body, "模拟引用发完没有清空"
    # 工具轮数原先只画在界面上、请求里根本没带这个字段
    assert 'id="chatRounds"' in body and "rounds: Number(" in body, "工具轮数没发出去"


def test_chat_test_model_override():
    """「聊天测试」能临时换模型（页面上默认填配置里那个），且绝不写回配置。"""
    from src.plugins.music_collector.config import LLMConfig

    base = LLMConfig(model="deepseek-v4.1-flash", cooldown=5, tools=False, tool_rounds=3)
    cfg = W.chat_test_config(base, {"model": "  qwen3-max ", "tools": True, "rounds": 99})
    assert cfg.model == "qwen3-max", cfg.model
    assert cfg.cooldown == 0, "测试不该占真群的冷却"
    assert cfg.tools is True
    assert cfg.tool_rounds == 8, "轮数上限 8，别让人手滑写 99"

    # 留空 / 全空格 = 用配置里的那个
    assert W.chat_test_config(base, {}).model == "deepseek-v4.1-flash"
    assert W.chat_test_config(base, {"model": "   "}).model == "deepseek-v4.1-flash"
    # 传进来的配置对象不许被改
    assert base.model == "deepseek-v4.1-flash" and base.cooldown == 5 and base.tools is False


def test_chat_page_has_model_box():
    """界面上得有模型输入框，请求体里也得真的带上它。"""
    body = F.DASHBOARD_HTML
    assert 'id="chatModel"' in body, "聊天测试缺模型输入框"
    assert 'id="chatModelReset"' in body, "缺「用配置里的」按钮"
    assert "syncChatModel" in body, "进页面没去取配置里的模型"
    assert "model: ((" in body, "发送时没带 model 字段"


def test_bot_groups_reference_only_existing_keys():
    """机器人页的分组卡片里，每个键都必须在 BOT_SCHEMA 里真实存在。

    ``BOT_GROUPS`` 是前端手写的白名单：键名写错一个字母**不会报错**，只会让那个字段
    静默消失（既不在卡片里、也不在「其它」里），只有人肉翻页面才看得出来。
    这条断言把它钉死，顺带保证新加的「说话时机」那 16 个字段别再掉回「其它」卡。
    """
    import re
    from pathlib import Path

    src = Path(F.__file__).read_text(encoding="utf-8")
    seg = src[src.index("const BOT_GROUPS=["): src.index("BOT_GROUPS.forEach")]
    grouped = re.findall(r'"([a-z_]+\.[a-z_0-9]+)"', seg)

    bot_keys = {f["key"] for s in W.BOT_SCHEMA for f in s["fields"]}
    unknown = [k for k in grouped if k not in bot_keys]
    assert not unknown, f"BOT_GROUPS 里有 schema 中不存在的键（写错会静默丢字段）: {unknown}"

    dupes = {k for k in grouped if grouped.count(k) > 1}
    assert not dupes, f"同一个键被放进多张卡片: {sorted(dupes)}"

    speak = [k for k in grouped if ".speak_" in k]
    assert len(speak) == 22, f"speak_* 应有 22 个进了卡片，实际 {len(speak)}: {sorted(speak)}"

    # 本次新增的几组都该有专属卡片，不许再落在「其它」里
    for key in (
        "llm.reply_split",
        "llm.time_guard",
        "llm.daily_budget",
        "memory.recall_enabled",
        "memory.buckets_enabled",
        "persona.heart_enabled",
    ):
        assert key in grouped, f"{key} 没有归到任何分组卡片"


if __name__ == "__main__":
    import tempfile

    _tmp = Path(tempfile.mkdtemp())
    test_dashboard_html_is_well_formed()
    test_chat_test_quote_inputs_clean()
    test_chat_test_model_override()
    test_chat_page_has_model_box()
    test_build_schema_covers_known_keys()
    test_enum_options_detected()
    test_coerce_value_types()
    test_apply_updates_atomic(_tmp)
    test_coerce_value_map()
    test_sharer_aliases_map_type()
    test_current_values_map_whole_dict()
    test_apply_updates_map(_tmp)
    test_apply_updates_rollback_save_failure_no_500(_tmp)
    test_netease_section_and_secret_fields()
    test_bot_groups_reference_only_existing_keys()
    print("webui tests OK")
