"""释放后等待：真实释放时刻、按动作下限与剩余预算等待（Spec §2.2、§3、§4、§5）。

本模块不发输入、不抓图、不OCR；失败原样抛出。档案读写与装载校验都在这里，
装载规则见 Spec §5（整档失效与逐动作拒收两种粒度分开）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator, Mapping, NamedTuple

from zzz_ddtool import control
from zzz_ddtool.paths import user_dir

if TYPE_CHECKING:  # 只用于类型注解；navigation → wininput → 本模块，运行期不反向导入
    from zzz_ddtool.navigation import Environment

#: Spec §4.2 动作全集（17 项）。
_PANEL_ACTIONS = frozenset({
    "open_panel",
    "reset",
    "slot",
    "open_sets",
    "set_option",
    "set_scroll",
    "confirm_sets",
    "mainstat_scroll",
    "open_mainstats",
    "mainstat_option",
    "collapse_mainstats",
    "substat_scroll",
    "open_substats",
    "substat_option",
    "substat_count",
    "close_panel",
    "close_panel_reset",
})

#: Spec §4.3 动作全集（12 项）。
_EXECUTOR_ACTIONS = frozenset({
    "select_disc",
    "open_preview",
    "open_upgrade",
    "material_ready",
    "upgrade_result",
    "refund_confirm",
    "refund_dismiss",
    "return_warehouse",
    "label_save",
    "label_discard",
    "clear_discard",
    "label_unlock_before_discard",
})

#: 全部合法动作ID（§4.2 + §4.3）。
ACTION_IDS = _PANEL_ACTIONS | _EXECUTOR_ACTIONS

#: `return_warehouse` 的三条允许边（Spec v9 §12）。与 ``readiness.RETURN_EDGES`` 逐字相同：
#: 本模块不反向导入 readiness，避免把屏幕判读依赖带进纯等待层。
RETURN_EDGE_IDS = ("modal_preview", "modal_warehouse", "preview_warehouse")

#: 逐边档案挂在哪个动作下；键名进档案，不能随意改。
RETURN_EDGE_ACTION = "return_warehouse"

#: Spec §4.3 表列 timeout（ms）；其余 §4.2 动作按 §4.2 段首的 3000 ms 活动窗口校验。
_TIMEOUT_MS: dict[str, int] = {
    "select_disc": 3180,
    "open_preview": 5180,
    "open_upgrade": 8180,
    "material_ready": 3180,
    "upgrade_result": 8180,
    "refund_confirm": 8180,
    "return_warehouse": 3580,
}
_PANEL_FLOOR_BOUND_MS = 3000

#: 等待分片的浮点残差容差（秒）：remaining 小于它即视为已满足，避免空转。
_WAIT_EPS_S = 1e-6

_SCHEMA_VERSION = 2
#: 动作级实测项的最少样本数；逐边项用 :data:`RETURN_EDGE_MIN_COUNT`。
_POLICY_MIN_COUNT = 10
#: Spec v9 §12：逐边实测项的最少样本数。
RETURN_EDGE_MIN_COUNT = 5
_TIME_ORIGIN = "native_release"
_PREDICATE_VERSION = 2
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_ENVIRONMENT_FIELDS = (
    "machine_id",
    "client_width",
    "client_height",
    "window_dpi",
    "game_exe_path",
    "game_build",
)


@dataclass(frozen=True)
class InputRelease:
    """一次原生输入的释放回执；api 为 §2.1 五个函数名之一。"""

    api: str
    released_at_ns: int


@dataclass(frozen=True)
class ActionWaitPolicy:
    """某动作当前生效的释放后下限；source 只取 default/measured/measurement。"""

    floor_ms: int
    source: str


class _WaitContext(NamedTuple):
    policies: Mapping[str, ActionWaitPolicy]
    measure_action: str | None
    return_edges: Mapping[str, ActionWaitPolicy]


_active: ContextVar[_WaitContext | None] = ContextVar("ddtool_release_waits", default=None)


@dataclass(frozen=True)
class WaitReceipt:
    """一次释放后等待的明细（§7.1 release_wait 的采数载体；内部使用）。"""

    released_at_ns: int
    action_id: str | None
    floor_ms: int
    policy_source: str
    requested_sleep_ms: float
    active_elapsed_ms: float


_receipts: ContextVar[list[WaitReceipt] | None] = ContextVar("ddtool_wait_receipts", default=None)


@contextmanager
def collect_wait_receipts() -> Iterator[list[WaitReceipt]]:
    """启用释放后等待明细收集（§7.1）：等待照常执行，明细登记进本次收集器。

    未启用时等待行为不变、明细直接丢弃；生成 final 报告的证据不依赖未收集的历史等待。
    """
    bucket: list[WaitReceipt] = []
    token = _receipts.set(bucket)
    try:
        yield bucket
    finally:
        _receipts.reset(token)


def record_wait_receipt(receipt: WaitReceipt) -> None:
    """向当前收集器登记一条等待明细；没有收集器时丢弃（§7.1）。"""
    bucket = _receipts.get()
    if bucket is not None:
        bucket.append(receipt)



def profile_path() -> Path:
    """实测档案路径（用户目录，不随版本目录搬迁）。"""
    return user_dir() / "post-release-waits.json"


@contextmanager
def release_waits(environment: "Environment", *, measure_action: str | None = None) -> Iterator[None]:
    """进入一次等待作用域：进入时读取一次档案；嵌套退出恢复前值。

    measure_action 仅供测量脚本：匹配动作的下限置 0，其余动作按保守值（不消费档案）。
    不允许全局禁用所有等待。
    """
    if measure_action is not None and measure_action not in ACTION_IDS:
        raise ValueError(f"measure_action 必须是 §4 动作ID：{measure_action!r}")
    policies, return_edges = load_policies_with_edges(environment)
    token = _active.set(_WaitContext(
        policies=policies, measure_action=measure_action, return_edges=return_edges
    ))
    try:
        yield
    finally:
        _active.reset(token)


def policy_for(
    action_id: str,
    *,
    fallback_ms: int,
    allow_measured: bool = True,
    return_edge: str | None = None,
) -> ActionWaitPolicy:
    """解析某动作的下限策略；未知动作、非法 fallback 或非布尔开关抛 ValueError。

    ``return_edge`` 只对 ``return_warehouse`` 有意义（Spec v9 §12）：传三条允许边之一时先取
    该边的逐边实测；该边无有效实测（或本轮传 None）再回落到动作级实测，最后才是
    ``fallback_ms``。逐边与动作级取样分开，快边不再被慢边的值拖住。
    """
    if action_id not in ACTION_IDS:
        raise ValueError(f"未知动作ID：{action_id!r}")
    if isinstance(fallback_ms, bool) or not isinstance(fallback_ms, int) or fallback_ms < 0:
        raise ValueError(f"fallback_ms 必须是非负整数：{fallback_ms!r}")
    if not isinstance(allow_measured, bool):
        raise ValueError(f"allow_measured 必须是布尔：{allow_measured!r}")
    if return_edge is not None:
        if action_id != RETURN_EDGE_ACTION:
            raise ValueError(f"只有 {RETURN_EDGE_ACTION} 支持 return_edge：{action_id!r}")
        if return_edge not in RETURN_EDGE_IDS:
            raise ValueError(f"未知 return_edge：{return_edge!r}")
    ctx = _active.get()
    if ctx is not None and ctx.measure_action == action_id:
        if not allow_measured:
            return ActionWaitPolicy(floor_ms=fallback_ms, source="default")
        return ActionWaitPolicy(floor_ms=0, source="measurement")
    if ctx is not None and ctx.measure_action is not None:
        # 测量会话里其余动作一律走保守值，不消费档案。
        return ActionWaitPolicy(floor_ms=fallback_ms, source="default")
    if ctx is not None and allow_measured:
        if return_edge is not None:
            policy = ctx.return_edges.get(return_edge)
            if policy is not None:
                return policy
        policy = ctx.policies.get(action_id)
        if policy is not None:
            return policy
    return ActionWaitPolicy(floor_ms=fallback_ms, source="default")


def wait_release(
    action_id: str,
    release: InputRelease,
    *,
    fallback_ms: int,
    allow_measured: bool = True,
    return_edge: str | None = None,
) -> None:
    """从真实释放点计时，按剩余预算分片睡满下限（§3.2）。

    每片请求睡眠不超过 20 ms，片前后检查停止/暂停；只调用控制回调，
    不触发输入前的业务校验。返回只表示下限到达，不表示业务就绪。
    """
    policy = policy_for(
        action_id, fallback_ms=fallback_ms, allow_measured=allow_measured,
        return_edge=return_edge,
    )
    release_s = release.released_at_ns / 1_000_000_000
    floor_s = policy.floor_ms / 1000.0
    requested_ms = 0.0
    try:
        while True:
            control.checkpoint_control()
            now = time.monotonic()
            paused = control.input_pause_intervals(release_s, now)
            active_s = (now - release_s) - sum(float(b) - float(a) for a, b in paused)
            remaining = floor_s - active_s
            if remaining <= _WAIT_EPS_S:
                return
            shard = min(0.02, remaining)
            requested_ms += shard * 1000.0
            time.sleep(shard)
    finally:
        # §7.1：无论正常、停止还是故障出口，都登记截至当下的真实明细（不冒充完成）。
        end = time.monotonic()
        paused = control.input_pause_intervals(release_s, end)
        active_ms = max(0.0, (end - release_s) - sum(float(b) - float(a) for a, b in paused)) * 1000.0
        record_wait_receipt(
            WaitReceipt(
                released_at_ns=release.released_at_ns,
                action_id=action_id,
                floor_ms=policy.floor_ms,
                policy_source=policy.source,
                requested_sleep_ms=requested_ms,
                active_elapsed_ms=active_ms,
            )
        )


def compute_floor_ms(stable_ms: Iterable[int]) -> int:
    """Spec §6.3 公式：floor = ceil((M + max(20, ceil(M*0.10))) / 10) * 10。"""
    values: list[int] = []
    for value in stable_ms:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"stable_ms 只能是非负整数：{value!r}")
        values.append(value)
    if not values:
        raise ValueError("stable_ms 不能为空")
    m = max(values)
    margin = max(20, math.ceil(m * 0.10))
    return math.ceil((m + margin) / 10.0) * 10


def _environment_block(environment: "Environment") -> dict[str, object] | None:
    """Spec §5：六字段齐全、尺寸/DPI 为正整数、字符串非空才允许持久化。"""
    machine_id = getattr(environment, "machine_id", None)
    client_width = getattr(environment, "client_width", None)
    client_height = getattr(environment, "client_height", None)
    window_dpi = getattr(environment, "window_dpi", None)
    game_exe_path = getattr(environment, "game_exe_path", None)
    game_build = getattr(environment, "game_build", None)
    if not isinstance(machine_id, str) or not machine_id:
        return None
    for value in (client_width, client_height, window_dpi):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
    for value in (game_exe_path, game_build):
        if not isinstance(value, str) or not value:
            return None
    return {
        "machine_id": machine_id,
        "client_width": client_width,
        "client_height": client_height,
        "window_dpi": window_dpi,
        "game_exe_path": game_exe_path,
        "game_build": game_build,
    }


def _floor_bound_ms(action_id: str) -> int | None:
    """floor 必须严格小于对应 timeout（§5）；标签与无就绪超时动作不适用。"""
    if action_id in _TIMEOUT_MS:
        return _TIMEOUT_MS[action_id]
    if action_id in _PANEL_ACTIONS:
        return _PANEL_FLOOR_BOUND_MS
    return None


def _valid_policy_item(
    action_id: str, item: object, *, min_count: int = _POLICY_MIN_COUNT
) -> tuple[ActionWaitPolicy | None, str | None]:
    """校验单个策略项；返回 (策略, None) 或 (None, 整档失效原因)。

    越界（floor >= timeout）不在本函数处理，由调用方按逐动作拒收。
    ``min_count`` 供逐边项使用：Spec v9 §12 要求每条边至少 5 个有效样本，
    低于动作级要求的 10。
    """
    if not isinstance(item, dict):
        return None, f"{action_id} 的策略不是对象"
    allowed = {"count", "max_stable_ms", "floor_ms", "report_sha256"}
    if set(item) != allowed:
        return None, f"{action_id} 的策略字段不符：{sorted(item)}"
    count = item["count"]
    max_stable_ms = item["max_stable_ms"]
    floor_ms = item["floor_ms"]
    report_sha256 = item["report_sha256"]
    if isinstance(count, bool) or not isinstance(count, int) or count < min_count:
        return None, f"{action_id} count 非法：{count!r}"
    if isinstance(max_stable_ms, bool) or not isinstance(max_stable_ms, int) or max_stable_ms < 0:
        return None, f"{action_id} max_stable_ms 非法：{max_stable_ms!r}"
    if isinstance(floor_ms, bool) or not isinstance(floor_ms, int) or floor_ms < 0:
        return None, f"{action_id} floor_ms 非法：{floor_ms!r}"
    if not isinstance(report_sha256, str) or not _SHA256_RE.match(report_sha256):
        return None, f"{action_id} report_sha256 非法"
    try:
        expected = compute_floor_ms([max_stable_ms])
    except ValueError:
        return None, f"{action_id} 公式复算失败"
    if floor_ms != expected:
        return None, f"{action_id} 公式复算不一致：{floor_ms} != {expected}"
    return ActionWaitPolicy(floor_ms=floor_ms, source="measured"), None


def _load_policies_impl(
    environment: "Environment", *, path: Path | str | None = None
) -> tuple[dict[str, ActionWaitPolicy], dict[str, ActionWaitPolicy]]:
    """读取并校验实测档案，返回 (动作级 policies, 逐边 return_edges)。任何整档失效返回两个空字典。

    整档失效（§5）：未知键、非法类型（含 bool 冒充数字）、负数、版本/环境不一致、
    公式复算不一致、count 不足、非法摘要、损坏 JSON。逐动作拒收：floor >= 对应 timeout
    的项按下限缺项处理，不影响其余动作。逐边项（Spec v9 §12）单独校验、单独拒收：
    某条边非法只丢该边，不牵连动作级项，也不使整档失效。
    """
    profile = Path(path) if path is not None else profile_path()
    empty: tuple[dict[str, ActionWaitPolicy], dict[str, ActionWaitPolicy]] = ({}, {})
    try:
        raw = json.loads(profile.read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(raw, dict):
        return empty
    if raw.get("schema_version") != _SCHEMA_VERSION or isinstance(raw.get("schema_version"), bool):
        return empty
    allowed_root = {"schema_version", "time_origin", "predicate_version", "environment", "policies"}
    if set(raw) == allowed_root | {"return_edges"}:
        pass
    elif set(raw) != allowed_root:
        return empty
    if raw["time_origin"] != _TIME_ORIGIN:
        return empty
    if (
        isinstance(raw["predicate_version"], bool)
        or raw["predicate_version"] != _PREDICATE_VERSION
    ):
        return empty
    file_env = raw["environment"]
    current_env = _environment_block(environment)
    if current_env is None or not isinstance(file_env, dict):
        return empty
    if set(file_env) != set(_ENVIRONMENT_FIELDS):
        return empty
    for field in _ENVIRONMENT_FIELDS:
        if file_env[field] != current_env[field]:
            return empty
    policies = raw["policies"]
    if not isinstance(policies, dict):
        return empty
    valid: dict[str, ActionWaitPolicy] = {}
    for action_id, item in policies.items():
        if action_id not in ACTION_IDS:
            return empty
        policy, error = _valid_policy_item(action_id, item)
        if error is not None:
            return empty
        bound = _floor_bound_ms(action_id)
        if bound is not None and policy.floor_ms >= bound:
            continue  # 逐动作拒收：按缺项 fallback，不使整份档案失效
        valid[action_id] = policy
    valid_edges = _valid_return_edges(raw.get("return_edges"))
    return valid, valid_edges


def _valid_return_edges(raw_edges: object) -> dict[str, ActionWaitPolicy]:
    """逐边项：整段必须是对象、键只能是三条允许边，单边非法只丢该边。"""
    if raw_edges is None:
        return {}
    if not isinstance(raw_edges, dict):
        return {}
    bound = _floor_bound_ms(RETURN_EDGE_ACTION)
    valid: dict[str, ActionWaitPolicy] = {}
    for edge, item in raw_edges.items():
        if edge not in RETURN_EDGE_IDS:
            continue
        policy, error = _valid_policy_item(edge, item, min_count=RETURN_EDGE_MIN_COUNT)
        if error is not None:
            continue
        if bound is not None and policy.floor_ms >= bound:
            continue
        valid[edge] = policy
    return valid


def load_policies(
    environment: "Environment", *, path: Path | str | None = None
) -> dict[str, ActionWaitPolicy]:
    """动作级实测项；逐边项见 :func:`load_policies_with_edges`。"""
    return _load_policies_impl(environment, path=path)[0]


def load_policies_with_edges(
    environment: "Environment", *, path: Path | str | None = None
) -> tuple[dict[str, ActionWaitPolicy], dict[str, ActionWaitPolicy]]:
    """(动作级实测项, 逐边实测项)；档案失效时两者都是空字典。"""
    return _load_policies_impl(environment, path=path)


def write_profile(
    environment: "Environment",
    measurements: Mapping[str, Mapping[str, object]],
    *,
    path: Path | str | None = None,
    return_edges: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """写出实测档案（§5）；输出且仅输出 status、sha256、error。

    ``return_edges`` 是 Spec v9 §12 的逐边实测项：键只能是三条允许边，值为与
    ``measurements`` 同构的对象。给了非空映射就写进根字段 ``return_edges``。
    **只由独立门禁传**——`return_warehouse` 按边分别测，装进动作级会把三边压成
    一个值，反过来让快边被慢边拖住。

    环境身份不完整返回 not_persistable；写入失败保留原档案并返回 write_failed；
    成功返回 written 与写出文件字节的 SHA256。
    """
    env_block = _environment_block(environment)
    if env_block is None:
        return {"status": "not_persistable", "sha256": None, "error": "环境身份不完整"}
    for action_id in measurements:
        if action_id not in ACTION_IDS:
            return {"status": "write_failed", "sha256": None, "error": f"未知动作ID：{action_id!r}"}
    edges_block: dict[str, dict[str, object]] = {}
    for edge, item in (return_edges or {}).items():
        if edge not in RETURN_EDGE_IDS:
            return {"status": "write_failed", "sha256": None, "error": f"未知 return_edge：{edge!r}"}
        edges_block[edge] = dict(item)
    payload: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "time_origin": _TIME_ORIGIN,
        "predicate_version": _PREDICATE_VERSION,
        "environment": env_block,
        "policies": {action_id: dict(item) for action_id, item in measurements.items()},
    }
    if edges_block:
        payload["return_edges"] = edges_block
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    dest = Path(path) if path is not None else profile_path()
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except Exception as exc:  # noqa: BLE001 - 失败必须保留原档案并原样报告
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return {"status": "write_failed", "sha256": None, "error": str(exc)}
    return {"status": "written", "sha256": sha, "error": None}
