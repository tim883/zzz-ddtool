"""禁止滚轮误触的输入框。

鼠标滚轮悬在数字框上滚动会把数值悄悄改掉（误触），这里整体禁掉：
wheelEvent 直接 ignore()，数字框自己不再消费滚轮，
事件照常冒泡给父级滚动区，滚轮回到「滚动页面」的正常行为。
"""

from __future__ import annotations

from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox


class _NoWheelMixin:
    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        event.ignore()


class NoWheelDoubleSpinBox(_NoWheelMixin, QDoubleSpinBox):
    """滚轮不改值的 QDoubleSpinBox。"""


class NoWheelSpinBox(_NoWheelMixin, QSpinBox):
    """滚轮不改值的 QSpinBox。"""
