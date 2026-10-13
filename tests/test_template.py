import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from zzz_ddtool.screen import DRIVE_DISC, MENU, OVERWORLD, STORAGE, classify_image, is_page
from zzz_ddtool.template import assets_dir

REFERENCES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
PAGES = [
    ("大地图/普通大世界.png", OVERWORLD),
    ("功能菜单/仓库入口.png", MENU),
    ("音擎仓库/仓库列表.png", STORAGE),
    ("驱动盘仓库/列表与侧栏.png", DRIVE_DISC),
]


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app


def test_template_files_exist() -> None:
    expected = [
        "world/quick_wheel.png",
        "menu/back.png",
        "menu/warehouse.png",
        "storage/recycle.png",
        "storage/title.png",
        "discs/dismantle.png",
        "discs/tab.png",
        "discs/title.png",
    ]
    missing = [name for name in expected if not (assets_dir() / name).is_file()]
    assert missing == []


@pytest.mark.parametrize("filename,page", PAGES)
def test_reference_pages_hit_their_icons(qapp: QApplication, filename, page) -> None:
    sample = REFERENCES / filename
    assert sample.is_file(), f"固定样本缺失：{sample}"
    image = QImage(str(sample))
    assert not image.isNull(), filename
    assert is_page(image, page), filename
    assert classify_image(image) == page, filename


def test_recycle_and_dismantle_do_not_cross(qapp: QApplication) -> None:
    storage = QImage(str(REFERENCES / "音擎仓库/仓库列表.png"))
    discs = QImage(str(REFERENCES / "驱动盘仓库/列表与侧栏.png"))
    assert not storage.isNull() and not discs.isNull()
    assert is_page(storage, STORAGE)
    assert not is_page(storage, DRIVE_DISC)
    assert is_page(discs, DRIVE_DISC)
    assert not is_page(discs, STORAGE)
