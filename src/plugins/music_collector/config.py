"""配置系统：YAML 落盘 + pydantic 校验 + 运行时热更新。

所有与时间相关的设置都集中在 `window` 段，可通过群内命令修改并立即生效。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

# 项目根目录（.../qq-music-collector）
ROOT_DIR = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
CONFIG_PATH = DATA_DIR / "config.yaml"
EXAMPLE_CONFIG_PATH = ROOT_DIR / "config.example.yaml"
DB_PATH = DATA_DIR / "collector.db"
NETEASE_SESSION_PATH = DATA_DIR / "netease_session.json"


class _PointsBase(BaseModel):
    """四个时间点的公共校验：老配置没有 `end` 时自动继承 `archive`。

    这样从旧版本升上来的 data/config.yaml 不用手改也能跑。
    """

    @model_validator(mode="before")
    @classmethod
    def _fill_end(cls, data: object) -> object:
        if isinstance(data, dict) and not data.get("end") and data.get("archive"):
            data = dict(data)
            data["end"] = data["archive"]
        return data


class WeeklyWindow(_PointsBase):
    """每周循环。时间点格式：`MON 20:00`（星期缩写 + 24 小时制）。"""

    start: str = "MON 00:00"
    summary: str = "SUN 22:00"
    #: 结束收集（此刻起不再收录新歌）
    end: str = "SUN 22:30"
    #: 归档建歌单；archive_same_as_end 打开时会被 end 覆盖
    archive: str = "SUN 22:30"


class DailyWindow(_PointsBase):
    """每日循环。时间点格式：`23:00`。"""

    start: str = "00:00"
    summary: str = "23:00"
    end: str = "23:30"
    archive: str = "23:30"


class OnceWindow(_PointsBase):
    """单次区间。时间点格式：`2026-08-10 00:00`。"""

    start: str = "2026-08-10 00:00"
    summary: str = "2026-08-20 22:00"
    end: str = "2026-08-20 22:30"
    archive: str = "2026-08-20 22:30"


class WindowConfig(BaseModel):
    mode: Literal["weekly", "daily", "once"] = "weekly"
    timezone: str = "Asia/Shanghai"
    #: 开：归档时刻 = 结束收集时刻（收工即建歌单，只跑一个任务）
    #: 关：先在 end 结束收集并播报，再到 archive 单独建歌单
    archive_same_as_end: bool = True
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
    #: 清单样式：list=逐首列（含分享者）  by_person=按人聚合  by_name=只列分享者名  none=不附
    sharer_style: Literal["list", "by_person", "by_name", "none"] = "list"
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
    #: 简介写入失败时的重试次数（网易云对改简介有频控，失败会自动入队补写）
    desc_retry: int = 3
    #: 定时补写待写入简介的间隔分钟数；<=0 关闭自动补写
    desc_retry_minutes: int = 30
    #: 昵称 / 歌名里的表情处理：text=转中文词 [音符]  strip=直接删  keep=原样
    #: 网易云简介是 utf8(3字节) 存储，emoji 是 4 字节，keep 有很大概率写不进去
    emoji_style: Literal["text", "strip", "keep"] = "text"
    #: 简介清单里是否带歌手名
    desc_show_artist: bool = True
    #: 简介清单条目之间是否插空行（by_person 样式下按人分段）
    desc_blank_line: bool = False
    #: 分享者昵称映射（显示层替换，入库仍存原始昵称）。键为原始昵称，值为展示名。
    #: 例：{"菜老名": "Jacksing"} —— 网易云简介 / 群内文字榜单 / WebUI 表格里
    #: 「菜老名」都会显示成「Jacksing」，但数据库里保留原始昵称不变。
    sharer_aliases: dict[str, str] = Field(default_factory=dict)


#: 自我介绍默认文案。占位符见 naming.py，另有 {nick} {count} {state} {playlist}
DEFAULT_INTRO = (
    "你好 {nick}，我是群音乐收集助手 🎵\n"
    "把网易云 / QQ音乐 / 酷狗 / 酷我 的歌曲分享到群里，我会自动收录并排序。\n"
    "本期：{window}（{state}），已收集 {count} 首。\n"
    "发送 /music help 查看全部命令。"
)


class IntroConfig(BaseModel):
    """被 @ 时的自我介绍。"""

    #: 总开关
    enabled: bool = True
    #: 文案模板，支持占位符；命令行里用 \n 表示换行
    text: str = DEFAULT_INTRO
    #: 同一个群的冷却秒数，防止刷屏；0 表示不限
    cooldown: int = 10
    #: 回复时是否 @ 提问者
    at_sender: bool = True
    #: 消息里带 /music 命令时不发自我介绍（避免和命令回复重复）
    skip_commands: bool = True
    #: 消息里同时带音乐链接时不发自我介绍（那是分享，不是提问）
    skip_music: bool = True
    #: 收集开关关闭 / 不在收集期时，是否仍然回应自我介绍
    always_reply: bool = True


class CardConfig(BaseModel):
    """音乐卡片发送策略。

    背景：NapCat / go-cqhttp 发送平台原生音乐卡片时要走外部**签名服务**换取
    ArkShare 结构，这个服务经常 500 或超时，表现为
    ``[音乐卡片签名失败] Unexpected status code: 500`` 加一条
    ``消息体无法解析`` 的报错。所以这里做成可降级的三级链路，
    保证签名服务挂掉时群里依然能看到歌曲信息。
    """

    #: 卡片模式：
    #: native = 平台原生卡片（好看，但依赖签名服务）
    #: custom = 自定义音乐卡片（自己拼标题/封面/跳转链接，不走签名服务）
    #: off    = 完全不发卡片，只发文字 + 封面
    mode: Literal["native", "custom", "off"] = "native"
    #: 原生卡片失败后，是否自动再试一次自定义卡片
    fallback_custom: bool = True
    #: 卡片全部失败时，是否补发一条文字（歌名 / 歌手 / 可点击链接）
    fallback_text: bool = True
    #: 文字兜底里是否附上封面图
    fallback_cover: bool = True
    #: 同一平台连续失败多少次后熔断，冷却期内直接跳过卡片不再空等；<=0 关闭熔断
    failure_threshold: int = 3
    #: 熔断冷却分钟数，到点后自动恢复试探
    cooldown_minutes: int = 10


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
    #: 手动覆盖收集状态（方便测试）：
    #: auto=按时间窗口自动判断  on=强制正在收集  off=强制不收集
    collect_override: Literal["auto", "on", "off"] = "auto"
    #: 生效群号，留空表示所有群
    groups: list[int] = Field(default_factory=list)
    #: 识别到音乐后是否回发音乐卡片；@+文字提示始终发送，本项只控制卡片
    reply_card: bool = True
    #: 同一首歌被重复分享时是否提示
    notify_duplicate: bool = True
    #: 汇总 / 归档结果发送到哪些群，留空则发回收集所在群
    report_groups: list[int] = Field(default_factory=list)
    #: 识别过程写详细日志，排查"分享了没反应"时打开
    debug_detect: bool = False
    window: WindowConfig = Field(default_factory=WindowConfig)
    playlist: PlaylistConfig = Field(default_factory=PlaylistConfig)
    card: CardConfig = Field(default_factory=CardConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    clear: ClearConfig = Field(default_factory=ClearConfig)
    intro: IntroConfig = Field(default_factory=IntroConfig)


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
        """落盘配置。Windows 下文件可能被编辑器/杀软瞬时锁住，做几次短重试。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._config.model_dump(mode="json")
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, indent=2)
        last_err: Optional[Exception] = None
        for attempt in range(5):
            try:
                self.path.write_text(text, encoding="utf-8")
                return
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.2 * (attempt + 1))
        if last_err is not None:
            raise last_err

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
