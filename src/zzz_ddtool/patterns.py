from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations, combinations_with_replacement

from zzz_ddtool.origin import Origin, bonus_budget, filled_count, infer_origin
from zzz_ddtool.models import MAX_SUBSTAT_SLOTS, Disc

JUNK = "废词条"
EMPTY = "未出"
CHECKPOINTS = (0, 3, 6, 9, 12, 15)


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
    names = [name for name, weight in weights.items() if weight > 0]
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
    hide_junk_bonus: bool = True,
) -> list[Pattern]:
    kept = []
    for pattern in patterns:
        if hide_junk_bonus and _has_junk_bonus(pattern):
            continue
        if threshold is not None and pattern.score < threshold:
            continue
        kept.append(pattern)
    return kept


def enumerate_all(
    weights: Mapping[str, float],
    *,
    hide_junk_bonus: bool = True,
) -> list[Pattern]:
    """穷举全部阶段卡片，不过阶段门槛。门槛留给用户在每档手填。"""
    cards: list[Pattern] = []
    for origin in Origin:
        for checkpoint in CHECKPOINTS:
            raw = enumerate_patterns(weights, origin=origin, checkpoint=checkpoint)
            cards.extend(first_filter(raw, threshold=None, hide_junk_bonus=hide_junk_bonus))
    return cards


def enumerate_filtered(
    weights: Mapping[str, float],
    thresholds: Mapping[int, float] | None = None,
    *,
    hide_junk_bonus: bool = True,
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
            cards.extend(first_filter(raw, threshold=require, hide_junk_bonus=hide_junk_bonus))
    return cards


def _has_junk_bonus(pattern: Pattern) -> bool:
    return any(slot.name == JUNK and slot.bonus > 0 for slot in pattern.slots)


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
