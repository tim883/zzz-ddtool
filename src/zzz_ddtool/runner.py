"""自动化主循环：按任务链导航、筛选、OCR、决策、点击。

所有真实交互都通过 nav / gamefilter / wininput / ocr 走，本层只负责编排。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtGui import QImage

from zzz_ddtool.capture import grab_game_image
from zzz_ddtool.chain import TaskJob
from zzz_ddtool.config import Preset
from zzz_ddtool.decide import decide
from zzz_ddtool.gamefilter import apply_game_filter
from zzz_ddtool.hotkeys import HotkeyController, RunnerStopped
from zzz_ddtool.layout import GRID_COLS, GRID_ORIGIN, GRID_STEP, scale_point
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.models import MAX_LEVEL, Action, Decision, Disc
from zzz_ddtool.nav import NavError, goto_warehouse
from zzz_ddtool.ocr import OcrError, parse_disc
from zzz_ddtool.trace import Trace, action_name as trace_action, page_name as trace_page
from zzz_ddtool.wininput import VK_ESCAPE, VK_R, click_client, press_key, press_key_once, wheel_client

# 等级标签 OCR 扫描区。右边界必须盖住第 9 列标签（中心 x≈1246，右缘≈1280）；
# 上边界放宽到 80：列表滚动停在半行位置时，首行标签位置会随滚动偏移
LABEL_SCAN_AREA = (60, 80, 1330, 1020)
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
        payload = {"time": stamp, "phase": phase, "cell": cell, "message": message,
                   "processed": self._processed, "max_discs": self.preset.max_discs}
        if self.on_status is not None:
            self.on_status(dict(payload))
        """把运行状态写到 log/run-status.json，随时可查看工具正在干什么。"""
        try:
            import json

            from zzz_ddtool.paths import status_file

            status_file().parent.mkdir(parents=True, exist_ok=True)
            temp = status_file().with_suffix(".tmp")
            temp.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            temp.replace(status_file())
        except Exception:
            self.log.exception("运行状态写入失败")
        self.log.info("状态 %s：%s %s", phase, cell, message)

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
        self._status_lines = []
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

    def _run_job(self, job: TaskJob) -> None:
        self.log.info("job start slot=%d sets=%s mains=%s", job.slot, job.sets, job.mainstats)
        try:
            goto_warehouse()
            self.trace.ok("drive_disc", "navigate", "已到驱动盘仓库")
        except NavError as exc:
            self.trace.fail("unknown", "navigate", str(exc))
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
        w, h = image.width(), image.height()
        gx, gy = scale_point(640, 540, w, h)

        # 滚回列表顶部：滚一轮扫一次，某轮滚动后扫描结果不再变化 = 到顶
        # （真顶再往上滚页面纹丝不动，这是"到顶"的唯一铁证），保证从第一行开始处理。
        prev_sig: tuple | None = None
        for _top_round in range(6):
            top_cells = self._visible_cells(w, h)
            sig = getattr(self, "_grid_signature", tuple(sorted(top_cells)))
            if sig == prev_sig:
                self.log.info("scroll top confirmed after %d rounds", _top_round)
                break
            prev_sig = sig
            wheel_client(hwnd, gx, gy, 8)  # 正数向上滚
            time.sleep(0.35)

        prev_signature: tuple | None = None
        rescan = False  # True=点格子异常后已恢复，需重扫当前页（不翻页）
        resume_after_selection = False
        up_counts: dict[tuple[int, int], int] = {}  # 每格升阶次数（跨重扫累计，防死循环）
        page_idx = 0
        while page_idx < self.preset.max_pages:
            self._checkpoint()
            if self._processed >= self.preset.max_discs:
                break
            if rescan:
                rescan = False
                prev_signature = None  # 允许同一页重扫
            cells = self._visible_cells(
                w,
                h,
                skip_tagged=self.preset.skip_discarded,
                skip_max_level=self.preset.skip_max_level,
            )
            signature = getattr(self, "_grid_signature", tuple(sorted(cells)))
            if signature == prev_signature:
                self.log.info("page %d unchanged after scroll, job done", page_idx)
                break
            prev_signature = signature
            if resume_after_selection:
                selected = getattr(self, "_selected_grid_cell", None)
                if selected is None:
                    self.trace.fail("drive_disc", "scan", "重扫未找到唯一选中框，停止以免跳盘", image=self._scan_image)
                    raise RunnerError("重扫未找到唯一选中框，停止任务链")
                cells = [cell for cell in cells if cell[:2] > selected]
                self.log.info("rescan resumes after current selection r%dc%d", *selected)
            cells = self._stable_grid_cells(cells)
            self.log.info("page %d: %d cells to process", page_idx, len(cells))
            self.trace.ok(
                "drive_disc", "scan", f"第 {page_idx + 1} 页扫到 {len(cells)} 个盘", image=self._scan_image
            )
            self._set_status("scanning", f"page {page_idx}: {len(cells)} cells")
            maxed = False
            total_cells = len(cells)
            last_processed_cell = None
            for cell_no, (row, col, cx, cy, _lv) in enumerate(cells, start=1):
                self._checkpoint()
                if self._processed >= self.preset.max_discs:
                    maxed = True
                    break
                cell_tag = f"r{row}c{col}"
                # 队列位置 + 网格位置都要有：前者知道进度，后者知道点哪
                where = f"第 {cell_no} 个/共 {total_cells} 个 · 第{row + 1}行第{col + 1}列"
                self.trace.new_disc()  # 盘号决定这批图最后留不留
                click_client(hwnd, cx, cy)
                time.sleep(0.5)
                detail_image, page, *_ = self._grab(allow_esc=False)
                if page in {"menu", "storage", "overworld"}:
                    # 点飞了（如误触弹层）：Esc 退出尝试回驱动盘页；
                    # 回得来就重扫当前页继续，回不来才中断。
                    self.log.warning("cell %s detail click left drive_disc (page=%s), try recover", cell_tag, page)
                    self.trace.warn(
                        page, "click_cell", f"{where} 点开了 {trace_page(page)}，先按 Esc 退回",
                        image=detail_image,
                    )
                    press_key(hwnd, VK_ESCAPE)
                    time.sleep(0.6)
                    back_image, page2, *_ = self._grab(allow_esc=False)
                    if page2 == "drive_disc":
                        self.log.info("recovered to drive_disc, rescan page")
                        self.trace.note("已退回驱动盘仓库，重扫本页")
                        rescan = True
                        resume_after_selection = False
                        break
                    self.log.error("recover failed (page=%s), stop job", page2)
                    self.trace.fail(
                        page2, "esc", f"退回失败，停在 {trace_page(page2)}，收工", image=back_image
                    )
                    raise RunnerError(f"退回失败：{page2}，停止任务链")
                self.trace.ok("detail", "click_cell", where, image=detail_image)
                # 点格子后仓库右侧面板直接显示详情；放大详情页使用另一组识别区域。
                disc = self._parse_disc(detail_image)
                if disc is None:
                    self.trace.fail("detail", "parse", f"{where} 没读出盘面，停止任务链", image=detail_image)
                    raise RunnerError(f"{cell_tag} 盘面识别失败")
                self._processed += 1
                last_processed_cell = (row, col)
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
                key = (cx, cy)
                if decision.action == Action.STOP:
                    self._record(job, disc, decision)
                    self.log.warning("cell %s decision STOP (%s), stop chain", cell_tag, decision.reason)
                    self.trace.fail("detail", "decide", f"判断不了：{decision.reason}，停止任务链", image=detail_image)
                    raise RunnerError(f"{cell_tag} 判断不确定：{decision.reason}，停止任务链")
                self.trace.ok(
                    "detail",
                    "decide",
                    f"{trace_action(decision.action.value)}：{decision.reason}",
                )
                if decision.action == Action.UPGRADE:
                    if up_counts.get(key, 0) >= _MAX_STAGE_UPGRADES:
                        self.log.warning(
                            "cell %s upgrades capped at %d, skip action", cell_tag, _MAX_STAGE_UPGRADES
                        )
                        self.trace.warn(
                            "detail",
                            "upgrade",
                            f"{where} 已升 {up_counts.get(key, 0)} 次，到上限不再升",
                        )
                    else:
                        if disc.discarded:
                            # 先摘掉旧弃置标签再强化，保证终态统一（R 是开关，按 discarded 条件化）
                            press_key_once(hwnd, VK_R)
                            time.sleep(0.5)
                            self.log.info("cell %s UPGRADE：摘除旧弃置标签 (R)", cell_tag)
                            self.trace.note("这个盘有弃置标记，先按 R 摘掉再强化")
                        # 进强化页，在强化页循环判断+升级（升完留原地，不每次 Esc 回网格）
                        if self._enter_upgrade_page(hwnd):
                            self._upgrade_loop(
                                hwnd, job, key, cell_tag, sub_preset, accepted_ids, up_counts
                            )
                        else:
                            raise RunnerError(f"{cell_tag} 进入强化页失败，停止任务链")
                        # 注意：_enter_upgrade_page 失败时内部已 _esc_back_to_grid，
                        # 这里不再重复 Esc（重复 Esc 会把仓库页退到功能页）
                    # 升级/弃置完已回网格：等级排序可能变 + 已弃置的要跳过，重扫当前页
                    rescan = True
                    resume_after_selection = False  # 强化会改变排序，使用新列表重新筛选
                    break
                elif decision.action == Action.MARK_DISCARD and disc.discarded:
                    # 已带标重判仍判弃置：空操作，不扣 max_discs 额度
                    self.log.info("cell %s already discarded, re-judge keeps it (no count)", cell_tag)
                    self.trace.note(f"{where} 已经打过弃置标记，不动")
                elif decision.action == Action.SKIP and disc.discarded:
                    # 跳过弃置标记盘：不点击不按键不扣额度
                    self.log.info("cell %s has discard tag, skip (no count)", cell_tag)
                    self.trace.note(f"{where} 有弃置标记，跳过")
                else:
                    self._record(job, disc, decision)
                    self._act(hwnd, decision.action, disc)
                    self.log.info(
                        "cell %s action=%s level=%d", cell_tag, decision.action.value, disc.level
                    )
                    self.trace.ok(
                        "detail",
                        decision.action.value,
                        f"{where} 已{trace_action(decision.action.value)}",
                    )
                    self._set_status("action", decision.action.value, cell_tag)
                time.sleep(0.3)
                # 复用已截取的详情图，仅比较滚动条，无额外截图或 OCR。
                # 第 2/3 行正常点击继续使用队列；实际滚动才重扫、校正游标。
                from zzz_ddtool.equipped import grid_did_scroll

                if self._scan_image is not None and grid_did_scroll(self._scan_image, detail_image):
                    self.log.info("grid auto-scrolled after %s, refresh pending coordinates", cell_tag)
                    resume_after_selection = True
                    rescan = True
                    break
            if rescan:
                continue
            if maxed or self._processed >= self.preset.max_discs:
                # max_discs 截停：留在当前页，绝不翻页——
                # 翻页尾巴的 Esc 按在网格上会把仓库退到功能页（19:13 轮实锤）
                self.log.info("max_discs hit, stop without page flip")
                break
            if getattr(self, "_grid_at_bottom", False):
                self.log.info("last page processed, job done")
                break
            self._checkpoint()
            # 仓库右侧详情没有弹层，直接在网格滚动，不按 Esc 离开仓库。
            # 小步滚动保留选中盘作为重叠游标，将待处理的底行移进中间区域。
            cursor = last_processed_cell or (getattr(self, "_selected_grid_cell", None) if resume_after_selection else None)
            rows = getattr(self, "_grid_rows", ())
            resume_after_selection = cursor is not None and (not rows or cursor[0] > min(rows))
            wheel_client(hwnd, gx, gy, -1)
            time.sleep(0.8)
            page_idx += 1

    def _stable_grid_cells(self, cells):
        """只点击中间行；到顶/到底时补齐不会触发自动滚动的边界行。"""
        rows = getattr(self, "_grid_rows", ())
        if not rows:
            return cells
        first, last = min(rows), max(rows)
        at_top = self._grid_at_top
        at_bottom = self._grid_at_bottom
        stable = [cell for cell in cells if (cell[0] != first or at_top) and (cell[0] != last or at_bottom)]
        if len(stable) != len(cells):
            self.log.info("defer %d edge cells until scrolled into stable rows", len(cells) - len(stable))
        return stable

    def _visible_cells(
        self,
        width: int,
        height: int,
        *,
        skip_tagged: bool = False,
        skip_max_level: bool = True,
    ) -> list[tuple[int, int, int, int, int]]:
        """OCR 当前页「等级」标签定位格子，返回 (row, col, cx, cy, level)，行优先去重。

        点的是图标中心（标签上方 GRID_LABEL_DY），已装备（头像）跳过；
        skip_tagged=True 时弃置标记格在扫描阶段直接剔除（连点都不点）。
        """
        image, page, _method, hwnd = self._grab(allow_esc=False)
        if page != "drive_disc":
            self.trace.fail(page, "scan", "当前不是驱动盘仓库，停止扫描", image=image)
            raise RunnerError(f"扫描前页面错误：{page}，停止任务链")
        self._scan_image = image  # 整页长什么样，交给 trace 存（这一页的唯一现场）
        ox, oy = scale_point(*GRID_ORIGIN, width, height)
        sx, sy = scale_point(*GRID_STEP, width, height)
        from zzz_ddtool.equipped import grid_scroll_limits, grid_scroll_position, tile_has_discard_badge, tile_has_portrait, tile_is_selected
        from zzz_ddtool.layout import GRID_LABEL_DY
        from zzz_ddtool.ocr import read_rect

        # 行聚类：按标签 y 坐标聚成行，不依赖固定行数——列表滚动停在半行
        # 位置时，固定映射会把首行算到界外、末行超出上限，造成永远丢一行。
        import re

        labels: list[tuple[int, int, int]] = []
        for line in read_rect(image, LABEL_SCAN_AREA):
            text = line.text.replace(" ", "")
            if not text.startswith("等级"):
                continue
            m = re.search(r"等级(\d+)", text)
            level = int(m.group(1)) if m else -1
            labels.append(((line.left + line.right) // 2, (line.top + line.bottom) // 2, level))
        labels.sort(key=lambda p: p[1])
        rows: list[list[tuple[int, int, int]]] = []
        for lx, ly, lv in labels:
            if rows and abs(ly - rows[-1][-1][1]) <= 60:
                rows[-1].append((lx, ly, lv))
            else:
                rows.append([(lx, ly, lv)])
        cells: dict[tuple[int, int], tuple[int, int, int, int]] = {}
        selected_cells = []
        raw_cells = []
        label_dy = scale_point(0, GRID_LABEL_DY, width, height)[1]
        min_cy = scale_point(0, 97, width, height)[1]
        max_cy = scale_point(0, 983, width, height)[1]
        for r_idx, cluster in enumerate(rows):
            tile_cy = cluster[0][1] - label_dy
            # 行被窗口边缘切掉的（格子露出不到一半）不点
            if not (min_cy <= tile_cy <= max_cy):
                self.log.info("row y=%d half-cut at window edge, skip", tile_cy)
                continue
            for lx, _ly, lv in sorted(cluster):
                col = round((lx - ox) / sx)
                if not (0 <= col < GRID_COLS):
                    continue
                cx = ox + col * sx
                raw_cells.append((r_idx, col, cx, tile_cy, lv))
                design_cx = round(cx * 1920 / width)
                design_cy = round(tile_cy * 1080 / height)
                if tile_is_selected(image, design_cx, design_cy):
                    selected_cells.append((r_idx, col))
                if skip_max_level and lv >= 15:
                    self.log.info("cell r%dc%d level %d max, skip at scan", r_idx, col, lv)
                    continue
                if self.preset.skip_equipped and tile_has_portrait(image, cx - 56, tile_cy - 68, cx + 56, tile_cy + 68):
                    self.log.info("cell r%dc%d equipped, skip", r_idx, col)
                    continue
                if skip_tagged and tile_has_discard_badge(image, cx, tile_cy):
                    self.log.info("cell r%dc%d discard-tagged, skip at scan", r_idx, col)
                    continue
                cells[(r_idx, col)] = (r_idx, col, cx, tile_cy, lv)
        self._selected_grid_cell = selected_cells[0] if len(selected_cells) == 1 else None
        self._grid_rows = tuple(sorted({cell[0] for cell in raw_cells}))
        self._grid_at_top, self._grid_at_bottom = grid_scroll_limits(image)
        # 标签全相同时也要凭滚动条区分页面；不把选中框/弃置变化算作翻页。
        self._grid_signature = (tuple(raw_cells), grid_scroll_position(image))
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
        key: tuple[int, int],
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
                disc = replace(disc, rarity=self._detail_disc.rarity, locked=self._detail_disc.locked,
                               equipped=self._detail_disc.equipped, discarded=False)
            decision = decide(disc, sub_preset, accepted_ids=accepted_ids)
            if decision.action == Action.STOP:
                self._record(job, disc, decision)
                self.log.warning("cell %s 强化页判断不确定 (%s)，停止任务链", cell_tag, decision.reason)
                self.trace.fail("upgrade", "decide", f"判断不了：{decision.reason}，停止任务链", image=image)
                raise RunnerError(f"{cell_tag} 判断不确定：{decision.reason}，停止任务链")
            elif decision.action == Action.UPGRADE:
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
                up_counts[key] = up_counts.get(key, 0) + 1
                self._record(job, disc, decision)
                self.log.info("cell %s UPGRADE done, level %d -> 强化页重判", cell_tag, disc.level)
                self._set_status("upgraded", f"level {disc.level}", cell_tag)
                continue  # 留强化页再判断
            elif decision.action == Action.MARK_DISCARD:
                self.log.info("cell %s 强化页判弃置，Esc 回详情页按 R", cell_tag)
                self.trace.ok("upgrade", "decide", f"判为弃置：{decision.reason}")
                self._record(job, disc, decision)
                self._discard_from_upgrade(hwnd)
                return
            else:
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

