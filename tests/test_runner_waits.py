"""§8 W1～W4、W7～W10 的 Runner 集成层直接断言。

规则（Spec §8）：用显式模拟时钟（``sim_time``）驱动真实等待引擎，不把 ``_wait_ready``
固定成「永远成功」；只替换窗口/截图/识别/输入接口，不发送任何真实游戏输入。
凡本文件把 ``_wait_ready`` 替换成替身的地方，都只针对「必须失败/必须抛中断」的分支，
真实等待引擎的边界由 ``tests/test_readiness.py`` 与本文件 W1/W2/W3 的真实轮询用例覆盖。
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtGui import QImage
from unittest.mock import Mock, call

import zzz_ddtool.ocr as ocr_module
import zzz_ddtool.readiness as readiness
import zzz_ddtool.runner as runner_module
from zzz_ddtool.config import Preset
from zzz_ddtool.models import Disc, Substat, next_stage_level
from zzz_ddtool.ocr import OcrError
from zzz_ddtool.readiness import ReadinessInterrupted, ReadinessTimeout, WaitResult, WaitSample
from zzz_ddtool.runner import Runner, RunnerError, _LabelContext
from zzz_ddtool.traversal import GridCell


# --------------------------------------------------------------------------
# 纯替身工具
# --------------------------------------------------------------------------
def image() -> QImage:
    img = QImage(1920, 1080, QImage.Format.Format_RGB32)
    img.fill(0)
    return img


def disc(level: int = 0, **kwargs) -> Disc:
    base = (Substat("暴击率", 1), Substat("攻击力", 1), Substat("生命值", 1))
    return Disc(level, base, **kwargs)


def line(text: str, box=(100, 100, 200, 140)):
    return ocr_module.OcrLine(text, 0.99, *box)


def timeout(message: str = "等待超时") -> ReadinessTimeout:
    return ReadinessTimeout(message, last_sample=None, active_ms=0.0, samples=1, pause_generation=None)


def cell(index: int = 0, row: int = 0, col: int = 0) -> GridCell:
    return GridCell(
        global_index=index,
        global_row0=index // 9,
        global_col0=col,
        screen_row0=row,
        screen_col0=col,
        center_x=166 + col * 135,
        center_y=267 + row * 175,
        level=0,
    )


class Frames:
    """按序返回帧；耗尽后重复最后一帧。每帧可带 page/disc/selected/image 配置。"""

    def __init__(self, specs):
        self.specs = [dict(spec) for spec in specs]
        self.index = 0
        self.grabs: list[dict] = []
        self.last = self.specs[-1]

    def grab(self, *_args, **_kwargs):
        spec = self.specs[min(self.index, len(self.specs) - 1)]
        self.index += 1
        self.grabs.append(spec)
        self.last = spec
        return (spec.get("image") or image(), spec.get("page", "drive_disc"), "printwindow", 1)


def patch_regions(monkeypatch, mapping):
    """把 ``ocr.read_rect`` 换成按区域返回固定 OCR 行；``None`` 键用于整窗区域。"""

    def fake(image_, region):
        key = tuple(region)
        if key in mapping:
            return list(mapping[key])
        if region and region[0] == 0 and region[1] == 0:
            return list(mapping.get(None, []))
        return []

    monkeypatch.setattr(ocr_module, "read_rect", fake)


def bind_grab(monkeypatch, runner, frames: Frames):
    monkeypatch.setattr(runner, "_grab", lambda *_a, **_k: frames.grab())


# --------------------------------------------------------------------------
# W1：select_disc 下限 + 双帧稳定 + 目标移走有限失败
# --------------------------------------------------------------------------
def test_w1_select_returns_only_after_bound_and_two_stable_target_frames(monkeypatch, sim_time):
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    click = Mock()
    press = Mock()
    monkeypatch.setattr(runner_module, "click_client", click)
    monkeypatch.setattr(runner_module, "press_key_once", press)
    target = disc(3)
    frames = Frames([
        {"selected": False, "disc": target, "page": "drive_disc"},
        {"selected": False, "disc": target, "page": "drive_disc"},
        {"selected": False, "disc": target, "page": "drive_disc"},
        {"selected": True, "disc": target, "page": "drive_disc"},
    ])
    bind_grab(monkeypatch, runner, frames)
    monkeypatch.setattr(runner, "_tile_selected_for_cell", lambda _image, _cell: frames.last["selected"])
    monkeypatch.setattr(runner_module, "parse_disc", lambda _image: frames.last["disc"])

    result = runner._wait_ready("select_disc", started_at=sim_time.now(), cell=cell())

    assert result.disc == target
    # 前 3 帧旧格选中；目标选中须等到下限 500 ms 之后两个 50 ms 独立稳定帧（第 12 次采样）。
    assert result.samples == 12
    assert frames.index == 12
    assert click.call_count == 0
    assert press.call_count == 0


def test_w1_select_target_never_selected_times_out_without_input(monkeypatch, sim_time):
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    click = Mock()
    press = Mock()
    monkeypatch.setattr(runner_module, "click_client", click)
    monkeypatch.setattr(runner_module, "press_key_once", press)
    frames = Frames([{"selected": False, "disc": disc(1), "page": "drive_disc"}])
    bind_grab(monkeypatch, runner, frames)
    monkeypatch.setattr(runner, "_tile_selected_for_cell", lambda _image, _cell: False)
    monkeypatch.setattr(runner_module, "parse_disc", lambda _image: frames.last["disc"])

    with pytest.raises(ReadinessTimeout) as excinfo:
        runner._wait_ready("select_disc", started_at=sim_time.now(), cell=cell())

    assert excinfo.value.samples >= 55  # 500 ms 下限不会让「一直不选中」提前成功
    assert frames.grabs[-1]["selected"] is False
    assert click.call_count == 0
    assert press.call_count == 0


def test_w1_select_timeout_recheck_rescans_without_extra_click(monkeypatch, sim_time):
    """§6.3：超时补拍仍为已知错页 → 原一次 Esc → 严格确认 drive_disc → rescan。"""
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    click = Mock()
    esc = Mock()
    monkeypatch.setattr(runner_module, "click_client", click)
    monkeypatch.setattr(runner_module, "press_key", esc)
    monkeypatch.setattr(runner_module, "press_key_once", Mock())
    monkeypatch.setattr(runner, "_wait_ready", Mock(side_effect=timeout("select 超时")))
    frames = Frames([
        {"page": "storage"},     # 超时后的补拍：仍是已知错页
        {"page": "drive_disc"},  # Esc 之后回到仓库
    ])
    bind_grab(monkeypatch, runner, frames)

    result = runner._process_cell_inner(1, Mock(), Mock(), cell(), Mock(), set(), {})

    assert result == "rescan"
    assert click.call_count == 1          # 选盘只点一次，不重定位
    assert esc.call_count == 1            # 只按 1 次 Esc 恢复
    assert frames.index == 2


# --------------------------------------------------------------------------
# W2：material_ready 取时刻边界与超时
# --------------------------------------------------------------------------
def test_w2_material_ready_two_stable_changed_frames_complete(monkeypatch, sim_time):
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    click = Mock()
    monkeypatch.setattr(runner_module, "click_client", click)
    parse = Mock()
    monkeypatch.setattr(runner_module, "parse_disc", parse)
    before_image = image()
    changed_image = image()
    frames = Frames([{"image": before_image}, {"image": changed_image}])
    bind_grab(monkeypatch, runner, frames)
    monkeypatch.setattr(
        runner, "_material_bar_crop",
        lambda img: np.zeros((4, 4)) if img is before_image else np.full((4, 4), 10.0),
    )
    patch_regions(monkeypatch, {
        Runner.STAGE_ADD_AREA: [line("阶段添加")],
        Runner.UPGRADE_CONFIRM_AREA: [line("升级")],
    })

    ok = runner._upgrade_once(1, 0, before=disc(0))

    assert ok is True
    assert runner._material_started_at is not None
    assert runner._upgrade_started_at is not None
    assert click.call_count == 2          # 阶段添加 + 升级，各一次
    parse.assert_not_called()             # 不增加预计等级 OCR/Disc


def test_w2_material_started_at_is_click_return_not_release(monkeypatch, sim_time):
    """模拟鼠标释放 1.000s、click_client 返回 1.100s：素材时刻必须取点击返回时刻。"""
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    clock = sim_time
    released: list[float] = []

    def click(_hwnd, _x, _y):
        released.append(clock.now())
        clock.t = 1.100

    monkeypatch.setattr(runner_module, "click_client", click)
    frames = Frames([{"image": image()}])
    bind_grab(monkeypatch, runner, frames)
    waits: list[float] = []

    def fake_wait(kind, *, started_at, **_kwargs):
        assert kind == "material_ready"
        waits.append(started_at)
        raise timeout("素材未就绪")

    monkeypatch.setattr(runner, "_wait_ready", fake_wait)
    patch_regions(monkeypatch, {Runner.STAGE_ADD_AREA: [line("阶段添加")]})

    clock.t = 1.000
    with pytest.raises(RunnerError, match="素材准备就绪超时"):
        runner._upgrade_once(1, 0, before=disc(0))

    assert released == [1.000]
    assert runner._material_started_at == 1.100
    assert waits == [1.100]
    assert runner._upgrade_started_at is None   # 升级输入 0
    assert frames.index == 1                    # 没有走到「升级」定位帧


# --------------------------------------------------------------------------
# W3：upgrade_result 只等期望档，不等下一档
# --------------------------------------------------------------------------
def test_w3_upgrade_result_waits_for_expected_level_not_next(monkeypatch, sim_time):
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    monkeypatch.setattr(runner_module, "click_client", Mock())
    monkeypatch.setattr(runner_module, "press_key_once", Mock())
    frames = Frames([{"disc": disc(1)}, {"disc": disc(2)}, {"disc": disc(3)}])
    bind_grab(monkeypatch, runner, frames)
    monkeypatch.setattr(runner, "_parse_upgrade_page", lambda _image: frames.last["disc"])
    runner._upgrade_started_at = sim_time.now()
    before = disc(0)

    result, frame = runner._await_upgrade_result(
        1, "R1C1", before=before, expected_level=next_stage_level(before.level),
        disc_slot=-1, stage_started=None,
    )

    assert result.level == 3
    assert frame is not None
    assert frames.grabs[-1]["disc"].level == 3
    assert runner._upgrade_started_at is None


# --------------------------------------------------------------------------
# W4：材料返还中断与接续
# --------------------------------------------------------------------------
def _interrupted(frame, wait_id=None):
    sample = WaitSample(
        frame=frame, captured_at=0.0, ready=False, state_key=("upgrade_result", "refund"),
        evidence={"refund": {"text": "材料返还确认"}}, interrupt_reason="material_refund",
    )
    exc = ReadinessInterrupted(
        "material_refund", last_sample=sample, active_ms=0.0, samples=2, pause_generation=None
    )
    if wait_id is not None:
        exc.wait_id = wait_id
    return exc


def test_w4_full_refund_evidence_resumes_once_without_resending_upgrade(monkeypatch, sim_time):
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    click = Mock()
    monkeypatch.setattr(runner_module, "click_client", click)
    refund_frame = image()
    result_frame = image()
    patch_regions(monkeypatch, {None: [line("材料返还"), line("确认", (10, 10, 60, 40))]})
    calls: list[tuple] = []

    def fake_wait(kind, *, started_at, **kwargs):
        calls.append((kind, started_at, kwargs))
        if len([c for c in calls if c[0] == "upgrade_result"]) == 1:
            raise _interrupted(refund_frame, wait_id="wait-1")
        return WaitResult(frame=result_frame, disc=disc(3), active_ms=0.0, samples=1, pause_generation=None)

    monkeypatch.setattr(runner, "_wait_ready", fake_wait)
    runner._upgrade_started_at = sim_time.now()
    runner._upgrade_input_id = "input-upgrade"

    result, frame = runner._await_upgrade_result(
        1, "R1C1", before=disc(0), expected_level=3, disc_slot=-1, stage_started=None,
    )

    assert result.level == 3
    assert frame is result_frame
    assert click.call_count == 1                      # 只点一次「确认」，绝不重发升级
    assert calls[1][0] == "upgrade_result"
    assert calls[1][1] == runner._refund_started_at    # 第二次等待起点 = 确认返回时刻
    assert calls[1][2]["trigger_input_id"] == runner._refund_input_id
    assert calls[1][2]["resume_from_wait_id"] == "wait-1"
    assert runner._upgrade_input_id == "input-upgrade"


def test_w4_second_result_abnormal_stops_without_second_confirm(monkeypatch, sim_time):
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    click = Mock()
    monkeypatch.setattr(runner_module, "click_client", click)
    refund_frame = image()
    patch_regions(monkeypatch, {None: [line("材料返还"), line("确认", (10, 10, 60, 40))]})
    seen = {"n": 0}

    def fake_wait(kind, *, started_at, **kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            raise _interrupted(refund_frame, wait_id="wait-1")
        raise timeout("返还后仍未就绪")

    monkeypatch.setattr(runner, "_wait_ready", fake_wait)
    runner._upgrade_started_at = sim_time.now()

    with pytest.raises(RunnerError, match="返还确认后升级结果仍未确认"):
        runner._await_upgrade_result(
            1, "R1C1", before=disc(0), expected_level=3, disc_slot=-1, stage_started=None,
        )

    assert click.call_count == 1     # 只确认一次，不重建第二次
    assert seen["n"] == 2


def test_w4_title_only_never_interrupts_and_upgrade_result_stays_pending(monkeypatch, sim_time):
    """只有标题没有「确认」行时不得确认；持续无盘面 → 结果无法确认失败。"""
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    click = Mock()
    monkeypatch.setattr(runner_module, "click_client", click)
    frames = Frames([{"image": image()}])
    bind_grab(monkeypatch, runner, frames)
    monkeypatch.setattr(runner, "_parse_upgrade_page", lambda _image: None)
    monkeypatch.setattr(runner, "_find_material_refund", lambda _image: None)
    monkeypatch.setattr(runner, "_wait_ready", Mock(side_effect=timeout("upgrade_result 超时")))
    runner._upgrade_started_at = sim_time.now()

    with pytest.raises(RunnerError, match="升级结果无法确认"):
        runner._await_upgrade_result(
            1, "R1C1", before=disc(0), expected_level=3, disc_slot=-1, stage_started=None,
        )

    assert click.call_count == 0


# --------------------------------------------------------------------------
# W7：return_warehouse 逐层 Esc 循环
# --------------------------------------------------------------------------
def _layer_runner(monkeypatch, sim_time, layers: dict, min_wait_ms: int):
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    esc = Mock()
    monkeypatch.setattr(runner_module, "press_key_once", esc)
    monkeypatch.setattr(runner, "_return_layer", lambda img: layers[id(img)])
    if min_wait_ms != readiness.DEFAULT_FLOOR_MS["return_warehouse"]:
        monkeypatch.setattr(
            runner, "_wait_policy",
            lambda kind: readiness.WaitPolicy(min_wait_ms, 1000, 50, 2),
        )
    return runner, esc


def test_w7_return_to_warehouse_single_esc_real_wait_default_bound(monkeypatch, sim_time):
    modal, warehouse = image(), image()
    runner, esc = _layer_runner(monkeypatch, sim_time, {id(modal): "modal", id(warehouse): "warehouse"},
                                readiness.DEFAULT_FLOOR_MS["return_warehouse"])
    frames = Frames([{"image": modal}] * 9 + [{"image": warehouse}])
    bind_grab(monkeypatch, runner, frames)

    assert runner._esc_back_to_grid(1) is True
    assert esc.call_count == 1
    # 前 8 次采样仍 modal（下限 600ms 前的候选全被丢弃），仓库层要等到第 13 次采样才凑够双帧稳定；
    # 加上 _esc_back_to_grid 进入循环前的那一次认层抓帧，共 15 次。
    assert frames.index == 15


def test_w7_return_two_layer_edges_esc_twice(monkeypatch, sim_time):
    modal, preview, warehouse = image(), image(), image()
    layers = {id(modal): "modal", id(preview): "preview", id(warehouse): "warehouse"}
    runner, esc = _layer_runner(monkeypatch, sim_time, layers, 0)
    # Esc#1：modal 两帧未就绪 → preview 两帧完成；Esc#2：preview → warehouse 两帧完成。
    frames = Frames([
        {"image": modal}, {"image": modal}, {"image": preview}, {"image": preview},
        {"image": preview}, {"image": preview}, {"image": warehouse}, {"image": warehouse},
    ])
    bind_grab(monkeypatch, runner, frames)

    assert runner._esc_back_to_grid(1) is True
    assert esc.call_count == 2


def test_w7_already_warehouse_sends_no_esc(monkeypatch, sim_time):
    warehouse = image()
    runner, esc = _layer_runner(monkeypatch, sim_time, {id(warehouse): "warehouse"}, 0)
    frames = Frames([{"image": warehouse}])
    bind_grab(monkeypatch, runner, frames)

    assert runner._esc_back_to_grid(1) is True
    assert esc.call_count == 0


def test_w7_unknown_layers_fail_without_blind_esc(monkeypatch, sim_time):
    unknown = image()
    runner = Runner(Preset(ocr_retry=3), hotkeys=False)
    runner.trace = Mock()
    esc = Mock()
    monkeypatch.setattr(runner_module, "press_key_once", esc)
    monkeypatch.setattr(runner, "_return_layer", lambda _img: "unknown")
    frames = Frames([{"image": unknown}])
    bind_grab(monkeypatch, runner, frames)

    with pytest.raises(RunnerError, match="无法确认盘面弹窗或仓库"):
        runner._esc_back_to_grid(1)
    assert esc.call_count == 0


def test_w7_max_esc_one_stops_before_second_esc(monkeypatch, sim_time):
    modal, preview = image(), image()
    runner, esc = _layer_runner(
        monkeypatch, sim_time, {id(modal): "modal", id(preview): "preview"}, 0
    )
    frames = Frames([{"image": modal}, {"image": preview}, {"image": preview}])
    bind_grab(monkeypatch, runner, frames)

    with pytest.raises(RunnerError, match="回退后弹窗仍未关闭"):
        runner._esc_back_to_grid(1, max_esc=1)
    assert esc.call_count == 1        # 第二次按键前就停，不额外退出仓库
    assert frames.index == 3


# --------------------------------------------------------------------------
# W8：标签快速复核有界、不重复输入、未确认解锁绝不 R
# --------------------------------------------------------------------------
def _label_runner(monkeypatch, ocr_retry=2):
    runner = Runner(Preset(ocr_retry=ocr_retry, skip_locked=False), hotkeys=False)
    runner.trace = Mock()
    press = Mock()
    monkeypatch.setattr(runner_module, "press_key_once", press)
    monkeypatch.setattr(runner_module.time, "sleep", lambda _s: None)
    return runner, press


def test_w8_verify_label_bounded_and_no_repeat_input(monkeypatch):
    runner, press = _label_runner(monkeypatch, ocr_retry=2)
    reader = Mock(return_value=({"locked": None, "discarded": None}, image()))
    monkeypatch.setattr(runner, "_read_label_state", reader)

    with pytest.raises(RunnerError, match="无法确认"):
        runner._verify_label(
            1, key="discarded", value=True, other_value=False, context=_LabelContext(None, None)
        )

    assert reader.call_count == 8     # max(1, ocr_retry) * 4
    assert press.call_count == 0


def test_w8_discard_never_presses_r_before_unlock_confirmed(monkeypatch):
    runner, press = _label_runner(monkeypatch, ocr_retry=2)
    reader = Mock(return_value=({"locked": True, "discarded": False}, image()))
    monkeypatch.setattr(runner, "_read_label_state", reader)

    with pytest.raises(RunnerError):
        runner._set_discard_label(1, discarded=False, value=True, before=disc(3))

    assert [c.args[1] for c in press.call_args_list] == [runner_module.VK_T]


def test_w8_ocr_error_propagates_immediately(monkeypatch):
    runner, press = _label_runner(monkeypatch, ocr_retry=3)
    reader = Mock(side_effect=OcrError("OCR 挂了"))
    monkeypatch.setattr(runner, "_read_label_state", reader)

    with pytest.raises(OcrError):
        runner._set_discard_label(1, discarded=False, value=True, before=disc(3))

    assert reader.call_count == 1
    assert press.call_count == 0


# --------------------------------------------------------------------------
# W9：open_preview / open_upgrade 接续
# --------------------------------------------------------------------------
def _upgrade_page_runner(monkeypatch):
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    click = Mock()
    monkeypatch.setattr(runner_module, "click_client", click)
    monkeypatch.setattr(runner, "_esc_back_to_grid", Mock())
    bind_grab(monkeypatch, runner, Frames([{"image": image()}]))
    patch_regions(monkeypatch, {Runner.UPGRADE_BTN_AREA: [line("查看", (1560, 800, 1700, 860))]})
    return runner, click


def test_w9_open_preview_timeout_does_not_resend_detail_button(monkeypatch):
    runner, click = _upgrade_page_runner(monkeypatch)
    monkeypatch.setattr(runner, "_wait_ready", Mock(side_effect=timeout("open_preview 超时")))

    assert runner._enter_upgrade_page(1, before=disc(3)) is False
    assert click.call_count == 1
    runner._esc_back_to_grid.assert_called_once_with(1)


def test_w9_open_upgrade_second_attempt_needs_same_object_preview(monkeypatch):
    runner, click = _upgrade_page_runner(monkeypatch)
    attempts = {"n": 0}

    def fake_wait(kind, **_kwargs):
        if kind == "open_preview":
            return WaitResult(frame=image(), disc=None, active_ms=0.0, samples=2, pause_generation=None)
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise timeout("第一次箭头超时")
        return WaitResult(frame=image(), disc=None, active_ms=0.0, samples=2, pause_generation=None)

    monkeypatch.setattr(runner, "_wait_ready", fake_wait)
    monkeypatch.setattr(runner, "_zoom_arrow_of", Mock(side_effect=[(500, 200), (510, 200)]))

    assert runner._enter_upgrade_page(1, before=disc(3)) is True
    assert click.call_count == 3      # 详情按钮 + 两次箭头
    runner._esc_back_to_grid.assert_not_called()


def test_w9_open_upgrade_fails_when_preview_is_lost(monkeypatch):
    runner, click = _upgrade_page_runner(monkeypatch)

    def fake_wait(kind, **_kwargs):
        if kind == "open_preview":
            return WaitResult(frame=image(), disc=None, active_ms=0.0, samples=2, pause_generation=None)
        raise timeout("箭头后未进强化页")

    monkeypatch.setattr(runner, "_wait_ready", fake_wait)
    monkeypatch.setattr(runner, "_zoom_arrow_of", Mock(side_effect=[(500, 200), None]))

    assert runner._enter_upgrade_page(1, before=disc(3)) is False
    assert click.call_count == 2      # 详情按钮 + 第一次箭头，第二次不再发
    runner._esc_back_to_grid.assert_called_once_with(1)


def test_w9_open_upgrade_probe_ignores_unknown_rarity_on_upgrade_page(monkeypatch):
    """强化页不显示等级上限 → 解析 rarity=None；对象接续只比等级与词条（§6.2 表）。

    2026-10-10 真机标定实测：若按含稀有度的 ``_core_key`` 全字段比较，强化页解析出的
    rarity 恒为 None，而 before 来自详情页（「等级12/15」→ 上限 15 → S），这条等待在
    真机上永远不就绪（`open_upgrade` 5/5 超时，12 帧全 `stage_add=True` 且词条一致）。
    """
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    bind_grab(monkeypatch, runner, Frames([{"image": image()}]))
    monkeypatch.setattr(runner, "_has_stage_add", lambda _frame: True)
    before = Disc(12, (Substat("暴击率", 1), Substat("攻击力", 1)), rarity="S")

    # 强化页：rarity 读不出，等级与词条与 before 一致 → 就绪
    upgrade = Disc(12, (Substat("攻击力", 1), Substat("暴击率", 1)), rarity=None)
    monkeypatch.setattr(runner, "_parse_upgrade_page", lambda _frame: upgrade)
    assert runner._probe_open_upgrade(before)().ready is True

    # 词条区域还没渲染完（少一条）→ 不就绪
    monkeypatch.setattr(
        runner, "_parse_upgrade_page",
        lambda _frame: Disc(12, (Substat("攻击力", 1),), rarity=None),
    )
    assert runner._probe_open_upgrade(before)().ready is False

    # 等级与 before 不一致 → 不就绪
    monkeypatch.setattr(
        runner, "_parse_upgrade_page",
        lambda _frame: Disc(11, upgrade.substats, rarity=None),
    )
    assert runner._probe_open_upgrade(before)().ready is False

    # 两侧稀有度都已知且不同 → 仍不就绪（稀有度只在都已知时参与比较）
    monkeypatch.setattr(
        runner, "_parse_upgrade_page",
        lambda _frame: Disc(12, upgrade.substats, rarity="A"),
    )
    assert runner._probe_open_upgrade(before)().ready is False

    # 没有「阶段添加」→ 不就绪
    monkeypatch.setattr(runner, "_has_stage_add", lambda _frame: False)
    monkeypatch.setattr(runner, "_parse_upgrade_page", lambda _frame: upgrade)
    assert runner._probe_open_upgrade(before)().ready is False


# --------------------------------------------------------------------------
# W10：标签目标状态即可完成、不盲按
# --------------------------------------------------------------------------
def test_w10_verify_label_completes_on_first_complete_target_state(monkeypatch):
    runner, _press = _label_runner(monkeypatch)
    img = image()
    reader = Mock(side_effect=[
        ({"locked": False, "discarded": None}, img),
        ({"locked": False, "discarded": True}, img),
    ])
    monkeypatch.setattr(runner, "_read_label_state", reader)

    out = runner._verify_label(
        1, key="discarded", value=True, other_value=False, context=_LabelContext(None, None)
    )

    assert out is img
    assert reader.call_count == 2     # 首个完整目标状态即完成，不要求双帧


def test_w10_other_label_unexpected_change_stops(monkeypatch):
    runner, press = _label_runner(monkeypatch)
    reader = Mock(return_value=({"locked": True, "discarded": True}, image()))
    monkeypatch.setattr(runner, "_read_label_state", reader)

    with pytest.raises(RunnerError, match="另一标签状态意外变化"):
        runner._verify_label(
            1, key="locked", value=True, other_value=False, context=_LabelContext(None, None)
        )

    assert press.call_count == 0


def test_w10_already_at_target_does_not_press(monkeypatch):
    runner, press = _label_runner(monkeypatch)
    reader = Mock(return_value=({"locked": False, "discarded": True}, image()))
    monkeypatch.setattr(runner, "_read_label_state", reader)

    changed = runner._set_discard_label(1, discarded=True, value=True, before=disc(3))

    assert changed is False
    assert press.call_count == 0
    assert reader.call_count == 1


# --------------------------------------------------------------------------
# P2：真实 outcome / termination_kind、提前返回、连续两次 run、返还两次等待
# --------------------------------------------------------------------------
class _PerfCapture:
    """把 scanperf 的会话与事件收集到内存，不改写真实 log/。"""

    def __init__(self, monkeypatch):
        import zzz_ddtool.scanperf as sp

        self.module = sp
        self.events: list = []
        self.sessions: list[dict] = []
        monkeypatch.setattr(sp, "begin_session", self._begin)
        monkeypatch.setattr(sp, "record_event", self._record)
        monkeypatch.setattr(sp, "end_session", lambda: None)

    def _begin(self, tag, **kwargs):
        self.sessions.append({"tag": tag, **kwargs})
        return Mock()

    def _record(self, event):
        self.events.append(event)
        return "id"

    def of(self, component: str):
        return [e for e in self.events if e.component == component]


def _run_runner(monkeypatch, preset: Preset, *, jobs=None, run_job=None):
    runner = Runner(preset, hotkeys=False)
    runner.trace = Mock()
    runner.trace.tag = "t"
    runner.trace.dir = None
    runner.log = Mock()
    monkeypatch.setattr(runner, "_set_status", Mock())
    monkeypatch.setattr(runner, "_finish_stats", Mock(return_value=""))
    if jobs is not None:
        monkeypatch.setattr(runner, "_jobs", lambda: jobs)
    if run_job is not None:
        monkeypatch.setattr(runner, "_run_job", run_job)
    return runner


def test_p2_invalid_max_pages_returns_empty_with_error_outcome(monkeypatch):
    capture = _PerfCapture(monkeypatch)
    click = Mock()
    press = Mock()
    monkeypatch.setattr(runner_module, "click_client", click)
    monkeypatch.setattr(runner_module, "press_key_once", press)
    monkeypatch.setattr(
        runner_module, "find_game_hwnd",
        Mock(side_effect=AssertionError("非法配额不得绑定窗口")),
    )
    runner = _run_runner(monkeypatch, Preset(max_pages=0))

    result = runner.run()

    assert result == []
    assert runner.outcome == "failed"
    assert runner.termination_kind == "error"
    run_events = capture.of("run_total")
    assert len(run_events) == 1
    assert run_events[0].details["runner_outcome"] == "failed"
    assert run_events[0].details["termination_kind"] == "error"
    assert click.call_count == 0
    assert press.call_count == 0
    assert capture.of("disc_total") == []


def test_p2_zero_discs_produces_only_run_total(monkeypatch):
    capture = _PerfCapture(monkeypatch)
    runner = _run_runner(monkeypatch, Preset(), jobs=())

    result = runner.run()

    assert result == []
    assert runner.outcome == "completed"
    assert runner.termination_kind == "completed"
    assert runner.processed_disc_count == 0
    assert capture.of("disc_total") == []
    assert len(capture.of("run_total")) == 1


def test_p2_two_consecutive_runs_have_unique_run_ids(monkeypatch):
    capture = _PerfCapture(monkeypatch)
    runner = _run_runner(monkeypatch, Preset(), jobs=())

    runner.run()
    runner.run()

    run_ids = [session["run_id"] for session in capture.sessions]
    assert len(run_ids) == 2
    assert run_ids[0] != run_ids[1]
    assert len(capture.of("run_total")) == 2


def test_p2_stop_maps_to_user_stopped(monkeypatch):
    capture = _PerfCapture(monkeypatch)
    runner = _run_runner(monkeypatch, Preset())
    job = replace(runner._default_job(), accepted=("x",))
    monkeypatch.setattr(runner, "_jobs", lambda: (job,))

    def run_job(_job, _replay=None):
        raise runner_module.RunnerStopped("F10")

    monkeypatch.setattr(runner, "_run_job", run_job)

    runner.run()

    assert runner.outcome == "stopped"
    assert runner.termination_kind == "user_stopped"
    assert capture.of("run_total")[0].details["termination_kind"] == "user_stopped"


def test_p2_material_shortfall_maps_to_material_shortfall(monkeypatch):
    capture = _PerfCapture(monkeypatch)
    runner = _run_runner(monkeypatch, Preset())
    job = replace(runner._default_job(), accepted=("x",))
    monkeypatch.setattr(runner, "_jobs", lambda: (job,))

    def run_job(_job, _replay=None):
        raise runner_module.MaterialShortfall(0, 3, 1)

    monkeypatch.setattr(runner, "_run_job", run_job)

    runner.run()

    assert runner.outcome == "failed"
    assert runner.termination_kind == "material_shortfall"
    assert capture.of("run_total")[0].details["termination_kind"] == "material_shortfall"


def test_p2_generic_error_maps_to_error(monkeypatch):
    capture = _PerfCapture(monkeypatch)
    runner = _run_runner(monkeypatch, Preset())
    job = replace(runner._default_job(), accepted=("x",))
    monkeypatch.setattr(runner, "_jobs", lambda: (job,))

    def run_job(_job, _replay=None):
        raise RunnerError("坏了")

    monkeypatch.setattr(runner, "_run_job", run_job)

    runner.run()

    assert runner.outcome == "failed"
    assert runner.termination_kind == "error"


def test_p2_refund_two_waits_are_distinct_and_linked(monkeypatch, sim_time, tmp_path):
    """§7.1.1/§6.2.1：返还的首个 wait_total=interrupted，第二个是独立事件并带确认输入/父等待。"""
    import json

    import zzz_ddtool.scanperf as sp

    path = tmp_path / "perf.jsonl"
    session = sp.begin_session("t", run_id="R1", path=path)
    try:
        runner = Runner(Preset(ocr_retry=1), hotkeys=False)
        runner.trace = Mock()
        runner._disc_event_id = session.new_event_id()
        phase = {"refund": True}
        monkeypatch.setattr(
            runner, "_grab", lambda *_a, **_k: (image(), "drive_disc", "printwindow", 1)
        )
        monkeypatch.setattr(
            runner, "_parse_upgrade_page", lambda _img: None if phase["refund"] else disc(3)
        )
        monkeypatch.setattr(
            runner, "_find_material_refund",
            lambda _img: line("材料返还 确认") if phase["refund"] else None,
        )
        # 确认点击走 _dismiss_material_refund 的整窗 OCR，给它同一份返还行。
        patch_regions(monkeypatch, {None: [line("材料返还"), line("确认", (10, 10, 60, 40))]})

        def click(_hwnd, _x, _y):
            phase["refund"] = False   # 确认后回到强化页，第二次等待能读到真实盘面

        monkeypatch.setattr(runner_module, "click_client", click)
        runner._upgrade_started_at = sim_time.now()

        result, _frame = runner._await_upgrade_result(
            1, "R1C1", before=disc(0), expected_level=3, disc_slot=-1, stage_started=None,
        )
    finally:
        sp.end_session()

    assert result.level == 3
    lines = [json.loads(row) for row in path.read_text(encoding="utf-8").splitlines() if row]
    waits = [row for row in lines if row["component"] == "wait_total"]
    assert len(waits) == 2
    assert waits[0]["details"]["outcome"] == "interrupted"
    assert waits[0]["event_id"] != waits[1]["event_id"]
    second = waits[1]["details"]
    assert second["trigger_input_id"] == runner._refund_input_id
    assert second["trigger_input_id"] is not None
    assert second["resume_from_wait_id"] == waits[0]["event_id"]
    assert second["outcome"] == "succeeded"
    assert waits[0]["parent_id"] == runner._disc_event_id
    assert waits[1]["parent_id"] == runner._disc_event_id

