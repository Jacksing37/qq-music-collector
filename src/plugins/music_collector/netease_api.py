"""网易云音乐 API 客户端（纯自实现，无第三方 SDK 依赖）。

网易云有多条协议通道，可用性在不同网络环境下差异很大。本模块同时实现三条，
按实测可用性依次降级：

======  ==============================  ==========================================
通道     入口                            实测（2026-08 家宽环境）
======  ==============================  ==========================================
linuxapi ``/api/linux/forward``          ✅ 建单 / 加歌 / **改简介** 全部可用
eapi     ``interface.music.163.com``     ✅ 登录态 / 改名 / 改标签；❌ 改简介返回 405
api      ``/api/...`` 明文                ✅ 建单 / 加歌 / 查询；❌ 改简介返回 405
weapi    ``/weapi/...``                  ❌ 一律返回 200 空响应（被网关拦截）
======  ==============================  ==========================================

所以「改简介」必须走 linuxapi，这也是之前简介一直写不进去的根因：
旧实现只试了 weapi/api，两条都是静默失败。

写简介后会读回校验，确认真的落库才算成功。
Cookie 持久化在 data/netease_session.json，重启免登录。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import random
import string
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# ---- weapi 加密常量（网易云前端公开常量） ----
_NONCE = b"0CoJUm6Qyw8W8jud"
_IV = b"0102030405060708"
_PUB_KEY_E = 0x10001
_MODULUS = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725"
    "152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312"
    "ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424"
    "d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e",
    16,
)
# ---- eapi / linuxapi 加密常量 ----
_EAPI_KEY = b"e82ckenh8dichen8"
_LINUX_KEY = b"rFgB&h#%2?^eDg:Q"

_BASE62 = string.ascii_letters + string.digits

#: 多通道降级时相邻两次请求之间错开的秒数（网易云对同接口连发很敏感，
#: 挨着发容易触发 406「操作频繁」）。测试里可置 0 避免空等。
CHANNEL_GAP_SECONDS = 1.0

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_LINUX_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/68.0.3440.106 Safari/537.36"
)
_MOBILE_UA = (
    "NeteaseMusic/9.0.65.240927161425(9000065);Dalvik/2.1.0 "
    "(Linux; U; Android 13; PJA110 Build/TP1A.220905.001)"
)

try:  # 插件内运行用 nonebot logger，离线测试退回标准库
    from nonebot.log import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("music_collector.netease")


class NeteaseError(RuntimeError):
    """网易云接口返回了非 200 的业务码。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"网易云接口错误 code={code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 加解密


def _aes_cbc_b64(data: bytes, key: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, _IV)
    return base64.b64encode(cipher.encrypt(pad(data, AES.block_size)))


def _rsa_no_padding(text: str) -> str:
    reversed_text = text[::-1].encode("utf-8")
    number = int(binascii.hexlify(reversed_text), 16)
    return format(pow(number, _PUB_KEY_E, _MODULUS), "x").zfill(256)


def weapi_encrypt(payload: dict[str, Any]) -> dict[str, str]:
    """按 weapi 规则加密请求体。"""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    secret_key = "".join(random.choice(_BASE62) for _ in range(16))
    stage1 = _aes_cbc_b64(text, _NONCE)
    stage2 = _aes_cbc_b64(stage1, secret_key.encode("utf-8"))
    return {
        "params": stage2.decode("utf-8"),
        "encSecKey": _rsa_no_padding(secret_key),
    }


def eapi_encrypt(api_path: str, payload: dict[str, Any]) -> dict[str, str]:
    """eapi：AES-ECB(hex)，params 里带路径摘要防篡改。

    ``api_path`` 必须是 ``/api/xxx`` 形式（不是 ``/eapi/xxx``）。
    """
    text = json.dumps(payload, ensure_ascii=False)
    digest = hashlib.md5(
        f"nobody{api_path}use{text}md5forencrypt".encode("utf-8")
    ).hexdigest()
    data = f"{api_path}-36cd479b6b5-{text}-36cd479b6b5-{digest}"
    cipher = AES.new(_EAPI_KEY, AES.MODE_ECB)
    return {"params": cipher.encrypt(pad(data.encode("utf-8"), 16)).hex().upper()}


def linuxapi_encrypt(url: str, params: dict[str, Any]) -> dict[str, str]:
    """linuxapi：把整个请求（含目标 URL）打包成一段 AES-ECB 密文转发。"""
    body = json.dumps(
        {"method": "POST", "url": url, "params": params}, ensure_ascii=False
    )
    cipher = AES.new(_LINUX_KEY, AES.MODE_ECB)
    return {"eparams": cipher.encrypt(pad(body.encode("utf-8"), 16)).hex().upper()}


def _decode_maybe_eapi(raw: bytes) -> dict[str, Any]:
    """eapi 响应可能是明文 JSON，也可能是 AES-ECB hex 密文。"""
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        plain = unpad(AES.new(_EAPI_KEY, AES.MODE_ECB).decrypt(bytes.fromhex(raw.decode())), 16)
        return json.loads(plain)
    except Exception as exc:
        raise NeteaseError(-1, f"响应无法解析: {raw[:120]!r} ({exc})") from exc


class NeteaseAPI:
    BASE = "https://music.163.com"
    EAPI_BASE = "https://interface.music.163.com"

    def __init__(self, session_path: Path, relogin_cfg=None) -> None:
        self.session_path = Path(session_path)
        self._cookies: dict[str, str] = {
            "os": "pc",
            "appver": "8.9.75",
            "osver": "Microsoft-Windows-10",
            "deviceId": "".join(random.choice(_BASE62) for _ in range(32)),
        }
        self._lock = asyncio.Lock()
        #: 取「自动重登」配置的回调（每次调用现取，便于配置热更新）。None=不自动重登
        self._relogin_cfg = relogin_cfg
        #: 上次自动重登尝试的单调时钟，用于冷却（初值 -inf：进程刚起时不因
        #: 「系统开机不足 cooldown 秒」被误判进冷却）
        self._last_relogin: float = -float("inf")
        #: 最近一次账密登录撞到风控（code=8810「网络环境存在安全风险」）时，
        #: 网易云随响应下发的**人工验证跳转链接**。只有拿它在真实浏览器里过一遍
        #: 验证，本机 IP 才可能被放行。仅存内存（单次尝试有效、会过期），每次
        #: 账密登录开始前重置，成功后清空；供 WebUI / 日志展示。
        self.last_risk_url: str = ""
        self._load_session()

    # ------------------------------------------------------------ session

    def _load_session(self) -> None:
        if self.session_path.exists():
            try:
                saved = json.loads(self.session_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    self._cookies.update(saved.get("cookies", {}))
            except (json.JSONDecodeError, OSError):
                pass

    def _save_session(self) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.session_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"cookies": self._cookies, "saved_at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, self.session_path)

    @property
    def logged_in(self) -> bool:
        return bool(self._cookies.get("MUSIC_U"))

    def clear_session(self) -> None:
        self._cookies.pop("MUSIC_U", None)
        self._cookies.pop("__csrf", None)
        self._save_session()

    def set_cookie_string(self, raw: str) -> None:
        """支持手动粘贴浏览器 Cookie。"""
        for item in raw.split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                self._cookies[k.strip()] = v.strip()
        self._save_session()

    def _cookies_for(self, os_name: str) -> dict[str, str]:
        """不同通道要求不同的 os 标记。"""
        cookies = dict(self._cookies)
        cookies["os"] = os_name
        if os_name == "linux":
            cookies["appver"] = "1.2.1"
        elif os_name == "android":
            cookies["appver"] = "9.0.65"
        return cookies

    def _capture_cookies(self, resp: httpx.Response) -> bool:
        """把响应里的 ``Set-Cookie`` 吸收进内存并落盘，返回 cookie 是否真的变了。

        **每条通道都必须调用**：网易云下发新 cookie 是发生在响应头上，而不同
        通道走的是不同的响应对象。原先只有 weapi 的 ``_post`` 做了这件事，于是
        登录 / 续期一旦落到 eapi、linuxapi 通道（本环境 weapi 常被网关拦成空响应，
        恰恰经常落到这两条），``_save_session()`` 就只是把**旧 cookie 又写了一遍**，
        表现为「重登了、日志说成功，但 cookie 没换、照样 301/405」。
        """
        changed = False
        for key, value in resp.cookies.items():
            if value and self._cookies.get(key) != value:
                self._cookies[key] = value
                changed = True
        if changed:
            self._save_session()
        return changed

    @property
    def cookie_fingerprint(self) -> str:
        """``MUSIC_U`` 的短指纹，日志里用它判断「重登后 cookie 到底换没换」。"""
        raw = self._cookies.get("MUSIC_U", "")
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8] if raw else "-"

    # ------------------------------------------------------------ weapi

    async def _post(
        self, path: str, payload: dict[str, Any], *, capture_cookies: bool = True
    ) -> dict[str, Any]:
        csrf = self._cookies.get("__csrf", "")
        body = dict(payload)
        body["csrf_token"] = csrf
        url = f"{self.BASE}/weapi{path}?csrf_token={csrf}"
        headers = {
            "User-Agent": _UA,
            "Referer": self.BASE,
            "Origin": self.BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            resp = await client.post(url, data=weapi_encrypt(body), cookies=self._cookies)
        if capture_cookies:
            self._capture_cookies(resp)
        if not resp.content:
            # 该环境下 weapi 常被网关拦成 200 空响应
            raise NeteaseError(-1, "weapi 返回空响应（通道被拦截）")
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise NeteaseError(-1, f"响应不是 JSON: {resp.text[:200]}") from exc

    async def _post_checked(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = await self._post(path, payload)
        code = data.get("code", 200)
        if code != 200:
            raise NeteaseError(code, str(data.get("message") or data.get("msg") or data))
        return data

    # ------------------------------------------------------------ api 明文

    async def _api_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "User-Agent": _UA,
            "Referer": self.BASE,
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            resp = await client.get(
                f"{self.BASE}/api{path}", params=params, cookies=self._cookies
            )
        self._capture_cookies(resp)
        resp.raise_for_status()
        return resp.json()

    async def _api_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "User-Agent": _UA,
            "Referer": self.BASE,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            resp = await client.post(
                f"{self.BASE}/api{path}", data=payload, cookies=self._cookies
            )
        self._capture_cookies(resp)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------ eapi

    async def _eapi_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """path 形如 ``/playlist/desc/update``。"""
        headers = {
            "User-Agent": _MOBILE_UA,
            "Referer": self.BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            resp = await client.post(
                f"{self.EAPI_BASE}/eapi{path}",
                data=eapi_encrypt(f"/api{path}", payload),
                cookies=self._cookies_for("pc"),
            )
        self._capture_cookies(resp)
        return _decode_maybe_eapi(resp.content)

    # ------------------------------------------------------------ linuxapi

    async def _linux_post(
        self, path: str, payload: dict[str, Any], *, os_name: str = "linux"
    ) -> dict[str, Any]:
        """通过 linux 客户端转发接口调用 ``/api{path}``。

        实测（2026-08）：desc/update 用 os=linux 会被拒（301 系统错误），
        改用 os=pc 能通过认证 —— 故简介写入优先 os=pc，其余维持 os=linux。
        """
        headers = {
            "User-Agent": _LINUX_UA,
            "Referer": self.BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        # 转发的 params 值必须全部是字符串
        params = {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
                  for k, v in payload.items()}
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            resp = await client.post(
                f"{self.BASE}/api/linux/forward",
                data=linuxapi_encrypt(f"{self.BASE}/api{path}", params),
                cookies=self._cookies_for(os_name),
            )
        self._capture_cookies(resp)
        if not resp.content:
            raise NeteaseError(-1, "linuxapi 返回空响应")
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise NeteaseError(-1, f"linuxapi 响应不是 JSON: {resp.text[:200]}") from exc

    # ------------------------------------------------------------ 匿名接口

    async def song_detail(self, song_ids: list[str]) -> list[dict[str, Any]]:
        if not song_ids:
            return []
        data = await self._api_get("/song/detail/", {"ids": json.dumps(song_ids)})
        return data.get("songs", []) or []

    async def search_songs(self, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        data = await self._api_get("/search/get/web", {
            "s": keyword, "type": 1, "offset": 0, "limit": limit, "total": "true",
        })
        if data.get("code") != 200:
            return []
        return (data.get("result") or {}).get("songs", []) or []

    # ------------------------------------------------------------ 登录接口

    async def login_status(self) -> Optional[dict[str, Any]]:
        """返回当前账号 profile，未登录返回 None。

        eapi 通道在本环境可用，能拿到真实昵称；失败再退回"仅有凭证"的占位信息。
        """
        if not self.logged_in:
            return None
        for fetch in (
            lambda: self._eapi_post("/nuser/account/get", {}),
            lambda: self._post("/w/nuser/account/get", {}),
        ):
            try:
                data = await fetch()
            except Exception:
                continue
            profile = data.get("profile")
            if isinstance(profile, dict) and profile.get("userId"):
                return profile
        return {"nickname": "已提供登录凭证（状态未实时校验）", "userId": 0}

    async def session_valid(self) -> bool:
        """真实校验登录态是否有效（cookie 存在 ≠ 未过期）。

        ``logged_in`` 只检查 MUSIC_U 是否存在；网易云 cookie 会过期/被风控，
        失效后 linuxapi 等通道统一返回 code=301「需要登录」。
        """
        try:
            profile = await self.login_status()
        except Exception:
            return False
        return bool(profile and profile.get("userId"))

    async def user_id(self) -> Optional[int]:
        profile = await self.login_status()
        if profile and profile.get("userId"):
            return int(profile["userId"])
        return None

    # ------------------------------------------------------------ 登录态维护 / 自动重登

    def _cfg(self):
        """现取「自动重登」配置（回调形式，便于配置热更新）；未接入时返回 None。"""
        if self._relogin_cfg is None:
            return None
        try:
            return self._relogin_cfg()
        except Exception:
            return None

    @staticmethod
    def _password_md5(cfg) -> str:
        """取密码的 md5：优先用配置里的 password_md5，否则把明文 md5 一下。"""
        given = (getattr(cfg, "password_md5", "") or "").strip().lower()
        if len(given) == 32 and all(c in "0123456789abcdef" for c in given):
            return given
        raw = getattr(cfg, "password", "") or ""
        return hashlib.md5(raw.encode("utf-8")).hexdigest() if raw else ""

    @staticmethod
    def _is_auth_error(text: str) -> bool:
        """失败原因是否属于「登录态失效」（网易云失效后统一返回 301）。"""
        t = text or ""
        return ("code=301" in t) or ("需要登录" in t) or ("未登录" in t)

    @staticmethod
    def _is_rate_limit(text: str) -> bool:
        """失败原因是否命中频控（``code=405``「操作频繁」）。

        注意：**eapi / api 的 ``desc/update`` 回 405 是常态**（这两条通道本来
        就写不进简介，见模块头注释），所以不能一看到 405 就当成「真被频控」；
        配合 ``_is_linux_channel_error`` 一起用：只有 linuxapi 系也回 405 才算。
        """
        t = text or ""
        return ("code=405" in t) or ("操作频繁" in t) or ("操作过于频繁" in t)

    @staticmethod
    def _is_linux_channel_error(text: str) -> bool:
        """失败条目是否来自 linuxapi 通道（``linuxapi`` / ``linuxapi(pc)``）。"""
        return (text or "").startswith("linuxapi")

    @staticmethod
    def _extract_risk_url(data: Any) -> str:
        """从登录响应里挖出风控「人工验证」跳转链接。

        code=8810 时网易云会随响应下发，形如::

            {"code": 8810, "message": "您当前的网络环境存在安全风险",
             "redirectUrl": "https://y.music.163.com/g/yida/<token>"}

        不同通道 / 版本字段名可能变（``redirectUrl`` / ``redirect_url``），甚至
        藏在子字典里，所以既查已知键名，也兜底扫任何指向 ``/g/yida/`` 的 http
        链接。挖不到时返回空串。
        """
        found = ""
        stack: list[Any] = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for key, val in cur.items():
                    if isinstance(val, str):
                        if key.lower() in ("redirecturl", "redirect_url") and val.startswith("http"):
                            return val
                        if not found and val.startswith("http") and "/yida/" in val:
                            found = val
                    elif isinstance(val, (dict, list)):
                        stack.append(val)
            elif isinstance(cur, list):
                stack.extend(cur)
        return found

    async def refresh_token(self) -> bool:
        """用现有 cookie 续期（``/login/token/refresh``），不需要账号密码。"""
        if not self.logged_in:
            return False
        for label, call in (
            ("weapi", lambda: self._post("/login/token/refresh", {})),
            ("eapi", lambda: self._eapi_post("/login/token/refresh", {})),
        ):
            try:
                data = await call()
            except Exception as exc:
                logger.debug(f"[netease] token 续期通道 {label} 失败: {exc}")
                continue
            if data.get("code") == 200:
                self._save_session()
                logger.info(
                    f"[netease] 登录态续期成功（{label}），cookie 指纹 {self.cookie_fingerprint}"
                )
                return True
        return False

    async def login_with_password(self) -> tuple[bool, str]:
        """手机号 + 密码重新登录；未配置手机号/密码时返回失败与原因。"""
        cfg = self._cfg()
        phone = (getattr(cfg, "phone", "") or "").strip() if cfg else ""
        pw_md5 = self._password_md5(cfg) if cfg else ""
        if not phone or not pw_md5:
            return False, "未配置 netease.phone / password（或 password_md5）"
        payload = {
            "phone": phone,
            "countrycode": (getattr(cfg, "countrycode", "") or "86"),
            "password": pw_md5,
            "rememberLogin": "true",
        }
        last = "所有通道都失败"
        # 每次尝试重算：风控链接只反映「本次」是否撞上，避免展示上一次的旧链接。
        self.last_risk_url = ""
        for label, call in (
            ("weapi", lambda: self._post("/login/cellphone", payload)),
            ("eapi", lambda: self._eapi_post("/login/cellphone", payload)),
            ("linuxapi", lambda: self._linux_post("/login/cellphone", payload, os_name="pc")),
        ):
            try:
                data = await call()
            except Exception as exc:
                last = f"{label}: {exc}"
                continue
            code = data.get("code")
            if code == 200:
                self.last_risk_url = ""
                self._save_session()
                logger.info(
                    f"[netease] 账密登录成功（{label}），cookie 指纹 {self.cookie_fingerprint}"
                )
                return True, label
            # 8810「网络环境存在安全风险」会带一个 redirectUrl，指向网易易盾的
            # 人工验证页；留在 self.last_risk_url 里给 WebUI / 日志展示，操作员可
            # 拿它在浏览器过一遍验证再重试。
            risk_url = self._extract_risk_url(data)
            if risk_url:
                self.last_risk_url = risk_url
            last = f"{label}: code={code} {data.get('message') or data.get('msg') or ''}".strip()
            if code in (501, 502):  # 账号不存在 / 密码错误，换通道也一样
                break
        if self.last_risk_url:
            logger.warning(
                f"[netease] 账密登录被风控拦截（{last}）；"
                f"请在浏览器打开以下链接完成人工验证后重试：{self.last_risk_url}"
            )
        return False, last

    async def ensure_logged_in(
        self,
        *,
        force: bool = False,
        ignore_cooldown: bool = False,
        fresh: bool = False,
    ) -> bool:
        """确保登录态可用：失效时先续期，仍不行再用账密重登。返回是否已恢复。

        - ``force=False``：cookie 在且真实有效就直接返回，不做多余请求；
        - ``force=True``：跳过乐观判断，直接走「续期 → 账密」流程；
        - ``fresh=True``：**跳过续期、直接用账密换一套全新 cookie**。
          续期（``/login/token/refresh``）只是给手上这套 cookie 原地续命，
          内容往往一个字节都不变——被风控标记的是「这套 cookie」，续期救不了，
          必须账密重登拿新的。没配账密时退回续期兜一下，至少别什么都不做。
        - ``ignore_cooldown=True``：忽略冷却（网页端手动点「重新登录」时用）；
        - ``relogin_cooldown`` 秒内的重复尝试会被跳过（防风控期疯狂重试）。
        """
        cfg = self._cfg()
        auto = bool(getattr(cfg, "auto_relogin", True)) if cfg is not None else False
        if not auto and not ignore_cooldown:
            return self.logged_in
        if not force and self.logged_in and await self.session_valid():
            return True
        cooldown = int(getattr(cfg, "relogin_cooldown", 300) or 0)
        now = time.monotonic()
        if cooldown > 0 and not ignore_cooldown and now - self._last_relogin < cooldown:
            logger.info("[netease] 自动重登处于冷却期，本次跳过")
            return False
        self._last_relogin = now
        if fresh:
            ok, note = await self.login_with_password()
            if ok and await self.session_valid():
                return True
            logger.info(f"[netease] 账密重登未成功（{note}），改试 cookie 续期兜底")
            if await self.refresh_token() and await self.session_valid():
                logger.info(
                    "[netease] 续期成功，但注意：续期通常**不下发新 cookie**，"
                    f"指纹仍为 {self.cookie_fingerprint}"
                )
                return True
            logger.warning(f"[netease] 强制换 cookie 重登未成功：{note}")
            return False
        if await self.refresh_token() and await self.session_valid():
            return True
        ok, note = await self.login_with_password()
        if ok and await self.session_valid():
            return True
        logger.warning(f"[netease] 自动重新登录未成功：{note}")
        return False

    # ------------------------------------------------------------ 歌单接口

    async def create_playlist(self, name: str, privacy: bool = False) -> int:
        if not self.logged_in:
            raise NeteaseError(-2, "网易云未登录，请先执行 /music cookie <MUSIC_U>")
        name = (name or "").strip()
        if not name:
            # 空名字网易云会建成默认的「用户xxx的歌单」，歌单名就"静默丢失"了。
            # 与其建出一个名字不对的歌单，不如直接失败并把原因交给调用方。
            raise NeteaseError(-1, "歌单名为空，已阻止创建（请检查「歌单名模板」配置）")
        payload = {"name": name[:40], "privacy": 10 if privacy else 0, "type": "NORMAL"}

        async def _try_once() -> tuple[Optional[int], str]:
            """跑一遍所有通道；返回 (歌单 id 或 None, 失败原因)。"""
            last = "所有通道都失败"
            for label, call in (
                ("linuxapi", lambda: self._linux_post("/playlist/create", payload)),
                ("api", lambda: self._api_post("/playlist/create", payload)),
                ("weapi", lambda: self._post_checked("/playlist/create", payload)),
            ):
                try:
                    data = await call()
                except Exception as exc:
                    last = f"{label}: {exc}"
                    continue
                pid = data.get("id") or (data.get("playlist") or {}).get("id")
                if pid:
                    logger.debug(f"[netease] 建歌单成功（{label}）id={pid}")
                    return int(pid), ""
                last = f"{label}: code={data.get('code')} {data.get('message') or data}".strip()
            return None, last

        pid, last_error = await _try_once()
        if pid is None and self._is_auth_error(last_error):
            # 登录态失效（301）：自动续期/重登后整体重试一次
            if await self.ensure_logged_in(force=True):
                logger.info("[netease] 已自动重新登录，重试建歌单")
                pid, last_error = await _try_once()
        if pid is None:
            raise NeteaseError(-1, f"创建歌单失败 -> {last_error}")
        await self._ensure_playlist_name(pid, name[:40])
        return pid

    async def _ensure_playlist_name(self, playlist_id: int, name: str) -> None:
        """创建后核对歌单名：网易云偶发忽略 name（建成「用户xxx的歌单」），此时补一次改名。

        只做补救、不影响建歌单成败：读不回或改名失败都只记日志。
        """
        try:
            actual = await self.playlist_name(playlist_id)
        except Exception:
            return
        if not actual or actual == name:
            return
        logger.warning(
            f"[netease] 歌单名未按预期生效（实际 {actual!r}），尝试改名 -> {name!r} playlist={playlist_id}"
        )
        await self.rename_playlist(playlist_id, name)

    async def playlist_name(self, playlist_id: int) -> str:
        """读回歌单名（创建 / 改名的写后校验用）。"""
        detail = await self.playlist_detail(int(playlist_id))
        return str((detail or {}).get("name") or "")

    async def rename_playlist(self, playlist_id: int, name: str) -> tuple[bool, str]:
        """改歌单名（``/playlist/update/name``），返回 ``(是否成功, 说明)``。

        逐通道降级并写后读回校验——该接口在部分环境会返回 200 却并不生效，
        所以必须回读确认真改了，否则就当这次通道失败、换下一个通道。
        """
        name = (name or "").strip()
        if not name:
            return False, "歌单名为空，跳过改名"
        if not self.logged_in:
            return False, "网易云未登录"
        pid = str(playlist_id)
        short = name[:40]
        attempts = [
            ("linuxapi", lambda: self._linux_post(
                "/playlist/update/name", {"id": pid, "name": short})),
            ("api", lambda: self._api_post(
                "/playlist/update/name", {"id": pid, "name": short})),
            ("weapi", lambda: self._post(
                "/playlist/update/name", {"id": pid, "name": short})),
            ("eapi", lambda: self._eapi_post(
                "/playlist/update/name", {"id": pid, "name": short})),
        ]
        errors: list[str] = []
        for i, (label, call) in enumerate(attempts):
            if i:
                # 紧挨着连发容易触发 406「操作频繁」，错开一秒
                await asyncio.sleep(CHANNEL_GAP_SECONDS)
            try:
                data = await call()
            except Exception as exc:
                errors.append(f"{label}:{exc}")
                continue
            code = data.get("code", 200)
            if code != 200:
                errors.append(f"{label}:code={code} {data.get('message') or data.get('msg') or ''}".strip())
                continue
            try:
                actual = await self.playlist_name(int(playlist_id))
            except Exception:
                actual = ""
            if actual == short:
                logger.info(f"[netease] 歌单改名成功（{label}）playlist={playlist_id}")
                return True, label
            errors.append(f"{label}:接口返回 200 但读回不一致")
        logger.warning(f"[netease] 歌单改名失败 playlist={playlist_id} -> {' | '.join(errors[:4])}")
        return False, " | ".join(errors[:4]) or "改名失败"

    async def add_tracks(self, playlist_id: int, track_ids: list[str]) -> dict[str, Any]:
        if not track_ids:
            return {"code": 200}
        payload = {
            "op": "add",
            "pid": str(playlist_id),
            "trackIds": json.dumps([str(t) for t in track_ids], separators=(",", ":")),
            "imme": "true",
        }
        last_error = "所有通道都失败"
        for label, call in (
            ("linuxapi", lambda: self._linux_post("/playlist/manipulate/tracks", payload)),
            ("api", lambda: self._api_post("/playlist/manipulate/tracks", payload)),
            ("weapi", lambda: self._post("/playlist/manipulate/tracks", payload)),
        ):
            try:
                data = await call()
            except Exception as exc:
                last_error = f"{label}: {exc}"
                continue
            code = data.get("code")
            # 502 = 歌单内歌曲重复，视为成功
            if code in (200, 502):
                return data
            last_error = f"{label}: code={code} {data.get('message') or data.get('msg') or ''}"
        raise NeteaseError(-1, f"加歌失败 -> {last_error}")

    async def remove_tracks(self, playlist_id: int, track_ids: list[str]) -> dict[str, Any]:
        """从歌单移除歌曲（``op=del``）。用于「同步到歌单」时删掉已不在窗口里的歌。

        与 ``add_tracks`` 共用同一套通道降级。返回网易云原始响应；业务层据此判断成功。
        """
        if not track_ids:
            return {"code": 200}
        if not self.logged_in:
            raise NeteaseError(-2, "网易云未登录，请先执行 /music cookie <MUSIC_U>")
        payload = {
            "op": "del",
            "pid": str(playlist_id),
            "trackIds": json.dumps([str(t) for t in track_ids], separators=(",", ":")),
            "imme": "true",
        }
        last_error = "所有通道都失败"
        for label, call in (
            ("linuxapi", lambda: self._linux_post("/playlist/manipulate/tracks", payload)),
            ("api", lambda: self._api_post("/playlist/manipulate/tracks", payload)),
            ("weapi", lambda: self._post("/playlist/manipulate/tracks", payload)),
        ):
            try:
                data = await call()
            except Exception as exc:
                last_error = f"{label}: {exc}"
                continue
            code = data.get("code")
            # 502 = 歌单内歌曲重复 / 已不存在，视为成功
            if code in (200, 502):
                return data
            last_error = f"{label}: code={code} {data.get('message') or data.get('msg') or ''}"
        raise NeteaseError(-1, f"删歌失败 -> {last_error}")

    async def playlist_detail(self, playlist_id: int) -> dict[str, Any]:
        """读取歌单详情（用于写后校验）。"""
        for path, params in (
            ("/v6/playlist/detail", {"id": playlist_id, "n": 0}),
            ("/playlist/detail", {"id": playlist_id}),
        ):
            try:
                data = await self._api_get(path, params)
            except Exception:
                continue
            playlist = data.get("playlist") or data.get("result")
            if isinstance(playlist, dict) and ("name" in playlist or "description" in playlist):
                return playlist
        return {}

    async def playlist_description(self, playlist_id: int) -> str:
        detail = await self.playlist_detail(playlist_id)
        return (detail.get("description") or "") if detail else ""

    async def playlist_track_ids(self, playlist_id: int) -> list[str]:
        """读取歌单内全部曲目 id（按当前实际顺序）。

        优先用 ``/playlist/track/all``（不受详情接口分页限制）；失败再退回从
        详情里抠 ``trackIds`` / ``tracks``。返回空列表表示拿不到（调用方应跳过重排）。
        """
        try:
            data = await self._api_get("/playlist/track/all", {
                "id": playlist_id, "limit": 1000, "offset": 0,
            })
            songs = data.get("songs") or data.get("trackList") or []
            ids = [str(s["id"]) for s in songs if isinstance(s, dict) and s.get("id")]
            if ids:
                return ids
        except Exception:
            pass
        detail = await self.playlist_detail(playlist_id)
        if not detail:
            return []
        ids: list[str] = []
        for t in detail.get("trackIds") or []:
            if isinstance(t, dict) and t.get("id"):
                ids.append(str(t["id"]))
            elif t:
                ids.append(str(t))
        if not ids:
            for t in detail.get("tracks") or []:
                if isinstance(t, dict) and t.get("id"):
                    ids.append(str(t["id"]))
        return [i for i in ids if i]

    async def reorder_tracks(self, playlist_id: int, ordered_ids: list[str]) -> dict[str, Any]:
        """按给定顺序重排歌单曲目（网易云 ``/song/order/update``）。

        ``ordered_ids`` 必须是歌单当前曲目的完整 id 列表（顺序即期望顺序），
        多/少/错任意一个都会报错，因此调用方需传入「当前实际曲目」重排后的完整列表。
        """
        ordered = [str(i) for i in ordered_ids]
        if not ordered:
            return {"code": 200}
        if not self.logged_in:
            raise NeteaseError(-2, "网易云未登录，请先执行 /music cookie <MUSIC_U>")
        payload = {
            "pid": str(playlist_id),
            "ids": json.dumps(ordered, separators=(",", ":")),
        }
        last_error = "所有通道都失败"
        for label, call in (
            ("linuxapi", lambda: self._linux_post("/song/order/update", payload)),
            ("api", lambda: self._api_post("/song/order/update", payload)),
            ("weapi", lambda: self._post_checked("/song/order/update", payload)),
        ):
            try:
                data = await call()
            except Exception as exc:
                last_error = f"{label}: {exc}"
                continue
            code = data.get("code")
            if code in (200,):
                return data
            last_error = f"{label}: code={code} {data.get('message') or data.get('msg') or ''}"
        raise NeteaseError(-1, f"歌单重排失败 -> {last_error}")

    async def update_description(
        self, playlist_id: int, desc: str, name: str = ""
    ) -> tuple[bool, str]:
        """更新歌单简介，返回 ``(是否成功, 说明)``。

        通道顺序按实测可用性排：linuxapi 是目前唯一能写进去的；eapi / api 的
        ``desc/update`` 会返回 405「操作过于频繁」，weapi 直接空响应。
        写完读回校验，避免"接口返回 200 但其实没写进去"的假成功。
        """
        desc = (desc or "")[:1000]
        if not desc:
            return True, "简介为空，跳过"
        if not self.logged_in:
            return False, "网易云未登录"

        pid = str(playlist_id)
        attempts = [
            # os=linux 的 desc/update 会被拒 301，os=pc 能通过认证（实测 2026-08）
            ("linuxapi(pc)", lambda: self._linux_post(
                "/playlist/desc/update", {"id": pid, "desc": desc}, os_name="pc")),
            ("linuxapi", lambda: self._linux_post(
                "/playlist/desc/update", {"id": pid, "desc": desc})),
            ("linuxapi-batch", lambda: self._linux_post("/batch", {
                "/api/playlist/desc/update": json.dumps(
                    {"id": playlist_id, "desc": desc}, ensure_ascii=False),
            })),
            ("eapi", lambda: self._eapi_post(
                "/playlist/desc/update", {"id": pid, "desc": desc})),
            ("api", lambda: self._api_post(
                "/playlist/desc/update", {"id": pid, "desc": desc})),
            ("weapi", lambda: self._post(
                "/playlist/desc/update", {"id": pid, "desc": desc})),
        ]

        async def _attempt_all() -> tuple[bool, str, list[str]]:
            """跑一遍所有通道；返回 (是否成功, 命中的通道, 失败原因列表)。"""
            errs: list[str] = []
            for i, (label, call) in enumerate(attempts):
                if i:
                    # 各通道紧挨着连发容易触发 406「操作频繁」频控，错开一秒
                    await asyncio.sleep(CHANNEL_GAP_SECONDS)
                try:
                    data = await call()
                except Exception as exc:
                    errs.append(f"{label}:{exc}")
                    continue
                sub = data.get("/api/playlist/desc/update")
                code = (sub or {}).get("code") if isinstance(sub, dict) else data.get("code")
                if code != 200:
                    message = ""
                    if isinstance(sub, dict):
                        message = str(sub.get("message") or sub.get("msg") or "")
                    message = message or str(data.get("message") or data.get("msg") or "")
                    errs.append(f"{label}:code={code} {message}".strip())
                    continue
                # 写后读回校验：必须和刚写的内容对得上，避免"返回 200 其实没写进去"
                try:
                    current = (await self.playlist_description(playlist_id)).strip()
                except Exception:
                    current = ""
                if current and (current == desc.strip() or current[:40] == desc.strip()[:40]):
                    logger.info(f"[netease] 简介写入成功（{label}）playlist={playlist_id}")
                    return True, label, errs
                errs.append(f"{label}:接口返回 200 但读回不一致")
            return False, "", errs

        ok, note, errors = await _attempt_all()
        if ok:
            return True, note

        # ---- 失败归因：是「登录态没了」还是「被频控了」----
        # ① 301 / 需要登录 = cookie 已失效，续期 / 账密重登能救。
        auth_failed = any(self._is_auth_error(e) for e in errors)
        # ② 405「操作频繁」= 写接口的**账号级频控**，换 cookie 治不了。
        #    2026-10-10 在线上实测确认：
        #      · 同一个 cookie 在 16:23:35 刚用 linuxapi 写成功另一个歌单，
        #        4 秒后写第二个就 405 —— 与登录态无关；
        #      · eapi 续期返回 code=200 但**不下发新 cookie**（指纹前后一致）；
        #      · 服务器 IP 的账密登录被网易云判「网络环境存在安全风险」
        #        （code=8810）直接拒绝。
        #    所以 405 只如实报错、**不重登**，把「等一会儿」交给上层：
        #    归档器见到频控就不再原地重试（越试越频繁），直接入队等 job_descfix 补写。
        #    注意 eapi / api 的 desc/update 回 405 是常态（这俩通道本来就写不进简介），
        #    不足为据，只有 linuxapi 系也回 405 才算「真被频控」。
        def _rate_blocked(errs: list[str]) -> bool:
            return any(
                self._is_linux_channel_error(e) and self._is_rate_limit(e) for e in errs
            )

        rate_blocked = _rate_blocked(errors)

        if auth_failed:
            logger.info(
                f"[netease] 简介写入判定为登录态失效，尝试重新登录后重试"
                f"（cookie 指纹 {self.cookie_fingerprint}）"
            )
            if await self.ensure_logged_in(force=True):
                logger.info(
                    f"[netease] 已自动重新登录，重试写简介"
                    f"（cookie 指纹 {self.cookie_fingerprint}）"
                )
                ok2, note2, errors2 = await _attempt_all()
                if ok2:
                    return True, f"{note2}（自动重登后）"
                errors = errors2
                rate_blocked = _rate_blocked(errors)
            else:
                try:
                    valid = await self.session_valid()
                except Exception:
                    valid = False
                if not valid:
                    return (
                        False,
                        "网易云登录态已失效（MUSIC_U 过期或被风控），"
                        "自动重登未成功（可在配置页填 netease.phone/password 开启账密重登），"
                        "也可私聊机器人执行 /music cookie <MUSIC_U>",
                    )

        # 顺带把名字补一次（改名通道和简介不同，不影响成败判定）
        if name:
            try:
                await self._eapi_post("/playlist/update/name", {"id": pid, "name": name[:40]})
            except Exception:
                pass
        reason = " | ".join(errors[:4]) or "未知原因"
        if rate_blocked:
            # 保留原始 code=405 串，方便上层（归档器 / 补写队列）识别为频控
            reason += "（网易云写接口频控：换 cookie 也无效，需隔一段时间再写，已入队等自动补写）"
        logger.warning(f"[netease] 简介写入失败 playlist={playlist_id} -> {reason}")
        return False, reason

    async def delete_playlist(self, playlist_id: int) -> bool:
        pid = str(playlist_id)
        for call in (
            lambda: self._linux_post("/playlist/delete", {"pid": pid, "id": pid}),
            lambda: self._api_post("/playlist/delete", {"pid": pid, "id": pid}),
        ):
            try:
                data = await call()
            except Exception:
                continue
            if data.get("code") == 200:
                return True
        return False

    @staticmethod
    def playlist_url(playlist_id: int) -> str:
        return f"https://music.163.com/#/playlist?id={playlist_id}"
