"""网易云「掉登录自动重登」测试。

覆盖：
1. ``_password_md5``：优先用配置里的 password_md5，否则把明文 md5 一下；
2. ``_is_auth_error``：301 / 需要登录 / 未登录 视为登录态失效；
3. ``auto_relogin=False`` 时完全不动登录态（保持旧行为）；
4. ``ensure_logged_in``：cookie 续期失败 -> 账密重登成功；
5. 冷却期内跳过、``ignore_cooldown=True`` 可强制；
6. ``update_description`` 端到端：全通道 301 -> 自动重登 -> 重试写入成功；
7. 自动重登失败且 cookie 确实失效时，返回可读的失败原因；
8. ``_capture_cookies``：eapi / linuxapi / api 通道下发的 Set-Cookie 也必须
   吸收并落盘（原先只有 weapi 收，于是走 eapi 通道登录「成功但 cookie 没换」）；
9. ``ensure_logged_in(fresh=True)``：跳过续期、直接用账密换一套全新 cookie；
10. 405「操作频繁」的归因：只有 **linuxapi 也回 405** 才算疑似风控、才重登；
    eapi / api 回 405 是常态（这两条通道本来就写不进简介），不得触发重登。
"""

import asyncio
import hashlib
import json
import pathlib
import sys
import tempfile
import types

import httpx  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import netease_api as na  # noqa: E402
from music_collector.netease_api import NeteaseAPI  # noqa: E402

# 测试里不要把通道之间的 1 秒间隔 / 405 退避真的等出来
na.CHANNEL_GAP_SECONDS = 0.0
na.RATE_LIMIT_BACKOFF_SECONDS = 0.0


def _resp(payload: dict, set_cookies: dict | None = None) -> httpx.Response:
    """造一个「带 Set-Cookie」的 httpx 响应（必须带 request，否则 .cookies 报错）。"""
    req = httpx.Request("POST", "https://music.163.com/api/playlist/desc/update")
    headers = [
        ("set-cookie", f"{k}={v}; Path=/") for k, v in (set_cookies or {}).items()
    ]
    return httpx.Response(
        200, headers=headers, content=json.dumps(payload).encode(), request=req
    )


def _patch_client(response: httpx.Response):
    """把 ``httpx.AsyncClient`` 换成固定返回该响应的假客户端。"""

    class FakeClient:
        def __init__(self, **_kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_k):
            return response

        async def get(self, *_a, **_k):
            return response

    return FakeClient


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


def test_rate_limit_helpers() -> None:
    assert NeteaseAPI._is_rate_limit("linuxapi(pc):code=405 操作频繁，请稍候再试")
    assert NeteaseAPI._is_rate_limit("eapi:code=405 操作频繁")
    assert NeteaseAPI._is_rate_limit("操作过于频繁")
    assert not NeteaseAPI._is_rate_limit("linuxapi:code=301 需要登录")
    assert not NeteaseAPI._is_rate_limit("")

    # linuxapi 系（含 linuxapi-batch）才算「唯一能写简介的通道」
    assert NeteaseAPI._is_linux_channel_error("linuxapi(pc):code=405 x")
    assert NeteaseAPI._is_linux_channel_error("linuxapi:code=405 x")
    assert NeteaseAPI._is_linux_channel_error("linuxapi-batch:code=405 x")
    assert not NeteaseAPI._is_linux_channel_error("eapi:code=405 x")
    assert not NeteaseAPI._is_linux_channel_error("api:code=405 x")


async def test_capture_cookies_covers_eapi_and_linuxapi() -> None:
    """eapi / linuxapi 通道下发的 Set-Cookie 也要吸收进内存并落盘。

    原实现只有 weapi 的 ``_post`` 捕获 cookie，于是本环境里登录 / 续期落到
    eapi、linuxapi 通道时，``_save_session()`` 只是把旧 cookie 又写了一遍——
    表现为「重登成功但 cookie 没换、照样 301/405」。
    """
    api, sess = new_api(make_cfg())
    api._cookies["MUSIC_U"] = "old-cookie"
    api._save_session()
    old_fp = api.cookie_fingerprint

    orig = na.httpx.AsyncClient
    try:
        # 1) eapi 通道
        na.httpx.AsyncClient = _patch_client(
            _resp({"code": 200}, {"MUSIC_U": "from-eapi", "__csrf": "csrf-e"})
        )
        data = await api._eapi_post("/playlist/desc/update", {"id": "1", "desc": "x"})
        assert data.get("code") == 200
        assert api._cookies["MUSIC_U"] == "from-eapi"
        assert api._cookies["__csrf"] == "csrf-e"
        assert api.cookie_fingerprint != old_fp
        assert api.cookie_fingerprint == hashlib.md5(b"from-eapi").hexdigest()[:8]
        saved = json.loads(sess.read_text(encoding="utf-8"))
        assert saved["cookies"]["MUSIC_U"] == "from-eapi", "eapi 通道的 cookie 没落盘"

        # 2) linuxapi 通道
        na.httpx.AsyncClient = _patch_client(
            _resp({"code": 200}, {"MUSIC_U": "from-linux"})
        )
        await api._linux_post("/playlist/desc/update", {"id": "1", "desc": "x"})
        assert api._cookies["MUSIC_U"] == "from-linux"

        # 3) api 明文通道
        na.httpx.AsyncClient = _patch_client(
            _resp({"code": 200}, {"MUSIC_U": "from-api"})
        )
        await api._api_post("/playlist/desc/update", {"id": "1", "desc": "x"})
        assert api._cookies["MUSIC_U"] == "from-api"

        saved = json.loads(sess.read_text(encoding="utf-8"))
        assert saved["cookies"]["MUSIC_U"] == "from-api", "api 通道的 cookie 没落盘"
    finally:
        na.httpx.AsyncClient = orig

    # 值没变时不算「刷新」，也不多写盘（保持原有语义）
    assert api._capture_cookies(_resp({"code": 200}, {"MUSIC_U": "from-api"})) is False


async def test_ensure_logged_in_fresh_skips_refresh() -> None:
    """``fresh=True``：跳过续期，直接用账密换一套全新 cookie。"""
    api, _ = new_api(make_cfg())
    api._cookies["MUSIC_U"] = "old-cookie"
    calls: list[str] = []
    state = {"valid": True}

    async def fake_weapi(path, payload):
        calls.append(path)
        if path == "/login/cellphone":
            api._cookies["MUSIC_U"] = "brand-new"
            return {"code": 200}
        return {"code": 200}

    async def fake_valid():
        return state["valid"]

    api._post = fake_weapi
    api.session_valid = fake_valid

    assert await api.ensure_logged_in(force=True, fresh=True) is True
    assert "/login/cellphone" in calls, calls
    assert "/login/token/refresh" not in calls, "fresh=True 不该先去续期"
    assert api._cookies["MUSIC_U"] == "brand-new"

    # 没配账密时退回续期兜一下，别什么都不做
    api2, _ = new_api(make_cfg(phone="", password="", password_md5=""))
    api2._cookies["MUSIC_U"] = "flagged"
    calls2: list[str] = []

    async def fake_weapi2(path, payload):
        calls2.append(path)
        return {"code": 200}

    async def fake_valid2():
        return True

    api2._post = fake_weapi2
    api2.session_valid = fake_valid2
    assert await api2.ensure_logged_in(force=True, fresh=True) is True
    assert calls2 == ["/login/token/refresh"], calls2


async def test_update_description_rate_limit_relogin_after_backoff() -> None:
    """连 linuxapi 都 405（疑似这套 cookie 被风控）-> 账密换新 cookie -> 重试成功。"""
    api, _ = new_api(make_cfg())
    api._cookies["MUSIC_U"] = "flagged"
    state = {"blocked": True, "written": "", "fingerprints": []}
    seen: list[tuple[str, str]] = []

    async def fake_linux(path, payload, os_name="linux"):
        seen.append(("linux", path))
        if path == "/playlist/desc/update":
            if state["blocked"]:
                return {"code": 405, "message": "操作频繁，请稍候再试"}
            state["written"] = payload["desc"]
            return {"code": 200}
        return {"code": 200}

    async def fake_405(*_a, **_k):
        return {"code": 405, "message": "操作频繁，请稍候再试"}

    async def fake_weapi(path, payload):
        seen.append(("weapi", path))
        if path == "/login/cellphone":
            state["blocked"] = False
            api._cookies["MUSIC_U"] = "brand-new"
            return {"code": 200}
        return {"code": 405, "message": "操作频繁，请稍候再试"}

    async def fake_desc(_pid):
        return state["written"]

    async def fake_valid():
        return True

    api._linux_post = fake_linux
    api._eapi_post = fake_405
    api._api_post = fake_405
    api._post = fake_weapi
    api.playlist_description = fake_desc
    api.session_valid = fake_valid

    # 记录每次写盘时的指纹，用于确认「重登确实换了 cookie」
    orig_save = api._save_session

    def spy_save():
        state["fingerprints"].append(api._cookies.get("MUSIC_U", ""))
        orig_save()

    api._save_session = spy_save

    ok, note = await api.update_description(123, "第一行\n第二行")
    assert ok is True, note
    assert "自动重登后" in note, note
    assert state["written"] == "第一行\n第二行"
    assert ("weapi", "/login/cellphone") in seen, seen
    assert ("weapi", "/login/token/refresh") not in seen, "405 场景应跳过续期直接换 cookie"
    # 重登时确实写入了新 cookie（指纹从 flagged 变成了 brand-new）
    assert "brand-new" in state["fingerprints"], state["fingerprints"]


async def test_plain_405_alone_does_not_relogin() -> None:
    """eapi / api 回 405 是常态：linuxapi 没回 405 时不得触发重登。"""
    api, _ = new_api(make_cfg())
    api._cookies["MUSIC_U"] = "ok"
    seen: list[str] = []

    async def fake_linux(path, payload, os_name="linux"):
        seen.append(path)
        return {"code": 200}  # 接口说成功，但读回对不上 -> 算失败

    async def fake_405(path, payload):
        seen.append(path)
        return {"code": 405, "message": "操作频繁，请稍候再试"}

    async def fake_desc(_pid):
        return "歌单里本来的旧简介"

    async def boom(*_a, **_k):
        raise AssertionError("linuxapi 没回 405 时不该触发重登")

    api._linux_post = fake_linux
    api._eapi_post = fake_405
    api._api_post = fake_405
    api._post = fake_405
    api.playlist_description = fake_desc
    api.login_with_password = boom
    api.refresh_token = boom

    ok, note = await api.update_description(321, "新简介")
    assert ok is False
    assert "405" in note, note
    assert "/login/cellphone" not in seen and "/login/token/refresh" not in seen


async def test_rate_limit_without_credentials_is_readable() -> None:
    """405 且没配账密：给可读原因，并且提示已入队等待补写。"""
    api, _ = new_api(make_cfg(phone="", password="", password_md5=""))
    api._cookies["MUSIC_U"] = "flagged"

    async def always_405(*_a, **_k):
        return {"code": 405, "message": "操作频繁，请稍候再试"}

    async def fake_desc(_pid):
        return ""

    async def fake_valid():
        return True

    api._linux_post = always_405
    api._eapi_post = always_405
    api._api_post = always_405
    api._post = always_405
    api.playlist_description = fake_desc
    api.session_valid = fake_valid

    ok, note = await api.update_description(9, "x")
    assert ok is False
    assert "405" in note, note
    assert "补写" in note, note
    assert "linuxapi" in note, note


async def main() -> None:
    test_pure_helpers()
    test_rate_limit_helpers()
    await test_disabled_keeps_old_behaviour()
    await test_refresh_then_password_login()
    await test_update_description_auto_relogin_retry()
    await test_update_description_relogin_failed_is_readable()
    await test_capture_cookies_covers_eapi_and_linuxapi()
    await test_ensure_logged_in_fresh_skips_refresh()
    await test_update_description_rate_limit_relogin_after_backoff()
    await test_plain_405_alone_does_not_relogin()
    await test_rate_limit_without_credentials_is_readable()
    print("OK test_netease_relogin")


if __name__ == "__main__":
    asyncio.run(main())
