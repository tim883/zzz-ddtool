"""扫描提速：性能事件记录与区间关联（Spec §7.1、§7.1.1）。

职责单一：把「一段耗时区间」记成一行 JSONL，并能把子事件挂到父事件上。
- 只依赖标准库和 ``paths``（§2.2）：不反向导入 capture/trace/ocr/discocr/runner。
  调用方通过模块级会话与上下文管理器上报，scanperf 不认识它们。

一次运行（一轮 ``Runner.run`` 或一次离线基准）一个会话：
    - ``run_id = f"{trace_tag}-{uuid4().hex}"``，一个 run_id 只对应一个 trace_tag；
    - 事件逐行写入 ``log_dir()/"perf"/"<run_id>.jsonl"``；
    - 没有活动会话时，所有上报都是 no-op：不创建残缺 run 记录，也不改调用方返回值。

事件 ID 在区间**开始前**预分配（§7.1.1），区间结束才写记录，所以子事件可以引用
尚未写出的父事件。包含关系用 ``parent_id`` 表达；汇总不得把父子区间相加。
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from zzz_ddtool.paths import log_dir

#: JSONL 每行的 schema 版本。
SCHEMA_VERSION = 1

#: ``component`` 只允许这些值（§7.1）。
COMPONENTS: tuple[str, ...] = (
    "capture",
    "crop",
    "template_match",
    "recognize_batch",
    "legacy_parse",
    "parse_total",
    "probe",
    "wait_total",
    "png_save",
    "disc_total",
    "run_total",
)
COMPONENT_SET = frozenset(COMPONENTS)

#: ``details.outcome`` 允许值（§7.1）。
OUTCOMES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "stopped",
    "timed_out",
    "returned_none",
    "interrupted",
)
OUTCOME_SET = frozenset(OUTCOMES)

#: 各 component 的最小必需 details 键（§7.1、§7.1.1）。可扩展，不能改名/改类型。
#: crop 的「实际像素 rects」「frame_size」在 Spec 里未固定键名，这里取 rects/frame_size
#: 并在实施记录登记（§10 自由裁量）。
REQUIRED_DETAIL_KEYS: dict[str, tuple[str, ...]] = {
    "crop": ("layout", "profile_id", "roi_ids", "rects", "frame_size"),
    "template_match": ("layout", "profile_id", "roi_ids", "template_hits", "matches"),
    "recognize_batch": ("roi_ids", "batch_size", "profile_id"),
    "legacy_parse": ("parser", "reject_reason", "profile_id"),
    "parse_total": ("profile_id", "reject_reason", "template_hits", "ocr_lines"),
    "probe": (
        "purpose",
        "wait_id",
        "sample_id",
        "sample_index",
        "kind",
        "captured_at",
        "probe_ended_at",
        "ready",
        "state_key",
        "pause_generation",
        "interrupt_reason",
        "evidence",
    ),
    "wait_total": (
        "kind",
        "started_at",
        "ended_at",
        "active_ms",
        "pause_intervals",
        "samples",
        "t_bad",
        "t_ready",
        "t_stable",
        "outcome",
    ),
    "png_save": ("source", "path", "saved"),
    "capture": ("method", "hwnd"),
    "disc_total": ("input_sent", "outcome"),
    "run_total": ("runner_outcome", "termination_kind", "end_reason"),
}


class _Inherit:
    """哨兵：该字段没显式给，按「外层区间 → 会话作用域 → None」继承。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<inherit>"


_INHERIT = _Inherit()


def perf_dir() -> Path:
    """性能记录根目录：``log/perf``（由 ``paths.log_dir()`` 解析）。"""
    return log_dir() / "perf"


def make_run_id(trace_tag: str) -> str:
    return f"{trace_tag}-{uuid.uuid4().hex}"


def _as_int(value: Any) -> int:
    """非负 int 计数；bool / 非法值一律当 0（不伪造）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def _outcome_for_exception(exc: BaseException) -> str:
    """异常 → outcome。RunnerStopped 记 stopped，其余 failed（§7.1）。

    scanperf 不导入 hotkeys（§2.2），按类名判断，避免反向依赖。
    """
    return "stopped" if type(exc).__name__ == "RunnerStopped" else "failed"


@dataclass
class PerfEvent:
    """一个待写的性能事件（顶层字段固定，见 §7.1）。"""

    component: str
    duration_ms: float
    details: dict[str, Any] = field(default_factory=dict)
    event_id: str | None = None
    parent_id: str | None = None
    job_index: int | None = None
    disc_index: int | None = None
    stage_level: int | None = None

    def __post_init__(self) -> None:
        if self.component not in COMPONENT_SET:
            raise ValueError(f"未知 component：{self.component!r}")
        if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, (int, float)):
            raise TypeError("duration_ms 必须是数字")
        if not math.isfinite(self.duration_ms) or self.duration_ms < 0:
            raise ValueError(f"duration_ms 非法：{self.duration_ms!r}")
        if not isinstance(self.details, dict):
            raise TypeError("details 必须是对象")
        for name, value in (
            ("job_index", self.job_index),
            ("disc_index", self.disc_index),
            ("stage_level", self.stage_level),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数或 None：{value!r}")

    def to_record(self, *, run_id: str, event_id: str, trace_tag: str) -> dict[str, Any]:
        """按 §7.1 固定顶层字段顺序产出可写对象；自动补 ``details.trace_tag``。"""
        details = dict(self.details)
        details.setdefault("trace_tag", trace_tag)
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "event_id": event_id,
            "parent_id": self.parent_id,
            "job_index": self.job_index,
            "disc_index": self.disc_index,
            "stage_level": self.stage_level,
            "component": self.component,
            "duration_ms": float(self.duration_ms),
            "details": details,
        }


class PerfSpan:
    """一次计时区间的句柄：可改 details、累加子事件计数，退出时自动落记录。

    没有活动会话时 ``event_id`` 为 None，``set()`` 只改本地 dict、不写盘，
    调用点无需分叉（保持公开接口与返回值不变）。
    """

    __slots__ = (
        "event_id",
        "component",
        "details",
        "parent_id",
        "job_index",
        "disc_index",
        "stage_level",
        "_acc",
        "_started",
    )

    def __init__(
        self,
        event_id: str | None,
        component: str,
        details: dict[str, Any],
        *,
        parent_id: str | None,
        job_index: int | None,
        disc_index: int | None,
        stage_level: int | None,
        started: float,
    ) -> None:
        self.event_id = event_id
        self.component = component
        self.details = details
        self.parent_id = parent_id
        self.job_index = job_index
        self.disc_index = disc_index
        self.stage_level = stage_level
        self._acc: dict[str, int] = {}
        self._started = started

    def set(self, **kwargs: Any) -> "PerfSpan":
        self.details.update(kwargs)
        return self

    def accumulate(self, key: str, count: Any) -> None:
        n = _as_int(count)
        if n:
            self._acc[key] = self._acc.get(key, 0) + n

    def merge_accumulated(self) -> None:
        for key, value in self._acc.items():
            self.details[key] = value


class PerfSession:
    """一轮运行的记录会话：持有 run_id、文件句柄与进行中的区间栈。"""

    def __init__(
        self,
        run_id: str,
        trace_tag: str,
        path: Path,
        *,
        capture_dir: str | None = None,
        stats_path: str | None = None,
        clock=time.monotonic,
    ) -> None:
        self.run_id = run_id
        self.trace_tag = trace_tag
        self.path = Path(path)
        self.capture_dir = capture_dir
        self.stats_path = stats_path
        self._clock = clock
        self._seq = 0
        self._lock = threading.RLock()
        self._open: dict[str, PerfSpan] = {}
        self._parent_stack: list[str] = []
        self._span_stack: list[PerfSpan] = []
        self._handle = None
        self._closed = False
        self.write_error: str | None = None
        #: 会话作用域：外层没有区间时，新事件从这三项取默认（step 6 由 Runner 设置）。
        self.scope_job_index: int | None = None
        self.scope_disc_index: int | None = None
        self.scope_stage_level: int | None = None

    # ---------------------------------------------------------------- 生命周期
    def open(self) -> None:
        """预创建 JSONL 文件；失败只记 ``write_error``，不抛给调用方。"""
        self._ensure_handle()

    def _ensure_handle(self) -> bool:
        if self._handle is not None:
            return True
        if self.write_error is not None or self._closed:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("w", encoding="utf-8")
        except OSError as exc:
            self.write_error = f"{type(exc).__name__}: {exc}"
            return False
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handle, self._handle = self._handle, None
            if handle is not None:
                try:
                    handle.flush()
                except Exception as exc:
                    self.write_error = f"{type(exc).__name__}: {exc}"
                finally:
                    try:
                        handle.close()
                    except Exception:
                        pass

    # ---------------------------------------------------------------- 事件 ID
    def new_event_id(self) -> str:
        """预分配本轮唯一事件 ID（区间开始前调用，§7.1.1）。"""
        with self._lock:
            self._seq += 1
            return f"e{self._seq:08d}"

    # ---------------------------------------------------------------- 写记录
    def record(self, event: PerfEvent) -> None:
        if self._closed:
            return
        with self._lock:
            event_id = event.event_id or self.new_event_id()
            record = event.to_record(run_id=self.run_id, event_id=event_id, trace_tag=self.trace_tag)
            parent = self._open.get(event.parent_id) if event.parent_id else None
            if parent is not None:
                # 子事件计数回流到父区间（§7.1.1）：template_match 的接受行数 → template_hits；
                # recognize_batch 的批大小 → ocr_lines。这样 parse_total 不必自己再算一遍。
                if event.component == "template_match":
                    parent.accumulate("template_hits", event.details.get("template_hits"))
                elif event.component == "recognize_batch":
                    parent.accumulate("ocr_lines", event.details.get("batch_size"))
            self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        if not self._ensure_handle():
            return
        try:
            line = json.dumps(record, ensure_ascii=False, allow_nan=False)
            self._handle.write(line + "\n")
            self._handle.flush()
        except Exception as exc:  # 序列化或写盘失败都不改调用方决策（§7.1）
            self.write_error = f"{type(exc).__name__}: {exc}"

    # ---------------------------------------------------------------- 区间
    def begin_span(
        self,
        component: str,
        details: dict[str, Any],
        *,
        parent_id: str | None,
        job_index: Any,
        disc_index: Any,
        stage_level: Any,
    ) -> PerfSpan:
        with self._lock:
            outer = self._span_stack[-1] if self._span_stack else None
            if parent_id is None and self._parent_stack:
                parent_id = self._parent_stack[-1]
            event_id = self.new_event_id()
            resolved_job = _resolve(job_index, outer, "job_index", self.scope_job_index)
            resolved_disc = _resolve(disc_index, outer, "disc_index", self.scope_disc_index)
            resolved_stage = _resolve(stage_level, outer, "stage_level", self.scope_stage_level)
            span = PerfSpan(
                event_id,
                component,
                details,
                parent_id=parent_id,
                job_index=resolved_job,
                disc_index=resolved_disc,
                stage_level=resolved_stage,
                started=self._clock(),
            )
            self._open[event_id] = span
            self._parent_stack.append(event_id)
            self._span_stack.append(span)
            return span

    def end_span(self, span: PerfSpan, *, failed: bool, exc: BaseException | None = None) -> None:
        if span.event_id is None:
            return
        elapsed_ms = (self._clock() - span._started) * 1000.0
        if elapsed_ms < 0:
            elapsed_ms = 0.0
        with self._lock:
            if self._parent_stack and self._parent_stack[-1] == span.event_id:
                self._parent_stack.pop()
            elif span.event_id in self._parent_stack:
                self._parent_stack.remove(span.event_id)
            if self._span_stack and self._span_stack[-1] is span:
                self._span_stack.pop()
            elif span in self._span_stack:
                self._span_stack.remove(span)
            self._open.pop(span.event_id, None)
            if failed:
                span.details["outcome"] = _outcome_for_exception(exc) if exc is not None else "failed"
                if exc is not None:
                    span.details.setdefault(
                        "exception", {"type": type(exc).__name__, "message": str(exc)}
                    )
            span.merge_accumulated()
            self.record(
                PerfEvent(
                    component=span.component,
                    duration_ms=elapsed_ms,
                    details=span.details,
                    event_id=span.event_id,
                    parent_id=span.parent_id,
                    job_index=span.job_index,
                    disc_index=span.disc_index,
                    stage_level=span.stage_level,
                )
            )


def _resolve(explicit: Any, outer: PerfSpan | None, attr: str, scope: int | None) -> int | None:
    """字段取数：显式 → 外层区间 → 会话作用域 → None。"""
    if explicit is not _INHERIT:
        return explicit
    if outer is not None:
        value = getattr(outer, attr)
        if value is not None:
            return value
    return scope


# ---------------------------------------------------------------------------
# 模块级会话
# ---------------------------------------------------------------------------

_SESSION: PerfSession | None = None
_SESSION_LOCK = threading.RLock()


def current_session() -> PerfSession | None:
    return _SESSION


def begin_session(
    trace_tag: str,
    *,
    run_id: str | None = None,
    path: Path | str | None = None,
    capture_dir: str | None = None,
    stats_path: str | None = None,
    clock=time.monotonic,
) -> PerfSession:
    """开启一轮记录；已有会话先关闭（不抛错）。"""
    global _SESSION
    rid = run_id or make_run_id(trace_tag)
    target = Path(path) if path is not None else perf_dir() / f"{rid}.jsonl"
    session = PerfSession(
        rid,
        trace_tag,
        target,
        capture_dir=capture_dir,
        stats_path=stats_path,
        clock=clock,
    )
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION.close()
        _SESSION = session
    session.open()
    return session


def end_session() -> None:
    """刷新并关闭当前会话；没有会话时为空操作。"""
    global _SESSION
    with _SESSION_LOCK:
        session, _SESSION = _SESSION, None
    if session is not None:
        session.close()


def set_scope(
    *,
    job_index: int | None | _Inherit = _INHERIT,
    disc_index: int | None | _Inherit = _INHERIT,
    stage_level: int | None | _Inherit = _INHERIT,
) -> None:
    """设置会话级默认作用域（外层无区间时生效）；未传的项保持不变。"""
    session = _SESSION
    if session is None:
        return
    if job_index is not _INHERIT:
        session.scope_job_index = job_index
    if disc_index is not _INHERIT:
        session.scope_disc_index = disc_index
    if stage_level is not _INHERIT:
        session.scope_stage_level = stage_level


def record_event(event: PerfEvent) -> None:
    """写入一个事件；没有活动会话则 no-op（不凭空创建残缺 run 记录，§7.1）。"""
    session = _SESSION
    if session is None:
        return
    session.record(event)


def new_event_id() -> str | None:
    session = _SESSION
    return session.new_event_id() if session is not None else None


@contextmanager
def timed(
    component: str,
    *,
    details: dict[str, Any] | None = None,
    parent_id: str | None = None,
    job_index: Any = _INHERIT,
    disc_index: Any = _INHERIT,
    stage_level: Any = _INHERIT,
    outcome: str | None = "succeeded",
) -> Iterator[PerfSpan]:
    """计时区间上下文：进入预分配 ID，退出落记录（异常走 finally）。

    无活动会话时返回一个 ``event_id=None`` 的游离句柄，调用点行为不变。
    """
    if component not in COMPONENT_SET:
        raise ValueError(f"未知 component：{component!r}")
    base: dict[str, Any] = dict(details) if details else {}
    if outcome is not None:
        base.setdefault("outcome", outcome)
    session = _SESSION
    if session is None:
        span = PerfSpan(
            None,
            component,
            base,
            parent_id=None,
            job_index=None,
            disc_index=None,
            stage_level=None,
            started=0.0,
        )
        try:
            yield span
        except BaseException:
            raise
        return

    span = session.begin_span(
        component,
        base,
        parent_id=parent_id,
        job_index=job_index,
        disc_index=disc_index,
        stage_level=stage_level,
    )
    try:
        yield span
    except BaseException as exc:
        session.end_span(span, failed=True, exc=exc)
        raise
    else:
        session.end_span(span, failed=False)


# ---------------------------------------------------------------------------
# 读取与摘要
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PerfReadResult:
    """读 JSONL 的结果。``evidence_complete=False`` 表示证据不完整，验收不通过。"""

    events: tuple[dict[str, Any], ...]
    evidence_complete: bool
    discarded_bytes: int = 0
    error: str | None = None


def read_perf_file(path: Path | str) -> PerfReadResult:
    """读一份性能 JSONL。

    - 末尾没有换行的截断行可丢弃，但标 ``evidence_complete=False`` 并记丢弃字节数；
    - 中间出现损坏行是证据错误：标不完整并记 ``error``，不跳过后宣称通过（§7.1）。
    """
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        return PerfReadResult((), False, 0, f"{type(exc).__name__}: {exc}")

    events: list[dict[str, Any]] = []
    discarded = 0
    error: str | None = None
    if raw and not raw.endswith(b"\n"):
        cut = raw.rfind(b"\n")
        discarded = len(raw) - (cut + 1)
        raw = raw[: cut + 1] if cut >= 0 else b""

    for number, line in enumerate(raw.split(b"\n"), start=1):
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            error = f"第 {number} 行损坏：{type(exc).__name__}: {exc}"
            break
        if not isinstance(obj, dict) or obj.get("component") not in COMPONENT_SET:
            error = f"第 {number} 行结构非法"
            break
        events.append(obj)

    complete = error is None and discarded == 0
    return PerfReadResult(tuple(events), complete, discarded, error)


def total_duration_ms(events, component: str) -> float:
    """只汇总指定 component 的 duration_ms（不把父子区间相加，§7.1）。"""
    total = 0.0
    for event in events:
        if event.get("component") != component:
            continue
        value = event.get("duration_ms")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            total += float(value)
    return total


def component_totals(events) -> dict[str, float]:
    """按 component 汇总各自耗时；父区间与其子区间各自成项，不相加。"""
    totals: dict[str, float] = {}
    for event in events:
        name = event.get("component")
        value = event.get("duration_ms")
        if name not in COMPONENT_SET:
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            totals[name] = totals.get(name, 0.0) + float(value)
    return totals


def missing_detail_keys(component: str, details: dict[str, Any]) -> list[str]:
    """返回该 component 相对 §7.1/§7.1.1 缺失的最小必需键（供测试核对）。"""
    required = REQUIRED_DETAIL_KEYS.get(component, ())
    return [key for key in required if key not in details]


__all__ = [
    "SCHEMA_VERSION",
    "COMPONENTS",
    "COMPONENT_SET",
    "OUTCOMES",
    "OUTCOME_SET",
    "REQUIRED_DETAIL_KEYS",
    "PerfEvent",
    "PerfSession",
    "PerfSpan",
    "PerfReadResult",
    "perf_dir",
    "make_run_id",
    "current_session",
    "begin_session",
    "end_session",
    "set_scope",
    "record_event",
    "new_event_id",
    "timed",
    "read_perf_file",
    "total_duration_ms",
    "component_totals",
    "missing_detail_keys",
]
