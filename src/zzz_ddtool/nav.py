from __future__ import annotations

import time
from pathlib import Path

from zzz_ddtool.capture import CaptureError, capture_game, find_game_hwnd, grab_game_image, save_image
from zzz_ddtool.layout import BACK_BTN, DRIVE_DISC_TAB, OVERWORLD_MENU_BTN, WAREHOUSE_BTN, scale_point
from zzz_ddtool.logutil import get_logger
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
from zzz_ddtool.wininput import InputError, VK_ESCAPE, click_client, press_key


class NavError(RuntimeError):
    pass


def _shot(tag: str) -> tuple[object, str, Path]:
    dest = capture_game()
    from PySide6.QtGui import QImage

    image = QImage(str(dest))
    page = classify_image(image)
    get_logger("zzz_ddtool.nav").info("page=%s tag=%s file=%s", page, tag, dest.name)
    return image, page, dest


def _grab(hwnd: int | None = None):
    image, method, target = grab_game_image(hwnd)
    return image, classify_image(image), method, target


def _require(image, page: str, what: str) -> None:
    if is_page(image, page):
        return
    detail = " ".join(f"{name}={score:.2f}/{need:.2f}" for name, score, need in icon_report(image, page))
    get_logger("zzz_ddtool.nav").warning("page check failed expect=%s %s", page, detail)
    raise NavError(f"{what}：图标对不上（{detail}），已中断以免进错页。")


def _click(hwnd: int, image, point: tuple[int, int], alt: bool = False) -> None:
    x, y = scale_point(*point, image.width(), image.height())
    click_client(hwnd, x, y, alt=alt)


def _open_drive_disc(hwnd: int, image, page: str) -> tuple[object, str]:
    if is_page(image, DRIVE_DISC):
        return image, DRIVE_DISC
    if not is_page(image, STORAGE) and page not in {STORAGE, UNKNOWN}:
        raise NavError(f"现在不在仓库（{page}），不能切驱动盘。")
    _click(hwnd, image, DRIVE_DISC_TAB)
    time.sleep(0.7)
    image, page, _method, _hwnd = _grab(hwnd)
    if is_page(image, DRIVE_DISC):
        return image, DRIVE_DISC
    raise NavError("点了驱动盘页签，图标对不上，已中断。")


def goto_warehouse() -> Path:
    """大地图 ↔ Esc 功能页 → 仓库 → 驱动盘。不点强化 / 弃置 / 拆解。"""
    log = get_logger("zzz_ddtool.nav")
    try:
        image, _method, hwnd = grab_game_image()
    except CaptureError as exc:
        raise NavError(str(exc)) from exc

    page = classify_image(image)
    log.info("start page=%s size=%sx%s", page, image.width(), image.height())

    try:
        if is_page(image, DRIVE_DISC):
            dest = capture_game()
            return dest
        if is_page(image, STORAGE) or page in {STORAGE, UNKNOWN}:
            image, page = _open_drive_disc(hwnd, image, page)
            _require(image, DRIVE_DISC, "驱动盘页")
            dest = capture_game()
            return dest

        if page == OVERWORLD:
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.45)
            image, page, dest = _shot("after-esc")
            _require(image, MENU, "功能页")

        if page == MENU or is_page(image, MENU):
            _click(hwnd, image, WAREHOUSE_BTN)
            time.sleep(0.7)
            image, page, dest = _shot("after-warehouse")
            if is_page(image, MENU):
                raise NavError("点了仓库还在功能页。用管理员开工具再试，或确认没被别的窗口挡住。")
            if is_page(image, OVERWORLD):
                raise NavError("点了仓库却回到大地图，已中断。")
            _require(image, STORAGE, "仓库页")
            image, page = _open_drive_disc(hwnd, image, page)
            _require(image, DRIVE_DISC, "驱动盘页")
            dest = capture_game()
            return dest
    except InputError as exc:
        raise NavError(str(exc)) from exc

    raise NavError("当前不是大地图或菜单，已中断。先按 Esc 看能不能出菜单。")


def snapshot_pages() -> dict[str, Path]:
    """大地图 → Esc 功能页 → 仓库 → 驱动盘。按页名存图。"""
    log = get_logger("zzz_ddtool.nav")
    try:
        image, page, method, hwnd = _grab()
    except CaptureError as exc:
        raise NavError(str(exc)) from exc
    log.info("snapshot start page=%s size=%sx%s", page, image.width(), image.height())

    try:
        if page in {STORAGE, DRIVE_DISC, UNKNOWN}:
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.5)
            image, page, method, hwnd = _grab(hwnd)
        if page == MENU:
            _click(hwnd, image, BACK_BTN)
            time.sleep(0.6)
            image, page, method, hwnd = _grab(hwnd)
            if page == MENU:
                press_key(hwnd, VK_ESCAPE)
                time.sleep(0.5)
                image, page, method, hwnd = _grab(hwnd)
        if page != OVERWORLD:
            raise NavError(f"到不了大地图（现在是 {page}）。先停在能按 Esc 出菜单的大世界。")
        _require(image, OVERWORLD, "大地图页")

        world = save_image(image, hwnd, method, name="大地图页")

        press_key(hwnd, VK_ESCAPE)
        time.sleep(0.7)
        image, page, method, hwnd = _grab(hwnd)
        if page != MENU:
            _click(hwnd, image, OVERWORLD_MENU_BTN, alt=True)
            time.sleep(0.7)
            image, page, method, hwnd = _grab(hwnd)
        _require(image, MENU, "功能页")
        menu = save_image(image, hwnd, method, name="功能页")

        _click(hwnd, image, WAREHOUSE_BTN)
        time.sleep(0.8)
        image, page, method, hwnd = _grab(hwnd)
        if page == MENU:
            raise NavError("点了仓库还在功能页。用管理员开工具再试。")
        if page == OVERWORLD:
            raise NavError("点了仓库却回到大地图，已中断。")
        _require(image, STORAGE, "仓库页")
        warehouse = save_image(image, hwnd, method, name="仓库页")

        image, page = _open_drive_disc(hwnd, image, page)
        _require(image, DRIVE_DISC, "驱动盘页")
        discs = save_image(image, hwnd, method, name="驱动盘页")
    except InputError as exc:
        raise NavError(str(exc)) from exc

    log.info(
        "snapshot done world=%s menu=%s warehouse=%s discs=%s seen=%s",
        world.name,
        menu.name,
        warehouse.name,
        discs.name,
        page,
    )
    return {"大地图页": world, "功能页": menu, "仓库页": warehouse, "驱动盘页": discs}
