"""日志设置的持久化：日志级别 + 截图张数上限。

以前这两样都只是「本次启动内生效」——界面里填的 3000 张、开的详细模式，
关掉再开又变回 300 / 常规。

读写落盘统一归这个模块（user/log-settings.json）：
    logutil 管级别、retain 管上限，两边各管各的运行时状态，
    谁都不自己碰文件——两处各写一份迟早互相覆盖。

两边仍是模块状态（进程内生效），启动时 restore() 把存的值装回去。
"""

from __future__ import annotations

import json
import logging

from zzz_ddtool.paths import log_settings_file
from zzz_ddtool.retain import MAX_CAPTURE_FILES

DEFAULT_LEVEL = logging.INFO
DEFAULT_MAX_FILES = MAX_CAPTURE_FILES

# 截图张数可填范围。界面上的 spinbox 也照这两个数（见 logsettings），别两处各写一个数字。
MIN_KEEP_FILES = 20
MAX_KEEP_FILES = 5000

# 界面可选的级别档位。存了别的 level（比如手改文件写成 ERROR）当没存过。
_LEVELS = (logging.DEBUG, logging.INFO, logging.WARNING)


def _pick_level(value: object) -> int:
    try:
        level = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_LEVEL
    return level if level in _LEVELS else DEFAULT_LEVEL


def _pick_keep(value: object) -> int:
    try:
        keep = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MAX_FILES
    return min(MAX_KEEP_FILES, max(MIN_KEEP_FILES, keep))


def load() -> tuple[int, int]:
    """读上次的（级别, 截图上限）。

    没存过、文件坏了、值不合法，一律走默认档——日志设置里
    读不出来顶多回到常规/300，绝不能让程序起不来。
    """
    try:
        raw = json.loads(log_settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_LEVEL, DEFAULT_MAX_FILES
    if not isinstance(raw, dict):
        return DEFAULT_LEVEL, DEFAULT_MAX_FILES
    return _pick_level(raw.get("level")), _pick_keep(raw.get("max_files"))


def save(level: int, keep: int) -> None:
    """落盘。写失败不影响使用——顶多下次回到默认档。"""
    try:
        target = log_settings_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"level": int(level), "max_files": int(keep)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def apply(level: int, keep: int) -> None:
    """装进运行时 + 落盘。"""
    from zzz_ddtool.logutil import set_level
    from zzz_ddtool.retain import set_max_files

    set_level(level)
    set_max_files(keep)
    save(level, keep)


def restore() -> tuple[int, int]:
    """启动时把上次存的设置装回去。

    只装运行时状态，不回收截图：回收等下一次存图时才发生（见 retain.py），
    别在启动这一刻就去删老截图。顺带把校验过的值写回一份，无害。
    """
    level, keep = load()
    apply(level, keep)
    return level, keep
