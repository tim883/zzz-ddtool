from itertools import combinations

from zzz_ddtool.config import Preset
from zzz_ddtool.decide import decide
from zzz_ddtool.gamedata import SUBSTAT_NAMES, blocked_substats
from zzz_ddtool.models import Action, Disc, Substat
from zzz_ddtool.origin import Origin
from zzz_ddtool.patterns import JUNK, EMPTY, Pattern, Slot, canonicalize, score_slots
from zzz_ddtool.substatfilter import plan_substat_filter


WEIGHTS = {"暴击率": 1.0, "暴击伤害": 1.0, "穿透值": 0.8}


def pid(names, *, origin=Origin.THREE, level=0, weights=WEIGHTS):
    slots = [Slot(name) for name in names]
    while len(slots) < (3 if origin == Origin.THREE and level == 0 else 4):
        slots.append(Slot(JUNK))
    if len(slots) == 3:
        slots.append(Slot(EMPTY))
    slots = canonicalize(slots, weights)
    return Pattern(origin, level, slots, score_slots(slots, weights)).pattern_id


def test_double_crit_uses_two_through_four():
    preset = Preset(weights=WEIGHTS, accepted=(
        pid(("暴击率", "暴击伤害")),
        pid(("暴击率", "暴击伤害"), origin=Origin.FOUR),
    ))
    plan = plan_substat_filter(preset)
    assert set(plan.names) == {"暴击率", "暴击伤害"}
    # 只有 2 个词条时游戏只允许数量 ≤ 2：minimum..词条数 与 ≥minimum 等价（2026-10-11 真机）。
    assert plan.counts == (2,)


def test_alternatives_need_full_union_and_shared_minimum():
    preset = Preset(weights=WEIGHTS, accepted=(
        pid(("暴击率", "暴击伤害")), pid(("暴击率", "穿透值")),
    ))
    plan = plan_substat_filter(preset)
    assert set(plan.names) == set(WEIGHTS)
    assert plan.counts == (2, 3)
    # 游戏条件可以多筛，最终白名单仍然拒绝另一种两两组合。
    disc = Disc(0, (Substat("暴击伤害"), Substat("穿透值"), Substat("防御力")))
    assert plan.matches(sub.name for sub in disc.substats)
    assert decide(disc, preset, accepted_ids=preset.accepted).action == Action.MARK_DISCARD


def test_single_crit_remains_after_adding_second_crit():
    preset = Preset(weights=WEIGHTS, accepted=(pid(("暴击率",)), pid(("暴击伤害",))))
    plan = plan_substat_filter(preset)
    assert set(plan.names) == {"暴击率", "暴击伤害"}
    assert plan.counts == (1, 2)
    assert plan.matches(("暴击率", "生命值", "防御力"))
    assert plan.matches(("暴击率", "生命值", "防御力", "暴击伤害"))


def test_thresholds_work_without_manual_card_selection():
    preset = Preset(weights={"暴击率": 1, "暴击伤害": 1}, stage_require={"three": {0: 2}, "four": {0: 2}})
    plan = plan_substat_filter(preset)
    assert set(plan.names) == {"暴击率", "暴击伤害"}
    assert plan.minimum == 2


def test_both_origins_and_every_fourth_stat_are_covered():
    presets = [
        Preset(weights=WEIGHTS, accepted=(pid(("暴击率", "暴击伤害")), pid(("穿透值",), origin=Origin.FOUR))),
        Preset(weights=WEIGHTS, stage_require={"three": {0: 1.8}, "four": {0: 2.8}}),
        Preset(weights=WEIGHTS, min_useful_types=3),
        Preset(filter_slot=2, weights={**WEIGHTS, "攻击力": 1}, stage_require={"three": {0: 2}, "four": {0: 2}}),
    ]
    for preset in presets:
        plan = plan_substat_filter(preset)
        pool = set(SUBSTAT_NAMES) - blocked_substats(preset.filter_slot, preset.filter_mainstats)
        assert not set(plan.names) & blocked_substats(preset.filter_slot, preset.filter_mainstats)
        for count in (3, 4):
            for names in combinations(sorted(pool), count):
                disc = Disc(0, tuple(Substat(name) for name in names))
                if decide(disc, preset, accepted_ids=preset.accepted).action != Action.UPGRADE:
                    continue
                assert plan.matches(names), (plan, names)
                if count == 3:
                    for fourth in pool - set(names):
                        assert plan.matches((*names, fourth)), (plan, names, fourth)


def test_filter_is_derived_only_from_zero_level_choices():
    preset = Preset(weights=WEIGHTS, accepted=(pid(("暴击率", "暴击伤害")), pid(("穿透值",), level=6)))
    plan = plan_substat_filter(preset)
    assert set(plan.names) == {"暴击率", "暴击伤害"}
    assert plan.minimum == 2


def test_all_junk_or_no_zero_level_selection_keeps_existing_processing():
    for accepted in ((pid(()),), (), (pid(("暴击率",), level=6),)):
        plan = plan_substat_filter(Preset(weights=WEIGHTS, accepted=accepted))
        assert plan.names == ()
        assert plan.counts == ()
        assert plan.reason


def test_all_combinations_or_no_weights_do_not_add_filter():
    assert not plan_substat_filter(Preset(weights=WEIGHTS)).names
    assert not plan_substat_filter(Preset()).names


def test_non_substats_cannot_be_used_for_game_filter():
    preset = Preset(weights={"暴击率": 1, "能量自动回复": 100}, stage_require={"three": {0: 1}, "four": {0: 1}})
    assert plan_substat_filter(preset).names == ("暴击率",)
