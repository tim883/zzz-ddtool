"""日志保留：按历史轮数整轮回收。

保留单位是**轮次目录**，不是张数：``log/<时间戳>/`` 整轮删，不留半截。
一轮里的主日志、报告、统计、性能、截图和诊断一起走，用户看到的就是
"最近 N 次运行还在"。

为什么只按轮数、不做容量/天数：
    排错靠 debug 模式重跑复现，不靠翻旧日志回溯。轮数用户能预期，
    容量随单轮规模浮动，反而说不清"还能留多久"。

谁不该被删：
    正在写入的轮次（本进程的当前轮，或别的进程还占着 ``.active`` 的轮次）
    另算，不占历史名额；全部收尾后名额才收敛。未登记目录、名字对不上的目录、
    目录联接/符号链接、越界路径一律不碰——清理不是递归清仓库。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from zzz_ddtool import loground

#: 默认保留最近多少轮已关闭的历史记录。
MAX_ROUNDS = 5
#: 配置里没写/旧格式时采用的默认值（与 MAX_ROUNDS 同一个数，名字分开只为读着清楚）。
DEFAULT_MAX_ROUNDS = MAX_ROUNDS
#: 界面可填范围（文本框，不超过 100）。
MIN_KEEP_ROUNDS = 1
MAX_KEEP_ROUNDS = 100

# 运行中生效的上限：日志设置里改过就是改后的值，否则是 MAX_ROUNDS。
# 只在本进程内生效（落盘见 logprefs.py）。
_max_rounds = MAX_ROUNDS


def _clamp(value: int) -> int:
    return min(MAX_KEEP_ROUNDS, max(MIN_KEEP_ROUNDS, int(value)))


def get_max_rounds() -> int:
    """当前生效的历史轮数上限。"""
    return _max_rounds


def set_max_rounds(value: int) -> None:
    """改历史轮数上限（日志设置里「历史日志保留最近 N 轮」）。"""
    global _max_rounds
    _max_rounds = _clamp(value)


def _root(root: Path | None) -> Path:
    if root is not None:
        return Path(root)
    from zzz_ddtool import paths

    return paths.log_root()


def _within(root: Path, candidate: Path) -> bool:
    """候选目录确实位于日志根里（挡住目录联接、符号链接和 .. 越界）。"""
    try:
        real_root = root.resolve(strict=True)
        real_candidate = candidate.resolve(strict=True)
    except OSError:
        return False
    if real_candidate == real_root:
        return False
    return real_root in real_candidate.parents


def registered(root: Path) -> bool:
    """这一轮是程序登记的轮次吗？（编号、登记文件、路径都要对得上）"""
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return False
    if not loground.NAME_PATTERN.match(root.name):
        return False
    record = loground.read_registry(root)
    if record is None:
        return False
    if record.get("id") != root.name:
        return False
    version = record.get("version")
    return isinstance(version, int) and 1 <= version <= loground.ROUND_VERSION


def iter_rounds(root: Path | None = None) -> list[Path]:
    """日志根下所有已登记的轮次目录，按创建顺序（名字=时间）排。"""
    parent = _root(root)
    return [p for p in loground.round_dirs(parent) if registered(p) and _within(parent, p)]


def is_history(root: Path) -> bool:
    """已关闭、且没有进程在写的轮次 = 可以回收的历史记录。"""
    record = loground.read_registry(root)
    if record is None or record.get("state") != "closed":
        return False
    return not loground.is_occupied(Path(root))


def history_rounds(root: Path | None = None) -> list[Path]:
    """可回收的历史轮次，最旧的在前。当前/他进程在写的不在其中。"""
    return [p for p in iter_rounds(root) if is_history(p)]


def count_rounds(root: Path | None = None) -> int:
    """历史轮数（不含正在写入的轮次）。"""
    return len(history_rounds(root))


def dir_size(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def usage(root: Path | None = None) -> tuple[int, int]:
    """(历史轮数, 日志根占用字节)。占用只用于展示，不是回收条件。"""
    parent = _root(root)
    return count_rounds(parent), dir_size(parent)


def retain_rounds(max_rounds: int | None = None, root: Path | None = None) -> list[Path]:
    """超出上限就从最旧的历史轮次整轮删。

    删不掉的留在原地、记一条原因，下一次触发重试；不把失败算进已回收数量。
    """
    from zzz_ddtool.logutil import get_logger

    limit = _max_rounds if max_rounds is None else _clamp(max_rounds)
    history = history_rounds(root)
    extra = len(history) - limit
    if extra <= 0:
        return []
    log = get_logger("zzz_ddtool.retain")
    removed: list[Path] = []
    for old in history[:extra]:
        if loground.remove_round(old):
            removed.append(old)
        else:
            log.warning("轮次没能清理，保留重试：%s", old)
    if removed:
        log.info("历史日志超过 %d 轮，清理了最旧的 %d 轮", limit, len(removed))
    return removed


def clear_history(root: Path | None = None) -> int:
    """清理全部已关闭的历史轮次，返回实际删掉的轮数（跳过和失败的不算）。"""
    removed = 0
    for old in history_rounds(root):
        if loground.remove_round(old):
            removed += 1
    return removed
