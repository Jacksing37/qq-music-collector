"""网易云音乐 API 客户端（weapi 加密协议，纯自实现，无第三方 SDK 依赖）。

覆盖本项目需要的能力：
- 匿名：歌曲详情、搜索（走 /api 旧接口，无需 weapi 加密）
- 登录：手动 Cookie 登录、创建歌单、批量加歌、改歌单简介（走 weapi，需登录）
Cookie 会持久化到 data/netease_session.json，重启后免登录。

注意：weapi 接口在某些网络环境下可能被风控返回空响应。若出现这种情况，
请改用 /music export 导出歌单文本手动创建。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import random
import string
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

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
_BASE62 = string.ascii_letters + string.digits

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

class NeteaseError(RuntimeError):
    """网易云接口返回了非 200 的业务码。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"网易云接口错误 code={code}: {message}")
        self.code = code
        self.message = message


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


class NeteaseAPI:
    BASE = "https://music.163.com"

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

    # ------------------------------------------------------------ request

    async def _post(
        self, path: str, payload: dict[str, Any], *, capture_cookies: bool = False
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
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise NeteaseError(-1, f"响应不是 JSON: {resp.text[:200]}") from exc
        return data

    async def _post_checked(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = await self._post(path, payload)
        code = data.get("code", 200)
        if code != 200:
            raise NeteaseError(code, str(data.get("message") or data.get("msg") or data))
        return data

    # ------------------------------------------------------------ 匿名接口

    # 匿名接口优先走 /api 下的旧接口（无需 weapi 加密，当前环境可用性更高）

    async def _api_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "User-Agent": _UA,
            "Referer": self.BASE,
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            resp = await client.get(f"{self.BASE}/api{path}", params=params)
        resp.raise_for_status()
        return resp.json()

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

        weapi 登录状态接口被风控时，只要本地有 MUSIC_U 就视为"已提供凭证"。
        """
        if not self.logged_in:
            return None
        try:
            data = await self._post("/w/nuser/account/get", {})
            profile = data.get("profile")
            if isinstance(profile, dict):
                return profile
        except Exception:
            pass
        return {"nickname": "已提供登录凭证（状态未实时校验）", "userId": 0}

    # ------------------------------------------------------------ 歌单接口

    async def _api_post(
        self, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """尝试走 /api 下的旧 POST 接口（部分环境比 weapi 更稳）。"""
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

    async def create_playlist(self, name: str, privacy: bool = False) -> int:
        if not self.logged_in:
            raise NeteaseError(-2, "网易云未登录，请先执行 /music cookie <MUSIC_U>")
        payload = {"name": name[:40], "privacy": 10 if privacy else 0, "type": "NORMAL"}
        data: dict[str, Any]
        try:
            # 优先尝试不需要 weapi 加密的旧接口
            data = await self._api_post("/playlist/create", payload)
        except Exception:
            data = await self._post_checked("/playlist/create", payload)
        pid = data.get("id") or (data.get("playlist") or {}).get("id")
        if not pid:
            raise NeteaseError(-1, f"创建歌单未返回 id: {data}")
        return int(pid)

    async def add_tracks(self, playlist_id: int, track_ids: list[str]) -> dict[str, Any]:
        if not track_ids:
            return {"code": 200}
        payload = {
            "op": "add",
            "pid": str(playlist_id),
            "trackIds": json.dumps([str(t) for t in track_ids], separators=(",", ":")),
            "imme": "true",
        }
        data: dict[str, Any]
        try:
            data = await self._api_post("/playlist/manipulate/tracks", payload)
        except Exception:
            data = await self._post("/playlist/manipulate/tracks", payload)
        code = data.get("code")
        # 502 = 歌单内歌曲重复，视为成功
        if code not in (200, 502):
            raise NeteaseError(int(code or -1), str(data.get("message") or data))
        return data

    async def update_description(self, playlist_id: int, desc: str) -> None:
        payload = {"id": str(playlist_id), "desc": desc[:1000]}
        try:
            await self._api_post("/playlist/desc/update", payload)
        except Exception:
            try:
                await self._post("/playlist/desc/update", payload)
            except Exception:
                pass

    @staticmethod
    def playlist_url(playlist_id: int) -> str:
        return f"https://music.163.com/#/playlist?id={playlist_id}"
