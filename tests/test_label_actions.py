"""标签转换真实编排；仅窗口、截图、识别及原生输入由替身隔离。"""
from dataclasses import replace
from unittest.mock import Mock

import pytest
from PySide6.QtGui import QImage

from zzz_ddtool import runner as rm
from zzz_ddtool.config import LabelPolicy, Preset
from zzz_ddtool.control import checkpoint_input, controlled_inputs
from zzz_ddtool.hotkeys import RunnerStopped
from zzz_ddtool.models import Action, Disc, Substat


@pytest.fixture
def labels(monkeypatch):
    runner = rm.Runner(Preset(skip_locked=False, ocr_retry=2), hotkeys=False)
    runner.trace = Mock()
    state = [Disc(3, (Substat("暴击率"), Substat("攻击力"), Substat("生命值")), locked=True)]
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(0)
    generation, stopped, keys = [0], [False], []

    def checkpoint():
        if stopped[0]:
            raise RunnerStopped("stop")
        return generation[0]

    monkeypatch.setattr(runner, "_checkpoint", checkpoint)
    monkeypatch.setattr(runner, "_grab", Mock(return_value=(image, "unknown", "test", 1)))
    monkeypatch.setattr(runner, "_esc_back_to_grid", Mock())
    monkeypatch.setattr(rm, "is_page", lambda *_: True)
    monkeypatch.setattr(rm.time, "sleep", lambda _: None)
    reader = Mock(side_effect=lambda _: {"locked": state[0].locked, "discarded": state[0].discarded})
    parser = Mock(side_effect=lambda _: state[0])
    monkeypatch.setattr(rm, "read_action_state_fast", reader)
    monkeypatch.setattr(rm, "parse_disc", parser)

    def press(hwnd, vk):
        checkpoint_input()
        checkpoint_input()  # 聚焦前仍检查，暂停改变会触发复验。
        keys.append(vk)
        key = "locked" if vk == rm.VK_T else "discarded"
        state[0] = replace(state[0], **{key: not getattr(state[0], key)})

    monkeypatch.setattr(rm, "press_key_once", press)
    # §4.3 标签释放下限由 wait_release 消费；本组用例验按键/复核语义，替换掉计时。
    monkeypatch.setattr(rm, "wait_release", lambda *args, **kwargs: None)
    return runner, state, generation, stopped, keys, reader, parser, press


@pytest.mark.parametrize("upgraded", [False, True])
@pytest.mark.parametrize("save", [False, True])
def test_locked_discard_both_entries_unlock_then_discard(labels, upgraded, save):
    runner, state, _, _, keys, reader, parser, _ = labels
    runner.preset = replace(runner.preset, labels=LabelPolicy(save=save))
    runner._detail_disc = state[0]
    with controlled_inputs(runner._checkpoint):
        if upgraded:
            runner._discard_from_upgrade(1)
        else:
            runner._act(1, Action.MARK_DISCARD, state[0])
    assert keys == [rm.VK_T, rm.VK_R]
    assert (state[0].locked, state[0].discarded) == (False, True)
    parser.assert_not_called()  # 正常按后从不重读副词条。
    assert reader.call_count >= 4
    assert runner.stats.summary()["discard_labels"] == 1


def test_discard_marking_disabled_still_unlocks_and_never_sends_r(labels):
    """「判断后打上弃置标记」关闭：锁定盘仍照旧先按下解锁，但不按 R、不计弃置标记。"""
    runner, state, _, _, keys, reader, parser, _ = labels
    runner.preset = replace(runner.preset, labels=LabelPolicy(discard=False))
    with controlled_inputs(runner._checkpoint):
        runner._act(1, Action.MARK_DISCARD, state[0])
    assert keys == [rm.VK_T]
    assert (state[0].locked, state[0].discarded) == (False, False)
    assert runner.stats.summary()["discard_labels"] == 0
    assert [item["kind"] for item in runner._label_inputs] == ["label_unlock_before_discard"]
    parser.assert_not_called()


def test_discard_marking_disabled_leaves_unlocked_disc_untouched(labels):
    """开关关闭且盘没锁：不读标签、不按键——没有要发的键就不做无谓判定。"""
    runner, state, _, _, keys, reader, _, _ = labels
    state[0] = replace(state[0], locked=False)
    runner.preset = replace(runner.preset, labels=LabelPolicy(discard=False))
    changed = runner._set_discard_label(1, discarded=False, value=True, before=state[0])
    assert changed is False and keys == []
    reader.assert_not_called()


def test_discard_marking_disabled_is_not_stopped_by_unreadable_state(labels):
    """只为解锁读数：读不准就不按 T，也不为一个不发的键停链。"""
    runner, state, _, _, keys, reader, _, _ = labels
    runner.preset = replace(runner.preset, labels=LabelPolicy(discard=False))
    reader.side_effect = lambda _: {"locked": None, "discarded": None}
    changed = runner._set_discard_label(1, discarded=False, value=True, before=state[0])
    assert changed is False and keys == []


def test_discard_marking_disabled_keeps_clearing_mark_before_upgrade(labels):
    """摘标记（进强化页前那一步）属于强化流程，开关关闭也照常按 R 执行。"""
    runner, state, _, _, keys, _, _, _ = labels
    state[0] = replace(state[0], locked=False, discarded=True)
    runner.preset = replace(runner.preset, labels=LabelPolicy(discard=False))
    with controlled_inputs(runner._checkpoint):
        changed = runner._set_discard_label(1, discarded=True, value=False, before=state[0])
    assert keys == [rm.VK_R]
    assert state[0].discarded is False
    assert changed is True


@pytest.mark.parametrize("discarded,value,expected", [(False, True, [rm.VK_R]),
                                                     (True, False, [rm.VK_R]),
                                                     (True, True, [])])
def test_set_or_remove_discard_uses_current_state(labels, monkeypatch, discarded, value, expected):
    runner, state, _, _, keys, _, _, _ = labels
    state[0] = replace(state[0], locked=False, discarded=discarded)
    sleep = Mock()
    monkeypatch.setattr(rm.time, "sleep", sleep)
    with controlled_inputs(runner._checkpoint):
        changed = runner._set_discard_label(1, discarded=not discarded, value=value, before=state[0])
    assert keys == expected  # 传入的旧状态不能导致反向切换。
    assert state[0].discarded is value
    assert changed is bool(expected)
    assert runner.stats.summary()["discard_labels"] == int(bool(expected) and value)
    if not expected:
        sleep.assert_not_called()


@pytest.mark.parametrize("effect", ["none", "unknown"])
def test_failed_unlock_never_sends_r(labels, monkeypatch, effect):
    runner, state, _, _, keys, _, _, _ = labels
    def press(hwnd, vk):
        checkpoint_input()
        keys.append(vk)
        if effect == "unknown":
            state[0] = replace(state[0], locked=None)
    monkeypatch.setattr(rm, "press_key_once", press)
    with controlled_inputs(runner._checkpoint), pytest.raises(rm.RunnerError, match="不重复按键"):
        runner._act(1, Action.MARK_DISCARD, state[0])
    assert keys == [rm.VK_T]
    assert runner.stats.summary()["discard_labels"] == 0


def test_failed_r_is_not_counted_or_retried(labels, monkeypatch):
    runner, state, _, _, keys, _, _, _ = labels
    state[0] = replace(state[0], locked=False)
    monkeypatch.setattr(rm, "press_key_once", lambda hwnd, vk: (checkpoint_input(), keys.append(vk)))
    with controlled_inputs(runner._checkpoint), pytest.raises(rm.RunnerError, match="不重复按键"):
        runner._act(1, Action.MARK_DISCARD, state[0])
    assert keys == [rm.VK_R] and runner.stats.summary()["discard_labels"] == 0


@pytest.mark.parametrize("changed", [False, True])
def test_pause_after_unlock_resumes_without_repeating_t(labels, monkeypatch, changed):
    runner, state, generation, _, keys, _, parser, press = labels
    original = state[0]
    def pause_after_t(hwnd, vk):
        press(hwnd, vk)
        if vk == rm.VK_T:
            generation[0] += 2
            if changed:
                state[0] = replace(state[0], level=6)
    monkeypatch.setattr(rm, "press_key_once", pause_after_t)
    with controlled_inputs(runner._checkpoint):
        if changed:
            with pytest.raises(rm.RunnerError, match="不一致"):
                runner._act(1, Action.MARK_DISCARD, original)
        else:
            runner._act(1, Action.MARK_DISCARD, original)
    assert keys == ([rm.VK_T] if changed else [rm.VK_T, rm.VK_R])
    assert parser.call_count == 1  # 仅恢复时复验一次对象。


def test_stop_between_unlock_and_discard_never_sends_r(labels, monkeypatch):
    runner, state, _, stopped, keys, _, _, press = labels
    def stop_after_t(hwnd, vk):
        press(hwnd, vk)
        stopped[0] = True
    monkeypatch.setattr(rm, "press_key_once", stop_after_t)
    with controlled_inputs(runner._checkpoint), pytest.raises(RunnerStopped):
        runner._act(1, Action.MARK_DISCARD, state[0])
    assert keys == [rm.VK_T]


@pytest.mark.parametrize("key", ["locked", "discarded"])
def test_unknown_before_input_never_sends_any_key(labels, key):
    runner, state, _, _, keys, _, _, _ = labels
    state[0] = replace(state[0], **{key: None})
    with pytest.raises(rm.RunnerError, match="无法确认"):
        runner._act(1, Action.MARK_DISCARD, state[0])
    assert keys == []


@pytest.mark.parametrize("fault", ["window", "page", "size"])
def test_changed_capture_binding_stops_without_input(labels, monkeypatch, fault):
    runner, state, _, _, keys, _, _, _ = labels
    if fault == "window":
        image = runner._grab()[0]
        runner._grab.return_value = (image, "unknown", "test", 2)
    elif fault == "page":
        monkeypatch.setattr(rm, "is_page", lambda *_: False)
    else:
        small = QImage(1280, 720, QImage.Format.Format_RGB32)
        runner._grab.side_effect = [runner._grab.return_value, (small, "unknown", "test", 1)]
    with controlled_inputs(runner._checkpoint), pytest.raises(rm.RunnerError):
        runner._act(1, Action.MARK_DISCARD, state[0])
    assert keys == []


@pytest.mark.parametrize("key", ["locked", "discarded"])
def test_both_keys_complete_on_first_known_target_without_full_read(labels, monkeypatch, key):
    runner, state, _, _, keys, reader, parser, _ = labels
    other = "discarded" if key == "locked" else "locked"
    reader.side_effect = [{key: None, other: False}, {key: True, other: False}]
    sleep = Mock()
    monkeypatch.setattr(rm.time, "sleep", sleep)
    context = rm._LabelContext(state[0], 0)
    runner._verify_label(1, key=key, value=True, other_value=False, context=context)
    assert reader.call_count == 2 and keys == []
    # §4.3：第一次复核由释放总下限覆盖（不叠加 50 ms），第二次才是 50 ms 间隔观察。
    assert [call.args[0] for call in sleep.call_args_list] == [0.05]
    parser.assert_not_called()
