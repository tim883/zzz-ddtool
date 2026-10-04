"""截图取图链与无效帧判定的守卫。

背景（2026-10-04）：换 Windows 后游戏以更高完整性级别运行，PrintWindow 被
UIPI 拒成 ERROR_ACCESS_DENIED(5)，旧代码退到 Qt grabWindow 只拿到白底废图，
而"全黑"判据拦不住空白图，于是废图被当成正常帧送进 OCR。改为
PrintWindow -> 屏幕 BitBlt 两级回退，并补上「空白图」判定。
"""
from __future__ import annotations

import ctypes

import pytest
from PySide6.QtGui import QColor, QImage


def _solid(color: QColor, w: int = 64, h: int = 64) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(color)
    return img


def _rich(w: int = 64, h: int = 64) -> QImage:
    """逐像素不同的图，模拟真实游戏画面（缩到 24×14 仍有很多种颜色）。"""
    img = QImage(w, h, QImage.Format.Format_RGB32)
    for y in range(h):
        for x in range(w):
            img.setPixelColor(x, y, QColor((x * 4) % 256, (y * 7) % 256, ((x + y) * 3) % 256))
    return img


def test_blank_detected_for_white_frame():
    from zzz_ddtool import capture

    assert capture._looks_like_blank(_solid(QColor(255, 255, 255))) is True


def test_blank_detected_for_black_frame():
    from zzz_ddtool import capture

    assert capture._looks_like_blank(_solid(QColor(0, 0, 0))) is True


def test_rich_frame_is_not_blank():
    from zzz_ddtool import capture

    assert capture._looks_like_blank(_rich()) is False


def test_unusable_covers_black_and_blank():
    from zzz_ddtool import capture

    assert capture._looks_unusable(_solid(QColor(0, 0, 0))) is True
    assert capture._looks_unusable(_solid(QColor(255, 255, 255))) is True
    assert capture._looks_unusable(_rich()) is False


def test_grab_prefers_printwindow(monkeypatch):
    from zzz_ddtool import capture

    monkeypatch.setattr(capture, "_restore_window", lambda hwnd: None)
    monkeypatch.setattr(capture, "_print_window", lambda hwnd: _rich())
    monkeypatch.setattr(capture, "_bitblt_screen", lambda hwnd: pytest.fail("不该退回 BitBlt"))

    _, method, _ = capture.grab_game_image(123)
    assert method == "printwindow"


def test_grab_falls_back_to_bitblt_when_printwindow_blank(monkeypatch):
    """PrintWindow 拿回白底废图时，必须退回屏幕 BitBlt。"""
    from zzz_ddtool import capture

    monkeypatch.setattr(capture, "_restore_window", lambda hwnd: None)
    monkeypatch.setattr(capture, "_print_window", lambda hwnd: _solid(QColor(255, 255, 255)))
    monkeypatch.setattr(capture, "_bitblt_screen", lambda hwnd: _rich())

    _, method, _ = capture.grab_game_image(123)
    assert method == "bitblt"


def test_grab_falls_back_to_bitblt_when_printwindow_fails(monkeypatch):
    """PrintWindow 直接失败（提权窗口）时也要退回屏幕 BitBlt。"""
    from zzz_ddtool import capture

    monkeypatch.setattr(capture, "_restore_window", lambda hwnd: None)
    monkeypatch.setattr(capture, "_print_window", lambda hwnd: None)
    monkeypatch.setattr(capture, "_bitblt_screen", lambda hwnd: _rich())

    _, method, _ = capture.grab_game_image(123)
    assert method == "bitblt"


def test_grab_raises_when_both_unusable(monkeypatch):
    from zzz_ddtool import capture

    monkeypatch.setattr(capture, "_restore_window", lambda hwnd: None)
    monkeypatch.setattr(capture, "_print_window", lambda hwnd: None)
    monkeypatch.setattr(capture, "_bitblt_screen", lambda hwnd: _solid(QColor(0, 0, 0)))

    with pytest.raises(capture.CaptureError):
        capture.grab_game_image(123)


def test_client_screen_rect_translates_client_origin(monkeypatch):
    from zzz_ddtool import capture

    monkeypatch.setattr(capture, "client_size", lambda hwnd: (1920, 1080))

    class _FakeUser32:
        def ClientToScreen(self, hwnd, ref):
            point = ctypes.cast(ref, ctypes.POINTER(capture.wintypes.POINT)).contents
            point.x, point.y = 340, 43
            return 1

    monkeypatch.setattr(capture, "user32", _FakeUser32())
    assert capture._client_screen_rect(1) == (340, 43, 1920, 1080)


def test_client_screen_rect_rejects_tiny_client(monkeypatch):
    from zzz_ddtool import capture

    monkeypatch.setattr(capture, "client_size", lambda hwnd: (4, 4))
    assert capture._client_screen_rect(1) is None
