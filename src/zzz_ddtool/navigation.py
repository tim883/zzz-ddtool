"""导航环境身份、滚动标定与按行移动（spec v0.8 §4.3、§5.1、§5.2、§5.4）。

本模块承载 `scroll_rows` 的发送侧与 `navigation-profile.json` 缓存。全程不读取、
不使用任何滑块结果（spec §5.4 第 243 行）。

真机标定基线（2026-10-02，1920×1080，证据见 log/captures/）：
- 置顶（切音擎 → 切回驱动盘）精确回到列表顶部，与原点图垂直位移 0px；
- 1 条原始滚轮事件（一次 -120）= 1 行 ≈ 174.83px；1/2 格实测分别为 1/2 行；
- 选中框左右竖线中心在 cx-62 / cx+64，扫单列即可读出锚点所在屏幕行；
- 点击视口末行会引起游戏自动居中（spec §5.1 第 203 行、§5.3 第 239 行），
  正常遍历中途不点首/末行。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from zzz_ddtool.logutil import get_logger

SCHEMA_VERSION = 1
CALIBRATION_ALGORITHM_VERSION = 1
# 单条滚轮事件的原始单位（spec §5.1 缓存表：单事件滚轮单位固定 120）
WHEEL_UNIT = 120
# spec §5.1 第 200 行：初始 100ms，重复性/两行连发失败才依次放宽
EVENT_INTERVAL_CANDIDATES_MS = (100, 200, 300)
# spec §5.1 第 2 条：n 达到 16 只记一次告警，16 不是硬失败阈值
ADVISORY_EVENT_COUNT = 16
# spec §5.1 第 2 条锚点：完整可见的第 3 行（0 基屏幕行 2）
ANCHOR_SCREEN_ROW0 = 2
# spec §5.1 第 207 行标定预算
PER_INTERVAL_BUDGET_S = 600.0
TOTAL_BUDGET_S = 2400.0
# 滚轮落点：网格中间行区域，避开右侧详情与悬浮层（spec §5.2 第 223 行）
SCROLL_CURSOR = (706, 442)
PROFILE_NAME = "navigation-profile.json"


class NavigationError(RuntimeError):
    pass


# ---------------------------------------------------------------- 环境身份


def machine_id() -> str | None:
    """本机 MachineGuid 的 SHA-256；读取失败返回 None（spec §5.1 第 3 条）。

    64 位 Windows 明确读 64 位视图；不记录原始值、不改用用户名或随机值。
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "MachineGuid")
    except OSError:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def window_dpi(hwnd: int) -> int | None:
    """绑定 hwnd 的 GetDpiForWindow；API 不可用或返回 0 时返回 None，不伪填 96。"""
    user32 = ctypes.windll.user32
    if not hasattr(user32, "GetDpiForWindow"):
        return None
    try:
        value = int(user32.GetDpiForWindow(hwnd))
    except Exception:
        return None
    return value or None


class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwStrucVersion", ctypes.c_uint32),
        ("dwFileVersionMS", ctypes.c_uint32),
        ("dwFileVersionLS", ctypes.c_uint32),
        ("dwProductVersionMS", ctypes.c_uint32),
        ("dwProductVersionLS", ctypes.c_uint32),
        ("dwFileFlagsMask", ctypes.c_uint32),
        ("dwFileFlags", ctypes.c_uint32),
        ("dwFileOS", ctypes.c_uint32),
        ("dwFileType", ctypes.c_uint32),
        ("dwFileSubtype", ctypes.c_uint32),
        ("dwFileDateMS", ctypes.c_uint32),
        ("dwFileDateLS", ctypes.c_uint32),
    ]


def file_version_quad(path: Path) -> str | None:
    """exe 版本资源四元组 a.b.c.d；读取失败返回 None（spec §5.1 第 2 条）。"""
    version = ctypes.windll.version
    try:
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buf):
            return None
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not version.VerQueryValueW(buf, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None
        if length.value < ctypes.sizeof(_VS_FIXEDFILEINFO):
            return None
        info = ctypes.cast(pointer, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
        return "{}.{}.{}.{}".format(
            info.dwFileVersionMS >> 16,
            info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16,
            info.dwFileVersionLS & 0xFFFF,
        )
    except Exception:
        return None


def game_identity(hwnd: int) -> tuple[str | None, str | None]:
    """绑定实例的 (规范化 exe 绝对路径, 构建标识)；任一读不到返回 None。

    构建标识 = 版本四元组｜字节长度｜mtime_ns（spec §5.1 缓存表）。
    """
    from zzz_ddtool.capture import process_exe_path, window_pid

    try:
        raw = process_exe_path(window_pid(hwnd))
    except Exception:
        return None, None
    if not raw:
        return None, None
    path = Path(raw)
    try:
        stat = path.stat()
    except OSError:
        return None, None
    quad = file_version_quad(path)
    if not quad:
        return None, None
    normalized = str(path).lower()
    build = f"{quad}|{stat.st_size}|{stat.st_mtime_ns}"
    return normalized, build


@dataclass(frozen=True)
class Environment:
    """一个任务绑定的运行环境身份（spec §5.1 缓存适用条件）。"""

    machine_id: str | None
    client_width: int
    client_height: int
    window_dpi: int | None
    game_exe_path: str | None
    game_build: str | None

    @property
    def persistable(self) -> bool:
        """身份字段齐全才允许写持久缓存（spec §5.1 第 3-4 条）。"""
        return all(
            (
                self.machine_id,
                self.window_dpi,
                self.game_exe_path,
                self.game_build,
                self.client_width,
                self.client_height,
            )
        )


def probe_environment(hwnd: int) -> Environment:
    """采集绑定实例的环境身份。任一字段读不到只影响缓存，不影响本轮运行。"""
    from zzz_ddtool.capture import client_size

    width, height = client_size(hwnd)
    exe_path, build = game_identity(hwnd)
    return Environment(
        machine_id=machine_id(),
        client_width=int(width),
        client_height=int(height),
        window_dpi=window_dpi(hwnd),
        game_exe_path=exe_path,
        game_build=build,
    )


# ---------------------------------------------------------------- 缓存读写


def profile_path() -> Path:
    from zzz_ddtool.paths import user_dir

    return user_dir() / PROFILE_NAME


@dataclass(frozen=True)
class NavProfile:
    """本机滚轮协议与环境（spec §5.1 缓存表，schema_version=1）。"""

    machine_id: str
    client_width: int
    client_height: int
    window_dpi: int
    game_exe_path: str
    game_build: str
    row_pitch_px: float
    origin_center_y: float
    events_per_row: int
    event_interval_ms: int
    settle_wait_ms: int
    schema_version: int = SCHEMA_VERSION
    calibration_algorithm_version: int = CALIBRATION_ALGORITHM_VERSION

    def matches(self, env: Environment) -> bool:
        """适用条件全等才算可用；任一不符、缺字段或版本不符一律忽略。"""
        if self.schema_version != SCHEMA_VERSION:
            return False
        if self.calibration_algorithm_version != CALIBRATION_ALGORITHM_VERSION:
            return False
        return (
            env.machine_id == self.machine_id
            and env.client_width == self.client_width
            and env.client_height == self.client_height
            and env.window_dpi == self.window_dpi
            and env.game_exe_path == self.game_exe_path
            and env.game_build == self.game_build
            and self.row_pitch_px > 0
            and self.events_per_row >= 1
            and self.event_interval_ms > 0
            and self.settle_wait_ms >= 0
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_profile(env: Environment) -> NavProfile | None:
    """读取并校验缓存；损坏/非法/不适用一律返回 None，由调用方重新标定。"""
    log = get_logger("zzz_ddtool.navigation")
    path = profile_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("导航缓存不可读（%s），忽略并重新标定", exc)
        return None
    try:
        profile = NavProfile(**raw)
    except TypeError as exc:
        log.warning("导航缓存字段不符（%s），忽略并重新标定", exc)
        return None
    if not profile.matches(env):
        log.info("导航缓存环境不匹配，忽略并重新标定")
        return None
    return profile


def save_profile(profile: NavProfile) -> Path:
    """写缓存；只在本机身份齐全时调用（spec §5.1 第 3-4 条）。"""
    path = profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(profile.as_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------- 布局与锚点


def is_empty_warehouse(image) -> bool:
    """置顶后的驱动盘页：数量为零且全部格位为空才确认空结果。

    不把缺少等级标签或黑屏当作空列表。固定网格仅用于验证空占位，不替代布局测量。
    """
    import re

    from zzz_ddtool.equipped import tile_is_empty
    from zzz_ddtool.layout import GRID_COLS, GRID_ROWS, GRID_ORIGIN, GRID_STEP, scale_point
    from zzz_ddtool.ocr import read_rect
    from zzz_ddtool.screen import DRIVE_DISC, is_page

    if not is_page(image, DRIVE_DISC):
        return False
    counts = [re.search(r"[\[【]?\s*(\d+)\s*[/／]\s*\d+", line.text)
              for line in read_rect(image, (60, 105, 420, 175))]
    if not any(match is not None and int(match.group(1)) == 0 for match in counts):
        return False
    return all(
        tile_is_empty(image, *scale_point(
            GRID_ORIGIN[0] + col * GRID_STEP[0], GRID_ORIGIN[1] + row * GRID_STEP[1],
            image.width(), image.height(),
        )) is True
        for row in range(GRID_ROWS) for col in range(GRID_COLS)
    )


def measure_layout(image) -> tuple[float, float]:
    """原点图测 (origin_center_y, row_pitch_px)（spec §4.1 第 1-2 条）。

    等级标签中心减去 `GRID_LABEL_DY` 得图标行中心，同行取中位数；相邻完整行
    中心差的中位数即行距。只有一行时用缩放后的既有布局值作只读参考，不做滚动
    标定。行距不由"请求移动一行"反推。
    """
    from zzz_ddtool.layout import GRID_LABEL_DY, LABEL_SCAN_AREA, scale_point
    from zzz_ddtool.ocr import read_rect

    width, height = image.width(), image.height()
    label_rows: list[float] = []
    for line in read_rect(image, LABEL_SCAN_AREA):
        text = line.text.replace(" ", "")
        if not text.startswith("等级"):
            continue
        label_rows.append((line.top + line.bottom) / 2.0)
    if not label_rows:
        raise NavigationError("原点图读不到「等级」标签，无法测量网格布局。")

    label_rows.sort()
    gap = float(scale_point(0, 60, width, height)[1])
    clusters: list[list[float]] = []
    for y in label_rows:
        if clusters and abs(y - clusters[-1][-1]) <= gap:
            clusters[-1].append(y)
        else:
            clusters.append([y])

    label_dy = float(scale_point(0, GRID_LABEL_DY, width, height)[1])
    centers = [statistics.median(cluster) - label_dy for cluster in clusters]
    origin = float(centers[0])
    if len(centers) >= 2:
        pitch = float(statistics.median([b - a for a, b in zip(centers, centers[1:])]))
    else:
        pitch = 175.0 * height / 1080.0
    return origin, pitch


def anchor_screen_rows(
    image,
    *,
    col0: int,
    origin_center_y: float,
    row_pitch_px: float,
    top_row0: int = 0,
    phase_px: float = 0.0,
    span: range = range(-8, 10),
) -> list[int]:
    """扫单列，返回可见选中框所在的屏幕逻辑行（0 基，相对当前视口）。

    选中框是同一件盘的固有标记，纵向滚动不改变其列；扫一列即可，成本与列表
    长度无关。返回空列表表示这一列没有选中框（可能锚点已滚出视口）。
    """
    from zzz_ddtool.equipped import tile_is_selected
    from zzz_ddtool.layout import GRID_ORIGIN, GRID_STEP, scale_point

    width, height = image.width(), image.height()
    origin_x = scale_point(*GRID_ORIGIN, width, height)[0]
    step_x = scale_point(*GRID_STEP, width, height)[0]
    cx = origin_x + col0 * step_x
    design_cx = round(cx * 1920 / width)
    hits: list[int] = []
    for screen_row0 in span:
        global_row0 = top_row0 + screen_row0
        if global_row0 < 0:
            continue
        cy = origin_center_y + screen_row0 * row_pitch_px - phase_px
        if not (0 <= cy < height):
            continue
        design_cy = round(cy * 1080 / height)
        if tile_is_selected(image, design_cx, design_cy):
            hits.append(screen_row0)
    return hits


# ---------------------------------------------------------------- 按行发送


@dataclass(frozen=True)
class ScrollSend:
    """一次按行发送的记录。请求量与实际位移分开（spec §4.3）。"""

    requested_rows: int
    total_events: int
    sent_events: int
    interval_ms: int

    @property
    def complete(self) -> bool:
        return self.sent_events >= self.total_events


def _place_cursor(hwnd: int) -> None:
    """批次开始时聚焦一次并把光标放到网格中间行（spec §5.2 第 219/223 行）。"""
    from zzz_ddtool.capture import client_size
    from zzz_ddtool.layout import scale_point
    from zzz_ddtool.wininput import client_to_screen, focus_window

    focus_window(hwnd)
    x, y = scale_point(*SCROLL_CURSOR, *client_size(hwnd))
    sx, sy = client_to_screen(hwnd, x, y)
    ctypes.windll.user32.SetCursorPos(sx, sy)
    time.sleep(0.06)


def scroll_rows(
    hwnd: int,
    signed_rows: int,
    profile: NavProfile,
    *,
    before_event: Any = None,
) -> ScrollSend:
    """按标定协议发送 signed_rows 行（正数向下、负数向上）。

    spec §5.2：一批移动内部只聚焦一次；光标放在网格中间行；每个事件前检查暂停/
    停止；按标定间隔发送独立事件，不用一条放大的 delta 顶替。发送完成仍只是
    预测视口，实际位置由调用方用锚点截图确认后才提交。
    """
    from zzz_ddtool.wininput import send_wheel_event

    if signed_rows == 0:
        raise NavigationError("按行移动的行数不能为 0。")
    if profile.events_per_row < 1:
        raise NavigationError(f"标定协议非法：events_per_row={profile.events_per_row}")

    unit = -1 if signed_rows > 0 else 1
    rows = abs(int(signed_rows))
    total = rows * profile.events_per_row

    _place_cursor(hwnd)

    sent = 0
    interval = profile.event_interval_ms / 1000.0
    for index in range(total):
        if before_event is not None:
            before_event(index)
        send_wheel_event(unit)
        sent += 1
        if index + 1 < total:
            time.sleep(interval)
    return ScrollSend(
        requested_rows=signed_rows,
        total_events=total,
        sent_events=sent,
        interval_ms=profile.event_interval_ms,
    )


def settle_and_read_anchor(
    hwnd: int,
    *,
    col0: int,
    origin_center_y: float,
    row_pitch_px: float,
    top_row0: int = 0,
    phase_px: float = 0.0,
    budget_ms: int = 2000,
    poll_ms: int = 150,
):
    """发送结束后轮询抓图，直到锚点屏幕行连续两次一致。

    返回 (rows, elapsed_ms, image)；elapsed_ms 即实测的渲染等待量，
    写进 navigation-profile 的 settle_wait_ms。
    """
    from zzz_ddtool.capture import grab_game_image

    log = get_logger("zzz_ddtool.navigation")
    start = time.monotonic()
    previous: list[int] | None = None
    rows: list[int] = []
    image = None
    while True:
        image, _method, _hwnd = grab_game_image(hwnd)
        rows = anchor_screen_rows(
            image,
            col0=col0,
            origin_center_y=origin_center_y,
            row_pitch_px=row_pitch_px,
            top_row0=top_row0,
            phase_px=phase_px,
        )
        elapsed = int((time.monotonic() - start) * 1000)
        if rows and previous is not None and rows == previous:
            log.info("锚点稳定：rows=%s settle=%dms", rows, elapsed)
            return rows, elapsed, image
        previous = rows
        if elapsed >= budget_ms:
            log.warning("锚点在 %dms 内未稳定：rows=%s", elapsed, rows)
            return rows, elapsed, image
        time.sleep(poll_ms / 1000)


# ---------------------------------------------------------------- 短距标定


@dataclass(frozen=True)
class CalibrationAttempt:
    """一个候选事件数的实测结果（spec §5.1 第 2 条）。"""

    interval_ms: int
    events: int
    displacement_px: float
    verdict: str
    stopped: bool


def calibrate(
    hwnd: int,
    *,
    env: Environment | None = None,
    intervals: tuple[int, ...] = EVENT_INTERVAL_CANDIDATES_MS,
    max_events: int = ADVISORY_EVENT_COUNT,
    total_budget_s: float = TOTAL_BUDGET_S,
    on_attempt: Any = None,
) -> NavProfile:
    """非破坏性短距自动标定，返回可用协议（spec §5.1）。

    只切页、选中中间行的盘查看和滚动；不强化、不打标。每个候选重新置顶、选中
    完整可见的第 3 行锚点，再向下发送 n 条事件；位移落在行距 ±2% 内才算候选，
    明显不足一行继续 n+1，超过一行则本档无合格整数协议、放宽间隔。
    """
    from zzz_ddtool.capture import grab_game_image
    from zzz_ddtool.nav import reset_to_top
    from zzz_ddtool.traversal import CAL_OK, CAL_OVER, rate_candidate
    from zzz_ddtool.wininput import click_client

    log = get_logger("zzz_ddtool.navigation")
    env = env or probe_environment(hwnd)
    started = time.monotonic()
    anchor_col = 4  # 第 5 列：左右都有邻格，避开被边缘裁切的列
    attempts: list[CalibrationAttempt] = []

    for interval_ms in intervals:
        interval_started = time.monotonic()
        probe = NavProfile(
            machine_id=env.machine_id or "",
            client_width=env.client_width,
            client_height=env.client_height,
            window_dpi=env.window_dpi or 0,
            game_exe_path=env.game_exe_path or "",
            game_build=env.game_build or "",
            row_pitch_px=0.0,
            origin_center_y=0.0,
            events_per_row=1,
            event_interval_ms=int(interval_ms),
            settle_wait_ms=0,
        )
        for events in range(1, max_events + 1):
            if time.monotonic() - started > total_budget_s:
                raise NavigationError("标定总预算耗尽，协议未验证。")
            if time.monotonic() - interval_started > PER_INTERVAL_BUDGET_S:
                log.warning("间隔 %dms 单档预算耗尽，换下一档", interval_ms)
                break

            reset_to_top()
            image, _method, _hwnd = grab_game_image(hwnd)
            origin_center_y, pitch = measure_layout(image)
            click_client(hwnd, _anchor_click_x(image, anchor_col), _anchor_click_y(origin_center_y, pitch))
            time.sleep(0.6)
            image, _method, _hwnd = grab_game_image(hwnd)
            before = anchor_screen_rows(
                image, col0=anchor_col, origin_center_y=origin_center_y, row_pitch_px=pitch
            )
            if before != [ANCHOR_SCREEN_ROW0]:
                raise NavigationError(
                    f"标定锚点没选上（读到屏幕行 {before}，期望 [{ANCHOR_SCREEN_ROW0}]）。"
                )

            scroll_rows(hwnd, events, probe)
            after_rows, settle_ms, _image = settle_and_read_anchor(
                hwnd,
                col0=anchor_col,
                origin_center_y=origin_center_y,
                row_pitch_px=pitch,
            )
            if not after_rows:
                stopped = True
                displacement = 0.0
                verdict = "stopped"
            else:
                advanced = ANCHOR_SCREEN_ROW0 - min(after_rows)
                stopped = advanced <= 0
                displacement = advanced * pitch
                verdict = rate_candidate(displacement, pitch)
            attempt = CalibrationAttempt(
                interval_ms=int(interval_ms),
                events=events,
                displacement_px=float(displacement),
                verdict=verdict,
                stopped=stopped,
            )
            attempts.append(attempt)
            if on_attempt is not None:
                on_attempt(attempt)
            log.info(
                "候选 interval=%dms events=%d displacement=%.1fpx verdict=%s stopped=%s",
                interval_ms,
                events,
                displacement,
                verdict,
                stopped,
            )
            if stopped:
                raise NavigationError(
                    f"候选 {events} 条事件被端点截停，不能用于选定一行协议（interval={interval_ms}ms）。"
                )
            if verdict == CAL_OK:
                profile = NavProfile(
                    machine_id=env.machine_id or "",
                    client_width=env.client_width,
                    client_height=env.client_height,
                    window_dpi=env.window_dpi or 0,
                    game_exe_path=env.game_exe_path or "",
                    game_build=env.game_build or "",
                    row_pitch_px=float(pitch),
                    origin_center_y=float(origin_center_y),
                    events_per_row=int(events),
                    event_interval_ms=int(interval_ms),
                    settle_wait_ms=int(settle_ms),
                )
                if events >= ADVISORY_EVENT_COUNT:
                    log.warning("候选事件数较多：%d（spec §5.1 第 2 条只告警不失败）", events)
                if not _verify_protocol(hwnd, profile, anchor_col):
                    raise NavigationError(
                        "一行协议未通过正反向与连续两行验证（spec §5.1 第 3 条）。"
                    )
                reset_to_top()
                return profile
            if verdict == CAL_OVER:
                log.warning("间隔 %dms 下 %d 条事件超过一行，本档无合格整数协议", interval_ms, events)
                break

    raise NavigationError("所有候选间隔都未得到合格的一行协议，标定失败。")


def _anchor_click_x(image, col0: int) -> int:
    from zzz_ddtool.layout import GRID_COLS, GRID_ORIGIN, GRID_STEP, scale_point

    if not (0 <= col0 < GRID_COLS):
        raise NavigationError(f"锚点列越界：{col0}")
    origin_x = scale_point(*GRID_ORIGIN, image.width(), image.height())[0]
    step_x = scale_point(*GRID_STEP, image.width(), image.height())[0]
    return origin_x + col0 * step_x


def _anchor_click_y(origin_center_y: float, row_pitch_px: float) -> int:
    return int(round(origin_center_y + ANCHOR_SCREEN_ROW0 * row_pitch_px))


def _verify_protocol(hwnd: int, profile: NavProfile, anchor_col: int) -> bool:
    """验证正、反向一行与连续两行，各连续两次且未被端点截停（spec §5.1 第 3 条）。

    向上验证必须从已确认的非顶部位置开始：先向下移动 rows 行再向上回移，避免在
    顶部被截停而误判成协议失败。
    """
    from zzz_ddtool.capture import grab_game_image
    from zzz_ddtool.nav import reset_to_top
    from zzz_ddtool.wininput import click_client

    log = get_logger("zzz_ddtool.navigation")

    def _prepare(rows: int, direction: int) -> tuple[float, float, int]:
        reset_to_top()
        image, _method, _hwnd = grab_game_image(hwnd)
        origin, pitch = measure_layout(image)
        click_client(hwnd, _anchor_click_x(image, anchor_col), _anchor_click_y(origin, pitch))
        time.sleep(0.6)
        image, _method, _hwnd = grab_game_image(hwnd)
        base = anchor_screen_rows(
            image, col0=anchor_col, origin_center_y=origin, row_pitch_px=pitch
        )
        if base != [ANCHOR_SCREEN_ROW0]:
            raise NavigationError(f"验证锚点没选上（读到 {base}）。")
        if direction < 0:
            scroll_rows(hwnd, rows, profile)
            rows_after, _settle, _image = settle_and_read_anchor(
                hwnd, col0=anchor_col, origin_center_y=origin, row_pitch_px=pitch
            )
            if len(rows_after) != 1:
                raise NavigationError(f"向上验证前锚点不唯一：{rows_after}")
            return origin, pitch, rows_after[0]
        return origin, pitch, ANCHOR_SCREEN_ROW0

    for rows, direction in ((1, 1), (1, -1), (2, 1), (2, -1)):
        for round_index in range(2):
            origin, pitch, base = _prepare(rows, direction)
            scroll_rows(hwnd, rows * direction, profile)
            after_rows, _settle, _image = settle_and_read_anchor(
                hwnd, col0=anchor_col, origin_center_y=origin, row_pitch_px=pitch
            )
            expected = base - rows * direction
            if after_rows != [expected]:
                log.warning(
                    "验证失败 rows=%s dir=%s 第%d次：期望 [%s] 实读 %s",
                    rows,
                    direction,
                    round_index + 1,
                    expected,
                    after_rows,
                )
                return False
    return True
