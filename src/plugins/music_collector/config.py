"""配置系统：YAML 落盘 + pydantic 校验 + 运行时热更新。

所有与时间相关的设置都集中在 `window` 段，可通过群内命令修改并立即生效。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("music_collector.config")

# 项目根目录（.../qq-music-collector）
ROOT_DIR = Path(__file__).resolve().parents[3]
#: 数据目录。可用环境变量 ``MUSIC_DATA_DIR`` 覆盖：测试脚本靠它把 config.yaml /
#: collector.db / 登录态全部落到临时目录，**避免回归测试写坏真实的 data/**。
DATA_DIR = Path(os.environ.get("MUSIC_DATA_DIR") or (ROOT_DIR / "data")).expanduser()
CACHE_DIR = DATA_DIR / "cache"
CONFIG_PATH = DATA_DIR / "config.yaml"
EXAMPLE_CONFIG_PATH = ROOT_DIR / "config.example.yaml"
DB_PATH = DATA_DIR / "collector.db"
NETEASE_SESSION_PATH = DATA_DIR / "netease_session.json"
#: 按群覆盖的配置项（``{群号: {dotted_key: 值}}``）。只存被**显式覆盖**的键，
#: 其余一律继承 config.yaml 的全局值。
GROUP_OVERRIDES_PATH = DATA_DIR / "group_overrides.yaml"


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
    #: 分享即归档：每收到一批新歌分享，立即把新歌追加进当前窗口歌单
    #: （自动复用已建歌单，不新建、不消耗期号）。默认关闭；开启后静默执行，
    #: 结果只在日志记录，不额外刷屏。
    auto_archive_on_share: bool = False
    #: 同一窗口内**同一用户只收录第一首**：该用户本期首次分享即占位，之后再分享
    #: 的歌不入榜（也不会进总库），只回一条提示（文案见 reply.sharer_limit_text）。
    #: 判定基准是「首次分享」而不是「首次成功收录」——首发那首即使因重复 / 无法
    #: 匹配没进榜，名额同样算已用掉，避免同一个人反复试探。
    one_per_sharer: bool = True
    #: 非网易云歌曲在**分享时**就立即探测能否匹配到网易云；匹配不到就在收录消息里
    #: 附带一条提示（文案见 reply.unmatched_text），让分享者当场知道这首歌不会
    #: 进本期歌单，而不是等到归档后的报告里才看到。
    #: 关闭后不做这次预探测（省一次搜索请求），匹配仍会在归档阶段进行。
    notify_unmatched: bool = True


#: 自我介绍默认文案。占位符见 naming.py，另有 {nick} {count} {state} {playlist}
DEFAULT_INTRO = (
    "你好 {nick}，我是群音乐收集助手 🎵\n"
    "把网易云 / QQ音乐 / 酷狗 / 酷我 的歌曲分享到群里，我会自动收录并排序。\n"
    "本期：{window}（{state}），已收集 {count} 首。\n"
    "发送 /music help 查看全部命令。"
)


#: 收录回复默认文案。占位符见 ReplyConfig 注释。
DEFAULT_ACCEPT = (
    " 已收录 · 本期第 {index} 首\n"
    "{title}\n"
    "{artists_line}{album_line}来源: {platform}\n"
    "{unmatched_line}"
)


class ReplyConfig(BaseModel):
    """收录回复文案（识别到新歌并入库后回发的那条消息）。"""

    #: 是否启用自定义模板；关闭时用内置格式（等同于默认模板）
    enabled: bool = False
    #: 收录回复模板，支持占位符：
    #:   {index}        本期序号
    #:   {nick}         分享者（已套昵称映射）
    #:   {title}        歌名          {artists}   歌手
    #:   {album}        专辑          {platform}  来源平台名
    #:   {url}          歌曲链接      {duration}  时长 mm:ss
    #:   {artists_line} 整行「歌手: xxx」，无歌手时整行消失
    #:   {album_line}   整行「专辑: xxx」，无专辑时整行消失
    #:   {unmatched_line} 整行「无法匹配到网易云」提示，匹配成功时整行消失
    #:                   （文案取自 reply.unmatched_text）
    #:   {song}         歌曲详情块（歌名 + 歌手 / 专辑 / 来源 / 时长）
    #:   {playlist}     当前群当前窗口的网易云歌单（名称 + 链接），
    #:                  本期还没归档时用 playlist_empty_text 代替
    #:   {count}        本期已收录首数   {window}  窗口文案
    #: 命令行里用 \n 表示换行
    accept_text: str = DEFAULT_ACCEPT
    #: {playlist} 在本期尚未归档时的替代文案
    playlist_empty_text: str = "（本期歌单还没生成）"
    #: 歌曲无法匹配到网易云时，附在收录消息里的提示（并入同一条消息，不额外刷屏）。
    #: 只在 playlist.notify_unmatched 打开、且该歌确实搜不到时才出现。
    #: 占位符：{title} 歌名 {artists} 歌手 {platform} 来源平台
    #:         {nick} 分享者 {index} 本期序号 {count} 本期已收录数 {window} 窗口文案
    unmatched_text: str = "⚠️ 这首在网易云没搜到，不会进本期歌单（可找管理员手动匹配）"
    #: 同一用户本期重复分享时的提示文案（见 playlist.one_per_sharer）。
    #: 占位符：{nick} 本次分享者（已套昵称映射）
    #:         {title} 其本期**首发**的歌名   {artists} 首发歌歌手
    #:         {platform} 首发歌来源平台      {index} 首发歌本期序号（不在榜单里时为 —）
    #:         {count} 本期已收录首数         {window} 窗口文案
    sharer_limit_text: str = " 本期你已经分享过《{title}》了，要更换的话请找管理员"


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


#: 总库重复提示默认文案。占位符见 MasterConfig.notify_template 注释。
DEFAULT_MASTER_DUP = (
    " 这首《{title}》在 {period} 期 {date}，由 {who} 分享过了哟"
)


class MasterConfig(BaseModel):
    """总库（跨窗口去重的群级歌曲库）。

    总库按群聚合所有窗口的歌曲，用于「分享过就别再重复推」以及独立归档成一个
    大歌单。数据落在 songs 表的 ``__master__`` 虚拟窗口里，复用现有收集/归档
    全部逻辑，因此功能与正常收集一致（可手动增删改、可拖拽排序、可同步到歌单）。

    - ``enabled`` 关闭时，分享不会写入总库，也不会做总库查重提示。
    - ``compare_on_share`` 开启后，分享已存在于总库的歌会提示重复（仅跨窗口；
      同窗口重复仍走原 ``notify_duplicate`` 逻辑，两者不重复刷屏）。
    - 归档相关字段（命名/简介/清单样式/期号/隐私等）与正常收集的 ``playlist``
      互相独立、可分别设置（配置页「总库」分组里平铺展示）。
    """

    #: 总库总开关
    enabled: bool = False
    #: 分享时是否与总库对比，命中已存在则提示重复
    compare_on_share: bool = True
    #: 重复提示模板，支持占位符（命令行/网页端用 \\n 表示换行）：
    #:   {title}    歌名          {artists}  歌手
    #:   {platform} 来源平台名     {sharer}   本次分享者（已套昵称映射）
    #:   {who}      总库首发者（首次进总库的人）   {index}  该歌在总库中的序号
    #:   {count}    总库当前总首数               {window} 当前窗口文案
    notify_template: str = DEFAULT_MASTER_DUP
    #: 分享即归档：每收到新分享立即把总库增量同步到总库歌单（静默执行，不刷屏）
    auto_archive: bool = False
    #: 歌单名模板（占位符见 PlaylistConfig.name_template）
    name_template: str = "群总库 {group}"
    #: 简介开头模板（占位符见 PlaylistConfig.description_template）
    description_template: str = "由 QQ 群 {group} 跨窗口汇总收集，共 {count} 首。"
    #: 简介里附上分享清单
    include_sharers: bool = True
    #: 清单样式：list=逐首列 / by_person=按人聚合 / by_name=只列分享者名 / none=不附
    sharer_style: Literal["list", "by_person", "by_name", "none"] = "list"
    #: 自增期号，每成功归档一次 +1
    seq: int = 1
    #: 期号自增
    seq_auto_increment: bool = True
    #: 一次性歌单名，设置后仅下一次归档生效，用完自动清空
    pending_name: str = ""
    #: 歌单隐私
    privacy: bool = False
    #: 非网易云来源的歌曲，是否在网易云搜索匹配后加入
    cross_platform_match: bool = True
    #: 严格匹配（歌名 + 歌手都要对得上）；关闭后只按歌名，命中率高但可能加错版本
    strict_match: bool = True
    #: 单次加歌批大小
    batch_size: int = 100
    #: 简介写入重试次数
    desc_retry: int = 3
    #: 定时补写待写入简介的间隔分钟数；<=0 关闭自动补写
    desc_retry_minutes: int = 30
    #: 表情处理：text=转中文词 / strip=直接删 / keep=原样
    emoji_style: Literal["text", "strip", "keep"] = "text"
    #: 简介清单里是否带歌手名
    desc_show_artist: bool = True
    #: 简介清单条目之间是否插空行
    desc_blank_line: bool = False
    #: 分享者昵称映射（仅展示层替换，入库仍保留原始昵称）
    sharer_aliases: dict[str, str] = Field(default_factory=dict)


class NeteaseConfig(BaseModel):
    """网易云登录态维护（掉登录后自动重登）。

    网易云的 `MUSIC_U` cookie 会过期 / 被风控，失效后所有写接口统一返回
    code=301「需要登录」。此时若开了 ``auto_relogin``，写简介/建歌单失败会
    自动尝试续期或重新登录，再重试一次，避免整天卡在「简介写入失败待补写」。

    两条重登路径：
    1. **续期**（``/login/token/refresh``）：用现有 cookie 续期，不需要账号密码，
       cookie 还没彻底失效时最省事；
    2. **账密登录**（``/login/cellphone``）：需要 ``phone`` + 密码。密码可填明文
       ``password``（本地配置文件里保存）或直接填 ``password_md5`` 避免明文。

    ⚠️ 配置里存的是账号凭证，请确保 ``data/config.yaml`` 只有自己可读。
    """

    #: 写接口失败且判定为登录态失效时，自动续期 / 重新登录后重试
    auto_relogin: bool = True
    #: 登录手机号（留空则只做 cookie 续期，不做账密登录）
    phone: str = ""
    #: 登录密码（明文，仅保存在本机配置文件）
    password: str = ""
    #: 登录密码的 md5（十六进制小写）；填了它就不用填 password
    password_md5: str = ""
    #: 手机号国家码
    countrycode: str = "86"
    #: 两次自动重登尝试的最小间隔（秒），防止风控期疯狂重试
    relogin_cooldown: int = 300


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


class LogsConfig(BaseModel):
    """运行日志缓冲（网页端「日志」页的数据源）。

    NoneBot 把日志打到 stdout，进程里没有可读的日志文件（服务器上由 systemd
    收进 journald），所以这里在内存里留一个定长环形缓冲，供网页端查看。
    缓冲只含**本次进程启动后**的日志；需要跨重启留存就把 ``file`` 配上。
    """

    #: 内存里保留的日志条数（50 ~ 200000）。满了丢最旧的
    lines: int = 2000
    #: 记录的最低等级：TRACE/DEBUG/INFO/SUCCESS/WARNING/ERROR/CRITICAL
    level: str = "INFO"
    #: 额外落盘的文件路径（相对路径基于项目根目录）；留空表示不落盘。
    #: 按 5MB 轮转、保留最近 3 份
    file: str = ""
    #: 记录「收到 / 发出的群消息」摘要（含普通聊天，不只音乐分享）。
    #: 想在日志页看到「谁在什么时候发了什么、机器人回了什么」就保持开启；
    #: 嫌吵可以关掉——开关收集、改设置、归档结果这些事件日志不受影响。
    trace_messages: bool = True


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
    #: 泰文字体路径（歌单图片里的泰文昵称/歌名需要）。留空则自动探测系统中的泰文字体
    #: （如 fonts-noto-sans-thai）。主字体（CJK）不含泰文，不装会显示成方块。
    thai_font_path: Optional[str] = None
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
    netease: NeteaseConfig = Field(default_factory=NeteaseConfig)
    card: CardConfig = Field(default_factory=CardConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    clear: ClearConfig = Field(default_factory=ClearConfig)
    intro: IntroConfig = Field(default_factory=IntroConfig)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)
    master: MasterConfig = Field(default_factory=MasterConfig)
    logs: LogsConfig = Field(default_factory=LogsConfig)


#: 键名里含这些词的配置项属于敏感信息，写日志时打码，别把密码 / cookie 记进日志页
_SECRET_KEYWORDS = ("password", "cookie", "token", "secret", "access_key")


def _mask_value(key: str, value: object) -> str:
    """把配置值渲染成适合写进日志的一行：敏感项打码、长文本截断、换行转义。"""
    low = key.lower()
    if any(word in low for word in _SECRET_KEYWORDS):
        return "******" if value else "(空)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "(空)"
    if isinstance(value, dict):
        return f"{{…{len(value)} 项…}}" if value else "{}"
    if isinstance(value, (list, tuple)):
        shown = ", ".join(str(v) for v in list(value)[:6])
        return f"[{shown}{', …' if len(value) > 6 else ''}]"
    text = str(value).replace("\n", "\\n")
    return text if len(text) <= 80 else text[:77] + "…"


class ConfigManager:
    """单例式配置管理器，负责加载 / 保存 / 热更新。

    多进程 / 手工编辑的自我保护：

    - **自动重载**：读取 ``config`` 时比对配置文件 mtime，发现被外部改过就先
      重新加载。否则运行中的进程会一直用启动时的旧配置（典型症状：网页端改了
      歌单名模板 / 期号，实际归档仍按旧值走，表现为「设置不生效」）。
    - **合并写入**：``update`` 写盘前先重载磁盘最新值，只覆盖本次要改的那个
      键。否则一个持旧内存的进程会把**整份**配置写回去，把别的进程刚写好的
      期号、模板等一起回退（典型症状：新窗口期号还是上一期的数字）。
    """

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._config: AppConfig = AppConfig()
        #: 上次读/写该文件时的 mtime，用于检测外部改动
        self._mtime: float = 0.0
        #: 是否已经 ``load()`` 过。没加载过时保持代码默认值，不做自动重载
        #: （否则测试/离线脚本会意外读到真实的 data/config.yaml）
        self._loaded: bool = False
        #: 内存态版本号，每次 load/save 自增。``effective_config`` 的缓存键用它
        #: 而不是 mtime —— 某些文件系统 mtime 只有秒级精度，同一秒内连写两次
        #: 会拿到相同的 mtime，缓存就不会失效。
        self._gen: int = 0

    def _disk_mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def reload_if_changed(self) -> bool:
        """磁盘上的配置比内存里新时自动重载；返回是否发生了重载。

        配置写坏（手工编辑语法错误）时保留内存里的旧配置，只记一条警告，
        免得把正在运行的服务打挂。
        """
        if not self._loaded:
            return False
        mtime = self._disk_mtime()
        if mtime <= self._mtime:
            return False
        try:
            self.load()
        except Exception as exc:  # noqa: BLE001 — 坏配置不应中断服务
            self._mtime = mtime  # 避免每次访问都重试解析同一个坏文件
            logger.warning(f"[music] 配置文件解析失败，继续使用内存中的旧配置: {exc}")
            return False
        logger.info("[music] 检测到配置文件被外部修改，已自动重载")
        return True

    @property
    def config(self) -> AppConfig:
        self.reload_if_changed()
        return self._config

    def load(self) -> AppConfig:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # 标记「已加载」，此后 config 访问才会做外部改动自动重载
        self._loaded = True

        if not self.path.exists():
            if EXAMPLE_CONFIG_PATH.exists():
                shutil.copyfile(EXAMPLE_CONFIG_PATH, self.path)
            else:
                self._config = AppConfig()
                self.save()
                return self._config

        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self._config = AppConfig.model_validate(raw)
        self._mtime = self._disk_mtime()
        self._gen += 1
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
                self._mtime = self._disk_mtime()
                self._gen += 1
                return
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.2 * (attempt + 1))
        if last_err is not None:
            raise last_err

    def update(self, dotted_key: str, value: object) -> None:
        """按 `window.weekly.start` 这样的点分路径更新并落盘。

        写盘前先 ``reload_if_changed``：如果配置在别处（网页端 / 另一个进程 /
        手工编辑）被改过，先合并那些改动，只覆盖本次这一个键，避免把旧内存
        整份写回去、连带回退别人刚写好的期号 / 模板等。
        """
        self.reload_if_changed()
        parts = dotted_key.split(".")
        data = self._config.model_dump(mode="json")
        cursor = data
        for part in parts[:-1]:
            if part not in cursor or not isinstance(cursor[part], dict):
                raise KeyError(f"配置项不存在: {dotted_key}")
            cursor = cursor[part]
        if parts[-1] not in cursor:
            raise KeyError(f"配置项不存在: {dotted_key}")
        old = cursor[parts[-1]]
        cursor[parts[-1]] = value
        # 先校验再落盘，避免写坏配置
        self._config = AppConfig.model_validate(data)
        self.save()
        # 所有通道（网页端 / 群内 /music 命令 / 代码内部）的配置改动都汇到这里，
        # 统一记一条，网页端「日志」页就能看清「什么时候改了什么设置」。
        if old != value:
            logger.info(
                f"[config] 修改 {dotted_key}: {_mask_value(dotted_key, old)} → "
                f"{_mask_value(dotted_key, value)}"
            )


config_manager = ConfigManager()


# -------------------------------------------------------------------- 按群配置
#
# 绝大多数设置都允许「按群覆盖」：全局 config.yaml 是默认值，某个群想要不一样
# 就在 group_overrides.yaml 里记一条。判定基准只有一处 —— 见 is_group_scopable。
#
# 少数配置是**进程级唯一**的（一个进程只有一份日志缓冲、一个缓存目录、一个网易云
# 账号、一份字体探测结果），按群覆盖它们没有意义，因此列进白名单，写入时一律落到
# 全局配置；配置页里也只允许在「全局默认」层编辑。

#: 进程级唯一、不允许按群覆盖的完整键名
GLOBAL_ONLY_KEYS: frozenset[str] = frozenset({
    # 生效群号决定了「有哪些群」，本身不能按群设
    "groups",
    # 识别调试日志是 detector 的进程级开关
    "debug_detect",
    # 定时清理是个进程级任务，时刻/天数按群设无处执行
    "clear.scheduled_enabled",
    "clear.keep_days",
    "clear.prune_at",
    # 字体是整机探测结果（render 模块缓存），不按群区分
    "render.font_path",
    "render.thai_font_path",
})

#: 整段都不允许按群覆盖的配置段
GLOBAL_ONLY_PREFIXES: tuple[str, ...] = ("logs.", "cache.", "netease.")


def is_group_scopable(dotted_key: str) -> bool:
    """该配置项是否允许「按群覆盖」。

    不允许的项（见 ``GLOBAL_ONLY_KEYS`` / ``GLOBAL_ONLY_PREFIXES``）即使带了
    ``group_id`` 提交，也会被写到全局配置里 —— 与其存一个永远不会生效的值误导人，
    不如直接落到真正生效的那一层。
    """
    if dotted_key in GLOBAL_ONLY_KEYS:
        return False
    return not dotted_key.startswith(GLOBAL_ONLY_PREFIXES)


def _apply_dotted(data: dict, dotted_key: str, value: object) -> None:
    """把 ``a.b.c`` 形式的值写进嵌套字典；路径不存在（配置项已删除）时忽略。"""
    parts = dotted_key.split(".")
    cursor = data
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            return
        cursor = nxt
    if parts[-1] in cursor:
        cursor[parts[-1]] = value


class GroupOverrides:
    """按群覆盖项：``{群号: {dotted_key: 值}}``，落在 data/group_overrides.yaml。

    与 ``ConfigManager`` 一样做 mtime 自动重载（网页端 / 手工编辑完立刻生效）。
    """

    def __init__(self, path: Path = GROUP_OVERRIDES_PATH) -> None:
        self.path = path
        self._data: dict[int, dict[str, object]] = {}
        self._mtime: float = 0.0
        self._gen: int = 0

    def _disk_mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    @property
    def gen(self) -> int:
        return self._gen

    @property
    def mtime(self) -> float:
        return self._mtime

    def reload_if_changed(self) -> bool:
        mtime = self._disk_mtime()
        if mtime == self._mtime:
            return False
        if mtime <= 0.0:
            # 文件被删了（或从未存在）→ 视为没有任何覆盖
            if self._data:
                self._data = {}
                self._gen += 1
            self._mtime = 0.0
            return True
        try:
            self.load()
        except Exception as exc:  # noqa: BLE001 — 坏文件不该拖垮服务
            self._mtime = mtime
            logger.warning(f"[music] 按群配置解析失败，继续用内存中的旧值: {exc}")
            return False
        logger.info("[music] 检测到按群配置被外部修改，已自动重载")
        return True

    def load(self) -> dict[int, dict[str, object]]:
        if not self.path.exists():
            self._data = {}
            self._mtime = 0.0
            self._gen += 1
            return self._data
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        data: dict[int, dict[str, object]] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    gid = int(k)
                except (TypeError, ValueError):
                    continue
                if isinstance(v, dict):
                    data[gid] = {str(kk): vv for kk, vv in v.items()}
        self._data = data
        self._mtime = self._disk_mtime()
        self._gen += 1
        return self._data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {str(gid): ov for gid, ov in sorted(self._data.items()) if ov}
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=True, indent=2)
        last_err: Optional[Exception] = None
        for attempt in range(5):
            try:
                self.path.write_text(text, encoding="utf-8")
                self._mtime = self._disk_mtime()
                self._gen += 1
                return
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.2 * (attempt + 1))
        if last_err is not None:
            raise last_err

    # ---- 读 ----

    def group_ids(self) -> list[int]:
        self.reload_if_changed()
        return sorted(self._data)

    def get(self, group_id: int) -> dict[str, object]:
        self.reload_if_changed()
        return dict(self._data.get(int(group_id)) or {})

    def overridden_keys(self, group_id: int) -> list[str]:
        return sorted(self.get(group_id))

    def has_window_override(self, group_id: int) -> bool:
        """该群是否单独设过收集时间窗口（决定要不要为它单独排定时任务）。"""
        return any(k == "window" or k.startswith("window.") for k in self.get(group_id))

    # ---- 写 ----

    def set(self, group_id: int, dotted_key: str, value: object) -> None:
        gid = int(group_id)
        cur = dict(self._data.get(gid) or {})
        cur[dotted_key] = value
        self._data[gid] = cur
        self.save()

    def set_many(self, group_id: int, values: dict[str, object]) -> None:
        gid = int(group_id)
        cur = dict(self._data.get(gid) or {})
        cur.update(values)
        self._data[gid] = cur
        self.save()

    def remove(self, group_id: int, dotted_key: str) -> None:
        gid = int(group_id)
        cur = dict(self._data.get(gid) or {})
        if dotted_key not in cur:
            return
        cur.pop(dotted_key, None)
        if cur:
            self._data[gid] = cur
        else:
            self._data.pop(gid, None)
        self.save()

    def clear(self, group_id: int) -> None:
        gid = int(group_id)
        if gid not in self._data:
            return
        self._data.pop(gid, None)
        self.save()


group_overrides = GroupOverrides()

#: 生效配置缓存：群号 -> ((全局版本, 覆盖版本), AppConfig)
_EFFECTIVE_CACHE: dict[int, tuple[tuple[int, int], AppConfig]] = {}


def invalidate_effective_cache() -> None:
    _EFFECTIVE_CACHE.clear()


def effective_config(group_id: Optional[int]) -> AppConfig:
    """取某个群的**生效配置**：全局 config.yaml 叠加该群的覆盖项。

    - 没传群号 / 该群没有任何覆盖 → 直接返回全局对象（省掉深拷贝与校验）。
    - 覆盖项里的值会走一遍 pydantic 校验；写坏的值只记警告并回退全局，
      不让一条手滑的覆盖把整个群搞挂。
    """
    base = config_manager.config
    if group_id is None:
        return base
    gid = int(group_id)
    group_overrides.reload_if_changed()
    overrides = group_overrides.get(gid)
    if not overrides:
        return base
    stamp = (config_manager._gen, group_overrides.gen)
    cached = _EFFECTIVE_CACHE.get(gid)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    data = base.model_dump(mode="json")
    for key, value in overrides.items():
        _apply_dotted(data, key, value)
    try:
        cfg = AppConfig.model_validate(data)
    except Exception as exc:  # noqa: BLE001 — 坏覆盖回退全局
        logger.warning(f"[music] 群 {gid} 的配置覆盖无效，暂时按全局默认走: {exc}")
        cfg = base
    _EFFECTIVE_CACHE[gid] = (stamp, cfg)
    return cfg


def apply_config_value(group_id: Optional[int], dotted_key: str, value: object) -> str:
    """按群写入一个配置项，返回实际写入的层（``"group"`` 或 ``"global"``）。

    带群号且该项允许按群覆盖 → 写进 group_overrides.yaml；否则写全局 config.yaml。
    两条路径都会记一条 ``[config]`` 日志，网页端「运行日志」页看得见谁改了什么。
    """
    if group_id is not None and is_group_scopable(dotted_key):
        gid = int(group_id)
        old = group_overrides.get(gid).get(dotted_key)
        group_overrides.set(gid, dotted_key, value)
        if old != value:
            logger.info(
                f"[config] 群{gid} 覆盖 {dotted_key}: "
                f"{_mask_value(dotted_key, old)} → {_mask_value(dotted_key, value)}"
            )
        return "group"
    config_manager.update(dotted_key, value)
    return "global"


def reset_config_value(group_id: int, dotted_key: str) -> bool:
    """取消某个群的某项覆盖，恢复继承全局默认值。返回是否真的删掉了一条。"""
    gid = int(group_id)
    had = dotted_key in group_overrides.get(gid)
    group_overrides.remove(gid, dotted_key)
    if had:
        logger.info(f"[config] 群{gid} 取消覆盖 {dotted_key}（恢复继承全局默认）")
    return had

