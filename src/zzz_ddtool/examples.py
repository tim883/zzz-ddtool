"""随包示例的投放。

用户已确认的语义（方案 A）：示例是**用户自己的存档**，不是程序管理的模板。
所以这里只在首次运行时把包里缺的示例种进 ``user/plan/``，此后用户改、删都不再
干预；要拿新示例走界面「获取公共示例」的主动通道。

「只种一次」用 ``user/.examples-seeded`` 标记，与 ``userdata.migrate_plans()``
的 ``.plans-imported`` 是两条独立的生命周期。
"""

from __future__ import annotations

from pathlib import Path

from zzz_ddtool import paths
from zzz_ddtool.userdata import copy_missing

SEEDED_MARKER = ".examples-seeded"


def shipped_examples() -> list[Path]:
    """包里自带的示例：``data/examples/*.yaml``，按文件名排序。"""
    source = paths.data_dir() / "examples"
    if not source.is_dir():
        return []
    return sorted(source.glob("*.yaml"))


def seed_examples() -> list[Path]:
    """把尚未种下的示例复制进 ``user/plan/``，返回本次新复制的目标路径。

    标记存在即直接返回：用户删掉的示例不复活，用户改过的示例不覆盖。
    磁盘失败不抛给界面 —— 投放失败不该让客户端起不来。
    """
    marker = paths.user_dir() / SEEDED_MARKER
    if marker.exists():
        return []

    copied: list[Path] = []
    for source in shipped_examples():
        target = paths.plans_dir() / source.name
        try:
            if target.exists():
                continue
            copy_missing(source, target)
        except OSError:
            continue
        copied.append(target)

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError:
        pass
    return copied
