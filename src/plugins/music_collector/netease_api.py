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

    def __init__(self, session_path: Path) -> None:
        self.session_path = Path(session_path)
        self._cookies: dict[str, str] = {
            "os": "pc",
            "appver": "8.9.75",
            "osver": "Microsoft-Windows-10",
            "deviceId": "".join(random.choice(_BASE62) for _ in range(32)),
        }
        self._lock = asyncio.Lock()
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
            changed = False
            for key, value in resp.cookies.items():
                if value and self._cookies.get(key) != value:
                    self._cookies[key] = value
                    changed = True
            if changed:
                self._save_session()
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
        return _decode_maybe_eapi(resp.content)

    # ------------------------------------------------------------ linuxapi

    async def _linux_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """通过 linux 客户端转发接口调用 ``/api{path}``。

        这是当前环境下唯一能改歌单简介的通道。
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
                cookies=self._cookies_for("linux"),
            )
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

    async def user_id(self) -> Optional[int]:
        profile = await self.login_status()
        if profile and profile.get("userId"):
            return int(profile["userId"])
        return None

    # ------------------------------------------------------------ 歌单接口

    async def create_playlist(self, name: str, privacy: bool = False) -> int:
        if not self.logged_in:
            raise NeteaseError(-2, "网易云未登录，请先执行 /music cookie <MUSIC_U>")
        payload = {"name": name[:40], "privacy": 10 if privacy else 0, "type": "NORMAL"}
        last_error = "所有通道都失败"
        for label, call in (
            ("linuxapi", lambda: self._linux_post("/playlist/create", payload)),
            ("api", lambda: self._api_post("/playlist/create", payload)),
            ("weapi", lambda: self._post_checked("/playlist/create", payload)),
        ):
            try:
                data = await call()
            except Exception as exc:
                last_error = f"{label}: {exc}"
                continue
            pid = data.get("id") or (data.get("playlist") or {}).get("id")
            if pid:
                logger.debug(f"[netease] 建歌单成功（{label}）id={pid}")
                return int(pid)
            last_error = f"{label}: {data.get('message') or data}"
        raise NeteaseError(-1, f"创建歌单失败 -> {last_error}")

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

        errors: list[str] = []
        for label, call in attempts:
            try:
                data = await call()
            except Exception as exc:
                errors.append(f"{label}:{exc}")
                continue
            sub = data.get("/api/playlist/desc/update")
            code = (sub or {}).get("code") if isinstance(sub, dict) else data.get("code")
            if code != 200:
                message = ""
                if isinstance(sub, dict):
                    message = str(sub.get("message") or sub.get("msg") or "")
                message = message or str(data.get("message") or data.get("msg") or "")
                errors.append(f"{label}:code={code} {message}".strip())
                continue
            # 写后读回校验：必须和刚写的内容对得上，避免"返回 200 其实没写进去"
            try:
                current = (await self.playlist_description(playlist_id)).strip()
            except Exception:
                current = ""
            if current and (current == desc.strip() or current[:40] == desc.strip()[:40]):
                logger.info(f"[netease] 简介写入成功（{label}）playlist={playlist_id}")
                return True, label
            errors.append(f"{label}:接口返回 200 但读回不一致")

        # 顺带把名字补一次（改名通道和简介不同，不影响成败判定）
        if name:
            try:
                await self._eapi_post("/playlist/update/name", {"id": pid, "name": name[:40]})
            except Exception:
                pass
        reason = " | ".join(errors[:4]) or "未知原因"
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
