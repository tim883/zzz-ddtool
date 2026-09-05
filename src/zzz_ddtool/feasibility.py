from __future__ import annotations

from collections.abc import Collection

from zzz_ddtool.models import Disc


def present_useful_types(disc: Disc, useful_names: Collection[str]) -> frozenset[str]:
    useful = set(useful_names)
    return frozenset(sub.name for sub in disc.substats if sub.name in useful)


def max_achievable_useful_types(disc: Disc, useful_names: Collection[str]) -> int:
    """乐观估计：空坑全部滚到尚未出现的有效词条。

    4 词条盘没有空坑，后面强化只会加在已有词条上，有效种类不会再增加。
    """
    useful = set(useful_names)
    present = present_useful_types(disc, useful)
    missing = useful - present
    return len(present) + min(disc.empty_slots, len(missing))


def fails_useful_quota(disc: Disc, useful_names: Collection[str], min_useful_types: int) -> bool:
    if min_useful_types <= 0:
        return False
    return max_achievable_useful_types(disc, useful_names) < min_useful_types
