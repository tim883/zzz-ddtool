"""后台截图的 Qt 兜底：QScreen/QPixmap 操作回到 GUI 主线程。"""
import threading

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication


class CaptureBridge(QObject):
    requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.requested.connect(self._capture, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _capture(self, request):
        from zzz_ddtool.capture import _grab_screen_region

        try:
            request["image"] = _grab_screen_region(request["hwnd"])
        except Exception as exc:
            request["error"] = exc
        finally:
            request["done"].set()

    def grab(self, hwnd):
        request = {"hwnd": hwnd, "done": threading.Event()}
        self.requested.emit(request)
        if not request["done"].wait(5):
            from zzz_ddtool.capture import CaptureError

            raise CaptureError("主线程截图响应超时")
        if "error" in request:
            raise request["error"]
        return request["image"]


_bridge = None


def install_capture_bridge():
    global _bridge
    app = QApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        raise RuntimeError("截图桥必须在 GUI 主线程初始化")
    if _bridge is None:
        _bridge = CaptureBridge(app)
    return _bridge


def grab_from_worker(hwnd):
    if _bridge is None:
        from zzz_ddtool.capture import CaptureError

        raise CaptureError("未初始化主线程截图桥")
    return _bridge.grab(hwnd)
