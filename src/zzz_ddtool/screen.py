from __future__ import annotations

import numpy as np

from zzz_ddtool.layout import BACK_BTN_REGION, BOTTOM_ICON_BAND, OVERWORLD_HINT, scale_rect
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.template import IconMark, all_marks_hit, marks_hit, to_rgb

MENU = "menu"
OVERWORLD = "overworld"
STORAGE = "storage"
DRIVE_DISC = "drive_disc"
UNKNOWN = "unknown"

# 盘面查看/强化弹窗右上角关闭 X。只有确认弹窗存在，才允许 Esc 回退。
DISC_MODAL_CLOSE = IconMark("盘面弹窗关闭", "discs/modal-close.png", (1625, 187, 1694, 234), 0.90)

# 强化页「阶段添加」按钮文字（紧贴四字 +5px 边距，1232,851-1346,883）。
# 按钮上缘「长按连续添加材料」提示（闪烁）、按钮下缘均有动画，不能拿来比对
# （2026-10-12 老大提醒）；本子区经真机 5 帧互比**零像素变动**（NCC=1.000），
# 非强化页同坐标 -0.19~0，阈值 0.90 余量充足。裁剪样式对齐 picker set-00（紧贴文字）。
# 模板取自 2026-10-12 真机帧 calibration-evidence-20261012-015208/frame-00011.png。
STAGE_ADD_ANCHOR = IconMark("阶段添加按钮", "discs/stage-add.png", (1232, 851, 1346, 883), 0.90)


def is_upgrade_stage_add(image) -> bool:
    """强化页固定锚点：「阶段添加」按钮模板匹配（毫秒级，不跑 OCR/认页）。

    供 open_upgrade 就绪探针判定"已进强化页"；替代原来 ~1s/轮的 OCR 判定。
    """
    return all_marks_hit(image, (STAGE_ADD_ANCHOR,))

# 详情页点「查看/强化」后的缩放预览层：右上角有「等级xx/15」，但**没有**关闭 X；
# 再 Esc 一次才回网格。区域只框缩放页的等级文字，详情页侧栏与仓库页都不在此处出词。
DISC_PREVIEW_LEVEL_AREA = (1140, 175, 1420, 265)


def is_disc_modal(image) -> bool:
    return all_marks_hit(image, (DISC_MODAL_CLOSE,))


def is_disc_preview(image) -> bool:
    """缩放预览层（详情页点「查看/强化」后的放大页）。

    这一层没有关闭 X，不能靠模板认；用右上角固定的「等级xx/15」文字判定，
    使 Esc 回退可以逐层退到网格，而不是当成未知画面卡住。
    """
    from zzz_ddtool.ocr import read_rect

    for line in read_rect(image, DISC_PREVIEW_LEVEL_AREA):
        if line.text.replace(" ", "").startswith("等级"):
            return True
    return False

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
        # 不用「驱动盘页签」做锚点：页签切过去后变选中态(黄底)，模板分掉到 0.81
        # 左右摇摆；驱动仓库标题本身已能区分音擎/驱动两个仓。
    ),
}

PAGE_ORDER = (DRIVE_DISC, STORAGE, MENU, OVERWORLD)


def icon_report(image, page: str) -> list[tuple[str, float, float]]:
    return marks_hit(image, PAGE_MARKS[page])


def is_page(image, page: str) -> bool:
    return all_marks_hit(image, PAGE_MARKS[page])


def _red_back_hits(image) -> int:
    left, top, right, bottom = scale_rect(*BACK_BTN_REGION, image.width(), image.height())
    step = max(1, (right - left) // 40)
    block = to_rgb(image)[top:bottom:step, left:right:step].astype(np.int32)
    if block.size == 0:
        return 0
    hit = (block[:, :, 0] > 170) & (block[:, :, 1] < 100) & (block[:, :, 2] < 100)
    return int(hit.sum())


def _bottom_icon_centers(image) -> list[int]:
    left, top, right, bottom = scale_rect(*BOTTOM_ICON_BAND, image.width(), image.height())
    block = to_rgb(image)[top:bottom:3, left:right:2].astype(np.int32)
    if block.size == 0:
        return []
    bright_per_col = (block.sum(axis=2) > 600).sum(axis=0)
    xs = np.arange(block.shape[1]) * 2 + left
    bright_xs = [int(x) for x in xs[bright_per_col >= 4]]
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
    block = to_rgb(image)[top:bottom:2, left:right:2].astype(np.int32)
    total = block.shape[0] * block.shape[1]
    if not total:
        return 0.0
    white = (block[:, :, 0] > 200) & (block[:, :, 1] > 200) & (block[:, :, 2] > 200)
    return int(white.sum()) / total


def _classify_heuristic(image) -> str:
    icons = _bottom_icon_centers(image)
    if len(icons) >= 10:
        return MENU
    if _overworld_hint(image) >= 0.04:
        return OVERWORLD
    # 注意：不能用「红色返回箭头」判 storage——详情/缩放页顶部也有同样的红箭头，
    # 会把缩放页误判成仓库页导致后续全错（2026-09-06 实锤）。认不出就老实 unknown。
    return UNKNOWN


def classify_image(image) -> str:
    """认页：逐页对模板，全不过再走启发式。判定结果记 DEBUG，供事后核对认页依据。"""
    for page in PAGE_ORDER:
        if is_page(image, page):
            get_logger("zzz_ddtool.screen").debug("认页 → %s（模板命中）", page)
            return page
    page = _classify_heuristic(image)
    get_logger("zzz_ddtool.screen").debug("认页 → %s（启发式兜底）", page)
    return page
