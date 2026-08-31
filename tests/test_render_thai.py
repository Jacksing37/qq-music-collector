"""泰文渲染回退：歌单图片里的泰文昵称/歌名要能显示（不变成方块）。

分两层验证：
1. 路由逻辑（不依赖真实字体）：泰文码点 U+0E00–U+0E7F 走泰文字体，其余走主字体；
   无泰文字体时泰文也走主字体（等价旧行为，不会崩）。
2. 真实渲染（依赖系统装有泰文字体）：有字体时开启/关闭泰文字体渲染结果应不同，
   证明泰文字体确实被用上；同时图片里应有文字像素。本机没装泰文字体时 SKIP。
"""
import asyncio
import sys
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: F401  —— 插件包导入前必须先 init
nonebot.init(driver="~fastapi")

from PIL import Image, ImageChops  # noqa: E402

from music_collector.config import RenderConfig  # noqa: E402
from music_collector.models import Song  # noqa: E402
from music_collector.render import (  # noqa: E402
    _find_thai_font,
    _script_font,
    _split_runs,
    render_song_list,
)


def test_script_routing():
    primary = object()
    thai = object()
    # 泰文码点 -> thai
    assert _script_font("ท", thai, primary) is thai
    assert _script_font("เ", thai, primary) is thai
    # 中文 / 英文 / 数字 -> primary
    assert _script_font("中", thai, primary) is primary
    assert _script_font("A", thai, primary) is primary
    assert _script_font("1", thai, primary) is primary
    # thai=None 时泰文也走 primary（无回退字体，等价旧行为）
    assert _script_font("ท", None, primary) is primary

    runs = _split_runs("中文ไทยMix", (primary, thai))
    assert [(f, r) for f, r in runs] == [
        (primary, "中文"),
        (thai, "ไทย"),
        (primary, "Mix"),
    ]


async def test_render_thai_real():
    thai_path = _find_thai_font(None)
    if not thai_path:
        print("[SKIP] 本机未检测到泰文字体，跳过真实渲染断言（装 fonts-noto-sans-thai 后重跑）")
        return

    songs = [
        Song(
            platform="netease", song_id="1", title="เพลงไทย",
            artists="นักร้อง", album="อัลบั้ม",
            sharer_name="เพื่อน", sharer_id=1,
        ),
    ]
    base_cfg = dict(show_cover=False, theme="dark")
    # 关闭泰文字体：强制找不到
    cfg_off = RenderConfig(thai_font_path="/nonexistent-thai.ttf", **base_cfg)
    # 开启泰文字体：自动探测
    cfg_on = RenderConfig(**base_cfg)

    out_dir = Path(tempfile.mkdtemp())
    try:
        off = await render_song_list(songs, "测试榜", "共 1 首", cfg_off, out_dir / "off")
        on = await render_song_list(songs, "测试榜", "共 1 首", cfg_on, out_dir / "on")
        assert off and on, "render 应产出图片"

        img_off = Image.open(off[0]).convert("RGB")
        img_on = Image.open(on[0]).convert("RGB")

        # 两种配置下泰文串渲染结果应不同（证明泰文字体确实被用上）
        diff = ImageChops.difference(img_off, img_on)
        changed = sum(1 for p in diff.getdata() if p != (0, 0, 0))
        assert changed > 0, "开启泰文字体后渲染结果应变化（泰文字体未被使用？）"

        # 图片里确有非背景（文字）像素
        bright = sum(1 for p in img_on.getdata() if sum(p) > 300)
        assert bright > 0, "图片里未画出任何文字像素"
    finally:
        for p in out_dir.rglob("*.png"):
            p.unlink(missing_ok=True)

    print(f"[OK] 泰文字体生效：{thai_path}")


if __name__ == "__main__":
    asyncio.run(test_render_thai_real())
    test_script_routing()
    print("OK test_render_thai")
