from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations, combinations_with_replacement

from zzz_ddtool.origin import Origin, bonus_budget, filled_count, infer_origin
from zzz_ddtool.models import MAX_SUBSTAT_SLOTS, Disc
from zzz_ddtool.gamedata import SUBSTAT_NAMES

JUNK = "废词条"
EMPTY = "未出"
CHECKPOINTS = (0, 3, 6, 9, 12, 15)

# 真正的副词条（固定 10 种）。权重表来自配置文件，历史配置里混进过「穿透率」这种
# 主属性名——不过滤的话它会被当有用词条参与穷举，凭空多出上千张游戏里不存在的组合。
_SUBSTAT_SET = frozenset(SUBSTAT_NAMES)


@dataclass(frozen=True)
class Slot:
    name: str
    bonus: int = 0

    def label(self) -> str:
        if self.name in (JUNK, EMPTY) and self.bonus <= 0:
            return self.name
        if self.bonus <= 0:
            return self.name
        return f"{self.name} +{self.bonus}"

    def token(self) -> str:
        if self.bonus <= 0:
            return self.name
        return f"{self.name}+{self.bonus}"


@dataclass(frozen=True)
class Pattern:
    origin: Origin
    checkpoint: int
    slots: tuple[Slot, ...]
    score: float

    @property
    def pattern_id(self) -> str:
        body = "|".join(slot.token() for slot in self.slots)
        return f"{self.origin.value}:{self.checkpoint}:{body}"


def useful_ordered(weights: Mapping[str, float]) -> tuple[str, ...]:
    """权重为正的词条名，按权重降序（同权重按名字）。

    只认真正的副词条（见 _SUBSTAT_SET）。这是最后一道闸：不管权重表从哪来
    ——配置文件的脏数据、界面上手敲的名字——非副词条一律不参与穷举，
    免得算出游戏里根本不存在的组合。JUNK/EMPTY 是占位符，也不该在这里出现。
    """
    names = [
        name for name, weight in weights.items() if weight > 0 and name in _SUBSTAT_SET
    ]
    return tuple(sorted(names, key=lambda name: (-float(weights[name]), name)))


def score_slots(slots: Sequence[Slot], weights: Mapping[str, float]) -> float:
    total = 0.0
    for slot in slots:
        if slot.name in (JUNK, EMPTY):
            continue
        weight = float(weights.get(slot.name, 0.0))
        if weight <= 0:
            continue
        total += weight * (1 + max(0, slot.bonus))
    return round(total, 4)


def canonicalize(slots: Sequence[Slot], weights: Mapping[str, float]) -> tuple[Slot, ...]:
    useful, junk, empty = [], [], []
    for slot in slots:
        if slot.name == EMPTY:
            empty.append(slot)
        elif slot.name == JUNK:
            junk.append(slot)
        else:
            useful.append(slot)
    useful.sort(key=lambda slot: (-float(weights.get(slot.name, 0.0)), slot.name, -slot.bonus))
    junk.sort(key=lambda slot: -slot.bonus)
    padded = [*useful, *junk, *empty]
    while len(padded) < MAX_SUBSTAT_SLOTS:
        padded.append(Slot(EMPTY, 0))
    return tuple(padded[:MAX_SUBSTAT_SLOTS])


def disc_to_pattern(disc: Disc, weights: Mapping[str, float], origin: Origin | None = None) -> Pattern | None:
    inferred = origin if origin is not None else infer_origin(disc)
    if inferred is None:
        return None
    from zzz_ddtool.config import checkpoint_level

    checkpoint = checkpoint_level(disc.level)
    useful = set(useful_ordered(weights))
    slots = []
    for sub in disc.substats:
        name = sub.name if sub.name in useful else JUNK
        slots.append(Slot(name, max(0, sub.rolls - 1)))
    if inferred is Origin.THREE and checkpoint == 0 and len(slots) < 4:
        slots.append(Slot(EMPTY, 0))
    canonical = canonicalize(slots, weights)
    return Pattern(inferred, checkpoint, canonical, score_slots(canonical, weights))


def enumerate_patterns(
    weights: Mapping[str, float],
    *,
    origin: Origin,
    checkpoint: int,
    cancelled: Callable[[], bool] | None = None,
) -> list[Pattern]:
    useful = useful_ordered(weights)
    filled = filled_count(origin, checkpoint)
    budget = bonus_budget(origin, checkpoint)
    empty_n = MAX_SUBSTAT_SLOTS - filled
    seen: set[str] = set()
    out: list[Pattern] = []
    max_useful = min(len(useful), filled)
    for count in range(0, max_useful + 1):
        for chosen in combinations(useful, count):
            if cancelled is not None and cancelled():
                return []
            kinds = list(chosen) + [JUNK] * (filled - count)
            for bonuses in _bonus_assignments(len(kinds), budget, kinds):
                raw = [Slot(kind, bonus) for kind, bonus in zip(kinds, bonuses, strict=True)]
                raw.extend(Slot(EMPTY, 0) for _ in range(empty_n))
                slots = canonicalize(raw, weights)
                pattern = Pattern(origin, checkpoint, slots, score_slots(slots, weights))
                if pattern.pattern_id in seen:
                    continue
                seen.add(pattern.pattern_id)
                out.append(pattern)
    out.sort(key=lambda item: (-item.score, item.pattern_id))
    return out


def first_filter(
    patterns: Sequence[Pattern],
    *,
    threshold: float | None = None,
) -> list[Pattern]:
    """只按分数门槛过滤，不动「强化落在废词条上」的组合。

    2026-10-14 起废词条上的强化按普通情况照常出卡：它是游戏里真实存在的盘面状态
    （`槽位 bonus>0`），把它藏起来等于在「本档要求」之外替用户多做一次判弃置。
    收不收这种盘由门槛与勾选表达：白给一次强化的盘，分数等于它上一档的样子，
    门槛抬到该收的分数之上它就自然落在门槛外。
    """
    kept = []
    for pattern in patterns:
        if threshold is not None and pattern.score < threshold:
            continue
        kept.append(pattern)
    return kept


def enumerate_all(weights: Mapping[str, float]) -> list[Pattern]:
    """穷举全部阶段卡片，不过阶段门槛。门槛留给用户在每档手填。"""
    cards: list[Pattern] = []
    for origin in Origin:
        for checkpoint in CHECKPOINTS:
            cards.extend(enumerate_patterns(weights, origin=origin, checkpoint=checkpoint))
    return cards


def enumerate_filtered(
    weights: Mapping[str, float],
    thresholds: Mapping[int, float] | None = None,
    *,
    stage_require: Mapping[str, Mapping[int, float]] | None = None,
) -> list[Pattern]:
    cards: list[Pattern] = []
    for origin in Origin:
        for checkpoint in CHECKPOINTS:
            raw = enumerate_patterns(weights, origin=origin, checkpoint=checkpoint)
            require = None
            if stage_require and origin.value in stage_require and checkpoint in stage_require[origin.value]:
                require = float(stage_require[origin.value][checkpoint])
            elif thresholds and checkpoint in thresholds:
                require = float(thresholds[checkpoint])
            cards.extend(first_filter(raw, threshold=require))
    return cards


def _bonus_assignments(n_slots: int, budget: int, kinds: Sequence[str]) -> list[tuple[int, ...]]:
    if n_slots == 0:
        return [()] if budget == 0 else []
    seen: set[tuple[int, ...]] = set()
    out: list[tuple[int, ...]] = []
    for combo in combinations_with_replacement(range(n_slots), budget):
        bonuses = [0] * n_slots
        for index in combo:
            bonuses[index] += 1
        canonical = _canon_bonus(tuple(bonuses), kinds)
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(tuple(bonuses))
    return out


def _canon_bonus(bonuses: tuple[int, ...], kinds: Sequence[str]) -> tuple[int, ...]:
    """废词条坑位不可分辨：只按种类分组后排序，避免同一组合重复出卡。"""
    grouped: dict[str, list[int]] = {}
    for kind, bonus in zip(kinds, bonuses, strict=True):
        grouped.setdefault(kind, []).append(bonus)
    parts = []
    for kind in sorted(grouped):
        parts.extend(sorted(grouped[kind], reverse=True))
    return tuple(parts)
