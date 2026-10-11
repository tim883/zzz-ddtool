"""从任务的组合选择推导单轮游戏筛选；不依赖截屏或输入。

游戏统计所选属性的命中种类数。始终选择 minimum..已选词条数（不超过 4），
保证新增第四词不会让盘子离开列表；超过词条数的数量按钮在游戏里是禁用态
（2026-10-11 真机确认），不能去点。枚举真实的 3/4 词组合评估范围，废词条不当成游戏属性。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from zzz_ddtool.config import Preset
from zzz_ddtool.decide import decide
from zzz_ddtool.gamedata import SUBSTAT_NAMES, blocked_substats
from zzz_ddtool.models import Action, Disc, Substat
from zzz_ddtool.patterns import useful_ordered


@dataclass(frozen=True)
class SubstatFilter:
    names: tuple[str, ...] = ()
    minimum: int = 0
    reason: str = ""
    candidates: int = 0
    total: int = 0

    @property
    def counts(self) -> tuple[int, ...]:
        if not self.names:
            return ()
        # 2026-10-11 真机（S5）：游戏只允许「命中数量 ≤ 已选词条数」，
        # 超过词条数的数量按钮是禁用态、点不动；minimum..上限 与 ≥minimum 等价。
        upper = min(4, len(self.names))
        if self.minimum > upper:
            return ()
        return tuple(range(self.minimum, upper + 1))

    def matches(self, names) -> bool:
        return not self.names or len(set(names) & set(self.names)) >= self.minimum

    def summary(self) -> str:
        if not self.names:
            return f"副词条不限：{self.reason}"
        return f"副词条 {'、'.join(self.names)}；命中数量 {'、'.join(map(str, self.counts))}"


def plan_substat_filter(preset: Preset) -> SubstatFilter:
    """穷举有效属性的子集，选覆盖所有目标且真实候选数最少的一轮条件。

    按任务的默认前提，0 级要求比高等级宽松；筛选条件只由 0 级推导。
    minimum..4 自动覆盖补出第四词后的状态，强化次数不影响命中数量。
    没有 0 级目标、全废组合、或条件不能减少候选时不加副词条筛选。
    """
    weights = preset.effective_weights()
    useful = useful_ordered(weights)
    if not useful:
        return SubstatFilter(reason="没有可用于筛选的有效副词条")
    blocked = blocked_substats(preset.filter_slot, preset.filter_mainstats)
    pool = tuple(name for name in SUBSTAT_NAMES if name not in blocked)
    bits = {name: 1 << i for i, name in enumerate(useful)}
    accepted = None if preset.accepted is None else set(preset.accepted)
    population: list[int] = []
    targets: set[int] = set()
    for count in (3, 4):
        for names in combinations(pool, count):
            mask = sum(bits.get(name, 0) for name in names)
            population.append(mask)
            disc = Disc(level=0, substats=tuple(Substat(name) for name in names))
            if decide(disc, preset, accepted_ids=accepted).action == Action.UPGRADE:
                targets.add(mask)
    total = len(population)
    if not targets:
        return SubstatFilter(reason="没有选中的 0 级组合，保留高等级处理范围", total=total)
    if 0 in targets:
        return SubstatFilter(reason="选中的组合包含全废词条，不能要求命中有效属性", total=total)
    best: tuple | None = None
    best_plan = SubstatFilter(reason="筛选不能缩小候选范围", candidates=total, total=total)
    for mask in range(1, 1 << len(useful)):
        minimum = min((target & mask).bit_count() for target in targets)
        if minimum == 0:
            continue
        candidates = sum((actual & mask).bit_count() >= minimum for actual in population)
        if candidates >= total:
            continue
        names = tuple(name for name in useful if bits[name] & mask)
        rank = (candidates, len(names), -minimum, names)
        if best is None or rank < best:
            best = rank
            best_plan = SubstatFilter(names, minimum, candidates=candidates, total=total)
    return best_plan
