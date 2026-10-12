"""S4 门禁离线用例（Spec §8.1、§9 T13）：离线汇总、校准/终验复算的拒绝与通过。

只读合成证据；判读用脚本化替身（真实 PNG 复算路径由 S5 真机证据承担）。
"""
from __future__ import annotations

import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor, QImage

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "dev-docs" / "功能改造" / "操作等待提速" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_release_offline as build  # noqa: E402
import release_wait_common as common  # noqa: E402
import verify_release_waits as gate  # noqa: E402

from zzz_ddtool import actionwait  # noqa: E402
from zzz_ddtool import config as _config  # noqa: E402

CASE_IDS = [f"T{i}" for i in range(1, 14)]
RELEASE_NS = 1_000_000_000
_MS = 1_000_000

_MINIMAL_YAML = """
name: fixture
game_filter:
  slot: 5
  slots: [5]
accepted: []
"""

_SETS_YAML = """
name: fixture-sets
game_filter:
  slot: 5
  slots: [5]
  sets: [甲]
accepted: []
"""

_KEY = ["filter_panel", None, [], [], None]


# --------------------------------------------------------------------------
# 离线汇总
# --------------------------------------------------------------------------
def _write_junit(path: Path, nodes: list[tuple[str, str]]) -> None:
    suite = ET.Element("testsuite", name="pytest")
    for nodeid, status in nodes:
        case = ET.SubElement(suite, "testcase", classname="x", name=nodeid.split("::")[-1])
        props = ET.SubElement(case, "properties")
        ET.SubElement(props, "property", name="pytest_nodeid", value=nodeid)
        if status == "failed":
            ET.SubElement(case, "failure", message="boom")
        elif status == "skipped":
            ET.SubElement(case, "skipped")
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def _node(index: int) -> str:
    return f"tests/test_mod.py::test_case_{index}"


def _build_offline(tmp_path: Path, *, status_of=None, cases_of=None):
    status_of = status_of or {}
    nodes = [(_node(i), status_of.get(i, "passed")) for i in range(3)]
    junit = tmp_path / "s2-junit.xml"
    _write_junit(junit, nodes)
    cases = cases_of or {key: [_node(i % 3)] for i, key in enumerate(CASE_IDS)}
    cases_path = tmp_path / "用例映射.json"
    cases_path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "offline.json"
    report, code = build.build([junit], cases_path, out)
    return junit, cases_path, out, report, code


def test_offline_gate_accepts_consistent_report(tmp_path):
    _junit, _cases, out, report, code = _build_offline(tmp_path)
    assert code == 0 and report["passed"] is True
    checks = gate.check_offline(out)
    assert checks.verdict, [(item["id"], item["detail"]) for item in checks.items if not item["passed"]]
    assert gate.main(["--stage", "offline", "--report", str(out)]) == 0


def test_offline_gate_rejects_missing_or_failed_mapped_nodes(tmp_path):
    _junit, _cases, out, report, code = _build_offline(
        tmp_path, status_of={0: "failed", 1: "skipped"},
    )
    assert code == 1 and report["passed"] is False
    checks = gate.check_offline(out)
    assert checks.verdict is False
    messages = " ".join(str(item["detail"]) for item in checks.items if not item["passed"])
    assert _node(0) in messages and _node(1) in messages

    other = tmp_path / "missing-node"
    other.mkdir()
    _junit2, _cases2, out2, report2, code2 = _build_offline(
        other, cases_of={key: ["tests/test_mod.py::test_missing"] for key in CASE_IDS},
    )
    assert code2 == 1 and report2["passed"] is False
    assert gate.check_offline(out2).verdict is False


def test_offline_gate_rejects_tampered_junit_and_identity_change(tmp_path):
    junit, _cases, out, report, code = _build_offline(tmp_path)
    assert code == 0
    junit.write_bytes(junit.read_bytes() + b"<!-- tampered -->")
    checks = gate.check_offline(out)
    assert checks.verdict is False
    messages = " ".join(str(item["detail"]) for item in checks.items if not item["passed"])
    assert "字节摘要不符" in messages

    identity = common.execution_identity()
    rel = next(rel for rel in identity["files"] if rel.startswith("src/"))
    altered = dict(identity["files"])
    altered[rel] = "0" * 64
    assert any("字节不一致" in message for message in
               common.identity_errors({"python_version": identity["python_version"], "files": altered}))
    missing = dict(identity["files"])
    missing.pop(rel)
    messages = common.identity_errors({"python_version": identity["python_version"], "files": missing})
    assert any("新增文件" in message and rel in message for message in messages), messages

    extra = ROOT / "tests" / "test_extra_new_file.py"  # 仅注入枚举集合，不落盘
    original = common.iter_identity_files
    try:
        common.iter_identity_files = lambda: list(original()) + [extra]
        errors = common.identity_errors(identity)
    finally:
        common.iter_identity_files = original
    assert any("新增文件" in message for message in errors)


# --------------------------------------------------------------------------
# 校准：合成证据
# --------------------------------------------------------------------------
class _StubResult:
    def __init__(self, ready, state_key):
        self.ready = ready
        self._key = state_key

    def state_key(self):
        return self._key


def _png(path: Path, tone: int = 9) -> tuple[str, str]:
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor(tone, tone, tone))
    assert image.save(str(path), "PNG")
    return path.as_posix(), common.sha256_file(path)


def _input_rows(call_id: int, role: str, released_at_ns: int) -> list[dict]:
    base = {
        "call_id": call_id, "api": "click_client", "role": role, "target": None,
        "stage_index": None, "selected": None, "hwnd": 1, "x": 10, "y": 20, "alt": None,
        "vk": None, "delta_units": None, "error": None,
    }
    begin = {**base, "t_ns": released_at_ns - 1_000_000, "event": "begin",
             "status": "started", "released_at_ns": None}
    end = {**begin, "t_ns": released_at_ns + 1_000_000, "event": "end",
           "status": "completed", "released_at_ns": released_at_ns}
    return [
        {"type": "input", "sample_id": None, "record": begin},
        {"type": "input", "sample_id": None, "record": end},
    ]


def _synthetic_calibration(
    tmp_path: Path, monkeypatch, *, actions=("open_panel", "reset", "select_disc"),
    per_action=10, hold_key_change=False, fabricated_next=False, wrong_next=False,
    missing_next=False, release_mismatch=False, config_sha=None, yaml=None,
):
    round_dir = tmp_path / "验收结果" / "round-1"
    round_dir.mkdir(parents=True, exist_ok=True)
    evidence = round_dir / "evidence"
    evidence.mkdir(exist_ok=True)
    config_path = tmp_path / "preset.yaml"
    config_path.write_text(yaml or _MINIMAL_YAML, encoding="utf-8")

    # 独立给出 20 ms 帧序列：80 ms 首次到达最终态，最后变化也是 80 ms；区间为 [80, 100]。
    offsets = [0, 20, 40, 60, 80, 100]
    samples: list[dict] = []
    observations: list[dict] = []
    inputs: list[dict] = []
    call_id = 100
    for action_id in actions:
        for index in range(per_action):
            call_id += 1
            action_call = call_id
            released = RELEASE_NS + call_id * 10_000_000
            next_call = call_id + 1
            call_id = next_call
            inputs.extend(_input_rows(action_call, action_id, released))
            inputs.extend(_input_rows(next_call, "slot", released + 350 * _MS))
            sample_id = f"{action_id}-{index}"
            binding = {"hwnd": 1, "pid": 2, "client_width": 1920, "client_height": 1080,
                       "window_dpi": 96, "pause_generation": 0}
            before_rel, before_sha = _png(evidence / f"{sample_id}-before.png")
            csv = ["t_ns,sig_sha256,interval_ms,matches_reference,changed"]
            previous = None
            for step, offset in enumerate(offsets):
                tone = 90 if step >= 4 else 9
                if hold_key_change and step == 5:
                    tone = 45  # 后续又变了：声明的最后变化时间/上界必须被独立复算拒绝。
                _png(evidence / f"{sample_id}-{step}.png", tone)
                signature = hashlib.sha256(bytes([tone]) * 128 * 128).hexdigest()
                csv.append(f"{released + offset * _MS},{signature},{20 if step else 0},"
                           f"{str(tone == 90).lower()},{str(previous is not None and tone != previous).lower()}")
                previous = tone
            (evidence / f"{sample_id}-frames.csv").write_text("\n".join(csv) + "\n", encoding="utf-8")
            _png(evidence / f"{sample_id}-reach.png", 90)
            _png(evidence / f"{sample_id}-lastchange.png", 90)
            confirm_rel, confirm_sha = _png(evidence / f"{sample_id}-confirm.png", 90)
            observations.append({
                "type": "observation", "sample_id": sample_id, "input_call_id": action_call,
                "action_id": action_id, "phase": "after_action",
                "captured_at_ns": released + 300 * _MS,
                "probe_ended_at_ns": released + 305 * _MS,
                "ready": True, "state_key": _KEY, "binding": binding,
                "frame_path": Path(confirm_rel).relative_to(round_dir).as_posix(), "frame_sha256": confirm_sha,
            })
            next_rel, next_sha = _png(evidence / f"{sample_id}-next.png", 90)
            observations.append({
                "type": "observation", "sample_id": sample_id, "input_call_id": next_call,
                "action_id": action_id, "phase": "after_next_step",
                "captured_at_ns": released + 400 * _MS,
                "probe_ended_at_ns": released + 405 * _MS,
                "ready": True, "state_key": ["probe_hold_ms", 100] if index == 0 else _KEY,
                "binding": binding,
                "frame_path": Path(next_rel).relative_to(round_dir).as_posix(),
                "frame_sha256": next_sha,
            })
            if action_id == "reset" and not fabricated_next:
                seg_rel, seg_sha = _png(evidence / f"{sample_id}-seg.png")
                observations.append({
                    "type": "observation", "sample_id": sample_id, "input_call_id": next_call,
                    "action_id": action_id, "phase": "after_next_step",
                    "captured_at_ns": released + 900 * _MS,
                    "probe_ended_at_ns": released + 905 * _MS,
                    "ready": True, "state_key": _KEY, "binding": binding,
                    "frame_path": Path(seg_rel).relative_to(round_dir).as_posix(),
                    "frame_sha256": seg_sha,
                })
            samples.append({
                "type": "sample", "sample_id": sample_id, "action_id": action_id,
                "input_call_id": action_call,
                "released_at_ns": released + (7_000_000 if release_mismatch else 0),
                "binding": binding,
                "before_frame_path": Path(before_rel).relative_to(round_dir).as_posix(),
                "before_frame_sha256": before_sha,
                "layout_id": None, "return_edge": None,
                "t_reach_ns": released + 80 * _MS, "t_last_change_ns": released + 80 * _MS,
                "wait_lower_ms": 80, "wait_upper_ms": 100, "sample_interval_ms": 20,
                "next_step_call_id": None if missing_next else (999_999 if fabricated_next else next_call),
                "next_step_captured_at_ns": released + 400 * _MS,
                "next_step_verified": not (wrong_next or missing_next),
                "valid": not wrong_next,
                "invalid_reason": "下一步未能作用于正确对象" if wrong_next else None,
                "input_call_ids": [action_call],
            })

    raw_path = evidence / "measure.jsonl"
    raw_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False)
                  for row in inputs + observations + samples) + "\n",
        encoding="utf-8",
    )
    # 只隔离最终 PNG 的视觉判读；区间、输入、CSV、哈希和有效性均由真实门禁复算。
    monkeypatch.setattr(gate.judge, "evaluate",
                        lambda action_id, image, ctx: _StubResult(image.pixelColor(0, 0).red() == 90, _KEY))

    report = {
        "schema_version": 2, "stage": "calibration",
        "spec_sha256": common.sha256_file(common.SPEC),
        "execution_identity": common.execution_identity(),
        "environment": {"machine_id": "m", "client_width": 1920, "client_height": 1080,
                        "window_dpi": 96, "game_exe_path": "c:/game.exe", "game_build": "b1"},
        "config_path": str(config_path),
        "config_sha256": config_sha or common.sha256_file(config_path),
        "raw_jsonl_path": str(raw_path),
        "raw_jsonl_sha256": common.sha256_file(raw_path),
        "actions": [],
        "errors": [],
        "passed": True,
    }
    import measure_release_waits as measure

    report["actions"] = measure.action_rows(samples, common.action_ids_in_spec_order(),
                                             {action_id: True for action_id in actions})
    report_path = round_dir / "calibration.json"
    common.write_json(report_path, report)
    return report, report_path


def test_calibration_gate_installs_profile_on_success(tmp_path, monkeypatch):
    report, report_path = _synthetic_calibration(tmp_path, monkeypatch)
    monkeypatch.setattr("zzz_ddtool.paths.repo_root", lambda: tmp_path)
    checks = gate._calibration_checks(report, report_path, install_profile=True)
    assert checks.verdict, [(item["id"], item["detail"]) for item in checks.items if not item["passed"]]
    profile = tmp_path / "user" / "post-release-waits.json"
    assert profile.is_file()
    policies = actionwait.load_policies(SimpleNamespace(**report["environment"]))
    assert set(policies) == {"open_panel", "reset", "select_disc"}
    assert all(policy.floor_ms == 120 for policy in policies.values())


def test_calibration_self_check_shallow_accepts_and_still_rejects(tmp_path, monkeypatch):
    """自身判定（deep=False）不重跑 OCR：一致证据放行，伪造下一步仍拒绝。"""
    report, report_path = _synthetic_calibration(tmp_path, monkeypatch)
    checks = gate._calibration_checks(report, report_path, install_profile=False, deep=False)
    assert checks.verdict, [(item["id"], item["detail"]) for item in checks.items if not item["passed"]]

    other = tmp_path / "b"
    other.mkdir()
    report2, report_path2 = _synthetic_calibration(
        other, monkeypatch, actions=("open_panel",), fabricated_next=True,
    )
    checks2 = gate._calibration_checks(report2, report_path2, install_profile=False, deep=False)
    assert checks2.verdict is False


@pytest.mark.parametrize("suffix,message", [
    ("frames.csv", "缺少帧签名 CSV"), ("reach.png", "缺少 t_reach 证据 PNG"),
])
def test_calibration_gate_rejects_missing_signature_evidence(tmp_path, monkeypatch, suffix, message):
    report, report_path = _synthetic_calibration(tmp_path, monkeypatch)
    evidence = Path(report["raw_jsonl_path"]).parent
    (evidence / f"open_panel-0-{suffix}").unlink()

    checks = gate._calibration_checks(report, report_path, install_profile=False)

    assert checks.verdict is False
    check = next(item for item in checks.items if item["id"] == "C7")
    assert check["passed"] is False and message in str(check["detail"])


def test_calibration_gate_rejects_key_change_inside_hold_window(tmp_path, monkeypatch):
    report, report_path = _synthetic_calibration(
        tmp_path, monkeypatch, actions=("open_panel",), hold_key_change=True,
    )
    checks = gate._calibration_checks(report, report_path, install_profile=False)
    assert checks.verdict is False
    messages = " ".join(str(item["detail"]) for item in checks.items if not item["passed"])
    assert "保持期" in messages or "复算不一致" in messages


def test_calibration_gate_rejects_fabricated_next_step_verified(tmp_path, monkeypatch):
    report, report_path = _synthetic_calibration(
        tmp_path, monkeypatch, actions=("open_panel",), fabricated_next=True,
    )
    checks = gate._calibration_checks(report, report_path, install_profile=False)
    assert checks.verdict is False
    messages = " ".join(str(item["detail"]) for item in checks.items if not item["passed"])
    assert "next_step_verified" in messages or "下一步" in messages


def test_calibration_gate_rejects_wrong_target_next_step(tmp_path, monkeypatch):
    """T13：下一步点错目标（样本判为无效却满足复算条件）→ 门禁拒绝。"""
    report, report_path = _synthetic_calibration(
        tmp_path, monkeypatch, actions=("open_panel",), wrong_next=True,
    )
    checks = gate._calibration_checks(report, report_path, install_profile=False)
    assert checks.verdict is False
    messages = " ".join(str(item["detail"]) for item in checks.items if not item["passed"])
    assert "未声明 verified" in messages or "复算不一致" in messages


def test_calibration_gate_rejects_valid_sample_without_next_step(tmp_path, monkeypatch):
    """§6.2 第 6 条：没有已验证下一步的样本不能算有效样本。"""
    report, report_path = _synthetic_calibration(
        tmp_path, monkeypatch, actions=("open_panel",), missing_next=True,
    )
    checks = gate._calibration_checks(report, report_path, install_profile=False)
    assert checks.verdict is False
    messages = " ".join(str(item["detail"]) for item in checks.items if not item["passed"])
    assert "缺少下一步验证" in messages


def test_substat_scroll_event_series_is_checked():
    def rows(count=8, delta=-3, step=60_000_000, api="send_wheel_event"):
        inputs = {}
        for index in range(count):
            call_id = 100 + index
            record = {
                "t_ns": index * step, "event": "end", "call_id": call_id,
                "api": api, "delta_units": delta, "status": "completed",
                "released_at_ns": index * step,
            }
            inputs[call_id] = {"end": record}
        return inputs

    good = list(range(100, 108))
    assert gate._substat_scroll_errors("s", good, rows()) == []
    assert any("8 条" in message for message in gate._substat_scroll_errors("s", good[:7], rows()))
    assert any("send_wheel_event" in message
               for message in gate._substat_scroll_errors("s", good, rows(api="wheel_client")))
    assert any("60 ms" in message
               for message in gate._substat_scroll_errors("s", good, rows(step=50_000_000)))
    assert any("delta" in message
               for message in gate._substat_scroll_errors("s", good, rows(delta=-6)))


def test_calibration_gate_rejects_call_id_or_config_mismatch(tmp_path, monkeypatch):
    report, report_path = _synthetic_calibration(
        tmp_path, monkeypatch, actions=("open_panel",), release_mismatch=True,
    )
    checks = gate._calibration_checks(report, report_path, install_profile=False)
    assert checks.verdict is False
    messages = " ".join(str(item["detail"]) for item in checks.items if not item["passed"])
    assert "释放点" in messages

    other = tmp_path / "b"
    other.mkdir()
    report2, report_path2 = _synthetic_calibration(
        other, monkeypatch, actions=("open_panel",), config_sha="0" * 64,
    )
    checks2 = gate._calibration_checks(report2, report_path2, install_profile=False)
    assert checks2.verdict is False
    assert any("config" in str(item["detail"]) for item in checks2.items if not item["passed"])


def test_calibration_gate_rejects_missing_required_action(tmp_path, monkeypatch):
    """§6.4 最低集合：必需动作未取得合格测量不得声明完成。"""
    report, report_path = _synthetic_calibration(
        tmp_path, monkeypatch, actions=("open_panel", "reset"), per_action=10,
    )
    checks = gate._calibration_checks(report, report_path, install_profile=False)
    assert checks.verdict is False
    messages = " ".join(str(item["detail"]) for item in checks.items if not item["passed"])
    assert "select_disc" in messages


def test_calibration_gate_allows_not_occurred_conditional_group(tmp_path, monkeypatch):
    """§6.4：条件不成立（从未发生）的组记 not_observed，不阻断本轮测量完成。"""
    report, report_path = _synthetic_calibration(
        tmp_path, monkeypatch, yaml=_SETS_YAML,
    )
    checks = gate._calibration_checks(report, report_path, install_profile=False)
    assert checks.verdict, [(item["id"], item["detail"]) for item in checks.items if not item["passed"]]
    by_action = {row["action_id"]: row for row in report["actions"]}
    assert by_action["open_sets"]["status"] == "not_observed"
    assert by_action["open_sets"]["sample_count"] == 0


# --------------------------------------------------------------------------
# 终验：合成 perf 证据
# --------------------------------------------------------------------------
def _final_evidence(tmp_path: Path, *, strip_release=False, measurement_rows=False,
                    speedup=None):
    from final_evidence_fixture import minimal_events

    (tmp_path / "preset.yaml").write_text(_MINIMAL_YAML, encoding="utf-8")
    events = minimal_events("run-x")
    if strip_release:
        root = events[-1]
        for item in (root["details"]["inputs"] or []):
            item.pop("released_at_ns", None)
            item.pop("release_wait", None)
        for event in events:
            if event.get("component") == "disc_total":
                for item in (event.get("details") or {}).get("inputs") or []:
                    item.pop("released_at_ns", None)
                    item.pop("release_wait", None)
    raw_path = tmp_path / "perf.jsonl"
    if measurement_rows:
        raw_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in [
                {"type": "sample", "sample_id": "s1"},
                {"type": "observation", "sample_id": "s1"},
            ]) + "\n",
            encoding="utf-8",
        )
    else:
        raw_path.write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
            encoding="utf-8",
        )
    from zzz_ddtool import runevidence

    root = next((e for e in events if e.get("component") == "run_total"), None)
    execution = {
        "run_id": root.get("run_id") if root else None,
        "jobs": (root.get("details") or {}).get("jobs") if root else None,
        "discs": runevidence.disc_records(events),
        "timing": runevidence.timing_record(events, root) if root else {},
        "coverage": runevidence.coverage_record(events, root.get("run_id")) if root else {},
        "processed": runevidence.processed_count(events),
    }
    execution_path = tmp_path / "execution.json"
    common.write_json(execution_path, execution)
    calibration_path = tmp_path / "calibration.json"
    common.write_json(calibration_path, {"schema_version": 1, "stage": "calibration",
                                         "actions": [], "passed": True})
    timing = gate._timing_errors(
        {"timing": {key: 0 for key in gate.TIMING_FIELDS}}, events, execution_path,
    )[1].get("recomputed") or {key: 0 for key in gate.TIMING_FIELDS}
    report = {
        "schema_version": 1, "stage": "final",
        "spec_sha256": common.sha256_file(common.SPEC),
        "execution_identity": common.execution_identity(),
        "environment": {"machine_id": "m", "client_width": 1920, "client_height": 1080,
                        "window_dpi": 96, "game_exe_path": "c:/game.exe", "game_build": "b1"},
        "config_path": str(tmp_path / "preset.yaml"),
        "config_sha256": common.sha256_file(tmp_path / "preset.yaml"),
        "raw_jsonl_path": str(raw_path),
        "raw_jsonl_sha256": common.sha256_file(raw_path),
        "actions": [],
        "errors": [],
        "passed": True,
        "processed": execution["processed"],
        "execution_report_path": str(execution_path),
        "execution_report_sha256": common.sha256_file(execution_path),
        "calibration_report_path": str(calibration_path),
        "calibration_report_sha256": common.sha256_file(calibration_path),
        "timing": timing,
        "speedup": speedup if speedup is not None else [],
    }
    import measure_release_waits as measure

    report["actions"] = measure.action_rows([], common.action_ids_in_spec_order())
    report_path = tmp_path / "final.json"
    common.write_json(report_path, report)
    return report, report_path


def test_final_gate_rejects_processed_zero_and_measurement_jsonl(tmp_path):
    report, report_path = _final_evidence(tmp_path, measurement_rows=True)
    report["processed"] = 0
    common.write_json(report_path, report)
    checks = gate._final_checks(report, report_path)
    assert checks.verdict is False
    messages = " ".join(str(item["detail"]) for item in checks.items if not item["passed"])
    assert "perf" in messages or "测量型" in messages


def test_final_gate_rejects_missing_release_evidence_and_no_speedup(tmp_path):
    report, report_path = _final_evidence(tmp_path, strip_release=True)
    checks = gate._final_checks(report, report_path)
    assert checks.verdict is False
    messages = " ".join(str(item["detail"]) for item in checks.items if not item["passed"])
    assert "发布" in messages or "释放" in messages or "字段缺失" in messages or "输入" in messages

    other = tmp_path / "b"
    other.mkdir()
    report2, report_path2 = _final_evidence(other)
    checks2 = gate._final_checks(report2, report_path2)
    assert checks2.verdict is False
    failed2 = {item["id"] for item in checks2.items if not item["passed"]}
    assert "F9" in failed2, [(item["id"], item["detail"]) for item in checks2.items]
    assert "F10" in failed2  # 自报 passed 与复算不一致必须联动拒绝


def test_final_gate_rejects_tampered_execution_evidence(tmp_path):
    """T13：执行记录（决策/处理数量）与原始 perf 复算不匹配 → 门禁拒绝。"""
    report, report_path = _final_evidence(tmp_path)
    execution_path = Path(report["execution_report_path"])
    execution = common.read_json(execution_path)
    execution["processed"] = int(execution.get("processed") or 0) + 1
    common.write_json(execution_path, execution)
    report["execution_report_sha256"] = common.sha256_file(execution_path)
    common.write_json(report_path, report)
    checks = gate._final_checks(report, report_path)
    assert checks.verdict is False
    failed = {item["id"]: item["detail"] for item in checks.items if not item["passed"]}
    assert "F7" in failed, [(item["id"], item["detail"]) for item in checks.items]
    messages = " ".join(str(detail) for detail in failed.values())
    assert "复算不一致" in messages
