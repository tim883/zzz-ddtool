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
# 音擎仓库页签。2026-10-02 真机标定：4 个页签圆中心约 x=1453/1546/1642/1736 (y=168)，
# 点最左 (1453,168) 后 classify_image 认到 storage（「音擎仓库标题」+「回收」），
# 再点 (1549,168) 回驱动盘。这就是切页置顶的路径。
ENGINE_TAB = (1453, 168)
# 漏斗白圈中心（filter-panel-bottom.png 实测圆心）。右边青蓝圆是拆解，点到会进拆解页。
FILTER_BTN = (149, 930)
# 拆解蓝标 +「拆解」字。任何点击落在这里都要拒绝。
DISMANTLE_FORBID = (210, 890, 460, 990)
# 旧硬坐标已废弃：分区/重置改 OCR 找按钮文字点中心，滚动状态无关。
# FILTER_RESET=(1301,1033) 偏到重置按钮左缘图标；FILTER_SLOTS 整表偏到错误按钮上。
FILTER_RESET = (1432, 1029)

# 右侧详情面板。OCR 实测标定：
# 旧值 DISC_LEVEL/SUB_STATS 偏下约 150px，导致 parse_disc 读不到副属性。
DETAIL_PANEL = (1400, 225, 1870, 800)
DISC_NAME = (1400, 260, 1620, 315)
DISC_LEVEL = (1180, 190, 1310, 250)  # 等级（右上，避开右侧「15」上限数字）
SUB_STATS = (1160, 390, 1790, 495)  # 副属性（两列：名字 数值 | 名字 数值）
ACTION_ROW = (1600, 1000, 1880, 1070)
# 同一行的 R/T 提示会随文字长度横向移动，必须整行识别，不能固定切成左右两栏。
ACTION_TEXT_LINE = (1600, 1026, 1880, 1057)
# 副属性每行的高度，约 51px；第一行中心距副属性区顶部约 26px
SUB_STAT_ROW_H = 51
SUB_STAT_FIRST = 26
# 副属性行内：名字在左，数值在右，按这个 x 切分
SUB_STAT_SPLIT_X = 1650

# 仓库中点选格子只更新右侧面板，不会打开放大详情页。
# 2026-10-01 失败现场 0006-识别盘面.png 实拍标定，和上面的放大页分开使用。
WAREHOUSE_DISC_LEVEL = (1460, 398, 1610, 445)
WAREHOUSE_SUB_STATS = (1410, 560, 1835, 765)

# 强化页（点详情页「查看/强化」进入的弹层），OCR 实测标定：
# 副属性整体比详情页左移约 300px，判断要在这里做，必须用强化页自己的坐标。
UPGRADE_SUB_STATS = (883, 419, 1692, 501)
# 强化页等级条最左一格，装「当前等级」数字。2026-10-04 按真机实拍重新夹紧：
# 旧值 (940,525,1100,580) 有两个毛病——
#   ① 底边 580 比数字底边（实拍 582）高 2px，把「9」的尾巴裁掉读成「6」；
#   ② 左边缘 940 切进等级条的绿色填充，绿填充涨上来盖住数字时整格读 0 行。
# 收紧到只套住数字本身（x 978~1023、y 543~582 实拍）后两个毛病都不犯。
UPGRADE_LEVEL = (955, 528, 1050, 592)
# 等级条整条：从左到右「当前等级 ┃ 经验 320/2400 ┃ 上限」。退路用——
# 窄格被绿填充盖住读不出时改读整条，带上经验文字反而更容易被检测器框住。
# 整条里混着经验票「320/2400」，解析时按「不含 / 的纯数字」挑，见 ocr._level_areas。
UPGRADE_LEVEL_BAR = (880, 515, 1650, 592)
UPGRADE_SUB_SPLIT_X = 1250  # 强化页副属性名字/数值分界
# 强化页材料槽三个的槽心。2026-10-02 实拍 0010-进强化页.png / 0011-强化.png 标定：
# 槽心 y=677，三个槽心 x=1161 / 1283 / 1405（间距 122）。
# 点「阶段添加」是一键补满本档素材；点单个材料槽只加一份素材，用来复现素材不足。
UPGRADE_MATERIAL_SLOTS = ((1161, 677), (1283, 677), (1405, 677))

# 左侧格子网格：9 列 x 4 行。funnel-debug.png 实测：
# 「等级」标签列中心 x=166 起步长 135，行中心 y=354 起步长 175。
# 图标中心在标签正上方约 87px，点击点必须是图标中心，不是标签。
GRID_COLS = 9
GRID_ROWS = 4
GRID_ORIGIN = (166, 267)
GRID_STEP = (135, 175)
# 图标中心到「等级」标签中心的垂直偏移（OCR 标定用）
GRID_LABEL_DY = 87
# 等级标签 OCR 扫描区（设计坐标）。右边界必须盖住第 9 列标签（中心 x≈1246，右缘≈1280）；
# 上边界放宽到 80：列表滚动停在半行位置时，首行标签位置会随滚动偏移。
LABEL_SCAN_AREA = (60, 80, 1330, 1020)


def scale_point(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    return round(x * width / DESIGN_W), round(y * height / DESIGN_H)


def scale_rect(left: int, top: int, right: int, bottom: int, width: int, height: int) -> tuple[int, int, int, int]:
    l, t = scale_point(left, top, width, height)
    r, b = scale_point(right, bottom, width, height)
    return l, t, r, b
