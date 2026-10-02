from __future__ import annotations

import re
import time
from collections.abc import Sequence
from difflib import SequenceMatcher

from PySide6.QtGui import QImage

from zzz_ddtool.capture import grab_game_image
from zzz_ddtool.layout import (
    DISMANTLE_FORBID,
    FILTER_BTN,
    scale_point,
    scale_rect,
)
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.ocr import read_rect
from zzz_ddtool.wininput import VK_ESCAPE, click_client, press_key, wheel_client

# 筛选面板里「套装 → 点击选择」条的搜索区（面板右半屏）
FILTER_SETS_BAR = (1300, 260, 1860, 380)
# 「分区[N]」按钮搜索区。未滚动时在 y≈766-876，滚动后在 y≈429-574，两态都盖住
FILTER_SLOT_AREA = (1300, 380, 1860, 950)
# 面板底部「重置」按钮搜索区（避开右边的批量操作）
FILTER_RESET_AREA = (1250, 990, 1650, 1080)
# 弹层 / 词条按钮的全窗口搜索区。认不上的话宁可中断。
FULL_WINDOW = (0, 0, 1920, 1080)
# 滚动找词条时的尝试上限
_MAX_SCROLL_ROUNDS = 8
# 筛选面板中部滚轮落点。面板内容比视口高，「主属性词条」区默认在视口外。
FILTER_PANEL_SCROLL = (1560, 500)
# 主属性 / 副属性词条区的标题文字，用来框定主属性选项的纵向范围。
MAINSTAT_TITLE = "主属性词条"
SUBSTAT_TITLE = "副属性词条"
# 把面板滚到主属性区的尝试上限（真机实测 3~4 次滚轮即到底）
_MAX_PANEL_SCROLLS = 8
# 主属性名与词条名的相似度下限，低于它宁可中断也不猜（手册 01 底线）
_MATCH_CUTOFF = 0.75


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
                if name in line.text or SequenceMatcher(None, name, line.text).ratio() >= _MATCH_CUTOFF:
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


def _text_lines() -> list:
    """抓一帧，返回全窗口的 OCR 文本行。"""
    image, _ = _grab()
    return read_rect(image, FULL_WINDOW)


def _title_y(lines, title: str) -> int | None:
    """找标题行的中心 y（取最靠上的那条），找不到返回 None。"""
    found = None
    for line in lines:
        if title not in line.text.replace(" ", ""):
            continue
        y = (line.top + line.bottom) // 2
        if found is None or y < found:
            found = y
    return found


def _toggle_below(lines, title_y: int):
    """标题正下方的「点击展开 / 点击收起」开关，取 y 最小的一条。"""
    best = None
    for line in lines:
        y = (line.top + line.bottom) // 2
        if y <= title_y:
            continue
        text = line.text.replace(" ", "")
        if "点击" not in text or ("展开" not in text and "收起" not in text):
            continue
        if best is None or y < best[0]:
            best = (y, line)
    return best[1] if best else None


def _open_mainstat_section(hwnd: int, width: int, height: int) -> list:
    """把筛选面板滚到「主属性词条」区，并确保该区已展开。

    真机标定（2026-10-02，log/captures/probe-filter-open.png）：
    面板内容比视口高，滚到顶时「主属性词条」区在视口外；
    该区默认**收起**，只有一条「点击展开」，点开后才是
    「生命值 / 攻击力百分比 / …」这些复选框。
    此前直接点名，OCR 永远找不到主属性名，滚 8 轮后中断——B5 的根因。

    **返回**「已确认展开」那一帧的 OCR 文本行。紧接着调用的 `_pick_mainstats` 就在同一
    状态下点名，直接把这一帧交给它，省掉一次全屏 OCR（实测 1.3~1.5s，且中间无点击、
    帧不会变）。
    """
    log = get_logger("zzz_ddtool.gamefilter")
    for _round in range(_MAX_PANEL_SCROLLS + 1):
        lines = _text_lines()
        title_y = _title_y(lines, MAINSTAT_TITLE)
        if title_y is None:
            wheel_client(hwnd, *scale_point(*FILTER_PANEL_SCROLL, width, height))
            time.sleep(0.35)
            continue
        toggle = _toggle_below(lines, title_y)
        if toggle is None:
            raise FilterClickError("主属性词条区没找到「点击展开 / 点击收起」开关，中断。")
        if "收起" in toggle.text.replace(" ", ""):
            return lines
        click_client(hwnd, (toggle.left + toggle.right) // 2, (toggle.top + toggle.bottom) // 2)
        time.sleep(0.7)
        after = _text_lines()
        after_y = _title_y(after, MAINSTAT_TITLE)
        after_toggle = _toggle_below(after, after_y) if after_y is not None else None
        if after_toggle is not None and "收起" in after_toggle.text.replace(" ", ""):
            log.info("已点开主属性词条区")
            return after
        raise FilterClickError("点了主属性「点击展开」但该区没有展开，中断。")
    raise FilterClickError(
        "滚动面板 %d 轮后仍未看到「主属性词条」区，中断。" % _MAX_PANEL_SCROLLS
    )


def _normalize_stat(text: str) -> str:
    """把预设里的写法对齐到游戏 UI。

    data/stats.json 的 slot_mainstats 写「攻击力%」，游戏面板显示「攻击力百分比」，
    不归一化就永远认不上（B5 的第二半）。
    """
    return text.replace("％", "%").replace("%", "百分比")


def _best_match(lines, name: str, top: int, bottom: int):
    """在 [top, bottom] 纵向区间里挑最贴 name 的一条：全等 > 包含 > 相似度。

    包含匹配按「名字占整条文本的比例」打分，避免「生命值」抢走
    「生命值百分比」这种更长更具体的条目。
    """
    wanted = _normalize_stat(name)
    best = None
    best_score = 0.0
    for line in lines:
        y = (line.top + line.bottom) // 2
        if not top <= y <= bottom:
            continue
        text = _normalize_stat(line.text.replace(" ", ""))
        if text == wanted:
            score = 3.0
        elif wanted in text:
            score = 2.0 + len(wanted) / max(1, len(text))
        else:
            score = SequenceMatcher(None, wanted, text).ratio()
            if score < _MATCH_CUTOFF:
                continue
        if score > best_score:
            best, best_score = line, score
    return best


def _pick_mainstats(
    hwnd: int, names: Sequence[str], *, width: int, height: int, lines: list | None = None
) -> None:
    """在已展开的主属性词条区里点名勾选。

    主属性是复选框，勾上即生效，没有弹层、也没有「确认筛选」；
    逐条精确点名为主，找不齐直接中断，不滚动、不猜。

    `lines` 传入 `_open_mainstat_section` 返回的那一帧即可（两者状态相同、中间无点击），
    不传则自己抓一帧。
    """
    log = get_logger("zzz_ddtool.gamefilter")
    pending = list(names)
    if not pending:
        return
    if lines is None:
        lines = _text_lines()
    title_y = _title_y(lines, MAINSTAT_TITLE)
    if title_y is None:
        raise FilterClickError("主属性词条区不在视口里，无法点名，中断。")
    sub_y = _title_y(lines, SUBSTAT_TITLE)
    top = title_y + 20
    bottom = sub_y - 20 if sub_y is not None and sub_y - 20 > top else title_y + 320
    missing: list[str] = []
    for name in pending:
        hit = _best_match(lines, name, top, bottom)
        if hit is None:
            missing.append(name)
            continue
        click_client(hwnd, (hit.left + hit.right) // 2, (hit.top + hit.bottom) // 2)
        log.info("mainstat clicked %r at (%d,%d)", name, hit.left, hit.top)
        time.sleep(0.3)
    if missing:
        raise FilterClickError(f"主属性词条区里找不到：{missing}")


def apply_sets_mainstats(
    hwnd: int,
    width: int,
    height: int,
    *,
    sets: Sequence[str],
    mainstats: Sequence[str],
) -> None:
    """在筛选面板里点开套装 / 主属性词条并勾选。

    两条路不一样：
    - 套装：点「点击选择」开弹层 → 弹层里点名 → 点「确认筛选」关弹层；
    - 主属性：面板滚到底 → 点「点击展开」展开词条区 → 区里点复选框（无确认按钮）。
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
        # 展开最后一帧直接交给点名用，省一次全屏 OCR（两者之间不点击，帧不变）。
        lines = _open_mainstat_section(hwnd, width, height)
        _pick_mainstats(hwnd, mainstats, width=width, height=height, lines=lines)
        log.info("mainstats picked: %s", list(mainstats))


def _click_slot(hwnd: int, slot: int, width: int, height: int) -> None:
    """OCR 找「分区[N]」按钮，点文字中心。面板滚动状态无关。认不上就中断。"""
    image, _ = _grab()
    for line in read_rect(image, FILTER_SLOT_AREA):
        text = line.text.replace(" ", "")
        if "分区" not in text:
            continue
        m = re.search(r"(\d+)", text)
        if m and int(m.group(1)) == slot:
            click_client(hwnd, (line.left + line.right) // 2, (line.top + line.bottom) // 2)
            return
    raise FilterClickError(f"筛选面板里没找到「分区[{slot}]」按钮，中断。")


def _click_reset(hwnd: int, width: int, height: int) -> None:
    """OCR 找面板底部的「重置」按钮点中心（避开批量操作）。"""
    image, _ = _grab()
    for line in read_rect(image, FILTER_RESET_AREA):
        text = line.text.replace(" ", "")
        if "重置" in text and "批量" not in text:
            click_client(hwnd, (line.left + line.right) // 2, (line.top + line.bottom) // 2)
            return
    raise FilterClickError("筛选面板里没找到「重置」按钮，中断。")


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
    分区/重置按钮靠 OCR 点文字中心，不用硬坐标（旧表偏到错误按钮上）。
    """
    log = get_logger("zzz_ddtool.gamefilter")
    fx, fy = scale_point(*FILTER_BTN, width, height)
    _refuse_dismantle(fx, fy, width, height)
    click_client(hwnd, fx, fy)
    time.sleep(0.55)
    _click_reset(hwnd, width, height)
    time.sleep(0.45)
    for slot in slots:
        _click_slot(hwnd, slot, width, height)
        time.sleep(0.12)
    if sets or mainstats:
        apply_sets_mainstats(hwnd, width, height, sets=sets, mainstats=mainstats)
    press_key(hwnd, VK_ESCAPE)
    time.sleep(0.35)
    log.info("game filter done slots=%s sets=%s mains=%s", list(slots), list(sets), list(mainstats))
