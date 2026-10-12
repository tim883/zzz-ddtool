"""日志基座。

这是 OCR + 模拟点击工具，每一步都截图是基础（见 trace.py），
不靠日志级别控制。级别只管"日志写多少"：

| 级别    | 行为                       |
|---------|----------------------------|
| DEBUG   | 额外打出每步的步骤号       |
| INFO    | 默认。每步一条「页面·动作」|
| WARNING | 可疑情况                   |
| ERROR   | 失败                       |

写到哪里（见 loground.py）：
    每条日志写进**当前轮次**的 ``ddtool.log``，不按大小分卷——一轮一个文件，
    回答"这轮发生了什么"不用跨文件拼。没有在建轮次时只往控制台写，不自己
    建目录：建轮次是显式操作。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_configured = False
_level = logging.INFO
_file_handler: logging.Handler | None = None
_file_path: Path | None = None

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


def _resolve(level: int | str) -> int:
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)


def _root_logger() -> logging.Logger:
    return logging.getLogger("zzz_ddtool")


def setup(level: int | str | None = None, *, console: bool = False) -> None:
    """初始化日志。可重复调用，后一次覆盖级别。

    不传 ``level`` 就沿用当前级别（``set_level`` 设过的），别把界面/配置文件
    里刚装好的级别又打回默认。
    """
    global _configured, _level, _file_handler, _file_path
    if level is not None:
        _level = _resolve(level)
    root = _root_logger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    _file_handler = None
    _file_path = None

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(ChineseFormatter())
        root.addHandler(stream)

    root.setLevel(_level)
    root.propagate = False
    _configured = True
    _attach_current()


def set_level(level: int | str) -> None:
    """运行中改级别（界面上切 DEBUG 就能看一步步截图）。"""
    global _level
    _level = _resolve(level)
    _root_logger().setLevel(_level)


def level() -> int:
    return _level


def use_round(round_) -> None:
    """把文件记录切到这一轮的 ``ddtool.log``（loground.begin/切换时调用）。"""
    if not _configured:
        setup()
    _install_file(Path(round_.log_path))


def detach_round() -> None:
    """关闭当前轮次的文件记录（切下一轮、退出时调用）。"""
    _drop_file()


def current_log_path() -> Path | None:
    """当前正在写的日志文件；没有就不返回路径（界面显示用）。"""
    return _file_path


def _attach_current() -> None:
    from zzz_ddtool import loground

    round_ = loground.current()
    if round_ is not None:
        _install_file(round_.log_path)


def _install_file(path: Path) -> None:
    global _file_handler, _file_path
    _drop_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    except OSError as exc:
        # 日志目录建不出来 / 不可写（共享根只读、权限不足）：报一声，程序照常跑
        _file_handler = None
        _file_path = None
        _root_logger().warning("日志文件打不开，本轮只写控制台：%s（%s）", path, exc)
        return
    handler.setFormatter(ChineseFormatter())
    _root_logger().addHandler(handler)
    _file_handler = handler
    _file_path = path


def _drop_file() -> None:
    global _file_handler, _file_path
    root = _root_logger()
    if _file_handler is not None:
        root.removeHandler(_file_handler)
        try:
            _file_handler.flush()
        except (OSError, ValueError):
            pass
        _file_handler.close()
    _file_handler = None
    _file_path = None


def get_logger(name: str = "zzz_ddtool") -> logging.Logger:
    if not _configured:
        setup()
    return logging.getLogger(name)
