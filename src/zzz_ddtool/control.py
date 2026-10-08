"""将任务的暂停/停止检查传到导航、筛选等底层输入，隔离不同线程。"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable


_checkpoint: ContextVar[Callable[[], None] | None] = ContextVar("ddtool_checkpoint", default=None)
_validation: ContextVar[Callable[[], None] | None] = ContextVar("ddtool_input_validation", default=None)


@contextmanager
def controlled_inputs(checkpoint: Callable[[], None]):
    token = _checkpoint.set(checkpoint)
    try:
        yield
    finally:
        _checkpoint.reset(token)


@contextmanager
def validated_inputs(validate: Callable[[], None]):
    """只在当前输入作用域内，控制检查返回后复验，再允许底层继续输入。"""
    token = _validation.set(validate)
    try:
        yield
    finally:
        _validation.reset(token)


def checkpoint_input() -> None:
    callback = _checkpoint.get()
    if callback is not None:
        callback()
    validate = _validation.get()
    if validate is not None:
        validate()
