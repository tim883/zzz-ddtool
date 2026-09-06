from __future__ import annotations

from zzz_ddtool.layout import BACK_BTN_REGION, BOTTOM_ICON_BAND, OVERWORLD_HINT, scale_rect
from zzz_ddtool.template import IconMark, all_marks_hit, marks_hit

MENU = "menu"
OVERWORLD = "overworld"
STORAGE = "storage"
DRIVE_DISC = "drive_disc"
UNKNOWN = "unknown"

PAGE_MARKS: dict[str, tuple[IconMark, ...]] = {
    OVERWORLD: (
        IconMark("快捷轮盘", "world/quick_wheel.png", (40, 990, 230, 1068), 0.80),
    ),
    MENU: (
        IconMark("菜单返回", "menu/back.png", (82, 13, 150, 90), 0.75),
        IconMark("底部仓库", "menu/warehouse.png", (1088, 928, 1160, 1048), 0.85),
    ),
    STORAGE: (
        IconMark("音擎仓库标题", "storage/title.png", (74, 112, 164, 168), 0.85),
        IconMark("回收", "storage/recycle.png", (250, 900, 438, 960), 0.94),
    ),
    DRIVE_DISC: (
        IconMark("驱动仓库标题", "discs/title.png", (74, 112, 164, 168), 0.85),
        IconMark("拆解", "discs/dismantle.png", (250, 900, 438, 960), 0.94),
        IconMark("驱动盘页签", "discs/tab.png", (1510, 126, 1588, 210), 0.85),
    ),
}

PAGE_ORDER = (DRIVE_DISC, STORAGE, MENU, OVERWORLD)


def icon_report(image, page: str) -> list[tuple[str, float, float]]:
    return marks_hit(image, PAGE_MARKS[page])


def is_page(image, page: str) -> bool:
    return all_marks_hit(image, PAGE_MARKS[page])


def _red_back_hits(image) -> int:
    left, top, right, bottom = scale_rect(*BACK_BTN_REGION, image.width(), image.height())
    hits = 0
    step = max(1, (right - left) // 40)
    for y in range(top, bottom, step):
        for x in range(left, right, step):
            color = image.pixelColor(x, y)
            if color.red() > 170 and color.green() < 100 and color.blue() < 100:
                hits += 1
    return hits


def _bottom_icon_centers(image) -> list[int]:
    left, top, right, bottom = scale_rect(*BOTTOM_ICON_BAND, image.width(), image.height())
    bright_xs: list[int] = []
    for x in range(left, right, 2):
        bright = 0
        for y in range(top, bottom, 3):
            color = image.pixelColor(x, y)
            if color.red() + color.green() + color.blue() > 600:
                bright += 1
        if bright >= 4:
            bright_xs.append(x)
    if not bright_xs:
        return []
    groups: list[tuple[int, int]] = []
    start = prev = bright_xs[0]
    for x in bright_xs[1:]:
        if x - prev > 16:
            groups.append((start, prev))
            start = x
        prev = x
    groups.append((start, prev))
    return [(a + b) // 2 for a, b in groups if b - a >= 8]


def _overworld_hint(image) -> float:
    left, top, right, bottom = scale_rect(*OVERWORLD_HINT, image.width(), image.height())
    hits = 0
    total = 0
    for y in range(top, bottom, 2):
        for x in range(left, right, 2):
            total += 1
            color = image.pixelColor(x, y)
            if color.red() > 200 and color.green() > 200 and color.blue() > 200:
                hits += 1
    return hits / total if total else 0.0


def _classify_heuristic(image) -> str:
    icons = _bottom_icon_centers(image)
    if len(icons) >= 10:
        return MENU
    if _overworld_hint(image) >= 0.04:
        return OVERWORLD
    if _red_back_hits(image) >= 8:
        return STORAGE
    return UNKNOWN


def classify_image(image) -> str:
    for page in PAGE_ORDER:
        if is_page(image, page):
            return page
    return _classify_heuristic(image)
