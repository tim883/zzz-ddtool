"""真机验收脚本（spec §8.4）。只有显式命令行才启动真实输入；pytest 不收集本文件。

    .venv/Scripts/python.exe tests/live_filter_replay.py --output-dir log/filter-replay-validation/run-01
    .venv/Scripts/python.exe tests/live_filter_replay.py --output-dir <新目录> --scenario different_sets

启动前准备（执行者负责）：游戏在驱动盘仓库页、窗口 1920×1080 dpi96（第 7 节标定布局）、
1 与 2 号位的目标套装各 1—36 件。脚本核对前提，不自行导航、换套装、改号位或处理盘；
所有真实输入走现有 HotkeyController 与检查点（不伪造 F9/F10），产物只写 output-dir。
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE / "src") not in sys.path:  # 只改本进程 import 路径，不动全局环境
    sys.path.insert(0, str(BASE / "src"))

from zzz_ddtool import gamefilter, navigation, wininput  # noqa: E402
from zzz_ddtool.capture import client_size, find_game_hwnd, window_pid  # noqa: E402
from zzz_ddtool.config import Preset  # noqa: E402
from zzz_ddtool.control import controlled_inputs  # noqa: E402
from zzz_ddtool.filter_replay import FilterEvidence, FilterReplaySession  # noqa: E402
from zzz_ddtool.hotkeys import HotkeyController, RunnerStopped  # noqa: E402
from zzz_ddtool.layout import LABEL_SCAN_AREA  # noqa: E402
from zzz_ddtool.logutil import get_logger  # noqa: E402
from zzz_ddtool.ocr import read_rect  # noqa: E402
from zzz_ddtool.runner import Runner  # noqa: E402
from zzz_ddtool.substatfilter import SubstatFilter, plan_substat_filter  # noqa: E402
from zzz_ddtool.traversal import (  # noqa: E402
    TraversalState,
    make_filter_signature,
    single_screen_verdict,
)

SCENARIOS = ("baseline", "different_sets", "pause_resume", "stop_after_click")
TASKS = ("A", "B", "C")
SLOTS = {"A": 1, "B": 2, "C": 1}
# 默认套装组合（spec §8.4 原值）。验收执行者可用 --sets 按环境另选一组：
# 条件是「筛完后该号位候选数落在 1—36」（单屏快照比较的环境前提）。
DEFAULT_SETS = ("河豚电音", "静听嘉音", "折枝剑歌")
EXPECTED_NAMES = {
    "A": {"攻击力%", "暴击伤害", "暴击率", "攻击力", "穿透值"},
    "B": {"攻击力%", "暴击伤害", "暴击率", "穿透值"},
    "C": {"攻击力%", "暴击伤害", "暴击率", "攻击力", "穿透值"},
}
EXPECTED_COUNTS = (3, 4)
WEIGHTS = {"攻击力%": 1.0, "暴击伤害": 1.0, "暴击率": 1.0, "攻击力": 1.0, "穿透值": 1.0}
WAREHOUSE_REGION = (0, 90, 700, 200)
WAREHOUSE_MARK = "驱动仓库"
CANDIDATE_MIN, CANDIDATE_MAX = 1, 36
OBSERVE_FRAMES = 3
PAUSE_MIN_SECONDS = 1.0
TRIGGER_TIMEOUT_SECONDS = 5.0
METRICS_PREFIX = "set_picker_metrics="
FULL_WINDOW_SIZE = (1920, 1080)
EXPECTED_DPI = 96


class AcceptanceError(RuntimeError):
    """场景不通过（普通失败，退出码 1）。"""


class EnvironmentUnmet(RuntimeError):
    """候选数量越界等环境不满足（退出码 4，非功能失败）。"""


@dataclass
class Item:
    """一个验收项：第 N 轮、legacy/optimized、号位任务 A/B/C。"""

    round: int
    path: str
    task: str
    sets: tuple[str, ...]
    preset: Preset
    plan: SubstatFilter

    @property
    def slot(self) -> int:
        return int(self.preset.filter_slot)

    @property
    def run_id(self) -> str:
        return f"r{self.round:02d}-{self.path}-{self.task}"


def _preset(slot: int) -> Preset:
    """验收专用 Preset（spec §8.4）：只供 plan_substat_filter 推导与初扫判读。"""
    return Preset(
        name="live-acceptance",
        weights=dict(WEIGHTS),
        filter_slot=slot,
        filter_mainstats=(),
        stage_require={"three": {0: 3.0}, "four": {0: 3.0}},
        min_useful_types=3,
        accepted=None,
    )


def _is_warehouse(image) -> bool:
    return any(
        WAREHOUSE_MARK in line.text.replace(" ", "") for line in read_rect(image, WAREHOUSE_REGION)
    )


class _MetricsCollector(logging.Handler):
    """从生产日志出口 `set_picker_metrics=` 采集指标（legacy 与 optimized 同一口径）。"""

    def __init__(self, sink: list) -> None:
        super().__init__(level=logging.INFO)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # 日志格式化异常不能影响验收主流程
            return
        if METRICS_PREFIX not in message:
            return
        payload = message.split(METRICS_PREFIX, 1)[1].strip()
        try:
            self.sink.append(json.loads(payload))
        except ValueError:
            self.sink.append({"parse_error": payload})


class Recorder:
    """inputs.jsonl 的唯一写入者：补 version/seq/run 关联，控制标记与输入共用一个序号分配器。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seq = 0
        self._pending: list[dict] = []
        self._lock = threading.Lock()
        self.run: dict | None = None
        self.ranges: dict[str, list[int]] = {}
        self.on_record = None

    # ---- 事件入口 ----
    def on_input(self, raw: dict) -> None:
        self._append(raw)

    def control(self, kind: str, t_ns: int | None = None) -> None:
        self._append(
            {
                "t_ns": int(t_ns if t_ns is not None else time.monotonic_ns()),
                "event": "control",
                "call_id": None,
                "api": None,
                "role": None,
                "target": None,
                "stage_index": None,
                "selected": None,
                "hwnd": None,
                "x": None,
                "y": None,
                "alt": None,
                "vk": None,
                "delta_units": None,
                "status": kind,
                "error": None,
            }
        )

    # ---- run 关联 ----
    def begin_run(self, item: Item) -> None:
        self.flush()
        self.run = {"run_id": item.run_id, "round": item.round, "path": item.path, "task": item.task}
        self.ranges[item.run_id] = [self._seq + 1, self._seq + 1]

    def end_run(self) -> None:
        self.flush()
        self.run = None

    def run_range(self, run_id: str) -> list[int]:
        return list(self.ranges.get(run_id, [0, 0]))

    # ---- 写盘 ----
    def flush(self) -> None:
        with self._lock:
            rows, self._pending = self._pending, []
        if not rows:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _append(self, raw: dict) -> None:
        with self._lock:
            self._seq += 1
            run = self.run
            row = {
                "version": 1,
                "seq": self._seq,
                "t_ns": int(raw.get("t_ns") or time.monotonic_ns()),
                "run_id": run["run_id"] if run else None,
                "round": run["round"] if run else None,
                "path": run["path"] if run else None,
                "task": run["task"] if run else None,
                "event": raw.get("event"),
                "call_id": raw.get("call_id"),
                "api": raw.get("api"),
                "role": raw.get("role"),
                "target": raw.get("target"),
                "stage_index": raw.get("stage_index"),
                "selected": raw.get("selected"),
                "hwnd": raw.get("hwnd"),
                "x": raw.get("x"),
                "y": raw.get("y"),
                "alt": raw.get("alt"),
                "vk": raw.get("vk"),
                "delta_units": raw.get("delta_units"),
                "status": raw.get("status"),
                "error": raw.get("error"),
            }
            self._pending.append(row)
            if run:
                span = self.ranges.setdefault(run["run_id"], [self._seq, self._seq])
                span[1] = self._seq
            hook = self.on_record
        if hook is not None:
            hook(row)


class Acceptance:
    """一次真机验收：把输入痕迹、证据帧、指标、快照与收尾产物收在一处。"""

    def __init__(self, output_dir: Path, scenario: str, sets: tuple[str, ...] = DEFAULT_SETS) -> None:
        self.output_dir = output_dir
        self.scenario = scenario
        self.sets = tuple(sets)
        self.recorder = Recorder(output_dir / "inputs.jsonl")
        self.controller: HotkeyController | None = None
        self.session: FilterReplaySession | None = None
        self.hwnd = 0
        self.width, self.height = FULL_WINDOW_SIZE
        self.runs: list[dict] = []
        self.comparisons: list[dict] = []
        self.timing: list[dict] = []
        self.snapshots: dict[tuple[int, str, str], dict] = {}
        self.environment: dict | None = None
        self.outcome = "failed"
        self.error: str | None = None
        self.stop_seen = False
        self.pause_marker_ns: int | None = None
        self.trigger = None
        self.triggered = False
        self.aux_thread: threading.Thread | None = None
        self.aux_error: str | None = None
        self._metrics: list[dict] = []
        self._paused_seen = False
        self._resumed_seen = False
        self.log = get_logger("zzz_ddtool.live_acceptance")

    # ---- 控制标记（§8.4：只记录状态，继续调用同一个 controller.checkpoint）----
    def note_control(self, kind: str) -> int:
        t_ns = time.monotonic_ns()
        self.recorder.control(kind, t_ns=t_ns)
        if kind == "paused":
            self.pause_marker_ns = t_ns
        self.log.info("控制标记 %s run=%s", kind, self.recorder.run)
        return t_ns

    def checkpoint(self) -> None:
        controller = self.controller
        assert controller is not None
        if controller.paused and not self._paused_seen:
            self._paused_seen = True
            self.note_control("paused")
        try:
            controller.checkpoint()
        except RunnerStopped:
            if not self.stop_seen:
                self.stop_seen = True
                self.note_control("stop_requested")
            raise
        if self._paused_seen and not self._resumed_seen and not controller.paused:
            self._resumed_seen = True
            self.note_control("resumed")

    # ---- 前提与守卫 ----
    def guard(self) -> None:
        hwnd = find_game_hwnd()
        if not hwnd:
            raise AcceptanceError("找不到游戏窗口，先在驱动盘仓库页打开游戏。")
        self.hwnd = int(hwnd)
        size = tuple(int(v) for v in client_size(self.hwnd))
        if size != FULL_WINDOW_SIZE:
            raise AcceptanceError(f"客户区 {size} 不是第 7 节标定尺寸 {FULL_WINDOW_SIZE}。")
        dpi = gamefilter._window_dpi(self.hwnd)
        if dpi != EXPECTED_DPI:
            raise AcceptanceError(f"窗口 DPI={dpi} 不是标定值 {EXPECTED_DPI}。")
        image, _ = gamefilter._grab()
        if gamefilter._picker_popup_open(image) is True:
            raise AcceptanceError("套装弹窗还开着：先按 Esc 关掉弹窗（脚本不代按）。")
        if gamefilter._panel_visible():
            raise AcceptanceError("右侧筛选面板还开着：先按 Esc 关掉面板（脚本自己会开，不能预开）。")
        if not _is_warehouse(image):
            raise AcceptanceError("当前不在驱动盘仓库页，脚本不导航、不处理盘。")

    def make_item(
        self, round_no: int, path: str, task: str, *, sets: tuple[str, ...] | None = None
    ) -> Item:
        preset = _preset(SLOTS[task])
        plan = plan_substat_filter(preset)
        get_logger("zzz_ddtool.gamefilter").info("自动筛选：%s", plan.summary())
        if set(plan.names) != EXPECTED_NAMES[task] or tuple(plan.counts) != EXPECTED_COUNTS:
            raise AcceptanceError(
                f"副属性推导不符（{task}）：期望 {sorted(EXPECTED_NAMES[task])}+{EXPECTED_COUNTS}，"
                f"实际 {plan.summary()}"
            )
        chosen = tuple(self.sets if sets is None else sets)
        if not chosen:
            raise AcceptanceError(f"验收项 {task} 的套装组合为空")
        return Item(round_no, path, task, chosen, preset, plan)

    # ---- 单项执行 ----
    def run_item(self, item: Item) -> None:
        evidence: list[FilterEvidence] = []
        metrics_before = len(self._metrics)
        frames: list[dict] = []
        self.recorder.begin_run(item)
        try:
            with gamefilter._capture_filter_evidence(evidence.append):
                gamefilter.apply_game_filter(
                    self.hwnd,
                    self.width,
                    self.height,
                    slots=(item.slot,),
                    sets=item.sets,
                    mainstats=(),
                    substats=item.plan.names,
                    substat_counts=item.plan.counts,
                    replay_session=self.session if item.path == "optimized" else None,
                )
        finally:
            self.recorder.end_run()
            if evidence:  # 失败或停止已取得的帧照常保存
                frames = self._write_frames(item, evidence)
        captured = self._metrics[metrics_before:]
        metrics = captured[-1] if captured else None
        if metrics is None:
            raise AcceptanceError(f"{item.run_id} 没有采到 set_picker_metrics 指标。")
        status, snapshot = self._warehouse(item)
        if status == "environment_unmet":
            raise EnvironmentUnmet(self.error or f"{item.run_id} 候选数量越界")
        if status != "ok" or snapshot is None:
            raise AcceptanceError(f"{item.run_id} 仓库快照不成立：{self.error}")
        kinds = [frame["event"] for frame in frames]
        if "set_stage_selected" not in kinds or kinds.count("substats_selected") != 1:
            raise AcceptanceError(f"{item.run_id} 证据帧不完整：{kinds}")
        self.snapshots[(item.round, item.path, item.task)] = snapshot
        span = self.recorder.run_range(item.run_id)
        self.runs.append(
            {
                "round": item.round,
                "path": item.path,
                "task": item.task,
                "slot": item.slot,
                "sets": list(item.sets),
                "names": list(item.plan.names),
                "counts": list(item.plan.counts),
                "metrics": metrics,
                "snapshot": snapshot,
                "image": f"{item.run_id}-warehouse.png",
                "evidence": frames,
                "inputs": {
                    "file": "inputs.jsonl",
                    "run_id": item.run_id,
                    "first_seq": span[0],
                    "last_seq": span[1],
                },
            }
        )
        self.log.info(
            "%s 完成 mode=%s 套装OCR=%s 轮询帧=%s 证据帧=%s",
            item.run_id,
            metrics.get("mode"),
            metrics.get("search_ocr_calls"),
            metrics.get("verification_frames"),
            len(frames),
        )

    def _write_frames(self, item: Item, evidence: list) -> list[dict]:
        """成功帧与失败/停止已取得的帧都写盘；PNG 写盘在筛选调用之外，不计入 elapsed_ms。"""
        entries: list[dict] = []
        for event in evidence:
            if event.event == "set_stage_selected" and event.stage_index is not None:
                name = f"{item.run_id}-sets-{int(event.stage_index):02d}.png"
            elif event.event == "substats_selected":
                name = f"{item.run_id}-substats.png"
            else:
                continue
            event.image.save(str(self.output_dir / name))
            entries.append(
                {
                    "event": event.event,
                    "stage_index": event.stage_index,
                    "targets": list(event.targets),
                    "boxes": [list(box) for box in event.boxes],
                    "image": name,
                }
            )
        return entries

    def _warehouse(self, item: Item) -> tuple[str, dict | None]:
        """抓仓库帧 → 确认页面 → 读总数 → 1—36 时做单屏快照。"""
        frame = None
        lines = None
        total = None
        for _attempt in range(OBSERVE_FRAMES):
            image, _ = gamefilter._grab()
            if not _is_warehouse(image):
                self.error = f"{item.run_id} 筛选后面板外页面不符（不在驱动盘仓库）"
                return "failed", None
            lines = read_rect(image, LABEL_SCAN_AREA)
            total = navigation.parse_warehouse_count(
                lines, width=image.width(), height=image.height()
            )
            if total is not None:
                frame = image
                break
            time.sleep(0.4)
        if frame is None or lines is None or total is None:
            self.error = f"{item.run_id} 仓库总数读取未知（已重拍 {OBSERVE_FRAMES} 帧）"
            return "failed", None
        image_name = f"{item.run_id}-warehouse.png"
        frame.save(str(self.output_dir / image_name))
        if total < CANDIDATE_MIN or total > CANDIDATE_MAX:
            if total == 0 and not navigation.is_empty_warehouse(frame):
                self.error = f"{item.run_id} 总数 0 但页面不是空仓库"
                return "failed", None
            self.environment = {
                "round": item.round,
                "path": item.path,
                "task": item.task,
                "total": int(total),
                "reason": "candidate_count_out_of_range",
                "image": image_name,
            }
            return "environment_unmet", None
        origin, pitch = navigation.measure_layout(frame, lines=lines)
        state = TraversalState(
            job_id=f"live-{item.round}-{item.path}-{item.task}",
            filter_signature=make_filter_signature(
                slot=item.slot,
                sets=item.sets,
                mainstats=(),
                tag=f"substats={','.join(item.plan.names)};minimum={item.plan.minimum}",
            ),
        )
        state.confirm_viewport(0, 0.0, row_pitch_px=pitch, at_top=True, at_bottom=True)
        runner = Runner(item.preset, hotkeys=False)
        runner._origin_center_y = float(origin)
        runner._row_pitch = float(pitch)
        cells, problems, skips = runner._scan_cells(state, image=frame, lines=lines)
        ok, reason = single_screen_verdict(
            cells, runner._last_empties, runner._last_unclear, total_count=int(total)
        )
        snapshot = {
            "total": int(total),
            "cells": [
                [cell.global_index, cell.global_row0, cell.global_col0, cell.level]
                for cell in sorted(cells, key=lambda cell: cell.global_index)
            ],
            "skips": [[index, text] for index, text in sorted(skips.items())],
            "empties": [[row, col] for row, col in sorted(runner._last_empties)],
            "problems": [str(problem) for problem in problems],
        }
        if not ok or snapshot["problems"]:
            self.error = f"{item.run_id} 单屏完整性不成立（{reason}）：{snapshot['problems'][:3]}"
            return "failed", None
        return "ok", snapshot

    # ---- 场景收尾 ----
    def compute_comparisons(self) -> list[dict]:
        rows: list[dict] = []
        for round_no in (1, 2, 3):
            for task in TASKS:
                legacy = self.snapshots.get((round_no, "legacy", task))
                optimized = self.snapshots.get((round_no, "optimized", task))
                rows.append(
                    {"round": round_no, "task": task, "equal": legacy is not None and legacy == optimized}
                )
        return rows

    def compute_timing(self) -> list[dict]:
        rows: list[dict] = []
        for task in ("B", "C"):
            legacy = [
                run["metrics"]["elapsed_ms"]
                for run in self.runs
                if run["task"] == task and run["path"] == "legacy" and run["metrics"]
            ]
            replay = [
                run["metrics"]["elapsed_ms"]
                for run in self.runs
                if run["task"] == task
                and run["path"] == "optimized"
                and run["metrics"].get("mode") == "replay"
            ]
            if len(legacy) != 3 or len(replay) != 3:
                raise AcceptanceError(
                    f"耗时样本数不对（{task}）：legacy={len(legacy)} replay={len(replay)}"
                )
            legacy_median = int(statistics.median(legacy))
            replay_median = int(statistics.median(replay))
            rows.append(
                {
                    "task": task,
                    "legacy_median_ms": legacy_median,
                    "replay_median_ms": replay_median,
                    "faster": replay_median < legacy_median,
                }
            )
        return rows

    def modes(self) -> dict[str, str]:
        return {
            f"r{run['round']:02d}-{run['path']}-{run['task']}": str(run["metrics"].get("mode"))
            for run in self.runs
        }

    # ---- 产物 ----
    def write_result(self) -> Path:
        payload = {
            "version": 3,
            "input_file": "inputs.jsonl",
            "scenario": self.scenario,
            "outcome": self.outcome,
            "environment": self.environment,
            "runs": self.runs,
            "comparisons": self.comparisons,
            "timing": self.timing,
        }
        path = self.output_dir / "result.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_review_skeleton(self) -> Path:
        """按成功 run 建 review.json 骨架；sets_exact/counts_exact 必须由看图核对填写。"""
        entries = [
            {
                "round": run["round"],
                "path": run["path"],
                "task": run["task"],
                "sets_exact": None,
                "counts_exact": None,
                "evidence_images": [item["image"] for item in run["evidence"]],
                "note": "",
            }
            for run in self.runs
        ]
        payload = {"version": 1, "entries": entries}
        path = self.output_dir / "review.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ---- 触发（R29：脚本自己调控制器，不发送 F9/F10）----
    def on_record(self, row: dict) -> None:
        if self.trigger is None or self.triggered:
            return
        if (
            row.get("event") == "end"
            and row.get("role") == "set_option"
            and row.get("status") == "completed"
        ):
            self.triggered = True
            self.trigger(row)

    def arm_pause_trigger(self, item_b: Item) -> None:
        def _trigger(row: dict) -> None:
            if row.get("run_id") != item_b.run_id:
                self.triggered = False
                return
            assert self.controller is not None
            if self.controller.toggle_pause() is not True:
                self.aux_error = "触发时 toggle_pause() 未返回 true"
                return
            self.log.info("暂停触发：B 首个套装点击后已请求暂停")
            self.aux_thread = threading.Thread(target=self._resume_when_paused, daemon=True)
            self.aux_thread.start()

        self.trigger = _trigger

    def _resume_when_paused(self) -> None:
        assert self.controller is not None
        deadline = time.monotonic() + TRIGGER_TIMEOUT_SECONDS
        while self.pause_marker_ns is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.pause_marker_ns is None:
            self.aux_error = f"{TRIGGER_TIMEOUT_SECONDS:.0f} 秒内未观察到 control/paused"
            self.controller.request_stop()  # 只用停止解除等待收尾
            return
        while (time.monotonic_ns() - self.pause_marker_ns) < int(PAUSE_MIN_SECONDS * 1e9):
            time.sleep(0.02)
        if self.controller.toggle_pause() is not False:
            self.aux_error = "恢复调用 toggle_pause() 未返回 false"
            self.controller.request_stop()
            return
        self.log.info("暂停触发：已恢复")

    def arm_stop_trigger(self, item_b: Item) -> None:
        def _trigger(row: dict) -> None:
            if row.get("run_id") != item_b.run_id:
                self.triggered = False
                return
            assert self.controller is not None
            self.controller.request_stop()
            self.log.info("停止触发：B 首个套装点击后已请求停止")

        self.trigger = _trigger

    def join_aux(self) -> None:
        if self.aux_thread is not None:
            self.aux_thread.join(timeout=TRIGGER_TIMEOUT_SECONDS + PAUSE_MIN_SECONDS + 5.0)
            if self.aux_thread.is_alive():
                self.aux_error = "辅助线程未结束"
            self.aux_thread = None

    def close(self) -> None:
        self.join_aux()
        self.session = None
        if self.controller is not None:
            self.controller.close()
            self.controller = None
        self.recorder.flush()
        self.log.info("验收脚本退出：scenario=%s outcome=%s error=%s", self.scenario, self.outcome, self.error)


# ---------------------------------------------------------------- 场景


def scenario_baseline(acc: Acceptance) -> None:
    orders = {1: ("legacy", "optimized"), 2: ("optimized", "legacy"), 3: ("legacy", "optimized")}
    for round_no in (1, 2, 3):
        acc.session = FilterReplaySession()
        try:
            for path in orders[round_no]:
                for task in TASKS:
                    acc.run_item(acc.make_item(round_no, path, task))
        finally:
            acc.session = None
    modes = acc.modes()
    for round_no in (1, 2, 3):
        for task in TASKS:
            legacy_mode = modes[f"r{round_no:02d}-legacy-{task}"]
            optimized_mode = modes[f"r{round_no:02d}-optimized-{task}"]
            if legacy_mode != "legacy":
                raise AcceptanceError(f"r{round_no:02d}-legacy-{task} 模式为 {legacy_mode}")
            expect = "learn" if task == "A" else "replay"
            if optimized_mode != expect:
                raise AcceptanceError(f"r{round_no:02d}-optimized-{task} 模式为 {optimized_mode}，期望 {expect}")
    for run in acc.runs:
        if run["path"] == "optimized" and run["task"] in ("B", "C"):
            if int(run["metrics"].get("search_ocr_calls") or 0) != 0:
                raise AcceptanceError(f"r{run['round']:02d}-optimized-{run['task']} 回放仍有名称查找 OCR")
            if not run["evidence"]:
                raise AcceptanceError(f"r{run['round']:02d}-optimized-{run['task']} 没有套装证据帧")
    acc.comparisons = acc.compute_comparisons()
    if not all(row["equal"] for row in acc.comparisons):
        bad = [f"r{row['round']:02d}-{row['task']}" for row in acc.comparisons if not row["equal"]]
        raise AcceptanceError(f"legacy 与 optimized 快照不相等：{bad}")
    acc.timing = acc.compute_timing()
    if not all(row["faster"] for row in acc.timing):
        raise AcceptanceError("B/C 的 replay 中位数没有比 legacy 快")
    acc.outcome = "completed"


def scenario_different_sets(acc: Acceptance) -> None:
    acc.session = FilterReplaySession()
    sets_b = (acc.sets[0],)  # B 用单套装变体：必须与 A/C 不同组合，单独学习
    for task in TASKS:
        acc.run_item(acc.make_item(1, "optimized", task, sets=sets_b if task == "B" else acc.sets))
    modes = acc.modes()
    if modes["r01-optimized-A"] != "learn" or modes["r01-optimized-B"] != "learn":
        raise AcceptanceError(f"different_sets 期望 A/B=learn，实际 {modes}")
    if modes["r01-optimized-C"] != "replay":
        raise AcceptanceError(f"different_sets 期望 C=replay，实际 {modes['r01-optimized-C']}")
    acc.recorder.flush()
    rows = list(_read_rows(acc.recorder.path))

    def _targets(run_id: str) -> set:
        return {
            row["target"]
            for row in rows
            if row.get("run_id") == run_id
            and row.get("role") == "set_option"
            and row.get("event") == "end"
            and row.get("status") == "completed"
        }

    if _targets("r01-optimized-B") != set(sets_b):
        raise AcceptanceError(f"different_sets B 实际选择的套装不是单套装变体 {sets_b}：{_targets('r01-optimized-B')}")
    if _targets("r01-optimized-C") != set(acc.sets):
        raise AcceptanceError(f"different_sets C 没有按原组合回放 {acc.sets}：{_targets('r01-optimized-C')}")
    if int(acc.runs[-1]["metrics"].get("search_ocr_calls") or 0) != 0:
        raise AcceptanceError("different_sets C 回放仍有名称查找 OCR")
    acc.outcome = "completed"


def _prepare_two_items(acc: Acceptance) -> tuple[Item, Item]:
    acc.session = FilterReplaySession()
    item_a = acc.make_item(1, "optimized", "A")
    acc.run_item(item_a)
    if acc.modes()["r01-optimized-A"] != "learn":
        raise AcceptanceError("A 应为 learn")
    return item_a, acc.make_item(1, "optimized", "B")


def _replay_contract(acc: Acceptance, run_id: str) -> dict:
    run = next((row for row in acc.runs if f"r{row['round']:02d}-{row['path']}-{row['task']}" == run_id), None)
    if run is None:
        raise AcceptanceError(f"{run_id} 没有成功 run")
    if run["metrics"].get("mode") != "replay":
        raise AcceptanceError(f"{run_id} 模式不是 replay")
    if int(run["metrics"].get("search_ocr_calls") or 0) != 0:
        raise AcceptanceError(f"{run_id} 回放仍有名称查找 OCR")
    return run


def scenario_pause_resume(acc: Acceptance) -> None:
    item_a, item_b = _prepare_two_items(acc)
    acc.arm_pause_trigger(item_b)
    try:
        acc.run_item(item_b)
    except RunnerStopped as exc:
        raise AcceptanceError("pause_resume 被停止中断（应为暂停后恢复）") from exc
    finally:
        acc.join_aux()
    if acc.aux_error:
        raise AcceptanceError(f"pause_resume 触发失败：{acc.aux_error}")
    if not acc.triggered:
        raise AcceptanceError("pause_resume 未命中 B 的首个套装点击")
    if acc.pause_marker_ns is None:
        raise AcceptanceError("pause_resume 没有 control/paused 标记")
    rows = list(_read_rows(acc.recorder.path))
    paused = next(row for row in rows if row.get("status") == "paused")
    resumed = next((row for row in rows if row.get("status") == "resumed"), None)
    if resumed is None:
        raise AcceptanceError("pause_resume 没有 control/resumed 标记")
    if (resumed["t_ns"] - paused["t_ns"]) < int(PAUSE_MIN_SECONDS * 1e9):
        raise AcceptanceError("pause_resume 暂停区间不足 1 秒")
    between = [row for row in rows if paused["seq"] < row["seq"] < resumed["seq"]]
    if any(row["event"] == "begin" for row in between):
        raise AcceptanceError("pause_resume 暂停期间出现新输入")
    run_b = _replay_contract(acc, "r01-optimized-B")
    options = [
        row
        for row in rows
        if row.get("run_id") == "r01-optimized-B"
        and row.get("role") == "set_option"
        and row.get("event") == "end"
        and row.get("status") == "completed"
    ]
    first_a = [
        row
        for row in rows
        if row.get("run_id") == "r01-optimized-A"
        and row.get("role") == "set_option"
        and row.get("event") == "end"
        and row.get("status") == "completed"
    ]
    if not options or not first_a:
        raise AcceptanceError("暂停场景缺少套装点击记录")
    if (options[0]["target"], options[0]["x"], options[0]["y"]) != (
        first_a[0]["target"], first_a[0]["x"], first_a[0]["y"],
    ):
        raise AcceptanceError("B 首个套装点击与 A 不一致")
    if sum(1 for row in options if row["stage_index"] and row["x"] == first_a[0]["x"] and row["y"] == first_a[0]["y"]) != 1:
        raise AcceptanceError("恢复后重复点击了 B 的首个套装")
    if not run_b["evidence"]:
        raise AcceptanceError("pause_resume B 缺少证据帧")
    acc.outcome = "completed"


def scenario_stop_after_click(acc: Acceptance) -> None:
    item_a, item_b = _prepare_two_items(acc)
    acc.arm_stop_trigger(item_b)
    try:
        acc.run_item(item_b)
    except RunnerStopped:
        pass
    finally:
        acc.join_aux()
    if acc.aux_error:
        raise AcceptanceError(f"stop_after_click 触发失败：{acc.aux_error}")
    if not acc.triggered:
        raise AcceptanceError("stop_after_click 未命中 B 的首个套装点击")
    rows = list(_read_rows(acc.recorder.path))
    if any(row.get("run_id") == "r01-optimized-B" and row["event"] == "begin" and row["seq"] > _stop_seq(rows) for row in rows):
        raise AcceptanceError("stop_after_click 停止后仍有新输入")
    if rows and rows[-1].get("status") != "stop_requested":
        raise AcceptanceError("stop_after_click 的 stop_requested 不是完整输入文件最后一行")
    b_options = [
        row
        for row in rows
        if row.get("run_id") == "r01-optimized-B"
        and row.get("role") == "set_option"
        and row.get("event") == "end"
        and row.get("status") == "completed"
    ]
    if len(b_options) != 1:
        raise AcceptanceError(f"stop_after_click 期望 B 恰有一个完成的套装点击，实际 {len(b_options)}")
    if not any(row.get("run_id") == "r01-optimized-A" for row in rows):
        raise AcceptanceError("stop_after_click 缺少 A 的输入记录")
    acc.outcome = "stopped"


def _stop_seq(rows: list[dict]) -> int:
    for row in rows:
        if row.get("status") == "stop_requested":
            return int(row["seq"])
    return 10**9


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="筛选提速与套装路径回放真机验收（spec §8.4）")
    parser.add_argument("--output-dir", required=True, help="必须为空或不存在；重复验收换新目录")
    parser.add_argument("--scenario", default="baseline", choices=SCENARIOS)
    parser.add_argument(
        "--sets",
        default=",".join(DEFAULT_SETS),
        help="验收用套装组合（逗号分隔）。默认是 spec §8.4 原值；"
        "执行者可按当前环境另选一组，前提是筛完后该号位候选数落在 1—36（单屏）。",
    )
    args = parser.parse_args(argv)

    chosen_sets = tuple(part.strip() for part in args.sets.split(",") if part.strip())
    if not chosen_sets:
        print("[拒绝] --sets 至少要有一个套装名。", file=sys.stderr)
        return 1
    if args.scenario == "different_sets" and len(chosen_sets) < 2:
        print("[拒绝] different_sets 需要至少两个套装名（B 用单套装变体对照）。", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir)
    if output_dir.exists():
        if output_dir.is_file() or any(output_dir.iterdir()):
            print(f"[拒绝] 输出目录必须为空或不存在：{output_dir}", file=sys.stderr)
            return 1
    else:
        output_dir.mkdir(parents=True)
    output_dir = output_dir.resolve()

    acc = Acceptance(output_dir, args.scenario, chosen_sets)
    logger = get_logger("zzz_ddtool.gamefilter")
    collector = _MetricsCollector(acc._metrics)
    logger.addHandler(collector)
    exit_code = 1
    try:
        acc.guard()
        acc.controller = HotkeyController()
        acc.recorder.on_record = acc.on_record
        with wininput.capture_inputs(acc.recorder.on_input), controlled_inputs(acc.checkpoint):
            {
                "baseline": scenario_baseline,
                "different_sets": scenario_different_sets,
                "pause_resume": scenario_pause_resume,
                "stop_after_click": scenario_stop_after_click,
            }[args.scenario](acc)
    except EnvironmentUnmet as exc:
        acc.outcome = "environment_unmet"
        acc.error = str(exc)
    except RunnerStopped as exc:
        acc.outcome = "failed"  # 非 stop_after_click 精确模式的停止不通过
        acc.error = f"顶层捕获 RunnerStopped：{exc}"
    except AcceptanceError as exc:
        acc.outcome = "failed"
        acc.error = str(exc)
    except Exception as exc:  # 意外异常同样写盘后非零退出
        acc.outcome = "failed"
        acc.error = f"{type(exc).__name__}: {exc}"
    finally:
        acc.close()
        logger.removeHandler(collector)
        result_path = acc.write_result()
        review_path = acc.write_review_skeleton()
        if acc.outcome == "completed":
            exit_code = 0
        elif acc.outcome == "stopped":
            exit_code = 2
        elif acc.outcome == "environment_unmet":
            exit_code = 4
        else:
            exit_code = 1
        print(
            f"[完成] scenario={args.scenario} outcome={acc.outcome} 退出码={exit_code}\n"
            f"  result.json={result_path}\n  review.json={review_path}（sets_exact/counts_exact 需逐图核对）"
            + (f"\n  失败原因：{acc.error}" if acc.error else "")
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
