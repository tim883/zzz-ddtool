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
from zzz_ddtool.layout import (
    GRID_COLS,
    GRID_ROWS,
    LABEL_SCAN_AREA,
    UPGRADE_MATERIAL_SLOTS,
    scale_point,
)
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.models import MAX_LEVEL, Action, Decision, Disc, next_stage_level
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
    RESOLVED_STATUSES,
    AnchorObservation,
    CellStatus,
    GridCell,
    GridMetrics,
    PositionError,
    TraversalState,
    bottom_evidence,
    check_forward_move,
    check_layout_reference,
    forward_batch_for_top,
    global_row_col,
    make_filter_signature,
    map_labels,
    max_restore_rows,
    split_stable_cells,
    tail_slots_confirmed,
)
from zzz_ddtool.wininput import VK_ESCAPE, VK_R, click_client, press_key, press_key_once

# 单个盘子连续升阶上限（0→15 最多 5 阶，留余量防 OCR 读级错误导致死循环）
_MAX_STAGE_UPGRADES = 6
# 正常前进每次请求的行数上界（spec §5.1：只允许 1 或 2 行，维持 H）
_FORWARD_ROWS = 2
# 恢复批量滚动后的兜底稳定等待；标定给出 settle_wait_ms 时优先用它
_SETTLE_FALLBACK_MS = 500


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

    def __init__(
        self,
        preset: Preset,
        *,
        hotkeys: bool = True,
        controller=None,
        on_status=None,
        start_index: int = 0,
        material_slot: int = 0,
    ) -> None:
        self.preset = preset
        # 测试入口：>0 时跳过前 start_index 个内部序号直接从它开始（见 _seed_start_index）。
        # 只在 CLI --start-index 显式给出时非零，预设文件不带这个字段。
        self.start_index = max(0, int(start_index))
        # 测试入口：>0 时升级改成点第 N 个材料槽（1~3）而不是「阶段添加」，
        # 只加一份素材、吃不满一档，用来在真机复现「素材不足、升完到不了下一档」。
        # 只在 CLI --material-slot 显式给出时非零，预设文件不带这个字段。
        self.material_slot = max(0, int(material_slot))
        self.log = get_logger("zzz_ddtool.runner")
        self.trace = Trace(tag=time.strftime("%Y%m%d-%H%M%S"))
        self.results: list[RunResult] = []
        self._processed = 0
        self._job_started_at = 0
        self._job_max_discs = preset.max_discs
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
        # 强化后恢复时的校验扫描结果，直接交给主循环复用（避免重复 OCR 目标列表）
        self._restored_scan: tuple | None = None
        # 最近一次扫描的末尾占位判定（spec §5.4）：只有它和锚点端点行为同时成立
        # 才允许把视口标记为到底。
        self._tail_slots_confirmed = False

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
            "max_discs": sum(self._job_limit(job) for job in self._jobs()),
            "job_processed": self._processed - getattr(self, "_job_started_at", 0),
            "job_max_discs": getattr(self, "_job_max_discs", self.preset.max_discs),
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

    def _grab(self, allow_esc: bool = True, need_page: bool = True):
        image, method, hwnd = grab_game_image(self._hwnd)
        if not need_page:
            # 调用方只借这一帧图（在弹层里按坐标点击），不关心停在哪一页：跳过认页。
            # 弹层不命中任何页模板，7 个模板逐个跑一遍要 1~5 秒，纯白等。
            return image, "unknown", method, hwnd
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
            max_discs=self.preset.max_discs,
        )

    def _jobs(self) -> tuple[TaskJob, ...]:
        return self.preset.task_chain or (self._default_job(),)

    def _job_limit(self, job: TaskJob) -> int:
        return max(1, job.max_discs if job.max_discs is not None else self.preset.max_discs)

    def _job_limit_reached(self) -> bool:
        return self._processed - self._job_started_at >= self._job_max_discs

    def _log_job_limit_reached(self) -> None:
        """本节处理数到上限、收工前记一条，避免静默 break（排查时看不清是限额还是到底）。"""
        self.log.info(
            "本节已处理 %d 个盘，达到上限 %d，收工",
            self._processed - self._job_started_at,
            self._job_max_discs,
        )

    def _sub_preset(self, job: TaskJob) -> Preset:
        """用单个 job 的权重/门槛构造临时 Preset 给 decide 用。"""
        return Preset(
            weights=job.weights,
            filter_slot=job.slot,
            filter_slots=(job.slot,),
            filter_mainstats=job.mainstats,
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
        self._job_started_at = 0
        self._job_max_discs = self.preset.max_discs
        self._status_lines = []
        self._state = None
        self._restored_scan = None
        self._tail_slots_confirmed = False
        self.outcome = "running"
        self.end_reason = ""
        jobs = self._jobs()
        import logging

        from zzz_ddtool.overlay import OverlayLogHandler

        overlay_handler = OverlayLogHandler()
        logging.getLogger("zzz_ddtool").addHandler(overlay_handler)
        total_limit = sum(self._job_limit(job) for job in jobs)
        self.log.info("run start jobs=%d max_discs=%d (F9 暂停 / F10 停止)", len(jobs), total_limit)
        self._set_status("run_start", f"jobs={len(jobs)}")
        self.trace.ok("unknown", "run", f"共 {len(jobs)} 个任务，各任务独立限额，合计最多处理 {total_limit} 个盘")
        try:
            for job in jobs:
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
        self._job_started_at = self._processed
        self._job_max_discs = self._job_limit(job)
        self.log.info("job start slot=%d sets=%s mains=%s", job.slot, job.sets, job.mainstats)
        hwnd = self._bind_instance()
        try:
            goto_warehouse(hwnd)
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
        # 先起第 1 批，再摆起点：跳号启动要按新视口覆盖批次计数（见 _seed_start_index）。
        state.start_first_batch()
        if self.start_index > 0:
            self._seed_start_index(hwnd, state, self.start_index)
        else:
            state.confirm_viewport(0, 0.0, row_pitch_px=self._row_pitch, at_top=True, at_bottom=False)
        self._set_status("batch", "首批网格起点")

        up_counts: dict[int, int] = {}

        while True:
            self._checkpoint()
            if self._job_limit_reached():
                self._log_job_limit_reached()
                break
            if state.work_batch_count > self.preset.max_pages:
                self.log.info(
                    "work_batch_count=%d 达到 max_pages=%d，收工", state.work_batch_count, self.preset.max_pages
                )
                break

            if self._restored_scan is not None:
                cells, problems, skips = self._restored_scan
                self._restored_scan = None
            else:
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

            # 从 next_index 起按序号取候选：已确认跳过的格位直接越过（仍占格位、
            # 不计数、不点击）；序号出现缺口说明前方有未解决格位（延期/视口外），
            # 不能越过（spec §5.2、§6.1）。
            pending: list[GridCell] = []
            ready_by_index = {cell.global_index: cell for cell in ready}
            expected = state.next_index
            while True:
                cell = ready_by_index.get(expected)
                if cell is None:
                    break
                if state.status_of(expected) in RESOLVED_STATUSES:
                    expected += 1
                    continue
                pending.append(cell)
                expected += 1

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
                if self._single_screen_tail(state, cells):
                    self.log.info("列表整屏可见且末尾占位已确认，直接判到底并补齐末行")
                    state.confirm_viewport(
                        state.viewport_top_row0 or 0,
                        state.row_phase_px or 0.0,
                        at_top=True,
                        at_bottom=True,
                    )
                    continue
                before_top = state.viewport_top_row0
                if not self._advance(hwnd, state, cells, rows=_FORWARD_ROWS):
                    self.log.info("无法继续前进，本任务结束")
                    break
                if state.viewport_top_row0 == before_top and not state.at_bottom:
                    self.log.warning("前进未改变视口且未到底，停止以避免空转")
                    break
                continue

            broke = False
            restore_after_upgrade = False
            for cell in pending:
                self._checkpoint()
                if self._job_limit_reached():
                    self._log_job_limit_reached()
                    break
                result = self._process_cell(
                    hwnd, job, state, cell, sub_preset, accepted_ids, up_counts
                )
                if result in ("rescan", "upgrade"):
                    broke = True
                    # 强化改变排序：本批坐标作废（spec §6.3.5）。未达处理上限且未停止时，
                    # 置顶并按保存视口恢复，再从 next_index 重建候选。
                    restore_after_upgrade = result == "upgrade"
                    break
                state.set_status(cell.global_index, CellStatus.DONE)
                state.commit_prefix()
                self._set_status("progress", f"next_index={state.next_index}", self._cell_tag(cell))
            if broke:
                state.invalidate_position()
                if restore_after_upgrade and not self._job_limit_reached():
                    self._restore_position(hwnd, state)
                continue
            if self._job_limit_reached():
                self._log_job_limit_reached()
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
        reset_to_top(hwnd)
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
        reset_to_top(hwnd)
        self.trace.ok(
            "drive_disc",
            "calibrate",
            f"原点 {self._origin_center_y:.1f} 行距 {self._row_pitch:.1f} "
            f"协议 {profile.events_per_row}事件/行@{profile.event_interval_ms}ms",
        )
        return profile

    def _seed_start_index(self, hwnd: int, state: TraversalState, start_index: int) -> None:
        """[测试入口] 把任务起点直接摆到指定内部序号，不真处理它前面的盘。

        跳过的是「状态」而不是「路径」：spec §8 的深序号验收场景只规定
        「视口在全局第 11~14 行、处理第 100 件」，没要求先真跑 99 件。所以这里
        把 [0, start_index) 全部标为 SKIPPED（不点击、不识别、不计数），
        再按实测滚轮协议把视口滚到该序号所在行，最后按实测布局确认视口。

        生产方式不启用：只由 CLI --start-index 打开，日志与 trace 都显式标注，
        以免把跳号跑误认成真实跑。
        """
        profile = self._profile
        if profile is None:
            raise RunnerError("没有可用的滚轮协议，无法跳号启动")
        if self._row_pitch <= 0:
            raise RunnerError("行距未知，无法跳号启动")

        row0, col0 = global_row_col(start_index)
        # 起点放在屏幕第 2 行（与 spec §8 验收场景一致）；起点在第 0 行时贴顶。
        top_row0 = max(0, row0 - 1)

        for index in range(start_index):
            state.set_status(index, CellStatus.SKIPPED)
        advanced = state.commit_prefix()
        if advanced != start_index:
            raise RunnerError(f"跳号启动失败：next_index={advanced}，期望 {start_index}")

        if top_row0 > 0:
            send = scroll_rows(
                hwnd, top_row0, profile, before_event=lambda _index: self._checkpoint()
            )
            if not send.complete:
                raise RunnerError("跳号启动的批量滚动未完成，停止任务链")
            self._await_scroll_settle(profile)

        image, _method, _hwnd = grab_game_image(hwnd)
        origin, pitch = measure_layout(image)
        reason = check_layout_reference(origin, pitch, image.width(), image.height())
        if reason:
            raise RunnerError(f"跳号启动后原点复核不通过：{reason}，停止任务链")
        self._origin_center_y = float(origin)
        self._row_pitch = float(pitch)
        profile = replace(
            profile, row_pitch_px=self._row_pitch, origin_center_y=self._origin_center_y
        )
        self._profile = profile
        state.navigation_profile = profile.as_dict()
        state.confirm_viewport(
            top_row0,
            0.0,
            row_pitch_px=self._row_pitch,
            at_top=top_row0 == 0,
            at_bottom=False,
        )
        state.clear_scroll()
        # 视口已经不在顶部，批次计数按等效批次数对齐：check_forward_move 的上界
        # 是「每批 2 行」推出来的，不同步就会把这次合法的深序号视口判成超量推进。
        state.work_batch_count = state.work_batch_id = forward_batch_for_top(top_row0)
        self.log.warning(
            "测试入口：跳号启动 next_index=%d（全局第 %d 行第 %d 列），视口顶部全局第 %d 行"
            "（批次对齐为第 %d 批）；前 %d 格标记为已跳过（未实际处理）",
            start_index,
            row0 + 1,
            col0 + 1,
            top_row0 + 1,
            state.work_batch_count,
            start_index,
        )
        self._set_status(
            "seed",
            f"测试跳号启动：next_index={start_index}（全局第 {row0 + 1} 行第 {col0 + 1} 列）",
        )
        self.trace.warn(
            "drive_disc",
            "seed",
            f"测试跳号：起点内部序号 {start_index}（全局第 {row0 + 1} 行第 {col0 + 1} 列），"
            f"视口顶部全局第 {top_row0 + 1} 行，前 {start_index} 格标为已跳过（未实际处理）",
            image=image,
        )

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

    def _classify_empty_slots(self, metrics: GridMetrics, state: TraversalState, image, cells):
        """没有「等级」标签的屏幕位置 → (确认空位集合, 拿不准集合)。

        空位认的是图标区原始占位（``tile_is_empty``），不是「没读到等级」。相位
        不为零（视口停在半行、格子被裁切）或格子超出画面时一律拿不准：spec §5.5
        第 259 行要求按失败路径处理，不能宣布空位。只有带标签的位置天然有盘，
        不必再采样，所以中途推进时这一步不增加额外像素开销。
        """
        from zzz_ddtool.equipped import tile_is_empty

        phase = state.row_phase_px or 0.0
        top = state.viewport_top_row0 or 0
        occupied = {
            (cell.screen_row0, cell.screen_col0)
            for cell in cells
            if 0 <= cell.screen_row0 < GRID_ROWS
        }
        empties: set[tuple[int, int]] = set()
        unclear: dict[tuple[int, int], str] = {}
        for row in range(GRID_ROWS):
            for col in range(GRID_COLS):
                if (row, col) in occupied:
                    continue
                if phase != 0.0:
                    unclear[(row, col)] = "视口未对齐，占位不可判"
                    continue
                icon_y = int(round(metrics.row_center_y(top + row, top, phase)))
                verdict = tile_is_empty(image, metrics.center_x_of(col), icon_y)
                if verdict is True:
                    empties.add((row, col))
                elif verdict is False:
                    unclear[(row, col)] = "图标区有盘但没读到等级"
                else:
                    unclear[(row, col)] = "图标区占位不确定"
        return empties, unclear

    def _scan_cells(self, state: TraversalState):
        """扫描当前视口 → (全部原始格位, 问题, 扫描跳过表)。

        行号由几何反推，不用 OCR 聚类序号；未映射/重复归列进 problems，调用方
        当作进度屏障重读（spec §5.5），不当空位。满级/装备/弃置格仍占全局格位，
        只记入 skips，不删号。
        """
        from zzz_ddtool.equipped import (
            tile_has_discard_badge,
            tile_has_portrait,
        )
        from zzz_ddtool.ocr import read_rect

        image, page, _method, hwnd = self._grab(allow_esc=False)
        if page != "drive_disc":
            self.trace.fail(page, "scan", "当前不是驱动盘仓库，停止扫描", image=image)
            raise RunnerError(f"扫描前页面错误：{page}，停止任务链")
        self._scan_image = image

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

        # 没读到「等级」的屏幕位置不等于空位（spec §5.5 第 259 行）：逐个看图标
        # 区原始占位，确认是游戏画的空格子才算合法空位；有盘漏读或占位拿不准都
        # 进 problems，作为进度屏障重读，重读耗尽后 failed，next_index 不越过缺口。
        empties, unclear = self._classify_empty_slots(metrics, state, image, cells)
        for (row, col), reason in sorted(unclear.items()):
            problems.append(f"屏幕第{row + 1}行第{col + 1}列{reason}")
        self._tail_slots_confirmed = tail_slots_confirmed(
            {
                (cell.screen_row0, cell.screen_col0)
                for cell in cells
                if 0 <= cell.screen_row0 < GRID_ROWS
            },
            empties,
            rows=GRID_ROWS,
            cols=GRID_COLS,
        )

        skips: dict[int, str] = {}
        for cell in cells:
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
                self._record(job, disc, decision)
                time.sleep(0.3)
                return "done"  # 没进强化页，画面未变，坐标仍然有效
            if disc.discarded:
                press_key_once(hwnd, VK_R)
                time.sleep(0.5)
                self.log.info("cell %s UPGRADE：摘除旧弃置标签 (R)", cell_tag)
                self.trace.note("这个盘有弃置标记，先按 R 摘掉再强化")
            # 进强化页前保存恢复点（spec §6.3.1）；连续升阶途中沿用同一恢复点，
            # 强化后的焦点不覆盖它。
            state.begin_upgrade(cell.global_index)
            if self._enter_upgrade_page(hwnd):
                self._upgrade_loop(
                    hwnd, job, cell.global_index, cell_tag, sub_preset, accepted_ids, up_counts
                )
            else:
                raise RunnerError(f"{cell_tag} 进入强化页失败，停止任务链")
            # 最终保留/跳过/弃置完成且确认回到仓库后，提交该件进度（spec §6.3.3）。
            state.finish_upgrade(CellStatus.DONE)
            self._set_status("upgrade_done", f"next_index={state.next_index}", cell_tag)
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

    def _single_screen_tail(self, state: TraversalState, cells: list[GridCell]) -> bool:
        """列表短到一屏装得下时是否可直接判到底（spec §5.4 末尾布局）。

        这种列表游戏根本不会滚动，没有可观测的锚点位移，端点行为这条路走不通，
        只能靠末尾占位确认结束（spec §5.5 第 259 行）。三条同时成立才算：
        贴在顶部、末尾占位已确认、网格里没有任何能当导航锚点的中间行格位——
        也就是只有首行有盘、盘数不足一行满 9 件。不满足就走原来的前进路径。
        """
        return self._tail_slots_confirmed and state.at_top and self._pick_anchor(cells) is None

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
        # 到底 = 锚点端点行为（实际位移小于请求或完全没动） + 末尾占位已确认
        # （spec §5.4）。相位相同或滑块都不能替代这两条依据；末尾占位拿不准时
        # 保留未知，不置 at_bottom，相关推进到此为止。
        at_bottom = bottom_evidence((observation,), tail_slots_confirmed=self._tail_slots_confirmed)
        state.confirm_viewport(new_top, state.row_phase_px or 0.0, at_top=False, at_bottom=at_bottom)
        state.clear_scroll()
        self._set_status("scroll", f"+{advanced} 行", "")
        if at_bottom:
            self.log.info("滚动被端点截停（请求 %d 实际 %d），标记到底", rows, advanced)
        elif observation.shortfall or observation.stopped:
            self.log.info(
                "滚动被端点截停（请求 %d 实际 %d），但末尾占位未确认，保留未知不置到底",
                rows,
                advanced,
            )
        return advanced > 0 or at_bottom

    # ---------------------------------------------------------------- 强化后恢复

    def _restore_position(self, hwnd: int, state: TraversalState) -> None:
        """强化改变排序后，恢复保存的视口（spec §5.3、§6.3.5）。

        「切页置顶 → 按保存视口批量返回」是完整的一次恢复尝试，最多两次；第二次
        只用于可重试的运行误差（目标网格/相位暂时读不出、位移或端点不一致）。
        请求超上界、协议缺失、进入流程外错误页直接 failed，不做第二次。每次尝试
        后做一次目标网格校验扫描，成功即缓存给主循环，恢复流程不重复 OCR。
        """
        if state.resume_point is None:
            raise RunnerError("强化后没有保存的恢复点，停止任务链")
        if not state.within_restore_limit():
            raise RunnerError(
                f"恢复请求 {state.restore_request_rows()} 行超过上界 "
                f"{state.restore_request_limit_rows}，停止任务链"
            )
        saved = state.resume_point
        state.reset_restore_attempts()
        last_reason = ""
        for attempt in (1, 2):
            state.begin_restore_attempt()
            self._set_status(
                "restore",
                f"第 {attempt} 次恢复：返回全局第 {saved.viewport_top_row0 + 1} 行视口",
            )
            self._restore_once(hwnd, state, saved)
            cells, problems, skips = self._scan_cells(state)
            if not problems:
                self._restored_scan = (cells, problems, skips)
                state.clear_resume()  # 已恢复到保存视口，恢复点可以释放
                self.trace.ok(
                    "drive_disc",
                    "restore",
                    f"已恢复到全局第 {saved.viewport_top_row0 + 1} 行视口，"
                    f"next_index={state.next_index}",
                    image=self._scan_image,
                )
                return
            last_reason = problems[0]
            self.log.warning("恢复第 %d 次校验不通过：%s", attempt, last_reason)
            self.trace.warn(
                "drive_disc", "restore", f"第 {attempt} 次恢复校验不通过：{last_reason}"
            )
        # 第二次仍不匹配：不能再重试（spec §5.3）。
        raise RunnerError(f"恢复保存视口失败（两次尝试）：{last_reason}，停止任务链")

    def _restore_once(self, hwnd: int, state: TraversalState, saved) -> None:
        """一次完整的「切页置顶 → 按保存视口批量返回」，并提交保存的实际位置。"""
        profile = self._profile
        if profile is None:
            raise RunnerError("没有可用的滚轮协议，无法恢复位置")

        reset_to_top(hwnd)
        image, _method, _hwnd = grab_game_image(hwnd)
        origin, pitch = measure_layout(image)
        reason = check_layout_reference(origin, pitch, image.width(), image.height())
        if reason:
            raise RunnerError(f"恢复前原点复核不通过：{reason}，停止任务链")
        self._origin_center_y = float(origin)
        self._row_pitch = float(pitch)
        profile = replace(profile, row_pitch_px=self._row_pitch, origin_center_y=self._origin_center_y)
        self._profile = profile
        state.navigation_profile = profile.as_dict()

        rows = state.restore_request_rows()
        if rows > 0:
            send = scroll_rows(hwnd, rows, profile, before_event=lambda _index: self._checkpoint())
            if not send.complete:
                raise RunnerError("恢复批量发送未完成，停止任务链")
            # 事件发完只说明输入已送出，列表仍在缓动。立刻抓图会让「等级」标签
            # 整体偏几像素，残差压过 §5.4 的 2% 映射容差，把正确的恢复误判成失败。
            self._await_scroll_settle(profile)
        # 提交保存的实际视口，不新建工作批次（spec §5.3、§6.4）。
        state.confirm_viewport(
            saved.viewport_top_row0,
            saved.row_phase_px,
            row_pitch_px=self._row_pitch,
            at_top=saved.at_top,
            at_bottom=saved.at_bottom,
        )
        state.clear_scroll()

    def _await_scroll_settle(self, profile: NavProfile) -> None:
        """恢复批量滚动后等列表缓动停稳，再让校验扫描抓图。

        滚动事件发完只说明输入已送出，列表仍在缓动；此时抓到的「等级」标签会整体
        偏几像素，残差压过 §5.4 的 2% 映射容差（174.5px 行距时约 3.49px），把本来
        正确的恢复误判成失败（2026-10-02 真机第 4 轮实测偏 3px）。

        等待量取标定实测的 settle_wait_ms ——正常前进就是靠它轮询到锚点稳定，
        恢复没有选中框可轮询，只能按实测值等待；缺失时用兜底值。
        """
        wait_ms = int(getattr(profile, "settle_wait_ms", 0) or 0)
        if wait_ms <= 0:
            wait_ms = _SETTLE_FALLBACK_MS
        self.log.info("恢复滚动完成，等列表稳定 %dms", wait_ms)
        time.sleep(wait_ms / 1000)

    # ---------------------------------------------------------------- 详情与强化

    def _parse_disc(self, image: QImage) -> Disc | None:
        return self._read_disc_with_retry(image, parse_disc, "detail")

    def _parse_upgrade_page(self, image: QImage) -> Disc | None:
        """强化页识别：先按强化页坐标读，读不到再按详情页坐标读。

        满级后游戏把强化界面切成「展示态」——等级标到右上角 15/15 MAX、强化按钮
        消失，强化页专用区域读不到等级，会误报「未识别出盘面」打断整条任务链
        （2026-10-02 真机第 5 轮实测：12→15 满级确认后 UPGRADE_LEVEL 区域无文字、
        DISC_LEVEL 区域读到「等级15」）。回退详情页坐标仍能读出 level=15，
        交给 decide 走 keep_max_level 正常收尾。
        """
        from zzz_ddtool import ocr as _ocr  # 走模块属性，保持可打桩

        disc = _ocr.parse_disc_upgrade_page(image)
        if disc is not None:
            return disc
        return _ocr.parse_disc(image)

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
                image = self._grab(allow_esc=False, need_page=False)[0]
        self.trace.fail(page, "parse", f"重试耗尽：{reason}", image=image)
        raise RunnerError(f"{page} 识别失败：{reason}，停止任务链")

    # 详情页右下大按钮（查看/强化，永远存在），OCR 实测标定
    UPGRADE_BTN_AREA = (1550, 780, 1900, 880)
    # 缩放页「等级00」文字区，>> 箭头在它右侧约 110px（实拍：文字 right≈1305，箭头中心≈1416）
    # 框必须只套住「等级xx」这一行：放宽到包含右侧「主属性」等行时，OCR 检测框会左移并把
    # 「等级00」整行读成「00碳美」之类的废字（2026-10-02 真机第 6 轮 R5C5 实测，
    # 同一布局下 (1100,150,1550,300) 误读、本框在 zoom-preview / 满级页 / 失败现场三张真机图上均正确）。
    ZOOM_LEVEL_AREA = (1150, 170, 1500, 280)
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
        """先认页再逐次退弹窗；已经在仓库时不按键，失败抛错终止任务链。

        盘面弹层有三层：缩放预览层（无关闭 X，靠等级文字认）、强化层（有 X）、
        详情侧栏（本身判为驱动盘仓库）。只有确认这几种层存在才按 Esc。
        """
        from zzz_ddtool.screen import is_disc_modal, is_disc_preview

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
            if not (is_disc_modal(image) or is_disc_preview(image)):
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

    def _read_zoom_level_line(self, hwnd: int):
        """读缩放页「等级xx」那一行（用它的右缘 +110px 算 >> 箭头位置）。

        缩放页没有可轮询的选中框，抓图可能落在缩放动画里；读不到就重拍，
        最多 max(1, ocr_retry) 张。返回 (line, image)，读不到时 line 为 None。
        """
        from zzz_ddtool.ocr import read_rect

        attempts = max(1, self.preset.ocr_retry)
        image = None
        for attempt in range(attempts):
            image, *_ = self._grab(allow_esc=False, need_page=False)
            for line in read_rect(image, self.ZOOM_LEVEL_AREA):
                if "等级" in line.text.replace(" ", ""):
                    return line, image
            if attempt + 1 < attempts:
                time.sleep(0.2)
        return None, image

    def _enter_upgrade_page(self, hwnd: int) -> bool:
        """详情页点「查看/强化」→ 缩放页点「》」→ 强化页（弹层）。

        返回 True=已进强化页（「阶段添加」出现）；False=中断（调用方 Esc 回仓库页）。
        """
        from zzz_ddtool.ocr import read_rect

        # 1) 详情页大按钮（0 级也显示「查看」）
        image, *_ = self._grab(allow_esc=False, need_page=False)
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
        level_line, image = self._read_zoom_level_line(hwnd)
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
                image, *_ = self._grab(allow_esc=False, need_page=False)
                lines = read_rect(image, self.STAGE_ADD_AREA)
                if any("阶段添加" in ln.text.replace(" ", "") for ln in lines):
                    stage_clicked = True
                    break
            if stage_clicked:
                break
            self.trace.warn("zoom", "enter_upgrade", f"第 {attempt} 次点箭头没进强化页", image=image)
            level_line, _image = self._read_zoom_level_line(hwnd)
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

    def _pick_material(self, hwnd: int, material_slot: int) -> bool:
        """[测试入口] 点第 N 个材料槽，只加一份素材（替代「阶段添加」）。

        真机复现「素材不足」用：单个材料槽加的经验吃不满一整档，点「升级」后
        等级停在下一档之前，正好走 _upgrade_loop 的期望等级校验。
        返回是否真的点出去了（槽号越界按最近的一档夹住）。
        """
        image, *_ = self._grab(allow_esc=False, need_page=False)
        index = min(max(1, material_slot), len(UPGRADE_MATERIAL_SLOTS))
        mx, my = scale_point(*UPGRADE_MATERIAL_SLOTS[index - 1], image.width(), image.height())
        click_client(hwnd, mx, my)
        self.log.info("action=UPGRADE 测试入口：点第 %d 个材料槽 (%d,%d)", index, mx, my)
        self.trace.ok("upgrade", "step-material", f"测试入口：点第 {index} 个材料槽", image=image)
        return True

    def _upgrade_once(self, hwnd: int, material_slot: int = 0) -> bool:
        """强化页选好素材 →「升级」，升一阶。返回两步是否都走完。

        material_slot=0 走生产的「阶段添加」（一键补满本档素材）；>0 是测试入口，
        改成点第 N 个材料槽（见 _pick_material）。
        """
        from zzz_ddtool.ocr import read_rect

        if material_slot:
            if not self._pick_material(hwnd, material_slot):
                return False
            time.sleep(0.6)
        else:
            image, *_ = self._grab(allow_esc=False, need_page=False)
            if not self._click_text(
                hwnd, read_rect(image, self.STAGE_ADD_AREA), "阶段添加", "「阶段添加」", "step-stage-add", image=image
            ):
                return False
            time.sleep(0.6)
        image, *_ = self._grab(allow_esc=False, need_page=False)
        if not self._click_text(
            hwnd, read_rect(image, self.UPGRADE_CONFIRM_AREA), "升级", "「升级」", "step-upgrade-confirm", image=image
        ):
            return False
        time.sleep(1.5)
        self.log.info("action=UPGRADE 完成（%s→升级）", "材料槽" if material_slot else "阶段添加")
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
        每升一阶后复核等级有没有到下一档（3/6/9/12/15），到不了就是素材耗尽，
        报错停止整条任务链。
        """
        upgraded = 0
        # 上一轮点了「升级」后应到达的等级（None = 没在等复核）与累计重读次数。
        expect_level: int | None = None
        expect_from = 0
        rechecks = 0
        while True:
            image, *_ = self._grab(allow_esc=False, need_page=False)
            disc = self._read_disc_with_retry(image, self._parse_upgrade_page, "upgrade")
            if self._detail_disc is not None:
                disc = replace(
                    disc,
                    rarity=self._detail_disc.rarity,
                    locked=self._detail_disc.locked,
                    equipped=self._detail_disc.equipped,
                    discarded=False,
                )
            # 先复核上一轮升级有没有到位。素材不够吃满一档时游戏只加到部分经验、
            # 等级停在期望值之前（3/6/9/12/15 之前），必须当场停链；否则等级没变、
            # decide 会一直判 UPGRADE，白转 _MAX_STAGE_UPGRADES 次后当「到升阶上限」
            # 成功收尾，把素材耗尽静默吞掉。读一次可能撞上强化动画，按 ocr_retry 重读。
            if expect_level is not None:
                if disc.level >= expect_level:
                    expect_level = None
                elif rechecks < max(1, self.preset.ocr_retry) - 1:
                    rechecks += 1
                    self.log.warning(
                        "cell %s 升级后等级还是 %d（期望到 %d），重读第 %d 次",
                        cell_tag,
                        disc.level,
                        expect_level,
                        rechecks,
                    )
                    time.sleep(0.4)
                    continue
                else:
                    reason = (
                        f"升级素材不足：从 {expect_from} 级升级后期望到 {expect_level} 级，"
                        f"实际只有 {disc.level} 级，停止任务链"
                    )
                    self.log.error("cell %s %s", cell_tag, reason)
                    self.trace.fail("upgrade", "upgrade", reason, image=image)
                    # 素材耗尽是正常业务收尾，不是异常画面：先把游戏退回仓库侧栏，
                    # 别把玩家卡在强化弹层里。回退本身失败也不该盖掉真正的原因。
                    try:
                        self._esc_back_to_grid(hwnd)
                    except RunnerError as esc_exc:
                        self.log.warning("素材不足收尾时回退仓库失败：%s", esc_exc)
                    raise RunnerError(f"{cell_tag} {reason}")
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
                if not self._upgrade_once(hwnd, self.material_slot):
                    raise RunnerError(f"{cell_tag} 强化按钮识别失败，停止任务链")
                upgraded += 1
                up_counts[global_index] = up_counts.get(global_index, 0) + 1
                self._record(job, disc, decision)
                expect_from = disc.level
                expect_level = next_stage_level(disc.level)
                rechecks = 0
                self.log.info(
                    "cell %s UPGRADE done, level %d -> 期望到 %d 级，强化页重判",
                    cell_tag,
                    disc.level,
                    expect_level,
                )
                self._set_status("upgraded", f"level {disc.level} -> {expect_level}", cell_tag)
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
