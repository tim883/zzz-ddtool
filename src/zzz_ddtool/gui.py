"""Desktop window: weight editor + pattern cards + trial decide.

Layout stays the card-picker window. White Fluent page, accent #0078D7.
Spin boxes keep the system arrows (not styled).
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import datetime
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QPoint, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QSplitter,
    QTabWidget,
    QToolButton,
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
    possible_substats,
)
from zzz_ddtool.seticons import paint_set_icon
from zzz_ddtool.decide import decide
from zzz_ddtool.models import Disc, Substat
from zzz_ddtool.origin import Origin
from zzz_ddtool.taskcards import PatternCatalog, counts_signature, effective_required_substats, effective_weights, has_required_substats, rebase_selection, task_counts
from zzz_ddtool.version import current_version
from zzz_ddtool.widgets import NoWheelDoubleSpinBox, NoWheelSpinBox
from zzz_ddtool.patterns import (
    CHECKPOINTS, EMPTY, JUNK, Pattern,
    enumerate_filtered, enumerate_patterns, first_filter, useful_ordered,
)

# 部位中文标签：右侧 tab 与「拷贝对方本档要求」按钮都用它，保持一致。
_ORIGIN_CN = {Origin.THREE: "3词", Origin.FOUR: "4词"}

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

# 游戏内筛选-主词条按槽位固定网格，每页尺寸一致，切部位不抖动。
# 左栏窄，排两列：5 号位的选项在三列里会被挤掉半边。
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
        "风属性伤害",
    ),
    6: ("生命值%", "攻击力%", "防御力%", "能量自动回复", "冲击力", "异常掌控"),
}
_MAINSTAT_COLS = 2
# 各部位的主词条页取同一个高度：最高的是 5 号位（1 个「不限」+ 10 个选项 = 6 行）。
_MAINSTAT_PAGE_HEIGHT = 160

# 三栏宽度。笔记本 150% 缩放时逻辑宽只有 1280 左右，固定像素的默认宽会把右栏挤到
# 一张半卡片宽；所以默认宽按窗口宽度折算，右栏优先保证能并排放两张卡。
_CARD_WIDTH = 188
_CARD_SPACING = 10
_CHAIN_MIN_WIDTH = 380
_CONFIG_MIN_WIDTH = 300
_COMFORT_CARDS = 2
# 窗口默认（没有布局记录时）的尺寸；屏幕放不下就收进可用区。
_DEFAULT_WINDOW_SIZE = (1320, 820)


def _cards_column_width(cards: int) -> int:
    """右栏并排放下 `cards` 张卡片要多宽：卡片 + 间距 + 滚动区边距 12×2 + 滚动条 + 页签边框。"""
    return cards * _CARD_WIDTH + (cards - 1) * _CARD_SPACING + 24 + 12 + 6


def _default_column_sizes(total: int) -> list[int]:
    """没有布局记录时三栏的默认宽：任务链 / 配置栏按比例取、各自封顶，余下全给卡片栏。"""
    chain = max(_CHAIN_MIN_WIDTH, min(540, round(total * 0.33)))
    config = max(_CONFIG_MIN_WIDTH, min(360, round(total * 0.22)))
    return [chain, config, max(1, total - chain - config)]

# 全局字体：字号必须**同时**写进样式表（见下面 STYLESHEET 末尾那条 QAbstractItemView）。
# 列表类控件（任务链的 QListWidget、下拉框弹出的 QListView）只要被样式表命中，
# Qt 就用**系统默认字体**（Windows 上是 9pt「微软雅黑 UI」）重算字号，
# `apply_light_style` 里 app.setFont() 的 12pt 传不进去 —— 表现就是任务链、
# 下拉列表的字比旁边同一排的标签小一圈。所以两处共用下面这两个常量，别各写各的。
_UI_FONT_FAMILY = "Microsoft YaHei"
_UI_FONT_PT = 12

STYLESHEET = """
QMainWindow, QWidget#root, QScrollArea, QScrollArea > QWidget > QWidget {
    background: #f8f9fc;
    color: #1a1a1a;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
}
QLabel { color: #1a1a1a; }
QLabel#rowLabel { color: #605e5c; }
QLabel#hint { color: #605e5c; }
QLabel#saveStateIdle { color: #8a8a8a; }
QLabel#saveState { color: #107c10; font-weight: 700; }
QLabel#bannerTitle { font-size: 20px; font-weight: 700; color: #1a1a1a; }
QLabel#appVersion { color: #605e5c; font-size: 13px; }
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
QFrame#logPanel {
    background: #ffffff;
    border: 1px solid #e1e1e1;
    border-radius: 8px;
}
QLabel#panelTitle { color: #605e5c; }
QFrame#filterSplit { background: #e6e6e6; border: none; }
QToolButton#logToggle {
    border: none;
    background: transparent;
    color: #2b2b2b;
    font-size: 15px;
    font-weight: 700;
    padding: 0;
}
QToolButton#logToggle:hover { background: #e6e6e6; border-radius: 3px; }
QListWidget {
    background: #ffffff;
    border: 1px solid #e1e1e1;
    border-radius: 6px;
    color: #1a1a1a;
}
QListWidget::item { padding: 8px 6px; }
QListWidget::item:selected { background: #e8f3fc; color: #0078d7; }
QSplitter::handle { background: #e1e1e1; width: 6px; }
QSplitter#mainSplitter::handle:vertical {
    height: 6px;
    background: #f8f9fc;
    border-top: 1px solid #e1e1e1;
}
QSplitter#mainSplitter::handle:vertical:hover { border-top: 1px solid #b0b0b0; }
QScrollBar:vertical { background: #f8f9fc; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #c8c8c8; min-height: 32px; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""

# item view 补字号：见 _UI_FONT_PT 上面的说明。少了这一条，任务链和所有下拉列表
# 会退回系统默认的 9pt，比旁边的标签小一圈。QAbstractItemView 一并覆盖 QListView /
# QListWidget / QTreeView / QTableView，以后再加列表控件也不会漏。
# 字体链跟上面主规则保持一致，字号取 _UI_FONT_PT。
STYLESHEET += (
    '\nQAbstractItemView { font-family: "Microsoft YaHei", "Segoe UI", sans-serif;'
    " font-size: %dpt; }\n" % _UI_FONT_PT
)


# 任务链条目里序号和正文之间的分隔符。序号和正文的「4号」都是数字，
# 纯文本连成一片分不清哪个是顺序、哪个是部位，所以用一道竖线隔开。
_JOB_ITEM_SEP = "│"
_JOB_SEQ_DIGITS = 2  # 序号列默认按 2 位数字留宽（够 1~99）。1 和 10 的竖线、正文起点才对得上
_JOB_SEQ_COLOR = "#0078d7"        # 主题蓝，与按钮同色
_JOB_SEQ_SELECTED_COLOR = "#005a9e"  # 选中行正文也是蓝的，序号压深一档才分得开
_JOB_SEP_COLOR = "#9aa5b1"
_JOB_BODY_COLOR = "#1a1a1a"
_JOB_COUNTS_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_JOB_COUNTS_STATE_ROLE = int(Qt.ItemDataRole.UserRole) + 2
_JOB_COUNT_WIDTH = 84  # 10pt 下「2016/2016」约 63px，留点余量
_JOB_COUNT_FONT_PT = 10


def _job_count_rects(rect: QRect) -> tuple[QRect, QRect]:
    right = rect.right() - 5
    return tuple(QRect(right - (2 - i) * _JOB_COUNT_WIDTH + 1, rect.top(),
                       _JOB_COUNT_WIDTH, rect.height()) for i in range(2))


class _JobCountHeader(QWidget):
    def __init__(self, view: QListWidget) -> None:
        super().__init__()
        self.view = view
        self.setFixedHeight(28)
        self.setToolTip("已选 / 合格：按开始任务时的有效设置统计，汇总 +0～+15 六档及全部分页。\n"
                        "未生成卡片名单的任务会跳过。统计的是组合数量，不是仓库盘子数量。")
        view.verticalScrollBar().rangeChanged.connect(lambda *_: self.update())

    def paintEvent(self, event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        font = QFont(self.font())
        font.setPointSize(_JOB_COUNT_FONT_PT)
        painter.setFont(font)
        painter.setPen(QColor("#5f6b7a"))
        offset = self.mapFromGlobal(self.view.viewport().mapToGlobal(QPoint(0, 0))).x()
        rect = QRect(offset, 0, self.view.viewport().width(), self.height())
        columns = _job_count_rects(rect)
        painter.drawText(QRect(offset + 5, 0, max(0, columns[0].left() - offset - 10), self.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "任务 · 已选/合格")
        for column, title in zip(columns, ("初始3词", "初始4词")):
            painter.drawText(column, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), title)


def _job_seq_width(fm_seq: QFontMetrics, seq: str, digits: int = _JOB_SEQ_DIGITS) -> int:
    """序号列宽：默认留够 `digits` 位数字的宽度，序号本身更长（过百的任务）就按实际宽。

    固定列宽是为了让「1 │ …」和「10 │ …」的竖线位置一致，正文不会随序号位数左右跳。
    """
    return max(fm_seq.horizontalAdvance("0" * digits), fm_seq.horizontalAdvance(seq))


class _JobItemDelegate(QStyledItemDelegate):
    """任务链条目：序号和正文分开画。

    序号占固定 2 位宽（右对齐，1 和 10 的竖线对齐）+ 一道浅灰竖线，正文用普通文字色。
    正文按可视宽度收省略号，完整内容在悬停提示里（栏宽 340–420px 也放不下两个套装 + 长主词条）。
    """

    def paint(self, painter, option, index) -> None:  # noqa: ANN001
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, widget)

        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        seq, _, body = text.partition(_JOB_ITEM_SEP)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        base = QFont(option.font)
        seq_font = QFont(base)
        seq_font.setWeight(QFont.Weight.Medium)
        fm = QFontMetrics(base)
        fm_seq = QFontMetrics(seq_font)

        seq = seq.strip()
        left = option.rect.left() + 5
        width = max(0, option.rect.width() - 10)
        seq_w = _job_seq_width(fm_seq, seq)
        sep_gap = fm.horizontalAdvance(_JOB_ITEM_SEP) + 10
        columns = _job_count_rects(option.rect)
        body_w = max(0, columns[0].left() - 10 - left - seq_w - sep_gap)

        painter.save()
        painter.setFont(seq_font)
        painter.setPen(QColor(_JOB_SEQ_SELECTED_COLOR if selected else _JOB_SEQ_COLOR))
        # 序号在固定列宽里右对齐：个位对齐个位，「1」和「10」的竖线落在同一处
        painter.drawText(
            QRect(left, option.rect.top(), seq_w, option.rect.height()),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            seq,
        )
        painter.setFont(base)
        painter.setPen(QColor(_JOB_SEP_COLOR))
        painter.drawText(
            QRect(left + seq_w + 6, option.rect.top(), option.rect.width(), option.rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            _JOB_ITEM_SEP,
        )
        painter.setPen(QColor(_JOB_SEQ_COLOR if selected else _JOB_BODY_COLOR))
        painter.drawText(
            QRect(left + seq_w + sep_gap, option.rect.top(), body_w, option.rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            fm.elidedText(body.strip(), Qt.TextElideMode.ElideRight, body_w),
        )
        count_font = QFont(base)
        count_font.setPointSize(_JOB_COUNT_FONT_PT)
        painter.setFont(count_font)
        counts = index.data(_JOB_COUNTS_ROLE)
        state = str(index.data(_JOB_COUNTS_STATE_ROLE) or "计算中")
        for column, origin in zip(columns, ("three", "four")):
            pair = counts.get(origin) if counts is not None else None
            label = f"{pair[0]}/{pair[1]}" if pair is not None else state
            painter.setPen(QColor("#c42b1c" if pair is not None and pair[0] == 0 and pair[1] > 0
                                 else _JOB_SEQ_COLOR if selected else _JOB_BODY_COLOR))
            painter.drawText(column, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), label)
        painter.restore()


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


def read_layout() -> dict:
    """读界面布局记忆。没记过、读坏、值不对，一律当空处理。"""
    import json

    from zzz_ddtool.paths import layout_file

    try:
        raw = json.loads(layout_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_layout(data: dict) -> None:
    """写界面布局记忆。写失败不影响使用——顶多下次回到默认布局。"""
    import json

    from zzz_ddtool.paths import layout_file

    target = layout_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def default_config_path() -> Path:
    """打开公共存档目录中的上次配置，默认档也是普通可编辑存档。"""
    from zzz_ddtool.examples import seed_examples
    from zzz_ddtool.paths import plans_dir
    from zzz_ddtool.userdata import migrate_plans

    migrate_plans()
    seed_examples()
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
        self.setFixedWidth(_CARD_WIDTH)
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
    propagate_require = Signal()
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
        self.required_substats: tuple[str, ...] = ()
        # 当前号位主词条屏蔽掉的副词条名（gamedata.blocked_substats）。apply_filter 里
        # 先把与主词条同名的必带剔掉——它在盘子副词条里永远不会出现，不该再参与筛卡。
        self.blocked: set[str] = set()
        self._page = 0

        self.require = NoWheelDoubleSpinBox()
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
        # 栏窄时让计数先被压，别把整条撑宽、给卡片区带出横向滚动条。
        # 最小宽写 1 而不是 0：0 表示「沿用文字宽」，等于没放开。
        self.count_label.setMinimumWidth(1)
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        level = QLabel(f"+{checkpoint}")
        level.setObjectName("stageLevel")
        require_label = QLabel("本档要求")
        require_label.setObjectName("hint")

        self.propagate_btn = QPushButton("递增下发")
        self.propagate_btn.setToolTip(
            f"以本档（+{checkpoint}）要求为 x，把更高档的本档要求依次设为 x+1、x+2、……"
            f"（每高 3 档 +1）。"
        )
        self.propagate_btn.clicked.connect(self.propagate_require.emit)

        bar = QFrame()
        bar.setObjectName("stageBar")
        header = QHBoxLayout(bar)
        header.setContentsMargins(10, 4, 10, 4)
        header.addWidget(level)
        header.addWidget(require_label)
        header.addWidget(self.require)
        header.addWidget(self.propagate_btn)
        header.addStretch()
        header.addWidget(self.count_label)

        self.flow_host = QWidget()
        self.flow = FlowLayout(self.flow_host, spacing=_CARD_SPACING)
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
        required = effective_required_substats(self.required_substats, self.blocked)
        self._visible_indices = [
            i for i, pattern in enumerate(self.patterns)
            if pattern.score + 1e-9 >= need and has_required_substats(pattern, required)
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

    def resync_checks(self, is_checked) -> None:  # noqa: ANN001
        """按策略重铺全部勾选态：不重建卡片、不重置页码。

        用在「名单变了、卡片本身没变」的场合——比如「全任务卡片全选」批量写入后，
        当前任务的 accepted 换了，权重和门槛没动，卡片不用重算，只把勾选反映出来。
        """
        self._checked = {pid for pid in self._ids if is_checked(pid)}
        for card in self.cards:
            card.set_checked_silent(card.pattern.pattern_id in self._checked)
        self.selection_changed.emit()

    def _on_require(self, _value: float) -> None:
        self.apply_filter()
        self.require_changed.emit()

class WeightEditor(QWidget):
    changed = Signal()
    required_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[QComboBox, NoWheelDoubleSpinBox, QWidget]] = []
        self._last_weights: dict[str, float] = {}
        self._required_by_row: dict[QWidget, QCheckBox] = {}
        self._last_required: tuple[str, ...] = ()
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
        # 先量一行词条的最小宽再拆掉：所在栏关了横向滚动条，栏宽得按它兜底，
        # 否则还没加载方案时算出的栏宽偏窄，之后「必带」会被截掉。
        self._add_row(KNOWN_SUBSTATS[0], 0.0)
        probe = self._rows[0][2]
        probe.ensurePolished()
        self._row_min_width = probe.minimumSizeHint().width()
        self._remove_row(probe)

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(max(hint.width(), self._row_min_width), hint.height())

    def set_weights(self, weights: dict[str, float], required_substats: tuple[str, ...] = ()) -> None:
        previous_block = self.blockSignals(True)
        while self._rows:
            self._remove_row(self._rows[0][2])
        for name, value in weights.items():
            if name not in SUBSTAT_NAMES:
                continue
            self._add_row(name, value, required=name in required_substats)
        self._last_weights = self.weights()
        self._last_required = self.required_substats()
        self.blockSignals(previous_block)

    def _notify_changed(self) -> None:
        current = self.weights()
        required = self.required_substats()
        weights_changed = current != self._last_weights
        required_changed = required != self._last_required
        self._last_weights, self._last_required = current, required
        if weights_changed:
            self.changed.emit()
        elif required_changed:
            self.required_changed.emit()

    def required_substats(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            combo.currentText().strip() for combo, _spin, row in self._rows
            if self._required_by_row[row].isChecked() and combo.currentText().strip() in SUBSTAT_NAMES
        ))

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

    def _add_row(self, name: str, value: float, *, required: bool = False) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(self._choices())
        combo.setCurrentText(name)
        spin = NoWheelDoubleSpinBox()
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
        required_box = QCheckBox("必带")
        required_box.setToolTip(
            "卡片必须同时包含全部勾选词条，并满足本档分数要求；保存时仅保留符合条件的已勾选卡片。"
            "“未出”位置可顶一个必带。与所主词条同名的必带无效（该名字永不作为副词条出现）。"
        )
        required_box.setChecked(required)
        required_box.toggled.connect(lambda *_: self._notify_changed())
        self._required_by_row[row] = required_box
        # 词条名同理：这是个可编辑下拉框，正在输入名字时不必每敲一个字就重算。
        # 从下拉列表里选中（activated）和输入完按回车/失焦（editingFinished）都要认。
        combo.activated.connect(lambda *_: self._notify_changed())
        combo.lineEdit().editingFinished.connect(self._notify_changed)
        spin.valueChanged.connect(lambda *_: self._notify_changed())
        combo.setFixedWidth(150)
        layout.addWidget(combo)
        layout.addWidget(spin)
        layout.addWidget(remove)
        layout.addWidget(required_box)
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
        self._required_by_row.pop(row, None)
        self._rows_box.removeWidget(row)
        row.setParent(None)
        row.deleteLater()


class TrialPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.level = NoWheelSpinBox()
        self.level.setRange(0, 15)
        self.locked = QCheckBox("锁定")
        self.equipped = QCheckBox("已装备")
        self.discarded = QCheckBox("已有弃置标记")
        self.rows: list[tuple[QComboBox, NoWheelSpinBox]] = []

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
            rolls = NoWheelSpinBox()
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


# 「全任务卡片全选」的提示语：原来挂在这颗按钮的悬停提示上，现在改成二次确认
# 弹窗的正文——这个动作会覆盖全部任务现有的勾选，值得挡一下。
SELECT_ALL_JOBS_HINT = (
    "对任务链里的每个任务：按它自己的权重生成组合，"
    "再把符合它自己本档要求的卡片全部勾上。\n"
    "会覆盖这些任务现有的勾选；之后新建的任务不受影响。"
)


class ConfirmSelectAllDialog(QDialog):
    """「全任务卡片全选」的二次确认：把原本的悬停提示写进弹窗正文。"""

    def __init__(self, job_count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("全任务卡片全选")
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        lead = QLabel(f"将对任务链里现有的全部 {job_count} 个任务执行：")
        lead.setWordWrap(True)
        layout.addWidget(lead)

        hint = QLabel(SELECT_ALL_JOBS_HINT)
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("全选")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ExampleFetchDialog(QDialog):
    """列出远端示例（文件名 / 标题 / 时间 / 说明 / 本地状态），勾选后下载。

    只负责"让用户挑"；落盘交给 `examples.save_example` —— 那个从不覆盖已有文件。
    """

    def __init__(self, items: list, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("获取公共示例")
        self.setMinimumWidth(620)
        self._items = list(items)

        layout = QVBoxLayout(self)
        intro = QLabel("勾选要下载的示例。本地已有同名且内容不同的，会并列保存一份，不覆盖你现有的。")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.list = QListWidget(self)
        for index, item in enumerate(self._items):
            entry = QListWidgetItem(self._row_text(item), self.list)
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            entry.setCheckState(Qt.CheckState.Checked)
            entry.setData(Qt.ItemDataRole.UserRole, index)
        layout.addWidget(self.list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("下载")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _local_state(self, item) -> str:
        from zzz_ddtool.paths import plans_dir

        target = plans_dir() / item.file
        try:
            if not target.exists():
                return "不存在"
            return "已是最新" if target.read_bytes() == item.content else "将并列保存"
        except OSError:
            return "未知"

    def _row_text(self, item) -> str:
        note = item.note or "-"
        state = self._local_state(item)
        return f"{item.file}  ·  {item.title}  ·  {item.updated_at}  ·  {note}  ·  {state}"

    def row_text(self, index: int) -> list[str]:
        """某一行的字段，供测试与无障碍阅读。"""
        item = self._items[index]
        return [item.file, item.title, item.updated_at, item.note, self._local_state(item)]

    def selected_items(self) -> list:
        picked = []
        for row in range(self.list.count()):
            entry = self.list.item(row)
            if entry.checkState() == Qt.CheckState.Checked:
                picked.append(self._items[entry.data(Qt.ItemDataRole.UserRole)])
        return picked


def qualifying_pattern_ids(
    slot: int,
    mainstats: tuple[str, ...],
    weights: Mapping[str, float],
    stage_require: Mapping[str, Mapping[int, float]] | None = None,
    catalog: PatternCatalog | None = None,
    *, required_substats: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """一个任务「生成组合 + 全选当前可见」等价的可勾选图案全集。

    口径与界面完全一致，只是按这个任务**自己的**配置跑一遍：
      1. 部位/主词条屏蔽掉的副词条不参与（同 _effective_weights）；
      2. 用剩下的权重穷举（等价于点「生成组合」）；
      3. 再按每档要求过滤 score >= 本档要求（等价于「符合门槛」）。
      4. 卡片必须同时包含 required_substats 中的全部词条。

    没有正权重词条时返回空集——那种任务界面本来就不出卡。
    """
    blocked = blocked_substats(slot, mainstats)
    effective = {name: value for name, value in weights.items() if name not in blocked}
    if not useful_ordered(effective):
        return ()
    # 与主词条同名的必带整条剔除：该名字在盘子副词条里永远不会出现，留着就是死条件。
    required = effective_required_substats(required_substats, blocked)
    if catalog is not None:
        grouped = catalog.get(effective, lambda: False)
        return tuple(pattern.pattern_id for (origin, level), patterns in grouped.items()
                     for pattern in patterns
                     if has_required_substats(pattern, required)
                     if possible_substats(slot, mainstats, (item.name for item in pattern.slots))
                     if pattern.score + 1e-9 >= float(((stage_require or {}).get(origin) or {}).get(level, 0)))
    return tuple(
        pattern.pattern_id
        for pattern in enumerate_filtered(effective, stage_require=stage_require)
        if has_required_substats(pattern, required)
        if possible_substats(slot, mainstats, (item.name for item in pattern.slots))
    )


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("驱动盘强化辅助")
        self.resize(*_DEFAULT_WINDOW_SIZE)
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
        # 布局记忆：拖动窗口/分隔条后延迟落盘，避免频繁写文件。
        self._layout_ready = False
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._persist_layout)
        self._closing = False
        self._exit_after_close: int | None = None
        self._utility_thread = None
        self._pattern_catalog = PatternCatalog()
        self._counts_thread = None
        self._job_counts: dict[str, tuple[tuple, dict]] = {}
        self._counts_timer = QTimer(self)
        self._counts_timer.setSingleShot(True)
        self._counts_timer.timeout.connect(self._start_task_counts)
        self.sections: dict[tuple[str, int], StageSection] = {}
        self._active_slot = 4
        self._slot_plans: dict[int, dict] = {slot: empty_slot_plan(slot) for slot in SLOTS}
        self._jobs: list[TaskJob] = []
        self._editing_job_id: str | None = None
        # 「应用于所有任务」那几个按钮：任务链为空时没有可铺的对象，统一禁用。
        self._apply_buttons: list[QPushButton] = []
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
        # 任务链给两个统计列留宽；各栏不允许拖到 0。
        splitter.setChildrenCollapsible(False)
        splitter.setSizes(_default_column_sizes(_DEFAULT_WINDOW_SIZE[0]))
        # 拖完分隔条延迟记一次，下次启动照旧。
        splitter.splitterMoved.connect(lambda *_: self._queue_layout_save())

        # 红框：三栏 + 操作栏，算一整块。拖红框下边那条线就只改它的高度。
        upper = QWidget()
        upper.setObjectName("upperPane")
        upper_layout = QVBoxLayout(upper)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.setSpacing(10)
        upper_layout.addWidget(splitter, 1)
        upper_layout.addWidget(self._build_action_bar(), 0)

        # 底部操作栏里的 _refresh_run_status() 会读它，得先有。
        self._log_tail = None
        # 上次显示的日志文件：换轮次时据此重置读取缓存和路径提示。
        self._log_path_shown = None
        log_box = QFrame()
        log_box.setObjectName("logPanel")
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(10, 8, 10, 10)
        log_layout.setSpacing(6)
        # 标题行：运行日志 [▾]  日志文件：…。收起来时只留这一行，日志框整个让位。
        log_head = QHBoxLayout()
        log_head.setContentsMargins(0, 0, 0, 0)
        log_head.setSpacing(4)
        self.log_title = QLabel("运行日志")
        self.log_title.setObjectName("panelTitle")
        self.log_toggle = QToolButton()
        self.log_toggle.setObjectName("logToggle")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setChecked(True)
        self.log_toggle.setText("▼")
        self.log_toggle.setFixedWidth(22)
        self.log_toggle.setToolTip("收起运行日志")
        self.log_toggle.toggled.connect(self._toggle_run_log)
        self.log_path = QLabel()
        self.log_path.setObjectName("hint")
        self.log_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._refresh_log_path_label()
        log_head.addWidget(self.log_title)
        log_head.addWidget(self.log_toggle)
        log_head.addSpacing(28)
        log_head.addWidget(self.log_path)
        log_head.addStretch(1)
        # 标题行单独装一层，纵向不参与拉伸。否则收起日志后这块空出来的高度会顶到标签上，
        # 标签被拉高、文字垂直居中，整行看着就像"飘"在空框正中间。
        log_head_host = QWidget()
        log_head_host.setLayout(log_head)
        log_head_host.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        # 顶对齐：日志框比标题行高时，多出来的高度留在下面，别上下平分把标题挤到正中间。
        log_layout.addWidget(log_head_host, 0, Qt.AlignmentFlag.AlignTop)
        self.run_log = QPlainTextEdit()
        self.run_log.setReadOnly(True)
        self.run_log.setMaximumBlockCount(300)
        self.run_log.setPlaceholderText("开始任务后，这里实时显示识别、判断、操作及失败原因。")
        # 日志高度不设上下限：拖窗口底边能自由拉高/压低，日志跟着长/短，
        # 不再封顶（封顶会让拖到极限时 Windows 把"拖边框"退化成"移动窗口"）。
        log_layout.addWidget(self.run_log)

        # 下面那块：状态行 + 运行日志。自身高度不吃窗口拉伸，只被红框顶着上下移动。
        lower = QWidget()
        lower.setObjectName("lowerPane")
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.setSpacing(6)
        lower_layout.addWidget(self._build_status_bar(), 0)
        lower_layout.addWidget(log_box, 1)
        self._lower_pane = lower
        self.log_panel = log_box

        self._main_splitter = QSplitter(Qt.Orientation.Vertical)
        self._main_splitter.setObjectName("mainSplitter")
        self._main_splitter.addWidget(upper)
        self._main_splitter.addWidget(lower)
        # 窗口底边拉伸只多给下面那块（也就是日志窗）；红框自己不长高。
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.setHandleWidth(6)
        self._main_splitter.setSizes([500, 270])
        self._lower_height = 270
        # 收起日志前下半块的高度：展开时按它还回来。None = 还没收起过。
        self._log_lower_height = None
        self._dragging_main_splitter = False
        # 红框下边那条分隔条由本窗口自己接管（见 eventFilter / _drag_red_line）。
        self._red_handle = self._main_splitter.handle(1)
        self._red_handle.installEventFilter(self)
        self._red_drag_global_y: float | None = None
        self._red_drag_upper = 0
        self._red_drag_window_h = 0
        outer.addWidget(self._main_splitter, 1)
        self._refresh_run_status()
        self._load_path(self._path)
        for box in (self.mark_discard, self.mark_lock, self.skip_locked, self.skip_equipped,
                    self.skip_discarded):
            box.toggled.connect(self._mark_unsaved)
        self.max_discs.valueChanged.connect(self._mark_unsaved)
        # 恢复上次的窗口大小/位置、三栏比例。放在最后：控件都建好了才动。
        self._restore_layout()
        self._layout_ready = True

    @staticmethod
    def _apply_sizes(splitter: QSplitter, saved: object) -> bool:
        """按记录恢复分隔条比例。条数对不上或值非法就忽略，保持默认布局。返回是否套用了。"""
        if not isinstance(saved, list) or len(saved) != len(splitter.sizes()):
            return False
        try:
            splitter.setSizes([max(1, int(value)) for value in saved])
        except (TypeError, ValueError):
            return False
        return True

    def _fit_to_screen(self, restored: bool) -> None:
        """窗口收进所在屏幕的可用区（任务栏以外），放不下就收窄收矮、挪回屏内。

        出厂的 1320×820 在 150% 缩放的笔记本（逻辑约 1280×680）上就放不下；布局记录
        也可能是在另一台更大的屏幕上存的。没有记录时顺手居中。
        """
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        # resize 的尺寸不含标题栏和边框，按常见值预留。
        frame_w, frame_h = 16, 40
        width = max(1, min(self.width(), area.width() - frame_w))
        height = max(1, min(self.height(), area.height() - frame_h))
        if (width, height) != (self.width(), self.height()):
            self.resize(width, height)
        if restored:
            x = min(max(self.x(), area.left()), area.right() + 1 - width - frame_w)
            y = min(max(self.y(), area.top()), area.bottom() + 1 - height - frame_h)
        else:
            x = area.left() + (area.width() - width - frame_w) // 2
            y = area.top() + (area.height() - height - frame_h) // 2
        self.move(max(area.left(), x), max(area.top(), y))

    def _balance_columns(self) -> None:
        """卡片栏放不下两张卡时，先从任务链、再从配置栏借宽，各自不低于最小宽。"""
        splitter = self._config_splitter
        sizes = splitter.sizes()
        if len(sizes) != 3 or sum(sizes) <= 0:
            return
        lack = _cards_column_width(_COMFORT_CARDS) - sizes[2]
        if lack <= 0:
            return
        for index in (0, 1):
            spare = sizes[index] - splitter.widget(index).minimumWidth()
            take = max(0, min(spare, lack))
            sizes[index] -= take
            sizes[2] += take
            lack -= take
        splitter.setSizes(sizes)

    def _restore_layout(self) -> None:
        """恢复上次退出时的界面布局。没记录过就保持默认。"""
        data = read_layout()
        geometry = data.get("geometry")
        restored = False
        if isinstance(geometry, str) and geometry:
            restored = bool(self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii"))))
        self._fit_to_screen(restored)
        # 两条比例都延到窗口真正摆好之后再套。构造函数里窗口还没显示，分隔条宽度
        # 还是出厂值，这会儿 setSizes 会被按错误的宽度归一化，比例就歪了
        # （窗口被屏幕夹窄时尤其明显：中间栏本该最宽，结果比右栏还窄）。
        saved = (data.get("config_splitter"), data.get("main_splitter"))
        QTimer.singleShot(0, lambda: self._apply_saved_sizes(saved))
        # 比例落定后再记一次下面那块的高度——拖红框时照它锁。
        QTimer.singleShot(0, self._remember_lower_height)

    def _apply_saved_sizes(self, saved: tuple[object, object]) -> None:
        """套用记录里的三栏 / 上下块比例；没有记录就按当前窗口宽度折算默认三栏宽。"""
        if not self._apply_sizes(self._config_splitter, saved[0]):
            self._config_splitter.setSizes(_default_column_sizes(sum(self._config_splitter.sizes())))
        self._balance_columns()
        self._apply_sizes(self._main_splitter, saved[1])

    def _persist_layout(self) -> None:
        """把当前布局写进 user/layout.json。写失败不影响使用。"""
        if not self._layout_ready:
            return
        data = read_layout()
        data["geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
        data["config_splitter"] = [int(value) for value in self._config_splitter.sizes()]
        data["main_splitter"] = [int(value) for value in self._main_splitter.sizes()]
        write_layout(data)

    def _queue_layout_save(self) -> None:
        """拖动中不写盘，停手后再落一次。"""
        if self._layout_ready and self.isVisible():
            self._layout_timer.start(800)

    def _remember_lower_height(self, lower_height: int | None = None) -> None:
        """记下红线以下那块的高度，拖红框时照它锁住。

        `lower_height` 只在刚改过分隔条、几何还没落定时显式传入（`sizes()` 有延迟，
        读早了会拿到旧值，拖红框时按错基准算出错误的窗口高度）。
        """
        if lower_height is None:
            lower_height = self._lower_pane.height()
        self._lower_height = lower_height

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001
        """接管红框下边那条分隔条：自己按鼠标位移拖，不走 QSplitter 的夹取。

        QSplitter 会把分隔条夹在「下半块最小高度」处——下半块（状态行 + 日志）一旦
        触到自己的最小高度，分隔条就再也拖不动，红框（三栏 + 操作栏）也就长不高了。
        这里直接读鼠标位移，红框能一直往上长，窗口底边跟着往下走。
        """
        if obj is self._red_handle:
            kind = event.type()
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._red_drag_global_y = event.globalPosition().y()
                self._red_drag_upper = self._main_splitter.widget(0).height()
                self._red_drag_window_h = self.height()
                return True
            if kind == QEvent.Type.MouseMove and self._red_drag_global_y is not None:
                self._drag_red_line(int(event.globalPosition().y() - self._red_drag_global_y))
                return True
            if kind == QEvent.Type.MouseButtonRelease and self._red_drag_global_y is not None:
                self._red_drag_global_y = None
                # 贴底后的拖动会挤下半块，松手时记下新高度，下次拖以它为准。
                QTimer.singleShot(0, self._remember_lower_height)
                self._queue_layout_save()
                return True
        return super().eventFilter(obj, event)

    def _red_drag_room(self) -> int | None:
        """拖红框时窗口还能往下长多少（相对拖动开始时）。None = 不设限。

        最大化 / 全屏时窗口长不了；否则以所在屏幕可用区的底边为界。
        """
        if self.isMaximized() or self.isFullScreen():
            return 0
        screen = self.screen()
        if screen is None:
            return None
        start_bottom = self.frameGeometry().bottom() - (self.height() - self._red_drag_window_h)
        return max(0, screen.availableGeometry().bottom() - start_bottom)

    def _drag_red_line(self, dy: int) -> None:
        """把红框按位移 `dy` 抬高（正）/压低（负）。

        下半块高度锁死不动，所以红框长高多少，窗口就跟着长高多少——它只是被顶下去。
        窗口最底下那条边才改下半块（也就是日志窗）。
        窗口长到屏幕底边（或已最大化）后再往下拖，就改从下半块里挤，挤到它的最小高度为止——
        否则矮屏上窗口一贴底，红框就再也拉不高，卡片区只剩一行。
        """
        upper = self._main_splitter.widget(0)
        wanted = max(max(0, upper.minimumSizeHint().height()), self._red_drag_upper + dy)
        delta = wanted - self._red_drag_upper
        lower = max(0, self._lower_height)
        grow = delta
        fixed = self.isMaximized() or self.isFullScreen()
        room = self._red_drag_room()
        if room is not None and delta > room:
            floor = max(0, self._lower_pane.minimumSizeHint().height())
            squeeze = min(delta - room, max(0, lower - floor))
            lower -= squeeze
            grow = room
            wanted = self._red_drag_upper + grow + squeeze
        elif fixed:
            # 窗口高度动不了：红框让出来的高度还给下半块。
            lower -= delta
            grow = 0
        self._dragging_main_splitter = True
        try:
            # 先长窗口，再摆比例：窗口不够高时 setSizes 会被夹，红框就顶不上去。
            if not fixed:
                self.resize(self.width(), max(1, self._red_drag_window_h + grow))
            self._main_splitter.setSizes([wanted, lower])
        finally:
            self._dragging_main_splitter = False

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        # 套装摘要按当前可用宽度重算省略号（宽度变了但文本没变也要刷新）。
        if getattr(self, "set_summary", None) is not None:
            self._update_set_summary()
        # 变窄时 QSplitter 先压有拉伸因子的卡片栏，这里再把宽度从左两栏借回来。
        if self._layout_ready and event.size().width() < event.oldSize().width():
            QTimer.singleShot(0, self._balance_columns)
        # 拖红框时窗口高度是我在补，不算用户改窗口大小，别在这记。
        if self._layout_ready and not self._dragging_main_splitter:
            QTimer.singleShot(0, self._remember_lower_height)
        self._queue_layout_save()

    def moveEvent(self, event) -> None:  # noqa: ANN001
        super().moveEvent(event)
        self._queue_layout_save()

    def _build_header(self) -> QVBoxLayout:
        """页头两行：上＝程序名 + 版本号 + 操作按钮（按钮靠右），下＝文件名 + 保存状态。

        两行各自占满整幅宽度，不是「左文字块 + 右按钮块」的两栏结构：两栏结构下
        第二行的右端只能到按钮左边缘，顶不到窗口右边（2026-10-14 改成上下两行）。
        """
        title = QLabel("驱动盘强化辅助")
        title.setObjectName("bannerTitle")
        # 版本号跟在程序名右侧：打包态读 exe 同级的 version.txt，开发态仓库根没有这个
        # 文件，会回退成 version.FALLBACK_VERSION（要和 pyproject 的 version 对齐）。
        self.version_label = QLabel(f"v{current_version()}")
        self.version_label.setObjectName("appVersion")
        self.version_label.setToolTip("当前程序版本（读程序目录下的 version.txt）")
        # 文件名标签跟当前存档走：悬停看全路径。它排在保存状态左边，两者一起靠右。
        self.path_label = QLabel()
        self.path_label.setObjectName("hint")
        # 保存状态常驻标签：文件名行最右的一个。
        # 宽度跟着文本走（「未保存」短、保存后带时间戳长），换来文件名和状态之间
        # 恒定的一小段间隔；不再用固定宽度顶住，状态文本变长时文件名跟着左移。
        self.save_label = QLabel()
        self.save_label.setObjectName("saveStateIdle")
        self.save_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # 页头只占两行，矮屏上多留给三栏：第一行程序名 + 版本号 + 操作按钮，
        # 第二行文件名 + 保存状态。第二行原来是「左边任务链…不必凑齐 6 个」那句操作提示
        # （2026-10-14 去掉），现在整行靠右排成「文件名 · 保存状态」，左边留白。
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(14)
        # 程序名和版本号按基线对齐：按钮比标题高，整行高度由按钮顶起来，
        # 只写 AlignBottom 的话版本号会掉到标题基线下面去。
        title_row.addWidget(title, 0, Qt.AlignmentFlag.AlignBaseline)
        title_row.addWidget(self.version_label, 0, Qt.AlignmentFlag.AlignBaseline)
        title_row.addStretch(1)
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(12)
        path_row.addStretch(1)
        path_row.addWidget(self.path_label)
        path_row.addWidget(self.save_label)
        self._reset_saved_state()

        open_btn = QPushButton("打开")
        save_btn = QPushButton("保存")
        save_as_btn = QPushButton("另存为")
        open_btn.clicked.connect(self.open_preset)
        save_btn.clicked.connect(self.save_current)
        save_as_btn.clicked.connect(self.save_as)
        settings_btn = QPushButton("日志设置")
        settings_btn.clicked.connect(self.open_settings)
        check_btn = QPushButton("检查更新")
        check_btn.clicked.connect(self.check_update)
        examples_btn = QPushButton("获取公共示例")
        examples_btn.clicked.connect(self.fetch_public_examples)
        self._task_controls = [
            open_btn,
            save_btn,
            save_as_btn,
            check_btn,
            examples_btn,
        ]

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(settings_btn)
        actions.addWidget(check_btn)
        actions.addWidget(examples_btn)
        actions.addWidget(open_btn)
        actions.addWidget(save_btn)
        actions.addWidget(save_as_btn)

        # 按钮挂在标题行右端，不再单独占一栏；第二行因此能顶到窗口右边。
        title_row.addLayout(actions)

        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        # 两行的间距取 10，和页头到下面三栏的间距（__init__ 里 outer.setSpacing(10)）相同：
        # 文件名/保存状态那一行上下空隙相等，不贴着按钮行的底边。
        header.setSpacing(10)
        header.addLayout(title_row)
        header.addLayout(path_row)
        return header

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
        self.weight_editor.required_changed.connect(self._on_required_changed)
        weights_layout.addWidget(self.weight_editor)
        self.apply_weights_btn = apply_weights = QPushButton("应用于所有任务")
        apply_weights.setToolTip("把当前单价和必带勾选铺到任务链里已存在的每个任务；之后新建的任务不受影响。")
        apply_weights.clicked.connect(self._apply_weights_to_all)
        weights_layout.addWidget(apply_weights, 0, Qt.AlignmentFlag.AlignLeft)
        self._apply_buttons.append(apply_weights)
        layout.addWidget(weights_box)

        flags_box = QGroupBox("硬过滤（任务链共用）")
        flags = QVBoxLayout(flags_box)
        self.mark_discard = QCheckBox("判断后打上弃置标记")
        self.mark_lock = QCheckBox("判断后打上锁定标记")
        self.skip_locked = QCheckBox("跳过有锁定标记")
        self.skip_equipped = QCheckBox("跳过已装备（无法跳过）")
        self.skip_discarded = QCheckBox("跳过有弃置标记")
        for box in (self.mark_discard, self.mark_lock):
            # 判断后打不打标记：默认都打；取消勾选只停发 R/T，判定与计数不变。
            box.setChecked(True)
            flags.addWidget(box)
        for box in (self.skip_equipped, self.skip_locked, self.skip_discarded):
            # 弃置标记默认不勾，其余两项默认勾。
            box.setChecked(box is not self.skip_discarded)
            flags.addWidget(box)
        layout.addWidget(flags_box)
        task_box = QGroupBox("当前任务限制")
        task_limits = QVBoxLayout(task_box)
        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("本次最多处理"))
        self.max_discs = NoWheelSpinBox()
        self.max_discs.setRange(1, 5000)
        self.max_discs.setValue(100)
        limit_row.addWidget(self.max_discs)
        limit_row.addWidget(QLabel("个盘子"))
        limit_row.addStretch()
        task_limits.addLayout(limit_row)
        self.apply_limit_btn = apply_limit = QPushButton("应用于所有任务")
        apply_limit.setToolTip("把当前额度铺到任务链里已存在的每个任务；之后新建的任务不受影响。")
        apply_limit.clicked.connect(self._apply_limit_to_all)
        task_limits.addWidget(apply_limit, 0, Qt.AlignmentFlag.AlignLeft)
        self._apply_buttons.append(apply_limit)
        layout.addWidget(task_box)

        self._build_game_filter(layout)
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 横向滚动条关着，栏宽小于内容最小宽就会直接截掉右边（「必带」「添加词条」）。
        content = panel.minimumSizeHint().width() + scroll.verticalScrollBar().sizeHint().width()
        scroll.setMinimumWidth(max(_CONFIG_MIN_WIDTH, content))
        scroll.setMaximumWidth(400)
        return scroll

    @staticmethod
    def _row_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("rowLabel")
        return label

    def _build_game_filter(self, layout: QVBoxLayout) -> None:
        """游戏内筛选：分两部分，都直接摆在面板上。

        第一部分是套装，换套装仍然开弹窗挑；第二部分是部位与主词条——
        1–6 号位不再进弹窗，4/5/6 有主词条可选，1/2/3 主词条固定，只显示提示。
        """
        game_box = QGroupBox("游戏内筛选（进仓库先重置再套用）")
        game = QVBoxLayout(game_box)
        game.setSpacing(4)

        self._filter_sets: list[str] = []
        set_row = QHBoxLayout()
        set_row.setSpacing(4)
        set_row.addWidget(self._row_label("套装"))
        self.set_summary = QLabel("未选")
        self.set_summary.setWordWrap(False)
        self.set_summary.setObjectName("hint")
        # 套装名可以很长：必须允许这个标签被压到任意窄，否则它的 sizeHint
        # 会把同一行的“选择套装”按钮顶出面板（右侧整块看不见）。
        self.set_summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_summary.setMinimumWidth(0)
        set_row.addWidget(self.set_summary, 1)
        pick_sets = QPushButton("选择套装")
        pick_sets.setToolTip("打开套装列表勾选；一个都不勾＝不限套装。")
        pick_sets.clicked.connect(self._open_set_picker)
        set_row.addWidget(pick_sets)
        game.addLayout(set_row)
        self.apply_sets_btn = apply_sets = QPushButton("应用于所有任务")
        apply_sets.setToolTip("把当前套装铺到任务链里已存在的每个任务；之后新建的任务不受影响。")
        apply_sets.clicked.connect(self._apply_sets_to_all)
        game.addWidget(apply_sets, 0, Qt.AlignmentFlag.AlignLeft)
        self._apply_buttons.append(apply_sets)

        split = QFrame()
        split.setObjectName("filterSplit")
        split.setFixedHeight(1)
        game.addWidget(split)

        slot_row = QHBoxLayout()
        slot_row.setSpacing(4)
        slot_row.addWidget(self._row_label("部位"))
        self.filter_slots: dict[int, QRadioButton] = {}
        self.slot_group = QButtonGroup(game_box)
        self.slot_group.setExclusive(True)
        for slot in SLOTS:
            radio = QRadioButton(str(slot))
            self.slot_group.addButton(radio, slot)
            self.filter_slots[slot] = radio
            slot_row.addWidget(radio)
        self.filter_slots[4].setChecked(True)
        self.slot_group.idClicked.connect(self._on_slot_clicked)
        slot_row.addStretch()
        game.addLayout(slot_row)

        self._main_boxes_by_slot: dict[int, dict[str, QCheckBox]] = {}
        self.filter_main_boxes: dict[str, QCheckBox] = {}
        self._main_groups: dict[int, QButtonGroup] = {}
        self._main_any_by_slot: dict[int, QCheckBox] = {}
        self.main_host = QStackedWidget()
        self.main_host.setObjectName("mainHost")
        self.main_host.setFixedHeight(_MAINSTAT_PAGE_HEIGHT)
        self._mainstat_pages: dict[int, QWidget] = {}
        for slot, names in _MAINSTAT_PAGES.items():
            boxes: dict[str, QCheckBox] = {}
            page = QWidget()
            page.setFixedHeight(_MAINSTAT_PAGE_HEIGHT)
            grid = QGridLayout(page)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(2)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            group = QButtonGroup(page)
            group.setExclusive(False)
            self._main_groups[slot] = group
            any_main = QCheckBox("不限")
            group.addButton(any_main)
            any_main.setChecked(True)
            any_main.toggled.connect(lambda checked, s=slot: self._on_main_changed(s, None, checked))
            self._main_any_by_slot[slot] = any_main
            grid.addWidget(any_main, 0, 0)
            for idx, name in enumerate(names, start=1):
                box = QCheckBox(name)
                group.addButton(box)
                box.toggled.connect(lambda checked, s=slot, n=name: self._on_main_changed(s, n, checked))
                boxes[name] = box
                grid.addWidget(box, idx // _MAINSTAT_COLS, idx % _MAINSTAT_COLS)
            self._mainstat_pages[slot] = page
            self._main_boxes_by_slot[slot] = boxes
            self.main_host.addWidget(page)
        empty_page = QWidget()
        empty_page.setFixedHeight(_MAINSTAT_PAGE_HEIGHT)
        self.main_host.addWidget(empty_page)

        main_row = QHBoxLayout()
        main_row.setSpacing(4)
        main_row.addWidget(self._row_label("主词条"))
        main_row.addWidget(self.main_host, 1)
        game.addLayout(main_row)

        self.main_hint = QLabel()
        self.main_hint.setObjectName("hint")
        self.main_hint.setWordWrap(True)
        self.main_hint.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.main_hint.setFixedHeight(48)
        game.addWidget(self.main_hint)

        substat_hint = QLabel("副词条：自动")
        substat_hint.setObjectName("hint")
        substat_hint.setToolTip("根据右侧初始 3 词、4 词的 0 级选择自动筛选；筛选数量从最低命中数选到 4。")
        game.addWidget(substat_hint)
        layout.addWidget(game_box)

    def _build_chain(self) -> QWidget:
        panel = QWidget()
        # 条目要放「序号 │ 4号 · 套装 · 主词条」。正文可用宽 = 栏宽 − 序号列 − 竖线两侧 − 内边距，
        # 12pt 下扣掉约 85px；实测「4号 · 双套装 · 暴击伤害」要 261px、「…· 能量自动回复」
        # 要 293px，所以要 380 才两行都不收省略号（340 连常见的双套装行都差 6px）。
        # 两个统计列固定宽，任务名可省略并悬停查看；允许拉宽看长名称。
        # 窄屏上让位给卡片栏，最窄收到 _CHAIN_MIN_WIDTH（再窄任务名就只剩省略号了）。
        panel.setMinimumWidth(_CHAIN_MIN_WIDTH)
        panel.setMaximumWidth(760)
        self.chain_panel = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("任务链")
        inner = QVBoxLayout(box)
        inner.setContentsMargins(6, 9, 6, 9)
        hint = QLabel("编完一个部位再新建。执行按从上到下的顺序。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        # 没有任务名：列表里靠「序号 + 部位 · 套装 · 主词条」区分，不再单独起名。
        self.chain_list = QListWidget()
        self.chain_list.setItemDelegate(_JobItemDelegate(self.chain_list))
        # 条目文本比栏宽长（最长要 279px），收省略号 + 悬停看全文，别出现横向滚动条
        self.chain_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.chain_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chain_list.currentRowChanged.connect(self._on_chain_row)
        new_btn = QPushButton("新建任务")
        new_btn.setObjectName("primary")
        new_btn.clicked.connect(self._add_job)
        copy_btn = QPushButton("复制任务")
        copy_btn.setToolTip("复制当前选中的任务，内容一样，各走各的。")
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
        self.chain_count_header = _JobCountHeader(self.chain_list)
        inner.addWidget(self.chain_count_header)
        inner.addWidget(self.chain_list, 1)
        inner.addLayout(add_row)
        inner.addLayout(row)
        layout.addWidget(box)
        return panel

    @staticmethod
    def _job_item_text(index: int, job: TaskJob) -> str:
        """列表条目文本：序号 │ 内容摘要（部位 · 套装 · 主词条）。

        没有任务名，这条摘要就是任务的唯一标识；改实时写回后摘要会跟着变，
        所以抽出来给两处共用。序号和正文之间那道竖线由 `_JobItemDelegate` 分色画。
        """
        return f"{index + 1} {_JOB_ITEM_SEP} {job.title()}"

    def _apply_job_item(self, item: QListWidgetItem, index: int, job: TaskJob) -> None:
        """写条目文本（栏窄时会收省略号）。"""
        item.setText(self._job_item_text(index, job))
        self._queue_job_counts(item, job)

    def _count_job(self, job: TaskJob) -> TaskJob:
        """Snapshot without parsing a large whitelist on the GUI thread."""
        if job.job_id != self._editing_job_id or self._loading:
            return replace(job, weights=dict(job.weights),
                           stage_require={origin: dict(table) for origin, table in job.stage_require.items()})
        return TaskJob(
            job_id=job.job_id, slot=self._active_slot, sets=tuple(self._filter_sets),
            mainstats=tuple(self._selected_mains()),
            weights=self.weight_editor.weights(), accepted=self._selection_seed,
            required_substats=self.weight_editor.required_substats(),
            stage_require={origin.value: {level: float(self.sections[(origin.value, level)].require.value())
                                          for level in CHECKPOINTS} for origin in Origin},
        )

    def _set_job_counts(self, item: QListWidgetItem, job: TaskJob, counts: dict | None,
                        state: str = "计算中") -> None:
        item.setData(_JOB_COUNTS_ROLE, counts)
        item.setData(_JOB_COUNTS_STATE_ROLE, state)
        detail = "\n".join(f"初始{3 if origin == 'three' else 4}词：{pair[0]}/{pair[1]}"
                           for origin, pair in (counts or {}).items())
        item.setToolTip(f"{job.title()}\n{detail or state}\n已选/合格，汇总六档及全部分页。")
        if state == "未生成":
            item.setToolTip(f"{job.title()}\n未生成：尚未指定卡片名单，开始任务时跳过。\n"
                            "请点击生成组合，或使用全任务卡片全选建立名单。")

    def _queue_job_counts(self, item: QListWidgetItem, job: TaskJob) -> None:
        if self._closing:
            return
        if job.accepted is None:
            self._job_counts.pop(job.job_id, None)
            generating = job.job_id == self._editing_job_id and (
                self._preview_thread is not None or self._preview_timer.isActive())
            self._set_job_counts(item, job, None, "计算中" if generating else "未生成")
            return
        signature = counts_signature(job)
        cached = self._job_counts.get(job.job_id)
        if cached is None or cached[0] != signature:
            cached = next((result for result in self._job_counts.values() if result[0] == signature), None)
        if cached is not None and cached[0] == signature:
            self._job_counts[job.job_id] = cached
            self._set_job_counts(item, job, cached[1])
        elif not useful_ordered(effective_weights(job)):
            counts = {origin.value: (0, 0) for origin in Origin}
            self._job_counts[job.job_id] = (signature, counts)
            self._set_job_counts(item, job, counts)
        else:
            self._set_job_counts(item, job, None)
            if self._counts_thread is not None:
                self._counts_thread.requestInterruption()
            self._counts_timer.start(180)

    def _refresh_current_counts(self) -> None:
        if self._loading or self._closing:
            return
        job = self._editing_job()
        if job is None:
            return
        job = self._count_job(job)
        item = next((self.chain_list.item(i) for i in range(self.chain_list.count())
                     if self.chain_list.item(i).data(Qt.ItemDataRole.UserRole) == job.job_id), None)
        if item is None:
            return
        if job.accepted is None:
            self._queue_job_counts(item, job)
        elif not self._preview_dirty:
            counts = {
                origin.value: (
                    sum(len(section.visible_accepted()) for section in self.sections.values() if section.origin is origin),
                    sum(len(section._visible_indices) for section in self.sections.values() if section.origin is origin),
                ) for origin in Origin
            }
            self._job_counts[job.job_id] = (counts_signature(job), counts)
            self._set_job_counts(item, job, counts)
        else:
            self._queue_job_counts(item, job)

    def _start_task_counts(self) -> None:
        if self._closing:
            return
        if self._counts_thread is not None:
            self._counts_thread.requestInterruption()
            return
        plans = []
        for saved in self._jobs:
            job = self._count_job(saved)
            if job.accepted is None:
                continue
            signature = counts_signature(job)
            cached = self._job_counts.get(job.job_id)
            if cached is None or cached[0] != signature:
                plans.append((signature, job))
        if not plans:
            return
        worker = _TaskCountsThread(plans, self._pattern_catalog, self)
        self._counts_thread = worker
        worker.result.connect(self._on_task_counts)
        worker.failed.connect(self._on_task_counts_failed)
        worker.finished.connect(self._on_task_counts_finished)
        worker.start()

    def _on_task_counts(self, signature: tuple, job_id: str, counts: dict) -> None:
        if self._closing:
            return
        for index, saved in enumerate(self._jobs):
            if saved.job_id != job_id:
                continue
            job = self._count_job(saved)
            if job.accepted is None:
                return
            if counts_signature(job) != signature:
                return  # Configuration changed while this result was being calculated.
            self._job_counts[job_id] = (signature, counts)
            item = self.chain_list.item(index)
            if item is not None:
                self._set_job_counts(item, job, counts)
            return

    def _on_task_counts_failed(self, signature: tuple, job_id: str, message: str) -> None:
        for index, saved in enumerate(self._jobs):
            if saved.job_id == job_id and counts_signature(self._count_job(saved)) == signature:
                item = self.chain_list.item(index)
                self._set_job_counts(item, saved, None, "计算失败")
                item.setToolTip(f"{saved.title()}\n统计失败：{message}；修改配置后重试。")

    def _on_task_counts_finished(self) -> None:
        worker = self._counts_thread
        self._counts_thread = None
        worker.deleteLater()
        if self._closing:
            self.close()
        elif worker.isInterruptionRequested():
            self._counts_timer.start(1)

    def _refresh_chain_list(self, select_id: str | None = None) -> None:
        current_id = select_id or self._editing_job_id
        ids = {job.job_id for job in self._jobs}
        self._job_counts = {job_id: result for job_id, result in self._job_counts.items() if job_id in ids}
        # 选中行必须也在屏蔽信号区间内：否则 setCurrentRow 会触发 currentRowChanged
        # → _on_chain_row → _load_job → rebuild_cards，把 242 张卡片整屏重建一遍
        # （实测 ~250ms），而调用方（加入/更新/删除）本身已经不需要重新载入。
        self.chain_list.blockSignals(True)
        self.chain_list.clear()
        for index, job in enumerate(self._jobs):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, job.job_id)
            self._apply_job_item(item, index, job)
            self.chain_list.addItem(item)
        if current_id:
            for row in range(self.chain_list.count()):
                item = self.chain_list.item(row)
                if item is not None and item.data(Qt.ItemDataRole.UserRole) == current_id:
                    self.chain_list.setCurrentRow(row)
                    break
        self.chain_list.blockSignals(False)
        self._update_apply_buttons()
        self._update_status()

    def _editing_job(self) -> TaskJob | None:
        """当前正在编辑的任务节点。没选中或 id 失效时返回 None。"""
        if not self._editing_job_id:
            return None
        for job in self._jobs:
            if job.job_id == self._editing_job_id:
                return job
        return None

    def _commit_editor(self) -> None:
        """把右侧编辑器当前状态写回。编辑器是唯一真相，只在「要离开它」的时候快照一次：
        切任务节点、切部位、落盘/试判/开始任务。这样就不需要「更新当前任务」按钮了。

        有选中任务时写进那个任务节点，顺带刷新它那行的摘要；
        没选中任务时只更新部位暂存，切回去时还能看到刚才编的内容。
        """
        if self._loading:
            return
        self._slot_plans[self._active_slot] = self._snapshot_slot()
        for index, job in enumerate(self._jobs):
            if job.job_id != self._editing_job_id:
                continue
            fresh = self._job_from_editor(job.job_id)
            self._jobs[index] = fresh
            item = self.chain_list.item(index)
            if item is not None:
                self._apply_job_item(item, index, fresh)
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
            required_substats=tuple(snap.get("required_substats") or ()),
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
            "required_substats": job.required_substats,
            "mainstats": tuple(job.mainstats),
            "min_useful_types": 0,
            "max_discs": job.max_discs if job.max_discs is not None else self._legacy_max_discs,
        }
        self._slot_plans[job.slot] = plan
        self._apply_slot_widgets(plan)
        self._loading = False
        self._selection_seed = job.accepted
        self._reset_preview()

    def _on_chain_row(self, row: int) -> None:
        if self._loading or row < 0 or row >= len(self._jobs):
            return
        # 先把手头这个任务编的内容写回去，再切走——不然刚改的权重会丢。
        self._commit_editor()
        self._load_job(self._jobs[row])

    def _add_job(self) -> None:
        """新建任务：建一个**空**任务并选中它，右侧随即清空等你编。

        右侧是左侧选中任务的镜像，所以新建之后右侧必须是空的——不能把上一个
        任务的内容顺过来当新任务，那是假信息。之后在右侧编，离开时自动写回它。
        """
        self._commit_editor()
        job = TaskJob(slot=self._active_slot, max_discs=100)
        self._jobs.append(job)
        self._mark_unsaved()
        self._editing_job_id = job.job_id
        self._refresh_chain_list(job.job_id)
        self._clear_editor()

    def _copy_job(self) -> None:
        """复制当前选中的任务：内容一模一样，新 id。

        新任务插在原任务下面，并选中它。没有名字可编号——两条一样的任务在列表里
        都显示各自的部位 · 套装 · 主词条，改完内容自然就分开了。
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
        # 试判页的表单不矮，不包一层滚动区，它的最小高度会把整排三栏一起撑高。
        trial_scroll = QScrollArea()
        trial_scroll.setWidgetResizable(True)
        trial_scroll.setWidget(self.trial)
        trial_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.tabs.addTab(self.three_host["scroll"], "初始 3 词")
        self.tabs.addTab(self.four_host["scroll"], "初始 4 词")
        self.tabs.addTab(trial_scroll, "试判")
        # 「拷贝另一侧的本档要求」放进页签栏右侧，不在卡片区顶上单占一行。
        self.copy_require_btn = QPushButton()
        self.copy_require_btn.clicked.connect(self._copy_require_for_current_tab)
        self.tabs.setCornerWidget(self.copy_require_btn, Qt.Corner.TopRightCorner)
        self.tabs.currentChanged.connect(lambda _index: self._update_status())
        self.tabs.currentChanged.connect(lambda _index: self._sync_copy_require_btn())
        self._sync_copy_require_btn()
        return self.tabs

    def _tab_origin(self) -> Origin | None:
        return {0: Origin.THREE, 1: Origin.FOUR}.get(self.tabs.currentIndex())

    def _sync_copy_require_btn(self) -> None:
        origin = self._tab_origin()
        self.copy_require_btn.setVisible(origin is not None)
        if origin is None:
            return
        other = Origin.FOUR if origin is Origin.THREE else Origin.THREE
        self.copy_require_btn.setText(f"拷贝{_ORIGIN_CN[other]}的本档要求")
        self.copy_require_btn.setToolTip(
            f"把「初始 {_ORIGIN_CN[other]}」每档的本档要求（卡片权重）照搬到当前页"
            f"（初始 {_ORIGIN_CN[origin]}）。"
        )

    def _copy_require_for_current_tab(self) -> None:
        origin = self._tab_origin()
        if origin is not None:
            self._copy_other_require(origin)

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
        else:
            text = "、".join(self._filter_sets)
        fm = QFontMetrics(self.set_summary.font())
        # 用标签当前的真实宽度收省略号（未布局时 width 为 0，兜底 180）。
        width = self.set_summary.width() or 180
        self.set_summary.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, width))
        self.set_summary.setToolTip(text)

    def _open_set_picker(self) -> None:
        extra = [name for name in self._filter_sets if name not in SETS]
        dialog = SetPickerDialog(self._filter_sets, extra=extra, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._filter_sets = dialog.selected()
        self._mark_unsaved()
        self._update_set_summary()
        self._refresh_current_item_text()

    def _refresh_current_item_text(self) -> None:
        """把当前任务那行的标签刷成编辑器里的最新内容。

        任务名删掉之后，列表标签就是这条任务的唯一标识，所以换套装、改主词条
        得立刻跟手，不能等切走再刷新。
        只改标签文本，**不动 `_jobs`**：真正的写回仍然只发生在 `_commit_editor`。
        """
        if self._loading or not self._editing_job_id:
            return
        for index, job in enumerate(self._jobs):
            if job.job_id != self._editing_job_id:
                continue
            item = self.chain_list.item(index)
            if item is not None:
                self._apply_job_item(item, index, self._job_from_editor(job.job_id))
            return

    def _update_apply_buttons(self) -> None:
        """「应用于所有任务」得有任务可铺；任务链空了就置灰。"""
        enabled = bool(self._jobs)
        for button in self._apply_buttons:
            button.setEnabled(enabled)

    def _apply_to_all(self, label: str, mutate) -> None:  # noqa: ANN001
        """把当前编辑器里的某项设置一次性铺到任务链里**已存在**的任务上。

        只影响此刻已经有的任务：之后再新建的任务拿它自己的默认值，不会被这里绑住。
        """
        self._commit_editor()
        if not self._jobs:
            self.status.setText("任务链是空的：先新建任务，再应用。")
            return
        for job in self._jobs:
            mutate(job)
        self._mark_unsaved()
        self._refresh_chain_list(self._editing_job_id)
        self.status.setText(f"已把{label}应用到全部 {len(self._jobs)} 个任务。")

    def _apply_weights_to_all(self) -> None:
        weights = dict(self.weight_editor.weights())
        required = self.weight_editor.required_substats()

        def apply(job):
            job.weights = dict(weights)
            job.required_substats = required

        self._apply_to_all("词条单价与必带", apply)

    def _apply_limit_to_all(self) -> None:
        limit = int(self.max_discs.value())
        self._apply_to_all("处理额度", lambda job: setattr(job, "max_discs", limit))

    def _apply_sets_to_all(self) -> None:
        sets = tuple(self._filter_sets)
        self._apply_to_all("套装", lambda job: setattr(job, "sets", sets))

    def _confirm_select_all_jobs(self) -> bool:
        """二次确认。单独成方法，测试里覆盖掉它就不用真弹窗。"""
        return ConfirmSelectAllDialog(len(self._jobs), self).exec() == QDialog.DialogCode.Accepted

    def _select_all_jobs(self) -> None:
        """跨全部任务：各自生成组合，再把符合本档要求的卡片全部勾上。

        口径 =「生成组合 + 全选当前可见」，逐个任务按**它自己的**权重和本档
        要求各跑一遍（部位不同，屏蔽的副词条也不同，不能共用当前界面那套）。
        会覆盖这些任务现有的勾选，执行前先弹二次确认。
        一次性的：只处理此刻已有的任务；之后新建的任务依旧是空的（accepted
        为 None，运行时跳过），不受这次影响。
        """
        self._commit_editor()
        if not self._jobs:
            self.status.setText("任务链是空的：先新建任务，再批量全选。")
            return
        if not self._confirm_select_all_jobs():
            self.status.setText("已取消：没有改动任何任务。")
            return
        # 快照成纯数据再进后台线程——那边不能碰界面控件。
        plans = [
            (
                job.job_id,
                job.slot,
                tuple(job.mainstats),
                dict(job.weights),
                tuple(job.required_substats),
                {
                    str(origin): dict(table)
                    for origin, table in (job.stage_require or {}).items()
                },
            )
            for job in self._jobs
        ]
        self._start_utility(
            lambda: [
                (job_id, qualifying_pattern_ids(slot, mains, weights, require, self._pattern_catalog,
                                               required_substats=required))
                for job_id, slot, mains, weights, required, require in plans
            ],
            self._apply_select_all,
            "正在为全部任务生成组合并勾选合格卡片…",
        )

    def _apply_select_all(self, results) -> None:  # noqa: ANN001
        by_id = {job.job_id: job for job in self._jobs}
        applied = 0
        skipped = 0
        total = 0
        for job_id, accepted in results:
            job = by_id.get(job_id)
            if job is None:
                continue
            if not accepted:
                # 空集绝对不能写：accepted=[] 在运行时是「全部判弃置」，
                # 不是「一张都不勾」。没设权重、或门槛高到没卡过，一律跳过。
                skipped += 1
                continue
            job.accepted = tuple(accepted)
            applied += 1
            total += len(accepted)
        self._mark_unsaved()
        self._refresh_chain_list(self._editing_job_id)
        current = self._editing_job()
        if current is not None and current.accepted is not None:
            # 当前任务的数据变了，界面得跟上：权重和门槛没动，卡片不用重算，
            # 只把勾选态重铺一遍。
            self._selection_seed = current.accepted
            if not self._preview_dirty:
                policy = self._card_check_policy()
                for section in self.sections.values():
                    section.resync_checks(policy)
        note = f"已按各自门槛为 {applied} 个任务勾选，共 {total} 张卡片"
        if skipped:
            note += f"；{skipped} 个任务没有符合门槛的卡片，已跳过未改动"
        if self._preview_dirty:
            note += "（当前任务尚未生成组合）"
        self.status.setText(note + "。")

    def _refresh_main_boxes(self, keep: set[str] | None = None) -> None:
        keep = set(keep if keep is not None else self._selected_mains())
        available = set(selectable_mainstats(self._selected_slots()))
        active_slot = self._active_slot if self._active_slot in self._mainstat_pages else None
        for slot, boxes in self._main_boxes_by_slot.items():
            group = self._main_groups[slot]
            group.setExclusive(False)
            choices = set(boxes) & keep if slot == self._active_slot else set()
            for name, box in boxes.items():
                box.blockSignals(True)
                box.setChecked(name in choices)
                box.blockSignals(False)
            any_main = self._main_any_by_slot[slot]
            any_main.blockSignals(True)
            any_main.setChecked(not choices)
            any_main.blockSignals(False)
        if active_slot is not None:
            self.main_host.setCurrentWidget(self._mainstat_pages[active_slot])
        else:
            self.main_host.setCurrentIndex(self.main_host.count() - 1)
        self.filter_main_boxes = self._main_boxes_by_slot.get(self._active_slot, {})
        if available:
            self.main_hint.setText(f"{self._active_slot} 号位主词条可多选，共用单价与卡片规则；单选时排除同名副词条。")
        else:
            self.main_hint.setText(
                f"{self._active_slot} 号位主词条固定是{FIXED_MAINSTATS[self._active_slot]}，不用选。右侧只编这个部位的副词条。"
            )

    def _snapshot_slot(self) -> dict:
        stage_require = {"three": {}, "four": {}}
        for (origin, checkpoint), section in self.sections.items():
            stage_require[origin][checkpoint] = float(section.require.value())
        return {
            "weights": self.weight_editor.weights(),
            "required_substats": self.weight_editor.required_substats(),
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
        self.weight_editor.set_weights(dict(plan.get("weights") or {}), tuple(plan.get("required_substats") or ()))
        self._apply_stage_require(plan.get("stage_require") or {})

    def _propagate_require(self, section: StageSection) -> None:
        """以当前档的本档要求 x 为基准，把同部位更高档依次设为 x+1、x+2……（每高 3 档 +1）。

        从任意档点都生效：点 +6 就只往下填 +9/+12/+15。
        直接 setValue（不拦信号），每档照常刷新可见卡片并计入未保存状态，
        与手动逐个改数值走完全相同的路径。
        """
        base = section.require.value()
        for checkpoint in CHECKPOINTS:
            if checkpoint <= section.checkpoint:
                continue
            target = self.sections[(section.origin.value, checkpoint)]
            target.require.setValue(base + (checkpoint - section.checkpoint) // 3)

    def _copy_other_require(self, origin: Origin) -> None:
        """把另一侧（3词/4词）每档的本档要求（右侧卡片权重）照搬到本侧。

        逐档 setValue（不拦信号），与手动改数值走同一条路径：每档照常刷新可见
        卡片、计入未保存状态。只搬「本档要求」这一列，不动卡片勾选。
        """
        other = Origin.FOUR if origin is Origin.THREE else Origin.THREE
        for checkpoint in CHECKPOINTS:
            value = self.sections[(other.value, checkpoint)].require.value()
            self.sections[(origin.value, checkpoint)].require.setValue(value)
        self._mark_unsaved()
        self.status.setText(
            f"已把「初始 {_ORIGIN_CN[other]}」每档的本档要求拷贝到「初始 {_ORIGIN_CN[origin]}」。"
        )

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

    def _on_main_changed(self, slot: int, name: str | None, checked: bool) -> None:
        if self._loading:
            return
        boxes = self._main_boxes_by_slot[slot]
        if name is None and checked:
            for box in boxes.values():
                box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(False)
        any_main = self._main_any_by_slot[slot]
        any_main.blockSignals(True)
        any_main.setChecked(not any(box.isChecked() for box in boxes.values()))
        any_main.blockSignals(False)
        self._on_filter_changed()

    def _on_filter_changed(self) -> None:
        if self._loading:
            return
        # 主词条进了任务标签，改完要先让列表那行跟上
        self._refresh_current_item_text()
        self._schedule_rebuild()

    def _origin_scroll(self, origin: Origin) -> dict[str, object]:
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(12, 8, 12, 8)
        for checkpoint in CHECKPOINTS:
            section = StageSection(origin, checkpoint, None)
            section.require_changed.connect(self._on_require_changed)
            section.selection_changed.connect(self._on_selection_changed)
            section.propagate_require.connect(lambda sec=section: self._propagate_require(sec))
            self.sections[(origin.value, checkpoint)] = section
            box.addWidget(section)
        box.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        return {"scroll": scroll, "inner": inner, "box": box}

    def _build_action_bar(self) -> QFrame:
        """操作栏：全选 / 生成组合 / 开始任务 …。

        它属于红框内部——红框 = 三栏 + 这一行，拖红框下边那条线就是改红框的高度。
        """
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
        self.refresh_cards_btn = QPushButton("生成组合")
        self.refresh_cards_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_cards_btn.clicked.connect(lambda: self.rebuild_cards())
        self._task_controls.append(self.refresh_cards_btn)
        # 跨全部任务的批量全勾：每个任务按它自己的权重和本档要求各算一遍。
        # 会覆盖这些任务现有的勾选，所以进 _apply_buttons —— 任务链空了就置灰。
        # 提示语不挂悬停，改由点击后的二次确认弹窗承载（见 ConfirmSelectAllDialog）。
        self.select_all_jobs_btn = QPushButton("全任务卡片全选")
        self.select_all_jobs_btn.clicked.connect(self._select_all_jobs)
        self._task_controls.append(self.select_all_jobs_btn)
        self._apply_buttons.append(self.select_all_jobs_btn)
        row.addWidget(all_on)
        row.addWidget(all_off)
        row.addWidget(self.refresh_cards_btn)
        row.addWidget(self.select_all_jobs_btn)
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
        return bar

    def _build_status_bar(self) -> QWidget:
        """状态行：当前任务 / 运行状态 / 结果统计。

        它在红线以下，和日志面板同属下面那块——高度不吃窗口拉伸，只被红框顶着走。
        """
        panel = QWidget()
        panel.setObjectName("statusPanel")
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.run_status_label = QLabel()
        self.run_status_label.setObjectName("status")
        # 结果统计：界面**不显示**详细日志正文，只给一行摘要 + 详细日志文件路径。
        self.run_stats_path_label = QLabel()
        self.run_stats_path_label.setObjectName("stats_path")
        self.run_stats_path_label.setWordWrap(True)
        self.run_stats_path_label.setTextFormat(Qt.TextFormat.RichText)
        self.run_stats_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self.run_stats_path_label.setOpenExternalLinks(True)
        self.run_stats_path_label.setVisible(False)
        box.addWidget(self.status)
        box.addWidget(self.run_status_label)
        box.addWidget(self.run_stats_path_label)
        self._run_timer = QTimer(self)
        self._run_timer.setInterval(1000)
        self._run_timer.timeout.connect(self._refresh_run_status)
        self._run_timer.start()
        self._refresh_run_status()
        return panel

    def _on_start_clicked(self) -> None:
        """用当前 UI 配置在子线程启动 Runner；运行期间禁用按钮，结束自动恢复。"""
        if self._run_thread is not None or self._utility_thread is not None:
            return
        if self._preview_thread is not None or self._preview_timer.isActive():
            self.status.setText("组合正在生成，请等待完成后开始任务。")
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
            if job.accepted is None:
                continue
            blocked = blocked_substats(job.slot, job.mainstats)
            if not any(value > 0 and name in SUBSTAT_NAMES and name not in blocked for name, value in job.weights.items()):
                QMessageBox.warning(self, "无法启动", f"第 {index} 个任务「{job.title()}」没有可用的正权重副词条。")
                return
        try:
            save_preset(self._path, preset)
        except Exception as exc:  # 落盘失败
            QMessageBox.warning(self, "无法启动", f"开始前自动保存失败：{exc}")
            return
        self._set_saved_state()
        if all(job.accepted is None for job in preset.task_chain):
            self.status.setText("所有任务均未生成，已跳过。请先生成组合。")
            return
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
        # 带上打开的 yaml：Runner 按它开本轮轮次并登记这轮用的是哪份配置。
        self._run_thread = _RunThread(deepcopy(preset), self._controller, config_path=str(self._path))
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
        # 任务收尾后按轮数回收一次；回收失败不影响界面收尾，下次触发再试。
        from zzz_ddtool.retain import retain_rounds

        try:
            retain_rounds()
        except Exception:
            pass
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

    @staticmethod
    def _path_exists(path: str) -> bool:
        """记录里的路径还在不在。路径本身坏掉（空值、非法字符）也当不在。"""
        try:
            return Path(path).exists()
        except (OSError, ValueError):
            return False

    def _show_stats(self, headline: str, path: str) -> None:
        """跑完：状态栏换成结果统计——一行摘要 + 详细日志路径（正文不进界面）。

        报告随整轮一起回收，记录里的旧路径可能已经没有文件：只给已存在的文件
        可点击链接，被清理过的写明原因，不指向不存在的东西。
        """
        self.run_status_label.setText(headline or "运行结束")
        self.run_status_label.setWordWrap(True)
        if not path:
            self.run_stats_path_label.setText("详细结果统计：本轮未生成报告文件")
        elif self._path_exists(path):
            uri = Path(path).as_uri()
            self.run_stats_path_label.setText(f'详细结果统计：<a href="{uri}">{path}</a>')
        else:
            self.run_stats_path_label.setText(
                f"详细结果统计：{path}（该轮报告已回收：历史日志清理）"
            )
        # 判据用 isHidden（显式隐藏标志）：isVisible 在窗口还没 show 时恒为 False。
        if self.run_stats_path_label.isHidden():
            self.run_stats_path_label.setVisible(True)

    def _hide_stats(self) -> None:
        """运行中/无统计：收起结果统计区，状态栏回到进度显示。"""
        if not self.run_stats_path_label.isHidden():
            self.run_stats_path_label.setVisible(False)

    def _show_run_status(self, data: dict) -> None:
        # 报告路径优先用 report_path，旧的 run-status.json 只写了 stats_path。
        report = data.get("report_path") or data.get("stats_path") or ""
        if report or data.get("stats_headline"):
            self._show_stats(data.get("stats_headline", ""), report)
            return
        self._hide_stats()
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
        """日志设置：级别 + 历史日志保留轮数。"""
        from zzz_ddtool.logsettings import LogSettingsDialog

        dialog = LogSettingsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        dialog.apply_settings()
        self.status.setText("日志设置已保存")

    def check_update(self) -> None:
        from zzz_ddtool.updater import check_for_update

        self._start_utility(check_for_update, self._handle_update_status, "正在检查更新…")

    def fetch_public_examples(self) -> None:
        from zzz_ddtool.examples import fetch_examples

        def collect() -> tuple[list, str]:
            """把网络/清单异常变成可展示的结果 —— _start_utility 的失败路径不弹窗。"""
            try:
                return fetch_examples(), ""
            except Exception as exc:  # ExamplesError 以及意外的网络错误
                return [], str(exc)

        self._start_utility(collect, self._handle_public_examples, "正在获取示例列表…")

    def _handle_public_examples(self, result) -> None:  # noqa: ANN001
        items, error = result
        if error:
            QMessageBox.information(self, "获取公共示例", error)
            return
        if not items:
            QMessageBox.information(self, "获取公共示例", "远端暂无示例。")
            return

        picked = self._open_example_fetch_dialog(items)
        if not picked:
            return

        from zzz_ddtool.examples import save_all_examples

        ok, problems = save_all_examples(picked)
        if problems:
            QMessageBox.information(
                self,
                "获取公共示例",
                f"已保存 {ok} 项；以下未保存：\n" + "\n".join(problems),
            )
            return
        QMessageBox.information(self, "获取公共示例", f"已保存 {ok} 项。")

    def _open_example_fetch_dialog(self, items) -> list:  # noqa: ANN001
        """单独成方法，测试里覆盖掉它就不用真弹窗。"""
        dialog = ExampleFetchDialog(items, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return []
        return dialog.selected_items()

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

    def _toggle_run_log(self, expanded: bool) -> None:
        """收起/展开运行日志。

        收起时日志区整个让位：日志框收掉、只留标题行，**窗口跟着变矮一截**（下面少掉一块），
        展开时原样长回来。窗口上沿不动，只有底边升降。
        """
        self.log_toggle.setText("▼" if expanded else "▶")
        self.log_toggle.setToolTip("收起运行日志" if expanded else "展开运行日志")

        before = self._lower_pane.height()
        if expanded:
            self.run_log.setVisible(True)
            self._lower_pane.layout().activate()
            # 展开：把上次收起让出去的高度要回来，但不低于"日志框刚好放得下"的高度
            hint = self._lower_pane.minimumSizeHint().height()
            target = max(hint, self._log_lower_height or hint)
        else:
            # 日志框让出的高度 = 日志框本身 + 它与标题行之间的间距。
            # 不从 minimumSizeHint 取：那个值有缓存，隐藏后未必立刻刷新。
            freed = self.run_log.height() + self.log_panel.layout().spacing()
            self._log_lower_height = before
            self.run_log.setVisible(False)
            self._lower_pane.layout().activate()
            target = max(0, before - freed)

        delta = target - before
        if delta:
            # 下半块变多少，窗口就变多少：上面（三栏）高度不动，只有底边升降。
            # 自己改窗口大小，别让 resizeEvent 的钳制把它弹回原样。
            self._dragging_main_splitter = True
            try:
                self.resize(self.width(), max(1, self.height() + delta))
                total = sum(self._main_splitter.sizes())
                self._main_splitter.setSizes([max(0, total - target), target])
            finally:
                self._dragging_main_splitter = False
        self._remember_lower_height(target)

    def _refresh_log_path_label(self) -> None:
        """标题行显示当前轮次的 ddtool.log；没有在建轮次就直说，不指向别的目录。"""
        from zzz_ddtool import loground

        round_ = loground.current()
        self._log_path_shown = round_.log_path if round_ is not None else None
        if self._log_path_shown is None:
            self.log_path.setText("日志文件：尚无日志目录")
        else:
            self.log_path.setText(f"日志文件：{self._log_path_shown}")

    def _refresh_run_status(self) -> None:
        """每秒读 run-status.json + 日志尾部，工具窗口实时显示运行状态。"""
        import json

        if self._controller is not None:
            self.pause_btn.setText("恢复 / F9" if self._controller.paused else "暂停 / F9")
            if self._controller.stopped:
                self.stop_btn.setText("等待安全停止…")
                self.stop_btn.setEnabled(False)
                self.pause_btn.setEnabled(False)

        from zzz_ddtool import loground, logutil

        text = "运行状态：—（还没有运行记录）"
        status_file = loground.status_file()
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
        # 读取当前轮次的主日志，覆盖导航、OCR、执行器等模块。
        if not hasattr(self, "run_log"):
            return
        # 换了轮次（任务开始、下一任务）：路径提示和读取缓存都跟着新文件走，
        # 别接着上一轮的正文和滚动位置显示。
        round_ = loground.current()
        if (round_.log_path if round_ is not None else None) != self._log_path_shown:
            self._log_tail = None
            self._refresh_log_path_label()
        log_path = logutil.current_log_path()
        if log_path is None:
            return
        try:
            with log_path.open("rb") as stream:
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
        self.name_edit.setText(preset.name)
        # 判断后打不打标记：两个勾是这一项的唯一来源，不再另存一份 _label_policy。
        self.mark_discard.setChecked(preset.labels.discard)
        self.mark_lock.setChecked(preset.labels.save)
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
            job.mainstats = tuple(dict.fromkeys(name for name in job.mainstats if name in _MAINSTAT_PAGES.get(job.slot, ())))
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
                "required_substats": first.required_substats,
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
        self._refresh_path_label()
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
        self._refresh_current_counts()

    def _on_required_changed(self) -> None:
        if self._loading:
            return
        required = self.weight_editor.required_substats()
        blocked = self._blocked_substats()
        for section in self.sections.values():
            section.required_substats = required
            section.blocked = blocked
            if not self._preview_dirty:
                section.apply_filter()
        self._mark_unsaved()
        self._update_status()
        self._refresh_current_counts()

    def _reset_preview(self) -> None:
        self._invalidate_preview()
        for section in self.sections.values():
            section.set_cards([], lambda _pid: False)
        self.trial.set_name_choices(self._blocked_substats())
        self._update_status()
        self._refresh_current_counts()

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
        worker = _PatternThread(generation, weights, self, self._pattern_catalog)
        self._preview_thread = worker
        worker.result.connect(self._on_preview_result)
        worker.failed.connect(self._on_preview_failed)
        worker.finished.connect(self._on_preview_finished)
        self.status.setText("正在后台计算组合…")
        self.refresh_cards_btn.setText("重新生成")
        self._refresh_current_counts()
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
        mains = tuple(self._selected_mains())
        patterns = [pattern for pattern in patterns if possible_substats(
            self._active_slot, mains, (slot.name for slot in pattern.slots)
        )]
        section = self.sections[key]
        section.required_substats = self.weight_editor.required_substats()
        section.blocked = self._blocked_substats()
        if section.can_reuse(patterns):
            section.reuse_cards(patterns, self._preview_policy)
        else:
            section.set_cards(patterns, self._preview_policy)
        if self._preview_install:
            # 每次只安装一个分区，事件循环可以处理输入、绘制及关闭请求。
            self._preview_timer.start(1)
            return
        self._preview_dirty = False
        # Explicit generation establishes a whitelist; editing settings alone does not.
        if self._selection_seed is None:
            self._selection_seed = tuple(self._accepted_ids())
            keep = frozenset(self._selection_seed)
            for section in self.sections.values():
                previous_block = section.blockSignals(True)
                section.resync_checks(lambda pid: pid in keep)
                section.blockSignals(previous_block)
            self._mark_unsaved()
        for section in self.sections.values():
            section.flow_host.show()
        self.refresh_cards_btn.setText("刷新组合")
        self._update_status()
        self._refresh_current_counts()

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
        else:
            self._refresh_current_counts()

    def _rebased_selection(self, filtered: bool = False) -> tuple[str, ...] | None:
        job = TaskJob(
            slot=self._active_slot, mainstats=tuple(self._selected_mains()),
            weights=self.weight_editor.weights(), accepted=self._selection_seed,
            required_substats=self.weight_editor.required_substats(),
            stage_require={origin.value: {level: float(self.sections[(origin.value, level)].require.value())
                                          for level in CHECKPOINTS} for origin in Origin},
        )
        return rebase_selection(job, filtered=filtered)

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
        self._refresh_current_counts()

    def _on_require_changed(self) -> None:
        self._mark_unsaved()
        self._update_status()
        self._refresh_current_counts()

    def collect_preset(self) -> Preset:
        # 落盘 / 试判 / 开始任务都走这里，是「离开编辑器」的汇总口：先把当前编辑内容
        # 写回任务节点，再取配置，否则跑的是上一次提交的旧值。
        self._commit_editor()
        # Apply the same effective selection to inactive tasks too. Batch weight edits
        # must not leave old IDs executable while the list shows the new configuration.
        for job in self._jobs:
            job.accepted = rebase_selection(job)
        self._refresh_chain_list(self._editing_job_id)
        self._refresh_current_counts()
        self._slot_plans[self._active_slot] = self._snapshot_slot()
        current = self._slot_plans[self._active_slot]
        return preset_from_dict(
            {
                "name": self.name_edit.text().strip() or "default",
                "weights": current.get("weights") or {},
                "required_substats": list(current.get("required_substats") or ()),
                "stage_require": current.get("stage_require") or {"three": {}, "four": {}},
                "min_useful_types": int(current.get("min_useful_types") or 0),
                "run": {"max_discs": int(self.max_discs.value())},
                "labels": {"discard": self.mark_discard.isChecked(),
                           "save": self.mark_lock.isChecked()},
                "game_filter": {
                    "sets": list(self._filter_sets),
                    "slot": self._active_slot,
                    "slots": [self._active_slot],
                    "mainstats": list(current.get("mainstats") or ()),
                },
                "slot_plans": {
                    str(slot): {
                        "weights": plan.get("weights") or {},
                        "required_substats": list(plan.get("required_substats") or ()),
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
        condition = "符合条件" if self.weight_editor.required_substats() else "符合门槛"
        self.status.setText(
            f"任务链 {len(self._jobs)} 节 · 正在编 {self._active_slot} 号 · "
            f"{scope}：组合共 {total} 种，{condition} {visible}，已勾选 {picked}"
        )

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
            # 只写状态栏会被忽略：更新下载耗时长，用户会一直以为只是"慢"。
            # 2026-10-14 线上就是这个表现——替身脚本写失败、状态栏有字、用户没看见。
            QMessageBox.warning(self, "操作失败", f"操作没能完成：\n\n{worker.error}")
        else:
            self._update_status()
            self._utility_success(worker.value)

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
        # 不加「✓」这类符号前缀：字体缺字形时是一个方块（2026-10-14 去掉）。
        # “成功”由绿色和「已保存」四个字表达，够清楚了。
        self.save_label.setText(f"已保存 · {datetime.now().strftime('%H:%M:%S')}")
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
        self.save_current()  # 落盘新档；内部同时记住新路径，下次启动就打开它
        # 另存为换了工作文件，界面文件名要跟上——2026-10-13 前这里漏了刷新，
        # 标签停在旧档名上，显示和实际保存目标不一致。
        self._refresh_path_label()

    def _refresh_path_label(self) -> None:
        """文件名标签跟随 self._path：打开和另存为换了工作文件后都要刷新。"""
        self.path_label.setText(self._path.name)
        self.path_label.setToolTip(str(self._path))

    def run_trial(self) -> None:
        disc = self.trial.disc()
        if disc is None:
            self.trial.result.setText("至少填一条副词条。")
            return
        preset = self.collect_preset()
        if preset.accepted is None:
            self.trial.result.setText("当前任务未生成卡片名单，执行时会跳过；请先生成组合。")
            return
        result = decide(disc, preset, preset.accepted)
        self.trial.result.setText(
            f"{result.action.value}  ·  {result.reason}\n"
            f"权重 {result.score}  ·  有效 {result.useful_types}/{result.max_useful_types}  ·  "
            f"+{result.checkpoint}  ·  {result.origin or '-'}"
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self._layout_timer.stop()
        self._persist_layout()
        self._closing = True
        self._preview_request = None
        self._preview_install = []
        self._preview_timer.stop()
        self._counts_timer.stop()
        if self._counts_thread is not None:
            self._counts_thread.requestInterruption()
            event.ignore()
            return
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
    font = QFont(_UI_FONT_FAMILY, _UI_FONT_PT)
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

    def __init__(self, generation: int, weights: dict[str, float], parent: QWidget,
                 catalog: PatternCatalog | None = None) -> None:
        super().__init__(parent)
        self.generation = generation
        self.weights = dict(weights)
        self.catalog = catalog or PatternCatalog()

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
            grouped = self.catalog.get(self.weights, cancelled, enumerate_patterns)
            if grouped is not None and not self.isInterruptionRequested():
                self.result.emit(self.generation, {key: list(cards) for key, cards in grouped.items()})
        except Exception as exc:
            self.failed.emit(self.generation, str(exc))


class _TaskCountsThread(QThread):
    result = Signal(object, str, dict)
    failed = Signal(object, str, str)

    def __init__(self, plans: list, catalog: PatternCatalog, parent: QWidget) -> None:
        super().__init__(parent)
        self.plans = plans
        self.catalog = catalog

    def run(self) -> None:
        from time import monotonic, sleep

        next_yield = monotonic() + 0.01

        def cancelled() -> bool:
            nonlocal next_yield
            if self.isInterruptionRequested():
                return True
            if monotonic() >= next_yield:
                sleep(0.001)
                next_yield = monotonic() + 0.01
            return self.isInterruptionRequested()

        memo = {}
        for signature, job in self.plans:
            if cancelled():
                return
            try:
                if signature in memo:
                    self.result.emit(signature, job.job_id, memo[signature])
                    continue
                grouped = self.catalog.get(effective_weights(job), cancelled)
                if grouped is None:
                    return
                counts = task_counts(job, grouped, cancelled)
                if counts is None or cancelled():
                    return
                memo[signature] = counts
                self.result.emit(signature, job.job_id, counts)
            except InterruptedError:
                return
            except Exception as exc:
                self.failed.emit(signature, job.job_id, str(exc))


class _RunThread(QThread):
    """在子线程跑 Runner，避免阻塞 GUI 事件循环；Runner 内部 F9/F10 热键照常生效。"""

    status_changed = Signal(dict)

    def __init__(self, preset: Preset, controller, config_path: str = "") -> None:
        super().__init__()
        self._preset = preset
        self._controller = controller
        self._config_path = config_path

    def run(self) -> None:
        try:
            from zzz_ddtool.runner import Runner

            # 每个任务在 Runner 里自己开一轮，不和启动轮次或上一个任务合并。
            Runner(
                self._preset,
                controller=self._controller,
                on_status=self.status_changed.emit,
                config_path=self._config_path,
            ).run()
        except Exception as exc:
            from zzz_ddtool.logutil import get_logger

            get_logger("zzz_ddtool.gui").exception("任务线程失败")
            self.status_changed.emit({"phase": "failed", "message": str(exc)})
        finally:
            self._controller.close()


def run_gui(config: Path | None = None) -> int:
    # 先把上次存的日志级别 / 历史轮数装回去。放在最前：get_logger 首次调用会懒
    # setup() 按默认级别配置，restore 越早，启动最早那批日志的级别越准。
    from zzz_ddtool import loground
    from zzz_ddtool.logprefs import restore
    from zzz_ddtool.retain import retain_rounds
    from zzz_ddtool.singleinstance import LockUnavailable, acquire, release

    restore()
    app = QApplication.instance() or QApplication(sys.argv)

    lock = None
    hint = ""
    try:
        lock = acquire()
    except LockUnavailable as exc:
        # 锁建不出来（共享根只读等）：让位照常启动，但不能说成"已在运行"
        hint = f"提示：无法建立单实例锁（{exc}）；本次不限制重复启动。"
    if lock is None and not hint:
        QMessageBox.information(None, "zzz-ddtool", "程序已在运行。")
        return 0
    try:
        # 开一轮启动日志：配置装载和界面阶段的日志有处可去，只开窗口不跑任务也留下它。
        # 任务各自在 Runner 里另开新轮次，不和这轮合并；目录建不出来就只写控制台，照常启动。
        try:
            loground.begin("gui")
        except OSError as exc:
            from zzz_ddtool.logutil import get_logger

            get_logger("zzz_ddtool.gui").warning("启动轮次没建起来，本次不写日志文件：%s", exc)
        apply_light_style(app)
        window = MainWindow(config)
        if hint:
            window.status.setText(hint)
        window.show()
        return app.exec()
    finally:
        # 退出：先关本轮文件、释放占用，再按轮数回收一次。回收失败也不挡退出，下次触发再试。
        try:
            loground.close_active()
        except Exception:
            pass
        try:
            retain_rounds()
        except Exception:
            pass
        if lock is not None:
            release(lock)

