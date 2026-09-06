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
from zzz_ddtool.hotkeys import HotkeyController, RunnerStopped
from zzz_ddtool.layout import GRID_COLS, GRID_ORIGIN, GRID_ROWS, GRID_STEP, scale_point
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.models import Action, Decision, Disc
from zzz_ddtool.nav import NavError, goto_warehouse
from zzz_ddtool.ocr import OcrError, parse_disc
from zzz_ddtool.wininput import VK_ESCAPE, VK_R, click_client, press_key, press_key_once, wheel_client

# 等级标签 OCR 扫描区。右边界必须盖住第 9 列标签（中心 x≈1246，右缘≈1280），
# 旧值 1240 会把最后一列裁掉，导致每页最右边的盘子永远扫不到。
LABEL_SCAN_AREA = (60, 150, 1330, 1000)
# 单个盘子连续升阶上限（0→15 最多 5 阶，留余量防 OCR 读级错误导致死循环）
_MAX_STAGE_UPGRADES = 6


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

    def __init__(self, preset: Preset, *, hotkeys: bool = True) -> None:
        self.preset = preset
        self.log = get_logger("zzz_ddtool.runner")
        self.results: list[RunResult] = []
        self._processed = 0
        self.hotkeys = HotkeyController() if hotkeys else None

    def _checkpoint(self) -> None:
        """F9 暂停 / F10 停止的安全检查点（未启用热键时为空操作）。"""
        if self.hotkeys is not None:
            self.hotkeys.checkpoint()

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
        self.results = []
        self._processed = 0
        jobs = self._jobs()
        self.log.info("run start jobs=%d max_discs=%d (F9 暂停 / F10 停止)", len(jobs), self.preset.max_discs)
        try:
            for job in jobs:
                if self._processed >= self.preset.max_discs:
                    self.log.info("max_discs reached, stop jobs")
                    break
                try:
                    self._run_job(job)
                except RunnerStopped:
                    self.log.warning("F10 停止：已处理 %d 个盘子，返回已有结果", self._processed)
                    break
        finally:
            if self.hotkeys is not None:
                self.hotkeys.close()
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

        # 筛选过程会滚动列表找 0 级盘，先把列表滚回顶部再扫描，
        # 否则视口上方的行（可能含未处理的 0 级盘）永远不会被扫到。
        for _ in range(3):
            wheel_client(hwnd, gx, gy, 8)  # 正数向上滚，顶部会自动停
            time.sleep(0.3)

        prev_signature: tuple | None = None
        rescan = False  # True=点格子异常后已恢复，需重扫当前页（不翻页）
        up_counts: dict[tuple[int, int], int] = {}  # 每格升阶次数（跨重扫累计，防死循环）
        page_idx = 0
        while page_idx < self.preset.max_pages:
            self._checkpoint()
            if rescan:
                rescan = False
                prev_signature = None  # 允许同一页重扫
            cells = self._visible_cells(w, h)
            signature = tuple(sorted(cells))
            if not cells or signature == prev_signature:
                self.log.info("page %d empty or same as before, job done", page_idx)
                break
            prev_signature = signature
            self.log.info("page %d: %d cells to process", page_idx, len(cells))
            for row, col, cx, cy in cells:
                self._checkpoint()
                if self._processed >= self.preset.max_discs:
                    break
                cell_tag = f"r{row}c{col}"
                click_client(hwnd, cx, cy)
                time.sleep(0.5)
                detail_image, page, *_ = self._grab(allow_esc=False)
                if page in {"menu", "storage", "overworld"}:
                    # 点飞了（如误触弹层）：Esc 退出尝试回驱动盘页；
                    # 回得来就重扫当前页继续，回不来才中断。
                    self.log.warning("cell %s detail click left drive_disc (page=%s), try recover", cell_tag, page)
                    self._dbg_capture(f"click-anomaly-{page}")
                    press_key(hwnd, VK_ESCAPE)
                    time.sleep(0.6)
                    _, page2, *_ = self._grab(allow_esc=False)
                    if page2 == "drive_disc":
                        self.log.info("recovered to drive_disc, rescan page")
                        rescan = True
                        break
                    self.log.error("recover failed (page=%s), stop job", page2)
                    self._dbg_capture(f"recover-failed-{page2}")
                    return
                # drive_disc=没点开详情（空格）；unknown=详情开着盖住认页图标，直接解析
                disc = self._parse_disc(detail_image)
                if disc is None:
                    self.log.info("cell %s no disc, skip", cell_tag)
                    continue
                decision = decide(disc, sub_preset, accepted_ids=accepted_ids)
                self._record(job, disc, decision)
                key = (cx, cy)
                if decision.action == Action.STOP:
                    self.log.warning("cell %s decision STOP (%s), end job", cell_tag, decision.reason)
                    return
                if decision.action == Action.UPGRADE:
                    if up_counts.get(key, 0) >= _MAX_STAGE_UPGRADES:
                        self.log.warning(
                            "cell %s upgrades capped at %d, skip action", cell_tag, _MAX_STAGE_UPGRADES
                        )
                    else:
                        if disc.discarded:
                            # 先摘掉旧弃置标签再强化，保证终态统一（R 是开关，按 discarded 条件化）
                            press_key_once(hwnd, VK_R)
                            time.sleep(0.5)
                            self.log.info("cell %s UPGRADE：摘除旧弃置标签 (R)", cell_tag)
                        if self._do_upgrade(hwnd):
                            up_counts[key] = up_counts.get(key, 0) + 1
                            self._processed += 1
                            self.log.info(
                                "cell %s UPGRADE done, level %d -> next stage re-judge", cell_tag, disc.level
                            )
                        # 无论成败，_do_upgrade 结束后都在仓库页：重扫让升阶后的盘
                        # 在 3/6/9/12 档重新解析词条再判（accepted 白名单按档生效）
                        rescan = True
                        break
                elif decision.action == Action.MARK_DISCARD and disc.discarded:
                    # 已带标重判仍判弃置：空操作，不扣 max_discs 额度
                    self.log.info("cell %s already discarded, re-judge keeps it (no count)", cell_tag)
                else:
                    self._act(hwnd, decision.action, disc)
                    self._processed += 1
                    self.log.info(
                        "cell %s action=%s level=%d", cell_tag, decision.action.value, disc.level
                    )
                time.sleep(0.3)
            if rescan:
                continue
            self._checkpoint()
            press_key(hwnd, VK_ESCAPE)  # 关详情再翻页，滚轮才作用在网格上
            time.sleep(0.4)
            wheel_client(hwnd, gx, gy, -5)  # 翻下一页
            time.sleep(0.8)
            page_idx += 1

    def _visible_cells(self, width: int, height: int) -> list[tuple[int, int, int, int]]:
        """OCR 当前页「等级」标签定位格子，返回 (row, col, cx, cy)，按行优先排序去重。

        点的是图标中心（标签上方 GRID_LABEL_DY），已装备（头像）跳过。
        """
        image, _method, hwnd = grab_game_image()
        ox, oy = scale_point(*GRID_ORIGIN, width, height)
        sx, sy = scale_point(*GRID_STEP, width, height)
        from zzz_ddtool.equipped import tile_has_portrait
        from zzz_ddtool.layout import GRID_LABEL_DY
        from zzz_ddtool.ocr import read_rect

        cells: dict[tuple[int, int], tuple[int, int, int, int]] = {}
        for line in read_rect(image, LABEL_SCAN_AREA):
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
            cells[(row, col)] = (row, col, cx, cy)
        # 强制行优先（第1行从左到右，再第2行…），保证处理顺序确定、不跳格
        return [cells[key] for key in sorted(cells)]

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
    # 缩放页「等级00」文字区，>> 箭头在它右侧约 110px（实拍：文字 right≈1305，箭头中心≈1416）
    ZOOM_LEVEL_AREA = (1100, 150, 1550, 300)
    # 强化页按钮区（老大 2026-09-06 实拍截图标定）：
    # 「阶段添加」中心≈(1282,866)，「升级」中心≈(1511,866)。旧区 y=860 起把按钮拦腰切断导致 OCR 认不出。
    STAGE_ADD_AREA = (1150, 800, 1420, 940)
    UPGRADE_CONFIRM_AREA = (1420, 800, 1650, 940)

    def _act(self, hwnd: int, action: Action, disc: Disc) -> None:
        # UPGRADE 在 _run_job 里内联处理（需要 rescan/计数联动），这里只管其余动作。
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
        """按 Esc 逐次回退，每按一次截屏认页，回到驱动盘仓库页立即停。

        绝不盲按、绝不退出仓库（老大硬性要求：留在仓库页最安全）。
        返回 True=已回到仓库页；False=越界离开仓库（调用方应停止动作）。
        """
        for i in range(max_esc):
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.5)
            _, page, *_ = self._grab(allow_esc=False)
            if page == "drive_disc":
                self.log.info("esc back to grid after %d esc", i + 1)
                return True
            if page in {"menu", "storage", "overworld"}:
                self.log.error("esc back overshot to %s, stop", page)
                return False
            # unknown = 还在详情/缩放/强化层，继续 Esc
        self.log.error("esc back failed after %d esc, still unknown", max_esc)
        return False

    def _dbg_capture(self, tag: str) -> None:
        """中断点自动存图，供事后标定（存 captures 目录，命名 upg-<tag>）。"""
        try:
            from zzz_ddtool.capture import grab_game_image, save_image

            img, method, hwnd2 = grab_game_image()
            save_image(img, hwnd2, method, name=f"upg-{tag}")
        except Exception as exc:  # 存图失败不影响主流程
            self.log.warning("dbg capture %s failed: %s", tag, exc)

    def _do_upgrade(self, hwnd: int) -> bool:
        """老大截图标定的完整链路：

        详情页大按钮(查看) → 缩放页「>>」箭头 → 强化页「阶段添加」→「升级」。
        每一步 OCR 认按钮，认不出就存图 + 逐次验证 Esc 回仓库页。
        返回 True=完成一次升级；False=中断（调用方安排重扫）。
        """
        from zzz_ddtool.ocr import read_rect

        def _click_line(lines, needle: str, region_desc: str, tag: str) -> bool:
            for line in lines:
                if needle in line.text.replace(" ", ""):
                    click_client(hwnd, (line.left + line.right) // 2, (line.top + line.bottom) // 2)
                    return True
            self.log.warning("UPGRADE: 没找到%s，中断回退", region_desc)
            self._dbg_capture(tag)
            return False

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
            self.log.warning("UPGRADE: 详情页没找到「查看/强化」大按钮，回仓库页")
            self._dbg_capture("step1-查看")
            self._esc_back_to_grid(hwnd)
            return False
        click_client(hwnd, (hit.left + hit.right) // 2, (hit.top + hit.bottom) // 2)
        time.sleep(0.9)

        def _zoom_level_line():
            image, *_ = self._grab(allow_esc=False)
            for line in read_rect(image, self.ZOOM_LEVEL_AREA):
                if "等级" in line.text.replace(" ", ""):
                    return line
            return None

        # 2) 缩放页「>>」金箭头：OCR「等级00」右缘 +110px（实拍标定：
        #    等级文字 right≈1305，箭头按钮中心≈1416）
        level_line = _zoom_level_line()
        if level_line is None:
            self.log.warning("UPGRADE: 缩放页没找到「等级」，回仓库页")
            self._dbg_capture("step2-等级")
            self._esc_back_to_grid(hwnd)
            return False

        # 3) 点箭头进强化页（弹层），轮询等「阶段添加」出现；确认还在缩放页才重试
        ax = level_line.right + 110
        ay = (level_line.top + level_line.bottom) // 2
        stage_clicked = False
        for attempt in (1, 2):
            click_client(hwnd, ax, ay)
            lines: list = []
            for _wait in range(4):  # 弹层渲染可能慢，最多等 ~2.8s
                time.sleep(0.7)
                image, *_ = self._grab(allow_esc=False)
                lines = read_rect(image, self.STAGE_ADD_AREA)
                if any("阶段添加" in ln.text.replace(" ", "") for ln in lines):
                    stage_clicked = True
                    break
            if stage_clicked:
                for ln in lines:
                    if "阶段添加" in ln.text.replace(" ", ""):
                        click_client(hwnd, (ln.left + ln.right) // 2, (ln.top + ln.bottom) // 2)
                        break
                break
            self._dbg_capture(f"step3-未进{attempt}")
            level_line = _zoom_level_line()
            if level_line is None:
                break  # 不是缩放页也没有阶段添加，别乱点，回退
            ax = level_line.right + 110
            ay = (level_line.top + level_line.bottom) // 2
        if not stage_clicked:
            self.log.warning("UPGRADE: 两次都没进强化页，回仓库页")
            self._esc_back_to_grid(hwnd)
            return False
        time.sleep(0.8)

        # 4) 「升级」确认
        image, *_ = self._grab(allow_esc=False)
        if not _click_line(read_rect(image, self.UPGRADE_CONFIRM_AREA), "升级", "「升级」", "step4-无升级"):
            self._esc_back_to_grid(hwnd)
            return False
        time.sleep(1.5)
        self.log.info("action=UPGRADE 完成（阶段添加→升级）")
        # 5) 逐次验证 Esc 回仓库页（绝不盲按、绝不退出仓库）
        self._esc_back_to_grid(hwnd)
        return True

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

