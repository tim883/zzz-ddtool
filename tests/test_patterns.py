from zzz_ddtool.models import Disc, Substat
from zzz_ddtool.origin import Origin, infer_origin
from zzz_ddtool.patterns import CHECKPOINTS, disc_to_pattern, enumerate_all, enumerate_patterns


def test_infer_three_vs_four_at_zero() -> None:
    three = Disc(level=0, substats=(Substat("暴击率"), Substat("暴击伤害"), Substat("生命值")))
    four = Disc(
        level=0,
        substats=(Substat("暴击率"), Substat("暴击伤害"), Substat("生命值"), Substat("防御力")),
    )
    assert infer_origin(three) is Origin.THREE
    assert infer_origin(four) is Origin.FOUR


def test_infer_after_upgrade_uses_bonus_budget() -> None:
    three = Disc(
        level=6,
        substats=(
            Substat("暴击率", 2),
            Substat("暴击伤害", 1),
            Substat("攻击力%", 1),
            Substat("生命值", 1),
        ),
    )
    four = Disc(
        level=6,
        substats=(
            Substat("暴击率", 2),
            Substat("暴击伤害", 2),
            Substat("攻击力%", 1),
            Substat("生命值", 1),
        ),
    )
    assert infer_origin(three) is Origin.THREE
    assert infer_origin(four) is Origin.FOUR


def test_four_liner_zero_collapses_junk() -> None:
    weights = {"暴击率": 1.0, "暴击伤害": 1.2, "攻击力%": 0.8}
    cards = enumerate_patterns(weights, origin=Origin.FOUR, checkpoint=0)
    ids = [card.pattern_id for card in cards]
    assert "four:0:暴击伤害|暴击率|废词条|废词条" in ids
    assert len(ids) == len(set(ids))


def test_disc_canonical_matches_enum() -> None:
    weights = {"暴击率": 1.0, "暴击伤害": 1.2, "攻击力%": 0.8}
    disc = Disc(
        level=0,
        substats=(
            Substat("生命值"),
            Substat("暴击率"),
            Substat("防御力"),
            Substat("暴击伤害"),
        ),
    )
    pattern = disc_to_pattern(disc, weights)
    assert pattern is not None
    assert pattern.pattern_id == "four:0:暴击伤害|暴击率|废词条|废词条"


def test_preview_enumerates_all_stages() -> None:
    weights = {"暴击率": 1.0, "暴击伤害": 1.2, "攻击力%": 0.8}
    cards = enumerate_all(weights, hide_junk_bonus=True)
    assert 15 in CHECKPOINTS
    assert {card.checkpoint for card in cards} == set(CHECKPOINTS)
    assert 40 <= len(cards) <= 800
