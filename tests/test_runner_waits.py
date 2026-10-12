"""§8 W1～W4、W7～W10 的 Runner 集成层直接断言。

规则（Spec §8）：用显式模拟时钟（``sim_time``）驱动真实等待引擎，不把 ``_wait_ready``
固定成「永远成功」；只替换窗口/截图/识别/输入接口，不发送任何真实游戏输入。
凡本文件把 ``_wait_ready`` 替换成替身的地方，都只针对「必须失败/必须抛中断」的分支，
真实等待引擎的边界由 ``tests/test_readiness.py`` 与本文件 W1/W2/W3 的真实轮询用例覆盖。
"""

from __future__ import annotations

import time as _time_module
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtGui import QImage
from unittest.mock import Mock, call

import zzz_ddtool.actionwait as actionwait
import zzz_ddtool.ocr as ocr_module
import zzz_ddtool.readiness as readiness
import zzz_ddtool.runner as runner_module
from zzz_ddtool.actionwait import InputRelease
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


def release(api: str = "click_client", at_ns: int | None = None) -> InputRelease:
    """替身输入释放回执；默认取当前（模拟）时钟，等价于「刚刚释放」。"""
    if at_ns is None:
        at_ns = int(_time_module.monotonic() * 1e9)
    return InputRelease(api, at_ns)


def click_stub(mock: Mock) -> Mock:
    """click_client 替身：记录调用并返回释放回执。"""
    mock.side_effect = lambda *args, **kwargs: release()
    return mock


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

    result = runner._wait_ready(
        "select_disc", release=InputRelease("click_client", int(sim_time.now() * 1e9)), cell=cell()
    )

    assert result.disc == target
    # 当前表值：选盘实测 320 ms + 200 ms 余量；550/600 ms 才得到两帧稳定目标。
    assert runner_module._READY_FLOOR_MS["select_disc"] == 520
    assert result.active_ms == pytest.approx(600)
    assert result.samples == 13
    assert frames.index == 13
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
        runner._wait_ready(
            "select_disc", release=InputRelease("click_client", int(sim_time.now() * 1e9)), cell=cell()
        )

    assert excinfo.value.samples >= 55  # 680 ms 下限不会让「一直不选中」提前成功
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
    click = click_stub(Mock())
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
    assert runner._material_release is not None
    assert runner._upgrade_release is not None
    assert click.call_count == 2          # 阶段添加 + 升级，各一次
    parse.assert_not_called()             # 不增加预计等级 OCR/Disc


def _grade_bar_frame(fill_rgb) -> QImage:
    """合成一帧：只有经验/预计等级条 ROI 有给定颜色，其余全黑。"""
    import zzz_ddtool.layout as layout_module

    left, top, right, bottom = layout_module.UPGRADE_LEVEL_BAR
    arr = np.zeros((1080, 1920, 4), dtype=np.uint8)
    arr[:, :, 3] = 255                                    # 不透明
    arr[top:bottom, left:right, 0] = fill_rgb[2]          # B
    arr[top:bottom, left:right, 1] = fill_rgb[1]          # G
    arr[top:bottom, left:right, 2] = fill_rgb[0]          # R
    return QImage(arr.tobytes(), 1920, 1080, QImage.Format.Format_RGB32).copy()


def test_w2_material_bar_descriptor_survives_fill_brightness_pulse():
    """§6.2 / §10：等级条描述子必须是「归一化饱和度」，不能是灰度。

    真机（2026-10-10 步骤 9，plan4 首盘）该条填充色上有一层**亮度呼吸动画**：
    旧的灰度逐格 key 每帧都变 → `wait_ready` 的稳定帧对永远凑不齐 →
    material_ready 3000ms 超时 → 整轮 RunnerError 失败（空条 54.6 与就绪暗相位
    94.8 的灰度差仅 1.7x，不足以做稳定 key）。守卫三件事：
    ① 空条为纯灰（无饱和度）→ 归一化后全 0；
    ② 同一填充结构的亮/暗相位给**同一个 key**，且差不超过稳定阈值；
    ③ 操作前→已填充达到变化阈值且 key 不同（「退回操作前状态则 ready=False」）。
    """
    runner = Runner(Preset(), hotkeys=False)
    blank = _grade_bar_frame((60, 60, 60))       # 操作前：空条＝纯灰
    bright = _grade_bar_frame((210, 225, 50))    # 已填充：黄绿（亮相位）
    dim = _grade_bar_frame((150, 165, 35))       # 同一填充的暗相位

    crop_blank = runner._material_bar_crop(blank)
    crop_bright = runner._material_bar_crop(bright)
    crop_dim = runner._material_bar_crop(dim)

    assert not crop_blank.any()                  # ① 无彩色 → 全 0
    assert crop_bright.any()

    # ② 亮度呼吸不改 key，也不超过稳定阈值
    assert runner._material_bar_key(crop_bright) == runner._material_bar_key(crop_dim)
    assert (
        runner._mean_abs_diff(crop_bright, crop_dim)
        <= runner_module._MATERIAL_BAR_STABLE_THRESHOLD
    )
    assert runner._material_bar_key(crop_bright) is not None

    # ③ 操作前 → 已填充：达到变化阈值，且 key 与空条不同
    assert (
        runner._mean_abs_diff(crop_blank, crop_bright)
        >= runner_module._MATERIAL_BAR_CHANGE_THRESHOLD
    )
    assert runner._material_bar_key(crop_blank) != runner._material_bar_key(crop_bright)
    assert (
        runner_module._MATERIAL_BAR_CHANGE_THRESHOLD
        > runner_module._MATERIAL_BAR_STABLE_THRESHOLD
    )


def test_w2_material_wait_starts_from_input_release(monkeypatch, sim_time):
    """§4.3：material_ready 的等待起点必须是素材点击的真实释放点，不是点击返回时刻。"""
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    clock = sim_time
    released: list[float] = []

    def click(_hwnd, _x, _y):
        released.append(clock.now())
        clock.t = 1.100  # 点击返回到调用方时时钟已走到 1.100 s
        return InputRelease("click_client", 1_000_000_000)  # 真实释放点 1.000 s

    monkeypatch.setattr(runner_module, "click_client", click)
    frames = Frames([{"image": image()}])
    bind_grab(monkeypatch, runner, frames)
    waits: list[InputRelease] = []

    def fake_wait(kind, *, release, **_kwargs):
        assert kind == "material_ready"
        waits.append(release)
        raise timeout("素材未就绪")

    monkeypatch.setattr(runner, "_wait_ready", fake_wait)
    patch_regions(monkeypatch, {Runner.STAGE_ADD_AREA: [line("阶段添加")]})

    clock.t = 1.000
    with pytest.raises(RunnerError, match="素材准备就绪超时"):
        runner._upgrade_once(1, 0, before=disc(0))

    assert released == [1.000]
    assert runner._material_release == InputRelease("click_client", 1_000_000_000)
    assert waits == [InputRelease("click_client", 1_000_000_000)]
    assert runner._upgrade_release is None      # 升级输入 0
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
    runner._upgrade_release = InputRelease("click_client", int(sim_time.now() * 1e9))
    before = disc(0)

    result, frame = runner._await_upgrade_result(
        1, "R1C1", before=before, expected_level=next_stage_level(before.level),
        disc_slot=-1, stage_started=None,
    )

    assert result.level == 3
    assert frame is not None
    assert frames.grabs[-1]["disc"].level == 3
    assert runner._upgrade_release is None


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
    click = click_stub(Mock())
    monkeypatch.setattr(runner_module, "click_client", click)
    refund_frame = image()
    result_frame = image()
    patch_regions(monkeypatch, {None: [line("材料返还"), line("确认", (10, 10, 60, 40))]})
    calls: list[tuple] = []

    def fake_wait(kind, *, release, **kwargs):
        calls.append((kind, release, kwargs))
        if len([c for c in calls if c[0] == "upgrade_result"]) == 1:
            raise _interrupted(refund_frame, wait_id="wait-1")
        return WaitResult(frame=result_frame, disc=disc(3), active_ms=0.0, samples=1, pause_generation=None)

    monkeypatch.setattr(runner, "_wait_ready", fake_wait)
    runner._upgrade_release = InputRelease("click_client", int(sim_time.now() * 1e9))
    runner._upgrade_input_id = "input-upgrade"

    result, frame = runner._await_upgrade_result(
        1, "R1C1", before=disc(0), expected_level=3, disc_slot=-1, stage_started=None,
    )

    assert result.level == 3
    assert frame is result_frame
    assert click.call_count == 1                      # 只点一次「确认」，绝不重发升级
    assert calls[1][0] == "upgrade_result"
    assert calls[1][1] is runner._refund_release       # 第二次等待起点 = 确认输入的释放回执
    assert calls[1][2]["action_id"] == "refund_confirm"  # 动作ID随路径取值
    assert calls[1][2]["trigger_input_id"] == runner._refund_input_id
    assert calls[1][2]["resume_from_wait_id"] == "wait-1"
    assert runner._upgrade_input_id == "input-upgrade"


def test_w4_second_result_abnormal_stops_without_second_confirm(monkeypatch, sim_time):
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    click = click_stub(Mock())
    monkeypatch.setattr(runner_module, "click_client", click)
    refund_frame = image()
    patch_regions(monkeypatch, {None: [line("材料返还"), line("确认", (10, 10, 60, 40))]})
    seen = {"n": 0}

    def fake_wait(kind, *, release, **kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            raise _interrupted(refund_frame, wait_id="wait-1")
        raise timeout("返还后仍未就绪")

    monkeypatch.setattr(runner, "_wait_ready", fake_wait)
    runner._upgrade_release = InputRelease("click_client", int(sim_time.now() * 1e9))

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
    runner._upgrade_release = InputRelease("click_client", int(sim_time.now() * 1e9))

    with pytest.raises(RunnerError, match="升级结果无法确认"):
        runner._await_upgrade_result(
            1, "R1C1", before=disc(0), expected_level=3, disc_slot=-1, stage_started=None,
        )

    assert click.call_count == 0


def _generation_interrupted(frame, pause_generation):
    sample = WaitSample(
        frame=frame, captured_at=0.0, ready=False, state_key=("upgrade_result", "refund"),
        evidence={"refund": {"text": "材料返还确认"}}, interrupt_reason="material_refund",
    )
    return ReadinessInterrupted(
        "material_refund", last_sample=sample, active_ms=0.0, samples=2,
        pause_generation=pause_generation,
    )


def test_w4_refund_confirmation_stops_on_pause_generation_change(monkeypatch, sim_time):
    """§6.2.1/W4：中断帧属于旧暂停代次 → 接续失效，确认输入前停止，不按旧截图坐标点击。"""
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    controller = Mock(paused=False)  # 暂停已恢复，但中断帧来自代次 0、当前代次 1
    controller.checkpoint = Mock(return_value=1)
    runner.hotkeys = controller
    click = Mock()
    monkeypatch.setattr(runner_module, "click_client", click)
    refund_frame = image()
    patch_regions(monkeypatch, {None: [line("材料返还"), line("确认", (10, 10, 60, 40))]})

    with pytest.raises(RunnerError, match="接续失效"):
        runner._resume_after_refund(
            1, "R1C1", _generation_interrupted(refund_frame, 0),
            before=disc(0), expected_level=3, disc_slot=-1, stage_started=None,
        )

    click.assert_not_called()  # 危险点击必须不发生


def test_w4_refund_confirmation_allows_same_pause_generation(monkeypatch, sim_time):
    """§6.2.1/W4：中断帧代次与当前一致 → 接续有效，正常确认一次并重建第二次结果等待。"""
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    controller = Mock(paused=False)
    controller.checkpoint = Mock(return_value=0)
    runner.hotkeys = controller
    click = Mock()
    monkeypatch.setattr(runner_module, "click_client", click)
    refund_frame = image()
    result_frame = image()
    patch_regions(monkeypatch, {None: [line("材料返还"), line("确认", (10, 10, 60, 40))]})
    monkeypatch.setattr(
        runner, "_wait_ready",
        Mock(return_value=WaitResult(frame=result_frame, disc=disc(3), active_ms=0.0,
                                     samples=1, pause_generation=0)),
    )

    result, frame = runner._resume_after_refund(
        1, "R1C1", _generation_interrupted(refund_frame, 0),
        before=disc(0), expected_level=3, disc_slot=-1, stage_started=None,
    )

    assert result.level == 3
    assert frame is result_frame
    click.assert_called_once()


@pytest.mark.parametrize("boundary", [1, 2])
@pytest.mark.parametrize("change", ["generation", "window", "size", "job"])
def test_refund_invalidated_at_real_click_or_focus_checkpoint_sends_no_mouse(
    monkeypatch, sim_time, boundary, change,
):
    """真实 click/focus 检查点改变绑定；不是仅模拟进入 resume 时的旧代次。"""
    from types import SimpleNamespace
    from zzz_ddtool import wininput
    from zzz_ddtool.control import controlled_inputs

    generation = {"value": 0}
    size = {"value": (1920, 1080)}
    controller = SimpleNamespace(paused=False, checkpoint=lambda: generation["value"])
    runner = Runner(Preset(), controller=controller, hotkeys=False)
    runner.trace = Mock()
    runner._hwnd, runner._pid = 1, 123
    runner._state = SimpleNamespace(job_id="j1", next_index=0)
    monkeypatch.setattr(runner_module, "client_size", lambda _h: size["value"])
    monkeypatch.setattr(runner_module, "window_pid", lambda _h: 123)
    native = Mock()
    monkeypatch.setattr(wininput, "user32", native)
    monkeypatch.setattr(wininput, "kernel32", Mock())
    monkeypatch.setattr(wininput, "is_foreground", lambda _h: True)
    monkeypatch.setattr(wininput, "client_to_screen", lambda _h, x, y: (x, y))
    read = Mock(return_value=[line("材料返还"), line("确认")])
    monkeypatch.setattr(ocr_module, "read_rect", read)
    original = wininput.checkpoint_input
    count = 0

    def checkpoint():
        nonlocal count
        count += 1
        if count == boundary:
            if change == "generation":
                generation["value"] = 2
            elif change == "window":
                runner._hwnd = 2
            elif change == "size":
                size["value"] = (1280, 720)
            else:
                runner._state.job_id = "j2"
        original()

    monkeypatch.setattr(wininput, "checkpoint_input", checkpoint)
    with controlled_inputs(runner._checkpoint), pytest.raises(RunnerError, match="接续失效"):
        runner._resume_after_refund(
            1, "R1C1", _generation_interrupted(image(), 0), before=disc(),
            expected_level=3, disc_slot=-1, stage_started=None,
        )
    native.mouse_event.assert_not_called()
    native.SetCursorPos.assert_not_called()
    assert read.call_count == 1


# --------------------------------------------------------------------------
# W7：return_warehouse 逐层 Esc 循环
# --------------------------------------------------------------------------
def _layer_runner(monkeypatch, sim_time, layers: dict, min_wait_ms: int):
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    esc = Mock(side_effect=lambda *a, **k: release("press_key_once"))
    monkeypatch.setattr(runner_module, "press_key_once", esc)
    monkeypatch.setattr(runner, "_return_layer", lambda img: layers[id(img)])
    if min_wait_ms != runner_module._READY_FLOOR_MS["return_warehouse"]:
        monkeypatch.setattr(
            runner, "_wait_policy",
            lambda kind: actionwait.ActionWaitPolicy(min_wait_ms, "default"),
        )
    return runner, esc


def test_w7_return_to_warehouse_single_esc_real_wait_default_bound(monkeypatch, sim_time):
    modal, warehouse = image(), image()
    runner, esc = _layer_runner(monkeypatch, sim_time, {id(modal): "modal", id(warehouse): "warehouse"},
                                runner_module._READY_FLOOR_MS["return_warehouse"])
    frames = Frames([{"image": modal}] * 9 + [{"image": warehouse}])
    bind_grab(monkeypatch, runner, frames)

    assert runner._esc_back_to_grid(1) is True
    assert esc.call_count == 1
    # 当前表值：返回实测 1411 ms + 200 ms 余量；1650/1700 ms 得到两帧稳定目标。
    # 加上进入循环前的那一次认层抓帧，共 36 次。
    assert runner_module._READY_FLOOR_MS["return_warehouse"] == 1611
    assert sim_time.now() == pytest.approx(10_001.7)
    assert frames.index == 36


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
    press = Mock(side_effect=lambda *a, **k: release("press_key_once"))
    monkeypatch.setattr(runner_module, "press_key_once", press)
    monkeypatch.setattr(runner_module.time, "sleep", lambda _s: None)
    # §4.3 标签释放下限由 wait_release 消费；本组用例验按键/复核语义，替换掉计时。
    monkeypatch.setattr(runner_module, "wait_release", lambda *args, **kwargs: None)
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


def test_w8_first_label_check_has_no_extra_50ms_gap(monkeypatch, sim_time):
    """§4.3/T11：第一次标签复核不再叠加 50 ms（由释放总下限覆盖）；后续失败观察仍每 50 ms 一次。"""
    runner = Runner(Preset(skip_locked=False, ocr_retry=2), hotkeys=False)
    runner.trace = Mock()
    times: list[float] = []

    def read(*_args, **_kwargs):
        times.append(sim_time.now())
        return {"locked": False, "discarded": None}, image()

    monkeypatch.setattr(runner, "_read_label_state", read)

    start = sim_time.now()
    with pytest.raises(RunnerError, match="无法确认"):
        runner._verify_label(
            1, key="discarded", value=True, other_value=False, context=_LabelContext(None, None)
        )

    assert len(times) == 8
    assert times[0] == start                                          # 首次复核前不睡 50 ms
    assert [round(b - a, 3) for a, b in zip(times, times[1:])] == [0.05] * 7


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
    click = click_stub(Mock())
    monkeypatch.setattr(runner_module, "click_client", click)
    monkeypatch.setattr(runner, "_esc_back_to_grid", Mock())
    bind_grab(monkeypatch, runner, Frames([{"image": image()}]))
    patch_regions(monkeypatch, {Runner.UPGRADE_BTN_AREA: [line("查看", (1560, 800, 1700, 860))]})
    return runner, click


def test_w9_open_preview_timeout_does_not_resend_detail_button(monkeypatch):
    """提速改造（2026-10-12）：open_preview 就绪轮询已移除（固定等待 + 硬编码箭头坐标），
    但「详情按钮只发一次、失败不盲目重发输入」的保证仍在：箭头超时后先重新定位，
    定位不到（强化页没进来、预览丢失）就不再发箭头，Esc 回仓库。"""
    runner, click = _upgrade_page_runner(monkeypatch)
    monkeypatch.setattr(runner, "_wait_ready", Mock(side_effect=timeout("箭头超时")))

    assert runner._enter_upgrade_page(1, before=disc(3)) is False
    assert click.call_count == 2      # 详情按钮 + 第一次箭头；重试定位不到不再发
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
    """提速改造（2026-10-12）：首次箭头坐标为硬编码，`_zoom_arrow_of` 只在重试前
    重新定位时调用；预览丢失（定位不到「等级」行）→ 不发第二次箭头。"""
    runner, click = _upgrade_page_runner(monkeypatch)

    def fake_wait(kind, **_kwargs):
        if kind == "open_preview":
            return WaitResult(frame=image(), disc=None, active_ms=0.0, samples=2, pause_generation=None)
        raise timeout("箭头后未进强化页")

    monkeypatch.setattr(runner, "_wait_ready", fake_wait)
    monkeypatch.setattr(runner, "_zoom_arrow_of", Mock(side_effect=[None]))

    assert runner._enter_upgrade_page(1, before=disc(3)) is False
    assert click.call_count == 2      # 详情按钮 + 第一次箭头，重试定位失败不再发
    runner._esc_back_to_grid.assert_called_once_with(1)


def test_w9_open_upgrade_probe_ignores_unknown_rarity_on_upgrade_page(monkeypatch):
    """提速改造（2026-10-12）：open_upgrade 就绪判定 = 固定坐标「阶段添加」锚点命中，
    不再解析盘面、不再比对对象（原 rarity=None 卡死问题随旧探针一并移除）。

    历史（2026-10-10 真机标定）：旧探针按含稀有度的 ``_core_key`` 全字段比较，
    强化页 rarity 恒为 None 而 before 为 S → 真机 5/5 超时；该教训的现行等价物是
    「就绪只看锚点，与 before 盘面完全解耦」。
    """
    runner = Runner(Preset(), hotkeys=False)
    runner.trace = Mock()
    bind_grab(monkeypatch, runner, Frames([{"image": image()}]))
    before = Disc(12, (Substat("暴击率", 1), Substat("攻击力", 1)), rarity="S")

    # 锚点命中 → 就绪（无论 before 是什么盘面）
    monkeypatch.setattr(runner_module, "is_upgrade_stage_add", lambda _frame: True)
    assert runner._probe_open_upgrade(before)().ready is True

    # 锚点未命中（页面没到/还没渲染完）→ 不就绪
    monkeypatch.setattr(runner_module, "is_upgrade_stage_add", lambda _frame: False)
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
            from zzz_ddtool.wininput import _observe_input
            with _observe_input("click_client", hwnd=_hwnd, x=_x, y=_y):
                phase["refund"] = False   # 逻辑输入边界保留真实观察，系统输入为替身
            return InputRelease("click_client", int(sim_time.now() * 1e9))

        monkeypatch.setattr(runner_module, "click_client", click)
        runner._upgrade_release = InputRelease("click_client", int(sim_time.now() * 1e9))

        from zzz_ddtool import wininput
        monkeypatch.setattr(runner_module, "client_size", lambda _h: (1920, 1080))
        monkeypatch.setattr(runner_module, "window_pid", lambda _h: 123)
        monkeypatch.setattr(wininput.time, "monotonic_ns", lambda: int(sim_time.now()*1e9))
        with wininput.capture_inputs(runner._observe_execution_input):
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


def test_wait_table_in_core_doc_syncs_with_code():
    """核心文档的「代码里设置的等待」总表必须与源码一致（作者裁定，最高优先级）。

    脱节时这里失败：运行
    `python dev-docs/功能改造/操作等待提速/scripts/dump_wait_table.py --write <README.md>` 同步。
    """
    import sys as _sys
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    scripts = root / "dev-docs" / "功能改造" / "操作等待提速" / "scripts"
    if str(scripts) not in _sys.path:
        _sys.path.insert(0, str(scripts))
    import dump_wait_table as dumper

    readme = root / "dev-docs" / "功能改造" / "操作等待提速" / "等待时间表.md"
    text = readme.read_text(encoding="utf-8")
    start, end = text.find(dumper.BEGIN), text.find(dumper.END)
    assert start >= 0 and end >= 0, "等待时间表缺 wait-table 标记块"
    block = text[start:end + len(dumper.END)]
    assert block.strip() == dumper.render_block().strip(), (
        "等待时间表与代码不一致：运行 scripts/dump_wait_table.py --write 同步"
    )

