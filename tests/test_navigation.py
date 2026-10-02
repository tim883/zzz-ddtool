"""导航环境身份、滚动标定与按行移动的离线回归（不发任何真实输入）。"""

import json
from pathlib import Path

import pytest
from PySide6.QtGui import QImage

import zzz_ddtool.navigation as nav_module
import zzz_ddtool.ocr as ocr_module
from zzz_ddtool.navigation import (
    Environment,
    NavProfile,
    NavigationError,
    anchor_screen_rows,
    load_profile,
    measure_layout,
    save_profile,
    scroll_rows,
)


FIXTURES = Path(__file__).parent / "fixtures"
BUILD = "1.2.3.4|123456|1700000000000000000"
EXE = r"d:\game\zzz\zenlesszonezero.exe"


def _env(**overrides) -> Environment:
    base = dict(
        machine_id="a" * 64,
        client_width=1920,
        client_height=1080,
        window_dpi=96,
        game_exe_path=EXE,
        game_build=BUILD,
    )
    base.update(overrides)
    return Environment(**base)


def _profile(**overrides) -> NavProfile:
    base = dict(
        machine_id="a" * 64,
        client_width=1920,
        client_height=1080,
        window_dpi=96,
        game_exe_path=EXE,
        game_build=BUILD,
        row_pitch_px=174.83,
        origin_center_y=267.5,
        events_per_row=1,
        event_interval_ms=100,
        settle_wait_ms=430,
    )
    base.update(overrides)
    return NavProfile(**base)


def _labels(rows: int, cols: int, *, top0: int = 347, pitch: int = 175):
    lines = []
    for r in range(rows):
        for c in range(cols):
            top = top0 + r * pitch
            lines.append(
                ocr_module.OcrLine("等级15", 0.99, 146 + c * 135, top, 186 + c * 135, top + 14)
            )
    return lines


# ------------------------------------------------------------------ 布局测量


def test_measure_layout_reads_origin_and_pitch(monkeypatch):
    monkeypatch.setattr(ocr_module, "read_rect", lambda *a, **k: _labels(4, 9))
    origin, pitch = measure_layout(QImage(1920, 1080, QImage.Format.Format_RGB32))
    assert origin == pytest.approx(267.0, abs=0.6)
    assert pitch == pytest.approx(175.0, abs=0.6)


def test_measure_layout_uses_reference_for_single_row(monkeypatch):
    monkeypatch.setattr(ocr_module, "read_rect", lambda *a, **k: _labels(1, 9))
    origin, pitch = measure_layout(QImage(1920, 1080, QImage.Format.Format_RGB32))
    assert origin == pytest.approx(267.0, abs=0.6)
    assert pitch == pytest.approx(175.0, abs=0.01)


def test_measure_layout_scales_with_client_size(monkeypatch):
    # 1280×720：标签中心 2/3 缩放，行距与标签偏移同步缩放
    lines = _labels(4, 9, top0=231, pitch=117)
    monkeypatch.setattr(ocr_module, "read_rect", lambda *a, **k: lines)
    origin, pitch = measure_layout(QImage(1280, 720, QImage.Format.Format_RGB32))
    assert origin == pytest.approx(238.0 - 58, abs=1.0)
    assert pitch == pytest.approx(117.0, abs=1.0)


def test_measure_layout_rejects_image_without_labels(monkeypatch):
    monkeypatch.setattr(ocr_module, "read_rect", lambda *a, **k: [])
    with pytest.raises(NavigationError):
        measure_layout(QImage(1920, 1080, QImage.Format.Format_RGB32))


# ------------------------------------------------------------------ 锚点读取


def test_anchor_screen_rows_finds_selected_cell():
    image = QImage(str(FIXTURES / "grid-before-autoscroll.png"))
    assert anchor_screen_rows(image, col0=0, origin_center_y=267.5, row_pitch_px=174.83) == [0]
    assert anchor_screen_rows(image, col0=1, origin_center_y=267.5, row_pitch_px=174.83) == []


def test_anchor_screen_rows_reads_scrolled_viewport():
    image = QImage(str(FIXTURES / "grid-after-autoscroll.png"))
    assert anchor_screen_rows(image, col0=0, origin_center_y=267.5, row_pitch_px=174.83) == [2]


def test_anchor_screen_rows_honours_phase():
    image = QImage(str(FIXTURES / "grid-before-autoscroll.png"))
    rows = anchor_screen_rows(
        image, col0=0, origin_center_y=267.5, row_pitch_px=174.83, phase_px=87.0
    )
    assert rows == []


def test_anchor_click_point_uses_layout_pitch():
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    assert nav_module._anchor_click_x(image, 4) == 706
    assert nav_module._anchor_click_y(267.5, 174.83) == 617


# ------------------------------------------------------------------ 协议缓存


def test_environment_persistable_requires_full_identity():
    assert _env().persistable
    assert not _env(window_dpi=None).persistable
    assert not _env(machine_id=None).persistable
    assert not _env(game_build=None).persistable
    assert not _env(game_exe_path=None).persistable


def test_profile_matches_only_identical_environment():
    env = _env()
    assert _profile().matches(env)
    assert not _profile().matches(_env(client_width=1280, client_height=720))
    assert not _profile().matches(_env(window_dpi=120))
    assert not _profile().matches(_env(game_build="9.9.9.9|1|2"))
    assert not _profile().matches(_env(game_exe_path="c:\\other\\zzz.exe"))
    assert not _profile().matches(_env(machine_id="b" * 64))


def test_profile_matches_rejects_version_and_illegal_values():
    env = _env()
    assert not _profile(schema_version=2).matches(env)
    assert not _profile(calibration_algorithm_version=99).matches(env)
    assert not _profile(events_per_row=0).matches(env)
    assert not _profile(row_pitch_px=0.0).matches(env)
    assert not _profile(event_interval_ms=0).matches(env)
    assert not _profile(settle_wait_ms=-1).matches(env)


def _use_temp_profile(tmp_path, monkeypatch) -> Path:
    target = tmp_path / "user" / "navigation-profile.json"
    monkeypatch.setattr(nav_module, "profile_path", lambda: target)
    return target


def test_profile_roundtrip(tmp_path, monkeypatch):
    _use_temp_profile(tmp_path, monkeypatch)
    env = _env()
    assert load_profile(env) is None
    profile = _profile()
    save_profile(profile)
    assert load_profile(env) == profile


def test_profile_ignored_when_environment_changed(tmp_path, monkeypatch):
    _use_temp_profile(tmp_path, monkeypatch)
    save_profile(_profile())
    assert load_profile(_env(client_height=720, client_width=1280)) is None
    assert load_profile(_env(window_dpi=120)) is None
    assert load_profile(_env(game_build="9.9.9.9|1|2")) is None
    assert load_profile(_env(machine_id="c" * 64)) is None


def test_profile_ignored_when_corrupt_or_incomplete(tmp_path, monkeypatch):
    target = _use_temp_profile(tmp_path, monkeypatch)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ not json", encoding="utf-8")
    assert load_profile(_env()) is None
    target.write_text(json.dumps({"machine_id": "a" * 64}), encoding="utf-8")
    assert load_profile(_env()) is None
    target.write_text(json.dumps([]), encoding="utf-8")
    assert load_profile(_env()) is None


def test_profile_ignored_when_values_illegal(tmp_path, monkeypatch):
    _use_temp_profile(tmp_path, monkeypatch)
    save_profile(_profile(events_per_row=0))
    assert load_profile(_env()) is None
    save_profile(_profile(row_pitch_px=-1.0))
    assert load_profile(_env()) is None


# ------------------------------------------------------------------ 按行发送


@pytest.fixture
def wheel_spy(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(nav_module, "_place_cursor", lambda hwnd: calls.append(("focus", hwnd)))
    monkeypatch.setattr(
        "zzz_ddtool.wininput.send_wheel_event", lambda units: calls.append(("wheel", units))
    )
    return calls


def test_scroll_rows_sends_one_event_per_row(wheel_spy):
    result = scroll_rows(7, 2, _profile(events_per_row=1, event_interval_ms=1))
    assert (result.requested_rows, result.total_events, result.sent_events) == (2, 2, 2)
    assert result.complete
    assert wheel_spy == [("focus", 7), ("wheel", -1), ("wheel", -1)]


def test_scroll_rows_multiplies_by_events_per_row(wheel_spy):
    result = scroll_rows(7, 2, _profile(events_per_row=3, event_interval_ms=1))
    assert result.total_events == 6
    assert [c for c in wheel_spy if c[0] == "wheel"] == [("wheel", -1)] * 6


def test_scroll_rows_upward_sends_positive_units(wheel_spy):
    scroll_rows(7, -2, _profile(events_per_row=1, event_interval_ms=1))
    assert [c for c in wheel_spy if c[0] == "wheel"] == [("wheel", 1), ("wheel", 1)]


def test_scroll_rows_calls_before_event_for_each_event(wheel_spy):
    seen: list[int] = []
    scroll_rows(7, 2, _profile(events_per_row=1, event_interval_ms=1), before_event=seen.append)
    assert seen == [0, 1]


def test_scroll_rows_rejects_zero_rows(wheel_spy):
    with pytest.raises(NavigationError):
        scroll_rows(7, 0, _profile())


def test_scroll_rows_rejects_illegal_protocol(wheel_spy):
    with pytest.raises(NavigationError):
        scroll_rows(7, 1, _profile(events_per_row=0))
    assert wheel_spy == []
