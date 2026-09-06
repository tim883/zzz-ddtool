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


def scale_point(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    return round(x * width / DESIGN_W), round(y * height / DESIGN_H)


def scale_rect(left: int, top: int, right: int, bottom: int, width: int, height: int) -> tuple[int, int, int, int]:
    l, t = scale_point(left, top, width, height)
    r, b = scale_point(right, bottom, width, height)
    return l, t, r, b
