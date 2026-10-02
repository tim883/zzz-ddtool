import ctypes
import logging
import threading
import time

import pytest

from zzz_ddtool import overlay


def wait_until(predicate):
    limit = time.monotonic() + 3
    while not predicate() and time.monotonic() < limit:
        time.sleep(0.01)
    assert predicate()


@pytest.mark.window
def test_native_overlay_pumps_on_owner_thread_and_reopens(monkeypatch):
    # 独立小窗口验证，不寻找或操作游戏，结束时销毁。
    monkeypatch.setattr(overlay, "_refresh_game_origin", lambda: True)
    query = overlay.user32.GetWindowThreadProcessId
    query.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
    query.restype = ctypes.wintypes.DWORD
    for _ in range(2):
        try:
            overlay.overlay_show(["测试：识别盘面", "F9 暂停 / F10 停止"])
            assert overlay._hwnd
            assert query(overlay._hwnd, None) == overlay._thread.native_id
            assert query(overlay._hwnd, None) != threading.get_native_id()
            wait_until(lambda: overlay.user32.IsWindowVisible(overlay._hwnd))
            wait_until(lambda: overlay._font is not None)
            assert overlay._msg_counts.get(overlay.WM_PAINT, 0) > 0
            overlay.overlay_hide()
            wait_until(lambda: not overlay.user32.IsWindowVisible(overlay._hwnd))
        finally:
            overlay.overlay_shutdown()
        assert overlay._hwnd is None
        assert not overlay._thread.is_alive()


def test_game_overlay_receives_real_logs_and_ignores_own_errors(monkeypatch):
    received = []
    monkeypatch.setattr(overlay, "overlay_show", lambda lines: received.append(list(lines)))
    handler = overlay.OverlayLogHandler()
    handler.handle(logging.LogRecord("zzz_ddtool.trace", logging.INFO, "", 0, "识别盘面成功", (), None))
    handler.handle(logging.LogRecord("zzz_ddtool.overlay", logging.ERROR, "", 0, "创建失败", (), None))
    assert len(received) == 1
    assert "F10" in received[0][0]
    assert "识别盘面成功" in received[0][-1]


@pytest.mark.window
def test_overlay_follows_without_new_logs_and_restores(monkeypatch):
    # 只移动本测试创建的独立窗口，不操作真实游戏。
    target = overlay.user32.CreateWindowExW(
        overlay.WS_EX_NOACTIVATE | overlay.WS_EX_TOOLWINDOW,
        "STATIC", "overlay-follow-test", overlay.WS_POPUP,
        200, 300, 800, 600, None, None, overlay.kernel32.GetModuleHandleW(None), None,
    )
    assert target
    overlay.user32.ShowWindow(target, 4)
    find = []

    def find_game():
        find.append(target)
        return target

    monkeypatch.setattr("zzz_ddtool.capture.find_game_hwnd", find_game)

    def geometry():
        rect = overlay.RECT()
        assert overlay.user32.GetWindowRect(overlay._hwnd, ctypes.byref(rect))
        return rect.left, rect.top, rect.right - rect.left

    try:
        overlay.overlay_show(["保持此行不更新，验证定时跟随"])
        wait_until(lambda: geometry() == (212, 312, 776))
        overlay.user32.SetWindowPos(target, None, -450, 150, 600, 500, 0x0014)
        wait_until(lambda: geometry() == (-438, 162, 576))
        assert len(find) == 1  # 缓存目标句柄，移动不重新枚举进程
        overlay.user32.ShowWindow(target, 0)
        wait_until(lambda: not overlay.user32.IsWindowVisible(overlay._hwnd))
        overlay.user32.ShowWindow(target, 4)
        wait_until(lambda: overlay.user32.IsWindowVisible(overlay._hwnd))
        overlay.overlay_hide()
        wait_until(lambda: not overlay.user32.IsWindowVisible(overlay._hwnd))
        time.sleep(0.2)
        assert not overlay.user32.IsWindowVisible(overlay._hwnd)  # 显式隐藏不被定时器重新显示
    finally:
        overlay.overlay_shutdown()
        overlay.user32.DestroyWindow(target)
