from __future__ import annotations

import ctypes
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
SRCCOPY = 0x00CC0020

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


def client_size(hwnd: int) -> tuple[int, int]:
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


def window_pid(hwnd: int) -> int:
    """窗口所属进程 PID（spec §5.1 实例绑定用）。"""
    return _pid_of(hwnd)


def process_exe_path(pid: int) -> str | None:
    """进程的完整 exe 绝对路径；读取失败返回 None。

    spec §5.1 第 2 条：不取文件名、不从 PATH 猜路径；查询链与 find_game_hwnd
    共用 OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) → QueryFullProcessImageNameW。
    """
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value or None
    finally:
        kernel32.CloseHandle(handle)


def _exe_name(pid: int) -> str:
    full = process_exe_path(pid)
    return Path(full).name if full else ""


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
        width, height = client_size(hwnd)
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


def _looks_like_blank(image) -> bool:
    """近似纯色（空白图）检测。

    PrintWindow 被拒时会抓到白底/空白的无效帧，它不是"全黑"，旧的黑图判据
    拦不住，会被当成正常帧送进 OCR。真实游戏画面缩到 24×14 也有几十种颜色，
    空白图只有寥寥数种；每通道 >>4 量化后超过 4 种即认为画面有内容。
    """
    width = image.width()
    height = image.height()
    if width < 8 or height < 8:
        return True
    small = image.scaled(24, 14)
    kinds = set()
    for y in range(small.height()):
        for x in range(small.width()):
            color = small.pixelColor(x, y)
            kinds.add((color.red() >> 4, color.green() >> 4, color.blue() >> 4))
            if len(kinds) > 4:
                return False
    return True


def _looks_unusable(image) -> bool:
    """黑图或空白图都算无效帧。"""
    return _is_mostly_black(image) or _looks_like_blank(image)


def _read_bitmap(mem_dc: int, bitmap: int, width: int, height: int):
    from PySide6.QtGui import QImage

    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB
    buf = (ctypes.c_char * (width * height * 4))()
    copied = gdi32.GetDIBits(
        mem_dc, bitmap, 0, height, buf, ctypes.byref(info), DIB_RGB_COLORS
    )
    if copied != height:
        return None
    return QImage(buf, width, height, width * 4, QImage.Format.Format_RGB32).copy()


def _gdi_grab(width: int, height: int, paint):
    """建一块兼容屏幕的 32 位位图，paint(mem_dc, screen_dc) 把内容画进去再读回。

    PrintWindow 与 BitBlt 两种取图方式共用这段 DC-位图-回读样板。
    """
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
        if not paint(mem_dc, screen_dc):
            return None
        return _read_bitmap(mem_dc, bitmap, width, height)
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)


def _print_window(hwnd: int):
    width, height = client_size(hwnd)
    if width < 8 or height < 8:
        return None

    def paint(mem_dc, _screen_dc):
        return bool(user32.PrintWindow(hwnd, mem_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT))

    return _gdi_grab(width, height, paint)


def _client_screen_rect(hwnd: int):
    """客户区在屏幕上的矩形 (x, y, w, h)；取不到返回 None。"""
    width, height = client_size(hwnd)
    if width < 8 or height < 8:
        return None
    point = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
        return None
    return point.x, point.y, width, height


def _bitblt_screen(hwnd: int):
    """从屏幕 DC 拷贝客户区。

    不打开目标进程，天然不受 UIPI 限制——游戏提权运行时 PrintWindow 会被
    ERROR_ACCESS_DENIED(5) 拒绝，这条路仍可用。前提：窗口可见且在最前
    （_restore_window 负责），否则拷到的是遮挡窗口的像素。
    """
    rect = _client_screen_rect(hwnd)
    if rect is None:
        return None
    x, y, width, height = rect

    def paint(mem_dc, screen_dc):
        return bool(gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, x, y, SRCCOPY))

    return _gdi_grab(width, height, paint)


def _grab_screen_region(hwnd: int):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QThread

    app = QApplication.instance() or QApplication([])
    if QThread.currentThread() != app.thread():
        from zzz_ddtool.capturebridge import grab_from_worker

        return grab_from_worker(hwnd)
    screen = app.primaryScreen()
    if screen is None:
        raise CaptureError("没有可用屏幕，截不了图。")
    pixmap = screen.grabWindow(hwnd)
    if pixmap.isNull() or pixmap.width() < 8:
        raise CaptureError("窗口截到的是空图。改成窗口化再试。")
    return pixmap.toImage()


def _restore_window(hwnd: int) -> None:
    """截图前把游戏恢复并提到前台。

    BitBlt 屏幕路径要求窗口可见且在最前。裸 SetForegroundWindow 在新 Windows
    上会被前台锁拒绝（返回 0），画面就落到别的窗口上——复用 wininput 的强激活
    （AttachThreadInput + 解锁前台 + Alt 兜底），与点击前用的是同一套。
    """
    from zzz_ddtool.wininput import InputError, focus_window, is_foreground

    if is_foreground(hwnd):
        return
    try:
        focus_window(hwnd)
    except InputError as exc:
        raise CaptureError(str(exc)) from exc


_overlay_hooks = None


def set_overlay_hooks(hide, restore) -> None:
    """注册悬浮层的隐藏/恢复回调（由 overlay 模块在窗口建好后调用）。

    改用屏幕 BitBlt 截屏后，工具自己的悬浮日志窗会被一起截进画面、污染 OCR。
    Win10 2004+ / Win11 上浮层用 WDA_EXCLUDEFROMCAPTURE 排除在捕获外（hide 直接
    返回 False，画面不闪）；老系统上 hide 才真正把浮层藏起来。分层上 capture
    不认识 overlay，只持有回调。
    """
    global _overlay_hooks
    _overlay_hooks = (hide, restore)


def _suspend_overlay() -> bool:
    if _overlay_hooks is None:
        return False
    try:
        return bool(_overlay_hooks[0]())
    except Exception:
        return False


def _resume_overlay(hidden: bool) -> None:
    if _overlay_hooks is None:
        return
    try:
        _overlay_hooks[1](hidden)
    except Exception:
        pass


def grab_game_image(hwnd: int | None = None):
    target = hwnd if hwnd is not None else find_game_hwnd()
    _restore_window(target)
    overlay_hidden = _suspend_overlay()
    try:
        image = _print_window(target)
        method = "printwindow"
        if image is None or _looks_unusable(image):
            # 提权运行的游戏会把 PrintWindow 拒成 ERROR_ACCESS_DENIED；
            # 屏幕 BitBlt 不打开目标进程，可兜底。窗口已由 _restore_window 提到最前。
            image = _bitblt_screen(target)
            method = "bitblt"
            if image is None or _looks_unusable(image):
                raise CaptureError(
                    "窗口截到的是黑图/空白图。确认游戏窗口化、可见、没有被别的窗口挡住。"
                )
    finally:
        _resume_overlay(overlay_hidden)
    get_logger("zzz_ddtool.capture").debug(
        "截屏 %s %dx%d hwnd=%s", method, image.width(), image.height(), target
    )
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


def capture_game(
    dest_dir: Path | None = None, name: str | None = None, *, hwnd: int | None = None
) -> Path:
    """截游戏窗口存盘。hwnd 传了就用它，不传按 find_game_hwnd() 现找。

    run 路径必须传绑定的 hwnd（spec §5.1：不因另一个窗口变大而重新选窗口）。
    """
    image, method, target = grab_game_image(hwnd)
    return save_image(image, target, method, name=name, dest_dir=dest_dir)
