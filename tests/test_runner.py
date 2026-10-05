"""执行器回归：全局格位遍历、扫描跳过、进度提交、白名单与失败传播。

替身网格模拟 4 行视口、可配置总行数的仓库；真实 OCR/输入/窗口接口全部替换，
不发送任何真实游戏输入。
"""

from dataclasses import replace
from pathlib import Path
from unittest.mock import ANY, Mock, call

import pytest
from PySide6.QtGui import QImage

import zzz_ddtool.ocr as ocr_module
import zzz_ddtool.runner as runner_module
import zzz_ddtool.screen as screen_module
from zzz_ddtool.chain import TaskJob
from zzz_ddtool.config import Preset
from zzz_ddtool.decide import decide
from zzz_ddtool.models import Action, Disc, Substat
from zzz_ddtool.patterns import enumerate_filtered
from zzz_ddtool.taskcards import effective_weights
from zzz_ddtool.hotkeys import HotkeyController, RunnerStopped
from zzz_ddtool.navigation import Environment, NavProfile, ScrollSend
from zzz_ddtool.runner import Runner, RunnerError
from zzz_ddtool.traversal import (
    VIEWPORT_SCROLLING,
    VIEWPORT_SINGLE_SCREEN,
    CellStatus,
    GridCell,
    TraversalState,
    check_forward_move,
    forward_batch_for_top,
    max_restore_rows,
)

VIEWPORT_ROWS = 4
GRID_COLS = 9

#: 替身默认按「长列表」准备：筛选后总数超过单屏容量 36，走一次置顶的遍历路径。
FAKE_WAREHOUSE_COUNT = 97


def fake_label_lines(count=FAKE_WAREHOUSE_COUNT):
    """替身列表区域 OCR 行：只有一条仓库数量行，没有「等级」标签。"""
    return [ocr_module.OcrLine(f"{count}/3000", 0.99, 70, 120, 350, 150)]


def generated_job(**kwargs):
    """Give execution regressions a real explicit whitelist, as the GUI now does."""
    job = TaskJob(**kwargs)
    return replace(job, accepted=tuple(p.pattern_id for p in enumerate_filtered(
        effective_weights(job), stage_require=job.stage_require)))


def generated_preset(**kwargs):
    preset = Preset(**kwargs)
    return replace(preset, accepted=tuple(p.pattern_id for p in enumerate_filtered(
        preset.effective_weights(), stage_require=preset.stage_require)))


def make_cell(global_row0, col, screen_row0, level=0):
    return GridCell(
        global_index=global_row0 * GRID_COLS + col,
        global_row0=global_row0,
        global_col0=col,
        screen_row0=screen_row0,
        screen_col0=col,
        center_x=166 + col * 135,
        center_y=267 + screen_row0 * 175,
        level=level,
    )


class FakeGrid:
    """假仓库：4 行视口，按 offset 平移；filtered 行仍有格位、只被扫描跳过。"""

    def __init__(self, total_rows=6, filtered_rows=(), last_row_cols=GRID_COLS):
        self.total_rows = total_rows
        self.filtered = set(filtered_rows)
        self.last_row_cols = last_row_cols
        self.offset = 0
        self.selected: tuple[int, int] | None = None
        self.visited: list[tuple[int, int]] = []
        self.scans = 0
        self.deferred_peak = 0

    @property
    def at_top(self) -> bool:
        return self.offset == 0

    @property
    def at_bottom(self) -> bool:
        return self.offset >= self.total_rows - VIEWPORT_ROWS

    def selected_cell(self):
        if self.selected is None:
            return None
        grow, col = self.selected
        screen = grow - self.offset
        if not (0 <= screen < VIEWPORT_ROWS):
            return None
        return make_cell(grow, col, screen)

    def scan(self):
        self.scans += 1
        cells = []
        skips: dict[int, str] = {}
        for screen in range(VIEWPORT_ROWS):
            grow = self.offset + screen
            if grow >= self.total_rows:
                continue
            cols = self.last_row_cols if grow == self.total_rows - 1 else GRID_COLS
            for col in range(cols):
                cell = make_cell(grow, col, screen)
                cells.append(cell)
                if grow in self.filtered:
                    skips[cell.global_index] = f"row {grow} filtered"
        self.deferred_peak = max(self.deferred_peak, 0)
        return cells, [], skips

    def advance(self, rows: int) -> int:
        before = self.offset
        self.offset = min(self.total_rows - VIEWPORT_ROWS, self.offset + rows)
        return self.offset - before


def fake_profile():
    return NavProfile(
        machine_id="m" * 8,
        client_width=1920,
        client_height=1080,
        window_dpi=96,
        game_exe_path="c:/game/zenlesszonezero.exe",
        game_build="1.2.3.4|100|200",
        row_pitch_px=175.0,
        origin_center_y=267.0,
        events_per_row=1,
        event_interval_ms=100,
        settle_wait_ms=100,
    )


def fake_env():
    return Environment(
        machine_id="m" * 8,
        client_width=1920,
        client_height=1080,
        window_dpi=96,
        game_exe_path="c:/game/zenlesszonezero.exe",
        game_build="1.2.3.4|100|200",
    )


@pytest.fixture
def offline_runner(monkeypatch):
    """所有屏幕、输入、窗口与导航接口全部替换，不操作真实游戏。"""
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    trace = Mock()
    monkeypatch.setattr(runner_module, "Trace", lambda **kwargs: trace)
    monkeypatch.setattr(Runner, "_set_status", Mock())
    monkeypatch.setattr(runner_module.time, "sleep", lambda seconds: None)

    monkeypatch.setattr(runner_module, "find_game_hwnd", lambda: 1)
    monkeypatch.setattr(runner_module, "window_pid", lambda hwnd: 100)
    monkeypatch.setattr(runner_module, "grab_game_image", lambda hwnd=None: (image, "test", 1))
    monkeypatch.setattr(runner_module, "measure_layout", lambda img, **kw: (267.0, 175.0))
    # 列表区域只读一次、由调用方复用：替身固定给一条数量行（默认 97/3000，长列表）。
    monkeypatch.setattr(Runner, "_read_label_lines", lambda self, image: fake_label_lines())
    monkeypatch.setattr(runner_module, "probe_environment", lambda hwnd: fake_env())
    monkeypatch.setattr(runner_module, "load_profile", lambda env: fake_profile())
    monkeypatch.setattr(
        runner_module,
        "scroll_rows",
        lambda hwnd, rows, profile, **kwargs: ScrollSend(
            rows,
            rows * profile.events_per_row,
            rows * profile.events_per_row,
            profile.event_interval_ms,
        ),
    )

    # 默认两行数据：首行可点、末行被延后，模拟真实稳定行策略
    default_cells = ([make_cell(0, 0, 0), make_cell(1, 0, 1)], [], {})
    monkeypatch.setattr(Runner, "_scan_cells", lambda self, state, **kw: default_cells)
    monkeypatch.setattr(Runner, "_grab", lambda *a, **kw: (image, "unknown", "test", 1))
    monkeypatch.setattr(Runner, "_advance", lambda self, hwnd, state, cells, *, rows: True)

    inputs = {
        name: Mock()
        for name in (
            "goto_warehouse",
            "apply_game_filter",
            "reset_game_filter",
            "click_client",
            "press_key",
            "press_key_once",
            "reset_to_top",
        )
    }
    for name, mock in inputs.items():
        monkeypatch.setattr(runner_module, name, mock)
    monkeypatch.setattr("zzz_ddtool.overlay.overlay_hide", Mock())
    monkeypatch.setattr("zzz_ddtool.overlay.overlay_show", Mock())
    # 缩放预览层判据默认关掉（真实现要跑 OCR）；需要模拟它的用例自行覆盖。
    monkeypatch.setattr(screen_module, "is_disc_preview", lambda image: False)
    return inputs


def wire_grid(runner, monkeypatch, grid: FakeGrid):
    """把替身网格接到 Runner：扫描返回全局格位，前进更新视口与端点。"""

    def scan(state, **kwargs):
        runner._selected_cell = grid.selected_cell()
        cells, problems, skips = grid.scan()
        runner._tail_slots_confirmed = grid.at_bottom
        return cells, problems, skips

    def advance(hwnd, state, cells, *, rows):
        grid.offset = min(grid.total_rows - VIEWPORT_ROWS, grid.offset + rows)
        advanced = grid.offset - (state.viewport_top_row0 or 0)
        state.advance_batch()
        state.confirm_viewport(
            grid.offset, 0.0, at_top=grid.at_top, at_bottom=grid.at_bottom
        )
        return advanced >= 0

    monkeypatch.setattr(runner, "_scan_cells", scan)
    monkeypatch.setattr(runner, "_advance", advance)

    def click(hwnd, x, y):
        col = round((x - 166) / 135)
        screen = round((y - 267) / 175)
        grow = grid.offset + screen
        grid.selected = (grow, col)
        grid.visited.append((grow, col))

    monkeypatch.setattr(runner_module, "click_client", Mock(side_effect=click))
    return grid


# ---------------------------------------------------------------- 遍历不漏不重复

@pytest.mark.parametrize("phase", ["navigation", "calibration", "filter"])
def test_stop_during_preparation_blocks_next_input(offline_runner, monkeypatch, phase):
    from zzz_ddtool import hotkeys, wininput

    monkeypatch.setattr(hotkeys.user32, "GetAsyncKeyState", lambda vk: 0)
    windows = Mock()
    monkeypatch.setattr(wininput, "user32", windows)
    monkeypatch.setattr(wininput, "kernel32", Mock())
    ctrl = HotkeyController()
    runner = Runner(generated_preset(max_discs=1), controller=ctrl)
    monkeypatch.setattr(runner, "_bind_instance", lambda: 1)
    monkeypatch.setattr(runner, "_ensure_profile", Mock())

    def interrupted(*args, **kwargs):
        ctrl.request_stop()
        wininput.click_client(1, 100, 100)

    if phase == "navigation":
        monkeypatch.setattr(runner_module, "goto_warehouse", interrupted)
    elif phase == "calibration":
        monkeypatch.setattr(runner, "_ensure_profile", interrupted)
    else:
        monkeypatch.setattr(runner_module, "apply_game_filter", interrupted)
    try:
        runner.run()
        assert runner.outcome == "stopped"
        assert runner.processed_disc_count == 0
        assert not windows.mock_calls
        offline_runner["click_client"].assert_not_called()
    finally:
        ctrl.close()


def test_calibration_preserves_user_stop(offline_runner, monkeypatch):
    runner = Runner(Preset(), hotkeys=False)
    monkeypatch.setattr(runner_module, "load_profile", lambda env: None)
    monkeypatch.setattr(runner_module, "calibrate", Mock(side_effect=RunnerStopped("user stop")))
    with pytest.raises(RunnerStopped):
        runner._ensure_profile(1)



@pytest.mark.parametrize("filtered_rows", [(), (0, 1, 2), (3, 4)])
def test_stable_rows_visit_every_disc_once(offline_runner, monkeypatch, filtered_rows):
    runner = Runner(Preset(max_discs=100, accepted=()), hotkeys=False)
    grid = wire_grid(runner, monkeypatch, FakeGrid(total_rows=6, filtered_rows=filtered_rows))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())
    runner.run()
    assert runner.outcome == "completed"
    expected = [(r, c) for r in range(6) for c in range(GRID_COLS) if r not in filtered_rows]
    assert grid.visited == expected
    assert runner.processed_disc_count == len(expected)
    assert grid.scans == 2  # 开篇批 + 滚 2 行后的批，不再逐格重扫。
    offline_runner["press_key"].assert_not_called()


def test_no_disc_is_processed_twice_across_batches(offline_runner, monkeypatch):
    runner = Runner(Preset(max_discs=100, accepted=()), hotkeys=False)
    grid = wire_grid(runner, monkeypatch, FakeGrid(total_rows=9))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())
    runner.run()
    assert runner.outcome == "completed"
    assert len(grid.visited) == len(set(grid.visited)) == 9 * GRID_COLS


def test_filtered_rows_keep_global_slots(offline_runner, monkeypatch):
    # 前 3 行满级整行跳过：仍占全局格位，next_index 推进但处理数为 0。
    runner = Runner(Preset(max_discs=100, accepted=()), hotkeys=False)
    grid = wire_grid(runner, monkeypatch, FakeGrid(total_rows=6, filtered_rows=(0, 1, 2)))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())
    runner.run()
    assert runner.outcome == "completed"
    assert grid.visited == [(r, c) for r in (3, 4, 5) for c in range(GRID_COLS)]
    assert runner.processed_disc_count == 27


def test_skipped_cells_are_never_collected_as_candidates(offline_runner, monkeypatch):
    """扫描判定跳过的格位不能被收成待处理：越过跳过区、不点击、不计数。

    2026-10-02 真机：next_index 落在待处理区内、后方还有跳过格位时，旧逻辑把
    跳过格位一起收进 pending，白点白计数，提前吃掉 max_discs 配额（processed
    虚高、next_index 直接跳到跳过区之后）。
    """
    runner = Runner(Preset(max_discs=100, accepted=()), hotkeys=False)
    grid = wire_grid(runner, monkeypatch, FakeGrid(total_rows=4, filtered_rows=(1, 3)))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())
    runner.run()
    assert runner.outcome == "completed"
    assert grid.visited == [(row, col) for row in (0, 2) for col in range(GRID_COLS)]
    assert runner.processed_disc_count == 2 * GRID_COLS


def test_empty_candidate_batch_still_advances(offline_runner, monkeypatch):
    # 候选全被跳过的批次不结束任务，继续向下推进直到出现候选。
    runner = Runner(Preset(max_discs=1, accepted=()), hotkeys=False)
    grid = wire_grid(runner, monkeypatch, FakeGrid(total_rows=6, filtered_rows=(0, 1, 2, 3)))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())
    runner.run()
    assert runner.outcome == "completed"
    assert runner.processed_disc_count == 1
    assert grid.visited == [(4, 0)]


def test_scan_problems_exhaust_retry_and_fail(offline_runner, monkeypatch):
    runner = Runner(Preset(max_discs=5, accepted=(), ocr_retry=2), hotkeys=False)
    calls = {"n": 0}

    def scan(state):
        calls["n"] += 1
        return [make_cell(0, 0, 0)], ["标签(300,500)无法映射到行列"], {}

    monkeypatch.setattr(runner, "_scan_cells", scan)
    runner.run()
    assert runner.outcome == "failed"
    assert "网格映射不完整" in runner.end_reason
    assert calls["n"] == 2  # 含首次共 max(1,ocr_retry) 张
    offline_runner["click_client"].assert_not_called()


def test_max_pages_limits_work_batches(offline_runner, monkeypatch):
    runner = Runner(Preset(max_discs=1000, accepted=(), max_pages=2), hotkeys=False)
    grid = wire_grid(runner, monkeypatch, FakeGrid(total_rows=20))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())
    runner.run()
    assert runner.outcome == "completed"
    # 首批可点前 3 行（27 格），滚 2 行后第 2 批可点 2 行（18 格），共 45 格。
    assert runner.processed_disc_count == 27 + 18
    assert grid.scans == 2


def test_invalid_max_pages_fails_without_input(offline_runner, monkeypatch):
    runner = Runner(Preset(max_pages=0), hotkeys=False)
    runner.run()
    assert runner.outcome == "failed"
    assert "max_pages 非法" in runner.end_reason
    offline_runner["goto_warehouse"].assert_not_called()


def test_max_discs_stops_mid_batch_without_scrolling(offline_runner, monkeypatch):
    runner = Runner(Preset(max_discs=5, accepted=()), hotkeys=False)
    grid = wire_grid(runner, monkeypatch, FakeGrid(total_rows=6))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())
    runner.run()
    assert runner.outcome == "completed"
    assert runner.processed_disc_count == 5
    assert grid.visited == [(0, c) for c in range(5)]


@pytest.mark.parametrize("limits", [(2, 3), (None, None)])
def test_each_task_has_its_own_processing_limit(offline_runner, monkeypatch, limits):
    jobs = tuple(TaskJob(slot=slot, max_discs=limit, accepted=()) for slot, limit in zip((4, 5), limits))
    runner = Runner(Preset(task_chain=jobs, max_discs=1), hotkeys=False)
    runner.log = Mock()
    grid = wire_grid(runner, monkeypatch, FakeGrid(total_rows=6))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())
    runner.run()
    assert runner.outcome == "completed"
    first, second = (limit if limit is not None else 1 for limit in limits)
    assert runner.processed_disc_count == first + second
    assert grid.visited == [(0, c) for c in range(first)] + [(0, c) for c in range(second)]
    assert runner._job_limit_reached()
    offline_runner["apply_game_filter"].assert_has_calls([
        call(1, 1920, 1080, slots=[4], sets=(), mainstats=()),
        call(1, 1920, 1080, slots=[5], sets=(), mainstats=()),
    ])


# ---------------------------------------------------------------- 重扫与失败传播


# ---------------------------------------------------------------- 强化后恢复


def blank_image():
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    return image


def test_restore_position_returns_to_saved_viewport(offline_runner, monkeypatch):
    """恢复：置顶一次 → 按保存视口发送协议行数 → 提交保存的实际位置。"""
    runner = Runner(Preset(), hotkeys=False)
    runner._profile = fake_profile()
    state = _state()
    state.restore_request_limit_rows = max_restore_rows(20)
    state.confirm_viewport(10, 50.0, row_pitch_px=175.0, at_top=False, at_bottom=False)
    state.begin_upgrade(100)
    monkeypatch.setattr(runner, "_scan_cells", lambda s, **kw: ([make_cell(10, 0, 0)], [], {}))
    sent: list[int] = []

    def fake_scroll(hwnd, rows, profile, **kwargs):
        sent.append(rows)
        return ScrollSend(rows, rows, rows, profile.event_interval_ms)

    monkeypatch.setattr(runner_module, "scroll_rows", fake_scroll)
    runner._restore_position(1, state)
    assert sent == [11]  # ceil(10 + 50/175)
    assert (state.viewport_top_row0, state.row_phase_px) == (10, 50.0)
    assert state.resume_point is None  # 恢复完成即释放
    assert state.position_valid is True
    # 置顶必须带上本任务绑定的 hwnd，不许再在内部重新选窗口（spec §5.1）
    offline_runner["reset_to_top"].assert_called_once_with(1)
    assert runner._restored_scan is not None  # 校验扫描结果留给主循环复用


def test_restore_waits_for_scroll_settle_before_scan(offline_runner, monkeypatch):
    """恢复批量滚动发完必须等列表缓动停稳，再让校验扫描抓图。

    真机第 4 轮：不等稳定时「等级」标签整体偏 3px，压过 2% 容差（3.49px），
    本来正确的恢复被误判成两次失败。
    """
    sleeps: list[float] = []
    monkeypatch.setattr(runner_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    runner = Runner(Preset(), hotkeys=False)

    runner._await_scroll_settle(fake_profile())  # settle_wait_ms=100
    assert sleeps == [0.1]

    sleeps.clear()
    runner._await_scroll_settle(replace(fake_profile(), settle_wait_ms=0))
    assert sleeps == [runner_module._SETTLE_FALLBACK_MS / 1000]


def test_restore_settles_only_after_actually_scrolling(offline_runner, monkeypatch):
    """保存视口就在顶部（请求 0 行）时不发送也不等待。"""
    runner = Runner(Preset(), hotkeys=False)
    runner._profile = fake_profile()
    state = _state()
    state.restore_request_limit_rows = max_restore_rows(20)
    state.confirm_viewport(0, 0.0, row_pitch_px=175.0, at_top=True, at_bottom=False)
    state.begin_upgrade(3)
    monkeypatch.setattr(runner, "_scan_cells", lambda s, **kw: ([make_cell(0, 0, 0)], [], {}))
    slept: list[float] = []
    monkeypatch.setattr(runner_module.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr(runner_module, "scroll_rows", Mock())

    runner._restore_position(1, state)
    assert slept == []


def test_restore_retries_once_when_grid_unreadable(offline_runner, monkeypatch):
    """第一次恢复后网格读不出 → 允许一次完整重试；第二次成功。"""
    runner = Runner(Preset(), hotkeys=False)
    runner._profile = fake_profile()
    state = _state()
    state.restore_request_limit_rows = max_restore_rows(20)
    state.confirm_viewport(4, 0.0, row_pitch_px=175.0)
    state.begin_upgrade(36)
    results = iter(
        [
            ([make_cell(4, 0, 0)], ["标签(300,500)无法映射到行列"], {}),
            ([make_cell(4, 0, 0)], [], {}),
        ]
    )
    monkeypatch.setattr(runner, "_scan_cells", lambda s, **kw: next(results))
    runner._restore_position(1, state)
    assert state.restore_attempt == 2
    assert state.restore_retries == 1
    assert offline_runner["reset_to_top"].call_count == 2


def test_restore_twice_unreadable_fails(offline_runner, monkeypatch):
    """两次完整恢复后网格仍读不出：停止任务链，不再重试。"""
    runner = Runner(Preset(), hotkeys=False)
    runner._profile = fake_profile()
    state = _state()
    state.restore_request_limit_rows = max_restore_rows(20)
    state.confirm_viewport(4, 0.0, row_pitch_px=175.0)
    state.begin_upgrade(36)
    monkeypatch.setattr(runner, "_scan_cells", lambda s, **kw: ([], ["标签无法映射到行列"], {}))
    with pytest.raises(RunnerError, match="恢复保存视口失败"):
        runner._restore_position(1, state)
    assert state.restore_attempt == 2
    assert offline_runner["reset_to_top"].call_count == 2


def test_restore_over_limit_fails_before_any_input(offline_runner, monkeypatch):
    """恢复请求超过本任务上界 H：发送任何输入前 failed。"""
    runner = Runner(Preset(), hotkeys=False)
    runner._profile = fake_profile()
    state = _state()
    state.restore_request_limit_rows = 5
    state.confirm_viewport(10, 0.0, row_pitch_px=175.0)
    state.begin_upgrade(90)
    with pytest.raises(RunnerError, match="超过上界"):
        runner._restore_position(1, state)
    offline_runner["reset_to_top"].assert_not_called()
    assert state.restore_attempt == 0 and state.restore_retries == 0


def test_restore_without_protocol_fails(offline_runner, monkeypatch):
    """没有可用滚轮协议时不允许恢复，直接 failed。"""
    runner = Runner(Preset(), hotkeys=False)
    runner._profile = None
    state = _state()
    state.confirm_viewport(0, 0.0, row_pitch_px=175.0)
    state.begin_upgrade(0)
    with pytest.raises(RunnerError, match="滚轮协议"):
        runner._restore_position(1, state)


def test_restore_failure_stops_entire_chain(offline_runner, monkeypatch):
    """首批正常、恢复校验失败：failed 且不进入下一个 TaskJob。"""
    jobs = (
        generated_job(slot=4, weights={"暴击率": 1}, accepted=None),
        generated_job(slot=5, weights={"暴击率": 1}, accepted=None),
    )
    runner = Runner(Preset(task_chain=jobs, max_discs=5), hotkeys=False)
    calls = {"n": 0}

    def scan(state):
        calls["n"] += 1
        if calls["n"] == 1:
            return [make_cell(0, 0, 0), make_cell(1, 0, 1)], [], {}
        return [make_cell(0, 0, 0)], ["标签(300,500)无法映射到行列"], {}

    monkeypatch.setattr(runner, "_scan_cells", scan)
    monkeypatch.setattr(runner, "_advance", lambda hwnd, state, cells, *, rows: False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    monkeypatch.setattr(runner, "_upgrade_loop", Mock())
    runner.run()
    assert runner.outcome == "failed"
    assert "恢复保存视口失败" in runner.end_reason
    # 导航同样要绑定本任务的 hwnd（find_game_hwnd 在 fixture 里返回 1）
    offline_runner["goto_warehouse"].assert_called_once_with(1)


def test_upgrade_cap_does_not_open_upgrade_page(offline_runner, monkeypatch):
    """升阶到上限后不再进强化页：画面未变，按 done 继续用本批坐标。"""
    runner = Runner(Preset(accepted=None), hotkeys=False)
    state = _state()
    state.confirm_viewport(0, 0.0, row_pitch_px=175.0)
    monkeypatch.setattr(runner, "_grab", lambda *a, **kw: (blank_image(), "drive_disc", "test", 1))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    enter = Mock()
    monkeypatch.setattr(runner, "_enter_upgrade_page", enter)
    job = runner._default_job()
    result = runner._process_cell(1, job, state, make_cell(0, 0, 0), runner._sub_preset(job), None, {0: 6})
    assert result == "done"
    enter.assert_not_called()
    assert state.resume_point is None
    offline_runner["press_key"].assert_not_called()


def test_upgrade_saves_resume_point_before_entering_page(offline_runner, monkeypatch):
    """进强化页前必须保存恢复点；强化后提交该件进度并交给恢复流程。"""
    runner = Runner(Preset(accepted=None), hotkeys=False)
    state = _state()
    state.restore_request_limit_rows = max_restore_rows(20)
    state.confirm_viewport(2, 0.0, row_pitch_px=175.0, at_top=False)
    for index in range(18):
        state.set_status(index, CellStatus.DONE)
    state.commit_prefix()
    runner._profile = fake_profile()
    monkeypatch.setattr(runner, "_grab", lambda *a, **kw: (blank_image(), "drive_disc", "test", 1))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    seen: dict = {}

    def upgrade_loop(hwnd, job, global_index, cell_tag, preset, accepted_ids, up_counts, disc_slot):
        seen["resume"] = state.resume_point
        seen["active"] = state.active_index

    monkeypatch.setattr(runner, "_upgrade_loop", upgrade_loop)
    job = runner._default_job()
    result = runner._process_cell(1, job, state, make_cell(2, 0, 0), runner._sub_preset(job), None, {})
    assert result == "upgrade"
    assert seen["resume"] is not None and seen["resume"].viewport_top_row0 == 2
    assert seen["active"] == 18
    assert seen["resume"].next_index == 19
    assert state.next_index == 19  # finish_upgrade 已提交该件
    assert state.resume_point is not None  # 恢复完成前仍保留


def test_max_discs_reached_after_upgrade_does_not_restore(offline_runner, monkeypatch):
    """第 N 件强化后达 max_discs：直接结束，不为恢复原位置再切页/滚动。"""
    runner = Runner(generated_preset(max_discs=1, accepted=None), hotkeys=False)
    monkeypatch.setattr(
        runner, "_scan_cells", lambda s, **kw: ([make_cell(0, 0, 0), make_cell(1, 0, 1)], [], {})
    )
    monkeypatch.setattr(runner, "_advance", lambda hwnd, state, cells, *, rows: False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    monkeypatch.setattr(runner, "_upgrade_loop", Mock())
    runner.run()
    assert runner.outcome == "completed"
    assert runner.processed_disc_count == 1
    # 长列表筛选后不置顶（关面板时游戏已回顶），恢复也不再置顶：全程零切页。
    offline_runner["reset_to_top"].assert_not_called()


def test_navigation_profile_is_prepared_before_filter(offline_runner, monkeypatch):
    """滚轮标定必须发生在设置游戏筛选之前。

    标定要选第 3 行的盘做锚点；筛选会把列表裁短（实测只剩 16/3000，第 3 行起全是
    空格），锚点落在空格子上就选不中，标定必失败。标定与列表内容无关，应满屏时先做。
    """
    order = []
    runner = Runner(generated_preset(max_discs=1, accepted=None), hotkeys=False)
    monkeypatch.setattr(Runner, "_ensure_profile", lambda self, hwnd: order.append("profile"))
    monkeypatch.setattr(
        Runner,
        "_prepare_navigation",
        lambda self, hwnd, state: runner_module.NavigationPrep(
            profile=fake_profile(), mode="scrolling"
        ),
    )
    offline_runner["apply_game_filter"].side_effect = lambda *a, **kw: order.append("filter")
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    monkeypatch.setattr(runner, "_upgrade_loop", Mock())
    runner.run()
    assert order[:2] == ["profile", "filter"], order


def test_ensure_profile_resets_filter_before_calibrating(offline_runner, monkeypatch):
    """标定前必须先重置筛选：残留筛选会裁短列表（实测 16/3000），第 3 行锚点落在
    空格上就选不中，标定必失败。标定要求列表满屏。"""
    calls = []
    monkeypatch.setattr(runner_module, "reset_game_filter", lambda *a: calls.append("reset"))
    runner = Runner(Preset(max_discs=1, accepted=None), hotkeys=False)
    runner._profile = None
    runner._ensure_profile(1)
    assert calls == ["reset"]
    assert runner._profile is not None


def test_dismiss_material_refund_only_clicks_confirm_when_popup_present(offline_runner, monkeypatch):
    """满级弹「材料返还」时点「确认」；没这个弹窗时绝不能乱点。

    这个弹窗会盖住强化页，让等级/副属性几个 OCR 区域全空；不处理就误报
    「未识别出盘面」中断整条任务链（2026-10-04 全链路实测）。
    """
    from zzz_ddtool import ocr as ocr_module

    clicks = []
    monkeypatch.setattr(runner_module, "click_client", lambda hwnd, x, y: clicks.append((x, y)))
    runner = Runner(Preset(max_discs=1, accepted=None), hotkeys=False)
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)

    def patch(texts):
        monkeypatch.setattr(
            ocr_module,
            "read_rect",
            lambda *a: [ocr_module.OcrLine(t, 1.0, 100, 200, 300, 240) for t in texts],
        )

    # 页面上有「确认」（别处的按钮）但没有「材料返还」标题 → 绝不能点
    patch(["等级15", "攻击力", "暴击率", "确认"])
    assert runner._dismiss_material_refund(1, image) is False
    assert clicks == []

    patch(["材料返还", "强化镀剂", "4", "确认"])
    assert runner._dismiss_material_refund(1, image) is True
    assert clicks == [(200, 220)]

    clicks.clear()
    patch(["材料返还", "强化镀剂"])
    assert runner._dismiss_material_refund(1, image) is False
    assert clicks == []


def test_click_leaving_page_recovers_and_rescans(offline_runner, monkeypatch):
    runner = Runner(Preset(max_discs=3, accepted=()), hotkeys=False)
    grid = wire_grid(runner, monkeypatch, FakeGrid(total_rows=6))
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    calls = {"n": 0}

    def grab(*args, **kwargs):
        calls["n"] += 1
        # 第 1 次是任务初始抓图，第 2 次（点击格子后）才模拟点飞
        return (image, "menu" if calls["n"] == 2 else "drive_disc", "test", 1)

    monkeypatch.setattr(runner, "_grab", grab)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())
    runner.run()
    assert runner.outcome == "completed"
    assert offline_runner["press_key"].call_count == 1  # 只按一次 Esc 退回
    # 点飞那格未处理成功，重扫后从同一格重新开始
    assert grid.visited == [(0, 0), (0, 0), (0, 1), (0, 2)]


def test_click_leaving_page_unrecoverable_fails(offline_runner, monkeypatch):
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    runner = Runner(Preset(max_discs=3, accepted=()), hotkeys=False)
    monkeypatch.setattr(
        runner, "_scan_cells", lambda state, **kw: ([make_cell(0, 0, 0), make_cell(1, 0, 1)], [], {})
    )
    monkeypatch.setattr(runner, "_grab", lambda *a, **kw: (image, "menu", "test", 1))
    runner.run()
    assert runner.outcome == "failed"
    assert "退回失败" in runner.end_reason


def test_scan_on_menu_is_failure_instead_of_empty_page(monkeypatch):
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    monkeypatch.setattr(runner_module, "grab_game_image", lambda hwnd=None: (image, "test", 1))
    read = Mock()
    monkeypatch.setattr(ocr_module, "read_rect", read)
    import zzz_ddtool.screen as screen_module

    monkeypatch.setattr(screen_module, "classify_image", lambda img: "menu")
    with pytest.raises(RunnerError, match="扫描前页面错误：menu"):
        runner._scan_cells(_state())
    read.assert_not_called()


def _state():
    from zzz_ddtool.traversal import TraversalState

    return TraversalState(job_id="t")


# ---------------------------------------------------------------- 决策与强化路径


def sample_disc(level=0):
    return Disc(
        level=level,
        substats=tuple(Substat(name) for name in ("暴击率", "暴击伤害", "生命值", "防御力")),
    )


@pytest.mark.parametrize("accepted, expected", [(None, None), ((), Action.MARK_DISCARD)])
def test_run_preserves_empty_whitelist(offline_runner, monkeypatch, accepted, expected):
    job = TaskJob(slot=4, weights={"暴击率": 1.0}, accepted=accepted)
    runner = Runner(Preset(task_chain=(job,), max_discs=1), hotkeys=False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    actions = []
    monkeypatch.setattr(runner, "_act", lambda hwnd, action, disc: actions.append(action))

    def upgrade_loop(hwnd, job, global_index, cell_tag, preset, accepted_ids, up_counts, disc_slot):
        actions.append(decide(sample_disc(), preset, accepted_ids=accepted_ids).action)

    monkeypatch.setattr(runner, "_upgrade_loop", upgrade_loop)
    runner.run()
    assert actions == ([] if expected is None else [expected])
    if accepted is None:
        assert runner.processed_disc_count == 0
        assert "未生成" in runner.end_reason
        for mock in offline_runner.values():
            mock.assert_not_called()


def test_ungenerated_tasks_are_skipped_and_valid_tasks_keep_order(offline_runner, monkeypatch):
    jobs = (
        TaskJob(slot=1, accepted=None, max_discs=99),
        TaskJob(slot=4, accepted=(), max_discs=2),
        TaskJob(slot=2, accepted=None, max_discs=99),
        generated_job(slot=5, weights={"暴击率": 1}, max_discs=3),
    )
    runner = Runner(Preset(task_chain=jobs), hotkeys=False)
    run_job = Mock()
    monkeypatch.setattr(runner, "_run_job", run_job)
    runner.run()
    assert [call.args[0].job_id for call in run_job.call_args_list] == [jobs[1].job_id, jobs[3].job_id]
    assert runner.outcome == "completed"
    assert "跳过 2 个未生成任务" in runner.end_reason
    assert [call.args[0] for call in runner._set_status.call_args_list].count('job_skipped') == 2
    assert sum(runner._job_limit(job) for job in runner._jobs() if job.accepted is not None) == 5
    assert runner.stats.jobs[0].processed == runner.stats.jobs[2].processed == 0


def test_direct_ungenerated_job_never_binds_or_sends_game_input(offline_runner, monkeypatch):
    runner = Runner(Preset(), hotkeys=False)
    bind = Mock(side_effect=AssertionError("An ungenerated task must not bind the game"))
    monkeypatch.setattr(runner, "_bind_instance", bind)
    runner._run_job(TaskJob(slot=4, weights={"暴击率": 1}))
    bind.assert_not_called()
    for mock in offline_runner.values():
        mock.assert_not_called()


@pytest.mark.parametrize("page", ["detail", "upgrade"])
def test_uncertain_origin_stops_entire_chain(offline_runner, monkeypatch, page):
    jobs = (generated_job(slot=4, weights={"暴击率": 1.0}), generated_job(slot=5, weights={"暴击率": 1.0}))
    runner = Runner(Preset(task_chain=jobs), hotkeys=False)
    uncertain = sample_disc(level=6)  # +6 却无强化次数，三/四词来源均不成立。
    monkeypatch.setattr(runner, "_parse_disc", lambda image: uncertain if page == "detail" else sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    monkeypatch.setattr(ocr_module, "parse_disc_upgrade_page", lambda image: uncertain)
    upgrade = Mock()
    discard = Mock()
    back = Mock()
    monkeypatch.setattr(runner, "_upgrade_once", upgrade)
    monkeypatch.setattr(runner, "_discard_from_upgrade", discard)
    monkeypatch.setattr(runner, "_esc_back_to_grid", back)

    results = runner.run()

    assert offline_runner["goto_warehouse"].call_count == 1
    assert offline_runner["click_client"].call_count == 1
    assert [(result.job_id, result.action, result.reason) for result in results] == [
        (jobs[0].job_id, "stop", "origin_ambiguous")
    ]
    upgrade.assert_not_called()
    discard.assert_not_called()
    back.assert_not_called()
    offline_runner["press_key"].assert_not_called()
    offline_runner["press_key_once"].assert_not_called()


def test_read_zoom_level_line_retries_until_readable(offline_runner, monkeypatch):
    """缩放页抓图可能落在缩放动画里：读不到等级要重拍，而不是直接判失败崩链。"""
    runner = Runner(Preset(), hotkeys=False)
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    monkeypatch.setattr(runner, "_grab", lambda **kw: (image, "unknown", "test", 1))
    line = ocr_module.OcrLine("等级03", 1, 1200, 200, 1310, 245)
    calls = {"n": 0}

    def read_rect(img, area):
        calls["n"] += 1
        return [] if calls["n"] == 1 else [line]

    monkeypatch.setattr(ocr_module, "read_rect", read_rect)

    got, _image = runner._read_zoom_level_line(1)

    assert got is line
    assert calls["n"] == 2


def test_read_zoom_level_line_gives_up_after_retries(offline_runner, monkeypatch):
    """重拍次数用尽仍读不到，返回 None，由调用方走回退。"""
    runner = Runner(Preset(), hotkeys=False)
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    monkeypatch.setattr(runner, "_grab", lambda **kw: (image, "unknown", "test", 1))
    calls = {"n": 0}

    def read_rect(img, area):
        calls["n"] += 1
        return []

    monkeypatch.setattr(ocr_module, "read_rect", read_rect)

    got, _image = runner._read_zoom_level_line(1)

    assert got is None
    assert calls["n"] == runner.preset.ocr_retry


def button_lines(runner, monkeypatch, missing=None):
    stage = ocr_module.OcrLine("阶段添加", 1, 1200, 840, 1360, 880)
    confirm = ocr_module.OcrLine("升级", 1, 1460, 840, 1560, 880)

    def read_rect(image, area):
        if area == runner.STAGE_ADD_AREA:
            return [] if missing == "stage" else [stage]
        if area == runner.UPGRADE_CONFIRM_AREA:
            return [] if missing == "confirm" else [confirm]
        raise AssertionError(f"unexpected area: {area}")

    monkeypatch.setattr(ocr_module, "read_rect", read_rect)


def test_run_calls_real_upgrade_buttons_then_rejudges(offline_runner, monkeypatch):
    job = generated_job(slot=4, weights={"暴击率": 1}, stage_require={"four": {0: 0, 3: 999}})
    runner = Runner(Preset(task_chain=(job,), max_discs=1), hotkeys=False)
    initial = sample_disc()
    upgraded = Disc(3, (Substat("暴击率", 2),) + initial.substats[1:])
    readings = iter((initial, upgraded))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: initial)
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    parser = Mock(side_effect=lambda image: next(readings))
    monkeypatch.setattr(ocr_module, "parse_disc_upgrade_page", parser)
    discard = Mock()
    monkeypatch.setattr(runner, "_discard_from_upgrade", discard)
    button_lines(runner, monkeypatch)
    runner.run()  # 不替换 _upgrade_loop / _upgrade_once / _click_text
    assert runner.outcome == "completed"
    assert runner.processed_disc_count == 1
    assert [(r.action, r.level) for r in runner.results] == [("upgrade", 0), ("mark_discard", 3)]
    assert offline_runner["click_client"].call_args_list == [
        call(1, 166, 267),
        call(1, 1280, 860),
        call(1, 1510, 860),
    ]
    assert parser.call_count == 2
    discard.assert_called_once_with(1)


def test_upgrade_page_falls_back_to_detail_layout_when_maxed(offline_runner, monkeypatch):
    """满级后强化界面切成展示态：强化页坐标读不到等级，必须回退详情页坐标解析。

    真机第 5 轮实测：12→15 满级确认后 UPGRADE_LEVEL 区域无文字、DISC_LEVEL 读到
    「等级15」，原实现直接报「未识别出盘面」把整条任务链打断。
    """
    job = generated_job(
        slot=4,
        weights={"暴击率": 1},
        stage_require={"four": {0: 0, 3: 0, 6: 0, 9: 0, 12: 0}},
    )
    runner = Runner(Preset(task_chain=(job,), max_discs=1), hotkeys=False)
    initial = sample_disc()
    # +15 四词条盘：强化 5 次全部落在已有词条上（rolls 6 = 1+5），origin 才能推断为 four
    maxed = Disc(15, (Substat("暴击率", 6),) + initial.substats[1:])
    monkeypatch.setattr(runner, "_parse_disc", lambda image: initial)
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    monkeypatch.setattr(ocr_module, "parse_disc_upgrade_page", lambda image: None)
    fallback = Mock(return_value=maxed)
    monkeypatch.setattr(ocr_module, "parse_disc", fallback)
    back = Mock(return_value=True)
    monkeypatch.setattr(runner, "_esc_back_to_grid", back)

    runner.run()

    assert runner.outcome == "completed"
    assert fallback.called, "满级展示态必须回退详情页坐标解析"
    assert [(r.action, r.level, r.reason) for r in runner.results] == [
        ("skip", 15, "keep_max_level")
    ]
    back.assert_called_once()


def test_upgrade_page_fails_when_both_layouts_unreadable(offline_runner, monkeypatch):
    """强化页与详情页两套坐标都读不出时，仍按原语义报错停止任务链。"""
    job = generated_job(slot=4, weights={"暴击率": 1}, stage_require={"four": {0: 0}})
    runner = Runner(Preset(task_chain=(job,), max_discs=1), hotkeys=False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc(0))
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    monkeypatch.setattr(ocr_module, "parse_disc_upgrade_page", lambda image: None)
    monkeypatch.setattr(ocr_module, "parse_disc", lambda image: None)

    runner.run()

    assert runner.outcome == "failed"
    assert "未识别出盘面" in runner.end_reason


@pytest.mark.parametrize("missing, click_count", [("stage", 0), ("confirm", 1)])
def test_upgrade_button_missing_does_not_click_next(offline_runner, monkeypatch, missing, click_count):
    runner = Runner(Preset(), hotkeys=False)
    button_lines(runner, monkeypatch, missing=missing)
    assert runner._upgrade_once(99) is False
    assert offline_runner["click_client"].call_count == click_count
    if click_count:
        offline_runner["click_client"].assert_called_once_with(99, 1280, 860)


def test_missing_upgrade_button_stops_later_jobs(offline_runner, monkeypatch):
    jobs = (generated_job(slot=4, weights={"暴击率": 1}), generated_job(slot=5, weights={"暴击率": 1}))
    runner = Runner(Preset(task_chain=jobs), hotkeys=False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    monkeypatch.setattr(ocr_module, "parse_disc_upgrade_page", lambda image: sample_disc())
    button_lines(runner, monkeypatch, missing="stage")
    runner.run()
    assert runner.outcome == "failed"
    assert "强化按钮识别失败" in runner.end_reason
    offline_runner["goto_warehouse"].assert_called_once()
    assert offline_runner["click_client"].call_count == 1  # 只有初始点选盘
    offline_runner["press_key"].assert_not_called()


def test_return_from_warehouse_does_not_press_escape(offline_runner, monkeypatch):
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    runner = Runner(Preset(), hotkeys=False)
    monkeypatch.setattr(runner, "_grab", lambda **kw: (image, "drive_disc", "test", 1))
    runner._discard_from_upgrade(1)
    offline_runner["press_key"].assert_not_called()
    offline_runner["press_key_once"].assert_called_once_with(1, runner_module.VK_R)


def test_auto_substat_filter_is_applied_for_each_task(offline_runner, monkeypatch):
    jobs = (
        generated_job(slot=4, weights={"暴击率": 1, "暴击伤害": 1}, stage_require={"three": {0: 2}, "four": {0: 2}}),
        generated_job(slot=5, weights={"穿透值": 1}, stage_require={"three": {0: 1}, "four": {0: 1}}),
    )
    runner = Runner(Preset(task_chain=jobs, max_discs=1), hotkeys=False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    monkeypatch.setattr(runner, "_upgrade_loop", Mock())
    monkeypatch.setattr(runner, "_restore_position", Mock())
    runner.run()
    assert runner.outcome == "completed"
    filters = offline_runner["apply_game_filter"].call_args_list
    assert set(filters[0].kwargs["substats"]) == {"暴击率", "暴击伤害"}
    assert filters[0].kwargs["substat_counts"] == (2, 3, 4)
    assert filters[1].kwargs["substats"] == ("穿透值",)
    assert filters[1].kwargs["substat_counts"] == (1, 2, 3, 4)


def test_disabled_labels_suppress_all_discard_changes_but_return_to_grid(offline_runner, monkeypatch):
    from zzz_ddtool.config import LabelPolicy

    runner = Runner(Preset(labels=LabelPolicy(discard=False, save=False)), hotkeys=False)
    back = Mock()
    monkeypatch.setattr(runner, "_esc_back_to_grid", back)
    runner._act(1, Action.MARK_DISCARD, sample_disc())
    runner._set_discard_label(1, discarded=True, value=False)
    runner._discard_from_upgrade(1)
    offline_runner["press_key_once"].assert_not_called()
    back.assert_called_once_with(1)
    assert runner._sub_preset(TaskJob(slot=4)).labels == runner.preset.labels


def test_return_from_zoom_preview_escapes_then_r(offline_runner, monkeypatch):
    """强化页 Esc 一层后停在缩放预览层：认出来再 Esc 一次，不能当未知画面卡住。"""
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    frames = iter([(image, "unknown", "test", 1), (image, "drive_disc", "test", 1)])
    runner = Runner(Preset(), hotkeys=False)
    monkeypatch.setattr(runner, "_grab", lambda **kw: next(frames))
    monkeypatch.setattr(screen_module, "is_disc_preview", lambda img: True)
    runner._discard_from_upgrade(1)
    assert offline_runner["press_key_once"].call_args_list == [
        call(1, runner_module.VK_ESCAPE),
        call(1, runner_module.VK_R),
    ]
    offline_runner["press_key"].assert_not_called()


@pytest.mark.parametrize("layers", [1, 2])
def test_discard_returns_only_existing_modal_layers(offline_runner, monkeypatch, layers):
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    frames = iter([(image, "unknown", "test", 1)] * layers + [(image, "drive_disc", "test", 1)])
    runner = Runner(Preset(), hotkeys=False)
    import zzz_ddtool.screen as screen_module

    monkeypatch.setattr(runner, "_grab", lambda **kw: next(frames))
    monkeypatch.setattr(screen_module, "is_disc_modal", lambda img: True)
    runner._discard_from_upgrade(1)
    assert offline_runner["press_key_once"].call_args_list == [
        call(1, runner_module.VK_ESCAPE)
    ] * layers + [call(1, runner_module.VK_R)]
    offline_runner["press_key"].assert_not_called()


@pytest.mark.parametrize("page", ["menu", "overworld", "storage"])
def test_return_from_wrong_page_stops_without_keypress(offline_runner, monkeypatch, page):
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    runner = Runner(Preset(), hotkeys=False)
    monkeypatch.setattr(runner, "_grab", lambda **kw: (image, page, "test", 1))
    with pytest.raises(RunnerError, match="返回驱动盘仓库失败"):
        runner._discard_from_upgrade(1)
    offline_runner["press_key_once"].assert_not_called()
    offline_runner["press_key"].assert_not_called()


def test_unrecognized_return_retries_without_escape(offline_runner, monkeypatch):
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    runner = Runner(Preset(ocr_retry=3), hotkeys=False)
    grab = Mock(return_value=(image, "unknown", "test", 1))
    import zzz_ddtool.screen as screen_module

    monkeypatch.setattr(runner, "_grab", grab)
    monkeypatch.setattr(screen_module, "is_disc_modal", lambda img: False)
    with pytest.raises(RunnerError, match="无法确认盘面弹窗或仓库"):
        runner._discard_from_upgrade(1)
    assert grab.call_count == 3
    offline_runner["press_key_once"].assert_not_called()


@pytest.mark.parametrize("level,rolls", [(3, 2), (15, 6)])
def test_return_failure_fails_entire_chain(offline_runner, monkeypatch, level, rolls):
    jobs = (
        generated_job(slot=4, weights={"暴击率": 1}, stage_require={"four": {3: 999}}),
        generated_job(slot=5, weights={"暴击率": 1}),
    )
    runner = Runner(Preset(task_chain=jobs), hotkeys=False)
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    frames = iter([(image, "unknown", "test", 1)] * 3 + [(image, "menu", "test", 1)])
    monkeypatch.setattr(runner, "_grab", lambda *a, **kw: next(frames))
    # +3 判弃置 / +15 判保留：两条真实回退分支都必须传播失败，不能继续 job。
    disc = Disc(level, (Substat("暴击率", rolls),) + sample_disc().substats[1:])
    monkeypatch.setattr(ocr_module, "parse_disc_upgrade_page", lambda image: disc)
    runner.run()
    assert runner.outcome == "failed"
    assert "返回驱动盘仓库失败" in runner.end_reason
    offline_runner["goto_warehouse"].assert_called_once()
    offline_runner["press_key_once"].assert_not_called()


def test_enter_upgrade_failure_cannot_complete(offline_runner, monkeypatch):
    runner = Runner(generated_preset(), hotkeys=False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: False)
    runner.run()
    assert runner.outcome == "failed"
    assert "进入强化页失败" in runner.end_reason


# ---------------------------------------------------------------- 测试跳号入口


def seed_state(limit_rows=39):
    state = TraversalState(job_id="j")
    state.restore_request_limit_rows = limit_rows
    return state


def spy_scroll(monkeypatch, sent):
    def fake_scroll(hwnd, rows, profile, **kwargs):
        sent.append(rows)
        total = rows * profile.events_per_row
        return ScrollSend(rows, total, total, profile.event_interval_ms)

    monkeypatch.setattr(runner_module, "scroll_rows", fake_scroll)


def test_start_index_seeds_prefix_and_lands_on_target_row(offline_runner, monkeypatch):
    """--start-index 把起点摆到指定序号所在行，前面的格标为已跳过。"""
    sent: list[int] = []
    spy_scroll(monkeypatch, sent)
    runner = Runner(Preset(max_discs=1), hotkeys=False, start_index=99)
    runner._profile = fake_profile()
    runner._row_pitch = 175.0
    state = seed_state()

    runner._seed_start_index(1, state, runner.start_index)

    assert state.next_index == 99
    assert state.viewport_top_row0 == 10  # 序号 99 = 全局第 12 行第 1 列 → 视口顶部第 11 行
    assert state.row_phase_px == 0.0
    assert state.at_top is False
    assert state.position_valid is True
    assert state.status_of(99) is CellStatus.PENDING  # 起点本身不被标跳过
    # 筛选后不置顶（关面板时游戏已回顶），跳号只做「滚到目标行」
    offline_runner["reset_to_top"].assert_not_called()
    assert sent == [10]


def test_start_index_inside_first_row_needs_no_scroll(offline_runner, monkeypatch):
    """起点落在第一屏内时不发滚动，视口仍按「已到顶」处理。"""
    sent: list[int] = []
    spy_scroll(monkeypatch, sent)
    runner = Runner(Preset(max_discs=1), hotkeys=False, start_index=5)
    runner._profile = fake_profile()
    runner._row_pitch = 175.0
    state = seed_state()

    runner._seed_start_index(1, state, 5)

    assert state.next_index == 5
    assert state.viewport_top_row0 == 0
    assert state.at_top is True
    assert sent == []


def test_start_index_does_not_touch_earlier_cells(offline_runner, monkeypatch):
    """跳号启动后首个处理的就是起点本身，序号更小的格位一次都不点。"""
    runner = Runner(Preset(max_discs=1, accepted=()), hotkeys=False, start_index=99)
    grid = wire_grid(runner, monkeypatch, FakeGrid(total_rows=16))
    grid.offset = 10  # 跳号把视口摆到全局第 11 行
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())

    runner.run()

    assert runner.outcome == "completed"
    assert grid.visited == [(11, 0)]  # 内部序号 99
    assert runner.processed_disc_count == 1


def test_start_index_defaults_to_zero(offline_runner):
    assert Runner(Preset(), hotkeys=False).start_index == 0
    assert Runner(Preset(), hotkeys=False, start_index=-5).start_index == 0


def test_start_index_aligns_batch_count_with_viewport(offline_runner, monkeypatch):
    """跳号到深处后批次计数按等效批次对齐。

    真机回归：start-index 134 把视口摆到全局第 14 行，批次计数仍是 1，
    下一次正常前进时 check_forward_move 按「每批 2 行」算出的上界只有 2，
    于是合法的视口被判「第 2 批顶部行 13 超过上界 2」，整轮 failed。
    """
    sent: list[int] = []
    spy_scroll(monkeypatch, sent)
    runner = Runner(Preset(max_discs=1), hotkeys=False, start_index=134)
    runner._profile = fake_profile()
    runner._row_pitch = 175.0
    state = seed_state()
    state.start_first_batch()

    runner._seed_start_index(1, state, runner.start_index)

    assert state.viewport_top_row0 == 13
    assert state.work_batch_count == forward_batch_for_top(13) == 8
    assert state.work_batch_id == state.work_batch_count
    # 下一次前进的批次上界必须放行这个顶部行
    check_forward_move(state.work_batch_count + 1, 13, 13, 2)
    assert sent == [13]


# ---------------------------------------------------------------- 末尾空位与到底判定

FIXTURE_TAIL = Path(__file__).parent / "fixtures" / "list-tail-empty-slot.png"
TAIL_ORIGIN_Y = 267.5
TAIL_PITCH = 174.0


def tail_image():
    image = QImage(str(FIXTURE_TAIL))
    assert not image.isNull(), FIXTURE_TAIL
    return image


def tail_runner():
    runner = Runner(Preset(), hotkeys=False)
    runner._origin_center_y = TAIL_ORIGIN_Y
    runner._row_pitch = TAIL_PITCH
    return runner


def tail_state():
    state = _state()
    state.confirm_viewport(0, 0.0, row_pitch_px=TAIL_PITCH, at_top=True, at_bottom=False)
    return state


def test_scan_confirms_tail_empty_slot_from_tile_placeholder():
    """真机图重放（驱动仓库 [152/3000] 末行 8 件）：空位由图标区占位确认，不是「没读到等级」。"""
    runner = tail_runner()
    runner.trace = Mock()
    runner._grab = Mock(return_value=(tail_image(), "drive_disc", "test", 1))

    cells, problems, _skips = runner._scan_cells(tail_state())

    assert problems == []
    assert len(cells) == 35
    assert runner._tail_slots_confirmed is True


def test_scan_flags_disc_without_level_label_as_barrier(monkeypatch):
    """有盘但没读到等级：占位判据认出「有盘」，走进度屏障，既不当空位也不丢盘。"""
    runner = tail_runner()
    runner.trace = Mock()
    runner._grab = Mock(return_value=(tail_image(), "drive_disc", "test", 1))
    real_read = ocr_module.read_rect

    def read_rect(image, area):
        kept = []
        for line in real_read(image, area):
            cx = (line.left + line.right) / 2
            cy = (line.top + line.bottom) / 2
            if abs(cx - 706) < 45 and abs(cy - 877) < 30:  # 末行第 5 列，模拟漏读
                continue
            kept.append(line)
        return kept

    monkeypatch.setattr(ocr_module, "read_rect", read_rect)
    cells, problems, _skips = runner._scan_cells(tail_state())

    assert len(cells) == 34
    assert any("有盘但没读到等级" in problem for problem in problems)


def test_advance_requires_tail_placeholder_before_marking_bottom(monkeypatch):
    """锚点被端点截停但末尾占位未确认时保留未知，不置 at_bottom（spec §5.4）。"""
    runner = Runner(Preset(), hotkeys=False)
    runner._profile = fake_profile()
    runner._origin_center_y = 267.0
    runner._row_pitch = 175.0
    runner._set_status = Mock()
    monkeypatch.setattr(runner_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(runner_module, "click_client", Mock())
    monkeypatch.setattr(runner_module, "grab_game_image", lambda hwnd=None: (None, "test", 1))
    monkeypatch.setattr(
        runner_module,
        "scroll_rows",
        lambda hwnd, rows, profile, **kw: ScrollSend(rows, rows, rows, profile.event_interval_ms),
    )
    state = _state()
    for _ in range(5):
        state.advance_batch()
    state.confirm_viewport(10, 0.0, row_pitch_px=175.0, at_top=False, at_bottom=False)
    anchor = make_cell(20, 0, 2)
    monkeypatch.setattr(runner_module, "anchor_screen_rows", lambda image, **kw: [anchor.screen_row0])
    monkeypatch.setattr(runner_module, "settle_and_read_anchor", lambda *a, **kw: ([2], 100, None))

    runner._tail_slots_confirmed = False
    assert runner._advance(1, state, [anchor], rows=2) is False
    assert state.at_bottom is False

    runner._tail_slots_confirmed = True
    assert runner._advance(1, state, [anchor], rows=2) is True
    assert state.at_bottom is True


def test_single_screen_tail_needs_confirmed_placeholder_and_no_anchor():
    """整屏小列表直接判到底的三个条件：贴顶、末尾占位确认、没有中间行锚点。"""
    runner = Runner(Preset(), hotkeys=False)
    state = _state()
    state.confirm_viewport(0, 0.0, row_pitch_px=175.0, at_top=True, at_bottom=False)
    cells = [make_cell(0, col, 0) for col in range(5)]

    runner._tail_slots_confirmed = True
    assert runner._single_screen_tail(state, cells) is True

    runner._tail_slots_confirmed = False
    assert runner._single_screen_tail(state, cells) is False

    runner._tail_slots_confirmed = True
    assert runner._single_screen_tail(state, cells + [make_cell(1, 0, 1)]) is False


def test_single_row_list_is_flushed_as_bottom(offline_runner, monkeypatch):
    """不足一行满 9 件的小列表：游戏不会滚动，必须靠末尾占位判到底并补齐末行。"""
    runner = Runner(Preset(max_discs=10, accepted=()), hotkeys=False)
    grid = wire_grid(runner, monkeypatch, FakeGrid(total_rows=1, last_row_cols=5))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())

    runner.run()

    assert runner.outcome == "completed"
    assert len(grid.visited) == 5
    assert runner.processed_disc_count == 5
    assert grid.scans == 1  # 确认短列表到底只改变遍历状态，复用同一帧解除末行延期。


def test_single_row_filtered_list_reuses_scan_without_clicks(offline_runner, monkeypatch):
    runner = Runner(Preset(max_discs=10, accepted=()), hotkeys=False)
    grid = wire_grid(runner, monkeypatch, FakeGrid(total_rows=1, filtered_rows=(0,), last_row_cols=5))

    runner.run()

    assert runner.outcome == "completed"
    assert grid.scans == 1
    assert grid.visited == []
    assert runner.processed_disc_count == 0


def test_failed_filter_is_included_in_action_timing(offline_runner):
    runner = Runner(Preset(max_discs=10, accepted=()), hotkeys=False)
    offline_runner["apply_game_filter"].side_effect = RunnerError("filter failed")

    runner.run()

    assert runner.outcome == "failed"
    spans = [span for span in runner.stats.actions if span.page == "筛选" and span.action == "设置游戏筛选"]
    assert len(spans) == 1
    assert spans[0].seconds >= 0


def test_empty_filter_finishes_job_and_continues_chain(offline_runner, monkeypatch):
    from zzz_ddtool.navigation import NavigationError

    runner = Runner(Preset(max_discs=1, task_chain=(
        TaskJob(slot=4, accepted=()), TaskJob(slot=5, accepted=()),
    )), hotkeys=False)
    wire_grid(runner, monkeypatch, FakeGrid(total_rows=1, last_row_cols=1))
    monkeypatch.setattr(runner, "_ensure_profile", Mock())
    runner._profile = fake_profile()
    # 任务 1：筛选后读不到「等级」标签且确认空仓 → 正常结束本任务。
    # 任务 2：读得出布局；长列表路径会「读一次 + 置顶后再读一次」，所以给两次值。
    monkeypatch.setattr(runner_module, "measure_layout", Mock(side_effect=[
        NavigationError("no labels"), (267.0, 175.0), (267.0, 175.0),
    ]))
    monkeypatch.setattr(runner_module, "is_empty_warehouse", lambda image: True)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())

    runner.run()

    assert runner.outcome == "completed"
    assert runner.stats.jobs[0].processed == 0
    assert runner.stats.jobs[1].processed == 1
    assert offline_runner["apply_game_filter"].call_count == 2


def test_missing_labels_without_empty_evidence_still_fails(offline_runner, monkeypatch):
    from zzz_ddtool.navigation import NavigationError

    runner = Runner(Preset(max_discs=1, accepted=()), hotkeys=False)
    monkeypatch.setattr(runner, "_ensure_profile", Mock())
    monkeypatch.setattr(runner_module, "measure_layout", Mock(side_effect=NavigationError("no labels")))
    monkeypatch.setattr(runner_module, "is_empty_warehouse", lambda image: False)

    runner.run()

    assert runner.outcome == "failed"
    assert runner.processed_disc_count == 0


# ---------------------------------------------------------- 单行小列表真机重放

FIXTURE_SINGLE_ROW = Path(__file__).parent / "fixtures" / "list-single-row-three-discs.png"


def test_scan_confirms_single_row_list_tail():
    """真机图重放（谶羽之誓 4 号位 + 攻击力% [3/3000]）：3 件贴首行、其余 33 格空位。"""
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    runner._origin_center_y = 268.5
    runner._row_pitch = 174.5
    image = QImage(str(FIXTURE_SINGLE_ROW))
    assert not image.isNull(), FIXTURE_SINGLE_ROW
    runner._grab = Mock(return_value=(image, "drive_disc", "test", 1))
    state = _state()
    state.confirm_viewport(0, 0.0, row_pitch_px=174.5, at_top=True, at_bottom=False)

    cells, problems, _skips = runner._scan_cells(state)

    assert problems == []
    assert sorted((cell.screen_row0, cell.screen_col0) for cell in cells) == [
        (0, 0),
        (0, 1),
        (0, 2),
    ]
    assert runner._tail_slots_confirmed is True
    # 中间行没有可观测锚点 → 整屏小列表直接判到底（真机首次触发的那条路）
    assert runner._single_screen_tail(state, cells) is True


# ---------------------------------------------------- 升级素材不足（升完到不了下一档）


def test_next_stage_level_is_block_end():
    """期望等级是「本档末尾」（3/6/9/12/15），不是 level + 3。

    强化页「阶段添加」按整个阶段补素材：从档内任意等级出发都补到本档末尾。
    """
    from zzz_ddtool.models import next_stage_level

    assert [next_stage_level(level) for level in range(0, 16)] == [
        3, 3, 3, 6, 6, 6, 9, 9, 9, 12, 12, 12, 15, 15, 15, 15
    ]


def test_upgrade_shortfall_stops_chain(offline_runner, monkeypatch):
    """升级后等级到不了下一档 = 素材耗尽：必须报错，不许当「到升阶上限」收尾。

    素材不够吃满一档时游戏只加部分经验，等级停在 3/6/9/12/15 之前。这里模拟
    0 → 1（期望 3）：第一次读到 0 判 UPGRADE，升级后连读 ocr_retry 次都只有 1。
    """
    job = TaskJob(slot=4, weights={"暴击率": 1}, stage_require={"four": {0: 0, 3: 999}})
    runner = Runner(Preset(task_chain=(job,)), hotkeys=False)
    image = blank_image()
    monkeypatch.setattr(runner, "_grab", lambda **kw: (image, "upgrade", "test", 1))
    calls = {"n": 0}

    def parse_upgrade(img):
        calls["n"] += 1
        return sample_disc(0 if calls["n"] == 1 else 1)

    monkeypatch.setattr(ocr_module, "parse_disc_upgrade_page", parse_upgrade)
    monkeypatch.setattr(runner, "_upgrade_once", lambda hwnd, slot=0: True)
    esc = Mock()
    monkeypatch.setattr(runner, "_esc_back_to_grid", esc)

    with pytest.raises(RunnerError, match="升级素材不足"):
        runner._upgrade_loop(1, job, 0, "R1C1", runner._sub_preset(job), None, {})

    assert calls["n"] == runner.preset.ocr_retry + 1
    # 素材不足要留下失败记录，且报的就是素材不足（不许被收尾时的回退失败盖掉）
    assert any("升级素材不足" in str(c) for c in runner.trace.fail.call_args_list)
    # 收尾先把游戏退回仓库，别把玩家卡在强化弹层里（正常业务场景的干净收尾）
    esc.assert_called_once_with(1)


def test_upgrade_reaching_next_stage_keeps_going(offline_runner, monkeypatch):
    """0 → 3 按期望到位：不误判素材不足，照常交给 decide。"""
    job = TaskJob(slot=4, weights={"暴击率": 1}, stage_require={"four": {0: 0, 3: 999}})
    runner = Runner(Preset(task_chain=(job,)), hotkeys=False)
    image = blank_image()
    monkeypatch.setattr(runner, "_grab", lambda **kw: (image, "upgrade", "test", 1))
    # 0 级只有 1 次强化次数说不通来源，0 → 3 必须带一次强化次数（+3 即一次）
    initial = sample_disc(0)
    upgraded = Disc(3, (Substat("暴击率", 2),) + initial.substats[1:])
    calls = {"n": 0}

    def parse_upgrade(img):
        calls["n"] += 1
        return initial if calls["n"] == 1 else upgraded

    monkeypatch.setattr(ocr_module, "parse_disc_upgrade_page", parse_upgrade)
    monkeypatch.setattr(runner, "_upgrade_once", lambda hwnd, slot=0: True)
    discard = Mock()
    monkeypatch.setattr(runner, "_discard_from_upgrade", discard)

    runner._upgrade_loop(1, job, 0, "R1C1", runner._sub_preset(job), None, {})

    assert calls["n"] == 2  # 升级前后各读一次，没有多余重读
    discard.assert_called_once()


def test_upgrade_shortfall_stops_later_jobs(offline_runner, monkeypatch):
    """素材不足要停整条任务链，不是只跳过这一个盘。"""
    jobs = (
        generated_job(slot=4, weights={"暴击率": 1}, stage_require={"four": {0: 0, 3: 999}}),
        generated_job(slot=5, weights={"暴击率": 1}),
    )
    runner = Runner(Preset(task_chain=jobs, max_discs=2), hotkeys=False)
    monkeypatch.setattr(
        runner, "_scan_cells", lambda s, **kw: ([make_cell(0, 0, 0), make_cell(1, 0, 1)], [], {})
    )
    monkeypatch.setattr(runner, "_advance", lambda hwnd, state, cells, *, rows: False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc(0))
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    calls = {"n": 0}

    def parse_upgrade(img):
        calls["n"] += 1
        return sample_disc(0 if calls["n"] == 1 else 1)

    monkeypatch.setattr(ocr_module, "parse_disc_upgrade_page", parse_upgrade)
    monkeypatch.setattr(runner, "_upgrade_once", lambda hwnd, slot=0: True)

    runner.run()

    assert runner.outcome == "failed"
    assert "升级素材不足" in runner.end_reason
    assert runner.processed_disc_count == 1  # 第一个盘就炸，后一个任务没轮到


def test_material_slot_entry_clicks_slot_not_stage_add(offline_runner, monkeypatch):
    """测试入口 --material-slot 3：升级改点第 3 个材料槽槽心，不再读「阶段添加」。"""
    runner = Runner(Preset(), hotkeys=False)
    image = blank_image()
    monkeypatch.setattr(runner, "_grab", lambda **kw: (image, "upgrade", "test", 1))
    confirm = ocr_module.OcrLine("升级", 1, 1460, 840, 1560, 880)

    def read_rect(img, area):
        if area == runner.UPGRADE_CONFIRM_AREA:
            return [confirm]
        raise AssertionError(f"测试入口不该读这个区域：{area}")

    monkeypatch.setattr(ocr_module, "read_rect", read_rect)

    assert runner._upgrade_once(99, 3) is True
    assert offline_runner["click_client"].call_args_list == [call(99, 1405, 677), call(99, 1510, 860)]


def test_material_slot_scales_with_window_size(offline_runner, monkeypatch):
    """材料槽坐标按实际客户区缩放，不是写死 1920×1080。"""
    runner = Runner(Preset(), hotkeys=False)
    image = QImage(1600, 900, QImage.Format.Format_RGB32)
    image.fill(0)
    monkeypatch.setattr(runner, "_grab", lambda **kw: (image, "upgrade", "test", 1))
    confirm = ocr_module.OcrLine("升级", 1, 1460, 840, 1560, 880)
    monkeypatch.setattr(
        ocr_module, "read_rect", lambda img, area: [confirm] if area == runner.UPGRADE_CONFIRM_AREA else []
    )

    assert runner._upgrade_once(99, 2) is True
    assert offline_runner["click_client"].call_args_list[0] == call(99, 1069, 564)


# ----------------------------------------------- 单屏整屏可见快速路径（改造方案 §4、§5）
#
# 验收（方案 §10）：数量与格位一致时 1/9/10/27/34/35 个盘进单屏；恰好 36 个靠总数
# 判定；37+ 不进单屏；单屏内不置顶、不探底、不滚轮；强化后原地重扫、无仓库页签点击。


def _empty_slots_beyond(count: int) -> set:
    """4×9 视口里，首 count 格之外全部格位（供单屏判定当「已确认空占位」）。"""
    order = [(row, col) for row in range(VIEWPORT_ROWS) for col in range(GRID_COLS)]
    return set(order[count:])


def _stub_single_screen_scan(runner, cells, count, *, unclear=None):
    """把扫描结果与原始可观察量（总数 / 空占位 / 拿不准）一起交给 Runner。"""

    def scan(state, **kwargs):
        runner._last_count = count
        runner._last_empties = _empty_slots_beyond(count) - set((unclear or {}).keys())
        runner._last_unclear = dict(unclear or {})
        return list(cells), [], {}

    return scan


def _single_screen_cells(count: int, *, levels=None):
    """从首行首列起连续铺 count 个实体盘，可指定各格等级。"""
    levels = levels or {}
    return [make_cell(0, index, 0, level=levels.get(index, 0)) for index in range(count)]


def test_prepare_navigation_uses_single_screen_without_top_reset(offline_runner, monkeypatch):
    """5 个盘整屏可见：不置顶、不探底，返回单屏准备并复用判定那一帧的扫描。"""
    runner = Runner(generated_preset(accepted=None), hotkeys=False)
    runner._profile = fake_profile()
    cells = _single_screen_cells(5)
    monkeypatch.setattr(Runner, "_read_label_lines", lambda self, image: fake_label_lines(5))
    monkeypatch.setattr(runner, "_scan_cells", _stub_single_screen_scan(runner, cells, 5))
    monkeypatch.setattr(runner_module, "is_page", lambda image, page: True)

    prep = runner._prepare_navigation(1, _state())

    assert prep is not None and prep.mode == VIEWPORT_SINGLE_SCREEN
    assert prep.total == 5
    assert prep.scan[0] == cells
    offline_runner["reset_to_top"].assert_not_called()


def test_prepare_navigation_long_list_does_not_reset_top(offline_runner, monkeypatch):
    """长列表（97 个）：不置顶——关掉筛选面板时游戏已回到顶部，切页纯属多余。"""
    runner = Runner(generated_preset(accepted=None), hotkeys=False)
    runner._profile = fake_profile()
    state = _state()

    prep = runner._prepare_navigation(1, state)

    assert prep is not None and prep.mode == VIEWPORT_SCROLLING
    assert state.viewport_mode == VIEWPORT_SCROLLING
    offline_runner["reset_to_top"].assert_not_called()


@pytest.mark.parametrize("count", [37, 300])
def test_confirm_single_screen_rejects_over_capacity(offline_runner, monkeypatch, count):
    runner = Runner(generated_preset(accepted=None), hotkeys=False)
    runner._profile = fake_profile()
    lines = fake_label_lines(count)

    prep, note = runner._confirm_single_screen(_state(), image=blank_image(), lines=lines)

    assert prep is None and "36" in note


def test_confirm_single_screen_not_used_for_start_index(offline_runner, monkeypatch):
    """测试跳号入口要把视口摆到列表深处，与「整屏可见」互斥，直接走长列表。"""
    runner = Runner(generated_preset(accepted=None), hotkeys=False, start_index=100)
    runner._profile = fake_profile()

    prep, note = runner._confirm_single_screen(
        _state(), image=blank_image(), lines=fake_label_lines(5)
    )

    assert prep is None and "跳号" in note


def test_single_screen_job_runs_without_top_reset_or_scroll(offline_runner, monkeypatch):
    """端到端：单屏任务处理完整屏结果，全程不置顶、不滚轮、不探底。"""
    runner = Runner(Preset(max_discs=10, accepted=()), hotkeys=False)
    cells = _single_screen_cells(5)
    monkeypatch.setattr(
        runner,
        "_prepare_navigation",
        lambda hwnd, state: runner_module.NavigationPrep(
            profile=fake_profile(), mode=VIEWPORT_SINGLE_SCREEN, total=5, scan=(cells, [], {})
        ),
    )
    scroll = Mock(
        side_effect=lambda hwnd, rows, profile, **kw: ScrollSend(
            rows, rows, rows, profile.event_interval_ms
        )
    )
    monkeypatch.setattr(runner_module, "scroll_rows", scroll)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())

    runner.run()

    assert runner.outcome == "completed"
    assert runner.processed_disc_count == 5
    offline_runner["reset_to_top"].assert_not_called()
    scroll.assert_not_called()
    offline_runner["press_key"].assert_not_called()


# ----------------------------------------------- 强化后原地复核（改造方案 §5.2、§6）


def _single_screen_state(cells_count: int, *, active_index: int) -> TraversalState:
    state = _state()
    state.restore_request_limit_rows = max_restore_rows(20)
    state.mark_viewport_mode(VIEWPORT_SINGLE_SCREEN, total=cells_count)
    state.confirm_viewport(0, 0.0, row_pitch_px=175.0, at_top=True, at_bottom=True)
    state.begin_upgrade(active_index)
    return state


def test_restore_single_screen_rechecks_in_place(offline_runner, monkeypatch):
    """单屏强化后原地重读：只允许刚强化那件等级上升，无其他变化即通过并继续。

    全程不切页（无 reset_to_top）、不滚轮；复核扫描结果缓存给主循环，不重复计数。
    """
    runner = Runner(Preset(accepted=None), hotkeys=False)
    runner._profile = fake_profile()
    runner._row_pitch = 175.0
    runner._origin_center_y = 267.0
    state = _single_screen_state(5, active_index=2)
    before = _single_screen_cells(5)
    after = _single_screen_cells(5, levels={2: 15})  # 刚强化的第 3 格升到 15
    monkeypatch.setattr(runner, "_grab_label_frame", lambda hwnd: (blank_image(), fake_label_lines(5)))
    monkeypatch.setattr(runner_module, "is_page", lambda image, page: True)
    monkeypatch.setattr(runner, "_scan_cells", _stub_single_screen_scan(runner, after, 5))

    runner._restore_position(1, state, before)

    assert state.resume_point is None
    assert state.viewport_mode == VIEWPORT_SINGLE_SCREEN
    assert runner._restored_scan[0] == after
    offline_runner["reset_to_top"].assert_not_called()
    assert any(
        c.args[1] == "single_screen_recheck" for c in runner.trace.ok.call_args_list
    )


def test_restore_single_screen_stops_chain_on_member_change(offline_runner, monkeypatch):
    """总数不变但别的格位也变了 = 列表成员/顺序变化：旧关联失效，停止任务链。

    切页置顶不能恢复物品原来的身份，所以不许回退到置顶兜底。
    """
    runner = Runner(Preset(accepted=None), hotkeys=False)
    runner._profile = fake_profile()
    runner._row_pitch = 175.0
    runner._origin_center_y = 267.0
    state = _single_screen_state(5, active_index=2)
    before = _single_screen_cells(5)
    after = _single_screen_cells(5, levels={2: 15, 4: 6})  # 第 5 格也变了
    monkeypatch.setattr(runner, "_grab_label_frame", lambda hwnd: (blank_image(), fake_label_lines(5)))
    monkeypatch.setattr(runner_module, "is_page", lambda image, page: True)
    monkeypatch.setattr(runner, "_scan_cells", _stub_single_screen_scan(runner, after, 5))

    with pytest.raises(RunnerError, match="成员变化"):
        runner._restore_position(1, state, before)

    offline_runner["reset_to_top"].assert_not_called()


def test_restore_single_screen_falls_back_on_viewport_anomaly(offline_runner, monkeypatch):
    """布局/相位读不出 = 视口异常：放弃快速路径，回退到既有置顶定位恢复。"""
    from zzz_ddtool.navigation import NavigationError

    runner = Runner(Preset(accepted=None), hotkeys=False)
    runner._profile = fake_profile()
    runner._row_pitch = 175.0
    runner._origin_center_y = 267.0
    state = _single_screen_state(5, active_index=2)
    monkeypatch.setattr(runner, "_grab_label_frame", lambda hwnd: (blank_image(), fake_label_lines(5)))
    monkeypatch.setattr(runner_module, "is_page", lambda image, page: True)
    # 原地复核每次测布局都失败，回退路径第一次就成功。
    monkeypatch.setattr(
        runner_module,
        "measure_layout",
        Mock(side_effect=[NavigationError("no labels")] * 3 + [(267.0, 175.0)] * 4),
    )
    monkeypatch.setattr(runner, "_scan_cells", lambda state, **kw: ([make_cell(0, 0, 0)], [], {}))

    runner._restore_position(1, state, _single_screen_cells(5))

    # 回退到置顶恢复：切页一次，成功恢复后释放恢复点。
    offline_runner["reset_to_top"].assert_called_once_with(1)
    assert state.resume_point is None
    assert any(c.args[1] == "restore_fallback" for c in runner.trace.warn.call_args_list)
