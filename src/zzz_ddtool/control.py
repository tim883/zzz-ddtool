"""将任务的暂停/停止检查传到导航、筛选等底层输入，隔离不同线程。

另提供纯秒级的暂停区间查询（Spec §3.2）：入参必须为有限单调秒，越界抛 ValueError
且不调用 pauses 回调；查询只读、不阻塞。
"""

import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable

_checkpoint: ContextVar[Callable[[], int | None] | None] = ContextVar("ddtool_checkpoint", default=None)
_validation: ContextVar[Callable[[int | None], None] | None] = ContextVar("ddtool_input_validation", default=None)
_pauses: ContextVar[Callable[[float, float], tuple[tuple[float, float], ...]] | None] = ContextVar(
    "ddtool_pauses", default=None
)


@contextmanager
def controlled_inputs(
    checkpoint: Callable[[], int | None],
    *,
    pauses: Callable[[float, float], tuple[tuple[float, float], ...]] | None = None,
):
    token = _checkpoint.set(checkpoint)
    token_pauses = _pauses.set(pauses)
    try:
        yield
    finally:
        _pauses.reset(token_pauses)
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


def checkpoint_control() -> int | None:
    """只走控制回调并返回代次（暂停阻塞、停止传播与原语义一致），不触发输入前复验。

    供等待分片使用：纯睡眠期间反复检查停止/暂停，但不重复跑输入前 OCR 校验。
    """
    callback = _checkpoint.get()
    return callback() if callback is not None else None


def checkpoint_input() -> None:
    callback = _checkpoint.get()
    generation = callback() if callback is not None else None
    validate = _validation.get()
    if validate is not None:
        validate(generation)


def input_pause_intervals(since: float, until: float) -> tuple[tuple[float, float], ...]:
    """查询 [since, until] 内实际暂停区间（升序、不相交的单调秒元组）。

    入参必须是有限单调秒：非有限数、负数、since > until 或 until 越过当前单调时钟
    （容差 1 ms）都抛 ValueError，且不调用 pauses 回调；没有暂停回调时返回空区间。
    """
    if isinstance(since, bool) or isinstance(until, bool):
        raise ValueError("暂停查询入参不能是布尔值")
    for value in (since, until):
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"暂停查询入参必须是有限秒：{value!r}")
    since_f = float(since)
    until_f = float(until)
    if since_f < 0 or until_f < since_f:
        raise ValueError(f"暂停查询区间非法：[{since_f}, {until_f}]")
    if until_f > time.monotonic() + 0.001:
        raise ValueError(f"暂停查询 until 越过当前单调时钟（疑似误传纳秒）：{until_f}")
    pauses = _pauses.get()
    if pauses is None:
        return ()
    return tuple(tuple(span) for span in pauses(since_f, until_f))
