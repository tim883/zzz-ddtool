from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from zzz_ddtool.logutil import get_logger
from zzz_ddtool.paths import captures_dir
from zzz_ddtool.retain import retain_captures

PROCESS_NAME = "ZenlessZoneZero.exe"
WINDOW_TITLES = ("ZenlessZoneZero", "绝区零")
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9
DIB_RGB_COLORS = 0
BI_RGB = 0

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


class CaptureError(RuntimeError):
    pass


def _client_size(hwnd: int) -> tuple[int, int]:
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    return rect.right - rect.left, rect.bottom - rect.top


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _exe_name(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).name
    finally:
        kernel32.CloseHandle(handle)


def _title_looks_like_game(title: str) -> bool:
    return title in WINDOW_TITLES or title.startswith("ZenlessZoneZero") or "绝区零" in title


def find_game_hwnd() -> int:
    by_process: list[tuple[int, int, int]] = []
    by_title: list[tuple[int, int, int]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _each(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        exe = _exe_name(_pid_of(hwnd))
        width, height = _client_size(hwnd)
        area = width * height
        if exe.lower() == PROCESS_NAME.lower():
            # 游戏本体：最小化时客户区为 0×0，仍要记录，截图前恢复窗口
            by_process.append((area, width, int(hwnd)))
            return True
        if width < 64 or height < 64:
            return True
        # 标题兜底：排除 OneDragon 的 python 窗口（如"绝区零 一条龙 01"）
        if _title_looks_like_game(title) and "python" not in exe.lower():
            by_title.append((area, width, int(hwnd)))
        return True

    user32.EnumWindows(_each, 0)
    pool = by_process or by_title
    if not pool:
        raise CaptureError("没找到游戏窗口。先开绝区零，窗口化，停在驱动盘仓库。")
    pool.sort(reverse=True)
    return pool[0][2]


def _is_mostly_black(image) -> bool:
    width = image.width()
    height = image.height()
    if width < 8 or height < 8:
        return True
    hits = 0
    samples = 0
    for y in (height // 8, height // 2, height * 7 // 8):
        for x in (width // 8, width // 2, width * 7 // 8):
            samples += 1
            color = image.pixelColor(x, y)
            if color.red() + color.green() + color.blue() < 18:
                hits += 1
    return hits >= samples - 1


def _print_window(hwnd: int):
    from PySide6.QtGui import QImage

    width, height = _client_size(hwnd)
    if width < 8 or height < 8:
        return None

    screen_dc = user32.GetDC(0)
    if not screen_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    if not mem_dc or not bitmap:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)
        return None

    old = gdi32.SelectObject(mem_dc, bitmap)
    try:
        ok = user32.PrintWindow(hwnd, mem_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        if not ok:
            return None

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        buf = (ctypes.c_char * (width * height * 4))()
        copied = gdi32.GetDIBits(
            mem_dc,
            bitmap,
            0,
            height,
            buf,
            ctypes.byref(info),
            DIB_RGB_COLORS,
        )
        if copied != height:
            return None
        image = QImage(buf, width, height, width * 4, QImage.Format.Format_RGB32)
        return image.copy()
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)


def _grab_screen_region(hwnd: int):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    screen = app.primaryScreen()
    if screen is None:
        raise CaptureError("没有可用屏幕，截不了图。")
    pixmap = screen.grabWindow(hwnd)
    if pixmap.isNull() or pixmap.width() < 8:
        raise CaptureError("窗口截到的是空图。改成窗口化再试。")
    return pixmap.toImage()


def _restore_window(hwnd: int) -> None:
    """游戏窗口最小化时先恢复并提到前台，否则 PrintWindow 截不到（客户区 0×0）。"""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.4)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)


def grab_game_image(hwnd: int | None = None):
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    target = hwnd if hwnd is not None else find_game_hwnd()
    _restore_window(target)
    image = _print_window(target)
    method = "printwindow"
    if image is None or _is_mostly_black(image):
        image = _grab_screen_region(target)
        method = "grabwindow"
        if _is_mostly_black(image):
            raise CaptureError("窗口截到的是黑图。改成窗口化再试。")
    return image, method, target


def save_image(image, hwnd: int, method: str, name: str | None = None, dest_dir: Path | None = None) -> Path:
    folder = dest_dir or captures_dir()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{name}.png" if name else folder / f"zzz-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    if not image.save(str(dest), "PNG"):
        raise CaptureError(f"写文件失败：{dest}")
    removed = retain_captures()
    get_logger("zzz_ddtool.capture").info(
        "captured %s (%sx%s) hwnd=%s method=%s removed=%s",
        dest.name,
        image.width(),
        image.height(),
        hwnd,
        method,
        len(removed),
    )
    return dest


def capture_game(dest_dir: Path | None = None, name: str | None = None) -> Path:
    image, method, hwnd = grab_game_image()
    return save_image(image, hwnd, method, name=name, dest_dir=dest_dir)
