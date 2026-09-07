"""3 词 / 4 词来源推断——判错会直接导致误弃，属于高价值回归点。"""

from zzz_ddtool.models import Disc, Substat
from zzz_ddtool.origin import Origin, bonus_budget, extra_bonus, filled_count, infer_origin


def _disc(level: int, rolls: tuple[int, ...]) -> Disc:
    return Disc(
        level=level,
        substats=tuple(Substat(name=f"词条{i}", rolls=r) for i, r in enumerate(rolls)),
    )


def test_plus_zero_three_lines_is_three() -> None:
    assert infer_origin(_disc(0, (1, 1, 1))) is Origin.THREE


def test_plus_zero_four_lines_is_four() -> None:
    assert infer_origin(_disc(0, (1, 1, 1, 1))) is Origin.FOUR


def test_four_liner_bonus_equals_events() -> None:
    # +6 = 2 次强化，4 词条上共 +2 → 初始 4 词
    assert infer_origin(_disc(6, (2, 2, 1, 1))) is Origin.FOUR


def test_three_liner_bonus_is_one_short() -> None:
    # +6 = 2 次强化，但只有 +1 落在词条上（另一次用来出第 4 条）→ 初始 3 词
    assert infer_origin(_disc(6, (2, 1, 1, 1))) is Origin.THREE


def test_ambiguous_returns_none_and_must_stop() -> None:
    # +6 已经强化过 2 次，4 词条上却一条 +1 都没有 → 对不上任何一套，停机不猜
    assert infer_origin(_disc(6, (1, 1, 1, 1))) is None
    # 反过来：+3 只强化过 1 次，词条上却 +2，也说不通
    assert infer_origin(_disc(3, (3, 1, 1, 1))) is None


def test_extra_bonus_counts_only_extra_rolls() -> None:
    assert extra_bonus(_disc(0, (1, 1, 1, 1))) == 0
    assert extra_bonus(_disc(3, (2, 1, 1, 1))) == 1


def test_three_liner_has_one_less_bonus_budget() -> None:
    # 初始 3 词要花掉一次强化来出第 4 条，所以能加到词条上的次数少 1
    assert bonus_budget(Origin.FOUR, 6) == 2
    assert bonus_budget(Origin.THREE, 6) == 1
    assert bonus_budget(Origin.THREE, 0) == 0


def test_filled_slots_three_liner_plus_zero_has_empty_slot() -> None:
    assert filled_count(Origin.THREE, 0) == 3
    assert filled_count(Origin.THREE, 3) == 4
    assert filled_count(Origin.FOUR, 0) == 4
