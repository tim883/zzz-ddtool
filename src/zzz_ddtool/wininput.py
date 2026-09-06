from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from random import randint, uniform

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_VIRTUALDESK = 0x4000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
VK_ESCAPE = 0x1B
VK_MENU = 0x12
VK_R = 0x52
SCAN_ESCAPE = 0x01
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_ACTIVATE = 0x0006
WA_ACTIVE = 1
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
HWND_TOP = 0
LSFW_UNLOCK = 2
ASFW_ANY = -1
MAPVK_VK_TO_VSC = 0
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


class InputError(RuntimeError):
    pass


def _send(inputs: list[INPUT]) -> None:
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise InputError("SendInput 没送出去。用管理员再开一次工具试试。")


def _abs_point(screen_x: int, screen_y: int) -> tuple[int, int]:
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if vw <= 1 or vh <= 1:
        raise InputError("读不到屏幕尺寸。")
    ax = int((screen_x - vx) * 65535 / (vw - 1))
    ay = int((screen_y - vy) * 65535 / (vh - 1))
    return ax, ay


def client_to_screen(hwnd: int, x: int, y: int) -> tuple[int, int]:
    point = wintypes.POINT(x, y)
    if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
        raise InputError("窗口坐标换算失败。")
    return int(point.x), int(point.y)


def is_foreground(hwnd: int) -> bool:
    return int(user32.GetForegroundWindow()) == int(hwnd)


def focus_window(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.LockSetForegroundWindow(LSFW_UNLOCK)
    try:
        user32.AllowSetForegroundWindow(ASFW_ANY)
    except Exception:
        pass
    user32.SendMessageW(hwnd, WM_ACTIVATE, WA_ACTIVE, 0)
    current = kernel32.GetCurrentThreadId()
    other = user32.GetWindowThreadProcessId(hwnd, None)
    user32.AttachThreadInput(current, other, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(current, other, False)
    time.sleep(0.2)
    if is_foreground(hwnd):
        return
    user32.keybd_event(0, 0, 0, 0)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    if not is_foreground(hwnd):
        raise InputError("游戏没到前台，按键会点到别的窗口。先点一下游戏再试。")


def pause(lo: float = 0.08, hi: float = 0.18) -> None:
    time.sleep(uniform(lo, hi))


def _key_event(vk: int, *, up: bool = False, scancode: bool = False) -> INPUT:
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_KEYUP if up else 0
    if scancode:
        flags |= KEYEVENTF_SCANCODE
    return INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(vk, scan, flags, 0, None)))


def click_client(hwnd: int, x: int, y: int, alt: bool = False) -> None:
    """Foreground click, same idea as OneDragon: move cursor then mouse down/up."""
    sx, sy = client_to_screen(hwnd, x + randint(-2, 2), y + randint(-2, 2))
    focus_window(hwnd)
    if alt:
        _send([_key_event(VK_MENU)])
        pause(0.15, 0.22)
    user32.SetCursorPos(sx, sy)
    pause(0.04, 0.08)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    if alt:
        pause(0.04, 0.08)
        _send([_key_event(VK_MENU, up=True)])
    pause()


def wheel_client(hwnd: int, x: int, y: int, delta: int = -3) -> None:
    """把光标移到窗口客户区 (x,y) 后滚动。delta 负数向下翻页。一格约 120。"""
    sx, sy = client_to_screen(hwnd, x, y)
    focus_window(hwnd)
    user32.SetCursorPos(sx, sy)
    pause(0.04, 0.08)
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta * 120, 0)
    time.sleep(0.15)
    pause()


def press_key_once(hwnd: int, vk: int) -> None:
    """只按一次键（down+up 一对）。R 弃置这类开关型键必须用这个：
    press_key 会发多对 down/up，R 按奇数次才有效，偶数次等于没按。"""
    focus_window(hwnd)
    _send([_key_event(vk), _key_event(vk, scancode=True)])
    time.sleep(0.05)
    _send([_key_event(vk, up=True), _key_event(vk, up=True, scancode=True)])
    time.sleep(0.03)
    pause(0.35, 0.55)


def press_key(hwnd: int, vk: int = VK_ESCAPE) -> None:
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) or SCAN_ESCAPE
    focus_window(hwnd)
    user32.keybd_event(vk, scan, 0, 0)
    time.sleep(0.04)
    user32.keybd_event(vk, scan, KEYEVENTF_KEYUP, 0)
    _send([_key_event(vk), _key_event(vk, scancode=True)])
    time.sleep(0.03)
    _send([_key_event(vk, up=True), _key_event(vk, up=True, scancode=True)])
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 1)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 1 | (1 << 30) | (1 << 31))
    pause(0.45, 0.65)
