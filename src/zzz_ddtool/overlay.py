"""游戏画面上的状态悬浮层（Win32 分层窗口 + GDI，纯 ctypes，零依赖）。

技术来自 March7thAssistant debug_overlay：
- WS_EX_LAYERED + LWA_COLORKEY：底色键透明，只有白字浮在游戏画面上
- WS_EX_TRANSPARENT + WM_NCHITTEST→HTTRANSPARENT：鼠标完全穿透到游戏
- WS_EX_TOPMOST + SWP_NOACTIVATE：浮在游戏上但不抢焦点
- 后台线程消息循环 + WM_PAINT 里 GDI 绘制
- 每 FOLLOW_INTERVAL_MS 跟随游戏客户区；游戏最小化/隐藏/不是前台窗口时隐藏

为什么不影响截图：这是独立悬浮窗口，capture 的 PrintWindow(游戏hwnd)
只渲染游戏窗口自身表面，拍不到本层，OCR 零污染。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes

MAX_LINES = 9
LINE_H = 24
FONT_H = 17
TEXT_COLOR = 0x00FFFFFF  # COLORREF 白
KEY_COLOR = 0x00202020  # COLORREF：_paint 用它填满整幅底，该色像素在色键模式下全透明

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
HWND_TOPMOST = -1
SWP_FLAGS = 0x0001 | 0x0002 | 0x0010  # NOMOVE | NOSIZE | NOACTIVATE
SW_SHOW = 5
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
WDA_EXCLUDEFROMCAPTURE = 0x00000011
WM_UPDATE = 0x8001
WM_HIDE = 0x8002
WM_TIMER = 0x0113
#: 跟随游戏窗口的轮询间隔（毫秒）。16ms 已经贴着系统定时器粒度（约 15.6ms），再调小
#: 没有意义；旧的 100ms 在 60Hz 下等于落后 6 帧，拖动窗口时肉眼看到的就是拖影。
FOLLOW_INTERVAL_MS = 16
NONANTIALIASED_QUALITY = 3  # 关抗锯齿，避免颜色键窗口文字边缘杂色
LWA_COLORKEY = 0x00000001
GA_ROOT = 2  # GetAncestor 的 gaFlags

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

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


def _bind(dll, name, args, result):
    fn = getattr(dll, name)
    fn.argtypes, fn.restype = args, result


# ctypes 默认返回 int 会截断 64 位 GDI/窗口句柄，字符串参数也必须声明。
_bind(kernel32, "GetModuleHandleW", [wintypes.LPCWSTR], wintypes.HMODULE)
_bind(user32, "RegisterClassW", [ctypes.POINTER(WNDCLASSW)], wintypes.ATOM)
_bind(user32, "UnregisterClassW", [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL)
_bind(user32, "CreateWindowExW", [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
      wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID], wintypes.HWND)
_bind(user32, "BeginPaint", [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)], wintypes.HDC)
_bind(user32, "EndPaint", [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)], wintypes.BOOL)
_bind(user32, "FillRect", [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH], ctypes.c_int)
_bind(user32, "GetMessageW", [ctypes.POINTER(MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint], ctypes.c_int)
_bind(user32, "TranslateMessage", [ctypes.POINTER(MSG)], wintypes.BOOL)
_bind(user32, "DispatchMessageW", [ctypes.POINTER(MSG)], ctypes.c_ssize_t)
_bind(user32, "PostMessageW", [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL)
_bind(user32, "DestroyWindow", [wintypes.HWND], wintypes.BOOL)
_bind(user32, "ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
_bind(user32, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL)
_bind(user32, "IsWindow", [wintypes.HWND], wintypes.BOOL)
_bind(user32, "IsIconic", [wintypes.HWND], wintypes.BOOL)
# 不声明 argtypes/restype 的话句柄会被截成 c_int，64 位上随时读错窗口。
_bind(user32, "GetForegroundWindow", [], wintypes.HWND)
_bind(user32, "GetAncestor", [wintypes.HWND, ctypes.c_uint], wintypes.HWND)
_bind(user32, "GetClientRect", [wintypes.HWND, ctypes.POINTER(RECT)], wintypes.BOOL)
_bind(user32, "GetWindowRect", [wintypes.HWND, ctypes.POINTER(RECT)], wintypes.BOOL)
_bind(user32, "SetTimer", [wintypes.HWND, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p], ctypes.c_size_t)
_bind(user32, "KillTimer", [wintypes.HWND, ctypes.c_size_t], wintypes.BOOL)
_bind(user32, "ClientToScreen", [wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL)
_bind(user32, "SetWindowPos", [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
      ctypes.c_int, ctypes.c_int, ctypes.c_uint], wintypes.BOOL)
_bind(user32, "SetLayeredWindowAttributes", [wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD], wintypes.BOOL)
_bind(user32, "InvalidateRect", [wintypes.HWND, ctypes.POINTER(RECT), wintypes.BOOL], wintypes.BOOL)
_bind(gdi32, "CreateFontW", [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR], wintypes.HANDLE)
_bind(gdi32, "CreateCompatibleDC", [wintypes.HDC], wintypes.HDC)
_bind(gdi32, "CreateCompatibleBitmap", [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP)
_bind(gdi32, "CreateSolidBrush", [wintypes.DWORD], wintypes.HBRUSH)
_bind(gdi32, "SelectObject", [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE)
_bind(gdi32, "DeleteObject", [wintypes.HANDLE], wintypes.BOOL)
_bind(gdi32, "DeleteDC", [wintypes.HDC], wintypes.BOOL)
_bind(gdi32, "SetTextColor", [wintypes.HDC, wintypes.DWORD], wintypes.DWORD)
_bind(gdi32, "SetBkMode", [wintypes.HDC, ctypes.c_int], ctypes.c_int)
_bind(gdi32, "TextOutW", [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int], wintypes.BOOL)
_bind(gdi32, "BitBlt", [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
      wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD], wintypes.BOOL)
_proc_ref = None  # 防 WNDPROC 被 GC
_hwnd = None
_ready = threading.Event()
_msg_counts: dict[int, int] = {}
_lines: list[str] = []
_lock = threading.Lock()
_screen_w = 1100
_screen_h = MAX_LINES * LINE_H + 16
_game_origin = [0, 0]  # 绘制用窗口内坐标；窗口自身定位到游戏客户区
_font = None
_thread = None
_init_lock = threading.Lock()
_target_hwnd = None
_last_geometry = None
_wanted_visible = False
_wda_active = False  # 浮层已设为「不参与屏幕捕获」（Win10 2004+ / Win11 支持）
_capture_hidden = False  # 截图期间临时隐藏浮层（WDA 不可用时的兜底）
# 离屏 DC + 位图复用：跟随提到 ~60Hz 后，每帧新建约 1MB 位图是纯浪费。
_paint_dc = None
_paint_bmp = None
_paint_old_bmp = None
_paint_size = None  # 缓存对应的 (_screen_w, _screen_h)
_paint_cache_warned = False


def _make_font() -> int:
    return gdi32.CreateFontW(
        -FONT_H, 0, 0, 0,
        400,  # FW_NORMAL（不加粗）
        0, 0, 0,
        1,  # DEFAULT_CHARSET
        0, 0,
        NONANTIALIASED_QUALITY,
        0,
        "Microsoft YaHei",
    )


def _discard_paint_cache() -> None:
    """释放复用的离屏 DC 与位图（换宽度或窗口销毁时）。位图要先选出去再删。"""
    global _paint_dc, _paint_bmp, _paint_old_bmp, _paint_size
    if _paint_dc and _paint_old_bmp:
        gdi32.SelectObject(_paint_dc, _paint_old_bmp)
    if _paint_bmp:
        gdi32.DeleteObject(_paint_bmp)
    if _paint_dc:
        gdi32.DeleteDC(_paint_dc)
    _paint_dc = None
    _paint_bmp = None
    _paint_old_bmp = None
    _paint_size = None


def _ensure_paint_cache(hdc: wintypes.HDC) -> None:
    """复用离屏 DC 与位图，只在尺寸变化时重建（跟随频率高，每帧新建太浪费）。"""
    global _paint_dc, _paint_bmp, _paint_old_bmp, _paint_size, _paint_cache_warned
    size = (_screen_w, _screen_h)
    if _paint_size == size and _paint_dc:
        return
    _discard_paint_cache()
    mem_dc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, _screen_w, _screen_h) if mem_dc else None
    if not mem_dc or not bmp:
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        if not _paint_cache_warned:
            _paint_cache_warned = True
            from zzz_ddtool.logutil import get_logger

            get_logger("zzz_ddtool.overlay").warning("离屏位图创建失败，悬浮日志画不出来")
        return
    _paint_old_bmp = gdi32.SelectObject(mem_dc, bmp)
    _paint_dc = mem_dc
    _paint_bmp = bmp
    _paint_size = size


def _paint(hdc: wintypes.HDC) -> None:
    global _font
    _ensure_paint_cache(hdc)
    mem_dc = _paint_dc
    if not mem_dc:
        return
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


def _wnd_proc(hwnd, msg, wparam, lparam):
    global _wanted_visible
    _msg_counts[msg] = _msg_counts.get(msg, 0) + 1
    if msg == WM_PAINT:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        _paint(hdc)
        user32.EndPaint(hwnd, ctypes.byref(ps))
        return 0
    if msg == WM_UPDATE:
        _wanted_visible = True
        if not _capture_hidden:
            user32.ShowWindow(hwnd, 4 if _refresh_game_origin() else SW_HIDE)
        user32.InvalidateRect(hwnd, None, True)
        return 0
    if msg == WM_TIMER:
        if _capture_hidden:
            if user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, SW_HIDE)
        elif _wanted_visible:
            visible = _refresh_game_origin()
            if bool(user32.IsWindowVisible(hwnd)) != visible:
                user32.ShowWindow(hwnd, 4 if visible else SW_HIDE)  # 不激活、不抢焦点
        return 0
    if msg == WM_HIDE:
        _wanted_visible = False
        user32.ShowWindow(hwnd, SW_HIDE)
        return 0
    if msg == WM_ERASEBKGND:
        return 1
    if msg == WM_NCHITTEST:
        return HTTRANSPARENT
    if msg == WM_CLOSE:
        user32.DestroyWindow(hwnd)
        return 0
    if msg == WM_DESTROY:
        user32.KillTimer(hwnd, 1)
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _window_loop() -> None:
    global _hwnd, _proc_ref, _font, _target_hwnd, _last_geometry, _wanted_visible, _wda_active
    _proc_ref = WNDPROC(_wnd_proc)
    wc = WNDCLASSW()
    wc.lpfnWndProc = _proc_ref
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = CLASS_NAME
    if not user32.RegisterClassW(ctypes.byref(wc)):
        from zzz_ddtool.logutil import get_logger

        get_logger("zzz_ddtool.overlay").warning("RegisterClassW 失败：%s", ctypes.WinError(ctypes.get_last_error()))
        _ready.set()
        return
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

        get_logger("zzz_ddtool.overlay").warning("CreateWindowExW 失败：%s", ctypes.WinError(ctypes.get_last_error()))
        user32.UnregisterClassW(CLASS_NAME, wc.hInstance)
        _ready.set()
        return
    _hwnd = hwnd
    # 底色键透明：整幅底填成 KEY_COLOR，键色像素全透明，只有白字浮在游戏画面上。
    # 不用整体 LWA_ALPHA——那会把背景一起压成一块半透明深色板子，拖窗口时非常显眼。
    user32.SetLayeredWindowAttributes(hwnd, KEY_COLOR, 0, LWA_COLORKEY)
    # 浮层不进入屏幕捕获：截图走屏幕 BitBlt 时会把它一起截进去，污染 OCR。
    # 设为 excluded 后用户仍照常看到浮层，但截图里没有它（Win10 2004+ / Win11）。
    # 老系统（Win10 2004 之前）不支持，置 _wda_active=False，改由截图侧临时隐藏兜底。
    _wda_active = bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
    if not _wda_active:
        from zzz_ddtool.logutil import get_logger

        get_logger("zzz_ddtool.overlay").warning(
            "系统不支持「排除屏幕捕获」（需 Win10 2004+），浮层改为截图时临时隐藏"
        )
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
    user32.SetTimer(hwnd, 1, FOLLOW_INTERVAL_MS, None)

    _install_capture_hooks()
    _ready.set()
    try:
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        _hwnd = None
        _target_hwnd = None
        _last_geometry = None
        _wanted_visible = False
        if _font:
            gdi32.DeleteObject(_font)
            _font = None
        _discard_paint_cache()
        user32.UnregisterClassW(CLASS_NAME, wc.hInstance)


def _ensure_window() -> bool:
    global _thread
    with _init_lock:
        if _thread is None or not _thread.is_alive():
            _ready.clear()
            _thread = threading.Thread(target=_window_loop, daemon=True, name="ddtool-overlay")
            _thread.start()
        return _ready.wait(3) and _hwnd is not None


def _game_in_foreground(hwnd) -> bool:
    """游戏是不是当前前台窗口的根窗口。

    切走（Alt+Tab、点别的窗口、Win+D 回桌面）时游戏既没最小化也没被隐藏，只看
    IsIconic/IsWindowVisible 会让浮层继续压在别人窗口上面，所以用前台判据补齐。
    取根窗口，是因为前台可能是游戏的子窗口或弹出窗口，那时仍算「在游戏里」。
    读不到前台时按「在游戏里」处理：宁可多显示，也不要把日志无声地吞掉。
    """
    try:
        foreground = user32.GetForegroundWindow()
        if not foreground:
            return False
        return int(user32.GetAncestor(foreground, GA_ROOT)) == int(hwnd)
    except Exception:
        return True


def _refresh_game_origin() -> bool:
    """定时跟随客户区，仅几何变化时移动；目标最小化/隐藏/不是前台时隐藏日志。"""
    global _target_hwnd, _last_geometry, _screen_w
    try:
        from zzz_ddtool.capture import find_game_hwnd

        if _target_hwnd is None or not user32.IsWindow(_target_hwnd):
            _target_hwnd = find_game_hwnd()
            _last_geometry = None
        hwnd = _target_hwnd
        if (
            user32.IsIconic(hwnd)
            or not user32.IsWindowVisible(hwnd)
            or not _game_in_foreground(hwnd)
        ):
            return False
        pt = wintypes.POINT(0, 0)
        rect = RECT()
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)) or not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return False
        width = min(1100, max(1, rect.right - rect.left - 24))
        geometry = (pt.x + 12, pt.y + 12, width, _screen_h)
        if geometry != _last_geometry:
            if not user32.SetWindowPos(_hwnd, wintypes.HWND(-1), *geometry, 0x0010):
                return False
            _screen_w = width
            _last_geometry = geometry
            user32.InvalidateRect(_hwnd, None, True)
        return True
    except Exception:
        return False


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
        user32.PostMessageW(_hwnd, WM_UPDATE, 0, 0)
    except Exception:
        from zzz_ddtool.logutil import get_logger

        get_logger("zzz_ddtool.overlay").exception("游戏悬浮日志更新失败")


def overlay_hide() -> None:
    try:
        if _hwnd:
            user32.PostMessageW(_hwnd, WM_HIDE, 0, 0)
    except Exception:
        pass


def overlay_shutdown() -> None:
    if _hwnd:
        user32.PostMessageW(_hwnd, WM_CLOSE, 0, 0)
    if _thread and _thread is not threading.current_thread():
        _thread.join(timeout=3)


def _suspend_for_capture() -> bool:
    """截图前调用，返回 True 表示「已临时隐藏，截完要恢复」。

    WDA_EXCLUDEFROMCAPTURE 生效时浮层本来就不进截图，直接返回 False（不闪）。
    老系统（Win10 2004 之前）不支持该特性，改为临时隐藏兜底。
    """
    global _capture_hidden
    if _wda_active or not _hwnd:
        return False
    try:
        _capture_hidden = True
        user32.ShowWindow(_hwnd, SW_HIDE)
        return True
    except Exception:
        _capture_hidden = False
        return False


def _resume_after_capture(hidden: bool) -> None:
    global _capture_hidden
    if not hidden:
        return
    _capture_hidden = False
    try:
        if _hwnd:
            user32.ShowWindow(_hwnd, SW_SHOWNOACTIVATE)
    except Exception:
        pass


def _install_capture_hooks() -> None:
    """把「截图前藏 / 截图后恢复」注册给 capture，保证抓到的画面里没有浮层。"""
    try:
        from zzz_ddtool import capture

        capture.set_overlay_hooks(_suspend_for_capture, _resume_after_capture)
    except Exception:
        pass


class OverlayLogHandler(logging.Handler):
    """订阅统一日志，游戏里也能看到导航/OCR 的实际步骤及错误。"""

    def __init__(self):
        super().__init__()
        from zzz_ddtool.logutil import ChineseFormatter

        self.setFormatter(ChineseFormatter())
        self.lines = []

    def emit(self, record):
        # 悬浮窗自己的错误只落文本日志，避免创建失败时递归调用自己。
        if record.name.startswith("zzz_ddtool.overlay"):
            return
        try:
            self.lines.extend(self.format(record).splitlines())
            del self.lines[:-(MAX_LINES - 1)]
            overlay_show(["F9 暂停/恢复 · F10 停止（在安全检查点生效）"] + self.lines)
        except Exception:
            self.handleError(record)
