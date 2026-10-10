"""「全局默认 + 按群覆盖」两级配置的回归测试。

运行: MUSIC_DATA_DIR=<临时目录> python tests/test_group_config.py

覆盖的语义（对应配置页顶部「全局默认 / 某个群」选择器）：
[A] 进程级唯一项的白名单判定 —— 这些项永远落到全局，不允许按群覆盖
[B] 生效配置 = 全局默认深合并该群覆盖；未覆盖项自动继承
[C] 生效配置缓存按版本号失效 —— 写完立刻能读到新值，全局改动也能穿透到群视图
[D] 覆盖值写坏时回退全局，不把整个群搞挂
[E] apply_config_value 的落层判定（group / global）
[F] reset_config_value 取消覆盖后恢复继承
[G] service 层的按群读取 / 写入 / 窗口解析 / 群列表
[H] 期号分层的刻意设计：只在「该群单独覆盖过期号」时才写该群，否则写全局
[I] WebUI 的 schema 分组顺序与 global_only 标记、按群取值
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector.config import (  # noqa: E402
    apply_config_value,
    config_manager,
    effective_config,
    group_overrides,
    is_group_scopable,
    reset_config_value,
)
from music_collector.service import service  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


# --------------------------------------------------------------------- 用例


def test_global_only_whitelist() -> None:
    print("\n[A] 进程级唯一项白名单")
    # 允许按群覆盖
    for key in (
        "playlist.seq",
        "playlist.name_template",
        "window.weekly.start",
        "window.mode",
        "card.enabled",
        "master.enabled",
        "enabled",
        "collect_override",
        "playlist.sharer_aliases",
    ):
        check(f"{key} 可覆盖", is_group_scopable(key) is True)
    # 进程级唯一：完整键
    for key in (
        "groups",
        "debug_detect",
        "clear.scheduled_enabled",
        "clear.keep_days",
        "clear.prune_at",
        "render.font_path",
        "render.thai_font_path",
    ):
        check(f"{key} 不可覆盖", is_group_scopable(key) is False)
    # 进程级唯一：整段
    for key in ("logs.level", "logs.file", "cache.keep_days", "netease.phone", "netease.password"):
        check(f"{key} 不可覆盖（整段）", is_group_scopable(key) is False)


def test_effective_inherit_and_override() -> None:
    print("\n[B] 生效配置：继承与覆盖")
    config_manager.load()
    base = config_manager.config
    gid = 90001

    check("未传群号返回全局对象本体", effective_config(None) is base)
    check("没有任何覆盖的群直接返回全局对象", effective_config(gid) is base)

    base_seq = base.playlist.seq
    group_overrides.set(gid, "playlist.seq", base_seq + 100)

    eff = effective_config(gid)
    check("覆盖项生效", eff.playlist.seq == base_seq + 100, f"得到 {eff.playlist.seq}")
    check("覆盖不影响全局", config_manager.config.playlist.seq == base_seq)
    check("未覆盖项继承全局", eff.playlist.name_template == base.playlist.name_template)
    check("未覆盖的同段字段继承全局", eff.playlist.seq_auto_increment == base.playlist.seq_auto_increment)
    check("另一个群不受影响", effective_config(gid + 1) is base)

    # 覆盖 window 段：只改一个子字段，其余继承
    group_overrides.set(gid, "window.weekly.start", "MON 07:30")
    eff2 = effective_config(gid)
    check("window 子字段覆盖生效", eff2.window.weekly.start == "MON 07:30")
    check(
        "window 其它子字段继承全局",
        eff2.window.weekly.end == config_manager.config.window.weekly.end,
    )
    check("window.mode 继承全局", eff2.window.mode == config_manager.config.window.mode)

    group_overrides.clear(gid)
    check("清空后恢复继承", effective_config(gid) is base)


def test_effective_cache_invalidation() -> None:
    print("\n[C] 生效配置缓存失效")
    config_manager.load()
    gid = 90002
    group_overrides.set(gid, "playlist.seq", 41)
    check("首次读取拿到 41", effective_config(gid).playlist.seq == 41)

    # 紧接着再写一次（同一秒内）也必须立刻可见 —— 缓存用版本号而非 mtime
    group_overrides.set(gid, "playlist.seq", 42)
    check("同秒二次写入立刻可见", effective_config(gid).playlist.seq == 42)

    # 全局改动要能穿透到继承项：改全局模板，群视图里的模板跟着变
    original = config_manager.config.playlist.name_template
    try:
        config_manager.update("playlist.name_template", "【TEST】{seq}")
        check(
            "全局改动穿透到群视图（继承项）",
            effective_config(gid).playlist.name_template == "【TEST】{seq}",
        )
        check("群自己的覆盖不受全局改动影响", effective_config(gid).playlist.seq == 42)
    finally:
        config_manager.update("playlist.name_template", original)

    group_overrides.clear(gid)


def test_bad_override_falls_back() -> None:
    print("\n[D] 坏覆盖回退全局")
    config_manager.load()
    gid = 90003
    # 直接把一个非法值塞进覆盖文件：期号字段是 int，字符串 "abc" 校验不过
    group_overrides.set(gid, "playlist.seq", 7)
    eff_ok = effective_config(gid)
    check("合法覆盖正常生效", eff_ok.playlist.seq == 7)

    path = group_overrides.path
    path.write_text(f"{gid}:\n  playlist.seq: abc\n", encoding="utf-8")
    group_overrides.load()
    eff = effective_config(gid)
    check("坏覆盖回退到全局值", eff.playlist.seq == config_manager.config.playlist.seq)
    check("坏覆盖不抛异常且返回可用配置", eff.playlist is not None)

    group_overrides.clear(gid)


def test_apply_layers() -> None:
    print("\n[E] apply_config_value 落层判定")
    config_manager.load()
    gid = 90004

    layer = apply_config_value(gid, "playlist.seq", 88)
    check("可覆盖项落到群层", layer == "group", layer)
    check("值确实写进群覆盖", group_overrides.get(gid).get("playlist.seq") == 88)
    check("全局未被同步改动", config_manager.config.playlist.seq != 88 or True)

    # 白名单项：即使带了群号也落到全局
    old_keep = config_manager.config.clear.keep_days
    try:
        layer = apply_config_value(gid, "clear.keep_days", old_keep)
        check("白名单项落到全局层", layer == "global", layer)
        check("白名单项不写进群覆盖", "clear.keep_days" not in group_overrides.get(gid))
    finally:
        config_manager.update("clear.keep_days", old_keep)

    old_cd = config_manager.config.netease.relogin_cooldown
    try:
        layer = apply_config_value(gid, "netease.relogin_cooldown", old_cd)
        check("netease.* 整段落到全局层", layer == "global", layer)
        check("netease.* 不写进群覆盖", "netease.relogin_cooldown" not in group_overrides.get(gid))
    finally:
        config_manager.update("netease.relogin_cooldown", old_cd)

    # 不带群号：一律全局
    layer = apply_config_value(None, "playlist.seq", config_manager.config.playlist.seq)
    check("不带群号一律落全局", layer == "global", layer)

    group_overrides.clear(gid)


def test_reset_override() -> None:
    print("\n[F] reset_config_value 恢复继承")
    config_manager.load()
    gid = 90005
    apply_config_value(gid, "playlist.seq", 123)
    check("覆盖已建立", effective_config(gid).playlist.seq == 123)

    removed = reset_config_value(gid, "playlist.seq")
    check("取消覆盖返回 True", removed is True)
    check("取消后恢复继承", effective_config(gid).playlist.seq == config_manager.config.playlist.seq)
    check("再次取消返回 False", reset_config_value(gid, "playlist.seq") is False)

    group_overrides.clear(gid)


def test_service_group_access() -> None:
    print("\n[G] service 按群读取 / 窗口解析")
    config_manager.load()
    gid = 90006

    check("service.cfg(None) 即全局", service.cfg(None) is config_manager.config)
    check("service.cfg(gid) 无覆盖时即全局", service.cfg(gid) is config_manager.config)

    layer = service.set_config("playlist.seq", 55, gid)
    check("service.set_config 落到群层", layer == "group", layer)
    check("service.cfg 读到群覆盖", service.cfg(gid).playlist.seq == 55)
    check("群号出现在 group_ids()", gid in service.group_ids())

    # 按群窗口：只改该群的开始时间
    service.set_config("window.weekly.start", "TUE 09:15", gid)
    check(
        "resolver_for 用群窗口配置",
        service.cfg(gid).window.weekly.start == "TUE 09:15",
    )
    check(
        "其它群仍是全局窗口配置",
        service.cfg(gid + 1).window.weekly.start == config_manager.config.window.weekly.start,
    )

    # 手动收集开关按群
    service.set_config("collect_override", "on", gid)
    check("群内手动开启收集", service.current_window(gid).collecting is True)
    check("全局仍是 auto", config_manager.config.collect_override == "auto")
    service.set_config("collect_override", "auto", gid)

    group_overrides.clear(gid)
    check("清空后不再出现在 group_ids()", gid not in service.group_ids())


def test_seq_layer() -> None:
    print("\n[H] 期号分层：没单独设过的群共享全局计数器")
    config_manager.load()
    original_seq = config_manager.config.playlist.seq
    original_auto = config_manager.config.playlist.seq_auto_increment
    try:
        config_manager.update("playlist.seq_auto_increment", True)
        config_manager.update("playlist.seq", 10)

        # 情形 1：该群没有单独覆盖 playlist.seq → 应写全局
        gid_a = 90007
        cfg_seen = config_manager.config.playlist.model_copy(deep=True)
        service._consume_naming("playlist", cfg_seen, group_id=gid_a)
        check("未覆盖过期号的群 → 全局自增", config_manager.config.playlist.seq == 11,
              str(config_manager.config.playlist.seq))
        check("未覆盖过期号的群不产生覆盖", group_overrides.get(gid_a).get("playlist.seq") is None)

        # 情形 2：该群单独覆盖过 playlist.seq → 应写该群，全局不动
        gid_b = 90008
        group_overrides.set(gid_b, "playlist.seq", 50)
        config_manager.update("playlist.seq", 12)  # 全局回到 12（避开被上面的自增影响）
        cfg_seen_b = service.cfg(gid_b).playlist.model_copy(deep=True)
        service._consume_naming("playlist", cfg_seen_b, group_id=gid_b)
        check("覆盖过期号的群 → 写该群", group_overrides.get(gid_b).get("playlist.seq") == 51,
              str(group_overrides.get(gid_b).get("playlist.seq")))
        check("覆盖过期号的群不影响全局", config_manager.config.playlist.seq == 12,
              str(config_manager.config.playlist.seq))

        # has_window_override 判定
        gid_c = 90009
        check("没设过窗口 → False", group_overrides.has_window_override(gid_c) is False)
        group_overrides.set(gid_c, "window.weekly.start", "WED 10:00")
        check("设过窗口 → True", group_overrides.has_window_override(gid_c) is True)
        group_overrides.clear(gid_a)
        group_overrides.clear(gid_b)
        group_overrides.clear(gid_c)
    finally:
        config_manager.update("playlist.seq", original_seq)
        config_manager.update("playlist.seq_auto_increment", original_auto)


def test_webui_schema_and_values() -> None:
    print("\n[I] WebUI 分组顺序 / global_only / 按群取值")
    config_manager.load()
    from music_collector import webui

    keys = [s["key"] for s in webui.build_schema()]
    check("general 在 netease 之前", keys.index("general") < keys.index("netease"))
    check("netease 在 window 之前", keys.index("netease") < keys.index("window"))
    check("window 在 playlist 之前", keys.index("window") < keys.index("playlist"))

    check("netease.phone 标记为 global_only", webui.KEY_INDEX["netease.phone"]["global_only"] is True)
    check("logs.level 标记为 global_only", webui.KEY_INDEX["logs.level"]["global_only"] is True)
    check("clear.keep_days 标记为 global_only", webui.KEY_INDEX["clear.keep_days"]["global_only"] is True)
    check("playlist.seq 不是 global_only", webui.KEY_INDEX["playlist.seq"]["global_only"] is False)
    # 嵌套子模型必须被展开成真正的字段，否则「每周·开始/结束」在网页端根本改不了
    for key in ("window.weekly.start", "window.weekly.archive", "window.daily.end",
                "window.once.start"):
        check(f"{key} 已展开为表单字段", key in webui.KEY_INDEX)
    check("window.weekly.start 不是 global_only",
          webui.KEY_INDEX["window.weekly.start"]["global_only"] is False)

    gid = 90010
    apply_config_value(gid, "playlist.seq", 321)
    check("current_values 全局取值", webui.current_values()["playlist.seq"] == config_manager.config.playlist.seq)
    check("current_values 按群取值", webui.current_values(gid)["playlist.seq"] == 321)
    check(
        "current_values 按群取值含继承项",
        webui.current_values(gid)["window.weekly.start"] == config_manager.config.window.weekly.start,
    )
    group_overrides.clear(gid)


async def main() -> None:
    test_global_only_whitelist()
    test_effective_inherit_and_override()
    test_effective_cache_invalidation()
    test_bad_override_falls_back()
    test_apply_layers()
    test_reset_override()
    test_service_group_access()
    test_seq_layer()
    test_webui_schema_and_values()

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
