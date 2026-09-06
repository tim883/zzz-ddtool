import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from zzz_ddtool.gui import MainWindow
from zzz_ddtool.models import Action, Disc, Substat


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app


def test_window_loads_example_and_trial(qapp: QApplication, tmp_path) -> None:
    window = MainWindow()
    assert window.windowTitle() == "驱动盘强化辅助"
    assert window.sections
    visible = sum(1 for section in window.sections.values() for card in section.cards if not card.isHidden())
    assert len(list(window.sections.values())[0].cards) > 0
    assert visible > 0

    window.trial.level.setValue(0)
    window.trial.rows[0][0].setCurrentText("暴击率")
    window.trial.rows[1][0].setCurrentText("暴击伤害")
    window.trial.rows[2][0].setCurrentText("生命值")
    window.trial.rows[3][0].setCurrentText("防御力")
    window.run_trial()
    assert "mark_discard" in window.trial.result.text() or "upgrade" in window.trial.result.text()

    dest = tmp_path / "from-gui.yaml"
    window._path = dest
    window.save_current()
    assert dest.exists()

    assert "f8f9fc" in (QApplication.instance().styleSheet() or "")
    window.close()


def test_collect_preset_roundtrip(qapp: QApplication) -> None:
    window = MainWindow()
    preset = window.collect_preset()
    assert preset.weights
    assert preset.accepted is not None
    assert decide_sample(preset) in {Action.UPGRADE, Action.MARK_DISCARD, Action.SKIP, Action.STOP}
    window.close()


def test_game_filter_checkboxes(qapp: QApplication) -> None:
    window = MainWindow()
    window._filter_sets = ["折枝剑歌"]
    window.filter_slots[4].click()
    qapp.processEvents()
    assert window._active_slot == 4
    assert "生命值" not in window.filter_main_boxes
    assert "暴击伤害" in window.filter_main_boxes
    window.filter_main_boxes["暴击伤害"].setChecked(True)
    preset = window.collect_preset()
    assert preset.filter_sets == ("折枝剑歌",)
    assert preset.filter_slot == 4
    assert preset.filter_mainstats == ("暴击伤害",)
    window.close()


def test_mainstat_excluded_from_substat_cards(qapp: QApplication) -> None:
    window = MainWindow()
    window.filter_slots[4].click()
    qapp.processEvents()
    window.weight_editor.set_weights({"暴击率": 1.0, "暴击伤害": 1.2})
    window.filter_main_boxes["暴击伤害"].setChecked(True)
    qapp.processEvents()
    window.rebuild_cards()
    assert "暴击伤害" not in window.weight_editor.weights()
    assert "暴击率" in window.weight_editor.weights()
    names = {
        slot.name
        for section in window.sections.values()
        for card in section.cards
        for slot in card.pattern.slots
    }
    assert "暴击伤害" not in names
    window.close()


def test_slot_switch_keeps_separate_plans(qapp: QApplication) -> None:
    window = MainWindow()
    window.filter_slots[4].click()
    qapp.processEvents()
    window.weight_editor.set_weights({"暴击率": 1.0})
    window.filter_slots[1].click()
    qapp.processEvents()
    assert window._active_slot == 1
    assert "暴击率" not in window.weight_editor.weights()
    window.weight_editor.set_weights({"生命值%": 1.0})
    window.filter_slots[4].click()
    qapp.processEvents()
    assert window.weight_editor.weights().get("暴击率") == 1.0
    window.close()


def decide_sample(preset) -> Action:
    from zzz_ddtool.decide import decide

    disc = Disc(
        level=0,
        substats=(Substat("暴击率"), Substat("暴击伤害"), Substat("攻击力%")),
    )
    return decide(disc, preset, preset.accepted).action
