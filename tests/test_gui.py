import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from zzz_ddtool.gui import MainWindow, WeightEditor, read_layout, write_layout
from zzz_ddtool.models import Action, Disc, Substat


def wait_until(qapp, predicate, timeout=10):
    import time
    from PySide6.QtTest import QTest

    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(5)
    assert predicate(), "Timed out waiting for the GUI worker"


def refresh_cards(window, qapp):
    window.rebuild_cards()
    wait_until(qapp, lambda: window._preview_thread is None and not window._preview_timer.isActive())
    assert not window._preview_dirty, window.status.text()


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path, monkeypatch):
    from zzz_ddtool import paths

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(paths, "user_dir", lambda: tmp_path / "user")
    monkeypatch.setattr(paths, "user_data_root", lambda: tmp_path / "application-root")
    monkeypatch.setattr(paths, "legacy_plan_dirs", lambda: (tmp_path / "legacy" / "data",))


def test_window_loads_example_and_trial(qapp: QApplication, tmp_path) -> None:
    window = MainWindow(config_path=tmp_path / "t.yaml")
    assert window.windowTitle() == "驱动盘强化辅助"
    assert window.sections
    # 右侧是选中任务的镜像：先建一个任务、编上词条，才有卡片可看
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1.0, "暴击伤害": 1.0, "攻击力%": 0.8})
    refresh_cards(window, qapp)
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
    assert preset.accepted is None  # Default selection does not need a generated whitelist.
    assert decide_sample(preset) in {Action.UPGRADE, Action.MARK_DISCARD, Action.SKIP, Action.STOP}
    window.close()


def test_editor_empty_when_no_job_selected(qapp: QApplication, tmp_path) -> None:
    """死线：左侧没有选中任务时，右侧必须是空的，绝不能显示残留内容（假信息）。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    assert window._jobs == []
    assert window.weight_editor.weights() == {}
    assert window.chain_list.count() == 0  # 没有任务就没有条目，也没有可编的名字
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
    assert window.chain_list.count() == 0
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
    refresh_cards(window, qapp)
    assert window.weight_editor.weights()["暴击伤害"] == 1.2
    assert "暴击率" in window.weight_editor.weights()
    names = {
        slot.name
        for section in window.sections.values()
        for card in section.cards
        for slot in card.pattern.slots
    }
    assert "暴击伤害" not in names
    window.close()


def test_weight_edit_only_fires_on_commit(qapp: QApplication, tmp_path) -> None:
    """改词条单价：敲键盘的过程中一次都不重算，只在编辑结束（回车/失焦）算一次。

    默认 QDoubleSpinBox 每敲一个字符就发一次 valueChanged，而每改一次价都要重算
    整屏组合（实测 4 词条 = 837 张卡，一次 4.2 秒），于是打「1.5」三下就卡三下。
    关掉 keyboardTracking 之后只在提交时发一次；点上下箭头调值仍然即时生效
    （那是「调一次」，不是「敲一下」）。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1.0})
    refresh_cards(window, qapp)

    fires: list[int] = []
    window.weight_editor.changed.connect(lambda: fires.append(1))
    _combo, spin, _row = window.weight_editor._rows[0]

    window.show()
    spin.setFocus()
    QTest.keyClick(spin, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    fires.clear()
    for key in (Qt.Key.Key_1, Qt.Key.Key_Period, Qt.Key.Key_5):
        QTest.keyClick(spin, key)
    qapp.processEvents()
    assert fires == [], "敲键盘的过程中不该重算"

    QTest.keyClick(spin, Qt.Key.Key_Return)
    qapp.processEvents()
    assert len(fires) == 1, "回车应当只提交一次"
    assert spin.value() == 1.5

    fires.clear()
    spin.stepUp()  # 点上下箭头是「调一次」，必须即时生效
    assert len(fires) == 1
    fires.clear()
    combo = window.weight_editor._rows[0][0]
    combo.lineEdit().editingFinished.emit()
    combo.activated.emit(combo.currentIndex())
    assert not fires, "内容未变化的提交不能取消正在进行的组合计算"
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


def test_copy_job_duplicates_content(qapp: QApplication, tmp_path) -> None:
    """复制任务：内容一样、id 是新的，两份互不影响。没有名字要编号。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1.0})
    qapp.processEvents()
    source_id = window._jobs[0].job_id

    window._copy_job()
    qapp.processEvents()
    assert len(window._jobs) == 2
    copied = window._jobs[1]
    assert copied.job_id != source_id  # to_dict 会带旧 id，必须换掉
    assert copied.weights == window._jobs[0].weights
    assert window._editing_job_id == copied.job_id  # 复制后选中副本
    # 两条内容一致，列表里显示一样——这是预期，改完就分开
    assert window._jobs[0].title() == copied.title()

    # 再复制一次：照样只是内容副本
    window._copy_job()
    qapp.processEvents()
    assert len(window._jobs) == 3

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


def test_new_job_starts_empty(qapp: QApplication, tmp_path) -> None:
    """新建任务 = 建一个空任务并选中它，右侧跟着空。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    first = window._jobs[-1]
    assert window.weight_editor.weights() == {}
    assert window._editing_job_id == first.job_id

    window.weight_editor.set_weights({"暴击率": 1.0})
    window._add_job()
    second = window._jobs[-1]
    assert second.weights == {}  # 新任务是空的，不会顺走上一个任务的内容
    assert window.weight_editor.weights() == {}  # 右侧跟着空
    window.close()


def test_job_label_is_derived_from_slot_sets_mainstats(qapp: QApplication, tmp_path) -> None:
    """列表标签只由「序号 + 部位 · 套装 · 主词条」拼出来，没有任务名这套东西。

    老存档里残留的 name 字段一律忽略：写回去也不再落盘。
    """
    from PySide6.QtCore import Qt

    from zzz_ddtool.chain import parse_task_chain
    from zzz_ddtool.gui import _JobItemDelegate

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.filter_slots[4].click()
    window._filter_sets = ["折枝剑歌"]
    # 「选择套装」弹窗落定后就是这两步，跟真实路径一致
    window._update_set_summary()
    window._refresh_current_item_text()
    qapp.processEvents()

    # 勾主词条只走信号，标签也必须立刻跟上（列表标签是唯一标识）
    window.filter_main_boxes["暴击伤害"].setChecked(True)
    qapp.processEvents()

    label = window.chain_list.item(0).text()
    assert label == "1 │ 4号 · 折枝剑歌 · 暴击伤害"
    assert "任务" not in label  # 没有默认名顶在前面
    assert "\n" not in label

    # 序号和正文之间那道竖线要真的分开画，且栏窄时的完整内容挂在悬停提示上
    assert isinstance(window.chain_list.itemDelegate(), _JobItemDelegate)
    # 不做「悬停显示详情」：条目只有文本，没有 tooltip
    assert window.chain_list.item(0).toolTip() == ""
    assert window.chain_list.textElideMode() == Qt.TextElideMode.ElideRight

    # 改部位 → 标签跟着变
    window.filter_slots[5].click()
    qapp.processEvents()
    label = window.chain_list.item(0).text()
    assert label.startswith("1 │ 5号 · 折枝剑歌")

    # 存档里若还写着 name，载入后既不显示也不回写
    jobs = parse_task_chain(
        [{"slot": 2, "name": "我起的名字", "sets": ["折枝剑歌"], "mainstats": []}]
    )
    assert len(jobs) == 1
    assert "name" not in jobs[0].to_dict()
    assert MainWindow._job_item_text(0, jobs[0]) == "1 │ 2号 · 折枝剑歌"
    window.close()


def test_chain_panel_is_wide_enough_for_label(qapp: QApplication, tmp_path) -> None:
    """任务链栏要放得下常见的长标签（双套装 + 主词条，实测全行 291px）。

    最极端的一行（双套装 + 「能量自动回复」）拉满 420px 才全放下，默认靠省略号 +
    悬停全文兜底，所以这里只保证常见长度不打折。
    """
    from PySide6.QtGui import QFontMetrics

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()
    window._add_job()
    window.filter_slots[4].click()
    window._filter_sets = ["静听嘉音", "折枝剑歌"]
    window._update_set_summary()
    window._refresh_main_boxes({"暴击伤害"})
    qapp.processEvents()

    assert window.chain_panel.minimumWidth() >= 340  # 220 时标签只看到半截
    assert window.chain_panel.maximumWidth() >= 400  # 拉得动才能把极端长行也放全

    text = window.chain_list.item(0).text()
    need = QFontMetrics(window.chain_list.font()).horizontalAdvance(text)
    room = window.chain_list.viewport().width()
    assert need <= room, (text, need, room)
    window.close()


def test_job_seq_column_keeps_two_digits_width(qapp: QApplication) -> None:
    """序号列默认按 2 位数字留宽：1 与 10 同宽，竖线和正文起点才不会错位。

    序号位数不同（9 → 10）时列宽不变，正文不会随序号位数左右跳；过百的任务
    （三位数）才按实际宽度放宽。
    """
    from PySide6.QtGui import QFont, QFontMetrics

    from zzz_ddtool.gui import _JOB_SEQ_DIGITS, _job_seq_width

    assert _JOB_SEQ_DIGITS == 2
    fm = QFontMetrics(QFont(qapp.font()))
    width = lambda seq: _job_seq_width(fm, seq)  # noqa: E731

    assert width("1") == width("10")  # 个位补满两位
    assert width("9") == width("10")  # 进位时列宽不动
    assert width("1") == width("99")
    assert width("100") > width("10")  # 三位数按实际宽度放宽，不截断
    qapp.processEvents()


def test_config_columns_cannot_be_collapsed(qapp: QApplication, tmp_path) -> None:
    """三栏分隔条不能把某一栏拖到 0 藏起来：拖到极限停在该栏最小宽。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()
    window._add_job()
    qapp.processEvents()

    splitter = window._config_splitter
    assert not splitter.childrenCollapsible()  # 关键开关：关掉「拖到底就折叠」

    # 把两侧的分隔条都往左拖到头，任何栏都不能消失
    splitter.moveSplitter(0, 0)
    qapp.processEvents()
    splitter.moveSplitter(0, 1)
    qapp.processEvents()
    sizes = splitter.sizes()
    assert len(sizes) == 3
    assert all(size > 0 for size in sizes), sizes
    assert sizes[0] >= 340  # 任务链栏停在自己的最小宽
    assert sizes[1] >= 300  # 配置栏停在自己的最小宽

    # 旧 layout.json 里若存了 0，恢复时也要夹回最小宽
    splitter.setSizes([0, 0, 0])
    qapp.processEvents()
    assert all(size > 0 for size in splitter.sizes()), splitter.sizes()
    window.close()


def test_remembers_last_opened_yaml(qapp: QApplication, tmp_path, monkeypatch) -> None:
    """打开时用上次的 yaml。

    只在用户换了文件（打开 / 另存为）时记；没记过、记的文件被删了、记录损坏，
    都回落自带的 example.yaml，不能启动不了。
    """
    import json

    from zzz_ddtool import paths
    from zzz_ddtool.config import preset_from_dict, save_preset
    from zzz_ddtool.gui import default_config_path, last_config_path, remember_config_path

    # user 目录指到临时目录，别碰真实的 user/
    monkeypatch.setattr(paths, "user_dir", lambda: tmp_path / "user")

    assert last_config_path() is None
    assert default_config_path().name == "example.yaml"

    # 故意用 2 个词条的轻量档：4 词条会生成 837 张卡，光是建卡就把测试拖慢十几秒
    mine = tmp_path / "我的方案.yaml"
    save_preset(
        mine,
        preset_from_dict(
            {
                "name": "轻量",
                "weights": {"暴击率": 1.0},
                "task_chain": [
                    {"id": "a", "slot": 1, "weights": {"暴击率": 1.0}},
                    {"id": "b", "slot": 2, "weights": {"暴击率": 1.5}},
                ],
            }
        ),
    )
    remember_config_path(mine)
    assert last_config_path() == mine.resolve()
    assert default_config_path() == mine.resolve()

    # 不传 config_path 启动 → 载入上次那份
    window = MainWindow()
    assert window._path == mine.resolve()
    assert len(window._jobs) == 2
    window.close()

    # 文件没了 → 回落默认，不能拿一个不存在的路径去启动
    mine.unlink()
    assert last_config_path() is None
    assert default_config_path().name == "example.yaml"

    # 记录损坏 → 回落默认，不抛异常
    last_file = paths.last_config_file()
    last_file.parent.mkdir(parents=True, exist_ok=True)
    for broken in ("{ 这不是 json", json.dumps({"path": "   "}), json.dumps(["不是字典"])):
        last_file.write_text(broken, encoding="utf-8")
        assert last_config_path() is None
        assert default_config_path().name == "example.yaml"


def test_open_and_save_as_remember_file(qapp: QApplication, tmp_path, monkeypatch) -> None:
    """点「打开」/「另存为」换了文件之后，那份就被记住。"""
    from zzz_ddtool import gui, paths
    from zzz_ddtool.config import preset_from_dict, save_preset
    from zzz_ddtool.gui import last_config_path

    monkeypatch.setattr(paths, "user_dir", lambda: tmp_path / "user")
    light = preset_from_dict({"name": "轻量", "weights": {"暴击率": 1.0}})

    target = tmp_path / "手动打开.yaml"
    save_preset(target, light)
    monkeypatch.setattr(
        gui.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(target), "YAML"))
    )
    window = MainWindow(config_path=tmp_path / "空.yaml")
    window.open_preset()
    assert last_config_path() == target.resolve()

    # 另存为换了当前工作文件，也要记住
    target2 = tmp_path / "另存出去.yaml"
    monkeypatch.setattr(
        gui.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target2), "YAML"))
    )
    window.save_as()
    assert target2.exists()
    assert last_config_path() == target2.resolve()
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
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1})
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


def test_substat_choices_match_the_authoritative_dictionary() -> None:
    """副词条下拉框的内容必须与 data/stats.json 的 substat_names 一致。

    这里曾经硬编码过一份，把「穿透值」误写成「穿透率」——穿透率是 5 号位主属性，
    副词条里根本不存在。后果是下拉框能选出一个游戏里没有的词条，选进去的组合
    永远命不中，还会把这种废组合写进 accepted。
    现在代码只声明顺序，内容取自 stats.json，这个测试守住这条不变量。
    """
    from zzz_ddtool.gamedata import MAINSTAT_NAMES, SUBSTAT_NAMES
    from zzz_ddtool.gui import KNOWN_SUBSTATS

    # 内容必须一模一样（只允许顺序不同）
    assert set(KNOWN_SUBSTATS) == set(SUBSTAT_NAMES)
    assert len(KNOWN_SUBSTATS) == len(set(KNOWN_SUBSTATS))  # 不能有重复

    # 常见笔误：「穿透率」是主属性，副词条是「穿透值」
    assert "穿透值" in KNOWN_SUBSTATS
    assert "穿透率" not in KNOWN_SUBSTATS
    assert "穿透率" in MAINSTAT_NAMES


def test_substat_pool_is_the_ten_rollable_ones() -> None:
    """副词条池固定 10 种，钉死，别再往里加。

    游戏里驱动盘的副词条只有这 10 种：
    生命值 / 生命值% / 攻击力 / 攻击力% / 防御力 / 防御力% / 暴击率 / 暴击伤害 /
    穿透值 / 异常精通。

    曾经往这个池子里混进 4 个主属性：
      * 「穿透率」是 5 号位主词条（副词条是「穿透值」）；
      * 「冲击力」「异常掌控」「能量自动回复」是 6 号位主词条。
    后果是界面下拉框里出现游戏里根本选不中的词条，选进去的组合永远命不中，
    右上角还会多出一大堆假卡片（实测权重里混一条「穿透率」，卡片从 837 张涨到
    2333 张，其中 1496 张把「穿透率」当成可能的副词条）。
    """
    from zzz_ddtool.gamedata import SLOT_MAINSTATS, SUBSTAT_NAMES

    assert set(SUBSTAT_NAMES) == {
        "生命值",
        "生命值%",
        "攻击力",
        "攻击力%",
        "防御力",
        "防御力%",
        "暴击率",
        "暴击伤害",
        "穿透值",
        "异常精通",
    }
    assert len(SUBSTAT_NAMES) == len(set(SUBSTAT_NAMES))  # 不能有重复

    for name in ("冲击力", "异常掌控", "能量自动回复"):
        assert name not in SUBSTAT_NAMES, f"{name} 是 6 号位主词条，不是副词条"
        assert name in SLOT_MAINSTATS[6]
    assert "穿透率" not in SUBSTAT_NAMES
    assert "穿透率" in SLOT_MAINSTATS[5]


def test_penetration_names_are_not_swapped() -> None:
    """「穿透率」和「穿透值」这一对最容易搞混，单独钉死。

    注意：主属性和副词条**大量重名是正常的**（生命值、攻击力、暴击率、暴击伤害、
    异常精通、异常掌控、冲击力、能量自动回复 两边都有），所以不能笼统断言两个词典
    无交集。只有穿透这一对是同概念不同名 —— 主属性叫「穿透率」，副词条叫「穿透值」。
    """
    from zzz_ddtool.gamedata import MAINSTAT_NAMES, SUBSTAT_NAMES

    assert "穿透率" in MAINSTAT_NAMES
    assert "穿透率" not in SUBSTAT_NAMES
    assert "穿透值" in SUBSTAT_NAMES
    assert "穿透值" not in MAINSTAT_NAMES


def test_weight_editor_rejects_non_substats(qapp: QApplication) -> None:
    """配置里的脏名字（如「穿透率」）不能出现在界面的权重表和下拉候选里。

    这是三个入口里最靠前的那个：脏数据从 yaml 载入时就在这里被挡住，下拉框
    也就不会再显示游戏里选不中的词条。守卫配合 patterns 层的过滤一起用。
    """
    from zzz_ddtool.gamedata import SUBSTAT_NAMES

    editor = WeightEditor()
    editor.set_weights({"攻击力%": 1.5, "穿透率": 0.6})

    assert editor.weights() == {"攻击力%": 1.5}  # 脏名字既没显示也没参与取值
    choices = editor._choices()
    assert "穿透率" not in choices
    assert set(choices) <= set(SUBSTAT_NAMES)


def test_weight_editor_add_button_rejects_non_substat(qapp: QApplication) -> None:
    """「添加词条」这条路也得挡——下拉框是可编辑的，用户能敲进任意字符串。

    这是脏名字最后一个入口：载入时会被 set_weights 挡，但手敲进去再点添加就绕过了。
    """
    editor = WeightEditor()
    editor.add_combo.setCurrentText("穿透率")
    editor._add_from_combo()

    assert editor._rows == []  # 一行都没建出来
    assert editor.weights() == {}


def test_trial_name_choices_only_come_from_dictionary(qapp: QApplication) -> None:
    """「点名」下拉的候选只能来自 data/stats.json，不能被权重表带脏。

    候选必须是**全部 10 个**副词条（没加权的也要能选，它们会算成废词条），
    而不是「权重表里的那些」。所以这里不接收、也不额外追加权重表的名字。
    """
    from zzz_ddtool.gamedata import SUBSTAT_NAMES
    from zzz_ddtool.gui import TrialPage

    page = TrialPage()
    page.set_name_choices(set())

    for combo, _rolls in page.rows:
        items = [combo.itemText(i) for i in range(combo.count())]
        assert "穿透率" not in items
        assert {name for name in items if name} == set(SUBSTAT_NAMES)


def _wheel_event(widget) -> object:  # noqa: ANN001
    """造一个「向上滚一格」的 QWheelEvent，直接喂给 widget.wheelEvent。"""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    pos = QPointF(widget.rect().center())
    return QWheelEvent(
        pos,
        pos,
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def test_number_spinboxes_ignore_mouse_wheel(qapp: QApplication) -> None:
    """滚轮悬在数字框上不得改值（误触）。

    两层守卫：
    1. 行为层 —— NoWheel 系 spinbox 收到滚轮必须「不吃掉、不改值」，
       事件 ignore() 后冒泡给父级滚动区，滚轮回到翻页；
    2. 接线层 —— 界面上实际使用的数字框必须都是 NoWheel 系，
       防止以后重构时改回裸 QDoubleSpinBox/QSpinBox 让误触复活。
    """
    from zzz_ddtool.gui import StageSection
    from zzz_ddtool.origin import Origin
    from zzz_ddtool.widgets import NoWheelDoubleSpinBox, NoWheelSpinBox

    for spin in (NoWheelDoubleSpinBox(), NoWheelSpinBox()):
        spin.setRange(0, 10)
        spin.setValue(3)
        event = _wheel_event(spin)
        spin.wheelEvent(event)
        assert not event.isAccepted()
        assert spin.value() == 3

    editor = WeightEditor()
    editor.set_weights({"攻击力": 1.0})
    assert isinstance(editor._rows[0][1], NoWheelDoubleSpinBox)

    assert isinstance(StageSection(Origin.THREE, 0, None).require, NoWheelDoubleSpinBox)
    assert isinstance(StageSection(Origin.FOUR, 3, None).require, NoWheelDoubleSpinBox)

    from zzz_ddtool.gui import TrialPage

    trial = TrialPage()
    assert isinstance(trial.level, NoWheelSpinBox)
    for _combo, rolls in trial.rows:
        assert isinstance(rolls, NoWheelSpinBox)


def test_qualifying_ids_match_generate_plus_select_visible(qapp: QApplication, tmp_path) -> None:
    """「全任务卡片全选」的口径必须与「生成组合 + 全选当前可见」逐位一致。

    纯函数 qualifying_pattern_ids 是那个按钮唯一的核心逻辑，这里把当前任务
    当成其中一个任务，拿界面真做出来的可见集合去对齐——口径一歪，按钮批量
    勾的东西就和用户手动点出来不是一回事了。
    """
    from zzz_ddtool.gui import qualifying_pattern_ids

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1.0, "暴击伤害": 0.8, "攻击力%": 0.5})
    # 只给部分档位填门槛，其余保持默认 0——两种情形都要对上
    for checkpoint in (0, 3, 6):
        window.sections[("three", checkpoint)].require.setValue(1.2)
        window.sections[("four", checkpoint)].require.setValue(0.9)
    refresh_cards(window, qapp)

    job = window._editing_job()
    # set_weights 屏蔽信号，任务节点还是空的——先让编辑器写回，再取任务的配置
    window._commit_editor()
    job = window._editing_job()
    actual = set(qualifying_pattern_ids(job.slot, job.mainstats, job.weights, job.stage_require))
    # 界面上「符合门槛」就是每个档位 section 的 _visible_indices（apply_filter 的产物）。
    # _checked 不能拿来比：没有历史勾选时策略是「全选」，它不区分可见与否。
    expected = {
        section._ids[i]
        for section in window.sections.values()
        for i in section._visible_indices
    }
    assert actual == expected
    assert actual
    window.close()


def test_select_all_jobs_covers_every_task_and_spares_new_ones(qapp: QApplication, tmp_path) -> None:
    """按钮给每个任务按它自己的权重+门槛填 accepted，且不碰之后新建的任务。"""
    from zzz_ddtool.config import preset_from_dict
    from zzz_ddtool.gui import qualifying_pattern_ids

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._apply_preset(preset_from_dict({
        "task_chain": [
            {"slot": 4, "weights": {"暴击率": 1.0}, "stage_require": {"four": {0: 1.0}}},
            {"slot": 6, "weights": {"攻击力%": 1.0, "暴击伤害": 0.5}},
            {"slot": 5, "weights": {}},  # 没设权重：界面本来就不出卡，必须跳过
        ],
    }))
    assert window.select_all_jobs_btn.isEnabled()

    # 按钮点击会先弹二次确认，测试里把它短路掉（确认弹窗本身另有守卫）
    window._confirm_select_all_jobs = lambda: True  # type: ignore[method-assign]
    window.select_all_jobs_btn.click()
    wait_until(qapp, lambda: window._utility_thread is None)

    first, second, third = window._jobs
    assert first.accepted == qualifying_pattern_ids(4, (), {"暴击率": 1.0}, {"four": {0: 1.0}})
    assert second.accepted == qualifying_pattern_ids(6, (), {"攻击力%": 1.0, "暴击伤害": 0.5}, {})
    assert first.accepted and second.accepted
    # 空集绝不能落地：accepted=[] 在运行时是「全部判弃置」，不是「一张不勾」
    assert third.accepted is None
    assert "已按各自门槛为 2 个任务勾选" in window.status.text()
    assert "1 个任务没有符合门槛的卡片" in window.status.text()

    # 之后新建的任务不受影响
    window._add_job()
    assert window._jobs[-1].accepted is None
    window.close()


def test_select_all_jobs_disabled_when_chain_empty(qapp: QApplication, tmp_path) -> None:
    window = MainWindow(config_path=tmp_path / "t.yaml")
    assert window._jobs == []
    # 没有任务可勾，按钮置灰（禁用的按钮点不动，所以直接调方法验兜底提示）
    assert not window.select_all_jobs_btn.isEnabled()
    window._select_all_jobs()
    assert "任务链是空的" in window.status.text()
    window.close()


def test_select_all_jobs_cancelled_changes_nothing(qapp: QApplication, tmp_path, monkeypatch) -> None:
    """二次确认点取消 = 一个字都不改，也不该起后台线程。"""
    from zzz_ddtool.config import preset_from_dict

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._apply_preset(preset_from_dict({
        "task_chain": [{"slot": 4, "weights": {"暴击率": 1.0}}],
    }))
    job = window._jobs[0]
    assert job.accepted is None

    asked: list[int] = []

    def reject(self):  # noqa: ANN001
        asked.append(len(self._jobs))
        return False

    monkeypatch.setattr(MainWindow, "_confirm_select_all_jobs", reject)
    window._select_all_jobs()

    assert asked == [1]  # 确实问了
    assert job.accepted is None  # 但没有改
    assert window._utility_thread is None  # 也没启动后台
    assert "已取消" in window.status.text()
    window.close()


def test_select_all_jobs_button_renamed_and_hint_moved_to_dialog(qapp: QApplication, tmp_path) -> None:
    """按钮改名 + 悬停提示撤掉，提示语搬进二次确认弹窗正文。"""
    from PySide6.QtWidgets import QLabel

    from zzz_ddtool.gui import SELECT_ALL_JOBS_HINT, ConfirmSelectAllDialog

    window = MainWindow(config_path=tmp_path / "t.yaml")
    assert window.select_all_jobs_btn.text() == "全任务卡片全选"
    assert window.select_all_jobs_btn.toolTip() == ""

    assert SELECT_ALL_JOBS_HINT.strip()
    dialog = ConfirmSelectAllDialog(3, window)
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert any(SELECT_ALL_JOBS_HINT == text for text in texts)
    assert any("3 个任务" in text for text in texts)
    dialog.close()
    window.close()


def _card_fingerprint(window: MainWindow) -> dict:
    """把每张卡「显示出来的东西」和它携带的图案逐字核对后快照下来。

    复用卡片时是就地改写文字，一旦哪一行漏改，屏幕上就会出现和图案对不上的
    内容——也就是假信息。所以这里不只看 pattern_id，还要看 label 的实际文本。
    """
    out = {}
    for key, section in window.sections.items():
        rows = []
        for card in section.cards:
            shown = [(label.text(), extra.text()) for label, extra in card._slot_rows]
            expect = [
                (slot.name, f"+{slot.bonus}" if slot.bonus > 0 else "")
                for slot in card.pattern.slots
            ]
            assert shown == expect, f"{key} 的卡片文字与图案不符：{shown} != {expect}"
            assert card.score_label.text() == f"{card.pattern.score:g}"
            rows.append((card.pattern.pattern_id, card.pattern.score))
        out[key] = rows
    return out


def _spy_set_cards(monkeypatch, window: MainWindow) -> list:
    """记录哪些分区真的走了「销毁重建」。"""
    rebuilt = []
    for section in window.sections.values():
        original = section.set_cards

        def spy(patterns, is_checked, _original=original, _section=section):
            rebuilt.append(_section)
            return _original(patterns, is_checked)

        monkeypatch.setattr(section, "set_cards", spy)
    return rebuilt


def test_weight_edit_reuses_cards_and_stays_truthful(qapp: QApplication, tmp_path, monkeypatch) -> None:
    """改词条单价只改分值和行的排列，绝不能整屏销毁重建。

    重建八百多张卡的代价是两三秒，其中「摘除旧卡」和「造新卡」各占一半；每敲
    一下单价都重来一遍，就是卡顿的来源。而改单价时穷举的是同一个组合空间，
    每个阶段的张数一张不差，所以可以原地复用、只把文字改掉。

    复用本身有个风险：改了一半的文字会和图案对不上，那就是假信息。所以下面
    还要拿「清空后全量重建」的结果做逐张比对。
    """
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1.0, "暴击伤害": 1.0, "攻击力%": 0.8})
    refresh_cards(window, qapp)
    assert any(section.cards for section in window.sections.values())

    rebuilt = _spy_set_cards(monkeypatch, window)

    # 把「攻击力%」的单价抬到 1.5，跨过 暴击率/暴击伤害 的排序位置
    spin = next(s for combo, s, _ in window.weight_editor._rows if combo.currentText() == "攻击力%")
    spin.setValue(1.5)

    refresh_cards(window, qapp)

    assert rebuilt == []  # 一张卡都没重建，走的是复用
    reused = _card_fingerprint(window)

    # 清空所有卡片再全量重建一次，两条路径的结果必须逐张一致
    for section in window.sections.values():
        section.set_cards([], lambda _pattern_id: False)
    refresh_cards(window, qapp)
    assert _card_fingerprint(window) == reused
    window.close()


def test_card_count_change_falls_back_to_rebuild(qapp: QApplication, tmp_path, monkeypatch) -> None:
    """词条被置零后图案数量会变，数量对不上就不能复用，必须老实重建。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1.0, "暴击伤害": 1.0, "攻击力%": 0.8})
    refresh_cards(window, qapp)
    before = sum(len(section.cards) for section in window.sections.values())
    assert before > 0

    rebuilt = _spy_set_cards(monkeypatch, window)

    spin = next(s for combo, s, _ in window.weight_editor._rows if combo.currentText() == "攻击力%")
    spin.setValue(0.0)  # 置零 = 这个位置的词条不再算「有用词条」

    refresh_cards(window, qapp)

    after = sum(len(section.cards) for section in window.sections.values())
    assert after < before
    assert rebuilt, "张数变了还走复用，卡片会和图案对不上"
    _card_fingerprint(window)
    window.close()


def test_below_threshold_cards_really_stay_hidden(qapp: QApplication, tmp_path) -> None:
    """低于门槛的卡不会创建控件，计数与分页均来自过滤后的模型。"""
    from PySide6.QtCore import Qt

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"攻击力%": 1.0, "暴击率": 1.0, "暴击伤害": 1.0})
    section = window.sections[("three", 12)]
    section.require.setValue(5.0)
    refresh_cards(window, qapp)

    need = section.require.value()
    assert section.cards
    assert any(p.score + 1e-9 < need for p in section.patterns)
    assert all(c.pattern.score + 1e-9 >= need for c in section.cards)
    explicit = Qt.WidgetAttribute.WA_WState_ExplicitShowHide
    assert all(not c.isHidden() and c.testAttribute(explicit) for c in section.cards)
    shown = sum(p.score + 1e-9 >= need for p in section.patterns)
    assert section.count_label.text() == f"{shown} / {len(section.patterns)} 张"
    window.close()


def test_edits_and_switches_do_not_generate_cards(qapp, tmp_path, monkeypatch):
    from zzz_ddtool import gui

    def unexpected(*args, **kwargs):
        pytest.fail("Editing should not enumerate combinations")

    monkeypatch.setattr(gui, "enumerate_patterns", unexpected)
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.add_combo.setCurrentText("暴击率")
    window.weight_editor._add_from_combo()
    window.weight_editor._rows[0][1].setValue(1.5)
    window.max_discs.setValue(7)
    first_id = window._editing_job_id
    window._add_job()
    assert window._jobs[0].weights == {"暴击率": 1.5}
    assert window._jobs[0].max_discs == 7
    window.max_discs.setValue(12)
    window.chain_list.setCurrentRow(0)
    assert window._editing_job_id == first_id
    assert window.max_discs.value() == 7
    assert window._jobs[1].max_discs == 12
    assert window._preview_thread is None
    assert not any(s.cards for s in window.sections.values())
    window.save_current()
    from zzz_ddtool.config import load_preset

    assert [job.max_discs for job in load_preset(window._path).task_chain] == [7, 12]
    window.close()


def test_pagination_preserves_all_pages_and_bulk_selection(qapp, tmp_path):
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1, "暴击伤害": 1, "攻击力%": 1, "穿透值": 0.5})
    refresh_cards(window, qapp)
    section = window.sections[("four", 15)]
    assert len(section.patterns) > section.PAGE_SIZE
    assert len(section.cards) == section.PAGE_SIZE
    first = section.cards[0].pattern.pattern_id
    section.cards[0].checkbox.setChecked(False)
    section.next_btn.click()
    second = section.cards[0].pattern.pattern_id
    assert second != first
    section.cards[0].checkbox.setChecked(False)
    section.previous_btn.click()
    assert not section.cards[0].checkbox.isChecked()
    accepted = window.collect_preset().task_chain[0].accepted
    assert first not in accepted and second not in accepted
    assert set(section._ids) - {first, second} <= set(accepted)
    section.check_visible(False)
    assert not section.visible_accepted()
    section.next_btn.click()
    assert all(not c.checkbox.isChecked() for c in section.cards)
    section.check_visible(True)
    assert set(section.visible_accepted()) == set(section._ids)
    window.close()


def test_unrendered_edits_and_thresholds_are_saved_correctly(qapp, tmp_path):
    from zzz_ddtool.patterns import Pattern, Slot, canonicalize
    from zzz_ddtool.origin import Origin

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    weights = {"暴击率": 1, "暴击伤害": 0.8}
    window.weight_editor.set_weights(weights)
    refresh_cards(window, qapp)
    window.sections[("four", 0)].check_visible(True)
    spin = next(s for c, s, _ in window.weight_editor._rows if c.currentText() == "暴击伤害")
    spin.setValue(1.5)
    assert window._preview_dirty
    preset = window.collect_preset()
    expected = Pattern(Origin.FOUR, 0, canonicalize([
        Slot("暴击率"), Slot("暴击伤害"), Slot("废词条"), Slot("废词条"),
    ], preset.weights), 0).pattern_id
    assert expected in preset.accepted
    window.sections[("four", 0)].require.setValue(3)
    assert expected not in window.collect_preset().accepted
    window.save_current()
    assert window.save_label.objectName() == "saveState"
    window.max_discs.setValue(8)
    assert window.save_label.objectName() == "saveStateIdle"
    window.close()


def test_preview_worker_keeps_gui_responsive_and_discards_stale_result(qapp, tmp_path, monkeypatch):
    import threading
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest
    from zzz_ddtool import gui

    entered = threading.Event()
    release = threading.Event()
    original = gui.enumerate_patterns
    main_id = threading.get_ident()

    def slow(weights, **kwargs):
        assert threading.get_ident() != main_id
        entered.set()
        assert release.wait(3)
        return original(weights, **kwargs)

    monkeypatch.setattr(gui, "enumerate_patterns", slow)
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1})
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    try:
        window.rebuild_cards()
        assert entered.wait(1)
        QTest.qWait(100)
        assert len(ticks) >= 3
        # 切换任务让旧结果作废，旧任务的结果不能出现在新任务里。
        window._add_job()
        release.set()
        wait_until(qapp, lambda: window._preview_thread is None)
        assert not any(s.patterns for s in window.sections.values())
        assert window.weight_editor.weights() == {}
    finally:
        release.set()
        timer.stop()
        window.close()
        wait_until(qapp, lambda: window._preview_thread is None)


def test_refresh_requests_coalesce_and_close_waits_for_worker(qapp, tmp_path, monkeypatch):
    import threading
    from zzz_ddtool import gui

    entered, release = threading.Event(), threading.Event()
    original = gui.enumerate_patterns

    def slow(weights, **kwargs):
        entered.set()
        release.wait(3)
        return original(weights, **kwargs)

    monkeypatch.setattr(gui, "enumerate_patterns", slow)
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1})
    try:
        window.rebuild_cards()
        assert entered.wait(1)
        worker = window._preview_thread
        window.weight_editor._rows[0][1].setValue(2)
        window.rebuild_cards()
        window.weight_editor._rows[0][1].setValue(3)
        window.rebuild_cards()
        assert window._preview_thread is worker
        release.set()
        wait_until(qapp, lambda: window._preview_thread is None and not window._preview_timer.isActive())
        assert not window._preview_dirty
        assert max(p.score for s in window.sections.values() for p in s.patterns) == 18
        entered.clear()
        release.clear()
        window.rebuild_cards()
        assert entered.wait(1)
        window.close()
        assert window._closing
        assert window._preview_thread is not None
    finally:
        release.set()
        window.close()
        wait_until(qapp, lambda: window._preview_thread is None)


def test_utility_runs_off_thread_and_reports_failure(qapp, tmp_path):
    import threading
    from PySide6.QtTest import QTest

    window = MainWindow(config_path=tmp_path / "t.yaml")
    entered, release = threading.Event(), threading.Event()
    calls = []
    main_id = threading.get_ident()

    def operation():
        calls.append(threading.get_ident())
        entered.set()
        release.wait(3)
        raise RuntimeError("test failure")

    try:
        window._start_utility(operation, lambda _result: pytest.fail("Unexpected success"), "working")
        assert entered.wait(1)
        QTest.qWait(20)
        assert calls[0] != main_id
        assert not window.start_btn.isEnabled()
        release.set()
        wait_until(qapp, lambda: window._utility_thread is None)
        assert window.start_btn.isEnabled()
        assert "test failure" in window.status.text()
    finally:
        release.set()
        window.close()
        wait_until(qapp, lambda: window._utility_thread is None)


def test_saving_migrates_all_legacy_task_limits(qapp, tmp_path):
    from zzz_ddtool.config import preset_from_dict, load_preset

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._apply_preset(preset_from_dict({
        "run": {"max_discs": 29},
        "task_chain": [{"slot": 1}, {"slot": 4}, {"slot": 6}],
    }))
    window.max_discs.setValue(5)
    window.save_current()
    assert [job.max_discs for job in load_preset(window._path).task_chain] == [5, 29, 29]
    window.close()


@pytest.mark.parametrize("slot, mainstat, sibling", [
    (1, "生命值", "生命值%"),
    (2, "攻击力", "攻击力%"),
    (3, "防御力", "防御力%"),
    (4, "攻击力%", "攻击力"),
    (5, "生命值%", "生命值"),
    (6, "防御力%", "防御力"),
])
def test_mainstat_has_priority_over_weights_everywhere(qapp, tmp_path, slot, mainstat, sibling):
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window._on_slot_clicked(slot)
    if slot >= 4:
        box = window.filter_main_boxes[mainstat]
        assert box.parent() is window._mainstat_pages[slot]
        box.setChecked(True)
    assert mainstat in window._blocked_substats()
    window.weight_editor.set_weights({
        mainstat: 20, sibling: 1, "暴击率": 1,
    })
    window._selection_seed = (f"four:0:{mainstat}|{sibling}|暴击率|废词条",)
    refresh_cards(window, qapp)
    names = {slot.name for s in window.sections.values() for p in s.patterns for slot in p.slots}
    assert mainstat not in names
    assert sibling in names
    preset = window.collect_preset()
    assert preset.weights[mainstat] == 20
    assert preset.task_chain[0].weights[mainstat] == 20
    assert mainstat not in preset.effective_weights()
    assert preset.accepted == ()
    for combo, _rolls in window.trial.rows:
        assert mainstat not in [combo.itemText(i) for i in range(combo.count())]
    window.close()


@pytest.mark.parametrize("slot", [4, 5, 6])
def test_mainstats_are_exclusive_and_prices_survive_switches(qapp, tmp_path, slot):
    from zzz_ddtool.config import load_preset

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window._on_slot_clicked(slot)
    prices = {"生命值%": 2, "攻击力%": 3, "防御力%": 4}
    window.weight_editor.set_weights(prices)
    window.filter_main_boxes["生命值%"].click()
    window.filter_main_boxes["攻击力%"].click()
    assert window._selected_mains() == ["攻击力%"]
    assert window.weight_editor.weights() == prices
    window._add_job()
    window.chain_list.setCurrentRow(0)
    assert window._selected_mains() == ["攻击力%"]
    assert window.weight_editor.weights() == prices
    window._main_any_by_slot[slot].click()
    assert window._selected_mains() == []
    assert window._effective_weights() == prices
    window.save_current()
    job = load_preset(window._path).task_chain[0]
    assert job.weights == prices and job.mainstats == ()
    window.close()


def test_loading_legacy_multi_mainstats_normalizes_unvisited_tasks(qapp, tmp_path):
    from zzz_ddtool.config import preset_from_dict

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._apply_preset(preset_from_dict({"task_chain": [
        {"slot": 4, "mainstats": ["暴击伤害", "暴击率"]},
        {"slot": 5, "mainstats": ["攻击力%", "生命值%"]},
    ]}))
    assert [job.mainstats for job in window.collect_preset().task_chain] == [("暴击伤害",), ("攻击力%",)]
    window.close()


def test_apply_buttons_disabled_without_tasks(qapp, tmp_path):
    """任务链空了就没有可铺的对象，三个按钮一起置灰。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    assert not window.apply_weights_btn.isEnabled()
    assert not window.apply_limit_btn.isEnabled()
    assert not window.apply_sets_btn.isEnabled()
    window._add_job()
    assert window.apply_weights_btn.isEnabled()
    assert window.apply_limit_btn.isEnabled()
    assert window.apply_sets_btn.isEnabled()
    window._remove_job()
    assert not window.apply_weights_btn.isEnabled()
    window.close()


def test_apply_weights_to_all_tasks_is_one_shot(qapp, tmp_path):
    """「应用于所有任务」只铺到此刻已存在的任务；之后新建的任务不受影响。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1})
    window._add_job()
    window._on_slot_clicked(6)
    window.weight_editor.set_weights({"攻击力%": 3})

    window.apply_weights_btn.click()
    qapp.processEvents()
    assert [job.weights for job in window._jobs] == [{"攻击力%": 3.0}, {"攻击力%": 3.0}]
    assert window.weight_editor.weights() == {"攻击力%": 3.0}

    window._add_job()
    qapp.processEvents()
    assert window._jobs[-1].weights == {}, "新建的任务不能被之前的「应用全部」绑住"
    window.close()


def test_apply_limit_to_all_tasks_is_one_shot(qapp, tmp_path):
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window._add_job()
    window.chain_list.setCurrentRow(0)
    qapp.processEvents()
    window.max_discs.setValue(7)

    window.apply_limit_btn.click()
    qapp.processEvents()
    assert [job.max_discs for job in window._jobs] == [7, 7]

    window._add_job()
    assert window._jobs[-1].max_discs == 100
    window.close()


def test_apply_sets_to_all_tasks_is_one_shot(qapp, tmp_path):
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window._filter_sets = ["折枝剑歌"]
    window._update_set_summary()
    window._add_job()
    assert window._filter_sets == []
    window.chain_list.setCurrentRow(0)
    qapp.processEvents()
    assert window._filter_sets == ["折枝剑歌"]

    window.apply_sets_btn.click()
    qapp.processEvents()
    assert [job.sets for job in window._jobs] == [("折枝剑歌",), ("折枝剑歌",)]

    window._add_job()
    assert window._jobs[-1].sets == ()
    window.close()


def test_game_filter_is_inline_and_slots_123_have_no_mainstat_choice(qapp, tmp_path):
    """游戏内筛选拆成面板上两块：套装另开弹窗，部位/主词条不再进弹窗。

    1/2/3 号位主词条固定，界面上不给单选项；4/5/6 才出本部位能出的主词条。
    """
    from PySide6.QtWidgets import QDialog

    window = MainWindow(config_path=tmp_path / "t.yaml")
    assert not hasattr(window, "_filter_dialog")
    # 控件挂在主窗口里，不在任何弹窗里
    assert window.filter_slots[4].window() is window
    assert window.main_host.window() is window
    assert not isinstance(window.main_host.parentWidget(), QDialog)

    for slot in (1, 2, 3):
        window._on_slot_clicked(slot)
        qapp.processEvents()
        assert window.filter_main_boxes == {}, f"{slot} 号位不该有主词条可选项"
        assert "固定" in window.main_hint.text()

    window._on_slot_clicked(5)
    qapp.processEvents()
    assert "穿透率" in window.filter_main_boxes
    assert "暴击伤害" not in window.filter_main_boxes
    assert "可选主词条" in window.main_hint.text()
    window.close()


def test_start_rejects_tasks_without_effective_prices(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: errors.append(text))
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._on_start_clicked()
    assert "添加" in errors[-1]
    window._add_job()
    window._on_slot_clicked(1)
    window.weight_editor.set_weights({"生命值": 2})
    window._on_start_clicked()
    assert "正权重" in errors[-1]
    assert window._run_thread is None
    window.close()


def test_counts_and_bulk_selection_share_the_active_origin(qapp, tmp_path):
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1, "暴击伤害": 1})
    refresh_cards(window, qapp)
    for index, origin in enumerate(("three", "four")):
        window.tabs.setCurrentIndex(index)
        window.sections[(origin, 0)].require.setValue(2)
        sections = window._current_sections()
        visible = sum(len(section._visible_indices) for section in sections)
        total = sum(len(section.patterns) for section in sections)
        assert total > visible > 0
        window._check_visible(False)
        assert f"组合共 {total} 种，符合门槛 {visible}，已勾选 0" in window.status.text()
        assert all(not section.visible_accepted() for section in sections)
        window._check_visible(True)
        assert f"已勾选 {visible}" in window.status.text()
        assert sum(len(section.visible_accepted()) for section in sections) == visible
    window.close()


def test_save_without_preview_keeps_all_tasks_and_manual_choices(qapp, tmp_path):
    from zzz_ddtool.config import load_preset
    from zzz_ddtool.runner import Runner

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1, "暴击伤害": 20})
    window.filter_main_boxes["暴击伤害"].click()
    refresh_cards(window, qapp)
    section = window.sections[("four", 0)]
    pid = next(pid for pid in section.visible_accepted() if "暴击率" in pid)
    next(card for card in section.cards if card.pattern.pattern_id == pid).checkbox.click()
    window._add_job()
    window._on_slot_clicked(6)
    window.weight_editor.set_weights({"攻击力%": 3})
    window.max_discs.setValue(15)
    assert window._preview_dirty
    window.save_current()
    saved = load_preset(window._path)
    first, second = saved.task_chain
    assert first.weights == {"暴击率": 1, "暴击伤害": 20}
    assert pid not in first.accepted and len(first.accepted) > 0
    assert second.weights == {"攻击力%": 3} and second.accepted is None
    assert second.max_discs == 15
    runner = Runner(saved)
    assert runner._jobs() == saved.task_chain
    assert runner._sub_preset(first).effective_weights() == {"暴击率": 1}
    disc = Disc(level=0, substats=tuple(Substat(name) for name in ("暴击率", "生命值", "攻击力", "防御力")))
    from zzz_ddtool.decide import decide

    decision = decide(disc, runner._sub_preset(first), first.accepted)
    assert decision.pattern_id == pid and decision.action is Action.MARK_DISCARD
    disc2 = Disc(level=0, substats=tuple(Substat(name) for name in ("攻击力%", "生命值", "攻击力", "防御力")))
    assert decide(disc2, runner._sub_preset(second), second.accepted).action is Action.UPGRADE
    window._apply_preset(saved)
    refresh_cards(window, qapp)
    assert pid not in window.sections[("four", 0)].visible_accepted()
    window.close()


def test_shared_mainstat_controls_follow_the_active_slot(qapp, tmp_path):
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    first = window._editing_job_id
    window.filter_main_boxes["攻击力%"].setChecked(True)
    assert window._selected_mains() == ["攻击力%"]
    window._add_job()
    window._on_slot_clicked(5)
    assert not window.filter_main_boxes["攻击力%"].isChecked()
    window.filter_main_boxes["生命值%"].setChecked(True)
    window.chain_list.setCurrentRow(0)
    assert window._editing_job_id == first
    assert window.filter_main_boxes["攻击力%"].isChecked()
    assert not window.filter_main_boxes["生命值%"].isChecked()
    assert window._blocked_substats() == {"攻击力%"}
    assert window._jobs[1].mainstats == ("生命值%",)
    window.close()


def test_gui_disables_legacy_useful_quota_for_all_tasks(qapp, tmp_path):
    from PySide6.QtWidgets import QLabel
    from zzz_ddtool.config import preset_from_dict

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._apply_preset(preset_from_dict({
        "min_useful_types": 4,
        "slot_plans": {5: {"min_useful_types": 3}},
        "task_chain": [
            {"slot": 4, "weights": {"暴击率": 1}, "min_useful_types": 4},
            {"slot": 5, "weights": {"暴击率": 1}, "min_useful_types": 3},
        ],
    }))
    preset = window.collect_preset()
    assert preset.min_useful_types == 0
    assert all(job.min_useful_types == 0 for job in preset.task_chain)
    assert all(plan["min_useful_types"] == 0 for plan in preset.slot_plans.values())
    assert not hasattr(window, "min_useful")
    assert "最少有效种类" not in [label.text() for label in window.findChildren(QLabel)]
    window.close()


def test_gui_preserves_global_label_policy_before_advanced_settings(qapp, tmp_path):
    from zzz_ddtool.config import preset_from_dict

    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._apply_preset(preset_from_dict({
        "labels": {"discard": False, "save": False},
        "task_chain": [{"slot": 4, "weights": {"暴击率": 1}}],
    }))
    preset = window.collect_preset()
    assert not preset.labels.discard
    assert not preset.labels.save
    window.close()


def test_window_layout_is_remembered_across_restarts(qapp: QApplication, tmp_path) -> None:
    """窗口大小/三栏比例/置顶要记住：调完关掉再开，不能又回到默认那一坨。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window.show()
    qapp.processEvents()
    window.resize(1500, 900)
    window._config_splitter.setSizes([260, 900, 300])
    window._main_splitter.setSizes([300, 520])
    window.stay_on_top.setChecked(True)
    qapp.processEvents()
    window.close()

    saved = read_layout()
    assert saved["on_top"] is True
    assert saved["geometry"]
    assert len(saved["config_splitter"]) == 3
    assert len(saved["main_splitter"]) == 2
    saved_main = list(saved["main_splitter"])

    reopened = MainWindow(config_path=tmp_path / "t.yaml")
    reopened.show()
    qapp.processEvents()
    assert reopened.stay_on_top.isChecked()
    sizes = reopened._config_splitter.sizes()
    # 出厂默认是 [240, 340, 740]（中间最窄）。恢复成功才会是中间最宽。
    assert sizes[1] > sizes[2]
    # 下面那块的比例照原样恢复（默认 [500, 270]，拖过之后不该回到默认）
    main_sizes = reopened._main_splitter.sizes()
    saved_ratio = saved_main[1] / sum(saved_main)
    assert abs(main_sizes[1] / sum(main_sizes) - saved_ratio) < 0.1
    assert saved_ratio > 0.5
    reopened.close()


def test_close_event_writes_layout_file(qapp: QApplication, tmp_path) -> None:
    """关窗时必须落盘，否则重启就丢。"""
    from zzz_ddtool import paths

    target = paths.layout_file()
    assert not target.exists()
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window.show()
    qapp.processEvents()
    window.close()
    assert target.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"config_splitter": "oops"},
        {"config_splitter": [1, 2]},
        {"config_splitter": [None, None, None]},
        {"main_splitter": [1]},
        {"main_splitter": "oops"},
        {"geometry": 123},
        {"geometry": ""},
        {"on_top": "yes"},
    ],
)
def test_restore_layout_tolerates_broken_file(qapp: QApplication, tmp_path, payload: dict) -> None:
    """layout.json 被手改坏了，界面也得能起来，不能崩在启动。"""
    write_layout(payload)
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window.close()


def test_read_layout_ignores_non_dict(qapp: QApplication, tmp_path) -> None:
    from zzz_ddtool import paths

    target = paths.layout_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_layout() == {}


def test_run_log_title_row_has_collapse_toggle(qapp: QApplication, tmp_path) -> None:
    """运行日志标题行：[运行日志] [▼]  日志文件：…；点箭头只收日志框，标题行留着。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window.show()
    qapp.processEvents()

    # 高度锁在 60~160：往下留一行能看，往上不让它无限霸占窗口
    assert window.run_log.minimumHeight() == 60
    assert window.run_log.maximumHeight() == 160

    # 默认展开：倒三角朝下，日志框可见
    assert window.log_toggle.isChecked()
    assert window.log_toggle.text() == "▼"
    assert window.run_log.isVisible()

    # 位置：标题 → 箭头 → 路径，且箭头和路径之间留了间距
    assert window.log_title.x() < window.log_toggle.x() < window.log_path.x()
    gap = window.log_path.x() - (window.log_toggle.x() + window.log_toggle.width())
    assert gap >= 24, gap

    # 收起：日志框藏掉，箭头转正，标题和路径照旧留着
    window.log_toggle.setChecked(False)
    qapp.processEvents()
    assert not window.run_log.isVisible()
    assert window.log_toggle.text() == "▶"
    assert window.log_title.isVisible()
    assert window.log_path.isVisible()

    # 再展开
    window.log_toggle.setChecked(True)
    qapp.processEvents()
    assert window.run_log.isVisible()
    assert window.log_toggle.text() == "▼"
    window.close()


def test_red_line_splitter_grows_upper_block_only(qapp: QApplication, tmp_path) -> None:
    """红线那条拉伸：只改红框（三栏 + 操作栏）的高度。

    下面那块（状态行 + 日志）高度锁住不动，只是被顶着上下移动；
    窗口最底下那条边才改下面那块——也就是日志窗的高度。
    """
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window.resize(1320, 900)
    window.show()
    qapp.processEvents()

    splitter = window._main_splitter
    assert splitter.count() == 2
    upper, lower = splitter.widget(0), splitter.widget(1)
    # 红框 = 三栏 + 操作栏；下面那块 = 状态行 + 日志面板
    assert upper.isAncestorOf(window._config_splitter)
    assert upper.isAncestorOf(window.start_btn)
    assert lower.isAncestorOf(window.run_status_label)
    assert lower.isAncestorOf(window.run_log)

    window_height = window.height()
    upper_before = upper.height()
    log_before = window.run_log.height()

    # 模拟把红线往下拖 120px：上半 +120、下半 -120
    splitter.setSizes([upper_before + 120, lower.height() - 120])
    qapp.processEvents()
    window._on_main_splitter_moved(splitter.handle(1).y(), 1)
    qapp.processEvents()

    # 红框长高了，窗口跟着长高
    assert upper.height() > upper_before
    assert window.height() > window_height
    # 下面那块高度锁住，只被顶下去了
    assert abs(window.run_log.height() - log_before) <= 4
    window.close()


def test_window_resize_only_grows_lower_block(qapp: QApplication, tmp_path) -> None:
    """窗口最底下那条边：拉高窗口只让日志窗变高，红框（三栏 + 操作栏）高度不动。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window.resize(1320, 820)
    window.show()
    qapp.processEvents()

    upper_before = window._main_splitter.widget(0).height()
    log_before = window.run_log.height()

    window.resize(1320, 1000)
    qapp.processEvents()

    assert window.run_log.height() > log_before
    assert window._main_splitter.widget(0).height() == upper_before


def test_window_shrink_stops_at_log_floor_never_squeezes_upper(qapp: QApplication, tmp_path) -> None:
    """窗口底边向上缩：只缩日志窗；缩到日志最小高度后窗口就缩不动，三栏永不被挤压。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window.resize(1320, 900)
    window.show()
    qapp.processEvents()

    upper = window._main_splitter.widget(0)
    upper_before = upper.height()

    # 一路向上缩，越过日志窗的最小高度
    for h in (800, 750, 700, 650):
        window.resize(1320, h)
        qapp.processEvents()
        assert upper.height() == upper_before, h
        assert window.run_log.height() >= window.run_log.minimumHeight(), h

    # 日志触底后再缩：窗口停在最矮处，三栏还是原样
    floor_height = window.height()
    window.resize(1320, 500)
    qapp.processEvents()
    assert window.height() == floor_height
    assert upper.height() == upper_before
    window.close()


def test_window_grow_stops_at_log_ceiling(qapp: QApplication, tmp_path) -> None:
    """窗口底边向下拉：日志涨到 160 就封顶，窗口不再被拉长，三栏也不跟着长。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window.resize(1320, 900)
    window.show()
    qapp.processEvents()

    upper = window._main_splitter.widget(0)
    upper_before = upper.height()
    ceiling = window.run_log.maximumHeight()
    # 900 高时日志已经顶到上限，窗口高度此刻就是最高点
    top = window.height()
    assert window.run_log.height() == ceiling

    # 一路向下拉：窗口不长、日志不长、三栏不长
    for h in (1000, 1100, 1400):
        window.resize(1320, h)
        qapp.processEvents()
        assert window.height() == top, h
        assert window.run_log.height() == ceiling, h
        assert upper.height() == upper_before, h
    window.close()


def test_window_height_is_clamped_not_pinned(qapp: QApplication, tmp_path) -> None:
    """窗口不给自己设最大高度——钉死成定长后，Windows 会把拖边框退化成移动窗口。

    上下限只在 resizeEvent 里钳，所以窗口属性本身必须保持"可自由拉伸"（未设 max）。
    """
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window.resize(1320, 900)
    window.show()
    qapp.processEvents()

    assert window.maximumHeight() == 16_777_215  # 没被 setMaximumHeight 钉住
    # 但内部限值是真的——往上拉仍被钳回去
    assert window._window_max_height == window.height()
    window.resize(1320, 1500)
    qapp.processEvents()
    assert window.height() == 900
    window.close()


def test_run_status_swaps_to_result_stats_when_finished(qapp: QApplication, tmp_path) -> None:
    """跑完：结果统计顶掉「运行状态/当前任务/已处理」三行。

    界面**只给一行摘要 + 详细日志路径**，不贴详细日志正文；运行中收起这块。
    """
    window = MainWindow(config_path=tmp_path / "t.yaml")

    running = {"phase": "running", "processed": 1, "max_discs": 10, "message": "处理中"}
    window._show_run_status(running)
    assert "运行状态：running" in window.run_status_label.text()
    assert window.run_stats_path_label.isHidden()

    report = tmp_path / "20261004-200000.md"
    report.write_text("# 运行结果统计 · t\n\n## 总览\n", encoding="utf-8")
    window._show_run_status(
        {
            "phase": "completed",
            "stats_path": str(report),
            "stats_headline": "共计耗时 1 分 00 秒，处理了 0 个盘，升满成功 0 件，判定弃置 0 件。",
        }
    )
    assert not window.run_stats_path_label.isHidden()
    assert (
        window.run_status_label.text()
        == "共计耗时 1 分 00 秒，处理了 0 个盘，升满成功 0 件，判定弃置 0 件。"
    )
    # 只给路径，不显示详细日志正文
    shown = window.run_stats_path_label.text()
    assert str(report) in shown
    assert "总览" not in shown

    # 下一轮开始：结果统计区必须收起来，别把上一轮的留在屏上
    window._show_run_status(running)
    assert window.run_stats_path_label.isHidden()
