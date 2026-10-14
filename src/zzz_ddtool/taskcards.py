"""Shared task card selection and bounded, in-memory combination cache."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from threading import Lock

from zzz_ddtool.chain import TaskJob
from zzz_ddtool.gamedata import blocked_substats, possible_substats
from zzz_ddtool.origin import Origin
from zzz_ddtool.patterns import (
    CHECKPOINTS, EMPTY, JUNK, Pattern, Slot, canonicalize, enumerate_patterns,
    score_slots, useful_ordered,
)


def effective_weights(job: TaskJob) -> dict[str, float]:
    blocked = blocked_substats(job.slot, job.mainstats)
    return {name: value for name, value in job.weights.items() if name not in blocked}


def effective_required_substats(required_substats: Iterable[str],
                               blocked: Iterable[str]) -> tuple[str, ...]:
    """必带里被该号位主词条屏蔽的名字整条剔除（屏蔽集合见 `gamedata.blocked_substats`）。

    主词条占了这个名字，副词条永远不会出现它，拿它当必带是永远命中不了的死条件：
    初始 3 词会靠未出槽误放行，强化出第 4 词后又必然淘汰。剔掉之后剩下的必带照旧
    按「命中数 + 未出槽数 >= 必带数」判，剔空即不再按必带筛。判据与权重侧同一把尺子。

    只影响判定，不改保存的必带——主词条随时可改，界面里那一行照旧保留。
    """
    excluded = set(blocked)
    return tuple(name for name in required_substats if name not in excluded)


def has_required_substats(pattern: Pattern, required_substats: tuple[str, ...]) -> bool:
    """卡片是否满足必带：已出的命中数不够时，每个「未出」槽顶一个必带词条。

    「未出」（EMPTY）表示该槽尚未揭示，可能是任一副词条，所以初始 3 词 @+0 的
    卡片允许缺一个必带。判据是 `命中数 + 未出槽数 >= 必带数`。

    「废词条」是已经出来的词条、只是不参与计分，不算未知，不能顶。初始 4 词与
    强化后的档位已经出满 4 条，没有未出，等价于原来的「必须全部命中」。
    """
    if not required_substats:
        return True
    required = set(required_substats)
    names = {slot.name for slot in pattern.slots}
    if required <= names:
        return True
    empty = sum(1 for slot in pattern.slots if slot.name == EMPTY)
    return empty > 0 and len(required & names) + empty >= len(required)


def rebase_selection(job: TaskJob, *, filtered: bool = True,
                     cancelled: Callable[[], bool] | None = None) -> tuple[str, ...] | None:
    """The same effective whitelist for editing, counts, saving and execution.

    None means an ungenerated, skipped task; an explicit empty tuple stays empty.
    Changing useful stats can collapse multiple old cards to one canonical card.
    """
    if job.accepted is None:
        return None
    weights = effective_weights(job)
    useful = set(useful_ordered(weights))
    if not useful:
        return ()
    blocked = blocked_substats(job.slot, job.mainstats)
    required = effective_required_substats(job.required_substats, blocked)
    out = set()
    for i, pid in enumerate(job.accepted):
        if i % 128 == 0 and cancelled is not None and cancelled():
            raise InterruptedError
        try:
            origin, checkpoint, body = pid.split(":", 2)
            slots = []
            for token in body.split("|"):
                name, plus, bonus = token.rpartition("+")
                if not plus:
                    name, bonus = token, "0"
                slots.append(Slot(name if name in useful or name == EMPTY else JUNK, int(bonus)))
            canonical = canonicalize(slots, weights)
            pattern = Pattern(Origin(origin), int(checkpoint), canonical, score_slots(canonical, weights))
        except ValueError:
            continue
        if any(token.split("+", 1)[0] in blocked for token in body.split("|")):
            continue
        if not possible_substats(job.slot, job.mainstats, (token.split("+", 1)[0] for token in body.split("|"))):
            continue
        if filtered:
            if not has_required_substats(pattern, required):
                continue
            if pattern.checkpoint not in CHECKPOINTS:
                continue
            need = (job.stage_require.get(origin) or {}).get(pattern.checkpoint, 0.0)
            if pattern.score + 1e-9 < need:
                continue
        out.add(pattern.pattern_id)
    return tuple(sorted(out))


def counts_signature(job: TaskJob) -> tuple:
    weights = effective_weights(job)
    return (
        job.slot, tuple(sorted(job.mainstats)),
        tuple(sorted(blocked_substats(job.slot, job.mainstats))),
        tuple((name, float(weights[name])) for name in useful_ordered(weights)),
        tuple(float((job.stage_require.get(origin.value) or {}).get(level, 0))
              for origin in Origin for level in CHECKPOINTS),
        job.accepted,
        tuple(sorted(job.required_substats)),
    )


class PatternCatalog:
    """Reuse generated cards across previews and tasks; never cache partial work.

    UI cache reads take only the short data lock. Generation is serialized in workers,
    so two consumers of the same weights do not enumerate the same combinations twice.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple, dict] = OrderedDict()
        self._lock = Lock()
        self._build_lock = Lock()

    @staticmethod
    def key(weights: Mapping[str, float]) -> tuple:
        return tuple((name, float(weights[name])) for name in useful_ordered(weights))

    def peek(self, weights: Mapping[str, float]) -> dict | None:
        with self._lock:
            return self._entries.get(self.key(weights))

    def get(self, weights: Mapping[str, float], cancelled: Callable[[], bool],
            enumerator=None) -> dict | None:
        enumerator = enumerator or enumerate_patterns
        key = self.key(weights)
        while not self._build_lock.acquire(timeout=0.01):
            if cancelled():
                return None
        try:
            if cancelled():
                return None
            with self._lock:
                cached = self._entries.get(key)
                if cached is not None:
                    self._entries.move_to_end(key)
                    return cached
            grouped = {}
            for origin in Origin:
                for level in CHECKPOINTS:
                    if cancelled():
                        return None
                    raw = enumerator(dict(key), origin=origin, checkpoint=level, cancelled=cancelled) if key else []
                    if cancelled():
                        return None
                    grouped[(origin.value, level)] = tuple(raw)
            with self._lock:
                self._entries[key] = grouped
                # At most four weight configurations and about one maximum-size catalog.
                while len(self._entries) > 1 and (
                    len(self._entries) > 4 or
                    sum(len(cards) for groups in self._entries.values() for cards in groups.values()) > 60000
                ):
                    self._entries.popitem(last=False)
            return grouped
        finally:
            self._build_lock.release()


def task_counts(job: TaskJob, grouped: Mapping[tuple[str, int], tuple[Pattern, ...]],
                cancelled: Callable[[], bool] = lambda: False) -> dict[str, tuple[int, int]] | None:
    if job.accepted is None:
        return None
    accepted = rebase_selection(job, cancelled=cancelled)
    keep = frozenset(accepted)
    blocked = blocked_substats(job.slot, job.mainstats)
    required = effective_required_substats(job.required_substats, blocked)
    result = {}
    for origin in Origin:
        picked = qualified = 0
        for level in CHECKPOINTS:
            need = float((job.stage_require.get(origin.value) or {}).get(level, 0))
            for i, pattern in enumerate(grouped[(origin.value, level)]):
                if i % 128 == 0 and cancelled():
                    return None
                if has_required_substats(pattern, required) and pattern.score + 1e-9 >= need and possible_substats(
                    job.slot, job.mainstats, (slot.name for slot in pattern.slots)
                ):
                    qualified += 1
                    if pattern.pattern_id in keep:
                        picked += 1
        result[origin.value] = (picked, qualified)
    return result
