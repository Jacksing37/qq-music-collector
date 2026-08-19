"""Web UI 端到端冒烟：真实挂载 FastAPI 路由并用 TestClient 打一遍。"""

import os
import sys

sys.path.insert(0, ".")

os.environ["MUSIC_WEBUI_TOKEN"] = "smoke-token"

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")
nonebot.load_plugins("src/plugins")

from fastapi.testclient import TestClient  # noqa: E402
from nonebot import get_app  # noqa: E402


def test_endpoints():
    app = get_app()
    with TestClient(app) as client:
        H = {"Authorization": "Bearer smoke-token"}

        # 未带 token -> 401
        assert client.get("/api/music-admin/schema").status_code == 401

        # schema 含关键字段
        r = client.get("/api/music-admin/schema", headers=H)
        assert r.status_code == 200
        keys = [f["key"] for s in r.json() for f in s["fields"]]
        assert "window.mode" in keys and "playlist.sharer_style" in keys

        # 当前配置可读取
        c = client.get("/api/music-admin/config", headers=H)
        assert c.status_code == 200
        assert "values" in c.json() and "schema" in c.json()

        # 改一个值并验证落盘
        p = client.patch("/api/music-admin/config", headers=H,
                         json={"values": {"window.mode": "daily", "playlist.seq": 42}})
        assert p.status_code == 200 and p.json()["ok"] is True

        # 非法值被拒绝
        p2 = client.patch("/api/music-admin/config", headers=H,
                          json={"values": {"playlist.sharer_style": "nope"}})
        assert p2.status_code == 400 and p2.json()["ok"] is False

        # 状态接口
        s = client.get("/api/music-admin/status", headers=H)
        assert s.status_code == 200
        assert "window_label" in s.json()

        print("webui e2e OK")


if __name__ == "__main__":
    test_endpoints()
