"""日志设置：级别 + 截图容量。

分开两件事，别混：
    级别   —— 日志写多少（截图始终全量，不受这个影响）
    容量   —— 截图留多少张，纯按数量，超了从最旧的轮次整轮删

为什么没有「失败永远保留」：
    排错靠 debug 模式重跑复现，不靠翻旧图回溯。留着失败图
    只会把正常的图挤掉。见 retain.py。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from zzz_ddtool.retain import MAX_CAPTURE_FILES, clear_captures, count_captures, retain_captures

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
    if not directory.exists():
        return 0
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


class LogSettingsDialog(QDialog):
    """日志设置窗口：级别 + 截图容量 + 当前占用。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("日志设置")
        self.setMinimumWidth(460)

        from zzz_ddtool.logutil import level as current_level
        from zzz_ddtool.paths import captures_dir, log_dir

        self._captures_dir = captures_dir
        self._log_dir = log_dir

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # 级别：截图不受影响，这句话必须写清楚，否则用户以为开了详细才存图
        self.level_combo = _LevelCombo()
        self.level_combo.set_level(current_level())
        form.addRow("日志级别", self.level_combo)
        note = QLabel("截图始终全量保存，不随级别变化。级别只管日志写多少。")
        note.setObjectName("hint")
        note.setWordWrap(True)
        form.addRow("", note)

        self.keep_spin = QSpinBox()
        self.keep_spin.setRange(20, 5000)
        self.keep_spin.setSingleStep(50)
        self.keep_spin.setSuffix(" 张")
        self.keep_spin.setValue(MAX_CAPTURE_FILES)
        form.addRow("截图最多留", self.keep_spin)

        keep_note = QLabel("超了从最旧的轮次整轮删。半截的轮次没参考价值。")
        keep_note.setObjectName("hint")
        keep_note.setWordWrap(True)
        form.addRow("", keep_note)

        layout.addLayout(form)

        # 当前占用：没有这个数字，用户填上限是瞎填
        self.usage = QLabel()
        self.usage.setObjectName("hint")
        self.usage.setWordWrap(True)
        layout.addWidget(self.usage)

        open_row = QHBoxLayout()
        open_btn = QPushButton("打开日志文件夹")
        open_btn.clicked.connect(self._open_folder)
        clear_btn = QPushButton("立即清理全部截图")
        clear_btn.clicked.connect(self._clear)
        open_row.addWidget(open_btn)
        open_row.addWidget(clear_btn)
        open_row.addStretch(1)
        layout.addLayout(open_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_usage()

    def _refresh_usage(self) -> None:
        """显示当前占了多少，让用户填数字时有依据。"""
        shots = count_captures()
        total = folder_size(self._log_dir())
        self.usage.setText(
            f"当前：截图 {shots} 张，日志目录共 {human_size(total)}\n"
            f"位置：{self._log_dir()}"
        )

    def _open_folder(self) -> None:
        folder = self._log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _clear(self) -> None:
        removed = clear_captures()
        self._refresh_usage()
        self.usage.setText(f"已清理 {removed} 张截图。\n" + self.usage.text())

    def chosen_level(self) -> int:
        return self.level_combo.currentData()

    def chosen_keep(self) -> int:
        return self.keep_spin.value()

    def apply_settings(self) -> None:
        """应用设置：改级别 + 按新上限回收。"""
        from zzz_ddtool.logutil import set_level

        set_level(self.chosen_level())
        retain_captures(max_files=self.chosen_keep())


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
