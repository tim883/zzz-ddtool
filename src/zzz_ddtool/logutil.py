"""日志基座。

日志级别直接决定截图行为（标准 logging 语义，不是自定义开关）：

| 级别    | 行为                             |
|---------|----------------------------------|
| DEBUG   | 每一步都截图（一步一截，排查用） |
| INFO    | 默认。只记关键动作，不截图       |
| WARNING | 可疑情况自动截图                 |
| ERROR   | 失败自动截图                     |

想看一步步怎么走的，把级别调到 DEBUG 即可，不用改代码。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from zzz_ddtool.paths import logs_dir
from zzz_ddtool.retain import retain_logs

_configured = False
_level = logging.INFO

LEVEL_NAMES = {
    logging.DEBUG: "调试",
    logging.INFO: "信息",
    logging.WARNING: "警告",
    logging.ERROR: "错误",
    logging.CRITICAL: "严重",
}


class ChineseFormatter(logging.Formatter):
    """级别名换中文，输出形如：

    15:42:03 [信息] 仓库页 · 点开第2行第3列 → 成功
    """

    def format(self, record: logging.LogRecord) -> str:
        stamp = self.formatTime(record, "%H:%M:%S")
        level = LEVEL_NAMES.get(record.levelno, record.levelname)
        return f"{stamp} [{level}] {record.getMessage()}"


def setup(level: int | str = logging.INFO, *, console: bool = False) -> None:
    """初始化日志。可重复调用，后一次覆盖级别。"""
    global _configured, _level
    _level = level if isinstance(level, int) else getattr(logging, str(level).upper(), logging.INFO)
    folder = logs_dir()
    folder.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("zzz_ddtool")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    file_handler = RotatingFileHandler(
        folder / "ddtool.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=6,
        encoding="utf-8",
    )
    file_handler.setFormatter(ChineseFormatter())
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(ChineseFormatter())
        root.addHandler(stream)

    root.setLevel(_level)
    root.propagate = False
    _configured = True
    retain_logs(folder)


def set_level(level: int | str) -> None:
    """运行中改级别（界面上切 DEBUG 就能看一步步截图）。"""
    global _level
    _level = level if isinstance(level, int) else getattr(logging, str(level).upper(), _level)
    logging.getLogger("zzz_ddtool").setLevel(_level)


def level() -> int:
    return _level


def get_logger(name: str = "zzz_ddtool") -> logging.Logger:
    if not _configured:
        setup()
    return logging.getLogger(name)
