"""Desktop window: weight editor + pattern cards + trial decide.

Layout follows genshin_artifact_auxiliary (left form, PySide6, dark Fusion)
and AdeptiScanner-ZZZ (one main window, start/stop bar, stay-on-top toggle).
Cards are the same 4-slot panels that used to live in the HTML preview.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from zzz_ddtool.config import Preset, load_preset, preset_from_dict, save_preset
from zzz_ddtool.decide import decide
from zzz_ddtool.models import Disc, Substat
from zzz_ddtool.origin import Origin
from zzz_ddtool.patterns import CHECKPOINTS, EMPTY, JUNK, Pattern, enumerate_all

KNOWN_SUBSTATS = (
    "暴击率",
    "暴击伤害",
    "攻击力%",
    "攻击力",
    "生命值%",
    "生命值",
    "防御力%",
    "防御力",
    "异常精通",
    "穿透率",
    "异常掌控",
)

DARK_STYLESHEET = """
QMainWindow, QWidget#root, QScrollArea, QScrollArea > QWidget > QWidget {
    background: #141414;
    color: #eee;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
}
QLabel { color: #eee; }
QLabel#hint { color: #8d8d8d; }
QLabel#bannerTitle { font-size: 20px; font-weight: 700; color: #f0d060; }
QLabel#score { color: #f0d060; font-size: 13px; font-weight: 600; }
QLabel#junk { color: #7a7a7a; }
QLabel#empty { color: #5c5c5c; font-style: italic; }
QLabel#slot { color: #eee; }
QLabel#bonus { color: #8ab4f8; font-size: 12px; }
QLabel#stageLevel {
    color: #f0d060; font-size: 15px; font-weight: 700; min-width: 36px;
}
QLabel#status { color: #9aa; }
QGroupBox {
    border: 1px solid #2a2a2a;
    border-radius: 8px;
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
    color: #ddd;
    background: #1a1a1a;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #cfcfcf; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #101010;
    color: #eee;
    border: 1px solid #333;
    border-radius: 5px;
    padding: 5px 8px;
    min-height: 24px;
}
QComboBox QAbstractItemView {
    background: #1b1b1b;
    color: #eee;
    selection-background-color: #f0d060;
    selection-color: #111;
}
QPushButton {
    background: #222;
    color: #eee;
    border: 1px solid #3d3d3d;
    border-radius: 5px;
    padding: 6px 12px;
}
QPushButton:hover { background: #2c2c2c; border-color: #f0d060; }
QPushButton:disabled { color: #666; border-color: #2a2a2a; }
QPushButton#primary {
    background: #3d3420;
    border-color: #f0d060;
    color: #f0d060;
    padding: 8px 18px;
    font-weight: 600;
}
QTabWidget::pane { border: 1px solid #2a2a2a; background: #141414; top: -1px; }
QTabBar::tab {
    background: #1a1a1a;
    color: #999;
    padding: 9px 18px;
    border: 1px solid #2a2a2a;
    border-bottom: none;
    margin-right: 4px;
}
QTabBar::tab:selected { color: #f0d060; background: #141414; border-bottom: 2px solid #f0d060; }
QCheckBox { color: #ddd; spacing: 8px; }
QCheckBox::indicator {
    width: 15px; height: 15px; border: 1px solid #555; border-radius: 3px; background: #101010;
}
QCheckBox::indicator:checked { background: #f0d060; border-color: #f0d060; }
QFrame#card {
    background: #1b1b1b;
    border: 1px solid #2f2f2f;
    border-radius: 8px;
}
QFrame#card:hover { border-color: #666; }
QFrame#card[checked="true"] { border: 1px solid #f0d060; }
QFrame#card[checked="false"] { background: #161616; }
QFrame#cardHead { background: #242018; border-bottom: 1px solid #3a3424; }
QFrame#stageBar {
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 6px;
}
QFrame#footer {
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 8px;
}
QSplitter::handle { background: #2a2a2a; width: 6px; }
QScrollBar:vertical { background: #141414; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3a3a3a; min-height: 32px; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""

LIGHT_STYLESHEET = """
QMainWindow, QWidget#root, QScrollArea, QScrollArea > QWidget > QWidget {
    background: #f3f3f3;
    color: #222;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
}
QLabel { color: #222; }
QLabel#hint { color: #777; }
QLabel#bannerTitle { font-size: 20px; font-weight: 700; color: #8a6d12; }
QLabel#score { color: #8a6d12; font-size: 13px; font-weight: 600; }
QLabel#junk { color: #8a8a8a; }
QLabel#empty { color: #9a9a9a; font-style: italic; }
QLabel#slot { color: #222; }
QLabel#bonus { color: #1a5fb4; font-size: 12px; }
QLabel#stageLevel {
    color: #8a6d12; font-size: 15px; font-weight: 700; min-width: 36px;
}
QLabel#status { color: #666; }
QGroupBox {
    border: 1px solid #e2e2e2;
    border-radius: 8px;
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
    color: #333;
    background: #ffffff;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #555; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #ffffff;
    color: #222;
    border: 1px solid #d0d0d0;
    border-radius: 5px;
    padding: 5px 8px;
    min-height: 24px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    color: #222;
    selection-background-color: #f0d060;
    selection-color: #111;
}
QPushButton {
    background: #ffffff;
    color: #222;
    border: 1px solid #d0d0d0;
    border-radius: 5px;
    padding: 6px 12px;
}
QPushButton:hover { background: #fff8e0; border-color: #c9a227; }
QPushButton:disabled { color: #999; border-color: #e5e5e5; background: #f0f0f0; }
QPushButton#primary {
    background: #fff1b8;
    border-color: #c9a227;
    color: #6b5410;
    padding: 8px 18px;
    font-weight: 600;
}
QTabWidget::pane { border: 1px solid #e2e2e2; background: #f3f3f3; top: -1px; }
QTabBar::tab {
    background: #ffffff;
    color: #666;
    padding: 9px 18px;
    border: 1px solid #e2e2e2;
    border-bottom: none;
    margin-right: 4px;
}
QTabBar::tab:selected { color: #8a6d12; background: #f3f3f3; border-bottom: 2px solid #c9a227; }
QCheckBox { color: #333; spacing: 8px; }
QCheckBox::indicator {
    width: 15px; height: 15px; border: 1px solid #c4c4c4; border-radius: 3px; background: #fff;
}
QCheckBox::indicator:checked { background: #c9a227; border-color: #c9a227; }
QFrame#card {
    background: #ffffff;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
}
QFrame#card:hover { border-color: #bdbdbd; }
QFrame#card[checked="true"] { border: 1px solid #c9a227; }
QFrame#card[checked="false"] { background: #fafafa; }
QFrame#cardHead { background: #fff6d4; border-bottom: 1px solid #ecd98a; }
QFrame#stageBar {
    background: #ffffff;
    border: 1px solid #e2e2e2;
    border-radius: 6px;
}
QFrame#footer {
    background: #ffffff;
    border: 1px solid #e2e2e2;
    border-radius: 8px;
}
QSplitter::handle { background: #e2e2e2; width: 6px; }
QScrollBar:vertical { background: #f3f3f3; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #cfcfcf; min-height: 32px; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""

THEMES = {"dark": DARK_STYLESHEET, "light": LIGHT_STYLESHEET}


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "configs" / "example.yaml"


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 12) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        x = rect.x()
        y = rect.y()
        line_height = 0
        space = self.spacing()
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + space
            if next_x - space > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + space
                next_x = x + hint.width() + space
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


class PatternCard(QFrame):
    toggled = Signal()

    def __init__(self, pattern: Pattern, checked: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pattern = pattern
        self.setObjectName("card")
        self.setFixedWidth(188)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("checked", "true" if checked else "false")

        self.checkbox = QCheckBox()
        self.checkbox.setChecked(checked)
        self.checkbox.toggled.connect(self._on_box)

        score = QLabel(f"{pattern.score:g}")
        score.setObjectName("score")
        score.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        head = QFrame()
        head.setObjectName("cardHead")
        head_row = QHBoxLayout(head)
        head_row.setContentsMargins(8, 5, 8, 5)
        head_row.addWidget(self.checkbox)
        head_row.addStretch()
        head_row.addWidget(score)

        body = QVBoxLayout()
        body.setContentsMargins(10, 6, 10, 8)
        body.setSpacing(2)
        for slot in pattern.slots:
            row = QHBoxLayout()
            name = QLabel(slot.name)
            extra = QLabel(f"+{slot.bonus}" if slot.bonus > 0 else "")
            extra.setObjectName("bonus")
            if slot.name == JUNK:
                name.setObjectName("junk")
            elif slot.name == EMPTY:
                name.setObjectName("empty")
            else:
                name.setObjectName("slot")
            row.addWidget(name, 1)
            row.addWidget(extra)
            body.addLayout(row)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(head)
        layout.addLayout(body)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if child is not self.checkbox:
                self.checkbox.toggle()
                event.accept()
                return
        super().mousePressEvent(event)

    def _on_box(self, checked: bool) -> None:
        self.setProperty("checked", "true" if checked else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.toggled.emit()

    @property
    def accepted(self) -> bool:
        return (not self.isHidden()) and self.checkbox.isChecked()


class StageSection(QWidget):
    require_changed = Signal()
    selection_changed = Signal()

    def __init__(
        self,
        origin: Origin,
        checkpoint: int,
        require: float | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.origin = origin
        self.checkpoint = checkpoint
        self.cards: list[PatternCard] = []

        self.require = QDoubleSpinBox()
        self.require.setDecimals(1)
        self.require.setRange(0.0, 30.0)
        self.require.setSingleStep(0.1)
        self.require.setValue(0.0 if require is None else require)
        self.require.valueChanged.connect(self._on_require)

        self.count_label = QLabel()
        self.count_label.setObjectName("hint")

        level = QLabel(f"+{checkpoint}")
        level.setObjectName("stageLevel")
        require_label = QLabel("本档要求")
        require_label.setObjectName("hint")

        bar = QFrame()
        bar.setObjectName("stageBar")
        header = QHBoxLayout(bar)
        header.setContentsMargins(10, 6, 10, 6)
        header.addWidget(level)
        header.addWidget(require_label)
        header.addWidget(self.require)
        header.addStretch()
        header.addWidget(self.count_label)

        self.flow_host = QWidget()
        self.flow = FlowLayout(self.flow_host, spacing=10)
        self.flow_host.setLayout(self.flow)
        self.flow_host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 14)
        layout.addWidget(bar)
        layout.addWidget(self.flow_host)

    def set_cards(self, patterns: list[Pattern], is_checked) -> None:  # noqa: ANN001
        while self.flow.count():
            item = self.flow.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.cards = []
        for pattern in patterns:
            card = PatternCard(pattern, checked=bool(is_checked(pattern.pattern_id)))
            card.toggled.connect(self.selection_changed.emit)
            self.flow.addWidget(card)
            self.cards.append(card)
        self.apply_filter()

    def apply_filter(self) -> None:
        need = self.require.value()
        shown = 0
        for card in self.cards:
            visible = card.pattern.score + 1e-9 >= need
            card.setVisible(visible)
            if not card.isHidden():
                shown += 1
        self.count_label.setText(f"{shown} / {len(self.cards)} 张")
        self.flow_host.updateGeometry()
        self.updateGeometry()

    def check_visible(self, on: bool) -> None:
        for card in self.cards:
            if not card.isHidden():
                card.checkbox.setChecked(on)

    def visible_accepted(self) -> list[str]:
        return [card.pattern.pattern_id for card in self.cards if card.accepted]

    def _on_require(self, _value: float) -> None:
        self.apply_filter()
        self.require_changed.emit()
        self.selection_changed.emit()


class WeightEditor(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[QComboBox, QDoubleSpinBox, QWidget]] = []
        self._box = QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._rows_box = QVBoxLayout()
        self._box.addLayout(self._rows_box)

        add_row = QHBoxLayout()
        self.add_combo = QComboBox()
        self.add_combo.setEditable(True)
        self.add_combo.addItems(KNOWN_SUBSTATS)
        add_btn = QPushButton("添加词条")
        add_btn.clicked.connect(self._add_from_combo)
        add_row.addWidget(self.add_combo, 1)
        add_row.addWidget(add_btn)
        self._box.addLayout(add_row)

    def set_weights(self, weights: dict[str, float]) -> None:
        while self._rows:
            self._remove_row(self._rows[0][2])
        for name, value in weights.items():
            self._add_row(name, value)

    def weights(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for combo, spin, _row in self._rows:
            name = combo.currentText().strip()
            if not name:
                continue
            out[name] = float(spin.value())
        return out

    def _add_from_combo(self) -> None:
        name = self.add_combo.currentText().strip()
        if not name or name in self.weights():
            return
        self._add_row(name, 1.0)
        self.changed.emit()

    def _add_row(self, name: str, value: float) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(KNOWN_SUBSTATS)
        combo.setCurrentText(name)
        spin = QDoubleSpinBox()
        spin.setDecimals(1)
        spin.setRange(0.0, 20.0)
        spin.setSingleStep(0.1)
        spin.setValue(value)
        remove = QPushButton("×")
        remove.setFixedWidth(28)
        remove.clicked.connect(lambda: self._delete(row))
        combo.currentTextChanged.connect(lambda *_: self.changed.emit())
        spin.valueChanged.connect(lambda *_: self.changed.emit())
        layout.addWidget(combo, 1)
        layout.addWidget(spin)
        layout.addWidget(remove)
        self._rows_box.addWidget(row)
        self._rows.append((combo, spin, row))

    def _delete(self, row: QWidget) -> None:
        self._remove_row(row)
        self.changed.emit()

    def _remove_row(self, row: QWidget) -> None:
        self._rows = [item for item in self._rows if item[2] is not row]
        row.deleteLater()


class TrialPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.level = QSpinBox()
        self.level.setRange(0, 15)
        self.locked = QCheckBox("锁定")
        self.equipped = QCheckBox("已装备")
        self.discarded = QCheckBox("已有弃置标记")
        self.rows: list[tuple[QComboBox, QSpinBox]] = []

        form = QFormLayout()
        form.addRow("等级", self.level)
        flags = QHBoxLayout()
        flags.addWidget(self.locked)
        flags.addWidget(self.equipped)
        flags.addWidget(self.discarded)
        flags.addStretch()
        form.addRow("状态", flags)

        for index in range(4):
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItem("")
            combo.addItems(KNOWN_SUBSTATS)
            rolls = QSpinBox()
            rolls.setRange(0, 5)
            rolls.setPrefix("+")
            row = QHBoxLayout()
            row.addWidget(combo, 1)
            row.addWidget(QLabel("强化"))
            row.addWidget(rolls)
            form.addRow(f"词条 {index + 1}", row)
            self.rows.append((combo, rolls))

        self.run = QPushButton("试判这件盘")
        self.run.setObjectName("primary")
        self.result = QLabel("先填词条，再对照当前勾选的卡片做一次判断。")
        self.result.setWordWrap(True)
        self.result.setObjectName("hint")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.run)
        layout.addWidget(self.result)
        layout.addStretch()

    def set_name_choices(self, names: list[str]) -> None:
        extras = [name for name in names if name and name not in KNOWN_SUBSTATS]
        for combo, _rolls in self.rows:
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("")
            combo.addItems(list(KNOWN_SUBSTATS) + extras)
            combo.setCurrentText(current)
            combo.blockSignals(False)

    def disc(self) -> Disc | None:
        subs: list[Substat] = []
        for combo, rolls in self.rows:
            name = combo.currentText().strip()
            if not name:
                continue
            subs.append(Substat(name=name, rolls=int(rolls.value()) + 1))
        if not subs:
            return None
        return Disc(
            level=int(self.level.value()),
            substats=tuple(subs),
            locked=self.locked.isChecked(),
            equipped=self.equipped.isChecked(),
            discarded=self.discarded.isChecked(),
        )


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("驱动盘强化辅助")
        self.resize(1180, 780)
        self._path = Path(config_path) if config_path else default_config_path()
        self._loading = False
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(250)
        self._rebuild_timer.timeout.connect(self.rebuild_cards)
        self.sections: dict[tuple[str, int], StageSection] = {}

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)
        outer.addLayout(self._build_header())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 860])
        outer.addWidget(splitter, 1)
        outer.addWidget(self._build_bottom())
        self._load_path(self._path)

    def _build_header(self) -> QHBoxLayout:
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("驱动盘强化辅助")
        title.setObjectName("bannerTitle")
        hint = QLabel("左边定规则，右边勾组合。识别和自动点击还没接上。")
        hint.setObjectName("hint")
        self.path_label = QLabel()
        self.path_label.setObjectName("hint")
        titles.addWidget(title)
        titles.addWidget(hint)
        titles.addWidget(self.path_label)

        open_btn = QPushButton("打开")
        save_btn = QPushButton("保存")
        save_as_btn = QPushButton("另存为")
        open_btn.clicked.connect(self.open_preset)
        save_btn.clicked.connect(self.save_current)
        save_as_btn.clicked.connect(self.save_as)
        self.stay_on_top = QCheckBox("置顶")
        self.stay_on_top.toggled.connect(self._toggle_on_top)
        self.light_theme = QCheckBox("浅色")
        self.light_theme.setChecked(load_ui_theme() == "light")
        self.light_theme.toggled.connect(self._toggle_theme)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.light_theme)
        actions.addWidget(self.stay_on_top)
        actions.addWidget(open_btn)
        actions.addWidget(save_btn)
        actions.addWidget(save_as_btn)

        banner = QHBoxLayout()
        banner.addLayout(titles, 1)
        banner.addLayout(actions)
        banner.setAlignment(actions, Qt.AlignmentFlag.AlignTop)
        return banner

    def _build_left(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(4)

        self.name_edit = QLineEdit()
        name_box = QGroupBox("预设")
        name_form = QFormLayout(name_box)
        name_form.addRow("名称", self.name_edit)
        layout.addWidget(name_box)

        weights_box = QGroupBox("词条单价（没写的是废词条）")
        weights_layout = QVBoxLayout(weights_box)
        self.weight_editor = WeightEditor()
        self.weight_editor.changed.connect(self._schedule_rebuild)
        weights_layout.addWidget(self.weight_editor)
        layout.addWidget(weights_box)

        flags_box = QGroupBox("硬过滤")
        flags = QVBoxLayout(flags_box)
        self.require_s = QCheckBox("只要 S 级")
        self.skip_locked = QCheckBox("跳过锁定")
        self.skip_equipped = QCheckBox("跳过已装备")
        self.skip_discarded = QCheckBox("跳过已有弃置标记")
        for box in (self.require_s, self.skip_locked, self.skip_equipped, self.skip_discarded):
            box.setChecked(True)
            flags.addWidget(box)
        useful_row = QHBoxLayout()
        useful_row.addWidget(QLabel("最少有效种类"))
        self.min_useful = QSpinBox()
        self.min_useful.setRange(0, 4)
        useful_row.addWidget(self.min_useful)
        useful_row.addStretch()
        flags.addLayout(useful_row)
        layout.addWidget(flags_box)
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(300)
        scroll.setMaximumWidth(380)
        return scroll

    def _build_right(self) -> QWidget:
        self.tabs = QTabWidget()
        self.three_host = self._origin_scroll(Origin.THREE)
        self.four_host = self._origin_scroll(Origin.FOUR)
        self.trial = TrialPage()
        self.trial.run.clicked.connect(self.run_trial)
        self.tabs.addTab(self.three_host["scroll"], "初始 3 词")
        self.tabs.addTab(self.four_host["scroll"], "初始 4 词")
        self.tabs.addTab(self.trial, "试判")
        return self.tabs

    def _origin_scroll(self, origin: Origin) -> dict[str, object]:
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(12, 8, 12, 8)
        for checkpoint in CHECKPOINTS:
            section = StageSection(origin, checkpoint, None)
            section.require_changed.connect(self._update_status)
            section.selection_changed.connect(self._update_status)
            self.sections[(origin.value, checkpoint)] = section
            box.addWidget(section)
        box.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        return {"scroll": scroll, "inner": inner, "box": box}

    def _build_bottom(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("footer")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 8, 10, 8)
        all_on = QPushButton("全选当前可见")
        all_off = QPushButton("取消当前可见")
        all_on.clicked.connect(lambda: self._check_visible(True))
        all_off.clicked.connect(lambda: self._check_visible(False))
        self.start_btn = QPushButton("开始强化")
        self.start_btn.setObjectName("primary")
        self.start_btn.setEnabled(False)
        self.start_btn.setToolTip("OCR 和模拟点击还没做，先把组合勾好。")
        self.status = QLabel()
        self.status.setObjectName("status")
        row.addWidget(all_on)
        row.addWidget(all_off)
        row.addWidget(self.status, 1)
        row.addWidget(self.start_btn)
        return bar

    def _load_path(self, path: Path) -> None:
        if path.exists():
            preset = load_preset(path)
        else:
            preset = preset_from_dict(
                {
                    "name": "双暴攻击",
                    "weights": {"暴击率": 1.0, "暴击伤害": 1.2, "攻击力%": 0.8},
                    "stage_require": {
                        "three": {0: 2.2, 3: 3.0, 6: 4.0, 9: 5.0, 12: 6.0, 15: 7.0},
                        "four": {0: 3.0, 3: 5.0, 6: 6.0, 9: 7.0, 12: 8.0, 15: 9.0},
                    },
                }
            )
        self._path = path
        self._apply_preset(preset)

    def _apply_preset(self, preset: Preset) -> None:
        self._loading = True
        self.name_edit.setText(preset.name)
        self.weight_editor.set_weights(dict(preset.weights))
        self.require_s.setChecked(preset.require_s_rank)
        self.skip_locked.setChecked(preset.skip_locked)
        self.skip_equipped.setChecked(preset.skip_equipped)
        self.skip_discarded.setChecked(preset.skip_discarded)
        self.min_useful.setValue(preset.min_useful_types)
        for origin in Origin:
            for checkpoint in CHECKPOINTS:
                section = self.sections[(origin.value, checkpoint)]
                required = preset.require_for(origin, checkpoint)
                section.require.blockSignals(True)
                section.require.setValue(0.0 if required is None else float(required))
                section.require.blockSignals(False)
        self.path_label.setText(self._path.name)
        self.path_label.setToolTip(str(self._path))
        self._loading = False
        self.rebuild_cards(preset.accepted)

    def _schedule_rebuild(self) -> None:
        if self._loading:
            return
        self._rebuild_timer.start()

    def rebuild_cards(self, accepted: tuple[str, ...] | None = None) -> None:
        weights = self.weight_editor.weights()
        patterns = enumerate_all(weights, hide_junk_bonus=True)
        grouped: dict[tuple[str, int], list[Pattern]] = defaultdict(list)
        for pattern in patterns:
            grouped[(pattern.origin.value, pattern.checkpoint)].append(pattern)
        is_checked = self._card_check_policy(accepted)
        for key, section in self.sections.items():
            section.set_cards(grouped.get(key, []), is_checked)
        self.trial.set_name_choices(list(weights))
        self._update_status()

    def _card_check_policy(self, accepted: tuple[str, ...] | None):
        previous_checked = set(self._accepted_ids())
        previous_all = {
            card.pattern.pattern_id for section in self.sections.values() for card in section.cards
        }
        if accepted is not None:
            keep = set(accepted)
            known = keep
            new_default = False
        elif previous_all:
            keep = previous_checked
            known = previous_all
            new_default = True
        else:
            return lambda _pattern_id: True

        def is_checked(pattern_id: str) -> bool:
            if pattern_id in known:
                return pattern_id in keep
            return new_default

        return is_checked

    def collect_preset(self) -> Preset:
        stage_require = {"three": {}, "four": {}}
        for (origin, checkpoint), section in self.sections.items():
            stage_require[origin][checkpoint] = float(section.require.value())
        return preset_from_dict(
            {
                "name": self.name_edit.text().strip() or "default",
                "weights": self.weight_editor.weights(),
                "stage_require": stage_require,
                "min_useful_types": int(self.min_useful.value()),
                "filters": {
                    "require_s_rank": self.require_s.isChecked(),
                    "skip_locked": self.skip_locked.isChecked(),
                    "skip_equipped": self.skip_equipped.isChecked(),
                    "skip_discarded": self.skip_discarded.isChecked(),
                },
                "accepted": self._accepted_ids(),
            }
        )

    def _accepted_ids(self) -> list[str]:
        ids: list[str] = []
        for section in self.sections.values():
            ids.extend(section.visible_accepted())
        return ids

    def _check_visible(self, on: bool) -> None:
        current = self.tabs.currentIndex()
        origin = Origin.THREE if current == 0 else Origin.FOUR if current == 1 else None
        for section in self.sections.values():
            if origin is None or section.origin is origin:
                section.check_visible(on)
        self._update_status()

    def _update_status(self) -> None:
        visible = sum(1 for section in self.sections.values() for card in section.cards if not card.isHidden())
        total = sum(len(section.cards) for section in self.sections.values())
        picked = len(self._accepted_ids())
        self.status.setText(f"可见 {visible} / {total} 张，已勾选 {picked} 张")

    def _toggle_on_top(self, on: bool) -> None:
        flags = self.windowFlags()
        if on:
            self.setWindowFlags(flags | Qt.WindowType.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(flags & ~Qt.WindowType.WindowStaysOnTopHint)
        self.show()

    def _toggle_theme(self, light: bool) -> None:
        apply_theme("light" if light else "dark")
        save_ui_theme("light" if light else "dark")
        self._refresh_card_chrome()

    def _refresh_card_chrome(self) -> None:
        for section in self.sections.values():
            for card in section.cards:
                card.style().unpolish(card)
                card.style().polish(card)
                card.update()

    def open_preset(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "打开预设", str(self._path.parent), "YAML (*.yaml *.yml)")
        if path:
            self._load_path(Path(path))

    def save_current(self) -> None:
        save_preset(self._path, self.collect_preset())
        self.path_label.setText(f"已保存  {self._path.name}")
        self.path_label.setToolTip(str(self._path))

    def save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "另存预设", str(self._path), "YAML (*.yaml *.yml)")
        if not path:
            return
        self._path = Path(path)
        self.save_current()

    def run_trial(self) -> None:
        disc = self.trial.disc()
        if disc is None:
            self.trial.result.setText("至少填一条副词条。")
            return
        result = decide(disc, self.collect_preset(), self._accepted_ids())
        self.trial.result.setText(
            f"{result.action.value}  ·  {result.reason}\n"
            f"权重 {result.score}  ·  有效 {result.useful_types}/{result.max_useful_types}  ·  "
            f"+{result.checkpoint}  ·  {result.origin or '-'}"
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        event.accept()


def ui_settings() -> QSettings:
    return QSettings("zzz-ddtool", "ui")


def load_ui_theme() -> str:
    value = str(ui_settings().value("theme", "dark"))
    return "light" if value == "light" else "dark"


def save_ui_theme(theme: str) -> None:
    settings = ui_settings()
    settings.setValue("theme", theme)
    settings.sync()


def apply_theme(theme: str, app: QApplication | None = None) -> None:
    app = app or QApplication.instance()
    if app is None:
        return
    name = "light" if theme == "light" else "dark"
    app.setStyle("Fusion")
    palette = QPalette()
    if name == "light":
        palette.setColor(QPalette.ColorRole.Window, QColor("#f6f6f6"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#222222"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#222222"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#222222"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#c9a227"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#111111"))
    else:
        palette.setColor(QPalette.ColorRole.Window, QColor("#121212"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#eeeeee"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#1b1b1b"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#eeeeee"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#2a2a2a"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#eeeeee"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#f0d060"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#111111"))
    app.setPalette(palette)
    font = QFont("Microsoft YaHei")
    if font.exactMatch():
        app.setFont(font)
    app.setStyleSheet(THEMES[name])


def run_gui(config: Path | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(load_ui_theme(), app)
    window = MainWindow(config)
    window.show()
    return app.exec()
