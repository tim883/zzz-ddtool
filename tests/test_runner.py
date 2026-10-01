"""执行器回归：白名单必须生效，判断不确定必须停止整条任务链。"""

from unittest.mock import Mock, call
from pathlib import Path

import pytest
from PySide6.QtGui import QImage

import zzz_ddtool.ocr as ocr_module
import zzz_ddtool.runner as runner_module
from zzz_ddtool.chain import TaskJob
from zzz_ddtool.config import Preset
from zzz_ddtool.decide import decide
from zzz_ddtool.models import Action, Disc, Substat
from zzz_ddtool.runner import Runner, RunnerError
from zzz_ddtool.equipped import grid_did_scroll, grid_scroll_limits, grid_scroll_position, tile_is_selected


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("width,height", [(1920, 1080), (1280, 720)])
def test_real_grid_scroll_and_animated_selection(width, height):
    before = QImage(str(FIXTURES / "grid-before-autoscroll.png")).scaled(width, height)
    after = QImage(str(FIXTURES / "grid-after-autoscroll.png")).scaled(width, height)
    assert grid_scroll_position(before) != grid_scroll_position(after)
    assert grid_did_scroll(before, after)
    assert not grid_did_scroll(after, after)
    assert tile_is_selected(after, 166, 616)  # 初次点击时的黄绿色动画也要识别。
    assert not tile_is_selected(after, 301, 616)
    assert not tile_is_selected(before, 166, 616)
    assert grid_scroll_limits(before) == (True, False)
    assert grid_scroll_limits(after) == (False, False)


@pytest.mark.parametrize("discarded", [False, True])
def test_bottom_scroll_resumes_same_row_without_scanning_each_disc(offline_runner, monkeypatch, discarded):
    before = QImage(str(FIXTURES / "grid-before-autoscroll.png"))
    after = QImage(str(FIXTURES / "grid-after-autoscroll.png"))
    runner = Runner(Preset(max_discs=5, accepted=(), skip_discarded=False), hotkeys=False)
    first = [(3, c, 166 + c * 135, 791, 0) for c in range(9)]
    # 重扫后的选中盘会被实际筛选排除，游标仍应定位它，并接着点同批剩余列。
    refreshed = [(2, c, 166 + c * 135, 616, 0) for c in range(1, 9)]
    scans = 0

    def visible(*args, **kwargs):
        nonlocal scans
        scans += 1
        runner._scan_image = before if scans <= 3 else after
        runner._grid_signature = "before" if scans <= 3 else "after"
        runner._selected_grid_cell = (2, 0)
        return first if scans <= 3 else refreshed

    monkeypatch.setattr(runner, "_visible_cells", visible)
    monkeypatch.setattr(runner, "_grab", lambda *a, **kw: (after, "drive_disc", "test", 1))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: Disc(0, sample_disc().substats, discarded=discarded))
    action = Mock()
    monkeypatch.setattr(runner, "_act", action)
    runner.run()
    assert runner.outcome == "completed"
    assert runner.processed_disc_count == 5
    assert offline_runner["click_client"].call_args_list == [call(1, 166, 791)] + [
        call(1, 166 + c * 135, 616) for c in range(1, 5)
    ]
    assert scans == 4  # 两次到顶扫描 + 首次扫描 + 实际滚动后的唯一重扫。
    assert offline_runner["wheel_client"].call_args_list == [call(1, 640, 540, 8)]
    assert action.call_count == (0 if discarded else 5)


def test_middle_rows_reuse_scan(offline_runner, monkeypatch):
    image = QImage(str(FIXTURES / "grid-before-autoscroll.png"))
    runner = Runner(Preset(max_discs=18, accepted=()), hotkeys=False)
    cells = [(r, c, 166 + c * 135, 267 + r * 175, 0) for r in (1, 2) for c in range(9)]
    scan = Mock(return_value=cells)
    runner._scan_image = image
    monkeypatch.setattr(runner, "_visible_cells", scan)
    monkeypatch.setattr(runner, "_grab", lambda *a, **kw: (image, "drive_disc", "test", 1))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())
    runner.run()
    assert runner.outcome == "completed"
    assert runner.processed_disc_count == 18
    assert scan.call_count == 3
    assert offline_runner["click_client"].call_args_list == [call(1, cx, cy) for _, _, cx, cy, _ in cells]


def test_filtered_page_scrolls_to_later_eligible_page(offline_runner, monkeypatch):
    runner = Runner(Preset(max_discs=1, accepted=()), hotkeys=False)
    scans = 0

    def visible(*a, **kw):
        nonlocal scans
        scans += 1
        runner._grid_signature = "top" if scans <= 3 else "next"
        return [] if scans <= 3 else [(0, 0, 166, 267, 0)]

    monkeypatch.setattr(runner, "_visible_cells", visible)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())
    runner.run()
    assert runner.outcome == "completed"
    assert runner.processed_disc_count == 1
    assert offline_runner["wheel_client"].call_args_list == [call(1, 640, 540, 8), call(1, 640, 540, -1)]


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("filtered_rows", [(), (0, 1, 2), (3, 4)])
def test_stable_rows_visit_every_disc_once(offline_runner, monkeypatch, locked, filtered_rows):
    # 六排盘、四排视口。只有滚动改变视口，绝不点击中途的首/末行。
    runner = Runner(Preset(max_discs=100, accepted=()), hotkeys=False)
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    offset = 0
    selected = None
    visited = []
    scans = 0

    def visible(*a, **kw):
        nonlocal scans
        scans += 1
        runner._scan_image = image
        runner._grid_rows = (0, 1, 2, 3)
        runner._grid_at_top, runner._grid_at_bottom = offset == 0, offset == 2
        runner._grid_signature = offset
        runner._selected_grid_cell = (selected[0] - offset, selected[1]) if selected else None
        return [(r, c, 166 + c * 135, 267 + r * 175, 0) for r in range(4) for c in range(9)
                if offset + r not in filtered_rows]

    def wheel(hwnd, x, y, delta):
        nonlocal offset
        if delta < 0:
            assert delta == -1
            offset = min(2, offset + 1)

    def click(hwnd, x, y):
        nonlocal selected
        row = round((y - 267) / 175)
        assert row != 0 or offset == 0
        assert row != 3 or offset == 2
        selected = (offset + row, round((x - 166) / 135))
        visited.append(selected)

    monkeypatch.setattr(runner, "_visible_cells", visible)
    monkeypatch.setattr(runner, "_grab", lambda *a, **kw: (image, "drive_disc", "test", 1))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: Disc(0, sample_disc().substats, locked=locked))
    monkeypatch.setattr(runner, "_act", Mock())
    offline_runner["wheel_client"].side_effect = wheel
    offline_runner["click_client"].side_effect = click
    runner.run()
    assert runner.outcome == "completed"
    assert visited == [(r, c) for r in range(6) for c in range(9) if r not in filtered_rows]
    assert runner.processed_disc_count == len(visited)
    assert scans == 5  # 两次到顶 + 三批扫描，最多 54 次点击仍只有三批扫描。
    offline_runner["press_key"].assert_not_called()


def test_unknown_selection_after_scroll_stops_before_next_click(offline_runner, monkeypatch):
    before = QImage(str(FIXTURES / "grid-before-autoscroll.png"))
    after = QImage(str(FIXTURES / "grid-after-autoscroll.png"))
    runner = Runner(Preset(max_discs=2, accepted=()), hotkeys=False)
    runner._scan_image = before
    monkeypatch.setattr(runner, "_visible_cells", lambda *a, **kw: [(3, c, 166 + c * 135, 791, 0) for c in range(9)])
    monkeypatch.setattr(runner, "_grab", lambda *a, **kw: (after, "drive_disc", "test", 1))
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_act", Mock())
    runner.run()
    assert runner.outcome == "failed"
    assert "未找到唯一选中框" in runner.end_reason
    assert offline_runner["click_client"].call_count == 1


@pytest.mark.parametrize("name,selected,limits", [
    ("grid-before-autoscroll.png", (0, 0), (True, False)),
    ("grid-after-autoscroll.png", (2, 0), (False, False)),
])
def test_scan_keeps_cursor_even_if_selected_disc_is_filtered(monkeypatch, name, selected, limits):
    image = QImage(str(FIXTURES / name))
    runner = Runner(Preset(skip_equipped=False), hotkeys=False)
    labels = [ocr_module.OcrLine("等级15" if r == 0 else "等级0", 1,
                               146 + c * 135, 347 + r * 175, 186 + c * 135, 361 + r * 175)
              for r in range(4) for c in range(9)]
    monkeypatch.setattr(runner_module, "grab_game_image", lambda: (image, "test", 1))
    monkeypatch.setattr(ocr_module, "read_rect", lambda *a: labels)
    # 模拟选中盘在扫描阶段被过滤；游标识别必须在过滤前进行。
    monkeypatch.setattr("zzz_ddtool.equipped.tile_has_discard_badge", lambda *a: True)
    assert runner._visible_cells(1920, 1080, skip_tagged=True) == []
    assert runner._selected_grid_cell == selected
    assert (runner._grid_at_top, runner._grid_at_bottom) == limits
    assert runner._grid_rows == (0, 1, 2, 3)


@pytest.fixture
def offline_runner(monkeypatch):
    # 所有屏幕、输入和悬浮窗接口均替换，不操作真实游戏。
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    trace = Mock()
    monkeypatch.setattr(runner_module, "Trace", lambda **kwargs: trace)
    monkeypatch.setattr(Runner, "_set_status", Mock())
    monkeypatch.setattr(Runner, "_grab", lambda *args, **kwargs: (image, "unknown", "test", 1))
    scans = 0

    def visible_cells(*args, **kwargs):
        nonlocal scans
        scans += 1
        # 两次到顶扫描 + 一次处理扫描；即使旧代码错误重扫也能结束测试。
        return [(0, 0, 166, 267, 0)] if scans <= 3 else []

    monkeypatch.setattr(Runner, "_visible_cells", visible_cells)
    monkeypatch.setattr(runner_module.time, "sleep", lambda seconds: None)
    inputs = {
        name: Mock()
        for name in ("goto_warehouse", "apply_game_filter", "click_client", "wheel_client", "press_key", "press_key_once")
    }
    for name, mock in inputs.items():
        monkeypatch.setattr(runner_module, name, mock)
    monkeypatch.setattr("zzz_ddtool.overlay.overlay_hide", Mock())
    monkeypatch.setattr("zzz_ddtool.overlay.overlay_show", Mock())
    return inputs


def sample_disc(level=0):
    return Disc(level=level, substats=tuple(Substat(name) for name in ("暴击率", "暴击伤害", "生命值", "防御力")))


@pytest.mark.parametrize("accepted, expected", [(None, Action.UPGRADE), ((), Action.MARK_DISCARD)])
def test_run_preserves_empty_whitelist(offline_runner, monkeypatch, accepted, expected):
    job = TaskJob(slot=4, weights={"暴击率": 1.0}, accepted=accepted)
    runner = Runner(Preset(task_chain=(job,), max_discs=1), hotkeys=False)
    monkeypatch.setattr(runner, "_parse_disc", lambda image: sample_disc())
    monkeypatch.setattr(runner, "_enter_upgrade_page", lambda hwnd: True)
    actions = []
    monkeypatch.setattr(runner, "_act", lambda hwnd, action, disc: actions.append(action))

    def upgrade_loop(hwnd, job, key, cell_tag, preset, accepted_ids, up_counts):
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
    assert offline_runner["click_client"].call_args_list == [call(1, 166, 267), call(1, 1280, 860), call(1, 1510, 860)]
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
    image = QImage(str(FIXTURES / "warehouse-after-upgrade-discard.png"))
    runner = Runner(Preset(), hotkeys=False)
    monkeypatch.setattr(runner, "_grab", lambda **kw: (image, "drive_disc", "test", 1))
    runner._discard_from_upgrade(1)
    offline_runner["press_key"].assert_not_called()
    offline_runner["press_key_once"].assert_called_once_with(1, runner_module.VK_R)


@pytest.mark.parametrize("layers", [1, 2])
def test_discard_returns_only_existing_modal_layers(offline_runner, monkeypatch, layers):
    modal = QImage(str(FIXTURES / "upgrade-modal.png"))
    warehouse = QImage(str(FIXTURES / "warehouse-after-upgrade-discard.png"))
    frames = iter([(modal, "unknown", "test", 1)] * layers + [(warehouse, "drive_disc", "test", 1)])
    runner = Runner(Preset(), hotkeys=False)
    monkeypatch.setattr(runner, "_grab", lambda **kw: next(frames))
    runner._discard_from_upgrade(1)
    assert offline_runner["press_key_once"].call_args_list == [call(1, runner_module.VK_ESCAPE)] * layers + [call(1, runner_module.VK_R)]
    offline_runner["press_key"].assert_not_called()


@pytest.mark.parametrize("page", ["menu", "overworld", "storage"])
def test_return_from_wrong_page_stops_without_keypress(offline_runner, monkeypatch, page):
    image = QImage(str(FIXTURES / "menu-after-extra-escape.png"))
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
    monkeypatch.setattr(runner, "_grab", grab)
    with pytest.raises(RunnerError, match="无法确认盘面弹窗或仓库"):
        runner._discard_from_upgrade(1)
    assert grab.call_count == 3
    offline_runner["press_key_once"].assert_not_called()


@pytest.mark.parametrize("level,rolls", [(3, 2), (15, 6)])
def test_return_failure_fails_entire_chain(offline_runner, monkeypatch, level, rolls):
    jobs = (TaskJob(slot=4, weights={"暴击率": 1}, stage_require={"four": {3: 999}}), TaskJob(slot=5, weights={"暴击率": 1}))
    runner = Runner(Preset(task_chain=jobs), hotkeys=False)
    image = QImage(str(FIXTURES / "menu-after-extra-escape.png"))
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


def test_scan_on_menu_is_failure_instead_of_empty_page(monkeypatch):
    image = QImage(str(FIXTURES / "menu-after-extra-escape.png"))
    runner = Runner(Preset(), hotkeys=False)
    monkeypatch.setattr(runner_module, "grab_game_image", lambda: (image, "test", 1))
    monkeypatch.setattr(runner, "trace", Mock())
    read = Mock()
    monkeypatch.setattr(ocr_module, "read_rect", read)
    with pytest.raises(RunnerError, match="扫描前页面错误：menu"):
        runner._visible_cells(1920, 1080)
    read.assert_not_called()
