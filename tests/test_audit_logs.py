"""审计日志：截图 / 识别 / 认页每次调用都要留痕。

用途：跑完一轮后拿 ddtool.log 对账——有没有多余截图、有没有重复识别、
认页依据对不对。这些是 DEBUG 级，默认「信息」级不输出。

注意：这里不能用 pytest 的 caplog。`logutil.setup()` 把 `zzz_ddtool` 这个 logger
的 propagate 关成了 False（日志只进文件、不往 root 走），而 caplog 的 handler 挂在
root 上，所以收不到 `zzz_ddtool.*` 的日志。改成把收集器直接挂到目标 logger。
"""

from __future__ import annotations

import logging

import pytest

from PySide6.QtGui import QImage

from zzz_ddtool import ocr, screen


class _EmptyResult:
    """替身 OCR 结果：零文本，省掉加载 30MB 模型。"""

    txts: list = []
    boxes: list = []
    scores: list = []


@pytest.fixture
def blank_image() -> QImage:
    image = QImage(1920, 1080, QImage.Format.Format_RGB888)
    image.fill(0xFFFFFF)
    return image


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "_get_engine", lambda: (lambda _crop: _EmptyResult()))


@pytest.fixture
def capture_logs():
    """把收集器挂到目标 logger 上，返回一个「开始收集」的函数。"""
    installed: list[tuple[logging.Logger, logging.Handler, int]] = []

    def start(logger_name: str) -> list[str]:
        messages: list[str] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        logger = logging.getLogger(logger_name)
        handler = _Collect()
        installed.append((logger, handler, logger.level))
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        return messages

    yield start
    for logger, handler, level in installed:
        logger.removeHandler(handler)
        logger.setLevel(level)


def test_read_rect_logs_every_call(blank_image, fake_engine, capture_logs) -> None:
    messages = capture_logs("zzz_ddtool.ocr")
    ocr.read_rect(blank_image, (100, 100, 200, 200))
    assert any("识别" in m and "行" in m for m in messages)


def test_same_image_keeps_same_id(blank_image, fake_engine, capture_logs) -> None:
    """同一张截图上的多次识别必须带同一个「图#」标识，才能数出重复识别。"""
    messages = capture_logs("zzz_ddtool.ocr")
    ocr.read_rect(blank_image, (100, 100, 200, 200))
    ocr.read_rect(blank_image, (166, 267, 300, 175))
    marks = [m.split("图#", 1)[1].rstrip("）") for m in messages if "图#" in m]
    assert len(marks) == 2
    assert marks[0] == marks[1]


def test_classify_image_logs_result(blank_image, capture_logs) -> None:
    messages = capture_logs("zzz_ddtool.screen")
    page = screen.classify_image(blank_image)
    assert any("认页" in m and page in m for m in messages)
