"""受限单步标定：不导航、不重置、不选项、不确认、不处理盘；仅显式 main 才操作游戏。"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtGui import QGuiApplication
from zzz_ddtool import gamefilter as gf, wininput
from zzz_ddtool.capture import find_game_hwnd, grab_game_image, window_pid
from zzz_ddtool.control import controlled_inputs, validated_inputs
from zzz_ddtool.filter_replay import SetPickerKey
from zzz_ddtool.hotkeys import HotkeyController
from zzz_ddtool.layout import FILTER_BTN
from zzz_ddtool.ocr import read_name_bands, read_rect
from zzz_ddtool.screen import DRIVE_DISC, is_page

COMMANDS = {"check", "open-panel", "open-popup", "scroll", "top", "esc"}
TOP_NAMES = ("荆棘玫瑰", "谶羽之誓", "拂晓行纪", "呼啸沙龙")


def parse_command(argv):
    if not argv or argv[0] not in COMMANDS:
        raise ValueError("只允许 check/open-panel/open-popup/scroll/top/esc")
    if argv[0] == "scroll":
        if len(argv) not in (2, 3):
            raise ValueError("scroll <delta> [1]")
        delta = int(argv[1])
        if not -12 <= delta <= 12 or delta == 0 or (len(argv) == 3 and int(argv[2]) != 1):
            raise ValueError("单步只允许一个事件，delta 非零且在 -12—12")
        return argv[0], delta
    if len(argv) != 1:
        raise ValueError("该命令不接受额外参数")
    return argv[0], None


class Probe:
    def __init__(self, controller, directory):
        self.controller = controller
        self.directory = directory
        self.hwnd = int(find_game_hwnd())
        self.pid = window_pid(self.hwnd)
        self.layout = gf._calibrated_layout(1920, 1080)
        self.key = SetPickerKey(self.hwnd, self.pid, 1920, 1080, 96, self.layout.layout_id, ())
        self.metrics = gf._PickerMetrics("learn")
        self.frames = []
        self.inputs = []

    def frame(self):
        self.controller.checkpoint()
        image, method, actual = grab_game_image(self.hwnd)
        if (int(actual) != self.hwnd or window_pid(self.hwnd) != self.pid or
                (image.width(), image.height()) != (1920, 1080) or gf._window_dpi(self.hwnd) != 96):
            raise RuntimeError("窗口/PID/尺寸/DPI 改变，拒绝输入")
        return image, method

    def save(self, tag):
        image, method = self.frame()
        path = self.directory / f"{len(self.frames):02d}-{tag}.png"
        if not image.save(str(path)):
            raise RuntimeError("原图保存失败")
        popup = gf._picker_popup_open(image)
        data = {"image": path.name, "method": method, "hwnd": self.hwnd, "pid": self.pid,
                "width": image.width(), "height": image.height(), "dpi": gf._window_dpi(self.hwnd),
                "popup": popup, "at_top": gf._picker_at_top(image, self.layout) if popup is True else None,
                "names": []}
        if popup is True:
            coarse = read_rect(image, self.layout.list_area)
            bands = gf._picker_name_bands(image, coarse, self.layout)
            lines = read_name_bands(image, bands)
            data["names"] = [{"text": line.text if line else None, "band": list(band),
                              "box": [line.left, line.top, line.right, line.bottom] if line else None}
                             for band, line in zip(bands, lines)]
            data["at_top"] = self.named_top_state(image, data["names"])
        self.frames.append(data)
        print(json.dumps(data, ensure_ascii=False), flush=True)
        return image

    def named_top_state(self, image, items):
        if len(items) < 4 or any(item["text"] is None for item in items[:4]):
            return None
        if tuple(item["text"] for item in items[:4]) != TOP_NAMES:
            return False
        return gf._picker_at_top(image, self.layout)

    def top_state(self, image):
        bands = gf._picker_name_bands(image, read_rect(image, self.layout.list_area), self.layout)
        return self.named_top_state(image, [{"text": line.text if line else None}
                                           for line in read_name_bands(image, bands)])

    def warehouse(self, image):
        return any("驱动仓库" in ln.text.replace(" ", "")
                   for ln in read_rect(image, (0, 90, 700, 200)))

    def require(self, predicate):
        image, _ = self.frame()
        if predicate(image) is not True:
            raise RuntimeError("当前页面或目标改变/未知，拒绝输入；不自动导航")

    def panel_state(self, image, visible):
        # 弹窗提示行亮度不适用于未遮挡的仓库网格；以仓库正证据和标题未被模态遮暗判入口。
        title_visible = gf._bright_tail(image, (75, 123, 340, 157)) >= (60 if visible else 110)
        return (title_visible and is_page(image, DRIVE_DISC) and self.warehouse(image) and
                gf._panel_present(image) is visible)

    def click(self, x, y, predicate):
        self.require(predicate)
        with validated_inputs(lambda: self.require(predicate)):
            wininput.click_client(self.hwnd, x, y)

    def popup_input(self, callback):
        self.require(lambda image: gf._picker_stage_matches(image, self.layout))
        blocked = wininput.input_blocked_reason(self.hwnd)
        if blocked:
            raise RuntimeError(blocked)
        with gf._checked_picker_input(self.metrics, self.layout, self.key, None,
                                      "标定最后检查点弹窗不符"):
            callback()

    def scroll(self, delta):
        self.popup_input(lambda: wininput.wheel_client(self.hwnd, self.layout.scroll_x,
                                                       self.layout.scroll_y, delta=delta))
        time.sleep(self.layout.scroll_wait_ms / 1000)

    def execute(self, command, delta):
        before = self.save("before")
        if command == "check":
            from zzz_ddtool.capture import _bitblt_screen, _print_window
            # 只保存对照图；没有完整观察前不激活窗口或发送输入。
            time.sleep(0.3)
            repeat = _print_window(self.hwnd)
            if repeat is not None:
                repeat.save(str(self.directory / "print-repeat.png"))
            foreground = wininput.is_foreground(self.hwnd)
            print(f"[抓取对照] foreground={foreground}", flush=True)
            if foreground:
                screen = _bitblt_screen(self.hwnd)
                if screen is not None:
                    screen.save(str(self.directory / "bitblt.png"))
            return
        blocked = wininput.input_blocked_reason(self.hwnd)
        if blocked:
            raise RuntimeError(blocked)
        if command == "open-panel":
            if self.panel_state(before, True):
                return
            self.click(*FILTER_BTN, lambda image: self.panel_state(image, False))
            time.sleep(0.7)
            self.require(lambda image: self.panel_state(image, True))
        elif command == "open-popup":
            if gf._picker_stage_matches(before, self.layout) is True:
                return
            self.require(lambda image: self.panel_state(image, True))
            hits = [ln for ln in read_rect(before, gf.FILTER_SETS_BAR) if ln.text.replace(" ", "") == "点击选择"]
            if len(hits) != 1:
                raise RuntimeError("套装打开按钮不唯一，拒绝点击")
            target = hits[0]
            box = (target.left, target.top, target.right, target.bottom)

            def target_present(image):
                hits = [ln for ln in read_rect(image, gf.FILTER_SETS_BAR)
                        if ln.text.replace(" ", "") == "点击选择"]
                return (self.panel_state(image, True) and len(hits) == 1 and
                        (hits[0].left, hits[0].top, hits[0].right, hits[0].bottom) == box)

            self.click((target.left + target.right) // 2, (target.top + target.bottom) // 2, target_present)
            time.sleep(0.8)
            self.require(lambda image: gf._picker_stage_matches(image, self.layout))
        elif command == "scroll":
            self.scroll(delta)
        elif command == "esc":
            if gf._picker_stage_matches(before, self.layout) is True:
                self.popup_input(lambda: wininput.press_key_once(self.hwnd, wininput.VK_ESCAPE))
                time.sleep(0.5)
                self.require(lambda image: gf._picker_popup_closed(image))
            else:
                predicate = lambda image: self.panel_state(image, True)
                self.require(predicate)
                with validated_inputs(lambda: self.require(predicate)):
                    wininput.press_key_once(self.hwnd, wininput.VK_ESCAPE)
                time.sleep(0.5)
                self.require(lambda image: self.panel_state(image, False))
        elif command == "top":
            for _ in range(8):
                image, _ = self.frame()
                if gf._picker_stage_matches(image, self.layout) is not True:
                    raise RuntimeError("回顶时弹窗不符")
                at_top = self.top_state(image)
                if at_top is None:
                    raise RuntimeError("顶部状态未知，拒绝追加滚动")
                if at_top is True:
                    break
                self.scroll(3)
                self.save("top-step")
            else:
                self.require(self.top_state)
        self.save("after")


def main(argv=None):
    command, delta = parse_command(sys.argv[1:] if argv is None else argv)
    app = QGuiApplication.instance() or QGuiApplication([])
    directory = ROOT / "log/debug/picker-scroll" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    directory.mkdir(parents=True, exist_ok=False)
    controller = HotkeyController()
    probe = None
    result = {"command": command, "delta": delta, "outcome": "failed"}
    print(f"[标定目录] {directory}", flush=True)
    try:
        probe = Probe(controller, directory)
        with controlled_inputs(controller.checkpoint), wininput.capture_inputs(probe.inputs.append):
            probe.execute(command, delta)
        result["outcome"] = "completed"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        controller.close()
        result["frames"] = probe.frames if probe else []
        result["inputs"] = probe.inputs if probe else []
        (directory / "probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[标定结束] {result['outcome']} {directory}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
