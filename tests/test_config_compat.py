"""配置向后兼容：旧 data/config.yaml 没有 end / collect_override / emoji_style 等字段。

_PointsBase 校验器应在老配置缺 end 时自动用 archive 填充，使升级后无需手改配置。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")

from music_collector.config import AppConfig  # noqa: E402


def test_window_without_end_inherits_archive() -> None:
    raw = {
        "window": {
            "mode": "weekly",
            "weekly": {
                "start": "MON 00:00",
                "summary": "SUN 22:00",
                "archive": "SUN 22:30",
            },
        }
    }
    cfg = AppConfig.model_validate(raw)
    assert cfg.window.weekly.end == "SUN 22:30", "老配置缺 end 时应继承 archive"
    assert cfg.window.archive_same_as_end is True


def test_missing_top_level_fields_use_defaults() -> None:
    cfg = AppConfig.model_validate({})
    assert cfg.collect_override == "auto"
    assert cfg.playlist.emoji_style == "text"
    assert cfg.playlist.desc_show_artist is True
    assert cfg.playlist.desc_blank_line is False


def test_end_independent_from_archive_when_set() -> None:
    raw = {
        "window": {
            "mode": "weekly",
            "archive_same_as_end": False,
            "weekly": {
                "start": "MON 00:00",
                "summary": "SUN 22:00",
                "end": "SUN 22:15",
                "archive": "SUN 22:30",
            },
        }
    }
    cfg = AppConfig.model_validate(raw)
    assert cfg.window.weekly.end == "SUN 22:15"
    assert cfg.window.weekly.archive == "SUN 22:30"
    assert cfg.window.archive_same_as_end is False


if __name__ == "__main__":
    test_window_without_end_inherits_archive()
    test_missing_top_level_fields_use_defaults()
    test_end_independent_from_archive_when_set()
    print("config compat tests OK")
