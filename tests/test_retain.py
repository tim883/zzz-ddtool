"""日志轮次：建 / 切 / 收 的生命周期，以及按历史轮数整轮回收。

保留单位是轮次目录，不是张数：``log/<时间戳>/`` 整轮删，一轮里的主日志、报告、
统计、性能、截图一起走。前提：排错靠 debug 重跑复现，不靠翻旧图回溯，所以不做
「失败永远保留」这类特殊策略——留着只会把正常的挤掉。

谁不该被删：正在写入的轮次（本进程的当前轮 / 别的进程占着 ``.active`` 的轮次）、
未登记目录、名字对不上的目录、越界路径。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from zzz_ddtool import loground, logutil, paths, retain


# ------------------------------------------------------------------ 造数据

def _name(index: int) -> str:
    return f"20260101-{index:06d}-000"


def _make_round(
    root: Path,
    name: str,
    *,
    state: str = "closed",
    pid: int | None = None,
    source: str = "task",
) -> Path:
    """造一个已登记的轮次目录（不建占用锁），供回收用例摆数据。"""
    folder = root / name
    (folder / "captures").mkdir(parents=True)
    (folder / "ddtool.log").write_text("本轮日志", encoding="utf-8")
    (folder / "captures" / "0001-识别盘面.png").write_bytes(b"png")
    (folder / "report.md").write_text("报告", encoding="utf-8")
    record = {
        "version": loground.ROUND_VERSION,
        "id": name,
        "source": source,
        "mode": loground.MODE_PRODUCTION,
        "state": state,
        "pid": os.getpid() if pid is None else pid,
        "created_at": "2026-01-01T00:00:00.000+08:00",
        "started_at": "",
        "ended_at": "",
        "outcome": "",
        "end_reason": "",
        "config": "",
        "task": "",
        "artifacts": {},
    }
    (folder / loground.REGISTRY_NAME).write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    return folder


@pytest.fixture
def round_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """独立日志根，并把它当成 ``paths.log_root()``：不碰真实 log/。"""
    root = tmp_path / "log"
    root.mkdir()
    monkeypatch.setattr(paths, "log_root", lambda: root)
    return root


@pytest.fixture(autouse=True)
def _restore_limit():
    keep = retain.get_max_rounds()
    yield
    retain.set_max_rounds(keep)


def _dir_names(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _close_round() -> None:
    """用例收工时务必收掉本进程轮次，别把模块状态留成「有一轮在建」。"""
    loground.close_active()


def _make_outward_link(target: Path, link: Path) -> None:
    """造一个指向日志根外面的目录链接；两种都建不出来就抛 OSError。

    目录联接（junction）在 Windows 上不需要提权，也比符号链接常见，
    而且 ``is_symlink()`` 认不出来——「不越界」这条线主要靠它兜。
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        raise OSError(result.stderr.strip() or result.stdout.strip())
    raise OSError("本机建不出指向目录的符号链接")


# ------------------------------------------------------------------ 回收

def test_under_limit_keeps_everything(round_root: Path) -> None:
    for index in range(3):
        _make_round(round_root, _name(index))
    assert retain.count_rounds() == 3
    assert retain.retain_rounds(5) == []
    assert _dir_names(round_root) == [_name(0), _name(1), _name(2)]


def test_over_limit_deletes_oldest_rounds_whole(round_root: Path) -> None:
    """最旧优先，整轮删——日志、截图、报告一起走，不留半截轮次。"""
    for index in range(4):
        _make_round(round_root, _name(index))
    oldest = round_root / _name(0)
    assert (oldest / "ddtool.log").is_file() and (oldest / "captures").is_dir()

    removed = retain.retain_rounds(2)

    assert [p.name for p in removed] == [_name(0), _name(1)]
    assert _dir_names(round_root) == [_name(2), _name(3)]
    assert not oldest.exists()
    assert retain.count_rounds() == 2


def test_limit_one_keeps_only_the_newest(round_root: Path) -> None:
    for index in range(3):
        _make_round(round_root, _name(index))
    assert len(retain.retain_rounds(1)) == 2
    assert _dir_names(round_root) == [_name(2)]
    assert retain.retain_rounds(1) == []  # 再收一次无事可做


def test_removal_failure_is_not_counted_as_recycled(
    round_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删不掉的留在原地等重试，不冒充实删数。"""
    for index in range(3):
        _make_round(round_root, _name(index))
    real = loground.remove_round
    monkeypatch.setattr(
        loground, "remove_round", lambda root: False if root.name == _name(0) else real(root)
    )

    removed = retain.retain_rounds(1)

    assert [p.name for p in removed] == [_name(1)]
    assert (round_root / _name(0)).is_dir()


def test_clear_history_counts_only_real_removals(round_root: Path) -> None:
    for index in range(3):
        _make_round(round_root, _name(index))
    _make_round(round_root, _name(3), state="open")  # 登记的进程还活着 → 占用
    stray = round_root / _name(4)  # 名字对但没有登记文件
    stray.mkdir()
    (round_root / "run-status.json").write_text("{}", encoding="utf-8")

    assert retain.clear_history() == 3
    assert _dir_names(round_root) == [_name(3), _name(4)]
    assert stray.is_dir()
    assert (round_root / "run-status.json").is_file()


def test_open_round_with_a_live_process_is_protected(round_root: Path) -> None:
    live = _make_round(round_root, _name(0), state="open")
    assert not retain.is_history(live)
    assert retain.clear_history() == 0
    assert live.is_dir()


def test_crashed_round_without_lock_is_recyclable(round_root: Path) -> None:
    """崩溃留下的 open 登记不该永久占名额：没有锁、进程已死就该能收回。"""
    crashed = _make_round(round_root, _name(0), state="open", pid=999999)
    assert retain.is_history(crashed)
    assert retain.clear_history() == 1
    assert not crashed.exists()


def test_current_round_is_not_recycled(round_root: Path) -> None:
    for index in range(3):
        _make_round(round_root, _name(index))
    active = loground.begin("task", root=round_root)
    try:
        assert loground.is_occupied(active.root)
        assert retain.retain_rounds(1) == [round_root / _name(0), round_root / _name(1)]
        assert active.root.is_dir()
        assert active.lock_path.exists()  # 清理没碰当前轮次的占用锁
        assert retain.history_rounds() == [round_root / _name(2)]
    finally:
        _close_round()


def test_name_matching_dir_without_registry_is_not_a_round(round_root: Path) -> None:
    stray = round_root / _name(0)
    stray.mkdir()
    assert not retain.registered(stray)
    assert retain.iter_rounds() == []
    assert retain.clear_history() == 0
    assert stray.is_dir()


def test_linked_dir_outside_the_root_is_not_recycled(round_root: Path, tmp_path: Path) -> None:
    """越界：指向日志根外面的目录联接/符号链接不枚举、不回收。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = _make_round(outside, _name(0))  # 名字、登记都合法，但在日志根之外
    link = round_root / _name(1)
    try:
        _make_outward_link(victim, link)
    except OSError as exc:
        pytest.skip(f"本机建不出目录联接/符号链接：{exc}")

    assert link not in retain.iter_rounds()
    assert retain.iter_rounds() == []
    assert retain.clear_history() == 0
    assert victim.is_dir()
    assert link.exists()


# ------------------------------------------------------------------ 跨进程占用

_HOLDER = """\
import sys, time
from pathlib import Path
from zzz_ddtool import loground

root = Path(sys.argv[1])
signal = Path(sys.argv[2])
round_ = loground.begin("probe", root=root)
signal.write_text(str(round_.root), encoding="utf-8")
time.sleep(float(sys.argv[3]))
"""


def _start_foreign_writer(root: Path, workdir: Path, seconds: float = 60.0):
    """起个子进程在 root 下开一轮并占住不放，返回 (进程, 轮次目录)。"""
    workdir.mkdir(parents=True, exist_ok=True)
    script = workdir / "holder.py"
    script.write_text(_HOLDER, encoding="utf-8")
    signal = workdir / "holding.txt"
    proc = subprocess.Popen(
        [sys.executable, str(script), str(root), str(signal), str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if signal.is_file():
            return proc, Path(signal.read_text(encoding="utf-8"))
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    proc.kill()
    out, err = proc.communicate(timeout=10)
    raise AssertionError(f"子进程没能占住轮次：stdout={out!r} stderr={err!r}")


def test_foreign_process_round_is_protected_until_it_exits(
    round_root: Path, tmp_path: Path
) -> None:
    child, foreign = _start_foreign_writer(round_root, tmp_path / "holder")
    try:
        assert foreign.parent == round_root
        assert loground.is_occupied(foreign)
        assert not retain.is_history(foreign)
        assert retain.clear_history() == 0
        assert foreign.is_dir()

        # 同批还有历史轮次时，他进程占着的那轮不占历史名额
        for index in range(3):
            _make_round(round_root, _name(index))
        assert len(retain.history_rounds()) == 3
        assert foreign not in retain.history_rounds()
    finally:
        child.kill()
        child.wait(timeout=10)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not retain.is_history(foreign):
        time.sleep(0.1)
    # 子进程一退出，锁由系统释放，这一轮就转成可回收的历史轮次
    assert retain.is_history(foreign)
    assert foreign in retain.history_rounds()


# ------------------------------------------------------------------ 轮次生命周期

def test_begin_creates_timestamped_round_with_registry_and_lock(round_root: Path) -> None:
    round_ = loground.use_production_context(
        "task", root=round_root, config="user/plan/a.yaml", task="3号位"
    )
    try:
        assert loground.NAME_PATTERN.match(round_.id), round_.id
        assert round_.root == round_root / round_.id
        assert round_.log_path == round_.root / "ddtool.log"
        assert round_.report_path == round_.root / "report.md"
        assert round_.stats_path == round_.root / "stats.json"
        assert round_.perf_path == round_.root / "perf.jsonl"
        assert round_.captures_dir == round_.root / "captures"
        assert round_.success_dir == round_.root / "success"
        assert round_.diagnostics_dir == round_.root / "diagnostics"
        assert round_.registry_path == round_.root / "round.json"
        assert round_.lock_path == round_.root / ".active"

        record = json.loads(round_.registry_path.read_text(encoding="utf-8"))
        assert record["version"] == loground.ROUND_VERSION
        assert record["id"] == round_.id
        assert record["source"] == "task"
        assert record["mode"] == loground.MODE_PRODUCTION
        assert record["state"] == "open"
        assert record["config"] == "user/plan/a.yaml"
        assert record["task"] == "3号位"
        assert isinstance(record["created_at"], str) and record["created_at"]
        assert record["artifacts"] == {
            "log": "ddtool.log",
            "report": "report.md",
            "stats": "stats.json",
            "perf": "perf.jsonl",
            "captures": "captures",
            "success": "success",
            "diagnostics": "diagnostics",
        }
        assert round_.lock_path.is_file()
        assert loground.is_occupied(round_.root)
        # 按需产生的目录不预建：现在只有登记、占用锁和主日志
        assert {p.name for p in round_.root.iterdir()} == {".active", "ddtool.log", "round.json"}
    finally:
        _close_round()


def test_begin_never_reuses_an_existing_directory(
    round_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同毫秒撞名要重取时间另建目录，不接管已有目录。"""
    taken = _name(0)
    (round_root / taken).mkdir()
    real = loground._new_round_id
    calls = {"n": 0}

    def collide_once() -> str:
        calls["n"] += 1
        return taken if calls["n"] == 1 else real()

    monkeypatch.setattr(loground, "_new_round_id", collide_once)
    round_ = loground.begin("task", root=round_root)
    try:
        assert round_.id != taken
        assert calls["n"] >= 2
        assert loground.read_registry(round_root / taken) is None
        assert not (round_root / taken / loground.LOCK_NAME).exists()
    finally:
        _close_round()


def test_second_begin_closes_previous_round_and_releases_lock(round_root: Path) -> None:
    first = loground.begin("gui", root=round_root)
    second = loground.begin("task", root=round_root)
    try:
        assert first.root != second.root
        record = json.loads(first.registry_path.read_text(encoding="utf-8"))
        assert record["state"] == "closed"
        assert record["ended_at"]
        assert not first.lock_path.exists()
        assert not loground.is_occupied(first.root)
        assert loground.current() is not None and loground.current().root == second.root
    finally:
        _close_round()


def test_finish_marks_closed_and_keeps_writing(round_root: Path) -> None:
    round_ = loground.begin("task", root=round_root, started=True)
    log = logutil.get_logger("zzz_ddtool.roundlife")
    try:
        log.info("收尾前")
        loground.finish("completed", reason="跑完")
        record = json.loads(round_.registry_path.read_text(encoding="utf-8"))
        assert record["state"] == "closed"
        assert record["outcome"] == "completed"
        assert record["end_reason"] == "跑完"
        assert record["ended_at"]
        assert record["started_at"]

        # 收尾 != 切轮次：文件继续写空闲/关闭日志，占用还没释放
        log.info("收尾后")
        assert loground.is_occupied(round_.root)
        text = round_.log_path.read_text(encoding="utf-8")
        assert "收尾前" in text and "收尾后" in text
    finally:
        _close_round()


def test_close_active_releases_the_round(round_root: Path) -> None:
    round_ = loground.begin("task", root=round_root)
    assert logutil.current_log_path() == round_.log_path

    _close_round()

    assert loground.current() is None
    assert logutil.current_log_path() is None
    record = json.loads(round_.registry_path.read_text(encoding="utf-8"))
    assert record["state"] == "closed"
    assert not round_.lock_path.exists()
    assert not loground.is_occupied(round_.root)

    logutil.get_logger("zzz_ddtool.roundlife").info("收尾之后还想写")
    assert logutil.current_log_path() is None
    assert "收尾之后还想写" not in round_.log_path.read_text(encoding="utf-8")


def test_claim_task_adopts_unstarted_round_then_opens_new_one(round_root: Path) -> None:
    entry = loground.begin("cli-run", root=round_root)
    claimed = loground.claim_task(config="user/plan/one.yaml", task="1号位")
    try:
        assert claimed.root == entry.root
        assert claimed.started_at
        assert claimed.config == "user/plan/one.yaml"
        assert claimed.task == "1号位"

        # 已经开过任务的那一轮不再被认领
        follow_up = loground.claim_task()
        assert follow_up.root != claimed.root
        assert follow_up.root.parent == round_root
    finally:
        _close_round()


def test_without_a_round_captures_stay_out_of_production(round_root: Path) -> None:
    """没有在建轮次时，截图/取证只给兜底位置，不落生产日志根。"""
    _close_round()
    captures = loground.captures_dir()
    diagnostics = loground.diagnostics_dir()

    assert captures.name == "captures"
    assert diagnostics.name == "diagnostics"
    assert captures.parent.name == "no-round"
    assert diagnostics.parent.name == "no-round"
    assert round_root not in captures.parents
    assert round_root not in diagnostics.parents
    assert not captures.exists() and not diagnostics.exists()


def test_status_pointer_stays_on_the_log_root(round_root: Path) -> None:
    round_ = loground.begin("task", root=round_root)
    try:
        assert loground.current_root() == round_root
        assert loground.status_file() == round_root / "run-status.json"
        # 查询不该把文件建出来，也不该落到轮次目录里
        assert not loground.status_file().exists()
        assert round_.root not in loground.status_file().parents
    finally:
        _close_round()
