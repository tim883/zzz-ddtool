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
# 漏斗白圈中心（filter-panel-bottom.png 实测圆心）。右边青蓝圆是拆解，点到会进拆解页。
FILTER_BTN = (149, 930)
# 拆解蓝标 +「拆解」字。任何点击落在这里都要拒绝。
DISMANTLE_FORBID = (210, 890, 460, 990)
# 旧硬坐标已废弃：分区/重置改 OCR 找按钮文字点中心，滚动状态无关。
# FILTER_RESET=(1301,1033) 偏到重置按钮左缘图标；FILTER_SLOTS 整表偏到错误按钮上。
FILTER_RESET = (1432, 1029)

# 右侧详情面板。2026-09-06 OCR 实测（out/captures/detail-page.png + 老大截图）重新标定：
# 旧值 DISC_LEVEL/MAIN_STAT/SUB_STATS 偏下约 150px，导致 parse_disc 读不到副属性。
DETAIL_PANEL = (1400, 225, 1870, 800)
DISC_NAME = (1400, 260, 1620, 315)
DISC_LEVEL = (1180, 190, 1310, 250)  # 等级（右上，避开右侧「15」上限数字）
MAIN_STAT = (1150, 295, 1470, 345)  # 主属性名字+数值
SUB_STATS = (1160, 390, 1790, 495)  # 副属性（两列：名字 数值 | 名字 数值）
ACTION_ROW = (1600, 1000, 1880, 1070)
# 副属性每行的高度，约 51px；第一行中心距副属性区顶部约 26px
SUB_STAT_ROW_H = 51
SUB_STAT_FIRST = 26
# 副属性行内：名字在左，数值在右，按这个 x 切分
SUB_STAT_SPLIT_X = 1650

# 强化页（点详情页「查看/强化」进入的弹层）。2026-09-06 OCR 实测老大截图：
# 副属性整体比详情页左移约 300px，判断要在这里做，必须用强化页自己的坐标。
UPGRADE_SUB_STATS = (883, 419, 1692, 501)
UPGRADE_LEVEL = (940, 525, 1100, 580)  # 强化页当前等级数字（避开右侧经验 0/1440）
UPGRADE_SUB_SPLIT_X = 1250  # 强化页副属性名字/数值分界

# 左侧格子网格：9 列 x 4 行。funnel-debug.png 实测：
# 「等级」标签列中心 x=166 起步长 135，行中心 y=354 起步长 175。
# 图标中心在标签正上方约 87px，点击点必须是图标中心，不是标签。
GRID_COLS = 9
GRID_ROWS = 4
GRID_ORIGIN = (166, 267)
GRID_STEP = (135, 175)
# 图标中心到「等级」标签中心的垂直偏移（OCR 标定用）
GRID_LABEL_DY = 87


def scale_point(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    return round(x * width / DESIGN_W), round(y * height / DESIGN_H)


def scale_rect(left: int, top: int, right: int, bottom: int, width: int, height: int) -> tuple[int, int, int, int]:
    l, t = scale_point(left, top, width, height)
    r, b = scale_point(right, bottom, width, height)
    return l, t, r, b
