"""执行器回归：全局格位遍历、扫描跳过、进度提交、白名单与失败传播。

替身网格模拟 4 行视口、可配置总行数的仓库；真实 OCR/输入/窗口接口全部替换，
不发送任何真实游戏输入。
"""

from unittest.mock import Mock, call

import pytest
from PySide6.QtGui import QImage

import zzz_ddtool.ocr as ocr_module
import zzz_ddtool.runner as runner_module
from zzz_ddtool.chain import TaskJob
from zzz_ddtool.config import Preset
from zzz_ddtool.decide import decide
from zzz_ddtool.models import Action, Disc, Substat
from zzz_ddtool.navigation import Environment, NavProfile
from zzz_ddtool.runner import Runner, RunnerError
from zzz_ddtool.traversal import GridCell

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
    monkeypatch.setattr(runner_module, "reset_to_top", Mock())
    monkeypatch.setattr(runner_module, "measure_layout", lambda img: (267.0, 175.0))
    monkeypatch.setattr(runner_module, "probe_environment", lambda hwnd: fake_env())
    monkeypatch.setattr(runner_module, "load_profile", lambda env: fake_profile())

    # 默认两行数据：首行可点、末行被延后，模拟真实稳定行策略
    default_cells = ([make_cell(0, 0, 0), make_cell(1, 0, 1)], [], {})
    monkeypatch.setattr(Runner, "_scan_cells", lambda self, state: default_cells)
    monkeypatch.setattr(Runner, "_grab", lambda *a, **kw: (image, "unknown", "test", 1))
    monkeypatch.setattr(Runner, "_advance", lambda self, hwnd, state, cells, *, rows: True)

    inputs = {
        name: Mock()
        for name in ("goto_warehouse", "apply_game_filter", "click_client", "press_key", "press_key_once")
    }
    for name, mock in inputs.items():
        monkeypatch.setattr(runner_module, name, mock)
    monkeypatch.setattr("zzz_ddtool.overlay.overlay_hide", Mock())
    monkeypatch.setattr("zzz_ddtool.overlay.overlay_show", Mock())
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


def test_rescan_without_unique_selection_stops(offline_runner, monkeypatch):
    # 升级改变排序必须重扫；重扫读不到唯一选中框时不能猜，直接失败。
    runner = Runner(Preset(max_discs=5, accepted=None), hotkeys=False)

    def scan(state):
        runner._selected_cell = None
        return [make_cell(0, 0, 0), make_cell(1, 0, 1)], [], {}

    monkeypatch.setattr(runner, "_scan_cells", scan)
    monkeypatch.setattr(runner, "_advance", lambda hwnd, state, cells, *, rows: False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    monkeypatch.setattr(ocr_module, "parse_disc_upgrade_page", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_upgrade_loop", Mock())
    runner.run()
    assert runner.outcome == "failed"
    assert "重扫未找到唯一选中框" in runner.end_reason
    assert offline_runner["click_client"].call_count == 1


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
