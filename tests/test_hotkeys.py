"""F9/F10 热键控制器的无键盘单测。

只测状态机（paused/stopped/checkpoint），不模拟真实按键——
GetAsyncKeyState 的全局行为不适合在自动化测试里碰。
"""

from __future__ import annotations

import threading

import pytest

from zzz_ddtool.hotkeys import HotkeyController, RunnerStopped


def test_checkpoint_passes_when_idle() -> None:
    ctrl = HotkeyController()
    try:
        ctrl.checkpoint()  # 不暂停不停止，应直接通过
        assert not ctrl.paused
        assert not ctrl.stopped
    finally:
        ctrl.close()


def test_pause_blocks_until_resumed() -> None:
    ctrl = HotkeyController()
    try:
        ctrl.paused = True
        done = threading.Event()

        def worker():
            ctrl.checkpoint()
            done.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert not done.wait(0.3), "暂停中 checkpoint 不应放行"
        ctrl.paused = False
        assert done.wait(2), "恢复后 checkpoint 应放行"
    finally:
        ctrl.close()


def test_stop_raises_runner_stopped() -> None:
    ctrl = HotkeyController()
    try:
        ctrl.paused = True  # 停止优先于暂停：即使暂停中也应立刻抛
        ctrl.stopped = True
        with pytest.raises(RunnerStopped):
            ctrl.checkpoint()
    finally:
        ctrl.close()


def test_global_polling_edges_pause_resume_stop(monkeypatch):
    import time
    from zzz_ddtool import hotkeys

    pressed = set()
    monkeypatch.setattr(hotkeys.user32, "GetAsyncKeyState", lambda vk: 0x8000 if vk in pressed else 0)
    ctrl = HotkeyController(poll_interval=0.01)

    def wait_for(predicate):
        until = time.monotonic() + 2
        while not predicate() and time.monotonic() < until:
            time.sleep(0.01)
        assert predicate()

    try:
        pressed.add(hotkeys.VK_F9)
        wait_for(lambda: ctrl.paused)
        time.sleep(0.05)
        assert ctrl.paused  # 按住只触发一次
        assert ctrl.pause_generation == 1
        pressed.clear()
        wait_for(lambda: hotkeys.VK_F9 not in ctrl._pressed)
        pressed.add(hotkeys.VK_F9)
        wait_for(lambda: not ctrl.paused)
        assert ctrl.checkpoint() == 2
        pressed.add(hotkeys.VK_F10)
        wait_for(lambda: ctrl.stopped)
        with pytest.raises(RunnerStopped):
            ctrl.checkpoint()
    finally:
        ctrl.close()
