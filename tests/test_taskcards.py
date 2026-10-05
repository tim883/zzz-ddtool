from dataclasses import replace

from zzz_ddtool.chain import TaskJob
from zzz_ddtool.config import Preset
from zzz_ddtool.decide import decide
from zzz_ddtool.gamedata import SUBSTAT_NAMES
from zzz_ddtool.models import Action, Disc, Substat
from zzz_ddtool.patterns import CHECKPOINTS, EMPTY, JUNK, enumerate_filtered
from zzz_ddtool.taskcards import PatternCatalog, effective_weights, rebase_selection, task_counts


def test_multi_mainstats_preserve_cross_main_substat_weights_and_filter_impossible_cards():
    from zzz_ddtool.gui import qualifying_pattern_ids
    from zzz_ddtool.taskcards import counts_signature

    job = TaskJob(slot=4, mainstats=("攻击力%", "暴击率"), weights={
        "攻击力%": 2, "暴击率": 3, "暴击伤害": 1,
    }, accepted=())
    assert effective_weights(job) == job.weights
    assert effective_weights(replace(job, mainstats=("攻击力%",))) == {"暴击率": 3, "暴击伤害": 1}
    grouped = PatternCatalog().get(effective_weights(job), lambda: False)
    qualified = qualifying_pattern_ids(job.slot, job.mainstats, job.weights)
    job.accepted = qualified
    assert any("攻击力%" in pid for pid in qualified)
    assert any("暴击率" in pid for pid in qualified)
    assert not any("攻击力%" in pid and "暴击率" in pid for pid in qualified)
    assert set(rebase_selection(job)) == set(qualified)
    for origin, (chosen, total) in task_counts(job, grouped).items():
        assert chosen == total == sum(pid.startswith(origin + ":") for pid in qualified)
    assert counts_signature(job) != counts_signature(replace(job, mainstats=("攻击力%", "生命值%")))


def test_multi_mainstats_runtime_accepts_both_possible_substat_families():
    from zzz_ddtool.gui import qualifying_pattern_ids

    weights = {"攻击力%": 2, "暴击率": 3, "暴击伤害": 1}
    job = TaskJob(slot=4, mainstats=("攻击力%", "暴击率"), weights=weights)
    accepted = qualifying_pattern_ids(job.slot, job.mainstats, weights)
    preset = Preset(weights=weights, filter_slot=4, filter_mainstats=job.mainstats, skip_locked=False)
    for relevant in ("攻击力%", "暴击率"):
        disc = Disc(0, (Substat(relevant), Substat("暴击伤害"), Substat("防御力"), Substat("生命值")))
        decision = decide(disc, preset, accepted)
        assert decision.action == Action.UPGRADE
        assert decision.score == weights[relevant] + 1


def test_counts_distinguish_ungenerated_and_explicit_empty_selection():
    job = TaskJob(slot=4, weights={"暴击率": 1, "暴击伤害": 0.5},
                  stage_require={"three": {0: 1.5}, "four": {0: 1.5}})
    catalog = PatternCatalog().get(effective_weights(job), lambda: False)
    expected = enumerate_filtered(effective_weights(job), stage_require=job.stage_require)
    assert task_counts(job, catalog) is None
    job.accepted = tuple(pattern.pattern_id for pattern in expected)
    counts = task_counts(job, catalog)
    for origin in ("three", "four"):
        total = sum(p.origin.value == origin for p in expected)
        assert counts[origin] == (total, total)
        assert task_counts(replace(job, accepted=()), catalog)[origin] == (0, total)
    chosen = tuple(p.pattern_id for p in expected if p.origin.value == "four")[:2]
    assert task_counts(replace(job, accepted=chosen), catalog)["four"] == (2, counts["four"][1])


def test_selection_rebases_deduplicates_and_filters_using_current_task():
    job = TaskJob(slot=4, weights={"暴击率": 1}, accepted=(
        "four:0:暴击率|攻击力%|废词条|废词条",
        "four:0:暴击率|暴击伤害|废词条|废词条",
        "four:3:暴击率|废词条+1|废词条|废词条",
    ))
    assert rebase_selection(job) == ("four:0:暴击率|废词条|废词条|废词条",)
    job.mainstats = ("攻击力%",)
    assert rebase_selection(job) == ("four:0:暴击率|废词条|废词条|废词条",)
    job.stage_require = {"four": {0: 1.5}}
    assert rebase_selection(job) == ()
    assert rebase_selection(replace(job, accepted=None)) is None


def test_catalog_reuses_weights_and_does_not_cache_cancelled_generation():
    from zzz_ddtool.patterns import enumerate_patterns

    calls = []
    stopped = False

    def generate(weights, **kwargs):
        calls.append(1)
        return enumerate_patterns(weights, **kwargs)

    catalog = PatternCatalog()
    first = catalog.get({"暴击率": 1}, lambda: False, generate)
    assert len(calls) == 12
    assert catalog.get({"暴击率": 1, "不存在": 2, "暴击伤害": 0}, lambda: False, generate) is first
    assert len(calls) == 12

    def interrupted(weights, **kwargs):
        nonlocal stopped
        stopped = True
        return []

    assert catalog.get({"暴击率": 2}, lambda: stopped, interrupted) is None
    assert catalog.peek({"暴击率": 2}) is None
    catalog.get({"暴击率": 2}, lambda: False, generate)
    assert len(calls) == 24


def test_card_totals_match_runtime_decisions_across_all_stages():
    job = TaskJob(slot=4, weights={"暴击率": 1, "暴击伤害": 0.5},
                  stage_require={origin: {level: level / 3 + 0.5 for level in CHECKPOINTS}
                                 for origin in ("three", "four")})
    grouped = PatternCatalog().get(effective_weights(job), lambda: False)
    chosen = tuple(p.pattern_id for cards in grouped.values() for p in cards if p.score >= 2)
    all_qualified = enumerate_filtered(effective_weights(job), stage_require=job.stage_require)
    for raw in (replace(job, accepted=tuple(p.pattern_id for p in all_qualified)),
                replace(job, accepted=chosen), replace(job, accepted=())):
        effective = replace(raw, accepted=rebase_selection(raw))
        preset = Preset(weights=effective.weights, stage_require=effective.stage_require,
                        require_s_rank=False, skip_locked=False, skip_equipped=False)
        expected = {"three": 0, "four": 0}
        for (origin, level), patterns in grouped.items():
            for pattern in patterns:
                junk = iter(name for name in SUBSTAT_NAMES if name not in effective.weights)
                subs = tuple(Substat(next(junk) if slot.name == JUNK else slot.name, rolls=slot.bonus + 1)
                             for slot in pattern.slots if slot.name != EMPTY)
                decision = decide(Disc(level=level, substats=subs), preset, effective.accepted)
                assert decision.pattern_id == pattern.pattern_id
                if decision.action in (Action.UPGRADE, Action.SKIP):
                    expected[origin] += 1
        assert {origin: pair[0] for origin, pair in task_counts(raw, grouped).items()} == expected
