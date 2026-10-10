"""F9 暂停 / F10 停止 全局热键。

用 GetAsyncKeyState 在后台守护线程里轮询（按边沿触发，按住只算一次），
不依赖窗口焦点，游戏在前台时也能响应。

Runner 在每个安全检查点调用 checkpoint()：
- 暂停中：阻塞等待，再按 F9 恢复，或按 F10 停止
- 已停止：抛 RunnerStopped，Runner 捕获后带已有结果优雅退出

检查点包括业务步骤和每次输入动作：导航、标定、筛选、强化途中也会响应。
已经开始的点击/按键会完成释放；暂停/停止在后续输入前生效。正在执行的 OCR
和固定等待不会被强行打断，停止后保留当前页面供复核。
"""

from __future__ import annotations

import ctypes
import threading
import time

from zzz_ddtool.logutil import get_logger

VK_F9 = 0x78
VK_F10 = 0x79

user32 = ctypes.windll.user32


class RunnerStopped(RuntimeError):
    pass


class HotkeyController:
    def __init__(self, poll_interval: float = 0.05) -> None:
        self.poll_interval = poll_interval
        self.paused = False
        self.pause_generation = 0
        self.stopped = False
        self._pressed: set[int] = set()
        self._lock = threading.Lock()
        # 实际因暂停阻塞过的区间 (start, end) 单调时钟；供 scan-waits 标定扣除（Spec §6.1）。
        self._pause_spans: list[tuple[float, float]] = []
        self._closed = threading.Event()
        self.log = get_logger("zzz_ddtool.hotkeys")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ddtool-hotkeys")
        self._thread.start()

    def _loop(self) -> None:
        while not self._closed.is_set():
            for vk, name in ((VK_F9, "F9"), (VK_F10, "F10")):
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                with self._lock:
                    if down and vk not in self._pressed and not self.stopped:
                        if vk == VK_F9:
                            self.paused = not self.paused
                            self.pause_generation += 1
                            self.log.info("热键 F9：暂停=%s", self.paused)
                        else:
                            self.stopped = True
                            self.log.info("热键 F10：请求停止")
                    if down:
                        self._pressed.add(vk)
                    else:
                        self._pressed.discard(vk)
            self._closed.wait(self.poll_interval)

    def checkpoint(self) -> int:
        """安全检查点：暂停则阻塞，停止则抛 RunnerStopped。

        返回值（当前 pause_generation）、按键释放与停止传播都不变。额外记录**实际**
        因 paused 阻塞的区间：第一次确实阻塞时记起点，恢复或抛停止时在 finally 记终点。
        """
        blocked_since: float | None = None
        try:
            while True:
                with self._lock:
                    if self.stopped:
                        raise RunnerStopped("F10 请求停止")
                    if not self.paused:
                        return self.pause_generation
                if blocked_since is None:
                    blocked_since = time.monotonic()
                time.sleep(0.1)
        finally:
            if blocked_since is not None:
                ended = time.monotonic()
                with self._lock:
                    self._pause_spans.append((blocked_since, ended))

    def pause_intervals(self, since: float, until: float) -> tuple[tuple[float, float], ...]:
        """该单调时钟区间内实际暂停阻塞的起止元组（已裁剪、合并且按开始排序）。

        只读查询在原控制锁下取快照；未启用/没有暂停返回空元组。只扣实际暂停阻塞，
        不把整个 checkpoint 调用耗时或 pause_generation 次数当暂停（Spec §6.1）。
        """
        with self._lock:
            raw = list(self._pause_spans)
        clipped: list[tuple[float, float]] = []
        for start, end in raw:
            lo = max(float(start), float(since))
            hi = min(float(end), float(until))
            if hi > lo:
                clipped.append((lo, hi))
        clipped.sort(key=lambda span: span[0])
        merged: list[tuple[float, float]] = []
        for start, end in clipped:
            if merged and start <= merged[-1][1]:
                prev_start, prev_end = merged[-1]
                merged[-1] = (prev_start, max(prev_end, end))
            else:
                merged.append((start, end))
        return tuple(merged)

    def toggle_pause(self) -> bool:
        with self._lock:
            if not self.stopped:
                self.paused = not self.paused
                self.pause_generation += 1
            paused = self.paused
        self.log.info("控制请求：暂停=%s", paused)
        return paused

    def request_stop(self) -> None:
        with self._lock:
            self.stopped = True
        self.log.info("控制请求：停止")

    def close(self) -> None:
        self._closed.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=0.2)
