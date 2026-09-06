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
from zzz_ddtool.wininput import VK_ESCAPE, VK_R, click_client, press_key, press_key_once, wheel_client


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
                time.sleep(0.5)
                detail_image, page, *_ = self._grab(allow_esc=False)
                if page in {"menu", "storage", "overworld"}:
                    self.log.warning("detail click left drive_disc (page=%s), stop job", page)
                    return
                # drive_disc=没点开详情（空格）；unknown=详情开着盖住认页图标，直接解析
                disc = self._parse_disc(detail_image)
                if disc is None:
                    self.log.info("cell (%d,%d) no disc, skip", cx, cy)
                    continue
                decision = decide(disc, sub_preset, accepted_ids=accepted_ids)
                self._record(job, disc, decision)
                self._act(hwnd, decision.action)
                self._processed += 1
                if decision.action == Action.STOP:
                    self.log.warning("decision STOP (%s), end job", decision.reason)
                    return
                time.sleep(0.3)
            press_key(hwnd, VK_ESCAPE)  # 关详情再翻页，滚轮才作用在网格上
            time.sleep(0.4)
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

    def _grab(self, allow_esc: bool = True):
        image, method, hwnd = grab_game_image()
        from zzz_ddtool.screen import classify_image
        page = classify_image(image)
        if page == "unknown" and allow_esc:
            # 可能筛选面板开着盖住认页图标：Esc 一下再认。
            # 注意：点格子后的详情页也返回 unknown，调用方须用 allow_esc=False。
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

    # 详情页右下大按钮（查看/强化，永远存在），OCR 实测标定
    UPGRADE_BTN_AREA = (1550, 780, 1900, 880)
    # 缩放页「等级00」文字区，>> 箭头在它右侧约 95px
    ZOOM_LEVEL_AREA = (1100, 150, 1550, 300)
    # 强化页「阶段添加」与「升级」按钮搜索区（老大截图实测位置）
    STAGE_ADD_AREA = (1100, 860, 1500, 980)
    UPGRADE_CONFIRM_AREA = (1450, 860, 1700, 980)

    def _act(self, hwnd: int, action: Action) -> None:
        # 注意：这些动作都不按 Esc——详情保持常开，下一个格子点击直接切换。
        # 翻页前由 _run_job 统一 Esc。
        if action == Action.UPGRADE:
            self._do_upgrade(hwnd)
        elif action == Action.MARK_DISCARD:
            press_key_once(hwnd, VK_R)  # R 打弃置标签
            time.sleep(0.5)
            self.log.info("action=MARK_DISCARD (R)")
        elif action == Action.SKIP:
            self.log.info("action=SKIP")
        elif action == Action.STOP:
            self.log.info("action=STOP")

    def _do_upgrade(self, hwnd: int) -> None:
        """老大截图标定的完整链路：

        详情页大按钮(查看) → 缩放页「>>」箭头 → 强化页「阶段添加」→「升级」→ Esc 回列表。
        每一步 OCR 认按钮，认不出就中断回退（手册 01）。
        """
        from zzz_ddtool.ocr import read_rect

        def _click_line(lines, needle: str, region_desc: str) -> bool:
            for line in lines:
                if needle in line.text.replace(" ", ""):
                    click_client(hwnd, (line.left + line.right) // 2, (line.top + line.bottom) // 2)
                    return True
            self.log.warning("UPGRADE: 没找到%s，中断回退", region_desc)
            return False

        # 1) 详情页大按钮（0 级也显示「查看」）
        image, *_ = self._grab()
        hit = None
        for text in ("查看", "强化"):
            for line in read_rect(image, self.UPGRADE_BTN_AREA):
                if text in line.text.replace(" ", ""):
                    hit = line
                    break
            if hit:
                break
        if hit is None:
            self.log.warning("UPGRADE: 详情页没找到「查看/强化」大按钮，Esc 跳过")
            press_key(hwnd, VK_ESCAPE)
            return
        click_client(hwnd, (hit.left + hit.right) // 2, (hit.top + hit.bottom) // 2)
        time.sleep(0.9)

        # 2) 缩放页「>>」金箭头：在「等级00」右侧约 95px
        image, *_ = self._grab()
        level_line = None
        for line in read_rect(image, self.ZOOM_LEVEL_AREA):
            if "等级" in line.text.replace(" ", ""):
                level_line = line
                break
        if level_line is None:
            self.log.warning("UPGRADE: 缩放页没找到「等级」，中断回退")
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.4)
            press_key(hwnd, VK_ESCAPE)
            return
        ax = level_line.right + 95
        ay = (level_line.top + level_line.bottom) // 2
        click_client(hwnd, ax, ay)
        time.sleep(1.0)

        # 3) 强化页「阶段添加」加素材
        image, *_ = self._grab()
        if not _click_line(read_rect(image, self.STAGE_ADD_AREA), "阶段添加", "「阶段添加」"):
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.4)
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.4)
            press_key(hwnd, VK_ESCAPE)
            return
        time.sleep(0.8)

        # 4) 「升级」确认
        image, *_ = self._grab()
        if not _click_line(read_rect(image, self.UPGRADE_CONFIRM_AREA), "升级", "「升级」"):
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.4)
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.4)
            press_key(hwnd, VK_ESCAPE)
            return
        time.sleep(1.5)
        self.log.info("action=UPGRADE 完成（阶段添加→升级）")
        # 5) 强化页 → 缩放页 → 网格
        press_key(hwnd, VK_ESCAPE)
        time.sleep(0.4)
        press_key(hwnd, VK_ESCAPE)
        time.sleep(0.4)
        press_key(hwnd, VK_ESCAPE)
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

