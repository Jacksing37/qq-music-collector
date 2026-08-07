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
    """歌单命名与简介。占位符说明见 naming.py 顶部注释。

    例：``Wk.{seq}线上学习{slash}`` -> ``Wk.86线上学习26/8/7``
    """

    #: 歌单名模板，常用占位符 {seq} {slash} {yy} {m} {d} {window} {count}
    name_template: str = "群歌单 {window}"
    #: 简介开头；后面会自动接上「谁分享了什么歌」的清单
    description_template: str = "由 QQ 群 {group} 在 {window} 期间收集，共 {count} 首。"
    #: 简介里附上分享清单
    include_sharers: bool = True
    #: 清单样式：list=逐首列（含分享者）  by_person=按人聚合  none=不附
    sharer_style: Literal["list", "by_person", "none"] = "list"
    #: 自增期号，每成功归档一次 +1（用于 Wk.86 这种编号）
    seq: int = 1
    #: 归档成功后是否自动递增 seq
    seq_auto_increment: bool = True
    #: 一次性歌单名。设置后仅下一次归档生效，用完自动清空
    pending_name: str = ""
    #: 歌单是否设为隐私
    privacy: bool = False
    #: 非网易云来源的歌曲，是否在网易云搜索匹配后加入
    cross_platform_match: bool = True
    #: 严格匹配（歌名 + 歌手都要对得上）；关闭后只按歌名匹配，命中率高但可能加错版本
    strict_match: bool = True
    #: 单次加歌批大小（网易云接口限制）
    batch_size: int = 100


class CacheConfig(BaseModel):
    """缓存图片自动回收。"""

    #: 总开关
    enabled: bool = True
    #: 保留天数，超过就删；<=0 表示不按时间清
    keep_days: float = 3
    #: 榜单长图最多保留个数；<=0 表示不限
    max_render_files: int = 60
    #: 封面缓存最多保留个数；<=0 表示不限
    max_cover_files: int = 400
    #: 每天几点做一次清理，格式 `04:30`
    clean_at: str = "04:30"
    #: 启动时先清一次
    clean_on_start: bool = True
    #: 每次渲染完顺手清一次
    clean_after_render: bool = True


class ClearConfig(BaseModel):
    """已收集歌曲的清理（注意区别于 cache：cache 清理的是榜单图片缓存）。"""

    #: 归档（结束收集）建歌单成功后，是否自动清空本期已收集歌曲
    after_archive: bool = False
    #: 定时清理总开关
    scheduled_enabled: bool = False
    #: 保留天数；早于 now - keep_days 的收集记录会被删除；<=0 表示不按时间清
    keep_days: float = 30
    #: 每天执行定时清理的时刻，格式 `05:00`
    prune_at: str = "05:00"


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
    #: 识别过程写详细日志，排查"分享了没反应"时打开
    debug_detect: bool = False
    window: WindowConfig = Field(default_factory=WindowConfig)
    playlist: PlaylistConfig = Field(default_factory=PlaylistConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    clear: ClearConfig = Field(default_factory=ClearConfig)


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
