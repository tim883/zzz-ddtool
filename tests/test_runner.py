"""执行器回归：全局格位遍历、扫描跳过、进度提交、白名单与失败传播。

替身网格模拟 4 行视口、可配置总行数的仓库；真实 OCR/输入/窗口接口全部替换，
不发送任何真实游戏输入。
"""

from dataclasses import replace
from unittest.mock import Mock, call

import pytest
from PySide6.QtGui import QImage

import zzz_ddtool.ocr as ocr_module
import zzz_ddtool.runner as runner_module
import zzz_ddtool.screen as screen_module
from zzz_ddtool.chain import TaskJob
from zzz_ddtool.config import Preset
from zzz_ddtool.decide import decide
from zzz_ddtool.models import Action, Disc, Substat
from zzz_ddtool.navigation import Environment, NavProfile, ScrollSend
from zzz_ddtool.runner import Runner, RunnerError
from zzz_ddtool.traversal import CellStatus, GridCell, max_restore_rows

VIEWPORT_ROWS = 4
GRID_COLS = 9


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

    def __init__(self, total_rows=6, filtered_rows=()):
        self.total_rows = total_rows
        self.filtered = set(filtered_rows)
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
            for col in range(GRID_COLS):
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
    monkeypatch.setattr(runner_module, "measure_layout", lambda img: (267.0, 175.0))
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
    monkeypatch.setattr(Runner, "_scan_cells", lambda self, state: default_cells)
    monkeypatch.setattr(Runner, "_grab", lambda *a, **kw: (image, "unknown", "test", 1))
    monkeypatch.setattr(Runner, "_advance", lambda self, hwnd, state, cells, *, rows: True)

    inputs = {
        name: Mock()
        for name in (
            "goto_warehouse",
            "apply_game_filter",
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

    def scan(state):
        runner._selected_cell = grid.selected_cell()
        return grid.scan()

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
    monkeypatch.setattr(runner, "_scan_cells", lambda s: ([make_cell(10, 0, 0)], [], {}))
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
    offline_runner["reset_to_top"].assert_called_once()
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
    monkeypatch.setattr(runner, "_scan_cells", lambda s: ([make_cell(0, 0, 0)], [], {}))
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
    monkeypatch.setattr(runner, "_scan_cells", lambda s: next(results))
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
    monkeypatch.setattr(runner, "_scan_cells", lambda s: ([], ["标签无法映射到行列"], {}))
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
        TaskJob(slot=4, weights={"暴击率": 1}, accepted=None),
        TaskJob(slot=5, weights={"暴击率": 1}, accepted=None),
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
    offline_runner["goto_warehouse"].assert_called_once()


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

    def upgrade_loop(hwnd, job, global_index, cell_tag, preset, accepted_ids, up_counts):
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
    runner = Runner(Preset(max_discs=1, accepted=None), hotkeys=False)
    monkeypatch.setattr(
        runner, "_scan_cells", lambda s: ([make_cell(0, 0, 0), make_cell(1, 0, 1)], [], {})
    )
    monkeypatch.setattr(runner, "_advance", lambda hwnd, state, cells, *, rows: False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    monkeypatch.setattr(runner, "_upgrade_loop", Mock())
    runner.run()
    assert runner.outcome == "completed"
    assert runner.processed_disc_count == 1
    # 仅任务开始时标定前后各置顶一次，恢复不再置顶。
    assert offline_runner["reset_to_top"].call_count == 2


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
    monkeypatch.setattr(runner, "_scan_cells", lambda state: ([make_cell(0, 0, 0), make_cell(1, 0, 1)], [], {}))
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


@pytest.mark.parametrize("accepted, expected", [(None, Action.UPGRADE), ((), Action.MARK_DISCARD)])
def test_run_preserves_empty_whitelist(offline_runner, monkeypatch, accepted, expected):
    job = TaskJob(slot=4, weights={"暴击率": 1.0}, accepted=accepted)
    runner = Runner(Preset(task_chain=(job,), max_discs=1), hotkeys=False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    actions = []
    monkeypatch.setattr(runner, "_act", lambda hwnd, action, disc: actions.append(action))

    def upgrade_loop(hwnd, job, global_index, cell_tag, preset, accepted_ids, up_counts):
        actions.append(decide(sample_disc(), preset, accepted_ids=accepted_ids).action)

    monkeypatch.setattr(runner, "_upgrade_loop", upgrade_loop)
    runner.run()
    assert actions == [expected]


@pytest.mark.parametrize("page", ["detail", "upgrade"])
def test_uncertain_origin_stops_entire_chain(offline_runner, monkeypatch, page):
    jobs = (TaskJob(slot=4, weights={"暴击率": 1.0}), TaskJob(slot=5, weights={"暴击率": 1.0}))
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
    job = TaskJob(slot=4, weights={"暴击率": 1}, stage_require={"four": {0: 0, 3: 999}})
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
    job = TaskJob(
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
    job = TaskJob(slot=4, weights={"暴击率": 1}, stage_require={"four": {0: 0}})
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
    jobs = (TaskJob(slot=4, weights={"暴击率": 1}), TaskJob(slot=5, weights={"暴击率": 1}))
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
        TaskJob(slot=4, weights={"暴击率": 1}, stage_require={"four": {3: 999}}),
        TaskJob(slot=5, weights={"暴击率": 1}),
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
    runner = Runner(Preset(), hotkeys=False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: False)
    runner.run()
    assert runner.outcome == "failed"
    assert "进入强化页失败" in runner.end_reason
