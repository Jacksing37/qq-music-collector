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

        # 管理员接口（只读 GET，不改动 .env）
        a = client.get("/api/music-admin/admin", headers=H)
        assert a.status_code == 200
        assert "superusers" in a.json() and "note" in a.json()

        # 网易云账号接口（只读 GET）
        ac = client.get("/api/music-admin/account", headers=H)
        assert ac.status_code == 200
        assert "logged_in" in ac.json()

        print("webui e2e OK")


def test_per_group_endpoints():
    """按群配置：下拉数据源 / 按群读写 / 恢复继承 / 进程级项自动落全局。"""
    app = get_app()
    gid = 999000111
    with TestClient(app) as client:
        H = {"Authorization": "Bearer smoke-token"}

        # 下拉数据源
        g = client.get("/api/music-admin/groups", headers=H)
        assert g.status_code == 200
        assert "groups" in g.json() and "overrides" in g.json()

        # 按群写一个值
        base = client.get("/api/music-admin/config", headers=H).json()["values"]
        p = client.patch("/api/music-admin/config", headers=H,
                         json={"group_id": gid, "values": {"playlist.seq": 4321}})
        assert p.status_code == 200 and p.json()["ok"] is True

        # 群视图能读到覆盖值 + 标记为「已覆盖」，且未覆盖项继承全局
        c = client.get(f"/api/music-admin/config?group_id={gid}", headers=H).json()
        assert c["group_id"] == gid
        assert c["values"]["playlist.seq"] == 4321
        assert "playlist.seq" in c["overridden"]
        assert c["values"]["window.mode"] == base["window.mode"]
        # 全局视图不受影响
        assert client.get("/api/music-admin/config", headers=H).json()["values"]["playlist.seq"] == base["playlist.seq"]

        # 群出现在下拉里，并标出覆盖项数
        gj = client.get("/api/music-admin/groups", headers=H).json()
        assert gid in gj["groups"]
        assert "playlist.seq" in gj["overrides"][str(gid)]

        # 按群状态接口
        s = client.get(f"/api/music-admin/status?group_id={gid}", headers=H)
        assert s.status_code == 200 and "window_label" in s.json()

        # 进程级配置项：带群号提交也应落到全局，不进群覆盖
        old_keep = base["clear.keep_days"]
        p2 = client.patch("/api/music-admin/config", headers=H,
                          json={"group_id": gid, "values": {"clear.keep_days": old_keep}})
        assert p2.status_code == 200 and p2.json()["ok"] is True
        assert "clear.keep_days" not in client.get(
            f"/api/music-admin/config?group_id={gid}", headers=H).json()["overridden"]

        # 恢复继承
        r = client.patch("/api/music-admin/config", headers=H,
                         json={"group_id": gid, "reset": ["playlist.seq"]})
        assert r.status_code == 200 and r.json()["ok"] is True
        c2 = client.get(f"/api/music-admin/config?group_id={gid}", headers=H).json()
        assert c2["values"]["playlist.seq"] == base["playlist.seq"]
        assert "playlist.seq" not in c2["overridden"]

        # 全局层没有可恢复的覆盖
        r2 = client.patch("/api/music-admin/config", headers=H, json={"reset": ["playlist.seq"]})
        assert r2.status_code == 400 and r2.json()["ok"] is False

        print("webui per-group OK")


if __name__ == "__main__":
    test_endpoints()
    test_per_group_endpoints()
