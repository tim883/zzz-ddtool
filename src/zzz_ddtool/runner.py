"""自动化主循环：按任务链导航、筛选、OCR、决策、点击。

所有真实交互都通过 nav / gamefilter / wininput / ocr 走，本层只负责编排。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QImage

from zzz_ddtool.capture import grab_game_image
from zzz_ddtool.chain import TaskJob
from zzz_ddtool.config import Preset
from zzz_ddtool.decide import decide
from zzz_ddtool.gamefilter import apply_game_filter
from zzz_ddtool.layout import GRID_COLS, GRID_ORIGIN, GRID_ROWS, GRID_STEP, scale_point
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.models import Action, Decision, Disc
from zzz_ddtool.nav import NavError, goto_warehouse
from zzz_ddtool.ocr import OcrError, parse_disc
from zzz_ddtool.wininput import VK_ESCAPE, click_client, press_key


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

    def __init__(self, preset: Preset) -> None:
        self.preset = preset
        self.log = get_logger("zzz_ddtool.runner")
        self.results: list[RunResult] = []
        self._processed = 0

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
        """跑完整轮任务链，返回处理结果。"""
        self.results = []
        self._processed = 0
        jobs = self._jobs()
        self.log.info("run start jobs=%d max_discs=%d", len(jobs), self.preset.max_discs)
        for job in jobs:
            if self._processed >= self.preset.max_discs:
                self.log.info("max_discs reached, stop jobs")
                break
            self._run_job(job)
        self.log.info("run done processed=%d", self._processed)
        return list(self.results)

    def _run_job(self, job: TaskJob) -> None:
        self.log.info("job start slot=%d sets=%s mains=%s", job.slot, job.sets, job.mainstats)
        try:
            goto_warehouse()
        except NavError as exc:
            raise RunnerError(f"导航失败：{exc}") from exc

        image, page, method, hwnd = self._grab()
        apply_game_filter(
            hwnd,
            image.width(),
            image.height(),
            slots=[job.slot],
            sets=job.sets,
            mainstats=job.mainstats,
        )
        time.sleep(0.3)

        sub_preset = self._sub_preset(job)
        accepted_ids = set(job.accepted) if job.accepted else None

        cells = _grid_cells(image.width(), image.height())
        for idx, (cx, cy) in enumerate(cells):
            if self._processed >= self.preset.max_discs:
                self.log.info("job hit max_discs")
                break
            self.log.debug("cell %d/%d (%d,%d)", idx + 1, len(cells), cx, cy)
            click_client(hwnd, cx, cy)
            time.sleep(0.4)
            detail_image, page, *_ = self._grab()
            if page != "drive_disc":
                self.log.warning("detail click left drive_disc (page=%s), stop job", page)
                break
            disc = self._parse_disc(detail_image)
            if disc is None:
                self.log.info("cell %d no disc, skip", idx + 1)
                press_key(hwnd, VK_ESCAPE)
                time.sleep(0.25)
                continue
            decision = decide(disc, sub_preset, accepted_ids=accepted_ids)
            self._record(job, disc, decision)
            self._act(hwnd, decision.action)
            self._processed += 1
            time.sleep(0.3)

    def _grab(self):
        image, method, hwnd = grab_game_image()
        from zzz_ddtool.screen import classify_image
        page = classify_image(image)
        return image, page, method, hwnd

    def _parse_disc(self, image: QImage) -> Disc | None:
        for attempt in range(max(1, self.preset.ocr_retry)):
            try:
                return parse_disc(image)
            except OcrError as exc:
                self.log.warning("ocr retry %d: %s", attempt + 1, exc)
                time.sleep(0.1)
        return None

    def _act(self, hwnd: int, action: Action) -> None:
        if action == Action.UPGRADE:
            # 强化按钮在右侧详情面板底部；直接用 Esc 关闭后由用户/后续流程处理
            press_key(hwnd, VK_ESCAPE)
            self.log.info("action=UPGRADE")
        elif action == Action.MARK_DISCARD:
            # 弃置按钮坐标：ACTION_ROW 右半区
            from zzz_ddtool.layout import ACTION_ROW, scale_rect
            image, *_ = self._grab()
            left, top, right, bottom = scale_rect(*ACTION_ROW, image.width(), image.height())
            x = (left + right) // 2 + 20
            y = (top + bottom) // 2
            click_client(hwnd, x, y)
            self.log.info("action=MARK_DISCARD")
        elif action == Action.SKIP:
            press_key(hwnd, VK_ESCAPE)
            self.log.info("action=SKIP")
        elif action == Action.STOP:
            press_key(hwnd, VK_ESCAPE)
            self.log.info("action=STOP")

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


def _grid_cells(width: int, height: int) -> list[tuple[int, int]]:
    """返回当前页 9×4 网格中心点（设计坐标按实际分辨率缩放）。"""
    ox, oy = scale_point(*GRID_ORIGIN, width, height)
    sx, sy = scale_point(*GRID_STEP, width, height)
    cells: list[tuple[int, int]] = []
    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            cells.append((ox + col * sx, oy + row * sy))
    return cells
