"""网易云「掉登录自动重登」测试。

覆盖：
1. ``_password_md5``：优先用配置里的 password_md5，否则把明文 md5 一下；
2. ``_is_auth_error``：301 / 需要登录 / 未登录 视为登录态失效；
3. ``auto_relogin=False`` 时完全不动登录态（保持旧行为）；
4. ``ensure_logged_in``：cookie 续期失败 -> 账密重登成功；
5. 冷却期内跳过、``ignore_cooldown=True`` 可强制；
6. ``update_description`` 端到端：全通道 301 -> 自动重登 -> 重试写入成功；
7. 自动重登失败且 cookie 确实失效时，返回可读的失败原因。
"""

import asyncio
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import netease_api as na  # noqa: E402
from music_collector.netease_api import NeteaseAPI  # noqa: E402

# 测试里不要把通道之间的 1 秒间隔真的等出来
na.CHANNEL_GAP_SECONDS = 0.0


def make_cfg(**kw) -> types.SimpleNamespace:
    """构造一个 NeteaseConfig 的替身（只用到这几个字段）。"""
    return types.SimpleNamespace(
        auto_relogin=kw.get("auto_relogin", True),
        phone=kw.get("phone", "13800000000"),
        password=kw.get("password", "secret"),
        password_md5=kw.get("password_md5", ""),
        countrycode=kw.get("countrycode", "86"),
        relogin_cooldown=kw.get("relogin_cooldown", 300),
    )


def new_api(cfg) -> tuple[NeteaseAPI, pathlib.Path]:
    sess = pathlib.Path(tempfile.mkdtemp()) / "session.json"
    api = NeteaseAPI(sess, relogin_cfg=(lambda: cfg) if cfg is not None else None)
    api._save_session()  # 建好目录，避免 _save_session 在 tmp 里失败
    return api, sess


def test_pure_helpers() -> None:
    cfg = make_cfg(password="secret")
    import hashlib

    assert NeteaseAPI._password_md5(cfg) == hashlib.md5(b"secret").hexdigest()
    # 合法的 32 位 hex -> 直接采用
    assert NeteaseAPI._password_md5(make_cfg(password_md5="A" * 32)) == "a" * 32
    # 非法（长度不对）-> 回落明文
    assert NeteaseAPI._password_md5(make_cfg(password_md5="abc", password="p")) == (
        hashlib.md5(b"p").hexdigest()
    )
    # 都没有 -> 空串
    assert NeteaseAPI._password_md5(make_cfg(password="", password_md5="")) == ""

    assert NeteaseAPI._is_auth_error("linuxapi:code=301 需要登录")
    assert NeteaseAPI._is_auth_error("需要登录")
    assert NeteaseAPI._is_auth_error("未登录")
    assert not NeteaseAPI._is_auth_error("weapi:code=406 操作频繁")
    assert not NeteaseAPI._is_auth_error("")


async def test_disabled_keeps_old_behaviour() -> None:
    api, _ = new_api(make_cfg(auto_relogin=False))
    api._cookies["MUSIC_U"] = "x"
    hits: list[str] = []

    async def boom(*_a, **_k):
        hits.append("call")
        raise AssertionError("auto_relogin=False 时不应发起任何登录请求")

    api._post = boom
    api._eapi_post = boom
    api._linux_post = boom
    assert await api.ensure_logged_in(force=True) is True  # 只回落到 logged_in
    assert hits == []

    # 没接入回调（relogin_cfg=None）也一样不动
    api2, _ = new_api(None)
    api2._cookies["MUSIC_U"] = "x"
    assert await api2.ensure_logged_in(force=True) is True


async def test_refresh_then_password_login() -> None:
    api, _ = new_api(make_cfg())
    api._cookies["MUSIC_U"] = "old-cookie"
    calls: list[tuple[str, str]] = []
    state = {"valid": True}

    async def fake_weapi(path, payload):
        calls.append(("weapi", path))
        if path == "/login/cellphone":
            assert payload["phone"] == "13800000000"
            assert payload["password"] == __import__("hashlib").md5(b"secret").hexdigest()
            assert payload["countrycode"] == "86"
            return {"code": 200, "account": {"id": 7}}
        return {"code": 301, "message": "需要登录"}

    async def fake_eapi(path, payload):
        calls.append(("eapi", path))
        return {"code": 301}

    async def fake_linux(path, payload, os_name="linux"):
        calls.append(("linux", path))
        return {"code": 301}

    api._post = fake_weapi
    api._eapi_post = fake_eapi
    api._linux_post = fake_linux

    async def fake_valid():
        return state["valid"]

    api.session_valid = fake_valid

    # 第一条路径：续期
    assert await api.ensure_logged_in(force=True) is True
    paths = [p for _c, p in calls]
    assert "/login/token/refresh" in paths, paths
    assert "/login/cellphone" in paths, paths

    # 第二条路径：冷却期内不再尝试
    calls.clear()
    assert await api.ensure_logged_in(force=True) is False
    assert calls == [], "冷却期内不应发起请求"

    # 第三条路径：ignore_cooldown 强制再来一次
    assert await api.ensure_logged_in(force=True, ignore_cooldown=True) is True
    assert any(p == "/login/cellphone" for _c, p in calls), calls


async def test_update_description_auto_relogin_retry() -> None:
    api, _ = new_api(make_cfg())
    api._cookies["MUSIC_U"] = "stale"
    state = {"valid": False, "written": ""}
    seen: list[str] = []

    async def fake_linux(path, payload, os_name="linux"):
        seen.append(path)
        if path == "/playlist/desc/update":
            if not state["valid"]:
                return {"code": 301, "message": "需要登录"}
            state["written"] = payload["desc"]
            return {"code": 200}
        return {"code": 200}

    async def fake_eapi(path, payload):
        seen.append(path)
        return {"code": 301, "message": "需要登录"}

    async def fake_apipost(path, payload):
        seen.append(path)
        return {"code": 301, "message": "需要登录"}

    async def fake_weapi(path, payload):
        seen.append(path)
        if path == "/login/cellphone":
            state["valid"] = True
            return {"code": 200}
        return {"code": 301, "message": "需要登录"}

    async def fake_desc(_pid):
        return state["written"]

    async def fake_valid():
        return state["valid"]

    api._linux_post = fake_linux
    api._eapi_post = fake_eapi
    api._api_post = fake_apipost
    api._post = fake_weapi
    api.playlist_description = fake_desc
    api.session_valid = fake_valid

    ok, note = await api.update_description(123, "第一行\n第二行")
    assert ok is True, note
    assert "自动重登后" in note, note
    assert state["written"] == "第一行\n第二行"
    assert "/login/cellphone" in seen, seen


async def test_update_description_relogin_failed_is_readable() -> None:
    # 没配手机号密码 -> 无法账密重登；cookie 又确实失效 -> 给可读原因
    api, _ = new_api(make_cfg(phone="", password="", password_md5=""))
    api._cookies["MUSIC_U"] = "stale"
    state = {"valid": False}

    async def always_301(*_a, **_k):
        return {"code": 301, "message": "需要登录"}

    async def fake_valid():
        return state["valid"]

    async def fake_desc(_pid):
        return ""

    api._linux_post = always_301
    api._eapi_post = always_301
    api._api_post = always_301
    api._post = always_301
    api.session_valid = fake_valid
    api.playlist_description = fake_desc

    ok, note = await api.update_description(123, "hello")
    assert ok is False
    assert "登录态已失效" in note, note
    assert "/music cookie" in note, note


async def main() -> None:
    test_pure_helpers()
    await test_disabled_keeps_old_behaviour()
    await test_refresh_then_password_login()
    await test_update_description_auto_relogin_retry()
    await test_update_description_relogin_failed_is_readable()
    print("OK test_netease_relogin")


if __name__ == "__main__":
    asyncio.run(main())
