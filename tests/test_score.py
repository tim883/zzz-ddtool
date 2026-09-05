from zzz_ddtool.feasibility import max_achievable_useful_types
from zzz_ddtool.models import Disc, Substat
from zzz_ddtool.score import score_disc


USEFUL = {"暴击率", "暴击伤害", "攻击力%"}


def test_score_counts_rolls() -> None:
    disc = Disc(
        level=3,
        substats=(Substat("暴击率", rolls=2), Substat("暴击伤害", rolls=1)),
    )
    assert score_disc(disc, {"暴击率": 1.0, "暴击伤害": 1.2}) == 3.2


def test_empty_slot_can_add_one_useful_type() -> None:
    disc = Disc(
        level=0,
        substats=(Substat("暴击率"), Substat("暴击伤害"), Substat("生命值")),
    )
    assert max_achievable_useful_types(disc, USEFUL) == 3


def test_full_slots_cannot_gain_types() -> None:
    disc = Disc(
        level=0,
        substats=(
            Substat("暴击率"),
            Substat("暴击伤害"),
            Substat("生命值"),
            Substat("防御力"),
        ),
    )
    assert max_achievable_useful_types(disc, USEFUL) == 2
