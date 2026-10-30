"""白名单 QQ 号登录：机器人**私聊**发一次性验证码。

为什么不用 QQ 互联 OAuth：官方接口只返回 ``openid``（应用内唯一、与 QQ 号没有可
反推关系），拿不到 QQ 号，跟「按 QQ 号维护白名单 + 从群成员批量导入」的设计对不上；
而且网站应用要备案域名 + 大陆服务器 + 实名审核。机器人本来就在群里，事件里自带
真实 sender QQ 号，让玩家收一条私聊码是成本最低、也足够硬的身份证明。

代价：玩家必须先把机器人加为好友，否则 ``send_private_msg`` 发不出去。
发不出去时这里返回一句明确的话，而不是让他对着"验证码已发送"干等。
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from .store import GameStore

#: 同一 QQ 两次要码的最小间隔（秒），避免拿机器人当短信轰炸机
CODE_COOLDOWN = 30.0


def _pick_bot():
    """当前在线的任意一个 bot 实例；没连上返回 None。"""
    try:
        import nonebot

        bots = nonebot.get_bots()
    except Exception:  # 离线测试 / 未初始化
        return None
    return next(iter(bots.values()), None)


def _cfg_get(cfg, name: str, default):
    return getattr(cfg, name, default)


class GameAuth:
    def __init__(self, store: GameStore, cfg_fn: Callable[[], object]) -> None:
        self.store = store
        self._cfg = cfg_fn
        self._last_code_at: dict[int, float] = {}

    # ------------------------------------------------------------ 白名单

    async def check_allowed(self, qq: int) -> tuple[bool, str]:
        """这个 QQ 能不能玩。返回 ``(是否允许, 拒绝原因)``。"""
        cfg = self._cfg()
        if not _cfg_get(cfg, "whitelist_only", True):
            return True, ""
        user = await self.store.get_user(qq)
        if user is None:
            return False, "你不在猜歌名单里，请找管理员把你的 QQ 号加进去"
        if not user.get("enabled"):
            return False, "你的猜歌资格已被停用，请找管理员"
        return True, ""

    # ------------------------------------------------------------ 发码

    async def request_code(self, qq: int) -> tuple[bool, str]:
        ok, reason = await self.check_allowed(qq)
        if not ok:
            return False, reason
        now = time.time()
        last = self._last_code_at.get(qq, 0.0)
        if now - last < CODE_COOLDOWN:
            wait = int(CODE_COOLDOWN - (now - last)) + 1
            return False, f"刚发过一条，{wait} 秒后再试"
        cfg = self._cfg()
        ttl = int(_cfg_get(cfg, "code_ttl_minutes", 10)) * 60
        code = await self.store.new_code(qq, ttl)
        bot = _pick_bot()
        if bot is None:
            return False, "机器人当前不在线，没法发私聊验证码，请稍后再试"
        text = (
            f"猜歌游戏登录验证码：{code}\n"
            f"{ttl // 60} 分钟内有效，只能使用一次。\n"
            "（不是你本人操作的请忽略这条消息）"
        )
        group_hint = int((await self.store.get_user(qq) or {}).get("source_group") or 0)
        sent, err = await self._send_dm(bot, qq, text, group_hint)
        if not sent:
            return False, (
                "私聊验证码发不出去：机器人还不是你好友，且群临时会话也没走通"
                "（需要群主在群设置里允许「发起临时会话」）。"
                f"最稳的办法是先加机器人为好友再试（错误：{err}）"
            )
        self._last_code_at[qq] = now
        return True, "验证码已私聊发给你，去 QQ 里看一下"

    async def _send_dm(self, bot, qq: int, text: str, group_id: int = 0) -> tuple[bool, str]:
        """先好友私聊；不行再试**群临时会话**（OneBot 的 ``group_id`` 参数）。

        群里「允许发起临时会话」是这一条能成立的前提——它不是好友关系，所以在
        QQ 里会落在一个单独的「临时会话」窗口，但对验证码来说够用了。
        """
        try:
            await bot.send_private_msg(user_id=qq, message=text)
            return True, ""
        except Exception as exc:  # noqa: BLE001 - 第一条不通才试第二条
            first = f"{type(exc).__name__}"
        if not group_id:
            return False, first
        try:
            await bot.send_private_msg(user_id=qq, group_id=group_id, message=text)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"{first} / 临时会话 {type(exc).__name__}"

    # ------------------------------------------------------------ 验码

    async def login(self, qq: int, code: str) -> tuple[Optional[str], str, str]:
        """验证码换登录态。返回 ``(token, 昵称, 失败原因)``。"""
        ok, reason = await self.check_allowed(qq)
        if not ok:
            return None, "", reason
        passed, why = await self.store.consume_code(qq, code)
        if not passed:
            return None, "", why
        cfg = self._cfg()
        user = await self.store.get_user(qq) or {}
        nick = str(user.get("nick") or "").strip()
        if not nick:
            nick = await self._fetch_nick(qq)
        token = await self.store.new_session(qq, nick, float(_cfg_get(cfg, "session_days", 30)))
        return token, nick, ""

    async def _fetch_nick(self, qq: int) -> str:
        """白名单里没存昵称时，问协议端要一个（拿不到就空着）。"""
        bot = _pick_bot()
        if bot is None:
            return ""
        try:
            info = await bot.get_stranger_info(user_id=qq)
            return str(info.get("nickname") or "")
        except Exception:  # noqa: BLE001 - 昵称只是展示，拿不到不算错
            return ""

    # ------------------------------------------------------------ 登录态

    async def resolve(self, token: str) -> Optional[dict]:
        cfg = self._cfg()
        days = float(_cfg_get(cfg, "session_days", 30))
        session = await self.store.get_session(token, days)
        if session is None:
            return None
        # 登录态还在，但人可能刚被移出名单 —— 以名单为准
        ok, _ = await self.check_allowed(int(session["qq"]))
        if not ok:
            await self.store.drop_session(token)
            return None
        return session

    async def logout(self, token: str) -> None:
        await self.store.drop_session(token)
