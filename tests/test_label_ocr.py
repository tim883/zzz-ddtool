"""固定提示行：完整语法、置信度与真实图像；不操作窗口。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtGui import QImage

from zzz_ddtool import ocr


@pytest.mark.parametrize("text,locked,discarded", [
    ("R弃置T锁定", False, False),
    ("R弃置T解除锁定", True, False),
    ("R解除弃置T锁定", False, True),
    ("R取消弃置T锁定", False, True),
    ("弃置锁定", False, False),
    ("R弃置", None, False),
    ("T解除锁定", True, None),
    ("R解除T锁定", None, None),
    ("R弃置T解除", None, None),
    ("杂字R弃置T锁定", None, None),
])
def test_fast_label_reads_only_one_line_without_detection(monkeypatch, text, locked, discarded):
    engine = Mock()
    engine.text_rec.return_value = SimpleNamespace(txts=[text], scores=[0.99])
    monkeypatch.setattr(ocr, "_get_engine", lambda: engine)
    detector = Mock(side_effect=AssertionError("快速标签禁止检测"))
    monkeypatch.setattr(ocr, "read_rect", detector)
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    assert ocr.read_action_state_fast(image) == {"locked": locked, "discarded": discarded}
    batch = engine.text_rec.call_args.args[0].img
    assert len(batch) == 1 and batch[0].shape == (31, 280, 3)
    detector.assert_not_called()


@pytest.mark.parametrize("score", [0.5, float("nan")])
def test_low_confidence_keeps_both_states_unknown(monkeypatch, score):
    engine = Mock()
    engine.text_rec.return_value = SimpleNamespace(txts=["R弃置T锁定"], scores=[score])
    monkeypatch.setattr(ocr, "_get_engine", lambda: engine)
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    assert ocr.read_action_state_fast(image) == {"locked": None, "discarded": None}


@pytest.mark.parametrize("relative,expected", [
    ("tests/fixtures/驱动盘仓库/列表与侧栏.png", {"locked": True, "discarded": False}),
    ("tests/fixtures/驱动盘详情/首档复用-侧栏盘面.png", {"locked": False, "discarded": False}),
    ("tests/fixtures/驱动盘仓库/标签-已弃置.png", {"locked": False, "discarded": True}),
])
def test_fast_label_archive_replay(relative, expected):
    path = Path(__file__).resolve().parents[1] / relative
    image = QImage(str(path))
    assert not image.isNull(), f"缺少永久样本：{path}"
    assert ocr.read_action_state_fast(image) == expected
