"""猜歌小游戏的 HTTP / WebSocket 接口，挂在 NoneBot 的 FastAPI 应用上。

两个命名空间，别混：

- ``/guess`` + ``/api/guess/*`` —— 玩家侧，用**游戏自己的登录态**（QQ 白名单 +
  机器人私聊验证码换来的 token），与管理令牌 ``MUSIC_WEBUI_TOKEN`` 完全无关；
- ``/music-admin/guess`` + ``/api/music-admin/game/*`` —— 管理员侧，仍用管理令牌，
  负责名单导入（含从群成员批量拉取）。

状态推送走 WebSocket，但**动作仍然走 HTTP**：一次动作 = 一个请求 + 一次广播，
比在 WS 上再实现一套消息协议简单得多，断线也不会留下半截状态。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from ..config import config_manager
from ..service import service as collect_service
from . import bridge
from .auth import GameAuth, _pick_bot
from .rooms import PHASE_LOBBY, PHASE_PLAYING, PHASE_SETTLED, PHASE_SETUP, Room, RoomError, RoomManager
from .store import GameStore
from .page import GAME_HTML, ADMIN_HTML

#: 房间巡检间隔（秒）：回收空闲房间 + 清过期验证码
REAP_SECONDS = 60.0

store: Optional[GameStore] = None      # 由 setup() 填充
manager: Optional[RoomManager] = None
auth: Optional[GameAuth] = None
_lock = asyncio.Lock()


def _cfg():
    return config_manager.config.game


def _token_of(request: Request) -> str:
    """玩家登录态：``Authorization: Bearer xxx``，也接受 ``?token=``。"""
    raw = request.headers.get("Authorization", "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return str(request.query_params.get("token") or "")


async def _ident(request: Request) -> dict:
    token = _token_of(request)
    session = await auth.resolve(token) if auth else None
    if session is None:
        raise _unauthorized()
    return session


def _unauthorized():
    from fastapi import HTTPException

    return HTTPException(status_code=401, detail="请先登录")


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _err(message: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=code)


# ------------------------------------------------------------------ 广播


class _Hub:
    """房间号 -> {QQ: [WebSocket, ...]}。一个 QQ 可能开好几个标签页。"""

    def __init__(self) -> None:
        self._conns: dict[str, dict[int, list[WebSocket]]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def add(self, code: str, qq: int, ws: WebSocket) -> None:
        self._conns.setdefault(code, {}).setdefault(qq, []).append(ws)
        self._locks[id(ws)] = asyncio.Lock()

    def remove(self, code: str, qq: int, ws: WebSocket) -> None:
        room = self._conns.get(code)
        if room is None:
            return
        bucket = room.get(qq) or []
        if ws in bucket:
            bucket.remove(ws)
        if not bucket:
            room.pop(qq, None)
        if not room:
            self._conns.pop(code, None)
        self._locks.pop(id(ws), None)

    def peers(self, code: str) -> dict[int, list[WebSocket]]:
        return self._conns.get(code, {})

    async def broadcast(self, room: Room) -> None:
        """把房间快照推给每个在线玩家（各自裁剪过的版本）。"""
        assert manager is not None
        for qq, sockets in list(self.peers(room.code).items()):
            if qq not in room.players:
                continue
            state = manager.state(room, qq)
            for ws in list(sockets):
                lock = self._locks.setdefault(id(ws), asyncio.Lock())
                try:
                    async with lock:
                        await ws.send_json({"type": "state", "state": state})
                except Exception:  # noqa: BLE001 - 对端已断开
                    self.remove(room.code, qq, ws)


hub = _Hub()


# ------------------------------------------------------------------ 玩家侧


async def _api_login_code(request: Request):
    if not _cfg().enabled:
        return _err("猜歌功能未开启", 404)
    body = await _body(request)
    try:
        qq = int(body.get("qq") or 0)
    except (TypeError, ValueError):
        return _err("QQ 号格式不对")
    if qq <= 0:
        return _err("请填写 QQ 号")
    ok, message = await auth.request_code(qq)
    return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 400)


async def _api_login(request: Request):
    if not _cfg().enabled:
        return _err("猜歌功能未开启", 404)
    body = await _body(request)
    try:
        qq = int(body.get("qq") or 0)
    except (TypeError, ValueError):
        return _err("QQ 号格式不对")
    token, nick, reason = await auth.login(qq, str(body.get("code") or ""))
    if token is None:
        return _err(reason or "登录失败")
    return JSONResponse({"ok": True, "token": token, "qq": qq, "nick": nick})


async def _api_logout(request: Request):
    await auth.logout(_token_of(request))
    return JSONResponse({"ok": True})


async def _api_me(request: Request):
    session = await _ident(request)
    return JSONResponse({
        "ok": True,
        "qq": int(session["qq"]),
        "nick": session["nick"],
        "single_allowed": bool(_cfg().allow_single),
        "rooms": manager.count(),
    })


async def _api_windows(request: Request):
    await _ident(request)
    return JSONResponse({"ok": True, "windows": await bridge.list_windows(store)})


async def _api_room_get(request: Request):
    session = await _ident(request)
    code = str(request.query_params.get("code") or "")
    room = manager.find(code)
    if room is None:
        return JSONResponse({"ok": False, "message": "房间不存在或已解散"}, status_code=404)
    qq = int(session["qq"])
    if qq not in room.players:
        return JSONResponse({"ok": False, "message": "你不在这个房间里"}, status_code=403)
    return JSONResponse({"ok": True, "state": manager.state(room, qq)})


async def _api_room_create(request: Request):
    session = await _ident(request)
    body = await _body(request)
    mode = str(body.get("mode") or "multi")
    if mode == "single" and not _cfg().allow_single:
        return _err("单人模式已关闭")
    qq = int(session["qq"])
    nick = str(session.get("nick") or qq)
    async with _lock:
        room, _player = manager.create(qq, nick, mode)
    await hub.broadcast(room)
    return JSONResponse({"ok": True, "code": room.code, "state": manager.state(room, qq)})


async def _api_room_join(request: Request):
    session = await _ident(request)
    body = await _body(request)
    code = str(body.get("code") or "").strip().upper()
    room = manager.find(code)
    if room is None:
        return _err("房间不存在或已解散")
    if room.phase == PHASE_SETTLED and not body.get("force"):
        # 已开牌的房间不再收人：否则新进来的人看得到全部答案
        return _err("这局已经结算结束了，让房主开下一局再进")
    qq = int(session["qq"])
    nick = str(session.get("nick") or qq)
    async with _lock:
        manager.join(room, qq, nick)
    await hub.broadcast(room)
    return JSONResponse({"ok": True, "code": room.code, "state": manager.state(room, qq)})


async def _api_room_action(request: Request):
    session = await _ident(request)
    body = await _body(request)
    qq = int(session["qq"])
    room = manager.find(str(body.get("code") or ""))
    if room is None:
        return _err("房间不存在或已解散", 404)
    if qq not in room.players:
        return _err("你不在这个房间里", 403)
    action = str(body.get("action") or "")
    is_host = room.host_qq == qq

    try:
        async with room.lock:
            message = await _dispatch(room, qq, is_host, action, body)
    except RoomError as exc:
        return _err(str(exc))
    await hub.broadcast(room)
    return JSONResponse({
        "ok": True,
        "message": message,
        "state": manager.state(room, qq),
    })


def _advance_locked(room: Room, idx_before: int) -> str:
    """交完当前这首之后推进：全员答完就翻页，翻到末尾直接结算。

    调用方必须已持有 ``room.lock``；结算只算内存里的答案，**不碰网络**。
    """
    if not room.all_submitted():
        return f"已提交，还有 {room.left_current()} 人没答"
    done = room.advance_if_ready()
    if done:
        room.settle(_cfg().guess_slots, list(_cfg().scores))
        return f"第 {idx_before + 1} 首答完，题目已过完，成绩揭晓"
    return (
        f"第 {idx_before + 1} 首全员答完，"
        f"进入第 {room.cur_idx + 1} 首（共 {len(room.cards)} 首）"
    )


async def _dispatch(room: Room, qq: int, is_host: bool, action: str, body: dict) -> str:
    """所有房间动作的唯一入口。返回一句给玩家看的话。

    注意：这里持锁执行，所以**不能**调用网络（归档另行处理）。
    """
    player = room.players[qq]

    if action == "select_source":
        if not is_host:
            raise RoomError("只有房主能选题库")
        if room.phase == PHASE_PLAYING:
            raise RoomError("已经开始了，先等这局结束")
        kind = str(body.get("kind") or "")
        if kind == "window":
            try:
                gid = int(body.get("group_id") or 0)
            except (TypeError, ValueError):
                raise RoomError("群号不对") from None
            window_key = str(body.get("window_key") or "")
            if not gid or not window_key:
                raise RoomError("请选择群和窗口")
            cards = await bridge.cards_from_window(gid, window_key)
            if not cards:
                raise RoomError("这个窗口里没有歌")
            room.cards = []
            room.uid_seq = 0
            for card in cards:
                room.add_card(card)
            room.source = {"kind": "window", "group_id": gid, "window_key": window_key}
            room.label = str(body.get("label") or window_key)
            room.phase = PHASE_SETUP
            room.result = None
            room.archive = None
            room.cur_idx = 0
            for p in room.players.values():
                p.answers = {}
            return f"题目已切换为「{room.label}」，共 {len(room.cards)} 首"
        if kind == "master":
            try:
                gid = int(body.get("group_id") or 0)
            except (TypeError, ValueError):
                raise RoomError("群号不对") from None
            if not gid:
                raise RoomError("先选群，再抽题")
            cfg = _cfg()
            cap = max(1, int(getattr(cfg, "random_max_count", 20)))
            try:
                want = int(body.get("count") or getattr(cfg, "random_default_count", 5))
            except (TypeError, ValueError):
                raise RoomError("题量不对") from None
            want = max(1, min(want, cap))
            cards = await bridge.cards_from_master(store, gid, want)
            if not cards:
                raise RoomError("这个群的总库里没有能出题的歌（要既有分享者、又有网易云音源）")
            room.cards = []
            room.uid_seq = 0
            for card in cards:
                room.add_card(card)
            room.source = {"kind": "master", "group_id": gid, "count": len(room.cards)}
            room.label = f"总库随机 {len(room.cards)} 首"
            room.phase = PHASE_SETUP
            room.result = None
            room.archive = None
            room.cur_idx = 0
            for p in room.players.values():
                p.answers = {}
            return (
                f"已从群 {gid} 的总库随机抽 {len(room.cards)} 首，"
                "分享者各不相同"
            )
        if kind == "live":
            room.source = {"kind": "live"}
            room.label = "现场收集"
            room.phase = PHASE_SETUP
            room.result = None
            room.archive = None
            room.cur_idx = 0
            return "已切到现场收集，大家把歌曲链接贴进来吧"
        raise RoomError("题库类型不对")

    if action == "add_songs":
        if room.phase != PHASE_SETUP:
            raise RoomError("现在不是收集阶段")
        if not room.source or room.source.get("kind") != "live":
            raise RoomError("只有「现场收集」模式下才能贴歌")
        added, message = await bridge.add_live_songs(
            room, qq, player.nick, str(body.get("text") or "")
        )
        return message if added or message else "没识别到歌曲"

    if action == "remove_song":
        if room.phase == PHASE_PLAYING:
            raise RoomError("作答中不能改题目")
        try:
            uid = int(body.get("uid") or 0)
        except (TypeError, ValueError):
            raise RoomError("题目编号不对") from None
        target = next((c for c in room.cards if c.uid == uid), None)
        if target is None:
            raise RoomError("没有这道题")
        if not is_host and target.sharer_qq != qq:
            raise RoomError("只能删自己贴的歌")
        room.remove_card(uid)
        return "已删除"

    if action == "start":
        if not is_host:
            raise RoomError("只有房主能开始游戏")
        if room.phase not in (PHASE_LOBBY, PHASE_SETUP):
            raise RoomError("现在不能开始")
        if len(room.cards) < 1:
            raise RoomError("还没有题目")
        room.phase = PHASE_PLAYING
        room.result = None
        room.archive = None
        # 开场打乱：卡片顺序与候选池顺序都洗一遍（顺带清空上一轮的作答与分数）
        room.shuffle_order()
        return f"游戏开始，共 {len(room.cards)} 首，一首一首来"

    if action == "submit":
        if room.phase != PHASE_PLAYING:
            raise RoomError("现在不是作答阶段")
        card = room.cur_card()
        if card is None:
            raise RoomError("题目已经答完了")
        # ``picks`` 是现在这一首的三个名字；老的整套 ``answers`` 也认（只取当前这首）
        picks = body.get("picks")
        if picks is None:
            answers = body.get("answers")
            if isinstance(answers, dict):
                picks = answers.get(str(card.uid), answers.get(card.uid, []))
        if not isinstance(picks, list):
            raise RoomError("答案格式不对")
        idx_before = room.cur_idx
        room.submit_song(qq, picks, _cfg().guess_slots)
        return _advance_locked(room, idx_before)

    if action == "skip_song":
        if not is_host:
            raise RoomError("只有房主能跳过这首")
        if room.phase != PHASE_PLAYING:
            raise RoomError("现在不是作答阶段")
        idx_before = room.cur_idx
        if room.skip_current():
            room.settle(_cfg().guess_slots, list(_cfg().scores))
            return "题目已过完，成绩已揭晓"
        return f"房主跳过了第 {idx_before + 1} 首，现在是第 {room.cur_idx + 1} 首"

    if action == "settle":
        if not is_host:
            raise RoomError("只有房主能强制结算")
        if room.phase != PHASE_PLAYING:
            raise RoomError("现在不是作答阶段")
        room.settle(_cfg().guess_slots, list(_cfg().scores))
        return "已按当前作答结算"

    if action == "transfer":
        try:
            target = int(body.get("qq") or 0)
        except (TypeError, ValueError):
            raise RoomError("对方 QQ 不对") from None
        manager.transfer(room, qq, target)
        return f"房主已转给 {room.nick_of(target)}"

    if action == "reset":
        if not is_host:
            raise RoomError("只有房主能开下一局")
        room.reset_round()
        return "已重置，可以开下一局了"

    if action == "leave":
        raise RoomError("请用退出接口")

    raise RoomError(f"未知操作：{action}")


async def _api_room_leave(request: Request):
    session = await _ident(request)
    body = await _body(request)
    qq = int(session["qq"])
    code = str(body.get("code") or "")
    room = manager.find(code)
    if room is None:
        return JSONResponse({"ok": True})
    async with _lock:
        empty = manager.leave(room, qq)
        # 人走了一条，当前这首可能就「全员答完」了，不能干等着
        if not empty and room.phase == PHASE_PLAYING:
            async with room.lock:
                if room.all_submitted():
                    if room.advance_if_ready():
                        room.settle(_cfg().guess_slots, list(_cfg().scores))
    if empty or room.mode == "single":
        manager.drop(room.code)
    else:
        await hub.broadcast(room)
    return JSONResponse({"ok": True})


async def _api_room_archive(request: Request):
    """归档到网易云歌单。网络操作，**不能**放在锁里跑。"""
    session = await _ident(request)
    body = await _body(request)
    qq = int(session["qq"])
    room = manager.find(str(body.get("code") or ""))
    if room is None:
        return _err("房间不存在或已解散", 404)
    if room.host_qq != qq:
        return _err("只有房主能归档", 403)
    if not _cfg().archive_by_host:
        return _err("管理员已关闭房主归档")
    if room.phase != PHASE_SETTLED:
        return _err("结算之后才能归档")
    result = await bridge.archive_room(room)
    await hub.broadcast(room)
    return JSONResponse({"ok": True, "archive": result, "state": manager.state(room, qq)})


async def _ws(ws: WebSocket):
    """房间实时通道：连上先推一次快照，之后由动作广播。

    心跳只用来探活，**不更新房间的 last_active**——否则房间永远不会被回收。
    """
    code = str(ws.query_params.get("code") or "")
    token = str(ws.query_params.get("token") or "")
    session = await auth.resolve(token) if auth else None
    room = manager.find(code) if manager else None
    if session is None or room is None:
        await ws.close(code=4401)
        return
    qq = int(session["qq"])
    if qq not in room.players:
        await ws.close(code=4403)
        return
    await ws.accept()
    player = room.players[qq]
    player.online = True
    hub.add(room.code, qq, ws)
    try:
        await ws.send_json({"type": "state", "state": manager.state(room, qq)})
        await hub.broadcast(room)
        while True:
            raw = await ws.receive_text()
            if raw == "ping":
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        hub.remove(room.code, qq, ws)
        if not hub.peers(room.code):
            player.online = False
        if manager.find(room.code) is not None:
            await hub.broadcast(room)


# ------------------------------------------------------------------ 管理侧


async def _api_admin_users(request: Request):
    from ..webui import _token_ok

    if not _token_ok(request):
        return _err("unauthorized", 401)
    keyword = str(request.query_params.get("q") or "")
    users = await store.list_users(keyword)
    groups = await collect_service.store.distinct_group_ids()
    return JSONResponse({
        "ok": True,
        "users": users,
        "groups": groups,
        "count": len(users),
    })


async def _api_admin_users_post(request: Request):
    from ..webui import _token_ok

    if not _token_ok(request):
        return _err("unauthorized", 401)
    body = await _body(request)
    action = str(body.get("action") or "list")

    if action == "import_text":
        rows = _parse_user_text(str(body.get("text") or ""))
        if not rows:
            return _err("没解析出有效的 QQ 号")
        added = await store.add_users([(qq, nick, 0) for qq, nick in rows])
        return JSONResponse({
            "ok": True,
            "message": f"共 {len(rows)} 个，新增 {added} 个，其余已存在",
        })

    if action == "import_group":
        try:
            gid = int(body.get("group_id") or 0)
        except (TypeError, ValueError):
            return _err("群号不对")
        if not gid:
            return _err("请选择群")
        bot = _pick_bot()
        if bot is None:
            return _err("机器人不在线，拉不到群成员")
        try:
            members = await bot.get_group_member_list(group_id=gid)
        except Exception as exc:  # noqa: BLE001
            return _err(f"拉取群成员失败：{type(exc).__name__}")
        rows = []
        for m in members or []:
            try:
                uid = int(m.get("user_id") or 0)
            except (TypeError, ValueError):
                continue
            if uid <= 0:
                continue
            nick = str(m.get("card") or m.get("nickname") or "")
            rows.append((uid, nick, gid))
        if not rows:
            return _err("这个群里没拉到成员")
        added = await store.add_users(rows)
        return JSONResponse({
            "ok": True,
            "message": f"群 {gid} 共 {len(rows)} 人，新增 {added} 个，其余已存在",
        })

    if action == "delete":
        qqs = body.get("qqs") or []
        if isinstance(body.get("qq"), (int, str)):
            qqs = [body.get("qq")]
        ids = []
        for q in qqs:
            try:
                ids.append(int(q))
            except (TypeError, ValueError):
                continue
        n = await store.delete_users(ids)
        return JSONResponse({"ok": True, "message": f"已删除 {n} 个"})

    if action == "toggle":
        try:
            qq = int(body.get("qq") or 0)
        except (TypeError, ValueError):
            return _err("QQ 号不对")
        ok = await store.set_enabled(qq, bool(body.get("enabled")))
        return JSONResponse({"ok": True, "message": "已更新" if ok else "没有这个 QQ"})

    return _err("未知操作")


def _parse_user_text(text: str) -> list[tuple[int, str]]:
    """解析批量导入的文本：一行一个，支持 ``QQ``、``QQ 昵称``、``QQ,昵称``。"""
    import re

    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\d{5,12})\b[\s,，\t:：]*(.*)$", line)
        if not m:
            continue
        qq = int(m.group(1))
        if qq in seen:
            continue
        seen.add(qq)
        out.append((qq, m.group(2).strip()[:32]))
    return out


# ------------------------------------------------------------------ 页面


async def _page_guess() -> HTMLResponse:
    if not _cfg().enabled:
        return HTMLResponse("<h3>猜歌功能未开启</h3>", status_code=404)
    return HTMLResponse(GAME_HTML)


async def _page_admin() -> HTMLResponse:
    return HTMLResponse(ADMIN_HTML)


# ------------------------------------------------------------------ 生命周期


async def setup() -> None:
    """建表、装配房间管理器、起回收巡检。由插件启动钩子调用。"""
    global store, manager, auth
    from ..config import DB_PATH

    store = GameStore(DB_PATH)
    await store.init()
    manager = RoomManager(_cfg)
    auth = GameAuth(store, _cfg)
    asyncio.create_task(_reap_loop())


async def _reap_loop() -> None:
    while True:
        try:
            await asyncio.sleep(REAP_SECONDS)
            if manager is None:
                continue
            dead = manager.reap()
            for code in dead:
                for qq, sockets in list(hub.peers(code).items()):
                    for ws in list(sockets):
                        try:
                            await ws.close(code=4410)
                        except Exception:  # noqa: BLE001
                            pass
                        hub.remove(code, qq, ws)
            if store is not None:
                await store.prune()
        except asyncio.CancelledError:  # pragma: no cover
            return
        except Exception:  # noqa: BLE001 - 巡检失败不该弄死循环
            await asyncio.sleep(5)


def register_game() -> None:
    """把页面与接口挂到 FastAPI 上。"""
    try:
        from nonebot import get_app
    except Exception:  # pragma: no cover
        return
    try:
        app = get_app()
    except Exception:  # pragma: no cover
        return

    app.add_api_route("/guess", _page_guess, methods=["GET"])
    app.add_api_route("/music-admin/guess", _page_admin, methods=["GET"])

    app.add_api_route("/api/guess/login/code", _api_login_code, methods=["POST"])
    app.add_api_route("/api/guess/login", _api_login, methods=["POST"])
    app.add_api_route("/api/guess/logout", _api_logout, methods=["POST"])
    app.add_api_route("/api/guess/me", _api_me, methods=["GET"])
    app.add_api_route("/api/guess/windows", _api_windows, methods=["GET"])
    app.add_api_route("/api/guess/room", _api_room_get, methods=["GET"])
    app.add_api_route("/api/guess/room/create", _api_room_create, methods=["POST"])
    app.add_api_route("/api/guess/room/join", _api_room_join, methods=["POST"])
    app.add_api_route("/api/guess/room/action", _api_room_action, methods=["POST"])
    app.add_api_route("/api/guess/room/leave", _api_room_leave, methods=["POST"])
    app.add_api_route("/api/guess/room/archive", _api_room_archive, methods=["POST"])
    app.add_api_route("/api/music-admin/game/users", _api_admin_users, methods=["GET"])
    app.add_api_route("/api/music-admin/game/users", _api_admin_users_post, methods=["POST"])
    app.add_api_websocket_route("/api/guess/ws", _ws)


__all__ = ["setup", "register_game"]
