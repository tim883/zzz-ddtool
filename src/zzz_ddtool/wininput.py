from __future__ import annotations

import ctypes
import time
from contextlib import contextmanager
from contextvars import ContextVar
from ctypes import wintypes
from itertools import count
from random import randint, uniform
from threading import Lock
from typing import Callable, Iterator, NamedTuple

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32

from zzz_ddtool.logutil import get_logger
from zzz_ddtool.control import checkpoint_input
from zzz_ddtool.hotkeys import RunnerStopped

_input_log = get_logger("zzz_ddtool.wininput")

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
VK_T = 0x54
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


# ---------------------------------------------------------------------------
# 输入观察（spec §8.4）
#
# 未启用 capture_inputs 时五个入口零开销直通：不存记录、不抓图、不建文件，也
# 不改变既有输入函数的参数、发送顺序、等待和检查点。启用后每个逻辑调用发一对
# begin/end：在初始检查点通过之后、首次实际发送之前发 begin；正常返回发
# status=completed 的 end；已发 begin 的调用异常退出发 failed/stopped 的 end
# （RunnerStopped → stopped）；在初始检查点就被停止的调用不发 begin/end、也
# 不分配 call_id。原异常原样传播，回调既不吞异常也不重发输入。
#
# 状态全部放在 ContextVar 里，按执行上下文隔离，嵌套退出恢复前值，不保留任何
# 进程级共享回调。call_id 由本模块分配：进程内从 1 起、跨 run 连续递增，
# begin/end 共用同一 ID，多线程下用锁保证唯一。
# ---------------------------------------------------------------------------


class _InputContext(NamedTuple):
    role: str
    target: str | int | None
    stage_index: int | None
    selected: bool | None


_input_sink: ContextVar[Callable[[dict[str, object]], None] | None] = ContextVar(
    "ddtool_input_sink", default=None
)
_input_context: ContextVar[_InputContext | None] = ContextVar("ddtool_input_context", default=None)

_call_ids = count(1)
_call_id_lock = Lock()


def _next_call_id() -> int:
    with _call_id_lock:
        return next(_call_ids)


def _new_record(
    api: str,
    *,
    role: str | None,
    target: str | int | None,
    stage_index: int | None,
    selected: bool | None,
    hwnd: int | None,
    x: int | None,
    y: int | None,
    alt: bool | None,
    vk: int | None,
    delta_units: int | None,
) -> dict[str, object]:
    """构造 begin 行；字段固定为 spec §8.4 的 16 个，顺序固定。"""
    return {
        "t_ns": time.monotonic_ns(),
        "event": "begin",
        "call_id": _next_call_id(),
        "api": api,
        "role": role,
        "target": target,
        "stage_index": stage_index,
        "selected": selected,
        "hwnd": hwnd,
        "x": x,
        "y": y,
        "alt": alt,
        "vk": vk,
        "delta_units": delta_units,
        "status": "started",
        "error": None,
    }


def _end_record(begin: dict[str, object], status: str, error: str | None) -> dict[str, object]:
    """end 行保留同一调用的全部参数（与 begin 一致），只换时间/事件/状态/错误。"""
    return {
        **begin,
        "t_ns": time.monotonic_ns(),
        "event": "end",
        "status": status,
        "error": error,
    }


@contextmanager
def _observe_input(
    api: str,
    *,
    hwnd: int | None = None,
    x: int | None = None,
    y: int | None = None,
    alt: bool | None = None,
    vk: int | None = None,
    delta_units: int | None = None,
) -> Iterator[None]:
    """包住一次逻辑输入调用。未启用 capture_inputs 时不取 call_id、不发事件。"""
    sink = _input_sink.get()
    if sink is None:
        yield
        return
    ctx = _input_context.get()
    begin = _new_record(
        api,
        role=ctx.role if ctx is not None else None,
        target=ctx.target if ctx is not None else None,
        stage_index=ctx.stage_index if ctx is not None else None,
        selected=ctx.selected if ctx is not None else None,
        hwnd=hwnd,
        x=x,
        y=y,
        alt=alt,
        vk=vk,
        delta_units=delta_units,
    )
    sink(begin)
    try:
        yield
    except BaseException as exc:
        status = "stopped" if isinstance(exc, RunnerStopped) else "failed"
        try:
            sink(_end_record(begin, status, type(exc).__name__))
        except Exception:
            # 观察回调自己出错也不能改变输入语义：原异常必须原样传播。
            _input_log.exception("输入观察回调上报失败时抛异常，已忽略以保留原异常")
        raise
    else:
        sink(_end_record(begin, "completed", None))


@contextmanager
def capture_inputs(sink: Callable[[dict[str, object]], None]) -> Iterator[None]:
    """在该执行上下文里把输入调用交给 sink 观察；可嵌套，退出时恢复前一个 sink。

    sink 收到原始字典（字段见 spec §8.4），只做记录，不得吞异常或重发输入。
    """
    token = _input_sink.set(sink)
    try:
        yield
    finally:
        _input_sink.reset(token)


@contextmanager
def input_context(
    role: str,
    *,
    target: str | int | None = None,
    stage_index: int | None = None,
    selected: bool | None = None,
) -> Iterator[None]:
    """给当前执行上下文里的输入附加角色元数据；可嵌套，退出时恢复前值。

    元数据只用于观察记录，不改变坐标、动作或既有匹配行为。
    """
    token = _input_context.set(_InputContext(role, target, stage_index, selected))
    try:
        yield
    finally:
        _input_context.reset(token)


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


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


_TOKEN_INTEGRITY_LEVEL = 25
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008

# ⚠️ 64 位下必须声明 argtypes/restype：SID 是 8 字节指针，不声明时 ctypes 会把传入的
# 大整数指针按 32 位 int 转换 → OverflowError: int too long to convert。
advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)


def _integrity_level(pid: int) -> int:
    """进程完整性级别（0x2000 Medium / 0x3000 High）；读不到返回 -1。

    GetSidSubAuthority* 的 argtypes/restype 在模块级已声明（64 位 SID 指针不声明会溢出）。
    """
    process = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return -1
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
            return -1
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
            if not size.value:
                return -1
            buf = ctypes.create_string_buffer(size.value)
            ok = advapi32.GetTokenInformation(
                token, _TOKEN_INTEGRITY_LEVEL, buf, size.value, ctypes.byref(size)
            )
            if not ok:
                return -1
            label = ctypes.cast(buf, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
            count = advapi32.GetSidSubAuthorityCount(label.Label.Sid).contents.value
            return int(advapi32.GetSidSubAuthority(label.Label.Sid, count - 1).contents.value)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(process)


def input_blocked_reason(hwnd: int) -> str | None:
    """目标窗口进程权限高于本进程时返回提示；能正常发输入则返回 None。

    Windows 的 UIPI 只允许把合成输入发给「同级或更低」完整性级别的进程。
    游戏若以管理员(High)运行、本工具是普通权限(Medium)，SendInput 与
    PostMessage(WM_KEYDOWN/WM_LBUTTONDOWN) 都会被**静默丢弃**——SendInput
    还会返回"成功"，现场表现就是「截图正常、认页正常，但按键/点击完全没反应」。
    """
    target = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target))
    game_il = _integrity_level(int(target.value))
    self_il = _integrity_level(int(kernel32.GetCurrentProcessId()))
    if game_il < 0 or self_il < 0:
        return None  # 读不到就别拦，免得误伤
    if game_il > self_il:
        return (
            f"游戏进程权限高于本工具（游戏 IL=0x{game_il:04X}，本工具 IL=0x{self_il:04X}），"
            "Windows(UIPI) 会拦掉全部合成输入，按键和点击都发不出去。"
            "请右键本工具选「以管理员身份运行」后再试。"
        )
    return None


def focus_window(hwnd: int) -> None:
    checkpoint_input()
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
    checkpoint_input()
    with _observe_input("click_client", hwnd=hwnd, x=x, y=y, alt=alt):
        _input_log.info("click (%d,%d) alt=%s", x, y, alt)
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
    """把光标移到窗口客户区 (x,y) 后滚动。delta 负数向下翻页。一格约 120。

    保持既有调用兼容（一次缩放 delta、自带聚焦/移光标/随机等待）。批量按行
    发送请用 send_wheel_event，不要用这个。"""
    checkpoint_input()
    with _observe_input("wheel_client", hwnd=hwnd, x=x, y=y, delta_units=delta):
        _input_log.info("wheel (%d,%d) delta=%d", x, y, delta)
        sx, sy = client_to_screen(hwnd, x, y)
        focus_window(hwnd)
        user32.SetCursorPos(sx, sy)
        pause(0.04, 0.08)
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta * 120, 0)
        time.sleep(0.15)
        pause()


def send_wheel_event(delta_units: int) -> None:
    """只发送一条原始滚轮事件（spec §5.2）。

    不聚焦、不移光标、不做随机等待、不发多条。调用方（scroll_rows）负责
    开始时聚焦与光标定位，并按标定间隔在事件之间自行等待。

    delta_units 为滚轮格数，一格固定 120：负数向下、正数向上。禁止用一条
    放大的 delta 代替已标定的多事件序列——游戏可能对单条大事件限幅或合并。
    """
    if delta_units == 0:
        raise InputError("滚轮事件格数不能为 0。")
    checkpoint_input()
    with _observe_input("send_wheel_event", delta_units=delta_units):
        _input_log.info("wheel_event delta_units=%d", delta_units)
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta_units * 120, 0)


def press_key_once(hwnd: int, vk: int) -> None:
    """只按一次键（down+up 一对）。R 弃置这类开关型键必须用这个：
    press_key 会发多对 down/up，R 按奇数次才有效，偶数次等于没按。"""
    checkpoint_input()
    with _observe_input("press_key_once", hwnd=hwnd, vk=vk):
        _input_log.info("key_once vk=0x%02X", vk)
        focus_window(hwnd)
        _send([_key_event(vk), _key_event(vk, scancode=True)])
        time.sleep(0.05)
        _send([_key_event(vk, up=True), _key_event(vk, up=True, scancode=True)])
        time.sleep(0.03)
        pause(0.35, 0.55)


def press_key(hwnd: int, vk: int = VK_ESCAPE) -> None:
    checkpoint_input()
    with _observe_input("press_key", hwnd=hwnd, vk=vk):
        _input_log.info("key vk=0x%02X", vk)
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
