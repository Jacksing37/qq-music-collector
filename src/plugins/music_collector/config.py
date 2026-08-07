"""配置系统：YAML 落盘 + pydantic 校验 + 运行时热更新。

所有与时间相关的设置都集中在 `window` 段，可通过群内命令修改并立即生效。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

# 项目根目录（.../qq-music-collector）
ROOT_DIR = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
CONFIG_PATH = DATA_DIR / "config.yaml"
EXAMPLE_CONFIG_PATH = ROOT_DIR / "config.example.yaml"
DB_PATH = DATA_DIR / "collector.db"
NETEASE_SESSION_PATH = DATA_DIR / "netease_session.json"


class WeeklyWindow(BaseModel):
    """每周循环。时间点格式：`MON 20:00`（星期缩写 + 24 小时制）。"""

    start: str = "MON 00:00"
    summary: str = "SUN 22:00"
    archive: str = "SUN 22:30"


class DailyWindow(BaseModel):
    """每日循环。时间点格式：`23:00`。"""

    start: str = "00:00"
    summary: str = "23:00"
    archive: str = "23:30"


class OnceWindow(BaseModel):
    """单次区间。时间点格式：`2026-08-10 00:00`。"""

    start: str = "2026-08-10 00:00"
    summary: str = "2026-08-20 22:00"
    archive: str = "2026-08-20 22:30"


class WindowConfig(BaseModel):
    mode: Literal["weekly", "daily", "once"] = "weekly"
    timezone: str = "Asia/Shanghai"
    #: 窗口关闭后收到的音乐链接是否仍然回复卡片（只是不入库）
    reply_outside_window: bool = True
    weekly: WeeklyWindow = Field(default_factory=WeeklyWindow)
    daily: DailyWindow = Field(default_factory=DailyWindow)
    once: OnceWindow = Field(default_factory=OnceWindow)


class PlaylistConfig(BaseModel):
    #: 可用占位符 {window} {group} {count} {date}
    name_template: str = "群歌单 {window}"
    description_template: str = "由 QQ 群 {group} 在 {window} 期间收集，共 {count} 首。"
    #: 歌单是否设为隐私
    privacy: bool = False
    #: 非网易云来源的歌曲，是否在网易云搜索匹配后加入
    cross_platform_match: bool = True
    #: 严格匹配（歌名 + 歌手都要对得上）；关闭后只按歌名匹配，命中率高但可能加错版本
    strict_match: bool = True
    #: 单次加歌批大小（网易云接口限制）
    batch_size: int = 100


class RenderConfig(BaseModel):
    #: 长图单页最多条目，超出自动分页
    max_items_per_image: int = 40
    #: 是否下载并绘制封面
    show_cover: bool = True
    #: 自定义字体路径，留空则自动探测系统中文字体
    font_path: Optional[str] = None
    #: 图片主题：light / dark
    theme: Literal["light", "dark"] = "dark"


class AppConfig(BaseModel):
    #: 总开关
    enabled: bool = True
    #: 生效群号，留空表示所有群
    groups: list[int] = Field(default_factory=list)
    #: 识别到音乐后是否 @ 分享者并回发卡片
    reply_card: bool = True
    #: 同一首歌被重复分享时是否提示
    notify_duplicate: bool = True
    #: 汇总 / 归档结果发送到哪些群，留空则发回收集所在群
    report_groups: list[int] = Field(default_factory=list)
    window: WindowConfig = Field(default_factory=WindowConfig)
    playlist: PlaylistConfig = Field(default_factory=PlaylistConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)


class ConfigManager:
    """单例式配置管理器，负责加载 / 保存 / 热更新。"""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._config: AppConfig = AppConfig()

    @property
    def config(self) -> AppConfig:
        return self._config

    def load(self) -> AppConfig:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        if not self.path.exists():
            if EXAMPLE_CONFIG_PATH.exists():
                shutil.copyfile(EXAMPLE_CONFIG_PATH, self.path)
            else:
                self._config = AppConfig()
                self.save()
                return self._config

        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self._config = AppConfig.model_validate(raw)
        return self._config

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._config.model_dump(mode="json")
        self.path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False, indent=2),
            encoding="utf-8",
        )

    def update(self, dotted_key: str, value: object) -> None:
        """按 `window.weekly.start` 这样的点分路径更新并落盘。"""
        parts = dotted_key.split(".")
        data = self._config.model_dump(mode="json")
        cursor = data
        for part in parts[:-1]:
            if part not in cursor or not isinstance(cursor[part], dict):
                raise KeyError(f"配置项不存在: {dotted_key}")
            cursor = cursor[part]
        if parts[-1] not in cursor:
            raise KeyError(f"配置项不存在: {dotted_key}")
        cursor[parts[-1]] = value
        # 先校验再落盘，避免写坏配置
        self._config = AppConfig.model_validate(data)
        self.save()


config_manager = ConfigManager()
