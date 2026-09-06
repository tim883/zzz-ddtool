from __future__ import annotations

import time
from collections.abc import Sequence
from difflib import SequenceMatcher

from PySide6.QtGui import QImage

from zzz_ddtool.capture import grab_game_image
from zzz_ddtool.layout import (
    DISMANTLE_FORBID,
    FILTER_BTN,
    FILTER_RESET,
    FILTER_SLOTS,
    scale_point,
    scale_rect,
)
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.ocr import read_rect
from zzz_ddtool.wininput import VK_ESCAPE, click_client, press_key, wheel_client

# 筛选面板里「套装 → 点击选择」条的搜索区（面板右半屏）
FILTER_SETS_BAR = (1300, 260, 1860, 380)
# 弹层 / 词条按钮的全窗口搜索区。认不上的话宁可中断。
FULL_WINDOW = (0, 0, 1920, 1080)
# 滚动找词条时的尝试上限
_MAX_SCROLL_ROUNDS = 8


class FilterClickError(RuntimeError):
    pass


def _refuse_dismantle(x: int, y: int, width: int, height: int) -> None:
    left, top, right, bottom = scale_rect(*DISMANTLE_FORBID, width, height)
    if left <= x <= right and top <= y <= bottom:
        raise FilterClickError(f"坐标 ({x},{y}) 落在拆解按钮上，已拒绝点击。")


def _grab() -> tuple[QImage, int]:
    image, _method, hwnd = grab_game_image()
    return image, hwnd


def _find_and_click(
    hwnd: int,
    needle: str,
    region: tuple[int, int, int, int],
    *,
    width: int,
    height: int,
    cutoff: float = 0.75,
    refuse_dismantle: bool = False,
) -> bool:
    """截屏 OCR，在 region 里找含 needle 的文本行并点中心。找到返回 True。"""
    image, _ = _grab()
    for line in read_rect(image, region):
        ratio = SequenceMatcher(None, needle, line.text).ratio()
        if needle not in line.text and ratio < cutoff:
            continue
        cx = (line.left + line.right) // 2
        cy = (line.top + line.bottom) // 2
        if refuse_dismantle:
            _refuse_dismantle(cx, cy, width, height)
        click_client(hwnd, cx, cy)
        return True
    return False


def _select_names(
    hwnd: int,
    names: Sequence[str],
    *,
    width: int,
    height: int,
    scroll_center: tuple[int, int],
) -> None:
    """在当前弹层里逐个点名。找不到就滚动重找，超过上限抛 FilterClickError。"""
    log = get_logger("zzz_ddtool.gamefilter")
    pending = list(names)
    if not pending:
        return
    for _round in range(_MAX_SCROLL_ROUNDS):
        image, _ = _grab()
        lines = read_rect(image, FULL_WINDOW)
        still: list[str] = []
        for name in pending:
            hit = None
            for line in lines:
                if name in line.text or SequenceMatcher(None, name, line.text).ratio() >= 0.75:
                    hit = line
                    break
            if hit is None:
                still.append(name)
                continue
            click_client(hwnd, (hit.left + hit.right) // 2, (hit.top + hit.bottom) // 2)
            log.info("picker clicked %r at (%d,%d)", name, hit.left, hit.top)
            time.sleep(0.3)
        pending = still
        if not pending:
            return
        wheel_client(hwnd, *scale_point(*scroll_center, width, height))
        time.sleep(0.35)
    raise FilterClickError(f"弹层里找不到目标，滚动 {_MAX_SCROLL_ROUNDS} 轮未果：{pending}")


def _confirm_popup(hwnd: int) -> None:
    """关弹层必须且只能点「确认筛选」，绝不按 Esc（按了会取消勾选）。

    找不到按钮就中断，保持现状等人处理。
    """
    if not _find_and_click(hwnd, "确认筛选", FULL_WINDOW, width=1920, height=1080, cutoff=0.8):
        raise FilterClickError("没找到「确认筛选」按钮，弹层未关闭。按手册 01 中断。")
    time.sleep(0.4)


def apply_sets_mainstats(
    hwnd: int,
    width: int,
    height: int,
    *,
    sets: Sequence[str],
    mainstats: Sequence[str],
) -> None:
    """在筛选面板里点开套装/主属性词条选择器并勾选。

    实测弹层（setpicker-live.png）：图标+名字网格，底部「重置 / 确认筛选」。
    全部靠 OCR 找字点击；认不上就中断（手册 01 底线）。
    """
    log = get_logger("zzz_ddtool.gamefilter")
    if sets:
        if not _find_and_click(hwnd, "点击选择", FILTER_SETS_BAR, width=width, height=height):
            raise FilterClickError("筛选面板里没找到「点击选择」条。")
        time.sleep(0.6)
        _select_names(hwnd, sets, width=width, height=height, scroll_center=(960, 540))
        _confirm_popup(hwnd)
        log.info("sets picked: %s", list(sets))
    if mainstats:
        # 主属性词条区在面板下部；先滚到面板底再点名
        _select_names(hwnd, mainstats, width=width, height=height, scroll_center=(1560, 700))
        _confirm_popup(hwnd)
        log.info("mainstats picked: %s", list(mainstats))


def apply_game_filter(
    hwnd: int,
    width: int,
    height: int,
    *,
    slots: Sequence[int],
    sets: Sequence[str],
    mainstats: Sequence[str],
) -> None:
    """Open 驱动盘筛选, 重置, apply slots/sets/mainstats, Esc to close.

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
        apply_sets_mainstats(hwnd, width, height, sets=sets, mainstats=mainstats)
    press_key(hwnd, VK_ESCAPE)
    time.sleep(0.35)
    log.info("game filter done slots=%s sets=%s mains=%s", list(slots), list(sets), list(mainstats))
