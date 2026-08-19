"""Web UI 核心逻辑测试：schema 自省 / 值转换 / 原子更新。

不依赖 HTTP，直接验证纯函数与 config_manager 写入路径。
"""

import sys
from pathlib import Path

sys.path.insert(0, ".")

import nonebot

nonebot.init(driver="~fastapi")  # noqa: E402

from src.plugins.music_collector import webui as W  # noqa: E402
from src.plugins.music_collector.config import AppConfig, config_manager  # noqa: E402


def _all_keys():
    return [f["key"] for s in W.SCHEMA for f in s["fields"]]


def test_build_schema_covers_known_keys():
    keys = _all_keys()
    for expected in ("enabled", "collect_override", "window.mode",
                     "window.weekly.start", "playlist.sharer_style",
                     "playlist.name_template", "card.mode", "intro.text",
                     "cache.keep_days", "clear.keep_days"):
        assert expected in keys, f"schema 缺少 {expected}"


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
