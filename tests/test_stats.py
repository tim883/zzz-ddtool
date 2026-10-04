"""运行统计的聚合口径（数字错了比没有统计更坏）。

守的是三件事：
    1. 结果分类只按 decision 的动作词映射，认不出来不许瞎猜成「强化」；
    2. 「满级保留」= 强化过 **且** 最终等级 >= 15，别把没升过的算进去；
    3. 中途抛错的盘不能污染常规统计（最终等级分布、各档耗时）。
"""

from __future__ import annotations

import json
import logging

from zzz_ddtool import stats as S


def _log() -> logging.Logger:
    return logging.getLogger("test.stats")


def test_outcome_mapping_follows_action_words() -> None:
    assert S.outcome_for_action("upgrade") == S.OUTCOME_UPGRADED
    assert S.outcome_for_action("mark_discard") == S.OUTCOME_DISCARDED
    assert S.outcome_for_action("skip") == S.OUTCOME_SKIPPED
    # stop 不在映射里：保守按「跳过」记，绝不能猜成「强化」
    assert S.outcome_for_action("stop") == S.OUTCOME_SKIPPED
    assert S.outcome_for_action("something_new") == S.OUTCOME_SKIPPED


def test_counts_split_by_outcome() -> None:
    st = S.RunStats(tag="t")
    a = st.disc_begin("j1", 0, 0)
    st.disc_end(a, S.OUTCOME_UPGRADED, 6)
    b = st.disc_begin("j1", 1, 0)
    st.disc_end(b, S.OUTCOME_DISCARDED, 3)
    c = st.disc_begin("j1", 2, 15)
    st.disc_end(c, S.OUTCOME_SKIPPED, 15)
    d = st.disc_begin("j1", 3, 0)
    st.disc_end(d, S.OUTCOME_FAILED, 3)

    counts = st.outcome_counts()
    assert counts[S.OUTCOME_UPGRADED] == 1
    assert counts[S.OUTCOME_DISCARDED] == 1
    assert counts[S.OUTCOME_SKIPPED] == 1
    assert counts[S.OUTCOME_FAILED] == 1


def test_max_level_kept_needs_level_15_and_upgrade() -> None:
    """15 级但没升过（本来就是满级、判跳过）不算「满级保留」。"""
    st = S.RunStats(tag="t")
    upgraded_to_15 = st.disc_begin("j1", 0, 12)
    st.disc_stage(upgraded_to_15, 12, 15, 10.0)
    st.disc_end(upgraded_to_15, S.OUTCOME_UPGRADED, 15)
    already_15_skipped = st.disc_begin("j1", 1, 15)
    st.disc_end(already_15_skipped, S.OUTCOME_SKIPPED, 15)

    assert st.summary()["max_level_kept"] == 1


def test_disc_end_is_idempotent_so_multi_exit_paths_do_not_double_count() -> None:
    st = S.RunStats(tag="t")
    slot = st.disc_begin("j1", 0, 0)
    st.disc_end(slot, S.OUTCOME_UPGRADED, 6)
    st.disc_end(slot, S.OUTCOME_DISCARDED, 9)  # 多出口重复调用：以第一次为准

    assert st.outcome_counts()[S.OUTCOME_UPGRADED] == 1
    assert st.outcome_counts()[S.OUTCOME_DISCARDED] == 0
    assert st.discs[slot].level_end == 6


def test_stage_seconds_bucket_by_reached_level() -> None:
    st = S.RunStats(tag="t")
    a = st.disc_begin("j1", 0, 0)
    st.disc_stage(a, 0, 3, 10.0)
    st.disc_stage(a, 3, 6, 12.0)
    b = st.disc_begin("j1", 1, 0)
    st.disc_stage(b, 0, 3, 14.0)

    buckets = st.summary()["stage_seconds"]
    assert buckets["3"]["n"] == 2
    assert buckets["3"]["mean"] == 12.0
    assert buckets["3"]["min"] == 10.0
    assert buckets["3"]["max"] == 14.0
    assert buckets["6"]["n"] == 1


def test_unfinished_disc_stays_out_of_level_distribution() -> None:
    st = S.RunStats(tag="t")
    done = st.disc_begin("j1", 0, 0)
    st.disc_end(done, S.OUTCOME_UPGRADED, 3)
    st.disc_begin("j1", 1, 0)  # 中途抛错，没有 disc_end

    data = st.summary()
    assert data["level_distribution"] == {"3": 1}
    assert data["unfinished"] == 1


def test_scan_counts_are_per_job_and_deduped() -> None:
    """格位计数按任务分开算：每个任务独立序号空间，跨任务求和没有意义。"""
    st = S.RunStats(tag="t")
    st.job_begin(1, "任务1", 1, ("折枝剑歌",), ())
    st.scan(["c"] * 9, {0: "满级", 1: "满级"}, 1, max_index=8)
    st.scan(["c"] * 9, {0: "满级", 2: "已弃置"}, 2, max_index=8)
    st.job_end(10.0, 0)
    st.job_begin(2, "任务2", 2, (), ())
    st.scan(["c"] * 5, {0: "满级"}, 0, max_index=4)
    st.job_end(5.0, 0)

    jobs = st.summary()["jobs"]
    # 同一格位在相邻批次里会重复出现，跳过数要去重
    assert jobs[0]["slots"] == 9
    assert jobs[0]["skipped"] == 3
    assert jobs[0]["deferred"] == 2  # 取各批最大值
    assert jobs[1]["slots"] == 5
    assert jobs[1]["skipped"] == 1

    totals = st.summary()["totals"]
    assert totals["slots"] == 14
    assert totals["skipped"] == 4
    assert totals["jobs"] == 2


def test_scan_without_job_is_ignored() -> None:
    st = S.RunStats(tag="t")
    st.scan(["c"], {}, 0, max_index=0)
    assert st.summary()["jobs"] == []


def test_absorb_steps_tolerates_missing_spans() -> None:
    """测试里 trace 常是替身；拿不到步骤也不能把收尾整段打崩。"""
    st = S.RunStats(tag="t")
    st.absorb_steps(None)  # 不抛
    assert st.summary()["steps"] == {}
    assert st.summary()["long_actions"] == {}


def test_long_actions_and_steps_stay_in_separate_buckets() -> None:
    """长动作是真实墙钟，步骤是距上一步的间隔；两者会重叠，绝不能混成一张表。"""
    st = S.RunStats(tag="t")
    st.span("筛选", "设置游戏筛选", 30.0)
    st.absorb_steps([S.StepSpan("驱动盘仓库", "扫描列表", "", 1.0)])

    data = st.summary()
    assert "筛选 · 设置游戏筛选" in data["long_actions"]
    assert "筛选 · 设置游戏筛选" not in data["steps"]
    assert "驱动盘仓库 · 扫描列表" in data["steps"]
    assert "驱动盘仓库 · 扫描列表" not in data["long_actions"]


def test_actions_group_by_page_and_action() -> None:
    st = S.RunStats(tag="t")
    st.absorb_steps(
        [
            S.StepSpan("驱动盘仓库", "扫描列表", "", 1.0),
            S.StepSpan("驱动盘仓库", "扫描列表", "", 3.0),
            S.StepSpan("强化页", "强化", "", 2.0),
        ]
    )
    steps = st.summary()["steps"]
    assert steps["驱动盘仓库 · 扫描列表"]["n"] == 2
    assert steps["驱动盘仓库 · 扫描列表"]["total"] == 4.0
    assert steps["驱动盘仓库 · 扫描列表"]["min"] == 1.0
    assert steps["驱动盘仓库 · 扫描列表"]["max"] == 3.0
    assert steps["驱动盘仓库 · 扫描列表"]["p50"] == 2.0
    assert steps["强化页 · 强化"]["n"] == 1


def test_spread_reports_fluctuation() -> None:
    """波动 = 极差 / 平均，用来回答「耗时浮动大吗」。"""
    st = S.RunStats(tag="t")
    st.absorb_steps([S.StepSpan("A", "a", "", 1.0), S.StepSpan("A", "a", "", 3.0)])
    assert st.summary()["steps"]["A · a"]["spread"] == 1.0

    steady = S.RunStats(tag="t2")
    steady.absorb_steps([S.StepSpan("A", "a", "", 2.0), S.StepSpan("A", "a", "", 2.0)])
    assert steady.summary()["steps"]["A · a"]["spread"] == 0.0


def test_write_and_log_summary_produce_readable_output(tmp_path, caplog) -> None:
    st = S.RunStats(tag="20261004-120000")
    st.job_begin(1, "任务1", 1, ("折枝剑歌",), ())
    st.scan(["c"] * 3, {0: "满级"}, 0, max_index=2)
    slot = st.disc_begin("j1", 0, 0)
    st.disc_stage(slot, 0, 3, 11.0)
    st.disc_end(slot, S.OUTCOME_UPGRADED, 3)
    st.job_end(12.5, 1)
    st.seconds = 12.5

    with caplog.at_level(logging.INFO, logger="test.stats"):
        st.log_summary(_log())

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "本轮统计" in text
    assert "升到 3 级" in text
    assert "筛选后共 3 个盘" in text

    target = st.write(tmp_path / "stats.json")
    assert target is not None and target.is_file()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["tag"] == "20261004-120000"
    assert data["outcomes"][S.OUTCOME_UPGRADED] == 1
    assert data["totals"]["processed"] == 1
    assert data["jobs"][0]["name"] == "任务1"
    # 不能留下 .tmp 尾巴
    assert not (tmp_path / "stats.tmp").exists()
