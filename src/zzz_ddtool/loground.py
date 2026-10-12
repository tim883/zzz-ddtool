"""日志轮次的生命周期：一轮 = 一个时间戳目录。

目录长这样（生产根 ``log/``，测试/开发根 ``log_test/`` 同构）::

    log/
      run-status.json                 当前状态指针（离开轮次，留在根上）
      20261012-153012-047/            一轮
        round.json                    轮次登记（程序管理的凭据）
        ddtool.log                    本轮文本日志
        report.md                     执行结果总结
        stats.json                    结构化统计
        perf.jsonl                    本轮性能与输入证据
        captures/                     步骤截图
        success/                      升满保留的成功盘图
        diagnostics/                  故障取证（按需产生）

为什么一轮一个目录：
    出问题时先看 ``round.json`` 知道这轮是谁、什么时候开始；再翻同一个目录里的
    日志和截图，不用按时间戳去别的目录里凑。回收也就能整轮删，不留下半截。

一轮什么时候开始、什么时候结束：
    - 一次正式任务（一份 yaml / 一次 ``Runner.run()``）建一个新轮次。
    - GUI 启动先建一个启动轮次放启动日志；第一个任务开始前另开新轮次，
      不和任务日志混在一个文件里。
    - 任务收尾写统计和报告后把登记改成 ``closed``，但文件继续留着写空闲和
      关闭日志，直到切换下一轮或退出程序才释放占用。
    - 创建轮次是显式操作。查询当前路径、读日志设置、刷新界面都不隐式建目录。

谁在写：``<轮次>/.active`` 是占用锁。写入进程打开它并加字节锁，
其他进程（包括另一个版本、CLI、脚本）用同一把锁判断"这轮还在写"，
锁住了就不回收。进程崩溃时句柄由系统释放，锁自然失效，不会永久占着。

目录名的毫秒后缀是为了同一秒里连续开两轮也不撞；真撞了就重新取时间，
用原子建目录保证不复用、不覆盖已有目录。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROUND_VERSION = 1
MODE_PRODUCTION = "production"
MODE_TEST = "test"

#: 轮次目录名：YYYYMMDD-HHMMSS-fff（本机本地时间，毫秒三位）
NAME_PATTERN = re.compile(r"^\d{8}-\d{6}-\d{3}$")
REGISTRY_NAME = "round.json"
LOCK_NAME = ".active"
STATUS_NAME = "run-status.json"

_ARTIFACTS = {
    "log": "ddtool.log",
    "report": "report.md",
    "stats": "stats.json",
    "perf": "perf.jsonl",
    "captures": "captures",
    "success": "success",
    "diagnostics": "diagnostics",
}

_state_lock = threading.RLock()
_mode = MODE_PRODUCTION
_active: Round | None = None
_active_fd: int | None = None


def _log():
    from zzz_ddtool.logutil import get_logger

    return get_logger("zzz_ddtool.loground")


@dataclass
class Round:
    """一轮日志的路径契约。查询这些路径不创建任何目录。"""

    root: Path
    id: str
    source: str
    mode: str
    created_at: str
    state: str = "open"
    config: str = ""
    task: str = ""
    started_at: str = ""
    ended_at: str = ""
    outcome: str = ""
    end_reason: str = ""
    artifacts: dict = field(default_factory=dict)

    @property
    def log_path(self) -> Path:
        return self.root / _ARTIFACTS["log"]

    @property
    def report_path(self) -> Path:
        return self.root / _ARTIFACTS["report"]

    @property
    def stats_path(self) -> Path:
        return self.root / _ARTIFACTS["stats"]

    @property
    def perf_path(self) -> Path:
        return self.root / _ARTIFACTS["perf"]

    @property
    def captures_dir(self) -> Path:
        return self.root / _ARTIFACTS["captures"]

    @property
    def success_dir(self) -> Path:
        return self.root / _ARTIFACTS["success"]

    @property
    def diagnostics_dir(self) -> Path:
        return self.root / _ARTIFACTS["diagnostics"]

    @property
    def registry_path(self) -> Path:
        return self.root / REGISTRY_NAME

    @property
    def lock_path(self) -> Path:
        return self.root / LOCK_NAME

    def as_record(self) -> dict:
        return {
            "version": ROUND_VERSION,
            "id": self.id,
            "source": self.source,
            "mode": self.mode,
            "state": self.state,
            "pid": os.getpid(),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "outcome": self.outcome,
            "end_reason": self.end_reason,
            "config": self.config,
            "task": self.task,
            "artifacts": dict(self.artifacts or _ARTIFACTS),
        }


# ------------------------------------------------------------------ 根目录


def _production_root() -> Path:
    from zzz_ddtool import paths

    return paths.log_root()


def _test_root() -> Path:
    from zzz_ddtool import paths

    return paths.test_log_root()


def mode() -> str:
    with _state_lock:
        return _mode


def current() -> Round | None:
    """当前轮次；没有在建轮次时返回 None（不创建任何东西）。"""
    with _state_lock:
        return _active


def current_root() -> Path:
    """当前上下文所在的日志根（生产 ``log/`` 或测试 ``log_test/``）。"""
    with _state_lock:
        if _active is not None:
            return _active.root.parent
        return _test_root() if _mode == MODE_TEST else _production_root()


def status_file() -> Path:
    """当前状态指针：留在当前上下文根上，不随轮次目录回收。"""
    return current_root() / STATUS_NAME


def captures_dir() -> Path:
    """当前轮次的步骤截图目录；没有轮次时给未登记兜底目录，绝不落生产日志根。"""
    round_ = current()
    if round_ is not None:
        return round_.captures_dir
    return _test_root() / "no-round" / _ARTIFACTS["captures"]


def diagnostics_dir() -> Path:
    """当前轮次的故障取证目录；没有轮次时同上兜底。"""
    round_ = current()
    if round_ is not None:
        return round_.diagnostics_dir
    return _test_root() / "no-round" / _ARTIFACTS["diagnostics"]


# ------------------------------------------------------------------ 占用锁


def _acquire_lock(path: Path) -> int | None:
    """打开并锁住 ``.active``。返回 fd；失败返回 None（不阻塞建轮次）。"""
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        _lock_fd(fd)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        _unlock_fd(fd)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _lock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _probe_lock(path: Path) -> bool:
    """别的进程有没有占着这轮？拿不到锁 = 有人在写。"""
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return False
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        _lock_fd(fd)
    except OSError:
        return True
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        _unlock_fd(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
    return False


def _pid_alive(pid: int) -> bool:
    """这个进程还在不在。判断不了时按"还在"处理，宁可少删不可误删。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def is_occupied(root: Path) -> bool:
    """这一轮还在被写吗？本进程的当前轮次、别的进程持有的锁都算占用。"""
    root = Path(root)
    with _state_lock:
        if _active is not None and _active.root == root:
            return True
    lock_path = root / LOCK_NAME
    if lock_path.exists():
        return _probe_lock(lock_path)
    record = read_registry(root)
    if record is None:
        return False
    if record.get("source") == "pytest" and record.get("pid") == os.getpid():
        return False
    if record.get("state") == "open":
        return _pid_alive(record.get("pid", 0))
    return False


# ------------------------------------------------------------------ 登记文件


def read_registry(root: Path) -> dict | None:
    """读 ``round.json``；读不出来（缺失、坏了、不是登记）返回 None。"""
    path = Path(root) / REGISTRY_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def write_registry(round_: Round) -> bool:
    """原子写登记文件：先写临时文件再替换，读到的一半状态不会留下。"""
    target = round_.registry_path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(REGISTRY_NAME + ".tmp")
        temp.write_text(
            json.dumps(round_.as_record(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temp, target)
    except OSError as exc:
        _log().warning("轮次登记写入失败：%s（%s）", target, exc)
        return False
    return True


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _new_round_id() -> str:
    now = datetime.now()
    return now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}"


def _make_root(parent: Path) -> tuple[Path, str]:
    """建轮次目录：同毫秒撞了就重新取时间；原子建目录，不复用已有目录。"""
    parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(200):
        name = _new_round_id()
        target = parent / name
        try:
            os.mkdir(target)
        except FileExistsError:
            continue
        except OSError as exc:
            raise OSError(f"日志目录建不出来（{parent}）：{exc}") from exc
        return target, name
    raise OSError(f"连续 {200} 次都撞上同一毫秒，日志目录没建出来：{parent}")


# ------------------------------------------------------------------ 生命周期


def _attach_logging(round_: Round) -> None:
    from zzz_ddtool.logutil import use_round

    use_round(round_)


def _detach_logging() -> None:
    from zzz_ddtool.logutil import detach_round

    detach_round()


def begin(
    source: str,
    *,
    config: str = "",
    task: str = "",
    root: Path | None = None,
    started: bool = False,
) -> Round:
    """开一轮。``source`` 记来源（gui/task/cli-run/probe/pytest/…）。

    已有轮次先收尾：一次只能有一轮在写，新轮次开始意味着上一轮结束。
    """
    global _active, _active_fd
    with _state_lock:
        old_round, old_fd = _active, _active_fd
        if old_round is not None:
            _finish_locked(outcome=old_round.outcome, reason=old_round.end_reason)
        parent = Path(root) if root is not None else current_root()
        target, name = _make_root(parent)
        round_ = Round(
            root=target,
            id=name,
            source=source,
            mode=_mode,
            created_at=_stamp(),
            started_at=_stamp() if started else "",
            config=config,
            task=task,
        )
        fd = _acquire_lock(round_.lock_path)
        write_registry(round_)
        _active, _active_fd = round_, fd
    _attach_logging(round_)
    if old_round is not None:
        _release_lock(old_fd)
        try:
            old_round.lock_path.unlink(missing_ok=True)
        except OSError:
            pass
    return round_


def begin_task(source: str = "task", *, config: str = "", task: str = "") -> Round:
    """一次正式任务的轮次：一份 yaml 一个目录，不复用启动轮次。"""
    return begin(source, config=config, task=task, started=True)


def use_test_context(source: str = "test", *, root: Path | None = None) -> Round:
    """切到测试/开发上下文（``log_test/``），并开一轮记录本次调用。"""
    global _mode
    with _state_lock:
        _mode = MODE_TEST
    return begin(source, root=root if root is not None else _test_root())


def use_production_context(source: str = "task", **kwargs) -> Round:
    """显式切回生产上下文（正式任务入口用）。"""
    global _mode
    with _state_lock:
        _mode = MODE_PRODUCTION
    return begin(source, **kwargs)


def mark_task_started(*, config: str = "", task: str = "") -> None:
    """补记任务开始时间；不改轮次身份。"""
    with _state_lock:
        if _active is None:
            return
        if not _active.started_at:
            _active.started_at = _stamp()
        if config:
            _active.config = config
        if task:
            _active.task = task
        write_registry(_active)


def finish(outcome: str = "", *, reason: str = "") -> None:
    """本轮任务收尾：登记改成 closed（可回收），但文件继续留着写空闲日志。

    统计、性能根事件、最终状态和收尾日志必须先落盘刷新，再叫这一下。
    """
    with _state_lock:
        _finish_locked(outcome=outcome, reason=reason)


def _finish_locked(*, outcome: str = "", reason: str = "") -> None:
    if _active is None:
        return
    if outcome:
        _active.outcome = outcome
    if reason:
        _active.end_reason = reason
    if _active.state == "open":
        _active.state = "closed"
        _active.ended_at = _stamp()
    write_registry(_active)


def close_active() -> None:
    """切换下一轮或退出程序：刷新并关闭文件、释放轮次占用。"""
    global _active, _active_fd
    with _state_lock:
        if _active is None:
            return
        _finish_locked()
        round_ = _active
        fd, _active, _active_fd = _active_fd, None, None
    _detach_logging()
    _release_lock(fd)
    try:
        round_.lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _cleanup(*_args) -> None:
    close_active()


atexit.register(_cleanup)


# ------------------------------------------------------------------ 目录清理工具


def remove_round(root: Path) -> bool:
    """整轮删除。目录里还有一个占用锁就拒绝。"""
    root = Path(root)
    if is_occupied(root):
        return False
    try:
        shutil.rmtree(root)
    except OSError as exc:
        _log().warning("清理轮次失败，保留待重试：%s（%s）", root, exc)
        return False
    return True


def round_dirs(parent: Path) -> list[Path]:
    """父目录下符合命名规则、且确实有登记文件的目录（按名字=时间排序）。"""
    parent = Path(parent)
    if not parent.is_dir():
        return []
    found: list[Path] = []
    try:
        entries = list(parent.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir() or entry.is_symlink():
            continue
        if not NAME_PATTERN.match(entry.name):
            continue
        if not (entry / REGISTRY_NAME).is_file():
            continue
        found.append(entry)
    return sorted(found, key=lambda p: p.name)


__all__ = [
    "MODE_PRODUCTION",
    "MODE_TEST",
    "NAME_PATTERN",
    "REGISTRY_NAME",
    "Round",
    "ROUND_VERSION",
    "begin",
    "begin_task",
    "captures_dir",
    "close_active",
    "current",
    "current_root",
    "diagnostics_dir",
    "finish",
    "is_occupied",
    "mark_task_started",
    "mode",
    "read_registry",
    "remove_round",
    "round_dirs",
    "status_file",
    "use_production_context",
    "use_test_context",
    "write_registry",
]
