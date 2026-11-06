"""验证 replysplit 的分条规则：换行优先、再切句末标点、再切逗号、碎块回并、超限合并。

关键约束：
- `enabled=False` 必须与"直接把原文发出去"逐字等价——关掉开关就是零行为变化
- 清理必须在合并之前完成，否则被合并段开头的残留前缀会留在消息里
- 超限只合并、不丢弃（丢内容比多发一条更糟）
- 碎块（单字 / 纯表情）不单独成条：「哈？」要单独发，「嗯」「🐳」不行

2026-11-06 起分条细到**逗号**：中文聊天里一个逗号就是换一口气，真人是一句一条。
（原来只在「模型给换行」或「正文 >60 字」时才切，结果模型用空格断句的 45 字回复
整条发出去，群里看着就是一段小作文。）
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")  # 触发插件加载（__init__ 会 require apscheduler）

from music_collector.replysplit import (  # noqa: E402
    MERGE_JOINER,
    MIN_PART_CHARS,
    between_delay,
    clean_part,
    split_reply,
)


def test_empty_input_yields_no_parts() -> None:
    assert split_reply("") == []
    assert split_reply("   \n  ") == []


def test_disabled_is_verbatim_passthrough() -> None:
    """关掉开关 = 与原行为逐字一致：不清理、不切分，原样一条发出。"""
    raw = "[2026-11-05 12:30 周三] 早上好\n[2026-11-05 12:31 周三] 今天听什么"
    out = split_reply(raw, enabled=False)
    assert out == [raw], out
    assert split_reply("甲\n乙\n丙", enabled=False) == ["甲\n乙\n丙"]
    assert split_reply("好听，单曲循环了。", enabled=False) == ["好听，单曲循环了。"]


def test_line_breaks_split_into_parts() -> None:
    out = split_reply("第一句\n第二句\n第三句")
    assert out == ["第一句", "第二句", "第三句"], out


def test_consecutive_line_breaks_count_as_one() -> None:
    """模型惯用空行分条（`\\n\\n`）：不能被拆出一条空消息。"""
    out = split_reply("第一句\n\n\n第二句")
    assert out == ["第一句", "第二句"], out


def test_enabled_strips_timestamp_prefix_per_part() -> None:
    """每一段都要各自清理——只清整段开头会让第二行起的前缀漏出去。"""
    raw = "[2026-11-05 12:30 周三] 早上好\n[2026-11-05 12:31 周三] 今天听什么"
    out = split_reply(raw, enabled=True)
    assert out == ["早上好", "今天听什么"], out
    for part in out:
        assert not part.startswith("[2026-"), part


# ---------------------------------------------------------------- 逗号级切分


def test_comma_splits_into_short_parts() -> None:
    """线上点名要的那个效果：一条 45 字的长句 → 5 条像真人连发。"""
    raw = (
        "哈？本小姐蓝色渐变长发加鲸鳍鲸尾，深蓝超透JS装真空上阵，"
        "裙摆短到一露腿就湿——杂鱼自己不会看吗，还问。"
    )
    out = split_reply(raw)
    assert out == [
        "哈？",
        "本小姐蓝色渐变长发加鲸鳍鲸尾",
        "深蓝超透JS装真空上阵",
        "裙摆短到一露腿就湿——杂鱼自己不会看吗",
        "还问。",
    ], out


def test_comma_separator_is_stripped_but_question_mark_kept() -> None:
    """切点那个逗号不该留在句尾；「？」「。」是正文的一部分，留着。"""
    out = split_reply("好的，我知道了。")
    assert out == ["好的", "我知道了。"], out


def test_space_is_not_a_split_point() -> None:
    """空格不切（模型常用空格代顿号，切了就是半句话一条）。"""
    out = split_reply("6比6啊那还真有机会翻呢")
    assert out == ["6比6啊那还真有机会翻呢"], out


def test_colon_and_enumeration_mark_do_not_split() -> None:
    """冒号 / 顿号不切：「注意：」「苹果、香蕉」都是半句话。"""
    out = split_reply("注意：明天要早起，苹果、香蕉和梨都好吃")
    assert out == ["注意：明天要早起", "苹果、香蕉和梨都好吃"], out


def test_thousands_separator_not_split() -> None:
    """半角逗号后面紧跟数字（千分位）不切。"""
    out = split_reply("一共 1,000 首，够了")
    assert out == ["一共 1,000 首", "够了"], out


def test_no_punctuation_stays_one_part() -> None:
    out = split_reply("今天这首歌我循环了一整天副歌部分特别抓耳")
    assert len(out) == 1, out


# ---------------------------------------------------------------- 碎块回并


def test_emoji_fragment_merged_into_previous_part() -> None:
    """纯表情 / 纯符号的碎块并回上一条，别单独发一个 🐳。"""
    out = split_reply("别被挤扁了~🐳")
    assert out == ["别被挤扁了~🐳"], out
    out = split_reply("先这样。🐳")
    assert out == ["先这样。🐳"], out


def test_single_char_fragment_merged_into_previous_part() -> None:
    """1 个字太碎，并回上一条；2 个字（「哈？」）算正常一句，留着。"""
    out = split_reply("好啊，嗯")
    assert out == ["好啊嗯"], out
    out = split_reply("真的吗？哈？")
    assert out == ["真的吗？", "哈？"], out
    assert MIN_PART_CHARS == 2


# ---------------------------------------------------------------- 上限


def test_cap_merges_overflow_into_last_part() -> None:
    raw = "\n".join(f"第{i}条消息内容" for i in range(1, 16))
    out = split_reply(raw, max_parts=10)
    assert len(out) == 10, out
    assert MERGE_JOINER in out[-1], out[-1]
    # 只合并、不丢内容
    joined = "".join(out).replace(MERGE_JOINER, "")
    for i in range(1, 16):
        assert f"第{i}条消息内容" in joined, f"第{i}条被丢了"


def test_cap_one_means_single_message() -> None:
    out = split_reply("甲\n乙\n丙", max_parts=1)
    assert len(out) == 1, out
    assert "甲" in out[0] and "丙" in out[0]


def test_cap_also_applies_to_comma_split() -> None:
    """逗号切出来的段同样受条数上限约束，不会被切成十几条。"""
    raw = "，".join(f"第{i}句" for i in range(1, 21)) + "。"
    out = split_reply(raw, max_parts=6)
    assert len(out) == 6, out


# ---------------------------------------------------------------- 清理与间隔


def test_clean_part_strips_only_line_head_timestamp() -> None:
    assert clean_part("[2026-11-05 12:30 周三] 你好") == "你好"
    assert clean_part("[12:30] 在的") == "在的"
    assert clean_part("[12:30:05] 在的") == "在的"
    # 「注意：」这类正文开头不能被误剥
    assert clean_part("注意：明天要早起") == "注意：明天要早起"
    # 出现在句中（非行首）的时间戳不动
    assert clean_part("他说[2026-11-05]会来") == "他说[2026-11-05]会来"


def test_between_delay_parses_range() -> None:
    for _ in range(20):
        value = between_delay("0.5-1.5")
        assert 0.5 <= value <= 1.5, value
    assert between_delay("1~3") >= 1.0


def test_between_delay_degrades_to_zero() -> None:
    assert between_delay("") == 0.0
    assert between_delay("abc") == 0.0
    assert between_delay("0-0") == 0.0
    assert between_delay("-5") == 0.0
    # 反向区间按大小自动交换，不抛异常
    assert 0.0 <= between_delay("2-0.5") <= 2.0


if __name__ == "__main__":
    test_empty_input_yields_no_parts()
    test_disabled_is_verbatim_passthrough()
    test_line_breaks_split_into_parts()
    test_consecutive_line_breaks_count_as_one()
    test_enabled_strips_timestamp_prefix_per_part()
    test_comma_splits_into_short_parts()
    test_comma_separator_is_stripped_but_question_mark_kept()
    test_space_is_not_a_split_point()
    test_colon_and_enumeration_mark_do_not_split()
    test_thousands_separator_not_split()
    test_no_punctuation_stays_one_part()
    test_emoji_fragment_merged_into_previous_part()
    test_single_char_fragment_merged_into_previous_part()
    test_cap_merges_overflow_into_last_part()
    test_cap_one_means_single_message()
    test_cap_also_applies_to_comma_split()
    test_clean_part_strips_only_line_head_timestamp()
    test_between_delay_parses_range()
    test_between_delay_degrades_to_zero()
    print("replysplit tests OK")
