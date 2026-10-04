from __future__ import annotations

from collections.abc import Collection

from zzz_ddtool.config import Preset, checkpoint_level
from zzz_ddtool.feasibility import fails_useful_quota, max_achievable_useful_types, present_useful_types
from zzz_ddtool.models import MAX_LEVEL, Action, Decision, Disc
from zzz_ddtool.origin import infer_origin
from zzz_ddtool.patterns import JUNK, disc_to_pattern
from zzz_ddtool.score import score_disc


def decide(disc: Disc, preset: Preset, accepted_ids: Collection[str] | None = None) -> Decision:
    weights = preset.effective_weights()
    useful = preset.useful_names()
    useful_now = len(present_useful_types(disc, useful))
    useful_max = max_achievable_useful_types(disc, useful)
    score = score_disc(disc, weights)
    checkpoint = checkpoint_level(disc.level)

    if preset.skip_locked and disc.locked:
        return _result(Action.SKIP, "locked", score, useful_now, useful_max, checkpoint)
    if preset.skip_equipped and disc.equipped:
        return _result(Action.SKIP, "equipped", score, useful_now, useful_max, checkpoint)
    if preset.skip_discarded and disc.discarded:
        return _result(Action.SKIP, "already_marked_discard", score, useful_now, useful_max, checkpoint)
    # 稀有度未知（None）按不跳过处理：宁可不跳，也不把读不准的盘误判成非 S 丢掉。
    if preset.require_s_rank and disc.rarity is not None and disc.rarity.upper() != "S":
        return _result(Action.SKIP, "not_s_rank", score, useful_now, useful_max, checkpoint)

    origin = infer_origin(disc)
    if origin is None:
        return _result(Action.STOP, "origin_ambiguous", score, useful_now, useful_max, checkpoint)

    pattern = disc_to_pattern(disc, weights, origin)
    if pattern is None:
        return _result(Action.STOP, "pattern_unreadable", score, useful_now, useful_max, checkpoint, origin.value)

    score = pattern.score
    checkpoint = pattern.checkpoint

    if accepted_ids is not None:
        if pattern.pattern_id not in accepted_ids:
            return _result(
                Action.MARK_DISCARD,
                "pattern_not_picked",
                score,
                useful_now,
                useful_max,
                checkpoint,
                origin.value,
                pattern.pattern_id,
            )
        if disc.level >= MAX_LEVEL:
            return _result(
                Action.SKIP,
                "keep_max_level",
                score,
                useful_now,
                useful_max,
                checkpoint,
                origin.value,
                pattern.pattern_id,
            )
        return _result(
            Action.UPGRADE,
            "picked_pattern",
            score,
            useful_now,
            useful_max,
            checkpoint,
            origin.value,
            pattern.pattern_id,
        )

    if fails_useful_quota(disc, useful, preset.min_useful_types):
        return _result(
            Action.MARK_DISCARD,
            (
                f"useful_types_impossible:{useful_now}+empty{disc.empty_slots}"
                f"<{preset.min_useful_types}(max{useful_max})"
            ),
            score,
            useful_now,
            useful_max,
            checkpoint,
            origin.value,
            pattern.pattern_id,
        )

    if any(slot.name == JUNK and slot.bonus > 0 for slot in pattern.slots):
        return _result(
            Action.MARK_DISCARD,
            "junk_bonus_hidden",
            score,
            useful_now,
            useful_max,
            checkpoint,
            origin.value,
            pattern.pattern_id,
        )

    required = preset.require_for(origin, checkpoint)
    if required is not None and score < required:
        return _result(
            Action.MARK_DISCARD,
            f"score {score} < require {required} at +{checkpoint} ({origin.value})",
            score,
            useful_now,
            useful_max,
            checkpoint,
            origin.value,
            pattern.pattern_id,
        )

    if disc.level >= MAX_LEVEL:
        return _result(
            Action.SKIP,
            "keep_max_level",
            score,
            useful_now,
            useful_max,
            checkpoint,
            origin.value,
            pattern.pattern_id,
        )

    return _result(
        Action.UPGRADE,
        f"score {score} >= require {required}",
        score,
        useful_now,
        useful_max,
        checkpoint,
        origin.value,
        pattern.pattern_id,
    )


def _result(
    action: Action,
    reason: str,
    score: float,
    useful_types: int,
    max_useful_types: int,
    checkpoint: int,
    origin: str | None = None,
    pattern_id: str | None = None,
) -> Decision:
    return Decision(
        action=action,
        reason=reason,
        score=score,
        useful_types=useful_types,
        max_useful_types=max_useful_types,
        checkpoint=checkpoint,
        origin=origin,
        pattern_id=pattern_id,
    )
