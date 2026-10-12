"""S4 测量脚本离线用例：参数/身份/报告组装、采样核心（替身 + 模拟时钟）与固定名规则。

真机采样层不在此驱动完整 Runner；`MeasureSession` 的等待点采样用脚本化替身断言。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor, QImage

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "dev-docs" / "功能改造" / "操作等待提速" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_release_offline as build  # noqa: E402
import measure_release_waits as measure  # noqa: E402
import release_wait_common as common  # noqa: E402
import verify_release_waits as gate  # noqa: E402

from zzz_ddtool.actionwait import InputRelease  # noqa: E402

CASE_IDS = [f"T{i}" for i in range(1, 14)]
_MS = 1_000_000

_MINIMAL_YAML = """
name: fixture
game_filter:
  slot: 5
  slots: [5]
accepted: []
"""


class _Clock:
    def __init__(self, start: float = 100.0) -> None:
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)


def _offline_report(tmp_path: Path) -> Path:
    suite = tmp_path / "junit.xml"
    import xml.etree.ElementTree as ET

    root = ET.Element("testsuite", name="pytest")
    for key in CASE_IDS:
        case = ET.SubElement(root, "testcase", classname="x", name=f"test_{key}")
        props = ET.SubElement(case, "properties")
        ET.SubElement(props, "property", name="pytest_nodeid", value=f"tests/t.py::test_{key}")
    ET.ElementTree(root).write(suite, encoding="utf-8", xml_declaration=True)
    cases = {key: [f"tests/t.py::test_{key}"] for key in CASE_IDS}
    cases_path = tmp_path / "用例映射.json"
    cases_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "offline.json"
    _report, code = build.build([suite], cases_path, out)
    assert code == 0
    return out


def test_measure_rejects_parameter_combinations(tmp_path):
    config = tmp_path / "preset.yaml"
    config.write_text(_MINIMAL_YAML, encoding="utf-8")
    offline = _offline_report(tmp_path)
    good = tmp_path / "验收结果" / "round-1"
    good.mkdir(parents=True)
    output = good / "calibration.json"

    assert measure.parse_args(mode="calibrate", offline_report=offline, config_path=config,
                              output=output, calibration_report=None) == []
    errors = measure.parse_args(mode="calibrate", offline_report=offline, config_path=config,
                                output=output, calibration_report=tmp_path / "c.json")
    assert any("禁止" in message for message in errors)
    errors = measure.parse_args(mode="final", offline_report=offline, config_path=config,
                                output=output, calibration_report=None)
    assert any("缺少" in message for message in errors)
    errors = measure.parse_args(mode="calibrate", offline_report=offline, config_path=config,
                                output=tmp_path / "验收结果" / "calibration.json",
                                calibration_report=None)
    assert any("固定名" in message for message in errors)
    errors = measure.parse_args(mode="calibrate", offline_report=offline, config_path=config,
                                output=tmp_path / "elsewhere" / "round" / "calibration.json",
                                calibration_report=None)
    assert any("轮次子目录" in message for message in errors)
    assert measure.main(["--mode", "calibrate"]) == 2


def test_measure_preflight_checks_offline_identity(tmp_path):
    config = tmp_path / "preset.yaml"
    config.write_text(_MINIMAL_YAML, encoding="utf-8")
    offline = _offline_report(tmp_path)
    assert measure.preflight_offline(offline, config) == []

    payload = common.read_json(offline)
    payload["passed"] = False
    common.write_json(offline, payload)
    assert any("未通过" in message for message in measure.preflight_offline(offline, config))

    payload["passed"] = True
    payload["spec_sha256"] = "0" * 64
    common.write_json(offline, payload)
    assert any("Spec" in message for message in measure.preflight_offline(offline, config))


def test_measure_preflight_calibration_requires_same_environment_and_config(tmp_path):
    config = tmp_path / "preset.yaml"
    config.write_text(_MINIMAL_YAML, encoding="utf-8")
    environment = {"machine_id": "m", "client_width": 1920, "client_height": 1080,
                   "window_dpi": 96, "game_exe_path": "c:/game.exe", "game_build": "b1"}
    calibration = tmp_path / "calibration.json"
    report = {
        "schema_version": 1, "stage": "calibration",
        "spec_sha256": common.sha256_file(common.SPEC),
        "execution_identity": common.execution_identity(),
        "environment": environment, "config_path": str(config),
        "config_sha256": common.sha256_file(config),
        "raw_jsonl_path": str(tmp_path / "m.jsonl"), "raw_jsonl_sha256": "0" * 64,
        "actions": [], "errors": [], "passed": True,
    }
    common.write_json(calibration, report)
    assert measure.preflight_calibration(calibration, config, environment) == []
    other_env = dict(environment, machine_id="other")
    assert any("环境" in message
               for message in measure.preflight_calibration(calibration, config, other_env))


def test_measure_evidence_rows_and_action_status():
    binding = {"hwnd": 1, "pid": 2, "client_width": 1920, "client_height": 1080,
               "window_dpi": 96, "pause_generation": 0}
    sample = measure.build_sample_row(
        sample_id="s1", action_id="slot", input_call_ids=[7], released_at_ns=1_000_000_000,
        binding=binding, before_frame=None, layout_id=None, return_edge=None,
        t_ready_ns=1_050_000_000, t_stable_ns=1_100_000_000, stable_ms=100,
        next_step_call_id=8, next_step_captured_at_ns=1_400_000_000, next_step_verified=True,
        valid=True, invalid_reason=None,
    )
    assert set(sample) == set(gate.SAMPLE_FIELDS)
    assert sample["input_call_id"] == 7 and sample["input_call_ids"] == [7]
    observation = measure.observation_row(
        sample_id="s1", input_call_id=7, action_id="slot", phase="after_action",
        captured_at_ns=1_050_000_000, probe_ended_at_ns=1_055_000_000, ready=True,
        state_key=["filter_panel", 5, [], [], None], binding=binding, frame=("a.png", "0" * 64),
    )
    assert set(observation) == set(gate.OBSERVATION_FIELDS)

    valid_rows = [dict(sample, sample_id=f"s{i}", stable_ms=90 + i) for i in range(10)]
    invalid_row = dict(sample, sample_id="bad", valid=False, invalid_reason="窗口内未稳定", stable_ms=None)
    rows = measure.action_rows([*valid_rows, invalid_row], ["slot", "reset"])
    by_action = {row["action_id"]: row for row in rows}
    assert by_action["slot"]["status"] == "measured"
    assert by_action["slot"]["sample_count"] == 10
    assert by_action["slot"]["max_stable_ms"] == 99
    import zzz_ddtool.actionwait as actionwait

    assert by_action["slot"]["floor_ms"] == actionwait.compute_floor_ms([99])
    assert by_action["reset"]["status"] == "not_observed"
    assert by_action["reset"]["sample_ids"] == []


def test_measure_fixed_name_copy_rules(tmp_path):
    round_dir = tmp_path / "验收结果" / "round-2"
    round_dir.mkdir(parents=True)
    source = round_dir / "calibration.json"
    source.write_text('{"a": 1}\n', encoding="utf-8")
    assert measure.copy_fixed(source, "calibrate") is True
    fixed = tmp_path / "验收结果" / "calibration.json"
    assert fixed.read_bytes() == source.read_bytes()
    assert measure.copy_fixed(tmp_path / "missing.json", "calibrate") is False


class _StubResult:
    def __init__(self, ready, state_key):
        self.ready = ready
        self._key = state_key

    def state_key(self):
        return self._key


def _session(tmp_path, monkeypatch, script):
    config = tmp_path / "preset.yaml"
    config.write_text(_MINIMAL_YAML, encoding="utf-8")
    round_dir = tmp_path / "验收结果" / "round-3"
    round_dir.mkdir(parents=True)
    clock = _Clock()
    monkeypatch.setattr(measure.time, "monotonic", clock.now)
    monkeypatch.setattr(measure.time, "sleep", clock.sleep)
    session = measure.MeasureSession(
        mode="calibrate", config_path=config, output=round_dir / "calibration.json",
        environment={"machine_id": "m", "client_width": 1920, "client_height": 1080,
                     "window_dpi": 96, "game_exe_path": "c:/game.exe", "game_build": "b1"},
        hotkeys=False, clock=clock.now,
    )
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor(3, 3, 3))

    def grab(**_kwargs):
        clock.sleep(0.001)
        return image, "unknown", "stub", 1

    session.runner = SimpleNamespace(
        _hwnd=1, _pid=2, hotkeys=None,
        _env=SimpleNamespace(client_width=1920, client_height=1080, window_dpi=96),
        _grab=grab,
    )
    index = {"n": 0}

    class _Judge:
        def __call__(self, image_arg):
            step = min(index["n"], len(script) - 1)
            index["n"] += 1
            ready, key = script[step]
            return _StubResult(ready, key)

    monkeypatch.setattr(measure.judge, "StreamJudge", lambda action_id, ctx: _Judge())
    return session, clock


def _note_input(session, call_id: int, role: str, released_ns: int) -> None:
    session._note_input({
        "t_ns": released_ns, "event": "end", "call_id": call_id, "api": "click_client",
        "role": role, "target": None, "stage_index": None, "selected": None, "hwnd": 1,
        "x": 1, "y": 2, "alt": None, "vk": None, "delta_units": None, "status": "completed",
        "error": None, "released_at_ns": released_ns,
    })


def test_measure_sampling_records_stable_pair_and_hold(tmp_path, monkeypatch):
    script = [(False, None)] + [(True, ["filter_panel", 5, [], [], None])] * 8
    session, clock = _session(tmp_path, monkeypatch, script)
    _note_input(session, 11, "open_panel", int(clock.now() * 1e9))
    session._measure_panel_action("open_panel", InputRelease("click_client", int(clock.now() * 1e9)))

    assert len(session.samples) == 1
    sample = session.samples[0]
    assert sample["valid"] is True and sample["invalid_reason"] is None
    assert sample["input_call_ids"] == [11]
    assert sample["t_ready_ns"] is not None and sample["t_stable_ns"] is not None
    assert sample["stable_ms"] >= 50
    assert sample["next_step_verified"] is False  # 尚未发生下一步输入
    rows = [row for row in session.evidence.observations if row["sample_id"] == sample["sample_id"]]
    assert len(rows) >= 5
    assert all(set(row) == set(gate.OBSERVATION_FIELDS) for row in rows)
    assert all(Path(session.round_dir / row["frame_path"]).is_file() for row in rows)

    # 回归：样本行必须随证据写入原始 JSONL（曾因会话与证据两份列表而丢失 sample 行）
    raw_path = session.round_dir / "raw.jsonl"
    session.evidence.write(raw_path)
    lines = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    sample_rows = [row for row in lines if row.get("type") == "sample"]
    assert len(sample_rows) == 1 and sample_rows[0]["sample_id"] == sample["sample_id"]
    assert any(row.get("type") == "observation" for row in lines)


def test_measure_sampling_marks_hold_violation_invalid(tmp_path, monkeypatch):
    key = ["filter_panel", 5, [], [], None]
    script = [(False, None)] + [(True, key)] * 2 + [(True, ["filter_panel", 6, [], [], None])] * 8
    session, clock = _session(tmp_path, monkeypatch, script)
    _note_input(session, 12, "open_panel", int(clock.now() * 1e9))
    session._measure_panel_action("open_panel", InputRelease("click_client", int(clock.now() * 1e9)))
    sample = session.samples[0]
    assert sample["valid"] is False
    assert "保持期" in sample["invalid_reason"]


def test_panel_plan_done_uses_samples_and_attempts():
    session = object.__new__(measure.MeasureSession)
    session.samples = []
    session._attempts = {}
    session.evidence = SimpleNamespace(inputs=[])
    assert measure._panel_plan_done(session) is False

    session.evidence = SimpleNamespace(inputs=[
        {"record": {"call_id": 1, "role": "close_panel_reset"}},
        {"record": {"call_id": 2, "role": "slot"}},
    ])
    samples = [
        {"action_id": action_id, "valid": True}
        for action_id in measure._PANEL_DRIVEN
        for _ in range(measure._TARGET_SAMPLES)
    ]
    session.samples = samples  # reset 没有路径样本：还不能算完成
    assert measure._panel_plan_done(session) is False
    session.samples = samples + [
        {"action_id": "reset", "valid": True, "next_step_call_id": 1} for _ in range(10)
    ] + [
        {"action_id": "reset", "valid": True, "next_step_call_id": 2} for _ in range(10)
    ]
    assert measure._panel_plan_done(session) is True

    session.samples = []
    session._attempts = {
        action_id: measure._MAX_ATTEMPTS_PER_ACTION for action_id in measure._PANEL_DRIVEN
    }
    session._attempts["reset"] = measure._MAX_ATTEMPTS_PER_ACTION * 2
    assert measure._panel_plan_done(session) is True


def test_reset_path_counts_split_by_next_role():
    session = object.__new__(measure.MeasureSession)
    session.samples = [
        {"action_id": "reset", "valid": True, "next_step_call_id": 1},
        {"action_id": "reset", "valid": True, "next_step_call_id": 2},
        {"action_id": "reset", "valid": True, "next_step_call_id": 99},
        {"action_id": "reset", "valid": False, "next_step_call_id": 1},
        {"action_id": "open_panel", "valid": True, "next_step_call_id": 1},
    ]
    session.evidence = SimpleNamespace(inputs=[
        {"record": {"call_id": 1, "role": "close_panel_reset"}},
        {"record": {"call_id": 2, "role": "close_panel_reset"}},
        {"record": {"call_id": 99, "role": "slot"}},
    ])
    assert measure._reset_path_counts(session) == (2, 1)


def test_drive_panel_cycles_alternates_and_stops(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("zzz_ddtool.gamefilter.apply_game_filter",
                        lambda hwnd, width, height, **kwargs: calls.append("apply"))
    monkeypatch.setattr("zzz_ddtool.gamefilter.reset_game_filter",
                        lambda hwnd, width, height: calls.append("reset"))
    monkeypatch.setattr("zzz_ddtool.substatfilter.plan_substat_filter",
                        lambda preset: SimpleNamespace(names=(), counts=()))
    monkeypatch.setattr("zzz_ddtool.filter_replay.FilterReplaySession", lambda: None)
    monkeypatch.setattr(measure, "_DRIVER_MIN_PANEL_CYCLES", 0)
    monkeypatch.setattr(measure, "_DRIVER_MAX_PANEL_CYCLES", 5)
    monkeypatch.setattr(measure, "_action_done", lambda session, action_id: False)
    runner = SimpleNamespace(
        _env=SimpleNamespace(client_width=1920, client_height=1080),
        _sub_preset=lambda job: object(),
    )
    job = SimpleNamespace(slot=5, sets=("甲",), mainstats=("乙",))
    session = object.__new__(measure.MeasureSession)
    session.evidence = SimpleNamespace(inputs=[])
    session.samples = []
    session._attempts = {}

    errors = measure._drive_panel_cycles(session, runner, 1, job)

    assert calls == ["apply", "reset", "apply", "reset", "apply"]  # 两条 reset 路径交替
    assert errors and "上限" in errors[0]

    calls.clear()
    monkeypatch.setattr(measure, "_action_done", lambda session, action_id: True)
    monkeypatch.setattr(measure, "_reset_path_counts", lambda session: (10, 10))
    assert measure._drive_panel_cycles(session, runner, 1, job) == []
    assert calls == []  # 计划已达成：一个周期都不跑


def test_drive_select_cycles_escape_and_menu_recovery(tmp_path, monkeypatch):
    from zzz_ddtool import wininput

    clicks: list[tuple] = []
    presses: list[int] = []
    waits: list[tuple] = []
    monkeypatch.setattr(wininput, "click_client", lambda hwnd, x, y: clicks.append((x, y)) or "r")
    monkeypatch.setattr(wininput, "press_key", lambda hwnd, vk=None, *a, **k: presses.append(vk))
    monkeypatch.setattr(wininput, "focus_window", lambda hwnd: None)
    monkeypatch.setattr("zzz_ddtool.nav.goto_warehouse", lambda hwnd: None)
    monkeypatch.setattr(measure, "_ensure_panel_closed", lambda runner, hwnd: None)
    monkeypatch.setattr("zzz_ddtool.gamefilter._panel_present", lambda image: False)
    monkeypatch.setattr("zzz_ddtool.gamefilter._picker_popup_closed", lambda image: True)
    monkeypatch.setattr(measure.time, "sleep", lambda seconds: None)
    cells = [
        SimpleNamespace(global_index=3, center_x=100, center_y=200),
        SimpleNamespace(global_index=4, center_x=300, center_y=200),
    ]
    pages = iter(["drive_disc", "menu", "drive_disc", "drive_disc"])
    runner = SimpleNamespace(
        _grab=lambda **kwargs: (object(), next(pages), "m", 1),
        _scan_cells=lambda state, image=None: (cells, [], []),
        _business_input=lambda kind, target=None: _cm(),
        _wait_ready=lambda kind, release=None, cell=None: waits.append((kind, cell.global_index)),
    )
    monkeypatch.setattr(measure, "_action_done", lambda session, action_id: False)
    monkeypatch.setattr(measure, "_MAX_ATTEMPTS_PER_ACTION", 2)
    job = SimpleNamespace(job_id="j", slot=5, sets=("甲",), mainstats=())
    preset = SimpleNamespace(max_pages=4)
    session = object.__new__(measure.MeasureSession)

    errors = measure._drive_select_cycles(session, runner, 1, preset, job)

    assert errors == []
    assert clicks == [(100, 200), (300, 200)]
    assert waits == [("select_disc", 3), ("select_disc", 4)]
    assert presses == [wininput.VK_ESCAPE] * 3  # 每次选盘后一次，菜单误开时补一次


class _cm:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_drive_calibration_wires_capture_and_closes_scope(monkeypatch):
    order: list[str] = []

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("zzz_ddtool.actionwait.collect_wait_receipts", lambda: _Ctx())
    monkeypatch.setattr("zzz_ddtool.control.controlled_inputs", lambda *args, **kwargs: _Ctx())
    monkeypatch.setattr("zzz_ddtool.wininput.capture_inputs", lambda sink: _Ctx())
    monkeypatch.setattr("zzz_ddtool.wininput.focus_window", lambda hwnd: order.append("focus"))
    monkeypatch.setattr("zzz_ddtool.nav.goto_warehouse", lambda hwnd: order.append("goto"))
    monkeypatch.setattr(measure, "_ensure_panel_closed", lambda runner, hwnd: order.append("clean"))
    monkeypatch.setattr(measure, "_drive_panel_cycles",
                        lambda session, runner, hwnd, job: order.append("panels") or [])
    monkeypatch.setattr(measure, "_drive_select_cycles",
                        lambda session, runner, hwnd, preset, job: order.append("selects") or [])
    job = SimpleNamespace(accepted=[1], mainstats=("乙",), slot=5, sets=("甲",), job_id="j")
    runner = SimpleNamespace(
        _pause_intervals=lambda since, until: (),
        _checkpoint=lambda: None,
        _close_release_scope=lambda: order.append("close_scope"),
        _bind_instance=lambda: order.append("bind") or 11,
        _observe_execution_input=lambda raw: None,
        _jobs=lambda: (job,),
        _sub_preset=lambda j: object(),
        _ensure_profile=lambda hwnd: order.append("profile"),
        _env=SimpleNamespace(client_width=1920, client_height=1080),
    )
    session = object.__new__(measure.MeasureSession)
    session.finish = lambda: order.append("finish")
    preset = SimpleNamespace(max_pages=4, max_discs=15)

    errors = measure.drive_calibration(session, runner, preset)

    assert errors == []
    assert order == ["bind", "focus", "goto", "clean", "profile", "panels", "selects", "close_scope", "finish"]


def test_executor_ctx_uses_production_cell_for_select_disc(tmp_path, monkeypatch):
    from zzz_ddtool.traversal import GridCell

    session, _clock = _session(tmp_path, monkeypatch, [(True, ["warehouse", 5, [], [], None])])
    cell = GridCell(global_index=7, global_row0=0, global_col0=1, screen_row0=0,
                    screen_col0=1, center_x=111, center_y=222, level=0)
    ctx = session._executor_ctx("select_disc", [], {"cell": cell})
    assert ctx.target == {"global_index": 7, "x": 111, "y": 222}


def test_missing_next_step_sample_is_invalid_at_finish(tmp_path, monkeypatch):
    script = [(False, None)] + [(True, ["filter_panel", 5, [], [], None])] * 8
    session, clock = _session(tmp_path, monkeypatch, script)
    _note_input(session, 11, "open_panel", int(clock.now() * 1e9))
    session._measure_panel_action("open_panel", InputRelease("click_client", int(clock.now() * 1e9)))
    sample = session.samples[0]
    assert sample["valid"] is True
    session.finish()
    assert sample["valid"] is False and sample["invalid_reason"] == "缺少下一步验证"


def _note_key_input(session, call_id: int, role: str, released_ns: int) -> None:
    """下一步是 Esc（close_panel/close_panel_reset）时的真实形态：press_key + vk=27。"""
    session._note_input({
        "t_ns": released_ns, "event": "end", "call_id": call_id, "api": "press_key",
        "role": role, "target": None, "stage_index": None, "selected": None, "hwnd": 1,
        "x": None, "y": None, "alt": None, "vk": 27, "delta_units": None,
        "status": "completed", "error": None, "released_at_ns": released_ns,
    })


def test_next_step_esc_only_is_verified_by_page_identity(tmp_path, monkeypatch):
    """§4.2/§6.2 第 6 条：下一步只有 Esc 时没有可视目标，按「页面身份仍须成立」验证。

    2026-10-11 第 6 轮 reset / substat_count 的下一步正是 Esc（close_panel），
    旧口径拿原动作判读去验面板已关闭的帧，13 个样本全被判无效。
    """
    script = [(False, None)] + [(True, ["filter_panel", 5, [], [], None])] * 8
    session, clock = _session(tmp_path, monkeypatch, script)
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor(4, 4, 4))
    session.runner._grab = lambda **_kw: (image, "warehouse", "stub", 1)
    _note_input(session, 11, "substat_count", int(clock.now() * 1e9))
    session._measure_panel_action("substat_count", InputRelease("click_client", int(clock.now() * 1e9)))
    sample = session.samples[0]
    _note_key_input(session, 12, "close_panel", int(clock.now() * 1e9))
    assert sample["next_step_call_id"] == 12
    assert sample["next_step_verified"] is True
    assert sample["valid"] is True


def test_next_step_esc_only_rejects_still_open_panel(tmp_path, monkeypatch):
    script = [(False, None)] + [(True, ["filter_panel", 5, [], [], None])] * 8
    session, clock = _session(tmp_path, monkeypatch, script)
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor(4, 4, 4))
    session.runner._grab = lambda **_kw: (image, "filter_panel", "stub", 1)
    _note_input(session, 11, "substat_count", int(clock.now() * 1e9))
    session._measure_panel_action("substat_count", InputRelease("click_client", int(clock.now() * 1e9)))
    sample = session.samples[0]
    _note_key_input(session, 12, "close_panel", int(clock.now() * 1e9))
    assert sample["next_step_verified"] is False
    assert sample["valid"] is False and sample["invalid_reason"] == "下一步未能作用于正确对象"


def test_judge_clickable_uses_panel_content_bottom_not_input_dead_band():
    """判读可点性以面板内容区下沿为界：真机最后一行主属性（bottom≈932）必须可点。"""
    from zzz_ddtool import gamefilter as gf

    class _Image:
        def width(self) -> int:
            return 1920

        def height(self) -> int:
            return 1080

    image = _Image()
    assert gf._PANEL_CLICK_BOTTOM_GAP == 160  # production 的开关避让带不变
    assert measure.judge._clickable(932, image) is True
    assert measure.judge._clickable(980, image) is True
    assert measure.judge._clickable(1010, image) is False  # 重置/批量操作栏


def test_judge_open_mainstats_ready_with_last_row_option(monkeypatch):
    """真机帧口径回归：主属性已展开、所需词条在最后一行也判就绪（2026-10-11 第 6 轮 15/15 失效）。"""
    from dataclasses import dataclass

    from zzz_ddtool import gamefilter as gf

    @dataclass
    class _Line:
        text: str
        left: int
        top: int
        right: int
        bottom: int
        score: float = 0.99

    class _Image:
        def width(self) -> int:
            return 1920

        def height(self) -> int:
            return 1080

    lines = [
        _Line(gf.MAINSTAT_TITLE, 1300, 680, 1402, 707),
        _Line("点击收起", 1524, 732, 1605, 760),
        _Line("攻击力百分比", 1367, 788, 1483, 814),
        _Line("异常精通", 1665, 901, 1746, 932),
    ]
    monkeypatch.setattr(measure.judge, "_panel_lines", lambda image: lines)
    monkeypatch.setattr(measure.judge, "_toggle_state", lambda l, t, i: True)
    monkeypatch.setattr(gf, "_option_selected", lambda image, line: False)
    ctx = measure.judge.JudgeContext(target=None, config={"mainstats": ("异常精通",)}, pending_names=())
    result = measure.judge.evaluate("open_mainstats", _Image(), ctx)
    assert result.ready is True
    assert result.boxes == [[1665, 901, 1746, 932]]


def test_next_step_input_is_not_absorbed_into_call_ids(tmp_path, monkeypatch):
    script = [(False, None)] + [(True, ["filter_panel", 5, [], [], None])] * 8
    session, clock = _session(tmp_path, monkeypatch, script)
    _note_input(session, 11, "open_panel", int(clock.now() * 1e9))
    session._measure_panel_action("open_panel", InputRelease("click_client", int(clock.now() * 1e9)))
    sample = session.samples[0]
    _note_input(session, 12, "open_panel", int(clock.now() * 1e9))  # 同角色紧随输入＝下一步
    assert sample["input_call_ids"] == [11]
    assert sample["next_step_call_id"] == 12 and sample["next_step_verified"] is True
    assert sample["valid"] is True
    rows = [row for row in session.evidence.observations if row["phase"] == "after_next_step"]
    assert rows and all(row["sample_id"] == sample["sample_id"] for row in rows)


def test_substat_scroll_sample_groups_eight_events(tmp_path, monkeypatch):
    script = [(False, None)] + [(True, ["filter_panel", 5, [], [], None])] * 8
    session, clock = _session(tmp_path, monkeypatch, script)
    ids = [30 + index for index in range(8)]
    for call_id in ids:
        _note_input(session, call_id, "substat_scroll", int(clock.now() * 1e9))
    session._measure_panel_action(
        "substat_scroll", InputRelease("send_wheel_event", int(clock.now() * 1e9)),
    )
    assert session.samples[0]["input_call_ids"] == ids
    assert session.samples[0]["input_call_id"] == ids[-1]


def test_sample_records_before_frame_from_production_grab(tmp_path, monkeypatch):
    from PySide6.QtGui import QColor, QImage

    script = [(False, None)] + [(True, ["filter_panel", 5, [], [], None])] * 8
    session, clock = _session(tmp_path, monkeypatch, script)
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor(5, 5, 5))
    session._last_grab = (image, clock.now())
    session._measure_panel_action("open_panel", InputRelease("click_client", int(clock.now() * 1e9)))
    sample = session.samples[0]
    assert sample["before_frame_path"] and sample["before_frame_sha256"]
    assert (session.round_dir / sample["before_frame_path"]).is_file()


def test_t4_panel_scroll_events_and_single_tail_budget(tmp_path, monkeypatch):
    """§4.2/T4：面板 raw 滚轮 8 条 -3、事件间隔 60 ms 不变，仅最后一条后按 310 ms 预算一次。"""
    from contextlib import contextmanager

    from zzz_ddtool import gamefilter as gf

    clock = _Clock(100.0)
    wheels: list[int] = []
    receipts: list = []
    contexts: list[tuple] = []
    waits: list[tuple] = []

    @contextmanager
    def fake_input_context(role, **kwargs):
        contexts.append((role, dict(kwargs)))
        yield

    def fake_send(delta):
        wheels.append(delta)
        receipt = InputRelease("send_wheel_event", int(clock.now() * 1e9))
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(gf, "input_context", fake_input_context)
    monkeypatch.setattr(gf, "send_wheel_event", fake_send)
    monkeypatch.setattr(gf, "wait_release",
                        lambda role, release, fallback_ms=None: waits.append((role, release, fallback_ms)))
    monkeypatch.setattr(gf, "focus_window", lambda hwnd: None)
    monkeypatch.setattr(gf, "client_to_screen", lambda hwnd, x, y: (x, y))
    monkeypatch.setattr(gf, "user32", SimpleNamespace(SetCursorPos=lambda x, y: None))
    monkeypatch.setattr(gf.time, "sleep", clock.sleep)

    gf._scroll_panel_to_bottom(1, 1920, 1080)

    assert wheels == [-3] * 8
    assert contexts == [("substat_scroll", {"defer_post_release": True})]
    stamps = [int(receipt.released_at_ns) for receipt in receipts]
    deltas = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
    assert deltas == [60 * _MS] * 7  # 事件间 60 ms 不变
    assert waits == [("substat_scroll", receipts[-1], 310)]  # 仅最后一条后一次预算，不叠加


def test_final_run_rejects_no_speedup_keeps_report(tmp_path, monkeypatch):
    """T13：§7.2 实测全不降 → 自身判定失败退 1、保留失败报告、不复制固定名、不再操作游戏。"""
    from final_evidence_fixture import minimal_events

    from zzz_ddtool import loground

    config = tmp_path / "preset.yaml"
    config.write_text(_MINIMAL_YAML, encoding="utf-8")
    # 本轮性能文件由当前轮次直接给出（不再按目录里最新的 jsonl 猜）。
    round_ = loground.use_test_context("release-measure", root=tmp_path / "log_test")
    round_.perf_path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in minimal_events("run-final")) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(measure, "_probe_environment", lambda: {
        "machine_id": "m", "client_width": 1920, "client_height": 1080, "window_dpi": 96,
        "game_exe_path": "c:/game.exe", "game_build": "b1",
    })

    class FakeRunner:
        instances: list = []

        def __init__(self, preset, *, hotkeys=True, claim_round=False):
            self.preset = preset
            self.hotkeys = hotkeys
            self.claim_round = claim_round
            self.ran = False
            FakeRunner.instances.append(self)

        def run(self):
            self.ran = True  # 替身不发送任何游戏输入

    monkeypatch.setattr(measure, "Runner", FakeRunner)
    copied_calls: list = []
    monkeypatch.setattr(measure, "copy_fixed", lambda *args, **kwargs: copied_calls.append(args) or True)
    output = tmp_path / "round" / "final.json"
    output.parent.mkdir()

    code = measure._run_final(config, output, None, hotkeys=False)

    assert code == 1
    assert FakeRunner.instances and FakeRunner.instances[0].ran
    assert FakeRunner.instances[0].claim_round is True  # 入口先开好轮次，Runner 认领它
    assert copied_calls == []  # 自身判定失败：不复制固定名
    report = common.read_json(output)
    assert report["passed"] is False and report["speedup"] == []
    assert report["stage"] == "final"
    assert Path(report["raw_jsonl_path"]) == round_.perf_path  # 复算用的是本轮 perf
    loground.close_active()  # 收尾释放占用：不把活动轮次留给后面的用例


@pytest.mark.parametrize("visible,usable,expected", [(True, True, True), (False, True, False), (True, False, False)])
def test_judge_set_option_uses_name_geometry_without_checkmark(monkeypatch, visible, usable, expected):
    judge = measure.judge
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(QColor(180, 220, 0))
    target = SimpleNamespace(left=1188, top=491, right=1266, bottom=517)
    pending = SimpleNamespace(left=1350, top=491, right=1428, bottom=517 if usable else 1080)
    targets = {"pending": pending}
    if visible:
        targets["target"] = target
    monkeypatch.setattr(judge, "_picker_layout", lambda _: object())
    monkeypatch.setattr(judge, "_picker_targets", lambda *_: targets)
    result = judge.evaluate("set_option", image, judge.JudgeContext(target="target", pending_names=("pending",)))
    assert result.ready is expected
    assert dict(result.states) == {"target_visible": visible, "pending_usable": usable}
