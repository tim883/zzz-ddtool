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

    window.light_theme.setChecked(False)
    window.light_theme.setChecked(True)
    assert "f3f3f3" in (QApplication.instance().styleSheet() or "")
    window.light_theme.setChecked(False)
    assert "141414" in (QApplication.instance().styleSheet() or "")
    window.close()


def test_collect_preset_roundtrip(qapp: QApplication) -> None:
    window = MainWindow()
    preset = window.collect_preset()
    assert preset.weights
    assert preset.accepted is not None
    assert decide_sample(preset) in {Action.UPGRADE, Action.MARK_DISCARD, Action.SKIP, Action.STOP}
    window.close()


def decide_sample(preset) -> Action:
    from zzz_ddtool.decide import decide

    disc = Disc(
        level=0,
        substats=(Substat("暴击率"), Substat("暴击伤害"), Substat("攻击力%")),
    )
    return decide(disc, preset, preset.accepted).action
