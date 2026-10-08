"""真机验收产物检查器（spec §8.4）。读 result.json + inputs.jsonl + review.json，逐条硬核。

    .venv/Scripts/python.exe tests/check_filter_replay_artifacts.py \
        --live-result <目录>/result.json --review <目录>/review.json

退出 0 只表示该场景真机验收通过（含合法 stop_after_click 的停止契约）；
缺文件、缺项、false/null、序号/调用/坐标/角色/顺序/数量/证据不符一律非零。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE / "src") not in sys.path:
    sys.path.insert(0, str(BASE / "src"))

from zzz_ddtool.gamefilter import _normalize_stat  # noqa: E402
from zzz_ddtool.substatfilter import plan_substat_filter  # noqa: E402

SCENARIOS = ("baseline", "different_sets", "pause_resume", "stop_after_click")
TASKS = ("A", "B", "C")
SETS = ("河豚电音", "静听嘉音", "折枝剑歌")
SETS_B_ONLY = ("河豚电音",)
PAUSE_MIN_NS = 1_000_000_000

CLICK_ROLES = {"open_panel", "reset", "slot", "open_sets", "set_option", "confirm_sets",
               "open_mainstats", "mainstat_option", "collapse_mainstats", "open_substats",
               "substat_option", "substat_count"}
SCROLL_ROLES = {"set_scroll", "mainstat_scroll", "substat_scroll"}
KEY_ROLES = {"close_panel"}
ALLOWED_ROLES = CLICK_ROLES | SCROLL_ROLES | KEY_ROLES
MAINSTAT_ROLES = {"open_mainstats", "mainstat_scroll", "mainstat_option"}
INPUT_FIELDS = (
    "version", "seq", "t_ns", "run_id", "round", "path", "task", "event", "call_id", "api",
    "role", "target", "stage_index", "selected", "hwnd", "x", "y", "alt", "vk", "delta_units",
    "status", "error",
)
PARAM_FIELDS = ("api", "role", "target", "stage_index", "selected", "hwnd", "x", "y", "alt",
                "vk", "delta_units")
RESULT_FIELDS = ("version", "input_file", "scenario", "outcome", "environment", "runs",
                 "comparisons", "timing")
RUN_FIELDS = ("round", "path", "task", "slot", "sets", "names", "counts", "metrics", "snapshot",
              "image", "evidence", "inputs")
EVIDENCE_FIELDS = ("event", "stage_index", "targets", "boxes", "image")
SNAPSHOT_FIELDS = ("total", "cells", "skips", "empties", "problems")
REVIEW_FIELDS = ("version", "entries")
REVIEW_ENTRY_FIELDS = ("round", "path", "task", "sets_exact", "counts_exact",
                       "evidence_images", "note")
# §9 步骤6：固定清单（含 wininput.py；新增文件按清单检查，不依赖 git 索引/未跟踪状态）
STATIC_FILES = (
    "src/zzz_ddtool/control.py",
    "src/zzz_ddtool/filter_replay.py",
    "src/zzz_ddtool/gamefilter.py",
    "src/zzz_ddtool/ocr.py",
    "src/zzz_ddtool/runner.py",
    "src/zzz_ddtool/stats.py",
    "src/zzz_ddtool/wininput.py",
    "tests/test_gamefilter_replay.py",
    "tests/test_input_control.py",
    "tests/test_input_capture.py",
    "tests/test_gamefilter_substat.py",
    "tests/test_runner.py",
    "tests/test_stats.py",
    "tests/live_filter_replay.py",
    "tests/check_filter_replay_artifacts.py",
    "tests/fixtures/filter-replay/manifest.json",
    "手册/参考图/驱动盘筛选/说明.md",
    "手册/参考图/驱动盘详情/说明.md",
    "手册/技术/识别标定.md",
)
MANIFEST_PATH = BASE / "tests" / "fixtures" / "filter-replay" / "manifest.json"


class Checker:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.scenario: str | None = None

    def fail(self, message: str) -> None:
        self.errors.append(message)

    # ---------------------------------------------------------------- 读取
    def load_json(self, path: Path, label: str):
        if not path.exists():
            self.fail(f"{label} 不存在：{path}")
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            self.fail(f"{label} 不是合法 JSON：{exc}")
            return None

    def load_inputs(self, path: Path) -> list[dict]:
        if not path.exists():
            self.fail(f"inputs.jsonl 不存在：{path}")
            return []
        rows: list[dict] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                self.fail(f"inputs.jsonl 第 {number} 行不是 JSON：{exc}")
                continue
            if set(row) != set(INPUT_FIELDS):
                self.fail(f"inputs.jsonl 第 {number} 行字段不符：{sorted(set(row) ^ set(INPUT_FIELDS))}")
                continue
            rows.append(row)
        return rows

    # ---------------------------------------------------------------- 输入文件
    def check_input_rows(self, rows: list[dict]) -> None:
        last_t = -1
        last_seq = 0
        begins: dict[int, dict] = {}
        ends: dict[int, dict] = {}
        for index, row in enumerate(rows, start=1):
            if row["version"] != 1:
                self.fail(f"第 {index} 行 version 不是 1")
            if row["seq"] != last_seq + 1:
                self.fail(f"第 {index} 行 seq={row['seq']} 不是连续递增（上一条 {last_seq}）")
            last_seq = row["seq"]
            if row["t_ns"] < last_t:
                self.fail(f"第 {index} 行 t_ns 回退")
            last_t = row["t_ns"]
            event = row["event"]
            if event == "control":
                if row["call_id"] is not None or row["api"] is not None or row["role"] is not None:
                    self.fail(f"第 {index} 行 control 标记带上了调用字段")
                if row["status"] not in ("paused", "resumed", "stop_requested"):
                    self.fail(f"第 {index} 行 control 状态未知：{row['status']}")
                if row["error"] is not None:
                    self.fail(f"第 {index} 行 control 标记的 error 不是 null")
                continue
            if event not in ("begin", "end"):
                self.fail(f"第 {index} 行 event 未知：{event}")
                continue
            call_id = row["call_id"]
            if not isinstance(call_id, int) or call_id <= 0:
                self.fail(f"第 {index} 行 call_id 非法：{call_id}")
                continue
            if event == "begin":
                if row["status"] != "started" or row["error"] is not None:
                    self.fail(f"第 {index} 行 begin 状态不是 started/error=null")
                if call_id in begins:
                    self.fail(f"call_id={call_id} 出现多个 begin")
                begins[call_id] = row
            else:
                if row["status"] not in ("completed", "failed", "stopped"):
                    self.fail(f"第 {index} 行 end 状态未知：{row['status']}")
                if row["status"] != "completed" and not row["error"]:
                    self.fail(f"第 {index} 行 {row['status']} 的 error 为空")
                if call_id in ends:
                    self.fail(f"call_id={call_id} 出现多个 end")
                ends[call_id] = row
            if row["role"] is not None and row["role"] not in ALLOWED_ROLES:
                self.fail(f"第 {index} 行 role 不在允许集合：{row['role']}")
            if row["role"] is None:
                self.fail(f"第 {index} 行普通输入没有 role")
            api = row["api"]
            role = row["role"]
            if api == "click_client":
                if role not in CLICK_ROLES:
                    self.fail(f"第 {index} 行 click_client 的角色不对：{role}")
                if row["alt"] is not False:
                    self.fail(f"第 {index} 行点击 alt 不是 false")
                if not isinstance(row["x"], int) or not isinstance(row["y"], int):
                    self.fail(f"第 {index} 行点击缺少客户区坐标")
            elif api in ("wheel_client", "send_wheel_event"):
                if role not in SCROLL_ROLES:
                    self.fail(f"第 {index} 行滚动的角色不对：{role}")
                if row["delta_units"] in (None, 0):
                    self.fail(f"第 {index} 行滚动没有 delta_units")
            elif api in ("press_key", "press_key_once"):
                if role not in KEY_ROLES or row["vk"] != 27:
                    self.fail(f"第 {index} 行按键只能是 close_panel/vk=27，实际 {role}/{row['vk']}")
            else:
                self.fail(f"第 {index} 行 api 不在允许集合：{api}")
        for call_id, begin in begins.items():
            end = ends.get(call_id)
            if end is None:
                self.fail(f"call_id={call_id} 只有 begin 没有 end")
                continue
            if end["seq"] < begin["seq"]:
                self.fail(f"call_id={call_id} 的 end 早于 begin")
            for field in PARAM_FIELDS:
                if begin[field] != end[field]:
                    self.fail(f"call_id={call_id} 的 {field} 在 begin/end 不一致")
        for call_id in ends:
            if call_id not in begins:
                self.fail(f"call_id={call_id} 只有 end 没有 begin")
        return None

    def calls(self, rows: list[dict]) -> list[dict]:
        """每个完成的逻辑调用一条（begin 与其 completed end）。"""
        begins = {row["call_id"]: row for row in rows if row["event"] == "begin"}
        out: list[dict] = []
        for row in rows:
            if row["event"] != "end":
                continue
            begin = begins.get(row["call_id"])
            if begin is None:
                continue
            merged = dict(begin)
            merged["status"] = row["status"]
            merged["end_seq"] = row["seq"]
            merged["begin_seq"] = begin["seq"]
            out.append(merged)
        return out

    # ---------------------------------------------------------------- run 归属
    def check_run_ranges(self, rows: list[dict], runs: list[dict]) -> None:
        covered: set[int] = set()
        spans: list[tuple[int, int, str]] = []
        for run in runs:
            info = run.get("inputs") or {}
            run_id = info.get("run_id")
            if not run_id:
                self.fail(f"run {run.get('task')} 缺少 inputs 引用")
                continue
            if info.get("file") != "inputs.jsonl":
                self.fail(f"run {run_id} 的 inputs.file 不是 inputs.jsonl")
            mine = [
                row
                for row in rows
                if row["run_id"] == run_id and row["round"] == run["round"]
                and row["path"] == run["path"] and row["task"] == run["task"]
            ]
            if not mine:
                self.fail(f"run {run_id} 没有输入记录")
                continue
            first, last = int(info.get("first_seq") or 0), int(info.get("last_seq") or 0)
            if first <= 0 or last < first:
                self.fail(f"run {run_id} 的 seq 范围非法：{first}-{last}")
                continue
            seqs = [row["seq"] for row in mine]
            if min(seqs) != first or max(seqs) != last:
                self.fail(f"run {run_id} 的 seq 范围 {first}-{last} 未覆盖实际记录 {min(seqs)}-{max(seqs)}")
            if any(not (first <= row["seq"] <= last) for row in mine):
                self.fail(f"run {run_id} 有记录落在引用范围之外")
            for row in mine:
                if row["round"] != run["round"] or row["path"] != run["path"] or row["task"] != run["task"]:
                    self.fail(f"run {run_id} 的记录 run 关联不一致")
            spans.append((first, last, run_id))
            covered.update(range(first, last + 1))
        spans.sort()
        for (first, last, run_id), (next_first, _next_last, next_id) in zip(spans, spans[1:]):
            if next_first <= last:
                self.fail(f"run {run_id} 与 {next_id} 的 seq 范围重叠")
        declared = {
            run["inputs"]["run_id"]: (run["round"], run["path"], run["task"])
            for run in runs
            if (run.get("inputs") or {}).get("run_id")
        }
        for row in rows:
            run_id = row["run_id"]
            if run_id is not None:
                if run_id in declared:
                    if (row["round"], row["path"], row["task"]) != declared[run_id]:
                        self.fail(
                            f"第 {row['seq']} 行的 run 关联与 {run_id} 声明不一致："
                            f"{(row['round'], row['path'], row['task'])}"
                        )
                elif not (
                    self.scenario == "stop_after_click"
                    and run_id == "r01-optimized-B"
                    and (row["round"], row["path"], row["task"]) == (1, "optimized", "B")
                ):
                    self.fail(f"第 {row['seq']} 行的 run_id 不在任何成功 run 里：{run_id}")
            if row["seq"] in covered:
                continue
            if self.scenario == "stop_after_click" and run_id == "r01-optimized-B" and (
                row["round"], row["path"], row["task"]
            ) == (1, "optimized", "B"):
                continue  # stop_after_click 唯一允许的覆盖例外：B 的部分调用
            self.fail(f"第 {row['seq']} 行不在任何成功 run 的引用范围内（run_id={run_id}）")

    def expected_sets(self, run: dict) -> tuple[str, ...]:
        """验收项该选的套装：以 run 自己记录的 `sets` 为准（执行者可用 --sets 按环境选定）。
        A/B/C 之间的一致性由 `check_sets_consistency` 按场景校验。"""
        return tuple(str(name) for name in run.get("sets") or ())

    def check_sets_consistency(self, runs: list[dict]) -> None:
        """套装组合的合法性与场景内一致性（spec §8.4 + 参数化约定）。"""
        by_task = {run["task"]: self.expected_sets(run) for run in runs}
        for task, names in by_task.items():
            if not names or any(not name.strip() for name in names):
                self.fail(f"{task} 的 sets 记录为空或不合法：{names}")
            if len(set(names)) != len(names):
                self.fail(f"{task} 的 sets 有重复：{names}")
        if self.scenario == "different_sets":
            if by_task.get("A") != by_task.get("C"):
                self.fail(f"different_sets 的 A/C 套装组合应一致：{by_task.get('A')} vs {by_task.get('C')}")
            single = by_task.get("B") or ()
            base = by_task.get("A") or ()
            if len(single) != 1 or single[0] not in base:
                self.fail(f"different_sets 的 B 应是 A/C 组合里的单套装：{single}（A/C={base}）")
        else:
            if len({names for names in by_task.values()}) > 1:
                self.fail(f"{self.scenario} 的 A/B/C 套装组合应一致：{by_task}")

    # ---------------------------------------------------------------- 单项顺序与数量
    def check_run_contract(self, run: dict, rows: list[dict], allow_failed: bool) -> None:
        run_id = f"r{run['round']:02d}-{run['path']}-{run['task']}"
        mine = [row for row in rows if row["run_id"] == run_id]
        calls = self.calls(mine)
        if not allow_failed:
            bad = [call for call in calls if call["status"] != "completed"]
            if bad:
                self.fail(f"run {run_id} 的成功记录里有非 completed 调用：{bad[:2]}")
        by_role: dict[str, list[dict]] = {}
        for call in calls:
            by_role.setdefault(str(call["role"]), []).append(call)
        for role in ("open_panel", "reset", "close_panel"):
            if len(by_role.get(role, [])) != 1:
                self.fail(f"run {run_id} 的 {role} 数量不是 1：{len(by_role.get(role, []))}")
        if by_role.get("close_panel") and calls and by_role["close_panel"][-1]["begin_seq"] != calls[-1]["begin_seq"]:
            self.fail(f"run {run_id} 的 close_panel 不是最后一个输入")
        slots = by_role.get("slot", [])
        if len(slots) != 1 or slots[0]["target"] != run["slot"]:
            self.fail(f"run {run_id} 的 slot 目标不是 {run['slot']}")
        if slots and by_role.get("reset") and not (
            by_role["reset"][0]["begin_seq"] < slots[0]["begin_seq"]
        ):
            self.fail(f"run {run_id} 的 reset 晚于 slot")
        if slots and by_role.get("set_option"):
            if not all(slots[0]["begin_seq"] < call["begin_seq"] for call in by_role["set_option"]):
                self.fail(f"run {run_id} 存在 slot 之前的套装选择")
        substat_calls = [
            call for call in calls
            if call["role"] in ("substat_option", "substat_count", "open_substats", "substat_scroll")
        ]
        if substat_calls and by_role.get("confirm_sets"):
            if not all(
                call["begin_seq"] > max(c["begin_seq"] for c in by_role["confirm_sets"])
                for call in substat_calls
            ):
                self.fail(f"run {run_id} 存在确认套装之前的副属性输入")
        if run["names"] and not substat_calls:
            self.fail(f"run {run_id} 没有副属性输入记录")
        metrics = run["metrics"]
        if run["path"] == "legacy" and metrics.get("mode") != "legacy":
            self.fail(f"run {run_id} 的 mode 不是 legacy：{metrics.get('mode')}")
        if run["path"] == "optimized" and metrics.get("mode") not in ("learn", "replay"):
            self.fail(f"run {run_id} 的 mode 非法：{metrics.get('mode')}")
        if metrics.get("mode") == "replay" and int(metrics.get("search_ocr_calls") or 0) != 0:
            self.fail(f"run {run_id} 回放仍有名称查找 OCR")
        if not metrics.get("success"):
            self.fail(f"run {run_id} 的 metrics.success 不是 true")
        # 套装目标集合与证据一致（目标名来自该 run 记录的 sets）
        options = [call for call in by_role.get("set_option", []) if call["status"] == "completed"]
        want = tuple(name.replace(" ", "") for name in self.expected_sets(run))
        got = tuple(str(call["target"]).replace(" ", "") for call in options)
        if len(options) != len(want) or sorted(got) != sorted(want):
            self.fail(f"run {run_id} 的 set_option 目标集合不符：{got} != {want}")
        if int(metrics.get("selection_clicks") or 0) != len(options):
            self.fail(
                f"run {run_id} 的 selection_clicks={metrics.get('selection_clicks')} 与 set_option={len(options)} 不符"
            )
        scrolls = by_role.get("set_scroll", [])
        if int(metrics.get("wheel_events") or 0) != len(scrolls):
            self.fail(
                f"run {run_id} 的 wheel_events={metrics.get('wheel_events')} 与 set_scroll={len(scrolls)} 不符"
            )
        if any(call["role"] in MAINSTAT_ROLES for call in calls):
            self.fail(f"run {run_id} 出现主属性输入（基线 mainstats 必须为空）")
        # 证据与 set_option 坐标、段号对应
        evidence = run["evidence"]
        sets_frames = [item for item in evidence if item["event"] == "set_stage_selected"]
        substat_frames = [item for item in evidence if item["event"] == "substats_selected"]
        if not sets_frames:
            self.fail(f"run {run_id} 没有套装完成证据帧")
        if len(substat_frames) != 1:
            self.fail(f"run {run_id} 的副属性证据帧数量不是 1：{len(substat_frames)}")
        for item in evidence:
            for key in EVIDENCE_FIELDS:
                if key not in item:
                    self.fail(f"run {run_id} 的证据条目缺字段 {key}")
            if len(item.get("targets") or []) != len(item.get("boxes") or []):
                self.fail(f"run {run_id} 的证据 targets/boxes 长度不一致")
            image = item.get("image")
            if not image or not (self.base_dir / image).exists():
                self.fail(f"run {run_id} 的证据图不存在：{image}")
        stage_boxes: dict[tuple[int, str], tuple[int, int, int, int]] = {}
        for item in sets_frames:
            for target, box in zip(item["targets"], item["boxes"]):
                stage_boxes[(int(item["stage_index"]), str(target))] = tuple(box)
        if len(stage_boxes) != len(options):
            self.fail(f"run {run_id} 的证据框数量与 set_option 数量不一致")
        for call in options:
            key = (int(call["stage_index"] or 0), str(call["target"]))
            box = stage_boxes.get(key)
            if box is None:
                self.fail(f"run {run_id} 的 set_option {key} 在证据里找不到对应框")
                continue
            center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
            if (call["x"], call["y"]) != center:
                self.fail(f"run {run_id} 的 set_option {key} 坐标 {call['x'], call['y']} 不是证据框中心 {center}")
        # 副属性：名称归一化、数量整数、selected 与计划一致、坐标等于证据框中心、不许重复切换
        substat_names = tuple(run["names"])
        counts = [int(value) for value in run["counts"]]
        option_calls = [call for call in by_role.get("substat_option", [])]
        count_calls = [call for call in by_role.get("substat_count", [])]
        seen_options: set[str] = set()
        for call in option_calls:
            target = _normalize_stat(str(call["target"]))
            if target not in {_normalize_stat(name) for name in substat_names}:
                self.fail(f"run {run_id} 的副属性目标不在计划里：{target}")
            if call["status"] == "completed":
                if target in seen_options:
                    self.fail(f"run {run_id} 重复切换同一副属性：{target}")
                seen_options.add(target)
                if call["selected"] is not True:
                    self.fail(f"run {run_id} 的副属性 {target} selected 不是 true")
        for call in count_calls:
            if not isinstance(call["target"], int) or not 1 <= call["target"] <= 4:
                self.fail(f"run {run_id} 的数量目标非法：{call['target']}")
                continue
            expect = call["target"] in counts
            if call["status"] == "completed" and call["selected"] is not expect:
                self.fail(f"run {run_id} 的数量 {call['target']} selected 与计划不符")
        if substat_frames:
            frame = substat_frames[0]
            expect_targets = [*substat_names, 1, 2, 3, 4]
            if [str(value) for value in frame["targets"]] != [str(value) for value in expect_targets]:
                self.fail(f"run {run_id} 副属性证据 targets 不符：{frame['targets']}")
            targets = list(frame["targets"])
            boxes = list(frame["boxes"])
            call_by_key = {(_normalize_stat(str(c["target"])), str(c.get("stage_index"))): c for c in option_calls}
            for target, box in zip(targets, boxes):
                key = (_normalize_stat(str(target)), "None")
                call = call_by_key.get(key)
                if call is None:
                    continue
                center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
                if (call["x"], call["y"]) != center:
                    self.fail(f"run {run_id} 副属性 {target} 坐标不是证据框中心")
        # 快照字段
        snapshot = run["snapshot"]
        for key in SNAPSHOT_FIELDS:
            if key not in snapshot:
                self.fail(f"run {run_id} 的快照缺字段 {key}")
        if snapshot.get("problems"):
            self.fail(f"run {run_id} 的快照 problems 非空：{snapshot['problems'][:2]}")
        if not (self.base_dir / run["image"]).exists():
            self.fail(f"run {run_id} 的仓库帧不存在：{run['image']}")

    # ---------------------------------------------------------------- 快照相等
    def check_snapshot_equality(self, runs: list[dict]) -> None:
        """同轮同标签的 legacy 与 optimized 快照必须精确相等（§8.4）。"""
        keyed = {(run["round"], run["path"], run["task"]): run for run in runs}
        for (round_no, path, task), run in keyed.items():
            if path != "legacy":
                continue
            other = keyed.get((round_no, "optimized", task))
            if other is None:
                continue
            if run["snapshot"] != other["snapshot"]:
                self.fail(f"r{round_no:02d}-{task} 的 legacy/optimized 快照不相等")

    # ---------------------------------------------------------------- review
    def check_review(self, review, runs: list[dict]) -> None:
        if review is None:
            return
        if set(review) != set(REVIEW_FIELDS):
            self.fail(f"review.json 顶层字段不符：{sorted(set(review) ^ set(REVIEW_FIELDS))}")
        if review.get("version") != 1:
            self.fail("review.json version 不是 1")
        entries = review.get("entries")
        if not isinstance(entries, list):
            self.fail("review.json entries 不是数组")
            return
        expect = {(run["round"], run["path"], run["task"]) for run in runs}
        seen: set[tuple[int, str, str]] = set()
        for entry in entries:
            if set(entry) != set(REVIEW_ENTRY_FIELDS):
                self.fail(f"review 条目字段不符：{sorted(set(entry) ^ set(REVIEW_ENTRY_FIELDS))}")
                continue
            key = (entry["round"], entry["path"], entry["task"])
            if key in seen:
                self.fail(f"review 条目重复：{key}")
            seen.add(key)
            if key not in expect:
                self.fail(f"review 出现未完成调用的条目：{key}")
            if entry["sets_exact"] is not True:
                self.fail(f"review {key} 的 sets_exact 不是 true：{entry['sets_exact']}（{entry.get('note')}）")
            if entry["counts_exact"] is not True:
                self.fail(f"review {key} 的 counts_exact 不是 true：{entry['counts_exact']}（{entry.get('note')}）")
        for key in expect - seen:
            self.fail(f"review 缺少成功 run 的条目：{key}")
        for run in runs:
            key = (run["round"], run["path"], run["task"])
            entry = next(
                (e for e in entries if (e["round"], e["path"], e["task"]) == key), None
            )
            if entry is None:
                continue
            want = [item["image"] for item in run["evidence"]]
            if sorted(entry["evidence_images"]) != sorted(want):
                self.fail(f"review {key} 的 evidence_images 未覆盖全部证据帧")

    # ---------------------------------------------------------------- 场景契约
    def check_scenario(self, result: dict, runs: list[dict], rows: list[dict], review) -> None:
        scenario = result["scenario"]
        outcome = result["outcome"]
        comparisons = result.get("comparisons") or []
        timing = result.get("timing") or []
        keyed = {(run["round"], run["path"], run["task"]): run for run in runs}
        if scenario == "baseline":
            if outcome != "completed":
                self.fail(f"baseline 期望 completed，实际 {outcome}")
            for round_no in (1, 2, 3):
                for path in ("legacy", "optimized"):
                    for task in TASKS:
                        if (round_no, path, task) not in keyed:
                            self.fail(f"baseline 缺 run r{round_no:02d}-{path}-{task}")
            if len(runs) != 18:
                self.fail(f"baseline 期望 18 个成功 run，实际 {len(runs)}")
            if len(comparisons) != 9 or not all(row.get("equal") is True for row in comparisons):
                self.fail(f"baseline 的 comparisons 不是 9 个 equal=true：{comparisons}")
            if len(timing) != 2:
                self.fail(f"baseline 的 timing 不是 2 项：{timing}")
            for row in timing:
                if row.get("task") not in ("B", "C") or row.get("faster") is not True:
                    self.fail(f"baseline 的耗时项不符：{row}")
            for (round_no, path, task), run in keyed.items():
                mode = run["metrics"]["mode"]
                if path == "legacy" and mode != "legacy":
                    self.fail(f"r{round_no:02d}-legacy-{task} 模式是 {mode}")
                if path == "optimized":
                    expect = "learn" if task == "A" else "replay"
                    if mode != expect:
                        self.fail(f"r{round_no:02d}-optimized-{task} 模式是 {mode}，期望 {expect}")
        elif scenario == "different_sets":
            if outcome != "completed":
                self.fail(f"different_sets 期望 completed，实际 {outcome}")
            if sorted(keyed) != [(1, "optimized", "A"), (1, "optimized", "B"), (1, "optimized", "C")]:
                self.fail(f"different_sets 的 run 集合不符：{sorted(keyed)}")
            if comparisons or timing:
                self.fail("different_sets 不应有 comparisons/timing")
            run_c = keyed.get((1, "optimized", "C"))
            if run_c and run_c["metrics"]["mode"] != "replay":
                self.fail("different_sets C 不是 replay")
        elif scenario in ("pause_resume", "stop_after_click"):
            self._check_control_scenario(result, keyed, rows)
        else:
            self.fail(f"未知场景：{scenario}")

    def _check_control_scenario(self, result: dict, keyed: dict, rows: list[dict]) -> None:
        scenario = result["scenario"]
        outcome = result["outcome"]
        pause_marks = [row for row in rows if row["status"] == "paused"]
        resumed_marks = [row for row in rows if row["status"] == "resumed"]
        stop_marks = [row for row in rows if row["status"] == "stop_requested"]
        if result.get("comparisons") or result.get("timing"):
            self.fail(f"{scenario} 不应有 comparisons/timing")
        if scenario == "pause_resume":
            if outcome != "completed":
                self.fail(f"pause_resume 期望 completed，实际 {outcome}")
            if sorted(keyed) != [(1, "optimized", "A"), (1, "optimized", "B")]:
                self.fail(f"pause_resume 的 run 集合不符：{sorted(keyed)}")
            if len(pause_marks) != 1 or len(resumed_marks) != 1:
                self.fail(f"pause_resume 的暂停/恢复标记数量不符：{len(pause_marks)}/{len(resumed_marks)}")
            elif resumed_marks[0]["t_ns"] - pause_marks[0]["t_ns"] < PAUSE_MIN_NS:
                self.fail("pause_resume 暂停区间不足 1 秒")
            else:
                between = [
                    row for row in rows
                    if pause_marks[0]["seq"] < row["seq"] < resumed_marks[0]["seq"]
                ]
                if any(row["event"] == "begin" for row in between):
                    self.fail("pause_resume 暂停期间出现新输入")
            run_b = keyed.get((1, "optimized", "B"))
            if run_b and run_b["metrics"]["mode"] != "replay":
                self.fail("pause_resume B 不是 replay")
            if not stop_marks and not pause_marks:
                self.fail("pause_resume 没有控制标记")
        else:
            if outcome != "stopped":
                self.fail(f"stop_after_click 期望 stopped，实际 {outcome}")
            if sorted(keyed) != [(1, "optimized", "A")]:
                self.fail(f"stop_after_click 只应有 A 成功 run：{sorted(keyed)}")
            if len(stop_marks) != 1:
                self.fail(f"stop_after_click 的 stop_requested 数量不是 1：{len(stop_marks)}")
                return
            stop = stop_marks[0]
            if rows and rows[-1]["seq"] != stop["seq"]:
                self.fail("stop_after_click 的 stop_requested 不是最后一行")
            if any(
                row["event"] == "begin" and row["seq"] > stop["seq"] for row in rows
            ):
                self.fail("stop_after_click 停止后仍有新输入")
            if any(
                row["seq"] > stop["seq"] and row["event"] != "end" for row in rows
            ):
                self.fail("stop_after_click 停止后出现多余记录")
            b_options = [
                row
                for row in rows
                if row["run_id"] == "r01-optimized-B"
                and row["role"] == "set_option"
                and row["event"] == "end"
                and row["status"] == "completed"
            ]
            if len(b_options) != 1:
                self.fail(f"stop_after_click 期望 B 恰有一个完成套装点击，实际 {len(b_options)}")
            a_options = [
                row
                for row in rows
                if row["run_id"] == "r01-optimized-A"
                and row["role"] == "set_option"
                and row["event"] == "end"
                and row["status"] == "completed"
            ]
            if b_options and a_options:
                first_a = min(a_options, key=lambda row: row["seq"])
                first_b = b_options[0]
                if (first_b["target"], first_b["x"], first_b["y"]) != (
                    first_a["target"], first_a["x"], first_a["y"]
                ):
                    self.fail("stop_after_click 的 B 首个套装点击与 A 不一致")

    # ---------------------------------------------------------------- 主流程
    def run(self, live_result: Path, review_path: Path) -> int:
        self.base_dir = live_result.parent
        result = self.load_json(live_result, "result.json")
        review = self.load_json(review_path, "review.json")
        if result is None or not isinstance(result, dict):
            self.report()
            return 1
        if set(result) != set(RESULT_FIELDS):
            self.fail(f"result.json 顶层字段不符：{sorted(set(result) ^ set(RESULT_FIELDS))}")
        if result.get("version") != 3:
            self.fail(f"result.json version 不是 3：{result.get('version')}")
        if result.get("input_file") != "inputs.jsonl":
            self.fail(f"result.json input_file 不是 inputs.jsonl：{result.get('input_file')}")
        scenario = result.get("scenario")
        if scenario not in SCENARIOS:
            self.fail(f"场景未知：{scenario}")
        self.scenario = scenario
        if result.get("outcome") not in ("completed", "failed", "stopped", "environment_unmet"):
            self.fail(f"outcome 未知：{result.get('outcome')}")
        if result.get("outcome") == "environment_unmet":
            self.fail("environment_unmet 不能取得场景验收通过")
            self.report()
            return 1
        rows = self.load_inputs(self.base_dir / "inputs.jsonl")
        runs = result.get("runs") or []
        for run in runs:
            missing = [key for key in RUN_FIELDS if key not in run]
            if missing:
                self.fail(f"run 缺字段 {missing}")
                continue
            if not isinstance(run.get("names"), list) or not isinstance(run.get("counts"), list):
                self.fail("run 的 names/counts 不是数组")
            if run["path"] not in ("legacy", "optimized") or run["task"] not in TASKS:
                self.fail(f"run 的 path/task 非法：{run['path']}/{run['task']}")
        if rows:
            self.check_input_rows(rows)
            self.check_run_ranges(rows, runs)
        self.check_snapshot_equality(runs)
        self.check_sets_consistency(runs)
        if scenario == "stop_after_click":
            for run in runs:
                self.check_run_contract(run, rows, allow_failed=True)
        else:
            for run in runs:
                self.check_run_contract(run, rows, allow_failed=False)
        self.check_review(review, runs)
        if scenario:
            self.check_scenario(result, runs, rows, review)
        self.report()
        return 1 if self.errors else 0

    def report(self) -> None:
        if not self.errors:
            print("[通过] 真机验收产物全部符合 §8.4 契约。")
            return
        print(f"[不通过] 共 {len(self.errors)} 项问题：")
        for message in self.errors[:40]:
            print("  - " + message)
        if len(self.errors) > 40:
            print(f"  ...（另有 {len(self.errors) - 40} 项）")


def check_static_files(base: Path = BASE, files: "tuple[str, ...]" = STATIC_FILES) -> list[str]:
    """§9 步骤6：固定清单逐个检查存在、UTF-8 可解码、行尾无空格或 tab、以换行结束（LF/CRLF 均可）。"""
    errors: list[str] = []
    for relative in files:
        path = base / relative
        if not path.exists():
            errors.append(f"{relative} 不存在")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"{relative} 不是 UTF-8：{exc}")
            continue
        if not text.endswith("\n"):
            errors.append(f"{relative} 未以换行结束")
        for number, line in enumerate(text.splitlines(), start=1):
            if line != line.rstrip(" \t"):
                errors.append(f"{relative}:{number} 行尾有空格或 tab")
    return errors


def check_manifest(path: Path = MANIFEST_PATH) -> list[str]:
    """§9 步骤6：清单版本 3、图片路径存在、布局引用一致、三个数组间 ID 唯一、身份源引用合法。"""
    errors: list[str] = []
    if not path.exists():
        return [f"{path.name} 不存在"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return [f"{path.name} 不是合法 UTF-8 JSON：{exc}"]
    if data.get("version") != 3:
        errors.append(f"{path.name} 版本不是 3：{data.get('version')!r}")
    layouts = data.get("layouts") or []
    layout_ids = [item.get("layout_id") for item in layouts]
    if not layouts:
        errors.append(f"{path.name} 缺少 layouts")
    if len(set(layout_ids)) != len(layout_ids):
        errors.append(f"{path.name} layouts 的 layout_id 重复")
    seen_ids: set[str] = set()
    ids_by_array: dict[str, set[str]] = {}
    for name in ("cases", "substat_cases", "name_span_cases"):
        items = data.get(name) or []
        ids = [item.get("id") for item in items]
        ids_by_array[name] = set(ids)
        if len(set(ids)) != len(ids):
            errors.append(f"{path.name} {name} 内 ID 重复")
        duplicates = seen_ids & set(ids)
        if duplicates:
            errors.append(f"{path.name} {name} 与其它数组 ID 冲突：{sorted(duplicates)[:3]}")
        seen_ids |= set(ids)
        for item in items:
            image = item.get("image")
            if image and not (BASE / image).exists():
                errors.append(f"{path.name} {name} 图片不存在：{image}")
            if name == "cases" and item.get("layout_id") not in layout_ids:
                errors.append(f"{path.name} cases 引用了未知布局：{item.get('layout_id')!r}")
    for item in data.get("cases") or []:
        source = item.get("identity_source_id")
        if source is not None and source not in ids_by_array.get("cases", set()):
            errors.append(f"{path.name} identity_source_id 引用不存在：{source}")
    for layout_id in layout_ids:
        if not any(item.get("layout_id") == layout_id for item in data.get("cases") or []):
            errors.append(f"{path.name} 布局 {layout_id!r} 没有任何 cases")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真机验收产物检查（spec §8.4 / §9 步骤6）")
    parser.add_argument("--live-result", help="<output-dir>/result.json")
    parser.add_argument("--review", help="<output-dir>/review.json")
    args = parser.parse_args(argv)
    if (args.live_result is None) != (args.review is None):
        parser.error("--live-result 与 --review 必须成对提供")

    static_errors = check_static_files() + check_manifest()
    if args.live_result is None:
        if static_errors:
            print(f"[不通过] 静态文件检查共 {len(static_errors)} 项问题：")
            for message in static_errors:
                print("  - " + message)
            return 1
        print("[通过] 固定清单与清单文件全部符合 §9 步骤6。")
        return 0

    checker = Checker()
    checker.errors.extend(static_errors)
    return checker.run(Path(args.live_result), Path(args.review))


if __name__ == "__main__":
    raise SystemExit(main())
