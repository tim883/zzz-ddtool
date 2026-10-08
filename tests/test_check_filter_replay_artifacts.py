"""检查器破坏性验收（spec §8.4 的离线部分，对应 R27/R28/R30 的判定面）。

合成一份内部自洽的验收产物（result.json + inputs.jsonl + 证据 PNG + review.json），
先确认检查器**通过**，再逐一破坏某一条要求，确认检查器**非零**。
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
CHECKER = BASE / "tests" / "check_filter_replay_artifacts.py"

SETS = ("河豚电音", "静听嘉音", "折枝剑歌")
SETS_B_ONLY = ("河豚电音",)
DEFAULT_SETS_FOR_TEST = ("河豚电音", "静听嘉音")  # 用于构造"组合不一致"的破坏样本
SUBSTATS = ("攻击力%", "暴击伤害", "暴击率", "穿透值")
GAME_NAMES = {"攻击力%": "攻击力百分比"}
COUNTS = (3, 4)
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/wD+YSKkAAAAAElFTkSuQmCC"
)


def _box(index: int) -> tuple[int, int, int, int]:
    top = 400 + index * 60
    return (900, top, 990, top + 28)


def _center(box: tuple[int, int, int, int]) -> tuple[int, int]:
    return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)


class Fixture:
    """按 §8.4 契约合成一份合法产物；每个破坏用例只改其中一处。"""

    def __init__(self, root: Path, scenario: str) -> None:
        self.root = root
        self.scenario = scenario
        self.rows: list[dict] = []
        self.runs: list[dict] = []
        self.review: list[dict] = []
        self.seq = 0
        self.call_id = 0
        self.t_ns = 1_000_000_000
        self.ranges: dict[str, list[int]] = {}

    # ---- 低层记录 ----
    def _row(self, run_id: str, event: str, **fields):
        self.seq += 1
        self.t_ns += 3_000_000
        row = {
            "version": 1,
            "seq": self.seq,
            "t_ns": self.t_ns,
            "run_id": run_id,
            "round": fields.get("round"),
            "path": fields.get("path"),
            "task": fields.get("task"),
            "event": event,
            "call_id": fields.get("call_id"),
            "api": fields.get("api"),
            "role": fields.get("role"),
            "target": fields.get("target"),
            "stage_index": fields.get("stage_index"),
            "selected": fields.get("selected"),
            "hwnd": fields.get("hwnd", 199880),
            "x": fields.get("x"),
            "y": fields.get("y"),
            "alt": fields.get("alt"),
            "vk": fields.get("vk"),
            "delta_units": fields.get("delta_units"),
            "status": fields.get("status"),
            "error": fields.get("error"),
        }
        self.rows.append(row)
        if run_id:
            span = self.ranges.setdefault(run_id, [self.seq, self.seq])
            span[1] = self.seq
        return row

    def call(self, run_id: str, api: str, *, role: str, target=None, stage=None, selected=None,
             x=None, y=None, delta=None, vk=None, round=None, path=None, task=None) -> int:
        self.call_id += 1
        common = dict(call_id=self.call_id, api=api, role=role, target=target, stage_index=stage,
                      selected=selected, x=x, y=y, delta_units=delta, vk=vk,
                      alt=False if api == "click_client" else None,
                      round=round, path=path, task=task)
        self._row(run_id, "begin", status="started", error=None, **common)
        self._row(run_id, "end", status="completed", error=None, **common)
        return self.call_id

    def control(self, kind: str, run_id: str | None = None, round=None, path=None, task=None) -> None:
        self._row(run_id, "control", call_id=None, api=None, role=None, status=kind, error=None,
                  round=round, path=path, task=task)

    # ---- 单个 run ----
    def build_run(self, round_no: int, path: str, task: str, *, mode: str,
                  n_stages: int = 2, partial: bool = False) -> None:
        slot = 1 if task in ("A", "C") else 2
        sets = SETS_B_ONLY if (self.scenario == "different_sets" and task == "B") else SETS
        run_id = f"r{round_no:02d}-{path}-{task}"
        meta = dict(round=round_no, path=path, task=task)
        self.call(run_id, "click_client", role="open_panel", x=1800, y=1040, **meta)
        self.call(run_id, "click_client", role="reset", x=1445, y=1015, **meta)
        self.call(run_id, "click_client", role="slot", target=slot, x=1560, y=715, **meta)
        self.call(run_id, "click_client", role="open_sets", x=1450, y=315, **meta)
        groups = self._groups(sets, n_stages)
        for _ in range(len(groups) - 1):
            self.call(run_id, "wheel_client", role="set_scroll", x=960, y=540, delta=-3, **meta)
        evidence: list[dict] = []
        clicked = 0
        for index, group in enumerate(groups, start=1):
            boxes = []
            for offset, name in enumerate(group):
                box = _box(len(evidence) * 3 + offset)
                x, y = _center(box)
                self.call(run_id, "click_client", role="set_option", target=name, stage=index,
                          selected=True, x=x, y=y, **meta)
                boxes.append(list(box))
                clicked += 1
                if partial and clicked == 1:  # stop_after_click 的 B：只到第一个套装点击
                    self.control("stop_requested", run_id, **meta)
                    return
            image = f"{run_id}-sets-{index:02d}.png"
            (self.root / image).write_bytes(PNG_1X1)
            evidence.append({"event": "set_stage_selected", "stage_index": index,
                             "targets": list(group), "boxes": boxes, "image": image})
        self.call(run_id, "click_client", role="confirm_sets", x=1145, y=864, **meta)
        self.call(run_id, "click_client", role="open_substats", x=1560, y=565, **meta)
        substat_boxes: dict[str, list[int]] = {}
        for index, name in enumerate(SUBSTATS):
            box = _box(50 + index)
            x, y = _center(box)
            self.call(run_id, "click_client", role="substat_option", target=GAME_NAMES.get(name, name),
                      selected=True, x=x, y=y, **meta)
            substat_boxes[name] = list(box)
        targets = [*SUBSTATS, 1, 2, 3, 4]
        boxes = [substat_boxes[name] for name in SUBSTATS]
        for count in range(1, 5):
            box = _box(60 + count)
            x, y = _center(box)
            self.call(run_id, "click_client", role="substat_count", target=count,
                      selected=count in COUNTS, x=x, y=y, **meta)
            boxes.append(list(box))
        image = f"{run_id}-substats.png"
        (self.root / image).write_bytes(PNG_1X1)
        evidence.append({"event": "substats_selected", "stage_index": None,
                         "targets": targets, "boxes": boxes, "image": image})
        self.call(run_id, "press_key", role="close_panel", vk=27, x=None, y=None, **meta)
        warehouse = f"{run_id}-warehouse.png"
        (self.root / warehouse).write_bytes(PNG_1X1)
        snapshot = {
            "total": 12,
            "cells": [[0, 0, 0, 15], [1, 0, 1, 12]],
            "skips": [],
            "empties": [[0, 8], [0, 9]],
            "problems": [],
        }
        metrics = {
            "mode": mode,
            "success": True,
            "search_ocr_calls": 0 if mode == "replay" else 9,
            "verification_frames": 7,
            "wheel_events": len(groups) - 1,
            "selection_clicks": len(sets),
            "elapsed_ms": 900 if mode != "replay" else 400,
        }
        span = self.ranges[run_id]
        self.runs.append({
            "round": round_no, "path": path, "task": task, "slot": slot,
            "sets": list(sets),
            "names": list(SUBSTATS), "counts": list(COUNTS), "metrics": metrics,
            "snapshot": snapshot, "image": warehouse, "evidence": evidence,
            "inputs": {"file": "inputs.jsonl", "run_id": run_id,
                       "first_seq": span[0], "last_seq": span[1]},
        })
        self.review.append({
            "round": round_no, "path": path, "task": task,
            "sets_exact": True, "counts_exact": True,
            "evidence_images": [item["image"] for item in evidence], "note": "",
        })

    @staticmethod
    def _groups(sets: tuple[str, ...], n_stages: int) -> list[list[str]]:
        if n_stages <= 1 or len(sets) == 1:
            return [list(sets)]
        return [list(sets[:2]), list(sets[2:])]

    # ---- 场景 ----
    def build(self, sets_by_task: dict | None = None) -> None:
        if self.scenario == "baseline":
            orders = {1: ("legacy", "optimized"), 2: ("optimized", "legacy"), 3: ("legacy", "optimized")}
            for round_no in (1, 2, 3):
                for path in orders[round_no]:
                    for task in ("A", "B", "C"):
                        mode = "legacy" if path == "legacy" else ("learn" if task == "A" else "replay")
                        self.build_run(round_no, path, task, mode=mode)
        elif self.scenario == "different_sets":
            for task in ("A", "B", "C"):
                mode = "replay" if task == "C" else "learn"
                self.build_run(1, "optimized", task, mode=mode, n_stages=1)
        elif self.scenario == "pause_resume":
            self.build_run(1, "optimized", "A", mode="learn")
            self.control("paused", "r01-optimized-B", round=1, path="optimized", task="B")
            self.t_ns += 1_500_000_000  # 暂停区间 ≥1 秒
            self.control("resumed", "r01-optimized-B", round=1, path="optimized", task="B")
            self.build_run(1, "optimized", "B", mode="replay")
        else:
            self.build_run(1, "optimized", "A", mode="learn")
            self.build_run(1, "optimized", "B", mode="replay", partial=True)

    # ---- 产物 ----
    def result(self) -> dict:
        comparisons, timing = [], []
        if self.scenario == "baseline":
            for round_no in (1, 2, 3):
                for task in ("A", "B", "C"):
                    comparisons.append({"round": round_no, "task": task, "equal": True})
            for task in ("B", "C"):
                timing.append({"task": task, "legacy_median_ms": 900,
                               "replay_median_ms": 400, "faster": True})
        outcome = "stopped" if self.scenario == "stop_after_click" else "completed"
        return {
            "version": 3,
            "input_file": "inputs.jsonl",
            "scenario": self.scenario,
            "outcome": outcome,
            "environment": None,
            "runs": self.runs,
            "comparisons": comparisons,
            "timing": timing,
        }

    def write(self) -> Path:
        self.build()
        result_path = self.root / "result.json"
        result_path.write_text(json.dumps(self.result(), ensure_ascii=False, indent=2), encoding="utf-8")
        with (self.root / "inputs.jsonl").open("w", encoding="utf-8") as handle:
            for row in self.rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        (self.root / "review.json").write_text(
            json.dumps({"version": 1, "entries": self.review}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result_path

    # ---- 破坏工具 ----
    def rewrite_rows(self) -> None:
        with (self.root / "inputs.jsonl").open("w", encoding="utf-8") as handle:
            for row in self.rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def update_result(self, path: Path) -> None:
        path.write_text(json.dumps(self.result(), ensure_ascii=False, indent=2), encoding="utf-8")

    def find_row(self, **match) -> dict:
        for row in self.rows:
            if all(row.get(key) == value for key, value in match.items()):
                return row
        raise AssertionError(f"夹具里找不到记录：{match}")


def run_checker(root: Path) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, "PYTHONUTF8": "1"}  # 父子一律 UTF-8，不依赖系统代码页
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(CHECKER),
         "--live-result", str(root / "result.json"),
         "--review", str(root / "review.json")],
        capture_output=True, text=True, encoding="utf-8", env=env,
    )


def expect_fail(result: subprocess.CompletedProcess, reason: str) -> None:
    assert result.returncode != 0, f"{reason}：检查器仍然通过\n{result.stdout}"
    assert "不通过" in result.stdout


@pytest.mark.parametrize("scenario", ["baseline", "different_sets", "pause_resume", "stop_after_click"])
def test_valid_fixture_passes(tmp_path: Path, scenario: str) -> None:
    fixture = Fixture(tmp_path, scenario)
    fixture.write()
    result = run_checker(tmp_path)
    assert result.returncode == 0, f"合法 {scenario} 产物应通过：\n{result.stdout}"


# ---------------------------------------------------------------- review 面
def test_review_null_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    fixture.review[0]["sets_exact"] = None
    (tmp_path / "review.json").write_text(
        json.dumps({"version": 1, "entries": fixture.review}, ensure_ascii=False), encoding="utf-8"
    )
    expect_fail(run_checker(tmp_path), "review 填 null")


def test_review_missing_entry_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    fixture.review.pop()
    (tmp_path / "review.json").write_text(
        json.dumps({"version": 1, "entries": fixture.review}, ensure_ascii=False), encoding="utf-8"
    )
    expect_fail(run_checker(tmp_path), "review 缺项")


def test_review_missing_file_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    (tmp_path / "review.json").unlink()
    expect_fail(run_checker(tmp_path), "缺 review.json")


def test_review_extra_entry_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    fixture.review.append(dict(fixture.review[0], task="C", round=1, path="optimized"))
    (tmp_path / "review.json").write_text(
        json.dumps({"version": 1, "entries": fixture.review}, ensure_ascii=False), encoding="utf-8"
    )
    expect_fail(run_checker(tmp_path), "review 重复条目")


# ---------------------------------------------------------------- 输入文件面
def test_deleted_row_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    victim = fixture.find_row(role="reset", event="end")
    fixture.rows = [row for row in fixture.rows if row["seq"] != victim["seq"]]
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "删掉一行输入记录")


def test_duplicate_reset_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    extra = dict(fixture.find_row(role="reset", event="begin"))
    extra["seq"] = max(row["seq"] for row in fixture.rows) + 1
    extra["t_ns"] = max(row["t_ns"] for row in fixture.rows) + 1
    fixture.rows.append(extra)
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "重复 reset")


def test_seq_gap_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    for row in fixture.rows[len(fixture.rows) // 2:]:
        row["seq"] += 1
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "seq 出现断号")


def test_time_backwards_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    fixture.rows[-1]["t_ns"] = 1
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "t_ns 回退")


def test_begin_end_param_mismatch_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    row = fixture.find_row(role="set_option", event="end")
    row["x"] = row["x"] + 5
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "begin/end 参数不一致")


def test_unknown_role_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    victim = fixture.find_row(role="reset", event="begin")
    call_id = victim["call_id"]
    for row in fixture.rows:
        if row["call_id"] == call_id:
            row["role"] = "strengthen"
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "未知角色")


def test_non_escape_key_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    fixture.find_row(role="close_panel", event="begin")["vk"] = 82
    fixture.find_row(role="close_panel", event="end")["vk"] = 82
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "非 Esc 按键")


def test_alt_click_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    row = fixture.find_row(role="set_option", event="begin")
    row["alt"] = True
    fixture.find_row(role="set_option", event="end")["alt"] = True
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "alt=true 点击")


def test_cross_run_mixing_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    row = fixture.find_row(role="set_option", event="begin")
    row["task"] = "C"
    fixture.find_row(role="set_option", event="end")["task"] = "C"
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "跨 run 混用")


def test_unattributed_row_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    fixture.rows[-1]["run_id"] = None
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "无归属记录")


def test_truncated_file_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    fixture.rows = fixture.rows[:-4]
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "输入文件被截断")


# ---------------------------------------------------------------- 坐标/证据面
def test_coordinate_not_box_center_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    row = fixture.find_row(role="set_option", event="begin")
    row["x"] = row["x"] + 30
    fixture.find_row(role="set_option", event="end")["x"] = row["x"]
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "点击坐标不是证据框中心")


def test_missing_evidence_image_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    (tmp_path / fixture.runs[0]["evidence"][0]["image"]).unlink()
    expect_fail(run_checker(tmp_path), "证据帧文件缺失")


def test_evidence_target_mismatch_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    fixture.runs[0]["evidence"][0]["targets"][0] = "河豚电音特别版"
    fixture.update_result(tmp_path / "result.json")
    expect_fail(run_checker(tmp_path), "证据目标与输入目标不一致")


def test_evidence_frame_count_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    fixture.runs[0]["evidence"] = [item for item in fixture.runs[0]["evidence"]
                                   if item["event"] != "substats_selected"]
    fixture.update_result(tmp_path / "result.json")
    expect_fail(run_checker(tmp_path), "缺副属性证据帧")


# ---------------------------------------------------------------- 指标/数量面
def test_selection_clicks_mismatch_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    fixture.runs[0]["metrics"]["selection_clicks"] += 1
    fixture.update_result(tmp_path / "result.json")
    expect_fail(run_checker(tmp_path), "selection_clicks 与点击次数不符")


def test_wheel_events_mismatch_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    fixture.runs[0]["metrics"]["wheel_events"] += 1
    fixture.update_result(tmp_path / "result.json")
    expect_fail(run_checker(tmp_path), "wheel_events 与 set_scroll 不符")


def test_replay_with_search_ocr_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    run_c = next(run for run in fixture.runs if run["task"] == "C")
    run_c["metrics"]["search_ocr_calls"] = 2
    fixture.update_result(tmp_path / "result.json")
    expect_fail(run_checker(tmp_path), "回放仍做名称查找 OCR")


def test_legacy_run_cannot_be_learn_mode(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "baseline")
    fixture.write()
    run = next(run for run in fixture.runs if run["path"] == "legacy")
    run["metrics"]["mode"] = "learn"
    fixture.update_result(tmp_path / "result.json")
    expect_fail(run_checker(tmp_path), "legacy 项写成 learn")


def test_baseline_needs_eighteen_runs(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "baseline")
    fixture.write()
    fixture.runs = fixture.runs[:-1]
    fixture.update_result(tmp_path / "result.json")
    expect_fail(run_checker(tmp_path), "baseline 少一个成功 run")


def test_baseline_snapshot_mismatch_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "baseline")
    fixture.write()
    run = next(run for run in fixture.runs
               if run["round"] == 1 and run["path"] == "optimized" and run["task"] == "A")
    run["snapshot"] = dict(run["snapshot"], total=13)
    fixture.update_result(tmp_path / "result.json")
    expect_fail(run_checker(tmp_path), "同标签快照不相等")


# ---------------------------------------------------------------- 控制场景面
def test_stop_with_extra_input_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "stop_after_click")
    fixture.write()
    last = fixture.rows[-1]
    assert last["status"] == "stop_requested"
    fixture.call("r01-optimized-B", "click_client", role="set_option", target="静听嘉音",
                 stage=1, selected=True, x=900, y=500, round=1, path="optimized", task="B")
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "停止后新增输入")


def test_stop_not_last_line_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "stop_after_click")
    fixture.write()
    marker = fixture.rows.pop()
    fixture.rows.insert(len(fixture.rows) - 2, marker)
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "stop_requested 不是最后一行")


def test_stop_forged_success_run_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "stop_after_click")
    fixture.write()
    fixture.runs.append(dict(fixture.runs[0], task="B"))
    fixture.update_result(tmp_path / "result.json")
    expect_fail(run_checker(tmp_path), "为停止的 B 伪造成功 run")


def test_pause_too_short_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "pause_resume")
    fixture.write()
    for row in fixture.rows:
        if row["status"] == "resumed":
            row["t_ns"] = row["t_ns"] - 900_000_000
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "暂停不足 1 秒")


def test_pause_with_input_inside_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "pause_resume")
    fixture.write()
    paused = next(row for row in fixture.rows if row["status"] == "paused")
    resumed = next(row for row in fixture.rows if row["status"] == "resumed")
    victim = next(
        row for row in fixture.rows
        if row["run_id"] == "r01-optimized-B" and row["event"] == "begin"
    )
    fixture.rows.remove(victim)
    fixture.rows.insert(fixture.rows.index(resumed), victim)
    for number, row in enumerate(fixture.rows, start=1):
        row["seq"] = number
    fixture.rewrite_rows()
    expect_fail(run_checker(tmp_path), "暂停期间出现输入")


def test_sets_record_mismatch_fails(tmp_path: Path) -> None:
    """run 记录的 sets 与它实际点过的套装不一致 → 非零。"""
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    fixture.runs[0]["sets"] = ["河豚电音", "别的套装"]
    fixture.update_result(tmp_path / "result.json")
    expect_fail(run_checker(tmp_path), "sets 记录与 set_option 目标不一致")


def test_different_sets_b_not_single_fails(tmp_path: Path) -> None:
    """different_sets 的 B 必须是 A/C 组合里的单套装 → 否则非零。"""
    fixture = Fixture(tmp_path, "different_sets")
    fixture.write()
    run_b = next(run for run in fixture.runs if run["task"] == "B")
    run_b["sets"] = ["河豚电音", "静听嘉音"]
    fixture.update_result(tmp_path / "result.json")
    expect_fail(run_checker(tmp_path), "different_sets 的 B 不是单套装变体")


def test_baseline_sets_inconsistent_fails(tmp_path: Path) -> None:
    """baseline 的 A/B/C 套装组合必须一致 → 否则非零。"""
    fixture = Fixture(tmp_path, "baseline")
    fixture.write()
    run = next(run for run in fixture.runs if run["task"] == "B" and run["path"] == "legacy")
    run["sets"] = list(DEFAULT_SETS_FOR_TEST)
    fixture.update_result(tmp_path / "result.json")
    expect_fail(run_checker(tmp_path), "baseline 各任务的套装组合不一致")


def test_environment_unmet_fails(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, "different_sets")
    result_path = fixture.write()
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["outcome"] = "environment_unmet"
    payload["environment"] = {"round": 1, "path": "optimized", "task": "A", "total": 0,
                              "reason": "candidate_count_out_of_range", "image": "x.png"}
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    expect_fail(run_checker(tmp_path), "environment_unmet 不得判通过")
