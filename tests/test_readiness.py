"""readiness（就绪等待 + 本机等待档案 + §9.1 标定计算）的离线单测。

覆盖 Spec §8 的 W5、W6a～W6e（模块级）、C1、C3 与 §6.1/§6.4/§9.1 的核心契约。
时钟/控制器由 conftest 的 ``sim_clock``/``fake_controller`` 显式提供（不 autouse）；
sleep 真正推进模拟时钟。Runner 层接续（W1～W4、W6b/W6e 的 Runner 分支、W7～W10）留步骤 6。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from zzz_ddtool import readiness
from zzz_ddtool.models import Disc, Substat


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
def _frame():
    from PySide6.QtGui import QImage

    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.fill(0)
    return image


def _sample(clock, *, ready, key=("d",), disc=None, interrupt=None):
    return readiness.WaitSample(
        frame=_frame(),
        captured_at=clock.now(),
        ready=ready,
        state_key=key,
        evidence={},
        disc=disc,
        interrupt_reason=interrupt,
    )


def _run(clock, controller, steps, *, calls, kind="select_disc", policy=None, started=None):
    """按 steps 顺序驱动 wait_ready；返回 (result, 实际 probe 次数)。"""
    policy = policy if policy is not None else readiness.default_policy(kind)
    started = clock.now() if started is None else started

    def probe():
        index = len(calls)
        calls.append(1)
        return steps[index](clock, controller)

    result = readiness.wait_ready(
        kind=kind,
        policy=policy,
        started_at=started,
        probe=probe,
        checkpoint=controller.checkpoint,
        pauses=controller.pause_intervals,
    )
    return result, len(calls)


def _advance(clock, seconds):
    clock.advance(seconds)


# --------------------------------------------------------------------------
# 默认策略
# --------------------------------------------------------------------------
def test_default_policies_match_spec_table():
    expected = {
        "select_disc": (500, 3000),
        "open_preview": (900, 5000),
        "open_upgrade": (1500, 8000),
        "material_ready": (600, 3000),
        "upgrade_result": (1500, 8000),
        "return_warehouse": (600, 3000),
    }
    assert set(readiness.KINDS) == set(expected)
    for kind, (min_wait, timeout) in expected.items():
        policy = readiness.default_policy(kind)
        assert (policy.min_wait_ms, policy.timeout_ms) == (min_wait, timeout)
        assert policy.poll_ms == 50 and policy.stable_frames == 2


# --------------------------------------------------------------------------
# 核心完成判定
# --------------------------------------------------------------------------
def test_two_stable_frames_returns_second_frame(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(500, 3000, 50, 2)
    key = ("k",)
    steps = [
        lambda c, ctrl: (_advance(c, 0.550), _sample(c, ready=True, key=key))[1],
        lambda c, ctrl: _sample(c, ready=True, key=key),
    ]
    calls: list[int] = []
    result, n = _run(sim_clock, fake_controller, steps, calls=calls, policy=policy)
    assert n == 2
    assert result.samples == 2
    assert result.pause_generation == 0


def test_key_change_restarts_sequence(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(500, 3000, 50, 2)
    steps = [
        lambda c, ctrl: (_advance(c, 0.550), _sample(c, ready=True, key=("a",)))[1],
        lambda c, ctrl: _sample(c, ready=True, key=("b",)),
        lambda c, ctrl: _sample(c, ready=True, key=("b",)),
    ]
    calls: list[int] = []
    result, n = _run(sim_clock, fake_controller, steps, calls=calls, policy=policy)
    assert n == 3  # 第一帧因 key 改变被丢弃，第二三帧才稳定
    assert result.samples == 3


def test_not_ready_clears_sequence(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(500, 3000, 50, 2)
    steps = [
        lambda c, ctrl: (_advance(c, 0.550), _sample(c, ready=True, key=("a",)))[1],
        lambda c, ctrl: _sample(c, ready=False, key=("a",)),
        lambda c, ctrl: _sample(c, ready=True, key=("a",)),
        lambda c, ctrl: _sample(c, ready=True, key=("a",)),
    ]
    calls: list[int] = []
    result, n = _run(sim_clock, fake_controller, steps, calls=calls, policy=policy)
    assert n == 4  # 中间 not-ready 清零，需要重新凑两帧
    assert result.samples == 4


def test_frames_before_lower_bound_not_counted(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(500, 3000, 50, 2)
    steps = [
        lambda c, ctrl: (_advance(c, 0.300), _sample(c, ready=True))[1],
        lambda c, ctrl: _sample(c, ready=True),
        lambda c, ctrl: (_advance(c, 0.250), _sample(c, ready=True))[1],  # -> 0.600
        lambda c, ctrl: _sample(c, ready=True),  # 0.650，稳定两帧
    ]
    calls: list[int] = []
    result, n = _run(sim_clock, fake_controller, steps, calls=calls, policy=policy)
    assert n == 4  # 下限前的两帧都不计入
    assert result.samples == 4


# --------------------------------------------------------------------------
# 超时与代次
# --------------------------------------------------------------------------
def test_timeout_reports_last_sample_and_pause_generation(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(500, 3000, 50, 2)
    steps = [
        lambda c, ctrl: (_advance(c, 3.100), _sample(c, ready=False))[1],
    ]
    calls: list[int] = []
    with pytest.raises(readiness.ReadinessTimeout) as excinfo:
        _run(sim_clock, fake_controller, steps, calls=calls, policy=policy)
    err = excinfo.value
    assert err.samples == 1
    assert err.last_sample is not None and err.last_sample.ready is False
    assert err.pause_generation == 0
    assert err.active_ms > 3000


def test_w6b_unknown_frames_timeout_with_last_sample(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(200, 400, 50, 2)
    calls: list[int] = []

    def step(c, ctrl):
        c.advance(0.500)
        return _sample(c, ready=False)

    steps = [step, step]
    with pytest.raises(readiness.ReadinessTimeout) as excinfo:
        _run(sim_clock, fake_controller, steps, calls=calls, policy=policy)
    assert excinfo.value.last_sample is not None
    assert excinfo.value.pause_generation == 0


def test_w6c_expired_probe_over_budget_times_out(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(500, 3000, 50, 2)
    steps = [lambda c, ctrl: (_advance(c, 3.100), _sample(c, ready=True))[1]]
    calls: list[int] = []
    with pytest.raises(readiness.ReadinessTimeout):
        _run(sim_clock, fake_controller, steps, calls=calls, policy=policy)
    assert len(calls) == 1  # 过期结果不推进、不补发


def test_w6a_stop_propagates_runner_stopped(sim_clock, fake_controller):
    from zzz_ddtool.hotkeys import RunnerStopped

    fake_controller.request_stop()
    calls: list[int] = []
    steps = [lambda c, ctrl: _sample(c, ready=True)]
    with pytest.raises(RunnerStopped):
        _run(sim_clock, fake_controller, steps, calls=calls)
    assert calls == []  # 第一次 checkpoint 即停止，未请求 probe


# --------------------------------------------------------------------------
# W6d 参数/策略校验
# --------------------------------------------------------------------------
def test_w6d_invalid_kind_rejected_before_probe(sim_clock, fake_controller):
    calls: list[int] = []
    with pytest.raises(ValueError):
        readiness.wait_ready(
            kind="bogus",
            policy=readiness.default_policy("select_disc"),
            started_at=sim_clock.now(),
            probe=lambda: (calls.append(1), _sample(sim_clock, ready=True))[1],
            checkpoint=fake_controller.checkpoint,
            pauses=fake_controller.pause_intervals,
        )
    assert calls == []


def test_w6d_invalid_policy_rejected_before_probe():
    with pytest.raises(ValueError):
        readiness.WaitPolicy(500, 3000, 40)  # poll_ms 非固定 50
    with pytest.raises(ValueError):
        readiness.WaitPolicy(500, 3000, 50, 3)  # stable_frames 非固定 2
    with pytest.raises(ValueError):
        readiness.WaitPolicy(True, 3000, 50, 2)  # bool 冒充 int
    with pytest.raises(ValueError):
        readiness.WaitPolicy(500, 500, 50, 2)  # timeout 必须大于 min_wait


def test_w6d_missing_required_param_rejected():
    readiness.validate_probe_params("select_disc", {"cell": object()})
    with pytest.raises(ValueError):
        readiness.validate_probe_params("select_disc", {})
    with pytest.raises(ValueError):
        readiness.validate_probe_params("material_ready", {"before": object()})
    with pytest.raises(ValueError):
        readiness.validate_probe_params("upgrade_result", {"before": object()})
    with pytest.raises(ValueError):
        readiness.validate_probe_params("bogus", {})


# --------------------------------------------------------------------------
# W6e 识别未就绪 vs 实际异常
# --------------------------------------------------------------------------
def test_w6e_select_completes_only_after_two_stable_frames(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(500, 3000, 50, 2)
    key = ("t",)
    steps = [
        lambda c, ctrl: (_advance(c, 0.500), _sample(c, ready=False))[1],  # 500 ms 未就绪
        lambda c, ctrl: _sample(c, ready=True, key=key),  # 550 ms 完整目标盘
        lambda c, ctrl: _sample(c, ready=True, key=key),  # 600 ms 同一稳定盘
    ]
    calls: list[int] = []
    result, n = _run(sim_clock, fake_controller, steps, calls=calls, policy=policy)
    assert n == 3
    assert result.samples == 3
    assert abs(result.active_ms - 600) < 1


def test_w6e_probe_errors_propagate_immediately(sim_clock, fake_controller):
    from zzz_ddtool.capture import CaptureError
    from zzz_ddtool.ocr import OcrError

    for error in (OcrError("x"), CaptureError("y"), RuntimeError("z")):
        calls: list[int] = []

        def step(c, ctrl, _error=error):
            raise _error

        with pytest.raises(type(error)):
            _run(sim_clock, fake_controller, [step], calls=calls)
        assert len(calls) == 1  # 第一次发生即传播，不采下一帧


# --------------------------------------------------------------------------
# 暂停（W5）与 active_ms
# --------------------------------------------------------------------------
def test_w5_pause_generation_change_discards_ready_frame(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(200, 5000, 50, 2)
    key = ("a",)
    calls: list[int] = []

    def step0(c, ctrl):
        c.advance(0.300)
        return _sample(c, ready=True, key=key)

    def step1(c, ctrl):
        ctrl.simulate_pause(1.0)  # 代次 0→1，时钟推进到 1.300
        return _sample(c, ready=True, key=key)

    def step_n(c, ctrl):
        return _sample(c, ready=True, key=key)

    steps = [step0, step1] + [step_n] * 20
    result, n = _run(sim_clock, fake_controller, steps, calls=calls, policy=policy)
    # 若不丢弃旧帧，第一二帧就会完成（n==2）；代次改变后必须重建下限与两帧。
    assert n >= 3
    assert result.pause_generation == 1
    assert result.samples == n


def test_pause_excluded_from_active_ms(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(100, 5000, 50, 2)
    key = ("a",)
    calls: list[int] = []

    def step0(c, ctrl):
        c.advance(0.200)
        return _sample(c, ready=True, key=key)

    def step1(c, ctrl):
        ctrl.simulate_pause(2.0)  # 真暂停 2 秒，不计入 active_ms
        return _sample(c, ready=True, key=key)

    def step_n(c, ctrl):
        return _sample(c, ready=True, key=key)

    steps = [step0, step1] + [step_n] * 20
    result, _n = _run(sim_clock, fake_controller, steps, calls=calls, policy=policy)
    elapsed_ms = (sim_clock.t - 10_000.0) * 1000.0  # sim_clock 起始 10000.0，期间无其它推进
    assert result.active_ms < elapsed_ms - 1000  # 2 秒暂停被扣掉
    assert result.pause_generation == 1


def test_pause_then_timeout_without_new_sample_has_no_last_sample(sim_clock, fake_controller):
    """§8 W5/W6b：代次改变后尚无新代次样本就超时，last_sample 必须为 None。

    旧实现只清 mode/key/times，超时对象仍携带代次 0 的旧帧（captured_at 属于旧代次）。
    """
    policy = readiness.WaitPolicy(0, 150, 50, 2)
    calls: list[int] = []

    def step0(c, ctrl):
        c.advance(0.060)
        return _sample(c, ready=False, key=("old-generation",))

    def step1(c, ctrl):
        # probe 期间暂停并恢复：代次 0→1，暂停阻塞 1 秒（不计入有效预算）。
        ctrl.simulate_pause(1.0)
        return _sample(c, ready=False, key=("new-generation",))

    with pytest.raises(readiness.ReadinessTimeout) as err:
        _run(sim_clock, fake_controller, [step0, step1], calls=calls, policy=policy)
    exc = err.value
    assert exc.pause_generation == 1
    assert exc.last_sample is None  # 恢复后没有新代次取帧，不能留旧代次帧当诊断证据


def test_pause_during_probe_discards_inflight_sample(sim_clock, fake_controller):
    """§8 W5：probe 返回后才发现跨代次，正在处理的样本同样作废，不能完成等待。"""
    policy = readiness.WaitPolicy(0, 5000, 50, 2)
    key = ("a",)
    calls: list[int] = []

    def step0(c, ctrl):
        return _sample(c, ready=True, key=key)

    def step1(c, ctrl):
        ctrl.simulate_pause(1.0)  # 代次 0→1；即便本帧 ready 也不许与旧代次首帧凑对
        return _sample(c, ready=True, key=key)

    def step_n(c, ctrl):
        return _sample(c, ready=True, key=key)

    result, n = _run(sim_clock, fake_controller, [step0, step1] + [step_n] * 20,
                     calls=calls, policy=policy)
    # 跨代次的 step1 若被消费就会与 step0 凑成两帧（n==2 完成）；必须重建后再要两帧。
    assert n == 4
    assert result.pause_generation == 1


# --------------------------------------------------------------------------
# 调度（§6.1：按采集时刻调度下一帧）
# --------------------------------------------------------------------------
def test_slow_probe_schedules_next_frame_from_capture_time(sim_clock, fake_controller):
    """probe 耗时达到 poll 后立即继续，不在慢 probe 后再机械睡足一个 poll。

    旧实现 ``target = probe 结束时刻 + poll``：两次各 100 ms 的 probe 会把第二帧推到
    150 ms 采集、250 ms 结束，220 ms 预算内本可完成的等待被拖成超时。
    """
    policy = readiness.WaitPolicy(0, 220, 50, 2)
    key = ("same",)
    captured: list[float] = []
    calls: list[int] = []

    def step(c, ctrl):
        taken = c.now()
        captured.append(taken - 10_000.0)  # sim_clock 起始 10000.0，记相对时刻
        c.advance(0.100)  # 本次 probe 的截图/OCR 耗时 100 ms > poll 50 ms
        return readiness.WaitSample(frame=_frame(), captured_at=taken, ready=True,
                                    state_key=key, evidence={})

    result, n = _run(sim_clock, fake_controller, [step, step], calls=calls, policy=policy)
    assert n == 2
    # 帧在 0/100 ms 采集（第二帧紧跟首次 probe 结束），200 ms 完成，预算 220 ms 内。
    assert captured == [pytest.approx(0.0), pytest.approx(0.1)]
    assert result.active_ms == pytest.approx(200.0)


# --------------------------------------------------------------------------
# material_refund 受控中断（§6.2.1）
# --------------------------------------------------------------------------
def test_material_refund_two_stable_frames_interrupts(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(200, 3000, 50, 2)
    key = ("refund",)
    steps = [
        lambda c, ctrl: (_advance(c, 0.300), _sample(c, ready=False, key=key, interrupt=readiness.MATERIAL_REFUND))[1],
        lambda c, ctrl: _sample(c, ready=False, key=key, interrupt=readiness.MATERIAL_REFUND),
    ]
    calls: list[int] = []
    with pytest.raises(readiness.ReadinessInterrupted) as excinfo:
        _run(sim_clock, fake_controller, steps, calls=calls, kind="upgrade_result", policy=policy)
    err = excinfo.value
    assert err.reason == readiness.MATERIAL_REFUND
    assert err.samples == 2
    assert err.last_sample is not None and err.last_sample.disc is None


def test_material_refund_wrong_kind_rejected(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(200, 3000, 50, 2)
    steps = [
        lambda c, ctrl: (_advance(c, 0.300), _sample(c, ready=False, interrupt=readiness.MATERIAL_REFUND))[1],
    ]
    calls: list[int] = []
    with pytest.raises(ValueError):
        _run(sim_clock, fake_controller, steps, calls=calls, kind="select_disc", policy=policy)


def test_material_refund_over_budget_prefers_timeout(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(200, 3000, 50, 2)
    steps = [
        lambda c, ctrl: (_advance(c, 0.300), _sample(c, ready=False, interrupt=readiness.MATERIAL_REFUND))[1],
        lambda c, ctrl: (_advance(c, 3.000), _sample(c, ready=False, interrupt=readiness.MATERIAL_REFUND))[1],
    ]
    calls: list[int] = []
    with pytest.raises(readiness.ReadinessTimeout):
        _run(sim_clock, fake_controller, steps, calls=calls, kind="upgrade_result", policy=policy)


def test_mutually_exclusive_sequences_rebuild(sim_clock, fake_controller):
    policy = readiness.WaitPolicy(200, 5000, 50, 2)
    steps = [
        lambda c, ctrl: (_advance(c, 0.300), _sample(c, ready=False, interrupt=readiness.MATERIAL_REFUND))[1],
        lambda c, ctrl: _sample(c, ready=True, key=("d",)),  # 切到 ready 序列
        lambda c, ctrl: _sample(c, ready=False, interrupt=readiness.MATERIAL_REFUND),  # 再切回中断序列
        lambda c, ctrl: _sample(c, ready=True, key=("d",)),  # 又切回 ready
        lambda c, ctrl: _sample(c, ready=True, key=("d",)),  # ready 稳定两帧
    ]
    calls: list[int] = []
    result, n = _run(sim_clock, fake_controller, steps, calls=calls, kind="upgrade_result", policy=policy)
    assert n == 5  # 中间不停在中途就绪/中断
    assert result.samples == 5


def test_wait_sample_rejects_illegal_interrupt_combo():
    with pytest.raises(ValueError):
        readiness.WaitSample(frame=_frame(), captured_at=0.0, ready=True, state_key=(),
                             evidence={}, interrupt_reason=readiness.MATERIAL_REFUND)
    with pytest.raises(ValueError):
        readiness.WaitSample(frame=_frame(), captured_at=0.0, ready=False, state_key=(),
                             evidence={}, disc=Disc(0, ()), interrupt_reason=readiness.MATERIAL_REFUND)
    with pytest.raises(ValueError):
        readiness.WaitSample(frame=_frame(), captured_at=0.0, ready=False, state_key=(),
                             evidence={}, interrupt_reason="bogus")


# --------------------------------------------------------------------------
# C1 本机等待档案（§6.4）
# --------------------------------------------------------------------------
ENV = {
    "machine_id": "abc123",
    "client_width": 1920,
    "client_height": 1080,
    "window_dpi": 96,
    "game_exe_path": "c:/game/zenless.exe",
    "game_build": "1.2.3.4|100|200",
}


def _policy_json(min_wait=800, timeout=3000, poll=50, stable=2):
    return {"min_wait_ms": min_wait, "timeout_ms": timeout, "poll_ms": poll, "stable_frames": stable}


def _document(**overrides):
    doc = {
        "schema_version": 1,
        "predicate_version": 1,
        "environment": dict(ENV),
        "policies": {"select_disc": _policy_json()},
        "measurements": {"select_disc": {"count": 20, "max_stable_ms": 120.0, "floor_ms": 800, "report_sha256": "ab"}},
    }
    doc.update(overrides)
    return doc


def _write_doc(tmp_path: Path, doc) -> Path:
    path = tmp_path / "scan-waits.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _defaults() -> dict:
    return {kind: readiness.default_policy(kind) for kind in readiness.KINDS}


def test_c1_missing_environment_field_invalidates(tmp_path):
    doc = _document()
    del doc["environment"]["window_dpi"]
    policies = readiness.load_wait_policies(ENV, path=_write_doc(tmp_path, doc))
    assert policies == _defaults()


def test_c1_environment_mismatch_invalidates(tmp_path):
    doc = _document()
    doc["environment"]["machine_id"] = "other"
    assert readiness.load_wait_policies(ENV, path=_write_doc(tmp_path, doc)) == _defaults()


def test_c1_corrupt_json_invalidates(tmp_path):
    path = tmp_path / "scan-waits.json"
    path.write_text("{not json", encoding="utf-8")
    assert readiness.load_wait_policies(ENV, path=path) == _defaults()


def test_c1_predicate_version_change_invalidates(tmp_path):
    assert readiness.load_wait_policies(ENV, path=_write_doc(tmp_path, _document(predicate_version=2))) == _defaults()


def test_c1_poll_ms_fixed_value_invalidates(tmp_path):
    doc = _document(policies={"select_disc": _policy_json(poll=40)})
    assert readiness.load_wait_policies(ENV, path=_write_doc(tmp_path, doc)) == _defaults()


def test_c1_stable_frames_fixed_value_invalidates(tmp_path):
    doc = _document(policies={"select_disc": _policy_json(stable=3)})
    assert readiness.load_wait_policies(ENV, path=_write_doc(tmp_path, doc)) == _defaults()


def test_c1_unknown_kind_invalidates(tmp_path):
    doc = _document(
        policies={"select_disc": _policy_json(), "discard_label": _policy_json()},
        measurements={
            "select_disc": {"count": 20, "max_stable_ms": 120.0, "floor_ms": 800, "report_sha256": "ab"},
            "discard_label": {"count": 1, "max_stable_ms": 1.0, "floor_ms": 1, "report_sha256": "ab"},
        },
    )
    assert readiness.load_wait_policies(ENV, path=_write_doc(tmp_path, doc)) == _defaults()


def test_c1_key_set_mismatch_invalidates(tmp_path):
    doc = _document()
    doc["measurements"]["open_preview"] = {"count": 5, "max_stable_ms": 1.0, "floor_ms": 1, "report_sha256": "ab"}
    assert readiness.load_wait_policies(ENV, path=_write_doc(tmp_path, doc)) == _defaults()


def test_c1_bool_as_int_invalidates(tmp_path):
    doc = _document(policies={"select_disc": _policy_json(min_wait=True)})
    assert readiness.load_wait_policies(ENV, path=_write_doc(tmp_path, doc)) == _defaults()


def test_c1_empty_collections_all_default(tmp_path):
    doc = _document(policies={}, measurements={})
    assert readiness.load_wait_policies(ENV, path=_write_doc(tmp_path, doc)) == _defaults()


def test_c1_partial_policies_use_default_for_missing(tmp_path):
    doc = _document(policies={"select_disc": _policy_json(min_wait=800)})
    policies = readiness.load_wait_policies(ENV, path=_write_doc(tmp_path, doc))
    assert policies["select_disc"].min_wait_ms == 800
    assert policies["open_upgrade"] == readiness.default_policy("open_upgrade")


def test_c1_write_ok_and_reload(tmp_path):
    path = tmp_path / "scan-waits.json"
    policies = {"select_disc": readiness.WaitPolicy(800, 3000, 50, 2)}
    measurements = {"select_disc": {"count": 20, "max_stable_ms": 120.0, "floor_ms": 800, "report_sha256": "ab"}}
    result = readiness.write_wait_profile(ENV, policies, measurements, path=path)
    assert result.status == "ok"
    assert result.profile_sha256 is not None
    assert path.is_file()
    from hashlib import sha256

    assert result.profile_sha256 == sha256(path.read_bytes()).hexdigest()
    assert readiness.load_wait_policies(ENV, path=path)["select_disc"].min_wait_ms == 800


def test_c1_write_not_persistable(tmp_path):
    path = tmp_path / "scan-waits.json"
    bad_env = dict(ENV)
    bad_env["game_build"] = None
    result = readiness.write_wait_profile(bad_env, {}, {}, path=path)
    assert result.status == "not_persistable"
    assert result.profile_sha256 is None
    assert not path.exists()


def test_c1_write_failure_keeps_existing_bytes(tmp_path, monkeypatch):
    path = tmp_path / "scan-waits.json"
    policies = {"select_disc": readiness.WaitPolicy(800, 3000, 50, 2)}
    measurements = {"select_disc": {"count": 20, "max_stable_ms": 120.0, "floor_ms": 800, "report_sha256": "ab"}}
    assert readiness.write_wait_profile(ENV, policies, measurements, path=path).status == "ok"
    original = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(readiness.os, "replace", boom)
    result = readiness.write_wait_profile(ENV, policies, measurements, path=path)
    assert result.status == "write_failed"
    assert result.profile_sha256 is None
    assert result.error is not None and result.error["type"] == "OSError"
    assert path.read_bytes() == original
    assert not (tmp_path / "scan-waits.json.tmp").exists()


def test_c1_write_overwrites_existing(tmp_path):
    path = tmp_path / "scan-waits.json"
    m = {"select_disc": {"count": 20, "max_stable_ms": 120.0, "floor_ms": 800, "report_sha256": "ab"}}
    readiness.write_wait_profile(ENV, {"select_disc": readiness.WaitPolicy(800, 3000, 50, 2)}, m, path=path)
    readiness.write_wait_profile(ENV, {"select_disc": readiness.WaitPolicy(900, 3000, 50, 2)}, m, path=path)
    assert readiness.load_wait_policies(ENV, path=path)["select_disc"].min_wait_ms == 900


# --------------------------------------------------------------------------
# C3 标定计算（§9.1）
# --------------------------------------------------------------------------
def test_c3_floor_from_max_stable_177():
    assert readiness.compute_floor_ms(177) == 280
    assert readiness.compute_floor_ms(0) == 100
    assert readiness.compute_floor_ms(100) == 200


def test_c3_floor_rejects_non_finite():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            readiness.compute_floor_ms(bad)


def _obs(sid, level, rolls=1, *, valid=True):
    return readiness.SelectObservation(sid, (level, (("x", rolls),)), valid=valid)


def test_c3_select_alternating_is_sufficient():
    obs = []
    for _ in range(10):
        obs.append(_obs("a", 0))
        obs.append(_obs("b", 3))
    summary = readiness.summarize_select(obs)
    assert (summary.count, summary.different_transitions) == (20, 19)
    assert summary.sufficient


def test_c3_select_same_id_insufficient():
    obs = [_obs("a", 0) for _ in range(20)]
    summary = readiness.summarize_select(obs)
    assert (summary.count, summary.different_transitions) == (20, 0)
    assert not summary.sufficient
    assert "content_diversity" in summary.reason_codes


def test_c3_select_different_id_same_text_insufficient():
    obs = [_obs("a", 0), _obs("b", 0), _obs("a", 0), _obs("b", 0)] * 5
    summary = readiness.summarize_select(obs)
    assert (summary.count, summary.different_transitions) == (20, 0)
    assert not summary.sufficient


def test_c3_select_invalid_sample_does_not_bridge():
    obs = [
        _obs("a", 1),
        _obs("b", 2),
        readiness.SelectObservation("c", (3, (("z", 1),)), valid=False),
        _obs("a", 1),
        _obs("b", 2),
    ]
    summary = readiness.summarize_select(obs)
    assert summary.count == 4
    assert summary.different_transitions == 2  # 无效样本两侧不搭桥


def test_c3_select_19_observations_insufficient():
    obs = []
    for i in range(19):
        obs.append(_obs("a" if i % 2 == 0 else "b", i))
    summary = readiness.summarize_select(obs)
    assert summary.count == 19
    assert not summary.sufficient
    assert "sample_count" in summary.reason_codes


def _select_20_with_transitions(n_transitions):
    """20 个有效观察，恰好产生 n_transitions 个不同转换。"""
    obs = [_obs("s", 0) for _ in range(10)]
    seq_ids = ["a", "b", "a", "b", "a", "b", "a", "b", "a", "b"]
    for i, sid in enumerate(seq_ids[:n_transitions]):
        obs.append(_obs(sid, 10 + i))
    # 补足到 20 个（重复最后一个，不产生转换）
    while len(obs) < 20:
        obs.append(obs[-1])
    obs = obs[:20]
    return obs


def test_c3_select_boundary_nine_vs_ten():
    # 10 个同 ID + 交替序列：首转换 + 交替内部转换
    nine = _select_20_with_transitions(9)
    ten = _select_20_with_transitions(10)
    s9 = readiness.summarize_select(nine)
    s10 = readiness.summarize_select(ten)
    assert s9.count == 20 and s9.different_transitions == 9 and not s9.sufficient
    assert s10.count == 20 and s10.different_transitions == 10 and s10.sufficient


def _return_obs(edge, t, **kwargs):
    return readiness.ReturnObservation(edge, t_stable_ms=t, **kwargs)


def test_c3_return_three_edges_take_max():
    obs = []
    obs += [_return_obs("modal_preview", t) for t in (90, 80, 70, 60, 50)]
    obs += [_return_obs("modal_warehouse", t) for t in (177, 160, 150, 140, 130)]
    obs += [_return_obs("preview_warehouse", t) for t in (30, 25, 20, 15, 10)]
    summary = readiness.summarize_return_warehouse(obs)
    assert summary.status == "calibrated"
    assert summary.floor_ms == 280  # max(190, 280, 130)
    assert summary.edge_floors["modal_warehouse"] == 280


def test_c3_return_missing_edge_keeps_default():
    obs = [_return_obs("modal_preview", t) for t in (90, 80, 70, 60, 50)]
    obs += [_return_obs("modal_warehouse", t) for t in (177, 160, 150, 140, 130)]
    summary = readiness.summarize_return_warehouse(obs)
    assert summary.status == "insufficient"
    assert summary.floor_ms == readiness.DEFAULT_FLOOR_MS["return_warehouse"]  # 保默认，不降
    assert "edge_coverage" in summary.reason_codes


def test_c3_return_non_allowed_edge_timeout_failed():
    obs = [_return_obs("modal_preview", t) for t in (90, 80, 70, 60, 50)]
    obs += [_return_obs("modal_warehouse", t) for t in (177, 160, 150, 140, 130)]
    obs += [_return_obs("preview_warehouse", t) for t in (30, 25, 20, 15, 10)]
    obs += [readiness.ReturnObservation(None, t_stable_ms=None, timed_out=True)]
    summary = readiness.summarize_return_warehouse(obs)
    assert summary.status == "failed"


def test_c3_return_false_ready_failed():
    obs = [_return_obs("modal_preview", t) for t in (90, 80, 70, 60, 50)]
    obs += [readiness.ReturnObservation("modal_preview", t_stable_ms=None, false_ready=True)]
    summary = readiness.summarize_return_warehouse(obs)
    assert summary.status == "failed"


def _report():
    return {
        "schema_version": 1,
        "spec_sha256": "aa",
        "execution_identity": {"head_sha": "h"},
        "environment": {"machine": "m"},
        "predicate_version": 1,
        "selection": {"confirmed_at": "2026-10-10T00:00:00+08:00"},
        "samples": [{"sample_id": 1, "t_stable": 100.0}],
        "summary": {"select_disc": "calibrated"},
        "report_sha256": "self",
        "profile_sha256": "self",
        "passed": True,
        "generated_at": "2026-10-10T09:00:00+08:00",
    }


def test_c3_digest_ignores_toplevel_metadata():
    base = _report()
    digest = readiness.calibration_report_sha256(base)
    changed = dict(base)
    changed.update(report_sha256="other", profile_sha256="other", passed=False, generated_at="later")
    assert readiness.calibration_report_sha256(changed) == digest


def test_c3_digest_changes_with_selection():
    base = _report()
    digest = readiness.calibration_report_sha256(base)
    changed = dict(base)
    changed["selection"] = {"confirmed_at": "2026-10-11T00:00:00+08:00"}
    assert readiness.calibration_report_sha256(changed) != digest


def test_c3_digest_rejects_non_finite():
    base = _report()
    base["samples"] = [{"sample_id": 1, "t_stable": float("nan")}]
    with pytest.raises(ValueError):
        readiness.calibration_report_sha256(base)


# --------------------------------------------------------------------------
# 依赖方向（Spec §2.2）
# --------------------------------------------------------------------------
def test_readiness_dependency_direction():
    source = Path(readiness.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"ocr", "capture", "trace", "hotkeys", "scanperf", "screen", "runner", "discocr"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("zzz_ddtool"):
            imported.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("zzz_ddtool"):
                    imported.add(alias.name.split(".")[-1])
    assert not (imported & forbidden), f"readiness 不得依赖：{imported & forbidden}"
