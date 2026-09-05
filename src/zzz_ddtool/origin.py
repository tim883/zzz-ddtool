from __future__ import annotations

from enum import Enum

from zzz_ddtool.models import Disc, UPGRADE_STEP


class Origin(str, Enum):
    THREE = "three"
    FOUR = "four"


def extra_bonus(disc: Disc) -> int:
    return sum(max(0, sub.rolls - 1) for sub in disc.substats)


def infer_origin(disc: Disc) -> Origin | None:
    """+0 靠词条条数；升过之后靠「强化次数 vs 词条上的 +n 总和」。

    对不上时返回 None，调用方应停机，不要猜着打标记。
    """
    events = max(0, disc.level // UPGRADE_STEP)
    bonus = extra_bonus(disc)
    if disc.level == 0 or disc.line_count < 4:
        if disc.line_count <= 3:
            return Origin.THREE
        return Origin.FOUR
    if bonus == events:
        return Origin.FOUR
    if bonus == events - 1:
        return Origin.THREE
    return None


def bonus_budget(origin: Origin, checkpoint: int) -> int:
    events = max(0, checkpoint // UPGRADE_STEP)
    if origin is Origin.FOUR:
        return events
    return max(0, events - 1)


def filled_count(origin: Origin, checkpoint: int) -> int:
    if origin is Origin.THREE and checkpoint == 0:
        return 3
    return 4
