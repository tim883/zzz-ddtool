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
from zzz_ddtool.wininput import VK_ESCAPE, click_client, press_key, wheel_client


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
        w, h = image.width(), image.height()
        gx, gy = scale_point(640, 540, w, h)

        prev_signature: tuple | None = None
        for page_idx in range(self.preset.max_pages):
            if self._processed >= self.preset.max_discs:
                self.log.info("job hit max_discs")
                break
            cells = self._visible_cells(w, h)
            signature = tuple(sorted(cells))
            if not cells or signature == prev_signature:
                self.log.info("page %d empty or same as before, job done", page_idx)
                break
            prev_signature = signature
            self.log.info("page %d: %d cells to process", page_idx, len(cells))
            for cx, cy in cells:
                if self._processed >= self.preset.max_discs:
                    break
                click_client(hwnd, cx, cy)
                time.sleep(0.4)
                detail_image, page, *_ = self._grab()
                if page != "drive_disc":
                    self.log.warning("detail click left drive_disc (page=%s), stop job", page)
                    return
                disc = self._parse_disc(detail_image)
                if disc is None:
                    self.log.info("cell (%d,%d) no disc, skip", cx, cy)
                    continue
                decision = decide(disc, sub_preset, accepted_ids=accepted_ids)
                self._record(job, disc, decision)
                self._act(hwnd, decision.action)
                self._processed += 1
                time.sleep(0.3)
            wheel_client(hwnd, gx, gy, -5)  # 翻下一页
            time.sleep(0.8)

    def _visible_cells(self, width: int, height: int) -> list[tuple[int, int]]:
        """OCR 当前页「等级」标签定位格子，返回未装备格子的图标中心点。

        点的是图标中心（标签上方 GRID_LABEL_DY），已装备（头像）跳过。
        """
        image, _method, hwnd = grab_game_image()
        ox, oy = scale_point(*GRID_ORIGIN, width, height)
        sx, sy = scale_point(*GRID_STEP, width, height)
        from zzz_ddtool.equipped import tile_has_portrait
        from zzz_ddtool.layout import GRID_LABEL_DY
        from zzz_ddtool.ocr import read_rect

        cells: list[tuple[int, int]] = []
        for line in read_rect(image, (60, 150, 1240, 1000)):
            text = line.text.replace(" ", "")
            if not text.startswith("等级"):
                continue
            lx = (line.left + line.right) // 2
            ly = (line.top + line.bottom) // 2
            col = round((lx - ox) / sx)
            row = round((ly + GRID_LABEL_DY - oy) / sy)
            if not (0 <= col < GRID_COLS and 0 <= row < GRID_ROWS):
                continue
            cx, cy = ox + col * sx, oy + row * sy
            if tile_has_portrait(image, cx - 56, cy - 68, cx + 56, cy + 68):
                self.log.info("cell r%dc%d equipped, skip", row, col)
                continue
            cells.append((cx, cy))
        return cells

    def _grab(self):
        image, method, hwnd = grab_game_image()
        from zzz_ddtool.screen import classify_image
        page = classify_image(image)
        if page == "unknown":
            # 可能筛选面板开着盖住认页图标：Esc 一下再认
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.5)
            image, method, hwnd = grab_game_image()
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

    # 详情页主按钮（强化/查看）区域，OCR 实测标定
    UPGRADE_BTN_AREA = (1550, 780, 1900, 880)
    # 强化页确认按钮搜索区（底部右侧），认不到就不点
    CONFIRM_AREA = (1300, 900, 1920, 1080)

    def _act(self, hwnd: int, action: Action) -> None:
        if action == Action.UPGRADE:
            self._do_upgrade(hwnd)
        elif action == Action.MARK_DISCARD:
            # TODO: 弃置走 R 键 + 确认弹窗，UI 未实测前不启用，先关详情
            press_key(hwnd, VK_ESCAPE)
            self.log.info("action=MARK_DISCARD (暂只关详情，不点弃置)")
        elif action == Action.SKIP:
            press_key(hwnd, VK_ESCAPE)
            self.log.info("action=SKIP")
        elif action == Action.STOP:
            press_key(hwnd, VK_ESCAPE)
            self.log.info("action=STOP")

    def _do_upgrade(self, hwnd: int) -> None:
        """点详情页「强化」→ 强化页找确认按钮点下去 → Esc 回列表。

        全程 OCR 认按钮，认不出就中断（手册 01）。
        """
        from zzz_ddtool.ocr import read_rect

        image, *_ = self._grab()
        hit = None
        for line in read_rect(image, self.UPGRADE_BTN_AREA):
            if "强化" in line.text:
                hit = line
                break
        if hit is None:
            self.log.warning("UPGRADE: 详情页没找到「强化」按钮，Esc 跳过")
            press_key(hwnd, VK_ESCAPE)
            return
        click_client(hwnd, (hit.left + hit.right) // 2, (hit.top + hit.bottom) // 2)
        time.sleep(1.2)

        image, page, *_ = self._grab()
        confirm = None
        for line in read_rect(image, self.CONFIRM_AREA):
            text = line.text.replace(" ", "")
            if "确认" in text or text == "强化":
                confirm = line
                break
        if confirm is None:
            self.log.warning("UPGRADE: 强化页没找到确认按钮(页面=%s)，Esc 放弃", page)
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.4)
            press_key(hwnd, VK_ESCAPE)
            return
        click_client(hwnd, (confirm.left + confirm.right) // 2, (confirm.top + confirm.bottom) // 2)
        time.sleep(1.5)
        self.log.info("action=UPGRADE clicked confirm at (%d,%d)", confirm.left, confirm.top)
        press_key(hwnd, VK_ESCAPE)  # 回详情/列表
        time.sleep(0.4)

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

