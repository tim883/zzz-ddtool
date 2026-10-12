"""actionwait 与输入层释放契约的离线验收（spec §2.1—§2.2、§3、§4.1、§5、§9 T1—T8 的 S2 部分）。

全部替身：不发真实输入、不睡真实时间、不碰真实档案（档案路径单独指到 tmp）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from zzz_ddtool import actionwait, control, wininput
from zzz_ddtool.actionwait import (
    ActionWaitPolicy,
    InputRelease,
    compute_floor_ms,
    load_policies,
    load_policies_with_edges,
    policy_for,
    release_waits,
    wait_release,
    write_profile,
)
from zzz_ddtool.hotkeys import RunnerStopped


class FakeTime:
    def __init__(self, start: float = 1.0) -> None:
        self.now = float(start)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def monotonic_ns(self) -> int:
        return int(round(self.now * 1_000_000_000))

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += float(seconds)


def _env(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        machine_id="M1",
        client_width=1920,
        client_height=1080,
        window_dpi=96,
        game_exe_path=r"D:\game\zzz.exe",
        game_build="build-1",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _env_block(env: SimpleNamespace) -> dict[str, object]:
    return {
        "machine_id": env.machine_id,
        "client_width": env.client_width,
        "client_height": env.client_height,
        "window_dpi": env.window_dpi,
        "game_exe_path": env.game_exe_path,
        "game_build": env.game_build,
    }


def _setup_no_windows(monkeypatch, *, patch_pause: bool = True) -> Mock:
    windows = Mock()
    windows.MapVirtualKeyW.return_value = wininput.SCAN_ESCAPE
    monkeypatch.setattr(wininput, "user32", windows)
    monkeypatch.setattr(wininput, "kernel32", Mock())
    monkeypatch.setattr(wininput, "focus_window", lambda hwnd: None)
    monkeypatch.setattr(wininput, "_send", lambda inputs: None)
    if patch_pause:
        monkeypatch.setattr(wininput, "pause", lambda *a, **k: None)
    return windows


def _freeze(monkeypatch, fake: FakeTime, *modules) -> None:
    for module in modules:
        monkeypatch.setattr(module, "time", fake)


# ---------------------------------------------------------------------------
# T1 / T2：取时点与剩余预算
# ---------------------------------------------------------------------------


def test_t1_release_timestamp_is_taken_at_mouse_up(monkeypatch):
    fake = FakeTime(0.92)  # 按住 0.08 后松开，UP 时钟恰在 1000 ms
    _setup_no_windows(monkeypatch)
    monkeypatch.setattr(wininput, "time", fake)
    monkeypatch.setattr(wininput, "_post_release_wait", lambda seconds: None)

    release = wininput.click_client(1, 100, 200)
    assert isinstance(release, InputRelease)
    assert release.api == "click_client"
    assert release.released_at_ns == 1_000_000_000


def test_t1_wait_burns_down_from_release_not_from_input_return(monkeypatch):
    fake = FakeTime(1.0)
    _freeze(monkeypatch, fake, actionwait, control)
    release = InputRelease(api="click_client", released_at_ns=1_000_000_000)
    fake.now += 0.08  # 释放后的日志/取证已消耗 80 ms

    wait_release("slot", release, fallback_ms=120)
    assert fake.sleeps, "应分片睡剩余预算"
    assert sum(fake.sleeps) == pytest.approx(0.04, abs=1e-6)
    assert all(seconds <= 0.02 + 1e-9 for seconds in fake.sleeps)
    # 以输入返回为起点再睡满 120 是错误实现：这里绝不能等于 0.12。
    assert sum(fake.sleeps) < 0.12


def test_t2_elapsed_floor_requests_no_sleep(monkeypatch):
    fake = FakeTime(1.0)
    _freeze(monkeypatch, fake, actionwait, control)
    release = InputRelease(api="click_client", released_at_ns=1_000_000_000)
    fake.now += 0.18  # 业务已抓图/识别到 1180 ms，floor=120 已满足

    wait_release("slot", release, fallback_ms=120)
    assert fake.sleeps == []


# ---------------------------------------------------------------------------
# T3 / T4：输入层收尾确定值与所有权
# ---------------------------------------------------------------------------


def test_t3_tail_values_and_no_random_after_release(monkeypatch):
    _setup_no_windows(monkeypatch, patch_pause=False)
    events: list[object] = []
    monkeypatch.setattr(wininput, "uniform", lambda lo, hi: events.append("uniform") or 0.0)
    monkeypatch.setattr(wininput, "_post_release_wait", lambda seconds: events.append(("tail", seconds)))
    monkeypatch.setattr(wininput, "randint", lambda lo, hi: 0)
    monkeypatch.setattr(wininput, "client_to_screen", lambda hwnd, x, y: (x, y))

    # click：释放前 1 次随机移动等待；释放后只有 180 ms 确定收尾。
    wininput.click_client(1, 100, 100)
    assert events == ["uniform", ("tail", 0.18)]
    events.clear()

    # click(alt)：释放前 3 次（Alt 前置、移光标、Alt 释放前）；释放后同样只有 180。
    wininput.click_client(1, 100, 100, alt=True)
    assert events == ["uniform", "uniform", "uniform", ("tail", 0.18)]
    events.clear()

    # wheel：释放后 330 ms；滚轮 delta 仍按格数×120。
    windows = wininput.user32
    wininput.wheel_client(1, 100, 100, delta=-4)
    assert events == ["uniform", ("tail", 0.33)]
    wheel_args = [call.args for call in windows.mouse_event.call_args_list if call.args[0] == wininput.MOUSEEVENTF_WHEEL]
    assert wheel_args == [(wininput.MOUSEEVENTF_WHEEL, 0, 0, -480, 0)]
    events.clear()

    # press_key_once：释放后 580 ms；press_key：释放后 650 ms。
    wininput.press_key_once(1, wininput.VK_R)
    assert events == [("tail", 0.58)]
    events.clear()
    wininput.press_key(1)
    assert events == [("tail", 0.65)]
    events.clear()

    # raw 滚轮：零收尾，不产生任何收尾请求。
    wininput.send_wheel_event(-3)
    assert events == []


def test_t3_release_order_keeps_native_steps(monkeypatch):
    windows = _setup_no_windows(monkeypatch)
    monkeypatch.setattr(wininput, "_post_release_wait", lambda seconds: None)
    monkeypatch.setattr(wininput, "randint", lambda lo, hi: 0)
    monkeypatch.setattr(wininput, "client_to_screen", lambda hwnd, x, y: (x, y))
    focus_calls = []
    monkeypatch.setattr(wininput, "focus_window", lambda hwnd: focus_calls.append(hwnd))

    wininput.click_client(7, 10, 20)
    names = [call[0] for call in windows.mock_calls if call[0] in {"SetCursorPos", "mouse_event"}]
    assert names == ["SetCursorPos", "mouse_event", "mouse_event"]
    down, up = (call.args for call in windows.mouse_event.call_args_list)
    assert down[0] == wininput.MOUSEEVENTF_LEFTDOWN and up[0] == wininput.MOUSEEVENTF_LEFTUP
    assert focus_calls == [7]


def test_t4_defer_input_layer_requests_zero_and_business_consumes_once(monkeypatch):
    fake = FakeTime(0.92)
    _freeze(monkeypatch, fake, wininput, actionwait, control)
    _setup_no_windows(monkeypatch)
    tails: list[float] = []
    monkeypatch.setattr(wininput, "_post_release_wait", lambda seconds: tails.append(seconds))

    with wininput.input_context(
        "set_option", target="X", stage_index=1, selected=True, defer_post_release=True
    ):
        release = wininput.click_client(1, 5, 6)
    assert tails == [], "业务接管时输入层不得再睡收尾"

    hold_sleep = sum(fake.sleeps)  # 释放前的按住等待，不计入业务预算
    wait_release("set_option", release, fallback_ms=120)
    business = sum(fake.sleeps) - hold_sleep
    assert business == pytest.approx(0.12, abs=1e-6), "业务总预算 120 ms，不叠加 180"


def test_policy_for_archive_and_measurement_context(monkeypatch, tmp_path):
    env = _env()
    profile = tmp_path / "post-release-waits.json"
    write_profile(
        env,
        {"open_panel": {"count": 10, "max_stable_ms": 100, "floor_ms": 120, "report_sha256": "a" * 64}},
        path=profile,
    )
    monkeypatch.setattr(actionwait, "profile_path", lambda: profile)

    with release_waits(env):
        assert policy_for("open_panel", fallback_ms=730) == ActionWaitPolicy(120, "measured")
        assert policy_for("reset", fallback_ms=630) == ActionWaitPolicy(630, "default")
        assert policy_for("reset", fallback_ms=630, allow_measured=False) == ActionWaitPolicy(630, "default")

    with release_waits(env, measure_action="open_panel"):
        assert policy_for("open_panel", fallback_ms=730) == ActionWaitPolicy(0, "measurement")
        assert policy_for("reset", fallback_ms=630) == ActionWaitPolicy(630, "default")

    with pytest.raises(ValueError):
        policy_for("no_such_action", fallback_ms=1)
    with pytest.raises(ValueError):
        policy_for("open_panel", fallback_ms=True)
    with pytest.raises(ValueError):
        policy_for("open_panel", fallback_ms=-1)
    with pytest.raises(ValueError):
        policy_for("open_panel", fallback_ms=1, allow_measured=1)


# ---------------------------------------------------------------------------
# T5：Alt / 多通道 Esc 的释放点
# ---------------------------------------------------------------------------


def test_t5_alt_and_esc_release_points(monkeypatch):
    fake = FakeTime(1.0)
    _setup_no_windows(monkeypatch)
    monkeypatch.setattr(wininput, "time", fake)
    tail_seen: list[int] = []
    monkeypatch.setattr(
        wininput, "_post_release_wait", lambda seconds: tail_seen.append(fake.monotonic_ns())
    )

    def send_advance(inputs):
        fake.now += 0.04

    monkeypatch.setattr(wininput, "_send", send_advance)
    release = wininput.click_client(1, 10, 20, alt=True)
    # 2000 ms 起：Alt 前置 _send(+40) → 按住 80 → UP → Alt KEYUP _send(+40)。
    assert release.released_at_ns == 1_160_000_000
    assert tail_seen == [1_160_000_000], "取时必须早于收尾等待"

    posts: list[int] = []

    def post_message(hwnd, msg, vk, lparam):
        fake.now = 1.05 if not posts else 1.10
        posts.append(fake.now)

    monkeypatch.setattr(wininput.user32, "PostMessageW", post_message)
    monkeypatch.setattr(wininput, "_send", lambda inputs: None)
    escape = wininput.press_key(1)
    assert escape.released_at_ns == 1_100_000_000  # 以最后一条 PostMessageW(WM_KEYUP) 取时
    assert len(posts) == 2


def test_t5_failure_before_final_release_keeps_null_and_propagates(monkeypatch):
    fake = FakeTime(1.0)
    _setup_no_windows(monkeypatch)
    monkeypatch.setattr(wininput, "time", fake)

    calls: list[int] = []

    def send_fail(inputs):
        calls.append(1)
        if len(calls) == 2:  # Alt KEYUP 那一发失败：释放点尚未到
            raise wininput.InputError("Alt 释放失败")

    monkeypatch.setattr(wininput, "_send", send_fail)
    records: list[dict[str, object]] = []
    with wininput.capture_inputs(records.append):
        with pytest.raises(wininput.InputError):
            wininput.click_client(1, 10, 20, alt=True)
    assert [r["status"] for r in records] == ["started", "failed"]
    assert records[0]["released_at_ns"] is None
    assert records[1]["released_at_ns"] is None


# ---------------------------------------------------------------------------
# T6：余量公式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stable", "floor"),
    [
        ([100], 120),
        ([141], 170),
        ([501], 560),
        ([100] * 9 + [1000], 1100),
    ],
)
def test_t6_floor_formula(stable, floor):
    assert compute_floor_ms(stable) == floor


@pytest.mark.parametrize("bad", [[], [True], [-1], [float("nan")], [1.5], ["1"]])
def test_t6_floor_rejects_invalid(bad):
    with pytest.raises(ValueError):
        compute_floor_ms(bad)


# ---------------------------------------------------------------------------
# T7：档案装载与写入
# ---------------------------------------------------------------------------


def _payload(env: SimpleNamespace) -> dict[str, object]:
    return {
        "schema_version": 2,
        "time_origin": "native_release",
        "predicate_version": 2,
        "environment": _env_block(env),
        "policies": {
            "open_panel": {"count": 10, "max_stable_ms": 100, "floor_ms": 120, "report_sha256": "a" * 64}
        },
    }


def test_t7_roundtrip_and_whole_file_invalidation(monkeypatch, tmp_path):
    env = _env()
    profile = tmp_path / "post-release-waits.json"
    result = write_profile(
        env,
        {"open_panel": {"count": 10, "max_stable_ms": 100, "floor_ms": 120, "report_sha256": "a" * 64}},
        path=profile,
    )
    assert result["status"] == "written" and isinstance(result["sha256"], str)
    assert load_policies(env, path=profile) == {"open_panel": ActionWaitPolicy(120, "measured")}

    mutations = {
        "extra-key": lambda p: p.update({"extra": 1}),
        "predicate-version": lambda p: p.update({"predicate_version": 1}),
        "time-origin": lambda p: p.update({"time_origin": "input_return"}),
        "count-9": lambda p: p["policies"]["open_panel"].update({"count": 9}),
        "formula": lambda p: p["policies"]["open_panel"].update({"floor_ms": 130}),
        "bool-floor": lambda p: p["policies"]["open_panel"].update({"floor_ms": True}),
        "bad-sha": lambda p: p["policies"]["open_panel"].update({"report_sha256": "zz"}),
        "unknown-action": lambda p: p["policies"].update({"nope": p["policies"]["open_panel"]}),
        "extra-policy-field": lambda p: p["policies"]["open_panel"].update({"x": 1}),
        "missing-field": lambda p: p.pop("time_origin"),
    }
    for name, mutate in mutations.items():
        payload = json.loads(json.dumps(_payload(env)))
        mutate(payload)
        path2 = tmp_path / f"{name}.json"
        path2.write_text(json.dumps(payload), encoding="utf-8")
        assert load_policies(env, path=path2) == {}, name

    path3 = tmp_path / "broken.json"
    path3.write_text("{not json", encoding="utf-8")
    assert load_policies(env, path=path3) == {}

    other = _env(game_build="build-2")
    assert load_policies(other, path=profile) == {}


def test_t7_floor_over_timeout_rejects_only_that_action(monkeypatch, tmp_path):
    env = _env()
    payload = _payload(env)
    payload["policies"]["open_panel"] = {
        "count": 10, "max_stable_ms": 2800, "floor_ms": 3080, "report_sha256": "a" * 64,
    }
    payload["policies"]["select_disc"] = {
        "count": 10, "max_stable_ms": 2900, "floor_ms": 3190, "report_sha256": "b" * 64,
    }
    payload["policies"]["reset"] = {
        "count": 10, "max_stable_ms": 100, "floor_ms": 120, "report_sha256": "c" * 64,
    }
    path = tmp_path / "bounds.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_policies(env, path=path)
    assert set(loaded) == {"reset"}, "越界动作按缺项 fallback，其余动作照常"

    payload["policies"] = {
        "reset": {"count": 9, "max_stable_ms": 100, "floor_ms": 120, "report_sha256": "c" * 64}
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_policies(env, path=path) == {}, "count<10 仍是整档失效"


def test_t7_write_failure_keeps_original_and_not_persistable(monkeypatch, tmp_path):
    env = _env()
    dest = tmp_path / "post-release-waits.json"
    dest.write_bytes(b"old-bytes")
    monkeypatch.setattr(actionwait.os, "replace", Mock(side_effect=OSError("disk full")))
    result = write_profile(
        env,
        {"open_panel": {"count": 10, "max_stable_ms": 100, "floor_ms": 120, "report_sha256": "a" * 64}},
        path=dest,
    )
    assert result["status"] == "write_failed" and result["sha256"] is None
    assert dest.read_bytes() == b"old-bytes"
    assert not dest.with_name(dest.name + ".tmp").exists()

    incomplete = _env(machine_id=None)
    assert write_profile(incomplete, {}, path=dest)["status"] == "not_persistable"
    assert load_policies(incomplete, path=dest) == {}


# ---------------------------------------------------------------------------
# T8：暂停扣除、停止传播与“不重复输入前复验”
# ---------------------------------------------------------------------------


def test_t8_pause_deduction_and_no_input_validation(monkeypatch):
    fake = FakeTime(1.0)
    _freeze(monkeypatch, fake, actionwait, control)
    checks: list[int] = []
    validate = Mock()

    def checkpoint() -> int:
        checks.append(1)
        return 0

    def pauses(since: float, until: float):
        return ((1.01, 2.01),) if until >= 2.01 and since <= 1.01 else ()

    release = InputRelease(api="open_panel", released_at_ns=1_000_000_000)
    fake.now = 2.01  # 1010 ms 暂停到 2010 ms

    with control.controlled_inputs(checkpoint, pauses=pauses), control.validated_inputs(validate):
        wait_release("open_panel", release, fallback_ms=120)

    assert sum(fake.sleeps) == pytest.approx(0.11, abs=1e-6), "恢复后尚需 110 ms 活动时间"
    assert all(seconds <= 0.02 + 1e-9 for seconds in fake.sleeps)
    assert len(checks) >= 2, "每个等待分片前后都要过控制回调"
    validate.assert_not_called()


def test_t9_write_profile_installs_return_edges(tmp_path):
    """Spec v9 §12：逐边项写进根字段 return_edges，并按逐边下限装载（≥5 样本）。"""
    env = _env()
    profile = tmp_path / "post-release-waits.json"
    edge_item = {"count": 5, "max_stable_ms": 60, "floor_ms": 80, "report_sha256": "d" * 64}
    result = write_profile(
        env,
        {"open_panel": {"count": 10, "max_stable_ms": 100, "floor_ms": 120,
                        "report_sha256": "a" * 64}},
        path=profile, return_edges={"preview_warehouse": edge_item},
    )
    assert result["status"] == "written"
    raw = json.loads(profile.read_text(encoding="utf-8"))
    assert set(raw) == {"schema_version", "time_origin", "predicate_version",
                        "environment", "policies", "return_edges"}
    assert raw["schema_version"] == 2
    assert raw["return_edges"] == {"preview_warehouse": edge_item}

    policies, edges = load_policies_with_edges(env, path=profile)
    assert policies == {"open_panel": ActionWaitPolicy(120, "measured")}
    assert edges == {"preview_warehouse": ActionWaitPolicy(80, "measured")}


def test_t9_return_edges_reject_unknown_edge_and_bad_items(tmp_path):
    """未知边名写不出去；单边样本不足（<5）或越界只丢该边，不牵连其他边。"""
    env = _env()
    profile = tmp_path / "bad-edge.json"
    assert write_profile(
        env, {}, path=profile, return_edges={"nope": {"count": 5, "max_stable_ms": 1,
                                                      "floor_ms": 50, "report_sha256": "e" * 64}},
    )["status"] == "write_failed"

    payload = _payload(env)
    payload["return_edges"] = {
        "preview_warehouse": {"count": 4, "max_stable_ms": 60, "floor_ms": 90,
                              "report_sha256": "d" * 64},   # 样本不足
        "modal_preview": {"count": 5, "max_stable_ms": 2000, "floor_ms": 4000,
                          "report_sha256": "f" * 64},        # 越界（>=3580 timeout）
        "modal_warehouse": {"count": 5, "max_stable_ms": 100, "floor_ms": 120,
                            "report_sha256": "0" * 64},      # 合格
    }
    path2 = tmp_path / "partial-edges.json"
    path2.write_text(json.dumps(payload), encoding="utf-8")
    policies, edges = load_policies_with_edges(env, path=path2)
    assert policies == {"open_panel": ActionWaitPolicy(120, "measured")}
    assert edges == {"modal_warehouse": ActionWaitPolicy(120, "measured")}


def test_t7_stop_during_shard_propagates_without_more_input(monkeypatch):
    fake = FakeTime(1.0)
    _freeze(monkeypatch, fake, actionwait, control)
    calls = {"n": 0}

    def checkpoint() -> int:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RunnerStopped("F10 请求停止")
        return 0

    release = InputRelease(api="open_panel", released_at_ns=1_000_000_000)
    with control.controlled_inputs(checkpoint):
        with pytest.raises(RunnerStopped):
            wait_release("open_panel", release, fallback_ms=120)
    assert fake.sleeps == [pytest.approx(0.02)], "停止后的分片不再追加"
