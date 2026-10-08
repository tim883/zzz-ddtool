"""wininput 输入观察入口（capture_inputs / input_context）的单元验收。

不发送任何真实系统输入：user32/kernel32、聚焦、_send、pause 与客户区坐标换算
全部换成替身。既有 test_input_control.py 的通行语义不受本文件影响。
"""

from __future__ import annotations

import threading
from unittest.mock import Mock

import pytest

from zzz_ddtool import wininput
from zzz_ddtool.hotkeys import RunnerStopped

# spec §8.4：sink 原始字典的字段与顺序固定。
FIELDS = (
    "t_ns",
    "event",
    "call_id",
    "api",
    "role",
    "target",
    "stage_index",
    "selected",
    "hwnd",
    "x",
    "y",
    "alt",
    "vk",
    "delta_units",
    "status",
    "error",
)

NO_CONTEXT = (None, None, None, None)


def _no_windows(monkeypatch) -> None:
    """所有真实输入通道换成替身。"""
    windows = Mock()
    windows.MapVirtualKeyW.return_value = wininput.SCAN_ESCAPE  # ctypes 需要真整数
    monkeypatch.setattr(wininput, "user32", windows)
    monkeypatch.setattr(wininput, "kernel32", Mock())
    monkeypatch.setattr(wininput, "focus_window", lambda hwnd: None)
    monkeypatch.setattr(wininput, "_send", lambda inputs: None)
    monkeypatch.setattr(wininput, "pause", lambda *a, **k: None)


def _collect(func, *args, **kwargs) -> list[dict[str, object]]:
    """在 capture_inputs 里跑一次输入调用，返回收到的原始记录。"""
    events: list[dict[str, object]] = []
    with wininput.capture_inputs(events.append):
        func(*args, **kwargs)
    return events


def _drive_all_entries() -> None:
    wininput.click_client(1, 100, 200)
    wininput.wheel_client(1, 100, 200, delta=-4)
    wininput.send_wheel_event(-3)
    wininput.press_key(1, 27)
    wininput.press_key_once(1, wininput.VK_R)


def _context(event: dict[str, object]) -> tuple:
    return (event["role"], event["target"], event["stage_index"], event["selected"])


def test_disabled_entries_emit_nothing(monkeypatch):
    """未启用 capture_inputs 时五个入口都不构造记录、不分配 call_id。"""
    _no_windows(monkeypatch)
    monkeypatch.setattr(
        wininput,
        "_new_record",
        Mock(side_effect=AssertionError("未启用 capture_inputs 时不得构造记录")),
    )
    _drive_all_entries()


def test_disabled_calls_do_not_consume_call_ids(monkeypatch):
    """未启用时连 call_id 都不分配，启用后的 ID 连续。"""
    _no_windows(monkeypatch)
    before = _collect(wininput.press_key, 1, 27)
    assert [e["event"] for e in before] == ["begin", "end"]

    _drive_all_entries()  # 仍未启用

    after = _collect(wininput.press_key, 1, 27)
    assert [e["event"] for e in after] == ["begin", "end"]
    assert after[0]["call_id"] == before[0]["call_id"] + 1


def test_click_client_records_one_pair_with_target_center(monkeypatch):
    _no_windows(monkeypatch)
    # 抖动用替身钉成 +2，屏幕换算再加 1000：记录必须是传入 API 的客户区中心。
    monkeypatch.setattr(wininput, "randint", lambda lo, hi: 2)
    monkeypatch.setattr(wininput, "client_to_screen", lambda hwnd, x, y: (x + 1000, y + 1000))

    events = _collect(wininput.click_client, 4321, 640, 360)
    assert [e["event"] for e in events] == ["begin", "end"]
    begin, end = events
    assert begin["api"] == end["api"] == "click_client"
    assert begin["call_id"] == end["call_id"]
    assert isinstance(begin["call_id"], int) and begin["call_id"] >= 1
    assert begin["status"] == "started" and begin["error"] is None
    assert end["status"] == "completed" and end["error"] is None
    for event in events:
        assert tuple(event) == FIELDS
        assert (event["hwnd"], event["x"], event["y"], event["alt"]) == (4321, 640, 360, False)
        assert event["vk"] is None and event["delta_units"] is None
        assert _context(event) == NO_CONTEXT
    assert isinstance(begin["t_ns"], int) and begin["t_ns"] > 0
    assert end["t_ns"] >= begin["t_ns"]

    with_alt = _collect(wininput.click_client, 4321, 640, 360, alt=True)
    assert [e["alt"] for e in with_alt] == [True, True]


def test_press_key_records_vk_and_one_pair(monkeypatch):
    _no_windows(monkeypatch)
    events = _collect(wininput.press_key, 9)
    assert [e["event"] for e in events] == ["begin", "end"]
    assert {e["api"] for e in events} == {"press_key"}
    assert {e["vk"] for e in events} == {27}  # 默认 VK_ESCAPE，含内部 _send 兼容发送仍只记一对
    assert {e["hwnd"] for e in events} == {9}
    assert {e["status"] for e in events} == {"started", "completed"}

    once = _collect(wininput.press_key_once, 9, wininput.VK_R)
    assert [e["event"] for e in once] == ["begin", "end"]
    assert {e["api"] for e in once} == {"press_key_once"}
    assert {e["vk"] for e in once} == {wininput.VK_R}


def test_wheel_client_records_delta_units(monkeypatch):
    _no_windows(monkeypatch)
    events = _collect(wininput.wheel_client, 77, 11, 22, delta=-5)
    assert [e["event"] for e in events] == ["begin", "end"]
    for event in events:
        assert event["api"] == "wheel_client"
        assert (event["hwnd"], event["x"], event["y"]) == (77, 11, 22)
        assert event["delta_units"] == -5
        assert event["alt"] is None and event["vk"] is None


def test_send_wheel_event_records_each_call(monkeypatch):
    _no_windows(monkeypatch)
    events: list[dict[str, object]] = []
    with wininput.capture_inputs(events.append):
        for _ in range(3):
            wininput.send_wheel_event(-3)
    assert [e["event"] for e in events] == ["begin", "end"] * 3
    assert {e["api"] for e in events} == {"send_wheel_event"}
    assert {e["delta_units"] for e in events} == {-3}
    for event in events:
        assert (event["hwnd"], event["x"], event["y"], event["alt"], event["vk"]) == (
            None, None, None, None, None,
        )
    begins = [e for e in events if e["event"] == "begin"]
    ends = [e for e in events if e["event"] == "end"]
    assert len({b["call_id"] for b in begins}) == 3
    assert [b["call_id"] for b in begins] == [e["call_id"] for e in ends]


def test_nested_capture_sinks_are_isolated(monkeypatch):
    _no_windows(monkeypatch)
    outer: list[dict[str, object]] = []
    inner: list[dict[str, object]] = []
    with wininput.capture_inputs(outer.append):
        wininput.press_key(1, 27)
        with wininput.capture_inputs(inner.append):
            wininput.press_key_once(1, wininput.VK_R)
        wininput.press_key(1, 27)

    assert [e["event"] for e in inner] == ["begin", "end"]  # 内层事件不到外层
    assert {e["api"] for e in inner} == {"press_key_once"}
    assert [e["event"] for e in outer] == ["begin", "end", "begin", "end"]
    assert {e["api"] for e in outer} == {"press_key"}
    begins = [e for e in outer + inner if e["event"] == "begin"]
    assert len({b["call_id"] for b in begins}) == 3


def test_input_context_metadata_and_restore(monkeypatch):
    _no_windows(monkeypatch)
    events: list[dict[str, object]] = []
    with wininput.capture_inputs(events.append):
        with wininput.input_context("set_option", target="河豚电音", stage_index=2, selected=True):
            wininput.click_client(1, 5, 6)
        wininput.press_key(1, 27)
        with wininput.input_context("substat_count", target=3, selected=False):
            wininput.wheel_client(1, 7, 8, delta=-2)
        with wininput.input_context("open_sets"):
            wininput.send_wheel_event(-3)

    assert _context(events[0]) == ("set_option", "河豚电音", 2, True)
    assert _context(events[1]) == ("set_option", "河豚电音", 2, True)  # end 保留同一调用参数
    assert _context(events[2]) == NO_CONTEXT
    assert _context(events[3]) == NO_CONTEXT
    assert _context(events[4]) == ("substat_count", 3, None, False)
    assert _context(events[5]) == ("substat_count", 3, None, False)
    assert _context(events[6]) == ("open_sets", None, None, None)

    after = _collect(wininput.press_key, 1, 27)
    assert _context(after[0]) == NO_CONTEXT


def test_nested_input_context_restores_outer(monkeypatch):
    _no_windows(monkeypatch)
    events: list[dict[str, object]] = []
    with wininput.capture_inputs(events.append):
        with wininput.input_context("set_option", target="外", stage_index=1, selected=True):
            with wininput.input_context("set_option", target="内", stage_index=2, selected=True):
                wininput.press_key(1, 27)
            wininput.press_key(1, 27)
    assert _context(events[0]) == ("set_option", "内", 2, True)
    assert _context(events[2]) == ("set_option", "外", 1, True)


def test_input_error_marks_failed_and_propagates(monkeypatch):
    _no_windows(monkeypatch)

    def boom(hwnd):
        raise wininput.InputError("焦点炸了")

    monkeypatch.setattr(wininput, "focus_window", boom)
    events: list[dict[str, object]] = []
    with wininput.capture_inputs(events.append):
        with pytest.raises(wininput.InputError):
            wininput.click_client(7, 10, 20)

    assert [e["event"] for e in events] == ["begin", "end"]
    begin, end = events
    assert begin["status"] == "started" and begin["error"] is None
    assert end["status"] == "failed" and end["error"] == "InputError"
    assert end["call_id"] == begin["call_id"]
    assert (end["hwnd"], end["x"], end["y"], end["alt"]) == (7, 10, 20, False)


def test_runner_stopped_in_call_marks_stopped_and_propagates(monkeypatch):
    _no_windows(monkeypatch)

    def boom(hwnd):
        raise RunnerStopped("F10 请求停止")

    monkeypatch.setattr(wininput, "focus_window", boom)
    events: list[dict[str, object]] = []
    with wininput.capture_inputs(events.append):
        with pytest.raises(RunnerStopped):
            wininput.press_key(3)

    assert [e["status"] for e in events] == ["started", "stopped"]
    assert events[1]["error"] == "RunnerStopped"
    assert events[1]["call_id"] == events[0]["call_id"]
    assert events[1]["vk"] == 27


def test_stop_at_initial_checkpoint_records_nothing(monkeypatch):
    _no_windows(monkeypatch)
    before = _collect(wininput.press_key, 1, 27)

    monkeypatch.setattr(
        wininput, "checkpoint_input", Mock(side_effect=RunnerStopped("F10 请求停止"))
    )
    sent = len(wininput.user32.mock_calls)
    events: list[dict[str, object]] = []
    with wininput.capture_inputs(events.append):
        with pytest.raises(RunnerStopped):
            wininput.click_client(1, 100, 200)

    assert events == []  # 未过初始检查点：不发 begin/end
    assert len(wininput.user32.mock_calls) == sent  # 也没有任何发送

    # 未发 begin 的调用不分配 call_id：下一次启用的 ID 只差 1
    monkeypatch.setattr(wininput, "checkpoint_input", lambda: None)
    after = _collect(wininput.press_key, 1, 27)
    assert after[0]["call_id"] == before[0]["call_id"] + 1


def test_zero_delta_wheel_event_rejected_before_any_record(monkeypatch):
    """delta_units=0 在检查点之前就拒绝（既有顺序不变），因此不产生记录。"""
    _no_windows(monkeypatch)
    events: list[dict[str, object]] = []
    with wininput.capture_inputs(events.append):
        with pytest.raises(wininput.InputError):
            wininput.send_wheel_event(0)
    assert events == []


def test_sink_error_does_not_replace_original_exception(monkeypatch):
    """回调在上报失败时自己出错，也必须让原异常原样传播。"""
    _no_windows(monkeypatch)

    def boom(hwnd):
        raise wininput.InputError("焦点炸了")

    monkeypatch.setattr(wininput, "focus_window", boom)

    def flaky_sink(record):
        if record["event"] == "end":
            raise RuntimeError("观察回调炸了")

    with pytest.raises(wininput.InputError):
        with wininput.capture_inputs(flaky_sink):
            wininput.press_key(1, 27)


def test_capture_and_context_restore_after_exception(monkeypatch):
    _no_windows(monkeypatch)
    monkeypatch.setattr(
        wininput, "focus_window", Mock(side_effect=RunnerStopped("F10 请求停止"))
    )
    events: list[dict[str, object]] = []
    with pytest.raises(RunnerStopped):
        with wininput.capture_inputs(events.append):
            with wininput.input_context("confirm_sets"):
                wininput.press_key(1, 27)
    assert [e["status"] for e in events] == ["started", "stopped"]
    assert _context(events[0]) == ("confirm_sets", None, None, None)

    # 异常退出后 sink 与上下文都恢复：后续输入不再产生事件、也不带旧角色
    monkeypatch.setattr(wininput, "focus_window", lambda hwnd: None)
    with wininput.capture_inputs(events.append):
        wininput.press_key(1, 27)
    assert [e["event"] for e in events[2:]] == ["begin", "end"]
    assert _context(events[2]) == NO_CONTEXT


def test_call_ids_unique_and_sinks_isolated_across_threads(monkeypatch):
    _no_windows(monkeypatch)
    results: dict[str, list[dict[str, object]]] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        events: list[dict[str, object]] = []
        results[name] = events
        with wininput.capture_inputs(events.append):
            barrier.wait(5)
            wininput.press_key(1, 27)

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert set(results) == {"t0", "t1"}
    for events in results.values():
        assert [e["event"] for e in events] == ["begin", "end"]
        assert events[0]["call_id"] == events[1]["call_id"]
    ids = [e["call_id"] for events in results.values() for e in events if e["event"] == "begin"]
    assert len(ids) == 2 and len(set(ids)) == 2
