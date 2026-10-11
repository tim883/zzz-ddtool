"""calibration / final 阶段门禁（Spec §9.1、§9.1.1、§9.2）的独立复算回归。

只断言 ``verify_spec.py`` 的判据逻辑：不连游戏、不跑 runner。报告一律在 tmp 里合成，
``SPEC``/``ROOT``/``execution_identity`` 与报告路径都打桩，不碰仓库里的 验收结果/。
"""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from copy import deepcopy
from final_evidence_fixture import PLAN, minimal_events, two_job_events
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "dev-docs" / "history" / "扫描提速" / "scripts" / "verify_spec.py"
MEASURE = ROOT / "dev-docs" / "history" / "扫描提速" / "scripts" / "measure_waits.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vs = _load(SCRIPT, "verify_spec_gate_under_test")
mw = _load(MEASURE, "measure_waits_ab_under_test")


# --------------------------------------------------------------------------
# 夹具构造
# --------------------------------------------------------------------------
def probes(count, *, key=("A",), t0=100.0, step=0.05, ready=None):
    """按 §9.1 口径造一段原始 sampling probes；``ready`` 可给标量或逐帧序列。

    同时带 ``state_key``（门禁读）与 ``_key``（采样脚本读），让两侧能在同一份序列上 A/B。
    """
    items = []
    for index in range(count):
        if ready is None:
            is_ready = True
        elif isinstance(ready, bool):
            is_ready = ready
        else:
            is_ready = ready[index]
        items.append({
            "captured_at": t0 + index * step,
            "ready": is_ready,
            "state_key": list(key) if key is not None else None,
            "_key": key,
        })
    return items


def with_stuck_tail(items, *, count, start_at, step=0.05):
    """追加一段一直不就绪的帧（内容卡住）：观察窗口永远追不上最后一个变化帧。"""
    return items + [
        {"captured_at": start_at + index * step, "ready": False,
         "state_key": None, "_key": None}
        for index in range(count)
    ]


def sample(sample_id, kind, probe_list, *, t0=100.0, edge=None, selection_id=None):
    """用与门禁**同一套复算**填好时刻字段的一条样本（门禁会独立重算并比对）。"""
    calc = vs._recompute_times(probe_list, t0=t0)
    return {
        "sample_id": sample_id,
        "kind": kind,
        "t0": t0,
        "probes": probe_list,
        "t_bad_ms": calc["t_bad_ms"],
        "t_ready_ms": calc["t_ready_ms"],
        "t_stable_ms": calc["t_stable_ms"],
        "false_ready": calc["false_ready"],
        "edge": edge,
        "selection_id": selection_id,
    }


EMPTY_SUMMARY = {
    "select_disc": {"status": "insufficient", "reason_codes": ["sample_count", "content_diversity"],
                    "valid_count": 0, "different_transitions": 0, "floor_ms": 500},
    "open_preview": {"status": "insufficient", "reason_codes": ["sample_count"],
                     "valid_count": 0, "floor_ms": 900},
    "open_upgrade": {"status": "insufficient", "reason_codes": ["sample_count"],
                     "valid_count": 0, "floor_ms": 1500},
    "material_ready": {"status": "measured_only", "reason_codes": ["consumption_measured_only"],
                       "valid_count": 0, "floor_ms": 600},
    "upgrade_result": {"status": "measured_only", "reason_codes": ["consumption_measured_only"],
                       "valid_count": 0, "floor_ms": 1500},
    "return_warehouse": {"status": "insufficient", "reason_codes": ["edge_coverage"],
                         "valid_count": 0, "floor_ms": 600,
                         "edge_floors": {edge: None for edge in vs.RETURN_EDGES}},
}


@pytest.fixture
def gate(monkeypatch, tmp_path):
    spec = tmp_path / "Spec.md"
    spec.write_text("spec bytes", encoding="utf-8")
    monkeypatch.setattr(vs, "SPEC", spec)
    monkeypatch.setattr(vs, "ROOT", tmp_path)

    identity = {"content_sha256": "cid", "files": {"a.py": "h"}, "worktree_status": ""}
    monkeypatch.setattr(vs, "execution_identity", lambda: dict(identity))

    (tmp_path / "sel").mkdir()
    list_path = tmp_path / "sel" / "标定选盘.json"
    list_path.write_text("[]", encoding="utf-8")
    start_frame = tmp_path / "sel" / "start-frame.png"
    start_frame.write_bytes(b"\x89PNG\r\n\x1a\nstart")

    calib = tmp_path / "等待标定.json"
    final = tmp_path / "最终流程.json"
    monkeypatch.setattr(vs, "CALIB_REPORT", calib)
    monkeypatch.setattr(vs, "FINAL_REPORT", final)
    return SimpleNamespace(
        spec=spec,
        root=tmp_path,
        identity=identity,
        calib=calib,
        final=final,
        list_rel="sel/标定选盘.json",
        list_path=list_path,
        start_rel="sel/start-frame.png",
        start_path=start_frame,
    )


def selection_body(gate, *, valid=True):
    return {
        "valid": valid,
        "reason_codes": [] if valid else ["selection_conflict"],
        "grid_geometry": {"cells": []},
        "checks": [],
        "list_path": gate.list_rel,
        "list_sha256": vs.sha256_file(gate.list_path),
        "start_frame": {"path": gate.start_rel, "sha256": vs.sha256_file(gate.start_path)},
        "hwnd": 1,
        "pid": 1,
        "candidates": [],
    }


def calib_body(gate, **over):
    body = {
        "schema_version": 1,
        "spec_sha256": vs.sha256_file(gate.spec),
        "execution_identity": dict(gate.identity),
        "environment": {"os": "Windows"},
        "predicate_version": 1,
        "selection": selection_body(gate),
        "samples": [],
        "summary": json.loads(json.dumps(EMPTY_SUMMARY)),
        "report_sha256": None,
        "profile_status": "not_persistable",
        "profile_sha256": None,
        "passed": True,
    }
    body.update(over)
    body["report_sha256"] = vs.sha256_bytes(vs.canon({key: body[key] for key in vs.DIGEST_KEYS}))
    return body


def write_calib(gate, body):
    gate.calib.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


def checks_by_id(checks):
    return {item["id"]: item for item in checks}


# --------------------------------------------------------------------------
# §9.1 间接复算：下限公式与时刻口径
# --------------------------------------------------------------------------
def test_floor_from_max_matches_spec_formula():
    # ceil((max + max(20, 2*50)) / 10) * 10
    assert vs._floor_from_max(177.0) == 280
    assert vs._floor_from_max(0.0) == 100
    assert vs._floor_from_max(10.0) == 110
    assert vs._floor_from_max(21.0) == 130


def test_recompute_times_takes_first_pair_of_the_last_run():
    """回归：整段只算一对——段尾帧不是 t_stable（曾把 62 帧的段尾当测点）。"""
    calc = vs._recompute_times(probes(62), t0=100.0)

    assert calc["ok"] is True
    assert calc["t_bad_ms"] is None
    assert calc["t_ready_ms"] == 0.0
    assert calc["t_stable_ms"] == pytest.approx(50.0)
    assert calc["false_ready"] is False


def test_recompute_times_marks_last_bad_before_the_confirming_pair():
    calc = vs._recompute_times(
        probes(81, ready=[False] * 21 + [True] * 60), t0=100.0
    )

    assert calc["t_bad_ms"] == pytest.approx(1000.0)
    assert calc["t_ready_ms"] == pytest.approx(1050.0)
    assert calc["t_stable_ms"] == pytest.approx(1100.0)


def test_recompute_times_flags_ready_on_non_final_content():
    keys = [("A",)] * 2 + [("B",)] * 60
    calc = vs._recompute_times(
        [{"captured_at": 100.0 + i * 0.05, "ready": True, "state_key": list(key)}
         for i, key in enumerate(keys)],
        t0=100.0,
    )

    assert calc["false_ready"] is True
    assert calc["t_ready_ms"] == pytest.approx(100.0)


def test_recompute_times_needs_the_whole_observation_window():
    assert vs._recompute_times(probes(2), t0=100.0)["ok"] is False
    assert vs._recompute_times(probes(1), t0=100.0)["reason"] == "no_stable_pair"


def test_recompute_times_requires_the_window_to_cover_a_late_content_change():
    """稳定帧对之后内容卡住不再就绪 → 窗口永远追不上；只看 t_stable 的尾段/跨度会误判。

    这类序列是真实可产生的：循环会因为目标时刻一直被「最后一个变化帧」推后而硬超时。
    """
    items = with_stuck_tail(probes(6), count=55, start_at=100.3)  # 末帧 103.0
    calc = vs._recompute_times(items, t0=100.0)

    assert calc["t_stable_ms"] == pytest.approx(50.0)  # 稳定帧对仍是开头那一对
    assert calc["ok"] is False
    assert calc["false_ready"] is False
    # 旧判据（只看尾段≥500ms 与跨度≥2000ms）在这里会给出 True —— 这正是要挡住的分歧。
    assert (items[-1]["captured_at"] - items[1]["captured_at"]) * 1000.0 >= 500.0
    assert (items[-1]["captured_at"] - 100.0) * 1000.0 >= 2000.0


def test_recompute_times_passes_when_the_content_settles_again():
    """同一序列若尾部真的稳定下来（再来两帧 A）→ 窗口盖住新尾段，仍算有效。"""
    items = with_stuck_tail(probes(6), count=55, start_at=100.3)
    items += [
        {"captured_at": 103.05, "ready": True, "state_key": ["A"], "_key": ("A",)},
        {"captured_at": 103.55, "ready": True, "state_key": ["A"], "_key": ("A",)},
    ]
    calc = vs._recompute_times(items, t0=100.0)

    # 末对 (61, 62) 确认稳定，且窗口盖到 103.55 ≥ 103.55 + 0.5？不——还差 500ms 尾段。
    assert calc["t_stable_ms"] == pytest.approx(3550.0)
    assert calc["ok"] is False


def test_recompute_times_passes_once_the_tail_is_fully_observed():
    items = with_stuck_tail(probes(6), count=55, start_at=100.3)
    items += [
        {"captured_at": 103.05, "ready": True, "state_key": ["A"], "_key": ("A",)},
        {"captured_at": 103.55, "ready": True, "state_key": ["A"], "_key": ("A",)},
        {"captured_at": 104.10, "ready": True, "state_key": ["A"], "_key": ("A",)},
    ]
    calc = vs._recompute_times(items, t0=100.0)

    assert calc["ok"] is True
    assert calc["t_stable_ms"] == pytest.approx(3550.0)


# --------------------------------------------------------------------------
# 门禁与采样脚本同口径 A/B（两侧实现独立，结论必须一致）
# --------------------------------------------------------------------------
AB_SERIES = {
    "首帧就绪": lambda: probes(62),
    "先不就绪后就绪": lambda: probes(81, ready=[False] * 21 + [True] * 60),
    "尾部卡住不就绪": lambda: with_stuck_tail(probes(6), count=55, start_at=100.3),
    "一直不就绪": lambda: probes(20, ready=False),
    "窗口不足": lambda: probes(2),
}


@pytest.mark.parametrize("name", list(AB_SERIES))
def test_gate_and_sampler_agree_on_times_and_false_ready(name):
    items = AB_SERIES[name]()

    sampler = mw.recompute_observation(items, t0=100.0)
    gate = vs._recompute_times(items, t0=100.0)

    assert gate["t_stable_ms"] == sampler["t_stable_ms"]
    assert gate["t_ready_ms"] == sampler["t_ready_ms"]
    assert gate["t_bad_ms"] == sampler["t_bad_ms"]
    assert bool(gate["false_ready"]) == bool(sampler["false_ready"])


def test_sampler_outcome_needs_the_loop_stop_reason_not_only_tail_and_span():
    """只看「尾段/跨度够不够」会把硬超时掐断的样本误判成 ok；门禁必须自己重放停止判据。"""
    items = with_stuck_tail(probes(6), count=55, start_at=100.3)

    # 采样脚本凭 loop 的 timed_out 得到 timeout；不带该标志时同一条序列会算成 ok。
    assert mw.recompute_observation(items, t0=100.0, timed_out=True)["outcome"] == "timeout"
    assert mw.recompute_observation(items, t0=100.0)["outcome"] == "ok"
    # 门禁不依赖报告里的 timed_out 字段，独立推出同一个「无效」。
    assert vs._recompute_times(items, t0=100.0)["ok"] is False


def test_core_key_ignores_rarity_labels_and_identity():
    base = {"level": 15, "substats": [{"name": "暴击率", "rolls": 2},
                                      {"name": "攻击力", "rolls": 1}]}
    plain = dict(base, rarity="S", locked=False, equipped=False, discarded=False, uid="u1")
    reordered = dict(base, substats=list(reversed(base["substats"])))

    assert vs._core_key({"final_core": plain}) == vs._core_key({"final_core": reordered})
    assert vs._core_key({"final_core": dict(base, level=14)}) != vs._core_key({"final_core": plain})
    other = dict(base, substats=[{"name": "暴击率", "rolls": 3}, {"name": "攻击力", "rolls": 1}])
    assert vs._core_key({"final_core": other}) != vs._core_key({"final_core": plain})
    assert vs._core_key({}) is None


# --------------------------------------------------------------------------
# §9.1 报告内汇总的独立复算
# --------------------------------------------------------------------------
def test_report_summary_of_empty_samples_keeps_defaults_and_passive_floors():
    body = vs._recompute_report_summary({"samples": []})

    assert body["time_bad"] == []
    assert body["summary"] == EMPTY_SUMMARY


def test_report_summary_calibrates_open_preview_from_max_stable():
    body = vs._recompute_report_summary({
        "samples": [sample(f"open-{i}", "open_preview", probes(62)) for i in range(5)]
    })

    entry = body["summary"]["open_preview"]
    assert entry["status"] == "calibrated"
    assert entry["floor_ms"] == vs._floor_from_max(50.0) == 150
    assert entry["valid_count"] == 5


def test_report_summary_fails_a_kind_on_false_ready():
    keys = [("A",)] * 2 + [("B",)] * 60
    bad = sample("open-0", "open_preview", [
        {"captured_at": 100.0 + i * 0.05, "ready": True, "state_key": list(key)}
        for i, key in enumerate(keys)
    ])
    body = vs._recompute_report_summary({"samples": [bad]})

    entry = body["summary"]["open_preview"]
    assert entry["status"] == "failed"
    assert entry["reason_codes"] == ["false_ready"]
    assert entry["floor_ms"] == 900


def test_report_summary_select_needs_count_and_diversity():
    rows = [
        sample(f"sel-{i:02d}", "select_disc", probes(62), selection_id=f"c{i % 10}")
        for i in range(20)
    ]
    for index, row in enumerate(rows):
        row["final_core"] = {"level": 15, "substats": [{"name": "攻击力", "rolls": 1}]}
        if index % 2 == 0:
            row["final_core"]["level"] = index // 2
    body = vs._recompute_report_summary({"samples": rows})

    entry = body["summary"]["select_disc"]
    assert entry["status"] == "calibrated"
    assert entry["valid_count"] == 20
    assert entry["different_transitions"] == 19

    short = vs._recompute_report_summary({"samples": rows[:19]})["summary"]["select_disc"]
    assert short["status"] == "insufficient"
    assert short["reason_codes"] == ["sample_count"]

    flat = json.loads(json.dumps(rows))
    for row in flat:
        row["final_core"] = {"level": 15, "substats": [{"name": "攻击力", "rolls": 1}]}
    same = vs._recompute_report_summary({"samples": flat})["summary"]["select_disc"]
    assert same["status"] == "insufficient"
    assert same["reason_codes"] == ["content_diversity"]


def test_gate_accepts_the_sample_core_the_sampler_writes():
    """跨模块形状守卫：采样端写进 ``final_core`` 的 core 字典，门禁须取到同一个 key。"""
    evidence = {"core": {"level": 15, "rarity": "S",
                         "substats": [{"name": "攻击力", "rolls": 1}]}}
    core = mw.core_of_evidence(evidence)

    assert isinstance(core, dict)
    assert vs._core_key({"final_core": core}) == (15, (("攻击力", 1),))
    assert mw.core_key_of_core(core) == vs._core_key({"final_core": core})


def test_report_summary_return_needs_every_edge_and_takes_the_max():
    rows = []
    for index in range(15):
        rows.append(sample(f"return-{index:02d}", "return_warehouse", probes(62),
                           edge=vs.RETURN_EDGES[index // 5]))
    body = vs._recompute_report_summary({"samples": rows})

    entry = body["summary"]["return_warehouse"]
    assert entry["status"] == "calibrated"
    assert set(entry["edge_floors"]) == set(vs.RETURN_EDGES)
    assert entry["floor_ms"] == max(entry["edge_floors"].values())

    partial = vs._recompute_report_summary({"samples": rows[:10]})["summary"]["return_warehouse"]
    assert partial["status"] == "insufficient"
    assert partial["reason_codes"] == ["edge_coverage"]
    assert partial["floor_ms"] == 600


def test_report_summary_return_fails_on_same_layer_timeout():
    """同层/未知边（edge=None）超时：整类 failed，不是 edge_coverage。"""
    rows = [sample(f"return-{i}", "return_warehouse", probes(62),
                   edge=vs.RETURN_EDGES[0]) for i in range(5)]
    stuck = sample("return-stuck", "return_warehouse", probes(20, ready=False), edge=None)
    body = vs._recompute_report_summary({"samples": rows + [stuck]})

    entry = body["summary"]["return_warehouse"]
    assert entry["status"] == "failed"
    assert entry["reason_codes"] == ["timeout"]
    assert entry["floor_ms"] == 600


def test_report_summary_fails_a_kind_whose_floor_reaches_the_timeout():
    """下限顶到该 kind 的固定超时（open_preview 5000）→ failed/timeout 并保默认。"""
    slow = [sample(f"open-{i}", "open_preview", probes(1100, step=5.0)) for i in range(5)]
    body = vs._recompute_report_summary({"samples": slow})

    entry = body["summary"]["open_preview"]
    assert slow[0]["t_stable_ms"] == 5000.0
    assert entry["status"] == "failed"
    assert entry["reason_codes"] == ["timeout"]
    assert entry["floor_ms"] == 900


def test_report_summary_select_does_not_bridge_across_invalid_samples():
    """无效样本要断开相邻转换（与 readiness.summarize_select 同口径：跨则会是 1 次）。"""
    def row(index, selection_id, level, *, valid=True):
        body = sample(f"sel-{index:02d}", "select_disc", probes(62), selection_id=selection_id)
        body["final_core"] = {"level": level, "substats": [{"name": "攻击力", "rolls": 1}]}
        if not valid:
            body["probes"] = probes(2)  # 观察窗口不足 → 该样本无效
        return body

    rows = [row(0, "c0", 0), row(1, "c1", 1, valid=False), row(2, "c2", 2)]
    entry = vs._recompute_report_summary({"samples": rows})["summary"]["select_disc"]
    assert entry["valid_count"] == 2
    assert entry["different_transitions"] == 0


def test_report_summary_flags_tampered_times_and_unknown_kind():
    body = vs._recompute_report_summary({
        "samples": [dict(sample("open-0", "open_preview", probes(62)), t_stable_ms=999.0)]
    })
    assert any("t_stable_ms" in item for item in body["time_bad"])

    unknown = vs._recompute_report_summary({"samples": [{"sample_id": "x", "kind": "?"}]})
    assert any("未知 kind" in item for item in unknown["time_bad"])


# --------------------------------------------------------------------------
# check_calibration（K1～K9）
# --------------------------------------------------------------------------
def test_check_calibration_passes_on_consistent_report(gate):
    write_calib(gate, calib_body(gate))

    checks, _evidence, passed = vs.check_calibration()
    assert passed is True
    assert [item["id"] for item in checks] == ["K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9"]


def test_check_calibration_fails_k1_when_report_is_missing(gate):
    checks, _evidence, passed = vs.check_calibration()

    assert passed is False
    assert len(checks) == 1 and checks[0]["id"] == "K1" and checks[0]["passed"] is False


def test_check_calibration_fails_k1_when_a_top_level_field_is_missing(gate):
    body = calib_body(gate)
    del body["predicate_version"]
    write_calib(gate, body)

    checks, _evidence, passed = vs.check_calibration()
    assert passed is False
    assert checks[0]["passed"] is False
    assert "predicate_version" in checks[0]["detail"]["missing"]


def test_check_calibration_fails_k2_on_spec_digest_mismatch(gate):
    body = calib_body(gate)
    body["spec_sha256"] = "0" * 64
    body["report_sha256"] = vs.sha256_bytes(vs.canon({k: body[k] for k in vs.DIGEST_KEYS}))
    write_calib(gate, body)

    by_id = checks_by_id(vs.check_calibration()[0])
    assert by_id["K2"]["passed"] is False
    assert by_id["K3"]["passed"] is True


def test_check_calibration_fails_k3_when_digest_tampered(gate):
    body = calib_body(gate)
    body["report_sha256"] = "f" * 64
    write_calib(gate, body)

    by_id = checks_by_id(vs.check_calibration()[0])
    assert by_id["K3"]["passed"] is False


def test_check_calibration_digest_ignores_self_reference_and_landing_fields(gate):
    """摘要取数对象固定：改 profile_* / report_sha256 自身不影响 K3 复算。"""
    body = calib_body(gate, profile_status="ok", profile_sha256="deadbeef")
    write_calib(gate, body)

    assert checks_by_id(vs.check_calibration()[0])["K3"]["passed"] is True


def test_check_calibration_fails_k4_on_content_identity_mismatch(gate, monkeypatch):
    write_calib(gate, calib_body(gate))
    monkeypatch.setattr(
        vs, "execution_identity",
        lambda: {"content_sha256": "other", "files": {"a.py": "h"}, "worktree_status": ""},
    )

    by_id = checks_by_id(vs.check_calibration()[0])
    assert by_id["K4"]["passed"] is False


def test_check_calibration_fails_k4_when_identity_has_no_files(gate):
    body = calib_body(gate)
    body["execution_identity"] = {"content_sha256": "cid", "files": {}}
    write_calib(gate, body)

    by_id = checks_by_id(vs.check_calibration()[0])
    assert by_id["K4"]["passed"] is False


def test_check_calibration_fails_k5_when_list_bytes_changed(gate):
    body = calib_body(gate)
    write_calib(gate, body)
    gate.list_path.write_text("[1]", encoding="utf-8")

    detail = checks_by_id(vs.check_calibration()[0])["K5"]
    assert detail["passed"] is False
    assert detail["detail"]["list"]["current"] != body["selection"]["list_sha256"]


def test_check_calibration_fails_k5_when_start_frame_changed(gate):
    write_calib(gate, calib_body(gate))
    gate.start_path.write_bytes(b"\x89PNG\r\n\x1a\nother")

    by_id = checks_by_id(vs.check_calibration()[0])
    assert by_id["K5"]["passed"] is False


def test_check_calibration_fails_k6_on_sample_time_tamper(gate):
    body = calib_body(gate)
    body["samples"] = [dict(sample("open-0", "open_preview", probes(62)), t_ready_ms=1234.0)]
    write_calib(gate, body)

    by_id = checks_by_id(vs.check_calibration()[0])
    assert by_id["K6"]["passed"] is False
    assert any("t_ready_ms" in item for item in by_id["K6"]["detail"]["mismatches"])


def test_check_calibration_fails_k6_on_false_ready_tamper(gate):
    keys = [("A",)] * 2 + [("B",)] * 60
    bad = sample("open-0", "open_preview", [
        {"captured_at": 100.0 + i * 0.05, "ready": True, "state_key": list(key)}
        for i, key in enumerate(keys)
    ])
    bad["false_ready"] = False
    body = calib_body(gate)
    body["samples"] = [bad]
    write_calib(gate, body)

    by_id = checks_by_id(vs.check_calibration()[0])
    assert by_id["K6"]["passed"] is False


def test_check_calibration_fails_k7_on_summary_floor_tamper(gate):
    body = calib_body(gate)
    body["summary"]["open_preview"]["floor_ms"] = 42
    write_calib(gate, body)

    by_id = checks_by_id(vs.check_calibration()[0])
    assert by_id["K7"]["passed"] is False
    assert any("open_preview" in item for item in by_id["K7"]["detail"]["mismatches"])


def test_check_calibration_fails_k7_when_a_kind_is_missing_from_summary(gate):
    body = calib_body(gate)
    del body["summary"]["upgrade_result"]
    write_calib(gate, body)

    by_id = checks_by_id(vs.check_calibration()[0])
    assert by_id["K7"]["passed"] is False
    assert any("upgrade_result" in item for item in by_id["K7"]["detail"]["mismatches"])


@pytest.mark.parametrize("status", ["write_failed", None, "bogus"])
def test_check_calibration_rejects_unusable_profile_status(gate, status):
    body = calib_body(gate, profile_status=status, profile_sha256=None, passed=False)
    write_calib(gate, body)

    by_id = checks_by_id(vs.check_calibration()[0])
    assert by_id["K8"]["passed"] is False


def test_check_calibration_requires_ok_profile_to_match_disk(gate):
    body = calib_body(gate, profile_status="ok", profile_sha256="deadbeef", passed=True)
    write_calib(gate, body)

    by_id = checks_by_id(vs.check_calibration()[0])
    assert by_id["K8"]["passed"] is False


def test_check_calibration_accepts_not_persistable_only_without_digest(gate):
    body = calib_body(gate, profile_sha256="deadbeef")
    write_calib(gate, body)

    assert checks_by_id(vs.check_calibration()[0])["K8"]["passed"] is False


def test_check_calibration_k9_ties_passed_to_selection_validity(gate):
    write_calib(gate, calib_body(gate, selection=selection_body(gate, valid=False), passed=True))
    assert checks_by_id(vs.check_calibration()[0])["K9"]["passed"] is False

    write_calib(gate, calib_body(gate, passed=False))
    assert checks_by_id(vs.check_calibration()[0])["K9"]["passed"] is False


def test_check_calibration_passes_with_calibrated_open_preview(gate):
    summary = json.loads(json.dumps(EMPTY_SUMMARY))
    summary["open_preview"] = {"status": "calibrated", "reason_codes": [],
                               "valid_count": 5, "floor_ms": 150}
    body = calib_body(gate, samples=[
        sample(f"open-{i}", "open_preview", probes(62)) for i in range(5)
    ], summary=summary)
    write_calib(gate, body)

    checks, evidence, passed = vs.check_calibration()
    assert passed is True
    assert checks_by_id(checks)["K7"]["passed"] is True
    assert evidence["recomputed_summary"]["open_preview"]["floor_ms"] == 150


def test_check_calibration_k8_profile_round_trip_and_rejects_extra_kinds(gate, monkeypatch):
    """§6.4：档案只存已完成标定的 kind，且写出结果必须能被加载端接受（修 R6 回归）。

    正向：混合状态 summary 只把 open_preview 写进档案，加载端接受、下限生效 → K8 通过；
    反向：档案里混入未标定的 open_upgrade（旧写法的全六 kind）→ K8 不通过。
    """
    import zzz_ddtool.readiness as readiness

    env = {"machine_id": "m", "client_width": 1920, "client_height": 1080,
           "window_dpi": 96, "game_exe_path": r"C:\game\zzz.exe", "game_build": "build-1"}
    summary = json.loads(json.dumps(EMPTY_SUMMARY))
    summary["open_preview"] = {"status": "calibrated", "reason_codes": [],
                               "valid_count": 5, "floor_ms": 150}
    body = calib_body(gate, profile_status="ok", environment=env, summary=summary, samples=[
        sample(f"open-{i}", "open_preview", probes(62)) for i in range(5)
    ])
    profile = gate.root / "user" / "scan-waits.json"
    result = readiness.write_wait_profile(
        env,
        {"open_preview": readiness.WaitPolicy(150, 5000, 50, 2)},
        {"open_preview": {"count": 5, "max_stable_ms": 100.0, "floor_ms": 150,
                          "report_sha256": body["report_sha256"]}},
        path=profile,
    )
    assert result.status == "ok"
    body["profile_sha256"] = result.profile_sha256
    write_calib(gate, body)
    monkeypatch.setattr(readiness, "wait_profile_path", lambda: profile)

    checks, _evidence, passed = vs.check_calibration()
    assert checks_by_id(checks)["K8"]["passed"] is True
    assert passed is True

    doc = json.loads(profile.read_text(encoding="utf-8"))
    for section in ("policies", "measurements"):
        assert set(doc[section]) == {"open_preview"}

    doc["policies"]["open_upgrade"] = {"min_wait_ms": 1500, "timeout_ms": 8000,
                                       "poll_ms": 50, "stable_frames": 2}
    doc["measurements"]["open_upgrade"] = {"count": 0, "max_stable_ms": None, "floor_ms": 1500,
                                           "report_sha256": body["report_sha256"]}
    profile.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    body["profile_sha256"] = vs.sha256_file(profile)
    write_calib(gate, body)

    assert checks_by_id(vs.check_calibration()[0])["K8"]["passed"] is False


# --------------------------------------------------------------------------
# check_final（F1～F6）
# --------------------------------------------------------------------------
FINAL_RUN_ID = "gate-run-" + "0" * 32


def _perf_event(event_id, component, *, job_index=None, disc_index=None,
                duration_ms=1.0, details=None, run_id=FINAL_RUN_ID):
    return {
        "schema_version": 1,
        "run_id": run_id,
        "event_id": event_id,
        "parent_id": None,
        "job_index": job_index,
        "disc_index": disc_index,
        "stage_level": None,
        "component": component,
        "duration_ms": duration_ms,
        "details": details or {},
    }


def final_perf_events():
    return minimal_events(FINAL_RUN_ID)


def write_perf(gate, events):
    gate.perf_path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )


@pytest.fixture
def final_gate(gate):
    config = gate.root / "plan.yaml"
    config.write_text(json.dumps(PLAN), encoding="utf-8")
    perf = gate.root / "perf.jsonl"
    gate.config_rel = "plan.yaml"
    gate.config_path = config
    gate.perf_rel = "perf.jsonl"
    gate.perf_path = perf
    write_perf(gate, final_perf_events())
    return gate


def _coverage_entry(status="not_observed", count=0, refs=None):
    return {"status": status, "count": count,
            "evidence_refs": [] if refs is None else refs}


def final_coverage():
    return {key: _coverage_entry() for key in
            ("upgrade", "label_save", "label_discard",
             "label_unlock_before_discard", "maxed_refund", "material_shortfall")}


def final_body(gate, *, events=None, **over):
    events = final_perf_events() if events is None else events
    root = events[-1]
    config_sha = vs.sha256_file(gate.config_path)
    body = {
        "schema_version": 1, "spec_sha256": vs.sha256_file(gate.spec),
        "execution_identity": dict(gate.identity), "run_id": FINAL_RUN_ID,
        "environment": {"machine_id": "m" * 8, "client_width": 1920, "client_height": 1080,
                        "window_dpi": 96, "game_exe_path": "c:/game/z.exe", "game_build": "1"},
        "config_path": gate.config_rel, "config_sha256_before": config_sha,
        "config_sha256_after": config_sha,
        "wait_profile_status": {"status": "default", "profile_path": "user/scan-waits.json", "profile_sha256": None},
        "effective_policies": {"select_disc": {"min_wait_ms": 500, "timeout_ms": 3000, "poll_ms": 50, "stable_frames": 2}},
        "jobs": deepcopy(root["details"]["jobs"]), "discs": mw._final_discs(events),
        "timing": mw._final_timing(events, {}, root),
        "coverage": mw._final_coverage(events, FINAL_RUN_ID, [], None, root["event_id"]),
        "perf_file": gate.perf_rel, "component_counts": dict(Counter(e["component"] for e in events)),
        "processed_discs": mw._final_processed(events), "runner_outcome": "completed",
        "termination_kind": "completed", "end_reason": "已处理 1 个盘", "run_total": deepcopy(root["details"]),
        "coverage_status": "sufficient", "evidence_complete": True, "passed": True,
    }
    body.update(over)
    return body


def write_final(gate, body):
    gate.final.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


def test_check_final_passes_on_consistent_report(final_gate):
    write_final(final_gate, final_body(final_gate))

    checks, _evidence, passed = vs.check_final()
    assert passed is True
    assert [item["id"] for item in checks] == ["F1", "F2", "F3", "F4", "F5", "F6"]


def test_check_final_fails_f1_when_report_is_missing(final_gate):
    checks, _evidence, passed = vs.check_final()

    assert passed is False
    assert len(checks) == 1 and checks[0]["id"] == "F1" and checks[0]["passed"] is False


def test_check_final_fails_f1_when_a_top_level_field_is_missing(final_gate):
    body = final_body(final_gate)
    del body["coverage_status"]
    write_final(final_gate, body)

    checks, _evidence, passed = vs.check_final()
    assert passed is False
    assert "coverage_status" in checks[0]["detail"]["missing"]


def test_check_final_fails_f1_when_contract_keys_are_missing(final_gate):
    """§9.2 约定键（run_id/discs/timing/coverage 等）缺失时 F1 必须拦下。"""
    body = final_body(final_gate)
    for key in ("run_id", "environment", "config_sha256_before", "config_sha256_after",
                "wait_profile_status", "effective_policies", "discs", "timing", "coverage"):
        del body[key]
    write_final(final_gate, body)

    checks, _evidence, passed = vs.check_final()
    assert passed is False
    missing = checks[0]["detail"]["missing"]
    for key in ("run_id", "discs", "timing", "coverage", "config_sha256_after"):
        assert key in missing


def test_check_final_fails_f2_on_spec_digest_mismatch(final_gate):
    write_final(final_gate, final_body(final_gate, spec_sha256="0" * 64))

    assert checks_by_id(vs.check_final()[0])["F2"]["passed"] is False


def test_check_final_fails_f3_when_plan_bytes_changed(final_gate):
    write_final(final_gate, final_body(final_gate))
    final_gate.config_path.write_text("max_pages: 4\n", encoding="utf-8")

    assert checks_by_id(vs.check_final()[0])["F3"]["passed"] is False


def test_check_final_fails_f3_when_before_after_disagree(final_gate):
    body = final_body(final_gate, config_sha256_after="0" * 64)
    write_final(final_gate, body)

    assert checks_by_id(vs.check_final()[0])["F3"]["passed"] is False


def test_check_final_fails_f4_on_content_identity_mismatch(final_gate, monkeypatch):
    write_final(final_gate, final_body(final_gate))
    monkeypatch.setattr(
        vs, "execution_identity",
        lambda: {"content_sha256": "other", "files": {"a.py": "h"}, "worktree_status": ""},
    )

    assert checks_by_id(vs.check_final()[0])["F4"]["passed"] is False


def test_check_final_fails_f5_when_perf_file_is_incomplete(final_gate):
    raw = final_gate.perf_path.read_text(encoding="utf-8")
    final_gate.perf_path.write_text(raw.rstrip("\n"), encoding="utf-8")
    write_final(final_gate, final_body(final_gate))

    checks, _evidence, passed = vs.check_final()
    by_id = checks_by_id(checks)
    assert by_id["F5"]["passed"] is False
    assert by_id["F5"]["detail"]["complete"] is False
    assert passed is False


def test_check_final_fails_f5_when_perf_file_is_missing(final_gate):
    final_gate.perf_path.unlink()
    write_final(final_gate, final_body(final_gate))

    by_id = checks_by_id(vs.check_final()[0])
    assert by_id["F5"]["passed"] is False
    assert by_id["F5"]["detail"]["error"]


def test_check_final_fails_f5_when_perf_lacks_required_components(final_gate):
    """负向对照：只剩一条真实 capture 事件（无 run_total/disc_total/probe/wait_total）必须拦下。"""
    write_perf(final_gate, [
        _perf_event("e00000001", "capture", details={"method": "PrintWindow", "hwnd": 1}),
    ])
    write_final(final_gate, final_body(final_gate))

    checks, _evidence, passed = vs.check_final()
    by_id = checks_by_id(checks)
    assert by_id["F5"]["passed"] is False
    assert by_id["F5"]["detail"]["components"] == {"capture": 1}
    assert passed is False


def test_check_final_fails_f5_on_run_id_mismatch(final_gate):
    write_final(final_gate, final_body(final_gate, run_id="other-run"))

    by_id = checks_by_id(vs.check_final()[0])
    assert by_id["F5"]["passed"] is False
    assert by_id["F5"]["detail"]["run_id_ok"] is False


def test_check_final_fails_f6_when_coverage_disagrees_with_outcome(final_gate):
    write_final(final_gate, final_body(final_gate, runner_outcome="stopped"))

    by_id = checks_by_id(vs.check_final()[0])
    assert by_id["F6"]["passed"] is False


def test_check_final_fails_f6_when_processed_is_below_one(final_gate):
    write_final(final_gate, final_body(final_gate, processed_discs=0))

    assert checks_by_id(vs.check_final()[0])["F6"]["passed"] is False


def test_check_final_fails_f6_when_processed_discs_is_tampered(final_gate):
    """报告自报计数与原始事件按 (job_index, disc_index) 复算不一致时必须拦下。"""
    write_final(final_gate, final_body(final_gate, processed_discs=5))

    by_id = checks_by_id(vs.check_final()[0])
    assert by_id["F6"]["passed"] is False
    assert by_id["F6"]["detail"]["recomputed_processed"] == 1


def test_check_final_counts_discs_by_job_identity_across_jobs(final_gate):
    """跨任务全局索引可重复：不同任务的同一 disc_index 是两盘，不能按裸索引合并。"""
    events = two_job_events(FINAL_RUN_ID)
    plan = deepcopy(PLAN)
    plan["task_chain"].append(dict(plan["task_chain"][0], id="j2"))
    final_gate.config_path.write_text(json.dumps(plan), encoding="utf-8")
    write_perf(final_gate, events)
    body = final_body(final_gate, events=events, processed_discs=2)
    write_final(final_gate, body)

    by_id = checks_by_id(vs.check_final()[0])
    assert by_id["F6"]["passed"] is True
    assert by_id["F6"]["detail"]["recomputed_processed"] == 2

    # 若报告把两盘按裸全局索引错并成 1 盘，必须拦下。
    write_final(final_gate, final_body(final_gate, processed_discs=1))
    by_id = checks_by_id(vs.check_final()[0])
    assert by_id["F6"]["passed"] is False


def test_check_final_fails_f6_when_measured_coverage_has_no_refs(final_gate):
    """已触发分支却无事件引用：如实报 measured 也判证据不完整，不能放过。"""
    coverage = final_coverage()
    coverage["upgrade"] = _coverage_entry(status="measured", count=1, refs=[])
    write_final(final_gate, final_body(final_gate, coverage=coverage))

    by_id = checks_by_id(vs.check_final()[0])
    assert by_id["F6"]["passed"] is False
    assert by_id["F6"]["detail"]["refs_complete"] is False


def test_check_final_fails_f6_when_coverage_ref_event_is_unresolvable(final_gate):
    coverage = final_coverage()
    coverage["upgrade"] = _coverage_entry(status="measured", count=1, refs=[{
        "run_id": FINAL_RUN_ID, "job_index": 0, "disc_index": 3,
        "input_ids": ["input-1"], "event_ids": ["e99999999"],
        "log_refs": [], "exception": None,
    }])
    write_final(final_gate, final_body(final_gate, coverage=coverage))

    by_id = checks_by_id(vs.check_final()[0])
    assert by_id["F6"]["passed"] is False
    assert by_id["F6"]["detail"]["refs_complete"] is False


def test_check_final_fails_f6_when_passed_lies_about_coverage(final_gate):
    write_final(final_gate, final_body(final_gate, coverage_status="insufficient", passed=True))

    assert checks_by_id(vs.check_final()[0])["F6"]["passed"] is False


def test_check_final_fails_f6_for_honest_failed_run(final_gate):
    """素材不足/F10/失败的真实报告：如实记录也必须让 final 门禁不通过（§9.2）。"""
    events = final_perf_events()
    events[-1]["details"] = {"runner_outcome": "failed",
                             "termination_kind": "material_shortfall",
                             "end_reason": "升级素材不足"}
    write_perf(final_gate, events)
    write_final(final_gate, final_body(
        final_gate,
        runner_outcome="failed", termination_kind="material_shortfall",
        coverage_status="insufficient", evidence_complete=True, passed=False,
    ))

    checks, _evidence, passed = vs.check_final()
    by_id = checks_by_id(checks)
    assert by_id["F6"]["passed"] is False
    assert passed is False


def test_check_final_requires_evidence_complete_for_sufficient_coverage(final_gate):
    write_final(final_gate, final_body(final_gate, evidence_complete=False))

    assert checks_by_id(vs.check_final()[0])["F6"]["passed"] is False


@pytest.mark.parametrize("field", ["jobs", "discs", "timing"])
def test_final_rejects_deleted_execution_evidence(final_gate, field):
    body = final_body(final_gate)
    body[field] = {} if field == "timing" else []
    write_final(final_gate, body)
    assert not vs.check_final()[2]


@pytest.mark.parametrize("change", ["trigger", "decision", "input", "interval", "quota"])
def test_final_rejects_broken_actual_links(final_gate, change):
    body = final_body(final_gate)
    if change == "trigger":
        body["timing"]["waits"][0]["trigger_input_id"] = "invented"
    elif change == "decision":
        body["discs"][0]["decisions"][0]["decision"]["useful_types"] = 99
    elif change == "input":
        body["discs"][0]["inputs"] = []
    elif change == "interval":
        body["discs"][0]["ended_at"] += 1
    else:
        body["jobs"][0]["quota_effective"] = 1
    write_final(final_gate, body)
    assert not vs.check_final()[2]


def test_final_rejects_phantom_input_even_when_report_and_events_agree(final_gate):
    events = final_perf_events()
    events[-1]["details"]["inputs"] = []
    events[0]["details"]["inputs"] = []
    write_perf(final_gate, events)
    write_final(final_gate, final_body(final_gate, events=events))
    checks, _, passed = vs.check_final()
    assert not passed
    assert not checks_by_id(checks)["F6"]["passed"]


def test_final_checks_raw_yaml_quota_before_loader_clamp(final_gate):
    plan = deepcopy(PLAN)
    plan["task_chain"][0]["max_discs"] = 0
    final_gate.config_path.write_text(json.dumps(plan), encoding="utf-8")
    write_final(final_gate, final_body(final_gate))
    assert not checks_by_id(vs.check_final()[0])["F3"]["passed"]


@pytest.mark.parametrize("change", ["api", "target", "wait_kind"])
def test_final_rejects_wrong_actual_operation_even_when_projection_matches(final_gate, change):
    events = final_perf_events()
    item = events[-1]["details"]["inputs"][0]
    if change == "api":
        item["api"] = "press_key_once"
    elif change == "target":
        item["target"]["x"] += 1
    else:
        next(e for e in events if e["component"] == "wait_total")["details"]["kind"] = "open_preview"
    write_perf(final_gate, events)
    write_final(final_gate, final_body(final_gate, events=events))
    assert not checks_by_id(vs.check_final()[0])["F6"]["passed"]
