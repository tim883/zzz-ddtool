"""取消必须拦住下一次真实输入；所有 Windows 接口都用替身。"""

import threading
from unittest.mock import Mock

import pytest

from zzz_ddtool import hotkeys, wininput
from zzz_ddtool.control import checkpoint_input, controlled_inputs, validated_inputs
from zzz_ddtool.hotkeys import HotkeyController, RunnerStopped


@pytest.mark.parametrize("action", [
    lambda: wininput.focus_window(1),
    lambda: wininput.click_client(1, 100, 100, alt=True),
    lambda: wininput.wheel_client(1, 100, 100),
    lambda: wininput.send_wheel_event(-3),
    lambda: wininput.press_key_once(1, wininput.VK_R),
    lambda: wininput.press_key(1),
])
def test_stop_blocks_input_before_any_windows_call(monkeypatch, action):
    windows = Mock()
    monkeypatch.setattr(wininput, "user32", windows)
    monkeypatch.setattr(wininput, "kernel32", Mock())
    stop = Mock(side_effect=RunnerStopped("test stop"))
    validate = Mock()
    with controlled_inputs(stop), validated_inputs(validate), pytest.raises(RunnerStopped):
        action()
    assert not windows.mock_calls
    # 异常退出后不能把已停止任务的回调泄漏给其他操作。
    checkpoint_input()
    stop.assert_called_once()
    validate.assert_not_called()


def test_input_validation_runs_after_control_and_is_context_local():
    order = []
    with controlled_inputs(lambda: order.append("control")), validated_inputs(lambda: order.append("outer")):
        checkpoint_input()
        with pytest.raises(ValueError), validated_inputs(lambda: (_ for _ in ()).throw(ValueError("invalid"))):
            checkpoint_input()
        thread = threading.Thread(target=checkpoint_input)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        checkpoint_input()
    checkpoint_input()
    assert order == ["control", "outer", "control", "control", "outer"]


def test_pause_then_stop_does_not_send_pending_wheel(monkeypatch):
    monkeypatch.setattr(hotkeys.user32, "GetAsyncKeyState", lambda vk: 0)
    windows = Mock()
    monkeypatch.setattr(wininput, "user32", windows)
    ctrl = HotkeyController()
    ctrl.toggle_pause()
    entered = threading.Event()
    done = threading.Event()
    outcomes = []

    def checkpoint():
        entered.set()
        ctrl.checkpoint()

    def worker():
        try:
            with controlled_inputs(checkpoint):
                wininput.send_wheel_event(-3)
        except RunnerStopped:
            outcomes.append("stopped")
        finally:
            done.set()

    thread = threading.Thread(target=worker, daemon=True)
    try:
        thread.start()
        assert entered.wait(2)
        assert not done.wait(0.15)
        assert not windows.mock_calls
        ctrl.request_stop()
        assert done.wait(2)
        assert outcomes == ["stopped"]
        assert not windows.mock_calls
    finally:
        ctrl.request_stop()
        thread.join(2)
        ctrl.close()


def test_task_control_does_not_leak_to_another_thread():
    checkpoint = Mock(side_effect=RunnerStopped("other task"))
    done = threading.Event()
    with controlled_inputs(checkpoint):
        thread = threading.Thread(target=lambda: (checkpoint_input(), done.set()), daemon=True)
        thread.start()
        assert done.wait(2)
        thread.join(2)
    checkpoint.assert_not_called()


def test_single_action_reuses_observation_only_for_same_pause_generation():
    generation = [0]
    validate = Mock()
    with controlled_inputs(lambda: generation[0]), validated_inputs(validate, reuse_unchanged=True):
        checkpoint_input()
        checkpoint_input()
        assert validate.call_count == 1
        generation[0] += 2  # 暂停、恢复发生在两检查点之间，也必须重新观察
        checkpoint_input()
        assert validate.call_count == 2
    with controlled_inputs(lambda: generation[0]), validated_inputs(validate, reuse_unchanged=True):
        checkpoint_input()  # 下一个动作绝不能复用前一个动作的观察
    assert validate.call_count == 3


def test_unknown_generation_keeps_every_boundary_validation():
    validate = Mock()
    with controlled_inputs(lambda: None), validated_inputs(validate, reuse_unchanged=True):
        checkpoint_input()
        checkpoint_input()
    assert validate.call_count == 2


def test_generation_change_rejects_internal_boundary_before_native_input():
    generation = [0]
    validate = Mock(side_effect=[None, ValueError("changed page")])
    native = Mock()
    with controlled_inputs(lambda: generation[0]), validated_inputs(validate, reuse_unchanged=True):
        checkpoint_input()
        generation[0] = 2
        with pytest.raises(ValueError, match="changed page"):
            checkpoint_input()
            native()
    native.assert_not_called()
