"""日志设置的持久化：日志级别 + 历史日志保留轮数。

以前这两样都只是「本次启动内生效」——界面里填的数字、开的详细模式，
关掉再开又变回默认。现在读写落盘统一归这个模块（``user/log-settings.json``）：

    logutil 管级别、retain 管轮数，两边各管各的运行时状态，
    谁都不自己碰文件——两处各写一份迟早互相覆盖。

两边仍是模块状态（进程内生效），启动时 ``restore()`` 把存的值装回去。

旧配置只有 ``max_files``（截图张数上限）。张数和轮数不是一个量纲，
300 张不能读成 300 轮，一律按新默认值处理；下次保存时写成新格式。
"""

from __future__ import annotations

import json
import logging

from zzz_ddtool.paths import log_settings_file
from zzz_ddtool.retain import (
    DEFAULT_MAX_ROUNDS,
    MAX_KEEP_ROUNDS,
    MIN_KEEP_ROUNDS,
)

#: 配置文件格式版本。1 = 只有 max_files 的旧格式；2 = level + max_rounds。
CONFIG_VERSION = 2

DEFAULT_LEVEL = logging.INFO
DEFAULT_KEEP_ROUNDS = DEFAULT_MAX_ROUNDS

# 界面可选的级别档位。存了别的 level（比如手改文件写成 ERROR）当没存过。
_LEVELS = (logging.DEBUG, logging.INFO, logging.WARNING)


def _pick_level(value: object) -> int:
    try:
        level = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_LEVEL
    return level if level in _LEVELS else DEFAULT_LEVEL


def _pick_rounds(value: object) -> int:
    try:
        keep = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_KEEP_ROUNDS
    return min(MAX_KEEP_ROUNDS, max(MIN_KEEP_ROUNDS, keep))


def load() -> tuple[int, int]:
    """读上次的（级别, 历史轮数上限）。

    没存过、文件坏了、值不合法、还是旧格式，一律走新默认档——日志设置里
    读不出来顶多回到常规 / 默认轮数，绝不能让程序起不来。
    """
    try:
        raw = json.loads(log_settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_LEVEL, DEFAULT_KEEP_ROUNDS
    if not isinstance(raw, dict):
        return DEFAULT_LEVEL, DEFAULT_KEEP_ROUNDS
    level = _pick_level(raw.get("level"))
    version = raw.get("version")
    if not isinstance(version, int) or version < CONFIG_VERSION:
        # 旧格式（只有 max_files）或没有版本：张数不当轮数用，按新默认值。
        return level, DEFAULT_KEEP_ROUNDS
    return level, _pick_rounds(raw.get("max_rounds"))


def save(level: int, keep_rounds: int) -> None:
    """落盘。写失败不影响使用——顶多下次回到默认档。"""
    try:
        target = log_settings_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "version": CONFIG_VERSION,
                    "level": int(level),
                    "max_rounds": int(keep_rounds),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def apply(level: int, keep_rounds: int) -> None:
    """装进运行时 + 落盘。"""
    from zzz_ddtool.logutil import set_level
    from zzz_ddtool.retain import set_max_rounds

    set_level(level)
    set_max_rounds(keep_rounds)
    save(level, keep_rounds)


def restore() -> tuple[int, int]:
    """启动时把上次存的设置装回去，并按轮数收一次旧日志。

    只装运行时状态 + 触发一次回收；回收只动已关闭的历史轮次，
    正在写入的轮次（含别的进程）不碰。
    """
    from zzz_ddtool.retain import retain_rounds

    level, keep_rounds = load()
    apply(level, keep_rounds)
    retain_rounds()
    return level, keep_rounds
