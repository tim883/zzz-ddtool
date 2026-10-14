from dataclasses import replace

from zzz_ddtool.chain import TaskJob
from zzz_ddtool.config import Preset
from zzz_ddtool.decide import decide
from zzz_ddtool.gamedata import SUBSTAT_NAMES
from zzz_ddtool.models import Action, Disc, Substat
from zzz_ddtool.patterns import CHECKPOINTS, EMPTY, JUNK, enumerate_filtered
from zzz_ddtool.taskcards import PatternCatalog, effective_weights, rebase_selection, task_counts


def test_required_substats_intersect_manual_choices_counts_and_bulk_qualifying():
    from zzz_ddtool.gui import qualifying_pattern_ids
    from zzz_ddtool.taskcards import counts_signature, has_required_substats

    job = TaskJob(slot=5, weights={"攻击力%": 1, "暴击率": 1, "暴击伤害": 0.5},
                  required_substats=("攻击力%", "暴击率"),
                  stage_require={"four": {0: 2.5}})
    catalog = PatternCatalog()
    grouped = catalog.get(job.weights, lambda: False)
    all_cards = [p for cards in grouped.values() for p in cards]
    expected = {p.pattern_id for p in all_cards if has_required_substats(p, job.required_substats)
                and p.score + 1e-9 >= job.stage_require.get(p.origin.value, {}).get(p.checkpoint, 0)}
    assert expected and len(expected) < len(all_cards)
    assert set(qualifying_pattern_ids(job.slot, job.mainstats, job.weights, job.stage_require,
                                     required_substats=job.required_substats)) == expected
    assert set(qualifying_pattern_ids(job.slot, job.mainstats, job.weights, job.stage_require, catalog,
                                     required_substats=job.required_substats)) == expected
    unchecked = next(iter(expected))
    job.accepted = tuple(p.pattern_id for p in all_cards if p.pattern_id != unchecked)
    assert set(rebase_selection(job)) == expected - {unchecked}
    assert set(rebase_selection(job, filtered=False)) == set(job.accepted)
    for origin, (picked, qualified) in task_counts(job, grouped).items():
        assert picked == sum(pid.startswith(origin + ":") for pid in expected - {unchecked})
        assert qualified == sum(pid.startswith(origin + ":") for pid in expected)
    assert counts_signature(job) != counts_signature(replace(job, required_substats=()))
    assert rebase_selection(replace(job, accepted=None)) is None
    assert rebase_selection(replace(job, accepted=())) == ()


def _card(*tokens):
    from zzz_ddtool.origin import Origin
    from zzz_ddtool.patterns import Pattern, Slot

    slots = []
    for token in tokens:
        name, plus, bonus = token.rpartition("+")
        slots.append(Slot(name if plus else token, int(bonus) if plus else 0))
    return Pattern(Origin.THREE, 0, tuple(slots), 0.0)


def test_has_required_substats_counts_each_unrevealed_slot_as_one_required_substat():
    """必带判定：命中数 + 未出槽数 >= 必带数；废词条是已出词条，不能顶。"""
    from zzz_ddtool.taskcards import has_required_substats

    required = ("攻击力%", "暴击率")
    assert has_required_substats(_card("攻击力%", "暴击率", "废词条", "废词条"), required)
    assert has_required_substats(_card("攻击力%", "废词条", "废词条", "未出"), required)
    assert has_required_substats(_card("暴击率", "废词条", "废词条", "未出"), required)
    assert not has_required_substats(_card("废词条", "废词条", "废词条", "未出"), required)
    assert not has_required_substats(_card("废词条", "废词条", "废词条", "废词条"), required)
    assert has_required_substats(_card("攻击力%", "废词条+1", "废词条", "废词条"), ("攻击力%",))

    four = ("攻击力%", "暴击率", "穿透值", "异常精通")
    assert not has_required_substats(_card("攻击力%", "暴击率", "穿透值", "废词条"), four)
    assert has_required_substats(_card("攻击力%", "暴击率", "穿透值", "未出"), four)
    assert not has_required_substats(_card("攻击力%", "暴击率", "废词条", "未出"), four)

    assert has_required_substats(_card("废词条", "废词条", "废词条", "未出"), ("攻击力%",))
    assert has_required_substats(_card("废词条", "废词条", "废词条", "废词条"), ())


def test_required_substat_zero_weight_only_passes_via_unrevealed_slot():
    """零单价词条永远不会以该名字出现在卡片上，仍按「未出槽顶一个」判。

    已出满 4 条的档位（初始 4 词、+3 及以上）没有未出槽，必带它恒不通过；
    只有初始 3 词 @+0 能靠那 1 个未出槽顶上，等强化出第 4 词再按严格条件判。
    """
    from zzz_ddtool.gui import qualifying_pattern_ids

    job = TaskJob(slot=4, weights={"攻击力%": 0, "暴击率": 1},
                  required_substats=("攻击力%",), accepted=("four:0:暴击率|废词条|废词条|废词条",))
    assert rebase_selection(job) == ()
    accepted = qualifying_pattern_ids(4, (), job.weights, required_substats=job.required_substats)
    assert accepted
    assert all(pid.startswith("three:0:") and "未出" in pid for pid in accepted)
    assert all("攻击力%" not in pid for pid in accepted)


def test_required_substat_equal_to_mainstat_is_dropped_everywhere():
    """必带与所主词条同名时整条剔除：该名字在盘子副词条里永远不会出现。

    判据与权重侧共用 blocked_substats——1/2/3 号位按固定主属性，4/5/6 号位只在
    主词条唯一确定时屏蔽；多选主词条时某主属性在别的盘上仍能当副词条，不剔。
    """
    from zzz_ddtool.gamedata import blocked_substats
    from zzz_ddtool.gui import qualifying_pattern_ids
    from zzz_ddtool.taskcards import effective_required_substats

    # 4/5/6 号位：唯一主词条时才屏蔽；勾多个主词条时不屏蔽。
    assert effective_required_substats(("异常精通",), blocked_substats(4, ("异常精通",))) == ()
    assert effective_required_substats(("攻击力%", "暴击率"),
                                       blocked_substats(6, ("攻击力%",))) == ("暴击率",)
    assert effective_required_substats(("攻击力%", "暴击率"),
                                       blocked_substats(4, ("攻击力%", "暴击率"))) == ("攻击力%", "暴击率")
    # 1/2/3 号位主属性固定，与它同名的必带同样无效。
    assert effective_required_substats(("攻击力", "暴击率"), blocked_substats(2, ())) == ("暴击率",)

    # 端到端：主词条「异常精通」的 4 号位，必带「异常精通」等于没勾必带。
    weights = {"异常精通": 1, "暴击率": 1, "攻击力%": 1}
    dropped = qualifying_pattern_ids(4, ("异常精通",), weights, required_substats=("异常精通",))
    assert dropped and dropped == qualifying_pattern_ids(4, ("异常精通",), weights)
    # 出满 4 条的档位也一起放行——旧逻辑下这些卡会被死条件全部淘汰。
    assert any(pid.startswith("four:0:") for pid in dropped)
    # 与主词条不同名的必带照旧生效，只剩它一条。
    both = qualifying_pattern_ids(4, ("异常精通",), weights, required_substats=("异常精通", "暴击率"))
    only_crit = qualifying_pattern_ids(4, ("异常精通",), weights, required_substats=("暴击率",))
    assert both == only_crit
    assert all("暴击率" in pid or (pid.startswith("three:0:") and "未出" in pid) for pid in both)


def test_four_required_substats_pass_initial_three_only_by_one_unrevealed_slot():
    """4 个必带 + 初始 3 词 @+0：3 条已出必须全部命中，缺的那个由未出槽顶。

    缺 2 个以上仍不通过，未出槽不会整片放行初始 3 词；已出满 4 条时照旧全命中。
    """
    from zzz_ddtool.gui import qualifying_pattern_ids

    names = ("暴击率", "暴击伤害", "攻击力%", "穿透值")
    accepted = qualifying_pattern_ids(5, (), dict.fromkeys(names, 1), required_substats=names)
    assert accepted
    initial_three = [pid for pid in accepted if pid.startswith("three:0:")]
    assert initial_three
    for pid in accepted:
        tokens = {token.split("+", 1)[0] for token in pid.split(":", 2)[2].split("|")}
        if pid.startswith("three:0:"):
            assert "未出" in tokens
            assert len(set(names) - tokens) == 1
        else:
            assert set(names) <= tokens
    assert any(pid.startswith("four:0:") for pid in accepted)
    assert any(pid.startswith("three:3:") for pid in accepted)


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
    # 前两张归一成同一张；第三张是「强化落在废词条上」，2026-10-14 起不再被丢掉。
    assert rebase_selection(job) == (
        "four:0:暴击率|废词条|废词条|废词条",
        "four:3:暴击率|废词条+1|废词条|废词条",
    )
    job.mainstats = ("攻击力%",)
    assert rebase_selection(job) == (
        "four:0:暴击率|废词条|废词条|废词条",
        "four:3:暴击率|废词条+1|废词条|废词条",
    )
    # 只卡 +0：白给的那档没设门槛，它留在名单里
    job.stage_require = {"four": {0: 1.5}}
    assert rebase_selection(job) == ("four:3:暴击率|废词条+1|废词条|废词条",)
    # +3 也抬到 1.5：两张都落到门槛外——收不收这种盘完全由门槛表达
    job.stage_require = {"four": {0: 1.5, 3: 1.5}}
    assert rebase_selection(job) == ()
    assert rebase_selection(replace(job, accepted=None)) is None


def test_roll_that_landed_on_junk_follows_the_threshold_only():
    """废词条上的强化不再被写死判弃置：有白名单时按名单，没有时按门槛。

    2026-10-14 删掉了 decide 里写死的 ``junk_bonus_hidden`` 弃置分支——它会在
    「本档要求」之外再做一次判弃置，用户无法表达相反的意思。
    """
    weights = {"攻击力%": 1.0, "暴击伤害": 1.0, "穿透值": 0.5}
    # +9、四词条、三次强化：其中一次落在废词条「生命值」上，分数 4.5
    disc = Disc(9, (
        Substat("攻击力%", 3), Substat("暴击伤害", 1),
        Substat("穿透值", 1), Substat("生命值", 2),
    ))
    whiffed = "four:9:攻击力%+2|暴击伤害|穿透值|废词条+1"
    preset = Preset(weights=weights, skip_locked=False)

    assert decide(disc, preset, ()).action == Action.MARK_DISCARD      # 没勾 → 弃置
    assert decide(disc, preset, (whiffed,)).action == Action.UPGRADE   # 勾了 → 继续强化
    assert decide(disc, preset, (whiffed,)).pattern_id == whiffed

    # 没有名单时（命令行分数判定）只由门槛决定
    low = Preset(weights=weights, stage_require={"four": {9: 4.0}}, skip_locked=False)
    high = Preset(weights=weights, stage_require={"four": {9: 5.0}}, skip_locked=False)
    assert decide(disc, low, None).action == Action.UPGRADE
    assert decide(disc, low, None).pattern_id == whiffed
    assert decide(disc, high, None).action == Action.MARK_DISCARD


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
                        skip_locked=False, skip_equipped=False)
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
