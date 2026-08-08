"""模拟签名服务 500，验证音乐卡片三级降级链。

复现的线上报错：
    [音乐卡片签名失败] 签名服务请求出错! Unexpected status code: 500
    Error: 消息体无法解析, 请检查是否发送了不支持的消息类型
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi")

from music_collector.bot_utils import (  # noqa: E402
    card_breaker,
    custom_music_card,
    music_card,
    send_music_card,
    song_fallback_text,
)
from music_collector.config import CardConfig  # noqa: E402
from music_collector.models import Song  # noqa: E402


def make_song() -> Song:
    return Song(
        platform="netease",
        song_id="450222919",
        title="没有理想的人不伤心",
        artists="新裤子",
        album="生命因你而火热",
        cover="https://p1.music.126.net/cover.jpg",
        url="https://music.163.com/#/song?id=450222919",
    )


class FakeBot:
    """记录每次 send 的消息类型；可指定哪些类型会失败。"""

    def __init__(self, fail_types: set[str]) -> None:
        self.fail_types = fail_types
        self.sent: list[str] = []

    async def send(self, event, message):
        kinds = [seg.type for seg in message]
        # 音乐段按 type 细分：163/qq 是原生，custom 是自定义
        label = kinds[0]
        if label == "music":
            label = "music:" + str(message[0].data.get("type", "?"))
        if label in self.fail_types:
            self.sent.append(label + "(失败)")
            raise RuntimeError("消息体无法解析, 请检查是否发送了不支持的消息类型")
        self.sent.append(label)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_segments_built():
    song = make_song()
    native = music_card(song)
    assert native is not None and native.data["type"] == "163"
    custom = custom_music_card(song)
    assert custom is not None and custom.data["type"] == "custom"
    assert custom.data["title"] == song.title
    assert custom.data["content"] == song.artists
    text = song_fallback_text(song)
    assert song.title in text and song.url in text and "新裤子" in text


def test_native_ok():
    card_breaker.reset()
    bot = FakeBot(fail_types=set())
    way = run(send_music_card(bot, None, make_song(), CardConfig()))
    assert way == "原生卡片", way
    assert bot.sent == ["music:163"], bot.sent


def test_native_fail_falls_back_to_custom():
    """签名服务 500：原生失败 → 自定义卡片顶上。"""
    card_breaker.reset()
    bot = FakeBot(fail_types={"music:163"})
    way = run(send_music_card(bot, None, make_song(), CardConfig()))
    assert way == "自定义卡片", way
    assert bot.sent == ["music:163(失败)", "music:custom"], bot.sent


def test_all_cards_fail_falls_back_to_text():
    """两种卡片都发不出去 → 文字兜底，信息不丢。"""
    card_breaker.reset()
    bot = FakeBot(fail_types={"music:163", "music:custom"})
    way = run(send_music_card(bot, None, make_song(), CardConfig()))
    assert way == "文字兜底", way
    assert bot.sent[-1] == "text", bot.sent
    assert "music:163(失败)" in bot.sent and "music:custom(失败)" in bot.sent


def test_breaker_skips_card_after_threshold():
    """连续失败到阈值后熔断，后续直接走文字，不再空等卡片。"""
    card_breaker.reset()
    cfg = CardConfig(failure_threshold=2, cooldown_minutes=10)
    song = make_song()
    for _ in range(2):
        bot = FakeBot(fail_types={"music:163", "music:custom"})
        run(send_music_card(bot, None, song, cfg))

    bot = FakeBot(fail_types={"music:163", "music:custom"})
    way = run(send_music_card(bot, None, song, cfg))
    assert way == "文字兜底", way
    # 熔断后一次卡片都不该尝试
    assert bot.sent == ["text"], bot.sent
    assert "熔断中" in card_breaker.status()


def test_mode_custom_and_off():
    card_breaker.reset()
    bot = FakeBot(fail_types=set())
    way = run(send_music_card(bot, None, make_song(), CardConfig(mode="custom")))
    assert way == "自定义卡片" and bot.sent == ["music:custom"], bot.sent

    card_breaker.reset()
    bot = FakeBot(fail_types=set())
    way = run(send_music_card(bot, None, make_song(), CardConfig(mode="off")))
    assert way == "文字兜底" and bot.sent == ["text"], bot.sent


def test_qq_platform_uses_mid_safe_path():
    """QQ 音乐 id 是 mid（非纯数字）时发不了原生卡片，应直接走自定义卡片。"""
    card_breaker.reset()
    song = Song(
        platform="qq",
        song_id="003OUlho2HcRHC",
        title="海阔天空",
        artists="Beyond",
        url="https://y.qq.com/n/ryqq/songDetail/003OUlho2HcRHC",
    )
    assert music_card(song) is None
    bot = FakeBot(fail_types=set())
    way = run(send_music_card(bot, None, song, CardConfig()))
    assert way == "自定义卡片", way
    assert bot.sent == ["music:custom"], bot.sent


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    card_breaker.reset()
    print("\n全部通过")
