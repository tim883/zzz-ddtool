"""永久真机原帧上的整页名称读取；输入只用替身，不发送游戏动作。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtGui import QImage

from zzz_ddtool import gamefilter as gf, ocr
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

    def state(frame, ref):
        name = next((n for n, line in by_name.items() if
                     (line.left,line.top,line.right,line.bottom) ==
                     (ref.left,ref.top,ref.right,ref.bottom)), None)
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
