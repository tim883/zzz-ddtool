"""§8 C3 的 §9.1.1 离线构造用例：选盘清单 + 开始帧几何、下限复算与报告摘要。

脚本 ``measure_waits.py`` 的纯离线层（evaluate_selection / floor_recompute_ms /
finalize_report / execution_identity）由本文件直接断言；真机采样层不在此驱动。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

import zzz_ddtool.ocr as ocr_module

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "dev-docs" / "功能改造" / "扫描提速" / "scripts" / "measure_waits.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("measure_waits_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析字符串注解时要能从 sys.modules 找回本模块，必须先注册再 exec。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mw = _load_module()

#: 1920×1080 下 GRID_ORIGIN=(166,267)、GRID_STEP=(135,175)、GRID_LABEL_DY=87。
COL0_X = 166
ROW0_LABEL_Y = 267 + 87
ROW1_LABEL_Y = 267 + 175 + 87


def label(text, left, top, right, bottom):
    return ocr_module.OcrLine(text, 0.99, left, top, right, bottom)


def row_label(col: int, icon_y: int, level: int = 0):
    x = COL0_X + 135 * col
    y = icon_y + 87
    return label(f"等级{level}", x - 40, y - 20, x + 40, y + 20)


def selection(cells, sequence, *, client=(1920, 1080), confirmed="2026-10-10T12:00:00+08:00",
              method="human_visual", schema=1):
    return {
        "schema_version": schema,
        "client_size": list(client),
        "confirmation_method": method,
        "confirmed_at": confirmed,
        "cells": cells,
        "select_sequence": sequence,
    }


def one_cell(sid="a", row=1, col=1, center=(COL0_X, 267)):
    return {
        "selection_id": sid,
        "visible_row": row,
        "visible_col": col,
        "center": list(center),
        "non_equipped": True,
        "non_locked": True,
    }


def evaluate(selection_body, lines):
    return mw.evaluate_selection(
        selection_body, frame=mw._blank_frame(), label_lines=lines, client_size=(1920, 1080)
    )


# --------------------------------------------------------------------------
# 下限复算
# --------------------------------------------------------------------------
def test_c3_floor_recompute_is_280():
    assert mw.floor_recompute_ms([177.0]) == 280


def test_c3_floor_recompute_rejects_empty_samples():
    with pytest.raises(ValueError):
        mw.floor_recompute_ms([])


# --------------------------------------------------------------------------
# 几何来源与退化
# --------------------------------------------------------------------------
def test_c3_valid_adjacent_rows_are_measured():
    lines = [row_label(0, 267), row_label(0, 442)]
    result = evaluate(selection([one_cell()], ["a"]), lines)

    assert result.valid is True
    assert result.grid_geometry["row_pitch_source"] == "measured"
    assert result.grid_geometry["col_pitch_source"] == "layout_default"
    assert result.grid_geometry["origin_center_y"] == pytest.approx(267.0)
    assert result.grid_geometry["row_pitch_px"] == pytest.approx(175.0)
    assert result.grid_geometry["cells"][0]["center"] == [COL0_X, 267]


def test_c3_single_row_degrades_to_layout_default():
    lines = [row_label(0, 267)]
    result = evaluate(selection([one_cell()], ["a"]), lines)

    assert result.valid is True
    assert result.grid_geometry["row_pitch_source"] == "layout_default"
    assert result.grid_geometry["row_pitch_px"] == pytest.approx(175.0)


def test_c3_double_pitch_rows_are_rejected():
    lines = [row_label(0, 267), row_label(0, 267 + 350)]
    result = evaluate(selection([one_cell()], ["a"]), lines)

    assert result.valid is False
    assert "selection_conflict" in result.reason_codes


def test_c3_clipped_top_row_cell_is_rejected():
    """首可读行部分裁切仍占行号 1，但不完整格位不能入选。"""
    lines = [row_label(0, 63), row_label(0, 63 + 175)]
    result = evaluate(selection([one_cell(center=(COL0_X, 63))], ["a"]), lines)

    assert result.valid is False
    assert "selection_conflict" in result.reason_codes


def test_c3_center_mismatch_is_rejected():
    lines = [row_label(0, 267), row_label(0, 442)]
    result = evaluate(selection([one_cell(center=(COL0_X + 34, 267))], ["a"]), lines)

    assert result.valid is False


# --------------------------------------------------------------------------
# 零标签 → selection_conflict，取样输入 0、passed False
# --------------------------------------------------------------------------
def test_c3_zero_labels_is_selection_conflict_with_zero_inputs():
    result = evaluate(selection([one_cell()], ["a"]), [])

    assert result.valid is False
    assert result.reason_codes == ("selection_conflict",)
    report = mw.selection_conflict_report(
        environment=None, selection_evaluation=result, identity={"head_sha": "x"}
    )
    assert report["passed"] is False
    assert report["sampling_inputs"] == 0
    assert report["summary"]["select_disc"]["status"] == "failed"
    assert "selection_conflict" in report["summary"]["select_disc"]["reason_codes"]
    assert report["samples"] == []


# --------------------------------------------------------------------------
# 清单字段校验
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [
        selection([one_cell()], ["a"], method="script"),
        selection([one_cell()], ["a"], confirmed="2026-10-10T12:00:00"),
        selection([one_cell()], ["a"], schema=2),
        selection([one_cell(), one_cell()], ["a"]),
        selection([one_cell()], ["b"]),
        selection([one_cell(center=(COL0_X, 267))], ["a"], client=(1600, 900)),
    ],
)
def test_c3_malformed_selection_is_rejected(body):
    lines = [row_label(0, 267), row_label(0, 442)]
    result = evaluate(body, lines)
    assert result.valid is False
    assert "selection_conflict" in result.reason_codes


def test_c3_confirmed_at_requires_timezone():
    assert mw.parse_confirmed_at("2026-10-10T12:00:00") is None
    assert mw.parse_confirmed_at("2026-10-10T12:00:00+08:00") is not None
    assert mw.parse_confirmed_at("2026-10-10T04:00:00Z") is not None


# --------------------------------------------------------------------------
# 报告顶层字段与摘要
# --------------------------------------------------------------------------
def test_c3_calibration_report_has_required_toplevel_fields(monkeypatch):
    result = evaluate(selection([one_cell()], ["a"]), [row_label(0, 267), row_label(0, 442)])
    report = mw.finalize_report(
        mw.selection_conflict_report(environment=None, selection_evaluation=result, identity={})
    )
    for key in ("schema_version", "spec_sha256", "execution_identity", "environment",
                "predicate_version", "selection", "samples", "summary", "report_sha256", "passed"):
        assert key in report
    assert report["report_sha256"] == mw.calibration_report_sha256(report)


def test_c3_digest_ignores_toplevel_metadata():
    result = evaluate(selection([one_cell()], ["a"]), [row_label(0, 267), row_label(0, 442)])
    base = mw.selection_conflict_report(
        environment=None, selection_evaluation=result, identity={}
    )
    tweaked = dict(base)
    tweaked["generated_at"] = "2026-10-10T13:00:00+08:00"
    tweaked["profile_sha256"] = None
    assert mw.calibration_report_sha256(base) == mw.calibration_report_sha256(tweaked)


def test_c3_digest_changes_with_selection_evidence():
    lines = [row_label(0, 267), row_label(0, 442)]
    lines_col2 = [row_label(0, 267), row_label(2, 267), row_label(0, 442)]
    first = evaluate(selection([one_cell()], ["a"]), lines)
    second = evaluate(
        selection([one_cell(col=3, center=(COL0_X + 135 * 2, 267))], ["a"]), lines_col2
    )
    assert first.valid and second.valid
    base = mw.selection_conflict_report(environment=None, selection_evaluation=first, identity={})
    changed = mw.selection_conflict_report(environment=None, selection_evaluation=second, identity={})
    base["selection"]["checks"] = first.checks
    changed["selection"]["checks"] = second.checks
    assert base["selection"]["grid_geometry"]["cells"] != changed["selection"]["grid_geometry"]["cells"]
    assert mw.calibration_report_sha256(base) != mw.calibration_report_sha256(changed)


# --------------------------------------------------------------------------
# execution_identity
# --------------------------------------------------------------------------
def test_c3_execution_identity_is_normalized_and_complete():
    identity = mw.execution_identity()

    paths = [item["path"] for item in identity["files"]]
    assert paths == sorted(paths)
    assert paths, "身份集合不能为空"
    assert any(p.startswith("src/zzz_ddtool/") and p.endswith(".py") for p in paths)
    assert any(p.startswith("tests/") for p in paths)
    assert any(p.endswith("measure_waits.py") for p in paths)
    assert not any("__pycache__" in p or p.endswith(".nssyncsc") for p in paths)

    from zzz_ddtool.readiness import canonical_json_bytes
    from hashlib import sha256

    recomputed = sha256(canonical_json_bytes(identity["files"])).hexdigest()
    assert identity["content_sha256"] == recomputed
    assert isinstance(identity["worktree_status"], str)
