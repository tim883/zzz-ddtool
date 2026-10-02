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
    # 右侧是选中任务的镜像：先建一个任务、编上词条，才有卡片可看
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1.0, "暴击伤害": 1.0, "攻击力%": 0.8})
    window.rebuild_cards()
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
    # 右侧是选中任务的镜像：没有选中任务时右侧为空，得先新建一个任务才有的可收集
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1.0})
    preset = window.collect_preset()
    assert preset.weights == {"暴击率": 1.0}
    assert preset.accepted is not None
    assert decide_sample(preset) in {Action.UPGRADE, Action.MARK_DISCARD, Action.SKIP, Action.STOP}
    window.close()


def test_editor_empty_when_no_job_selected(qapp: QApplication, tmp_path) -> None:
    """死线：左侧没有选中任务时，右侧必须是空的，绝不能显示残留内容（假信息）。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    assert window._jobs == []
    assert window.weight_editor.weights() == {}
    assert not window.job_name_edit.isEnabled()
    assert all(len(section.cards) == 0 for section in window.sections.values())

    # 新建一个任务、编点内容，再删掉它 → 右侧必须回到空
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1.0})
    window.filter_slots[5].click()
    qapp.processEvents()
    assert window.weight_editor.weights()

    window._remove_job()
    qapp.processEvents()
    assert window._jobs == []
    assert window.weight_editor.weights() == {}
    assert not window.job_name_edit.isEnabled()
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


def test_slot_switch_retargets_selected_job(qapp: QApplication, tmp_path) -> None:
    """换部位 = 改当前选中任务的部位，任务保持选中、内容保留。

    原来换部位会把任务取消选中，于是「选中任务 → 换部位」永远做不到：
    一点部位就丢选中，再点任务又跳回旧部位。
    """
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1.0})
    window.filter_slots[1].click()
    qapp.processEvents()
    assert window._active_slot == 1
    job = window._jobs[-1]  # 写回时对象会被替换，这里重新取
    assert job.slot == 1  # 部位跟着任务走
    assert window._editing_job_id == job.job_id  # 仍然选中
    assert window.weight_editor.weights().get("暴击率") == 1.0  # 内容没丢
    window.close()


def test_copy_job_duplicates_content_and_numbers_name(qapp: QApplication, tmp_path) -> None:
    """复制任务：内容一样、id 是新的、名字加编号，两份互不影响。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.job_name_edit.setText("紫手套")
    window.job_name_edit.editingFinished.emit()
    window.weight_editor.set_weights({"暴击率": 1.0})
    qapp.processEvents()
    source_id = window._jobs[0].job_id

    window._copy_job()
    qapp.processEvents()
    assert len(window._jobs) == 2
    copied = window._jobs[1]
    assert copied.name == "紫手套 (2)"
    assert copied.job_id != source_id  # to_dict 会带旧 id，必须换掉
    assert copied.weights == window._jobs[0].weights
    assert window._editing_job_id == copied.job_id  # 复制后选中副本

    # 再复制一次：基于基准名编号，不能叠成「紫手套 (2) (2)」
    window._copy_job()
    qapp.processEvents()
    assert "紫手套 (3)" in [job.name for job in window._jobs]

    # 两份独立
    window.chain_list.setCurrentRow(0)
    qapp.processEvents()
    window.weight_editor.set_weights({"生命值%": 1.0})
    window.chain_list.setCurrentRow(1)
    qapp.processEvents()
    assert "生命值%" in window._jobs[0].weights
    assert "生命值%" not in window._jobs[1].weights
    window.close()


def test_copy_without_selection_does_nothing(qapp: QApplication, tmp_path) -> None:
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._copy_job()
    assert window._jobs == []
    window.close()


def test_new_job_starts_empty_and_gets_unique_name(qapp: QApplication, tmp_path) -> None:
    """新建任务 = 建一个空任务并选中它，右侧跟着空；名字自动去重。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    first = window._jobs[-1]
    assert first.name == "任务"
    assert window.weight_editor.weights() == {}
    assert window._editing_job_id == first.job_id

    window.weight_editor.set_weights({"暴击率": 1.0})
    window._add_job()
    second = window._jobs[-1]
    assert second.name == "任务 (2)"  # 不重名
    assert second.weights == {}  # 新任务是空的，不会顺走上一个任务的内容
    assert window.weight_editor.weights() == {}  # 右侧跟着空
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
