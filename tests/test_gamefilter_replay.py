"""套装弹窗路径回放：固定样本清单与静态判读（spec §8.1 R16 / R20—R22 / R25）。

R16（reference_manifest）消费 `tests/fixtures/filter-replay/manifest.json` 的全部静态样本：
`cases`、`substat_cases`、`name_span_cases` 逐项跑**真实**判读函数并比对 expected，
缺图或缺必要类别直接失败，不用替身充当字符 OCR。

R25（ocr_state_restoration）验证 `read_name_span` 在成功 / 返回 None / 后端抛异常三条退出路径后，
普通 OCR 引擎的字符框开关都恢复到进入前的值，且原异常不被吞掉。
"""

from __future__ import annotations

import gc
import json
import threading
import weakref
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtGui import QImage

from zzz_ddtool import gamefilter as gf
from zzz_ddtool import ocr
from zzz_ddtool import wininput
from zzz_ddtool.actionwait import InputRelease
from zzz_ddtool.filter_replay import (
    FilterReplaySession,
    SetPickerClick,
    SetPickerIdentity,
    SetPickerLayout,
    SetPickerRoute,
    SetPickerStage,
    identity_matches,
    make_identity,
)
from zzz_ddtool.control import checkpoint_input, controlled_inputs
from zzz_ddtool.hotkeys import RunnerStopped
from zzz_ddtool.ocr import OcrLine, read_name_span, read_rect

BASE = Path(__file__).resolve().parents[1]
MANIFEST = BASE / "tests" / "fixtures" / "filter-replay" / "manifest.json"
_DATA = json.loads(MANIFEST.read_text(encoding="utf-8"))
_LAYOUT_RAW = _DATA["layouts"][0]
LAYOUT = SetPickerLayout(
    layout_id=_LAYOUT_RAW["layout_id"],
    list_area=tuple(_LAYOUT_RAW["list_area"]),
    confirm_area=tuple(_LAYOUT_RAW["confirm_area"]),
    origin_mode=_LAYOUT_RAW["origin_mode"],
    top_wheel_events=_LAYOUT_RAW["top_wheel_events"],
    top_delta_units=_LAYOUT_RAW["top_delta_units"],
    scroll_x=_LAYOUT_RAW["scroll_x"],
    scroll_y=_LAYOUT_RAW["scroll_y"],
    scroll_wait_ms=_LAYOUT_RAW["scroll_wait_ms"],
    click_wait_ms=_LAYOUT_RAW["click_wait_ms"],
    confirm_wait_ms=_LAYOUT_RAW["confirm_wait_ms"],
)
_CASES = {case["id"]: case for case in _DATA["cases"]}

_images: dict[str, QImage] = {}
_lines: dict[str, list] = {}


def _image(relative: str) -> QImage:
    if relative not in _images:
        path = BASE / relative
        assert path.exists(), f"清单引用的图不存在：{relative}"
        image = QImage(str(path))
        assert not image.isNull(), f"固定样本无法解码：{relative}"
        _images[relative] = image
    return _images[relative]


def _ocr_lines(relative: str) -> list:
    if relative not in _lines:
        _lines[relative] = read_rect(_image(relative), (0, 0, 1920, 1080))
    return _lines[relative]


def _name_box(relative: str, name: str) -> tuple[int, int, int, int] | None:
    """在目标区域 OCR 找候选行 → read_name_span 隔离精确文字框（真实 OCR，不用替身）。

    套装名只在 layout.list_area 内找：弹窗左侧的效果说明栏也会显示已选套装名，不能拿它当格子名称；
    「确认筛选」在 layout.confirm_area 内找。
    """
    image = _image(relative)
    area = LAYOUT.confirm_area if name == "确认筛选" else LAYOUT.list_area
    left, top, right, bottom = area
    for line in _ocr_lines(relative):
        cx = (line.left + line.right) // 2
        cy = (line.top + line.bottom) // 2
        if not (left <= cx <= right and top <= cy <= bottom):
            continue
        if name in line.text.replace(" ", ""):
            span = read_name_span(image, line, name=name)
            if span is not None:
                return (span.left, span.top, span.right, span.bottom)
    return None


def _click_box(box: tuple[int, int, int, int]) -> SimpleNamespace:
    left, top, right, bottom = box
    return SimpleNamespace(left=left, top=top, right=right, bottom=bottom)


def _identity_of(case_id: str):
    case = _CASES[case_id]
    box = _name_box(case["image"], case["target"])
    assert box is not None, f"源用例 {case_id} 的名称隔离失败"
    identity = make_identity(_image(case["image"]), box)
    assert identity is not None, f"源用例 {case_id} 的指纹生成失败"
    return identity


def _case_params():
    for case in _DATA["cases"]:
        yield pytest.param(case, id=f"R16-{case['id']}")


def test_reference_manifest_cases() -> None:
    """R16：清单 cases 逐项真判读（每条单独断言，便于定位失败项）。"""
    failures: list[str] = []
    for case in _data_cases():
        expected = case["expected"]
        observation = case["observation"]
        if observation == "selected":
            continue  # 历史勾选语料保留；当前执行已移除此判据。
        image = _image(case["image"])
        if observation == "popup_open":
            got = gf._picker_popup_open(image)
        elif observation == "popup_closed":
            got = gf._picker_popup_closed(image)
        elif observation == "at_top":
            got = gf._picker_at_top(image, LAYOUT)
        elif observation == "stage_match":
            got = gf._picker_stage_matches(image, LAYOUT)
        elif observation == "enabled":
            box = _name_box(case["image"], case["target"])
            if box is None:
                failures.append(f"{case['id']}: 名称隔离失败（{case['target']}）")
                continue
            click = _click_box(box)
            got = gf._picker_enabled(image, click)
        elif observation == "identity_match":
            identity = _identity_of(case["identity_source_id"])
            got = identity_matches(image, identity)
        else:
            failures.append(f"{case['id']}: 未知 observation {observation!r}")
            continue
        if got != expected:
            failures.append(f"{case['id']}: {observation} 期望 {expected!r}，实得 {got!r}")
    assert not failures, "静态样本不符：\n" + "\n".join(failures)


def _data_cases():
    return _DATA["cases"]


def test_pre_click_failure_saves_the_rejected_frame_without_recapturing(monkeypatch, tmp_path):
    """保存身份不符那一帧，不能用稍后抓的另一帧代替。"""
    from PySide6.QtGui import QColor

    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    ref = _click_box((1188, 491, 1266, 517))
    image.setPixelColor(0, 0, QColor(180, 220, 0))
    monkeypatch.setattr(gf, "identity_matches", lambda *_: False)
    monkeypatch.setattr(gf.loground, "diagnostics_dir", lambda: tmp_path)
    monkeypatch.setattr(gf, "get_logger", lambda _: Mock())
    monkeypatch.setattr(gf, "_grab", Mock(side_effect=AssertionError("诊断不能重新截图")))
    assert gf._pre_click_state(image, ref, object()) is False
    image.fill(0)  # 后续状态已变化，落盘仍须保留导致拒绝的原图
    folder = tmp_path / "picker-failures"
    files = list(folder.glob("*.json"))
    assert len(files) == 1
    details = json.loads(files[0].read_text(encoding="utf-8"))
    assert details["identity"] is False
    assert "selected" not in details and "color_hits" not in details
    saved = QImage(str(files[0].with_suffix(".png")))
    assert not saved.isNull() and saved.pixelColor(0, 0) == QColor(180, 220, 0)


def test_reference_manifest_name_spans() -> None:
    """R16：name_span_cases 逐项跑真实字符框隔离，逐项比对返回框或 None。"""
    failures: list[str] = []
    for case in _DATA["name_span_cases"]:
        image = _image(case["image"])
        left, top, right, bottom = case["row_rect"]
        line = OcrLine(
            text=case["row_text"], left=left, top=top, right=right, bottom=bottom, score=1.0
        )
        result = read_name_span(image, line, name=case["name"])
        expected = case["expected_rect"]
        if expected is None:
            if result is not None:
                failures.append(f"{case['id']}: 期望 None，实得 ({result.left},{result.top},{result.right},{result.bottom})")
            continue
        if result is None:
            failures.append(f"{case['id']}: 期望 {expected}，实得 None")
            continue
        got = [result.left, result.top, result.right, result.bottom]
        if got != list(expected):
            failures.append(f"{case['id']}: 期望 {expected}，实得 {got}")


def test_reference_manifest_substats() -> None:
    """R16：substat_cases 用真实 `_panel_lines` → `_inspect_substat_targets` 逐项比对。"""
    failures: list[str] = []
    for case in _DATA["substat_cases"]:
        image = _image(case["image"])
        lines = gf._panel_lines(image)
        ready, found = gf._inspect_substat_targets(
            image, lines, names=case["names"], counts=case["counts"]
        )
        if ready != case["expected_ready"]:
            failures.append(f"{case['id']}: 就位期望 {case['expected_ready']!r}，实得 {ready!r}")
        keys = sorted(found.keys(), key=lambda k: (isinstance(k, int), k))
        if keys != sorted(case["expected_keys"], key=lambda k: (isinstance(k, int), k)):
            failures.append(f"{case['id']}: 键期望 {case['expected_keys']}，实得 {keys}")
        height = image.height()
        for key, line in found.items():
            if line.bottom > height - gf._PANEL_CLICK_BOTTOM_GAP:
                failures.append(f"{case['id']}: 键 {key} 的框落在底部点击禁区：{line.bottom}")
    assert not failures, "副属性样本不符：\n" + "\n".join(failures)


# ---------------------------------------------------------------- 行为用例（替身输入/OCR）

class _PickerStub:
    """弹窗替身：脚本化每屏内容、记录输入、按点击推进状态。

    只替换“读图/发输入”这两层；被验的是真实的学习/回放控制流与指标。
    """

    def __init__(self, screens, *, boxes=None, confirm_box=(1093, 846, 1197, 882), hwnd=100, width=1920, height=1080):
        self.screens = [list(screen) for screen in screens]
        self.index = 0
        self.selected: set[str] = set()
        self.popup_open = True
        self.hwnd = hwnd
        self.width = width
        self.height = height
        self.clicks: list[tuple[int, int]] = []
        self.wheels: list[tuple[int, int, int]] = []
        self.ocr_areas: list[tuple[int, int, int, int]] = []
        self.reads: list[tuple[int, int, int, int]] = []
        self.keys: list[int] = []
        self.span_names: list[str] = []
        self.confirm_box = confirm_box
        self.confirm_center = (
            (confirm_box[0] + confirm_box[2]) // 2,
            (confirm_box[1] + confirm_box[3]) // 2,
        )
        self.boxes = boxes or {}
        # 可调替身行为（各用例按需覆盖）
        self.at_top: bool | None = True
        self.enabled: bool | None = True
        self.identity_ok: bool | None = True
        self.mark_selected = True
        self.stop_after_clicks: int | None = None
            # 这些帧号（index）上 make_identity 返回 None，模拟「行被裁掉/遮挡」的本帧不可用
        self.identity_none_frames: set[int] = set()

    def reset_log(self) -> None:
        self.clicks.clear()
        self.wheels.clear()
        self.reads.clear()
        self.keys.clear()
        self.selected.clear()
        self.index = 0
        self.popup_open = True

    # ---- 供被验代码调用的替身 ----
    def box_of(self, screen_index: int, position: int, name: str) -> tuple[int, int, int, int]:
        if name in self.boxes:
            return self.boxes[name]
        col, row = position % 4, position // 4
        x = 900 + col * 163
        y = 502 + row * 150
        return (x - 38, y - 13, x + 38, y + 13)

    def name_boxes(self):
        screen = self.screens[min(self.index, len(self.screens) - 1)]
        return {name: self.box_of(self.index, pos, name) for pos, name in enumerate(screen)}

    def lines_for(self, area):
        layout = gf._calibrated_layout(self.width, self.height)
        if area == gf.FULL_WINDOW or (layout is not None and area == layout.list_area):
            screen = self.screens[min(self.index, len(self.screens) - 1)]
            lines = [
                OcrLine(text=name, left=b[0], top=b[1], right=b[2], bottom=b[3], score=1.0)
                for name, b in ((n, self.box_of(self.index, p, n)) for p, n in enumerate(screen))
            ]
            if area == gf.FULL_WINDOW and self.popup_open:
                left, top, right, bottom = self.confirm_box
                lines.append(OcrLine(text="确认筛选", left=left, top=top, right=right, bottom=bottom, score=1.0))
            return lines
        if area == layout.confirm_area and self.popup_open:
            left, top, right, bottom = self.confirm_box
            return [OcrLine(text="确认筛选", left=left, top=top, right=right, bottom=bottom, score=1.0)]
        if area == gf.FILTER_RESET_AREA:
            return [OcrLine(text="重置", left=1400, top=1000, right=1490, bottom=1030, score=1.0)]
        if area == gf.FILTER_SLOT_AREA:
            return [OcrLine(text="分区[1]", left=1500, top=700, right=1620, bottom=730, score=1.0)]
        if area == gf.FILTER_SETS_BAR:
            return [OcrLine(text="点击选择", left=1400, top=300, right=1520, bottom=330, score=1.0)]
        return []

    def apply_click(self, x: int, y: int) -> None:
        """点击后的页面状态推进（真实发送与替身共用）。"""
        cx, cy = self.confirm_center
        if abs(x - cx) <= 3 and abs(y - cy) <= 3:
            self.popup_open = False
            return
        name = self.name_at(x, y)
        if name is not None and self.mark_selected:
            self.selected.add(name)

    def apply_wheel(self) -> None:
        self.index = min(self.index + 1, len(self.screens) - 1)

    def name_at(self, x: int, y: int) -> str | None:
        for name, (left, top, right, bottom) in self.name_boxes().items():
            if left <= x <= right and top <= y <= bottom:
                return name
        return None


def _install_stub(monkeypatch: pytest.MonkeyPatch, stub: _PickerStub, *, passthrough: bool = False) -> None:
    """装替身。passthrough=True 时点击/滚轮/按键仍走真实 wininput（用于验证输入痕迹）。"""
    image = QImage(1920, 1080, QImage.Format_RGB32)
    image.fill(0)

    # 替身帧与真实模板资产无关：本组用例统一走名称带 OCR 路径，结果只由脚本决定。
    monkeypatch.setattr(gf, "_picker_templates_disabled", True)
    monkeypatch.setattr(gf, "_grab", lambda: (image, stub.hwnd))
    monkeypatch.setattr(gf, "_window_dpi", lambda _hwnd: 96)
    monkeypatch.setattr(gf, "window_pid", lambda _hwnd: 200)
    monkeypatch.setattr(gf, "find_game_hwnd", lambda: stub.hwnd)
    monkeypatch.setattr(gf, "client_size", lambda _hwnd: (stub.width, stub.height))
    monkeypatch.setattr(gf.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(gf, "read_rect", lambda _image, area: (stub.reads.append(tuple(area)), stub.lines_for(tuple(area)))[1])
    monkeypatch.setattr(gf, "read_name_bands", lambda _image, _bands: stub.lines_for(_layout().list_area))

    def _span(_image, line, *, name):
        stub.span_names.append(name)
        for candidate, box in stub.name_boxes().items():
            if candidate == name:
                return OcrLine(text=name, left=box[0], top=box[1], right=box[2], bottom=box[3], score=0.9)
        if name == "确认筛选":
            left, top, right, bottom = stub.confirm_box
            return OcrLine(text=name, left=left, top=top, right=right, bottom=bottom, score=0.9)
        return None

    monkeypatch.setattr(gf, "read_name_span", _span)

    def _identity(_image, box):
        if stub.index in stub.identity_none_frames:
            return None  # 本帧不可用：行被裁掉或被遮挡
        return SetPickerIdentity(
            left=box[0], top=box[1], right=box[2], bottom=box[3],
            width=box[2] - box[0], height=box[3] - box[1],
            glyph_mask=b"\x01" * ((box[2] - box[0]) * (box[3] - box[1])),
        )

    monkeypatch.setattr(gf, "make_identity", _identity)
    monkeypatch.setattr(gf, "identity_matches", lambda _image, _identity: stub.identity_ok)
    monkeypatch.setattr(gf, "_picker_enabled", lambda _image, _click: stub.enabled)

    monkeypatch.setattr(gf, "_picker_at_top", lambda _image, _layout: stub.at_top)
    monkeypatch.setattr(gf, "_picker_stage_matches", lambda _image, _layout: True)
    monkeypatch.setattr(gf, "_picker_popup_closed", lambda _image: not stub.popup_open)
    monkeypatch.setattr(gf, "_panel_visible", lambda: stub.popup_open is False)

    def _click(_hwnd, x, y):
        checkpoint_input()  # 与 wininput.click_client 一致：暂停/停止先过检查点
        if passthrough:
            result = wininput.click_client(_hwnd, x, y)
        else:
            result = InputRelease("click_client", 0)
        stub.clicks.append((x, y))
        stub.apply_click(x, y)
        return result

    def _wheel(_hwnd, x, y, delta=-3):
        checkpoint_input()
        if passthrough:
            result = wininput.wheel_client(_hwnd, x, y, delta)
        else:
            result = InputRelease("wheel_client", 0)
        stub.wheels.append((x, y, delta))
        stub.apply_wheel()
        return result

    def _key(_hwnd, vk):
        if passthrough:
            result = wininput.press_key(_hwnd, vk)
        else:
            result = InputRelease("press_key", 0)
        stub.keys.append(vk)
        return result

    monkeypatch.setattr(gf, "click_client", _click)
    monkeypatch.setattr(gf, "wheel_client", _wheel)
    monkeypatch.setattr(gf, "press_key", _key)
    # §4.2 释放预算在 gamefilter 内由 wait_release 消费；本组用例只验动作/痕迹，替换掉计时。
    monkeypatch.setattr(gf, "wait_release", lambda *args, **kwargs: None)


def _layout() -> SetPickerLayout:
    layout = gf._calibrated_layout(1920, 1080)
    assert layout is not None
    return layout


def test_learn_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """R01：空首屏不入路径；两段各 1 条 -8 滚动，三个目标各点一次，成功后 routes 数量为 1。"""
    stub = _PickerStub([[], ["静听嘉音", "折枝剑歌"], ["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    gf._select_sets_with_replay(
        stub.hwnd, ("河豚电音", "静听嘉音", "折枝剑歌"), width=1920, height=1080, replay_session=session
    )
    assert len(session.routes) == 1
    route = next(iter(session.routes.values()))
    assert [stage.wheel_events for stage in route.stages] == [1, 1]
    assert [stage.delta_units for stage in route.stages] == [-8, -8]
    assert [[click.name for click in stage.clicks] for stage in route.stages] == [
        ["静听嘉音", "折枝剑歌"],
        ["河豚电音"],
    ]
    assert stub.selected == {"河豚电音", "折枝剑歌", "静听嘉音"}
    assert len(stub.clicks) == 4  # 三个选项 + 一次确认
    metrics = session.last_metrics
    assert metrics is not None and metrics["mode"] == "learn" and metrics["success"] is True
    assert metrics["selection_clicks"] == 3
    assert metrics["wheel_events"] == 2
    assert metrics["search_ocr_calls"] >= 2


def test_different_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """R04：目标集合不同 → 缓存不命中，只点新集合里的目标。"""
    stub = _PickerStub([["谶羽之誓"], ["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    gf._select_sets_with_replay(
        stub.hwnd, ("谶羽之誓",), width=1920, height=1080, replay_session=session
    )
    assert stub.selected == {"谶羽之誓"}
    stub.index = 0
    stub.selected.clear()
    stub.popup_open = True
    gf._select_sets_with_replay(
        stub.hwnd, ("河豚电音",), width=1920, height=1080, replay_session=session
    )
    assert stub.selected == {"河豚电音"}
    assert len(session.routes) == 2
    assert session.last_metrics is not None and session.last_metrics["mode"] == "learn"


def test_center_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    """R05：记录的是名称框中心，不是文字框左上角；回放不再重复缩放。"""
    box = (1351, 770, 1429, 798)
    stub = _PickerStub([["河豚电音"]], boxes={"河豚电音": box})
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    gf._select_sets_with_replay(stub.hwnd, ("河豚电音",), width=1920, height=1080, replay_session=session)
    route = next(iter(session.routes.values()))
    click = route.stages[0].clicks[0]
    assert (click.x, click.y) == (1390, 784)
    assert (click.left, click.top) == (1351, 770)


def test_wheel_units(monkeypatch: pytest.MonkeyPatch) -> None:
    """R06：回放按记录逐条发事件，恰好两条 -3，不是一条 -6、也不是六条 -1。"""
    stub = _PickerStub([["河豚电音"]], boxes={"河豚电音": (1351, 770, 1429, 798)})
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    layout = _layout()
    key = gf._picker_key(stub.hwnd, 1920, 1080, ("谶羽之誓",), layout)
    session.routes[key] = SetPickerRoute(
        key=key,
        layout=layout,
        stages=(
            SetPickerStage(wheel_events=2, delta_units=-3, clicks=tuple()),
            SetPickerStage(
                wheel_events=0,
                delta_units=0,
                clicks=(
                    SetPickerClick(
                        name="谶羽之誓",
                        left=1351, top=770, right=1429, bottom=798, x=1390, y=784,
                        identity=SetPickerIdentity(left=1351, top=770, right=1429, bottom=798,
                                                   width=78, height=28, glyph_mask=b"\x01" * (78 * 28)),
                    ),
                ),
            ),
        ),
        confirm_x=1145, confirm_y=864,
        confirm_identity=SetPickerIdentity(left=1088, top=845, right=1203, bottom=883,
                                           width=115, height=38, glyph_mask=b"\x01" * (115 * 38)),
    )
    stub.screens = [["谶羽之誓"]]
    stub.boxes["谶羽之誓"] = (1351, 770, 1429, 798)
    gf._select_sets_with_replay(stub.hwnd, ("谶羽之誓",), width=1920, height=1080, replay_session=session)
    assert stub.wheels == [(960, 540, -3), (960, 540, -3)]
    assert session.last_metrics is not None and session.last_metrics["mode"] == "replay"
    assert session.last_metrics["search_ocr_calls"] == 0


def _run(session, stub, names, width=1920, height=1080):
    gf._select_sets_with_replay(stub.hwnd, names, width=width, height=height, replay_session=session)


def test_replay_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """R02：同一组套装第二次执行走回放，滚动/点击序列与首次相同，名称查找 OCR 为 0。"""
    stub = _PickerStub([[], ["静听嘉音", "折枝剑歌"], ["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    names = ("河豚电音", "静听嘉音", "折枝剑歌")
    _run(session, stub, names)
    first_clicks, first_wheels = list(stub.clicks), list(stub.wheels)
    stub.reset_log()
    _run(session, stub, names)
    assert stub.wheels == first_wheels
    assert stub.clicks == first_clicks
    metrics = session.last_metrics
    assert metrics is not None and metrics["mode"] == "replay" and metrics["search_ocr_calls"] == 0


def test_name_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """R03：输入顺序倒置仍命中同一键，回放按首次记录的顺序执行。"""
    stub = _PickerStub([["河豚电音", "静听嘉音", "折枝剑歌"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    _run(session, stub, ("河豚电音", "静听嘉音", "折枝剑歌"))
    first_clicks = list(stub.clicks)
    stub.reset_log()
    _run(session, stub, ("折枝剑歌", "静听嘉音", "河豚电音"))
    assert stub.clicks == first_clicks
    assert session.last_metrics is not None and session.last_metrics["mode"] == "replay"


@pytest.mark.parametrize("change", ["size", "pid", "dpi0"])
def test_new_environment(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    """R07：尺寸/pid/DPI 变化都不复用旧点击；新环境有标定才 learn，DPI=0 不缓存。"""
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    _run(session, stub, ("河豚电音",))
    assert len(session.routes) == 1
    stub.reset_log()
    if change == "size":
        _run(session, stub, ("河豚电音",), width=1600, height=900)
        assert session.routes == {}
        assert session.last_metrics is not None and session.last_metrics["mode"] == "legacy"
    elif change == "pid":
        monkeypatch.setattr(gf, "window_pid", lambda _hwnd: 201)
        _run(session, stub, ("河豚电音",))
        assert session.last_metrics is not None and session.last_metrics["mode"] == "learn"
        assert len(session.routes) == 1 and next(iter(session.routes)).pid == 201
    else:
        monkeypatch.setattr(gf, "_window_dpi", lambda _hwnd: 0)
        _run(session, stub, ("河豚电音",))
        assert session.routes == {}
        assert session.last_metrics is not None and session.last_metrics["mode"] == "legacy"


def test_cached_replay_does_not_check_origin_or_target(monkeypatch):
    """已有坐标按记录执行，顶部、名称身份、布局和确认按钮不再识别。"""
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    _run(session, stub, ("河豚电音",))
    expected = list(stub.clicks)
    stub.reset_log()
    for name in ("_picker_at_top", "_picker_stage_matches", "_pre_click_state",
                 "identity_matches", "_confirm_ready", "read_rect", "read_name_bands", "read_name_span"):
        monkeypatch.setattr(gf, name, Mock(side_effect=AssertionError("缓存目标不识别")))
    _run(session, stub, ("河豚电音",))
    assert stub.clicks == expected and stub.wheels == []
    assert session.last_metrics["mode"] == "replay" and session.last_metrics["success"]
    assert session.last_metrics["search_ocr_calls"] == 0
    assert session.last_metrics["verification_frames"] == 1  # 最终关闭确认


def test_wheel_top(monkeypatch: pytest.MonkeyPatch) -> None:
    """R09：wheel_top 先且仅先发 4 条 +3 回顶，再发路径的 1 条 -3；回顶不入 stage；指标 5。"""
    layout = SetPickerLayout(
        layout_id="wheel-top-test",
        list_area=(800, 340, 1490, 840),
        confirm_area=(1040, 838, 1260, 900),
        origin_mode="wheel_top",
        top_wheel_events=4,
        top_delta_units=3,
        scroll_x=960,
        scroll_y=540,
        scroll_wait_ms=0,
        click_wait_ms=0,
        confirm_wait_ms=0,
    )
    box = (1351, 770, 1429, 798)
    stub = _PickerStub([["谶羽之誓"]], boxes={"谶羽之誓": box})
    _install_stub(monkeypatch, stub)
    monkeypatch.setattr(gf, "_calibrated_layout", lambda _w, _h: layout)
    session = FilterReplaySession()
    key = gf._picker_key(stub.hwnd, 1920, 1080, ("谶羽之誓",), layout)
    session.routes[key] = SetPickerRoute(
        key=key,
        layout=layout,
        stages=(
            SetPickerStage(
                wheel_events=1,
                delta_units=-3,
                clicks=(
                    SetPickerClick(name="谶羽之誓", left=box[0], top=box[1], right=box[2], bottom=box[3],
                                   x=1390, y=784,
                                   identity=SetPickerIdentity(left=box[0], top=box[1], right=box[2], bottom=box[3],
                                                              width=78, height=28, glyph_mask=b"\x01" * (78 * 28))),
                ),
            ),
        ),
        confirm_x=1145,
        confirm_y=864,
        confirm_identity=SetPickerIdentity(left=1088, top=845, right=1203, bottom=883,
                                           width=115, height=38, glyph_mask=b"\x01" * (115 * 38)),
    )
    _run(session, stub, ("谶羽之誓",))
    assert stub.wheels == [(960, 540, 3)] * 4 + [(960, 540, -3)]
    assert session.last_metrics is not None and session.last_metrics["wheel_events"] == 5


def test_missing_checkmark_feedback_does_not_block_clicks(monkeypatch: pytest.MonkeyPatch) -> None:
    """勾选检测已移除：没有勾选反馈也不阻断后续点击和确认。"""
    stub = _PickerStub([["静听嘉音", "折枝剑歌"], ["河豚电音"]])
    _install_stub(monkeypatch, stub)
    stub.mark_selected = False
    session = FilterReplaySession()
    _run(session, stub, ("河豚电音", "静听嘉音", "折枝剑歌"))
    assert len(stub.clicks) == 4  # 三个目标和一次确认
    assert stub.selected == set() and not stub.popup_open
    assert len(session.routes) == 1


@pytest.mark.parametrize("state", [False, None])
def test_disabled_or_unknown(monkeypatch: pytest.MonkeyPatch, state: bool | None) -> None:
    """§13：套装不使用亮度门禁；确认按钮不可用或未知仍停止。"""
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    stub.enabled = state
    session = FilterReplaySession()
    with pytest.raises(gf.FilterClickError):
        _run(session, stub, ("河豚电音",))
    assert len(stub.clicks) == 1  # 套装已点，确认未发
    assert session.routes == {}


def test_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """R12：第一段成功后第二段身份不符 → 报错停止，第一个目标不重复点击、不继续确认。"""
    stub = _PickerStub([["静听嘉音"], ["折枝剑歌"]])
    _install_stub(monkeypatch, stub)
    monkeypatch.setattr(gf, "identity_matches", lambda _image, _identity: stub.index == 0)
    session = FilterReplaySession()
    with pytest.raises(gf.FilterClickError):
        _run(session, stub, ("静听嘉音", "折枝剑歌"))
    assert len(stub.clicks) == 1
    assert stub.popup_open is True
    assert session.routes == {}


def test_stop_after_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """R14：第一个点击后检查点抛 RunnerStopped → 原样传播，后续滚动/点击/确认为 0，不缓存。"""
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()

    def checkpoint() -> None:
        if len(stub.clicks) >= 1:
            raise RunnerStopped("F10 请求停止")

    with controlled_inputs(checkpoint), pytest.raises(RunnerStopped):
        _run(session, stub, ("河豚电音",))
    assert len(stub.clicks) == 1
    assert stub.wheels == []
    assert session.routes == {}
    assert session.last_metrics is not None and session.last_metrics["success"] is False


@pytest.mark.parametrize("mode", ["learn", "replay"])
def test_pause_resume(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """R13：第一个点击后 F9 暂停 → 恢复；暂停期间无输入、第一点击不重复、本段照常检查、最终成功。"""
    stub = _PickerStub([["静听嘉音", "折枝剑歌"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    names = ("静听嘉音", "折枝剑歌")
    if mode == "replay":
        _run(session, stub, names)  # 先学一条路径
        stub.reset_log()
    entered = threading.Event()
    state = {"pauses": 0}

    def checkpoint() -> None:
        if len(stub.clicks) != 1 or state["pauses"]:
            return
        state["pauses"] += 1
        before = (list(stub.clicks), list(stub.wheels))
        entered.set()
        assert entered.wait(5.0)
        assert (stub.clicks, stub.wheels) == (before[0], before[1])  # 暂停期间无输入

    with controlled_inputs(checkpoint):
        _run(session, stub, names)
    assert state["pauses"] == 1
    first = stub.name_boxes()["静听嘉音"]
    first_click = ((first[0] + first[2]) // 2, (first[1] + first[3]) // 2)
    assert stub.clicks.count(first_click) == 1  # 第一点击不重复
    assert len(stub.clicks) == 3  # 两个目标各一次 + 确认
    assert stub.wheels == []
    assert stub.popup_open is False
    assert session.last_metrics is not None and session.last_metrics["mode"] == mode


def test_empty_and_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """R15：空 sets 无任何弹窗输入；replay_session=None 走旧名称查找与确认，不留全局缓存。"""
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    empty_metrics = session.last_metrics
    _run(session, stub, ())
    assert stub.clicks == [] and stub.wheels == [] and stub.reads == []
    assert session.last_metrics is empty_metrics and session.routes == {}

    gf._select_sets_with_replay(stub.hwnd, ("河豚电音",), width=1920, height=1080, replay_session=None)
    layout = _layout()
    assert stub.reads[0] == gf.FULL_WINDOW  # 旧路径用整窗 OCR 找名称与确认
    assert layout.list_area not in stub.reads
    assert stub.span_names == []  # 不先生成字形指纹
    assert stub.popup_open is False
    stub.reset_log()
    gf._select_sets_with_replay(stub.hwnd, ("河豚电音",), width=1920, height=1080, replay_session=None)
    assert stub.reads[0] == gf.FULL_WINDOW and len(stub.clicks) == 2  # 名称 + 确认，仍走旧路径：无全局缓存


def _line(text: str, box: tuple[int, int, int, int]) -> OcrLine:
    return OcrLine(text=text, left=box[0], top=box[1], right=box[2], bottom=box[3], score=1.0)


def test_matching_ranks() -> None:
    """R17：严格按 §5.1.2 元组取值——精确＞包含＞相似；相似低于 0.75 不入围；不看行顺序。"""
    weak = _line("河豚大侠", (900, 500, 990, 530))  # 相似，ratio = 0.5 ＜ 0.75
    similar = _line("河豚电台", (900, 700, 990, 730))  # 相似且 ratio = 0.75 ≥ 0.75
    contains = _line("河豚电音 3", (900, 600, 990, 630))
    exact = _line("河豚电音", (900, 400, 990, 430))
    # 顺序打乱：弱匹配在前，精确在后；仍取精确
    winner = gf._match_targets([weak, contains, similar, exact], ["河豚电音"])
    assert winner is not None and winner["河豚电音"] is exact
    # 去精确后：包含 (2, 0.8) ＞ 相似 (1, 0.83)
    winner = gf._match_targets([similar, contains], ["河豚电音"])
    assert winner is not None and winner["河豚电音"] is contains
    # 只有相似：≥0.75 入围，<0.75 不入围
    winner = gf._match_targets([similar], ["河豚电音"])
    assert winner is not None and winner["河豚电音"] is similar
    assert gf._match_targets([weak], ["河豚电音"]) == {}


def test_matching_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """R17：同分或同框冲突在**本帧点击之前**失败，不按行序取第一项。"""
    box = (900, 400, 990, 430)
    assert gf._match_targets([_line("河豚电音", box), _line("河豚电音", (1200, 400, 1290, 430))], ["河豚电音"]) is None
    assert gf._match_targets([_line("河豚电音", box)], ["河豚电音", "河豚"]) is None
    stub = _PickerStub([["河豚电音", "河豚电音"]], boxes={"河豚电音": box})
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    with pytest.raises(gf.FilterClickError):
        _run(session, stub, ("河豚电音",))
    assert stub.clicks == []
    assert session.routes == {}


def test_identity_replay_no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """R18：回放零名称查找 OCR（列表/确认区一读就炸），不复验缓存文字身份。"""
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    _run(session, stub, ("河豚电音",))
    stub.reset_log()
    layout = _layout()
    real_lines = stub.lines_for

    def _read_list_fails(_image, area):
        if tuple(area) in (layout.list_area, layout.confirm_area):
            raise AssertionError("回放不应读列表/确认区做名称查找")
        return real_lines(tuple(area))

    monkeypatch.setattr(gf, "read_rect", _read_list_fails)
    _run(session, stub, ("河豚电音",))
    metrics = session.last_metrics
    assert metrics is not None and metrics["mode"] == "replay" and metrics["search_ocr_calls"] == 0
    assert len(session.routes) == 1
    # 缓存回放不调用文字身份检测
    stub.reset_log()
    stub.identity_ok = False
    _run(session, stub, ("河豚电音",))
    assert len(stub.clicks) == 2 and stub.wheels == []
    assert len(session.routes) == 1


def test_identity_no_file_and_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """R18：指纹只存在路径对象里（模块无文件写入），routes 清空后可释放。"""
    source = (BASE / "src" / "zzz_ddtool" / "filter_replay.py").read_text(encoding="utf-8")
    for marker in ("open(", "write_text", "write_bytes", "json.dump", "pickle"):
        assert marker not in source
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    _run(session, stub, ("河豚电音",))
    route = next(iter(session.routes.values()))
    ref = weakref.ref(route.stages[0].clicks[0].identity)
    assert ref() is not None
    del route
    session.routes.clear()
    gc.collect()
    assert ref() is None


@pytest.mark.parametrize(
    "names",
    [("",), ("   ",), ("河豚电音", "河豚电音"), ("河豚电音", " 河豚电音 ")],
)
def test_illegal_names_no_input(monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]) -> None:
    """R19：非法套装名在第一条游戏输入之前失败，不新增主词条校验。"""
    stub = _PickerStub([[]])
    _install_stub(monkeypatch, stub)
    monkeypatch.setattr(gf, "_pick_mainstats", lambda *a, **k: pytest.fail("不应进入主词条校验"))
    with pytest.raises(gf.FilterClickError):
        gf.apply_game_filter(1, 1920, 1080, slots=(1,), sets=names, mainstats=("攻击力",))
    assert stub.clicks == [] and stub.wheels == [] and stub.keys == []


def test_evidence_sink_restoration(monkeypatch: pytest.MonkeyPatch) -> None:
    """R26：嵌套 sink 只到内层；内层退出（正常/两类异常）后只回到外层；退出外层不再取证且不残留回调。"""
    assert gf._evidence_enabled() is False
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    outer: list = []
    inner: list = []

    with gf._capture_filter_evidence(outer.append):
        with gf._capture_filter_evidence(inner.append):
            _run(session, stub, ("河豚电音",))
            assert len(inner) == 1 and outer == []  # 内层事件仅到 B
        stub.reset_log()
        _run(session, stub, ("河豚电音",))
        assert len(inner) == 1 and len(outer) == 1  # 内层退出后只到 A

        stub.reset_log()
        def fail_input():
            raise gf.FilterClickError("输入失败")

        with gf._capture_filter_evidence(inner.append), controlled_inputs(fail_input), pytest.raises(gf.FilterClickError):
            _run(session, stub, ("河豚电音",))
        assert len(inner) == 1 and len(outer) == 1  # 异常路径同样只回落到 A

        def _stop() -> None:
            raise RunnerStopped("F10 请求停止")

        stub.identity_ok = True
        stub.reset_log()
        with gf._capture_filter_evidence(inner.append), controlled_inputs(_stop), pytest.raises(RunnerStopped):
            _run(session, stub, ("河豚电音",))
        assert len(inner) == 1 and len(outer) == 1

    after = len(outer)
    stub.reset_log()
    _run(session, stub, ("河豚电音",))
    assert len(outer) == after  # 退出外层后不再取证
    assert gf._evidence_enabled() is False  # 不残留回调


def test_evidence_contract_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """R23（离线部分）：证据只多出每段一帧，targets/boxes 对应实际目标；回放 OCR=0、输入不变。"""
    stub = _PickerStub([[], ["静听嘉音", "折枝剑歌"], ["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    names = ("河豚电音", "静听嘉音", "折枝剑歌")
    image = QImage(1920, 1080, QImage.Format_RGB32)
    image.fill(0)
    grabs = {"n": 0}

    def _grab():
        grabs["n"] += 1
        return image, stub.hwnd

    monkeypatch.setattr(gf, "_grab", _grab)
    _run(session, stub, names)
    first_clicks, first_wheels = list(stub.clicks), list(stub.wheels)
    stub.reset_log()
    before = grabs["n"]
    _run(session, stub, names)  # 回放（未启用证据）
    baseline = grabs["n"] - before
    stub.reset_log()
    before = grabs["n"]
    events: list = []
    with gf._capture_filter_evidence(events.append):
        _run(session, stub, names)
    assert grabs["n"] - before == baseline + 2  # 仅启用取证时，每个已发送段另存一帧；不判断勾选。
    assert len(events) == 2  # 两个选择段各一帧
    assert [event.event for event in events] == ["set_stage_selected", "set_stage_selected"]
    assert [event.stage_index for event in events] == [1, 2]
    assert [list(event.targets) for event in events] == [["静听嘉音", "折枝剑歌"], ["河豚电音"]]
    assert all(event.image.width() == 1920 for event in events)
    for event in events:  # 证据里的框中心就是实际点击过的目标
        centers = [((b[0] + b[2]) // 2, (b[1] + b[3]) // 2) for b in event.boxes]
        assert centers and all(center in first_clicks for center in centers)
    metrics = session.last_metrics
    assert metrics is not None and metrics["mode"] == "replay" and metrics["search_ocr_calls"] == 0
    assert stub.clicks == first_clicks and stub.wheels == first_wheels


def test_live_input_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """§8.4/R27（离线部分）：痕迹里的角色、目标、段号、坐标与指标一致，begin/end 成对完成。"""
    stub = _PickerStub([["河豚电音"]], hwnd=1)
    _install_stub(monkeypatch, stub, passthrough=True)
    monkeypatch.setattr(wininput, "focus_window", lambda _hwnd: None)
    monkeypatch.setattr(wininput, "_send", lambda _inputs: None)
    monkeypatch.setattr(wininput, "pause", lambda *a, **k: None)
    monkeypatch.setattr(wininput, "client_to_screen", lambda _hwnd, x, y: (x, y))
    monkeypatch.setattr(wininput, "randint", lambda _lo, _hi: 0)
    windows = Mock()
    windows.MapVirtualKeyW.return_value = 1  # ctypes 需要真整数
    monkeypatch.setattr(wininput, "user32", windows)
    monkeypatch.setattr(wininput, "kernel32", Mock())
    session = FilterReplaySession()
    events: list = []
    with wininput.capture_inputs(events.append):
        gf.apply_game_filter(
            1, 1920, 1080, slots=(1,), sets=("河豚电音",), mainstats=(), replay_session=session
        )
    begins = [event for event in events if event["event"] == "begin"]
    assert [event["role"] for event in begins] == [
        "open_panel", "reset", "slot", "open_sets", "set_option", "confirm_sets", "close_panel",
    ]
    assert all(event["status"] == "completed" for event in events if event["event"] == "end")
    ends = [event for event in events if event["event"] == "end"]
    assert len(ends) == len(begins)
    assert sorted(event["call_id"] for event in begins) == sorted(event["call_id"] for event in ends)
    by_role = {event["role"]: event for event in begins}
    assert by_role["slot"]["target"] == 1 and by_role["slot"]["stage_index"] is None
    option = by_role["set_option"]
    box = stub.name_boxes()["河豚电音"]
    assert option["target"] == "河豚电音" and option["stage_index"] == 1 and option["selected"] is True
    assert (option["x"], option["y"]) == ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    assert by_role["close_panel"]["vk"] == 27
    assert by_role["reset"]["target"] is None and by_role["confirm_sets"]["target"] is None
    metrics = session.last_metrics
    assert metrics is not None
    assert metrics["selection_clicks"] == 1 and metrics["wheel_events"] == 0

    # 多轮滚动：每条 wheel_client 恰记一对 begin/end（不与 set_option 重复计数）
    stub2 = _PickerStub([["静听嘉音"], ["折枝剑歌"], ["河豚电音"]])
    _install_stub(monkeypatch, stub2, passthrough=True)
    events2: list = []
    with wininput.capture_inputs(events2.append):
        gf._select_sets_with_replay(
            stub2.hwnd, ("河豚电音", "静听嘉音", "折枝剑歌"),
            width=1920, height=1080, replay_session=None,
        )

    def pairs(rows: list, role: str) -> int:
        begins = [r for r in rows if r.get("role") == role and r["event"] == "begin"]
        ends = [r for r in rows if r.get("role") == role and r["event"] == "end"]
        assert len(begins) == len(ends)
        assert sorted(r["call_id"] for r in begins) == sorted(r["call_id"] for r in ends)
        return len(ends)

    assert pairs(events2, "set_scroll") == 2  # 两次滚动各恰一对，不是一条批次记录
    assert pairs(events2, "set_option") == 3
    assert all(
        r["delta_units"] == -3 for r in events2 if r.get("role") == "set_scroll" and r["event"] == "end"
    )
    assert stub2.popup_open is False


def test_legacy_metrics_and_stage_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """R28（记账部分）：legacy 路径也按 §5.4/§8.4 记账——每轮查找名称的 OCR、每次选项点击、
    每次路径滚动各计一次，且 set_option 记录带从 1 起的 stage_index（与套装证据段号一致）。"""
    stub = _PickerStub([["静听嘉音"], ["河豚电音"]])
    _install_stub(monkeypatch, stub, passthrough=True)
    monkeypatch.setattr(wininput, "focus_window", lambda _hwnd: None)
    monkeypatch.setattr(wininput, "_send", lambda _inputs: None)
    monkeypatch.setattr(wininput, "pause", lambda *a, **k: None)
    monkeypatch.setattr(wininput, "client_to_screen", lambda _hwnd, x, y: (x, y))
    monkeypatch.setattr(wininput, "randint", lambda _lo, _hi: 0)
    windows = Mock()
    windows.MapVirtualKeyW.return_value = 1
    monkeypatch.setattr(wininput, "user32", windows)
    monkeypatch.setattr(wininput, "kernel32", Mock())
    captured: list[dict] = []
    monkeypatch.setattr(gf, "_log_picker_metrics", lambda metrics: captured.append(metrics.payload()))
    events: list = []
    with wininput.capture_inputs(events.append):
        gf._select_sets_with_replay(
            stub.hwnd, ("静听嘉音", "河豚电音"), width=1920, height=1080, replay_session=None
        )
    assert captured and captured[-1]["mode"] == "legacy"
    options = [e for e in events if e.get("role") == "set_option" and e["event"] == "end"]
    scrolls = [e for e in events if e.get("role") == "set_scroll" and e["event"] == "end"]
    assert len(options) == 2 and len(scrolls) == 1
    assert captured[-1]["selection_clicks"] == len(options)
    assert captured[-1]["wheel_events"] == len(scrolls)
    assert captured[-1]["search_ocr_calls"] == 3  # 两轮查找名称 + 一次确认按钮查找
    assert [e["stage_index"] for e in options] == [1, 2]
    assert all(e["selected"] is True for e in options)
    assert stub.popup_open is False


def test_unusable_frame_fails_without_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """§3.3 / R21：本帧指纹不可用 → 整段零点击失败，不保留待处理、不滚动重试。"""
    stub = _PickerStub([["静听嘉音"], ["静听嘉音", "河豚电音"]])
    _install_stub(monkeypatch, stub)
    stub.identity_none_frames = {0}  # 首屏那行未完整露出，指纹不可用
    session = FilterReplaySession()
    with pytest.raises(gf.FilterClickError, match="文字指纹生成失败"):
        _run(session, stub, ("河豚电音", "静听嘉音"))
    assert stub.clicks == [] and stub.wheels == []  # 任何点击/滚动之前停止
    assert session.routes == {}


def test_span_failure_stops_before_any_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """R21：名称框隔离失败 → 整段零选项点击失败，即使同帧还有别的可用目标。"""
    stub = _PickerStub([["静听嘉音", "河豚电音"]])
    _install_stub(monkeypatch, stub)
    original = gf.read_name_bands

    def batch(image, bands):
        return [replace(line, text="河豚电台") if line.text == "河豚电音" else line
                for line in original(image, bands)]

    monkeypatch.setattr(gf, "read_name_bands", batch)
    session = FilterReplaySession()
    with pytest.raises(gf.FilterClickError, match="无法隔离精确文字框"):
        _run(session, stub, ("河豚电音", "静听嘉音"))
    assert stub.clicks == []  # 同帧另一个目标也不点
    assert session.routes == {}


def test_approximate_name_retries_on_fresh_frame_before_clicking(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户决定（2026-10-11）：近似命中不点击，重拍重读，认对一次即用。"""
    stub = _PickerStub([["静听嘉音", "河豚电音"]])
    _install_stub(monkeypatch, stub)
    original = gf.read_name_bands
    calls = {"n": 0}

    def batch(image, bands):
        calls["n"] += 1
        lines = original(image, bands)
        if calls["n"] == 1:  # 首次近似命中：不点击，换新帧重读
            return [replace(line, text="河豚电台") if line.text == "河豚电音" else line
                    for line in lines]
        return lines

    monkeypatch.setattr(gf, "read_name_bands", batch)
    session = FilterReplaySession()
    _run(session, stub, ("河豚电音", "静听嘉音"))

    assert calls["n"] == 2  # 第一次近似命中后重读一次即认对
    assert len(stub.clicks) == 3  # 两个目标各一次 + 确认
    assert stub.selected == {"河豚电音", "静听嘉音"}
    assert stub.popup_open is False
    assert session.last_metrics is not None and session.last_metrics["success"] is True


def test_approximate_name_retries_are_bounded_and_never_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """重读有上限：始终认不对时按 R21 整段零点击停止，不无限重拍。"""
    stub = _PickerStub([["静听嘉音", "河豚电音"]])
    _install_stub(monkeypatch, stub)
    original = gf.read_name_bands
    calls = {"n": 0}

    def batch(image, bands):
        calls["n"] += 1
        return [replace(line, text="河豚电台") if line.text == "河豚电音" else line
                for line in original(image, bands)]

    monkeypatch.setattr(gf, "read_name_bands", batch)
    session = FilterReplaySession()
    with pytest.raises(gf.FilterClickError, match="无法隔离精确文字框"):
        _run(session, stub, ("河豚电音", "静听嘉音"))

    assert calls["n"] == gf._PICKER_NAME_READ_ATTEMPTS
    assert stub.clicks == [] and stub.wheels == []
    assert session.routes == {}


def test_picker_template_asset_failure_falls_back_to_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """模板资产损坏：只点名告警一次并回落名称带 OCR，点选流程照常完成、不点击错目标。"""
    stub = _PickerStub([["静听嘉音", "河豚电音"]])
    _install_stub(monkeypatch, stub)
    monkeypatch.setattr(gf, "_picker_templates_disabled", False)
    warnings: list = []
    monkeypatch.setattr(
        gf.get_logger("zzz_ddtool.gamefilter"), "warning",
        lambda *args, **kwargs: warnings.append(args),
    )

    def broken(_image, _band):
        raise gf.picker_names.PickerAssetError("boom")

    monkeypatch.setattr(gf.picker_names, "classify", broken)
    session = FilterReplaySession()
    _run(session, stub, ("河豚电音", "静听嘉音"))

    assert stub.selected == {"河豚电音", "静听嘉音"}
    assert gf._picker_templates_disabled is True
    assert warnings


# ---------------------------------------------------------------- R25


def _switches(engine) -> tuple[object, object]:
    get = lambda name: getattr(engine, name, None)  # noqa: E731
    return get("return_word_box"), get("return_single_char_box")


def _probe_line() -> OcrLine:
    return OcrLine(text="等级15", left=120, top=330, right=220, bottom=380, score=1.0)


def test_ocr_state_restoration(monkeypatch: pytest.MonkeyPatch) -> None:
    """R25：成功 / None / 异常三条路径后字符框开关都恢复；普通读取不被污染。"""
    image = _image("tests/fixtures/驱动盘筛选/套装弹窗-选择段1-前.png")
    engine = ocr._get_engine()
    before = _switches(engine)

    # 1) 成功路径
    span = read_name_span(image, _probe_line_for(image), name="谶羽之誓")
    assert span is not None and span.text == "谶羽之誓"
    assert _switches(engine) == before, "成功路径后引擎开关未恢复"

    # 2) 返回 None 的路径（候选行里没有目标名称）
    assert read_name_span(image, OcrLine(text="荆棘玫瑰", left=862, top=489, right=940, bottom=515, score=1.0), name="谶羽之誓") is None
    assert _switches(engine) == before, "None 路径后引擎开关未恢复"

    # 3) 后端抛异常：异常原样抛出，开关照旧恢复
    class _BoomEngine:
        def __call__(self, *args, **kwargs):
            raise RuntimeError("backend down")

        # 开关属性复制自真实引擎，便于断言恢复行为
        return_word_box = before[0]
        return_single_char_box = before[1]

    boom = _BoomEngine()
    monkeypatch.setattr(ocr, "_get_engine", lambda: boom)
    with pytest.raises(RuntimeError, match="backend down"):
        read_name_span(image, _probe_line_for(image), name="谶羽之誓")
    monkeypatch.undo()
    assert _switches(engine) == before, "异常路径后引擎开关未恢复"

    # 4) 普通 read_rect 交错执行不受影响
    lines = read_rect(image, (760, 290, 1160, 340))
    assert lines, "普通 read_rect 在校验后应仍能工作"
    assert _switches(engine) == before, "普通读取后引擎开关发生变化"


def _probe_line_for(image: QImage) -> OcrLine:
    for line in read_rect(image, (760, 290, 1160, 340)) or []:
        if "谶羽之誓" in line.text.replace(" ", ""):
            return line
    for line in read_rect(image, (0, 0, 1920, 1080)):
        if "谶羽之誓" in line.text.replace(" ", ""):
            return line
    raise AssertionError("样本图里找不到 谶羽之誓 的候选行")


# ---------------------------------------------------------------- 记录面（R28）与场景收尾（R29/R30）


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("河豚电音", "河豚电音"),
        ("河豚电音 3", "河豚电音"),
        ("河豚电音×9", "河豚电音"),
        ("河豚电音特别版", "河豚电音特别版"),
    ],
    ids=["exact", "suffix_space", "suffix_times", "extra_text"],
)
def test_legacy_target_normalization(raw: str, expected: str) -> None:
    """R28（字符串面）：归一化相等记归一化原文；合法件数后缀去后缀；其他保留原文。"""
    assert gf._legacy_option_target(raw, "河豚电音") == expected


def test_legacy_target_normalization_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """R28（记录面）：legacy 的 set_option 记录与证据 targets 复用同一字符串；
    去后缀只改记录文本，不改点击中心、不额外 OCR。"""
    stub = _PickerStub([["河豚电音 3"]])
    _install_stub(monkeypatch, stub, passthrough=True)
    monkeypatch.setattr(wininput, "focus_window", lambda _hwnd: None)
    monkeypatch.setattr(wininput, "_send", lambda _inputs: None)
    monkeypatch.setattr(wininput, "pause", lambda *a, **k: None)
    monkeypatch.setattr(wininput, "client_to_screen", lambda _hwnd, x, y: (x, y))
    monkeypatch.setattr(wininput, "randint", lambda _lo, _hi: 0)
    windows = Mock()
    windows.MapVirtualKeyW.return_value = 1
    monkeypatch.setattr(wininput, "user32", windows)
    monkeypatch.setattr(wininput, "kernel32", Mock())
    rows: list = []
    evidence: list = []
    with wininput.capture_inputs(rows.append), gf._capture_filter_evidence(evidence.append):
        gf._select_sets_with_replay(stub.hwnd, ("河豚电音",), width=1920, height=1080, replay_session=None)
    option_ends = [r for r in rows if r.get("role") == "set_option" and r["event"] == "end"]
    assert [r["target"] for r in option_ends] == ["河豚电音"]  # 去合法件数后缀
    assert not stub.span_names  # legacy 不做字符框隔离、不额外 OCR
    frames = [e for e in evidence if e.event == "set_stage_selected"]
    assert len(frames) == 1 and frames[0].targets == ("河豚电音",)  # 记录与证据同字符串
    box = stub.name_boxes()["河豚电音 3"]
    assert frames[0].boxes == (tuple(box),)
    assert (option_ends[0]["x"], option_ends[0]["y"]) == ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)


# ---------------------------------------------------------------- 点击前状态（F01）


@pytest.mark.parametrize("replay", [False, True], ids=["learn", "replay"])
def test_picker_batch_does_not_capture_between_clicks(monkeypatch, replay):
    """一帧准备三个目标；中间点击动画不参与判读，整批结束后才定位确认按钮。"""
    names = ("河豚电音", "静听嘉音", "折枝剑歌")
    stub = _PickerStub([names])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    if replay:
        with controlled_inputs(lambda: 0):
            _run(session, stub, names)
        stub.reset_log()
    original_grab = gf._grab
    capture_positions = []

    def grab():
        capture_positions.append(len(stub.clicks))
        assert len(stub.clicks) not in (1, 2), "批内不得截图判读"
        return original_grab()

    monkeypatch.setattr(gf, "_grab", grab)
    with controlled_inputs(lambda: 0):
        _run(session, stub, names)
    assert [stub.name_at(x, y) for x, y in stub.clicks[:-1]] == list(names)
    assert stub.selected == set(names)
    assert (4 if replay else 3) in capture_positions  # 回放只在确认后抓图
    assert session.last_metrics["success"] is True


def test_picker_batch_rejects_bad_last_target_before_first_click(monkeypatch):
    """末项名称身份不符时整批不发送输入，不能先点前两项再发现失败。"""
    names = ("河豚电音", "静听嘉音", "折枝剑歌")
    stub = _PickerStub([names])
    _install_stub(monkeypatch, stub)
    last_box = stub.name_boxes()[names[-1]]
    monkeypatch.setattr(gf, "identity_matches", lambda _image, identity:
                        (identity.left, identity.top, identity.right, identity.bottom) != last_box)
    session = FilterReplaySession()
    with controlled_inputs(lambda: 0), pytest.raises(gf.FilterClickError, match="输入前状态不符"):
        _run(session, stub, names)
    assert stub.clicks == []
    assert session.routes == {}


@pytest.mark.parametrize("changed", [False, True], ids=["resume", "page_changed"])
def test_picker_batch_resume_checks_only_remaining_targets(monkeypatch, changed):
    names = ("河豚电音", "静听嘉音", "折枝剑歌")
    stub = _PickerStub([names])
    _install_stub(monkeypatch, stub)
    original_pre = gf._pre_click_state
    checked = []

    def pre(image, ref, identity):
        name = stub.name_at((ref.left + ref.right) // 2, (ref.top + ref.bottom) // 2)
        checked.append((len(stub.clicks), name))
        return original_pre(image, ref, identity)

    def checkpoint():
        if stub.clicks and changed:
            stub.identity_ok = False
        return 1 if stub.clicks else 0  # 模拟第一项之后暂停恢复

    monkeypatch.setattr(gf, "_pre_click_state", pre)
    session = FilterReplaySession()
    with controlled_inputs(checkpoint):
        if changed:
            with pytest.raises(gf.FilterClickError, match="输入前状态不符"):
                _run(session, stub, names)
        else:
            _run(session, stub, names)
    assert checked == [(0, name) for name in names] + [(1, name) for name in names[1:]]
    assert len(stub.clicks) == (1 if changed else 4)
    if changed:
        assert session.routes == {}
    else:
        assert stub.selected == set(names)


def test_pre_click_identity_unknown_ignores_brightness(monkeypatch):
    """§13：亮度不改变身份未知的三帧预算。"""
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    monkeypatch.setattr(gf, "identity_matches", lambda _image, _identity: None)
    monkeypatch.setattr(gf, "_picker_enabled", lambda _image, _click: False)
    session = FilterReplaySession()
    with pytest.raises(gf.FilterClickError, match="输入前状态不符"):
        _run(session, stub, ("河豚电音",))
    assert stub.clicks == []
    assert session.last_metrics is not None and session.last_metrics["verification_frames"] == 4


# ---------------------------------------------------------------- 审查修正补验收（F02/F03/F04/F05）


def test_pause_changed_page_before_resume_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """F02/§5.3：暂停检查点返回后页面已变 → 不发送该次点击（追加输入 0），失败且不发布路径。"""
    stub = _PickerStub([["静听嘉音", "河豚电音"]])
    _install_stub(monkeypatch, stub)
    def hook() -> None:
        if len(stub.clicks) == 1:
            stub.identity_ok = False

    session = FilterReplaySession()
    with controlled_inputs(hook), pytest.raises(gf.FilterClickError, match="输入前状态不符"):
        _run(session, stub, ("静听嘉音", "河豚电音"))
    assert len(stub.clicks) == 1  # 页面变化后的那一次点击没有发出
    assert session.routes == {}


def test_pause_changed_window_during_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """F02/§5.3：回放中暂停期间窗口绑定变化 → 旧坐标一次都不发，路径移除。"""
    stub = _PickerStub([["静听嘉音", "河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    _run(session, stub, ("静听嘉音", "河豚电音"))  # 先学习缓存
    assert len(session.routes) == 1
    stub.reset_log()

    def hook() -> None:
        monkeypatch.setattr(gf, "_window_dpi", lambda _hwnd: 144)

    with controlled_inputs(hook), pytest.raises(gf.FilterClickError, match="窗口绑定变化"):
        _run(session, stub, ("静听嘉音", "河豚电音"))
    assert stub.clicks == []
    assert session.routes == {}


def test_panel_present_pixel_judgement() -> None:
    """F03：面板存在判据是像素判据（零 OCR）——实拍面板帧 True、仓库帧 False。"""
    panel = _image("tests/fixtures/驱动盘筛选/套装弹窗-确认后.png")
    closed = _image("tests/fixtures/驱动盘筛选/筛选面板-关闭.png")
    assert (panel.width(), panel.height()) == (1920, 1080)
    assert (closed.width(), closed.height()) == (1920, 1080)
    assert gf._panel_present(panel) is True
    assert gf._panel_present(closed) is False


def test_replay_scope_no_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    """F03/R18：回放范围内所有 read_rect / read_name_span 调用即失败；面板检查同样不 OCR。"""
    real_panel_visible = gf._panel_visible
    stub = _PickerStub([["静听嘉音", "河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    _run(session, stub, ("静听嘉音", "河豚电音"))
    assert len(session.routes) == 1
    stub.reset_log()

    frame = QImage(1920, 1080, QImage.Format.Format_RGB32)
    frame.fill(0)
    left, top, right, bottom = gf.FILTER_RESET_AREA
    for y in range(top, bottom, 2):
        for x in range(left, right, 2):
            frame.setPixel(x, y, 0xFFFFFFFF)
    monkeypatch.setattr(gf, "_grab", lambda: (frame, stub.hwnd))
    monkeypatch.setattr(gf, "_panel_visible", real_panel_visible)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("回放范围内不应出现 OCR 调用")

    monkeypatch.setattr(gf, "read_rect", forbidden)
    monkeypatch.setattr(gf, "read_name_span", forbidden)
    _run(session, stub, ("静听嘉音", "河豚电音"))  # 回放整段：零 OCR


def test_evidence_context_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """F04/R26：取证回调按执行上下文隔离——其他线程不收事件，各上下文交错也不串。"""
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    outer: list = []

    def bare_emit() -> None:
        gf._emit_evidence("set_stage_selected", 1, ("b",), (), image)

    with gf._capture_filter_evidence(outer.append):
        emit_thread = threading.Thread(target=bare_emit)
        emit_thread.start()
        emit_thread.join()
        assert outer == []  # 其他线程的 emit 不进本上下文的 sink
        gf._emit_evidence("set_stage_selected", 1, ("a",), (), image)
        assert len(outer) == 1

    other: list[list] = [[], []]
    seen: list[int] = []

    def worker(sink: list) -> None:
        with gf._capture_filter_evidence(sink.append):
            gf._emit_evidence("set_stage_selected", 1, ("c",), (), image)
            seen.append(len(sink))

    threads = [threading.Thread(target=worker, args=(sink,)) for sink in other]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert seen == [1, 1]  # 两个独立上下文各收各的，互不串
    assert [len(sink) for sink in other] == [1, 1]
    assert gf._evidence_enabled() is False


@pytest.mark.parametrize(
    ("sequence", "expect_frames", "ok"),
    [
        ([None, True], None, True),        # 顶部瞬时未知 → 补拍后成功
        ([None, None, None], 3, False),    # 三次未知 → 失败
        ([False], 1, False),               # 明确不符 → 立即失败
    ],
)
def test_origin_observation_budget(sequence, expect_frames, ok, monkeypatch):
    """F05/§4.3：起始顶部观察接入三帧预算——未知补拍、耗尽失败、明确不符立即失败。"""
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    values = list(sequence)
    monkeypatch.setattr(gf, "_picker_at_top", lambda _image, _layout: values.pop(0) if values else True)
    session = FilterReplaySession()
    if ok:
        _run(session, stub, ("河豚电音",))
        assert session.last_metrics is not None and session.last_metrics["success"] is True
    else:
        with pytest.raises(gf.FilterClickError, match="起始位置检查失败"):
            _run(session, stub, ("河豚电音",))
        assert stub.clicks == []
        assert session.last_metrics is not None
        assert session.last_metrics["verification_frames"] == expect_frames


def test_stage_layout_observation_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """F05/§5.2：回放不调用段布局判读。"""
    stub = _PickerStub([["静听嘉音", "河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    _run(session, stub, ("静听嘉音", "河豚电音"))
    stub.reset_log()
    values = [None, True]
    monkeypatch.setattr(
        gf, "_picker_stage_matches", lambda _image, _layout: values.pop(0) if values else True
    )
    _run(session, stub, ("静听嘉音", "河豚电音"))
    assert values == [None, True]
    assert stub.wheels == []
    assert session.last_metrics is not None and session.last_metrics["mode"] == "replay"
    assert session.last_metrics["wheel_events"] == 0  # 单屏路线：无滚动事件


def test_confirm_observation_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """F05/§4.2：确认按钮可用观察接入三帧预算（None→True 补拍，帧数正确）。"""
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    # 选项 A 的底层检查点 → 确认检查点（None → 补拍 True）；无上层重复扫描。
    values = [None, True]
    monkeypatch.setattr(gf, "_picker_enabled", lambda _image, _click: values.pop(0) if values else True)
    session = FilterReplaySession()
    _run(session, stub, ("河豚电音",))
    # 起始 1 + 点击前 1 + 确认定位 1 + 确认观察 2 + 关闭确认 1
    assert session.last_metrics is not None
    assert session.last_metrics["verification_frames"] == 6
    assert session.last_metrics["success"] is True


@pytest.mark.parametrize("mode", ["learn", "replay"])
@pytest.mark.parametrize("action", ["option", "confirm", "scroll", "wheel_top"])
@pytest.mark.parametrize("boundary", ["entry", "focus"])
@pytest.mark.parametrize("known_generation", [False, True])
def test_changed_page_at_native_checkpoint(monkeypatch, mode, action, boundary, known_generation):
    """C01：走真实 click/wheel/focus 检查点，变化后追加原生输入为零；不能假成功。"""
    screens = [[], ["河豚电音"]] if action == "scroll" else [["河豚电音"]]
    stub = _PickerStub(screens)
    _install_stub(monkeypatch, stub, passthrough=True)
    windows = Mock()
    monkeypatch.setattr(wininput, "user32", windows)
    monkeypatch.setattr(wininput, "kernel32", Mock())
    monkeypatch.setattr(wininput, "client_to_screen", lambda hwnd, x, y: (x, y))
    monkeypatch.setattr(wininput, "randint", lambda lo, hi: 0)
    monkeypatch.setattr(wininput, "is_foreground", lambda hwnd: True)
    monkeypatch.setattr(wininput, "pause", lambda *args: None)
    if action == "wheel_top":
        layout = replace(_layout(), origin_mode="wheel_top", top_wheel_events=2, top_delta_units=3)
        monkeypatch.setattr(gf, "_calibrated_layout", lambda width, height: layout)
    session = FilterReplaySession()
    if mode == "replay":
        _run(session, stub, ("河豚电音",))
        stub.reset_log()
        windows.reset_mock()
    state = {"active": False, "changed": False, "sent_before": None}
    role = {"option": "set_option", "confirm": "confirm_sets"}.get(action, "set_scroll")

    def record(event):
        if event["role"] == role:
            state["active"] = event["event"] == "begin"

    def checkpoint():
        context = wininput._input_context.get()
        if state["changed"] or context is None or context.role != role:
            return
        if boundary == "focus" and not state["active"]:
            return
        # 此处模拟检查点阻塞后用户改变页面，再恢复。
        state["changed"] = True
        state["sent_before"] = len(windows.mouse_event.mock_calls)
        if mode == "replay":
            monkeypatch.setattr(gf, "_window_dpi", lambda _hwnd: 144)
        else:
            stub.identity_ok = False
            monkeypatch.setattr(gf, "_picker_stage_matches", lambda image, layout: False)

    def with_generation():
        checkpoint()
        return (2 if state["changed"] else 0) if known_generation else None

    with wininput.capture_inputs(record), controlled_inputs(with_generation), pytest.raises(gf.FilterClickError):
        _run(session, stub, ("河豚电音",))
    assert state["changed"]
    assert len(windows.mouse_event.mock_calls) == state["sent_before"]
    assert len(stub.clicks) == (1 if action == "confirm" else 0)
    assert stub.wheels == []
    assert session.routes == {}
    assert session.last_metrics["success"] is False


@pytest.mark.parametrize("sequence,ok,frames", [([None, True], True, 2), ([None]*3, False, 3), ([False], False, 1)])
def test_real_stage_unknown_observation(monkeypatch, sequence, ok, frames):
    """C02：使用真实段判读，弹窗未知保留至外层三帧观察，而不是直接 False。"""
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFFFF)
    states = iter(sequence)
    monkeypatch.setattr(gf, "_picker_popup_open", lambda image: next(states))
    monkeypatch.setattr(gf, "_grab", lambda: (image, 100))
    monkeypatch.setattr(gf.time, "sleep", lambda seconds: None)
    metrics = gf._PickerMetrics("replay")
    result = gf._observe(metrics, lambda image: gf._picker_stage_matches(image, _layout()))
    assert (result is True) is ok
    assert metrics.verification_frames == frames


def test_real_stage_mismatch_precedes_unknown(monkeypatch):
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    monkeypatch.setattr(gf, "_picker_popup_open", lambda image: None)
    assert gf._picker_stage_matches(image, _layout()) is False


@pytest.mark.parametrize("exists", [False, True])
def test_reference_image_missing_or_corrupt_fails(tmp_path, monkeypatch, exists):
    monkeypatch.setitem(globals(), "BASE", tmp_path)
    relative = "broken-image.png"
    if exists:
        (tmp_path / relative).write_bytes(b"not a PNG")
    with pytest.raises(AssertionError):
        _image(relative)
    _images.pop(relative, None)


@pytest.mark.parametrize("mode", ["learn", "replay"])
@pytest.mark.parametrize("field", ["hwnd", "pid", "size", "dpi"])
def test_environment_changed_at_input_checkpoint(monkeypatch, mode, field):
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    if mode == "replay":
        _run(session, stub, ("河豚电音",))
        stub.reset_log()

    def checkpoint():
        if field == "hwnd":
            monkeypatch.setattr(gf, "find_game_hwnd", lambda: stub.hwnd + 1)
            original = gf._grab
            monkeypatch.setattr(gf, "_grab", lambda: (original()[0], stub.hwnd + 1))
        elif field == "pid":
            monkeypatch.setattr(gf, "window_pid", lambda hwnd: 201)
        elif field == "dpi":
            monkeypatch.setattr(gf, "_window_dpi", lambda hwnd: 120)
        else:
            monkeypatch.setattr(gf, "client_size", lambda hwnd: (1280, 720))
            image = QImage(1280, 720, QImage.Format.Format_RGB32)
            monkeypatch.setattr(gf, "_grab", lambda: (image, stub.hwnd))

    with controlled_inputs(checkpoint), pytest.raises(gf.FilterClickError):
        _run(session, stub, ("河豚电音",))
    assert stub.clicks == [] and stub.wheels == []
    assert session.routes == {} and session.last_metrics["success"] is False


@pytest.mark.parametrize("mode", ["learn", "replay"])
@pytest.mark.parametrize("changed", [False, True])
def test_blocked_focus_checkpoint_resume(monkeypatch, mode, changed):
    """检查点实际阻塞时无原生输入；恢复不变则继续，页面变化则停止。"""
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub, passthrough=True)
    windows = Mock()
    monkeypatch.setattr(wininput, "user32", windows)
    monkeypatch.setattr(wininput, "kernel32", Mock())
    monkeypatch.setattr(wininput, "client_to_screen", lambda hwnd, x, y: (x, y))
    monkeypatch.setattr(wininput, "is_foreground", lambda hwnd: True)
    monkeypatch.setattr(wininput, "pause", lambda *args: None)
    session = FilterReplaySession()
    if mode == "replay":
        _run(session, stub, ("河豚电音",))
        stub.reset_log()
        windows.reset_mock()
    entered, resume = threading.Event(), threading.Event()
    state = {"active": False}
    errors = []

    def record(event):
        if event["role"] == "set_option":
            state["active"] = event["event"] == "begin"

    def checkpoint():
        if state["active"] and not entered.is_set():
            entered.set()
            assert resume.wait(2), "测试未恢复暂停"

    def worker():
        try:
            with wininput.capture_inputs(record), controlled_inputs(checkpoint):
                _run(session, stub, ("河豚电音",))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    try:
        thread.start()
        assert entered.wait(2)
        assert windows.mouse_event.call_count == 0
        if changed:
            if mode == "replay":
                monkeypatch.setattr(gf, "_window_dpi", lambda _hwnd: 144)
            else:
                stub.identity_ok = False
        resume.set()
        thread.join(2)
        assert not thread.is_alive()
    finally:
        resume.set()
        thread.join(2)
    if changed:
        assert len(errors) == 1 and isinstance(errors[0], gf.FilterClickError)
        assert windows.mouse_event.call_count == 0
        assert stub.clicks == [] and session.routes == {}
    else:
        assert errors == []
        assert len(stub.clicks) == 2  # 一次选项、一次确认；恢复不重复已发输入。
        assert len(session.routes) == 1 and session.last_metrics["success"] is True


@pytest.mark.parametrize("name", ["混沌爵士", "原始朋克", "啄木鸟电音", "拂晓生花", "如影相随", "法厄同之歌"])
def test_zero_count_or_dark_art_pre_click_real_frame(name, monkeypatch):
    """190926 原帧：0 件灰锁及非零暗图案均不因亮度拒绝，真实 OCR/指纹/勾区。"""
    relative = "tests/fixtures/驱动盘筛选/套装弹窗-混沌爵士零件数.png"
    image = _image(relative)
    box = _name_box(relative, name)
    assert box is not None
    identity = make_identity(image, box)
    assert identity is not None
    enabled = Mock(side_effect=AssertionError("套装选项不得调用亮度可用性判读"))
    monkeypatch.setattr(gf, "_picker_enabled", enabled)
    assert gf._pre_click_state(image, _click_box(box), identity) is True
    enabled.assert_not_called()


def test_all_requested_sets_clicked_without_option_enabled_check(monkeypatch):
    """学习/回放都点齐请求集合，亮度观察只在确认按钮调用，缓存目标不减。"""
    names = ("混沌爵士", "河豚电音")
    stub = _PickerStub([list(names)])
    _install_stub(monkeypatch, stub)
    enabled_calls = []

    def confirm_only(_image, click):
        box = (click.left, click.top, click.right, click.bottom)
        assert box == stub.confirm_box
        enabled_calls.append(box)
        return True

    monkeypatch.setattr(gf, "_picker_enabled", confirm_only)
    session = FilterReplaySession()
    for mode in ("learn", "replay"):
        _run(session, stub, names)
        assert stub.selected == set(names)
        assert len(stub.clicks) == 3
        assert session.last_metrics["mode"] == mode
        assert session.last_metrics["selection_clicks"] == 2
        if mode == "replay":
            assert session.last_metrics["search_ocr_calls"] == 0
        route = next(iter(session.routes.values()))
        assert {c.name for stage in route.stages for c in stage.clicks} == set(names)
        stub.reset_log()
    assert len(enabled_calls) == 1  # 只在首次学习确认按钮时判读


_THUNDER_FRAME = "tests/fixtures/驱动盘筛选/套装弹窗-震星迪斯科候选漏字.png"


def test_truncated_thunder_candidate_recovered_with_precise_glyph_box():
    image = _image(_THUNDER_FRAME)
    lines = read_rect(image, LAYOUT.list_area)
    line = gf._match_targets(lines, ("震星迪斯科",))["震星迪斯科"]
    assert line.text == "星迪斯科"
    assert read_name_span(image, line, name="震星迪斯科") is None
    metrics = gf._PickerMetrics("learn")
    span = gf._read_picker_name_span(image, line, name="震星迪斯科", layout=LAYOUT, metrics=metrics)
    assert span is not None and span.text == "震星迪斯科"
    assert (span.left, span.top, span.right, span.bottom) == (858, 738, 945, 760)
    assert metrics.search_ocr_calls == 2
    identity = make_identity(image, (span.left, span.top, span.right, span.bottom))
    assert identity is not None
    assert gf._pre_click_state(image, span, identity) is True


def test_name_span_success_does_not_repeat_ocr():
    image = _image(_THUNDER_FRAME)
    line = gf._match_targets(read_rect(image, LAYOUT.list_area), ("激素朋克",))["激素朋克"]
    metrics = gf._PickerMetrics("learn")
    assert gf._read_picker_name_span(image, line, name="激素朋克", layout=LAYOUT, metrics=metrics) is not None
    assert metrics.search_ocr_calls == 1


@pytest.mark.parametrize("box,size", [
    ((790, 737, 865, 761), (1920, 1080)),  # 原候选越出单格/列表
    ((800, 339, 850, 360), (1920, 1080)),  # 名称部分在列表上方
    ((1540, 737, 1620, 761), (1920, 1080)),  # 列中心不匹配
    ((883, 737, 948, 761), (1280, 720)),  # 未标定尺寸
])
def test_name_recovery_refuses_invalid_search_geometry(box, size, monkeypatch):
    image = QImage(*size, QImage.Format_RGB32)
    image.fill(0)
    reader = Mock(return_value=None)
    monkeypatch.setattr(gf, "read_name_span", reader)
    line = OcrLine(text="星迪斯科", score=0.9, left=box[0], top=box[1], right=box[2], bottom=box[3])
    metrics = gf._PickerMetrics("learn")
    assert gf._read_picker_name_span(image, line, name="震星迪斯科", layout=LAYOUT, metrics=metrics) is None
    assert reader.call_count == metrics.search_ocr_calls == 1


def test_name_recovery_bounded_to_single_label_and_does_not_hide_exception(monkeypatch):
    image = _image(_THUNDER_FRAME)
    line = OcrLine(text="星迪斯科", score=0.9, left=883, top=737, right=948, bottom=761)
    calls = []

    def reader(_image, search_line, *, name):
        calls.append(search_line)
        if len(calls) == 2:
            raise RuntimeError("字符 OCR 故障")
        return None

    monkeypatch.setattr(gf, "read_name_span", reader)
    metrics = gf._PickerMetrics("learn")
    with pytest.raises(RuntimeError, match="字符 OCR 故障"):
        gf._read_picker_name_span(image, line, name="震星迪斯科", layout=LAYOUT, metrics=metrics)
    assert len(calls) == metrics.search_ocr_calls == 2
    assert (calls[1].left, calls[1].top, calls[1].right, calls[1].bottom) == (851, 733, 974, 765)


def test_real_frame_learning_and_replay_after_truncated_candidate(monkeypatch):
    """真实故障帧的 OCR/隔离/指纹跑完整学习，再用同一图回放；只替换输入和动态勾选。"""
    image = _image(_THUNDER_FRAME)
    confirm_line = next(ln for ln in read_rect(image, LAYOUT.confirm_area) if ln.text == "确认筛选")
    confirm_span = read_name_span(image, confirm_line, name="确认筛选")
    assert confirm_span is not None
    originals = {name: getattr(gf, name) for name in ("read_rect", "read_name_span", "read_name_bands", "make_identity", "identity_matches")}
    stub = _PickerStub([["震星迪斯科", "激素朋克"]],
        boxes={"震星迪斯科": (858, 738, 945, 760), "激素朋克": (1192, 738, 1261, 761)},
        confirm_box=(confirm_span.left, confirm_span.top, confirm_span.right, confirm_span.bottom))
    _install_stub(monkeypatch, stub)
    monkeypatch.setattr(gf, "_grab", lambda: (image, stub.hwnd))
    for name, function in originals.items():
        monkeypatch.setattr(gf, name, function)
    session = FilterReplaySession()
    for mode in ("learn", "replay"):
        _run(session, stub, ("震星迪斯科", "激素朋克"))
        assert stub.selected == {"震星迪斯科", "激素朋克"}
        assert len(stub.clicks) == 3
        assert session.last_metrics["mode"] == mode
        assert session.last_metrics["success"] is True
        if mode == "replay":
            assert session.last_metrics["search_ocr_calls"] == 0
        stub.reset_log()


def test_all_twelve_visible_names_in_truncated_candidate_frame():
    """同一真机滚动帧覆盖全部完整可见名称，避免只验收新报错的一项。"""
    names = ("法厄同之歌", "河豚电音", "混沌爵士", "激素朋克", "静听嘉音", "灵魂摇滚",
             "如影相随", "原始朋克", "折枝剑歌", "震星迪斯科", "啄木鸟电音", "自由蓝调")
    image = _image(_THUNDER_FRAME)
    lines = read_rect(image, LAYOUT.list_area)
    matches = gf._match_targets(lines, names)
    assert matches is not None and set(matches) == set(names)
    for name in names:
        metrics = gf._PickerMetrics("learn")
        span = gf._read_picker_name_span(image, matches[name], name=name, layout=LAYOUT, metrics=metrics)
        assert span is not None and span.text == name
        assert make_identity(image, (span.left, span.top, span.right, span.bottom)) is not None
        assert metrics.search_ocr_calls == (2 if name == "震星迪斯科" else 1)


@pytest.mark.parametrize("wrong_name", ["荆棘玫瑰", "震星迪斯科9", "迪斯科"])
def test_name_recovery_rejects_wrong_name_or_mixed_prefix(wrong_name):
    image = _image(_THUNDER_FRAME)
    line = OcrLine(text="星迪斯科", score=0.9, left=883, top=737, right=948, bottom=761)
    metrics = gf._PickerMetrics("learn")
    assert gf._read_picker_name_span(image, line, name=wrong_name, layout=LAYOUT, metrics=metrics) is None
    assert metrics.search_ocr_calls == 2


def test_name_recovery_restores_engine_after_second_failure(monkeypatch):
    first = SimpleNamespace(word_results=((("科", 0.9, [[0, 0], [10, 0], [10, 20], [0, 20]]),),))
    engine = Mock(side_effect=[first, RuntimeError("第二次识别失败")])
    engine.return_word_box = False
    engine.return_single_char_box = False
    monkeypatch.setattr(ocr, "_get_engine", lambda: engine)
    image = _image(_THUNDER_FRAME)
    line = OcrLine(text="星迪斯科", score=0.9, left=883, top=737, right=948, bottom=761)
    metrics = gf._PickerMetrics("learn")
    with pytest.raises(RuntimeError, match="第二次识别失败"):
        gf._read_picker_name_span(image, line, name="震星迪斯科", layout=LAYOUT, metrics=metrics)
    assert engine.call_count == metrics.search_ocr_calls == 2
    assert (engine.return_word_box, engine.return_single_char_box) == (False, False)


@pytest.mark.parametrize("identity", [True, False, None])
def test_pre_click_state_uses_only_identity(identity, monkeypatch):
    monkeypatch.setattr(gf, "identity_matches", lambda *_: identity)
    monkeypatch.setattr(gf, "_picker_enabled", Mock(side_effect=AssertionError("套装不检测亮度")))
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    assert gf._pre_click_state(image, _click_box((100, 100, 140, 120)), object()) is identity


@pytest.mark.parametrize("hits", [0, 50, 100, 500])
def test_checkmark_colors_do_not_block_identity_match(hits, monkeypatch):
    from PySide6.QtGui import QColor
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    for i in range(hits):
        image.setPixelColor(1274 + i % 38, 376 + i // 38, QColor(180, 220, 0))
    monkeypatch.setattr(gf, "identity_matches", lambda *_: True)
    monkeypatch.setattr(gf, "_save_picker_pre_click_failure", Mock(side_effect=AssertionError("勾选不能阻断")))
    assert gf._pre_click_state(image, _click_box((1188, 491, 1266, 517)), object()) is True


@pytest.mark.parametrize("replay", [False, True])
def test_picker_waits_actual_time_only_after_last_click(monkeypatch, replay):
    names = ("河豚电音", "静听嘉音", "折枝剑歌")
    stub = _PickerStub([names])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    if replay:
        with controlled_inputs(lambda: 0):
            _run(session, stub, names)
        stub.reset_log()
    waits = []
    monkeypatch.setattr(gf, "wait_release", lambda role, release, **kw: waits.append((role, len(stub.clicks), kw)))
    with controlled_inputs(lambda: 0):
        _run(session, stub, names)
    assert [w for w in waits if w[0] == "set_option"] == [
        ("set_option", 1, {"fallback_ms": 0, "allow_measured": False}),
        ("set_option", 2, {"fallback_ms": 0, "allow_measured": False}),
        ("set_option", 3, {"fallback_ms": 87, "allow_measured": True}),
    ]


def test_legacy_picker_tail_waits_each_viewport_before_scrolling(monkeypatch):
    names = ("静听嘉音", "折枝剑歌")
    stub = _PickerStub([[names[0]], [names[1]]])
    _install_stub(monkeypatch, stub)
    waits = []
    monkeypatch.setattr(gf, "wait_release", lambda role, release, **kw: waits.append((role, len(stub.clicks), kw)))
    gf._select_names(stub.hwnd, names, width=1920, height=1080, scroll_center=(960, 540))
    assert [w for w in waits if w[0] == "set_option"] == [
        ("set_option", 1, {"fallback_ms": 0, "allow_measured": False}),
        ("set_option", 1, {"fallback_ms": 87, "allow_measured": False}),
        ("set_option", 2, {"fallback_ms": 0, "allow_measured": False}),
        ("set_option", 2, {"fallback_ms": 87, "allow_measured": False}),
    ]


@pytest.mark.parametrize("cached_round,new_round", [(0, 0), (1, 0), (0, 2), (2, 1)])
def test_new_target_is_recognized_and_merged_with_cached_coordinates(monkeypatch, cached_round, new_round):
    names = ("河豚电音", "静听嘉音")
    screens = [[] for _ in range(max(cached_round, new_round) + 1)]
    screens[cached_round].append(names[0])
    screens[new_round].append(names[1])
    stub = _PickerStub(screens)
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    _run(session, stub, names[:1])
    cached_click = next(iter(session.routes.values())).stages[-1].clicks[0]
    stub.reset_log()
    original_read = gf._read_picker_targets
    original_identity = gf.make_identity
    recognized = []
    identity_boxes = []
    captures = []
    original_grab = gf._grab

    def read(image, pending, layout, metrics, **kwargs):
        assert pending == [names[1]], "缓存名称不得重新匹配"
        recognized.append(stub.index)
        return original_read(image, pending, layout, metrics, **kwargs)

    def identity(image, box):
        identity_boxes.append(box)
        return original_identity(image, box)

    def grab():
        captures.append((stub.index, len(stub.clicks)))
        return original_grab()

    monkeypatch.setattr(gf, "_read_picker_targets", read)
    monkeypatch.setattr(gf, "make_identity", identity)
    monkeypatch.setattr(gf, "_grab", grab)
    monkeypatch.setattr(gf, "_pre_click_state", Mock(side_effect=AssertionError("识别完就连续点击")))
    monkeypatch.setattr(gf, "_confirm_ready", Mock(side_effect=AssertionError("确认坐标已缓存")))
    _run(session, stub, names)
    assert recognized == list(range(new_round + 1))
    assert identity_boxes == [stub.box_of(new_round, screens[new_round].index(names[1]), names[1])]
    assert (cached_click.x, cached_click.y) in stub.clicks[:-1]
    assert stub.selected == set(names) and len(stub.clicks) == 3
    assert len(stub.wheels) == max(cached_round, new_round)
    if cached_round == new_round:
        assert (cached_round, 1) not in captures  # 两种来源坐标在同一批次点击
    route = session.routes[gf._picker_key(stub.hwnd, 1920, 1080, names, _layout())]
    assert {c.name for stage in route.stages for c in stage.clicks} == set(names)
    stub.reset_log()
    monkeypatch.setattr(gf, "_read_picker_targets", Mock(side_effect=AssertionError("全部已有缓存")))
    _run(session, stub, names)
    assert session.last_metrics["mode"] == "replay" and session.last_metrics["search_ocr_calls"] == 0
    assert stub.selected == set(names)


def test_cached_names_are_reused_across_target_sets_without_ocr(monkeypatch):
    names = ("河豚电音", "静听嘉音", "折枝剑歌")
    stub = _PickerStub([list(names)])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    _run(session, stub, names[:2])
    stub.reset_log()
    _run(session, stub, names[2:])
    stub.reset_log()
    for name in ("_read_picker_targets", "read_rect", "read_name_bands", "make_identity",
                 "_pre_click_state", "_picker_stage_matches", "_picker_at_top", "_confirm_ready"):
        monkeypatch.setattr(gf, name, Mock(side_effect=AssertionError("跨集合缓存坐标不得识别")))
    _run(session, stub, (names[0], names[2]))
    assert stub.selected == {names[0], names[2]} and len(stub.clicks) == 3
    assert session.last_metrics["mode"] == "replay" and session.last_metrics["search_ocr_calls"] == 0


def test_mixed_scan_skips_cached_name_band(monkeypatch):
    stub = _PickerStub([["河豚电音", "静听嘉音"]])
    _install_stub(monkeypatch, stub)
    image, _ = gf._grab()
    boxes = stub.name_boxes()
    cached_box = boxes["河豚电音"]
    cached = SetPickerClick("河豚电音", *cached_box,
        (cached_box[0]+cached_box[2])//2, (cached_box[1]+cached_box[3])//2,
        gf.make_identity(image, cached_box))
    bands = tuple(boxes.values())
    monkeypatch.setattr(gf, "_picker_name_bands", lambda *args: bands)
    received = []

    def read(_image, requested_bands):
        received.extend(requested_bands)
        box = boxes["静听嘉音"]
        return [OcrLine(text="静听嘉音", left=box[0], top=box[1], right=box[2], bottom=box[3], score=1)]

    monkeypatch.setattr(gf, "read_name_bands", read)
    matched = gf._read_picker_targets(image, ["静听嘉音"], _layout(), gf._PickerMetrics("learn"),
                                      cached_clicks=[cached])
    assert received == [boxes["静听嘉音"]]
    assert list(matched) == ["静听嘉音"]


def test_mixed_missing_target_stops_without_confirmation_or_publishing_cache(monkeypatch):
    stub = _PickerStub([["河豚电音"]])
    _install_stub(monkeypatch, stub)
    session = FilterReplaySession()
    _run(session, stub, ("河豚电音",))
    stub.reset_log()
    with pytest.raises(gf.FilterClickError, match="仍未找到全部目标"):
        _run(session, stub, ("河豚电音", "静听嘉音"))
    assert len(stub.clicks) == 1  # 已发缓存选项不重发、不盲点确认
    assert stub.popup_open and session.routes == {}
    assert session.last_metrics["success"] is False


def test_learning_and_cached_replay_wait_for_same_scroll_completion(monkeypatch):
    names = ("静听嘉音", "折枝剑歌")
    stub = _PickerStub([[], names])
    _install_stub(monkeypatch, stub)
    clock = [0]
    settled_at = [0]
    scroll_waits = []
    real_wheel = gf.wheel_client

    def wheel(*args, **kwargs):
        result = real_wheel(*args, **kwargs)
        settled_at[0] = clock[0] + 784
        return result

    def wait(role, release, *, fallback_ms, **kwargs):
        if role == "set_scroll":
            scroll_waits.append(fallback_ms)
        clock[0] += fallback_ms

    monkeypatch.setattr(gf, "wheel_client", wheel)
    monkeypatch.setattr(gf, "wait_release", wait)
    # 弹层布局已经出现不代表滚动结束；过早核对缓存位置仍会得到False。
    monkeypatch.setattr(gf, "identity_matches", lambda *_: clock[0] >= settled_at[0])
    session = FilterReplaySession()
    with controlled_inputs(lambda: 0):
        _run(session, stub, names)
    assert len(session.routes) == 1
    assert session.last_metrics["mode"] == "learn"
    stub.reset_log()
    with controlled_inputs(lambda: 0):
        _run(session, stub, names)
    assert scroll_waits == [784, 784]
    assert session.last_metrics["mode"] == "replay"
    assert session.last_metrics["success"] and session.last_metrics["search_ocr_calls"] == 0
    assert stub.selected == set(names) and len(stub.wheels) == 1


def test_legacy_scroll_uses_same_picker_scroll_budget(monkeypatch):
    names = ("静听嘉音", "折枝剑歌")
    stub = _PickerStub([[names[0]], [names[1]]])
    _install_stub(monkeypatch, stub)
    scroll_waits = []
    def wait(role, release, **kwargs):
        if role == "set_scroll":
            scroll_waits.append(kwargs)
    monkeypatch.setattr(gf, "wait_release", wait)
    gf._select_names(stub.hwnd, names, width=1920, height=1080, scroll_center=(960,540))
    assert scroll_waits == [{"fallback_ms": 784, "allow_measured": False}]


def test_scroll_rejection_frame_is_in_motion_despite_valid_popup_layout():
    from dataclasses import replace
    from zzz_ddtool.filter_replay import identity_score
    reference = _image("tests/fixtures/驱动盘筛选/套装弹窗-两项选中第三项未选-20261012.png")
    moving = _image("tests/fixtures/驱动盘筛选/套装回放-滚动中拒绝原帧-20261012.png")
    settled = _image("tests/fixtures/驱动盘筛选/套装回放-滚动稳定失败后-20261012.png")
    identity = make_identity(reference, (1188,491,1266,517))
    assert identity is not None
    assert gf._picker_stage_matches(moving, LAYOUT) is True
    assert identity_matches(moving, identity) is False
    assert identity_matches(settled, identity) is True
    displaced = replace(identity, top=identity.top+10, bottom=identity.bottom+10)
    assert identity_score(moving, displaced) > identity_score(moving, identity) + 0.2
