"""验证 replysplit 的分条规则：**只按空行分**、其余原样、超限合并。

2026-11-06 定稿的规则：
- 空行（连续 2 个及以上换行）= 一条消息；
- 单行内的换行 / 逗号 / 句号 / 问号 / 感叹号**一律不切**（切了就是半句话一条）；
- 每段只清掉行首残留的时间戳；
- `enabled=False` 与「直接把原文发出去」逐字等价；
- 超限只合并、不丢弃（丢内容比多发一条更糟）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")  # 触发插件加载（__init__ 会 require apscheduler）

from music_collector.replysplit import (  # noqa: E402
    MERGE_JOINER,
    between_delay,
    clean_part,
    split_reply,
)


def test_empty_input_yields_no_parts() -> None:
    assert split_reply("") == []
    assert split_reply("   \n  ") == []


def test_blank_lines_split_into_parts() -> None:
    out = split_reply("第一句\n\n第二句\n\n第三句")
    assert out == ["第一句", "第二句", "第三句"], out


def test_multiple_blank_lines_count_as_one() -> None:
    """连续空行只算一个分隔，不能切出空消息。"""
    out = split_reply("第一句\n\n\n\n第二句")
    assert out == ["第一句", "第二句"], out


def test_single_newline_is_not_a_split_point() -> None:
    """单个换行是同一条消息里的折行，不切。"""
    out = split_reply("第一句\n第二句")
    assert out == ["第一句\n第二句"], out


def test_punctuation_is_never_split() -> None:
    """逗号 / 句号 / 问号 / 感叹号都不切——这是 2026-11-06 退回「只认空行」的核心断言。"""
    raw = (
        "哈？本小姐蓝色渐变长发加鲸鳍鲸尾，深蓝超透JS装真空上阵，"
        "裙摆短到一露腿就湿——杂鱼自己不会看吗，还问。"
    )
    out = split_reply(raw)
    assert out == [raw], out

    out = split_reply("好听，单曲循环了。")
    assert out == ["好听，单曲循环了。"], out


def test_disabled_is_verbatim_passthrough() -> None:
    """关掉开关 = 与原行为逐字一致：不清理、不切分，原样一条发出。"""
    raw = "[2026-11-05 12:30 周三] 早上好\n\n[2026-11-05 12:31 周三] 今天听什么"
    assert split_reply(raw, enabled=False) == [raw]
    assert split_reply("甲\n乙\n丙", enabled=False) == ["甲\n乙\n丙"]


def test_enabled_strips_timestamp_prefix_per_part() -> None:
    """每一段各自清理——只清整段开头会让后面几段的前缀漏出去。"""
    raw = "[2026-11-05 12:30 周三] 早上好\n\n[2026-11-05 12:31 周三] 今天听什么"
    out = split_reply(raw, enabled=True)
    assert out == ["早上好", "今天听什么"], out
    for part in out:
        assert not part.startswith("[2026-"), part


def test_model_style_reply_splits_on_blank_lines_only() -> None:
    """线上那条的形态：模型用空行分条 → 按空行发；行内逗号不动。"""
    raw = "哈？火山罐挨着人质\n\n那今晚开个自定义试试不就知道了\n\n人质别放得太挤，免得真被挤扁了~🐳"
    out = split_reply(raw)
    assert out == [
        "哈？火山罐挨着人质",
        "那今晚开个自定义试试不就知道了",
        "人质别放得太挤，免得真被挤扁了~🐳",
    ], out


def test_cap_merges_overflow_into_last_part() -> None:
    raw = "\n\n".join(f"第{i}条消息内容" for i in range(1, 16))
    out = split_reply(raw, max_parts=10)
    assert len(out) == 10, out
    assert MERGE_JOINER in out[-1], out[-1]
    # 只合并、不丢内容
    joined = "".join(out).replace(MERGE_JOINER, "")
    for i in range(1, 16):
        assert f"第{i}条消息内容" in joined, f"第{i}条被丢了"


def test_cap_one_means_single_message() -> None:
    out = split_reply("甲\n\n乙\n\n丙", max_parts=1)
    assert len(out) == 1, out
    assert "甲" in out[0] and "丙" in out[0]


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
    test_blank_lines_split_into_parts()
    test_multiple_blank_lines_count_as_one()
    test_single_newline_is_not_a_split_point()
    test_punctuation_is_never_split()
    test_disabled_is_verbatim_passthrough()
    test_enabled_strips_timestamp_prefix_per_part()
    test_model_style_reply_splits_on_blank_lines_only()
    test_cap_merges_overflow_into_last_part()
    test_cap_one_means_single_message()
    test_clean_part_strips_only_line_head_timestamp()
    test_between_delay_parses_range()
    test_between_delay_degrades_to_zero()
    print("replysplit tests OK")
