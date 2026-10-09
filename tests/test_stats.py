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
    st.job_begin(1, 1, ("折枝剑歌",), ())
    st.scan(["c"] * 9, {0: "满级", 1: "满级"}, 1, max_index=8)
    st.scan(["c"] * 9, {0: "满级", 2: "已弃置"}, 2, max_index=8)
    st.job_end(10.0, 0)
    st.job_begin(2, 2, (), ())
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
    st.job_begin(1, 1, ("折枝剑歌",), ())
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
    assert "筛选后合计 3 个盘" in text
    assert "共计耗时 12 秒" in text

    target = st.write(tmp_path / "stats.json")
    assert target is not None and target.is_file()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["tag"] == "20261004-120000"
    assert data["outcomes"][S.OUTCOME_UPGRADED] == 1
    assert data["totals"]["processed"] == 1
    assert data["jobs"][0]["slot"] == 1
    assert data["jobs"][0]["sets"] == ["折枝剑歌"]
    # 任务不再有名字字段：靠序号 + 部位 + 套装 + 主词条认
    assert "name" not in data["jobs"][0]
    # 不能留下 .tmp 尾巴
    assert not (tmp_path / "stats.tmp").exists()


# ---------------------------------------------------------------- 执行结果总结


def _sample() -> S.RunStats:
    """一件升满 15 级的盘 + 一个任务，用来生成完整报告。"""
    st = S.RunStats(tag="20261004-200000")
    st.job_begin(1, 1, ("折枝剑歌",), ())
    st.scan(["c"] * 3, {0: "满级"}, 0, max_index=2)
    slot = st.disc_begin("j1", 0, 0)
    for frm, to in ((0, 3), (3, 6), (6, 9), (9, 12), (12, 15)):
        st.disc_stage(slot, frm, to, float(to))
    st.disc_end(slot, S.OUTCOME_UPGRADED, 15)
    # 又一件「升过级但没到 15」的盘：用来把「成功 = 升满 15」和「强化过」两个口径卡开，
    # 否则两件盘只放一件时两个数都是 1，改坏口径也测不出来。
    short = st.disc_begin("j1", 1, 6)
    st.disc_stage(short, 6, 9, 9.0)
    st.disc_end(short, S.OUTCOME_UPGRADED, 9)
    st.job_end(65.0, 1)
    st.seconds = 194.0
    return st


def test_headline_counts_the_four_asked_numbers() -> None:
    """一行摘要要能独立读懂：耗时 / 处理数 / 升满成功 / 判定弃置，四个都在。

    摘要**不带括号解释**——那是给详细日志用的；客户端那行只摆数。
    """
    head = _sample().headline()
    assert "共计耗时 3 分 14 秒" in head
    assert "处理了 2 个盘" in head
    assert "升满成功 1 件" in head
    assert "判定弃置 0 件" in head
    # 括号里的口径说明不该出现在摘要里
    assert "（" not in head and "）" not in head


def test_detailed_report_keeps_the_caveats() -> None:
    """括号里的口径说明从摘要挪到详细日志：摘要不写，详细日志必须写清。"""
    text = _sample().report_text()
    assert S.KEPT_NOTE in text
    assert S.DISCARD_NOTE in text


def test_report_fixes_all_five_stages_including_12() -> None:
    """档位是 3/6/9/12/15 五档；某档没数据也要留一行，别让人以为漏了一档。"""
    assert S.STAGE_LEVELS == (3, 6, 9, 12, 15)
    text = S.RunStats(tag="t").report_text()  # 全空的报告也得列齐 5 档
    for level in (3, 6, 9, 12, 15):
        assert f"升到 {level} 级" in text


def test_report_and_log_include_actual_nonstandard_reached_levels(caplog) -> None:
    st = S.RunStats(tag="t")
    slot = st.disc_begin("j", 0, 0)
    st.disc_stage(slot, 0, 4, 5.0)
    st.disc_stage(slot, 4, 6, 7.0)
    st.disc_end(slot, S.OUTCOME_DISCARDED, 6)
    data = st.summary()
    assert {k: v["n"] for k, v in data["stage_seconds"].items()} == {"4": 1, "6": 1}
    text = st.report_text()
    assert "| 升到 4 级 | 1 | 5.0s" in text
    assert "| 升到 6 级 | 1 | 7.0s" in text
    assert text.index("| 升到 3 级") < text.index("| 升到 4 级") < text.index("| 升到 6 级")
    with caplog.at_level(logging.INFO):
        st.log_summary(logging.getLogger("nonstandard_stage"))
    assert any("升到 4 级：1 次" in record.getMessage() for record in caplog.records)


def test_report_distinguishes_scan_skips_from_processed_skips() -> None:
    st = S.RunStats(tag="t")
    st.job_begin(1, 1, (), ())
    st.scan([], {0: "满级", 1: "弃置"}, 0, max_index=2)
    slot = st.disc_begin("j", 2, 0)
    st.disc_end(slot, S.OUTCOME_SKIPPED, 0)
    st.job_end(1.0, processed=1)
    text = st.report_text()
    assert "| 扫描跳过 | 2 个 |" in text
    assert "| 进入处理后跳过 | 1 | 已读取盘面，未改动 |" in text


def test_discard_label_count_can_differ_from_judged_discard() -> None:
    """「判定弃置」报判定数；盘上已有标记时不会真打，所以实打数 ≤ 判定数。"""
    st = S.RunStats(tag="t")
    for index in (0, 1):
        slot = st.disc_begin("j1", index, 0)
        st.disc_end(slot, S.OUTCOME_DISCARDED, 3)
    st.note_discard_label()  # 只有一件真把标记打上去了

    data = st.summary()
    assert data["outcomes"][S.OUTCOME_DISCARDED] == 2
    assert data["discard_labels"] == 1
    text = st.report_text()
    assert "实际打上弃置标记：**1** 次" in text


def test_report_text_is_human_markdown() -> None:
    text = _sample().report_text()
    assert text.startswith("# 运行结果统计 · 20261004-200000")
    for section in (
        "## 总览",
        "## 逐任务",
        "## 结果明细",
        "## 最终等级分布",
        "## 单件升级耗时（按到达档位）",
        "## 说明",
    ):
        assert section in text
    assert "会重叠，别相加" in text
    assert "不等于实际打过弃置标记的数量" in text


def test_report_survives_an_empty_run() -> None:
    """一件盘都没处理的空转也得给出可读报告，不能崩。"""
    text = S.RunStats(tag="t").report_text()
    assert "处理了 0 个盘" in text
    assert "（无：本轮没有走完流程的盘）" in text


def test_write_report_writes_markdown_file(tmp_path) -> None:
    target = _sample().write_report(tmp_path / "r.md")
    assert target is not None and target.is_file()
    assert "运行结果统计" in target.read_text(encoding="utf-8")
    assert not (tmp_path / "r.tmp").exists()


def test_kept_disc_detail_lists_slot_set_and_line_count() -> None:
    """「升满成功明细」要列出成功盘的部位号、套装名、词条数，让用户看清是哪几件。"""
    st = S.RunStats(tag="t")
    st.job_begin(1, 4, ("震星迪斯科",), ())  # 4 号位、震星迪斯科
    # 一件升满 15 级的 4 词条盘
    slot = st.disc_begin("j1", 0, 0, slot=4, sets=("震星迪斯科",), line_count=4)
    st.disc_stage(slot, 0, 3, 3.0)
    st.disc_end(slot, S.OUTCOME_UPGRADED, 15, 12.5)
    # 一件只升到 9 级的盘，不应出现在成功明细
    short = st.disc_begin("j1", 1, 0, slot=4, sets=("震星迪斯科",), line_count=3)
    st.disc_end(short, S.OUTCOME_UPGRADED, 9, 5.0)
    st.job_end(20.0, 1)

    data = st.summary()
    assert data["max_level_kept"] == 1
    assert len(data["kept_discs"]) == 1
    kept = data["kept_discs"][0]
    assert kept["slot"] == 4 and kept["sets"] == ["震星迪斯科"] and kept["line_count"] == 4

    text = st.report_text()
    assert "## 升满成功明细（15 级且判定保留）" in text
    # 成功盘的具体信息要出现在报告里
    assert "4 号位" in text
    assert "震星迪斯科" in text
    assert "4 条" in text
    # 没升满的不该进明细
    assert "9 级" not in text.split("升满成功明细", 1)[1].split("## 最终等级分布", 1)[0]


def test_kept_disc_detail_unknown_set_when_job_has_no_set() -> None:
    """任务没限定套装时，成功明细的套装列显示「未知」，不能崩。"""
    st = S.RunStats(tag="t")
    st.job_begin(2, 2, (), ())  # 2 号位、不限套装
    slot = st.disc_begin("j2", 0, 0, slot=2, sets=(), line_count=2)
    st.disc_end(slot, S.OUTCOME_UPGRADED, 15, 8.0)
    st.job_end(10.0, 1)
    text = st.report_text()
    assert "2 号位" in text
    assert "未知" in text
    assert "2 条" in text
