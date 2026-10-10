"""pytest 全局配置：钉死 Qt 平台，防止测试把窗口弹到桌面上。

**必须在任何测试模块 import 之前**把 ``QT_QPA_PLATFORM`` 设成 ``offscreen``。
否则 PySide6 会退回真实 Windows 平台，测试里 new 出来的 ``QWidget`` 就会真的
建桌面窗口、抢用户焦点。

2026-10-04 踩过：跑 ``pytest`` 时真的弹了窗口，把正在动鼠标的用户屏幕抢了。
根因是防弹窗只靠各测试文件在模块顶层写
``os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")``——
``setdefault`` 只在变量不存在时才设，一旦环境里已有该变量、或 import 顺序让
``QApplication`` 先建出来，它就**静默失效**，Qt 退回真实平台。

这里在收集测试前用**赋值**强制设置，不依赖 import 顺序。
要故意跑真窗口（例如验浮层）时，设 ``ZZZ_DDTOOL_TEST_WINDOW=1`` 放行。
"""

from __future__ import annotations

import os

import pytest

if os.environ.get("ZZZ_DDTOOL_TEST_WINDOW") != "1":
    os.environ["QT_QPA_PLATFORM"] = "offscreen"


def pytest_collection_modifyitems(items):
    """把真实 pytest node ID 写进 JUnit testcase property ``pytest_nodeid``（Spec §9）。

    阶段门禁按 node ID 精确匹配映射节点，不靠截断的 classname/name 猜。本 hook 只读写
    ``item.user_properties``，不 import Qt、不建窗口，保持 conftest 的零依赖约定。
    """
    for item in items:
        props = getattr(item, "user_properties", None)
        if props is None:
            item.user_properties = props = []
        props.append(("pytest_nodeid", item.nodeid))


# --------------------------------------------------------------------------
# 模拟时钟 / 控制器（Spec §8：必须由用例显式请求，不设全局 autouse）
# --------------------------------------------------------------------------
class SimClock:
    """测试用单调时钟；sleep 真正推进时间，不允许 no-op（Spec §8）。"""

    def __init__(self, start: float = 10_000.0) -> None:
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += float(seconds)
        return self.t

    def sleep(self, seconds: float) -> None:
        seconds = float(seconds)
        if seconds < 0:
            raise ValueError("模拟 sleep 不能为负")
        self.t += seconds


class FakeController:
    """模拟控制器：实现与 §6.1 相同的契约（checkpoint 返回代次；pause_intervals 只读）。"""

    def __init__(self, clock: SimClock) -> None:
        self.clock = clock
        self.stopped = False
        self.paused = False
        self.pause_generation = 0
        self._spans: list[tuple[float, float]] = []

    def checkpoint(self) -> int:
        if self.stopped:
            from zzz_ddtool.hotkeys import RunnerStopped

            raise RunnerStopped("F10 请求停止")
        return self.pause_generation

    def pause_intervals(self, since: float, until: float) -> tuple[tuple[float, float], ...]:
        clipped: list[tuple[float, float]] = []
        for start, end in self._spans:
            lo, hi = max(start, float(since)), min(end, float(until))
            if hi > lo:
                clipped.append((lo, hi))
        clipped.sort()
        merged: list[tuple[float, float]] = []
        for start, end in clipped:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return tuple(merged)

    def simulate_pause(self, seconds: float) -> None:
        """记录一次真实暂停阻塞并推进代次（时钟随之前进）。"""
        start = self.clock.now()
        self.clock.advance(seconds)
        self._spans.append((start, self.clock.now()))
        self.pause_generation += 1

    def request_stop(self) -> None:
        self.stopped = True


@pytest.fixture
def sim_clock(monkeypatch):
    """把 readiness 的时钟/睡眠替换成 SimClock（只在显式请求时生效）。"""
    from zzz_ddtool import readiness

    clock = SimClock()
    monkeypatch.setattr(readiness, "_now", clock.now)
    monkeypatch.setattr(readiness, "_sleep", clock.sleep)
    return clock


@pytest.fixture
def fake_controller(sim_clock):
    return FakeController(sim_clock)

