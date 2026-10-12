"""日志设置：级别 + 历史日志保留轮数。

分开两件事，别混：
    级别     —— 日志写多少（截图始终全量，不受这个影响）
    保留轮数 —— 已关闭的历史轮次留几轮，超了从最旧的整轮删

这两样都会记到 ``user/log-settings.json``（读写归 logprefs.py），下次启动自动装回。

保留轮数是文本框：只允许 1—100，超过 100 不进（见 QIntValidator）。
正在写入的那一轮不占历史名额，所以运行期间目录数可能暂时多于设置值。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QIntValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from zzz_ddtool import loground
from zzz_ddtool.logprefs import (
    MAX_KEEP_ROUNDS,
    MIN_KEEP_ROUNDS,
    apply as apply_prefs,
    load as load_prefs,
)
from zzz_ddtool.retain import clear_history, count_rounds, dir_size, retain_rounds

# 级别只影响日志写多少，不影响截图
LEVELS: tuple[tuple[str, int], ...] = (
    ("常规 —— 每步一条「页面 · 动作 → 结果」", logging.INFO),
    ("详细 —— 额外打出每步的步骤号", logging.DEBUG),
    ("只看问题 —— 可疑和失败", logging.WARNING),
)


def human_size(num_bytes: int) -> str:
    """把字节数说成人话，别在界面上摆一长串数字。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def folder_size(directory: Path) -> int:
    return dir_size(directory)


class LogSettingsDialog(QDialog):
    """日志设置窗口：级别 + 保留轮数 + 当前占用和清理。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("日志设置")
        self.setMinimumWidth(500)

        from zzz_ddtool import paths

        self._log_root = paths.log_root

        # 从记着的设置里读，不从运行时状态读：只在内存里改过没保存的那一轮不该被显示出来
        saved_level, saved_rounds = load_prefs()

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # 级别：截图不受影响，这句话必须写清楚，否则用户以为开了详细才存图
        self.level_combo = _LevelCombo()
        self.level_combo.set_level(saved_level)
        form.addRow("日志级别", self.level_combo)
        note = QLabel("截图始终全量保存，不随级别变化。级别只管日志写多少。")
        note.setObjectName("hint")
        note.setWordWrap(True)
        form.addRow("", note)

        self.rounds_edit = QLineEdit(str(saved_rounds))
        self.rounds_edit.setValidator(QIntValidator(MIN_KEEP_ROUNDS, MAX_KEEP_ROUNDS, self))
        self.rounds_edit.setMaximumWidth(90)
        rounds_row = QHBoxLayout()
        rounds_row.addWidget(self.rounds_edit)
        rounds_row.addWidget(QLabel(f"轮（{MIN_KEEP_ROUNDS}—{MAX_KEEP_ROUNDS}）"))
        rounds_row.addStretch(1)
        form.addRow("历史日志保留最近", rounds_row)

        keep_note = QLabel(
            "超了从最旧的轮次整轮删（主日志、报告、统计、性能、截图一起走）。"
            "正在写入的那一轮不算名额。"
        )
        keep_note.setObjectName("hint")
        keep_note.setWordWrap(True)
        form.addRow("", keep_note)

        layout.addLayout(form)

        # 当前占用：没有这些数字，用户填上限是瞎填
        self.usage = QLabel()
        self.usage.setObjectName("hint")
        self.usage.setWordWrap(True)
        layout.addWidget(self.usage)

        button_row = QHBoxLayout()
        current_btn = QPushButton("打开当前日志文件夹")
        current_btn.clicked.connect(self._open_current)
        root_btn = QPushButton("打开日志根目录")
        root_btn.clicked.connect(self._open_root)
        clear_btn = QPushButton("清理历史日志")
        clear_btn.clicked.connect(self._clear)
        button_row.addWidget(current_btn)
        button_row.addWidget(root_btn)
        button_row.addWidget(clear_btn)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_usage()

    def _refresh_usage(self) -> None:
        """显示当前轮次、历史轮数和占用，让用户填数字时有依据。"""
        root = self._log_root()
        history = count_rounds(root)
        total = dir_size(root)
        round_ = loground.current()
        if round_ is None:
            current = "当前：没有正在写入的轮次"
        else:
            current = f"当前轮次：{round_.id}（{round_.root}）"
        self.usage.setText(
            f"{current}\n"
            f"历史轮次：{history} 轮；日志根共 {human_size(total)}\n"
            f"日志根：{root}"
        )

    def _open_current(self) -> None:
        round_ = loground.current()
        folder = round_.root if round_ is not None else self._log_root()
        self._open(folder)

    def _open_root(self) -> None:
        self._open(self._log_root())

    @staticmethod
    def _open(folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _clear(self) -> None:
        """只清理已关闭的历史轮次，留着正在写入的目录。"""
        removed = clear_history()
        self._refresh_usage()
        self.usage.setText(f"已清理 {removed} 轮历史日志。\n" + self.usage.text())

    def chosen_level(self) -> int:
        return self.level_combo.currentData()

    def chosen_rounds(self) -> int:
        """文本框里的轮数。空着或填坏（中间态）就退回记着的值。"""
        try:
            value = int(self.rounds_edit.text().strip())
        except (TypeError, ValueError):
            return load_prefs()[1]
        return min(MAX_KEEP_ROUNDS, max(MIN_KEEP_ROUNDS, value))

    def apply_settings(self) -> None:
        """应用设置：改级别 + 记下新轮数 + 按它回收。

        轮数要落盘，否则下次启动 retain 又回到默认值，用户填的数字等于白填。
        """
        apply_prefs(self.chosen_level(), self.chosen_rounds())
        retain_rounds()


class _LevelCombo(QComboBox):
    """级别下拉。直接用中文当显示文字，存的 int 放在 item data 里。"""

    def __init__(self) -> None:
        super().__init__()
        for text, value in LEVELS:
            self.addItem(text, value)

    def set_level(self, level: int) -> None:
        for index, (_text, value) in enumerate(LEVELS):
            if value == level:
                self.setCurrentIndex(index)
                return
        self.setCurrentIndex(0)
