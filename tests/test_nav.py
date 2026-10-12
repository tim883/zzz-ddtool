"""nav 页签切换回归：认页即返回，不用固定等待兜底。

替身替换点击、抓图、认页与睡眠；不操作真实游戏，也不触发真实 OCR。
"""

from PySide6.QtGui import QImage

import pytest

import zzz_ddtool.nav as nav_module
from zzz_ddtool.nav import NavError


def blank_image() -> QImage:
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    return image


def _stub_common(monkeypatch):
    """点击、抓图、睡眠全部替身；返回记录点击与睡眠的容器。"""
    control = {"clicks": [], "sleeps": []}
    monkeypatch.setattr(nav_module, "_click", lambda hwnd, image, point, alt=False:
                        control["clicks"].append(point))
    monkeypatch.setattr(nav_module, "time", type("T", (), {
        "sleep": staticmethod(lambda seconds: control["sleeps"].append(seconds)),
    }))
    return control


def test_switch_tab_returns_on_first_recognized_frame(monkeypatch):
    """认页第一张就命中时不得有任何固定等待，且不再多抓图。"""
    control = _stub_common(monkeypatch)
    grabs = []

    def grab(hwnd):
        grabs.append(1)
        return blank_image(), "storage", "printwindow", 1

    monkeypatch.setattr(nav_module, "_grab", grab)
    monkeypatch.setattr(nav_module, "is_page", lambda image, page: True)

    image, method, target = nav_module._switch_tab(
        1, blank_image(), nav_module.ENGINE_TAB, nav_module.STORAGE, "音擎仓库"
    )

    assert image is not None and method == "printwindow" and target == 1
    assert control["clicks"] == [nav_module.ENGINE_TAB]
    assert grabs == [1], "认页命中后不得继续抓图"
    assert control["sleeps"] == [], "点完不得先固定等待再认页"


def test_switch_tab_recaptures_when_page_not_ready(monkeypatch):
    """认不出时按 ocr_retry 重拍，命中即返回；不消耗额外点击。"""
    control = _stub_common(monkeypatch)
    results = iter([False, False, True])
    grabs = []

    def grab(hwnd):
        grabs.append(1)
        return blank_image(), "unknown", "printwindow", 1

    monkeypatch.setattr(nav_module, "_grab", grab)
    monkeypatch.setattr(nav_module, "is_page", lambda image, page: next(results))

    nav_module._switch_tab(
        1, blank_image(), nav_module.ENGINE_TAB, nav_module.STORAGE, "音擎仓库", ocr_retry=3
    )

    assert len(grabs) == 3
    assert control["clicks"] == [nav_module.ENGINE_TAB]


def test_switch_tab_raises_after_clicks_exhausted(monkeypatch):
    """两轮都认不出：存现场并抛错，不静默继续。"""
    _stub_common(monkeypatch)
    monkeypatch.setattr(nav_module, "_grab", lambda hwnd: (blank_image(), "unknown", "printwindow", 1))
    monkeypatch.setattr(nav_module, "is_page", lambda image, page: False)
    monkeypatch.setattr(nav_module, "icon_report", lambda image, page: [("锚点", 0.10, 0.90)])
    monkeypatch.setattr(nav_module, "save_image", lambda *a, **kw: None)

    with pytest.raises(NavError) as excinfo:
        nav_module._switch_tab(
            1, blank_image(), nav_module.ENGINE_TAB, nav_module.STORAGE, "音擎仓库",
            clicks=2, ocr_retry=2,
        )
    assert "切音擎仓库失败" in str(excinfo.value)
    assert "锚点=0.10/0.90" in str(excinfo.value)
