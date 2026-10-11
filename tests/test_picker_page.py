"""永久真机原帧上的整页名称读取；输入只用替身，不发送游戏动作。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtGui import QImage

from zzz_ddtool import gamefilter as gf, ocr, picker_names
from zzz_ddtool.control import checkpoint_input, controlled_inputs
from zzz_ddtool.filter_replay import FilterReplaySession, identity_matches, make_identity

BASE = Path(__file__).resolve().parents[1] / "手册/参考图/驱动盘筛选"
CASES = [
    ("套装弹窗-顶部.png", ("荆棘玫瑰", "月光骑士颂", "雪兔梦游仙境")),
    ("套装弹窗-选择段2-前.png", ("折枝剑歌", "静听嘉音")),
    ("套装弹窗-震星迪斯科候选漏字.png", ("震星迪斯科", "激素朋克")),
    ("套装弹窗-混沌爵士零件数.png", ("混沌爵士", "原始朋克")),
]


@pytest.fixture(scope="module", params=CASES, ids=lambda item: item[0])
def page(request):
    filename, targets = request.param
    image = QImage(str(BASE / filename))
    assert not image.isNull() and (image.width(), image.height()) == (1920, 1080)
    layout = gf._calibrated_layout(1920, 1080)
    coarse = ocr.read_rect(image, layout.list_area)
    bands = gf._picker_name_bands(image, coarse, layout)
    names = ocr.read_name_bands(image, bands)
    return image, layout, bands, names, targets


def test_complete_page_reads_twelve_names_and_stable_precise_identities(page):
    image, _, bands, names, targets = page
    assert len(bands) == len(names) == 12
    assert all(line is not None for line in names)
    assert len({line.text for line in names}) == 12
    by_name = {line.text: line for line in names}
    for target in targets:
        line = by_name[target]
        identity = make_identity(image, (line.left, line.top, line.right, line.bottom))
        assert identity is not None and identity_matches(image, identity) is True
        assert gf._pre_click_state(image, line, identity) is True


def test_page_learning_then_replay_uses_real_identity_and_no_name_ocr(page, monkeypatch):
    image, layout, _, names, targets = page
    selected = set()
    popup = [True]
    sent = []
    by_name = {line.text: line for line in names}
    original_selected = gf._picker_selected
    monkeypatch.setattr(gf, "_grab", lambda: (image, 1))
    monkeypatch.setattr(gf, "_window_dpi", lambda _: 96)
    monkeypatch.setattr(gf, "window_pid", lambda _: 2)
    monkeypatch.setattr(gf, "_establish_origin", Mock())  # 中部静态帧不证明顶部/真实翻页
    monkeypatch.setattr(gf.time, "sleep", lambda _: None)
    monkeypatch.setattr(gf, "_picker_popup_closed", lambda _: not popup[0])
    monkeypatch.setattr(gf, "_panel_present", lambda _: not popup[0])

    def name_of_box(box, tol=12):
        """按名称框认名字；底图为同字数固定宽的“字区”框，与 OCR 框可差十几像素内（格距 164px，无歧义）。"""
        left, top, right, bottom = box
        return next((n for n, line in by_name.items()
                     if abs(line.left - left) <= tol and abs(line.top - top) <= tol
                     and abs(line.right - right) <= tol and abs(line.bottom - bottom) <= tol), None)

    def state(frame, ref):
        name = name_of_box((ref.left, ref.top, ref.right, ref.bottom))
        return True if name in selected else original_selected(frame, ref)

    def click(hwnd, x, y):
        checkpoint_input()
        checkpoint_input()
        name = next((n for n, line in by_name.items() if line.left <= x <= line.right
                     and line.top <= y <= line.bottom), None)
        if name is None:
            assert layout.confirm_area[0] <= x <= layout.confirm_area[2]
            assert layout.confirm_area[1] <= y <= layout.confirm_area[3]
            popup[0] = False
        else:
            assert name in targets and name not in selected
            selected.add(name)
        sent.append((x, y))

    monkeypatch.setattr(gf, "_picker_selected", state)
    monkeypatch.setattr(gf, "click_client", click)
    monkeypatch.setattr(gf, "wheel_client", Mock(side_effect=AssertionError("本帧全部目标已可见")))
    session = FilterReplaySession()
    with controlled_inputs(lambda: 0):
        gf._select_sets_with_replay(1, targets, width=1920, height=1080, replay_session=session)
    assert selected == set(targets) and len(sent) == len(targets) + 1
    assert session.last_metrics["success"] and len(session.routes) == 1
    learned = list(sent)
    selected.clear(); sent.clear(); popup[0] = True
    monkeypatch.setattr(gf, "read_rect", Mock(side_effect=AssertionError("回放不能查找 OCR")))
    monkeypatch.setattr(gf, "read_name_bands", Mock(side_effect=AssertionError("回放不能批读 OCR")))
    with controlled_inputs(lambda: 0):
        gf._select_sets_with_replay(1, targets, width=1920, height=1080, replay_session=session)
    assert sent == learned and selected == set(targets)
    assert session.last_metrics["search_ocr_calls"] == 0


def test_full_row_bands_do_not_depend_on_each_candidate_x_extent():
    image = QImage(1920,1080,QImage.Format.Format_RGB32)
    line = ocr.OcrLine("星迪斯科", 1, 883,738,948,761)
    bands = gf._picker_name_bands(image, [line], gf._calibrated_layout(1920,1080))
    assert bands == ((826,734,974,765),(990,734,1138,765),
                     (1154,734,1302,765),(1318,734,1466,765))


def test_cutoff_and_icon_text_are_not_complete_name_rows():
    image = QImage(1920,1080,QImage.Format.Format_RGB32)
    lines = [ocr.OcrLine("文字",1,860,338,940,355),
             ocr.OcrLine("DEMO",1,860,500,940,520),
             ocr.OcrLine("×12",1,860,500,940,520)]
    assert gf._picker_name_bands(image,lines,gf._calibrated_layout(1920,1080)) == ()


def test_batch_exception_does_not_change_global_engine_switches(monkeypatch):
    engine = ocr._get_engine()
    before = tuple(getattr(engine,x) for x in ocr._WORD_BOX_ATTRS)
    monkeypatch.setattr(engine,"text_rec",Mock(side_effect=RuntimeError("backend")))
    image = QImage(1920,1080,QImage.Format.Format_RGB32)
    with pytest.raises(RuntimeError,match="backend"):
        ocr.read_name_bands(image,[(826,480,974,520)])
    assert tuple(getattr(engine,x) for x in ocr._WORD_BOX_ATTRS) == before


def test_batch_result_count_mismatch_is_not_silently_zipped(monkeypatch):
    engine = ocr._get_engine()
    monkeypatch.setattr(engine,"text_rec",lambda _: SimpleNamespace(txts=[],word_results=[]))
    image = QImage(1920,1080,QImage.Format.Format_RGB32)
    with pytest.raises(ocr.OcrError,match="数量"):
        ocr.read_name_bands(image,[(826,480,974,520)])


def test_real_scroll_edge_excludes_clipped_bottom_row():
    image = QImage(str(BASE / "套装弹窗-滚动边缘四行.png"))
    assert not image.isNull()
    layout = gf._calibrated_layout(1920, 1080)
    bands = gf._picker_name_bands(image, ocr.read_rect(image, layout.list_area), layout)
    names = ocr.read_name_bands(image, bands)
    assert [line.text if line else None for line in names] == [
        "囚徒手记", "雪兔梦游仙境", "流光咏叹", "沧浪行歌",
        "月光骑士颂", "拂晓生花", "山大王", "云岿如我",
        "法厄同之歌", "如影相随", "折枝剑歌", "静听嘉音",
    ]


def test_edge_rows_are_removed_before_complete_row_count():
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    lines = [ocr.OcrLine("文字", 1, 860, y, 940, y + 20)
             for y in (350, 480, 630, 780, 812)]
    assert len(gf._picker_name_bands(image, lines, gf._calibrated_layout(1920, 1080))) == 12


def test_four_complete_rows_still_fail():
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    lines = [ocr.OcrLine("文字", 1, 860, y, 940, y + 20)
             for y in (400, 500, 600, 700)]
    with pytest.raises(gf.FilterClickError, match="几何"):
        gf._picker_name_bands(image, lines, gf._calibrated_layout(1920, 1080))


def test_real_bottom_name_precision():
    image = QImage(str(BASE / "套装弹窗-底部獠牙错字.png"))
    assert not image.isNull()
    layout = gf._calibrated_layout(1920, 1080)
    bands = gf._picker_name_bands(image, ocr.read_rect(image, layout.list_area), layout)
    names = ocr.read_name_bands(image, bands)
    assert names[9].text == "獠牙重金属"
    assert len(names) == 12 and names[10:] == [None, None]
    matched = gf._match_targets([line for line in names if line], ["獠牙重金属"])
    assert matched["獠牙重金属"] == names[9]
    line = names[9]
    identity = make_identity(image, (line.left, line.top, line.right, line.bottom))
    assert identity is not None and identity_matches(image, identity) is True
    assert gf._pre_click_state(image, line, identity) is True


def test_name_batch_removes_chroma_without_rewriting_recognized_text(monkeypatch):
    import numpy as np
    result = SimpleNamespace(txts=["撩牙重金属"], word_results=[()])
    observed = []

    def recognize(request):
        crop, = request.img
        observed.append(crop)
        return result

    engine = SimpleNamespace(text_rec=recognize, cal_rec_boxes=lambda *args: result)
    chars = [ocr.OcrLine(c, .9, 100 + i * 10, 200, 110 + i * 10, 220)
             for i, c in enumerate("撩牙重金属")]
    monkeypatch.setattr(ocr, "_get_engine", lambda: engine)
    monkeypatch.setattr(ocr, "_char_boxes", lambda *args: chars)
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0xff224488)
    line, = ocr.read_name_bands(image, [(90, 190, 250, 230)])
    assert len(observed) == 1 and observed[0].shape == (40, 160, 3)
    assert np.array_equal(observed[0][:, :, 0], observed[0][:, :, 1])
    assert np.array_equal(observed[0][:, :, 1], observed[0][:, :, 2])
    assert line == ocr.OcrLine("撩牙重金属", .9, 100, 200, 150, 220)


def test_measured_three_row_pages_cover_contiguous_rows():
    expected = [
        "荆棘玫瑰", "谶羽之誓", "拂晓行纪", "呼啸沙龙",
        "囚徒手记", "雪兔梦游仙境", "流光咏叹", "沧浪行歌",
        "月光骑士颂", "拂晓生花", "山大王", "云岿如我",
        "法厄同之歌", "如影相随", "折枝剑歌", "静听嘉音",
        "混沌爵士", "原始朋克", "啄木鸟电音", "河豚电音",
        "震星迪斯科", "自由蓝调", "激素朋克", "灵魂摇滚",
        "摇摆爵士", "炎狱重金属", "混沌重金属", "雷暴重金属",
        "极地重金属", "獠牙重金属",
    ]
    layout = gf._calibrated_layout(1920, 1080)
    assert layout.layout_id == "1920x1080-v8"
    assert (gf._PICKER_SCROLL_EVENTS, gf._PICKER_SCROLL_DELTA) == (1, -8)
    for suffix, start, stop in [("顶部", 0, 12), ("中部", 12, 24), ("底部", 20, 30)]:
        image = QImage(str(BASE / f"套装弹窗-三行翻页-{suffix}.png"))
        assert not image.isNull()
        bands = gf._picker_name_bands(image, ocr.read_rect(image, layout.list_area), layout)
        names = ocr.read_name_bands(image, bands)
        assert [line.text for line in names if line] == expected[start:stop]


CANONICAL_SET_NAMES = [
    "荆棘玫瑰", "谶羽之誓", "拂晓行纪", "呼啸沙龙",
    "囚徒手记", "雪兔梦游仙境", "流光咏叹", "沧浪行歌",
    "月光骑士颂", "拂晓生花", "山大王", "云岿如我",
    "法厄同之歌", "如影相随", "折枝剑歌", "静听嘉音",
    "混沌爵士", "原始朋克", "啄木鸟电音", "河豚电音",
    "震星迪斯科", "自由蓝调", "激素朋克", "灵魂摇滚",
    "摇摆爵士", "炎狱重金属", "混沌重金属", "雷暴重金属",
    "极地重金属", "獠牙重金属",
]


def test_picker_name_templates_cover_all_thirty_names():
    entries = picker_names.load_entries()
    assert {entry.name for entry in entries} == set(CANONICAL_SET_NAMES)
    assert all(entry.array.size for entry in entries)


def test_templates_decide_real_bottom_and_misread_frames():
    """2026-10-11 误读帧与既有底部帧：模板逐带认定，含「獠牙重金属」不靠 OCR。"""
    layout = gf._calibrated_layout(1920, 1080)
    expected = ["震星迪斯科", "自由蓝调", "激素朋克", "灵魂摇滚", "摇摆爵士",
                "炎狱重金属", "混沌重金属", "雷暴重金属", "极地重金属", "獠牙重金属",
                None, None]
    for filename in ("套装弹窗-三行翻页-底部.png", "套装弹窗-底部獠牙误读-20261011.png"):
        image = QImage(str(BASE / filename))
        assert not image.isNull()
        bands = gf._picker_name_bands(image, ocr.read_rect(image, layout.list_area), layout)
        got = [picker_names.classify(image, band) for band in bands]
        assert [hit[0] if hit else None for hit in got] == expected, filename


def test_read_targets_decides_by_template_without_name_ocr(monkeypatch):
    """生产入口：獠牙重金属 由图片模板认定；批量名称 OCR 即使整批返回空也不影响。"""
    layout = gf._calibrated_layout(1920, 1080)
    image = QImage(str(BASE / "套装弹窗-底部獠牙误读-20261011.png"))
    metrics = gf._PickerMetrics("learn")
    monkeypatch.setattr(gf, "read_name_bands", lambda _image, _bands: [])

    matched = gf._read_picker_targets(image, ["獠牙重金属"], layout, metrics)

    assert matched is not None and "獠牙重金属" in matched
    line = matched["獠牙重金属"]
    assert line.text == "獠牙重金属"
    assert 40 < line.right - line.left < 130
