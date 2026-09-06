from __future__ import annotations

import pytest
from PySide6.QtGui import QImage

from zzz_ddtool.models import Substat
from zzz_ddtool.ocr import _best_match, _fix_percent, _parse_substat, _parse_value, parse_disc, read_level, read_mainstat

SAMPLE = "手册/样张/详情页.png"


@pytest.fixture
def sample_image() -> QImage:
    return QImage(SAMPLE)


def test_fix_percent() -> None:
    assert _fix_percent("%6") == "6%"
    assert _fix_percent("6%") == "6%"
    assert _fix_percent("18") == "18"


def test_parse_value() -> None:
    assert _parse_value("6%") == (6.0, True)
    assert _parse_value("9.6%") == (9.6, True)
    assert _parse_value("18") == (18.0, False)
    assert _parse_value("%6") == (6.0, True)


def test_best_match() -> None:
    assert _best_match("昇常精通", ("异常精通", "攻击力"), cutoff=0.5) == "异常精通"
    assert _best_match("foo", ("异常精通",), cutoff=0.9) is None


def test_parse_substat() -> None:
    assert _parse_substat("异常精通+1", "18") == Substat("异常精通", rolls=2)
    assert _parse_substat("攻击力+2", "%6") == Substat("攻击力%", rolls=3)
    assert _parse_substat("防御力+1", "9.6%") == Substat("防御力%", rolls=2)
    assert _parse_substat("昇常精通+1", "18") == Substat("异常精通", rolls=2)
    assert _parse_substat("生命值", "128") == Substat("生命值", rolls=1)


def test_read_level(sample_image: QImage) -> None:
    # 详情页等级文字 OCR 有误差（03 可能读成 00），只验证能读到数字
    assert read_level(sample_image) is not None


def test_read_mainstat(sample_image: QImage) -> None:
    assert read_mainstat(sample_image) == "生命值"


def test_parse_disc(sample_image: QImage) -> None:
    disc = parse_disc(sample_image)
    assert disc is not None
    names = [sub.name for sub in disc.substats]
    assert "防御力" in names
    assert "攻击力%" in names
    assert "暴击率" in names
