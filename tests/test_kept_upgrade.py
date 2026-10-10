"""升满保留标签和统计；所有窗口、截图与原生输入均替身，不触碰游戏。"""

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtGui import QImage

from zzz_ddtool import runner as rm, wininput
from zzz_ddtool.config import LabelPolicy, Preset
from zzz_ddtool.capture import CaptureError
from zzz_ddtool.control import checkpoint_input, controlled_inputs
from zzz_ddtool.hotkeys import RunnerStopped
from zzz_ddtool.models import Action, Decision, Disc, Substat
from zzz_ddtool.ocr import OcrError, parse_disc
from zzz_ddtool.runner import Runner, RunnerError


def kept_disc(level=15, **kwargs):
    return Disc(level, (Substat("暴击率", 1 + level // 3), Substat("暴击伤害"),
                        Substat("攻击力"), Substat("攻击力%")), **kwargs)


@pytest.fixture
def setup(monkeypatch, fast_wait):
    runner = Runner(Preset(ocr_retry=2, weights={"暴击率": 1.0}), hotkeys=False)
    runner.trace = Mock()
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    monkeypatch.setattr(runner, "_grab", Mock(return_value=(image, "drive_disc", "test", 1)))

    def fake_upgrade_once(hwnd, material_slot=0, *, before=None):
        # 替身升级输入：按 §6.2 记录升级时刻，供 _await_upgrade_result 使用。
        runner._upgrade_started_at = rm.time.monotonic()
        return True

    monkeypatch.setattr(runner, "_upgrade_once", fake_upgrade_once)
    back = Mock(return_value=True)
    monkeypatch.setattr(runner, "_esc_back_to_grid", back)
    monkeypatch.setattr(rm.time, "sleep", lambda _: None)
    state = [kept_disc()]
    parser = Mock(side_effect=lambda _image: state[0])
    monkeypatch.setattr(rm, "parse_disc", parser)
    monkeypatch.setattr(rm, "is_page", lambda *_: True)
    monkeypatch.setattr(rm, "read_action_state_fast",
                        lambda _: {"locked": state[0].locked, "discarded": state[0].discarded})
    keys = []

    def press(hwnd, vk):
        checkpoint_input()  # 输入入口
        checkpoint_input()  # focus_window 内部边界
        keys.append(vk)
        state[0] = replace(state[0], locked=True)

    monkeypatch.setattr(rm, "press_key_once", press)
    return runner, state, parser, keys, back


def test_unlocked_kept_disc_locks_once_before_any_restore(setup):
    runner, state, parser, keys, back = setup
    runner._finish_kept_upgrade(1, kept_disc())
    assert keys == [wininput.VK_T]
    assert state[0].locked is True
    back.assert_called_once_with(1)
    assert parser.call_count == 3  # 初始、两个输入边界；动作后只读标签


def test_already_locked_disc_is_not_toggled(setup, monkeypatch):
    runner, state, parser, keys, _ = setup
    state[0] = kept_disc(locked=True)
    sleep = Mock()
    monkeypatch.setattr(rm.time, "sleep", sleep)
    before_stats = runner.stats.summary()
    runner._finish_kept_upgrade(1, kept_disc())
    assert keys == [] and parser.call_count == 1
    sleep.assert_not_called()
    assert runner.stats.summary() == before_stats


def test_save_disabled_adds_no_ocr_or_key(setup):
    runner, _, parser, keys, _ = setup
    runner.preset = replace(runner.preset, labels=LabelPolicy(save=False))
    runner._finish_kept_upgrade(1, kept_disc())
    assert keys == []
    parser.assert_not_called()


@pytest.mark.parametrize("changed", [None, kept_disc(locked=None), kept_disc(12)])
def test_unknown_or_different_disc_is_not_locked(setup, changed):
    runner, state, parser, keys, _ = setup
    state[0] = changed
    with pytest.raises(RunnerError):
        runner._finish_kept_upgrade(1, kept_disc())
    assert keys == [] and parser.call_count <= 2


@pytest.mark.parametrize("boundary", [1, 2])
def test_pause_changed_disc_at_input_boundary_sends_no_key(setup, boundary):
    runner, state, _, keys, _ = setup
    calls = [0]

    def checkpoint():
        calls[0] += 1
        if calls[0] == boundary:
            state[0] = kept_disc(12)

    with controlled_inputs(checkpoint), pytest.raises(RunnerError, match="不一致"):
        runner._finish_kept_upgrade(1, kept_disc())
    assert keys == []


def test_stop_at_input_boundary_is_preserved(setup):
    runner, _, _, keys, _ = setup
    with controlled_inputs(Mock(side_effect=RunnerStopped("stop"))), pytest.raises(RunnerStopped):
        runner._finish_kept_upgrade(1, kept_disc())
    assert keys == []


def test_lock_failure_does_not_toggle_a_second_time(setup, monkeypatch):
    runner, _, _, keys, _ = setup
    def no_effect(hwnd, vk):
        checkpoint_input()
        keys.append(vk)
    monkeypatch.setattr(rm, "press_key_once", no_effect)
    with pytest.raises(RunnerError, match="不重复按键"):
        runner._finish_kept_upgrade(1, kept_disc())
    assert keys == [wininput.VK_T]


@pytest.mark.parametrize("save", [False, True])
def test_upgraded_keep_is_counted_as_success_even_when_save_disabled(setup, monkeypatch, save):
    runner, _, _, _, _ = setup
    runner.preset = replace(runner.preset, labels=LabelPolicy(save=save))
    finish = Mock()
    monkeypatch.setattr(runner, "_finish_kept_upgrade", finish)
    monkeypatch.setattr(runner, "_parse_upgrade_page", Mock(return_value=kept_disc()))
    job = runner._default_job()
    index = runner.stats.disc_begin(job.job_id, 0, 12)
    assert runner._upgrade_loop(1, job, 0, "R1C1", runner.preset, None, {}, index,
                                initial_disc=kept_disc(12)) == 15
    finish.assert_called_once()
    assert runner.stats.discs[index].outcome == "upgraded"
    assert runner.stats.summary()["max_level_kept"] == 1


def test_keep_label_failure_is_not_counted_as_success(setup, monkeypatch):
    runner, _, _, _, _ = setup
    monkeypatch.setattr(runner, "_finish_kept_upgrade", Mock(side_effect=RunnerError("lock failed")))
    monkeypatch.setattr(runner, "_parse_upgrade_page", Mock(return_value=kept_disc()))
    job = runner._default_job()
    index = runner.stats.disc_begin(job.job_id, 0, 12)
    with pytest.raises(RunnerError, match="lock failed"):
        runner._upgrade_loop(1, job, 0, "R1C1", runner.preset, None, {}, index,
                             initial_disc=kept_disc(12))
    assert runner.stats.discs[index].outcome == "failed"
    assert runner.stats.summary()["max_level_kept"] == 0


def test_originally_max_level_remains_skipped(setup, monkeypatch):
    runner, _, _, keys, _ = setup
    finish = Mock()
    monkeypatch.setattr(runner, "_finish_kept_upgrade", finish)
    job = runner._default_job()
    index = runner.stats.disc_begin(job.job_id, 0, 15)
    runner._upgrade_loop(1, job, 0, "R1C1", runner.preset, None, {}, index, initial_disc=kept_disc())
    finish.assert_not_called()
    assert runner.stats.discs[index].outcome == "skipped" and keys == []


def test_real_kept_finish_runs_inside_upgrade_loop_and_counts_only_after_lock(setup, monkeypatch):
    runner, state, _, keys, _ = setup
    monkeypatch.setattr(runner, "_parse_upgrade_page", Mock(return_value=kept_disc()))
    job = runner._default_job()
    index = runner.stats.disc_begin(job.job_id, 0, 12)
    runner._upgrade_loop(1,job,0,"R1C1",runner.preset,None,{},index,initial_disc=kept_disc(12))
    assert keys == [wininput.VK_T] and state[0].locked is True
    assert runner.stats.summary()["max_level_kept"] == 1


def test_ocr_error_propagates_immediately_without_retry(setup):
    """§6.1.1：保留锁定盘面复核抛 OcrError 立即原样传播，不重读、不发后续 T/R。"""
    runner, _, parser, keys, _ = setup
    parser.side_effect = OcrError("OCR unavailable")
    with pytest.raises(OcrError):
        runner._finish_kept_upgrade(1, kept_disc())
    assert parser.call_count == 1 and keys == []


def test_unknown_after_t_fails_without_second_t(setup, monkeypatch):
    runner, state, parser, keys, _ = setup
    def press(hwnd, vk):
        checkpoint_input()
        keys.append(vk)
        state[0] = kept_disc(locked=None)
    monkeypatch.setattr(rm, "press_key_once", press)
    with pytest.raises(RunnerError, match="无法确认"):
        runner._finish_kept_upgrade(1, kept_disc())
    assert keys == [wininput.VK_T] and parser.call_count == 2  # 按后不读整盘


def test_level_fifteen_final_discard_uses_original_discard_path(setup, monkeypatch):
    runner, _, _, keys, _ = setup
    finish, discard = Mock(), Mock()
    monkeypatch.setattr(runner, "_finish_kept_upgrade", finish)
    monkeypatch.setattr(runner, "_discard_from_upgrade", discard)
    monkeypatch.setattr(runner, "_parse_upgrade_page", Mock(return_value=kept_disc()))
    decisions = iter([Decision(Action.UPGRADE, "test_upgrade", 1, 1, 4, 12),
                      Decision(Action.MARK_DISCARD, "test_discard", 0, 0, 4, 15)])
    monkeypatch.setattr(rm, "decide", lambda *args, **kwargs: next(decisions))
    job = runner._default_job()
    index = runner.stats.disc_begin(job.job_id, 0, 12)
    runner._upgrade_loop(1, job, 0, "R1C1", runner.preset, None, {}, index,
                         initial_disc=kept_disc(12))
    finish.assert_not_called()
    discard.assert_called_once_with(1)
    assert keys == [] and runner.stats.discs[index].outcome == "discarded"
    assert runner.stats.summary()["max_level_kept"] == 0


def test_upgrade_discard_failure_is_not_counted_as_success(setup, monkeypatch):
    runner, _, _, _, _ = setup
    monkeypatch.setattr(runner, "_discard_from_upgrade", Mock(side_effect=RunnerError("discard failed")))
    monkeypatch.setattr(runner, "_parse_upgrade_page", Mock(return_value=kept_disc()))
    decisions = iter([Decision(Action.UPGRADE, "test_upgrade", 1, 1, 4, 12),
                      Decision(Action.MARK_DISCARD, "test_discard", 0, 0, 4, 15)])
    monkeypatch.setattr(rm, "decide", lambda *args, **kwargs: next(decisions))
    job = runner._default_job()
    index = runner.stats.disc_begin(job.job_id, 0, 12)
    with pytest.raises(RunnerError, match="discard failed"):
        runner._upgrade_loop(1, job, 0, "R1C1", runner.preset, None, {}, index,
                             initial_disc=kept_disc(12))
    assert runner.stats.discs[index].outcome == "failed"
    assert runner.stats.summary()["discard_labels"] == 0


@pytest.mark.parametrize("error", [CaptureError, OcrError, wininput.InputError])
def test_finish_transport_errors_are_counted_failed(setup, monkeypatch, error):
    runner, _, _, _, _ = setup
    monkeypatch.setattr(runner, "_finish_kept_upgrade", Mock(side_effect=error("backend")))
    monkeypatch.setattr(runner, "_parse_upgrade_page", Mock(return_value=kept_disc()))
    job = runner._default_job()
    index = runner.stats.disc_begin(job.job_id, 0, 12)
    with pytest.raises(RunnerError, match="收尾失败"):
        runner._upgrade_loop(1, job, 0, "R1C1", runner.preset, None, {}, index,
                             initial_disc=kept_disc(12))
    assert runner.stats.discs[index].outcome == "failed"
    assert runner.stats.summary()["max_level_kept"] == 0


def test_archived_disc_data_keeps_upgrade_label_state_unknown():
    base = Path(__file__).resolve().parents[1] / "手册/参考图/驱动盘详情"
    # 完整盘面图没有仓库侧栏的文字提示，不能把未知当未锁定；收尾必须先回侧栏。
    for filename, level, locked in [("升满保留-初始未锁定.png", 0, False),
                                    ("升满保留-15级未锁定.png", 15, None)]:
        image = QImage(str(base / filename))
        assert not image.isNull() and (image.width(), image.height()) == (1920, 1080)
        disc = parse_disc(image)
        assert disc is not None and disc.level == level and disc.locked is locked
