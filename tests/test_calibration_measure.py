"""§9.1 标定采样脚本的离线层：观测复算、持续观察调度与四类 kind 汇总。

只驱动 ``measure_waits.py`` 的可注入层（时钟/探针替身），不碰游戏、不写等待档案。
真机采样层属 §9 步骤 6～7，由步骤 8b/9 在真机上执行。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "dev-docs" / "功能改造" / "扫描提速" / "scripts" / "measure_waits.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("measure_waits_step8", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mw = _load_module()


# --------------------------------------------------------------------------
# §9.1 观测复算
# --------------------------------------------------------------------------
def probe_item(key, *, t, ready=True, index=0):
    return {
        "sample_index": index,
        "captured_at": t,
        "probe_ended_at": t + 0.03,
        "ready": ready,
        "state_key": list(key) if key is not None else None,
        "_key": key,
        "evidence": {},
        "interrupt_reason": None,
    }


def series(keys, *, step=0.05, t0=100.0, ready=None):
    return [
        probe_item(key, t=t0 + i * step, ready=(key is not None) if ready is None else ready[i],
                   index=i)
        for i, key in enumerate(keys)
    ]


def test_first_frame_ready_has_zero_lower_bound():
    calc = mw.recompute_observation(series([("A",)] * 62), t0=100.0)

    assert calc["outcome"] == "ok"
    assert calc["t_bad_ms"] is None
    assert calc["t_ready_ms"] == 0.0
    assert calc["t_stable_ms"] == pytest.approx(50.0)
    assert calc["false_ready"] is False


def test_last_non_final_sample_before_stable_pair_is_t_bad():
    calc = mw.recompute_observation(series([None] * 21 + [("A",)] * 60), t0=100.0)

    assert calc["t_bad_ms"] == pytest.approx(1000.0)
    assert calc["t_ready_ms"] == pytest.approx(1050.0)
    assert calc["t_stable_ms"] == pytest.approx(1100.0)


def test_early_ready_on_non_final_content_is_false_ready():
    """选中边框先出现、侧栏仍是旧盘：早期 ready 与最终 key 不同 → 假就绪。"""
    calc = mw.recompute_observation(series([("A",), ("A",)] + [("B",)] * 60), t0=100.0)

    assert calc["false_ready"] is True
    assert calc["outcome"] == "false_ready"


def test_later_candidate_revokes_earlier_one_and_recomputes_from_new_tail():
    keys = [None] * 5 + [("A",)] * 40 + [("B",)] * 40
    calc = mw.recompute_observation(series(keys), t0=100.0)

    assert calc["t_ready_ms"] == pytest.approx(5 * 50.0 + 40 * 50.0)
    assert calc["t_stable_ms"] == pytest.approx(calc["t_ready_ms"] + 50.0)


def test_short_tail_or_span_is_excluded_as_timeout():
    calc = mw.recompute_observation(series([("A",), ("A",)]), t0=100.0)

    assert calc["outcome"] == "timeout"
    assert calc["exclude_reason"] == "timeout"


def test_without_stable_pair_there_is_no_measurement():
    calc = mw.recompute_observation(series([None] * 60, ready=[False] * 60), t0=100.0)

    assert calc["outcome"] == "predicate_error"
    assert calc["t_stable_ms"] is None


# --------------------------------------------------------------------------
# §9.1 持续观察
# --------------------------------------------------------------------------
class FakeClock:
    """可注入的单调时钟：sleep 直接把时间推后，不真等。"""

    def __init__(self, start: float = 100.0) -> None:
        self.t = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make_probe(clock: FakeClock, *, cost=0.03, key=("A",), ready=True, flip=None):
    state = {"n": 0}

    def probe():
        captured = clock.t
        clock.t += cost
        active_key, active_ready = key, ready
        if flip is not None:
            active_key, active_ready = flip(state["n"], clock.t)
        state["n"] += 1
        return SimpleNamespace(
            captured_at=captured, ready=active_ready, state_key=active_key,
            evidence={}, disc=None, interrupt_reason=None,
        )

    return probe


def test_observation_waits_the_full_minimum_window_even_when_immediately_ready():
    clock = FakeClock()
    samples, timed_out = mw.observe_transition(
        make_probe(clock), t0=100.0, timeout_ms=3000, now=clock.now, sleep=clock.sleep
    )

    assert timed_out is False
    assert samples[-1]["captured_at"] - 100.0 >= mw.OBSERVE_MIN_MS / 1000.0 - 1e-6
    calc = mw.recompute_observation(samples, t0=100.0)
    assert calc["outcome"] == "ok"
    assert calc["t_bad_ms"] is None


def test_observation_extends_when_content_changes_in_the_tail():
    clock = FakeClock()
    samples, timed_out = mw.observe_transition(
        make_probe(clock, flip=lambda _i, now: ((("A",) if now < 101.9 else ("B",)), True)),
        t0=100.0, timeout_ms=8000, now=clock.now, sleep=clock.sleep,
    )

    calc = mw.recompute_observation(samples, t0=100.0)
    assert timed_out is False
    assert calc["t_stable_ms"] > 1900.0, "尾段内容变化后必须按新序列延长观察"
    assert samples[-1]["captured_at"] >= 101.9 + mw.OBSERVE_TAIL_MS / 1000.0 - 1e-6


def test_observation_stops_at_hard_timeout_without_ready():
    clock = FakeClock()
    samples, timed_out = mw.observe_transition(
        make_probe(clock, ready=False), t0=100.0, timeout_ms=400,
        now=clock.now, sleep=clock.sleep,
    )

    assert timed_out is True
    assert samples[-1]["captured_at"] - 100.0 <= 0.4 + 0.05


def test_observation_times_out_when_content_gets_stuck_after_a_stable_pair():
    """稳定帧对之后内容卡住不再就绪：目标时刻被最后一个变化帧一直推后 → 硬超时。"""
    clock = FakeClock()
    samples, timed_out = mw.observe_transition(
        make_probe(clock, flip=lambda index, _now: ((("A",) if index < 2 else None), index < 2)),
        t0=100.0, timeout_ms=3000, now=clock.now, sleep=clock.sleep,
    )

    assert timed_out is True
    calc = mw.recompute_observation(samples, t0=100.0, timed_out=timed_out)
    assert calc["outcome"] == "timeout"
    assert calc["t_ready_ms"] == pytest.approx(0.0)
    assert calc["t_stable_ms"] == pytest.approx(80.0)  # 测点仍是开头那一对（每轮 30+50ms）
    # 反例说明：不带 timed_out 时同一条序列会被尾段/跨度判成 ok —— loop 的停止原因不可省。
    assert mw.recompute_observation(samples, t0=100.0)["outcome"] == "ok"


def test_probe_and_checkpoint_errors_propagate_unchanged():
    clock = FakeClock()

    def boom():
        raise RuntimeError("识别炸了")

    with pytest.raises(RuntimeError, match="识别炸了"):
        mw.observe_transition(boom, t0=100.0, timeout_ms=1000, now=clock.now, sleep=clock.sleep)

    def stop():
        raise RuntimeError("F10 请求停止")

    with pytest.raises(RuntimeError, match="F10 请求停止"):
        mw.observe_transition(
            make_probe(clock), t0=100.0, timeout_ms=1000,
            checkpoint=stop, now=clock.now, sleep=clock.sleep,
        )


# --------------------------------------------------------------------------
# §9.1 四类 kind 汇总
# --------------------------------------------------------------------------
def core_of(key):
    """``(level, ((名称, 词条数), ...))`` → 报告里的 ``final_core`` 形态。"""
    level, pairs = key
    return {"level": level, "substats": [{"name": name, "rolls": rolls} for name, rolls in pairs]}


def select_rows(pairs, *, valid=True):
    """样本记录里**没有** ``core_key`` 字段：统计 key 由 ``final_core`` 推出。"""
    return [
        {"sample_id": f"select-{i:02d}", "selection_id": sid, "final_core": core_of(key),
         "valid_for_floor": valid, "t_stable_ms": 120.0 + i, "false_ready": False}
        for i, (sid, key) in enumerate(pairs)
    ]


def distinct_pairs(count):
    return [
        (f"c{i % 10}", (i // 2, (("攻击力", 1),)) if i % 2 == 0 else (i // 2, (("暴击率", 2),)))
        for i in range(count)
    ]


def test_select_is_calibrated_with_enough_distinct_transitions():
    entry = mw.summarize_select_kind(select_rows(distinct_pairs(20)), default_floor_ms=500)

    assert entry["valid_count"] == 20
    assert entry["different_transitions"] == 19
    assert entry["status"] == "calibrated"
    assert entry["floor_ms"] % 10 == 0


def test_select_stat_key_comes_from_final_core_without_rarity():
    """回归：记录里只有 ``final_core``（无 ``core_key``），统计不得因此炸掉。"""
    rows = select_rows(distinct_pairs(20))
    assert all("core_key" not in row for row in rows)

    # 稀有度不参与「不同内容」判定：只改稀有度不产生转换。
    for index, row in enumerate(rows):
        row["final_core"] = dict(row["final_core"], rarity="S" if index % 2 else "A")
    assert mw.summarize_select_kind(rows, default_floor_ms=500)["status"] == "calibrated"
    assert mw.summarize_select_kind(rows, default_floor_ms=500)["different_transitions"] == 19
    assert mw.core_key_of_core({"level": 15, "substats": []}) == (15, ())
    assert mw.core_key_of_core(None) is None


def test_final_core_is_a_core_dict_not_a_stat_key():
    """回归：``core_of_evidence`` 必须产出 core 字典，不能直接产出统计 key。

    样本的 ``final_core`` 由 ``build_sample_record`` 写入；两个消费方
    （本模块 ``summarize_select_kind``、门禁 ``verify_spec._core_key``）都按
    ``core_key_of_core(final_core)`` 取 key。生产端若先转成 ``(level, pairs)``，
    消费方二次转换会因「不是 dict」拿到 ``None``，相邻不同内容转换恒计 0。
    """
    evidence = {"core": {"level": 15, "rarity": "S",
                         "substats": [{"name": "攻击力", "rolls": 1}]}}
    core = mw.core_of_evidence(evidence)

    assert isinstance(core, dict)
    assert core == evidence["core"]
    assert mw.core_key_of_core(core) == (15, (("攻击力", 1),))
    assert mw.core_of_evidence({"core": "不是字典"}) is None
    assert mw.core_of_evidence({"nothing": 1}) is None
    assert mw.core_of_evidence(None) is None


def test_summarize_counts_transitions_on_cores_from_core_of_evidence():
    """端到端形状：生产端 ``core_of_evidence`` 的产物喂进汇总，必须真的数出转换。"""
    high = mw.core_of_evidence(
        {"core": {"level": 15, "substats": [{"name": "攻击力", "rolls": 3}]}}
    )
    low = mw.core_of_evidence(
        {"core": {"level": 0, "substats": [{"name": "暴击率", "rolls": 1}]}}
    )
    rows = [
        {"sample_id": f"select-{i:02d}", "selection_id": "s1" if i % 2 == 0 else "s2",
         "final_core": high if i % 2 == 0 else low,
         "valid_for_floor": True, "t_stable_ms": 200.0, "false_ready": False}
        for i in range(20)
    ]
    entry = mw.summarize_select_kind(rows, default_floor_ms=500)

    assert entry["valid_count"] == 20
    assert entry["different_transitions"] == 19
    assert entry["status"] == "calibrated"


def test_select_needs_both_count_and_content_diversity():
    same = select_rows([(f"c{i % 10}", (0, (("攻击力", 1),))) for i in range(20)])
    entry = mw.summarize_select_kind(same, default_floor_ms=500)
    assert entry["status"] == "insufficient"
    assert "content_diversity" in entry["reason_codes"]
    assert entry["floor_ms"] == 500

    short = select_rows(distinct_pairs(19))
    short_entry = mw.summarize_select_kind(short, default_floor_ms=500)
    assert short_entry["status"] == "insufficient"
    assert "sample_count" in short_entry["reason_codes"]
    assert short_entry["floor_ms"] == 500


def test_select_false_ready_fails_the_kind():
    rows = select_rows(distinct_pairs(20))
    rows[3]["false_ready"] = True
    entry = mw.summarize_select_kind(rows, default_floor_ms=500)

    assert entry["status"] == "failed"
    assert entry["reason_codes"] == ["false_ready"]
    assert entry["floor_ms"] == 500


def transition_rows(count, *, false_ready=False, timeout=False):
    return [
        {
            "sample_id": f"open-{i:02d}",
            "valid_for_floor": not timeout,
            "t_stable_ms": 200.0 + i * 10,
            "false_ready": false_ready,
            "outcome": "timeout" if timeout else "ok",
        }
        for i in range(count)
    ]


def test_transition_needs_five_valid_samples():
    assert mw.summarize_transition(
        "open_preview", transition_rows(5), default_floor_ms=900
    )["status"] == "calibrated"
    short = mw.summarize_transition("open_preview", transition_rows(4), default_floor_ms=900)
    assert short["status"] == "insufficient"
    assert short["floor_ms"] == 900


def test_transition_timeout_or_false_ready_fails_the_kind():
    timed = mw.summarize_transition(
        "open_upgrade", transition_rows(5, timeout=True), default_floor_ms=1500
    )
    assert timed["status"] == "failed"
    assert timed["reason_codes"] == ["timeout"]
    assert timed["floor_ms"] == 1500

    false = mw.summarize_transition(
        "open_upgrade", transition_rows(5, false_ready=True), default_floor_ms=1500
    )
    assert false["status"] == "failed"
    assert false["reason_codes"] == ["false_ready"]


def return_rows(edges):
    return [
        {
            "sample_id": f"return-{i:02d}", "edge": edge, "valid_for_floor": True,
            "t_stable_ms": 300.0 + i, "timed_out": False, "false_ready": False,
        }
        for i, edge in enumerate(edges)
    ]


def test_return_needs_every_allowed_edge_and_takes_the_max():
    full = return_rows(
        ["modal_preview"] * 5 + ["modal_warehouse"] * 5 + ["preview_warehouse"] * 5
    )
    entry = mw.summarize_return_kind(full, default_floor_ms=600)

    assert entry["status"] == "calibrated"
    assert entry["edge_floors"] == {
        "modal_preview": 410, "modal_warehouse": 410, "preview_warehouse": 420,
    }
    assert entry["floor_ms"] == 420

    missing = return_rows(["modal_preview"] * 5 + ["modal_warehouse"] * 5)
    short = mw.summarize_return_kind(missing, default_floor_ms=600)
    assert short["status"] == "insufficient"
    assert short["edge_floors"]["preview_warehouse"] is None
    assert short["floor_ms"] == 600


def test_return_times_out_on_same_layer_and_keeps_default():
    rows = return_rows(["modal_preview"] * 5) + [{
        "sample_id": "return-99", "edge": None, "valid_for_floor": False,
        "t_stable_ms": None, "timed_out": True, "false_ready": False,
    }]
    entry = mw.summarize_return_kind(rows, default_floor_ms=600)

    assert entry["status"] == "failed"
    assert entry["reason_codes"] == ["timeout"]
    assert entry["floor_ms"] == 600


def test_build_summary_covers_six_kinds_and_keeps_passive_defaults():
    summary = mw.build_summary(
        select=mw.summarize_select_kind(select_rows(distinct_pairs(20)), default_floor_ms=500),
        transitions={
            "open_preview": mw.summarize_transition(
                "open_preview", transition_rows(5), default_floor_ms=900
            ),
            "open_upgrade": mw.summarize_transition(
                "open_upgrade", transition_rows(5), default_floor_ms=1500
            ),
        },
        returns=mw.summarize_return_kind(
            return_rows(
                ["modal_preview"] * 5 + ["modal_warehouse"] * 5 + ["preview_warehouse"] * 5
            ),
            default_floor_ms=600,
        ),
        identities={},
    )

    assert set(summary) == set(mw.readiness.KINDS)
    assert summary["material_ready"]["status"] == "measured_only"
    assert summary["material_ready"]["reason_codes"] == ["consumption_measured_only"]
    assert summary["material_ready"]["floor_ms"] == 600
    assert summary["upgrade_result"]["floor_ms"] == 1500
    for kind in mw.readiness.KINDS:
        assert summary[kind]["policy"]["poll_ms"] == 50
        assert summary[kind]["policy"]["stable_frames"] == 2
        assert "report_sha256" not in summary[kind]["measurement"]


def test_report_digest_ignores_self_reference_and_written_state():
    report = mw.selection_conflict_report(
        environment=None,
        selection_evaluation=mw.SelectionEvaluation(False, ("selection_conflict",), None),
        identity={},
    )
    digest_before = mw.calibration_report_sha256(report)

    report["profile_sha256"] = "deadbeef"
    report["report_sha256"] = "cafebabe"
    report["passed"] = True
    assert mw.calibration_report_sha256(report) == digest_before


def test_policies_from_summary_use_floors_with_fixed_schedule():
    summary = {
        kind: {"floor_ms": 700, "status": "calibrated"} for kind in mw.readiness.KINDS
    }
    policies = mw.policies_from_summary(summary)

    assert set(policies) == set(mw.readiness.KINDS)
    for kind, policy in policies.items():
        assert policy.min_wait_ms == 700
        assert policy.poll_ms == 50
        assert policy.stable_frames == 2
        assert policy.timeout_ms == mw.readiness.default_policy(kind).timeout_ms


def test_policies_from_summary_keeps_only_calibrated_kinds():
    """§6.4：档案仅存已完成标定的 kind；失败/不足/被动观测从两侧省略、运行时用默认。"""
    summary = {
        "select_disc": {"floor_ms": 1560, "status": "calibrated"},
        "open_preview": {"floor_ms": 1260, "status": "calibrated"},
        "open_upgrade": {"floor_ms": 1500, "status": "failed"},
        "material_ready": {"floor_ms": 600, "status": "measured_only"},
        "upgrade_result": {"floor_ms": 1500, "status": "measured_only"},
        "return_warehouse": {"floor_ms": 600, "status": "insufficient"},
    }
    policies = mw.policies_from_summary(summary)

    assert set(policies) == {"select_disc", "open_preview"}
    assert [policy.min_wait_ms for policy in policies.values()] == [1560, 1260]


def test_measurements_carry_the_report_digest_for_calibrated_kinds():
    summary = {
        kind: {"floor_ms": 600, "status": "calibrated",
               "measurement": {"count": 3, "max_stable_ms": 120.0}}
        for kind in mw.readiness.KINDS
    }
    summary["open_upgrade"] = {"floor_ms": 1500, "status": "failed",
                               "measurement": {"count": 0, "max_stable_ms": None}}
    measurements = mw.measurements_from_summary(summary, "abc123")

    assert set(measurements) == set(mw.readiness.KINDS) - {"open_upgrade"}
    assert all(body["report_sha256"] == "abc123" for body in measurements.values())
    assert all(body["floor_ms"] == 600 for body in measurements.values())


def _env():
    return {
        "machine_id": "machine-1",
        "client_width": 1920,
        "client_height": 1080,
        "window_dpi": 96,
        "game_exe_path": r"C:\game\zzz.exe",
        "game_build": "build-1",
    }


def test_profile_written_from_mixed_summary_is_loadable_and_applies_floors(tmp_path):
    """写出→加载回环：混合状态 summary 只写已标定 kind，加载后下限真正生效（修 R6 回归）。"""
    summary = {
        "select_disc": {"floor_ms": 1560, "status": "calibrated",
                        "measurement": {"count": 20, "max_stable_ms": 1455.1}},
        "open_preview": {"floor_ms": 1260, "status": "calibrated",
                         "measurement": {"count": 5, "max_stable_ms": 1157.4}},
        "open_upgrade": {"floor_ms": 1500, "status": "failed",
                         "measurement": {"count": 0, "max_stable_ms": None}},
        "material_ready": {"floor_ms": 600, "status": "measured_only",
                           "measurement": {"count": 0, "max_stable_ms": None}},
        "upgrade_result": {"floor_ms": 1500, "status": "measured_only",
                           "measurement": {"count": 0, "max_stable_ms": None}},
        "return_warehouse": {"floor_ms": 600, "status": "insufficient",
                             "measurement": {"count": 5, "max_stable_ms": 399.1}},
    }
    path = tmp_path / "scan-waits.json"
    policies = mw.policies_from_summary(summary)
    measurements = mw.measurements_from_summary(summary, "abc123")
    assert set(policies) == set(measurements) == {"select_disc", "open_preview"}

    assert mw.readiness.write_wait_profile(_env(), policies, measurements, path=path).status == "ok"
    loaded = mw.readiness.load_wait_policies(_env(), path=path)
    assert loaded["select_disc"].min_wait_ms == 1560
    assert loaded["open_preview"].min_wait_ms == 1260
    assert loaded["open_upgrade"] == mw.readiness.default_policy("open_upgrade")
    assert loaded["material_ready"] == mw.readiness.default_policy("material_ready")
    assert loaded["upgrade_result"] == mw.readiness.default_policy("upgrade_result")


def test_profile_without_calibrated_kinds_is_empty_and_legal(tmp_path):
    """全部 kind 未完成标定时，空档案合法：两侧省略、加载全用默认（§6.4）。"""
    summary = {
        kind: {"floor_ms": mw.readiness.DEFAULT_FLOOR_MS[kind], "status": "insufficient",
               "measurement": {"count": 0, "max_stable_ms": None}}
        for kind in mw.readiness.KINDS
    }
    policies = mw.policies_from_summary(summary)
    measurements = mw.measurements_from_summary(summary, "abc123")
    assert policies == {} and measurements == {}

    path = tmp_path / "scan-waits.json"
    assert mw.readiness.write_wait_profile(_env(), policies, measurements, path=path).status == "ok"
    loaded = mw.readiness.load_wait_policies(_env(), path=path)
    assert all(loaded[kind] == mw.readiness.default_policy(kind) for kind in mw.readiness.KINDS)
