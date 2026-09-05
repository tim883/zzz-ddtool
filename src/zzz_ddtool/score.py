from __future__ import annotations

from collections.abc import Mapping

from zzz_ddtool.models import Disc, Substat


def score_disc(disc: Disc, weights: Mapping[str, float]) -> float:
    return round(sum(score_substat(sub, weights) for sub in disc.substats), 4)


def score_substat(sub: Substat, weights: Mapping[str, float]) -> float:
    weight = float(weights.get(sub.name, 0.0))
    if weight <= 0:
        return 0.0
    rolls = max(1, sub.rolls)
    return weight * rolls
