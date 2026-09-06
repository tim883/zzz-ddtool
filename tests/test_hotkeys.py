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
