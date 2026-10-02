"""自动化主循环：按任务链导航、筛选、OCR、决策、点击。

所有真实交互都通过 nav / navigation / gamefilter / wininput / ocr 走，本层只负责编排。
位置模型见 traversal.py（纯计算），滚轮协议与短距标定见 navigation.py。

全局位置依据（spec v0.8 §4/§5）：
- 行号由几何反推（原点 + 行距 + 相位），不用 OCR 聚类序号；
- 视口来自置顶原点、实测滚轮协议与累计行数；
- 遍历、到顶/到底、结束判断与恢复放行全程不读滑块。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace

from PySide6.QtGui import QImage

from zzz_ddtool.capture import find_game_hwnd, grab_game_image, window_pid
from zzz_ddtool.chain import TaskJob
from zzz_ddtool.config import Preset
from zzz_ddtool.decide import decide
from zzz_ddtool.gamefilter import apply_game_filter
from zzz_ddtool.hotkeys import HotkeyController, RunnerStopped
from zzz_ddtool.layout import LABEL_SCAN_AREA
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.models import MAX_LEVEL, Action, Decision, Disc
from zzz_ddtool.nav import NavError, goto_warehouse, reset_to_top
from zzz_ddtool.navigation import (
    NavProfile,
    calibrate,
    load_profile,
    measure_layout,
    probe_environment,
    save_profile,
    scroll_rows,
    settle_and_read_anchor,
    anchor_screen_rows,
)
from zzz_ddtool.ocr import OcrError, parse_disc
from zzz_ddtool.trace import Trace, action_name as trace_action, page_name as trace_page
from zzz_ddtool.traversal import (
    AnchorObservation,
    CellStatus,
    GridCell,
    GridMetrics,
    PositionError,
    TraversalState,
    check_forward_move,
    check_layout_reference,
    make_filter_signature,
    map_labels,
    max_restore_rows,
    split_stable_cells,
)
from zzz_ddtool.wininput import VK_ESCAPE, VK_R, click_client, press_key, press_key_once

# 单个盘子连续升阶上限（0→15 最多 5 阶，留余量防 OCR 读级错误导致死循环）
_MAX_STAGE_UPGRADES = 6
# 正常前进每次请求的行数上界（spec §5.1：只允许 1 或 2 行，维持 H）
_FORWARD_ROWS = 2


class RunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    job_id: str
    action: str
    reason: str
    score: float
    level: int


class Runner:
    """执行一个 Preset。任务链为空时，用 preset 当前槽位合成一个默认任务。"""

    def __init__(self, preset: Preset, *, hotkeys: bool = True, controller=None, on_status=None) -> None:
        self.preset = preset
        self.log = get_logger("zzz_ddtool.runner")
        self.trace = Trace(tag=time.strftime("%Y%m%d-%H%M%S"))
        self.results: list[RunResult] = []
        self._processed = 0
        self._scan_image = None  # 最近一次整页扫描的图（存盘用）
        self.hotkeys = controller if controller is not None else (HotkeyController() if hotkeys else None)
        self.on_status = on_status
        self.outcome = "idle"
        self.end_reason = ""
        self._detail_disc = None
        # 本任务绑定实例与实测布局
        self._hwnd: int | None = None
        self._pid: int | None = None
        self._state: TraversalState | None = None
        self._profile: NavProfile | None = None
        self._origin_center_y: float = 0.0
        self._row_pitch: float = 175.0
        self._selected_cell: GridCell | None = None

    @property
    def processed_disc_count(self) -> int:
        return self._processed

    def _checkpoint(self) -> None:
        """F9 暂停 / F10 停止的安全检查点（未启用热键时为空操作）。"""
        if self.hotkeys is not None:
            paused = self.hotkeys.paused
            if paused:
                self._set_status("paused", "已暂停，F9 或恢复按钮继续")
            self.hotkeys.checkpoint()
            if paused:
                self._set_status("running", "已恢复")

    _status_lines: list[str] = []

    def _set_status(self, phase: str, message: str = "", cell: str = "") -> None:
        """运行状态三处落点：run-status.json + 游戏画面悬浮层 + 日志。"""
        stamp = time.strftime("%H:%M:%S")
        line = " ".join(x for x in (stamp, cell, phase, message) if x)
        self._status_lines.append(line)
        del self._status_lines[:-8]
        payload = {
            "time": stamp,
            "phase": phase,
            "cell": cell,
            "message": message,
            "processed": self._processed,
            "max_discs": self.preset.max_discs,
        }
        if self._state is not None:
            payload.update(self._state.status_payload())
        if self.on_status is not None:
            self.on_status(dict(payload))
        try:
            import json

            from zzz_ddtool.paths import status_file

            status_file().parent.mkdir(parents=True, exist_ok=True)
            temp = status_file().with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(status_file())
        except Exception:
            self.log.exception("运行状态写入失败")
        self.log.info("状态 %s：%s %s", phase, cell, message)

    # ---------------------------------------------------------------- 实例绑定

    def _bind_instance(self) -> int:
        """任务开始时选定并绑定唯一游戏实例（spec §5.1 第 1 条）。

        本任务的截图、输入、DPI 与 exe 身份都绑定同一 hwnd/PID，不因另一个
        窗口变大而重新选择。
        """
        hwnd = find_game_hwnd()
        self._hwnd = int(hwnd)
        self._pid = window_pid(self._hwnd)
        self.log.info("bound game instance hwnd=%s pid=%s", self._hwnd, self._pid)
        return self._hwnd

    def _grab(self, allow_esc: bool = True):
        image, method, hwnd = grab_game_image(self._hwnd)
        from zzz_ddtool.screen import classify_image

        page = classify_image(image)
        if page == "unknown" and allow_esc:
            # 可能筛选面板开着盖住认页图标：Esc 一下再认。
            # 注意：点格子后的详情页也返回 unknown，调用方须用 allow_esc=False。
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.5)
            image, method, hwnd = grab_game_image(self._hwnd)
            page = classify_image(image)
        return image, page, method, hwnd

    # ---------------------------------------------------------------- 任务与预设

    def _default_job(self) -> TaskJob:
        return TaskJob(
            slot=self.preset.filter_slot,
            sets=self.preset.filter_sets,
            mainstats=self.preset.filter_mainstats,
            weights=dict(self.preset.weights),
            stage_require={
                str(origin): {int(level): float(value) for level, value in table.items()}
                for origin, table in (self.preset.stage_require or {}).items()
            },
            accepted=self.preset.accepted,
            min_useful_types=self.preset.min_useful_types,
        )

    def _jobs(self) -> tuple[TaskJob, ...]:
        return self.preset.task_chain or (self._default_job(),)

    def _sub_preset(self, job: TaskJob) -> Preset:
        """用单个 job 的权重/门槛构造临时 Preset 给 decide 用。"""
        return Preset(
            weights=job.weights,
            stage_require=job.stage_require,
            min_useful_types=job.min_useful_types,
            require_s_rank=self.preset.require_s_rank,
            skip_locked=self.preset.skip_locked,
            skip_equipped=self.preset.skip_equipped,
            skip_discarded=self.preset.skip_discarded,
            accepted=job.accepted,
        )

    def run(self) -> list[RunResult]:
        """跑完整轮任务链，返回处理结果。F10 停止时返回已处理部分。"""
        if not self._valid_max_pages():
            self.outcome = "failed"
            self.end_reason = f"max_pages 非法：{self.preset.max_pages!r}，未发送任何游戏输入"
            self.log.error(self.end_reason)
            return []
        self.results = []
        self._processed = 0
        self._status_lines = []
        self._state = None
        self.outcome = "running"
        self.end_reason = ""
        jobs = self._jobs()
        import logging

        from zzz_ddtool.overlay import OverlayLogHandler

        overlay_handler = OverlayLogHandler()
        logging.getLogger("zzz_ddtool").addHandler(overlay_handler)
        self.log.info("run start jobs=%d max_discs=%d (F9 暂停 / F10 停止)", len(jobs), self.preset.max_discs)
        self._set_status("run_start", f"jobs={len(jobs)}")
        self.trace.ok("unknown", "run", f"共 {len(jobs)} 个任务，本轮最多处理 {self.preset.max_discs} 个盘")
        try:
            for job in jobs:
                if self._processed >= self.preset.max_discs:
                    self.trace.ok("unknown", "run", f"到上限 {self.preset.max_discs} 个，收工")
                    break
                self._checkpoint()
                self._run_job(job)
            self.outcome = "completed"
            self.end_reason = f"已处理 {self._processed} 个盘"
        except RunnerStopped:
            self.outcome = "stopped"
            self.end_reason = f"用户停止，已处理 {self._processed} 个盘"
            self.trace.ok("unknown", "run", self.end_reason)
        except Exception as exc:
            self.outcome = "failed"
            self.end_reason = str(exc)
            self.log.exception("run failed")
            self.trace.fail("unknown", "run", self.end_reason)
        finally:
            if self.hotkeys is not None:
                self.hotkeys.close()
            self.trace.finish()
            self._set_status(self.outcome, self.end_reason)
            logging.getLogger("zzz_ddtool").removeHandler(overlay_handler)
            overlay_handler.close()
        self.log.info("run %s processed=%d", self.outcome, self._processed)
        return list(self.results)

    def _valid_max_pages(self) -> bool:
        value = self.preset.max_pages
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    # ---------------------------------------------------------------- 主任务

    def _run_job(self, job: TaskJob) -> None:
        self.log.info("job start slot=%d sets=%s mains=%s", job.slot, job.sets, job.mainstats)
        hwnd = self._bind_instance()
        try:
            goto_warehouse()
            self.trace.ok("drive_disc", "navigate", "已到驱动盘仓库")
        except NavError as exc:
            self.trace.fail("unknown", "navigate", str(exc))
            raise RunnerError(f"导航失败：{exc}") from exc

        image, page, method, _hwnd = self._grab()
        apply_game_filter(
            hwnd,
            image.width(),
            image.height(),
            slots=[job.slot],
            sets=job.sets,
            mainstats=job.mainstats,
        )
        self.trace.ok(
            "drive_disc",
            "filter",
            f"{job.slot}号位"
            + (f" 套装 {'、'.join(job.sets)}" if job.sets else "")
            + (f" 主词条 {'、'.join(job.mainstats)}" if job.mainstats else ""),
        )
        time.sleep(0.3)

        sub_preset = self._sub_preset(job)
        accepted_ids = None if job.accepted is None else set(job.accepted)

        # 每任务新建状态：不继承上一任务进度（spec §4.3、§6.1）
        state = TraversalState(
            job_id=job.job_id,
            filter_signature=make_filter_signature(
                slot=job.slot, sets=job.sets, mainstats=job.mainstats
            ),
        )
        state.restore_request_limit_rows = max_restore_rows(self.preset.max_pages)
        self._state = state

        profile = self._prepare_navigation(hwnd, state)
        state.confirm_viewport(0, 0.0, row_pitch_px=self._row_pitch, at_top=True, at_bottom=False)
        state.start_first_batch()
        self._set_status("batch", "首批网格起点")

        up_counts: dict[int, int] = {}
        resume_after_selection = False

        while True:
            self._checkpoint()
            if self._processed >= self.preset.max_discs:
                break
            if state.work_batch_count > self.preset.max_pages:
                self.log.info(
                    "work_batch_count=%d 达到 max_pages=%d，收工", state.work_batch_count, self.preset.max_pages
                )
                break

            cells, problems, skips = self._scan_cells_with_retry(state)
            ready, deferred = split_stable_cells(
                cells, at_top=state.at_top, at_bottom=state.at_bottom
            )
            for cell in deferred:
                state.block(cell.global_index, CellStatus.DEFERRED)
            for index, reason in skips.items():
                state.set_status(index, CellStatus.SKIPPED)
                self.log.info("cell %d 扫描跳过：%s（仍占全局格位）", index, reason)
            state.commit_prefix()

            pending: list[GridCell] = []
            expected = state.next_index
            for cell in sorted(ready, key=lambda c: c.global_index):
                if cell.global_index < expected:
                    continue
                if cell.global_index != expected:
                    break  # 前方有未解决格位（屏障），不能越过
                pending.append(cell)
                expected += 1

            if resume_after_selection:
                resume_after_selection = False
                selected = self._selected_cell
                if selected is None:
                    self.trace.fail(
                        "drive_disc", "scan", "重扫未找到唯一选中框，停止以免跳盘", image=self._scan_image
                    )
                    raise RunnerError("重扫未找到唯一选中框，停止任务链")
                pending = [c for c in pending if c.global_index > selected.global_index]

            self.log.info(
                "batch %d: %d pending, %d skipped, %d deferred",
                state.work_batch_count,
                len(pending),
                len(skips),
                len(deferred),
            )
            self.trace.ok(
                "drive_disc",
                "scan",
                f"第 {state.work_batch_count} 批：待处理 {len(pending)}，跳过 {len(skips)}",
                image=self._scan_image,
            )
            self._set_status("scanning", f"batch {state.work_batch_count}: {len(pending)} cells")

            if not pending:
                if state.at_bottom:
                    self.log.info("已到底且无待处理格位，本任务结束")
                    break
                before_top = state.viewport_top_row0
                if not self._advance(hwnd, state, cells, rows=_FORWARD_ROWS):
                    self.log.info("无法继续前进，本任务结束")
                    break
                if state.viewport_top_row0 == before_top and not state.at_bottom:
                    self.log.warning("前进未改变视口且未到底，停止以避免空转")
                    break
                continue

            broke = False
            for cell in pending:
                self._checkpoint()
                if self._processed >= self.preset.max_discs:
                    break
                result = self._process_cell(
                    hwnd, job, state, cell, sub_preset, accepted_ids, up_counts
                )
                if result in ("rescan", "upgrade"):
                    broke = True
                    # 强化改变排序：本批坐标作废，重扫后从原焦点之后继续（阶段4接恢复）
                    resume_after_selection = result == "upgrade"
                    break
                state.set_status(cell.global_index, CellStatus.DONE)
                state.commit_prefix()
                self._set_status("progress", f"next_index={state.next_index}", self._cell_tag(cell))
            if broke:
                state.invalidate_position()
                continue
            if self._processed >= self.preset.max_discs:
                break
            if state.at_bottom:
                break
            before_top = state.viewport_top_row0
            if not self._advance(hwnd, state, cells, rows=_FORWARD_ROWS):
                break
            if state.viewport_top_row0 == before_top and not state.at_bottom:
                self.log.warning("前进未改变视口且未到底，停止以避免空转")
                break

    # ---------------------------------------------------------------- 导航准备

    def _prepare_navigation(self, hwnd: int, state: TraversalState) -> NavProfile:
        """置顶 → 复核原点布局 → 复用或标定协议 → 再次置顶（spec §5.1）。

        标定/检查结束必须再次置顶，才获取正式首批网格。
        """
        env = probe_environment(hwnd)
        reset_to_top()
        image, _method, _hwnd = grab_game_image(hwnd)
        origin, pitch = measure_layout(image)
        reason = check_layout_reference(origin, pitch, image.width(), image.height())
        if reason:
            self.trace.fail("drive_disc", "layout", f"网格布局不受支持：{reason}", image=image)
            raise RunnerError(f"网格布局不受支持：{reason}，停止任务链")
        self._origin_center_y = float(origin)
        self._row_pitch = float(pitch)

        profile = load_profile(env)
        if profile is None:
            if env.window_dpi is None:
                raise RunnerError("无法读取游戏窗口 DPI，未开始滚动标定")
            try:
                profile = calibrate(hwnd, env=env)
            except Exception as exc:
                raise RunnerError(f"滚轮标定失败：{exc}，停止任务链") from exc
            if env.persistable:
                try:
                    save_profile(profile)
                except OSError as exc:
                    self.log.warning("导航缓存写入失败（%s），仅本轮使用", exc)
        # 每任务以本轮实测布局为准（缓存不决定原点，spec §5.1 第 168 行）
        profile = replace(profile, row_pitch_px=self._row_pitch, origin_center_y=self._origin_center_y)
        self._profile = profile
        state.navigation_profile = profile.as_dict()
        reset_to_top()
        self.trace.ok(
            "drive_disc",
            "calibrate",
            f"原点 {self._origin_center_y:.1f} 行距 {self._row_pitch:.1f} "
            f"协议 {profile.events_per_row}事件/行@{profile.event_interval_ms}ms",
        )
        return profile

    # ---------------------------------------------------------------- 扫描

    def _cell_tag(self, cell: GridCell) -> str:
        row, col = cell.display()
        return f"R{row}C{col}"

    def _metrics(self, state: TraversalState, image) -> GridMetrics:
        return GridMetrics.from_client(
            image.width(),
            image.height(),
            origin_center_y=self._origin_center_y,
            row_pitch_px=self._row_pitch,
        )

    def _scan_cells(self, state: TraversalState):
        """扫描当前视口 → (全部原始格位, 问题, 扫描跳过表)。

        行号由几何反推，不用 OCR 聚类序号；未映射/重复归列进 problems，调用方
        当作进度屏障重读（spec §5.5），不当空位。满级/装备/弃置格仍占全局格位，
        只记入 skips，不删号。
        """
        from zzz_ddtool.equipped import (
            tile_has_discard_badge,
            tile_has_portrait,
            tile_is_selected,
        )
        from zzz_ddtool.ocr import read_rect

        image, page, _method, hwnd = self._grab(allow_esc=False)
        if page != "drive_disc":
            self.trace.fail(page, "scan", "当前不是驱动盘仓库，停止扫描", image=image)
            raise RunnerError(f"扫描前页面错误：{page}，停止任务链")
        self._scan_image = image

        width, height = image.width(), image.height()
        metrics = self._metrics(state, image)
        labels: list[tuple[float, float, int]] = []
        for line in read_rect(image, LABEL_SCAN_AREA):
            text = line.text.replace(" ", "")
            if not text.startswith("等级"):
                continue
            match = re.search(r"等级(\d+)", text)
            level = int(match.group(1)) if match else -1
            labels.append(
                (
                    float((line.left + line.right) // 2),
                    float((line.top + line.bottom) // 2),
                    level,
                )
            )

        cells, problems = map_labels(
            metrics,
            labels,
            viewport_top_row0=state.viewport_top_row0 or 0,
            row_phase_px=state.row_phase_px or 0.0,
        )

        selected: list[GridCell] = []
        skips: dict[int, str] = {}
        for cell in cells:
            design_cx = round(cell.center_x * 1920 / width)
            design_cy = round(cell.center_y * 1080 / height)
            if tile_is_selected(image, design_cx, design_cy):
                selected.append(cell)
                continue
            if self.preset.skip_max_level and cell.level >= MAX_LEVEL:
                skips[cell.global_index] = f"等级 {cell.level} 已满"
                continue
            if self.preset.skip_equipped and tile_has_portrait(
                image,
                cell.center_x - 56,
                cell.center_y - 68,
                cell.center_x + 56,
                cell.center_y + 68,
            ):
                skips[cell.global_index] = "已装备"
                continue
            if self.preset.skip_discarded and tile_has_discard_badge(
                image, cell.center_x, cell.center_y
            ):
                skips[cell.global_index] = "已打弃置标记"
                continue
        self._selected_cell = selected[0] if len(selected) == 1 else None
        return cells, problems, skips

    def _scan_cells_with_retry(self, state: TraversalState):
        """漏格/漏行重读：最多 max(1,ocr_retry) 张（含首次），仍不完整 failed。"""
        attempts = max(1, self.preset.ocr_retry)
        cells, problems, skips = self._scan_cells(state)
        for _ in range(1, attempts):
            if not problems:
                break
            self.log.warning("网格映射不完整（%s），重读第 %d 次", problems[0], _ + 1)
            time.sleep(0.15)
            cells, problems, skips = self._scan_cells(state)
        if problems:
            self.trace.fail(
                "drive_disc",
                "scan",
                f"网格映射不完整，重读耗尽：{problems[0]}",
                image=self._scan_image,
            )
            raise RunnerError(f"网格映射不完整：{problems[0]}，停止任务链")
        return cells, problems, skips

    # ---------------------------------------------------------------- 格位处理

    def _process_cell(
        self,
        hwnd: int,
        job: TaskJob,
        state: TraversalState,
        cell: GridCell,
        sub_preset: Preset,
        accepted_ids,
        up_counts: dict,
    ) -> str:
        """处理一个格位。返回 done / rescan。"""
        row, col = cell.display()
        cell_tag = self._cell_tag(cell)
        where = f"全局第{row}行第{col}列（序号 {cell.global_index}）"
        self.trace.new_disc()
        click_client(hwnd, cell.center_x, cell.center_y)
        time.sleep(0.5)
        detail_image, page, *_ = self._grab(allow_esc=False)
        if page in {"menu", "storage", "overworld"}:
            self.log.warning("cell %s 点开了 %s，先按 Esc 退回", cell_tag, page)
            self.trace.warn(
                page, "click_cell", f"{where} 点开了 {trace_page(page)}，先按 Esc 退回", image=detail_image
            )
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.6)
            back_image, page2, *_ = self._grab(allow_esc=False)
            if page2 == "drive_disc":
                self.log.info("recovered to drive_disc, rescan batch")
                self.trace.note("已退回驱动盘仓库，重扫本批")
                return "rescan"
            self.log.error("recover failed (page=%s), stop job", page2)
            self.trace.fail(page2, "esc", f"退回失败，停在 {trace_page(page2)}，收工", image=back_image)
            raise RunnerError(f"退回失败：{page2}，停止任务链")

        self.trace.ok("detail", "click_cell", where, image=detail_image)
        disc = self._parse_disc(detail_image)
        if disc is None:
            self.trace.fail("detail", "parse", f"{where} 没读出盘面，停止任务链", image=detail_image)
            raise RunnerError(f"{cell_tag} 盘面识别失败")
        self._processed += 1
        self._detail_disc = disc
        self.trace.ok(
            "detail",
            "parse",
            f"{where} 等级{disc.level} 副词条{len(disc.substats)}条"
            + ("（已锁定）" if disc.locked else "")
            + ("（已弃置）" if disc.discarded else ""),
            image=detail_image,
        )

        decision = decide(disc, sub_preset, accepted_ids=accepted_ids)
        if decision.action == Action.STOP:
            self._record(job, disc, decision)
            self.log.warning("cell %s decision STOP (%s), stop chain", cell_tag, decision.reason)
            self.trace.fail("detail", "decide", f"判断不了：{decision.reason}，停止任务链", image=detail_image)
            raise RunnerError(f"{cell_tag} 判断不确定：{decision.reason}，停止任务链")
        self.trace.ok(
            "detail", "decide", f"{trace_action(decision.action.value)}：{decision.reason}"
        )

        if decision.action == Action.UPGRADE:
            if up_counts.get(cell.global_index, 0) >= _MAX_STAGE_UPGRADES:
                self.log.warning("cell %s upgrades capped, skip action", cell_tag)
                self.trace.warn(
                    "detail",
                    "upgrade",
                    f"{where} 已升 {up_counts.get(cell.global_index, 0)} 次，到上限不再升",
                )
            else:
                if disc.discarded:
                    press_key_once(hwnd, VK_R)
                    time.sleep(0.5)
                    self.log.info("cell %s UPGRADE：摘除旧弃置标签 (R)", cell_tag)
                    self.trace.note("这个盘有弃置标记，先按 R 摘掉再强化")
                if self._enter_upgrade_page(hwnd):
                    self._upgrade_loop(
                        hwnd, job, cell.global_index, cell_tag, sub_preset, accepted_ids, up_counts
                    )
                else:
                    raise RunnerError(f"{cell_tag} 进入强化页失败，停止任务链")
            # 强化会改变排序：本批作废，重扫（坐标与数组全部重建）
            return "upgrade"
        if decision.action == Action.MARK_DISCARD and disc.discarded:
            self.log.info("cell %s already discarded, re-judge keeps it (no count)", cell_tag)
            self.trace.note(f"{where} 已经打过弃置标记，不动")
        elif decision.action == Action.SKIP and disc.discarded:
            self.log.info("cell %s has discard tag, skip (no count)", cell_tag)
            self.trace.note(f"{where} 有弃置标记，跳过")
        else:
            self._record(job, disc, decision)
            self._act(hwnd, decision.action, disc)
            self.log.info("cell %s action=%s level=%d", cell_tag, decision.action.value, disc.level)
            self.trace.ok("detail", decision.action.value, f"{where} 已{trace_action(decision.action.value)}")
            self._set_status("action", decision.action.value, cell_tag)
        time.sleep(0.3)
        return "done"

    # ---------------------------------------------------------------- 正常前进

    @staticmethod
    def _pick_anchor(cells: list[GridCell]) -> GridCell | None:
        """选一个全局位置已知、位于中间行的格位作导航锚点（spec §5.2）。"""
        mids = [c for c in cells if c.screen_row0 in (1, 2)]
        if not mids:
            return None
        mids.sort(key=lambda c: (abs(c.screen_row0 - 2), c.screen_col0))
        return mids[0]

    def _advance(self, hwnd: int, state: TraversalState, cells: list[GridCell], *, rows: int) -> bool:
        """正常短距前进（spec §5.2）：选中锚点 → 按协议发送 → 用锚点位移确认。

        请求量与已发送量分开；实际位移由锚点截图确认，不按滚轮次数直接累加。
        返回是否成功前进（被端点完全截停时也返回 True 并把 at_bottom 置位）。
        """
        profile = self._profile
        if profile is None:
            raise RunnerError("没有可用的滚轮协议，无法前进")
        anchor = self._pick_anchor(cells)
        if anchor is None:
            self.log.warning("没有可用的导航锚点格位，停止前进")
            return False

        origin, pitch = self._origin_center_y, self._row_pitch
        click_client(hwnd, anchor.center_x, anchor.center_y)
        time.sleep(0.4)
        image, _method, _hwnd = grab_game_image(hwnd)
        before = anchor_screen_rows(
            image, col0=anchor.screen_col0, origin_center_y=origin, row_pitch_px=pitch
        )
        if before != [anchor.screen_row0]:
            self.log.warning("锚点未唯一选中（读到 %s），停止前进", before)
            return False

        send = scroll_rows(
            hwnd, rows, profile, before_event=lambda _index: self._checkpoint()
        )
        state.begin_scroll(rows, send.total_events)
        for _ in range(send.sent_events):
            state.mark_event_sent()
        state.confirm_scroll()

        after, _settle_ms, _image = settle_and_read_anchor(
            hwnd,
            col0=anchor.screen_col0,
            origin_center_y=origin,
            row_pitch_px=pitch,
            top_row0=state.viewport_top_row0 or 0,
            phase_px=state.row_phase_px or 0.0,
        )
        observation = AnchorObservation(
            anchor_index=anchor.global_index,
            screen_row0_before=before[0],
            screen_row0_after=(after[0] if after else before[0]),
            requested_rows=rows,
        )
        advanced = max(0, observation.advanced_rows)
        previous_top = state.viewport_top_row0 or 0
        new_top = previous_top + advanced
        try:
            check_forward_move(state.work_batch_count + 1, previous_top, new_top, rows)
        except PositionError as exc:
            raise RunnerError(f"正常前进校验失败：{exc}，停止任务链") from exc

        state.advance_batch()
        at_bottom = observation.shortfall or observation.stopped
        state.confirm_viewport(new_top, state.row_phase_px or 0.0, at_top=False, at_bottom=at_bottom)
        state.clear_scroll()
        self._set_status("scroll", f"+{advanced} 行", "")
        if at_bottom:
            self.log.info("滚动被端点截停（请求 %d 实际 %d），标记到底", rows, advanced)
        return advanced > 0 or at_bottom

    # ---------------------------------------------------------------- 详情与强化

    def _parse_disc(self, image: QImage) -> Disc | None:
        return self._read_disc_with_retry(image, parse_disc, "detail")

    def _read_disc_with_retry(self, image, parser, page):
        reason = "未识别出盘面"
        for attempt in range(max(1, self.preset.ocr_retry)):
            try:
                disc = parser(image)
                if disc is not None:
                    return disc
            except OcrError as exc:
                reason = str(exc)
                self.log.warning("ocr retry %d: %s", attempt + 1, exc)
            if attempt + 1 < max(1, self.preset.ocr_retry):
                time.sleep(0.1)
                image = self._grab(allow_esc=False)[0]
        self.trace.fail(page, "parse", f"重试耗尽：{reason}", image=image)
        raise RunnerError(f"{page} 识别失败：{reason}，停止任务链")

    # 详情页右下大按钮（查看/强化，永远存在），OCR 实测标定
    UPGRADE_BTN_AREA = (1550, 780, 1900, 880)
    # 缩放页「等级00」文字区，>> 箭头在它右侧约 110px（实拍：文字 right≈1305，箭头中心≈1416）
    ZOOM_LEVEL_AREA = (1100, 150, 1550, 300)
    # 强化页按钮区（老大 2026-09-06 实拍截图标定）：
    # 「阶段添加」中心≈(1282,866)，「升级」中心≈(1511,866)。旧区 y=860 起把按钮拦腰切断导致 OCR 认不出。
    STAGE_ADD_AREA = (1150, 800, 1420, 940)
    UPGRADE_CONFIRM_AREA = (1420, 800, 1650, 940)

    def _act(self, hwnd: int, action: Action, disc: Disc) -> None:
        # UPGRADE 在 _process_cell 里内联处理（需要 rescan/计数联动），这里只管其余动作。
        # 注意：这些动作都不按 Esc——详情保持常开，下一个格子点击直接切换。
        # R 是开关：按之前必须看 disc.discarded，否则打标签会变成摘标签（反之亦然）。
        if action == Action.MARK_DISCARD:
            if disc.discarded:
                self.log.info("action=MARK_DISCARD：已带弃置标签，跳过按键")
            else:
                press_key_once(hwnd, VK_R)  # R 打弃置标签
                time.sleep(0.5)
                self.log.info("action=MARK_DISCARD (R)")
        elif action == Action.SKIP:
            self.log.info("action=SKIP")
        elif action == Action.STOP:
            self.log.info("action=STOP")

    def _esc_back_to_grid(self, hwnd: int, max_esc: int = 4) -> bool:
        """先认页再逐次退弹窗；已经在仓库时不按键，失败抛错终止任务链。"""
        from zzz_ddtool.screen import is_disc_modal

        esc_count = unknown_count = 0
        while True:
            self._checkpoint()
            image, page, *_ = self._grab(allow_esc=False)
            if page == "drive_disc":
                self.log.info("warehouse confirmed after %d esc, keep sidebar open", esc_count)
                return True
            if page in {"menu", "storage", "overworld"}:
                reason = f"返回驱动盘仓库失败：停在{trace_page(page)}，停止任务链"
                self.trace.fail(page, "esc", reason, image=image)
                raise RunnerError(reason)
            if not is_disc_modal(image):
                # 动画中可能暂时认不出，有限重拍；未知画面绝不盲按 Esc。
                unknown_count += 1
                if unknown_count >= max(1, self.preset.ocr_retry):
                    reason = "返回驱动盘仓库失败：无法确认盘面弹窗或仓库，停止任务链"
                    self.trace.fail(page, "esc", reason, image=image)
                    raise RunnerError(reason)
                time.sleep(0.2)
                continue
            unknown_count = 0
            if esc_count >= max_esc:
                reason = f"返回驱动盘仓库失败：{max_esc} 次回退后弹窗仍未关闭，停止任务链"
                self.trace.fail(page, "esc", reason, image=image)
                raise RunnerError(reason)
            # 通用 press_key 会通过多个输入通道发键，回退必须只发一次。
            press_key_once(hwnd, VK_ESCAPE)
            esc_count += 1
            time.sleep(0.6)

    def _click_text(self, hwnd: int, lines, needle: str, region_desc: str, tag: str, image=None) -> bool:
        """在 OCR 结果里找含 needle 的行并点中心，找不到存图返回 False。"""
        for line in lines:
            if needle in line.text.replace(" ", ""):
                click_client(hwnd, (line.left + line.right) // 2, (line.top + line.bottom) // 2)
                return True
        self.log.warning("UPGRADE: 没找到%s，中断回退", region_desc)
        self.trace.fail("upgrade", "click_cell", f"在{region_desc}没找到「{needle}」", image=image)
        return False

    def _enter_upgrade_page(self, hwnd: int) -> bool:
        """详情页点「查看/强化」→ 缩放页点「》」→ 强化页（弹层）。

        返回 True=已进强化页（「阶段添加」出现）；False=中断（调用方 Esc 回仓库页）。
        """
        from zzz_ddtool.ocr import read_rect

        # 1) 详情页大按钮（0 级也显示「查看」）
        image, *_ = self._grab(allow_esc=False)
        hit = None
        for text in ("查看", "强化"):
            for line in read_rect(image, self.UPGRADE_BTN_AREA):
                if text in line.text.replace(" ", ""):
                    hit = line
                    break
            if hit:
                break
        if hit is None:
            self.log.warning("UPGRADE: 详情页没找到「查看/强化」大按钮")
            self.trace.fail("detail", "enter_upgrade", "没找到「查看/强化」大按钮", image=image)
            self._esc_back_to_grid(hwnd)
            return False
        click_client(hwnd, (hit.left + hit.right) // 2, (hit.top + hit.bottom) // 2)
        time.sleep(0.9)

        # 2) 缩放页「等级」右缘 +110px 的「>>」箭头
        image, *_ = self._grab(allow_esc=False)
        level_line = None
        for line in read_rect(image, self.ZOOM_LEVEL_AREA):
            if "等级" in line.text.replace(" ", ""):
                level_line = line
                break
        if level_line is None:
            self.log.warning("UPGRADE: 缩放页没找到「等级」")
            self.trace.fail("zoom", "enter_upgrade", "没找到「等级」两字", image=image)
            self._esc_back_to_grid(hwnd)
            return False
        ax = level_line.right + 110
        ay = (level_line.top + level_line.bottom) // 2

        # 3) 点箭头进强化页，轮询等「阶段添加」出现
        stage_clicked = False
        for attempt in (1, 2):
            click_client(hwnd, ax, ay)
            for _wait in range(4):  # 弹层渲染可能慢，最多等 ~2.8s
                time.sleep(0.7)
                image, *_ = self._grab(allow_esc=False)
                lines = read_rect(image, self.STAGE_ADD_AREA)
                if any("阶段添加" in ln.text.replace(" ", "") for ln in lines):
                    stage_clicked = True
                    break
            if stage_clicked:
                break
            self.trace.warn("zoom", "enter_upgrade", f"第 {attempt} 次点箭头没进强化页", image=image)
            image, *_ = self._grab(allow_esc=False)
            level_line = None
            for line in read_rect(image, self.ZOOM_LEVEL_AREA):
                if "等级" in line.text.replace(" ", ""):
                    level_line = line
                    break
            if level_line is None:
                break  # 不是缩放页也没有阶段添加，别乱点，回退
            ax = level_line.right + 110
            ay = (level_line.top + level_line.bottom) // 2
        if not stage_clicked:
            self.log.warning("UPGRADE: 两次都没进强化页")
            self.trace.fail("zoom", "enter_upgrade", "点了两次箭头都没进强化页")
            self._esc_back_to_grid(hwnd)
            return False
        time.sleep(0.8)
        self.trace.ok("upgrade", "enter_upgrade", "已进强化页")
        return True

    def _upgrade_once(self, hwnd: int) -> bool:
        """强化页点「阶段添加」→「升级」，升一阶。返回是否完成。"""
        from zzz_ddtool.ocr import read_rect

        image, *_ = self._grab(allow_esc=False)
        if not self._click_text(
            hwnd, read_rect(image, self.STAGE_ADD_AREA), "阶段添加", "「阶段添加」", "step-stage-add", image=image
        ):
            return False
        time.sleep(0.6)
        image, *_ = self._grab(allow_esc=False)
        if not self._click_text(
            hwnd, read_rect(image, self.UPGRADE_CONFIRM_AREA), "升级", "「升级」", "step-upgrade-confirm", image=image
        ):
            return False
        time.sleep(1.5)
        self.log.info("action=UPGRADE 完成（阶段添加→升级）")
        self.trace.ok("upgrade", "upgrade", "强化了一阶")
        return True

    def _discard_from_upgrade(self, hwnd: int) -> None:
        """确认回到仓库侧边详情后按 R；侧边详情本来就是网格，不再 Esc。"""
        self._esc_back_to_grid(hwnd)
        self._checkpoint()
        press_key_once(hwnd, VK_R)
        time.sleep(0.5)
        self.log.info("action=MARK_DISCARD (R) from upgrade page")
        self.trace.ok("detail", "discard", "已按 R 打上弃置标记")

    def _upgrade_loop(
        self,
        hwnd: int,
        job: TaskJob,
        global_index: int,
        cell_tag: str,
        sub_preset: Preset,
        accepted_ids,
        up_counts: dict,
    ) -> None:
        """在强化页循环判断+升级，直到弃置/升满/跳过，最后回到网格。

        判断在强化页做（左边卡片有副属性），判升级就「阶段添加」+「升级」原地
        循环，判弃置才 Esc 回详情页按 R。不再每次升级完都 Esc 回网格全量重扫。
        """
        from zzz_ddtool.ocr import parse_disc_upgrade_page

        upgraded = 0
        while True:
            image, *_ = self._grab(allow_esc=False)
            disc = self._read_disc_with_retry(image, parse_disc_upgrade_page, "upgrade")
            if self._detail_disc is not None:
                disc = replace(
                    disc,
                    rarity=self._detail_disc.rarity,
                    locked=self._detail_disc.locked,
                    equipped=self._detail_disc.equipped,
                    discarded=False,
                )
            decision = decide(disc, sub_preset, accepted_ids=accepted_ids)
            if decision.action == Action.STOP:
                self._record(job, disc, decision)
                self.log.warning("cell %s 强化页判断不确定 (%s)，停止任务链", cell_tag, decision.reason)
                self.trace.fail("upgrade", "decide", f"判断不了：{decision.reason}，停止任务链", image=image)
                raise RunnerError(f"{cell_tag} 判断不确定：{decision.reason}，停止任务链")
            if decision.action == Action.UPGRADE:
                if upgraded >= _MAX_STAGE_UPGRADES or disc.level >= MAX_LEVEL:
                    self.log.info(
                        "cell %s 升级停止（level %d / 升了 %d 次），回网格", cell_tag, disc.level, upgraded
                    )
                    self.trace.ok(
                        "upgrade",
                        "run",
                        f"升到 {disc.level} 级"
                        + ("（已满级）" if disc.level >= MAX_LEVEL else "（到升阶上限）"),
                    )
                    self._esc_back_to_grid(hwnd)
                    return
                if not self._upgrade_once(hwnd):
                    raise RunnerError(f"{cell_tag} 强化按钮识别失败，停止任务链")
                upgraded += 1
                up_counts[global_index] = up_counts.get(global_index, 0) + 1
                self._record(job, disc, decision)
                self.log.info("cell %s UPGRADE done, level %d -> 强化页重判", cell_tag, disc.level)
                self._set_status("upgraded", f"level {disc.level}", cell_tag)
                continue  # 留强化页再判断
            if decision.action == Action.MARK_DISCARD:
                self.log.info("cell %s 强化页判弃置，Esc 回详情页按 R", cell_tag)
                self.trace.ok("upgrade", "decide", f"判为弃置：{decision.reason}")
                self._record(job, disc, decision)
                self._discard_from_upgrade(hwnd)
                return
            # SKIP：不动，直接 Esc 回网格；STOP 已在前面停止整条任务链。
            self.log.info("cell %s 强化页判 %s，回网格", cell_tag, decision.action.value)
            self.trace.ok("upgrade", "decide", f"判为跳过：{decision.reason}")
            self._record(job, disc, decision)
            self._esc_back_to_grid(hwnd)
            return

    def _record(self, job: TaskJob, disc: Disc, decision: Decision) -> None:
        self.results.append(
            RunResult(
                job_id=job.job_id,
                action=decision.action.value,
                reason=decision.reason,
                score=decision.score,
                level=disc.level,
            )
        )
