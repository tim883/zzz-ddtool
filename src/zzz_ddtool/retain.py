"""日志保留：纯按数量，超了删最旧的。

为什么只按数量、不做容量/天数：
    排错靠 debug 模式重跑复现，不靠翻旧日志回溯。
    所以留一堆图没意义，只要保证"想看刚跑完那轮时还在"就行。

为什么没有「失败永远保留」：
    同上。失败图留着也不会有人翻——发现问题就重跑 debug 复现。
    留着反而把正常的图挤没了。

    log/text/ddtool.log     文本日志（RotatingFileHandler 按份数轮转）
    log/captures/<时间戳>/  每轮截图，按总张数整轮回收
"""

from __future__ import annotations

from pathlib import Path

# 截图总张数上限。超了从最旧的轮次开始整轮删。
MAX_CAPTURE_FILES = 300
# 文本日志保留份数（每份 2MB）
MAX_LOG_FILES = 6

# 运行中生效的上限：日志设置里改过就是改后的值，否则是 MAX_CAPTURE_FILES。
# 只在本进程内生效（和日志级别一样不落盘），否则每次写完都要回收回默认值。
_max_files = MAX_CAPTURE_FILES


def get_max_files() -> int:
    """当前生效的截图上限。"""
    return _max_files


def set_max_files(value: int) -> None:
    """改截图上限（日志设置里「截图最多留」）。至少留 1 张。"""
    global _max_files
    _max_files = max(1, int(value))


def count_captures(directory: Path | None = None) -> int:
    """当前截图总张数（跨所有轮次）。"""
    from zzz_ddtool.paths import captures_dir

    root = directory or captures_dir()
    if not root.exists():
        return 0
    return sum(1 for p in root.rglob("*.png") if p.is_file())


def retain_captures(max_files: int | None = None) -> list[Path]:
    """按总张数回收截图：从最旧的轮次开始整轮删，删到不超上限为止。

    整轮删而不是逐张删——半截的轮次没有参考价值。
    max_files 不传就用当前生效上限（见 set_max_files）。
    """
    from zzz_ddtool.paths import captures_dir

    limit = _max_files if max_files is None else max_files
    root = captures_dir()
    if not root.exists():
        return []

    total = count_captures(root)
    if total <= limit:
        return []

    # 轮次目录按名字排序（名字是时间戳，字典序 = 时间序）
    rounds = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    removed: list[Path] = []
    for old in rounds:
        if total <= limit:
            break
        files = [p for p in old.rglob("*.png") if p.is_file()]
        try:
            for f in files:
                f.unlink()
            total -= len(files)
            removed.extend(files)
        except OSError:
            break
        try:
            old.rmdir()
        except OSError:
            pass
    return removed


def clear_captures() -> int:
    """清空所有截图（界面上「立即清理」用）。返回删掉的张数。"""
    from zzz_ddtool.paths import captures_dir

    root = captures_dir()
    if not root.exists():
        return 0
    removed = count_captures(root)
    for old in list(root.iterdir()):
        if old.is_dir():
            for f in old.rglob("*.png"):
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                old.rmdir()
            except OSError:
                pass
    return removed
