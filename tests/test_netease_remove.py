"""netease_api.remove_tracks 通道降级测试（桩掉三个 post，验证 op=del 与参数）。"""

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector.netease_api import NeteaseAPI  # noqa: E402


async def main() -> None:
    api = NeteaseAPI(pathlib.Path("nope_session.json"))
    calls: list[tuple] = []

    async def fake_linux(path, payload, os_name="linux"):
        calls.append(("linux", path, dict(payload)))
        return {"code": 200}

    async def fake_api(path, payload):
        calls.append(("api", path, dict(payload)))
        return {"code": 200}

    async def fake_weapi(path, payload):
        calls.append(("weapi", path, dict(payload)))
        return {"code": 200}

    api._linux_post = fake_linux
    api._api_post = fake_api
    api._post = fake_weapi
    api._cookies["MUSIC_U"] = "x"  # 视为已登录

    res = await api.remove_tracks(123, ["9", "8"])
    assert res["code"] == 200, res
    # 首次尝试走 linuxapi，op=del，pid 为字符串
    assert calls[0][0] == "linux"
    assert calls[0][2]["op"] == "del"
    assert calls[0][2]["pid"] == "123"
    assert '"9"' in calls[0][2]["trackIds"] and '"8"' in calls[0][2]["trackIds"]

    # 空列表立即返回，不触发任何通道
    calls.clear()
    res2 = await api.remove_tracks(123, [])
    assert res2["code"] == 200 and calls == []

    # 全部通道失败 -> 抛 NeteaseError
    async def boom(_p, _pl, **_k):
        raise RuntimeError("blocked")
    api._linux_post = boom
    api._api_post = boom
    api._post = boom
    raised = False
    try:
        await api.remove_tracks(123, ["1"])
    except Exception as exc:  # noqa: BLE001
        raised = True
        assert "删歌失败" in str(exc)
    assert raised, "全部通道失败时应抛错"

    print("OK test_netease_remove")


if __name__ == "__main__":
    asyncio.run(main())
