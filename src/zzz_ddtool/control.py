"""将任务的暂停/停止检查传到导航、筛选等底层输入，隔离不同线程。"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable


_checkpoint: ContextVar[Callable[[], None] | None] = ContextVar("ddtool_checkpoint", default=None)


@contextmanager
def controlled_inputs(checkpoint: Callable[[], None]):
    token = _checkpoint.set(checkpoint)
    try:
        yield
    finally:
        _checkpoint.reset(token)


def checkpoint_input() -> None:
    callback = _checkpoint.get()
    if callback is not None:
        callback()
