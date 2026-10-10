"""单实例限制：同一台机器上只允许一个 GUI 实例。

锁文件放在**公共根**（``user_data_root()``）而不是版本目录，所以不同版本
目录里的 exe 也会互相拦住 —— 两个实例会共用同一份 ``log/`` 与
``run-status.json``，还会重复注册 F9/F10。

只由 GUI 入口使用；CLI（``run``、``read-disc`` 等）不参与，避免破坏开发
流程与 ``scripts/smoke_exe.py``。

陈旧的锁由 ``QLockFile`` 自身处理：它会读锁文件里的 PID 并检查该进程是否
仍在运行，进程已死则接管，因此崩溃不会永久锁死。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLockFile

from zzz_ddtool import paths

LOCK_NAME = ".zzz-ddtool.lock"


def lock_path() -> Path:
    """锁文件位置：公共根。换版本目录不会换锁，跨版本因此也互相拦住。"""
    return paths.user_data_root() / LOCK_NAME


def acquire() -> QLockFile | None:
    """拿到独占锁返回锁对象；已有实例在运行、或共享根不可写时返回 ``None``。

    共享根建不出来（只读位置、权限不足）时不抛异常 —— 单实例限制失败不该
    让界面起不来，由调用方决定提示。
    """
    try:
        lock_path().parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    lock = QLockFile(str(lock_path()))
    return lock if lock.tryLock(0) else None


def release(lock: QLockFile) -> None:
    lock.unlock()
