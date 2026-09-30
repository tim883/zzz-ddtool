"""Desktop window: weight editor + pattern cards + trial decide.

Layout stays the card-picker window. White Fluent page, accent #0078D7.
Spin boxes keep the system arrows (not styled).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QFontMetrics, QMouseEvent, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QStackedWidget,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from zzz_ddtool.chain import TaskJob, job_from_dict, new_job_id
from zzz_ddtool.config import Preset, load_preset, preset_from_dict, save_preset
from zzz_ddtool.gamedata import SETS, SLOTS, empty_slot_plan, selectable_mainstats
from zzz_ddtool.seticons import paint_set_icon
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

# 游戏内筛选-主词条按槽位固定三列网格，每页尺寸一致，切部位不抖动
_MAINSTAT_PAGES: dict[int, tuple[str, ...]] = {
    4: ("生命值%", "攻击力%", "防御力%", "暴击率", "暴击伤害", "异常精通"),
    5: (
        "生命值%",
        "攻击力%",
        "防御力%",
        "穿透率",
        "物理伤害",
        "火属性伤害",
        "冰属性伤害",
        "电属性伤害",
        "以太伤害",
    ),
    6: ("生命值%", "攻击力%", "防御力%", "能量自动回复", "冲击力", "异常掌控"),
}
_MAINSTAT_COLS = 3

STYLESHEET = """
QMainWindow, QWidget#root, QScrollArea, QScrollArea > QWidget > QWidget {
    background: #f8f9fc;
    color: #1a1a1a;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
}
QLabel { color: #1a1a1a; }
QLabel#hint { color: #605e5c; }
QLabel#bannerTitle { font-size: 20px; font-weight: 700; color: #1a1a1a; }
QLabel#score { color: #0078d7; font-size: 13px; font-weight: 600; }
QLabel#junk { color: #8a8a8a; }
QLabel#empty { color: #9a9a9a; font-style: italic; }
QLabel#slot { color: #1a1a1a; }
QLabel#bonus { color: #0078d7; font-size: 12px; }
QLabel#stageLevel {
    color: #0078d7; font-size: 15px; font-weight: 700; min-width: 36px;
}
QLabel#status { color: #605e5c; }
QGroupBox {
    border: 1px solid #e1e1e1;
    border-radius: 8px;
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
    color: #323130;
    background: #ffffff;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #605e5c; }
QLineEdit, QComboBox {
    background: #ffffff;
    color: #1a1a1a;
    border: 1px solid #d1d1d1;
    border-radius: 4px;
    padding: 3px 8px;
    min-height: 22px;
}
QLineEdit:focus, QComboBox:focus {
    border: 1px solid #0078d7;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    color: #1a1a1a;
    selection-background-color: #0078d7;
    selection-color: #fff;
}
QPushButton {
    background: #ffffff;
    color: #1a1a1a;
    border: 1px solid #d1d1d1;
    border-radius: 4px;
    padding: 5px 12px;
}
QPushButton:hover { background: #f3f3f3; }
QPushButton:disabled { color: #a0a0a0; border-color: #e5e5e5; background: #f0f0f0; }
QPushButton#primary {
    background: #0078d7;
    border: none;
    color: #fff;
    padding: 8px 18px;
    font-weight: 600;
}
QPushButton#primary:hover { background: #106ebe; }
QPushButton#primary:disabled { background: #d1d1d1; color: #888; }
QTabWidget::pane { border: 1px solid #e1e1e1; background: #f8f9fc; top: -1px; }
QTabBar::tab {
    background: #ffffff;
    color: #605e5c;
    padding: 9px 18px;
    border: 1px solid #e1e1e1;
    border-bottom: none;
    margin-right: 4px;
}
QTabBar::tab:selected { color: #0078d7; background: #f8f9fc; border-bottom: 2px solid #0078d7; }
QCheckBox { color: #1a1a1a; spacing: 6px; }
QFrame#card {
    background: #ffffff;
    border: 1px solid #e1e1e1;
    border-radius: 6px;
}
QFrame#card:hover { border-color: #c8c8c8; }
QFrame#card[checked="true"] { border: 1px solid #0078d7; }
QFrame#card[checked="false"] { background: #fafafa; }
QFrame#setTile {
    background: #ffffff;
    border: 1px solid #e1e1e1;
    border-radius: 8px;
}
QFrame#setTile[checked="true"] { border: 2px solid #0078d7; }
QFrame#stageBar {
    background: #ffffff;
    border: 1px solid #e1e1e1;
    border-radius: 6px;
}
QFrame#footer {
    background: #ffffff;
    border: 1px solid #e1e1e1;
    border-radius: 8px;
}
QListWidget {
    background: #ffffff;
    border: 1px solid #e1e1e1;
    border-radius: 6px;
    color: #1a1a1a;
}
QListWidget::item { padding: 8px 6px; }
QListWidget::item:selected { background: #e8f3fc; color: #0078d7; }
QSplitter::handle { background: #e1e1e1; width: 6px; }
QScrollBar:vertical { background: #f8f9fc; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #c8c8c8; min-height: 32px; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""



def default_config_path() -> Path:
    from zzz_ddtool.paths import data_dir

    return data_dir() / "example.yaml"


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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(1)
        head_row = QHBoxLayout()
        head_row.setContentsMargins(0, 0, 0, 2)
        head_row.addWidget(self.checkbox)
        head_row.addStretch()
        head_row.addWidget(score)
        layout.addLayout(head_row)
        for slot in pattern.slots:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            name = QLabel(slot.name)
            extra = QLabel(f"+{slot.bonus}" if slot.bonus > 0 else "")
            extra.setObjectName("bonus")
            if slot.name == JUNK:
                name.setObjectName("junk")
            elif slot.name == EMPTY:
                name.setObjectName("empty")
            else:
                name.setObjectName("slot")
            name.setFixedHeight(18)
            extra.setFixedHeight(18)
            row.addWidget(name, 1)
            row.addWidget(extra)
            layout.addLayout(row)

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
        self.require.setFixedWidth(72)
        self.require.setFixedHeight(24)
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
        header.setContentsMargins(10, 4, 10, 4)
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
        self._blocked: set[str] = set()
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

    def set_blocked(self, names: set[str]) -> None:
        self._blocked = set(names)
        self._refresh_add_combo()
        dropped = False
        for combo, _spin, row in list(self._rows):
            if combo.currentText().strip() in self._blocked:
                self._remove_row(row)
                dropped = True
        if dropped:
            self.changed.emit()

    def set_weights(self, weights: dict[str, float]) -> None:
        while self._rows:
            self._remove_row(self._rows[0][2])
        for name, value in weights.items():
            if name in self._blocked:
                continue
            self._add_row(name, value)

    def weights(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for combo, spin, _row in self._rows:
            name = combo.currentText().strip()
            if not name or name in self._blocked:
                continue
            out[name] = float(spin.value())
        return out

    def _choices(self, current: str = "") -> list[str]:
        names = [name for name in KNOWN_SUBSTATS if name not in self._blocked]
        if current and current not in names and current not in self._blocked:
            names = [current, *names]
        return names

    def _refresh_add_combo(self) -> None:
        current = self.add_combo.currentText()
        self.add_combo.blockSignals(True)
        self.add_combo.clear()
        self.add_combo.addItems(self._choices())
        if current not in self._blocked:
            self.add_combo.setCurrentText(current)
        self.add_combo.blockSignals(False)

    def _add_from_combo(self) -> None:
        name = self.add_combo.currentText().strip()
        if not name or name in self.weights() or name in self._blocked:
            return
        self._add_row(name, 1.0)
        self.changed.emit()

    def _add_row(self, name: str, value: float) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(self._choices(name))
        combo.setCurrentText(name)
        spin = QDoubleSpinBox()
        spin.setDecimals(1)
        spin.setRange(0.0, 20.0)
        spin.setSingleStep(0.1)
        spin.setFixedHeight(24)
        spin.setValue(value)
        remove = QPushButton("×")
        remove.setFixedWidth(28)
        remove.clicked.connect(lambda: self._delete(row))
        combo.currentTextChanged.connect(lambda *_: self.changed.emit())
        spin.valueChanged.connect(lambda *_: self.changed.emit())
        combo.setFixedWidth(150)
        layout.addWidget(combo)
        layout.addWidget(spin)
        layout.addWidget(remove)
        layout.addStretch()
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
            combo.setMaximumWidth(240)
            row.addWidget(combo)
            row.addWidget(QLabel("强化"))
            row.addWidget(rolls)
            row.addStretch()
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

    def set_name_choices(self, names: list[str], blocked: set[str] | None = None) -> None:
        blocked = set(blocked or ())
        allowed = [name for name in KNOWN_SUBSTATS if name not in blocked]
        extras = [name for name in names if name and name not in allowed and name not in blocked]
        for combo, _rolls in self.rows:
            current = combo.currentText()
            if current in blocked:
                current = ""
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("")
            combo.addItems(allowed + extras)
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


class SetTile(QFrame):
    toggled = Signal(str, bool)

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self._checked = False
        self.setObjectName("setTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(108, 118)
        icon = QLabel()
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(paint_set_icon(name, 64))
        title = QLabel(name)
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        box = QVBoxLayout(self)
        box.setContentsMargins(6, 8, 6, 6)
        box.addWidget(icon)
        box.addWidget(title)
        self.set_checked(False)

    def set_checked(self, on: bool) -> None:
        self._checked = on
        self.setProperty("checked", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.set_checked(not self._checked)
            self.toggled.emit(self.name, self._checked)
        super().mousePressEvent(event)


class SetPickerDialog(QDialog):
    def __init__(self, selected: list[str], extra: list[str] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择套装")
        self.resize(760, 560)
        self._picked = set(selected)
        names = list(SETS)
        for name in extra or []:
            if name not in names:
                names.append(name)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索套装名")
        self.search.textChanged.connect(self._filter)

        grid_host = QWidget()
        self._grid = QGridLayout(grid_host)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._tiles: list[SetTile] = []
        for index, name in enumerate(names):
            tile = SetTile(name)
            tile.set_checked(name in self._picked)
            tile.toggled.connect(self._on_tile)
            self._tiles.append(tile)
            self._grid.addWidget(tile, index // 6, index % 6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(grid_host)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)

    def _on_tile(self, name: str, on: bool) -> None:
        if on:
            self._picked.add(name)
        else:
            self._picked.discard(name)

    def _filter(self, text: str) -> None:
        needle = text.strip()
        for tile in self._tiles:
            tile.setVisible(needle in tile.name)

    def selected(self) -> list[str]:
        return [tile.name for tile in self._tiles if tile.name in self._picked]


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("驱动盘强化辅助")
        self.resize(1320, 820)
        self._path = Path(config_path) if config_path else default_config_path()
        self._loading = False
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(250)
        self._rebuild_timer.timeout.connect(self.rebuild_cards)
        self.sections: dict[tuple[str, int], StageSection] = {}
        self._active_slot = 4
        self._slot_plans: dict[int, dict] = {slot: empty_slot_plan(slot) for slot in SLOTS}
        self._jobs: list[TaskJob] = []
        self._editing_job_id: str | None = None
        app = QApplication.instance()
        if app is not None and not app.styleSheet():
            apply_light_style(app)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)
        outer.addLayout(self._build_header())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_chain())
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([240, 340, 740])
        outer.addWidget(splitter, 1)
        outer.addWidget(self._build_bottom())
        self._load_path(self._path)

    def _build_header(self) -> QHBoxLayout:
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("驱动盘强化辅助")
        title.setObjectName("bannerTitle")
        hint = QLabel("左边任务链，中间编当前部位，右边勾组合。设完一号就加入链，不必凑齐 6 个。")
        hint.setObjectName("hint")
        self.path_label = QLabel()
        self.path_label.setObjectName("hint")
        titles.addWidget(title)
        titles.addWidget(hint)
        titles.addWidget(self.path_label)

        capture_btn = QPushButton("截取游戏")
        capture_btn.clicked.connect(self.capture_game)
        warehouse_btn = QPushButton("进入驱动盘")
        warehouse_btn.clicked.connect(self.goto_warehouse)
        open_btn = QPushButton("打开")
        save_btn = QPushButton("保存")
        save_as_btn = QPushButton("另存为")
        open_btn.clicked.connect(self.open_preset)
        save_btn.clicked.connect(self.save_current)
        save_as_btn.clicked.connect(self.save_as)
        self.stay_on_top = QCheckBox("置顶")
        self.stay_on_top.toggled.connect(self._toggle_on_top)
        settings_btn = QPushButton("日志设置")
        settings_btn.clicked.connect(self.open_settings)
        check_btn = QPushButton("检查更新")
        check_btn.clicked.connect(self.check_update)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.stay_on_top)
        actions.addWidget(settings_btn)
        actions.addWidget(check_btn)
        actions.addWidget(capture_btn)
        actions.addWidget(warehouse_btn)
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

        # 预设名称（YAML 的 name 字段，非方案名）不在面板显示，保存时沿用
        self.name_edit = QLineEdit()
        self.name_edit.hide()

        weights_box = QGroupBox("词条单价（没写的是废词条）")
        weights_layout = QVBoxLayout(weights_box)
        self.weight_editor = WeightEditor()
        self.weight_editor.changed.connect(self._schedule_rebuild)
        weights_layout.addWidget(self.weight_editor)
        layout.addWidget(weights_box)

        archive_box = QGroupBox("存档（权重+档位要求，各槽位共用）")
        archive_layout = QVBoxLayout(archive_box)
        archive_layout.setSpacing(4)
        self.archive_combo = QComboBox()
        self.archive_combo.setEditable(True)
        self.archive_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        apply_btn = QPushButton("套用")
        apply_btn.clicked.connect(self._apply_archive)
        save_btn = QPushButton("存入当前")
        save_btn.clicked.connect(self._save_archive)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self._delete_archive)
        arow1 = QHBoxLayout()
        arow1.addWidget(self.archive_combo, 1)
        arow1.addWidget(apply_btn)
        arow2 = QHBoxLayout()
        arow2.addWidget(save_btn)
        arow2.addWidget(del_btn)
        arow2.addStretch()
        archive_layout.addLayout(arow1)
        archive_layout.addLayout(arow2)
        layout.addWidget(archive_box)

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
        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("本次最多处理"))
        self.max_discs = QSpinBox()
        self.max_discs.setRange(1, 5000)
        self.max_discs.setValue(100)
        limit_row.addWidget(self.max_discs)
        limit_row.addWidget(QLabel("个盘子"))
        limit_row.addStretch()
        flags.addLayout(limit_row)
        layout.addWidget(flags_box)

        self._filter_sets: list[str] = []
        self._filter_dialog = QDialog(self)
        self._filter_dialog.setWindowTitle("游戏内筛选（进仓库先重置再套用）")
        self._filter_dialog.setMinimumWidth(420)
        game_form = QFormLayout(self._filter_dialog)
        self.set_summary = QLabel("未选")
        self.set_summary.setWordWrap(False)
        self.set_summary.setObjectName("hint")
        pick_sets = QPushButton("选择套装")
        pick_sets.clicked.connect(self._open_set_picker)
        set_row = QHBoxLayout()
        set_row.addWidget(self.set_summary, 1)
        set_row.addWidget(pick_sets)
        self.filter_slots: dict[int, QRadioButton] = {}
        self.slot_group = QButtonGroup(self._filter_dialog)
        self.slot_group.setExclusive(True)
        slots_row = QHBoxLayout()
        for slot in SLOTS:
            radio = QRadioButton(str(slot))
            self.slot_group.addButton(radio, slot)
            self.filter_slots[slot] = radio
            slots_row.addWidget(radio)
        self.filter_slots[4].setChecked(True)
        self.slot_group.idClicked.connect(self._on_slot_clicked)
        slots_row.addStretch()
        self._all_main_boxes: dict[str, QCheckBox] = {}
        self.filter_main_boxes: dict[str, QCheckBox] = {}
        self.main_host = QStackedWidget()
        self.main_host.setObjectName("mainHost")
        self.main_host.setFixedHeight(80)
        self._mainstat_pages: dict[int, QWidget] = {}
        for slot, names in _MAINSTAT_PAGES.items():
            page = QWidget()
            page.setFixedHeight(80)
            grid = QGridLayout(page)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(6)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(2, 1)
            for idx, name in enumerate(names):
                box = QCheckBox(name)
                box.toggled.connect(self._on_filter_changed)
                self._all_main_boxes[name] = box
                grid.addWidget(box, idx // _MAINSTAT_COLS, idx % _MAINSTAT_COLS)
            self._mainstat_pages[slot] = page
            self.main_host.addWidget(page)
        empty_page = QWidget()
        empty_page.setFixedHeight(80)
        self.main_host.addWidget(empty_page)
        self.main_hint = QLabel()
        self.main_hint.setObjectName("hint")
        self.main_hint.setWordWrap(True)
        self.main_hint.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.main_hint.setFixedHeight(48)
        game_form.addRow("套装", set_row)
        game_form.addRow("部位", slots_row)
        game_form.addRow("主词条", self.main_host)
        game_form.addRow("", self.main_hint)
        done_row = QHBoxLayout()
        done_row.addStretch()
        done_btn = QPushButton("完成")
        done_btn.clicked.connect(self._filter_dialog.accept)
        done_row.addWidget(done_btn)
        game_form.addRow("", done_row)

        game_box = QGroupBox("游戏内筛选（进仓库先重置再套用）")
        game_row = QHBoxLayout(game_box)
        self.filter_summary = QLabel("未设置")
        self.filter_summary.setObjectName("hint")
        self.filter_summary.hide()  # 长文本不再显示（顶出面板），只留选择按钮
        pick_filter = QPushButton("选择…")
        pick_filter.setFixedWidth(80)
        pick_filter.clicked.connect(self._open_filter_dialog)
        game_row.addWidget(pick_filter)
        game_row.addStretch()
        layout.addWidget(game_box)
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(280)
        scroll.setMaximumWidth(360)
        return scroll

    def _build_chain(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(220)
        panel.setMaximumWidth(260)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        box = QGroupBox("任务链")
        inner = QVBoxLayout(box)
        hint = QLabel("编完一个部位再加入。执行按从上到下的顺序。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        self.chain_list = QListWidget()
        self.chain_list.currentRowChanged.connect(self._on_chain_row)
        add_btn = QPushButton("加入任务链")
        add_btn.setObjectName("primary")
        add_btn.clicked.connect(self._add_job)
        update_btn = QPushButton("更新当前任务")
        update_btn.clicked.connect(self._update_job)
        row = QHBoxLayout()
        up_btn = QPushButton("上移")
        down_btn = QPushButton("下移")
        del_btn = QPushButton("移除")
        up_btn.clicked.connect(lambda: self._move_job(-1))
        down_btn.clicked.connect(lambda: self._move_job(1))
        del_btn.clicked.connect(self._remove_job)
        row.addWidget(up_btn)
        row.addWidget(down_btn)
        row.addWidget(del_btn)
        inner.addWidget(hint)
        inner.addWidget(self.chain_list, 1)
        inner.addWidget(add_btn)
        inner.addWidget(update_btn)
        inner.addLayout(row)
        layout.addWidget(box)
        return panel

    def _refresh_chain_list(self, select_id: str | None = None) -> None:
        current_id = select_id or self._editing_job_id
        self.chain_list.blockSignals(True)
        self.chain_list.clear()
        for index, job in enumerate(self._jobs):
            item = QListWidgetItem(f"{index + 1}. {job.title()}")
            item.setData(Qt.ItemDataRole.UserRole, job.job_id)
            self.chain_list.addItem(item)
        self.chain_list.blockSignals(False)
        if current_id:
            for row in range(self.chain_list.count()):
                item = self.chain_list.item(row)
                if item is not None and item.data(Qt.ItemDataRole.UserRole) == current_id:
                    self.chain_list.setCurrentRow(row)
                    break
        self._update_status()

    def _job_from_editor(self, job_id: str | None = None) -> TaskJob:
        snap = self._snapshot_slot()
        return TaskJob(
            job_id=job_id or new_job_id(),
            slot=self._active_slot,
            sets=tuple(self._filter_sets),
            mainstats=tuple(snap.get("mainstats") or ()),
            weights=dict(snap.get("weights") or {}),
            stage_require={
                str(origin): {int(level): float(value) for level, value in table.items()}
                for origin, table in (snap.get("stage_require") or {}).items()
            },
            accepted=snap.get("accepted"),
            min_useful_types=int(snap.get("min_useful_types") or 0),
        )

    def _load_job(self, job: TaskJob) -> None:
        self._loading = True
        self._editing_job_id = job.job_id
        self._active_slot = job.slot
        for slot, radio in self.filter_slots.items():
            radio.blockSignals(True)
            radio.setChecked(slot == job.slot)
            radio.blockSignals(False)
        self._filter_sets = list(job.sets)
        self._update_set_summary()
        plan = {
            "weights": dict(job.weights),
            "stage_require": {
                str(origin): dict(table) for origin, table in (job.stage_require or {}).items()
            },
            "accepted": job.accepted,
            "mainstats": tuple(job.mainstats),
            "min_useful_types": job.min_useful_types,
        }
        self._slot_plans[job.slot] = plan
        self._apply_slot_widgets(plan)
        self._loading = False
        self.rebuild_cards(job.accepted)

    def _on_chain_row(self, row: int) -> None:
        if self._loading or row < 0 or row >= len(self._jobs):
            return
        self._slot_plans[self._active_slot] = self._snapshot_slot()
        self._load_job(self._jobs[row])

    def _add_job(self) -> None:
        job = self._job_from_editor()
        self._jobs.append(job)
        self._editing_job_id = job.job_id
        snap = self._snapshot_slot()
        self._slot_plans[job.slot] = snap
        # 加入任务链后，存为使用的存档：把该方案实际用的权重+档位要求写回所选存档
        name = self.archive_combo.currentText().strip()
        if name:
            data = self._load_archives()
            data[name] = {
                "weights": dict(snap.get("weights") or {}),
                "stage_require": {
                    origin: dict(table)
                    for origin, table in (snap.get("stage_require") or {}).items()
                },
            }
            self._write_archives(data)
            self._refresh_archive_combo(name)
        self._refresh_chain_list(job.job_id)

    def _update_job(self) -> None:
        if not self._editing_job_id:
            self.path_label.setText("先点任务链里的一项，或先「加入任务链」。")
            return
        for index, job in enumerate(self._jobs):
            if job.job_id != self._editing_job_id:
                continue
            self._jobs[index] = self._job_from_editor(job.job_id)
            self._slot_plans[self._jobs[index].slot] = self._snapshot_slot()
            self._refresh_chain_list(self._editing_job_id)
            self.path_label.setText(f"已更新任务  {self._jobs[index].title()}")
            return
        self.path_label.setText("当前任务不在链里，请先加入。")

    def _remove_job(self) -> None:
        row = self.chain_list.currentRow()
        if row < 0 or row >= len(self._jobs):
            return
        removed = self._jobs.pop(row)
        if self._editing_job_id == removed.job_id:
            self._editing_job_id = None
        self._refresh_chain_list()

    def _move_job(self, delta: int) -> None:
        row = self.chain_list.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= len(self._jobs):
            return
        self._jobs[row], self._jobs[target] = self._jobs[target], self._jobs[row]
        self._refresh_chain_list(self._jobs[target].job_id)

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

    def _selected_slots(self) -> list[int]:
        return [self._active_slot]

    def _selected_mains(self) -> list[str]:
        return [name for name, box in self.filter_main_boxes.items() if box.isChecked()]

    def _update_set_summary(self) -> None:
        if not self._filter_sets:
            text = "未选"
            full = ""
        else:
            text = "、".join(self._filter_sets)
            full = text
        fm = QFontMetrics(self.set_summary.font())
        width = max(self.set_summary.width(), 180)
        self.set_summary.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, width))
        self.set_summary.setToolTip(full)
        self._update_filter_summary()

    def _update_filter_summary(self) -> None:
        parts = [f"{self._active_slot} 号位"]
        if self._filter_sets:
            parts.append("套装 " + "、".join(self._filter_sets))
        else:
            parts.append("套装不限")
        mains = self._selected_mains()
        if mains:
            parts.append("主词条 " + "、".join(mains))
        text = " · ".join(parts)
        fm = QFontMetrics(self.filter_summary.font())
        width = max(self.filter_summary.width(), 160)
        self.filter_summary.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, width))
        self.filter_summary.setToolTip(text)

    def _open_filter_dialog(self) -> None:
        self._update_set_summary()
        self._filter_dialog.exec()

    def _open_set_picker(self) -> None:
        extra = [name for name in self._filter_sets if name not in SETS]
        dialog = SetPickerDialog(self._filter_sets, extra=extra, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._filter_sets = dialog.selected()
        self._update_set_summary()

    def _refresh_main_boxes(self, keep: set[str] | None = None) -> None:
        keep = set(keep if keep is not None else self._selected_mains())
        available = set(selectable_mainstats(self._selected_slots()))
        active_slot = self._active_slot if self._active_slot in self._mainstat_pages else None
        for name, box in self._all_main_boxes.items():
            is_available = name in available
            box.blockSignals(True)
            box.setChecked(is_available and name in keep)
            box.setVisible(is_available)
            box.blockSignals(False)
        if active_slot is not None:
            self.main_host.setCurrentWidget(self._mainstat_pages[active_slot])
        else:
            self.main_host.setCurrentIndex(self.main_host.count() - 1)
        self.filter_main_boxes = {
            name: box for name, box in self._all_main_boxes.items() if name in available
        }
        if available:
            self.main_hint.setText(f"{self._active_slot} 号位可选主词条。已选主词条不会出现在右侧副词条里。")
        else:
            fixed = {1: "生命值", 2: "攻击力", 3: "防御力"}
            self.main_hint.setText(
                f"{self._active_slot} 号位主词条固定是{fixed[self._active_slot]}，不用选。右侧只编这个部位的副词条。"
            )

    def _load_archives(self) -> dict:
        import json

        from zzz_ddtool.paths import archives_file

        f = archives_file()
        if not f.exists():
            return {}
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_archives(self, data: dict) -> None:
        import json

        from zzz_ddtool.paths import archives_file

        archives_file().parent.mkdir(parents=True, exist_ok=True)
        archives_file().write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _refresh_archive_combo(self, select: str | None = None) -> None:
        names = sorted(self._load_archives())
        self.archive_combo.blockSignals(True)
        self.archive_combo.clear()
        self.archive_combo.addItems(names)
        if select:
            self.archive_combo.setCurrentText(select)
        self.archive_combo.blockSignals(False)

    def _save_archive(self) -> None:
        name = self.archive_combo.currentText().strip()
        if not name:
            self.path_label.setText("存档：先在框里输入存档名再点「存入当前」。")
            return
        snap = self._snapshot_slot()
        data = self._load_archives()
        data[name] = {
            "weights": dict(snap.get("weights") or {}),
            "stage_require": {
                origin: dict(table)
                for origin, table in (snap.get("stage_require") or {}).items()
            },
        }
        self._write_archives(data)
        self._refresh_archive_combo(name)
        self.path_label.setText(f"存档：已保存「{name}」。")

    def _apply_archive(self) -> None:
        name = self.archive_combo.currentText().strip()
        data = self._load_archives().get(name)
        if not data:
            self.path_label.setText(f"存档：没有叫「{name}」的存档。")
            return
        self.weight_editor.set_weights(dict(data.get("weights") or {}))
        self._apply_stage_require(data.get("stage_require") or {})
        self._schedule_rebuild()
        self.path_label.setText(f"存档：已套用「{name}」到 {self._active_slot} 号位。")

    def _delete_archive(self) -> None:
        name = self.archive_combo.currentText().strip()
        data = self._load_archives()
        if name in data:
            del data[name]
            self._write_archives(data)
        self._refresh_archive_combo()
        self.path_label.setText(f"存档：已删除「{name}」。")

    def _snapshot_slot(self) -> dict:
        stage_require = {"three": {}, "four": {}}
        for (origin, checkpoint), section in self.sections.items():
            stage_require[origin][checkpoint] = float(section.require.value())
        return {
            "weights": self.weight_editor.weights(),
            "stage_require": stage_require,
            "accepted": tuple(self._accepted_ids()),
            "mainstats": tuple(self._selected_mains()),
            "sets": tuple(self._filter_sets),
            "min_useful_types": int(self.min_useful.value()),
        }

    def _apply_stage_require(self, table: dict) -> None:
        for origin in Origin:
            for checkpoint in CHECKPOINTS:
                section = self.sections[(origin.value, checkpoint)]
                required = (table.get(origin.value) or {}).get(checkpoint)
                section.require.blockSignals(True)
                section.require.setValue(0.0 if required is None else float(required))
                section.require.blockSignals(False)

    def _apply_slot_widgets(self, plan: dict) -> None:
        self.min_useful.setValue(int(plan.get("min_useful_types") or 0))
        self._refresh_main_boxes(set(plan.get("mainstats") or ()))
        self.weight_editor.set_blocked(set(self._selected_mains()))
        self.weight_editor.set_weights(dict(plan.get("weights") or {}))
        self._apply_stage_require(plan.get("stage_require") or {})

    def _on_slot_clicked(self, slot: int) -> None:
        if self._loading or slot == self._active_slot:
            return
        self._slot_plans[self._active_slot] = self._snapshot_slot()
        self._editing_job_id = None
        self._active_slot = slot
        plan = self._slot_plans.get(slot) or empty_slot_plan(slot)
        self._filter_sets = list(plan.get("sets") or ())
        self._update_set_summary()
        self._apply_slot_widgets(plan)
        accepted = plan.get("accepted")
        self.rebuild_cards(None if accepted is None else tuple(accepted))
        self.chain_list.blockSignals(True)
        self.chain_list.clearSelection()
        self.chain_list.blockSignals(False)

    def _on_filter_changed(self) -> None:
        if self._loading:
            return
        self.weight_editor.set_blocked(set(self._selected_mains()))
        self._update_filter_summary()
        self._schedule_rebuild()

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
        self.start_btn.setToolTip("先用命令行 zzz_ddtool run 跑，窗口这里显示运行状态。")
        self.status = QLabel()
        self.status.setObjectName("status")
        row.addWidget(all_on)
        row.addWidget(all_off)
        row.addWidget(self.status, 1)
        row.addWidget(self.start_btn)
        self.run_status_label = QLabel()
        self.run_status_label.setObjectName("status")
        row.addWidget(self.run_status_label, 1)
        self._run_timer = QTimer(self)
        self._run_timer.setInterval(1000)
        self._run_timer.timeout.connect(self._refresh_run_status)
        self._run_timer.start()
        self._refresh_run_status()
        self._refresh_archive_combo()
        return bar

    def open_settings(self) -> None:
        """日志设置：级别 + 截图容量。"""
        from zzz_ddtool.logsettings import LogSettingsDialog

        dialog = LogSettingsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        dialog.apply_settings()
        self.status.setText("日志设置已保存")

    def check_update(self) -> None:
        """检查更新：读 Gitee 发行版，对比本地版本号，用户确认后下载更新。

        - 有程序包：下载后在重启时替换（退出 + 替身 bat）。
        - 只有数据包：下载覆盖 data/，立即生效、不退出。
        """
        from zzz_ddtool.updater import apply_data_update, apply_program_update, check_for_update

        st = check_for_update()
        if st.error:
            QMessageBox.information(self, "检查更新", st.error)
            return
        if not st.available:
            QMessageBox.information(self, "检查更新", f"已是最新版本 {st.current_version}")
            return

        detail = f"发现新版本 {st.latest_version}（当前 {st.current_version}）\n\n{st.body}".rstrip()
        yes = QMessageBox.StandardButton.Yes
        no = QMessageBox.StandardButton.No

        if st.program_url:
            ask = f"{detail}\n\n将下载完整程序包，程序会退出、重启后完成更新。现在更新？"
            if QMessageBox.question(self, "检查更新", ask, yes | no, yes) != yes:
                return
            try:
                apply_program_update(st.program_url)
            except Exception as exc:  # 网络 / 解压失败
                QMessageBox.warning(self, "检查更新", f"更新失败：{exc}")
        elif st.data_url:
            ask = f"{detail}\n\n将下载数据包（套装名/词条名/图片），立即生效、无需重启。现在更新？"
            if QMessageBox.question(self, "检查更新", ask, yes | no, yes) != yes:
                return
            try:
                apply_data_update(st.data_url)
            except Exception as exc:
                QMessageBox.warning(self, "检查更新", f"更新失败：{exc}")
                return
            QMessageBox.information(self, "检查更新", "数据已更新，立即生效。")
        else:
            QMessageBox.information(self, "检查更新", f"{detail}\n\n该版本未附带可下载的更新包。")

    def _refresh_run_status(self) -> None:
        """每秒读 run-status.json + 日志尾部，工具窗口实时显示运行状态。"""
        import json

        from zzz_ddtool.paths import status_file as _status_file_path

        text = "运行状态：—（还没有运行记录）"
        status_file = _status_file_path()
        if status_file.exists():
            try:
                data = json.loads(status_file.read_text(encoding="utf-8"))
                text = (
                    f"运行状态：{data.get('phase', '—')}  格子:{data.get('cell', '—')}  "
                    f"进度:{data.get('processed', 0)}/{data.get('max_discs', 0)}  "
                    f"时间:{data.get('time', '—')}"
                )
            except Exception:
                pass
        self.run_status_label.setText(text)

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
        self.require_s.setChecked(preset.require_s_rank)
        self.skip_locked.setChecked(preset.skip_locked)
        self.skip_equipped.setChecked(preset.skip_equipped)
        self.skip_discarded.setChecked(preset.skip_discarded)
        self.max_discs.setValue(preset.max_discs)
        self._jobs = [
            copied
            for job in preset.task_chain
            for copied in [job_from_dict(job.to_dict())]
            if copied is not None
        ]
        self._editing_job_id = self._jobs[0].job_id if self._jobs else None
        self._slot_plans = {slot: empty_slot_plan(slot) for slot in SLOTS}
        for slot, plan in (preset.slot_plans or {}).items():
            self._slot_plans[int(slot)] = dict(plan)
        if self._jobs:
            first = self._jobs[0]
            self._active_slot = first.slot
            self._filter_sets = list(first.sets)
            plan = {
                "weights": dict(first.weights),
                "stage_require": {
                    str(origin): dict(table) for origin, table in (first.stage_require or {}).items()
                },
                "accepted": first.accepted,
                "mainstats": tuple(first.mainstats),
                "sets": tuple(first.sets),
                "min_useful_types": first.min_useful_types,
            }
            self._slot_plans[first.slot] = plan
            accepted = first.accepted
        else:
            self._active_slot = preset.filter_slot
            self._filter_sets = list(preset.filter_sets)
            self._slot_plans[preset.filter_slot] = {
                "weights": dict(preset.weights),
                "stage_require": {key: dict(table) for key, table in preset.stage_require.items()},
                "accepted": preset.accepted,
                "mainstats": tuple(preset.filter_mainstats),
                "sets": tuple(preset.filter_sets),
                "min_useful_types": preset.min_useful_types,
            }
            accepted = preset.accepted
        for slot, radio in self.filter_slots.items():
            radio.blockSignals(True)
            radio.setChecked(slot == self._active_slot)
            radio.blockSignals(False)
        self._update_set_summary()
        self._apply_slot_widgets(self._slot_plans[self._active_slot])
        self._refresh_chain_list(self._editing_job_id)
        self.path_label.setText(self._path.name)
        self.path_label.setToolTip(str(self._path))
        self._loading = False
        self.rebuild_cards(accepted)

    def _schedule_rebuild(self) -> None:
        if self._loading:
            return
        self._rebuild_timer.start()

    def rebuild_cards(self, accepted: tuple[str, ...] | None = None) -> None:
        blocked = set(self._selected_mains())
        weights = {name: value for name, value in self.weight_editor.weights().items() if name not in blocked}
        patterns = enumerate_all(weights, hide_junk_bonus=True)
        grouped: dict[tuple[str, int], list[Pattern]] = defaultdict(list)
        for pattern in patterns:
            grouped[(pattern.origin.value, pattern.checkpoint)].append(pattern)
        is_checked = self._card_check_policy(accepted)
        for key, section in self.sections.items():
            section.set_cards(grouped.get(key, []), is_checked)
        self.trial.set_name_choices(list(weights), blocked)
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
        self._slot_plans[self._active_slot] = self._snapshot_slot()
        current = self._slot_plans[self._active_slot]
        return preset_from_dict(
            {
                "name": self.name_edit.text().strip() or "default",
                "weights": current.get("weights") or {},
                "stage_require": current.get("stage_require") or {"three": {}, "four": {}},
                "min_useful_types": int(current.get("min_useful_types") or 0),
                "run": {"max_discs": int(self.max_discs.value())},
                "game_filter": {
                    "sets": list(self._filter_sets),
                    "slot": self._active_slot,
                    "slots": [self._active_slot],
                    "mainstats": list(current.get("mainstats") or ()),
                },
                "slot_plans": {
                    str(slot): {
                        "weights": plan.get("weights") or {},
                        "stage_require": plan.get("stage_require") or {"three": {}, "four": {}},
                        "mainstats": list(plan.get("mainstats") or ()),
                        "sets": list(plan.get("sets") or ()),
                        "min_useful_types": int(plan.get("min_useful_types") or 0),
                        **({"accepted": list(plan["accepted"])} if plan.get("accepted") is not None else {}),
                    }
                    for slot, plan in self._slot_plans.items()
                    if plan
                },
                "task_chain": [job.to_dict() for job in self._jobs],
                "filters": {
                    "require_s_rank": self.require_s.isChecked(),
                    "skip_locked": self.skip_locked.isChecked(),
                    "skip_equipped": self.skip_equipped.isChecked(),
                    "skip_discarded": self.skip_discarded.isChecked(),
                },
                "accepted": list(current.get("accepted") or ()),
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
        self.status.setText(
            f"任务链 {len(self._jobs)} 节 · 正在编 {self._active_slot} 号 · "
            f"组合库共 {total} 种，当前筛选可见 {visible}，已勾选 {picked}"
        )

    def _toggle_on_top(self, on: bool) -> None:
        flags = self.windowFlags()
        if on:
            self.setWindowFlags(flags | Qt.WindowType.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(flags & ~Qt.WindowType.WindowStaysOnTopHint)
        self.show()

    def capture_game(self) -> None:
        from zzz_ddtool.capture import CaptureError, capture_game

        try:
            dest = capture_game()
        except CaptureError as exc:
            self.path_label.setText(str(exc))
            return
        self.path_label.setText(f"已截取  {dest.name}")
        self.path_label.setToolTip(str(dest))

    def goto_warehouse(self) -> None:
        from zzz_ddtool.nav import NavError, goto_warehouse

        try:
            dest = goto_warehouse()
        except NavError as exc:
            self.path_label.setText(str(exc))
            return
        self.path_label.setText(f"已进入驱动盘  {dest.name}")
        self.path_label.setToolTip(str(dest))

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


def apply_light_style(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#f8f9fc"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#1a1a1a"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#1a1a1a"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#1a1a1a"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#0078d7"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    font = QFont("Microsoft YaHei")
    if font.exactMatch():
        app.setFont(font)
    app.setStyleSheet(STYLESHEET)


def run_gui(config: Path | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    apply_light_style(app)
    window = MainWindow(config)
    window.show()
    return app.exec()
