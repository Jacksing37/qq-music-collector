"""验证 replysplit 的分条规则：换行优先、标点兜底、超限合并、碎片回并。

关键约束（对应设计文档 `docs/聊天体验拼装设计.md` §5 P1 验收）：
- `enabled=False` 必须与"直接把原文发出去"逐字等价——关掉开关就是零行为变化
- 清理必须在合并之前完成，否则被合并段开头的残留前缀会留在消息里
- 超限只合并、不丢弃（丢内容比多发一条更糟）
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")  # 触发插件加载（__init__ 会 require apscheduler）

from music_collector.replysplit import (  # noqa: E402
    FRAGMENT_MAX,
    MERGE_JOINER,
    between_delay,
    clean_part,
    split_reply,
)


def test_empty_input_yields_no_parts() -> None:
    assert split_reply("") == []
    assert split_reply("   \n  ") == []


def test_single_short_line_unchanged() -> None:
    """单段短文本必须原样返回——这是"默认行为不变"的核心断言。"""
    assert split_reply("好的，我知道了。") == ["好的，我知道了。"]


def test_line_breaks_split_into_parts() -> None:
    out = split_reply("第一句\n第二句\n第三句")
    assert out == ["第一句", "第二句", "第三句"], out


def test_consecutive_line_breaks_count_as_one() -> None:
    out = split_reply("第一句\n\n\n第二句")
    assert out == ["第一句", "第二句"], out


def test_disabled_is_verbatim_passthrough() -> None:
    """关掉开关 = 与原行为逐字一致：不清理、不切分，原样一条发出。

    这是 P1 的硬约束（关掉时零行为变化）。刻意**不**顺手清理时间戳——
    清理只在打开功能后生效，否则默认路径的输出就变了。
    """
    raw = "[2026-11-05 12:30 周三] 早上好\n[2026-11-05 12:31 周三] 今天听什么"
    out = split_reply(raw, enabled=False)
    assert out == [raw], out
    assert split_reply("甲\n乙\n丙", enabled=False) == ["甲\n乙\n丙"]


def test_enabled_strips_timestamp_prefix_per_part() -> None:
    """每一段都要各自清理——只清整段开头会让第二行起的前缀漏出去。"""
    raw = "[2026-11-05 12:30 周三] 早上好\n[2026-11-05 12:31 周三] 今天听什么"
    out = split_reply(raw, enabled=True)
    assert out == ["早上好", "今天听什么"], out
    for part in out:
        assert not part.startswith("[2026-"), part


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


def test_long_text_without_linebreak_splits_by_punct() -> None:
    text = "今天这首歌我循环了一整天，副歌部分特别抓耳。" * 3
    assert len(text) > 60
    out = split_reply(text)
    assert len(out) >= 2, out
    assert "".join(out) == text, "标点切分不得改动内容"


def test_short_text_is_not_punct_split() -> None:
    """短回复不该被切碎：60 字以内一律一条。"""
    text = "好听，单曲循环了。"
    assert split_reply(text) == [text]


def test_short_lines_from_model_are_kept_verbatim() -> None:
    """模型主动断的短行必须保留——中文「第一句」才 3 个字，不能被当成碎片吞掉。

    这条是改造过程中真实踩到的坑：碎片回并曾经作用于全部文本，导致
    「第一句\\n第二句\\n第三句」被合并成一句，模型的停顿意图全丢。
    """
    out = split_reply("第一句\n第二句\n第三句")
    assert len(out) == 3, out


def test_fragment_merged_into_previous_part() -> None:
    """仅对**算法兜底切分**生效：末尾碎块并回前一条，避免单独发一句「嗯。」。"""
    text = "我觉得这首歌的编曲特别好听。" * 5 + "嗯。"
    assert len(text) > 60
    out = split_reply(text)
    assert len(out) >= 2, out
    assert "嗯。" not in out, f"碎块被单独发出去了: {out}"
    assert out[-1].endswith("嗯。"), out[-1]
    assert len("嗯。") <= FRAGMENT_MAX


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
    test_single_short_line_unchanged()
    test_line_breaks_split_into_parts()
    test_consecutive_line_breaks_count_as_one()
    test_disabled_is_verbatim_passthrough()
    test_enabled_strips_timestamp_prefix_per_part()
    test_cap_merges_overflow_into_last_part()
    test_cap_one_means_single_message()
    test_long_text_without_linebreak_splits_by_punct()
    test_short_text_is_not_punct_split()
    test_short_lines_from_model_are_kept_verbatim()
    test_fragment_merged_into_previous_part()
    test_clean_part_strips_only_line_head_timestamp()
    test_between_delay_parses_range()
    test_between_delay_degrades_to_zero()
    print("replysplit tests OK")
