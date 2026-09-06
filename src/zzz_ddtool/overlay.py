"""游戏画面上的状态悬浮层（Win32 分层窗口 + GDI，纯 ctypes，零依赖）。

技术来自 March7thAssistant debug_overlay：
- WS_EX_LAYERED + LWA_COLORKEY：品红底色键透明，只显示画上去的文字
- WS_EX_TRANSPARENT + WM_NCHITTEST→HTTRANSPARENT：鼠标完全穿透到游戏
- WS_EX_TOPMOST + SWP_NOACTIVATE：浮在游戏上但不抢焦点
- 后台线程消息循环 + WM_PAINT 里 GDI 绘制

为什么不影响截图：这是独立悬浮窗口，capture 的 PrintWindow(游戏hwnd)
只渲染游戏窗口自身表面，拍不到本层，OCR 零污染。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes

MAX_LINES = 8
LINE_H = 24
FONT_H = 17
TEXT_COLOR = 0x00FFFFFF  # COLORREF 白
KEY_COLOR = 0x00FF00FF  # COLORREF 品红 = 透明键

WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_NCHITTEST = 0x0084
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
HTTRANSPARENT = -1
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
LWA_COLORKEY = 0x00000001
HWND_TOPMOST = -1
SWP_FLAGS = 0x0001 | 0x0002 | 0x0010  # NOMOVE | NOSIZE | NOACTIVATE
SW_SHOW = 5
SW_HIDE = 0
NONANTIALIASED_QUALITY = 3  # 关抗锯齿，避免颜色键窗口文字边缘杂色

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# 消息参数必须按 64 位宽度转换，否则 DefWindowProcW 收到大 lParam 会 OverflowError
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
)


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", ctypes.c_uint),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


CLASS_NAME = "ZZZDDToolOverlay"
_proc_ref = None  # 防 WNDPROC 被 GC
_hwnd = None
_ready = threading.Event()
_msg_counts: dict[int, int] = {}
_lines: list[str] = []
_lock = threading.Lock()
_screen_w = user32.GetSystemMetrics(0)
_screen_h = user32.GetSystemMetrics(1)
_game_origin = [40, 40]  # 文字绘制原点（屏幕坐标），每次 update 按游戏窗口刷新
_font = None


def _make_font() -> int:
    return gdi32.CreateFontW(
        -FONT_H, 0, 0, 0,
        700,  # FW_BOLD
        0, 0, 0,
        1,  # DEFAULT_CHARSET
        0, 0,
        NONANTIALIASED_QUALITY,
        0,
        "Microsoft YaHei",
    )


def _paint(hdc: wintypes.HDC) -> None:
    global _font
    mem_dc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, _screen_w, _screen_h)
    old_bmp = gdi32.SelectObject(mem_dc, bmp)
    key_brush = gdi32.CreateSolidBrush(KEY_COLOR)
    full = RECT(0, 0, _screen_w, _screen_h)
    user32.FillRect(mem_dc, ctypes.byref(full), key_brush)
    gdi32.DeleteObject(key_brush)
    if _font is None:
        _font = _make_font()
    old_font = gdi32.SelectObject(mem_dc, _font)
    gdi32.SetTextColor(mem_dc, TEXT_COLOR)
    gdi32.SetBkMode(mem_dc, 1)  # TRANSPARENT
    with _lock:
        lines = list(_lines)
    ox, oy = _game_origin
    for i, line in enumerate(lines):
        buf = ctypes.c_wchar_p(line)
        gdi32.TextOutW(mem_dc, ox + 12, oy + 8 + i * LINE_H, buf, len(line))
    gdi32.SelectObject(mem_dc, old_font)
    gdi32.BitBlt(hdc, 0, 0, _screen_w, _screen_h, mem_dc, 0, 0, 0x00CC0020)  # SRCCOPY
    gdi32.SelectObject(mem_dc, old_bmp)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)


def _wnd_proc(hwnd, msg, wparam, lparam):
    _msg_counts[msg] = _msg_counts.get(msg, 0) + 1
    if msg == WM_PAINT:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        _paint(hdc)
        user32.EndPaint(hwnd, ctypes.byref(ps))
        return 0
    if msg == WM_ERASEBKGND:
        return 1
    if msg == WM_NCHITTEST:
        return HTTRANSPARENT
    if msg == WM_CLOSE:
        user32.DestroyWindow(hwnd)
        return 0
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _ensure_window() -> bool:
    global _hwnd, _proc_ref
    if _hwnd:
        return True
    _ready.clear()
    _proc_ref = WNDPROC(_wnd_proc)
    wc = WNDCLASSW()
    wc.lpfnWndProc = _proc_ref
    wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
    wc.lpszClassName = CLASS_NAME
    if not user32.RegisterClassW(ctypes.byref(wc)):
        from zzz_ddtool.logutil import get_logger

        get_logger("zzz_ddtool.overlay").warning("RegisterClassW 失败")
        return False
    hwnd = user32.CreateWindowExW(
        WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
        CLASS_NAME,
        "",
        WS_POPUP,
        0, 0, _screen_w, _screen_h,
        None, None, wc.hInstance, None,
    )
    if not hwnd:
        from zzz_ddtool.logutil import get_logger

        get_logger("zzz_ddtool.overlay").warning("CreateWindowExW 失败")
        return False
    _hwnd = hwnd
    user32.SetLayeredWindowAttributes(hwnd, KEY_COLOR, 0, LWA_COLORKEY)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)

    def _loop():
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    threading.Thread(target=_loop, daemon=True, name="ddtool-overlay").start()
    _ready.set()
    return True


def _refresh_game_origin() -> None:
    """把文字绘制原点对齐游戏窗口客户区左上角。"""
    try:
        from zzz_ddtool.capture import find_game_hwnd

        hwnd = find_game_hwnd()
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        _game_origin[0] = max(0, pt.x)
        _game_origin[1] = max(0, pt.y)
    except Exception:
        pass


def overlay_show(lines: list[str]) -> None:
    """显示/更新悬浮文字（传空列表 = 隐藏）。任何异常都不影响主流程。"""
    try:
        if not lines:
            overlay_hide()
            return
        if not _ensure_window():
            return
        with _lock:
            _lines[:] = lines[-MAX_LINES:]
        _refresh_game_origin()
        user32.ShowWindow(_hwnd, SW_SHOW)
        user32.SetWindowPos(_hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
        user32.InvalidateRect(_hwnd, None, True)
        user32.UpdateWindow(_hwnd)  # 强制同步走一次 WM_PAINT，不等系统调度
    except Exception:
        pass


def overlay_hide() -> None:
    try:
        if _hwnd:
            user32.ShowWindow(_hwnd, SW_HIDE)
    except Exception:
        pass
