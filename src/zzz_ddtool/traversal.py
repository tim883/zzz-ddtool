"""筛选后列表的全局位置模型（spec v0.6 §4、§5.4、§6.1）。

只做纯计算：编号/行列换算、原点几何与行相位、列归位、格位映射、连续前缀
进度提交、工作批次和恢复点。真实截图、滚动标定与点击由 runner / nav /
wininput 负责；本模块不 import PySide6，可在无游戏环境下直接回归。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from zzz_ddtool.layout import GRID_COLS, GRID_LABEL_DY, GRID_ORIGIN, GRID_STEP, scale_point

# 既有布局的只读参考（1920×1080 设计坐标），只用于复核实测原点，不是运行时真值。
REFERENCE_ORIGIN_Y = GRID_ORIGIN[1]
REFERENCE_ROW_PITCH = GRID_STEP[1]
# 布局容差：行距的 2%（spec §5.4）。
LAYOUT_TOLERANCE = 0.02


class PositionError(RuntimeError):
    """定位不可信、几何不可用或进度提交违约。上层转成任务 failed。"""


class CellStatus(str, Enum):
    """单个格位的解决状态（spec §6.1）。只有 DONE / SKIPPED 能推进 next_index。"""

    PENDING = "pending"     # 待处理
    DONE = "done"           # 处理完成
    SKIPPED = "skipped"     # 确认跳过（含扫描规则剔除）
    UNKNOWN = "unknown"     # 识别未知：进度屏障
    DEFERRED = "deferred"   # 延期边界：进度屏障


#: 可推进 next_index 的状态集合。
RESOLVED_STATUSES = frozenset({CellStatus.DONE, CellStatus.SKIPPED})


# --------------------------------------------------------------------------
# 编号与行列换算（spec §4.1）
# --------------------------------------------------------------------------


def global_row_col(index: int, cols: int = GRID_COLS) -> tuple[int, int]:
    """内部 0 基序号 → (0 基行, 0 基列)。"""
    if index < 0:
        raise PositionError(f"格子序号不能为负：{index}")
    return divmod(index, cols)


def global_index(row0: int, col0: int, cols: int = GRID_COLS) -> int:
    """(0 基行, 0 基列) → 内部 0 基序号。"""
    if row0 < 0 or not (0 <= col0 < cols):
        raise PositionError(f"行列越界：row0={row0} col0={col0}")
    return row0 * cols + col0


def display_row_col(index: int, cols: int = GRID_COLS) -> tuple[int, int]:
    """日志/GUI 用的 1 基行列。"""
    row0, col0 = global_row_col(index, cols)
    return row0 + 1, col0 + 1


#: 正常前进每批最多向下 2 行（spec §5.2）；开局恢复上界 H 由它和批数决定。
FORWARD_ROWS_PER_BATCH = 2


def forward_top_limit(batch_no: int) -> int:
    """第 batch_no 批（1 基）允许的最大整数顶部行索引：2*(b-1)（spec §5.1）。"""
    return FORWARD_ROWS_PER_BATCH * (max(1, batch_no) - 1)


def max_restore_rows(max_pages: int) -> int:
    """开局可计算的恢复请求行数上界 H（spec §5.1）。

    H = 0（P=1，无正常前进）或 2*(P-1)+1（含半行相位余量）。
    """
    if max_pages <= 1:
        return 0
    return FORWARD_ROWS_PER_BATCH * (max_pages - 1) + 1


def restore_request_rows(top_row0: int, phase_px: float, pitch_px: float) -> int:
    """从置顶原点返回保存视口的总请求行数 ceil(top + phase/pitch)（spec §5.3）。"""
    if pitch_px <= 0:
        raise PositionError(f"行距非法：{pitch_px}")
    if top_row0 < 0:
        raise PositionError(f"顶部行不能为负：{top_row0}")
    return math.ceil(top_row0 + phase_px / pitch_px)


def layout_reference(width: int, height: int) -> tuple[float, float]:
    """缩放后的既有布局参考：(首行图标中心 y, 行距)。只读复核用（spec §5.4）。"""
    origin_y = scale_point(0, REFERENCE_ORIGIN_Y, width, height)[1]
    pitch = scale_point(0, REFERENCE_ROW_PITCH, width, height)[1]
    return float(origin_y), float(pitch)


def check_layout_reference(origin_center_y: float, row_pitch_px: float, width: int, height: int) -> str | None:
    """实测原点与缩放布局不符时返回原因，符合返回 None。容差为参考行距的 2%。"""
    ref_origin, ref_pitch = layout_reference(width, height)
    if ref_pitch <= 0:
        return "布局参考行距非法"
    tolerance = LAYOUT_TOLERANCE * ref_pitch
    if abs(origin_center_y - ref_origin) > tolerance:
        return f"首行中心 {origin_center_y:.1f} 与参考 {ref_origin:.1f} 偏差超 2% 行距"
    if abs(row_pitch_px - ref_pitch) > tolerance:
        return f"行距 {row_pitch_px:.1f} 与参考 {ref_pitch:.1f} 偏差超 2% 行距"
    return None


def make_filter_signature(*, slot: int, sets: Sequence[str] = (), mainstats: Sequence[str] = (), tag: str = "") -> str:
    """由本轮不可变筛选配置生成签名（spec §4.2）。不含会变的运行时状态。"""
    raw = "|".join(
        (
            f"slot={int(slot)}",
            "sets=" + ",".join(sorted(str(item) for item in sets)),
            "mains=" + ",".join(sorted(str(item) for item in mainstats)),
            f"tag={tag}",
        )
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 网格几何：列中心与行反推（spec §4.1）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GridMetrics:
    """一个窗口客户区尺寸下的网格几何。

    列中心与 label_dy 来自既有布局常数，行几何来自原点图实测。所有"y"入参
    都指 OCR 读到的「等级」标签中心；图标中心 = 标签中心 - label_dy。
    """

    width: int
    height: int
    origin_center_y: float
    row_pitch_px: float
    col_centers: tuple[float, ...]
    col_pitch_px: float
    label_dy: float

    @classmethod
    def from_client(
        cls,
        width: int,
        height: int,
        *,
        origin_center_y: float,
        row_pitch_px: float,
        label_dy: float | None = None,
    ) -> "GridMetrics":
        ox = scale_point(GRID_ORIGIN[0], GRID_ORIGIN[1], width, height)[0]
        sx = scale_point(GRID_STEP[0], GRID_STEP[1], width, height)[0]
        centers = tuple(float(round(ox + sx * col)) for col in range(GRID_COLS))
        if row_pitch_px <= 0:
            raise PositionError(f"行距必须为正：{row_pitch_px}")
        if label_dy is None:
            label_dy = float(scale_point(0, GRID_LABEL_DY, width, height)[1])
        return cls(
            width=int(width),
            height=int(height),
            origin_center_y=float(origin_center_y),
            row_pitch_px=float(row_pitch_px),
            col_centers=centers,
            col_pitch_px=float(sx),
            label_dy=float(label_dy),
        )

    @property
    def col_count(self) -> int:
        return len(self.col_centers)

    def row_center_y(self, global_row0: int, viewport_top_row0: int, row_phase_px: float) -> float:
        """全局行在客户区里的图标中心 y（spec §4.1）。"""
        return (
            self.origin_center_y
            + (global_row0 - viewport_top_row0) * self.row_pitch_px
            - row_phase_px
        )

    def screen_row_of(self, icon_center_y: float, row_phase_px: float) -> int | None:
        """由图标中心 y 反推屏幕逻辑行；残差超过行距 2% 返回 None（不映射）。

        禁止用 OCR 聚类序号替代本结果，否则整行漏识别会让后续行向前补号。
        """
        raw = (icon_center_y - self.origin_center_y + row_phase_px) / self.row_pitch_px
        nearest = round(raw)
        if abs(raw - nearest) * self.row_pitch_px > LAYOUT_TOLERANCE * self.row_pitch_px:
            return None
        return int(nearest)

    def icon_center_of_label(self, label_center_y: float) -> float:
        """「等级」标签中心 y → 图标中心 y（spec §4.1 第 1 条）。"""
        return float(label_center_y) - self.label_dy

    def col_of(self, center_x: float) -> int | None:
        """标签中心 x 归到唯一列；落不进任何列（超出半列间距）返回 None。"""
        if not self.col_centers:
            return None
        best = min(range(len(self.col_centers)), key=lambda col: abs(center_x - self.col_centers[col]))
        if abs(center_x - self.col_centers[best]) > self.col_pitch_px / 2:
            return None
        return best

    def locate(
        self, center_x: float, label_center_y: float, *, viewport_top_row0: int, row_phase_px: float
    ) -> tuple[int, int, float] | None:
        """OCR 标签 → (屏幕逻辑行, 列, 图标中心 y)；任一不可信返回 None。"""
        icon_y = self.icon_center_of_label(label_center_y)
        screen_row0 = self.screen_row_of(icon_y, row_phase_px)
        if screen_row0 is None or screen_row0 < 0:
            return None
        col0 = self.col_of(center_x)
        if col0 is None:
            return None
        return screen_row0, col0, icon_y

    def center_x_of(self, col0: int) -> int:
        if not (0 <= col0 < self.col_count):
            raise PositionError(f"列越界：{col0}")
        return int(round(self.col_centers[col0]))


@dataclass(frozen=True)
class GridCell:
    """已映射到全局格位的一格；center_x/center_y 是图标中心（点击点）。"""

    global_index: int
    global_row0: int
    global_col0: int
    screen_row0: int
    screen_col0: int
    center_x: int
    center_y: int
    level: int = -1

    def display(self) -> tuple[int, int]:
        return self.global_row0 + 1, self.global_col0 + 1


def map_labels(
    metrics: GridMetrics,
    labels: Iterable[tuple[float, float, int]],
    *,
    viewport_top_row0: int,
    row_phase_px: float,
) -> tuple[list[GridCell], list[str]]:
    """原始标签 → 全局格位（spec §4.1、§5.5）。

    labels 的每项是 (标签中心 x, 标签中心 y, 等级)。行号由几何反推，不用标签
    序号；不可映射或同一行列重复归位记入 problems，调用方须据此把该位置当作
    进度屏障，而不是当作跳过。GridCell.center_y 存图标中心（点击点）。
    """
    cells: list[GridCell] = []
    problems: list[str] = []
    seen: set[tuple[int, int]] = set()
    for center_x, label_center_y, level in labels:
        position = metrics.locate(
            center_x, label_center_y, viewport_top_row0=viewport_top_row0, row_phase_px=row_phase_px
        )
        if position is None:
            problems.append(f"标签({center_x:.0f},{label_center_y:.0f})无法映射到行列")
            continue
        screen_row0, col0, icon_y = position
        if (screen_row0, col0) in seen:
            problems.append(f"屏幕第{screen_row0 + 1}行第{col0 + 1}列重复归位")
            continue
        seen.add((screen_row0, col0))
        row0 = viewport_top_row0 + screen_row0
        cells.append(
            GridCell(
                global_index=global_index(row0, col0),
                global_row0=row0,
                global_col0=col0,
                screen_row0=screen_row0,
                screen_col0=col0,
                center_x=int(round(center_x)),
                center_y=int(round(icon_y)),
                level=level,
            )
        )
    cells.sort(key=lambda cell: cell.global_index)
    return cells, problems


def split_stable_cells(
    cells: Sequence[GridCell], *, at_top: bool, at_bottom: bool
) -> tuple[list[GridCell], list[GridCell]]:
    """按 spec §5.2 拆成 (可点, 延期)：边界行只有到顶/到底时才点。"""
    if not cells:
        return [], []
    rows = {cell.screen_row0 for cell in cells}
    first, last = min(rows), max(rows)
    ready: list[GridCell] = []
    deferred: list[GridCell] = []
    for cell in cells:
        edge = (cell.screen_row0 == first and not at_top) or (cell.screen_row0 == last and not at_bottom)
        (deferred if edge else ready).append(cell)
    return ready, deferred


# --------------------------------------------------------------------------
# 滚动定位与滑块关系（spec §5.2、§5.4）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SliderMapping:
    """本任务实测的滑块—位移关系。只在当前 TaskJob 有效，不跨任务沿用。"""

    px_per_row: float
    error_px: float = 0.5

    def row_offset_interval(self, saved_px: float, current_px: float) -> tuple[float, float]:
        """已知保存滑块与当前滑块读数时，可能的全局行偏移区间（含量化误差）。"""
        if abs(self.px_per_row) < 1e-6:
            raise PositionError("滑块—行关系不可用（斜率为零）")
        center = (float(current_px) - float(saved_px)) / self.px_per_row
        half = abs(self.error_px / self.px_per_row)
        return center - half, center + half


def target_offset_unique(low: float, high: float, target_rows: float) -> bool:
    """区间必须包含目标且排除相邻整行，才允许 position_valid（spec §5.4）。"""
    if not (low <= target_rows <= high):
        return False
    return not (low <= target_rows - 1 <= high) and not (low <= target_rows + 1 <= high)


@dataclass
class ScrollOperation:
    """一次批量滚动的发送进度；暂停期间保留（spec §5.2）。"""

    requested_rows: int
    total_events: int
    sent_events: int = 0
    phase: str = "sending"
    confirmed: bool = False

    @property
    def remaining_events(self) -> int:
        return max(0, self.total_events - self.sent_events)

    def mark_event_sent(self) -> int:
        if self.sent_events >= self.total_events:
            raise PositionError("滚动事件已全部发送")
        self.sent_events += 1
        return self.sent_events

    def waiting(self) -> None:
        self.phase = "waiting"

    def confirm(self) -> None:
        self.phase = "confirmed"
        self.confirmed = True


# --------------------------------------------------------------------------
# 恢复点与任务状态（spec §4.2）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResumePoint:
    """强化前保存的视口快照；连续升阶期间不被强化后的焦点覆盖。"""

    viewport_top_row0: int
    row_phase_px: float
    next_index: int
    slider: tuple[int, int] | None = None
    at_top: bool = False
    at_bottom: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "next_index": self.next_index,
            "viewport_top_row0": self.viewport_top_row0,
            "row_phase_px": self.row_phase_px,
            "at_bottom": self.at_bottom,
        }


@dataclass
class TraversalState:
    """一个 TaskJob 的独立遍历状态。新任务必须新建实例，不继承上一任务进度。"""

    job_id: str
    filter_signature: str = ""
    next_index: int = 0
    viewport_top_row0: int | None = None
    row_phase_px: float | None = None
    row_pitch_px: float | None = None
    at_top: bool = False
    at_bottom: bool = False
    position_valid: bool = False
    active_index: int | None = None
    resume_point: ResumePoint | None = None
    navigation_profile: Mapping[str, Any] | None = None
    slider_mapping: SliderMapping | None = None
    task_restore_limit_rows: int = 0
    work_batch_id: int = 0
    work_batch_count: int = 0
    scroll_operation: ScrollOperation | None = None
    restore_attempt: int = 0
    restore_retries: int = 0
    _status: dict[int, CellStatus] = field(default_factory=dict, repr=False)

    # --- 编号 ---

    def row_col(self, index: int | None = None) -> tuple[int, int]:
        target = self.next_index if index is None else index
        return global_row_col(target)

    # --- 进度提交（唯一的连续前缀规则，spec §6.1）---

    def status_of(self, index: int) -> CellStatus:
        return self._status.get(index, CellStatus.PENDING)

    def set_status(self, index: int, status: CellStatus) -> CellStatus:
        if index < 0:
            raise PositionError(f"格子序号不能为负：{index}")
        self._status[index] = status
        return status

    def commit_prefix(self) -> int:
        """从 next_index 起推进已解决前缀；遇到待处理/未知/延期立即停住。

        已推进的格位状态随即丢弃：前缀之外仍需用到的判断才保留。
        """
        index = self.next_index
        while self._status.get(index) in RESOLVED_STATUSES:
            self._status.pop(index, None)
            index += 1
        self.next_index = index
        return index

    def block(self, index: int, status: CellStatus) -> None:
        """把某个格位标为未解决，用作进度屏障（未知/延期）。"""
        if status in RESOLVED_STATUSES:
            raise PositionError(f"{status} 不是屏障状态")
        self.set_status(index, status)

    # --- 视口位置 ---

    def confirm_viewport(
        self,
        top_row0: int,
        phase_px: float,
        *,
        row_pitch_px: float | None = None,
        at_top: bool | None = None,
        at_bottom: bool | None = None,
    ) -> None:
        """提交已确认的实际视口。未知时不得调用，也不得伪填 0。"""
        pitch = row_pitch_px if row_pitch_px is not None else self.row_pitch_px
        if top_row0 < 0:
            raise PositionError(f"顶部行不能为负：{top_row0}")
        if pitch is None or pitch <= 0:
            raise PositionError("行距未知，不能确认视口位置")
        if not (0 <= phase_px < pitch):
            raise PositionError(f"行相位越界：{phase_px}（行距 {pitch}）")
        self.viewport_top_row0 = int(top_row0)
        self.row_phase_px = float(phase_px)
        if row_pitch_px is not None:
            self.row_pitch_px = float(row_pitch_px)
        if at_top is not None:
            self.at_top = bool(at_top)
        if at_bottom is not None:
            self.at_bottom = bool(at_bottom)
        self.position_valid = True

    def invalidate_position(self) -> None:
        """定位失效：保留逻辑进度，只清掉位置可信度。"""
        self.position_valid = False

    def offset_px(self) -> float:
        if self.viewport_top_row0 is None or self.row_phase_px is None or self.row_pitch_px is None:
            raise PositionError("视口位置未知，无法计算像素偏移")
        return self.viewport_top_row0 * self.row_pitch_px + self.row_phase_px

    def in_restore_limit(self) -> bool:
        """保存视口的恢复请求是否在本任务已获准的滚动能力内。"""
        if self.resume_point is None:
            return False
        needed = restore_request_rows(
            self.resume_point.viewport_top_row0,
            self.resume_point.row_phase_px,
            self.row_pitch_px or 1.0,
        )
        return needed <= self.task_restore_limit_rows

    # --- 工作批次（spec §6.4）---

    def start_first_batch(self) -> int:
        """首次正式网格成功映射后调用，即使候选全被剔除也计第 1 批。"""
        self.work_batch_count = 1
        self.work_batch_id = 1
        return self.work_batch_id

    def advance_batch(self) -> int:
        """正常向下推进并确认到达不同视口后调用。恢复/重拍/标定不新建批。"""
        self.work_batch_count += 1
        self.work_batch_id = self.work_batch_count
        return self.work_batch_id

    def work_batches_left(self, max_pages: int) -> int:
        return max(0, int(max_pages) - self.work_batch_count)

    # --- 强化与恢复点（spec §6.3）---

    def begin_upgrade(self, active_index: int, *, slider: tuple[int, int] | None = None) -> ResumePoint:
        """进入强化前保存恢复点；连续升阶重复调用只保留第一次的视口。"""
        if self.resume_point is None:
            if self.viewport_top_row0 is None or self.row_phase_px is None:
                raise PositionError("视口位置未知，不能保存恢复点")
            self.resume_point = ResumePoint(
                viewport_top_row0=self.viewport_top_row0,
                row_phase_px=self.row_phase_px,
                next_index=active_index + 1,
                slider=slider,
                at_top=self.at_top,
                at_bottom=self.at_bottom,
            )
        self.active_index = int(active_index)
        return self.resume_point

    def finish_upgrade(self, status: CellStatus = CellStatus.DONE) -> int:
        """单件最终判断结束并已回到仓库后调用：提交该件进度，释放恢复点。"""
        if self.active_index is not None:
            self.set_status(self.active_index, status)
        self.active_index = None
        self.resume_point = None
        self.clear_scroll()
        return self.commit_prefix()

    def clear_resume(self) -> None:
        self.resume_point = None
        self.active_index = None

    # --- 滚动发送进度 ---

    def begin_scroll(self, requested_rows: int, total_events: int) -> ScrollOperation:
        if total_events < 0:
            raise PositionError(f"事件总数不能为负：{total_events}")
        self.scroll_operation = ScrollOperation(
            requested_rows=int(requested_rows), total_events=int(total_events)
        )
        return self.scroll_operation

    def mark_event_sent(self) -> int:
        if self.scroll_operation is None:
            raise PositionError("没有进行中的滚动发送")
        return self.scroll_operation.mark_event_sent()

    def confirm_scroll(self) -> None:
        if self.scroll_operation is not None:
            self.scroll_operation.confirm()

    def clear_scroll(self) -> None:
        self.scroll_operation = None

    # --- 恢复尝试计数（spec §5.3）---

    def begin_restore_attempt(self) -> int:
        self.restore_attempt += 1
        self.restore_retries = max(0, self.restore_attempt - 1)
        return self.restore_attempt

    def reset_restore_attempts(self) -> None:
        self.restore_attempt = 0
        self.restore_retries = 0

    # --- 状态快照（spec §7）---

    def status_payload(self) -> dict[str, Any]:
        """可直接并入 Runner 状态 payload 的新增键。"""
        index = self.active_index if self.active_index is not None else self.next_index
        row_display: int | None
        col_display: int | None
        if index < 0:
            row_display = col_display = None
        else:
            row_display, col_display = display_row_col(index)
        operation = self.scroll_operation
        return {
            "job_id": self.job_id,
            "next_index": self.next_index,
            "global_row": row_display,
            "global_col": col_display,
            "viewport_top_row": None if self.viewport_top_row0 is None else self.viewport_top_row0 + 1,
            "row_phase_px": self.row_phase_px,
            "row_pitch_px": self.row_pitch_px,
            "position_valid": self.position_valid,
            "restore_target": None if self.resume_point is None else self.resume_point.as_payload(),
            "work_batch_id": self.work_batch_id,
            "work_batch_count": self.work_batch_count,
            "scroll_sent_events": 0 if operation is None else operation.sent_events,
            "scroll_total_events": 0 if operation is None else operation.total_events,
            "restore_attempt": self.restore_attempt,
            "restore_retries": self.restore_retries,
        }


def check_forward_move(
    batch_no: int, previous_top_row0: int, new_top_row0: int, requested_rows: int
) -> None:
    """正常前进的位置校验（spec §5.1）：增长不超请求量，且不超批次上界。"""
    if requested_rows < 1:
        raise PositionError(f"正常前进请求行数必须为正：{requested_rows}")
    growth = new_top_row0 - previous_top_row0
    if growth < 0:
        raise PositionError(f"正常前进不能向上移动：{previous_top_row0} → {new_top_row0}")
    if growth > requested_rows:
        raise PositionError(f"超量滚动：请求 {requested_rows} 行，整数顶部行增长 {growth} 行")
    limit = forward_top_limit(batch_no)
    if new_top_row0 > limit:
        raise PositionError(f"第 {batch_no} 批顶部行 {new_top_row0} 超过上界 {limit}")
