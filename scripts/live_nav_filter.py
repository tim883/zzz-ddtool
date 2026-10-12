"""Live: 大地图 → 功能页 → 仓库 → 驱动盘 → 开筛选 → 重置 → 摸索 → Esc。

大页切换先图标确认再点。不点强化 / 弃置 / 拆解。
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from zzz_ddtool import loground
from zzz_ddtool.capture import CaptureError, grab_game_image, save_image
from zzz_ddtool.gamefilter import _refuse_dismantle
from zzz_ddtool.layout import (
    BACK_BTN,
    DRIVE_DISC_TAB,
    FILTER_BTN,
    FILTER_RESET,
    OVERWORLD_MENU_BTN,
    WAREHOUSE_BTN,
    scale_point,
)
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.nav import NavError, _require
from zzz_ddtool.screen import (
    DRIVE_DISC,
    MENU,
    OVERWORLD,
    STORAGE,
    UNKNOWN,
    classify_image,
    icon_report,
    is_page,
)
from zzz_ddtool.wininput import VK_ESCAPE, click_client, press_key


log = get_logger("zzz_ddtool.live")
NOTES: list[str] = []


def note(msg: str) -> None:
    log.info("%s", msg)
    NOTES.append(msg)
    print(msg, flush=True)


def grab(hwnd: int | None = None):
    image, method, target = grab_game_image(hwnd)
    page = classify_image(image)
    return image, page, method, target


def shot(image, hwnd: int, method: str, name: str) -> Path:
    dest = save_image(image, hwnd, method, name=name)
    note(f"shot {name} -> {dest.name}")
    return dest


def click(hwnd: int, image: QImage, point: tuple[int, int], alt: bool = False) -> None:
    x, y = scale_point(*point, image.width(), image.height())
    _refuse_dismantle(x, y, image.width(), image.height())
    note(f"click {point} -> {x},{y} alt={alt}")
    click_client(hwnd, x, y, alt=alt)


def report(image: QImage, page: str) -> str:
    detail = " ".join(f"{name}={score:.2f}/{need:.2f}" for name, score, need in icon_report(image, page))
    return f"{page} {detail}"


def orange_reset_hits(image: QImage) -> int:
    x, y = scale_point(*FILTER_RESET, image.width(), image.height())
    hits = 0
    for yy in range(y - 18, y + 19, 2):
        for xx in range(x - 22, x + 23, 2):
            if xx < 0 or yy < 0 or xx >= image.width() or yy >= image.height():
                continue
            c = image.pixelColor(xx, yy)
            if c.red() > 190 and 90 < c.green() < 170 and c.blue() < 70:
                hits += 1
    return hits


def filter_open(image: QImage) -> bool:
    return orange_reset_hits(image) >= 6


def back_to_overworld(hwnd: int, image: QImage, page: str) -> tuple[QImage, str, str]:
    if is_page(image, OVERWORLD) or page == OVERWORLD:
        return image, OVERWORLD, "printwindow"
    if page in {STORAGE, DRIVE_DISC, UNKNOWN} or is_page(image, STORAGE) or is_page(image, DRIVE_DISC):
        if filter_open(image):
            note("筛选似乎开着，先 Esc")
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.45)
            image, page, method, hwnd = grab(hwnd)
        note("从仓库/未知页 Esc 回功能页或大地图")
        press_key(hwnd, VK_ESCAPE)
        time.sleep(0.55)
        image, page, method, hwnd = grab(hwnd)
    if page == MENU or is_page(image, MENU):
        note("功能页点返回回大地图")
        click(hwnd, image, BACK_BTN)
        time.sleep(0.6)
        image, page, method, hwnd = grab(hwnd)
        if page == MENU or is_page(image, MENU):
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.5)
            image, page, method, hwnd = grab(hwnd)
    return image, page, method


def main() -> int:
    loground.use_test_context("live-nav-filter")
    QApplication.instance() or QApplication([])
    try:
        image, page, method, hwnd = grab()
    except CaptureError as exc:
        note(f"抓不到游戏: {exc}")
        return 2

    note(f"start page={page} {image.width()}x{image.height()} {report(image, page) if page != UNKNOWN else ''}")
    shot(image, hwnd, method, "live-00-start")

    try:
        image, page, method = back_to_overworld(hwnd, image, page)
        image, page, method, hwnd = grab(hwnd)
        note(f"after home page={page} {report(image, OVERWORLD)}")
        shot(image, hwnd, method, "live-01-大地图")
        _require(image, OVERWORLD, "大地图页")

        note("Esc 开功能页")
        press_key(hwnd, VK_ESCAPE)
        time.sleep(0.7)
        image, page, method, hwnd = grab(hwnd)
        if page != MENU and not is_page(image, MENU):
            note("Esc 没出菜单，试点右上菜单")
            click(hwnd, image, OVERWORLD_MENU_BTN, alt=True)
            time.sleep(0.7)
            image, page, method, hwnd = grab(hwnd)
        note(f"after menu page={page} {report(image, MENU)}")
        shot(image, hwnd, method, "live-02-功能页")
        _require(image, MENU, "功能页")

        note("点仓库")
        click(hwnd, image, WAREHOUSE_BTN)
        time.sleep(0.8)
        image, page, method, hwnd = grab(hwnd)
        note(f"after warehouse page={page} {report(image, STORAGE)}")
        shot(image, hwnd, method, "live-03-仓库页")
        if is_page(image, MENU):
            raise NavError("点了仓库还在功能页。用管理员开工具再试。")
        if is_page(image, OVERWORLD):
            raise NavError("点了仓库却回到大地图，已中断。")
        _require(image, STORAGE, "仓库页")

        note("点驱动盘页签")
        click(hwnd, image, DRIVE_DISC_TAB)
        time.sleep(0.75)
        image, page, method, hwnd = grab(hwnd)
        note(f"after disc tab page={page} {report(image, DRIVE_DISC)}")
        shot(image, hwnd, method, "live-04-驱动盘页")
        _require(image, DRIVE_DISC, "驱动盘页")

        note("点筛选漏斗")
        click(hwnd, image, FILTER_BTN)
        time.sleep(0.65)
        image, page, method, hwnd = grab(hwnd)
        opened = filter_open(image)
        note(f"after funnel page={page} filter_open={opened} orange_hits={orange_reset_hits(image)}")
        dest = shot(image, hwnd, method, "live-05-筛选开")
        if not opened:
            NOTES.append(f"漏斗后没看到重置橙钮，图在 {dest.name}，等你看。")
            return 3

        note("点重置（先清掉游戏里上次留下的条件）")
        click(hwnd, image, FILTER_RESET)
        time.sleep(0.5)
        image, page, method, hwnd = grab(hwnd)
        shot(image, hwnd, method, "live-06-筛选重置后")

        # 1024 筛选样张 ×1.875。重置后蓝标签会消失，分区可能上移。
        rarity_s = (1425, 544)
        slot1 = (1425, 881)
        pick = (1575, 310)
        note(f"试探点稀有度 S {rarity_s}")
        click(hwnd, image, rarity_s)
        time.sleep(0.35)
        image, page, method, hwnd = grab(hwnd)
        shot(image, hwnd, method, "live-06b-点S后")

        note(f"试探点分区[1] {slot1}")
        click(hwnd, image, slot1)
        time.sleep(0.35)
        image, page, method, hwnd = grab(hwnd)
        shot(image, hwnd, method, "live-06c-点分区1后")

        note(f"试探点套装「点击选择」{pick}；套装和主词条是同一个入口")
        click(hwnd, image, pick)
        time.sleep(0.7)
        image, page, method, hwnd = grab(hwnd)
        dest = shot(image, hwnd, method, "live-07-套装点选器")
        NOTES.append(f"套装/主词条点选器图：{dest.name}。还不会选具体套装名和主词条，等你标。")
        NOTES.append("分区[5][6] 在样张里没露出来，可能要下滑筛选面板。")

        note("Esc 关点选器或关筛选")
        press_key(hwnd, VK_ESCAPE)
        time.sleep(0.45)
        image, page, method, hwnd = grab(hwnd)
        shot(image, hwnd, method, "live-08-第一次Esc后")
        if filter_open(image):
            note("筛选还开着，再 Esc 关掉")
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.4)
            image, page, method, hwnd = grab(hwnd)
            shot(image, hwnd, method, "live-09-筛选关")
        else:
            note("第一次 Esc 后筛选已关")

        image, page, method, hwnd = grab(hwnd)
        note(f"end page={page} filter_open={filter_open(image)} {report(image, DRIVE_DISC)}")
        if not is_page(image, DRIVE_DISC):
            raise NavError("关筛选后不在驱动盘页，已中断。")
        note("OK 大地图→仓库→驱动盘→筛选→Esc 走通")
        return 0
    except (NavError, Exception) as exc:
        note(f"STOP {exc}")
        try:
            image, page, method, hwnd = grab()
            shot(image, hwnd, method, "live-err")
        except Exception:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
