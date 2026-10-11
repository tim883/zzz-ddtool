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
from contextlib import contextmanager
from dataclasses import dataclass, replace

from PySide6.QtGui import QImage

from zzz_ddtool import readiness, runevidence, scanperf
from zzz_ddtool.capture import CaptureError, client_size, find_game_hwnd, grab_game_image, save_image, window_pid
from zzz_ddtool.chain import TaskJob
from zzz_ddtool.config import Preset
from zzz_ddtool import actionwait
from zzz_ddtool.actionwait import InputRelease, wait_release
from zzz_ddtool.control import controlled_inputs, validated_inputs
from zzz_ddtool.decide import decide
from zzz_ddtool.filter_replay import FilterReplaySession
from zzz_ddtool.gamefilter import (
    _MEASURED_SAFETY_MS,
    apply_game_filter,
    reset_game_filter,
)
from zzz_ddtool.substatfilter import plan_substat_filter
from zzz_ddtool.hotkeys import HotkeyController, RunnerStopped
from zzz_ddtool.layout import (
    GRID_COLS,
    GRID_ROWS,
    LABEL_SCAN_AREA,
    UPGRADE_LEVEL_BAR,
    UPGRADE_MATERIAL_SLOTS,
    scale_point,
    scale_rect,
)
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.models import MAX_LEVEL, Action, Decision, Disc, next_stage_level
from zzz_ddtool.nav import NavError, goto_warehouse, reset_to_top
from zzz_ddtool.navigation import (
    COUNT_AREA,
    NavigationError,
    NavProfile,
    calibrate,
    load_profile,
    measure_layout,
    is_empty_warehouse,
    parse_warehouse_count,
    probe_environment,
    save_profile,
    scroll_rows,
    settle_and_read_anchor,
    anchor_screen_rows,
)
from zzz_ddtool.ocr import OcrError, parse_disc, read_action_state_fast
from zzz_ddtool.paths import stats_dir
from zzz_ddtool.readiness import (
    MATERIAL_REFUND,
    ReadinessInterrupted,
    ReadinessTimeout,
    WaitResult,
)
from zzz_ddtool.screen import DRIVE_DISC, classify_image, is_disc_modal, is_disc_preview, is_page
from zzz_ddtool.stats import (
    OUTCOME_DISCARDED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_UPGRADED,
    RunStats,
    outcome_for_action,
)
from zzz_ddtool.trace import Trace, action_name as trace_action, page_name as trace_page
from zzz_ddtool.traversal import (
    RESOLVED_STATUSES,
    VIEWPORT_SCROLLING,
    VIEWPORT_SINGLE_SCREEN,
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
    grid_snapshot,
    make_filter_signature,
    map_labels,
    max_restore_rows,
    single_screen_verdict,
    snapshot_change,
    split_stable_cells,
    tail_slots_confirmed,
)
from zzz_ddtool.wininput import InputError, VK_ESCAPE, VK_R, VK_T, click_client, press_key, press_key_once

# 单个盘子连续升阶上限（0→15 最多 5 阶，留余量防 OCR 读级错误导致死循环）
_MAX_STAGE_UPGRADES = 6
# 正常前进每次请求的行数上界（spec §5.1：只允许 1 或 2 行，维持 H）
_FORWARD_ROWS = 2
# 恢复批量滚动后的兜底稳定等待；标定给出 settle_wait_ms 时优先用它
_SETTLE_FALLBACK_MS = 500

# ---- §4.3 执行器动作的就绪参数（本轮固定表值，不从旧等待档案读取）----
#: 从真实释放点计的就绪超时（ms）。
_READY_TIMEOUT_MS: dict[str, int] = {
    "select_disc": 3180,
    "open_preview": 5180,
    "open_upgrade": 8180,
    "material_ready": 3180,
    "upgrade_result": 8180,
    "refund_confirm": 8180,
    "return_warehouse": 3580,
}
#: 无测量时的总下限（ms）；有实测档案时由 actionwait 提供。
#: 2026-10-12 作者裁定：与 §4.2 同规矩 = 真机实测（取有效样本最大值，见《等待时间表》）
#: + _MEASURED_SAFETY_MS；不准再手写 +200。
_READY_FLOOR_MS: dict[str, int] = {
    "select_disc": 320 + _MEASURED_SAFETY_MS,
    "open_preview": 600 + _MEASURED_SAFETY_MS,
    "open_upgrade": 327 + _MEASURED_SAFETY_MS,
    "material_ready": 747 + _MEASURED_SAFETY_MS,
    "upgrade_result": 350 + _MEASURED_SAFETY_MS,
    "refund_confirm": 1277 + _MEASURED_SAFETY_MS,
    "return_warehouse": 1411 + _MEASURED_SAFETY_MS,
}
#: 标签动作（§4.3）：R/T 单次按键后的总下限；四项共用一个常量，取四项实测最大值
#: （label_save 127 / label_discard 87 / clear_discard 135 / label_unlock_before_discard 125）。
_LABEL_FLOOR_MS = 135 + _MEASURED_SAFETY_MS
#: 返还弹窗未接续结果等待时的收尾预算（§4.3 refund_dismiss；与 refund_confirm 共用实测 1277）。
_REFUND_DISMISS_FLOOR_MS = 1277 + _MEASURED_SAFETY_MS

# ---- 就绪等待（§6.2 material_ready）的像素差分阈值（§10 裁量）----
# 描述子＝经验/预计等级条 ROI 的「饱和度」（max(R,G,B)-min(R,G,B)）再按各帧自身 p85 归一化。
# 空条与底色是灰（饱和度恒为 0），只有填充色像素有饱和度 → 彩色掩码天然排掉灰区；
# 按本帧 p85 归一化又抵消了填充色上那层亮度呼吸动画。变化阈值必须大于稳定阈值。
# 真实帧对核对（步骤 9 真机，2026-10-10）：
#   就绪态 24 帧两两最大差 0.008；操作前空条 → 就绪态 ≥0.63（空条归一化后全 0）→ 分离度约 78x。
_MATERIAL_BAR_CHANGE_THRESHOLD = 0.20
_MATERIAL_BAR_STABLE_THRESHOLD = 0.05
#: 逐格「是否已填充」的二值判据（归一化饱和度）；24 帧就绪态离该阈值最差裕量 0.129。
_MATERIAL_BAR_FILL_THRESHOLD = 0.5
#: 本帧饱和度 p85 低于此值＝整条无彩色（空条/尚未填充）→ 归一化后视为全 0。
_MATERIAL_BAR_MIN_SATURATION = 12.0


def _jsonable_key(key):
    """§7.1.1：state_key 的嵌套 tuple 按数组保存原值，不只存不可复算的 hash。"""
    if isinstance(key, (tuple, list)):
        return [_jsonable_key(item) for item in key]
    if isinstance(key, dict):
        return {str(name): _jsonable_key(value) for name, value in key.items()}
    return key

# 装备判据暂不启用：判据缺独立答案卷、识别不准（2026-10-05 用户确认，后续要恢复）。
# 恢复方法：本常量改回 True 即可；tile_has_portrait、YAML 的 skip_equipped 字段、界面控件
# 与扫描分支全部原样保留，无需其他改动。
SKIP_EQUIPPED_ENABLED = False


@dataclass
class _LabelContext:
    before: Disc | None
    generation: int | None
    size: tuple[int, int] | None = None


class RunnerError(RuntimeError):
    pass


class MaterialShortfall(RunnerError):
    """升级素材不足：本档实际升级后未达到下一档期望等级（Spec §6.2 upgrade_result）。

    ``from_level`` 为本档实际升级前 ``before.level``；``expected_level`` 为发升级前冻结的
    ``next_stage_level(before.level)``；``actual_level`` 为有限确认末次可读的真实低等级。
    不能取素材添加后的预计等级，也不能从中文报错文本反推属性。
    """

    def __init__(self, from_level: int, expected_level: int, actual_level: int) -> None:
        self.from_level = from_level
        self.expected_level = expected_level
        self.actual_level = actual_level
        super().__init__(
            f"升级素材不足：从 {from_level} 级升级后期望到 {expected_level} 级，"
            f"实际只有 {actual_level} 级，停止任务链"
        )


@dataclass(frozen=True)
class RunResult:
    job_id: str
    action: str
    reason: str
    score: float
    level: int


@dataclass(frozen=True)
class NavigationPrep:
    """筛选后的准备结果（改造方案 §5.1）。

    mode=single_screen 时 ``scan`` 是判定成立那一帧的扫描结果，主循环直接复用：
    既不置顶、也不探底，首批就是全部待处理行。mode=scrolling 时只置顶一次，
    首批网格由主循环自己扫。
    """

    profile: NavProfile
    mode: str
    total: int | None = None
    scan: tuple | None = None


@dataclass(frozen=True)
class RecoveryCache:
    """本批已验证的候选；只在同任务、同筛选、同尺寸下恢复使用。"""

    job_id: str
    filter_signature: str
    width: int
    height: int
    total: int | None
    cells: tuple[GridCell, ...]
    skips: dict[int, str]


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
        # 运行统计：收尾时写成 log/stats/<tag>.json + 一段中文摘要进文本日志。
        self.stats = RunStats(tag=self.trace.tag)
        self.results: list[RunResult] = []
        self._processed = 0
        self._job_started_at = 0
        self._job_max_discs = preset.max_discs
        self._scan_image = None  # 最近一次整页扫描的图（存盘用）
        self.hotkeys = controller if controller is not None else (HotkeyController() if hotkeys else None)
        self.on_status = on_status
        self.outcome = "idle"
        self.end_reason = ""
        #: Spec §7.1 run_total：completed/user_stopped/error（未进入 Runner 时不产生根事件）。
        self.termination_kind = ""
        self._detail_disc = None
        # ---- 就绪等待基础（§6.1、§6.2）----
        #: 本任务绑定的环境身份（供等待作用域使用），由 _bind_instance 填入（§3.4）。
        self._env = None
        #: 本轮 release_waits 作用域句柄与身份（同环境复用，变更先退出旧作用域）。
        self._release_scope_cm = None
        self._release_scope_key: tuple | None = None
        #: 本轮 run 的退出栈（统一撤销 release_waits 等作用域）。
        self._scope_stack = None
        #: 本轮 §7.1 等待明细收集器（run() 建立，收尾并入执行输入记录）。
        self._wait_receipts: list = []
        #: 各动作的真实释放回执（§3.1：截自 click_client/press_key_once 的释放时刻）。
        self._material_release: InputRelease | None = None
        self._upgrade_release: InputRelease | None = None
        self._refund_release: InputRelease | None = None
        #: 生产等待共用的控制回调：暂停区间查询（未启用控制器返回空元组）。
        self._pause_intervals_fn = None
        #: 本档「材料返还确认」输入的事件 ID（第二次 upgrade_result 的 trigger_input_id）。
        self._refund_input_id: str | None = None
        #: 本盘 disc_total 的事件 ID（_process_cell 建立，等待事件挂它下面）。
        self._disc_event_id: str | None = None
        #: 本档「发升级」输入的事件 ID（upgrade_result 等待的 upgrade_input_id）。
        self._upgrade_input_id: str | None = None
        #: 最近一次 _wait_ready 产生的 wait_total 事件 ID（供 §6.2.1 第二次等待引用）。
        self._last_wait_id: str | None = None
        #: 首档 seed 建立时的暂停代次（open_upgrade 完成时刻；§6.2 首档接续证据）。
        self._upgrade_entry_generation: int | None = None
        #: 当前正在计时的 disc_total 区间句柄（供内部方法补 input_sent/result）。
        self._disc_span = None
        #: §9.2 逐任务执行记录（job_index/skipped/processed/end_reason），每轮 run 重建。
        self._job_records: list[dict] = []
        #: §9.2 coverage：本轮实际发出的标签输入（label_save/label_discard/unlock），
        #: 只在真实按键且复核确认的执行点记录，已达目标未按键不记。
        self._label_inputs: list[dict] = []
        #: §9.2 coverage：本轮实际捕获的 MaterialShortfall 证据（无则 None）。
        self._shortfall_exception: dict | None = None
        self._input_records: list[dict] = []
        self._input_by_call: dict = {}
        self._last_input_id: str | None = None
        self._input_metadata: dict = {}
        self._latest_decision: dict | None = None
        self._current_job_index: int | None = None
        self._evidence_error: str | None = None
        self._run_exception: dict | None = None
        # 本任务绑定实例与实测布局
        self._hwnd: int | None = None
        self._pid: int | None = None
        self._state: TraversalState | None = None
        self._profile: NavProfile | None = None
        self._origin_center_y: float = 0.0
        self._row_pitch: float = 175.0
        # 强化后恢复时的校验扫描结果，直接交给主循环复用（避免重复 OCR 目标列表）
        self._restored_scan: tuple | None = None
        self._restored_scan_reused = False
        # 最近一次扫描的末尾占位判定（spec §5.4）：只有它和锚点端点行为同时成立
        # 才允许把视口标记为到底。
        self._tail_slots_confirmed = False
        # 最近一次扫描的原始可观察量：确认空占位、拿不准占位、筛选后总数。
        # 单屏判定与强化前后快照对比都用这一份，不重复采样（方案 §7）。
        self._last_empties: set[tuple[int, int]] = set()
        self._last_unclear: dict[tuple[int, int], str] = {}
        self._last_count: int | None = None

    @property
    def processed_disc_count(self) -> int:
        return self._processed

    def _checkpoint(self) -> int | None:
        """F9 暂停 / F10 停止的安全检查点（未启用热键时为空操作）。"""
        if self.hotkeys is not None:
            paused = self.hotkeys.paused
            if paused:
                self._set_status("paused", "已暂停，F9 或恢复按钮继续")
            generation = self.hotkeys.checkpoint()
            if paused:
                self._set_status("running", "已恢复")
            return generation if type(generation) is int else None
        return None

    _status_lines: list[str] = []

    def _set_status(
        self, phase: str, message: str = "", cell: str = "", report_path: str = ""
    ) -> None:
        """运行状态三处落点：run-status.json + 游戏画面悬浮层 + 日志。

        report_path：跑完后的结果统计详细日志（.md）路径。只在收尾那一次带上，
        界面据此把状态栏换成结果统计的一行摘要 + 详细日志路径；运行中的状态不带它，
        界面照旧显示进度。界面**不显示**详细日志正文，只给路径。
        """
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
            "max_discs": sum(self._job_limit(job) for job in self._jobs() if job.accepted is not None),
            "job_processed": self._processed - getattr(self, "_job_started_at", 0),
            "job_max_discs": getattr(self, "_job_max_discs", self.preset.max_discs),
        }
        if self._state is not None:
            payload.update(self._state.status_payload())
        if report_path:
            payload["stats_path"] = report_path
            payload["stats_headline"] = self.stats.headline()
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
        # 游戏若以管理员运行而本工具是普通权限，UIPI 会静默拦掉全部输入，
        # 现场表现是「导航转 8 圈失败」，极易被误当成识别/导航 bug。这里先拦。
        from zzz_ddtool.wininput import input_blocked_reason

        blocked = input_blocked_reason(self._hwnd)
        if blocked:
            raise RunnerError(blocked)
        # §3.4：绑定与权限检查通过后、返回 hwnd 前探测环境并进入 release_waits 作用域，
        # 使首个 _ensure_profile 的 reset_game_filter 也使用本机策略。
        env = probe_environment(self._hwnd)
        self._env = env
        self._enter_release_scope(env)
        return self._hwnd

    def _enter_release_scope(self, env) -> None:
        """§3.4：进入 release_waits 作用域；同环境复用，绑定/环境变更先退出旧作用域。"""
        key = None
        if env is not None:
            key = (
                env.machine_id,
                env.client_width,
                env.client_height,
                env.window_dpi,
                env.game_exe_path,
                env.game_build,
            )
        if self._release_scope_cm is not None and key == self._release_scope_key:
            return
        self._close_release_scope()
        cm = actionwait.release_waits(env)
        cm.__enter__()
        self._release_scope_cm = cm
        self._release_scope_key = key
        if self._scope_stack is not None:
            self._scope_stack.callback(self._close_release_scope)

    def _close_release_scope(self) -> None:
        """退出当前 release_waits 作用域（可重复调用）。"""
        cm, self._release_scope_cm = self._release_scope_cm, None
        if cm is not None:
            cm.__exit__(None, None, None)

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

    # ---------------------------------------------------------------- 就绪等待基础（§6）

    #: §6.2 return_warehouse 每次 Esc 允许的更浅层进展（三条边）。
    RETURN_EDGES: frozenset = frozenset(
        {("modal", "preview"), ("modal", "warehouse"), ("preview", "warehouse")}
    )

    def _pause_intervals(self, since: float, until: float) -> tuple[tuple[float, float], ...]:
        """§6.1 pauses 回调：未启用控制器时返回空元组。"""
        if self.hotkeys is None:
            return ()
        return self.hotkeys.pause_intervals(since, until)

    def _wait_policy(self, action_id: str) -> actionwait.ActionWaitPolicy:
        """§4.3：就绪下限唯一来源（有效实测或该动作保守值）；timeout 见 _READY_TIMEOUT_MS。"""
        return actionwait.policy_for(action_id, fallback_ms=_READY_FLOOR_MS[action_id])

    # ---------------------------------------------------------------- 层级与返还（§6.2）

    def _return_layer(self, image) -> str:
        """§6.2 return_warehouse 的纯读取层级适配，复用现有 screen 判据。

        只接受 modal / preview / warehouse / unknown：错页（menu/storage/overworld）
        沿用原失败路径抛 RunnerError；有 ``is_disc_modal`` 的关闭 X → modal；
        无 X 且严格命中 ``DRIVE_DISC`` → warehouse；前两者均不满足且 ``is_disc_preview``
        命中 → preview；其他 → unknown。不用任意像素变化认层。
        """
        page = classify_image(image)
        if page in {"menu", "storage", "overworld"}:
            raise RunnerError(f"返回层级时停在{trace_page(page)}，停止任务链")
        if is_disc_modal(image):
            return "modal"
        if is_page(image, DRIVE_DISC):
            return "warehouse"
        if is_disc_preview(image):
            return "preview"
        return "unknown"

    def _find_material_refund(self, image):
        """§6.2.1：只读传入帧找「材料返还」确认行；不截图、不输入。

        去空格后至少一行含 ``MATERIAL_REFUND_TITLE``，且按 OCR 原顺序找到第一行含
        「确认」的文字才返回该行；任一条件缺失返回 None。``OcrError`` 原样传播。
        """
        from zzz_ddtool import ocr as _ocr

        lines = _ocr.read_rect(image, (0, 0, image.width(), image.height()))
        title = self.MATERIAL_REFUND_TITLE
        if not any(title in line.text.replace(" ", "") for line in lines):
            return None
        for line in lines:
            if "确认" in line.text.replace(" ", ""):
                return line
        return None

    # ---------------------------------------------------------------- 证据与判据工具

    @staticmethod
    def _core_of(disc: Disc | None):
        """§7.1.1 evidence.core：词条按名称排序，未读取为 null。"""
        if disc is None:
            return None
        return {
            "level": disc.level,
            "rarity": disc.rarity,
            "substats": [
                {"name": substat.name, "rolls": substat.rolls}
                for substat in sorted(disc.substats, key=lambda s: s.name)
            ],
        }

    @staticmethod
    def _core_key(disc: Disc | None):
        """对象核心盘面 key：等级、稀有度及按名称排序的 (name, rolls)。"""
        if disc is None:
            return None
        return (
            disc.level,
            disc.rarity,
            tuple(sorted((s.name, s.rolls) for s in disc.substats)),
        )

    @staticmethod
    def _same_upgrade_object(disc: Disc | None, before: Disc) -> bool:
        """强化页对象接续：等级与可见词条一致即可，稀有度不单方向阻塞就绪。

        §6.2 表中 `open_upgrade` 的 ready 条件是「实际等级与 before 一致，词条区域
        完成渲染」；§6.3 只要求两次转场的**等级/布局证据**与 before 一致，并明确
        「合法 rarity=None 不单独阻塞就绪」。

        强化页不显示等级上限（只有当前等级 `12` 与材料进度 `2040/5760`），
        `parse_disc_upgrade_page` 的 rarity 必为 None；而 before 来自详情页
        （读到「等级12/15」→ 上限 15 → S）。若按 `_core_key` 全字段比较，这条等待
        在真机上永远不就绪（2026-10-10 真机标定实测：`open_upgrade` 5/5 超时，
        探针 12 帧全为 `layer=upgrade, stage_add=True, 词条与 before 一致`
        却从未形成稳定帧对）。故稀有度只在两侧都已知时比较。
        """
        if disc is None:
            return False
        if disc.level != before.level:
            return False
        if (
            disc.rarity is not None
            and before.rarity is not None
            and disc.rarity != before.rarity
        ):
            return False
        return (
            tuple(sorted((s.name, s.rolls) for s in disc.substats))
            == tuple(sorted((s.name, s.rolls) for s in before.substats))
        )

    @staticmethod
    def _evidence(*, core=None, return_layer=None, label_state=None,
                  target_selected=None, refund=None, **extra):
        evidence = {
            "core": core,
            "return_layer": return_layer,
            "label_state": label_state,
            "target_selected": target_selected,
            "refund": refund,
        }
        evidence.update(extra)
        return evidence

    def _tile_selected_for_cell(self, image, cell: GridCell):
        """§6.2：目标格位的选中证据；客户区像素换成 1920×1080 设计坐标再判。"""
        from zzz_ddtool.equipped import tile_is_selected

        width, height = image.width(), image.height()
        if width <= 0 or height <= 0:
            return None
        design_x = round(cell.center_x * 1920 / width)
        design_y = round(cell.center_y * 1080 / height)
        return tile_is_selected(image, design_x, design_y)

    def _has_stage_add(self, image) -> bool:
        from zzz_ddtool.ocr import read_rect

        return any("阶段添加" in ln.text.replace(" ", "") for ln in read_rect(image, self.STAGE_ADD_AREA))

    def _has_upgrade_button(self, image) -> bool:
        from zzz_ddtool.ocr import read_rect

        return any("升级" in ln.text.replace(" ", "") for ln in read_rect(image, self.UPGRADE_CONFIRM_AREA))

    def _zoom_level_of(self, image):
        """缩放/满级展示页的「等级xx」行；读不到返回 None。"""
        from zzz_ddtool.ocr import read_rect

        for line in read_rect(image, self.ZOOM_LEVEL_AREA):
            if "等级" in line.text.replace(" ", ""):
                return line
        return None

    @staticmethod
    def _level_of_line(line) -> int | None:
        if line is None:
            return None
        match = re.search(r"等级(\d+)", line.text.replace(" ", ""))
        return int(match.group(1)) if match else None

    def _material_bar_crop(self, image):
        """§6.2 material_ready：经验/预计等级条的归一化饱和度数组（各帧同一 ROI/口径）。

        掩码＝**彩色掩码**：空条与底色是灰（饱和度 0），只有填充色像素有饱和度，
        灰区因此自动被排除；再按本帧 p85 归一化，抵消填充色上的亮度呼吸动画。
        整条都无彩色（空条/尚未填充）时返回全 0，与「已更新」明显分离。
        """
        import numpy as np

        from zzz_ddtool.ocr import qimage_to_bgr

        if image is None or image.isNull():
            return None
        left, top, right, bottom = scale_rect(
            *UPGRADE_LEVEL_BAR, image.width(), image.height()
        )
        if right <= left or bottom <= top:
            return None
        crop = image.copy(left, top, right - left, bottom - top)
        bgr = qimage_to_bgr(crop)
        if bgr.ndim != 3:
            return None
        blue = bgr[:, :, 0].astype("float32")
        green = bgr[:, :, 1].astype("float32")
        red = bgr[:, :, 2].astype("float32")
        saturation = np.maximum(np.maximum(red, green), blue) - np.minimum(
            np.minimum(red, green), blue
        )
        scale = float(np.percentile(saturation, 85))
        if scale < _MATERIAL_BAR_MIN_SATURATION:
            return np.zeros_like(saturation)
        return saturation / scale

    @staticmethod
    def _mean_abs_diff(a, b) -> float | None:
        import numpy as np

        if a is None or b is None or a.shape != b.shape:
            return None
        return float(np.abs(a - b).mean())

    @staticmethod
    def _material_bar_key(crop):
        """候选像素状态的粗 key（可 JSON 化，非物品 ID）：逐格「是否已填充」。

        归一化饱和度 ≥ 填充阈值记 1。填充过程从左到右逐格点亮（key 变化→未稳定），
        填满后各格稳定为 1，与呼吸动画的亮度强弱无关。
        """
        import numpy as np

        if crop is None:
            return None
        bits: list[int] = []
        for band in np.array_split(crop, 4, axis=0):
            for block in np.array_split(band, 4, axis=1):
                bits.append(1 if float(block.mean()) >= _MATERIAL_BAR_FILL_THRESHOLD else 0)
        return tuple(bits)

    # ---------------------------------------------------------------- probe 工厂（§6.1、§6.2）

    def _make_ready_probe(
        self, kind: str, *, before: Disc | None = None, before_frame=None,
        cell: GridCell | None = None, expected_level: int | None = None,
    ):
        """§6.1/§6.2：生产与标定唯一共用的纯读取 probe 构造入口。

        非法 kind / 缺必填项在构造时即抛 ValueError（第一次 probe 之前）；返回值是
        一个闭包，只取绑定窗口新帧并判定，不发送任何输入。每个动作建立新的局部采样
        状态，不跨动作复用。异常原样向上传播（§6.1.1）。
        """
        readiness.validate_probe_params(
            kind,
            {
                "cell": cell,
                "before": before,
                "before_frame": before_frame,
                "expected_level": expected_level,
            },
        )
        if kind == "select_disc":
            return self._probe_select_disc(cell)
        if kind == "open_preview":
            return self._probe_open_preview(before)
        if kind == "open_upgrade":
            return self._probe_open_upgrade(before)
        if kind == "material_ready":
            return self._probe_material_ready(before, before_frame)
        if kind == "upgrade_result":
            return self._probe_upgrade_result(before, expected_level)
        return self._probe_return_warehouse(before_frame)

    def _probe_select_disc(self, cell: GridCell):
        def probe() -> readiness.WaitSample:
            frame, page, _method, _hwnd = self._grab(allow_esc=False)
            captured = time.monotonic()
            if page in {"menu", "storage", "overworld"}:
                return readiness.WaitSample(
                    frame=frame,
                    captured_at=captured,
                    ready=False,
                    state_key=("select_disc", page, "wrong_page"),
                    evidence=self._evidence(return_layer=page),
                )
            selected = self._tile_selected_for_cell(frame, cell)
            disc = None
            if page == DRIVE_DISC or is_disc_modal(frame):
                disc = parse_disc(frame)
            ready = bool(selected is True and disc is not None)
            return readiness.WaitSample(
                frame=frame,
                captured_at=captured,
                ready=ready,
                state_key=("select_disc", page, cell.global_index, selected, self._core_key(disc)),
                evidence=self._evidence(
                    core=self._core_of(disc), return_layer=page, target_selected=selected
                ),
                disc=disc if ready else None,
            )

        return probe

    def _probe_open_preview(self, before: Disc):
        def probe() -> readiness.WaitSample:
            frame, _page, _method, _hwnd = self._grab(allow_esc=False, need_page=False)
            captured = time.monotonic()
            layer = self._return_layer(frame)
            level_line = self._zoom_level_of(frame)
            level = self._level_of_line(level_line)
            ready = bool(layer == "preview" and level is not None and level == before.level)
            return readiness.WaitSample(
                frame=frame,
                captured_at=captured,
                ready=ready,
                state_key=("open_preview", layer, before.level, level),
                evidence=self._evidence(
                    return_layer=layer,
                    level_text=(level_line.text if level_line is not None else None),
                    before_core=self._core_of(before),
                ),
            )

        return probe

    def _probe_open_upgrade(self, before: Disc):
        def probe() -> readiness.WaitSample:
            frame, _page, _method, _hwnd = self._grab(allow_esc=False, need_page=False)
            captured = time.monotonic()
            has_add = self._has_stage_add(frame)
            disc = self._parse_upgrade_page(frame) if has_add else None
            ready = bool(has_add and self._same_upgrade_object(disc, before))
            return readiness.WaitSample(
                frame=frame,
                captured_at=captured,
                ready=ready,
                state_key=("open_upgrade", has_add, self._core_key(disc)),
                evidence=self._evidence(
                    core=self._core_of(disc),
                    return_layer="upgrade" if has_add else "unknown",
                    stage_add=has_add,
                    before_core=self._core_of(before),
                ),
                disc=disc if ready else None,
            )

        return probe

    def _probe_material_ready(self, before: Disc, before_frame):
        baseline = self._material_bar_crop(before_frame)
        state: dict = {"candidate": None}
        # 提速（2026-10-12 实测）：_has_stage_add 单次识别 ~1.0 s，而本等待需要连续两帧
        # 稳定才能在 3.18 s 预算内就绪；同一页面里按钮不会中途消失，看到过一次就锁住。
        fixed: dict = {"has_add": False, "has_upgrade": False}

        def probe() -> readiness.WaitSample:
            frame, _page, _method, _hwnd = self._grab(allow_esc=False, need_page=False)
            captured = time.monotonic()
            has_add = fixed["has_add"]
            if not has_add:
                has_add = bool(self._has_stage_add(frame))
                fixed["has_add"] = has_add
            has_upgrade = fixed["has_upgrade"]
            if not has_upgrade:
                has_upgrade = bool(self._has_upgrade_button(frame))
                fixed["has_upgrade"] = has_upgrade
            crop = self._material_bar_crop(frame)
            delta = self._mean_abs_diff(baseline, crop)
            changed = delta is not None and delta >= _MATERIAL_BAR_CHANGE_THRESHOLD
            if not changed:
                state["candidate"] = None
            else:
                candidate = state["candidate"]
                if candidate is None:
                    state["candidate"] = crop
                elif (diff := self._mean_abs_diff(candidate, crop)) is not None and (
                    diff > _MATERIAL_BAR_STABLE_THRESHOLD
                ):
                    state["candidate"] = crop
            key = None if state["candidate"] is None else self._material_bar_key(state["candidate"])
            ready = bool(has_add and has_upgrade and key is not None)
            return readiness.WaitSample(
                frame=frame,
                captured_at=captured,
                ready=ready,
                state_key=("material_ready", has_add, has_upgrade, key),
                evidence=self._evidence(
                    return_layer="upgrade" if has_add else "unknown",
                    delta=delta,
                    changed=changed,
                    before_core=self._core_of(before),
                ),
            )

        return probe

    def _probe_upgrade_result(self, before: Disc, expected_level: int):
        def probe() -> readiness.WaitSample:
            frame, _page, _method, _hwnd = self._grab(allow_esc=False, need_page=False)
            captured = time.monotonic()
            disc = self._parse_upgrade_page(frame)
            if disc is not None:
                ready = disc.level >= expected_level
                return readiness.WaitSample(
                    frame=frame,
                    captured_at=captured,
                    ready=ready,
                    state_key=("upgrade_result", self._core_key(disc)),
                    evidence=self._evidence(
                        core=self._core_of(disc),
                        return_layer="upgrade",
                        expected_level=expected_level,
                    ),
                    disc=disc if ready else None,
                )
            # 只有本帧读不到盘面才做全窗口返还 OCR（§6.2.1）。
            refund = self._find_material_refund(frame)
            if refund is not None:
                text = refund.text.replace(" ", "")
                rect = [int(refund.left), int(refund.top), int(refund.right), int(refund.bottom)]
                return readiness.WaitSample(
                    frame=frame,
                    captured_at=captured,
                    ready=False,
                    state_key=("upgrade_result", "refund", self.MATERIAL_REFUND_TITLE, text, tuple(rect)),
                    evidence=self._evidence(refund={"text": text, "rect": rect}),
                    interrupt_reason=MATERIAL_REFUND,
                )
            return readiness.WaitSample(
                frame=frame,
                captured_at=captured,
                ready=False,
                state_key=("upgrade_result", "unreadable"),
                evidence=self._evidence(),
            )

        return probe

    def _probe_return_warehouse(self, before_frame):
        state: dict = {"before_layer": None}

        def probe() -> readiness.WaitSample:
            if state["before_layer"] is None:
                state["before_layer"] = self._return_layer(before_frame)
            frame, _page, _method, _hwnd = self._grab(allow_esc=False, need_page=False)
            captured = time.monotonic()
            layer = self._return_layer(frame)
            allowed = (state["before_layer"], layer) in self.RETURN_EDGES
            ready = bool(allowed and layer in ("preview", "warehouse"))
            return readiness.WaitSample(
                frame=frame,
                captured_at=captured,
                ready=ready,
                state_key=("return_warehouse", state["before_layer"], layer),
                evidence=self._evidence(
                    return_layer=layer,
                    before_layer=state["before_layer"],
                    allowed_edge=allowed,
                ),
            )

        return probe

    # ---------------------------------------------------------------- 等待包装（§6.1、§7.1.1）

    @contextmanager
    def _business_input(self, kind: str, target=None):
        from zzz_ddtool.wininput import input_context

        previous = self._input_metadata
        self._input_metadata = {"target": target}
        try:
            # §3.2/§4.3：业务接管收尾等待，输入层释放即返回，业务固定等待统一用 wait_release。
            # target 同时写入原生观察记录（§6.4：select_disc 等动作取实际输入的 target/格序号）。
            with input_context(kind, target=target, defer_post_release=True):
                yield
        finally:
            self._input_metadata = previous

    def _observe_execution_input(self, raw: dict) -> None:
        """真实 begin/end 的只读 sink；失败不能改变输入语义或丢弃已发输入。"""
        try:
            if raw["event"] == "begin":
                span = self._disc_span
                ji = span.job_index if span is not None else self._current_job_index
                job = next((j for j in self._job_records if j["job_index"] == ji), None)
                choice = self._latest_decision if span is not None else None
                hwnd = raw.get("hwnd") or self._hwnd
                size = list(client_size(hwnd)) if hwnd is not None else None
                generation = getattr(self.hotkeys, "pause_generation", None)
                item = {
                    "input_id": f"input-{raw['call_id']}", "call_id": raw["call_id"],
                    "api": raw["api"], "kind": raw.get("role") or raw["api"],
                    "phase": "disc" if span is not None else (
                        "preparation" if job is None or job.get("prepared_at") is None else "recovery"),
                    "job_index": ji, "disc_index": span.disc_index if span is not None else None,
                    "disc_event_id": span.event_id if span is not None else None,
                    "started_at": raw["t_ns"] / 1e9, "ended_at": None,
                    "status": "started", "exception": None, "hwnd": hwnd,
                    "pid": window_pid(hwnd) if hwnd is not None else None,
                    "client_size": size, "pause_generation": generation if type(generation) is int else None,
                    "decision_id": choice["decision_id"] if choice else None,
                    "disc": runevidence.snapshot(choice["disc"]) if choice else None,
                    "preset_sha256": span.details.get("preset_sha256") if span else (job or {}).get("preset_sha256"),
                    "accepted_sha256": span.details.get("accepted_sha256") if span else (job or {}).get("accepted_sha256"),
                    "target": runevidence.snapshot(self._input_metadata.get("target", raw.get("target"))),
                    "result": {}, "wait_ids": [],
                    "released_at_ns": None, "release_wait": None,
                    **{k: raw.get(k) for k in ("x", "y", "vk", "delta_units")},
                }
                self._input_records.append(item)
                self._input_by_call[raw["call_id"]] = item
                if span is not None:
                    span.details.setdefault("inputs", []).append(item)
                if job is not None:
                    job["input_ids"].append(item["input_id"])
            else:
                item = self._input_by_call[raw["call_id"]]
                item.update(ended_at=raw["t_ns"] / 1e9, status=raw["status"],
                            exception={"type": raw["error"]} if raw.get("error") else None,
                            released_at_ns=raw.get("released_at_ns"))
                if self._disc_span is not None and raw["status"] == "completed":
                    self._disc_span.set(input_sent=True)
                self._last_input_id = item["input_id"]
        except Exception as exc:
            self._evidence_error = f"{type(exc).__name__}: {exc}"
            self.log.warning("执行输入证据记录失败", exc_info=True)

    def _input_record(self, input_id):
        return next((i for i in reversed(self._input_records) if i["input_id"] == input_id), None)

    def _note_decision(self, disc: Disc, decision: Decision, preset: Preset, accepted_ids) -> None:
        span = self._disc_span
        if span is None:
            return
        rows = span.details.setdefault("decisions", [])
        item = dict(decision_id=f"{span.event_id}:decision-{len(rows)}", at=time.monotonic(),
                    disc=runevidence.snapshot(disc), decision=runevidence.snapshot(decision),
                    preset_sha256=runevidence.digest(preset),
                    accepted_sha256=runevidence.accepted_digest(accepted_ids))
        rows.append(item)
        span.set(core=self._core_of(disc))
        self._latest_decision = item
        scanperf.set_scope(stage_level=disc.level)

    def _wait_ready(
        self, kind: str, *, release: InputRelease, before: Disc | None = None,
        before_frame=None, cell: GridCell | None = None, expected_level: int | None = None,
        trigger_input_id: str | None = None, resume_from_wait_id: str | None = None,
        action_id: str | None = None,
    ) -> WaitResult:
        """§6.1/§4.3：以触发输入的释放点为起点做一次就绪等待，并包装 wait_total/probe 事件。

        下限取自 ActionWaitPolicy（有效实测或该动作保守值），超时取 §4.3 固定表；两者都按
        ``release.released_at_ns`` 起的活动时间计（扣暂停），不在下限之后重开超时。
        超时/中断原样抛出（ReadinessTimeout / ReadinessInterrupted），其他异常原样传播；
        无活动性能会话时只做等待、不写事件、不改变返回值。``trigger_input_id``/
        ``resume_from_wait_id`` 供 §6.2.1 的第二次结果等待关联首个等待与确认输入；
        ``action_id`` 用于返还确认这类 readiness kind 与动作ID不同的等待。
        """
        probe = self._make_ready_probe(
            kind, before=before, before_frame=before_frame, cell=cell,
            expected_level=expected_level,
        )
        action_id = action_id or kind
        policy = self._wait_policy(action_id)
        ready_policy = readiness.WaitPolicy(
            policy.floor_ms, _READY_TIMEOUT_MS[action_id], readiness.POLL_MS, readiness.STABLE_FRAMES
        )
        started_at = release.released_at_ns / 1_000_000_000
        trigger_input_id = trigger_input_id or self._last_input_id
        stage_level = before.level if before is not None else (
            self._latest_decision["disc"]["level"] if self._latest_decision is not None else None)
        session = scanperf.current_session()
        if session is None:
            self._last_wait_id = None
            result: WaitResult | None = None
            error: BaseException | None = None
            try:
                result = readiness.wait_ready(
                    kind=kind, policy=ready_policy, started_at=started_at, probe=probe,
                    checkpoint=self._checkpoint, pauses=self._pause_intervals,
                )
                return result
            except BaseException as exc:  # noqa: BLE001 - 原样传播，仅用于记明细
                error = exc
                raise
            finally:
                self._fill_release_wait(
                    trigger_input_id, action_id, release, policy, result, error, time.monotonic(),
                )

        wait_id = session.new_event_id()
        self._last_wait_id = wait_id
        samples: list[dict] = []
        counter: dict = {"n": 0, "gen": None}

        def checkpoint() -> int | None:
            gen = self._checkpoint()
            counter["gen"] = gen
            return gen

        def wrapped_probe() -> "readiness.WaitSample":
            index = counter["n"]
            counter["n"] += 1
            details = {
                "purpose": f"wait:{kind}",
                "wait_id": wait_id,
                "sample_id": None,
                "sample_index": index,
                "kind": kind,
                "captured_at": None,
                "probe_ended_at": None,
                "ready": None,
                "state_key": None,
                "pause_generation": counter["gen"],
                "interrupt_reason": None,
                "evidence": None,
            }
            with scanperf.timed("probe", parent_id=wait_id, stage_level=stage_level, details=details) as span:
                sample = probe()
                ended = time.monotonic()
                span.set(
                    captured_at=sample.captured_at,
                    probe_ended_at=ended,
                    ready=bool(sample.ready),
                    state_key=_jsonable_key(sample.state_key),
                    pause_generation=counter["gen"],
                    interrupt_reason=sample.interrupt_reason,
                    evidence=sample.evidence,
                )
                samples.append(
                    {
                        "probe_event_id": span.event_id,
                        "sample_index": index,
                        "captured_at": sample.captured_at,
                        "probe_ended_at": ended,
                        "ready": bool(sample.ready),
                        "state_key": _jsonable_key(sample.state_key),
                        "pause_generation": counter["gen"],
                        "interrupt_reason": sample.interrupt_reason,
                        "evidence": sample.evidence,
                    }
                )
            return sample

        error: BaseException | None = None
        result = None
        try:
            result = readiness.wait_ready(
                kind=kind, policy=ready_policy, started_at=started_at, probe=wrapped_probe,
                checkpoint=checkpoint, pauses=self._pause_intervals,
            )
            return result
        except BaseException as exc:  # noqa: BLE001 - 原样传播，仅用于记事件
            error = exc
            raise
        finally:
            ended = time.monotonic()
            self._fill_release_wait(
                trigger_input_id, action_id, release, policy, result, error, ended,
            )
            intervals = self._pause_intervals(started_at, ended)
            paused = sum(float(b) - float(a) for a, b in intervals)
            active_ms = max(0.0, (ended - started_at) - paused) * 1000.0
            outcome = "succeeded"
            interrupt_reason = None
            if isinstance(error, ReadinessInterrupted):
                outcome, interrupt_reason = "interrupted", error.reason
            elif isinstance(error, ReadinessTimeout):
                outcome = "timed_out"
            elif isinstance(error, RunnerStopped):
                outcome = "stopped"
            elif error is not None:
                outcome = "failed"
            t_bad, t_ready, t_stable = runevidence.wait_times(samples)
            details = {
                "kind": kind,
                "started_at": started_at,
                "ended_at": ended,
                "active_ms": active_ms,
                "pause_intervals": [
                    {"start": float(a), "end": float(b)} for a, b in intervals
                ],
                "samples": samples,
                "t_bad": t_bad,
                "t_ready": t_ready,
                "t_stable": t_stable,
                "outcome": outcome,
                "trigger_input_id": trigger_input_id,
                "upgrade_input_id": self._upgrade_input_id if kind == "upgrade_result" else None,
                "resume_from_wait_id": resume_from_wait_id,
                "interrupt_reason": interrupt_reason,
            }
            if error is not None:
                details["exception"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
            actual_input = self._input_record(trigger_input_id)
            if actual_input is not None:
                actual_input["wait_ids"].append(wait_id)
                actual_input["result"] = dict(
                    verified=result is not None,
                    actual_level=result.disc.level if result is not None and result.disc else None,
                    to_layer=(samples[-1]["evidence"] or {}).get("return_layer") if samples else None,
                    exception=details.get("exception"),
                )
                if kind == "select_disc" and result is not None:
                    actual_input["disc"] = runevidence.snapshot(result.disc)
            if kind == "upgrade_result":
                upgrade_input = self._input_record(self._upgrade_input_id)
                if upgrade_input is not None and upgrade_input is not actual_input:
                    upgrade_input["wait_ids"].append(wait_id)
                    upgrade_input["result"] = dict(
                        verified=result is not None,
                        actual_level=result.disc.level if result is not None and result.disc else None,
                        exception=details.get("exception"),
                    )
            try:
                session.record(
                    scanperf.PerfEvent(
                        component="wait_total",
                        duration_ms=(ended - started_at) * 1000.0,
                        details=details,
                        event_id=wait_id,
                        parent_id=self._disc_event_id,
                        job_index=self._disc_span.job_index if self._disc_span else self._current_job_index,
                        disc_index=self._disc_span.disc_index if self._disc_span else None,
                        stage_level=stage_level,
                    )
                )
            except Exception:
                self.log.warning("wait_total 事件写入失败", exc_info=True)

    def _fill_release_wait(self, trigger_input_id, action_id, release, policy, result, error, ended):
        """§7.1：等待消费者完成后把六字段明细写进对应执行输入记录（故障也保留截至当下）。"""
        item = self._input_record(trigger_input_id)
        if item is None:
            return
        requested = None
        if result is not None:
            requested = getattr(result, "requested_sleep_ms", None)
        if requested is None and error is not None:
            requested = getattr(error, "requested_sleep_ms", None)
        requested = float(requested or 0.0)
        release_s = release.released_at_ns / 1_000_000_000
        paused = sum(float(b) - float(a) for a, b in self._pause_intervals(release_s, ended))
        item["released_at_ns"] = release.released_at_ns
        item["release_wait"] = {
            "action_id": action_id,
            "released_at_ns": release.released_at_ns,
            "floor_ms": policy.floor_ms,
            "policy_source": policy.source,
            "requested_sleep_ms": requested,
            "active_elapsed_ms": max(0.0, (ended - release_s) - paused) * 1000.0,
        }

    def _merge_wait_receipts(self) -> None:
        """§7.1：把等待消费者登记的释放明细按释放点并入执行输入记录。

        未被任何消费者接管的已释放输入（如 substat_scroll 的前 7 条原始滚轮）按
        action_id=null / floor_ms=0 / requested_sleep_ms=0 如实记录，不冒充等待。
        """
        by_ns = {receipt.released_at_ns: receipt for receipt in self._wait_receipts}
        for item in self._input_records:
            ns = item.get("released_at_ns")
            if ns is None or item.get("release_wait") is not None:
                continue
            receipt = by_ns.get(ns)
            if receipt is None:
                continue
            item["release_wait"] = {
                "action_id": receipt.action_id,
                "released_at_ns": receipt.released_at_ns,
                "floor_ms": receipt.floor_ms,
                "policy_source": receipt.policy_source,
                "requested_sleep_ms": receipt.requested_sleep_ms,
                "active_elapsed_ms": receipt.active_elapsed_ms,
            }
        for item in self._input_records:
            if item.get("released_at_ns") is None or item.get("release_wait") is not None:
                continue
            item["release_wait"] = {
                "action_id": None,
                "released_at_ns": item["released_at_ns"],
                "floor_ms": 0,
                "policy_source": "default",
                "requested_sleep_ms": 0.0,
                "active_elapsed_ms": 0.0,
            }

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
            required_substats=self.preset.required_substats,
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
            labels=self.preset.labels,
            required_substats=job.required_substats,
        )

    def run(self) -> list[RunResult]:
        """跑完整轮任务链，返回处理结果。F10 停止时返回已处理部分。

        §7.1：run_total 在入口建会话后即开始，早于 ``_run`` 的 max_pages 校验及主
        try/finally，覆盖任务准备与所有真实退出；零盘退出仍产生且只产生一个根事件。
        """
        import uuid

        # §9.2：入口清空终止类型，避免上一轮的 completed/user_stopped 残留到本轮，
        # 尤其是 _run 在主 try/finally 之前直接返回 [] 的分支。
        self.termination_kind = ""
        self._input_records = []
        self._input_by_call = {}
        self._last_input_id = None
        self._current_job_index = None
        self._latest_decision = None
        self._input_metadata = {}
        self._evidence_error = None
        self._run_exception = None
        self._job_records = []
        self._label_inputs = []
        self._shortfall_exception = None
        tag = getattr(self.trace, "tag", None)
        if not isinstance(tag, str) or not tag:
            tag = "run"
        run_id = f"{tag}-{uuid.uuid4().hex}"
        capture_dir = getattr(self.trace, "dir", None)
        stats_target = stats_dir() / f"{tag}.json"
        scanperf.begin_session(
            tag,
            run_id=run_id,
            capture_dir=(str(capture_dir) if capture_dir is not None else None),
            stats_path=str(stats_target),
        )
        run_started = time.monotonic()
        try:
            # 导航、标定、筛选与强化共享此任务的控制器。只在一次完整输入前检查，
            # 不在 mouse/key down 与 up 之间抛出停止，避免留下按住的键。
            from contextlib import ExitStack

            from zzz_ddtool.wininput import capture_inputs

            with ExitStack() as stack:
                self._scope_stack = stack
                with (
                    controlled_inputs(self._checkpoint, pauses=self._pause_intervals),
                    capture_inputs(self._observe_execution_input),
                    actionwait.collect_wait_receipts() as receipts,
                ):
                    self._wait_receipts = receipts
                    return self._run()
        finally:
            self._scope_stack = None
            self._close_release_scope()
            ended = time.monotonic()
            # §7.1：等待明细（含输入层自己拥有的收尾）并入执行输入记录后再记 run_total。
            self._merge_wait_receipts()
            scanperf.record_event(
                scanperf.PerfEvent(
                    component="run_total",
                    duration_ms=max(0.0, (ended - run_started) * 1000.0),
                    details={
                        "runner_outcome": self.outcome,
                        "termination_kind": self.termination_kind or "error",
                        "end_reason": self.end_reason,
                        "capture_dir": (str(capture_dir) if capture_dir is not None else None),
                        "stats_path": str(stats_target),
                        "started_at": run_started, "ended_at": ended,
                        "jobs": self._job_records, "inputs": self._input_records,
                        "evidence_error": self._evidence_error,
                        "exception": self._shortfall_exception or self._run_exception,
                    },
                )
            )
            scanperf.end_session()

    def _run(self) -> list[RunResult]:
        if not self._valid_max_pages():
            self.outcome = "failed"
            self.termination_kind = "error"
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
        self._restored_scan_reused = False
        self._tail_slots_confirmed = False
        self._last_empties = set()
        self._last_unclear = {}
        self._last_count = None
        # 每轮重建释放回执与明细收集（run() 建立收集器；环境未变时作用域复用同一档案）。
        self._material_release = None
        self._upgrade_release = None
        self._refund_release = None
        self._refund_input_id = None
        self._wait_receipts = []
        self._disc_event_id = None
        self._upgrade_input_id = None
        self._upgrade_entry_generation = None
        self._job_records = []
        self._label_inputs = []
        self._shortfall_exception = None
        self.outcome = "running"
        self.end_reason = ""
        # spec §3.6：每次 _run 新建会话，本次 _run 的多个 _run_job 共享该实例。
        replay_session = FilterReplaySession()
        jobs = self._jobs()
        import logging

        from zzz_ddtool.overlay import OverlayLogHandler

        overlay_handler = OverlayLogHandler()
        logging.getLogger("zzz_ddtool").addHandler(overlay_handler)
        total_limit = sum(self._job_limit(job) for job in jobs if job.accepted is not None)
        self.log.info("run start jobs=%d max_discs=%d (F9 暂停 / F10 停止)", len(jobs), total_limit)
        self._set_status("run_start", f"jobs={len(jobs)}")
        self.trace.ok("unknown", "run", f"共 {len(jobs)} 个任务，各任务独立限额，合计最多处理 {total_limit} 个盘")
        run_started = time.monotonic()
        skipped_jobs = 0
        try:
            for index, job in enumerate(jobs, 1):
                self._checkpoint()
                # §7.1：本轮事件的任务归属用 0 起 job_index；盘/档作用域在更内层建立。
                scanperf.set_scope(job_index=index - 1, disc_index=None, stage_level=None)
                self._current_job_index = index - 1
                self._latest_decision = None
                sub_preset = self._sub_preset(job)
                job_record = {
                    "job_index": index - 1,
                    "job_id": job.job_id,
                    "skipped": job.accepted is None,
                    "processed": 0,
                    "end_reason": None,
                    "slot": job.slot, "sets": list(job.sets), "mainstats": list(job.mainstats),
                    "accepted": None if job.accepted is None else list(job.accepted),
                    "max_discs": job.max_discs,
                    "quota_raw": job.max_discs if job.max_discs is not None else self.preset.max_discs,
                    "quota_effective": 0 if job.accepted is None else self._job_limit(job),
                    "labels": runevidence.snapshot(self.preset.labels),
                    "preset": runevidence.snapshot(sub_preset),
                    "preset_sha256": runevidence.digest(sub_preset),
                    "accepted_sha256": runevidence.accepted_digest(job.accepted),
                    "started_at": time.monotonic(), "prepared_at": None, "ended_at": None,
                    "input_ids": [],
                }
                self._job_records.append(job_record)
                self.stats.job_begin(
                    index,
                    job.slot,
                    job.sets,
                    job.mainstats,
                )
                if job.accepted is None:
                    skipped_jobs += 1
                    self._job_started_at = self._processed
                    self._job_max_discs = 0
                    self._state = None
                    message = f"第 {index} 个任务「{job.title()}」未生成卡片名单，已跳过"
                    self.log.info(message)
                    self.trace.ok("unknown", "job_skipped", message)
                    self._set_status("job_skipped", message)
                    self.stats.job_end(0.0, 0)
                    job_record["end_reason"] = "skipped"
                    job_record["ended_at"] = time.monotonic()
                    continue
                job_started = time.monotonic()
                try:
                    self._run_job(job, replay_session)
                except RunnerStopped:
                    job_record["end_reason"] = "stopped"
                    raise
                except Exception:
                    job_record["end_reason"] = "failed"
                    raise
                else:
                    job_record["end_reason"] = (
                        "quota_reached" if self._job_limit_reached() else "exhausted"
                    )
                finally:
                    job_record["processed"] = self._processed - self._job_started_at
                    # 进模型前就记上用量：任务中途抛错也要能看到它花了多久。
                    self.stats.job_end(
                        time.monotonic() - job_started,
                        self._processed - self._job_started_at,
                    )
                    job_record["ended_at"] = time.monotonic()
            self.outcome = "completed"
            self.termination_kind = "completed"
            self.end_reason = f"已处理 {self._processed} 个盘"
            if skipped_jobs:
                self.end_reason += f"；跳过 {skipped_jobs} 个未生成任务"
        except RunnerStopped:
            self.outcome = "stopped"
            self.termination_kind = "user_stopped"
            self.end_reason = f"用户停止，已处理 {self._processed} 个盘"
            self.trace.ok("unknown", "run", self.end_reason)
        except MaterialShortfall as exc:
            # §9.2：素材不足有独立终止类型，靠类型而非中文消息判定。
            self.outcome = "failed"
            self.termination_kind = "material_shortfall"
            self.end_reason = str(exc)
            self._shortfall_exception = {
                "type": "zzz_ddtool.runner.MaterialShortfall",
                "message": str(exc),
                "attributes": {
                    "from_level": exc.from_level,
                    "expected_level": exc.expected_level,
                    "actual_level": exc.actual_level,
                },
            }
            self.log.warning("run material_shortfall: %s", exc)
            self.trace.fail("unknown", "run", self.end_reason)
        except Exception as exc:
            self.outcome = "failed"
            self.termination_kind = "error"
            self.end_reason = str(exc)
            self._run_exception = {"type": type(exc).__name__, "message": str(exc)}
            self.log.exception("run failed")
            self.trace.fail("unknown", "run", self.end_reason)
        finally:
            # spec §3.6：正常结束、异常、F10 三种收尾都清空路由（连同文字指纹一并释放）。
            replay_session.routes.clear()
            if self.hotkeys is not None:
                self.hotkeys.close()
            self.trace.finish()
            report = self._finish_stats(run_started)
            self._set_status(self.outcome, self.end_reason, report_path=report)
            logging.getLogger("zzz_ddtool").removeHandler(overlay_handler)
            overlay_handler.close()
        self.log.info("run %s processed=%d", self.outcome, self._processed)
        return list(self.results)

    def _kept_image_path(self, disc_slot: int) -> str:
        """成功盘（升满保留）截图存到 log/stats/<tag>/success/，返回相对 .md 的路径。

        截不到（窗口最小化、游戏不在前台、统计对象被 mock 等）就返回空串，不影响主流程。
        截什么界面由调用时机决定（目前截判定成功那一刻的游戏画面，后续可调成切到详情页再截）。
        """
        try:
            if not (0 <= disc_slot < len(self.stats.discs)):
                return ""
            disc = self.stats.discs[disc_slot]
            image, _method, _hwnd = grab_game_image(self._hwnd)
            dest = stats_dir() / str(self.stats.tag) / "success"
            path = save_image(image, self._hwnd, "printwindow", name=f"disc{disc.index}", dest_dir=dest)
            return f"success/{path.name}"
        except Exception:
            self.log.warning("成功盘截图失败，不附图：disc_slot=%s", disc_slot)
            return ""

    def _finish_stats(self, run_started: float) -> str:
        """收尾统计：并入 Trace 的步骤耗时 → 写日志摘要 → 落 JSON + 执行结果总结。

        返回执行结果总结（.md）的绝对路径，供界面显示「详细日志在哪」；
        写不出去就返回空串。统计出问题不影响任务结果，只记一条警告。
        """
        try:
            self.stats.seconds = time.monotonic() - run_started
            self.stats.absorb_steps(self.trace.spans)
            self.stats.log_summary(self.log)
            target = self.stats.write(stats_dir() / f"{self.stats.tag}.json")
            if target is not None:
                self.log.info("统计已写入 %s", target)
            else:
                self.log.warning("结构化统计写入失败（不影响本轮结果）")
            report_path = self.stats.write_report(stats_dir() / f"{self.stats.tag}.md")
            if report_path is not None:
                self.log.info("执行结果总结已写入 %s", report_path)
                return str(report_path)
            self.log.warning("执行结果总结写入失败（不影响本轮结果）")
            return ""
        except Exception:
            self.log.warning("运行统计生成失败（不影响本轮结果）", exc_info=True)
            return ""

    def _valid_max_pages(self) -> bool:
        value = self.preset.max_pages
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    # ---------------------------------------------------------------- 主任务

    def _run_job(self, job: TaskJob, replay_session: FilterReplaySession | None = None) -> None:
        if job.accepted is None:
            self.log.info("任务 %s 未生成卡片名单，跳过", job.job_id)
            return
        self._job_started_at = self._processed
        self._job_max_discs = self._job_limit(job)
        self.log.info("job start slot=%d sets=%s mains=%s", job.slot, job.sets, job.mainstats)
        hwnd = self._bind_instance()
        navigate_started = time.monotonic()
        try:
            goto_warehouse(hwnd)
            self.trace.ok("drive_disc", "navigate", "已到驱动盘仓库")
        except NavError as exc:
            self.trace.fail("unknown", "navigate", str(exc))
            raise RunnerError(f"导航失败：{exc}") from exc
        finally:
            self.stats.span("导航", "到驱动盘仓库", time.monotonic() - navigate_started)

        # 滚轮协议必须在设置筛选之前备好：标定要选第 3 行的盘做锚点，而筛选会把列表
        # 裁短（实测只剩 16/3000，从第 3 行起全是空格），空行选不中锚点，标定必失败。
        with self.stats.timed("导航", "滚轮协议准备"):
            self._ensure_profile(hwnd)

        image, page, method, _hwnd = self._grab()
        sub_preset = self._sub_preset(job)
        substat_filter = plan_substat_filter(sub_preset)
        self.log.info("自动筛选：%s", substat_filter.summary())
        self._set_status("filter", substat_filter.summary())
        extra_filter = {}
        if substat_filter.names:
            extra_filter = {"substats": substat_filter.names, "substat_counts": substat_filter.counts}
        with self.stats.timed("筛选", "设置游戏筛选"):
            apply_game_filter(
                hwnd,
                image.width(),
                image.height(),
                slots=[job.slot],
                sets=job.sets,
                mainstats=job.mainstats,
                replay_session=replay_session,
                **extra_filter,
            )
        self.trace.ok(
            "drive_disc",
            "filter",
            f"{job.slot}号位"
            + (f" 套装 {'、'.join(job.sets)}" if job.sets else "")
            + (f" 主词条 {'、'.join(job.mainstats)}" if job.mainstats else "")
            + f"；{substat_filter.summary()}",
        )
        time.sleep(0.3)

        accepted_ids = None if job.accepted is None else set(job.accepted)

        # 每任务新建状态：不继承上一任务进度（spec §4.3、§6.1）
        state = TraversalState(
            job_id=job.job_id,
            filter_signature=make_filter_signature(
                slot=job.slot, sets=job.sets, mainstats=job.mainstats,
                tag=f"substats={','.join(substat_filter.names)};minimum={substat_filter.minimum}",
            ),
        )
        state.restore_request_limit_rows = max_restore_rows(self.preset.max_pages)
        self._state = state
        self._restored_scan = None
        self._restored_scan_reused = False

        with self.stats.timed("导航", "筛选后准备"):
            prep = self._prepare_navigation(hwnd, state)
        if self._job_records:
            self._job_records[-1]["prepared_at"] = time.monotonic()
        if prep is None:
            self._set_status("job_empty", "筛选结果为空，继续下一任务")
            return
        # 先起第 1 批，再摆起点：跳号启动要按新视口覆盖批次计数（见 _seed_start_index）。
        state.start_first_batch()
        scan_snapshot = None
        if prep.mode == VIEWPORT_SINGLE_SCREEN:
            # 整屏可见：结果本来就都在当前画面里，上下端点同时成立，不置顶也不探底。
            state.mark_viewport_mode(VIEWPORT_SINGLE_SCREEN, total=prep.total)
            state.confirm_viewport(
                0, 0.0, row_pitch_px=self._row_pitch, at_top=True, at_bottom=True
            )
            scan_snapshot = prep.scan
            self._set_status("batch", f"单屏：整屏 {prep.total} 个盘，直接处理")
            self.log.info(
                "viewport_mode=single_screen 筛选后总数=%s 完整可见格数=%d 相位=0 工作批次=%d",
                prep.total,
                len(scan_snapshot[0]) if scan_snapshot else 0,
                state.work_batch_count,
            )
        elif self.start_index > 0:
            self._seed_start_index(hwnd, state, self.start_index)
            self._set_status("batch", "首批网格起点")
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

            reused = False
            if scan_snapshot is not None:
                cells, problems, skips = scan_snapshot
                scan_snapshot = None
            elif self._restored_scan is not None:
                cells, problems, skips = self._restored_scan
                reused = self._restored_scan_reused
                self._restored_scan = None
                self._restored_scan_reused = False
            else:
                cells, problems, skips = self._scan_cells_with_retry(state)
            recovery_cache = self._make_recovery_cache(state, cells, skips)
            ready, deferred = split_stable_cells(
                cells, at_top=state.at_top, at_bottom=state.at_bottom
            )
            if not reused:
                self.stats.scan(
                    cells,
                    skips,
                    len(deferred),
                    max_index=max((cell.global_index for cell in cells), default=None),
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
                "scan_cache" if reused else "scan",
                f"第 {state.work_batch_count} 批：待处理 {len(pending)}，跳过 {len(skips)}",
                image=self._scan_image,
            )
            self._set_status("reusing" if reused else "scanning", f"batch {state.work_batch_count}: {len(pending)} cells")

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
                    # 这里只提交端点状态，游戏画面未变化。复用扫描结果解除末行延期，
                    # 不再抓图/OCR 一遍；后续点击、前进或恢复仍会重新扫描。
                    scan_snapshot = (cells, problems, skips)
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
                    # 升级前移只影响已解决前缀。未达额度时复用本批未处理候选；
                    # 单屏直接简查，长列表恢复保存视口后换算坐标。
                    restore_after_upgrade = result == "upgrade"
                    break
                state.set_status(cell.global_index, CellStatus.DONE)
                state.commit_prefix()
                self._set_status("progress", f"next_index={state.next_index}", self._cell_tag(cell))
            if broke:
                if (
                    restore_after_upgrade
                    and state.viewport_mode == VIEWPORT_SINGLE_SCREEN
                    and state.single_screen_total is not None
                    and state.single_screen_total > 0
                    and state.next_index == state.single_screen_total
                ):
                    self.log.info("单屏全部格位已解决，本任务结束，省略末件候选恢复")
                    break
                state.invalidate_position()
                if restore_after_upgrade and not self._job_limit_reached():
                    self._restore_position(hwnd, state, cells, cache=recovery_cache)
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

    def _ensure_profile(self, hwnd: int) -> None:
        """备好滚轮协议（每次 run 只做一次）。

        **必须在设置筛选之前调用**：标定要选第 3 行的盘做锚点，筛选会把列表裁短
        （实测只剩 16/3000，从第 3 行起全是空格），空行选不中锚点，标定必失败。
        标定只取决于网格布局、与列表内容无关，满屏时做最稳。
        """
        if self._profile is not None:
            return
        # 标定要求列表满屏：上一轮可能残留筛选条件把列表裁短（实测只剩 16/3000，
        # 第 3 行锚点落在空格上），所以先重置一次筛选。只做一次（首个任务）。
        image, _method, _hwnd = grab_game_image(hwnd)
        reset_game_filter(hwnd, image.width(), image.height())
        self.trace.ok("drive_disc", "filter", "标定前重置筛选")
        # §3.4：环境探测已移到 _bind_instance（绑定后、reset 之前），此处沿用 self._env，
        # 导航仍按「重置筛选 → 测量布局 → 加载/标定导航」的原顺序执行；
        # 未绑定直接调用（测试/单步）时退回一次只读探测。
        env = self._env if self._env is not None else probe_environment(hwnd)
        # 不在这里置顶：calibrate 内部每个候选都会自己 reset_to_top，而 measure_layout
        # 只看网格几何、与列表滚到哪无关。
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
            except RunnerStopped:
                raise
            except Exception as exc:
                raise RunnerError(f"滚轮标定失败：{exc}，停止任务链") from exc
            if env.persistable:
                try:
                    save_profile(profile)
                except OSError as exc:
                    self.log.warning("导航缓存写入失败（%s），仅本轮使用", exc)
        # 每任务以本轮实测布局为准（缓存不决定原点，spec §5.1 第 168 行）
        self._profile = replace(
            profile, row_pitch_px=self._row_pitch, origin_center_y=self._origin_center_y
        )

    def _read_label_lines(self, image):
        """一次读列表区域（`LABEL_SCAN_AREA`），返回原始 OCR 行。

        数量解析、布局测量、单屏判定与首批扫描共用这一份结果：同一帧只识别一次，
        避免准备、布局测量、正式扫描各抓一次图各认一次（改造方案 §7）。
        """
        from zzz_ddtool.ocr import read_rect

        return read_rect(image, LABEL_SCAN_AREA)

    def _grab_label_frame(self, hwnd: int):
        """抓一帧并读出它的列表区域一次，返回 ``(image, lines)``。"""
        image, _method, _hwnd = grab_game_image(hwnd)
        return image, self._read_label_lines(image)

    def _prepare_navigation(self, hwnd: int, state: TraversalState) -> NavigationPrep | None:
        """筛选后准备（改造方案 §5.1）。

        先读当前画面：结果整屏可见就直接返回单屏准备，否则按长列表准备。两条路径
        都**不调用 `reset_to_top`**、都不发送探底滚轮——当前画面本来就是顶部原点，
        理由见长列表分支的注释。返回 None 表示筛选结果为空。
        """
        profile = self._profile
        if profile is None:
            raise RunnerError("滚轮协议未就绪，停止任务链")

        image, lines = self._grab_label_frame(hwnd)
        origin: float | None = None
        pitch: float | None = None
        # 筛选面板刚关闭时画面可能还在动：读不到「等级」先重拍，别急着宣布空仓。
        for attempt in range(max(1, self.preset.ocr_retry)):
            try:
                origin, pitch = measure_layout(image, lines=lines)
                break
            except NavigationError:
                if is_empty_warehouse(image):
                    self.log.info("筛选结果为空，正常结束当前任务")
                    self.trace.ok(
                        "drive_disc", "scan", "筛选结果为空（0 个盘），继续下一任务", image=image
                    )
                    return None
                if attempt + 1 >= max(1, self.preset.ocr_retry):
                    raise
                self.log.warning("筛选后网格读不出「等级」标签，重拍第 %d 次", attempt + 1)
                time.sleep(0.3)
                image, lines = self._grab_label_frame(hwnd)
        assert origin is not None and pitch is not None
        reason = check_layout_reference(origin, pitch, image.width(), image.height())
        if reason:
            self.trace.fail("drive_disc", "layout", f"网格布局不受支持：{reason}", image=image)
            raise RunnerError(f"网格布局不受支持：{reason}，停止任务链")
        self._origin_center_y = float(origin)
        self._row_pitch = float(pitch)
        profile = replace(profile, row_pitch_px=self._row_pitch, origin_center_y=self._origin_center_y)
        self._profile = profile
        state.navigation_profile = profile.as_dict()

        prep, note = self._confirm_single_screen(state, image=image, lines=lines)
        if prep is not None:
            self.trace.ok("drive_disc", "layout_check", self._layout_detail(profile), image=image)
            self.trace.ok("drive_disc", "single_screen", note, image=image)
            return prep

        # 长列表：不置顶、也不再测一次布局，直接用上面那一次测量结果（方案 §5.1）。
        #
        # 事实：**关闭筛选面板时游戏自己就把列表滚回了顶部**，不用我们切页去置顶。
        # 证据（20261005-133401 轮日志与截图）：0004 是「设置游戏筛选」成功那一刻拍的，
        # 那时置顶动作还没发生，画面已经是 34/3000、实体盘从第 1 格起连续排列、末尾
        # EMPTY；之后两次切页置顶（13:34:40→44、13:34:45→49）拍到的 0005 与它逐像素
        # 相比，网格区差异只有 7%（全是图标高光与背景流动），说明置顶没改变视口。
        #
        # 也就是说置顶既不改筛选结果、也不改视口，只是白花一次切页往返，已删除。视口
        # 原点沿用筛选关闭后那一次测量——它本来就在顶部。
        state.mark_viewport_mode(VIEWPORT_SCROLLING)
        self.trace.ok("drive_disc", "layout_check", f"{self._layout_detail(profile)}；{note}", image=image)
        return NavigationPrep(profile=profile, mode=VIEWPORT_SCROLLING)

    def _layout_detail(self, profile: NavProfile) -> str:
        return (
            f"原点 {self._origin_center_y:.1f} 行距 {self._row_pitch:.1f} "
            f"协议 {profile.events_per_row}事件/行@{profile.event_interval_ms}ms"
        )

    def _confirm_single_screen(
        self, state: TraversalState, *, image, lines
    ) -> tuple[NavigationPrep | None, str]:
        """筛选结果是否整屏可见（改造方案 §4）。

        成立返回 ``(单屏准备, 原因)``，准备里带上判定那一帧的扫描结果供主循环复用；
        不成立返回 ``(None, 原因)``，调用方把原因写进审计日志后走长列表路径。
        判定依据见 `traversal.single_screen_verdict`；数量优先从刚读的那一次 OCR 里取。
        """
        profile = self._profile
        if profile is None:
            raise RunnerError("滚轮协议未就绪，停止任务链")
        capacity = GRID_COLS * GRID_ROWS
        total = parse_warehouse_count(lines, width=image.width(), height=image.height())
        if self.start_index > 0:
            # 测试入口要求把视口摆到列表深处，与「整屏可见」互斥，直接走长列表路径。
            return None, "测试跳号启动，跳过整屏判定"
        if total is None:
            return None, "仓库数量没识别出来，不确认整屏"
        if total <= 0:
            return None, f"仓库数量 {total}，按空结果处理"
        if total > capacity:
            return None, f"仓库数量 {total} 超过单屏容量 {capacity}"
        if not is_page(image, DRIVE_DISC):
            return None, "当前画面不是驱动盘仓库页，不确认整屏"
        cells, problems, skips = self._scan_cells(state, image=image, lines=lines)
        ok, reason = single_screen_verdict(
            cells,
            self._last_empties,
            self._last_unclear,
            total_count=total,
        )
        if ok and problems:
            ok, reason = False, f"网格映射不完整：{problems[0]}"
        if not ok:
            return None, f"整屏判定不成立：{reason}"
        return (
            NavigationPrep(
                profile=profile,
                mode=VIEWPORT_SINGLE_SCREEN,
                total=total,
                scan=(cells, problems, skips),
            ),
            f"{reason}；未置顶、未探底，直接处理全部待处理行",
        )

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

    def _scan_cells(self, state: TraversalState, *, image=None, lines=None):
        """扫描当前视口 → (全部原始格位, 问题, 扫描跳过表)。

        行号由几何反推，不用 OCR 聚类序号；未映射/重复归列进 problems，调用方
        当作进度屏障重读（spec §5.5），不当空位。满级/装备/弃置格仍占全局格位，
        只记入 skips，不删号。

        image/lines：调用方已经抓好图、读过列表区域时直接传进来，本函数不再抓图
        不再识别（改造方案 §7：准备、布局测量与正式扫描共用同一份识别结果）。
        """
        from zzz_ddtool.equipped import (
            tile_has_discard_badge,
            tile_has_lock_badge,
            tile_has_portrait,
        )

        if image is None:
            image, page, _method, hwnd = self._grab(allow_esc=False)
            if page != "drive_disc":
                self.trace.fail(page, "scan", "当前不是驱动盘仓库，停止扫描", image=image)
                raise RunnerError(f"扫描前页面错误：{page}，停止任务链")
        self._scan_image = image
        if lines is None:
            lines = self._read_label_lines(image)
        self._last_count = parse_warehouse_count(
            lines, width=image.width(), height=image.height()
        )

        metrics = self._metrics(state, image)
        labels: list[tuple[float, float, int]] = []
        for line in lines:
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
        self._last_empties = empties
        self._last_unclear = unclear
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
            # 状态识别一律做；**是否因此跳过由任务 YAML 决定**（识别与处理分开，2026-10-05 计划 §3.2）。
            lock_state = tile_has_lock_badge(image, cell.center_x, cell.center_y)
            discard_state = tile_has_discard_badge(image, cell.center_x, cell.center_y)
            if self.preset.skip_locked and lock_state is True:
                skips[cell.global_index] = "已锁定"
                continue
            if self.preset.skip_discarded and discard_state is True:
                skips[cell.global_index] = "已打弃置标记"
                continue
            # 拿不准且该状态配了跳过（会改变处理决定）→ 记问题，交给重读；耗尽后停链。
            # 没配跳过的状态即使拿不准也不阻塞：该盘照常点开，状态在点开后读。
            if self.preset.skip_locked and lock_state is None:
                problems.append(
                    f"屏幕第{cell.screen_row0 + 1}行第{cell.screen_col0 + 1}列锁定标记拿不准"
                )
            if self.preset.skip_discarded and discard_state is None:
                problems.append(
                    f"屏幕第{cell.screen_row0 + 1}行第{cell.screen_col0 + 1}列弃置标记拿不准"
                )
            if SKIP_EQUIPPED_ENABLED and self.preset.skip_equipped and tile_has_portrait(
                image,
                cell.center_x - 56,
                cell.center_y - 68,
                cell.center_x + 56,
                cell.center_y + 68,
            ):
                skips[cell.global_index] = "已装备"
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
        """§7.1 disc_total：包住单盘的入口到全部动作/复核/统计/恢复收尾，异常也走 finally。

        从函数入口、任何 Trace/检查点/点击前开始，不从 click_client 内部起算，也不
        沿用现有 ``discs[].seconds``。无活动会话时只跑内层，不改变返回值/异常。
        """
        session = scanperf.current_session()
        if session is None:
            return self._process_cell_inner(
                hwnd, job, state, cell, sub_preset, accepted_ids, up_counts
            )
        details = {"input_sent": False, "result": None, "processed": False,
                   "inputs": [], "decisions": [], "preset_sha256": runevidence.digest(sub_preset),
                   "accepted_sha256": runevidence.accepted_digest(accepted_ids)}
        previous = self._disc_event_id
        with scanperf.timed("disc_total", details=details, disc_index=cell.global_index) as span:
            self._disc_span = span
            self._disc_event_id = span.event_id
            self._latest_decision = None
            try:
                result = self._process_cell_inner(
                    hwnd, job, state, cell, sub_preset, accepted_ids, up_counts
                )
                span.set(result=result)
                return result
            finally:
                # §7.1：先把等待明细并入本盘输入记录，再让 disc_total 落盘，两份副本保持一致。
                self._merge_wait_receipts()
                self._disc_span = None
                self._disc_event_id = previous
                self._latest_decision = None
                scanperf.set_scope(stage_level=None)

    def _process_cell_inner(
        self,
        hwnd: int,
        job: TaskJob,
        state: TraversalState,
        cell: GridCell,
        sub_preset: Preset,
        accepted_ids,
        up_counts: dict,
    ) -> str:
        """处理一个格位。返回 done / rescan / upgrade。"""
        row, col = cell.display()
        cell_tag = self._cell_tag(cell)
        where = f"全局第{row}行第{col}列（序号 {cell.global_index}）"
        self.trace.new_disc()
        with self._business_input("select_disc", target={
            "global_index": cell.global_index, "x": cell.center_x, "y": cell.center_y,
        }):
            select_release = click_client(hwnd, cell.center_x, cell.center_y)
        if self._disc_span is not None:
            self._disc_span.set(input_sent=True)
        try:
            selection = self._wait_ready("select_disc", release=select_release, cell=cell)
        except ReadinessTimeout as timeout_exc:
            # §6.3：超时后补拍一张新帧，只有仍是已知错页才走原一次 Esc 恢复。
            if self._select_timeout_recheck(hwnd, timeout_exc, where, cell_tag, cell) == "rescan":
                return "rescan"
            raise RunnerError(f"{cell_tag} 选择超时：{timeout_exc}，停止任务链")
        detail_image = selection.frame
        self.trace.ok("detail", "click_cell", where, image=detail_image)
        disc = selection.disc
        if disc is None:
            self.trace.fail("detail", "parse", f"{where} 没读出盘面，停止任务链", image=detail_image)
            raise RunnerError(f"{cell_tag} 盘面识别失败")
        self._processed += 1
        self._detail_disc = disc
        if self._disc_span is not None:
            # §9.2：本盘最新已验证 Disc 的核心盘面随 disc_total 事件保存。
            self._disc_span.set(core=self._core_of(disc), processed=True)
        # 统计：从认出盘面这一刻起算这件盘的耗时（含进强化页、逐档升级、收尾）。
        # slot/sets 取所属任务的过滤值（每个任务按固定号位+套装过滤），
        # line_count 取副词条条数——这些只用于「升满成功明细」，不影响统计口径。
        disc_slot = self.stats.disc_begin(
            job.job_id, cell.global_index, disc.level,
            slot=job.slot, sets=job.sets, line_count=disc.line_count,
        )
        self.trace.ok(
            "detail",
            "parse",
            f"{where} 等级{disc.level} 副词条{len(disc.substats)}条"
            + ("（已锁定）" if disc.locked else "")
            + ("（已弃置）" if disc.discarded else ""),
            image=detail_image,
        )

        decision = decide(disc, sub_preset, accepted_ids=accepted_ids)
        self._note_decision(disc, decision, sub_preset, accepted_ids)
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
                img = ""
                if disc.level >= MAX_LEVEL:
                    img = self._kept_image_path(disc_slot)
                self.stats.disc_end(disc_slot, OUTCOME_UPGRADED, disc.level, decision.score, image=img)
                return "done"  # 没进强化页，画面未变，坐标仍然有效
            if disc.discarded:
                self._set_discard_label(hwnd, discarded=True, value=False)
            # 进强化页前保存恢复点（spec §6.3.1）；连续升阶途中沿用同一恢复点，
            # 强化后的焦点不覆盖它。
            state.begin_upgrade(cell.global_index)
            if self._enter_upgrade_page(hwnd, before=disc):
                self._verified_upgrade_level = self._upgrade_loop(
                    hwnd,
                    job,
                    cell.global_index,
                    cell_tag,
                    sub_preset,
                    accepted_ids,
                    up_counts,
                    disc_slot,
                    initial_disc=disc,
                    initial_generation=self._upgrade_entry_generation,
                )
            else:
                raise RunnerError(f"{cell_tag} 进入强化页失败，停止任务链")
            # 最终保留/跳过/弃置完成且确认回到仓库后，提交该件进度（spec §6.3.3）。
            state.finish_upgrade(CellStatus.DONE)
            self._set_status("upgrade_done", f"next_index={state.next_index}", cell_tag)
            return "upgrade"
        if decision.action == Action.MARK_DISCARD and disc.discarded:
            # 口径（2026-10-05）：点开并读出盘面就已计 1 件处理；这里维持弃置标记不动，
            # 结果清单同样记一条——凡点击过的盘都出现在清单里，判定弃置计数照记、实际打标不记。
            self._record(job, disc, decision)
            self.log.info("cell %s already discarded, re-judge keeps it (counted as processed)", cell_tag)
            self.trace.note(f"{where} 已经打过弃置标记，维持不动；本件已计 1 件处理")
        elif decision.action == Action.SKIP and disc.discarded:
            self._record(job, disc, decision)
            self.log.info("cell %s has discard tag, skip (counted as processed)", cell_tag)
            self.trace.note(f"{where} 有弃置标记，跳过；本件已计 1 件处理")
        else:
            self._record(job, disc, decision)
            self._act(hwnd, decision.action, disc)
            self.log.info("cell %s action=%s level=%d", cell_tag, decision.action.value, disc.level)
            message = f"{where} 已{trace_action(decision.action.value)}"
            if decision.action == Action.MARK_DISCARD and not self.preset.labels.discard:
                message = f"{where} 判为弃置，标签设置关闭，未打标签"
            self.trace.ok("detail", decision.action.value, message)
            self._set_status("action", decision.action.value, cell_tag)
        self.stats.disc_end(
            disc_slot, outcome_for_action(decision.action.value), disc.level, decision.score
        )
        return "done"

    def _select_timeout_recheck(self, hwnd: int, timeout_exc, where: str, cell_tag: str, cell) -> str | None:
        """§6.3：select_disc 超时后补拍一张新帧，只有仍是已知错页才走原一次 Esc 恢复。

        补拍前后都 checkpoint 并取当前绑定窗口新帧；新帧已是正确页面/未知/对象证据不足
        一律以选择超时停止（返回 None），不追加 Esc、不推进正常动作。仅当新帧仍明确为
        menu/storage/overworld 时才按一次 Esc 并等 0.6 秒，再严格认 drive_disc 决定重扫或失败。
        补拍/判页作为 disc_total 下的 probe 记录（purpose=select_timeout_recheck）。
        """
        self._checkpoint()
        session = scanperf.current_session()
        if session is None:
            image, page, *_ = self._grab(allow_esc=False)
        else:
            details = {
                "purpose": "select_timeout_recheck",
                "wait_id": getattr(timeout_exc, "wait_id", None),
                "sample_id": None,
                "sample_index": 0,
                "kind": "select_disc",
                "captured_at": None,
                "probe_ended_at": None,
                "ready": False,
                "state_key": None,
                "pause_generation": None,
                "interrupt_reason": None,
                "evidence": None,
            }
            with scanperf.timed("probe", parent_id=self._disc_event_id, details=details) as span:
                image, page, *_ = self._grab(allow_esc=False)
                span.set(captured_at=time.monotonic(), probe_ended_at=time.monotonic(), evidence=self._evidence(return_layer=page))
        self._checkpoint()
        if page not in {"menu", "storage", "overworld"}:
            self.log.warning("cell %s 选择超时且新帧不是可恢复错页（page=%s），停止", cell_tag, page)
            self.trace.fail(page, "select", f"{where} 选择超时，未确认可恢复的错页", image=image)
            return None
        self.log.warning("cell %s 点开了 %s 且已超时，按 Esc 退回", cell_tag, page)
        self.trace.warn(page, "select_timeout", f"{where} 选择超时且停在{trace_page(page)}，按 Esc 退回", image=image)
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

    def _make_recovery_cache(self, state, cells, skips) -> RecoveryCache | None:
        image = self._scan_image
        if image is None or image.isNull() or not cells or not state.position_valid:
            return None
        if getattr(self, "_last_count", None) is None:
            return None
        ordered = sorted(cells, key=lambda cell: cell.global_index)
        levels = [cell.level for cell in ordered]
        if any(level < 0 for level in levels) or levels != sorted(levels, reverse=True):
            return None
        return RecoveryCache(
            state.job_id, state.filter_signature, image.width(), image.height(),
            self._last_count, tuple(ordered), dict(skips),
        )

    def _recovery_summary(self, image, state, cache) -> None:
        """只核对页面、尺寸与标题数量；不读全网格等级/状态。"""
        from zzz_ddtool.ocr import read_rect

        if (state.job_id, state.filter_signature) != (cache.job_id, cache.filter_signature):
            raise RunnerError("恢复缓存任务或筛选签名已变化，停止任务链")
        if (image.width(), image.height()) != (cache.width, cache.height):
            raise NavigationError("恢复时窗口尺寸改变，缓存坐标不可用")
        if not is_page(image, DRIVE_DISC):
            raise NavigationError("恢复时未确认驱动盘仓库页")
        if cache.total is not None:
            total = parse_warehouse_count(
                read_rect(image, COUNT_AREA), width=image.width(), height=image.height()
            )
            if total is None:
                raise NavigationError("恢复时标题数量读不清")
            if total != cache.total:
                raise RunnerError(f"恢复时仓库数量 {cache.total} → {total}，缓存编号失效，停止任务链")

    # 单列位置检查只使用等级文字的位置，不读取等级数值、不生成候选或判断标记。
    RECOVERY_COLUMN_AREA = (100, 200, 230, 945)

    def _recovery_column_lines(self, image):
        from zzz_ddtool.ocr import read_rect

        return read_rect(image, self.RECOVERY_COLUMN_AREA)

    def _recovery_position_reason(self, image, state) -> str | None:
        metrics = self._metrics(state, image)
        rows = []
        for line in self._recovery_column_lines(image):
            if not line.text.replace(" ", "").startswith("等级"):
                continue
            icon_y = metrics.icon_center_of_label((line.top + line.bottom) / 2)
            row = metrics.screen_row_of(icon_y, state.row_phase_px or 0.0)
            if row is None or not 0 <= row < GRID_ROWS:
                return "恢复目标单列行位置/相位不匹配"
            rows.append(row)
        if len(set(rows)) < 2 or len(rows) != len(set(rows)):
            return "恢复目标单列行位置证据不足"
        return None

    def _reuse_recovery_cache(self, state, cache, image) -> None:
        self._checkpoint()
        metrics = self._metrics(state, image)
        top, phase = state.viewport_top_row0, state.row_phase_px
        cells = [replace(
            cell,
            screen_row0=cell.global_row0 - top,
            center_x=int(round(metrics.center_x_of(cell.global_col0))),
            center_y=int(round(metrics.row_center_y(cell.global_row0, top, phase))),
        ) for cell in cache.cells]
        # 已解决前缀的旧等级/标记不再用于决策；保留格位几何仅供下一次前进选锚点。
        skips = {index: reason for index, reason in cache.skips.items() if index >= state.next_index}
        self._scan_image = image
        self._restored_scan = (cells, [], skips)
        self._restored_scan_reused = True
        state.clear_resume()
        self.trace.ok(
            "drive_disc", "recovery_cache",
            f"复用未处理候选：next_index={state.next_index}，未重扫等级/标签，工作批次不变",
            image=image,
        )

    def _restore_position(self, hwnd: int, state: TraversalState, cells=None, *, cache=None) -> None:
        """强化改变排序后回到仓库并继续（spec §5.3、§6.3.5；改造方案 §5.2、§6）。

        有本批缓存时，单屏只确认页面/尺寸/数量，长列表置顶返回保存视口后只核对
        单列标签位置；复用未处理候选，不再扫描全网格等级/状态。
        缓存缺失或简查证据不足才走原有重扫分支；不支持成员退出。

        cells：进入强化前的格位，供异常后备复核；cache 为本批已确认的恢复缓存。
        """
        if state.resume_point is None:
            raise RunnerError("强化后没有保存的恢复点，停止任务链")
        self._checkpoint()
        if cache is not None:
            if (state.job_id, state.filter_signature) != (cache.job_id, cache.filter_signature):
                raise RunnerError("恢复缓存任务或筛选签名已变化，停止任务链")
            if state.viewport_mode != VIEWPORT_SINGLE_SCREEN:
                self._restore_position_via_top(hwnd, state, cache=cache)
                return
            for attempt in range(max(1, self.preset.ocr_retry)):
                self._checkpoint()
                image = self._grab(allow_esc=False, need_page=False)[0]
                try:
                    self._recovery_summary(image, state, cache)
                    reason = check_layout_reference(
                        self._origin_center_y, self._row_pitch, image.width(), image.height()
                    )
                    if reason:
                        raise NavigationError(reason)
                except NavigationError as exc:
                    self.log.warning("单屏恢复简查第 %d 次失败：%s", attempt + 1, exc)
                    if attempt + 1 < max(1, self.preset.ocr_retry):
                        time.sleep(0.3)
                    continue
                state.confirm_viewport(0, 0.0, row_pitch_px=self._row_pitch, at_top=True, at_bottom=True)
                self._reuse_recovery_cache(state, cache, image)
                return
            self.trace.warn("drive_disc", "restore_fallback", "单屏页面/数量简查耗尽，异常路径重扫")
        if state.viewport_mode == VIEWPORT_SINGLE_SCREEN and state.single_screen_total is not None:
            if cache is None:
                self._restore_single_screen(hwnd, state, list(cells or ()))
            else:
                self._restore_single_screen(hwnd, state, list(cells or ()), cache=cache)
            return
        self._restore_position_via_top(hwnd, state)

    def _restore_single_screen(self, hwnd: int, state: TraversalState, cells, *, cache=None) -> None:
        """单屏任务强化后原地复核（改造方案 §5.2、§6）。

        仅在缓存缺失或正常简查失败时调用。页面、布局/相位与完整覆盖证据须成立；
        缓存可用时只核对数量和未处理后缀，不比较已过时的前缀等级。
        无缓存时保留原有强化前后快照核对。
        通过就用新坐标继续（缓存新扫描给主循环，不新建工作批次、不重复计数）。

        后备复核区分以下情况：
        - 布局或相位异常（含窗口尺寸变化、半行偏移）→ 放弃快速路径，回退到既有
          置顶定位恢复；
        - 本件升级后在已解决前缀内按等级降序前移 → 核对强化页确认的最终等级及
          未解决后缀，使用新坐标继续，next_index 不回退；
        - 数量减少、无法由本件升级解释的等级/后缀变化 → 停止任务链。
          切页置顶不能恢复物品原来的身份关联，所以不许把它当兜底。
        """
        before = grid_snapshot(cells, total=state.single_screen_total)
        active_index = None if state.resume_point is None else state.resume_point.next_index - 1
        attempts = max(1, self.preset.ocr_retry)
        fallback_reason = ""
        attempt = 0
        while attempt < attempts:
            attempt += 1
            image, lines = self._grab_label_frame(hwnd)
            if not is_page(image, DRIVE_DISC):
                fallback_reason = "当前不是驱动盘仓库页"
                break
            try:
                origin, pitch = measure_layout(image, lines=lines)
            except NavigationError as exc:
                fallback_reason = str(exc)
                if attempt < attempts:
                    time.sleep(0.3)
                continue
            reason = check_layout_reference(origin, pitch, image.width(), image.height())
            if reason:
                fallback_reason = reason
                break
            self._origin_center_y = float(origin)
            self._row_pitch = float(pitch)
            profile = self._profile
            if profile is not None:
                profile = replace(
                    profile, row_pitch_px=self._row_pitch, origin_center_y=self._origin_center_y
                )
                self._profile = profile
                state.navigation_profile = profile.as_dict()
            cells_now, problems, skips = self._scan_cells(state, image=image, lines=lines)
            total = self._last_count
            ok, why = single_screen_verdict(
                cells_now,
                self._last_empties,
                self._last_unclear,
                total_count=total,
            )
            if ok and problems:
                ok, why = False, f"网格映射不完整：{problems[0]}"
            if not ok:
                fallback_reason = why
                self.log.warning("单屏原地复核第 %d 次不成立：%s", attempt, why)
                if attempt < attempts:
                    time.sleep(0.3)
                continue
            if cache is None:
                change = snapshot_change(
                    before,
                    grid_snapshot(cells_now, total=total),
                    active_index=active_index,
                    resolved_prefix=state.next_index,
                    upgraded_level=getattr(self, "_verified_upgrade_level", None),
                )
            else:
                old_suffix = {cell.global_index: cell.level for cell in cache.cells if cell.global_index >= state.next_index}
                new_suffix = {cell.global_index: cell.level for cell in cells_now if cell.global_index >= state.next_index}
                change = ("仓库数量变化" if total != cache.total else
                          "未处理后缀变化" if old_suffix != new_suffix else None)
            if change:
                # 成员或顺序变了：旧格位关联失效，绝不能按新序号继续（会漏盘）。
                self.trace.fail(
                    "drive_disc",
                    "restore",
                    f"强化后列表成员变化：{change}，旧候选关联失效，停止任务链",
                    image=image,
                )
                raise RunnerError(f"强化后列表成员变化（{change}），停止任务链待关联规则完善")
            self._restored_scan = (cells_now, problems, skips)
            self._restored_scan_reused = False
            state.mark_viewport_mode(VIEWPORT_SINGLE_SCREEN, total=total)
            state.confirm_viewport(
                0, 0.0, row_pitch_px=self._row_pitch, at_top=True, at_bottom=True
            )
            state.clear_resume()
            self.trace.ok(
                "drive_disc",
                "single_screen_recheck",
                f"原地复核通过：共 {total} 个盘整屏可见，已解决区允许本件升级前移，"
                f"未解决后缀保持不变；next_index={state.next_index}；未切页、未滚轮",
                image=image,
            )
            return
        self.log.warning("单屏原地复核未通过（%s），回退置顶定位恢复", fallback_reason)
        self.trace.warn(
            "drive_disc",
            "restore_fallback",
            f"单屏原地复核未通过：{fallback_reason}，回退置顶定位恢复",
        )
        self._restore_position_via_top(hwnd, state, cache=cache)

    def _restore_position_via_top(self, hwnd: int, state: TraversalState, *, cache=None) -> None:
        """「切页置顶 → 按保存视口批量返回」是完整的一次恢复尝试，最多两次；第二次
        只用于可重试的运行误差（目标网格/相位暂时读不出、位移或端点不一致）。
        请求超上界、协议缺失、进入流程外错误页直接 failed，不做第二次。每次尝试
        后有缓存则只简查位置并复用候选；缓存缺失或定位证据不足才重扫。
        """
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
            if cache is None:
                self._restore_once(hwnd, state, saved)
            else:
                self._checkpoint()
                try:
                    self._restore_once(hwnd, state, saved, cache=cache)
                    self._checkpoint()
                    image = self._grab(allow_esc=False, need_page=False)[0]
                    self._recovery_summary(image, state, cache)
                    reason = self._recovery_position_reason(image, state)
                except NavigationError as exc:
                    last_reason = str(exc)
                    state.invalidate_position()
                    self.log.warning("恢复简查第 %d 次失败：%s", attempt, exc)
                    continue
                if reason is None:
                    self._reuse_recovery_cache(state, cache, image)
                    return
                self.trace.warn("drive_disc", "restore_fallback", f"定位简查失败：{reason}，异常路径重扫")
            cells, problems, skips = self._scan_cells(state)
            if cache is not None and not problems:
                if self._last_count is None:
                    problems.append("异常重扫的仓库总数仍未知")
                elif self._last_count != cache.total:
                    raise RunnerError("异常重扫发现仓库数量变化，停止任务链")
                old = {cell.global_index: cell.level for cell in cache.cells if cell.global_index >= state.next_index}
                new = {cell.global_index: cell.level for cell in cells if cell.global_index >= state.next_index}
                if old != new:
                    raise RunnerError("异常重扫发现未处理后缀变化，停止任务链")
            if not problems:
                self._restored_scan = (cells, problems, skips)
                self._restored_scan_reused = False
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

    def _restore_once(self, hwnd: int, state: TraversalState, saved, *, cache=None) -> None:
        """一次完整的「切页置顶 → 按保存视口批量返回」，并提交保存的实际位置。"""
        profile = self._profile
        if profile is None:
            raise RunnerError("没有可用的滚轮协议，无法恢复位置")

        with self.stats.timed("导航", "恢复：切页置顶"):
            reset_to_top(hwnd)
        image, _method, _hwnd = grab_game_image(hwnd)
        if cache is None:
            origin, pitch = measure_layout(image)
        else:
            self._recovery_summary(image, state, cache)
            origin, pitch = measure_layout(image, lines=self._recovery_column_lines(image))
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
        """恢复批量滚动后等列表缓动停稳，再抓图做定位检查或异常校验扫描。

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
        """§6.1.1：只对解析**正常返回 None** 按 §3.2 的次数/间隔重拍。

        解析实际抛出的 ``OcrError`` 立即原样向上传递，不改 None、不重试、不当超时；
        其他程序错误同样不在此吞掉。至少调用一次 parser（``max(1, ocr_retry)`` 次）。
        """
        reason = "未识别出盘面"
        attempts = max(1, self.preset.ocr_retry)
        for attempt in range(attempts):
            disc = parser(image)
            if disc is not None:
                return disc
            if attempt + 1 < attempts:
                time.sleep(0.1)
                image = self._grab(allow_esc=False, need_page=False)[0]
        self.trace.fail(page, "parse", f"重试耗尽：{reason}", image=image)
        raise RunnerError(f"{page} 识别失败：{reason}，停止任务链")

    # 「材料返还」确认弹窗：盘升到满级时，多出来的强化素材会返还并弹一个确认框，
    # 把强化页整个盖住，OCR 在等级/副属性几个区域全都读 0 行，误报「未识别出盘面」
    # 打断整条任务链（2026-10-04 全链路实测：处理到第 4 件、12→15 满级后弹出）。
    # 注意游戏文案是「返还」不是「退还」，标题 OCR 实测 (898,404,1021,442) score=1.00。
    MATERIAL_REFUND_TITLE = "材料返还"

    def _dismiss_material_refund(self, hwnd: int, image: QImage, *, resume_result: bool = False) -> bool:
        """识别「材料返还」确认弹窗并点「确认」；没这个弹窗就返回 False。

        只在盘面识别失败之后调用——全窗口 OCR 约 1.5s，正常路径不付这个代价。
        「确认」按钮靠 OCR 找字点中心，不硬编坐标：弹窗高度随退还材料行数变。
        §4.3：resume_result=False 只消费 refund_dismiss 的收尾预算再交回已有识别重试；
        True 时不在本函数内睡固定值，确认释放点由调用方接续 refund_confirm 完整等待。
        """
        from zzz_ddtool import ocr as _ocr

        lines = _ocr.read_rect(image, (0, 0, image.width(), image.height()))
        if not any(self.MATERIAL_REFUND_TITLE in line.text.replace(" ", "") for line in lines):
            return False
        for line in lines:
            if "确认" in line.text.replace(" ", ""):
                role = "refund_confirm" if resume_result else "refund_dismiss"
                with self._business_input(role):
                    self._refund_release = click_client(
                        hwnd, (line.left + line.right) // 2, (line.top + line.bottom) // 2
                    )
                self._refund_input_id = self._last_input_id
                self.log.info("材料返还弹窗：已点「确认」")
                self.trace.ok("upgrade", "refund", "材料返还弹窗已确认")
                if not resume_result:
                    # refund_dismiss：只消费原 180+500 的收尾预算，再交回已有有限识别重试。
                    wait_release(
                        "refund_dismiss", self._refund_release, fallback_ms=_REFUND_DISMISS_FLOOR_MS
                    )
                return True
        self.log.warning("检测到「材料返还」但没找到「确认」按钮，交回调用方报错")
        return False

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

    def _read_label_state(self, hwnd: int, context: _LabelContext) -> tuple[dict, QImage]:
        """只检查绑定窗口、仓库和提示行；仅暂停代次改变时复验核心盘面。"""
        generation = self._checkpoint()
        image, _page, _method, actual_hwnd = self._grab(allow_esc=False, need_page=False)
        if actual_hwnd != hwnd or (self._hwnd is not None and self._hwnd != hwnd):
            raise RunnerError("标签操作的游戏窗口改变，停止任务链")
        if self._pid is not None and window_pid(hwnd) != self._pid:
            raise RunnerError("标签操作的游戏进程改变，停止任务链")
        size = (image.width(), image.height())
        if context.size is None:
            context.size = size
        if size != context.size or not is_page(image, DRIVE_DISC):
            raise RunnerError("标签操作时尺寸改变或已离开驱动盘仓库，停止任务链")
        if generation != context.generation:
            current = parse_disc(image) if context.before is not None else None
            if current is None or self._label_core(current) != self._label_core(context.before):
                raise RunnerError("暂停后标签操作对象无法确认或盘面不一致，停止任务链")
            context.generation = generation
        state = read_action_state_fast(image)
        # 识别中发生暂停时，丢弃旧帧；下一次读取再复验，不能凭旧结果发下一键。
        if self._checkpoint() != generation:
            return {"locked": None, "discarded": None}, image
        return state, image

    @staticmethod
    def _label_core(disc: Disc) -> tuple:
        return disc.level, disc.rarity, sorted((s.name, s.rolls) for s in disc.substats)

    def _note_label_input(self, kind: str, key: str) -> None:
        """§9.2 coverage：在真实发出标签按键且复核确认后记录一次标签输入证据。

        只记实际按键；已达目标未按键不记。事件归属取当前 disc_total 区间（无会话为 None）。
        """
        span = self._disc_span
        self._label_inputs.append(
            {
                "kind": kind,
                "key": key,
                "disc_event_id": span.event_id if span is not None else None,
                "job_index": span.job_index if span is not None else None,
                "disc_index": span.disc_index if span is not None else None,
            }
        )

    def _verify_label(self, hwnd: int, *, key: str, value: bool, other_value: bool,
                      context: _LabelContext) -> QImage:
        """R/T 共用快速复核；轮询只读，不补发任何标签输入。"""
        other = "discarded" if key == "locked" else "locked"
        actual_input = self._input_record(self._last_input_id)
        state = {}
        verified = False
        error = None
        try:
            for attempt in range(max(1, self.preset.ocr_retry) * 4):
                self._checkpoint()
                if attempt:
                    # §4.3：第一次快速复核由释放总下限覆盖（不叠加 50 ms）；后续观察仍间隔 50 ms。
                    time.sleep(0.05)
                state, image = self._read_label_state(hwnd, context)
                if state[other] is not None and state[other] != other_value:
                    raise RunnerError("按键后另一标签状态意外变化，不重复按键，停止任务链")
                if state[key] is value and state[other] is other_value:
                    verified = True
                    return image
            raise RunnerError("按键后标签状态无法确认，不重复按键，停止任务链")
        except BaseException as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            if actual_input is not None:
                actual_input["result"] = dict(verified=verified, actual=state.get(key),
                                              other_actual=state.get(other), exception=error)

    def _set_discard_label(self, hwnd: int, *, discarded: bool, value: bool,
                           before: Disc | None = None) -> bool:
        """所有弃置标签增删的统一入口；高级设置关闭时不发送标签按键。"""
        if not self.preset.labels.allows("discard"):
            self.log.info("弃置标签设置关闭：保留判断结果，不修改标签")
            self.trace.note("标签设置关闭，不修改弃置标签")
            return False
        # discarded 是调用者先前的读数；输入前必须使用当前帧，不能信旧标签。
        context = _LabelContext(before or self._detail_disc, self._checkpoint())
        state = None
        for _attempt in range(max(1, self.preset.ocr_retry)):
            state, _image = self._read_label_state(hwnd, context)
            if state["discarded"] is value:
                return False
            if state["locked"] is not None and state["discarded"] is not None:
                break
            time.sleep(0.05)
        if state is None or state["locked"] is None or state["discarded"] is None:
            raise RunnerError("标签输入前状态无法确认，停止任务链")

        def send(vk: int, expected: dict, kind: str, key: str, target: bool) -> None:
            def validate() -> None:
                actual, _image = self._read_label_state(hwnd, context)
                if actual != expected:
                    raise RunnerError("标签输入前状态改变或未知，停止任务链")
            with validated_inputs(validate, reuse_unchanged=True), self._business_input(
                kind, target={"key": key, "before": expected[key], "value": target},
            ):
                release = press_key_once(hwnd, vk)
            # §4.3：标签总下限 630 从按键释放点计。
            wait_release(kind, release, fallback_ms=_LABEL_FLOOR_MS)

        if value and state["locked"]:
            # G07：先解锁再弃置，避免直接 R 产生确认弹窗。
            send(VK_T, state, "label_unlock_before_discard", "locked", False)
            self._verify_label(hwnd, key="locked", value=False,
                               other_value=state["discarded"], context=context)
            state = {"locked": False, "discarded": state["discarded"]}
            self._note_label_input("label_unlock_before_discard", "T")
            self.log.info("弃置前已确认解锁 (T)")
        send(VK_R, state, "label_discard" if value else "clear_discard", "discarded", value)
        self._verify_label(hwnd, key="discarded", value=value,
                           other_value=state["locked"], context=context)
        self.log.info("discard label=%s (R)", value)
        if value:
            # 「判定为弃置」和「真打上标记」不是一个数，这里只数后者。
            self.stats.note_discard_label()
            self._note_label_input("label_discard", "R")
        return True

    def _finish_kept_upgrade(self, hwnd: int, disc: Disc) -> None:
        """在当前盘仍被选中时完成保留标签；不能在切页置顶后补按 T。"""
        self._esc_back_to_grid(hwnd)
        if not self.preset.labels.allows("save"):
            self.log.info("保存标签设置关闭：升满保留，不修改锁定标签")
            return

        def read_current() -> tuple[Disc, QImage]:
            for attempt in range(max(1, self.preset.ocr_retry)):
                self._checkpoint()
                image, page, *_ = self._grab(allow_esc=False)
                if page != "drive_disc":
                    raise RunnerError("保留锁定时已离开驱动盘仓库，停止任务链")
                # §6.1.1：解析实际抛出的 OcrError 立即原样传播，不改 None、不再重读；
                # 只有正常返回 None 才按 ocr_retry 次数重拍。
                current = parse_disc(image)
                if current is not None:
                    expected_stats = sorted((s.name, s.rolls) for s in disc.substats)
                    actual_stats = sorted((s.name, s.rolls) for s in current.substats)
                    if (current.level, current.rarity, actual_stats) != (disc.level, disc.rarity, expected_stats):
                        raise RunnerError("保留锁定时盘面与刚升级的盘不一致，停止任务链")
                    if current.locked is not None:
                        return current, image
                if attempt + 1 < max(1, self.preset.ocr_retry):
                    time.sleep(0.2)
            raise RunnerError("保留锁定时锁定状态无法确认，停止任务链")

        current, image = read_current()
        if current.locked is True:
            self.log.info("升满保留：盘已锁定，不重复按 T")
            self.trace.ok("detail", "lock", "升满保留，已锁定", image=image)
            return

        def validate_unlocked() -> None:
            checked, _ = read_current()
            if checked.locked is not False or checked.discarded is not False:
                raise RunnerError("锁定输入前标签状态改变或未知，停止任务链")

        context = _LabelContext(disc, self._checkpoint(), (image.width(), image.height()))
        with validated_inputs(validate_unlocked), self._business_input(
            "label_save", target={"key": "locked", "before": False, "value": True},
        ):
            save_release = press_key_once(hwnd, VK_T)
        wait_release("label_save", save_release, fallback_ms=_LABEL_FLOOR_MS)
        image = self._verify_label(hwnd, key="locked", value=True,
                                   other_value=False, context=context)
        self._note_label_input("label_save", "T")
        self.log.info("升满保留：已确认锁定标签 (T)")
        self.trace.ok("detail", "lock", "升满保留，已确认 T 锁定", image=image)

    def _act(self, hwnd: int, action: Action, disc: Disc) -> None:
        # UPGRADE 在 _process_cell 里内联处理（需要 rescan/计数联动），这里只管其余动作。
        # 注意：这些动作都不按 Esc——详情保持常开，下一个格子点击直接切换。
        # R 是开关：按之前必须看 disc.discarded，否则打标签会变成摘标签（反之亦然）。
        if action == Action.MARK_DISCARD:
            self._set_discard_label(hwnd, discarded=disc.discarded, value=True, before=disc)
        elif action == Action.SKIP:
            self.log.info("action=SKIP")
        elif action == Action.STOP:
            self.log.info("action=STOP")

    def _esc_back_to_grid(self, hwnd: int, max_esc: int = 4) -> bool:
        """先认层再逐次退弹窗；已经在仓库时不按键、不制造等待，失败抛错终止任务链。

        §6.2/§6.3 return_warehouse：每次 Esc 后的静候由 ``return_warehouse`` 等待替代
        （modal→preview/warehouse、preview→warehouse 的允许边）。中间 preview 只结束本次
        Esc 等待，严格确认 warehouse 才返回 True；同层/unknown 不完成本次等待、不加计数。
        初始层由当前绑定窗口帧确认；最初 unknown 时按原 ocr_retry 次数/重拍间隔取证据，
        未发 Esc 就没有等待。层级枚举由 ``_return_layer`` 提供（错页直接抛 RunnerError）。
        """
        esc_count = unknown_count = 0
        image, _page, *_ = self._grab(allow_esc=False)
        while True:
            self._checkpoint()
            try:
                layer = self._return_layer(image)
            except RunnerError as exc:
                reason = f"返回驱动盘仓库失败：{exc}，停止任务链"
                self.trace.fail("esc", "esc", reason, image=image)
                raise RunnerError(reason)
            if layer == "warehouse":
                self.log.info("warehouse confirmed after %d esc, keep sidebar open", esc_count)
                return True
            if layer == "unknown":
                # 动画中可能暂时认不出，有限重拍；未知画面绝不盲按 Esc。
                unknown_count += 1
                if unknown_count >= max(1, self.preset.ocr_retry):
                    reason = "返回驱动盘仓库失败：无法确认盘面弹窗或仓库，停止任务链"
                    self.trace.fail("unknown", "esc", reason, image=image)
                    raise RunnerError(reason)
                time.sleep(0.2)
                image, _page, *_ = self._grab(allow_esc=False)
                continue
            unknown_count = 0
            if esc_count >= max_esc:
                reason = f"返回驱动盘仓库失败：{max_esc} 次回退后弹窗仍未关闭，停止任务链"
                self.trace.fail(layer, "esc", reason, image=image)
                raise RunnerError(reason)
            # 通用 press_key 会通过多个输入通道发键，回退必须只发一次。
            before_frame = image
            with self._business_input("return_warehouse", target={"from_layer": layer}):
                esc_release = press_key_once(hwnd, VK_ESCAPE)
            esc_count += 1
            try:
                result = self._wait_ready(
                    "return_warehouse", release=esc_release, before_frame=before_frame
                )
            except ReadinessTimeout as exc:
                reason = f"返回驱动盘仓库失败：Esc 后未稳定进入更浅层（{exc}），停止任务链"
                self.trace.fail(layer, "esc", reason, image=image)
                raise RunnerError(reason)
            image = result.frame

    def _click_text(self, hwnd: int, lines, needle: str, region_desc: str, tag: str, image=None):
        """在 OCR 结果里找含 needle 的行并点中心；命中返回释放回执，落空存图返回 None。"""
        for line in lines:
            if needle in line.text.replace(" ", ""):
                return click_client(hwnd, (line.left + line.right) // 2, (line.top + line.bottom) // 2)
        self.log.warning("UPGRADE: 没找到%s，中断回退", region_desc)
        self.trace.fail("upgrade", "click_cell", f"在{region_desc}没找到「{needle}」", image=image)
        return None

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

    def _enter_upgrade_page(self, hwnd: int, *, before: Disc) -> bool:
        """详情页点「查看/强化」→ 缩放页点「》」→ 强化页（弹层）。

        §6.3：详情按钮返回后的 0.9 秒由 ``open_preview`` 替代；箭头返回后的轮询与
        成功等待由一次 ``open_upgrade`` 替代。返回 True=已进强化页；False=中断
        （调用方 Esc 回仓库页）。``before`` 为本次 select 得到的盘面，用于接续证据。
        """
        from zzz_ddtool.ocr import read_rect

        self._upgrade_entry_generation = None
        # 1) 详情页大按钮（0 级也显示「查看」）
        image, *_ = self._grab(allow_esc=False, need_page=False)
        hit = None
        button_lines = read_rect(image, self.UPGRADE_BTN_AREA)
        for text in ("查看", "强化"):
            for line in button_lines:
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
        with self._business_input("open_preview"):
            preview_release = click_client(hwnd, (hit.left + hit.right) // 2, (hit.top + hit.bottom) // 2)
        try:
            preview = self._wait_ready("open_preview", release=preview_release, before=before)
        except ReadinessTimeout as exc:
            # §6.3：open_preview 超时不重发详情按钮、不点箭头，清理后返回 False。
            self.log.warning("UPGRADE: open_preview 超时（%s）", exc)
            self.trace.fail("zoom", "enter_upgrade", f"未进入预览层：{exc}")
            self._esc_back_to_grid(hwnd)
            return False
        image = preview.frame

        # 2) 缩放页「等级」右缘 +110px 的「>>」箭头（用预览完成帧定位）
        arrow = self._zoom_arrow_of(image, before)
        if arrow is None:
            self.log.warning("UPGRADE: 缩放页没找到可定位的「等级」/箭头")
            self.trace.fail("zoom", "enter_upgrade", "没找到「等级」两字或对象不一致", image=image)
            self._esc_back_to_grid(hwnd)
            return False
        ax, ay = arrow

        # 3) 点箭头进强化页；首次超时最多保留一次第二箭头输入（须重新确认同对象预览）。
        for attempt in (1, 2):
            with self._business_input("open_upgrade"):
                upgrade_release = click_client(hwnd, ax, ay)
            try:
                entered = self._wait_ready("open_upgrade", release=upgrade_release, before=before)
                # §6.2 首档接续证据：记录进入强化页确认时刻的暂停代次，供首档 seed 校验。
                self._upgrade_entry_generation = entered.pause_generation
                self.trace.ok("upgrade", "enter_upgrade", "已进强化页")
                return True
            except ReadinessTimeout as exc:
                self.trace.warn("zoom", "enter_upgrade", f"第 {attempt} 次点箭头没进强化页（{exc}）", image=image)
                if attempt == 2:
                    break
                # §6.3：第二次箭头前必须在新帧上重新确认仍是同对象预览层且箭头可定位。
                fresh, _page, *_ = self._grab(allow_esc=False, need_page=False)
                arrow = self._zoom_arrow_of(fresh, before)
                if arrow is None:
                    break
                ax, ay = arrow
        self.log.warning("UPGRADE: 未进入强化页（超时且未恢复同对象预览）")
        self.trace.fail("zoom", "enter_upgrade", "点箭头后未进入强化页")
        self._esc_back_to_grid(hwnd)
        return False

    def _zoom_arrow_of(self, image, before: Disc):
        """§6.2 open_preview 接续：从预览帧读「等级」行并校验与 before 一致，定位箭头。"""
        level_line = self._zoom_level_of(image)
        if level_line is None:
            return None
        level = self._level_of_line(level_line)
        if level is None or level != before.level:
            return None
        return level_line.right + 110, (level_line.top + level_line.bottom) // 2

    def _pick_material(self, hwnd: int, material_slot: int) -> bool:
        """[测试入口] 点第 N 个材料槽，只加一份素材（替代「阶段添加」）。

        真机复现「素材不足」用：单个材料槽加的经验吃不满一整档，点「升级」后
        等级停在下一档之前，正好走 _upgrade_loop 的期望等级校验。
        返回是否真的点出去了（槽号越界按最近的一档夹住）。
        """
        image, *_ = self._grab(allow_esc=False, need_page=False)
        index = min(max(1, material_slot), len(UPGRADE_MATERIAL_SLOTS))
        mx, my = scale_point(*UPGRADE_MATERIAL_SLOTS[index - 1], image.width(), image.height())
        with self._business_input("material_ready"):
            self._material_release = click_client(hwnd, mx, my)
        self.log.info("action=UPGRADE 测试入口：点第 %d 个材料槽 (%d,%d)", index, mx, my)
        self.trace.ok("upgrade", "step-material", f"测试入口：点第 {index} 个材料槽", image=image)
        return True

    def _upgrade_once(self, hwnd: int, material_slot: int = 0, *, before: Disc) -> bool:
        """强化页选好素材 →「升级」，升一阶。返回两步是否都走完。

        §6.2/§4.3：调用开始清空素材/升级释放回执；素材输入前取得 before_frame，素材输入后
        按释放点执行 material_ready；随后定位并点击一次「升级」，其释放点供
        ``_upgrade_loop`` 的 upgrade_result 使用。不保留原 1.5 秒等待。
        material_slot>0 是测试入口，走 _pick_material。
        """
        from zzz_ddtool.ocr import read_rect

        self._material_release = None
        self._upgrade_release = None
        before_frame, _page, *_ = self._grab(allow_esc=False, need_page=False)
        if material_slot:
            if not self._pick_material(hwnd, material_slot):
                return False
        else:
            with self._business_input("material_ready"):
                self._material_release = self._click_text(
                    hwnd, read_rect(before_frame, self.STAGE_ADD_AREA), "阶段添加", "「阶段添加」",
                    "step-stage-add", image=before_frame,
                )
            if self._material_release is None:
                return False
        if self._material_release is None:
            raise RunnerError("素材输入未成功发出，无法确认素材准备就绪，停止任务链")
        try:
            self._wait_ready(
                "material_ready", release=self._material_release,
                before=before, before_frame=before_frame,
            )
        except ReadinessTimeout as exc:
            raise RunnerError(f"素材准备就绪超时：{exc}，停止任务链")
        # material_ready 完成后才定位并点击一次「升级」。
        image, *_ = self._grab(allow_esc=False, need_page=False)
        with self._business_input("upgrade_result", target={
            "before_level": before.level, "expected_level": next_stage_level(before.level),
        }):
            self._upgrade_release = self._click_text(
                hwnd, read_rect(image, self.UPGRADE_CONFIRM_AREA), "升级", "「升级」",
                "step-upgrade-confirm", image=image,
            )
        if self._upgrade_release is None:
            return False
        self._upgrade_input_id = self._last_input_id
        self.log.info("action=UPGRADE 已发出升级输入（%s）", "材料槽" if material_slot else "阶段添加")
        self.trace.ok("upgrade", "upgrade", "已发出升级输入")
        return True

    def _discard_from_upgrade(self, hwnd: int) -> None:
        """确认回到仓库侧边详情后按 R；侧边详情本来就是网格，不再 Esc。"""
        self._esc_back_to_grid(hwnd)
        self._checkpoint()
        discarded = bool(self._detail_disc and self._detail_disc.discarded)
        if self._set_discard_label(hwnd, discarded=discarded, value=True):
            self.trace.ok("detail", "discard", "已按 R 打上弃置标记")

    def _reread_seed_frame(self, hwnd: int, cell_tag: str, seed: Disc) -> tuple[Disc, "QImage"]:
        """§6.2/W9：首档 seed 失效后的唯一一次补读——一张新帧、一次完整强化盘面读取。

        不调用 ``_read_disc_with_retry`` 再包多帧重读；``OcrError`` 等读取异常原样传播。
        返回 None（不在预期页面）或核心盘面与原盘不同都抛 RunnerError，不发素材/升级输入。
        """
        image, *_ = self._grab(allow_esc=False, need_page=False)
        disc = self._parse_upgrade_page(image)
        if disc is None:
            self.trace.fail("upgrade", "decide", f"{cell_tag} 首档接续失效，补读未在强化页读出盘面", image=image)
            raise RunnerError(f"{cell_tag} 首档接续失效后补读未读出强化盘面，停止任务链")
        if not self._same_upgrade_object(disc, seed):
            self.trace.fail(
                "upgrade", "decide",
                f"{cell_tag} 首档接续失效，补读盘面（{disc.level} 级）与原盘（{seed.level} 级）核心不符",
                image=image,
            )
            raise RunnerError(f"{cell_tag} 首档接续失效后补读盘面与原盘核心不符，停止任务链")
        self.log.info("cell %s 首档接续失效，补读新帧核对原盘一致，按补读盘面继续", cell_tag)
        return disc, image

    def _upgrade_loop(
        self,
        hwnd: int,
        job: TaskJob,
        global_index: int,
        cell_tag: str,
        sub_preset: Preset,
        accepted_ids,
        up_counts: dict,
        disc_slot: int = -1,
        *,
        initial_disc: Disc | None = None,
        initial_generation: int | None = None,
    ) -> int:
        """在强化页循环判断+升级，直到弃置/升满/跳过，最后回到网格。

        判断在强化页做（左边卡片有副属性），判升级就「阶段添加」+「升级」原地
        循环，判弃置才 Esc 回详情页按 R。不再每次升级完都 Esc 回网格全量重扫。
        每升一阶后复核等级有没有到下一档（3/6/9/12/15），到不了就是素材耗尽，
        报错停止整条任务链。

        返回强化页最终确认的等级，供单屏复核验证本件前移，不能用期望档位代替。
        disc_slot：这件盘在 RunStats 里的下标，用来记各档升级耗时（-1 = 不统计）。
        initial_generation：首档 seed 建立（open_upgrade 完成）时的暂停代次（§6.2）。
        """
        upgraded = 0
        # 本档从阶段添加/材料操作前起算，包含按钮定位、输入、等待和结果识别。
        stage_started: float | None = None
        # 上一轮 upgrade_result 的完成帧/盘面，交下一轮判断，不再重复解析该帧。
        pending: tuple[Disc, QImage | None] | None = None
        while True:
            if initial_disc is not None:
                seed, initial_disc = initial_disc, None
                paused = self.hotkeys is not None and self.hotkeys.paused
                generation = self._checkpoint()
                if not paused and (generation is None or generation == initial_generation):
                    # 接续证据完整：已解析当前侧栏且已确认进入强化页；尚未升级，词条数据没有变化。
                    disc = seed
                    image = None
                    self.log.info("cell %s 首档复用侧栏盘面，省略强化前重复词条 OCR", cell_tag)
                else:
                    # §6.2/W9：暂停或代次改变使 seed 失效。只对一张新帧做一次完整强化盘面
                    # 读取（_parse_upgrade_page 内部仍允许同帧 fast→legacy 及满级 detail 回退），
                    # 与原盘比较核心盘面；读不出/核心不同则停止，不发素材/升级/标签输入。
                    disc, image = self._reread_seed_frame(hwnd, cell_tag, seed)
            elif pending is not None:
                # §6.3：升级结果等待已消费返回帧/盘面，不再附加固定等待或重复解析。
                disc, image = pending
                pending = None
            else:
                image, *_ = self._grab(allow_esc=False, need_page=False)
                try:
                    disc = self._read_disc_with_retry(image, self._parse_upgrade_page, "upgrade")
                except RunnerError:
                    # 满级「材料返还」遮住盘面时先确认；其他识别错误原样传播。
                    if not self._dismiss_material_refund(hwnd, image):
                        raise
                    image, *_ = self._grab(allow_esc=False, need_page=False)
                    disc = self._read_disc_with_retry(image, self._parse_upgrade_page, "upgrade")
            if self._detail_disc is not None:
                disc = replace(
                    disc,
                    rarity=self._detail_disc.rarity,
                    locked=self._detail_disc.locked,
                    equipped=self._detail_disc.equipped,
                    discarded=self._detail_disc.discarded and not self.preset.labels.discard,
                )
            decision = decide(disc, sub_preset, accepted_ids=accepted_ids)
            self._note_decision(disc, decision, sub_preset, accepted_ids)
            if decision.action == Action.STOP:
                self._record(job, disc, decision)
                self.log.warning("cell %s 强化页判断不确定 (%s)，停止任务链", cell_tag, decision.reason)
                self.trace.fail("upgrade", "decide", f"判断不了：{decision.reason}，停止任务链", image=image)
                self.stats.disc_end(disc_slot, OUTCOME_FAILED, disc.level, decision.score)
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
                    img = ""
                    if disc.level >= MAX_LEVEL:
                        img = self._kept_image_path(disc_slot)
                    self.stats.disc_end(disc_slot, OUTCOME_UPGRADED, disc.level, decision.score, image=img)
                    self._esc_back_to_grid(hwnd)
                    return disc.level
                stage_started = time.monotonic()
                before_disc = disc
                expected_level = next_stage_level(before_disc.level)
                if not self._upgrade_once(hwnd, self.material_slot, before=before_disc):
                    raise RunnerError(f"{cell_tag} 强化按钮识别失败，停止任务链")
                upgraded += 1
                up_counts[global_index] = up_counts.get(global_index, 0) + 1
                self._record(job, disc, decision)
                # §6.3：用刚发出的升级输入时刻执行一次 upgrade_result，消费返回帧/盘面。
                disc, image = self._await_upgrade_result(
                    hwnd, cell_tag, before=before_disc, expected_level=expected_level,
                    disc_slot=disc_slot, stage_started=stage_started,
                )
                stage_started = None
                pending = (disc, image)
                self.log.info(
                    "cell %s UPGRADE done, level %d -> %d，强化页重判",
                    cell_tag, before_disc.level, disc.level,
                )
                self._set_status("upgraded", f"level {before_disc.level} -> {disc.level}", cell_tag)
                continue  # 留强化页再判断
            if decision.action == Action.MARK_DISCARD:
                self.log.info("cell %s 强化页判弃置，Esc 回详情页按 R", cell_tag)
                self.trace.ok("upgrade", "decide", f"判为弃置：{decision.reason}")
                self._record(job, disc, decision)
                self._detail_disc = disc  # 暂停复验使用最后实读等级/词条，而非升级前旧盘面。
                try:
                    self._discard_from_upgrade(hwnd)
                except RunnerStopped:
                    raise
                except (RunnerError, CaptureError, OcrError, InputError):
                    self.stats.disc_end(disc_slot, OUTCOME_FAILED, disc.level, decision.score)
                    raise
                self.stats.disc_end(disc_slot, OUTCOME_DISCARDED, disc.level, decision.score)
                return disc.level
            if decision.reason == "keep_max_level" and upgraded > 0:
                try:
                    self._finish_kept_upgrade(hwnd, disc)
                except RunnerStopped:
                    raise
                except (RunnerError, CaptureError, OcrError, InputError) as exc:
                    self.stats.disc_end(disc_slot, OUTCOME_FAILED, disc.level, decision.score)
                    if isinstance(exc, RunnerError):
                        raise
                    raise RunnerError(f"保留锁定收尾失败：{exc}，停止任务链") from exc
                self._record(job, disc, decision)
                img = ""
                if disc.level >= MAX_LEVEL:
                    img = self._kept_image_path(disc_slot)
                self.stats.disc_end(disc_slot, OUTCOME_UPGRADED, disc.level, decision.score, image=img)
                return disc.level
            # 其他 SKIP 不动，直接 Esc 回网格；STOP 已在前面停止整条任务链。
            self.log.info("cell %s 强化页判 %s，回网格", cell_tag, decision.action.value)
            self.trace.ok("upgrade", "decide", f"判为跳过：{decision.reason}")
            self._record(job, disc, decision)
            self.stats.disc_end(
                disc_slot, outcome_for_action(decision.action.value), disc.level, decision.score
            )
            self._esc_back_to_grid(hwnd)
            return disc.level

    # ---------------------------------------------------------------- 升级结果与返还（§6.2、§6.2.1）

    def _await_upgrade_result(
        self, hwnd: int, cell_tag: str, *, before: Disc, expected_level: int,
        disc_slot: int, stage_started: float | None,
    ) -> tuple[Disc, "QImage | None"]:
        """§6.2/§6.3：用刚发出的升级输入时刻执行一次 upgrade_result 并消费返回帧/盘面。

        成功消费返回帧/盘面后清空升级时刻；超时走有限诊断/素材不足出口；已确认返还抛
        ReadinessInterrupted 时按 §6.2.1 接续（首个 wait_total 仍按真实出口记 interrupted）。
        """
        release = self._upgrade_release
        if release is None:
            raise RunnerError(f"{cell_tag} 未记录升级输入释放点，停止任务链")
        try:
            result = self._wait_ready(
                "upgrade_result", release=release, before=before,
                expected_level=expected_level, trigger_input_id=self._upgrade_input_id,
            )
        except ReadinessInterrupted as interrupted:
            return self._resume_after_refund(
                hwnd, cell_tag, interrupted, before=before, expected_level=expected_level,
                disc_slot=disc_slot, stage_started=stage_started,
            )
        except ReadinessTimeout as timeout_exc:
            return self._upgrade_result_timeout(
                hwnd, cell_tag, timeout_exc, before=before, expected_level=expected_level,
                disc_slot=disc_slot, stage_started=stage_started,
            )
        # §6.3：成功消费返回的新帧/盘面后清空升级释放回执，普通成功路径不再附加固定等待。
        self._upgrade_release = None
        disc = result.disc
        if disc is None:
            raise RunnerError(f"{cell_tag} 升级结果未读出真实盘面，停止任务链")
        if stage_started is not None:
            self.stats.disc_stage(disc_slot, before.level, disc.level, time.monotonic() - stage_started)
        return disc, result.frame

    def _upgrade_result_timeout(
        self, hwnd: int, cell_tag: str, timeout_exc, *, before: Disc, expected_level: int,
        disc_slot: int, stage_started: float | None,
    ) -> tuple[Disc, "QImage | None"]:
        """§6.2：结果就绪超时后的有限诊断/素材不足出口，不重建预算、不转成功。"""
        attempts = max(1, self.preset.ocr_retry)
        last: Disc | None = None
        image = None
        for attempt in range(attempts):
            self._checkpoint()
            image, _page, *_ = self._grab(allow_esc=False, need_page=False)
            disc = self._parse_upgrade_page(image)  # OcrError 原样传播（§6.1.1）
            if disc is not None and disc.level >= expected_level:
                # 迟到的满足等级：仍按结果就绪超时失败，不转成功、不抛素材不足。
                self.trace.fail(
                    "upgrade", "upgrade",
                    f"{cell_tag} 升级结果就绪超时，补读已到 {disc.level} 级", image=image,
                )
                raise RunnerError(
                    f"{cell_tag} 升级结果就绪超时（期望 {expected_level}，补读到 {disc.level}），停止任务链"
                )
            if disc is None:
                refund_line = self._find_material_refund(image)
                if refund_line is not None:
                    # §6.2.1：实读仍无盘面且 finder 确认返还 → 走同一确认接续。
                    return self._resume_after_refund(
                        hwnd, cell_tag, None, refund_line=refund_line, before=before,
                        expected_level=expected_level, disc_slot=disc_slot,
                        stage_started=stage_started,
                    )
            else:
                last = disc
            if attempt + 1 < attempts:
                time.sleep(0.2)
        if last is None:
            self.trace.fail("upgrade", "upgrade", f"{cell_tag} 升级结果始终无盘面", image=image)
            raise RunnerError(f"{cell_tag} 升级结果无法确认（始终无盘面），停止任务链")
        reason = (
            f"升级素材不足：从 {before.level} 级升级后期望到 {expected_level} 级，"
            f"实际只有 {last.level} 级，停止任务链"
        )
        self.log.error("cell %s %s", cell_tag, reason)
        self.trace.fail("upgrade", "upgrade", reason, image=image)
        if stage_started is not None:
            self.stats.disc_stage(disc_slot, before.level, last.level, time.monotonic() - stage_started)
        self.stats.disc_end(disc_slot, OUTCOME_FAILED, last.level)
        # 素材耗尽是正常业务收尾：先把游戏退回仓库侧栏，回退失败也不盖掉真正原因。
        try:
            self._esc_back_to_grid(hwnd)
        except RunnerError as esc_exc:
            self.log.warning("素材不足收尾时回退仓库失败：%s", esc_exc)
        raise MaterialShortfall(before.level, expected_level, last.level)

    def _confirm_material_refund(self, hwnd: int, frame) -> bool:
        """§6.2.1：确认绑定/帧有效后，用给定帧点一次「确认」，记录确认输入时刻与 ID。"""
        self._checkpoint()
        if frame is None or frame.isNull():
            return False
        env = self._env
        if env is not None and (frame.width() != env.client_width or frame.height() != env.client_height):
            self.log.warning("材料返还确认帧尺寸 %dx%d 与绑定客户区 %dx%d 不符，拒绝按旧坐标确认",
                             frame.width(), frame.height(), env.client_width, env.client_height)
            return False
        self._refund_release = None
        self._refund_input_id = None
        return self._dismiss_material_refund(hwnd, frame, resume_result=True)

    def _resume_after_refund(
        self, hwnd: int, cell_tag: str, interrupted, *, refund_line=None,
        before: Disc, expected_level: int, disc_slot: int, stage_started: float | None,
    ) -> tuple[Disc, "QImage | None"]:
        """§6.2.1：确认一次材料返还后，从确认输入时刻重建第二次 upgrade_result（不重发升级）。

        每档最多一次确认尝试、两次结果等待；第二次再出现返还/超时或始终无盘面则有限失败。
        中断帧属于旧暂停代次时接续失效：确认输入前停止，不按旧截图坐标点击（§8 W4）。
        """
        if interrupted is not None:
            generation = self._checkpoint()
            interrupted_generation = getattr(interrupted, "pause_generation", None)
            if (
                generation is not None
                and interrupted_generation is not None
                and generation != interrupted_generation
            ):
                raise RunnerError(
                    f"{cell_tag} 材料返还接续失效（暂停代次 {interrupted_generation}→{generation}），"
                    "不使用旧帧确认，停止任务链"
                )
            frame = interrupted.last_sample.frame
            resume_from = getattr(interrupted, "wait_id", None) or self._last_wait_id
        else:
            generation = self._checkpoint()
            frame, _page, *_ = self._grab(allow_esc=False, need_page=False)
            resume_from = self._last_wait_id
            interrupted_generation = generation
        binding = (self._hwnd, self._pid, self._env)
        state = self._state
        position = None if state is None else (state.job_id, state.next_index)
        scope = (self._disc_event_id, self._upgrade_input_id)
        size = (frame.width(), frame.height()) if frame is not None else None

        def validate_refund() -> None:
            # 入口、OCR 后的 click 检查点及其 focus 检查点都必须仍属于中断帧。
            # 不重拍/重做全窗 OCR；暂停使旧帧失效，直接停止而非确认另一个弹窗。
            current = self._checkpoint()
            current_position = None if self._state is None else (self._state.job_id, self._state.next_index)
            invalid = (
                (interrupted_generation is not None and current != interrupted_generation)
                or binding != (self._hwnd, self._pid, self._env)
                or position != current_position
                or scope != (self._disc_event_id, self._upgrade_input_id)
                or (self._hwnd is not None and self._hwnd != hwnd)
            )
            if self._pid is not None:
                invalid = invalid or window_pid(hwnd) != self._pid or client_size(hwnd) != size
            if invalid:
                raise RunnerError(f"{cell_tag} 材料返还接续失效，不使用旧帧确认，停止任务链")

        validate_refund()
        with validated_inputs(validate_refund):
            if not self._confirm_material_refund(hwnd, frame):
                raise RunnerError(f"{cell_tag} 材料返还确认失败，停止任务链")
        if self._refund_release is None:
            raise RunnerError(f"{cell_tag} 未记录返还确认输入释放点，停止任务链")
        try:
            result = self._wait_ready(
                "upgrade_result", release=self._refund_release, before=before,
                expected_level=expected_level, trigger_input_id=self._refund_input_id,
                resume_from_wait_id=resume_from, action_id="refund_confirm",
            )
        except (ReadinessTimeout, ReadinessInterrupted) as exc:
            raise RunnerError(f"{cell_tag} 返还确认后升级结果仍未确认：{exc}，停止任务链")
        disc = result.disc
        if disc is None:
            raise RunnerError(f"{cell_tag} 返还确认后未读出真实盘面，停止任务链")
        if stage_started is not None:
            self.stats.disc_stage(disc_slot, before.level, disc.level, time.monotonic() - stage_started)
        return disc, result.frame

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
