import os

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from zzz_ddtool.gui import MainWindow, WeightEditor
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
    window.rebuild_cards()

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
    window.rebuild_cards()
    assert any(section.cards for section in window.sections.values())

    rebuilt = _spy_set_cards(monkeypatch, window)

    # 把「攻击力%」的单价抬到 1.5，跨过 暴击率/暴击伤害 的排序位置
    spin = next(s for combo, s, _ in window.weight_editor._rows if combo.currentText() == "攻击力%")
    spin.setValue(1.5)
    window._rebuild_timer.stop()  # 不等防抖，直接跑
    window.rebuild_cards()

    assert rebuilt == []  # 一张卡都没重建，走的是复用
    reused = _card_fingerprint(window)

    # 清空所有卡片再全量重建一次，两条路径的结果必须逐张一致
    for section in window.sections.values():
        section.set_cards([], lambda _pattern_id: False)
    window.rebuild_cards()
    assert _card_fingerprint(window) == reused
    window.close()


def test_card_count_change_falls_back_to_rebuild(qapp: QApplication, tmp_path, monkeypatch) -> None:
    """词条被置零后图案数量会变，数量对不上就不能复用，必须老实重建。"""
    window = MainWindow(config_path=tmp_path / "t.yaml")
    window._add_job()
    window.weight_editor.set_weights({"暴击率": 1.0, "暴击伤害": 1.0, "攻击力%": 0.8})
    window.rebuild_cards()
    before = sum(len(section.cards) for section in window.sections.values())
    assert before > 0

    rebuilt = _spy_set_cards(monkeypatch, window)

    spin = next(s for combo, s, _ in window.weight_editor._rows if combo.currentText() == "攻击力%")
    spin.setValue(0.0)  # 置零 = 这个位置的词条不再算「有用词条」
    window._rebuild_timer.stop()
    window.rebuild_cards()

    after = sum(len(section.cards) for section in window.sections.values())
    assert after < before
    assert rebuilt, "张数变了还走复用，卡片会和图案对不上"
    _card_fingerprint(window)
    window.close()
