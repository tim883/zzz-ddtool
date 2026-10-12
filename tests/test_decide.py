from zzz_ddtool.config import preset_from_dict
from zzz_ddtool.decide import decide
from zzz_ddtool.models import Action, Disc, Substat


def _preset(**overrides):
    data = {
        "name": "t",
        "weights": {"暴击率": 1.0, "暴击伤害": 1.2, "攻击力%": 0.8},
        "stage_require": {
            "three": {0: 2.0, 3: 2.8},
            "four": {0: 2.0, 3: 2.8},
        },
        "min_useful_types": 3,
        "filters": {"skip_locked": True, "skip_equipped": True},
    }
    data.update(overrides)
    return preset_from_dict(data)


def test_four_liner_two_useful_cannot_reach_three() -> None:
    # 权重可以很高（双暴），但 4 词条只占了 2 个有效坑，第 3 种有效词条永远出不来。
    disc = Disc(
        level=0,
        substats=(
            Substat("暴击率"),
            Substat("暴击伤害"),
            Substat("生命值"),
            Substat("防御力"),
        ),
    )
    result = decide(disc, _preset())
    assert result.action is Action.MARK_DISCARD
    assert "useful_types_impossible" in result.reason
    assert result.score == 2.2


def test_three_liner_two_useful_still_has_a_slot() -> None:
    disc = Disc(
        level=0,
        substats=(
            Substat("暴击率"),
            Substat("暴击伤害"),
            Substat("生命值"),
        ),
    )
    result = decide(disc, _preset())
    assert result.action is Action.UPGRADE
    assert result.max_useful_types == 3


def test_three_liner_one_useful_cannot_reach_three() -> None:
    disc = Disc(
        level=0,
        substats=(
            Substat("暴击率"),
            Substat("生命值"),
            Substat("防御力"),
        ),
    )
    result = decide(disc, _preset())
    assert result.action is Action.MARK_DISCARD
    assert result.max_useful_types == 2


def test_skip_locked_and_equipped() -> None:
    locked = Disc(level=0, substats=(Substat("暴击率"),), locked=True)
    equipped = Disc(level=0, substats=(Substat("暴击率"),), equipped=True)
    preset = _preset()
    assert decide(locked, preset).action is Action.SKIP
    assert decide(equipped, preset).action is Action.SKIP


def test_score_below_threshold_marks_discard() -> None:
    disc = Disc(
        level=0,
        substats=(
            Substat("暴击率"),
            Substat("攻击力%"),
            Substat("异常精通"),
        ),
    )
    # 有效种类最多 2（空 1 坑），min_useful=2 时过坑位，但分数 1.8 < 2.0
    result = decide(disc, _preset(min_useful_types=2))
    assert result.action is Action.MARK_DISCARD
    assert "require" in result.reason


def test_four_liner_stage_require_is_per_checkpoint() -> None:
    disc = Disc(
        level=0,
        substats=(
            Substat("暴击率"),
            Substat("暴击伤害"),
            Substat("生命值"),
            Substat("防御力"),
        ),
    )
    preset = _preset(min_useful_types=0, stage_require={"four": {0: 3.0, 3: 5.0}, "three": {0: 3.0}})
    assert decide(disc, preset).action is Action.MARK_DISCARD


def test_plus_15_mismatch_marks_discard() -> None:
    disc = Disc(
        level=15,
        substats=(
            Substat("暴击率", 6),
            Substat("生命值", 1),
            Substat("防御力", 1),
            Substat("攻击力", 1),
        ),
    )
    preset = _preset(min_useful_types=0, stage_require={"four": {15: 9.0}, "three": {15: 9.0}})
    result = decide(disc, preset)
    assert result.action is Action.MARK_DISCARD
    assert result.checkpoint == 15


def test_second_pass_whitelist_overrides_min_useful() -> None:
    disc = Disc(
        level=0,
        substats=(
            Substat("暴击率"),
            Substat("暴击伤害"),
            Substat("生命值"),
            Substat("防御力"),
        ),
    )
    preset = _preset()
    keep = "four:0:暴击伤害|暴击率|废词条|废词条"
    assert decide(disc, preset, accepted_ids={keep}).action is Action.UPGRADE
    assert decide(disc, preset, accepted_ids=set()).action is Action.MARK_DISCARD


def test_non_s_rank_is_always_skipped() -> None:
    """非 S 一律跳过，没有开关；稀有度读不出（None）仍按不跳过处理。

    读不出就跳过会把好盘误丢，所以未知一律按不跳过处理（`Disc.rarity` 默认 None
    就是为此）。这是 B2「恒默认 S」的反面：既不蒙 S，也不拿未知当非 S。
    2026-10-12 起「跳过非S级」开关已删除，判定固定，不再看配置。
    """
    preset = _preset()
    subs = (Substat("暴击率"), Substat("暴击伤害"), Substat("攻击力%"))

    assert decide(Disc(level=0, substats=subs, rarity="A"), preset).reason == "not_s_rank"
    assert decide(Disc(level=0, substats=subs, rarity="B"), preset).reason == "not_s_rank"
    assert decide(Disc(level=0, substats=subs, rarity="S"), preset).reason != "not_s_rank"
    assert decide(Disc(level=0, substats=subs, rarity=None), preset).reason != "not_s_rank"
    assert decide(Disc(level=0, substats=subs, rarity="s"), preset).reason != "not_s_rank"


def test_legacy_require_s_rank_false_cannot_bring_non_s_back() -> None:
    """老存档写过 filters.require_s_rank: false，也不改变行为：非 S 仍然跳过。"""
    preset = preset_from_dict({"filters": {"require_s_rank": False, "skip_locked": False}})
    subs = (Substat("暴击率"), Substat("暴击伤害"), Substat("攻击力%"))

    assert decide(Disc(level=0, substats=subs, rarity="A"), preset).reason == "not_s_rank"
