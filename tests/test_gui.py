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
    window = MainWindow(config_path=tmp_path / "t.yaml")
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


def test_collect_preset_roundtrip(qapp: QApplication, tmp_path) -> None:
    window = MainWindow(config_path=tmp_path / "t.yaml")
    preset = window.collect_preset()
    assert preset.weights
    assert preset.accepted is not None
    assert decide_sample(preset) in {Action.UPGRADE, Action.MARK_DISCARD, Action.SKIP, Action.STOP}
    window.close()


def test_game_filter_checkboxes(qapp: QApplication, tmp_path) -> None:
    window = MainWindow(config_path=tmp_path / "t.yaml")
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


def test_mainstat_excluded_from_substat_cards(qapp: QApplication, tmp_path) -> None:
    window = MainWindow(config_path=tmp_path / "t.yaml")
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


def test_slot_switch_keeps_separate_plans(qapp: QApplication, tmp_path) -> None:
    window = MainWindow(config_path=tmp_path / "t.yaml")
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


def test_log_panel_updates_rotates_and_shows_failure(qapp, tmp_path, monkeypatch):
    from zzz_ddtool import paths

    monkeypatch.setattr(paths, "logs_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "status_file", lambda: tmp_path / "status.json")
    logfile = tmp_path / "ddtool.log"
    logfile.write_text("02:42:06 [信息] 点开格子\n", encoding="utf-8")
    window = MainWindow(config_path=tmp_path / "t.yaml")
    try:
        assert "点开格子" in window.run_log.toPlainText()
        with logfile.open("a", encoding="utf-8") as stream:
            stream.write("02:42:13 [错误] 识别失败：重试耗尽\n")
        window._refresh_run_status()
        assert "重试耗尽" in window.run_log.toPlainText()
        window._show_run_status({"phase": "failed", "message": "识别失败", "processed": 0, "max_discs": 2})
        assert "识别失败" in window.run_status_label.text()
        logfile.write_text("新一轮日志\n", encoding="utf-8")
        window._refresh_run_status()
        assert window.run_log.toPlainText() == "新一轮日志"
    finally:
        window.close()


def test_task_buttons_and_close_share_controller(qapp, tmp_path, monkeypatch):
    import time
    import threading
    from zzz_ddtool import runner, hotkeys

    entered = threading.Event()
    controllers = []
    monkeypatch.setattr(hotkeys.user32, "GetAsyncKeyState", lambda key: 0)

    class FakeRunner:
        def __init__(self, preset, *, controller, on_status):
            self.controller, self.on_status = controller, on_status
            controllers.append(controller)

        def run(self):
            entered.set()
            self.on_status({"phase": "running", "message": "替身执行中"})
            while not self.controller.stopped:
                time.sleep(0.01)
            self.on_status({"phase": "stopped", "message": "已停止"})

    monkeypatch.setattr(runner, "Runner", FakeRunner)
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window.show()
    window.start_btn.click()
    assert entered.wait(3)
    window._on_start_clicked()  # 防重复启动
    assert len(controllers) == 1
    assert not window.start_btn.isEnabled()
    window.pause_btn.click()
    assert controllers[0].paused
    window.pause_btn.click()
    assert not controllers[0].paused
    window.close()  # 关闭先请求停止，等线程释放后才真正关闭
    assert controllers[0].stopped
    limit = time.monotonic() + 3
    while window._run_thread is not None and time.monotonic() < limit:
        qapp.processEvents()
        time.sleep(0.01)
    assert window._run_thread is None
    assert not window.isVisible()
    assert controllers[0]._closed.is_set()


def test_worker_qt_capture_dispatches_to_main_thread(qapp, monkeypatch):
    import threading
    import time
    from PySide6.QtCore import QThread
    from PySide6.QtGui import QImage
    from zzz_ddtool import capture, capturebridge

    bridge = capturebridge.install_capture_bridge()
    main_id = threading.get_ident()
    calls = []
    original = capture._grab_screen_region

    def screenshot(hwnd):
        if QThread.currentThread() != qapp.thread():
            return original(hwnd)
        calls.append((threading.get_ident(), hwnd))
        return QImage(10, 10, QImage.Format.Format_RGB32)

    monkeypatch.setattr(capture, "_grab_screen_region", screenshot)
    result = []
    worker = threading.Thread(target=lambda: result.append(capture._grab_screen_region(123)))
    worker.start()
    limit = time.monotonic() + 3
    while worker.is_alive() and time.monotonic() < limit:
        qapp.processEvents()
        time.sleep(0.01)
    worker.join(timeout=0.1)
    assert not worker.is_alive()
    assert calls == [(main_id, 123)]
    assert result[0].width() == 10
