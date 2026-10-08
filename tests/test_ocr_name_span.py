"""`read_name_span`：候选整行框里的「名称字符范围」隔离（spec §3.3 / R20 / R21 / R25）。

参考图用 tests/fixtures/warehouse-detail.png（真机 1920×1080）里的「等级15」那一行：
它是**同一行里「名称 + 件数」**的真实样本，正好覆盖「行框含件数后缀、要把名称
精确隔离出来」的场景，不用另造图。

全文件共 9 次真实 OCR（read_rect 2 + read_name_span 6 + 逐字符框探针 1；异常路径
那次只跑检测就抛错，空名称那条直接返回、不发识别），单次约 0.2—1.3 秒。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QImage

from zzz_ddtool import ocr
from zzz_ddtool.ocr import OcrLine, read_name_span, read_rect

SAMPLE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "warehouse-detail.png"

#: 「等级15」那一行的设计坐标区域（1920x1080 图上 1:1）。
ROW_RECT = (120, 330, 220, 380)
#: 行内名称与件数：行框含件数后缀，名称范围必须只取「等级」两个字。
NAME = "等级"
COUNT = "15"


def _switches(engine) -> tuple[object, object]:
    """引擎上的两个字符框开关；属性不存在按 None 记（R25 用同一口径）。"""
    return (
        getattr(engine, "return_word_box", None),
        getattr(engine, "return_single_char_box", None),
    )


def _restore_switches(engine, saved: tuple[object, object]) -> None:
    """把引擎上的两个开关按进入前的样子放回去（None 表示属性本来不存在）。"""
    for attr, value in zip(ocr._WORD_BOX_ATTRS, saved):
        if value is None:
            if hasattr(engine, attr):
                delattr(engine, attr)
        else:
            setattr(engine, attr, value)


def _assert_inside(span: OcrLine, row: OcrLine) -> None:
    assert span.left >= row.left, f"名称框左越界：{span!r} 不在 {row!r} 内"
    assert span.top >= row.top, f"名称框上越界：{span!r} 不在 {row!r} 内"
    assert span.right <= row.right, f"名称框右越界：{span!r} 不在 {row!r} 内"
    assert span.bottom <= row.bottom, f"名称框下越界：{span!r} 不在 {row!r} 内"


@pytest.fixture(scope="module")
def sample_image() -> QImage:
    image = QImage(str(SAMPLE))
    assert not image.isNull(), f"参考图缺失或无法解码：{SAMPLE}"
    assert (image.width(), image.height()) == (1920, 1080)
    return image


@pytest.fixture(scope="module")
def rows(sample_image: QImage) -> list[OcrLine]:
    """用真实 read_rect 找到那一行；识别文本必须是「等级15」。"""
    lines = read_rect(sample_image, ROW_RECT)
    assert len(lines) == 1, f"该区域应只有一行，实际 {lines!r}"
    assert lines[0].text == f"{NAME}{COUNT}", f"行文本对不上：{lines[0]!r}"
    return lines


def test_name_span_of_whole_row_text(sample_image: QImage, rows: list[OcrLine]) -> None:
    """用该行文本本身当 name：返回框非空、落在行框内、text 等于 name。"""
    engine = ocr._get_engine()
    before = _switches(engine)

    span = read_name_span(sample_image, rows[0], name=rows[0].text)

    assert span is not None, "整行文本就是目标名，必须隔离成功"
    assert span.text == rows[0].text
    assert 0.0 < span.score <= 1.0, f"score 应为目标字符置信度：{span.score}"
    assert span.right > span.left and span.bottom > span.top
    _assert_inside(span, rows[0])
    assert _switches(engine) == before == (False, False), "正常返回也必须还原引擎开关"


def test_name_span_isolates_name_from_count_suffix(
    sample_image: QImage, rows: list[OcrLine]
) -> None:
    """行框含件数后缀时，只取名称字的范围：名称框比整行框窄，且不含件数字符。"""
    engine = ocr._get_engine()
    before = _switches(engine)

    name_span = read_name_span(sample_image, rows[0], name=NAME)
    whole_span = read_name_span(sample_image, rows[0], name=NAME + COUNT)

    assert name_span is not None and whole_span is not None
    assert name_span.text == NAME
    _assert_inside(name_span, rows[0])
    _assert_inside(whole_span, rows[0])
    # 件数「15」在名称右边：名称框必须更窄，左边界不右移。
    assert name_span.right < whole_span.right, f"{name_span!r} 把件数也框进去了"
    assert name_span.left == whole_span.left
    assert name_span.bottom == whole_span.bottom
    assert _switches(engine) == before, "多次调用后引擎开关仍要复位"


def test_name_span_matches_union_of_name_char_boxes(
    sample_image: QImage, rows: list[OcrLine]
) -> None:
    """返回框 = 目标字符框并集的外接矩形，且与非目标字符框无正面积交叠。

    先用一次真实识别把该行的逐字符框取出来做参照（这也是「不给字符框就当隔离
    失败」的前提：逐字符框在这台后端上确实可用）。
    """
    engine = ocr._get_engine()
    before = _switches(engine)
    row = rows[0]
    crop = sample_image.copy(row.left, row.top, row.right - row.left, row.bottom - row.top)
    try:
        with ocr._WORD_BOX_LOCK:
            result = engine(
                ocr.qimage_to_bgr(crop),
                return_word_box=True,
                return_single_char_box=True,
            )
    finally:
        _restore_switches(engine, before)

    chars = ocr._char_boxes(result, row.left, row.top)
    assert [char.text for char in chars] == list(f"{NAME}{COUNT}"), (
        f"逐字符框不可用或不按字符拆分：{[c.text for c in chars]!r}"
    )
    assert all(char.right > char.left for char in chars)

    span = read_name_span(sample_image, row, name=NAME)
    assert span is not None

    targets = chars[: len(NAME)]
    others = chars[len(NAME) :]
    assert span.left == min(char.left for char in targets)
    assert span.top == min(char.top for char in targets)
    assert span.right == max(char.right for char in targets)
    assert span.bottom == max(char.bottom for char in targets)
    assert span.score == min(char.score for char in targets)

    for char in others:
        overlap_x = min(span.right, char.right) - max(span.left, char.left)
        overlap_y = min(span.bottom, char.bottom) - max(span.top, char.top)
        assert not (overlap_x > 0 and overlap_y > 0), (
            f"名称框 {span!r} 与件数字框 {char!r} 有正面积交叠"
        )
    assert _switches(engine) == before


def test_name_span_returns_none_for_unknown_name(
    sample_image: QImage, rows: list[OcrLine], monkeypatch: pytest.MonkeyPatch
) -> None:
    """换成该行不含的名字返回 None；空名直接 None 且不发识别。"""
    engine = ocr._get_engine()
    before = _switches(engine)

    assert read_name_span(sample_image, rows[0], name="完全不存在的名字XYZ") is None
    assert _switches(engine) == before, "返回 None 也必须还原引擎开关"

    def _boom():
        raise AssertionError("空名称不该触发任何 OCR")

    monkeypatch.setattr(ocr, "_get_engine", _boom)
    assert read_name_span(sample_image, rows[0], name="  ") is None


def test_name_span_restores_engine_switches_on_backend_error(
    sample_image: QImage, rows: list[OcrLine], monkeypatch: pytest.MonkeyPatch
) -> None:
    """内部调用抛异常时：异常原样传播，两个开关照样还原。"""
    engine = ocr._get_engine()
    before = _switches(engine)
    seen: list[tuple[object, object]] = []

    class _BoomRecognizer:
        """替掉识别器：此时引擎已被写入字符框模式，然后抛错。"""

        def __call__(self, *_args, **_kwargs):
            seen.append(_switches(engine))
            raise RuntimeError("识别器炸了")

    monkeypatch.setattr(engine, "text_rec", _BoomRecognizer())
    with pytest.raises(RuntimeError, match="识别器炸了"):
        read_name_span(sample_image, rows[0], name=NAME)

    assert seen == [(True, True)], f"异常前应当已进入字符框模式，实际 {seen!r}"
    assert _switches(engine) == before, "异常路径也必须还原引擎开关"


def test_read_rect_after_name_span_is_unchanged(
    sample_image: QImage, rows: list[OcrLine], monkeypatch: pytest.MonkeyPatch
) -> None:
    """交错的普通 read_rect 仍正常返回，且不会残留字符框模式去多做字符框计算。"""
    engine = ocr._get_engine()
    assert _switches(engine) == (False, False)
    char_box_calls: list[int] = []
    real_cal_rec_boxes = engine.cal_rec_boxes

    class _CountingCalRecBoxes:
        def __call__(self, *args, **kwargs):
            char_box_calls.append(1)
            return real_cal_rec_boxes(*args, **kwargs)

    monkeypatch.setattr(engine, "cal_rec_boxes", _CountingCalRecBoxes())

    again = read_rect(sample_image, ROW_RECT)

    assert [line.text for line in again] == [line.text for line in rows]
    assert char_box_calls == [], "普通 read_rect 不该计算字符框（临时模式泄漏）"
    assert _switches(engine) == (False, False)


@pytest.mark.parametrize("split_lines", [False, True])
def test_name_span_preserves_line_character_order_and_rejects_multiple_lines(monkeypatch, split_lines):
    """不同 top 不重排单行；两条文字行不能被拼接成套装名。"""
    from types import SimpleNamespace
    from unittest.mock import Mock
    chars = (
        ("震", 0.9, [[0, 2], [10, 2], [10, 22], [0, 22]]),
        ("星", 0.9, [[11, 1], [21, 1], [21, 21], [11, 21]]),
        ("迪", 0.9, [[22, 1], [32, 1], [32, 21], [22, 21]]),
        ("斯", 0.9, [[33, 1], [43, 1], [43, 21], [33, 21]]),
        ("科", 0.9, [[44, 1], [54, 1], [54, 21], [44, 21]]),
    )
    result = SimpleNamespace(word_results=(chars[:2], chars[2:]) if split_lines else (chars,))
    engine = Mock(return_value=result)
    engine.return_word_box = False
    engine.return_single_char_box = False
    monkeypatch.setattr(ocr, "_get_engine", lambda: engine)
    image = QImage(60, 30, QImage.Format_RGB32)
    image.fill(0)
    line = OcrLine(text="震星迪斯科", score=0.9, left=0, top=0, right=60, bottom=30)
    span = read_name_span(image, line, name="震星迪斯科")
    if split_lines:
        assert span is None
    else:
        assert span is not None and span.text == "震星迪斯科"
        assert (span.left, span.top, span.right, span.bottom) == (0, 1, 54, 22)
    assert _switches(engine) == (False, False)
