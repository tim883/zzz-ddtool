"""监听器标定入口的输入门禁，原生输入和截图全部替身。"""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtGui import QImage

from zzz_ddtool.control import checkpoint_input, controlled_inputs
from zzz_ddtool.hotkeys import RunnerStopped

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("picker_scroll_probe_test", ROOT / "scripts/picker_scroll_probe.py")
probe_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe_module)


@pytest.mark.parametrize("argv", [["pick", "静听嘉音"], ["reset"], ["confirm"], ["phase1"],
                                  ["scroll", "0"], ["scroll", "-13"], ["scroll", "-3", "2"],
                                  ["scroll", "-3", "1", "extra"], ["esc", "extra"]])
def test_illegal_command_rejected_before_controller_or_window(monkeypatch, argv):
    init = Mock(side_effect=AssertionError("禁止初始化"))
    monkeypatch.setattr(probe_module, "HotkeyController", init)
    with pytest.raises(ValueError):
        probe_module.main(argv)
    init.assert_not_called()


@pytest.fixture
def setup(monkeypatch, tmp_path):
    p = probe_module
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    monkeypatch.setattr(p, "find_game_hwnd", lambda: 10)
    monkeypatch.setattr(p, "window_pid", lambda hwnd: 20)
    monkeypatch.setattr(p, "grab_game_image", lambda hwnd: (image, "test", hwnd))
    monkeypatch.setattr(p.gf, "_window_dpi", lambda hwnd: 96)
    monkeypatch.setattr(p.gf, "window_pid", lambda hwnd: 20)
    monkeypatch.setattr(p.gf, "_grab", lambda: (image, 10))
    monkeypatch.setattr(p.gf, "_picker_stage_matches", lambda image, layout: True)
    monkeypatch.setattr(p.wininput, "input_blocked_reason", lambda hwnd: None)
    monkeypatch.setattr(p.time, "sleep", lambda _: None)
    controller = Mock(checkpoint=Mock(return_value=0))
    probe = p.Probe(controller, tmp_path)
    sent = []
    def wheel(*args, **kwargs):
        checkpoint_input()
        checkpoint_input()
        sent.append(kwargs["delta"])
    monkeypatch.setattr(p.wininput, "wheel_client", wheel)
    return p, probe, image, sent


def test_one_scroll_preserves_two_native_boundaries(setup):
    p, probe, _, sent = setup
    with controlled_inputs(lambda: 0):
        probe.scroll(-6)
    assert sent == [-6]


@pytest.mark.parametrize("boundary", [1, 2])
def test_page_change_at_entry_or_focus_refuses_scroll(setup, monkeypatch, boundary):
    p, probe, _, sent = setup
    calls = [0]
    def checkpoint():
        calls[0] += 1
        if calls[0] == boundary:
            monkeypatch.setattr(p.gf, "_picker_stage_matches", lambda image, layout: False)
        return 2 if calls[0] >= boundary else 0
    with controlled_inputs(checkpoint), pytest.raises(p.gf.FilterClickError):
        probe.scroll(-6)
    assert sent == []


def test_stop_prevents_scroll(setup):
    _, probe, _, sent = setup
    with controlled_inputs(Mock(side_effect=RunnerStopped("stop"))), pytest.raises(RunnerStopped):
        probe.scroll(-6)
    assert sent == []


@pytest.mark.parametrize("field", ["pid", "dpi", "size", "hwnd"])
def test_environment_mismatch_refuses_input(setup, monkeypatch, field):
    p, probe, image, sent = setup
    if field == "pid":
        monkeypatch.setattr(p, "window_pid", lambda hwnd: 21)
    elif field == "dpi":
        monkeypatch.setattr(p.gf, "_window_dpi", lambda hwnd: 120)
    elif field == "size":
        monkeypatch.setattr(p, "grab_game_image", lambda hwnd: (QImage(800, 600, image.format()), "test", hwnd))
    else:
        monkeypatch.setattr(p, "grab_game_image", lambda hwnd: (image, "test", 11))
    with pytest.raises(RuntimeError):
        probe.scroll(-6)
    assert sent == []


def test_top_already_confirmed_does_not_scroll(setup, monkeypatch):
    p, probe, image, sent = setup
    monkeypatch.setattr(probe, "save", lambda tag: image)
    monkeypatch.setattr(probe, "top_state", lambda image: True)
    probe.execute("top", None)
    assert sent == []


def test_top_is_bounded_when_geometry_cannot_confirm(setup, monkeypatch):
    p, probe, image, sent = setup
    monkeypatch.setattr(probe, "save", lambda tag: image)
    monkeypatch.setattr(probe, "top_state", lambda image: False)
    with controlled_inputs(lambda: 0), pytest.raises(RuntimeError):
        probe.execute("top", None)
    assert sent == [3] * 8


def test_unknown_top_state_stops_without_scrolling(setup, monkeypatch):
    p, probe, image, sent = setup
    monkeypatch.setattr(probe, "save", lambda tag: image)
    monkeypatch.setattr(probe, "top_state", lambda image: None)
    with pytest.raises(RuntimeError):
        probe.execute("top", None)
    assert sent == []


@pytest.mark.parametrize("path,visible,expected", [
    ("手册/参考图/驱动盘仓库/列表与侧栏.png", False, True),
    ("手册/参考图/驱动盘筛选/套装弹窗-确认后.png", True, True),
    ("手册/参考图/驱动盘筛选/套装弹窗-顶部.png", False, False),
    ("手册/参考图/驱动盘筛选/套装弹窗-顶部.png", True, False),
])
def test_warehouse_entry_uses_positive_page_evidence_not_popup_band(setup, path, visible, expected):
    _, probe, _, _ = setup
    image = QImage(str(ROOT / path))
    assert not image.isNull()
    assert probe.panel_state(image, visible) is expected


def test_aligned_middle_row_cannot_be_mistaken_for_top(setup, monkeypatch):
    p, probe, image, _ = setup
    monkeypatch.setattr(p.gf, "_picker_at_top", lambda image, layout: True)
    middle = [{"text": name} for name in ("法厄同之歌", "如影相随", "折枝剑歌", "静听嘉音")]
    assert probe.named_top_state(image, middle) is False
    assert probe.named_top_state(image, [{"text": name} for name in p.TOP_NAMES]) is True
    assert probe.named_top_state(image, [{"text": None}] * 4) is None
