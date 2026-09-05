from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# S 级驱动盘副词条最多 4 条。初始可能是 3 条或 4 条。
MAX_SUBSTAT_SLOTS = 4
MAX_LEVEL = 15
UPGRADE_STEP = 3


class Action(str, Enum):
    UPGRADE = "upgrade"
    MARK_DISCARD = "mark_discard"
    SKIP = "skip"
    STOP = "stop"


@dataclass(frozen=True)
class Substat:
    """rolls=1 表示这条刚出现；2 表示出现后又强化了一次（界面上的 +1）。"""

    name: str
    rolls: int = 1


@dataclass(frozen=True)
class Disc:
    level: int
    substats: tuple[Substat, ...]
    rarity: str = "S"
    locked: bool = False
    equipped: bool = False
    discarded: bool = False

    @property
    def line_count(self) -> int:
        return len(self.substats)

    @property
    def empty_slots(self) -> int:
        return max(0, MAX_SUBSTAT_SLOTS - self.line_count)

    @property
    def is_four_liner(self) -> bool:
        return self.line_count >= MAX_SUBSTAT_SLOTS


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    score: float
    useful_types: int
    max_useful_types: int
    checkpoint: int
    origin: str | None = None
    pattern_id: str | None = None
