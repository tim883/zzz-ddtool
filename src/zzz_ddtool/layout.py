from __future__ import annotations

DESIGN_W = 1920
DESIGN_H = 1080

# Client pixels on 1920×1080 windowed 16:9. Scale with the real client size.
WAREHOUSE_BTN = (1122, 970)
BACK_BTN = (132, 52)
OVERWORLD_MENU_BTN = (1876, 50)
BACK_BTN_REGION = (69, 23, 195, 80)
BOTTOM_ICON_BAND = (180, 930, 1750, 1010)
OVERWORLD_HINT = (20, 980, 280, 1060)
# OneDragon storage_wengine.yml TAB-驱动盘 (1510,126)-(1588,210)
DRIVE_DISC_TAB = (1549, 168)
DRIVE_DISC_TAB_REGION = (1510, 126, 1588, 210)
# 漏斗白圈中心。右边青蓝圆是拆解，点到会进拆解页。
FILTER_BTN = (166, 906)
# 拆解蓝标 +「拆解」字。任何点击落在这里都要拒绝。
DISMANTLE_FORBID = (210, 890, 460, 990)
# 1024×576 筛选图 ×1.875 → 1920×1080；关面板用 Esc，不点叉
FILTER_RESET = (1301, 1033)
FILTER_SLOTS = {
    1: (1460, 780),
    2: (1540, 780),
    3: (1620, 780),
    4: (1700, 780),
    5: (1460, 850),
    6: (1540, 850),
}

# 右侧详情面板。坐标来自 scripts/ocr_probe.py 在手册/样张/驱动盘页.png 上的实测。
DETAIL_PANEL = (1400, 225, 1870, 800)
DISC_NAME = (1400, 260, 1620, 315)
DISC_LEVEL = (1450, 390, 1620, 445)
MAIN_STAT = (1415, 480, 1840, 535)
SUB_STATS = (1415, 558, 1840, 775)
ACTION_ROW = (1600, 1000, 1880, 1070)
# 副属性每行的高度，约 51px；第一行中心距副属性区顶部约 26px
SUB_STAT_ROW_H = 51
SUB_STAT_FIRST = 26
# 副属性行内：名字在左，数值在右，按这个 x 切分
SUB_STAT_SPLIT_X = 1650

# 左侧格子网格：9 列 x 4 行（实测列间距约 135，行高约 175）
GRID_COLS = 9
GRID_ROWS = 4
GRID_ORIGIN = (134, 340)
GRID_STEP = (135, 175)


def scale_point(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    return round(x * width / DESIGN_W), round(y * height / DESIGN_H)


def scale_rect(left: int, top: int, right: int, bottom: int, width: int, height: int) -> tuple[int, int, int, int]:
    l, t = scale_point(left, top, width, height)
    r, b = scale_point(right, bottom, width, height)
    return l, t, r, b
