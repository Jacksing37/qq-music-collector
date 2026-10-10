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
10. 405「操作频繁」的处理：**不重登**（实测换 cookie 无效），只如实报错并
    保留 ``code=405`` 供上层识别；eapi / api 回 405 是常态，不算频控；
11. 归档器：命中频控只试 1 次就入队（越试越频繁），非频控失败照旧重试 3 次；
12. 账密登录撞风控（code=8810）时把响应里的**人工验证链接**（``redirectUrl``）
    存进 ``last_risk_url`` 供 WebUI / 日志展示；登录成功后清空。
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

# 测试里不要把通道之间的 1 秒间隔真的等出来
na.CHANNEL_GAP_SECONDS = 0.0


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


async def test_rate_limit_does_not_relogin() -> None:
    """405 频控**不重登**（换 cookie 治不了），但 reason 要保住 ``code=405`` 供上层识别。

    依据 2026-10-10 线上实测：同一个 cookie 4 秒前刚写成功另一个歌单；
    eapi 续期返回 code=200 却不下发新 cookie；服务器 IP 的账密登录被
    网易云判「网络环境存在安全风险」(code=8810) 直接拒。故 405 只报错。
    """
    api, _ = new_api(make_cfg())
    api._cookies["MUSIC_U"] = "flagged"
    calls = {"n": 0}

    async def always_405(*_a, **_k):
        calls["n"] += 1
        return {"code": 405, "message": "操作频繁，请稍候再试"}

    async def fake_desc(_pid):
        return ""

    async def boom(*_a, **_k):
        raise AssertionError("405 频控不该触发重登或续期（换 cookie 无效）")

    api._linux_post = always_405
    api._eapi_post = always_405
    api._api_post = always_405
    api._post = always_405
    api.playlist_description = fake_desc
    api.login_with_password = boom
    api.refresh_token = boom

    ok, note = await api.update_description(123, "第一行\n第二行")
    assert ok is False
    assert "code=405" in note, note
    assert "频控" in note, note
    # 六条通道各试一次（linuxapi(pc) / linuxapi / linuxapi-batch / eapi / api / weapi）
    assert calls["n"] == 6, calls["n"]


async def test_plain_405_alone_is_not_labelled_rate_limited() -> None:
    """eapi / api 回 405 是常态：linuxapi 没回 405 时既不算频控、也不重登。"""
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


async def test_archiver_stops_retrying_on_rate_limit() -> None:
    """归档器：命中频控只试 1 次就入队（原地重试只会越试越频繁）。"""
    from music_collector.archiver import Archiver

    api, _ = new_api(make_cfg())
    calls = {"n": 0}
    saved: dict = {}

    async def always_405(playlist_id, desc, name=""):
        calls["n"] += 1
        return False, "linuxapi(pc):code=405 操作频繁，请稍候再试（网易云写接口频控）"

    class FakeStore:
        async def drop_pending_desc(self, playlist_id):
            return None

        async def save_pending_desc(self, playlist_id, name, group_id, desc, note, **kw):
            saved.update(pid=playlist_id, note=note, gid=group_id)

    api.update_description = always_405
    arch = Archiver(api, FakeStore())
    ok, note = await arch.write_description(
        999, "简介内容", name="歌单名", group_id=1, retries=3
    )
    assert ok is False
    assert calls["n"] == 1, f"频控只该试 1 次，实际 {calls['n']}"
    assert saved["pid"] == "999" and saved["gid"] == 1
    assert "code=405" in saved["note"], saved["note"]


async def test_archiver_still_retries_non_rate_limit_failures() -> None:
    """非频控失败照旧退避重试 3 次（对照组，防止把重试整个关掉）。"""
    from music_collector.archiver import Archiver

    api, _ = new_api(make_cfg())
    calls = {"n": 0}
    saved: dict = {}

    async def always_fail(playlist_id, desc, name=""):
        calls["n"] += 1
        return False, "weapi:code=-1 weapi 返回空响应（通道被拦截）"

    class FakeStore:
        async def drop_pending_desc(self, playlist_id):
            return None

        async def save_pending_desc(self, playlist_id, name, group_id, desc, note, **kw):
            saved.update(pid=playlist_id)

    api.update_description = always_fail
    arch = Archiver(api, FakeStore())
    ok, _ = await arch.write_description(888, "简介", name="n", group_id=2, retries=3)
    assert ok is False
    assert calls["n"] == 3, calls["n"]
    assert saved["pid"] == "888"


async def test_rate_limit_note_is_readable_without_credentials() -> None:
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


def test_extract_risk_url() -> None:
    """8810 响应里的人工验证链接要能被挖出来（含字段名/嵌套兜底）。"""
    url = "https://y.music.163.com/g/yida/e60c57537a0e41c4b3aecde63701369a"
    assert NeteaseAPI._extract_risk_url(
        {"code": 8810, "message": "您当前的网络环境存在安全风险", "redirectUrl": url}
    ) == url
    # 下划线命名
    assert NeteaseAPI._extract_risk_url({"code": 8810, "redirect_url": url}) == url
    # 藏在子字典里
    assert NeteaseAPI._extract_risk_url({"data": {"x": {"redirectUrl": url}}}) == url
    # 只给了一个 /g/yida/ 链接、键名不认识，也能兜底扫到
    assert NeteaseAPI._extract_risk_url({"weird": url}) == url
    # 正常响应 / 空数据 -> 空串
    assert NeteaseAPI._extract_risk_url({"code": 200}) == ""
    assert NeteaseAPI._extract_risk_url({}) == ""
    assert NeteaseAPI._extract_risk_url(None) == ""


async def test_login_8810_surfaces_verify_link() -> None:
    """账密登录撞风控：返回失败、拿到 redirectUrl、把链接存进 last_risk_url。"""
    api, _ = new_api(make_cfg())
    api._cookies["MUSIC_U"] = "flagged"
    url = "https://y.music.163.com/g/yida/deadbeef"
    err = {"code": 8810, "message": "您当前的网络环境存在安全风险", "redirectUrl": url}

    async def always_8810(*_a, **_k):
        return dict(err)

    api._post = always_8810
    api._eapi_post = always_8810
    api._linux_post = always_8810
    # 账密登录成功后才会 _save_session；此处不应被调用到（保证 8810 不算账密错误）
    ok, note = await api.login_with_password()
    assert ok is False
    assert "8810" in note, note
    assert api.last_risk_url == url, api.last_risk_url


async def test_login_success_clears_risk_url() -> None:
    """一旦账密登录成功，旧的验证链接应被清掉（不再误导）。"""
    api, _ = new_api(make_cfg())
    api._cookies["MUSIC_U"] = "old"
    api.last_risk_url = "https://y.music.163.com/g/yida/old"
    risks: list[str] = []

    async def weapi(path, payload):
        if path == "/login/cellphone":
            risks.append(api.last_risk_url)  # 成功前应已重置
            return {"code": 200, "account": {"id": 1}}
        return {"code": 301}

    api._post = weapi
    api._cookies["__csrf"] = "csrf"
    ok, _ = await api.login_with_password()
    assert ok is True
    assert api.last_risk_url == "", api.last_risk_url
    assert risks == [""], risks


async def test_ensure_logged_in_fresh_keeps_risk_link_on_refresh_fallback() -> None:
    """fresh 重登撞 8810 后靠续期救回：登录态虽可用，但风控链接仍保留给人工验证。"""
    api, _ = new_api(make_cfg())
    api._cookies["MUSIC_U"] = "flagged"
    url = "https://y.music.163.com/g/yida/cafe"

    async def always_8810(*_a, **_k):
        return {"code": 8810, "message": "您当前的网络环境存在安全风险", "redirectUrl": url}

    async def fake_refresh():
        return True

    async def fake_valid():
        return True

    api._post = always_8810
    api._eapi_post = always_8810
    api._linux_post = always_8810
    api.refresh_token = fake_refresh
    api.session_valid = fake_valid

    assert await api.ensure_logged_in(force=True, fresh=True) is True
    assert api.last_risk_url == url, api.last_risk_url


async def main() -> None:
    test_pure_helpers()
    test_rate_limit_helpers()
    test_extract_risk_url()
    await test_disabled_keeps_old_behaviour()
    await test_refresh_then_password_login()
    await test_update_description_auto_relogin_retry()
    await test_update_description_relogin_failed_is_readable()
    await test_capture_cookies_covers_eapi_and_linuxapi()
    await test_ensure_logged_in_fresh_skips_refresh()
    await test_rate_limit_does_not_relogin()
    await test_plain_405_alone_is_not_labelled_rate_limited()
    await test_rate_limit_note_is_readable_without_credentials()
    await test_archiver_stops_retrying_on_rate_limit()
    await test_archiver_still_retries_non_rate_limit_failures()
    await test_login_8810_surfaces_verify_link()
    await test_login_success_clears_risk_url()
    await test_ensure_logged_in_fresh_keeps_risk_link_on_refresh_fallback()
    print("OK test_netease_relogin")


if __name__ == "__main__":
    asyncio.run(main())
