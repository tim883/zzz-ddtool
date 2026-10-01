from __future__ import annotations

import time
from pathlib import Path

from zzz_ddtool.capture import CaptureError, capture_game, find_game_hwnd, grab_game_image, save_image
from zzz_ddtool.layout import (
    BACK_BTN,
    DRIVE_DISC_TAB,
    ENGINE_TAB,
    OVERWORLD_MENU_BTN,
    WAREHOUSE_BTN,
    scale_point,
)
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


def _open_drive_disc(hwnd: int, image, page: str, retries: int = 2) -> tuple[object, str]:
    """仓库页切到驱动盘页签，切完认页验证；没切过去就再点一次。"""
    for i in range(max(1, retries)):
        if is_page(image, DRIVE_DISC):
            return image, DRIVE_DISC
        if not (is_page(image, STORAGE) or page in {STORAGE, UNKNOWN}):
            break
        get_logger("zzz_ddtool.nav").info("点驱动盘页签 (第%d次)", i + 1)
        _click(hwnd, image, DRIVE_DISC_TAB)
        time.sleep(1.2)
        image, page, _method, _hwnd = _grab(hwnd)
        if is_page(image, DRIVE_DISC):
            return image, DRIVE_DISC
        # 切换失败存现场图，供事后查状态
        try:
            save_image(image, _hwnd, _method, name=f"nav-切页签失败{i + 1}")
        except Exception:
            pass
    detail = " ".join(f"{name}={score:.2f}/{need:.2f}" for name, score, need in icon_report(image, DRIVE_DISC))
    raise NavError(f"点了驱动盘页签还是没切过去（{detail}），已中断。")


def goto_warehouse() -> Path:
    """任意正常操作状态 → 驱动盘仓库页。

    逐状态认页处理，循环直到站稳驱动盘页：
    drive_disc=完事；storage=点驱动盘页签；menu=点仓库；overworld/unknown=Esc 剥一层
    （unknown=详情/缩放/强化弹层等正常操作残留页，每次 Esc 关一层再认）。
    全程不点强化/弃置/拆解。
    """
    log = get_logger("zzz_ddtool.nav")
    try:
        image, _method, hwnd = grab_game_image()
    except CaptureError as exc:
        raise NavError(str(exc)) from exc

    try:
        unknown_streak = 0
        for step in range(8):
            page = classify_image(image)
            log.info("goto warehouse step=%d page=%s", step, page)
            if is_page(image, DRIVE_DISC):
                return capture_game()
            if is_page(image, STORAGE) or page == STORAGE:
                image, page = _open_drive_disc(hwnd, image, page)
                return capture_game()
            if page == MENU:
                _click(hwnd, image, WAREHOUSE_BTN)
                time.sleep(0.8)
                image, page, _dest = _shot("after-warehouse")
                continue
            # overworld=Esc 出菜单是正常路径；unknown（详情/缩放/强化弹层）=先存图再剥一层
            if page == UNKNOWN:
                unknown_streak += 1
                if unknown_streak > 3:
                    raise NavError("连续 3 层都认不出页面，已中断（现场图见 captures）。")
                try:
                    save_image(image, hwnd, "printwindow", name=f"nav-unknown{unknown_streak}")
                except Exception:
                    pass
            else:
                unknown_streak = 0
            press_key(hwnd, VK_ESCAPE)
            time.sleep(0.6)
            image = grab_game_image()[0]
        raise NavError("转了 8 圈都没站上驱动盘页，已中断（页面状态见日志）。")
    except InputError as exc:
        raise NavError(str(exc)) from exc


def _switch_tab(
    hwnd: int,
    image,
    point: tuple[int, int],
    page: str,
    what: str,
    *,
    clicks: int = 2,
    ocr_retry: int = 3,
):
    """点一个仓库页签并认页（spec §5.3 的页签重试规则）。

    同一页签最多点 ``clicks`` 次；每次点击后的认页最多取 ``max(1, ocr_retry)``
    张截图（含首次）。仍认不出就存现场图并抛错，不盲点更多次。
    """
    log = get_logger("zzz_ddtool.nav")
    last = image
    for i in range(max(1, clicks)):
        _click(hwnd, last, point)
        time.sleep(1.1)
        for shot in range(max(1, ocr_retry)):
            img, _page, method, target = _grab(hwnd)
            if is_page(img, page):
                log.info("切页 %s 成功（第%d次点击，认页第%d张）", what, i + 1, shot + 1)
                return img, method, target
            last = img
        try:
            save_image(last, hwnd, "printwindow", name=f"nav-{what}切换失败{i + 1}")
        except Exception:
            pass
    detail = " ".join(f"{name}={score:.2f}/{need:.2f}" for name, score, need in icon_report(last, page))
    raise NavError(f"切{what}失败（{detail}），已中断以免进错页。")


def reset_to_top(*, ocr_retry: int = 3) -> Path:
    """切页置顶：驱动盘仓库 → 音擎仓库 → 驱动盘仓库（spec §4.3 reset_to_top）。

    只切页，不改筛选、不强化、不打标。游戏在切回驱动盘时把列表滚回顶部；
    成功返回置顶后的驱动盘页截图，调用方据此确认原点与行对齐。
    失败一律抛 NavError，由上层按任务失败终止，不发送额外归位操作掩盖失败。
    """
    log = get_logger("zzz_ddtool.nav")
    try:
        image, _method, hwnd = grab_game_image()
    except CaptureError as exc:
        raise NavError(str(exc)) from exc

    if not is_page(image, DRIVE_DISC):
        log.info("置顶前不在驱动盘页（%s），先走导航站稳", classify_image(image))
        goto_warehouse()
        image, _method, hwnd = grab_game_image(hwnd)
        if not is_page(image, DRIVE_DISC):
            raise NavError("置顶前到不了驱动盘仓库，已中断。")

    try:
        _switch_tab(hwnd, image, ENGINE_TAB, STORAGE, "音擎仓库", ocr_retry=ocr_retry)
        image, method, hwnd = _switch_tab(
            hwnd, image, DRIVE_DISC_TAB, DRIVE_DISC, "驱动盘仓库", ocr_retry=ocr_retry
        )
    except InputError as exc:
        raise NavError(str(exc)) from exc

    log.info("置顶完成，已回到驱动盘仓库顶部")
    return save_image(image, hwnd, method, name="置顶-驱动盘页")


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
