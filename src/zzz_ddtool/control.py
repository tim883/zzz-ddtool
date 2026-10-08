"""将任务的暂停/停止检查传到导航、筛选等底层输入，隔离不同线程。"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable


_checkpoint: ContextVar[Callable[[], int | None] | None] = ContextVar("ddtool_checkpoint", default=None)
_validation: ContextVar[Callable[[int | None], None] | None] = ContextVar("ddtool_input_validation", default=None)


@contextmanager
def controlled_inputs(checkpoint: Callable[[], int | None]):
    token = _checkpoint.set(checkpoint)
    try:
        yield
    finally:
        _checkpoint.reset(token)


@contextmanager
def validated_inputs(validate: Callable[[], None], *, reuse_unchanged: bool = False):
    """只在当前输入作用域内，控制检查返回后复验，再允许底层继续输入。"""
    validated_generation = None

    def run(generation: int | None) -> None:
        nonlocal validated_generation
        known = type(generation) is int
        if reuse_unchanged and known and validated_generation == generation:
            return
        validate()
        validated_generation = generation if known else None

    token = _validation.set(run)
    try:
        yield
    finally:
        _validation.reset(token)


def checkpoint_input() -> None:
    callback = _checkpoint.get()
    generation = callback() if callback is not None else None
    validate = _validation.get()
    if validate is not None:
        validate(generation)
