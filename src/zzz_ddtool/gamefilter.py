from __future__ import annotations

import time
from collections.abc import Sequence

from zzz_ddtool.layout import DISMANTLE_FORBID, FILTER_BTN, FILTER_RESET, FILTER_SLOTS, scale_point, scale_rect
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.wininput import VK_ESCAPE, click_client, press_key


class FilterClickError(RuntimeError):
    pass


def _refuse_dismantle(x: int, y: int, width: int, height: int) -> None:
    left, top, right, bottom = scale_rect(*DISMANTLE_FORBID, width, height)
    if left <= x <= right and top <= y <= bottom:
        raise FilterClickError(f"坐标 ({x},{y}) 落在拆解按钮上，已拒绝点击。")


def apply_game_filter(
    hwnd: int,
    width: int,
    height: int,
    *,
    slots: Sequence[int],
    sets: Sequence[str],
    mainstats: Sequence[str],
) -> None:
    """Open 驱动盘筛选, 重置, apply slots, Esc to close.

    能按键就按键：关筛选用 Esc，不扫关闭按钮。
    """
    log = get_logger("zzz_ddtool.gamefilter")
    fx, fy = scale_point(*FILTER_BTN, width, height)
    _refuse_dismantle(fx, fy, width, height)
    click_client(hwnd, fx, fy)
    time.sleep(0.55)
    rx, ry = scale_point(*FILTER_RESET, width, height)
    click_client(hwnd, rx, ry)
    time.sleep(0.45)
    for slot in slots:
        if slot not in FILTER_SLOTS:
            continue
        sx, sy = scale_point(*FILTER_SLOTS[slot], width, height)
        click_client(hwnd, sx, sy)
        time.sleep(0.12)
    if sets or mainstats:
        log.info("game filter saved sets=%s mainstats=%s; picker clicks next", list(sets), list(mainstats))
    press_key(hwnd, VK_ESCAPE)
    time.sleep(0.35)
    log.info("game filter reset then slots=%s closed=esc", list(slots))
