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
                     "window.weekly", "playlist.sharer_style",
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


if __name__ == "__main__":
    import tempfile

    _tmp = Path(tempfile.mkdtemp())
    test_build_schema_covers_known_keys()
    test_enum_options_detected()
    test_coerce_value_types()
    test_apply_updates_atomic(_tmp)
    test_coerce_value_map()
    test_sharer_aliases_map_type()
    test_current_values_map_whole_dict()
    test_apply_updates_map(_tmp)
    print("webui tests OK")
