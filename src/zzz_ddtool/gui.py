"""Desktop window: weight editor + pattern cards + trial decide.

Layout stays the card-picker window. White Fluent page, accent #0078D7.
Spin boxes keep the system arrows (not styled).
"""

from __future__ import annotations

import re
import sys
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
    QInputDialog,
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
    QStyle,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from zzz_ddtool.chain import TaskJob, job_from_dict, new_job_id
from zzz_ddtool.config import Preset, load_preset, preset_from_dict, save_preset
from zzz_ddtool.gamedata import (
    FIXED_MAINSTATS,
    SETS,
    SLOTS,
    SUBSTAT_NAMES,
    blocked_substats,
    empty_slot_plan,
    selectable_mainstats,
)
from zzz_ddtool.seticons import paint_set_icon
from zzz_ddtool.decide import decide
from zzz_ddtool.models import Disc, Substat
from zzz_ddtool.origin import Origin
from zzz_ddtool.patterns import (
    CHECKPOINTS, EMPTY, JUNK, Pattern, Slot, canonicalize,
    enumerate_patterns, first_filter, score_slots, useful_ordered,
)

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
    from zzz_ddtool.userdata import persistent_config_path

    persistent = persistent_config_path(path)
    if persistent.exists():
        if persistent != path:
            remember_config_path(persistent)
        return persistent
    return None


def default_config_path() -> Path:
    """打开公共存档目录中的上次配置，默认档也是普通可编辑存档。"""
    from zzz_ddtool.paths import plans_dir
    from zzz_ddtool.userdata import migrate_plans

    migrate_plans()
    remembered = last_config_path()
    if remembered is not None:
        return remembered
    return plans_dir() / "example.yaml"


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
        self._slot_rows: list[tuple[QLabel, QLabel]] = []
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
            self._slot_rows.append((name, extra))

    def set_pattern(self, pattern: Pattern) -> None:
        """就地换一张图案：只改写文字，不销毁重建控件。

        改词条单价时，图案的数量和分行结构都不变，变的只是分值与行的排列；
        而重建一张卡要 1.5ms（控件树 + 布局 + 样式匹配），全屏 800 多张就是
        两秒多。这里逐行做 diff，没变的行一个字节都不写。

        行数恒为 MAX_SUBSTAT_SLOTS（patterns.canonicalize 会补齐并截断），
        所以 self._slot_rows 与 pattern.slots 一定等长。
        """
        self.pattern = pattern
        for (name_label, extra_label), slot in zip(self._slot_rows, pattern.slots):
            if name_label.text() != slot.name:
                name_label.setText(slot.name)
                wanted = JUNK if slot.name == JUNK else EMPTY if slot.name == EMPTY else "slot"
                if name_label.objectName() != wanted:
                    # objectName 是 QSS 选择器的依据，换了必须重新匹配样式
                    name_label.setObjectName(wanted)
                    name_label.style().unpolish(name_label)
                    name_label.style().polish(name_label)
            text = f"+{slot.bonus}" if slot.bonus > 0 else ""
            if extra_label.text() != text:
                extra_label.setText(text)
        self.set_score(pattern.score)

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
    PAGE_SIZE = 24

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
        self.patterns: list[Pattern] = []
        self._ids: list[str] = []
        self._checked: set[str] = set()
        self._visible_indices: list[int] = []
        self._page = 0

        self.require = QDoubleSpinBox()
        self.require.setDecimals(1)
        self.require.setRange(0.0, 30.0)
        self.require.setSingleStep(0.1)
        self.require.setKeyboardTracking(False)
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
        paging = QHBoxLayout()
        self.previous_btn = QPushButton()
        self.previous_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        self.previous_btn.setToolTip("上一页")
        self.next_btn = QPushButton()
        self.next_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward))
        self.next_btn.setToolTip("下一页")
        self.page_label = QLabel()
        self.previous_btn.clicked.connect(lambda: self._change_page(-1))
        self.next_btn.clicked.connect(lambda: self._change_page(1))
        paging.addStretch()
        paging.addWidget(self.previous_btn)
        paging.addWidget(self.page_label)
        paging.addWidget(self.next_btn)
        layout.addLayout(paging)
        self.apply_filter()

    def set_cards(self, patterns: list[Pattern], is_checked) -> None:  # noqa: ANN001
        self._set_patterns(patterns, is_checked)
        self.apply_filter()

    def _set_patterns(self, patterns: list[Pattern], is_checked) -> None:  # noqa: ANN001
        self.patterns = patterns
        self._ids = [pattern.pattern_id for pattern in patterns]
        self._checked = {pid for pid in self._ids if is_checked(pid)}
        self._page = 0

    def _clear_cards(self) -> None:
        while self.flow.count():
            item = self.flow.takeAt(self.flow.count() - 1)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.cards = []

    def _render_page(self) -> None:
        start = self._page * self.PAGE_SIZE
        indices = self._visible_indices[start:start + self.PAGE_SIZE]
        if len(indices) != len(self.cards):
            self._clear_cards()
            for index in indices:
                card = PatternCard(self.patterns[index], checked=self._ids[index] in self._checked)
                card.toggled.connect(lambda c=card: self._on_card_toggled(c))
                self.flow.addWidget(card)
                self.cards.append(card)
        else:
            for card, index in zip(self.cards, indices):
                card.set_pattern(self.patterns[index])
                card.set_checked_silent(self._ids[index] in self._checked)
        for card in self.cards:
            card.setVisible(True)
        pages = max(1, (len(self._visible_indices) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page_label.setText(f"{self._page + 1} / {pages}")
        self.previous_btn.setEnabled(self._page > 0)
        self.next_btn.setEnabled(self._page + 1 < pages)
        self.flow_host.updateGeometry()
        self.updateGeometry()

    def _change_page(self, delta: int) -> None:
        pages = max(1, (len(self._visible_indices) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._page = min(pages - 1, max(0, self._page + delta))
        self._render_page()

    def _on_card_toggled(self, card: PatternCard) -> None:
        pid = card.pattern.pattern_id
        if card.checkbox.isChecked():
            self._checked.add(pid)
        else:
            self._checked.discard(pid)
        self.selection_changed.emit()

    def can_reuse(self, patterns: list[Pattern]) -> bool:
        return len(patterns) == len(self.patterns)

    def reuse_cards(self, patterns: list[Pattern], is_checked) -> None:  # noqa: ANN001
        self._set_patterns(patterns, is_checked)
        self.apply_filter()

    def apply_filter(self) -> None:
        need = self.require.value()
        self._visible_indices = [
            i for i, pattern in enumerate(self.patterns) if pattern.score + 1e-9 >= need
        ]
        self._page = 0
        self.count_label.setText(f"{len(self._visible_indices)} / {len(self.patterns)} 张")
        self._render_page()

    def check_visible(self, on: bool) -> None:
        ids = {self._ids[i] for i in self._visible_indices}
        if on:
            self._checked.update(ids)
        else:
            self._checked.difference_update(ids)
        for card in self.cards:
            card.set_checked_silent(on)
        self.selection_changed.emit()

    def visible_accepted(self) -> list[str]:
        return [self._ids[i] for i in self._visible_indices if self._ids[i] in self._checked]

    def _on_require(self, _value: float) -> None:
        self.apply_filter()
        self.require_changed.emit()

class WeightEditor(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[QComboBox, QDoubleSpinBox, QWidget]] = []
        self._last_weights: dict[str, float] = {}
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
        previous_block = self.blockSignals(True)
        while self._rows:
            self._remove_row(self._rows[0][2])
        for name, value in weights.items():
            if name not in SUBSTAT_NAMES:
                continue
            self._add_row(name, value)
        self._last_weights = self.weights()
        self.blockSignals(previous_block)

    def _notify_changed(self) -> None:
        current = self.weights()
        if current != self._last_weights:
            self._last_weights = current
            self.changed.emit()

    def weights(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for combo, spin, _row in self._rows:
            name = combo.currentText().strip()
            if name not in SUBSTAT_NAMES:
                continue
            out[name] = float(spin.value())
        return out

    def _choices(self) -> list[str]:
        """单价表保留全部合法副词条，主属性排除只发生在计算侧。"""
        return list(KNOWN_SUBSTATS)

    def _add_from_combo(self) -> None:
        name = self.add_combo.currentText().strip()
        # 只认真正的副词条。这个下拉框是可编辑的，用户能敲进任意字符串；不挡的话
        # 敲个「穿透率」点一下「添加词条」就能建出一行，界面上就多出游戏里选不中的词条。
        if (
            not name
            or name not in SUBSTAT_NAMES
            or name in self.weights()
        ):
            return
        self._add_row(name, 1.0)
        self._notify_changed()

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
        combo.activated.connect(lambda *_: self._notify_changed())
        combo.lineEdit().editingFinished.connect(self._notify_changed)
        spin.valueChanged.connect(lambda *_: self._notify_changed())
        combo.setFixedWidth(150)
        layout.addWidget(combo)
        layout.addWidget(spin)
        layout.addWidget(remove)
        layout.addStretch()
        self._rows_box.addWidget(row)
        self._rows.append((combo, spin, row))

    def _delete(self, row: QWidget) -> None:
        self._remove_row(row)
        self._notify_changed()

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

    def set_name_choices(self, blocked: set[str] | None = None) -> None:
        """点名下拉的候选：只给真正的副词条，减去当前被 blocked 的主属性。

        候选**只认** data/stats.json（经 KNOWN_SUBSTATS）。这里原先会把调用方传进来的
        `names`（权重表里的词条名）中多出来的名字追加进候选，但那个追加是多余的：点名是
        给「这件盘的副词条」命名，候选必须是**全部 10 个**——没加权的副词条也要能选，
        它们会算成废词条；而权重表里的名字本来就是这 10 个的子集。于是 `extras` 正常时
        是空集，唯一的效果是在权重表有脏名时把脏名注入候选。已去掉，参数一并删掉。
        """
        blocked = set(blocked or ())
        allowed = [name for name in KNOWN_SUBSTATS if name not in blocked]
        for combo, _rolls in self.rows:
            current = combo.currentText()
            if current in blocked:
                current = ""
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("")
            combo.addItems(allowed)
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
        self._preview_dirty = True
        self._selection_seed: tuple[str, ...] | None = None
        self._preview_generation = 0
        self._preview_thread = None
        self._preview_request = None
        self._preview_install = []
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._install_preview_section)
        self._closing = False
        self._exit_after_close: int | None = None
        self._utility_thread = None
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
        for box in (self.require_s, self.skip_locked, self.skip_equipped, self.skip_discarded):
            box.toggled.connect(self._mark_unsaved)
        self.max_discs.valueChanged.connect(self._mark_unsaved)

    def _build_header(self) -> QHBoxLayout:
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("驱动盘强化辅助")
        title.setObjectName("bannerTitle")
        hint = QLabel("左边任务链，中间编当前部位，右边勾组合。设完一号就新建任务，不必凑齐 6 个。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
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
        self.archive_combo.lineEdit().setPlaceholderText("选择或输入存档名称")
        self._archive_names: set[str] = set()
        self.archive_combo.currentTextChanged.connect(self._update_archive_buttons)
        self.archive_apply_btn = apply_btn = QPushButton("套用")
        apply_btn.clicked.connect(self._apply_archive)
        self.archive_save_btn = save_btn = QPushButton("保存存档")
        save_btn.clicked.connect(self._save_archive)
        self.archive_delete_btn = del_btn = QPushButton("删除")
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
        self.archive_status = QLabel()
        self.archive_status.setWordWrap(True)
        self.archive_status.setObjectName("hint")
        archive_layout.addWidget(self.archive_status)
        layout.addWidget(archive_box)

        flags_box = QGroupBox("硬过滤（任务链共用）")
        flags = QVBoxLayout(flags_box)
        self.require_s = QCheckBox("只要 S 级")
        self.skip_locked = QCheckBox("跳过锁定")
        self.skip_equipped = QCheckBox("跳过已装备")
        self.skip_discarded = QCheckBox("跳过已有弃置标记")
        for box in (self.require_s, self.skip_locked, self.skip_equipped, self.skip_discarded):
            box.setChecked(True)
            flags.addWidget(box)
        layout.addWidget(flags_box)
        task_box = QGroupBox("当前任务限制")
        task_limits = QVBoxLayout(task_box)
        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("本次最多处理"))
        self.max_discs = QSpinBox()
        self.max_discs.setRange(1, 5000)
        self.max_discs.setValue(100)
        limit_row.addWidget(self.max_discs)
        limit_row.addWidget(QLabel("个盘子"))
        limit_row.addStretch()
        task_limits.addLayout(limit_row)
        layout.addWidget(task_box)

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
        self._main_boxes_by_slot: dict[int, dict[str, QRadioButton]] = {}
        self.filter_main_boxes: dict[str, QRadioButton] = {}
        self._main_groups: dict[int, QButtonGroup] = {}
        self._main_any_by_slot: dict[int, QRadioButton] = {}
        self.main_host = QStackedWidget()
        self.main_host.setObjectName("mainHost")
        self.main_host.setFixedHeight(80)
        self._mainstat_pages: dict[int, QWidget] = {}
        for slot, names in _MAINSTAT_PAGES.items():
            boxes: dict[str, QRadioButton] = {}
            page = QWidget()
            page.setFixedHeight(80)
            grid = QGridLayout(page)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(6)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(2, 1)
            group = QButtonGroup(page)
            self._main_groups[slot] = group
            any_main = QRadioButton("不限")
            group.addButton(any_main)
            any_main.setChecked(True)
            any_main.toggled.connect(self._on_filter_changed)
            self._main_any_by_slot[slot] = any_main
            grid.addWidget(any_main, 0, 0)
            for idx, name in enumerate(names, start=1):
                box = QRadioButton(name)
                group.addButton(box)
                box.toggled.connect(self._on_filter_changed)
                boxes[name] = box
                grid.addWidget(box, idx // _MAINSTAT_COLS, idx % _MAINSTAT_COLS)
            self._mainstat_pages[slot] = page
            self._main_boxes_by_slot[slot] = boxes
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
        pick_filter.setToolTip("套装、部位与主属性手动选择；副属性按右侧 0 级组合自动筛选，所有等级一起处理。")
        pick_filter.clicked.connect(self._open_filter_dialog)
        game_row.addWidget(pick_filter)
        substat_hint = QLabel("副词条：自动")
        substat_hint.setObjectName("hint")
        substat_hint.setToolTip("根据右侧初始 3 词、4 词的 0 级选择自动筛选；筛选数量从最低命中数选到 4。")
        game_row.addWidget(substat_hint)
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
        self._mark_unsaved()
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
        self.max_discs.setValue(100)
        self._selection_seed = None
        self._filter_sets = []
        self._update_set_summary()
        self._refresh_main_boxes(set())
        self._sync_job_name_edit()
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
            max_discs=int(snap["max_discs"]),
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
            "min_useful_types": 0,
            "max_discs": job.max_discs if job.max_discs is not None else self._legacy_max_discs,
        }
        self._slot_plans[job.slot] = plan
        self._apply_slot_widgets(plan)
        self._loading = False
        self._sync_job_name_edit()
        self._selection_seed = job.accepted
        self._reset_preview()

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
        self._commit_editor()
        job = TaskJob(slot=self._active_slot, name=self._unique_job_name("任务"), max_discs=100)
        self._jobs.append(job)
        self._mark_unsaved()
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
        self._mark_unsaved()
        self._load_job(copied)
        self._refresh_chain_list(copied.job_id)

    def _remove_job(self) -> None:
        row = self.chain_list.currentRow()
        if row < 0 or row >= len(self._jobs):
            return
        removed = self._jobs.pop(row)
        self._mark_unsaved()
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
        self._mark_unsaved()
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
        self.tabs.currentChanged.connect(lambda _index: self._update_status())
        return self.tabs

    def _selected_slots(self) -> list[int]:
        return [self._active_slot]

    def _selected_mains(self) -> list[str]:
        return [name for name, box in self.filter_main_boxes.items() if box.isChecked()]

    def _blocked_substats(self) -> set[str]:
        return blocked_substats(self._active_slot, self._selected_mains())

    def _effective_weights(self) -> dict[str, float]:
        blocked = self._blocked_substats()
        return {name: value for name, value in self.weight_editor.weights().items() if name not in blocked}

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
        self._mark_unsaved()
        self._update_set_summary()

    def _refresh_main_boxes(self, keep: set[str] | None = None) -> None:
        keep = set(keep if keep is not None else self._selected_mains())
        available = set(selectable_mainstats(self._selected_slots()))
        active_slot = self._active_slot if self._active_slot in self._mainstat_pages else None
        for slot, boxes in self._main_boxes_by_slot.items():
            group = self._main_groups[slot]
            group.setExclusive(False)
            choice = next((name for name in boxes if slot == self._active_slot and name in keep), None)
            for name, box in boxes.items():
                box.blockSignals(True)
                box.setChecked(name == choice)
                box.blockSignals(False)
            any_main = self._main_any_by_slot[slot]
            any_main.blockSignals(True)
            any_main.setChecked(choice is None)
            any_main.blockSignals(False)
            group.setExclusive(True)
        if active_slot is not None:
            self.main_host.setCurrentWidget(self._mainstat_pages[active_slot])
        else:
            self.main_host.setCurrentIndex(self.main_host.count() - 1)
        self.filter_main_boxes = self._main_boxes_by_slot.get(self._active_slot, {})
        if available:
            self.main_hint.setText(f"{self._active_slot} 号位可选主词条。已选主词条不会出现在右侧副词条里。")
        else:
            self.main_hint.setText(
                f"{self._active_slot} 号位主词条固定是{FIXED_MAINSTATS[self._active_slot]}，不用选。右侧只编这个部位的副词条。"
            )

    def _load_archives(self) -> dict:
        import json

        from zzz_ddtool.paths import archives_file, legacy_archives_file

        f = archives_file()
        migrate = not f.exists()
        source = legacy_archives_file() if migrate else f
        if not source.exists():
            return {}
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or any(not isinstance(item, dict) for item in data.values()):
            raise ValueError("存档文件格式不正确")
        if migrate:
            self._write_archives(data)
        return data

    def _write_archives(self, data: dict) -> None:
        import json

        from zzz_ddtool.paths import archives_file

        target = archives_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)

    def _update_archive_buttons(self, _text: str = "") -> None:
        exists = self.archive_combo.currentText().strip() in self._archive_names
        self.archive_apply_btn.setEnabled(exists)
        self.archive_delete_btn.setEnabled(exists)

    def _archive_error(self, exc: Exception) -> None:
        from zzz_ddtool.paths import archives_file

        self.archive_status.setText(f"存档失败：{exc}")
        self.archive_status.setToolTip(str(archives_file()))

    def _refresh_archive_combo(self, select: str | None = None) -> None:
        try:
            names = sorted(self._load_archives())
        except (OSError, ValueError) as exc:
            self._archive_error(exc)
            names = []
        self._archive_names = set(names)
        self.archive_combo.blockSignals(True)
        self.archive_combo.clear()
        self.archive_combo.addItems(names)
        if select:
            self.archive_combo.setCurrentText(select)
        self.archive_combo.blockSignals(False)
        self._update_archive_buttons()

    def _save_archive(self) -> None:
        name = self.archive_combo.currentText().strip()
        if not name:
            name, confirmed = QInputDialog.getText(self, "保存存档", "存档名称")
            name = name.strip()
            if not confirmed or not name:
                return
        snap = self._snapshot_slot()
        try:
            data = self._load_archives()
            data[name] = {
                "weights": dict(snap.get("weights") or {}),
                "stage_require": {
                    origin: dict(table)
                    for origin, table in (snap.get("stage_require") or {}).items()
                },
            }
            self._write_archives(data)
        except (OSError, ValueError) as exc:
            self._archive_error(exc)
            return
        self._refresh_archive_combo(name)
        self.archive_status.setText(f"已保存「{name}」。")

    def _apply_archive(self) -> None:
        name = self.archive_combo.currentText().strip()
        try:
            data = self._load_archives().get(name)
            if data is None:
                raise ValueError(f"没有叫「{name}」的存档")
            weights = {key: float(value) for key, value in dict(data.get("weights") or {}).items()}
            stage_require = {
                str(origin): {int(level): float(value) for level, value in dict(table).items()}
                for origin, table in dict(data.get("stage_require") or {}).items()
            }
        except (OSError, ValueError, TypeError) as exc:
            self._archive_error(exc)
            return
        if self._editing_job() is None:
            self._add_job()
        self.weight_editor.set_weights(weights)
        self._apply_stage_require(stage_require)
        self._selection_seed = None
        self._schedule_rebuild()
        self.archive_status.setText(f"已套用「{name}」到当前任务。")

    def _delete_archive(self) -> None:
        name = self.archive_combo.currentText().strip()
        try:
            data = self._load_archives()
            if name not in data:
                raise ValueError(f"没有叫「{name}」的存档")
            del data[name]
            self._write_archives(data)
        except (OSError, ValueError) as exc:
            self._archive_error(exc)
            return
        self._refresh_archive_combo()
        self.archive_status.setText(f"已删除「{name}」。")

    def _snapshot_slot(self) -> dict:
        stage_require = {"three": {}, "four": {}}
        for (origin, checkpoint), section in self.sections.items():
            stage_require[origin][checkpoint] = float(section.require.value())
        return {
            "weights": self.weight_editor.weights(),
            "stage_require": stage_require,
            "accepted": self._rebased_selection(filtered=True),
            "mainstats": tuple(self._selected_mains()),
            "sets": tuple(self._filter_sets),
            "min_useful_types": 0,
            "max_discs": int(self.max_discs.value()),
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
        self.max_discs.setValue(int(plan.get("max_discs") or 100))
        self._refresh_main_boxes(set(plan.get("mainstats") or ()))
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
        self._update_filter_summary()
        self._schedule_rebuild()

    def _origin_scroll(self, origin: Origin) -> dict[str, object]:
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(12, 8, 12, 8)
        for checkpoint in CHECKPOINTS:
            section = StageSection(origin, checkpoint, None)
            section.require_changed.connect(self._on_require_changed)
            section.selection_changed.connect(self._on_selection_changed)
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
        footer = QVBoxLayout(bar)
        footer.setContentsMargins(10, 8, 10, 8)
        row = QHBoxLayout()
        footer.addLayout(row)
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
        self.refresh_cards_btn = QPushButton("生成组合")
        self.refresh_cards_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_cards_btn.clicked.connect(lambda: self.rebuild_cards())
        self._task_controls.append(self.refresh_cards_btn)
        row.addWidget(all_on)
        row.addWidget(all_off)
        row.addWidget(self.refresh_cards_btn)
        row.addStretch()
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
        self.status.setWordWrap(True)
        footer.addWidget(self.status)
        footer.addWidget(self.run_status_label)
        self._run_timer = QTimer(self)
        self._run_timer.setInterval(1000)
        self._run_timer.timeout.connect(self._refresh_run_status)
        self._run_timer.start()
        self._refresh_run_status()
        self._refresh_archive_combo()
        return bar

    def _on_start_clicked(self) -> None:
        """用当前 UI 配置在子线程启动 Runner；运行期间禁用按钮，结束自动恢复。"""
        if self._run_thread is not None or self._utility_thread is not None:
            return
        try:
            preset = self.collect_preset()
        except Exception as exc:  # 配置缺失 / 构建失败
            QMessageBox.warning(self, "无法启动", f"配置构建失败：{exc}")
            return
        if not preset.task_chain:
            QMessageBox.warning(self, "无法启动", "请先添加并配置任务。")
            return
        for index, job in enumerate(preset.task_chain, start=1):
            blocked = blocked_substats(job.slot, job.mainstats)
            if not any(value > 0 and name in SUBSTAT_NAMES and name not in blocked for name, value in job.weights.items()):
                QMessageBox.warning(self, "无法启动", f"第 {index} 个任务「{job.name}」没有可用的正权重副词条。")
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
            + (f"当前任务:{data['job_processed']}/{data.get('job_max_discs', 0)}  " if 'job_processed' in data else "")
            +
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
        from zzz_ddtool.updater import check_for_update

        self._start_utility(check_for_update, self._handle_update_status, "正在检查更新…")

    def _handle_update_status(self, st) -> None:  # noqa: ANN001
        from zzz_ddtool.updater import apply_data_update, apply_program_update

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
            if QMessageBox.question(self, "检查更新", ask, yes | no, yes) == yes:
                self._start_utility(
                    lambda: apply_program_update(st.program_url), lambda _result: None, "正在下载程序更新…"
                )
        elif st.data_url:
            ask = f"{detail}\n\n将下载数据包（套装名/词条名/图片），立即生效、无需重启。现在更新？"
            if QMessageBox.question(self, "检查更新", ask, yes | no, yes) == yes:
                self._start_utility(
                    lambda: apply_data_update(st.data_url),
                    lambda _result: QMessageBox.information(self, "检查更新", "数据已更新，立即生效。"),
                    "正在下载数据更新…",
                )
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
        from zzz_ddtool.userdata import persistent_config_path

        path = persistent_config_path(path)
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
        self._label_policy = preset.labels
        self.name_edit.setText(preset.name)
        self.require_s.setChecked(preset.require_s_rank)
        self.skip_locked.setChecked(preset.skip_locked)
        self.skip_equipped.setChecked(preset.skip_equipped)
        self.skip_discarded.setChecked(preset.skip_discarded)
        self.max_discs.setValue(preset.max_discs)
        self._legacy_max_discs = preset.max_discs
        self._jobs = [
            copied
            for job in preset.task_chain
            for copied in [job_from_dict(job.to_dict())]
            if copied is not None
        ]
        for job in self._jobs:
            job.min_useful_types = 0
            job.mainstats = tuple(name for name in job.mainstats if name in _MAINSTAT_PAGES.get(job.slot, ()))[:1]
            if job.max_discs is None:
                job.max_discs = preset.max_discs
        self._editing_job_id = self._jobs[0].job_id if self._jobs else None
        self._slot_plans = {slot: empty_slot_plan(slot) for slot in SLOTS}
        for slot, plan in (preset.slot_plans or {}).items():
            self._slot_plans[int(slot)] = dict(plan)
            self._slot_plans[int(slot)]["min_useful_types"] = 0
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
                "min_useful_types": 0,
                "max_discs": first.max_discs if first.max_discs is not None else preset.max_discs,
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
            self._selection_seed = accepted
            self._reset_preview()
        else:
            self._clear_editor()

    def _schedule_rebuild(self) -> None:
        if self._loading:
            return
        self._invalidate_preview()
        self._mark_unsaved()
        self.trial.set_name_choices(self._blocked_substats())
        self._update_status()

    def _reset_preview(self) -> None:
        self._invalidate_preview()
        for section in self.sections.values():
            section.set_cards([], lambda _pid: False)
        self.trial.set_name_choices(self._blocked_substats())
        self._update_status()

    def _invalidate_preview(self) -> None:
        self._preview_generation += 1
        self._preview_dirty = True
        self._preview_timer.stop()
        self._preview_install = []
        self._preview_request = None
        if self._preview_thread is not None:
            self._preview_thread.requestInterruption()
        for section in self.sections.values():
            section.flow_host.hide()
            section.previous_btn.setEnabled(False)
            section.next_btn.setEnabled(False)
        self.refresh_cards_btn.setText("生成组合")

    def rebuild_cards(self, accepted: tuple[str, ...] | None = None) -> None:
        if self._closing:
            return
        if accepted is not None:
            self._selection_seed = accepted
        self._invalidate_preview()
        blocked = self._blocked_substats()
        weights = self._effective_weights()
        self.trial.set_name_choices(blocked)
        if not useful_ordered(weights):
            for section in self.sections.values():
                section.set_cards([], lambda _pid: False)
                section.flow_host.show()
            self._preview_dirty = False
            self._update_status()
            return
        request = (self._preview_generation, weights, self._card_check_policy())
        if self._preview_thread is not None:
            self._preview_request = request
        else:
            self._start_preview(request)

    def _start_preview(self, request: tuple) -> None:
        generation, weights, policy = request
        self._preview_policy = policy
        worker = _PatternThread(generation, weights, self)
        self._preview_thread = worker
        worker.result.connect(self._on_preview_result)
        worker.failed.connect(self._on_preview_failed)
        worker.finished.connect(self._on_preview_finished)
        self.status.setText("正在后台计算组合…")
        self.refresh_cards_btn.setText("重新生成")
        worker.start()

    def _on_preview_result(self, generation: int, grouped: dict) -> None:
        if self._closing or generation != self._preview_generation:
            return
        self._preview_install = list(grouped.items())
        self.status.setText("正在显示组合…")
        self._preview_timer.start(1)

    def _install_preview_section(self) -> None:
        if self._closing or not self._preview_install:
            return
        key, patterns = self._preview_install.pop(0)
        section = self.sections[key]
        if section.can_reuse(patterns):
            section.reuse_cards(patterns, self._preview_policy)
        else:
            section.set_cards(patterns, self._preview_policy)
        if self._preview_install:
            # 每次只安装一个分区，事件循环可以处理输入、绘制及关闭请求。
            self._preview_timer.start(1)
            return
        self._preview_dirty = False
        for section in self.sections.values():
            section.flow_host.show()
        self.refresh_cards_btn.setText("刷新组合")
        self._update_status()

    def _on_preview_failed(self, generation: int, message: str) -> None:
        if generation == self._preview_generation and not self._closing:
            self.status.setText(f"组合生成失败：{message}")
            self.refresh_cards_btn.setText("重试生成")

    def _on_preview_finished(self) -> None:
        worker = self._preview_thread
        self._preview_thread = None
        worker.deleteLater()
        if self._closing:
            self.close()
        elif self._preview_request is not None:
            request = self._preview_request
            self._preview_request = None
            self._start_preview(request)

    def _rebased_selection(self, filtered: bool = False) -> tuple[str, ...] | None:
        weights = self._effective_weights()
        blocked = self._blocked_substats()
        useful = set(useful_ordered(weights))
        if self._selection_seed is None:
            return None
        if not useful:
            return ()
        out = set()
        for pid in self._selection_seed:
            try:
                origin, checkpoint, body = pid.split(":", 2)
                slots = []
                for token in body.split("|"):
                    name, plus, bonus = token.rpartition("+")
                    if not plus:
                        name, bonus = token, "0"
                    slots.append(Slot(name if name in useful or name == EMPTY else JUNK, int(bonus)))
                canonical = canonicalize(slots, weights)
                pattern = Pattern(Origin(origin), int(checkpoint), canonical, score_slots(canonical, weights))
            except ValueError:
                continue
            if any(token.split("+", 1)[0] in blocked for token in body.split("|")):
                continue
            if filtered:
                section = self.sections.get((pattern.origin.value, pattern.checkpoint))
                if section is None or pattern.score + 1e-9 < section.require.value():
                    continue
                if not first_filter([pattern]):
                    continue
            out.add(pattern.pattern_id)
        return tuple(sorted(out))

    def _card_check_policy(self):
        accepted = self._rebased_selection()
        if accepted is None:
            return lambda _pid: True
        keep = frozenset(accepted)
        return lambda pid: pid in keep

    def _on_selection_changed(self) -> None:
        if self._preview_dirty:
            return
        self._selection_seed = tuple(
            pid for section in self.sections.values() for pid in section._ids if pid in section._checked
        )
        self._mark_unsaved()
        self._update_status()

    def _on_require_changed(self) -> None:
        self._mark_unsaved()
        self._update_status()

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
                "labels": {"discard": self._label_policy.discard, "save": self._label_policy.save},
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
                **({"accepted": list(current["accepted"])} if current.get("accepted") is not None else {}),
            }
        )

    def _accepted_ids(self) -> list[str]:
        ids: list[str] = []
        for section in self.sections.values():
            ids.extend(section.visible_accepted())
        return ids

    def _check_visible(self, on: bool) -> None:
        if self._preview_dirty:
            return
        for section in self._current_sections():
            section.check_visible(on)
        self._update_status()

    def _current_sections(self) -> list[StageSection]:
        index = self.tabs.currentIndex()
        origin = Origin.THREE if index == 0 else Origin.FOUR if index == 1 else None
        return [section for section in self.sections.values() if origin is None or section.origin is origin]

    def _update_status(self) -> None:
        if self._preview_dirty:
            self.status.setText("组合待生成")
            return
        sections = self._current_sections()
        visible = sum(len(section._visible_indices) for section in sections)
        total = sum(len(section.patterns) for section in sections)
        picked = sum(len(section.visible_accepted()) for section in sections)
        scope = self.tabs.tabText(self.tabs.currentIndex()) if self.tabs.currentIndex() < 2 else "全部组合"
        self.status.setText(
            f"任务链 {len(self._jobs)} 节 · 正在编 {self._active_slot} 号 · "
            f"{scope}：组合共 {total} 种，符合门槛 {visible}，已勾选 {picked}"
        )

    def _toggle_on_top(self, on: bool) -> None:
        flags = self.windowFlags()
        if on:
            self.setWindowFlags(flags | Qt.WindowType.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(flags & ~Qt.WindowType.WindowStaysOnTopHint)
        self.show()

    def _start_utility(self, operation, on_success, message: str) -> None:  # noqa: ANN001
        if self._utility_thread is not None or self._run_thread is not None or self._closing:
            return
        self._utility_success = on_success
        worker = _UtilityThread(operation, self)
        self._utility_thread = worker
        worker.finished.connect(self._on_utility_finished)
        self.start_btn.setEnabled(False)
        for button in self._task_controls:
            button.setEnabled(False)
        self.status.setText(message)
        worker.start()

    def _on_utility_finished(self) -> None:
        worker = self._utility_thread
        self._utility_thread = None
        worker.deleteLater()
        self.start_btn.setEnabled(True)
        for button in self._task_controls:
            button.setEnabled(True)
        if worker.exit_code is not None:
            self._exit_after_close = worker.exit_code
            self.close()
        elif self._closing:
            self.close()
        elif worker.error is not None:
            self.status.setText(f"操作失败：{worker.error}")
        else:
            self._update_status()
            self._utility_success(worker.value)

    def _show_capture_result(self, dest: Path, label: str) -> None:
        self.path_label.setText(f"{label}  {dest.name}")
        self.path_label.setToolTip(str(dest))

    def capture_game(self) -> None:
        from zzz_ddtool.capture import capture_game

        self._start_utility(
            capture_game, lambda dest: self._show_capture_result(dest, "已截取"), "正在截取游戏…"
        )

    def goto_warehouse(self) -> None:
        from zzz_ddtool.nav import goto_warehouse

        self._start_utility(
            goto_warehouse, lambda dest: self._show_capture_result(dest, "已进入驱动盘"),
            "正在进入驱动盘…",
        )

    def open_preset(self) -> None:
        from zzz_ddtool.paths import plans_dir

        path, _ = QFileDialog.getOpenFileName(self, "打开任务存档", str(plans_dir()), "YAML (*.yaml *.yml)")
        if path:
            self._load_path(Path(path))
            remember_config_path(self._path)

    def save_current(self) -> None:
        save_preset(self._path, self.collect_preset())
        remember_config_path(self._path)
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

    def _mark_unsaved(self) -> None:
        if not self._loading and self.save_label.objectName() != "saveStateIdle":
            self._reset_saved_state()

    @staticmethod
    def _repolish(widget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)

    def save_as(self) -> None:
        from zzz_ddtool.paths import plans_dir
        from zzz_ddtool.userdata import persistent_config_path

        path, _ = QFileDialog.getSaveFileName(self, "另存任务存档", str(plans_dir() / self._path.name), "YAML (*.yaml *.yml)")
        if not path:
            return
        self._path = persistent_config_path(Path(path))
        self.save_current()
        # 另存为换了工作文件，下次启动就打开它
        remember_config_path(self._path)

    def run_trial(self) -> None:
        disc = self.trial.disc()
        if disc is None:
            self.trial.result.setText("至少填一条副词条。")
            return
        preset = self.collect_preset()
        result = decide(disc, preset, preset.accepted)
        self.trial.result.setText(
            f"{result.action.value}  ·  {result.reason}\n"
            f"权重 {result.score}  ·  有效 {result.useful_types}/{result.max_useful_types}  ·  "
            f"+{result.checkpoint}  ·  {result.origin or '-'}"
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self._preview_request = None
        self._preview_install = []
        self._preview_timer.stop()
        if self._preview_thread is not None:
            self._preview_thread.requestInterruption()
            event.ignore()
            return
        if self._utility_thread is not None:
            self.status.setText("等待当前操作结束后关闭…")
            event.ignore()
            return
        if self._run_thread is not None:
            self._close_after_run = True
            self._stop_run()
            event.ignore()
            return
        from zzz_ddtool.overlay import overlay_shutdown

        overlay_shutdown()
        event.accept()
        self._run_timer.stop()
        if self._exit_after_close is not None:
            QApplication.exit(self._exit_after_close)


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


class _UtilityThread(QThread):
    def __init__(self, operation, parent: QWidget) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.operation = operation
        self.value = None
        self.error = None
        self.exit_code = None

    def run(self) -> None:
        try:
            self.value = self.operation()
        except SystemExit as exc:
            self.exit_code = int(exc.code or 0)
        except Exception as exc:
            self.error = str(exc)


class _PatternThread(QThread):
    result = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, generation: int, weights: dict[str, float], parent: QWidget) -> None:
        super().__init__(parent)
        self.generation = generation
        self.weights = dict(weights)

    def run(self) -> None:
        from time import monotonic, sleep

        next_yield = monotonic() + 0.01

        def cancelled() -> bool:
            nonlocal next_yield
            if self.isInterruptionRequested():
                return True
            # Python 穷举会争用 GIL，主动让出时间片以保证 Qt 输入及时处理。
            if monotonic() >= next_yield:
                sleep(0.001)
                next_yield = monotonic() + 0.01
            return self.isInterruptionRequested()

        try:
            grouped = {}
            for origin in Origin:
                for checkpoint in CHECKPOINTS:
                    if self.isInterruptionRequested():
                        return
                    patterns = enumerate_patterns(
                        self.weights, origin=origin, checkpoint=checkpoint,
                        cancelled=cancelled,
                    )
                    grouped[(origin.value, checkpoint)] = first_filter(patterns)
            if not self.isInterruptionRequested():
                self.result.emit(self.generation, grouped)
        except Exception as exc:
            self.failed.emit(self.generation, str(exc))


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

