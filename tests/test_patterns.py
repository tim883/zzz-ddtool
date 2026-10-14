from zzz_ddtool.models import Disc, Substat
from zzz_ddtool.origin import Origin, infer_origin
from zzz_ddtool.patterns import (
    CHECKPOINTS,
    EMPTY,
    JUNK,
    disc_to_pattern,
    enumerate_all,
    enumerate_patterns,
    useful_ordered,
)


def test_useful_ordered_drops_non_substats() -> None:
    """权重表里的主属性名不能当有用词条。

    「穿透率」是 5 号位主属性，历史配置里被写进过 weights。不过滤的话它会被当成
    一个副词条参与穷举，凭空算出上千张游戏里根本不存在的组合。
    """
    weights = {"攻击力%": 1.0, "穿透率": 0.6, "暴击伤害": 0.5}
    assert useful_ordered(weights) == ("攻击力%", "暴击伤害")


def test_useful_ordered_drops_placeholders() -> None:
    """废词条/未出是占位符，不是可加权的词条。"""
    assert useful_ordered({JUNK: 1.0, EMPTY: 1.0, "暴击率": 1.0}) == ("暴击率",)


def test_polluted_weights_enumerate_identically_to_clean() -> None:
    """脏权重（多一条主属性）与干净权重的穷举结果必须逐张一致。

    这条是端到端的守卫：只要 useful_ordered 那道闸漏了，卡片数立刻对不上。
    """
    clean = {"攻击力%": 1.0, "暴击伤害": 0.5, "暴击率": 0.5}
    dirty = {**clean, "穿透率": 0.6}
    assert [p.pattern_id for p in enumerate_all(clean)] == [
        p.pattern_id for p in enumerate_all(dirty)
    ]


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
    cards = enumerate_all(weights)
    assert 15 in CHECKPOINTS
    assert {card.checkpoint for card in cards} == set(CHECKPOINTS)
    # 2026-10-14 起「强化落在废词条上」的组合照常出卡（见下一条用例），张数因此变大。
    assert len(cards) == 810


def test_enumeration_keeps_rolls_that_landed_on_junk() -> None:
    """废词条上的强化是游戏里真实存在的状态，必须出卡。

    2026-10-14 之前 ``first_filter(hide_junk_bonus=True)`` 把这些组合整张丢掉：运行时
    按盘面算出的图案查不到卡片 → 直接判弃置，而界面上没有任何地方能表达相反的意思。
    现在照常出卡，收不收由每档「本档要求」与勾选决定。
    """
    weights = {"暴击率": 1.0, "暴击伤害": 1.2, "攻击力%": 0.8}
    # 走**卡片库**（enumerate_all 会过 first_filter），而不是裸的 enumerate_patterns：
    # 被过滤掉的正是 first_filter 这一层，裸穷举前后都含这些组合，钉不住改动。
    cards = {card.pattern_id: card for card in enumerate_all(weights)}
    root = cards["four:0:暴击伤害|暴击率|攻击力%|废词条"]
    whiffed = cards["four:3:暴击伤害|暴击率|攻击力%|废词条+1"]

    # 白给的那次强化不加分：它的分数就是这张盘 +0 时的分数，门槛天然能把它筛掉。
    assert whiffed.score == root.score
