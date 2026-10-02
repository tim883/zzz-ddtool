"""Offline GUI responsiveness and rendering check; never sends game input."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontDatabase

from zzz_ddtool.gamedata import SUBSTAT_NAMES
from zzz_ddtool.gui import MainWindow
from zzz_ddtool import gui


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    font_file = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    if font_file.exists():
        font_id = QFontDatabase.addApplicationFont(str(font_file))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            app.setFont(QFont(families[0], 9))
    with tempfile.TemporaryDirectory(prefix="ddtool-profile-") as temp:
        window = MainWindow(config_path=Path(temp) / "preset.yaml")
        window._add_job()
        window.resize(1440, 1000)
        window.show()
        QTest.qWait(50)
        gaps = []
        previous = time.perf_counter()

        def heartbeat():
            nonlocal previous
            now = time.perf_counter()
            gaps.append((now - previous) * 1000)
            previous = now

        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(heartbeat)
        timer.start()
        report = {}
        install_times = []
        original_install = window._install_preview_section
        window._preview_timer.timeout.disconnect()

        def measured_install():
            started = time.perf_counter()
            original_install()
            install_times.append((time.perf_counter() - started) * 1000)

        window._preview_timer.timeout.connect(measured_install)
        computation_times = []
        original_enumerate = gui.enumerate_patterns

        def measured_enumerate(*args, **kwargs):
            started = time.perf_counter()
            result = original_enumerate(*args, **kwargs)
            computation_times.append((time.perf_counter() - started) * 1000)
            return result

        gui.enumerate_patterns = measured_enumerate
        try:
            for count in (5, 10):
                weights = {name: 1.0 for name in SUBSTAT_NAMES[:count]}
                window.weight_editor.set_weights(weights)
                window._schedule_rebuild()
                gaps.clear()
                install_times.clear()
                computation_times.clear()
                previous = time.perf_counter()
                started = previous
                window.rebuild_cards()
                deadline = started + 60
                while (window._preview_thread is not None or window._preview_timer.isActive()) and time.perf_counter() < deadline:
                    app.processEvents()
                    QTest.qWait(2)
                assert not window._preview_dirty, window.status.text()
                QTest.qWait(50)
                report[str(count)] = {
                    "generation_ms": round((time.perf_counter() - started) * 1000, 1),
                    "patterns": sum(len(s.patterns) for s in window.sections.values()),
                    "widgets": sum(len(s.cards) for s in window.sections.values()),
                    "heartbeat_count": len(gaps),
                    "max_event_gap_ms": round(max(gaps, default=0), 1),
                    "max_install_ms": round(max(install_times, default=0), 1),
                    "enumeration_ms": round(sum(computation_times), 1),
                }
                window.grab().save(str(args.outdir / f"client-{count}-stats.png"))
                before = time.perf_counter()
                window.weight_editor._rows[0][1].setValue(1.5)
                report[str(count)]["weight_edit_ms"] = round((time.perf_counter() - before) * 1000, 1)
                before = time.perf_counter()
                window._add_job()
                report[str(count)]["new_task_ms"] = round((time.perf_counter() - before) * 1000, 1)
                before = time.perf_counter()
                window.chain_list.setCurrentRow(0)
                report[str(count)]["task_switch_ms"] = round((time.perf_counter() - before) * 1000, 1)
                window._remove_job()
                window._add_job()
            window.resize(1280, 800)
            QTest.qWait(50)
            window.grab().save(str(args.outdir / "client-1280.png"))
            window._filter_dialog.show()
            for slot in (4, 5, 6):
                window._on_slot_clicked(slot)
                window._filter_dialog.resize(600, 380)
                QTest.qWait(40)
                window._filter_dialog.grab().save(str(args.outdir / f"mainstat-{slot}.png"))
            window._filter_dialog.close()
        finally:
            gui.enumerate_patterns = original_enumerate
            timer.stop()
            window.close()
            while window._preview_thread is not None:
                app.processEvents()
                QTest.qWait(5)
        result = json.dumps(report, indent=2)
        (args.outdir / "profile.json").write_text(result, encoding="utf-8")
        print(result)


if __name__ == "__main__":
    main()
