"""全局位置模型回归：纯计算，不发送任何真实游戏输入（spec §8 模型用例）。

覆盖 plan 阶段 1 完成条件：格位映射、连续前缀提交、恢复点、移走并插入后的
身份不变、满级整行跳过、半行裁切、任务间状态隔离。
"""

from __future__ import annotations

import pytest

from zzz_ddtool.traversal import (
    CAL_OK,
    CAL_OVER,
    CAL_SHORT,
    AnchorObservation,
    CellStatus,
    ResumePoint,
    GridCell,
    GridMetrics,
    PositionError,
    TraversalState,
    bottom_evidence,
    check_forward_move,
    check_layout_reference,
    forward_batch_for_top,
    forward_top_limit,
    global_index,
    global_row_col,
    display_row_col,
    layout_reference,
    make_filter_signature,
    map_labels,
    max_restore_rows,
    rate_candidate,
    restore_request_rows,
    split_stable_cells,
    tail_slots_confirmed,
)

W, H = 1920, 1080


def metrics(*, phase: float = 0.0, pitch: float = 175.0, origin: float = 267.0) -> GridMetrics:
    return GridMetrics.from_client(W, H, origin_center_y=origin, row_pitch_px=pitch)


def label_for(metrics_: GridMetrics, screen_row0: int, col0: int, *, phase: float, top0: int, level: int = 0):
    """按几何造一个标签：(标签中心 x, 图标中心 y, 等级)。标签中心 = 图标中心 + 87。"""
    center_y = metrics_.row_center_y(top0 + screen_row0, top0, phase)
    center_x = metrics_.center_x_of(col0)
    return (center_x, center_y + 87.0, level)


# --------------------------------------------------------------------------
# 编号与行列（spec §4.1、§8 第一行用例）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "index,row0,col0,display",
    [
        (0, 0, 0, (1, 1)),        # 用户序号 1
        (8, 0, 8, (1, 9)),        # 用户序号 9
        (9, 1, 0, (2, 1)),        # 用户序号 10
        (99, 11, 0, (12, 1)),     # 用户序号 100
        (100, 11, 1, (12, 2)),    # 用户序号 101
    ],
)
def test_index_and_display_conversion(index, row0, col0, display):
    assert global_row_col(index) == (row0, col0)
    assert global_index(row0, col0) == index
    assert display_row_col(index) == display


def test_negative_index_rejected():
    with pytest.raises(PositionError):
        global_row_col(-1)


# --------------------------------------------------------------------------
# 行中心、相位与几何反推（spec §4.1、§5.4）
# --------------------------------------------------------------------------


def test_viewport_three_rows_down_puts_screen_third_row_at_global_sixth():
    geom = metrics()
    # 视口顶部为全局第 4 行（0 基 3），屏幕第 3 行（0 基 2）就是全局第 6 行。
    assert geom.row_center_y(5, 3, 0.0) == 267 + 2 * 175
    assert display_row_col(global_index(5, 0)) == (6, 1)
    assert geom.screen_row_of(geom.row_center_y(5, 3, 0.0), 0.0) == 2


def test_half_row_phase_and_clipped_logical_first_row():
    geom = metrics()
    # 顶部全局行索引 10、相位半行：屏幕逻辑第 0 行中心在原点上方 87.5px（被裁切）。
    assert geom.row_center_y(10, 10, 87.5) == 267 - 87.5
    # 即使第 0 行没被 OCR 到，下一完整行仍是屏幕逻辑第 1 行 → 全局第 12 行。
    assert geom.screen_row_of(geom.row_center_y(11, 10, 87.5), 87.5) == 1


def test_screen_row_comes_from_geometry_not_cluster_order():
    """整行漏识别不能让后续行向前补号（spec §5.5）。"""
    geom = metrics()
    cells, problems = map_labels(
        geom,
        [label_for(geom, 1, 0, phase=0.0, top0=0)],
        viewport_top_row0=0,
        row_phase_px=0.0,
    )
    assert problems == []
    # 若误用 OCR 聚类序号会得到全局第 1 行；几何反推得到全局第 2 行（index 9）。
    assert [cell.global_index for cell in cells] == [9]
    assert cells[0].display() == (2, 1)


def test_residual_over_two_percent_is_not_mapped():
    geom = metrics()
    center_y = geom.row_center_y(1, 0, 0.0) + 10.0  # 偏 10px > 175*2%=3.5px
    assert geom.screen_row_of(center_y, 0.0) is None


def test_metrics_rejects_non_positive_pitch():
    with pytest.raises(PositionError):
        GridMetrics.from_client(W, H, origin_center_y=267.0, row_pitch_px=0.0)


# --------------------------------------------------------------------------
# 列归位与格位映射（spec §4.1 第 4 条、§5.5）
# --------------------------------------------------------------------------


def test_column_classification_uses_half_pitch_window():
    geom = metrics()
    assert geom.col_of(geom.center_x_of(0)) == 0
    assert geom.col_of(geom.center_x_of(8)) == 8
    # 超出最右列半间距 → 不归列。
    assert geom.col_of(geom.center_x_of(8) + geom.col_pitch_px) is None


def test_duplicate_column_is_a_problem_not_a_skip():
    geom = metrics()
    dup_x = geom.center_x_of(3)
    labels = [
        (dup_x, geom.row_center_y(0, 0, 0.0) + 87.0, 0),
        (dup_x, geom.row_center_y(0, 0, 0.0) + 89.0, 0),
    ]
    cells, problems = map_labels(geom, labels, viewport_top_row0=0, row_phase_px=0.0)
    assert len(cells) == 1
    assert any("重复归位" in item for item in problems)


def test_unmappable_label_recorded_as_problem():
    geom = metrics()
    _, problems = map_labels(
        geom, [(10.0, 10.0, 0)], viewport_top_row0=0, row_phase_px=0.0
    )
    assert len(problems) == 1


def test_map_labels_maps_full_row_in_column_order():
    geom = metrics()
    labels = [label_for(geom, 0, col, phase=0.0, top0=0) for col in range(9)]
    cells, problems = map_labels(geom, labels, viewport_top_row0=0, row_phase_px=0.0)
    assert problems == []
    assert [cell.global_index for cell in cells] == list(range(9))
    assert [cell.global_col0 for cell in cells] == list(range(9))


def test_split_stable_cells_defers_edge_rows_until_endpoint():
    cells = [
        GridCell(global_index=i, global_row0=0, global_col0=0, screen_row0=i // 9,
                 screen_col0=i % 9, center_x=0, center_y=0)
        for i in range(18)
    ]
    ready, deferred = split_stable_cells(cells, at_top=False, at_bottom=False)
    assert {cell.screen_row0 for cell in deferred} == {0, 1}
    assert ready == []
    ready, deferred = split_stable_cells(cells, at_top=True, at_bottom=True)
    assert deferred == []
    assert len(ready) == 18


# --------------------------------------------------------------------------
# 连续前缀提交（spec §6.1）
# --------------------------------------------------------------------------


def test_commit_prefix_stops_at_first_unresolved():
    state = TraversalState(job_id="j1")
    for index in range(100):
        state.set_status(index, CellStatus.DONE)
    state.set_status(107, CellStatus.SKIPPED)  # 后方已确认满级，但前方有未解决。
    assert state.commit_prefix() == 100  # 不是 108
    for index in range(100, 108):  # 100 处理完成，101..107 已确认跳过
        state.set_status(index, CellStatus.DONE if index == 100 else CellStatus.SKIPPED)
    assert state.commit_prefix() == 108  # 越过紧接着已跳过的格位
    assert state.next_index == 108
    assert state.status_of(107) == CellStatus.PENDING  # 已推进的前缀状态被丢弃


@pytest.mark.parametrize("barrier", [CellStatus.UNKNOWN, CellStatus.DEFERRED, CellStatus.PENDING])
def test_barrier_statuses_block_progress(barrier):
    state = TraversalState(job_id="j1")
    state.set_status(0, CellStatus.DONE)
    state.set_status(1, barrier)
    state.set_status(2, CellStatus.SKIPPED)
    assert state.commit_prefix() == 1


def test_skipped_whole_row_advances_without_processing():
    """满级整行跳过仍占格位：进度推进，但不产生处理计数（plan 阶段 1）。"""
    state = TraversalState(job_id="j1")
    for index in range(18):  # 前两行 18 格全满级
        state.set_status(index, CellStatus.SKIPPED)
    assert state.commit_prefix() == 18


def test_block_rejects_resolved_status():
    state = TraversalState(job_id="j1")
    with pytest.raises(PositionError):
        state.block(0, CellStatus.DONE)


# --------------------------------------------------------------------------
# 强化移位后的身份延续（spec §2、§8）
# --------------------------------------------------------------------------


def _move(ids: list[str], src: int, dst: int) -> list[str]:
    ids.insert(dst, ids.pop(src))
    return ids


@pytest.mark.parametrize("src,dst", [(99, 10), (99, 99), (50, 0), (100, 3)])
def test_moving_a_disc_keeps_following_identity(src, dst):
    ids = [f"disc-{i}" for i in range(150)]
    follower = ids[src + 1]
    _move(ids, src, dst)
    assert ids[src + 1] == follower, "移走再插入前方后，原来的下一件必须仍在原序号"
    assert ids[dst] == f"disc-{src}"


def test_next_index_survives_upgrade_focus_move():
    state = TraversalState(job_id="j1")
    state.confirm_viewport(10, 0.0, row_pitch_px=175.0)
    for index in range(99):
        state.set_status(index, CellStatus.DONE)
    state.commit_prefix()
    assert state.next_index == 99
    state.begin_upgrade(99)  # 第 100 件强化后焦点移到前方
    state.finish_upgrade()
    assert state.next_index == 100  # 下一件仍是第 101 件


# --------------------------------------------------------------------------
# 恢复点（spec §4.2、§6.3）
# --------------------------------------------------------------------------


def test_single_resume_point_for_multi_stage_upgrade():
    state = TraversalState(job_id="j1")
    state.confirm_viewport(10, 50.0, row_pitch_px=175.0)
    for index in range(100):
        state.set_status(index, CellStatus.DONE)
    state.commit_prefix()
    assert state.next_index == 100

    first = state.begin_upgrade(100)
    for _ in range(3):  # 0→3→6→9，焦点每档都变
        assert state.begin_upgrade(100) is first
    assert state.resume_point.next_index == 101
    assert state.resume_point.viewport_top_row0 == 10
    assert state.resume_point.row_phase_px == 50.0  # 不被后续焦点覆盖

    assert state.finish_upgrade() == 101
    assert state.active_index is None
    # 恢复点保留到定位恢复完成（spec §6.3.3、§6.3.5），恢复后显式释放。
    assert state.resume_point is not None
    state.clear_resume()
    assert state.resume_point is None


def test_upgrade_cannot_skip_earlier_unresolved_cell():
    state = TraversalState(job_id="j1")
    state.confirm_viewport(0, 0.0, row_pitch_px=175.0)
    for index in range(99):
        state.set_status(index, CellStatus.DONE)
    state.block(99, CellStatus.UNKNOWN)  # 前方有未知格位
    state.commit_prefix()
    assert state.next_index == 99
    state.begin_upgrade(100)
    assert state.finish_upgrade() == 99  # 不越过屏障
    assert state.status_of(100) == CellStatus.DONE


def test_begin_upgrade_without_viewport_fails():
    state = TraversalState(job_id="j1")
    with pytest.raises(PositionError):
        state.begin_upgrade(0)


# --------------------------------------------------------------------------
# 视口、相位与偏移
# --------------------------------------------------------------------------


def test_offset_px_includes_phase():
    state = TraversalState(job_id="j1")
    state.confirm_viewport(10, 87.5, row_pitch_px=175.0)
    assert state.offset_px() == 10 * 175 + 87.5


@pytest.mark.parametrize("phase", [-1.0, 175.0, 200.0])
def test_confirm_viewport_rejects_bad_phase(phase):
    state = TraversalState(job_id="j1")
    with pytest.raises(PositionError):
        state.confirm_viewport(0, phase, row_pitch_px=175.0)


def test_confirm_viewport_requires_known_pitch():
    state = TraversalState(job_id="j1")
    with pytest.raises(PositionError):
        state.confirm_viewport(0, 0.0)


# --------------------------------------------------------------------------
# 开局上界与请求取整（spec §5.1、§5.3）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("max_pages,expected", [(1, 0), (2, 3), (3, 5), (20, 39)])
def test_max_restore_rows(max_pages, expected):
    assert max_restore_rows(max_pages) == expected


def test_bottom_saved_half_row_requests_upper_bound_row():
    # top=38、phase=半行 → ceil(38.5)=39=H，不应误报能力不足。
    assert restore_request_rows(38, 87.5, 175.0) == 39
    assert restore_request_rows(38, 87.5, 175.0) <= max_restore_rows(20)
    assert restore_request_rows(10, 87.5, 175.0) == 11  # 不是 1 行


def test_restore_request_rows_rejects_zero_pitch():
    with pytest.raises(PositionError):
        restore_request_rows(1, 0.0, 0.0)


def test_forward_top_limit():
    assert forward_top_limit(1) == 0
    assert forward_top_limit(2) == 2
    assert forward_top_limit(20) == 38


def test_forward_batch_for_top_is_the_inverse_of_the_limit():
    """跳号把视口摆到任意深度后，等效批次必须让 forward_top_limit 放行该顶部行。"""
    for top in range(0, 40):
        batch = forward_batch_for_top(top)
        assert forward_top_limit(batch) >= top, (top, batch)
        assert batch >= 1
    assert forward_batch_for_top(0) == 1
    assert forward_batch_for_top(1) == 2
    assert forward_batch_for_top(2) == 2
    assert forward_batch_for_top(13) == 8  # 真机回归：全局第 14 行视口


def test_forward_move_allows_a_seeded_deep_viewport():
    """真机回归：跳号到全局第 14 行（顶部 13）后，下一次前进不再被判「超过上界」。"""
    top = 13
    check_forward_move(forward_batch_for_top(top) + 1, top, top, 2)
    with pytest.raises(PositionError, match="超过上界"):
        check_forward_move(2, top, top, 2)  # 批次没跟着走时的原症状


def test_forward_move_validation():
    check_forward_move(2, 0, 2, 2)
    with pytest.raises(PositionError, match="超量滚动"):
        check_forward_move(2, 0, 3, 2)
    with pytest.raises(PositionError, match="超过上界"):
        check_forward_move(2, 2, 4, 2)
    with pytest.raises(PositionError, match="不能向上"):
        check_forward_move(2, 2, 1, 2)
    with pytest.raises(PositionError, match="必须为正"):
        check_forward_move(2, 0, 0, 0)


# --------------------------------------------------------------------------
# 布局参考复核（spec §5.4）
# --------------------------------------------------------------------------


def test_layout_reference_scales_with_client_size():
    assert layout_reference(1920, 1080) == (267.0, 175.0)
    assert layout_reference(1280, 720) == (178.0, 117.0)


def test_layout_reference_accepts_measured_origin():
    assert check_layout_reference(267.0, 175.0, 1920, 1080) is None
    assert check_layout_reference(266.0, 175.0, 1920, 1080) is None  # 1px 在 3.5px 容差内


@pytest.mark.parametrize("origin,pitch", [(280.0, 175.0), (267.0, 190.0)])
def test_layout_reference_reports_mismatch(origin, pitch):
    reason = check_layout_reference(origin, pitch, 1920, 1080)
    assert reason is not None and "2%" in reason


# --------------------------------------------------------------------------
# 锚点位移证据、标定候选与端点依据（spec §5.1、§5.2、§5.4）
#
# 全局位置不依赖滑块：本模块无滑块字段，也没有任何以滑块读数为条件的判据。
# --------------------------------------------------------------------------


def test_state_has_no_slider_fields():
    """spec §8：确认无测距字段及依赖。"""
    state = TraversalState(job_id="j1")
    payload = state.status_payload()
    assert not any("slider" in key.lower() for key in payload)
    assert not hasattr(state, "slider_mapping")
    assert not hasattr(state, "task_restore_limit_rows")
    point = ResumePoint(viewport_top_row0=0, row_phase_px=0.0, next_index=1)
    assert not any("slider" in key.lower() for key in point.as_payload())


def test_anchor_advanced_rows_counts_screen_row_movement():
    obs = AnchorObservation(anchor_index=30, screen_row0_before=2, screen_row0_after=0, requested_rows=2)
    assert obs.advanced_rows == 2
    assert not obs.shortfall and not obs.stopped


def test_anchor_shortfall_marks_endpoint_stop():
    obs = AnchorObservation(anchor_index=30, screen_row0_before=2, screen_row0_after=1, requested_rows=2)
    assert obs.advanced_rows == 1 and obs.shortfall
    assert not obs.stopped


def test_anchor_no_movement_at_all_is_stopped():
    obs = AnchorObservation(anchor_index=30, screen_row0_before=2, screen_row0_after=2, requested_rows=2)
    assert obs.advanced_rows == 0 and obs.stopped and obs.shortfall


@pytest.mark.parametrize(
    "displacement,expected",
    [
        (175.0, CAL_OK),
        (171.5, CAL_OK),  # -2%
        (178.5, CAL_OK),  # +2%
        (100.0, CAL_SHORT),
        (227.5, CAL_OVER),  # 0.6 行直接跳成 1.3 行
    ],
)
def test_rate_candidate_windows(displacement, expected):
    assert rate_candidate(displacement, 175.0) == expected


def test_over_candidate_is_never_chosen_by_rounding():
    """0.6 行跳成 1.3 行：该间隔无合格整数协议，不插值也不舍入。"""
    assert rate_candidate(1.3 * 175.0, 175.0) == CAL_OVER


def test_bottom_evidence_requires_both_endpoint_and_tail():
    full = AnchorObservation(30, 2, 0, 2)
    short = AnchorObservation(30, 2, 1, 2)
    assert not bottom_evidence([full], tail_slots_confirmed=True)
    assert not bottom_evidence([short], tail_slots_confirmed=False)  # 末尾占位未确认
    assert bottom_evidence([short], tail_slots_confirmed=True)
    assert bottom_evidence([full, short], tail_slots_confirmed=True)


def test_restore_request_rows_is_internal_limit_not_measured_distance():
    """H 只是内部请求上界，不代表已测量 H 行（spec §5.1）。"""
    state = TraversalState(job_id="j1")
    state.confirm_viewport(10, 87.5, row_pitch_px=175.0)  # top=10、半行相位
    state.restore_request_limit_rows = max_restore_rows(20)  # H=39
    state.begin_upgrade(global_index(10, 0))
    assert state.restore_request_rows() == 11  # ceil(10.5)
    assert state.within_restore_limit()
    state.restore_request_limit_rows = 5  # 逻辑错误导致的过小上界
    assert not state.within_restore_limit()


# --------------------------------------------------------------------------
# 工作批次（spec §6.4）
# --------------------------------------------------------------------------


def test_work_batch_counting():
    state = TraversalState(job_id="j1")
    state.confirm_viewport(0, 0.0, row_pitch_px=175.0)
    assert state.work_batch_count == 0
    assert state.start_first_batch() == 1
    assert state.advance_batch() == 2
    assert state.advance_batch() == 3
    assert state.work_batches_left(20) == 17
    # 恢复/重拍不新建批：调用方不得调用 advance_batch。
    state.begin_upgrade(10)
    state.finish_upgrade()
    assert state.work_batch_count == 3


# --------------------------------------------------------------------------
# 滚动发送进度（spec §5.2）
# --------------------------------------------------------------------------


def test_scroll_progress_keeps_remaining_events_on_pause():
    state = TraversalState(job_id="j1")
    operation = state.begin_scroll(2, 5)
    state.mark_event_sent()
    state.mark_event_sent()
    assert operation.sent_events == 2
    assert operation.remaining_events == 3  # 暂停恢复只发剩余
    operation.waiting()
    assert operation.phase == "waiting"
    state.confirm_scroll()
    assert operation.confirmed and operation.phase == "confirmed"
    state.clear_scroll()
    assert state.scroll_operation is None


def test_mark_event_sent_without_operation_fails():
    with pytest.raises(PositionError):
        TraversalState(job_id="j1").mark_event_sent()


# --------------------------------------------------------------------------
# 恢复尝试计数（spec §5.3）
# --------------------------------------------------------------------------


def test_restore_attempt_counters():
    state = TraversalState(job_id="j1")
    assert (state.restore_attempt, state.restore_retries) == (0, 0)
    assert state.begin_restore_attempt() == 1
    assert (state.restore_attempt, state.restore_retries) == (1, 0)
    assert state.begin_restore_attempt() == 2
    assert (state.restore_attempt, state.restore_retries) == (2, 1)
    state.reset_restore_attempts()
    assert (state.restore_attempt, state.restore_retries) == (0, 0)


# --------------------------------------------------------------------------
# 状态快照、筛选签名与任务隔离（spec §7、§6.4）
# --------------------------------------------------------------------------


def test_status_payload_exposes_required_keys():
    state = TraversalState(job_id="j1")
    state.confirm_viewport(10, 87.5, row_pitch_px=175.0, at_top=False, at_bottom=False)
    for index in range(100):
        state.set_status(index, CellStatus.DONE)
    state.commit_prefix()
    state.begin_upgrade(99)
    state.begin_scroll(2, 5)
    state.mark_event_sent()
    payload = state.status_payload()
    assert payload["job_id"] == "j1"
    assert payload["next_index"] == 100
    assert payload["global_row"] == 12 and payload["global_col"] == 1  # 显示 active_index
    assert payload["viewport_top_row"] == 11
    assert payload["row_phase_px"] == 87.5 and payload["row_pitch_px"] == 175.0
    assert payload["position_valid"] is True
    assert payload["restore_target"]["next_index"] == 100
    assert payload["work_batch_count"] == 0
    assert payload["scroll_sent_events"] == 1 and payload["scroll_total_events"] == 5
    assert (payload["restore_attempt"], payload["restore_retries"]) == (0, 0)


def test_status_payload_hides_unknown_position():
    payload = TraversalState(job_id="j2").status_payload()
    assert payload["viewport_top_row"] is None  # 不伪写第 1 行
    assert payload["position_valid"] is False
    assert payload["restore_target"] is None


def test_filter_signature_is_stable_and_config_only():
    first = make_filter_signature(slot=4, sets=("A", "B"), mainstats=("暴击率",))
    same = make_filter_signature(slot=4, sets=("B", "A"), mainstats=("暴击率",))
    other = make_filter_signature(slot=5, sets=("A", "B"), mainstats=("暴击率",))
    assert first == same
    assert first != other


def test_task_states_are_isolated():
    first = TraversalState(job_id="j1", filter_signature="a")
    first.confirm_viewport(5, 0.0, row_pitch_px=175.0)
    first.set_status(0, CellStatus.DONE)
    first.commit_prefix()
    first.start_first_batch()

    second = TraversalState(job_id="j2", filter_signature="b")
    assert second.next_index == 0
    assert second.viewport_top_row0 is None and second.position_valid is False
    assert second.work_batch_count == 0
    assert second.filter_signature != first.filter_signature
    assert first.next_index == 1 and first.work_batch_count == 1  # 互不影响


# --------------------------------------------------------------------------
# 末尾占位确认（spec §5.4、§5.5 第 259 行）
# --------------------------------------------------------------------------

ROWS, COLS = 4, 9


def _screen_set(pairs):
    return {(row, col) for row, col in pairs}


def _full_rows(*rows):
    return [(row, col) for row in rows for col in range(COLS)]


def test_tail_slots_confirmed_for_partial_last_row():
    occupied = _screen_set(_full_rows(0, 1, 2) + [(3, col) for col in range(8)])
    assert tail_slots_confirmed(occupied, {(3, 8)}, rows=ROWS, cols=COLS)


def test_tail_slots_confirmed_rejects_undecided_tail():
    """末行剩下的位置没确认是空位：不当空位，也不宣布到底。"""
    occupied = _screen_set(_full_rows(0, 1, 2) + [(3, col) for col in range(8)])
    assert not tail_slots_confirmed(occupied, set(), rows=ROWS, cols=COLS)


def test_tail_slots_confirmed_rejects_middle_hole():
    """中间冒出空位说明这不是真正的列表末尾。"""
    occupied = _screen_set(_full_rows(0) + [(3, col) for col in range(8)])
    assert not tail_slots_confirmed(occupied, {(1, 4)}, rows=ROWS, cols=COLS)


def test_tail_slots_confirmed_rejects_ragged_last_row():
    """末行实体盘从左端起不连续：列分布有歧义，走失败路径。"""
    occupied = _screen_set(_full_rows(0) + [(3, 0), (3, 2)])
    assert not tail_slots_confirmed(occupied, {(3, 1)}, rows=ROWS, cols=COLS)


def test_tail_slots_confirmed_accepts_full_bottom_row():
    """末行满行时看不到占位，空位不构成矛盾；是否到底仍由锚点端点行为决定。"""
    occupied = _screen_set(_full_rows(0, 1, 2, 3))
    assert tail_slots_confirmed(occupied, set(), rows=ROWS, cols=COLS)


def test_tail_slots_confirmed_for_empty_list():
    viewport = _screen_set(_full_rows(0, 1, 2, 3))
    assert tail_slots_confirmed(set(), viewport, rows=ROWS, cols=COLS)
    assert not tail_slots_confirmed(set(), set(), rows=ROWS, cols=COLS)


def test_tail_slots_confirmed_for_short_list_ending_mid_viewport():
    occupied = _screen_set(_full_rows(0) + [(1, 0)])
    rest = [(1, col) for col in range(1, COLS)] + _full_rows(2, 3)
    assert tail_slots_confirmed(occupied, _screen_set(rest), rows=ROWS, cols=COLS)
    assert not tail_slots_confirmed(occupied, _screen_set(rest[:-1]), rows=ROWS, cols=COLS)
