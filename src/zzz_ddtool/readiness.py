"""就绪等待、控制暂停区间与本机等待档案（Spec §6.1 / §6.1.1 / §6.2.1 / §6.4 / §9.1）。

本模块只做三件事：

1. 通用等待器 :func:`wait_ready`：调度、双帧稳定完成判定、下限/超时（扣除暂停）、
   暂停代次失效与 ``material_refund`` 受控中断。只做调度与判定，不写性能事件、
   不碰游戏输入、不自己截图。
2. 本机等待档案 ``user/scan-waits.json`` 的加载/校验与原子写盘（§6.4）。
3. 标定结果计算：下限 ``floor_ms``、select 不同内容转换计数、return 三边取最大、
   报告证据摘要 ``report_sha256``（§9.1）。

依赖方向（Spec §2.2）：只依赖模型、图像类型与路径；不导入 runner / ocr / screen /
capture / trace / hotkeys / scanperf。环境身份以普通映射传入，控制器通过回调接入。
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zzz_ddtool.models import Disc
from zzz_ddtool.paths import user_dir

if TYPE_CHECKING:  # 运行期不导入 Qt；帧类型只用于标注
    from PySide6.QtGui import QImage

__all__ = [
    "WaitPolicy",
    "WaitSample",
    "WaitResult",
    "ReadinessTimeout",
    "ReadinessInterrupted",
    "wait_ready",
    "wait_profile_path",
    "validate_policy",
    "validate_probe_params",
    "load_wait_policies",
    "write_wait_profile",
    "ProfileWriteResult",
    "canonical_json_bytes",
    "compute_floor_ms",
    "summarize_select",
    "summarize_return_warehouse",
    "calibration_report_sha256",
    "SelectObservation",
    "SelectSummary",
    "ReturnObservation",
    "ReturnSummary",
    "KINDS",
    "REQUIRED_PARAMS",
    "DEFAULT_FLOOR_MS",
    "MATERIAL_REFUND",
    "PREDICATE_VERSION",
]

SCHEMA_VERSION = 1
PREDICATE_VERSION = 1
POLL_MS = 50
STABLE_FRAMES = 2
MATERIAL_REFUND = "material_refund"

#: §6.2 六个 kind；丢弃旧的 discard_label/lock_label。
KINDS: tuple[str, ...] = (
    "select_disc",
    "open_preview",
    "open_upgrade",
    "material_ready",
    "upgrade_result",
    "return_warehouse",
)

#: §6.2 默认（下限 ms, 超时 ms）；poll/stable 为固定值。
_DEFAULT_BOUNDS: dict[str, tuple[int, int]] = {
    "select_disc": (500, 3000),
    "open_preview": (900, 5000),
    "open_upgrade": (1500, 8000),
    "material_ready": (600, 3000),
    "upgrade_result": (1500, 8000),
    "return_warehouse": (600, 3000),
}

#: §6.2 各 kind 的必填输入（factory 与等待器共用；缺项在第一次 probe 前拒绝）。
REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "select_disc": ("cell",),
    "open_preview": ("before",),
    "open_upgrade": ("before",),
    "material_ready": ("before", "before_frame"),
    "upgrade_result": ("before", "expected_level"),
    "return_warehouse": ("before_frame",),
}

#: 未标定 kind 采用的保留下限（§9.1：return 三条边缺边时保默认）。
DEFAULT_FLOOR_MS: dict[str, int] = {k: v[0] for k, v in _DEFAULT_BOUNDS.items()}

ENVIRONMENT_FIELDS: tuple[str, ...] = (
    "machine_id",
    "client_width",
    "client_height",
    "window_dpi",
    "game_exe_path",
    "game_build",
)
_INT_ENV_FIELDS = ("client_width", "client_height", "window_dpi")

#: §9.1 return 三条允许边。
RETURN_EDGES: tuple[str, ...] = ("modal_preview", "modal_warehouse", "preview_warehouse")
RETURN_MIN_SAMPLES = 5
SELECT_MIN_SAMPLES = 20
SELECT_MIN_DIFFERENT = 10

#: §6.4 报告证据摘要取数对象（排除自引用/生成时间等）。
REPORT_DIGEST_KEYS: tuple[str, ...] = (
    "schema_version",
    "spec_sha256",
    "execution_identity",
    "environment",
    "predicate_version",
    "selection",
    "samples",
    "summary",
)

_EPS = 1e-9


# --------------------------------------------------------------------------
# 时钟与睡眠（供测试替换，不改全局 time）
# --------------------------------------------------------------------------
def _now() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# --------------------------------------------------------------------------
# 规范化与摘要（§6.4）
# --------------------------------------------------------------------------
def canonical_json_bytes(obj: Any) -> bytes:
    """§6.4 规范化：键排序、紧凑分隔符、不转义中文、拒绝 NaN/Infinity。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


# --------------------------------------------------------------------------
# 类型（§6.1）
# --------------------------------------------------------------------------
def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是 int，不接受 bool 或其他类型：{value!r}")
    return value


def _validate_policy_fields(min_wait_ms: Any, timeout_ms: Any, poll_ms: Any, stable_frames: Any) -> None:
    _require_int(min_wait_ms, "min_wait_ms")
    _require_int(timeout_ms, "timeout_ms")
    _require_int(poll_ms, "poll_ms")
    _require_int(stable_frames, "stable_frames")
    if min_wait_ms < 0:
        raise ValueError(f"min_wait_ms 不能为负：{min_wait_ms}")
    if timeout_ms <= min_wait_ms:
        raise ValueError(f"timeout_ms({timeout_ms}) 必须大于 min_wait_ms({min_wait_ms})")
    if poll_ms != POLL_MS:
        raise ValueError(f"poll_ms 固定为 {POLL_MS}，得到 {poll_ms}")
    if stable_frames != STABLE_FRAMES:
        raise ValueError(f"stable_frames 固定为 {STABLE_FRAMES}，得到 {stable_frames}")


@dataclass(frozen=True)
class WaitPolicy:
    min_wait_ms: int
    timeout_ms: int
    poll_ms: int
    stable_frames: int = STABLE_FRAMES

    def __post_init__(self) -> None:
        _validate_policy_fields(
            self.min_wait_ms, self.timeout_ms, self.poll_ms, self.stable_frames
        )


def validate_policy(policy: WaitPolicy) -> None:
    """统一策略校验（Spec §6.1）：供构造与档案加载共用。"""
    if not isinstance(policy, WaitPolicy):
        raise ValueError(f"策略必须是 WaitPolicy：{policy!r}")
    _validate_policy_fields(
        policy.min_wait_ms, policy.timeout_ms, policy.poll_ms, policy.stable_frames
    )


def default_policy(kind: str) -> WaitPolicy:
    if kind not in KINDS:
        raise ValueError(f"非法等待 kind：{kind!r}")
    min_wait, timeout = _DEFAULT_BOUNDS[kind]
    return WaitPolicy(min_wait, timeout, POLL_MS, STABLE_FRAMES)


def validate_probe_params(kind: str, provided: Mapping[str, Any]) -> None:
    """factory 与等待器共用的必填项校验（§6.1）：缺项/非法 kind 在第一次 probe 前拒绝。"""
    if kind not in KINDS:
        raise ValueError(f"非法等待 kind：{kind!r}")
    missing = [name for name in REQUIRED_PARAMS[kind] if provided.get(name) is None]
    if missing:
        raise ValueError(f"{kind} 缺少必填项：{missing}")


@dataclass(frozen=True)
class WaitSample:
    """一次 probe 的只读结果（§6.1）。"""

    frame: "QImage"
    captured_at: float
    ready: bool
    state_key: tuple
    evidence: dict[str, object]
    disc: Disc | None = None
    interrupt_reason: str | None = None

    def __post_init__(self) -> None:
        if self.interrupt_reason not in (None, MATERIAL_REFUND):
            raise ValueError(f"interrupt_reason 只接受 None 或 {MATERIAL_REFUND}：{self.interrupt_reason!r}")
        if self.interrupt_reason is not None:
            if self.ready:
                raise ValueError("中断样本必须 ready=False")
            if self.disc is not None:
                raise ValueError("中断样本必须 disc=None")
        if not isinstance(self.state_key, tuple):
            raise ValueError("state_key 必须是 tuple")


@dataclass(frozen=True)
class WaitResult:
    frame: "QImage"
    disc: Disc | None
    active_ms: float
    samples: int
    pause_generation: int | None


class ReadinessTimeout(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        last_sample: WaitSample | None,
        active_ms: float,
        samples: int,
        pause_generation: int | None,
    ) -> None:
        super().__init__(message)
        self.last_sample = last_sample
        self.active_ms = active_ms
        self.samples = samples
        self.pause_generation = pause_generation


class ReadinessInterrupted(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        last_sample: WaitSample,
        active_ms: float,
        samples: int,
        pause_generation: int | None,
    ) -> None:
        super().__init__(f"等待被受控中断：{reason}")
        self.reason = reason
        self.last_sample = last_sample
        self.active_ms = active_ms
        self.samples = samples
        self.pause_generation = pause_generation


# --------------------------------------------------------------------------
# 等待器（§6.1）
# --------------------------------------------------------------------------
def wait_ready(
    *,
    kind: str,
    policy: WaitPolicy,
    started_at: float,
    probe: Callable[[], WaitSample],
    checkpoint: Callable[[], int | None],
    pauses: Callable[[float, float], tuple[tuple[float, float], ...]],
) -> WaitResult:
    """按 §6.1 就绪等待：双帧稳定、下限、超时（扣暂停）、代次失效、受控中断。

    - 只做调度/判定/返回或抛错；不写性能事件。
    - ``probe``/``checkpoint`` 抛出的异常原样向上传播（§6.1.1）。
    - 暂停阻塞不计入 ``active_ms``/超时；代次改变丢弃旧帧与候选，下限从恢复时刻重算。
    - ``material_refund`` 需两个独立稳定候选，满足后抛 ``ReadinessInterrupted``。
    """
    if kind not in KINDS:
        raise ValueError(f"非法等待 kind：{kind!r}")
    validate_policy(policy)
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        raise ValueError("started_at 必须是数值（time.monotonic()）")
    for name, fn in (("probe", probe), ("checkpoint", checkpoint), ("pauses", pauses)):
        if not callable(fn):
            raise ValueError(f"{name} 必须是可调用对象")

    min_wait_s = policy.min_wait_ms / 1000.0
    timeout_s = policy.timeout_ms / 1000.0
    poll_s = policy.poll_ms / 1000.0
    need = policy.stable_frames

    def _paused_seconds(until: float) -> float:
        total = 0.0
        for start, end in pauses(started_at, until):
            total += float(end) - float(start)
        return total

    def _active_at(moment: float) -> float:
        return (moment - started_at) - _paused_seconds(moment)

    def _active_ms(moment: float) -> float:
        return max(0.0, _active_at(moment)) * 1000.0

    state: dict[str, Any] = {
        "gen": None,
        "gen_start_active": 0.0,
        "mode": None,
        "key": None,
        "times": [],
        "last": None,
        "count": 0,
    }

    def _on_checkpoint() -> None:
        gen = checkpoint()
        if gen is None:
            return
        if isinstance(gen, bool) or not isinstance(gen, int):
            raise ValueError(f"checkpoint 返回的代次必须是 int 或 None：{gen!r}")
        if state["gen"] is not None and gen != state["gen"]:
            # 暂停代次改变：丢弃旧帧、连续计数与返回候选，下限从恢复时刻重算。
            state["mode"] = None
            state["key"] = None
            state["times"] = []
            state["gen_start_active"] = _active_at(_now())
        state["gen"] = gen

    def _raise_timeout(moment: float) -> None:
        raise ReadinessTimeout(
            f"{kind} 等待超时（预算 {policy.timeout_ms} ms）",
            last_sample=state["last"],
            active_ms=_active_ms(moment),
            samples=state["count"],
            pause_generation=state["gen"],
        )

    def _finish(sample: WaitSample, moment: float):
        if sample.interrupt_reason is not None:
            if kind != "upgrade_result":
                raise ValueError("interrupt_reason 只允许用于 upgrade_result")
            raise ReadinessInterrupted(
                MATERIAL_REFUND,
                last_sample=sample,
                active_ms=_active_ms(moment),
                samples=state["count"],
                pause_generation=state["gen"],
            )
        return WaitResult(
            frame=sample.frame,
            disc=sample.disc,
            active_ms=_active_ms(moment),
            samples=state["count"],
            pause_generation=state["gen"],
        )

    while True:
        _on_checkpoint()
        now = _now()
        if _active_at(now) > timeout_s:
            _raise_timeout(now)

        sample = probe()  # 异常原样传播（§6.1.1）
        state["count"] += 1
        state["last"] = sample

        end = _now()
        _on_checkpoint()
        if _active_at(end) > timeout_s:
            _raise_timeout(end)

        if not sample.ready and sample.interrupt_reason is None:
            state["mode"] = None
            state["key"] = None
            state["times"] = []
        else:
            if sample.interrupt_reason is not None:
                if kind != "upgrade_result":
                    raise ValueError("interrupt_reason 只允许用于 upgrade_result")
                mode, key = "interrupt", (sample.interrupt_reason, sample.state_key)
            else:
                mode, key = "ready", (sample.state_key,)
            if _active_at(sample.captured_at) - state["gen_start_active"] >= min_wait_s - _EPS:
                if state["mode"] != mode or state["key"] != key:
                    state["mode"], state["key"] = mode, key
                    state["times"] = [sample.captured_at]
                elif sample.captured_at - state["times"][-1] >= poll_s - _EPS:
                    state["times"].append(sample.captured_at)
                    if len(state["times"]) >= need:
                        return _finish(sample, end)

        # 调度下一帧：按 probe 结束时刻 + poll 间隔；probe 已耗时足够则立即继续。
        target = end + poll_s
        while True:
            moment = _now()
            if moment >= target:
                break
            _on_checkpoint()
            if _active_at(moment) > timeout_s:
                _raise_timeout(moment)
            gap = min(poll_s, target - moment)
            if gap > 0:
                _sleep(gap)


# --------------------------------------------------------------------------
# 本机等待档案（§6.4）
# --------------------------------------------------------------------------
def wait_profile_path() -> Path:
    """唯一返回 ``user_dir()/scan-waits.json``（§6.4）。"""
    return user_dir() / "scan-waits.json"


def _env_value_valid(field: str, value: Any) -> bool:
    if field in _INT_ENV_FIELDS:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    return isinstance(value, str) and value != ""


def environment_persistable(environment: Mapping[str, Any] | None) -> bool:
    """六个身份字段齐全且类型合法才允许写持久档案（复用 Environment.persistable 语义）。"""
    if not isinstance(environment, Mapping):
        return False
    return all(_env_value_valid(field, environment.get(field)) for field in ENVIRONMENT_FIELDS)


def _environment_equals(stored: Any, current: Mapping[str, Any] | None) -> bool:
    if not isinstance(stored, Mapping) or not environment_persistable(current):
        return False
    for field in ENVIRONMENT_FIELDS:
        value = stored.get(field)
        if not _env_value_valid(field, value):
            return False
        if value != current.get(field):
            return False
    return True


def _policy_from_json(body: Any) -> WaitPolicy:
    if not isinstance(body, Mapping):
        raise ValueError(f"策略项必须是对象：{body!r}")
    allowed = {"min_wait_ms", "timeout_ms", "poll_ms", "stable_frames"}
    if set(body.keys()) - allowed:
        raise ValueError(f"策略含未知键：{sorted(set(body) - allowed)}")
    return WaitPolicy(
        min_wait_ms=body.get("min_wait_ms"),
        timeout_ms=body.get("timeout_ms"),
        poll_ms=body.get("poll_ms", POLL_MS),
        stable_frames=body.get("stable_frames", STABLE_FRAMES),
    )


def _policy_to_json(policy: WaitPolicy) -> dict[str, Any]:
    return {
        "min_wait_ms": policy.min_wait_ms,
        "timeout_ms": policy.timeout_ms,
        "poll_ms": policy.poll_ms,
        "stable_frames": policy.stable_frames,
    }


def _measurement_valid(body: Any) -> bool:
    if not isinstance(body, Mapping):
        return False
    count = body.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return False
    max_stable = body.get("max_stable_ms")
    if isinstance(max_stable, bool) or not isinstance(max_stable, (int, float)):
        return False
    if not math.isfinite(float(max_stable)):
        return False
    floor = body.get("floor_ms")
    if isinstance(floor, bool) or not isinstance(floor, int) or floor < 0:
        return False
    sha = body.get("report_sha256")
    if sha is not None and not isinstance(sha, str):
        return False
    return True


def load_wait_policies(
    environment: Mapping[str, Any] | None, *, path: Path | None = None
) -> dict[str, WaitPolicy]:
    """读取并校验本机等待档案；任一不合法 → 整份失效、全用默认（§6.4）。"""
    target = Path(path) if path is not None else wait_profile_path()
    defaults = {kind: default_policy(kind) for kind in KINDS}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    if not isinstance(raw, Mapping):
        return defaults
    if raw.get("schema_version") != SCHEMA_VERSION:
        return defaults
    if raw.get("predicate_version") != PREDICATE_VERSION:
        return defaults
    if not _environment_equals(raw.get("environment"), environment):
        return defaults
    policies = raw.get("policies")
    measurements = raw.get("measurements")
    if not isinstance(policies, Mapping) or not isinstance(measurements, Mapping):
        return defaults
    if set(policies.keys()) != set(measurements.keys()):
        return defaults
    if any(key not in KINDS for key in policies):
        return defaults
    effective = dict(defaults)
    for key, body in policies.items():
        try:
            effective[key] = _policy_from_json(body)
        except (ValueError, TypeError):
            return defaults
    for body in measurements.values():
        if not _measurement_valid(body):
            return defaults
    return effective


@dataclass(frozen=True)
class ProfileWriteResult:
    status: str  # ok / not_persistable / write_failed
    profile_sha256: str | None
    error: dict[str, str] | None


def write_wait_profile(
    environment: Mapping[str, Any] | None,
    policies: Mapping[str, WaitPolicy],
    measurements: Mapping[str, Any],
    *,
    path: Path | None = None,
) -> ProfileWriteResult:
    """同目录临时文件 + 原子替换写档案；失败保留原档案、清理临时文件、报 write_failed（§6.4）。"""
    target = Path(path) if path is not None else wait_profile_path()
    if not environment_persistable(environment):
        return ProfileWriteResult("not_persistable", None, None)
    assert environment is not None  # noqa: S101 - persistable 已保证
    document = {
        "schema_version": SCHEMA_VERSION,
        "predicate_version": PREDICATE_VERSION,
        "environment": {field: environment.get(field) for field in ENVIRONMENT_FIELDS},
        "policies": {kind: _policy_to_json(policy) for kind, policy in policies.items()},
        "measurements": {
            kind: {
                "count": body.get("count"),
                "max_stable_ms": body.get("max_stable_ms"),
                "floor_ms": body.get("floor_ms"),
                "report_sha256": body.get("report_sha256"),
            }
            for kind, body in measurements.items()
        },
    }
    data = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return ProfileWriteResult(
            "write_failed", None, {"type": type(exc).__name__, "message": str(exc)}
        )
    return ProfileWriteResult("ok", _sha256_bytes(data), None)


# --------------------------------------------------------------------------
# 标定结果计算（§9.1）
# --------------------------------------------------------------------------
def compute_floor_ms(max_stable_ms: float, *, poll_ms: int = POLL_MS) -> int:
    """§9.1 下限公式：``ceil((max(t_stable-t0) + max(20, 2*poll_ms)) / 10) * 10``。"""
    if isinstance(max_stable_ms, bool) or not isinstance(max_stable_ms, (int, float)):
        raise ValueError(f"max_stable_ms 必须是数值：{max_stable_ms!r}")
    if not math.isfinite(float(max_stable_ms)):
        raise ValueError("max_stable_ms 必须是有限数值（拒绝 NaN/Infinity）")
    slack = max(20, 2 * _require_int(poll_ms, "poll_ms"))
    return int(math.ceil((float(max_stable_ms) + slack) / 10.0) * 10)


@dataclass(frozen=True)
class SelectObservation:
    """一次有效的 select 观察：同一格位证据 + 完整最终盘面 + 两稳定帧。"""

    selection_id: str
    core_key: tuple  # (level, tuple(sorted((name, rolls))))
    valid: bool = True


@dataclass(frozen=True)
class SelectSummary:
    count: int
    different_transitions: int
    sufficient: bool
    reason_codes: tuple[str, ...]


def summarize_select(observations: list[SelectObservation] | tuple[SelectObservation, ...]) -> SelectSummary:
    """§9.1 select 统计：count 为有效样本数，different 为相邻不同格位/不同内容转换数。

    无效样本不计 count/转换，也不跨无效样本搭桥；首件无前件不计；
    相邻同 ID（即使内容变化）与不同 ID 同文（core_key 相等）均计 0。
    """
    count = 0
    different = 0
    previous: SelectObservation | None = None
    for obs in observations:
        if not obs.valid:
            previous = None
            continue
        count += 1
        if (
            previous is not None
            and obs.selection_id != previous.selection_id
            and obs.core_key != previous.core_key
        ):
            different += 1
        previous = obs
    codes: list[str] = []
    if count < SELECT_MIN_SAMPLES:
        codes.append("sample_count")
    if different < SELECT_MIN_DIFFERENT:
        codes.append("content_diversity")
    return SelectSummary(count, different, not codes, tuple(codes))


@dataclass(frozen=True)
class ReturnObservation:
    """一次 return_warehouse 观察；``edge`` 为三条允许边之一，同层/unknown 为 None。"""

    edge: str | None
    t_stable_ms: float | None = None
    timed_out: bool = False
    false_ready: bool = False


@dataclass(frozen=True)
class ReturnSummary:
    status: str  # calibrated / insufficient / failed
    floor_ms: int
    edge_floors: dict[str, int | None]
    reason_codes: tuple[str, ...]


def summarize_return_warehouse(
    observations: list[ReturnObservation] | tuple[ReturnObservation, ...],
    *,
    default_floor_ms: int = DEFAULT_FLOOR_MS["return_warehouse"],
) -> ReturnSummary:
    """§9.1 return 逐边取最大：每条允许边需 >=5 有效样本；缺边/不足保默认；失败整体 failed。"""
    if any(obs.false_ready for obs in observations):
        return ReturnSummary("failed", default_floor_ms, {}, ("false_ready",))
    if any(obs.timed_out and obs.edge is None for obs in observations):
        return ReturnSummary("failed", default_floor_ms, {}, ("timeout",))

    by_edge: dict[str, list[float]] = {edge: [] for edge in RETURN_EDGES}
    for obs in observations:
        if obs.edge in RETURN_EDGES and not obs.timed_out and obs.t_stable_ms is not None:
            by_edge[obs.edge].append(float(obs.t_stable_ms))

    edge_floors: dict[str, int | None] = {}
    for edge in RETURN_EDGES:
        if len(by_edge[edge]) >= RETURN_MIN_SAMPLES:
            edge_floors[edge] = compute_floor_ms(max(by_edge[edge]))
        else:
            edge_floors[edge] = None
    if any(value is None for value in edge_floors.values()):
        return ReturnSummary("insufficient", default_floor_ms, edge_floors, ("edge_coverage",))
    floor = max(value for value in edge_floors.values() if value is not None)
    return ReturnSummary("calibrated", floor, edge_floors, ())


def calibration_report_sha256(report: Mapping[str, Any]) -> str:
    """§6.4 证据摘要：取固定键集合规范化后 SHA256，排除自引用/生成时间。"""
    subset = {key: report[key] for key in REPORT_DIGEST_KEYS}
    return _sha256_bytes(canonical_json_bytes(subset))
