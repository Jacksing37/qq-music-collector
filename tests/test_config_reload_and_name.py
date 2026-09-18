"""配置自动重载/合并写入 + 歌单名创建校验的回归测试。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_config_reload_and_name.py

覆盖（都对应线上真实症状）：
[A] 配置被外部改动后自动重载 —— 不再一直用启动时的旧值（「歌单名设置不生效」）
[B] update() 合并写入 —— 不用旧内存整份回写，覆盖外部刚改好的期号
[C] 期号自增以磁盘最新值为基准 —— 归档途中被外部改大也不会退回去（「新窗口期号没变」）
[D] 空歌单名被拒绝 —— 不再建成网易云的默认名「用户xxx的歌单」
[E] 创建后名字未被网易云采纳时自动改名补救
[F] 复用归档：显式歌单名会真正改名，并把真实名字存进 archives（报告不再显示占位假名）
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402
import yaml  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector.archiver import Archiver  # noqa: E402
from music_collector.config import ConfigManager, PlaylistConfig, config_manager  # noqa: E402
from music_collector.models import Song  # noqa: E402
from music_collector.netease_api import NeteaseAPI, NeteaseError  # noqa: E402
from music_collector.service import CollectorService  # noqa: E402
from music_collector.store import Store  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


def _song(sid: str, title: str) -> Song:
    return Song(
        platform="netease", song_id=sid, title=title, artists="测试歌手",
        sharer_id=1, sharer_name="张三", url="", netease_id=sid,
    )


def _external_write(path: Path, mutate) -> None:
    """模拟「另一个进程 / 网页端 / 手工编辑」改配置文件。

    写完把 mtime 往后拨 5 秒，避免同一时间粒度内两次写入 mtime 相同、
    导致自动重载检测不到（NTFS 上 1µs 级，但显式拨快更稳）。
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    future = time.time() + 5
    os.utime(path, (future, future))


# ---------------------------------------------------------------- A/B

def test_reload_and_merge_update() -> None:
    print("\n[A/B] 配置自动重载 + update 合并写入")
    tmp = Path(tempfile.mkdtemp())
    cm = ConfigManager(tmp / "config.yaml")
    cm.load()
    cm.update("playlist.name_template", "Wk.{seq}测试")
    cm.update("playlist.seq", 42)

    # 外部把期号与模板都改了
    _external_write(tmp / "config.yaml", lambda d: d["playlist"].update(
        {"seq": 99, "name_template": "外部改的名字"}))

    check("外部改动被自动重载（期号）", cm.config.playlist.seq == 99,
          f"seq={cm.config.playlist.seq}")
    check("外部改动被自动重载（模板）", cm.config.playlist.name_template == "外部改的名字",
          f"tpl={cm.config.playlist.name_template}")

    # 再写另一个键：必须合并，不能把外部刚写的 99 回退成 42
    cm.update("playlist.pending_name", "一次性名")
    check("update 保留外部期号（不回退）", cm.config.playlist.seq == 99,
          f"seq={cm.config.playlist.seq}")
    check("update 写入了目标键", cm.config.playlist.pending_name == "一次性名",
          f"pending={cm.config.playlist.pending_name!r}")
    # 落盘内容同样要保持 99
    on_disk = yaml.safe_load((tmp / "config.yaml").read_text(encoding="utf-8"))
    check("落盘期号没有被回退", on_disk["playlist"]["seq"] == 99,
          f"disk={on_disk['playlist']['seq']}")


# ---------------------------------------------------------------- C

class _StubNetease:
    """归档期间模拟「外部把期号改大」，验证自增以磁盘最新值为基准。"""

    logged_in = True

    def __init__(self, config_path: Path, on_create=None) -> None:
        self.created: list[str] = []
        self._path = config_path
        self._on_create = on_create

    async def create_playlist(self, name: str, privacy: bool) -> int:
        self.created.append(name)
        if self._on_create:
            self._on_create()
        return 7001

    async def add_tracks(self, playlist_id, track_ids) -> None: ...
    async def remove_tracks(self, playlist_id, track_ids) -> None: ...
    async def update_description(self, playlist_id, desc, name="") -> tuple[bool, str]:
        return True, "ok"

    def playlist_url(self, playlist_id) -> str:
        return f"https://music.163.com/playlist/{playlist_id}"


async def test_seq_uses_latest_value() -> None:
    print("\n[C] 期号自增以磁盘最新值为基准（不丢失更新）")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()

    config_manager.path = tmp / "config.yaml"
    config_manager.load()
    config_manager.update("playlist.seq", 5)
    config_manager.update("playlist.name_template", "Wk.{seq}线上学习")
    config_manager.update("playlist.seq_auto_increment", True)
    config_manager.update("playlist.pending_name", "")

    # 归档在建歌单那一刻，另一个进程把期号改成了 100
    def bump_outside() -> None:
        _external_write(tmp / "config.yaml", lambda d: d["playlist"].update({"seq": 100}))

    api = _StubNetease(tmp / "config.yaml", on_create=bump_outside)
    svc = CollectorService()
    svc.store = store
    svc.netease = api
    svc.archiver = Archiver(api, store)

    gid = 8101
    state = svc.current_window()
    await store.add_song(gid, state.key, _song("1", "一"))
    report = await svc.run_archive(gid, window=state)

    check("归档成功且为新建", report.ok and report.created_new)
    check("期号以最新值 100 为基准自增到 101",
          config_manager.config.playlist.seq == 101,
          f"seq={config_manager.config.playlist.seq}")


# ---------------------------------------------------------------- D

async def test_empty_name_rejected() -> None:
    print("\n[D] 空歌单名被拒绝（不建默认名歌单）")
    api = NeteaseAPI(Path(tempfile.mkdtemp()) / "session.json")
    api._cookies = {"MUSIC_U": "fake"}  # 伪装已登录，避免走到未登录分支
    try:
        await api.create_playlist("   ")
        check("空名被拒绝", False, "居然建成功了")
    except NeteaseError as exc:
        msg = getattr(exc, "message", str(exc))
        check("空名被拒绝且提示原因", "歌单名" in msg, f"msg={msg!r}")


# ---------------------------------------------------------------- E

class _NameIgnoringAPI(NeteaseAPI):
    """模拟网易云忽略了 create 的 name（建成默认名）时的补救链路。"""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._cookies = {"MUSIC_U": "fake"}
        self.actual = "用户318045740的歌单"
        self.renamed: list[tuple[int, str]] = []

    async def _linux_post(self, path, payload, **kwargs):  # noqa: ANN001, ANN003
        return {"id": 9001, "code": 200}

    async def playlist_detail(self, playlist_id: int) -> dict:
        return {"name": self.actual}

    async def rename_playlist(self, playlist_id: int, name: str) -> tuple[bool, str]:
        self.renamed.append((playlist_id, name))
        self.actual = name
        return True, "stub"


async def test_name_verified_after_create() -> None:
    print("\n[E] 创建后名字未被采纳 → 自动改名补救")
    api = _NameIgnoringAPI(Path(tempfile.mkdtemp()) / "session.json")
    pid = await api.create_playlist("Wk.42线上学习26/9/18")
    check("创建返回歌单 id", pid == 9001, f"pid={pid}")
    check("自动按期望名改名", api.renamed == [(9001, "Wk.42线上学习26/9/18")],
          f"renamed={api.renamed}")
    check("改后名字正确", api.actual == "Wk.42线上学习26/9/18", f"actual={api.actual!r}")


# ---------------------------------------------------------------- F

class _StubArchiveAPI:
    logged_in = True

    def __init__(self) -> None:
        self.created: list[str] = []
        self.renamed: list[tuple[int, str]] = []

    async def create_playlist(self, name: str, privacy: bool) -> int:
        self.created.append(name)
        return 8001

    async def add_tracks(self, playlist_id, track_ids) -> None: ...
    async def remove_tracks(self, playlist_id, track_ids) -> None: ...
    async def update_description(self, playlist_id, desc, name="") -> tuple[bool, str]:
        return True, "ok"

    async def rename_playlist(self, playlist_id: int, name: str) -> tuple[bool, str]:
        self.renamed.append((playlist_id, name))
        return True, "stub"

    def playlist_url(self, playlist_id) -> str:
        return f"https://music.163.com/playlist/{playlist_id}"


async def test_reuse_rename_and_stored_name() -> None:
    print("\n[F] 复用归档按显式名改名 + archives 存真实歌单名")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()
    api = _StubArchiveAPI()
    arch = Archiver(api, store)  # type: ignore[arg-type]
    cfg = PlaylistConfig(
        name_template="Wk.{seq}-{window}", seq=5,
        pending_name="", include_sharers=True, sharer_style="list",
    )
    gid = 8201

    r1 = await arch.archive(gid, "W1", "窗口1", [_song("1", "一")], cfg)
    check("新建用模板名", r1.created_new and r1.playlist_name == "Wk.5-窗口1",
          f"name={r1.playlist_name!r}")
    saved = await store.get_archive(gid, "W1")
    check("archives 存下真实歌单名", (saved or {}).get("playlist_name") == "Wk.5-窗口1",
          f"stored={(saved or {}).get('playlist_name')!r}")

    # 复用（同窗口再次归档）+ 显式指定名字 -> 必须真的改名
    r2 = await arch.archive(gid, "W1", "窗口1", [_song("1", "一"), _song("2", "二")],
                            cfg, name_override="手工名字")
    check("复用不新建", r2.ok and not r2.created_new)
    check("复用按显式名改名", api.renamed == [(8001, "手工名字")], f"renamed={api.renamed}")
    check("报告名字为显式名", r2.playlist_name == "手工名字", f"name={r2.playlist_name!r}")

    # 再次复用（无显式名）：报告要用存档里的真实名字，而不是「群歌单 窗口1」占位
    r3 = await arch.archive(gid, "W1", "窗口1",
                            [_song("1", "一"), _song("2", "二"), _song("3", "三")], cfg)
    check("无显式名时报告真实名字", r3.playlist_name == "手工名字",
          f"name={r3.playlist_name!r}")
    check("不再出现占位假名", "群歌单 窗口1" != r3.playlist_name)


async def main() -> int:
    test_reload_and_merge_update()
    await test_seq_uses_latest_value()
    await test_empty_name_rejected()
    await test_name_verified_after_create()
    await test_reuse_rename_and_stored_name()
    print(f"\n{'=' * 52}\n通过 {PASSED} 项，失败 {FAILED} 项")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
