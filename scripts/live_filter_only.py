"""Continue from current page: leave 拆解 if needed, open filter, reset, probe, Esc."""

from __future__ import annotations

import time

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from zzz_ddtool import loground
from zzz_ddtool.capture import CaptureError, grab_game_image, save_image
from zzz_ddtool.gamefilter import _refuse_dismantle
from zzz_ddtool.layout import FILTER_BTN, FILTER_RESET, scale_point
from zzz_ddtool.logutil import get_logger
from zzz_ddtool.nav import NavError, _require
from zzz_ddtool.screen import DRIVE_DISC, classify_image, icon_report, is_page
from zzz_ddtool.wininput import VK_ESCAPE, click_client, press_key

log = get_logger("zzz_ddtool.live")


def note(msg: str) -> None:
    log.info("%s", msg)
    print(msg, flush=True)


def grab(hwnd=None):
    image, method, target = grab_game_image(hwnd)
    return image, classify_image(image), method, target


def shot(image, hwnd, method, name):
    dest = save_image(image, hwnd, method, name=name)
    note(f"shot {name} -> {dest.name}")
    return dest


def click(hwnd, image, point):
    x, y = scale_point(*point, image.width(), image.height())
    _refuse_dismantle(x, y, image.width(), image.height())
    note(f"click {point} -> {x},{y}")
    click_client(hwnd, x, y)


def orange_reset_hits(image: QImage) -> int:
    x, y = scale_point(*FILTER_RESET, image.width(), image.height())
    hits = 0
    for yy in range(y - 18, y + 19, 2):
        for xx in range(x - 22, x + 23, 2):
            if 0 <= xx < image.width() and 0 <= yy < image.height():
                c = image.pixelColor(xx, yy)
                if c.red() > 190 and 90 < c.green() < 170 and c.blue() < 70:
                    hits += 1
    return hits


def main() -> int:
    loground.use_test_context("live-filter-only")
    QApplication.instance() or QApplication([])
    try:
        image, page, method, hwnd = grab()
    except CaptureError as exc:
        note(str(exc))
        return 2
    note(f"start page={page} {image.width()}x{image.height()}")
    shot(image, hwnd, method, "live2-00-start")

    try:
        if not is_page(image, DRIVE_DISC):
            note("不在驱动盘页（可能在拆解），Esc 退出")
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.6)
            image, page, method, hwnd = grab(hwnd)
            shot(image, hwnd, method, "live2-01-Esc后")
        if not is_page(image, DRIVE_DISC):
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.55)
            image, page, method, hwnd = grab(hwnd)
            shot(image, hwnd, method, "live2-01b-再Esc")
        note(f"page={page} {icon_report(image, DRIVE_DISC)}")
        _require(image, DRIVE_DISC, "驱动盘页")

        note("点漏斗白圈，避开拆解蓝标")
        click(hwnd, image, FILTER_BTN)
        time.sleep(0.7)
        image, page, method, hwnd = grab(hwnd)
        opened = orange_reset_hits(image) >= 6
        note(f"after funnel page={page} filter_open={opened} orange={orange_reset_hits(image)}")
        dest = shot(image, hwnd, method, "live2-02-筛选开")
        if not opened:
            note(f"没看到重置橙钮，图 {dest.name}")
            return 3

        note("点重置")
        click(hwnd, image, FILTER_RESET)
        time.sleep(0.5)
        image, page, method, hwnd = grab(hwnd)
        shot(image, hwnd, method, "live2-03-重置后")

        rarity_s = (1425, 544)
        slot1 = (1425, 881)
        pick = (1575, 310)
        note(f"试探 S {rarity_s}")
        click(hwnd, image, rarity_s)
        time.sleep(0.35)
        image, page, method, hwnd = grab(hwnd)
        shot(image, hwnd, method, "live2-04-点S")

        note(f"试探 分区[1] {slot1}")
        click(hwnd, image, slot1)
        time.sleep(0.35)
        image, page, method, hwnd = grab(hwnd)
        shot(image, hwnd, method, "live2-05-点分区1")

        note(f"试探 点击选择 {pick}")
        click(hwnd, image, pick)
        time.sleep(0.7)
        image, page, method, hwnd = grab(hwnd)
        dest = shot(image, hwnd, method, "live2-06-套装点选器")
        note(f"套装/主词条点选器 {dest.name}，具体怎么勾还不会")

        press_key(hwnd, VK_ESCAPE)
        time.sleep(0.45)
        image, page, method, hwnd = grab(hwnd)
        shot(image, hwnd, method, "live2-07-Esc1")
        if orange_reset_hits(image) >= 6:
            note("筛选还开着，再 Esc")
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.4)
            image, page, method, hwnd = grab(hwnd)
            shot(image, hwnd, method, "live2-08-Esc2")

        image, page, method, hwnd = grab(hwnd)
        note(f"end page={page} filter_open={orange_reset_hits(image)>=6}")
        _require(image, DRIVE_DISC, "关筛选后驱动盘页")
        note("OK 筛选开/重置/试探/Esc")
        return 0
    except (NavError, Exception) as exc:
        note(f"STOP {exc}")
        try:
            image, page, method, hwnd = grab()
            shot(image, hwnd, method, "live2-err")
        except Exception:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
