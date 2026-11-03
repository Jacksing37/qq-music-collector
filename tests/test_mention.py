"""「模型自己决定 @ 谁」（mention）的回归。

只测不联网的部分：候选名单怎么建、名字怎么解析、工具怎么拒绝越界。
真发到群里要人工验（引用某人一句问「你怎么看」，看它有没有 @ 对被引用的那位）。

这里最关键的一组断言是**名单外的人点不到**——「拿不准就不 @」是靠这份小名单
写死的，不是靠模型自觉，所以必须有测试钉住它。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

if not os.environ.get("MUSIC_DATA_DIR"):
    os.environ["MUSIC_DATA_DIR"] = tempfile.mkdtemp(prefix="music-test-data-")

sys.path.insert(0, ".")

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")  # noqa: E402

from src.plugins.music_collector import mention as M  # noqa: E402
from src.plugins.music_collector import tools as T  # noqa: E402
from src.plugins.music_collector.config import AppConfig, LLMConfig  # noqa: E402

BOT = 3172424517
ASKER = 2531546239          # 菜老名
QUOTED = 1778690479         # Luvu53lfplz
BYSTANDER = 888888          # 群里没出现过的人

PEOPLE = [
    {"qq": ASKER, "nick": "菜老名", "aliases": ["老名"]},
    {"qq": QUOTED, "nick": "Luvu53lfplz", "aliases": []},
    {"qq": BYSTANDER, "nick": "路人甲", "aliases": []},
]


def _targets(text="（引用 Luvu53lfplz 的消息：雌小鬼太废了）\n菜老名(2531546239)：你怎么看", **kw):
    base = dict(
        sender_qq=ASKER, sender_nick="菜老名",
        quote_qq=QUOTED, quote_nick="Luvu53lfplz",
        at_qqs=[], text=text, people=PEOPLE, self_qq=BOT,
    )
    base.update(kw)
    return M.build_targets(**base)


# ---------------------------------------------------------------- 候选名单


def test_targets_only_from_context():
    """只有「本次出现过」的人进名单：提问者、被引用者、被 @ 的人、正文点名的。"""
    targets = _targets()
    qqs = [t.qq for t in targets]
    assert ASKER in qqs and QUOTED in qqs, qqs
    assert BYSTANDER not in qqs, "档案里没在本条上下文出现的人不许进名单"

    why = {t.qq: t.why for t in targets}
    assert "问你" in why[ASKER]
    assert "引用" in why[QUOTED]


def test_at_and_named_people():
    """消息里被 @ 的人、正文里点名的群友都能进。"""
    targets = _targets(text="帮我看下 老名 说的对不对", quote_qq=0, quote_nick="", at_qqs=[(BYSTANDER, "路人甲")])
    qqs = [t.qq for t in targets]
    assert BYSTANDER in qqs, "@ 出来的人该进名单"
    assert ASKER in qqs, "正文里点了别名「老名」，应该把他收进来"


def test_self_never_listed():
    targets = M.build_targets(sender_qq=BOT, sender_nick="BigDee", text="", people=[], self_qq=BOT)
    assert targets == [], "机器人自己不能进可 @ 名单"


def test_one_char_names_dont_match():
    """一个字的昵称太容易误命中，不该被正文匹配带进来。"""
    people = [{"qq": 777, "nick": "甲", "aliases": []}]
    targets = M.build_targets(sender_qq=ASKER, sender_nick="菜老名", text="甲说的", people=people)
    assert [t.qq for t in targets] == [ASKER], targets


# ---------------------------------------------------------------- 解析


def test_resolve_forms():
    targets = _targets()
    assert M.resolve("Luvu53lfplz", targets)[0] == QUOTED
    assert M.resolve("1778690479", targets)[0] == QUOTED
    assert M.resolve("Luvu53lfplz(1778690479)", targets)[0] == QUOTED
    assert M.resolve("老名", targets)[0] == ASKER, "别名也要能点"
    assert M.resolve("@菜老名", targets)[0] == ASKER


def test_resolve_rejects():
    targets = _targets()
    qq, err = M.resolve("路人甲", targets)
    assert qq is None and err, "名单外的人必须解析失败"
    qq, err = M.resolve("", targets)
    assert qq is None and err
    qq, err = M.resolve("999999999", targets)
    assert qq is None and err
    # 昵称和 QQ 对不上：不许蒙混
    qq, err = M.resolve("Luvu53lfplz(2531546239)", targets)
    assert qq is None and "对不上" in err, err


def test_resolve_duplicate_names():
    """同名多人：拒绝并要求用 昵称(QQ号) 指定。"""
    people = [
        {"qq": 111, "nick": "小明", "aliases": []},
        {"qq": 222, "nick": "小明", "aliases": []},
    ]
    targets = M.build_targets(sender_qq=0, sender_nick="", text="小明 你怎么看", people=people)
    assert len([t for t in targets if t.nick == "小明"]) == 2, targets
    qq, err = M.resolve("小明", targets)
    assert qq is None and "好几个" in err, err
    assert M.resolve("小明(222)", targets)[0] == 222


# ---------------------------------------------------------------- 工具


def _ctx(targets, cap=3):
    cfg = LLMConfig(mention_max=cap)
    return T.ToolContext(cfg=cfg, group_id=543486099, mention_targets=list(targets))


def test_handler_records_and_caps():
    async def run():
        targets = _targets()
        ctx = _ctx(targets)
        out = await T._handle_mention({"who": "Luvu53lfplz"}, ctx)
        assert ctx.mentions == [QUOTED], ctx.mentions
        assert "已记下" in out, out
        # 重复同一个人：不重复记
        await T._handle_mention({"who": "Luvu53lfplz"}, ctx)
        assert ctx.mentions == [QUOTED]
        # 第二个、第三个人
        await T._handle_mention({"who": "菜老名"}, ctx)
        assert ctx.mentions == [QUOTED, ASKER]
        # 超过上限：拒绝
        out = await T._handle_mention({"who": "菜老名"}, ctx)  # 已在列表里
        ctx.mention_targets.append(M.Target(qq=555, nick="第三人"))
        out = await T._handle_mention({"who": "第三人"}, ctx)
        assert ctx.mentions == [QUOTED, ASKER], "第三个已经超上限"
        assert "最多" in out, out

    asyncio.run(run())


def test_handler_rejects_outsider():
    async def run():
        targets = _targets()
        ctx = _ctx(targets)
        out = await T._handle_mention({"who": "路人甲"}, ctx)
        assert ctx.mentions == [], "名单外的人不许被记下"
        assert "这次能 @ 的人" in out, "拒绝时要把名单回给模型"
        assert "Luvu53lfplz" in out

    asyncio.run(run())


def test_handler_without_targets():
    async def run():
        ctx = T.ToolContext(cfg=LLMConfig(), group_id=1, mention_targets=[])
        out = await T._handle_mention({"who": "随便谁"}, ctx)
        assert ctx.mentions == []
        assert "没有可以 @ 的人" in out, out

    asyncio.run(run())


def test_tool_registered_only_with_targets():
    names_off = [t.name for t in T.build(LLMConfig(mention_enabled=False), 1, 2, "甲",
                                         mention_targets=_targets())[0]]
    assert "mention" not in names_off

    names_no_targets = [t.name for t in T.build(LLMConfig(), 1, 2, "甲")[0]]
    assert "mention" not in names_no_targets, "没人可 @ 时不该给这个工具"

    names_on = [t.name for t in T.build(LLMConfig(), 1, 2, "甲",
                                        mention_targets=_targets())[0]]
    assert "mention" in names_on, names_on
    assert "mention" in T.ALLOWED_TOOL_NAMES
    assert not T.looks_like_install("mention")


def test_rules_mention_the_hard_rule():
    """提示词里必须写着「拿不准就别 @」——这是给模型的最后一道提醒。"""
    rules = T.TOOL_RULES
    assert "mention" in rules
    assert "拿不准就别 @" in rules
    desc = T.MENTION.description
    assert "判断不了就别用" in desc and "宁可不 @" in desc
    assert "只能点" in desc


def test_config_defaults_and_labels():
    cfg = AppConfig().llm
    assert cfg.mention_enabled is True
    assert cfg.mention_max == 3

    from src.plugins.music_collector import webui as W

    keys = [f["key"] for s in W.SCHEMA for f in s["fields"]]
    for key in ("llm.mention_enabled", "llm.mention_max"):
        assert key in keys, f"schema 缺少 {key}"
        assert key in W.FIELD_META, f"FIELD_META 缺少 {key}"


if __name__ == "__main__":
    test_targets_only_from_context()
    test_at_and_named_people()
    test_self_never_listed()
    test_one_char_names_dont_match()
    test_resolve_forms()
    test_resolve_rejects()
    test_resolve_duplicate_names()
    test_handler_records_and_caps()
    test_handler_rejects_outsider()
    test_handler_without_targets()
    test_tool_registered_only_with_targets()
    test_rules_mention_the_hard_rule()
    test_config_defaults_and_labels()
    print("mention tests OK")
