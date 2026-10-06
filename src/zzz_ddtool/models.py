from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# S 级驱动盘副词条最多 4 条。初始可能是 3 条或 4 条。
MAX_SUBSTAT_SLOTS = 4
MAX_LEVEL = 15
UPGRADE_STEP = 3


def next_stage_level(level: int) -> int:
    """升一阶后应到达的等级（3 / 6 / 9 / 12 / 15）。

    强化页「阶段添加」按整个阶段补素材：从档内任意等级（0、1、2）出发都补到本档
    末尾，所以期望是「本档末尾」而不是 level + 3。素材不够吃满一档时游戏只加到
    部分经验，等级停在期望值之前——那正是「素材不足」的现场。
    """
    return min((max(0, level) // UPGRADE_STEP) * UPGRADE_STEP + UPGRADE_STEP, MAX_LEVEL)


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
    # 由界面读出的稀有度（S/A/B）；读不出时为 None——**不假定是 S**。
    # require_s_rank 只在确认「不是 S」时跳过，未知按不跳过处理（宁可不跳，不误跳好盘）。
    rarity: str | None = None
    # 锁定/弃置由仓库侧栏的 R/T 提示读出：True/False 为已确认状态，None = 未知。
    # 未知不默认成 False（未标记）：解析层读不到时显式传 None，要按对应跳过规则
    # 处理时必须先重读确认。这里默认 False 只服务不读界面的合成场景（测试、预览）。
    locked: bool | None = False
    equipped: bool = False
    discarded: bool | None = False

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
