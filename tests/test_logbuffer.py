"""网页端「运行日志」功能测试。

覆盖三块：
1. ``logbuffer`` 环形缓冲本体：等级阈值、级别/关键词过滤、截尾取最新、环形淘汰、
   容量调整、清空、统计字段；
2. 与 loguru 的接线：``install()`` 幂等（不能装出两个 sink 导致日志翻倍）、
   等级热更新、``uninstall()`` 能摘干净、可选落盘；
3. 前后端契约：sidebar 入口 / 页面区块 / 接口路径 / 路由注册 / schema 里
   有 logs.* 字段与中文分组名 —— 防止以后改了一头忘了另一头。
"""

import asyncio
import inspect
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")

from nonebot.log import logger  # noqa: E402

from music_collector import logbuffer, webui  # noqa: E402


def _reset() -> None:
    """每个用例前把缓冲与 sink 恢复干净（本文件同进程串跑）。"""
    logbuffer.uninstall()
    logbuffer.buffer.clear()
    logbuffer.buffer.set_level(logbuffer.DEFAULT_LEVEL)
    logbuffer.buffer.set_capacity(logbuffer.DEFAULT_LINES)


# ---------------------------------------------------------------- 缓冲本体


def test_push_and_snapshot_shape() -> None:
    _reset()
    logbuffer.buffer.push("INFO", "music_collector.demo", "hello")
    items = logbuffer.buffer.snapshot()
    assert len(items) == 1, items
    it = items[0]
    for key in ("ts", "time", "level", "levelno", "name", "message", "exc"):
        assert key in it, key
    assert it["level"] == "INFO"
    assert it["name"] == "music_collector.demo"
    assert it["message"] == "hello"
    assert it["exc"] is None
    assert len(it["time"]) == 19, it["time"]


def test_level_threshold_discards_low_level() -> None:
    _reset()
    base = logbuffer.stats()["total"]
    logbuffer.buffer.set_level("WARNING")
    logbuffer.buffer.push("DEBUG", "x", "不该留")
    logbuffer.buffer.push("INFO", "x", "也不该留")
    logbuffer.buffer.push("WARNING", "x", "留下")
    logbuffer.buffer.push("ERROR", "x", "留下")
    msgs = [i["message"] for i in logbuffer.buffer.snapshot()]
    assert msgs == ["留下", "留下"], msgs
    # 被丢弃的不该计入 total（total 是「实际记录条数」，且不随 clear 归零）
    assert logbuffer.stats()["total"] - base == 2


def test_snapshot_level_filter() -> None:
    _reset()
    for lv in ("INFO", "WARNING", "ERROR"):
        logbuffer.buffer.push(lv, "x", f"msg-{lv}")
    assert [i["level"] for i in logbuffer.buffer.snapshot(level="INFO")] == ["INFO", "WARNING", "ERROR"]
    assert [i["level"] for i in logbuffer.buffer.snapshot(level="WARNING")] == ["WARNING", "ERROR"]
    assert [i["level"] for i in logbuffer.buffer.snapshot(level="error")] == ["ERROR"]
    # 认不出的级别名当作不过滤，而不是筛成空
    assert len(logbuffer.buffer.snapshot(level="NOPE")) == 3


def test_snapshot_query_matches_message_name_and_exc() -> None:
    _reset()
    logbuffer.buffer.push("INFO", "music_collector.archiver", "开始归档")
    logbuffer.buffer.push("ERROR", "music_collector.netease_api", "写简介失败", exc="Traceback\nValueError: boom")
    assert len(logbuffer.buffer.snapshot(query="归档")) == 1
    assert len(logbuffer.buffer.snapshot(query="netease_api")) == 1
    assert len(logbuffer.buffer.snapshot(query="ValueError")) == 1
    assert len(logbuffer.buffer.snapshot(query="不存在的东西")) == 0
    # 大小写不敏感
    assert len(logbuffer.buffer.snapshot(query="TRACEBACK")) == 1


def test_snapshot_limit_keeps_newest() -> None:
    _reset()
    for i in range(10):
        logbuffer.buffer.push("INFO", "x", f"m{i}")
    assert [i["message"] for i in logbuffer.buffer.snapshot(limit=3)] == ["m7", "m8", "m9"]
    # 非法 limit 不炸，也不截断
    assert len(logbuffer.buffer.snapshot(limit="abc")) == 10


def test_snapshot_source_filter() -> None:
    """按 logger 名过滤：服务器上噪音来自 nonebot（协议端）与 uvicorn（网页请求）。"""
    _reset()
    logbuffer.buffer.push("INFO", logbuffer.BOT_LOGGER, "开始归档")
    logbuffer.buffer.push("SUCCESS", "nonebot", "OneBot V11 | [message.group.normal] ...")
    logbuffer.buffer.push("INFO", "uvicorn", 'GET /api/music-admin/logs HTTP/1.1" 200')
    logbuffer.buffer.push("WARNING", logbuffer.BOT_LOGGER, "写简介失败")

    assert len(logbuffer.buffer.snapshot()) == 4
    assert [i["message"] for i in logbuffer.buffer.snapshot(source="nonebot")] == [
        "OneBot V11 | [message.group.normal] ..."
    ]
    assert len(logbuffer.buffer.snapshot(source="uvicorn")) == 1
    # 只看机器人：三种写法都认
    for alias in ("bot", "music", "self", logbuffer.BOT_LOGGER):
        got = logbuffer.buffer.snapshot(source=alias)
        assert len(got) == 2, (alias, got)
        assert all(i["name"] == logbuffer.BOT_LOGGER for i in got)
    # 与关键词过滤可叠加
    both = logbuffer.buffer.snapshot(source="bot", query="归档")
    assert len(both) == 1 and both[0]["message"] == "开始归档"


def test_sources_counts_and_order() -> None:
    _reset()
    for _ in range(3):
        logbuffer.buffer.push("INFO", "nonebot", "n")
    logbuffer.buffer.push("INFO", "uvicorn", "u")
    logbuffer.buffer.push("INFO", logbuffer.BOT_LOGGER, "b")

    rows = logbuffer.buffer.sources()
    assert rows[0]["name"] == logbuffer.BOT_LOGGER, rows     # 机器人永远排最前
    assert rows[0]["count"] == 1
    counts = {r["name"]: r["count"] for r in rows}
    assert counts == {logbuffer.BOT_LOGGER: 1, "nonebot": 3, "uvicorn": 1}, counts
    # 其余按条数从多到少
    assert [r["name"] for r in rows[1:]] == ["nonebot", "uvicorn"], rows
    # 空缓冲不炸
    logbuffer.buffer.clear()
    assert logbuffer.buffer.sources() == []
    # 模块级快捷方式与实例方法一致
    assert logbuffer.sources() == logbuffer.buffer.sources()


def test_ring_eviction_keeps_capacity() -> None:
    _reset()
    logbuffer.buffer.set_capacity(50)
    assert logbuffer.buffer.stats()["capacity"] == 50
    for i in range(80):
        logbuffer.buffer.push("INFO", "x", f"m{i}")
    items = logbuffer.buffer.snapshot()
    assert len(items) == 50, len(items)
    assert items[0]["message"] == "m30", items[0]["message"]     # 最旧的 30 条被丢
    assert items[-1]["message"] == "m79"


def test_capacity_change_drops_oldest_and_clamps() -> None:
    _reset()
    logbuffer.buffer.set_capacity(100)
    for i in range(100):
        logbuffer.buffer.push("INFO", "x", f"m{i}")
    logbuffer.buffer.set_capacity(60)          # 缩容：只留最新的 60 条
    items = logbuffer.buffer.snapshot()
    assert len(items) == 60
    assert items[0]["message"] == "m40"
    # 越界值被夹到合法范围
    logbuffer.buffer.set_capacity(1)
    assert logbuffer.buffer.stats()["capacity"] == 50
    logbuffer.buffer.set_capacity(10 ** 9)
    assert logbuffer.buffer.stats()["capacity"] == 200_000


def test_level_name_normalization() -> None:
    _reset()
    logbuffer.buffer.set_level("debug")
    assert logbuffer.buffer.level == "DEBUG"
    logbuffer.buffer.set_level("  警告 ")       # 中文/乱值 -> 回默认
    assert logbuffer.buffer.level == "INFO"
    logbuffer.buffer.set_level(None)
    assert logbuffer.buffer.level == "INFO"


def test_clear_returns_count_and_empties() -> None:
    _reset()
    base = logbuffer.stats()["total"]
    for i in range(5):
        logbuffer.buffer.push("INFO", "x", f"m{i}")
    assert logbuffer.clear() == 5
    assert logbuffer.snapshot() == []
    assert logbuffer.stats()["size"] == 0
    assert logbuffer.stats()["total"] - base == 5    # total 是累计，不随清空归零


def test_stats_fields() -> None:
    _reset()
    st = logbuffer.stats()
    for key in ("size", "capacity", "total", "level", "started_at", "installed", "file"):
        assert key in st, key
    assert st["installed"] is False       # 未 install 前不该谎报已挂载
    assert st["file"] is None
    assert st["started_at"] <= time.time()


# ---------------------------------------------------------------- 与 loguru 接线


def test_install_captures_loguru_and_is_idempotent() -> None:
    _reset()
    logbuffer.install(lines=500, level="INFO")
    assert logbuffer.stats()["installed"] is True
    logger.info("第一条")
    first_id = logbuffer._sink_id
    # 重复 install（同等级）不能装出第二个 sink，否则每条日志会被记两次
    logbuffer.install(lines=500, level="INFO")
    assert logbuffer._sink_id == first_id, "重复 install 不该重建 sink"
    logger.info("第二条")
    msgs = [i["message"] for i in logbuffer.snapshot()]
    assert msgs == ["第一条", "第二条"], msgs


def test_install_level_hot_update() -> None:
    _reset()
    logbuffer.install(lines=500, level="WARNING")
    logger.info("INFO 不该被记")
    logger.warning("WARNING 该被记")
    msgs = [i["message"] for i in logbuffer.snapshot()]
    assert msgs == ["WARNING 该被记"], msgs

    old_id = logbuffer._sink_id
    logbuffer.install(lines=500, level="DEBUG")      # 等级变了要换 sink
    assert logbuffer._sink_id != old_id, "等级变化应重新注册 sink"
    logger.debug("DEBUG 现在该被记了")
    msgs = [i["message"] for i in logbuffer.snapshot()]
    assert "DEBUG 现在该被记了" in msgs, msgs


def test_exception_is_captured() -> None:
    _reset()
    logbuffer.install(lines=500, level="DEBUG")
    try:
        raise ValueError("boom-marker")
    except ValueError:
        logger.exception("出错了")
    items = logbuffer.snapshot(query="boom-marker")
    assert len(items) == 1, items
    assert items[0]["exc"], "异常堆栈应被抓下来"
    assert "ValueError" in items[0]["exc"]
    assert items[0]["level"] == "ERROR"


def test_uninstall_stops_capture() -> None:
    _reset()
    logbuffer.install(lines=500, level="DEBUG")
    logger.info("装着的")
    logbuffer.uninstall()
    logger.info("卸掉后的")
    msgs = [i["message"] for i in logbuffer.snapshot()]
    assert "装着的" in msgs
    assert "卸掉后的" not in msgs, "uninstall 之后不该再被记录"
    assert logbuffer.stats()["installed"] is False


def test_optional_file_sink_writes() -> None:
    _reset()
    # 不用 TemporaryDirectory 上下文：Windows 上 loguru 握着文件句柄，
    # 句柄没释放就会在退出时抛 PermissionError。手动管理，先摘 sink 再删。
    td = Path(tempfile.mkdtemp(prefix="music-logfile-"))
    try:
        path = td / "sub" / "bot.log"               # 目录不存在应自动建
        logbuffer.install(lines=500, level="INFO", file=str(path))
        assert logbuffer.stats()["file"] == str(path)
        logger.info("落盘-marker")
        try:
            logger.complete()                        # enqueue=True，等它写完
        except Exception:
            pass
        text = ""
        for _ in range(20):
            if path.exists():
                text = path.read_text(encoding="utf-8") or ""
                if "落盘-marker" in text:
                    break
            time.sleep(0.05)
        assert path.exists(), "文件 sink 应把目录建出来"
        assert "落盘-marker" in text, text
        assert "INFO" in text
    finally:
        logbuffer.uninstall()                        # 释放句柄
        shutil.rmtree(td, ignore_errors=True)
    logbuffer.install(lines=500, level="INFO")       # 复位，后面的用例不依赖文件
    assert logbuffer.stats()["file"] is None


def test_configure_from_config_object() -> None:
    _reset()
    class _Cfg:
        lines = 300
        level = "WARNING"
        file = ""
    logbuffer.configure_from(_Cfg)
    st = logbuffer.stats()
    assert st["capacity"] == 300
    assert st["level"] == "WARNING"
    assert st["installed"] is True


# ---------------------------------------------------------------- 前后端契约


def test_frontend_has_logs_entry_and_page() -> None:
    html = webui.DASHBOARD_HTML
    assert 'data-page="logs"' in html, "sidebar 要有日志入口"
    assert 'id="page-logs"' in html, "要有日志页区块"
    assert "/api/music-admin/logs" in html, "前端要引用日志接口"
    assert 'id="logView"' in html
    assert 'id="logLevel"' in html
    assert 'id="logSource"' in html, "要有来源筛选（只看机器人 / 只看协议端）"
    assert 'id="logAuto"' in html
    # switchPage 里要把 logs 页接上加载逻辑
    idx = html.index("function switchPage(name){")
    body = html[idx:idx + 900]
    assert 'name==="logs"' in body, "switchPage 要处理 logs 页"


def test_api_logs_source_param_roundtrip() -> None:
    """接口要把 source 参数透传下去，并把来源清单 / 机器人 logger 名回给前端。"""
    _reset()
    webui.logbuffer.buffer.push("INFO", logbuffer.BOT_LOGGER, "机器人日志")
    webui.logbuffer.buffer.push("INFO", "nonebot", "协议端日志")
    res = asyncio.run(webui._api_logs(_Req(params={"source": "bot"})))
    payload = json.loads(res.body)
    assert [i["message"] for i in payload["logs"]] == ["机器人日志"], payload["logs"]
    assert payload["bot_logger"] == logbuffer.BOT_LOGGER
    names = {r["name"] for r in payload["sources"]}
    assert {logbuffer.BOT_LOGGER, "nonebot"} <= names, payload["sources"]


def test_api_logs_route_registered() -> None:
    assert asyncio.iscoroutinefunction(webui._api_logs), "_api_logs 必须是 async"
    src = inspect.getsource(webui.register_webui)
    assert '"/api/music-admin/logs"' in src, "register_webui 里要注册 logs 路由"


def test_schema_exposes_logs_section() -> None:
    titles = {s["key"]: s["title"] for s in webui.SCHEMA}
    assert titles.get("logs") == "运行日志", titles.get("logs")
    keys = {f["key"] for s in webui.SCHEMA for f in s["fields"]}
    assert {"logs.lines", "logs.level", "logs.file", "logs.trace_messages"} <= keys, keys
    # 所有字段都要有中文标签（否则页面上会出现英文 key）
    for key in ("logs.lines", "logs.level", "logs.file", "logs.trace_messages"):
        field = webui.KEY_INDEX[key]
        assert any("\u4e00" <= ch <= "\u9fff" for ch in field["label"]), (key, field["label"])


def test_config_page_has_jump_bar() -> None:
    """配置页很长，顶部要有分组跳转栏（点一下滚到对应分组）。"""
    html = webui.DASHBOARD_HTML
    assert 'id="page-config"' in html, "要有配置页区块"
    # 跳转栏必须在配置页内部、位于表单之前
    sec = html.index('id="page-config"')
    nav = html.index('id="cfgNav"', sec)
    form = html.index('id="configForm"', sec)
    assert sec < nav < form, "cfgNav 要在 configForm 之前"
    # 渲染时给每个分组打 id 并生成按钮，滚动时高亮当前分组
    assert 'card.id="cfg-sec-"' in html
    assert "cfg-nav-btn" in html
    assert "syncCfgNav" in html
    assert "scrollIntoView" in html
    assert ".cfg-sec{scroll-margin-top:" in html, "要留出吸顶导航的偏移，否则标题被挡住"


def test_mobile_media_queries_present() -> None:
    """手机端适配：窄屏下侧边栏变顶部导航条、配置项上下排布、表格可横向滚动。"""
    html = webui.DASHBOARD_HTML
    assert "@media (max-width:900px)" in html, "要有平板/手机断点"
    assert "@media (max-width:560px)" in html, "要有小屏断点"
    # 断点里必须真的改掉桌面端的固定侧边栏宽度
    idx = html.index("@media (max-width:900px)")
    block = html[idx: html.index("@media (max-width:560px)")]
    assert "flex-direction:column" in block, "布局要改成纵向"
    assert "overflow-x:auto" in block, "侧边栏要能横向滑动"
    assert ".field{grid-template-columns:1fr" in block, "配置项要改成上下排布"
    assert ".gtbl{min-width:" in block, "表格要能横向滚动"
    assert ".footbar{left:0" in block, "底部保存条要占满宽度"
    # 别名页也要适配
    assert "@media (max-width:640px)" in webui.ALIASES_HTML, "别名页也要有手机断点"


class _Req:
    """够用的 Request 替身：只需要 method / query_params / headers / json()。"""

    def __init__(self, method="GET", params=None, headers=None, body=None):
        self.method = method
        self.query_params = params or {}
        self.headers = headers or {}
        self._body = body

    async def json(self):
        return self._body


def test_api_logs_get_filters() -> None:
    _reset()
    logbuffer.buffer.push("INFO", "music_collector.a", "普通信息")
    logbuffer.buffer.push("ERROR", "music_collector.b", "炸了")

    resp = asyncio.run(webui._api_logs(_Req(params={"level": "ERROR"})))
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert [i["level"] for i in data["logs"]] == ["ERROR"]
    assert data["levels"] == list(logbuffer.LEVELS)
    assert "installed" in data["stats"]

    resp = asyncio.run(webui._api_logs(_Req(params={"q": "普通"})))
    data = json.loads(resp.body)
    assert len(data["logs"]) == 1
    assert data["logs"][0]["message"] == "普通信息"

    resp = asyncio.run(webui._api_logs(_Req(params={"limit": "1"})))
    data = json.loads(resp.body)
    assert len(data["logs"]) == 1


def test_api_logs_post_clears() -> None:
    _reset()
    for i in range(3):
        logbuffer.buffer.push("INFO", "x", f"m{i}")
    resp = asyncio.run(webui._api_logs(_Req(method="POST", body={"action": "clear"})))
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert data["removed"] == 3
    assert logbuffer.snapshot() == []

    bad = asyncio.run(webui._api_logs(_Req(method="POST", body={"action": "nope"})))
    assert bad.status_code == 400


def test_api_logs_requires_token() -> None:
    _reset()
    old = webui._TOKEN
    webui._TOKEN = "secret-token"
    try:
        try:
            asyncio.run(webui._api_logs(_Req()))
        except Exception as exc:  # fastapi.HTTPException
            assert getattr(exc, "status_code", None) == 401, exc
        else:
            raise AssertionError("没带令牌不该放行")
        ok = asyncio.run(webui._api_logs(_Req(headers={"Authorization": "Bearer secret-token"})))
        assert json.loads(ok.body)["ok"] is True
    finally:
        webui._TOKEN = old


if __name__ == "__main__":
    test_push_and_snapshot_shape()
    test_level_threshold_discards_low_level()
    test_snapshot_level_filter()
    test_snapshot_query_matches_message_name_and_exc()
    test_snapshot_limit_keeps_newest()
    test_snapshot_source_filter()
    test_sources_counts_and_order()
    test_ring_eviction_keeps_capacity()
    test_capacity_change_drops_oldest_and_clamps()
    test_level_name_normalization()
    test_clear_returns_count_and_empties()
    test_stats_fields()
    test_install_captures_loguru_and_is_idempotent()
    test_install_level_hot_update()
    test_exception_is_captured()
    test_uninstall_stops_capture()
    test_optional_file_sink_writes()
    test_configure_from_config_object()
    test_frontend_has_logs_entry_and_page()
    test_api_logs_route_registered()
    test_schema_exposes_logs_section()
    test_config_page_has_jump_bar()
    test_mobile_media_queries_present()
    test_api_logs_get_filters()
    test_api_logs_source_param_roundtrip()
    test_api_logs_post_clears()
    test_api_logs_requires_token()
    logbuffer.uninstall()
    print("logbuffer / WebUI 日志页 tests OK")
