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

        # ---- 机器人页（大模型回复）：独立一页，配置页不再出现 llm 段 ----
        assert all(sec["key"] != "llm" for sec in c.json()["schema"])
        b = client.get("/api/music-admin/bot", headers=H)
        assert b.status_code == 200
        bj = b.json()
        assert [sec["key"] for sec in bj["schema"]] == ["llm"]
        assert any(k.startswith("llm.") for k in bj["values"])
        assert "ready" in bj["status"] and "endpoint" in bj["status"]
        # 未带 token -> 401
        assert client.get("/api/music-admin/bot").status_code == 401

        # 没填 Key 时「测试连接」应给出可读失败（而不是 500、也不会真发请求）。
        # 先把 api_key 清空，保证这条断言不依赖上一个测试留下的配置、也不会真联网
        client.patch("/api/music-admin/config", headers=H,
                     json={"values": {"llm.api_key": "", "llm.enabled": False}})
        t = client.post("/api/music-admin/bot/test", headers=H, json={})
        assert t.status_code == 200 and t.json()["ok"] is False
        assert "没填全" in t.json()["message"]

        # 保存 llm 配置（走同一个 PATCH 接口）
        p3 = client.patch("/api/music-admin/config", headers=H,
                          json={"values": {"llm.enabled": True, "llm.model": "smoke-model"}})
        assert p3.status_code == 200 and p3.json()["ok"] is True
        b2 = client.get("/api/music-admin/bot", headers=H).json()
        assert b2["values"]["llm.model"] == "smoke-model" and b2["status"]["enabled"] is True

        # 采样 / 思考参数：网页能写、能回读，状态里能看到「实际发出去的参数」
        p4 = client.patch("/api/music-admin/config", headers=H, json={"values": {
            "llm.thinking": "high",
            "llm.thinking_style": "enable_thinking",
            "llm.thinking_budget": 2048,
            "llm.top_p": 0.8,
            "llm.extra_body": '{"top_k": 20}',
        }})
        assert p4.status_code == 200 and p4.json()["ok"] is True, p4.text
        b3 = client.get("/api/music-admin/bot", headers=H).json()
        assert b3["values"]["llm.thinking"] == "high"
        assert b3["values"]["llm.thinking_budget"] == 2048
        assert abs(b3["values"]["llm.top_p"] - 0.8) < 1e-6
        th = b3["status"]["thinking"]
        assert th["mode"] == "high" and th["style"] == "enable_thinking", th
        assert th["budget"] > 0, th
        pl = b3["status"]["payload"]
        assert pl["enable_thinking"] is True and pl["top_k"] == 20, pl
        assert "messages" not in pl and "api_key" not in pl, pl
        # 上下文占用（条数 + 字符数）与预算上限，供「快爆了没」判断
        assert "history_stats" in b3["status"] and b3["status"]["history_chars_limit"] == 3000, \
            b3["status"]

        # 工具与技能：状态里要能看出「真的挂上了什么」
        tw = b3["status"]["tools"]
        assert tw["enabled"] is True and "fetch_url" in tw["names"], tw
        assert tw["rounds"] == 3 and tw["scripts"] is False, tw
        assert isinstance(tw["skills"], list) and tw["skills_dir"], tw
        # 关掉总闸后请求体里就不该再有 tools
        client.patch("/api/music-admin/config", headers=H,
                     json={"values": {"llm.tools": False}})
        b_off = client.get("/api/music-admin/bot", headers=H).json()
        assert b_off["status"]["tools"]["enabled"] is False, b_off["status"]["tools"]
        assert b_off["status"]["tools"]["names"] == [], b_off["status"]["tools"]
        assert "tools" not in b_off["status"]["payload"], b_off["status"]["payload"]
        client.patch("/api/music-admin/config", headers=H,
                     json={"values": {"llm.tools": True}})

        # DeepSeek 写法：状态里要给出「实际强度」而不是一个不存在的预算
        p5 = client.patch("/api/music-admin/config", headers=H, json={"values": {
            "llm.model": "deepseek-v4.1-flash",
            "llm.thinking": "max",
            "llm.thinking_style": "deepseek",
        }})
        assert p5.status_code == 200 and p5.json()["ok"] is True, p5.text
        b4 = client.get("/api/music-admin/bot", headers=H).json()
        th4 = b4["status"]["thinking"]
        assert th4["style"] == "deepseek" and th4["mode"] == "max", th4
        assert th4["effort"] == "max", th4
        assert th4["budget"] == 0, th4
        pl4 = b4["status"]["payload"]
        assert pl4["thinking"] == {"type": "enabled"} and pl4["reasoning_effort"] == "max", pl4
        # 关闭：只发 thinking.disabled，不能留 reasoning_effort（它没有「关」这个值）
        client.patch("/api/music-admin/config", headers=H,
                     json={"values": {"llm.thinking": "off"}})
        pl5 = client.get("/api/music-admin/bot", headers=H).json()["status"]["payload"]
        assert pl5["thinking"] == {"type": "disabled"} and "reasoning_effort" not in pl5, pl5

        # 非法 extra_body 在保存时就被拦住（而不是等群里 @ 才发现）
        bad = client.patch("/api/music-admin/config", headers=H,
                           json={"values": {"llm.extra_body": "{oops"}})
        assert bad.status_code == 400 and "llm.extra_body" in bad.json()["errors"], bad.text

        # 还原，别影响后面的用例
        client.patch("/api/music-admin/config", headers=H, json={"values": {
            "llm.thinking": "auto", "llm.thinking_style": "auto", "llm.thinking_budget": 0,
            "llm.top_p": 1.0, "llm.extra_body": "", "llm.enabled": False,
            "llm.model": "smoke-model",
        }})

        # 清空上下文（内存态，随便调）
        hh = client.post("/api/music-admin/bot/history", headers=H, json={})
        assert hh.status_code == 200 and hh.json()["ok"] is True

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
