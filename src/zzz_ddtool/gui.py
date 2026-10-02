"""Desktop window: weight editor + pattern cards + trial decide.

Layout stays the card-picker window. White Fluent page, accent #0078D7.
Spin boxes keep the system arrows (not styled).
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QThread, QTimer, Signal
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
    QPlainTextEdit,
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
from zzz_ddtool.gamedata import (
    SETS,
    SLOTS,
    SUBSTAT_NAMES,
    empty_slot_plan,
    selectable_mainstats,
)
from zzz_ddtool.seticons import paint_set_icon
from zzz_ddtool.decide import decide
from zzz_ddtool.models import Disc, Substat
from zzz_ddtool.origin import Origin
from zzz_ddtool.patterns import CHECKPOINTS, EMPTY, JUNK, Pattern, enumerate_all

# 词条下拉的顺序偏好：常用的排前面（双爆最常用）。
#
# 这里**只声明顺序**，不声明内容；内容取自 gamedata.SUBSTAT_NAMES（data/stats.json）：
#   * 对不上的名字直接丢弃，没列到的自动补在后面；
#   * 名单本身必须是**真正的副词条**（固定 10 种）。踩过两次坑：一次把「穿透值」
#     写成「穿透率」（那是 5 号位主属性），一次把「冲击力」「能量自动回复」
#     （6 号位主属性）当成副词条补了进来 —— 都会让下拉框多出游戏里选不中的词条，
#     选进去的组合永远命不中。守卫见 tests/test_gui.py 的副词条池用例。
_SUBSTAT_ORDER = (
    "暴击率",
    "暴击伤害",
    "攻击力%",
    "攻击力",
    "生命值%",
    "生命值",
    "防御力%",
    "防御力",
    "异常精通",
    "穿透值",
)

KNOWN_SUBSTATS = tuple(
    [name for name in _SUBSTAT_ORDER if name in SUBSTAT_NAMES]
    + [name for name in SUBSTAT_NAMES if name not in _SUBSTAT_ORDER]
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
QLabel#saveStateIdle { color: #8a8a8a; }
QLabel#saveState { color: #107c10; font-weight: 700; }
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



def _base_job_name(name: str) -> str:
    """剥掉名字尾部的「(2)」这类自动编号，取基准名。

    复制时用它算新名字：复制「紫手套 (2)」应该出「紫手套 (3)」，
    而不是「紫手套 (2) (2)」。
    """
    return re.sub(r"\s*\(\d+\)\s*$", "", name).strip() or "任务"


def _pencil_icon(size: int = 16):
    """自绘铅笔图标：微软雅黑没有 U+270E，直接写字符会渲染成缺字方框。"""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QIcon, QPainter, QPen, QPixmap

    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#4a4a4a"))
    pen.setWidthF(1.3)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    # 笔身（斜放）：左下一段到右上，两条边 + 两端封口
    painter.drawLine(QPointF(4.6, 9.6), QPointF(11.2, 3.0))
    painter.drawLine(QPointF(6.4, 11.4), QPointF(13.0, 4.8))
    painter.drawLine(QPointF(4.6, 9.6), QPointF(6.4, 11.4))
    painter.drawLine(QPointF(11.2, 3.0), QPointF(13.0, 4.8))
    # 笔尖：收成左下角一点
    painter.drawLine(QPointF(4.6, 9.6), QPointF(2.2, 13.0))
    painter.drawLine(QPointF(6.4, 11.4), QPointF(2.2, 13.0))
    painter.end()
    return QIcon(pix)


def remember_config_path(path: Path) -> None:
    """记下这次用的 yaml，下次启动直接打开它。

    只在用户**换了文件**的时候记（打开、另存为）。写失败不影响使用——
    这只是个便利功能，坏了顶多回到默认档。
    """
    import json

    from zzz_ddtool.paths import last_config_file

    target = last_config_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"path": str(Path(path).resolve())}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def last_config_path() -> Path | None:
    """读上次用的 yaml。没记过、读不出来、或那个文件已经不在了，都返回 None。"""
    import json

    from zzz_ddtool.paths import last_config_file

    try:
        raw = json.loads(last_config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    value = str(raw.get("path") or "").strip()
    if not value:
        return None
    path = Path(value)
    return path if path.exists() else None


def default_config_path() -> Path:
    """启动时打开哪份 yaml：优先用上次打开的，否则用自带的 example.yaml。"""
    remembered = last_config_path()
    if remembered is not None:
        return remembered
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
        self.score_label = score

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

    def set_checked_silent(self, on: bool) -> None:
        """复用卡片时同步勾选态：不发 toggled，只刷新样式，避免连锁重建。"""
        if self.checkbox.isChecked() == on:
            return
        self.checkbox.blockSignals(True)
        self.checkbox.setChecked(on)
        self.checkbox.blockSignals(False)
        self.setProperty("checked", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_score(self, value: float) -> None:
        text = f"{value:g}"
        if self.score_label.text() != text:
            self.score_label.setText(text)

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
                # takeAt 只是把它移出布局，控件还是 flow_host 的子控件、还会被画在
                # 原来的位置上。必须再摘掉父级，否则重建期间旧卡和新卡叠着显示。
                widget.setParent(None)
                widget.deleteLater()
        self.cards = []
        for pattern in patterns:
            card = PatternCard(pattern, checked=bool(is_checked(pattern.pattern_id)))
            card.toggled.connect(self.selection_changed.emit)
            self.flow.addWidget(card)
            self.cards.append(card)
        self.apply_filter()

    def patterns_match(self, patterns: list[Pattern]) -> bool:
        """卡片结构与目标是否一致。一致即可原地复用，不必重建控件。

        pattern_id 不含分数（见 patterns.pattern_id），所以「同一批图案、只改了权重值」
        也会判定为可复用——分值由 update_card_states 就地改写。
        """
        if len(patterns) != len(self.cards):
            return False
        return all(
            card.pattern.pattern_id == pattern.pattern_id
            for card, pattern in zip(self.cards, patterns)
        )

    def update_card_states(self, patterns: list[Pattern], is_checked) -> None:  # noqa: ANN001
        """复用既有卡片，只写分值差与勾选态，避免整屏销毁重建。"""
        for card, pattern in zip(self.cards, patterns):
            card.pattern = pattern
            card.set_score(pattern.score)
            card.set_checked_silent(bool(is_checked(pattern.pattern_id)))
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
            # 主属性（被 blocked）本来就不该当副词条；非副词条的名字则是配置里的脏数据
            # （历史配置出现过「穿透率」），放进来下拉框会多出游戏里选不中的词条。
            if name in self._blocked or name not in SUBSTAT_NAMES:
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

    def _choices(self) -> list[str]:
        """下拉候选：只给真正的副词条，减去当前被 blocked 的主属性。

        这里原先有一句「current 不在候选里就插到最前面」的兜底，本意是别让已选中的值
        消失；实际效果是**把配置里的脏名字（如「穿透率」）原样塞回下拉框**，于是下拉框
        多出游戏里选不中的词条。现在不注入了：set_weights 已挡掉非法名，用户自己敲进来
        的名字由 editable 下拉框自己显示。
        """
        return [name for name in KNOWN_SUBSTATS if name not in self._blocked]

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
        combo.addItems(self._choices())
        combo.setCurrentText(name)
        spin = QDoubleSpinBox()
        spin.setDecimals(1)
        spin.setRange(0.0, 20.0)
        spin.setSingleStep(0.1)
        spin.setFixedHeight(24)
        # 关掉「边敲边发」：默认 QDoubleSpinBox 每敲一个字符就发一次 valueChanged，
        # 而每次改价都要重算整屏组合（八百多张卡，两秒起步），于是打「1.5」三下就
        # 卡三下。关掉之后，信号只在**编辑结束**时发一次——回车、Tab 或点到别处；
        # 点上下箭头调值仍然立即生效（那是「调整一次」，不是「敲一下」）。
        spin.setKeyboardTracking(False)
        spin.setValue(value)
        remove = QPushButton("×")
        remove.setFixedWidth(28)
        remove.clicked.connect(lambda: self._delete(row))
        # 词条名同理：这是个可编辑下拉框，正在输入名字时不必每敲一个字就重算。
        # 从下拉列表里选中（activated）和输入完按回车/失焦（editingFinished）都要认。
        combo.activated.connect(lambda *_: self.changed.emit())
        combo.lineEdit().editingFinished.connect(lambda: self.changed.emit())
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
        """摘掉一行，并立刻把它从界面上拿下来。

        只调 deleteLater() 是不够的：真正的销毁要等事件循环跑到 DeferredDelete，
        在那之前控件仍挂在布局里、仍会被绘制——切任务时就会看到旧行和新行叠在一起，
        也就是「凭空多出来的词条」。所以先把布局和父级都摘干净（这一步是同步生效的，
        视觉上立即消失），再排队销毁。
        """
        self._rows = [item for item in self._rows if item[2] is not row]
        self._rows_box.removeWidget(row)
        row.setParent(None)
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
        self._rebuilding = False
        self._rebuild_pending = False
        self._busy_cursor = False
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(250)
        self._rebuild_timer.timeout.connect(self.rebuild_cards)
        self.sections: dict[tuple[str, int], StageSection] = {}
        self._active_slot = 4
        self._slot_plans: dict[int, dict] = {slot: empty_slot_plan(slot) for slot in SLOTS}
        self._jobs: list[TaskJob] = []
        self._editing_job_id: str | None = None
        self._run_thread = None
        self._controller = None
        self._close_after_run = False
        self._task_status = None
        from zzz_ddtool.capturebridge import install_capture_bridge

        install_capture_bridge()
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
        self._config_splitter = splitter
        splitter.addWidget(self._build_chain())
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([240, 340, 740])
        outer.addWidget(splitter, 1)
        outer.addWidget(self._build_bottom())
        log_box = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_box)
        self.run_log = QPlainTextEdit()
        self.run_log.setReadOnly(True)
        self.run_log.setMaximumBlockCount(300)
        self.run_log.setPlaceholderText("开始任务后，这里实时显示识别、判断、操作及失败原因。")
        self.run_log.setMinimumHeight(100)
        self.run_log.setMaximumHeight(160)
        log_layout.addWidget(self.run_log)
        from zzz_ddtool.paths import logs_dir

        log_path = QLabel(f"日志文件：{logs_dir() / 'ddtool.log'}")
        log_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        log_layout.addWidget(log_path)
        outer.addWidget(log_box)
        self._log_tail = None
        self._refresh_run_status()
        self._load_path(self._path)

    def _build_header(self) -> QHBoxLayout:
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("驱动盘强化辅助")
        title.setObjectName("bannerTitle")
        hint = QLabel("左边任务链，中间编当前部位，右边勾组合。设完一号就新建任务，不必凑齐 6 个。")
        hint.setObjectName("hint")
        self.path_label = QLabel()
        self.path_label.setObjectName("hint")
        # 保存状态常驻标签：固定在标题行右端，不占用文件名位置。
        # 固定宽度避免文本长短变化时布局跳动。
        self.save_label = QLabel()
        self.save_label.setObjectName("saveStateIdle")
        self.save_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.save_label.setFixedWidth(160)
        state_row = QHBoxLayout()
        state_row.setContentsMargins(0, 0, 0, 0)
        state_row.addWidget(self.path_label)
        state_row.addStretch(1)
        state_row.addWidget(self.save_label)
        titles.addWidget(title)
        titles.addWidget(hint)
        titles.addLayout(state_row)
        self._reset_saved_state()

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
        self._task_controls = [capture_btn, warehouse_btn, open_btn, save_btn, save_as_btn, check_btn]

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
        hint = QLabel("编完一个部位再新建。执行按从上到下的顺序。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        # 任务名字：选中节点后可改。名字只影响列表标签，不参与执行判断。
        self.job_name_edit = QLineEdit()
        self.job_name_edit.setPlaceholderText("任务名")
        self.job_name_edit.setEnabled(False)
        self.job_name_edit.editingFinished.connect(self._rename_job)
        rename_btn = QPushButton()
        rename_btn.setIcon(_pencil_icon())
        rename_btn.setFixedWidth(30)
        rename_btn.setToolTip("重命名当前任务")
        rename_btn.clicked.connect(self._focus_job_name)
        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        name_row.addWidget(self.job_name_edit, 1)
        name_row.addWidget(rename_btn)
        self.chain_list = QListWidget()
        self.chain_list.currentRowChanged.connect(self._on_chain_row)
        new_btn = QPushButton("新建任务")
        new_btn.setObjectName("primary")
        new_btn.clicked.connect(self._add_job)
        copy_btn = QPushButton("复制任务")
        copy_btn.setToolTip("复制当前选中的任务，名字自动加编号")
        copy_btn.clicked.connect(self._copy_job)
        add_row = QHBoxLayout()
        add_row.setSpacing(4)
        add_row.addWidget(new_btn, 1)
        add_row.addWidget(copy_btn, 1)
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
        inner.addLayout(name_row)
        inner.addWidget(self.chain_list, 1)
        inner.addLayout(add_row)
        inner.addLayout(row)
        layout.addWidget(box)
        return panel

    @staticmethod
    def _job_item_text(index: int, job: TaskJob) -> str:
        """列表条目文本：两行——第一行名字，第二行内容摘要。没名字的老档案只有摘要一行，
        免得两行一模一样。改实时写回后摘要会跟着变，所以抽出来给两处共用。"""
        if job.name:
            return f"{index + 1}. {job.name}\n      {job.title()}"
        return f"{index + 1}. {job.title()}"

    def _refresh_chain_list(self, select_id: str | None = None) -> None:
        current_id = select_id or self._editing_job_id
        # 选中行必须也在屏蔽信号区间内：否则 setCurrentRow 会触发 currentRowChanged
        # → _on_chain_row → _load_job → rebuild_cards，把 242 张卡片整屏重建一遍
        # （实测 ~250ms），而调用方（加入/更新/删除）本身已经不需要重新载入。
        self.chain_list.blockSignals(True)
        self.chain_list.clear()
        for index, job in enumerate(self._jobs):
            item = QListWidgetItem(self._job_item_text(index, job))
            item.setData(Qt.ItemDataRole.UserRole, job.job_id)
            self.chain_list.addItem(item)
        if current_id:
            for row in range(self.chain_list.count()):
                item = self.chain_list.item(row)
                if item is not None and item.data(Qt.ItemDataRole.UserRole) == current_id:
                    self.chain_list.setCurrentRow(row)
                    break
        self.chain_list.blockSignals(False)
        self._sync_job_name_edit()
        self._update_status()

    def _editing_job(self) -> TaskJob | None:
        """当前正在编辑的任务节点。没选中或 id 失效时返回 None。"""
        if not self._editing_job_id:
            return None
        for job in self._jobs:
            if job.job_id == self._editing_job_id:
                return job
        return None

    def _sync_job_name_edit(self) -> None:
        """把名字文本框对齐到当前任务。没选中任务时清空并禁用。"""
        job = self._editing_job()
        self.job_name_edit.blockSignals(True)
        if job is None:
            self.job_name_edit.clear()
            self.job_name_edit.setEnabled(False)
        else:
            self.job_name_edit.setText(job.name)
            # 名字就是名字，不用摘要顶替（摘要已经显示在列表第二行）
            self.job_name_edit.setPlaceholderText("任务名")
            self.job_name_edit.setEnabled(True)
        self.job_name_edit.blockSignals(False)

    def _focus_job_name(self) -> None:
        if not self.job_name_edit.isEnabled():
            return
        self.job_name_edit.setFocus()
        self.job_name_edit.selectAll()

    def _rename_job(self) -> None:
        if self._loading:
            return
        job = self._editing_job()
        if job is None:
            return
        name = self.job_name_edit.text().strip()
        if not name:
            # 名字不能空：空了列表就只剩摘要，认不出这是谁。回落一个不重名的默认名。
            name = self._unique_job_name("任务", exclude=job)
        elif name != job.name:
            # 不能重名：跟别的任务撞了就自动编号
            name = self._unique_job_name(name, exclude=job)
        if name == job.name:
            return
        job.name = name
        self._refresh_chain_list(job.job_id)

    def _commit_editor(self) -> None:
        """把右侧编辑器当前状态写回。编辑器是唯一真相，只在「要离开它」的时候快照一次：
        切任务节点、切部位、落盘/试判/开始任务。这样就不需要「更新当前任务」按钮了。

        有选中任务时写进那个任务节点（名字不在编辑器里，原样保留），顺带刷新它那行的摘要；
        没选中任务时只更新部位暂存，切回去时还能看到刚才编的内容。
        """
        if self._loading:
            return
        self._slot_plans[self._active_slot] = self._snapshot_slot()
        for index, job in enumerate(self._jobs):
            if job.job_id != self._editing_job_id:
                continue
            fresh = self._job_from_editor(job.job_id)
            fresh.name = job.name
            self._jobs[index] = fresh
            item = self.chain_list.item(index)
            if item is not None:
                item.setText(self._job_item_text(index, fresh))
            return

    def _clear_editor(self) -> None:
        """把右侧清成「什么都没有」。

        死线：右侧是左侧选中任务的镜像。左侧没有选中任务时，右侧必须是空的——
        绝不能残留上一个任务或上一次编辑的内容，那是假信息。
        """
        self.weight_editor.set_weights({})
        self._apply_stage_require({})
        self.min_useful.setValue(0)
        self._filter_sets = []
        self._update_set_summary()
        self._refresh_main_boxes(set())
        self._sync_job_name_edit()
        self._rebuild_timer.stop()
        self.rebuild_cards(None)

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
        self._sync_job_name_edit()
        self.rebuild_cards(job.accepted)

    def _on_chain_row(self, row: int) -> None:
        if self._loading or row < 0 or row >= len(self._jobs):
            return
        # 先把手头这个任务编的内容写回去，再切走——不然刚改的权重会丢。
        self._commit_editor()
        self._load_job(self._jobs[row])

    def _unique_job_name(self, base: str, exclude: TaskJob | None = None) -> str:
        """取一个不重名的任务名：base 被占了就 base (2)、(3)…（exclude 用于改名时排除自己）。"""
        used = {job.name for job in self._jobs if job.name and job is not exclude}
        if base not in used:
            return base
        index = 2
        while f"{base} ({index})" in used:
            index += 1
        return f"{base} ({index})"

    def _add_job(self) -> None:
        """新建任务：建一个**空**任务并选中它，右侧随即清空等你编。

        右侧是左侧选中任务的镜像，所以新建之后右侧必须是空的——不能把上一个
        任务的内容顺过来当新任务，那是假信息。之后在右侧编，离开时自动写回它。
        """
        job = TaskJob(slot=self._active_slot, name=self._unique_job_name("任务"))
        self._jobs.append(job)
        self._editing_job_id = job.job_id
        self._refresh_chain_list(job.job_id)
        self._clear_editor()
        self._focus_job_name()

    def _copy_job(self) -> None:
        """复制当前选中的任务：内容一模一样，新 id，名字加编号。

        「紫手套」复制出「紫手套 (2)」，再复制「紫手套 (2)」出「紫手套 (3)」——
        所以先剥掉名字尾部的编号取基准名，再找下一个没用过的编号。
        新任务插在原任务下面，并选中它。
        """
        job = self._editing_job()
        if job is None:
            self.path_label.setText("先选中一个任务再复制。")
            return
        # 先把编辑器内容写回，免得复制出来的是上次写回时的旧值
        self._commit_editor()
        job = self._editing_job()
        if job is None:
            return
        copied = job_from_dict(job.to_dict())
        if copied is None:
            return
        # to_dict/job_from_dict 会把原 id 一起带过来，必须换成新的：
        # 两个任务共用一个 job_id 会让「按 id 找当前任务」永远命中前一个。
        copied.job_id = new_job_id()
        copied.name = self._unique_job_name(_base_job_name(job.name))
        row = self._jobs.index(job)
        self._jobs.insert(row + 1, copied)
        self._load_job(copied)
        self._refresh_chain_list(copied.job_id)

    def _remove_job(self) -> None:
        row = self.chain_list.currentRow()
        if row < 0 or row >= len(self._jobs):
            return
        removed = self._jobs.pop(row)
        if self._editing_job_id == removed.job_id:
            self._editing_job_id = None
            # 删掉正在编的那个，顺手选中原位置的邻居；一个都不剩就保持未选中
            if self._jobs:
                neighbour = self._jobs[min(row, len(self._jobs) - 1)]
                self._editing_job_id = neighbour.job_id
                self._load_job(neighbour)
        self._refresh_chain_list()
        if self._editing_job_id is None:
            # 链空了：右侧必须是空的，不能留着刚被删掉那个任务的内容
            self._clear_editor()

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
        """换部位。

        两种情形分开处理：
        - 有选中任务：换部位就是**改这个任务的部位**，任务保持选中，右侧继续镜像它。
          （原来会把任务取消选中，导致「换完部位任务就丢了、再点回来又是旧部位」，
          等于没法给已选中的任务换部位。）
        - 没选中任务：只是换个部位准备新建，右侧保持空。
        """
        if self._loading or slot == self._active_slot:
            return
        job = self._editing_job()
        if job is None:
            self._active_slot = slot
            for name, radio in self.filter_slots.items():
                radio.blockSignals(True)
                radio.setChecked(name == slot)
                radio.blockSignals(False)
            self._clear_editor()
            return
        # 先把当前编辑器内容写回这个任务，再改部位
        self._commit_editor()
        # _commit_editor 会把 _jobs 里那一项换成新对象，得重新取一次引用，
        # 否则改的是已经被替换掉的旧对象。
        job = self._editing_job()
        if job is None:
            return
        job.slot = slot
        self._active_slot = slot
        self._load_job(job)
        # 部位变了，主词条跟着变：清掉不属于新部位的选择，并同步回任务
        job.mainstats = tuple(self._selected_mains())
        self._refresh_chain_list(job.job_id)

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
        self._task_controls.extend([all_on, all_off])
        self.start_btn = QPushButton("开始任务")
        self.start_btn.setObjectName("primary")
        self.start_btn.setToolTip("用当前配置开始处理；运行中游戏内按 F9 暂停 / F10 停止。")
        self.start_btn.clicked.connect(self._on_start_clicked)
        self.status = QLabel()
        self.status.setObjectName("status")
        row.addWidget(all_on)
        row.addWidget(all_off)
        row.addWidget(self.status, 1)
        row.addWidget(self.start_btn)
        self.pause_btn = QPushButton("暂停 / F9")
        self.stop_btn = QPushButton("停止 / F10")
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._toggle_run_pause)
        self.stop_btn.clicked.connect(self._stop_run)
        row.addWidget(self.pause_btn)
        row.addWidget(self.stop_btn)
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

    def _on_start_clicked(self) -> None:
        """用当前 UI 配置在子线程启动 Runner；运行期间禁用按钮，结束自动恢复。"""
        if self._run_thread is not None:
            return
        try:
            preset = self.collect_preset()
        except Exception as exc:  # 配置缺失 / 构建失败
            QMessageBox.warning(self, "无法启动", f"配置构建失败：{exc}")
            return
        try:
            save_preset(self._path, preset)
        except Exception as exc:  # 落盘失败
            QMessageBox.warning(self, "无法启动", f"开始前自动保存失败：{exc}")
            return
        self._set_saved_state()
        self.start_btn.setEnabled(False)
        self.start_btn.setText("运行中…")
        self._config_splitter.setEnabled(False)
        for button in self._task_controls:
            button.setEnabled(False)
        self._task_status = {"phase": "starting", "message": "正在启动任务"}
        self._show_run_status(self._task_status)
        from zzz_ddtool.hotkeys import HotkeyController
        from copy import deepcopy

        self._controller = HotkeyController()
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self._run_thread = _RunThread(deepcopy(preset), self._controller)
        self._run_thread.status_changed.connect(self._on_task_status)
        self._run_thread.finished.connect(self._on_run_finished)
        self._run_thread.start()

    def _on_run_finished(self) -> None:
        """子线程结束（完成 / F10 停止 / 异常）后恢复按钮。"""
        self.start_btn.setEnabled(True)
        self.start_btn.setText("开始任务")
        self._config_splitter.setEnabled(True)
        for button in self._task_controls:
            button.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.pause_btn.setText("暂停 / F9")
        self.stop_btn.setText("停止 / F10")
        self._controller.close()
        self._controller = None
        self._run_thread.deleteLater()
        self._run_thread = None
        self._refresh_run_status()
        if self._close_after_run:
            self.close()

    def _toggle_run_pause(self) -> None:
        if self._controller is not None:
            self._controller.toggle_pause()

    def _stop_run(self) -> None:
        if self._controller is not None:
            self._controller.request_stop()
            self.stop_btn.setText("等待安全停止…")
            self.stop_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)

    def _show_run_status(self, data: dict) -> None:
        row, col = data.get("global_row"), data.get("global_col")
        where = f"全局{row}行{col}列  " if row else ""
        viewport = data.get("viewport_top_row")
        view = f"视口第{viewport}行  " if viewport else ""
        restore = ""
        target = data.get("restore_target")
        if target is not None:
            restore = f"恢复点第{int(target.get('viewport_top_row0', 0)) + 1}行"
            attempt = data.get("restore_attempt")
            if attempt:
                restore += f"（第 {attempt} 次）"
            restore += "  "
        self.run_status_label.setText(
            f"运行状态：{data.get('phase', '—')}  格子:{data.get('cell') or '—'}  "
            f"进度:{data.get('processed', 0)}/{data.get('max_discs', 0)}\n"
            f"{where}{view}{restore}\n"
            f"{data.get('message', '')}"
        )
        self.run_status_label.setWordWrap(True)

    def _on_task_status(self, data: dict) -> None:
        self._task_status = dict(data)
        self._show_run_status(data)

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

        if self._controller is not None:
            self.pause_btn.setText("恢复 / F9" if self._controller.paused else "暂停 / F9")
            if self._controller.stopped:
                self.stop_btn.setText("等待安全停止…")
                self.stop_btn.setEnabled(False)
                self.pause_btn.setEnabled(False)

        from zzz_ddtool.paths import status_file as _status_file_path

        text = "运行状态：—（还没有运行记录）"
        status_file = _status_file_path()
        if status_file.exists():
            try:
                data = json.loads(status_file.read_text(encoding="utf-8"))
                self._show_run_status(data)
                text = self.run_status_label.text()
            except Exception:
                pass
        self.run_status_label.setText(text)
        if self._task_status is not None:
            self._show_run_status(self._task_status)
        # 读取统一日志文件，覆盖导航、OCR、执行器等模块；日志轮转/设置重建也能继续读取。
        if not hasattr(self, "run_log"):
            return
        from zzz_ddtool.paths import logs_dir

        try:
            with (logs_dir() / "ddtool.log").open("rb") as stream:
                size = stream.seek(0, 2)
                offset = max(0, size - 128 * 1024)
                stream.seek(offset)
                raw = stream.read()
            if offset:
                raw = raw.partition(b"\n")[2]
            tail = "\n".join(raw.decode("utf-8", errors="replace").splitlines()[-300:])
        except OSError:
            return
        if tail == self._log_tail:
            return
        self._log_tail = tail
        bar = self.run_log.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2
        position = bar.value()
        self.run_log.setPlainText(tail)
        bar.setValue(bar.maximum() if follow else position)

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
            # 任务链是空的：右侧保持空。不拿 preset 里的权重来填——那是档案里的
            # 残留值，会在「没选中任何任务」时显示出来，属于假信息。
            self._active_slot = preset.filter_slot
            self._filter_sets = []
            accepted = None
        for slot, radio in self.filter_slots.items():
            radio.blockSignals(True)
            radio.setChecked(slot == self._active_slot)
            radio.blockSignals(False)
        self._update_set_summary()
        if self._jobs:
            self._apply_slot_widgets(self._slot_plans[self._active_slot])
        self._refresh_chain_list(self._editing_job_id)
        self._reset_saved_state()
        self.path_label.setText(self._path.name)
        self.path_label.setToolTip(str(self._path))
        self._loading = False
        if self._jobs:
            self.rebuild_cards(accepted)
        else:
            self._clear_editor()

    def _schedule_rebuild(self) -> None:
        if self._loading or self._rebuilding:
            return
        self._rebuild_timer.start()

    # 组合卡片超过这个数量才提示「正在生成…」，小批量重建是毫秒级，
    # 弹提示反而是一闪而过，比不提示更烦。
    _BUSY_CARD_THRESHOLD = 120

    def _begin_busy(self, text: str) -> None:
        """在建卡前把提示先画出来。

        建卡是同步的，会把事件循环堵死——所以提示必须在堵死**之前**排一次绘制，
        否则它和卡片一起在最后才出现，等于没提示。这是这里唯一有意义的一步。
        """
        self.status.setText(text)
        if not self._busy_cursor:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self._busy_cursor = True
        # 排一次绘制把上面的文案真正刷到屏上；之后主线程再堵，用户也能看见这句话。
        QApplication.processEvents()

    def _end_busy(self) -> None:
        if self._busy_cursor:
            QApplication.restoreOverrideCursor()
            self._busy_cursor = False

    def rebuild_cards(self, accepted: tuple[str, ...] | None = None) -> None:
        if self._rebuilding:
            # processEvents（_begin_busy 里那一下）可能把已到期的重建定时器放出来，
            # 造成重入。记下来，等这一轮跑完再补一次，避免嵌套重建。
            self._rebuild_pending = True
            return
        self._rebuilding = True
        try:
            self._rebuild_cards_inner(accepted)
        finally:
            self._rebuilding = False
            self._end_busy()
            if self._rebuild_pending:
                self._rebuild_pending = False
                self._schedule_rebuild()

    def _rebuild_cards_inner(self, accepted: tuple[str, ...] | None = None) -> None:
        blocked = set(self._selected_mains())
        weights = {name: value for name, value in self.weight_editor.weights().items() if name not in blocked}
        if not weights:
            # 一个词条都没有就没有组合可言。注意 enumerate_all 在权重为空时仍会产出
            # 「全废词条」的图案，照着建就会在右侧摆出几张看似真实的卡片——那是假信息。
            # 直接清空。新建任务后右侧必须是真的空，就是靠这里。
            for section in self.sections.values():
                section.set_cards([], lambda _pattern_id: False)
            self.trial.set_name_choices([], blocked)
            self._update_status()
            return
        patterns = enumerate_all(weights, hide_junk_bonus=True)
        grouped: dict[tuple[str, int], list[Pattern]] = defaultdict(list)
        for pattern in patterns:
            grouped[(pattern.origin.value, pattern.checkpoint)].append(pattern)
        is_checked = self._card_check_policy(accepted)
        # 先算出这一轮要真建多少张卡，只有够大才值得提示。
        # 判定结果顺便复用给下面的主循环，避免同一个 patterns_match 跑两遍。
        todo: dict[tuple[str, int], list[Pattern]] = {}
        for key, section in self.sections.items():
            wanted = grouped.get(key, [])
            if not section.patterns_match(wanted):
                todo[key] = wanted
        pending_cards = sum(len(wanted) for wanted in todo.values())
        if pending_cards >= self._BUSY_CARD_THRESHOLD:
            self._begin_busy(f"正在生成组合…  共 {pending_cards} 张，请稍候")
        for key, section in self.sections.items():
            wanted = grouped.get(key, [])
            if key not in todo:
                section.update_card_states(wanted, is_checked)
            else:
                section.set_cards(wanted, is_checked)
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
        # 落盘 / 试判 / 开始任务都走这里，是「离开编辑器」的汇总口：先把当前编辑内容
        # 写回任务节点，再取配置，否则跑的是上一次提交的旧值。
        self._commit_editor()
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
            remember_config_path(self._path)

    def save_current(self) -> None:
        save_preset(self._path, self.collect_preset())
        self._set_saved_state()

    def _set_saved_state(self) -> None:
        """保存成功后右侧状态常驻显示绿字时间戳，直到下次保存或被载入重置。"""
        self.save_label.setObjectName("saveState")
        self._repolish(self.save_label)
        self.save_label.setText(f"✓ 已保存 · {datetime.now().strftime('%H:%M:%S')}")
        self.save_label.setToolTip(str(self._path))

    def _reset_saved_state(self) -> None:
        self.save_label.setObjectName("saveStateIdle")
        self._repolish(self.save_label)
        self.save_label.setText("未保存")
        self.save_label.setToolTip(str(self._path))

    @staticmethod
    def _repolish(widget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)

    def save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "另存预设", str(self._path), "YAML (*.yaml *.yml)")
        if not path:
            return
        self._path = Path(path)
        self.save_current()
        # 另存为换了工作文件，下次启动就打开它
        remember_config_path(self._path)

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
        if self._run_thread is not None:
            self._close_after_run = True
            self._stop_run()
            event.ignore()
            return
        from zzz_ddtool.overlay import overlay_shutdown

        overlay_shutdown()
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


class _RunThread(QThread):
    """在子线程跑 Runner，避免阻塞 GUI 事件循环；Runner 内部 F9/F10 热键照常生效。"""

    status_changed = Signal(dict)

    def __init__(self, preset: Preset, controller) -> None:
        super().__init__()
        self._preset = preset
        self._controller = controller

    def run(self) -> None:
        try:
            from zzz_ddtool.runner import Runner

            Runner(self._preset, controller=self._controller, on_status=self.status_changed.emit).run()
        except Exception as exc:
            from zzz_ddtool.logutil import get_logger

            get_logger("zzz_ddtool.gui").exception("任务线程失败")
            self.status_changed.emit({"phase": "failed", "message": str(exc)})
        finally:
            self._controller.close()


def run_gui(config: Path | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    apply_light_style(app)
    window = MainWindow(config)
    window.show()
    return app.exec()

