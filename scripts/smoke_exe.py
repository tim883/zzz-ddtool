"""exe 冒烟验收：验证打包态的资源加载、窗口、本轮日志与正常退出。

用法::

    .venv\\Scripts\\python.exe scripts/smoke_exe.py <exe路径> [--fixture <盘面截图>]

做两件事，都不触碰游戏：

1. **无窗口**：跑 ``read-disc`` 读一张真机盘面截图，验证 rapidocr 模型与 Qt
   在打包态可加载（PyInstaller 最常见的坑就是模型没带进去）。read-disc 是手动
   诊断入口，属测试上下文，输出显式写到 ``<公共根>/log_test/``；
   windowed exe 没有可用 stdout，只能靠它写出的 JSON 回读结果。
2. **有窗口**：启动 GUI，等主窗口出现，确认首次运行在公共根建立 ``log/``
   并写入一轮——``log/<时间戳编号>/`` 里有 ``ddtool.log`` 与轮次登记
   ``round.json``；发 WM_CLOSE 关闭，确认进程退出、无残留。

只杀自己启动的那个进程，不动任何其他进程。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zzz_ddtool import loground

MAIN_TITLE = "驱动盘强化辅助"
WM_CLOSE = 0x0010
ROUND_LOG_NAME = "ddtool.log"

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def visible_windows(pid: int) -> list[tuple[int, str]]:
    """列出该 PID 的可见顶层窗口 (hwnd, title)。"""
    found: list[tuple[int, str]] = []

    def cb(hwnd, _lparam):  # noqa: ANN001
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            found.append((int(hwnd), buf.value))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


def wait_for_window(pid: int, timeout_s: float) -> tuple[int, str] | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for hwnd, title in visible_windows(pid):
            if MAIN_TITLE in title:
                return hwnd, title
        time.sleep(0.5)
    return None


def public_root(pkg: Path) -> Path:
    """公共根：与 zzz_ddtool.paths.user_data_root() 同一条规则。

    打包态 = 从 exe 同级向上找到 `.zzz-ddtool-root` 的那一层，没有标记时回退到 exe 同级。
    """
    for parent in (pkg, *pkg.parents):
        if (parent / ".zzz-ddtool-root").is_file():
            return parent
    return pkg


def log_root(pkg: Path) -> Path:
    """正式运行日志根 ``<公共根>/log``。"""
    return public_root(pkg) / "log"


def test_log_root(pkg: Path) -> Path:
    """测试/开发日志根 ``<公共根>/log_test``（手动诊断等入口的落点）。"""
    return public_root(pkg) / "log_test"


def round_dirs(root: Path) -> list[Path]:
    """日志根下登记的轮次目录：名字合轮次编号规则，且带 round.json。"""
    if not root.is_dir():
        return []
    return sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir()
            and loground.NAME_PATTERN.match(path.name)
            and (path / loground.REGISTRY_NAME).is_file()
        ),
        key=lambda path: path.name,
    )


def new_round_since(root: Path, before: set[str], timeout_s: float = 10.0) -> Path | None:
    """等本轮 GUI 新建的轮次目录出现（启动轮次早于窗口，这里只是兜底）。"""
    deadline = time.time() + timeout_s
    while True:
        fresh = [path for path in round_dirs(root) if path.name not in before]
        if fresh:
            return fresh[-1]
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def wait_for_path(path: Path, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while True:
        if path.exists():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def step_resource_check(exe: Path, pkg: Path, fixture: Path) -> dict:
    """打包态资源加载：read-disc 读真机截图，结果写到测试日志根。"""
    out = test_log_root(pkg) / "smoke-read-disc.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    result: dict = {
        "step": "resource",
        "out": str(out),
        "cmd": [str(exe), "read-disc", str(fixture), "--out", str(out)],
    }
    try:
        proc = subprocess.run(
            [str(exe), "read-disc", str(fixture), "--out", str(out)],
            capture_output=True,
            timeout=300,
        )
        result["returncode"] = proc.returncode
    except subprocess.TimeoutExpired:
        result["ok"] = False
        result["error"] = "read-disc 超时（300s）"
        return result

    result["json_written"] = out.exists()
    if out.exists():
        payload = json.loads(out.read_text(encoding="utf-8"))
        result["payload_ok"] = payload.get("ok")
        disc = payload.get("disc") or {}
        result["level"] = disc.get("level")
        result["substats"] = [s.get("name") for s in (disc.get("substats") or [])]
        result["error"] = payload.get("error")
    result["ok"] = bool(result.get("json_written") and result.get("payload_ok"))
    result["test_log_root_created"] = test_log_root(pkg).is_dir()
    return result


def step_gui_check(exe: Path, pkg: Path, timeout_s: float) -> dict:
    """GUI 窗口 + 公共根本轮日志 + 正常退出。"""
    root = log_root(pkg)
    before = {path.name for path in round_dirs(root)}
    result: dict = {"step": "gui", "log_root": str(root), "log_root_existed": root.is_dir()}
    proc = subprocess.Popen([str(exe)], cwd=str(pkg))
    result["pid"] = proc.pid
    try:
        found = wait_for_window(proc.pid, timeout_s)
        result["window"] = None if not found else {"hwnd": found[0], "title": found[1]}
        result["windows_all"] = [t for _h, t in visible_windows(proc.pid)]

        # 轮次目录在 GUI 启动时就建好，先看它：窗口起不来时这份证据同样有用
        round_dir = new_round_since(root, before)
        result["new_round_created"] = round_dir is not None
        result["round_dir"] = None if round_dir is None else str(round_dir)
        if round_dir is not None:
            registry = round_dir / loground.REGISTRY_NAME
            log_file = round_dir / ROUND_LOG_NAME
            result["round_registry"] = str(registry)
            result["round_registry_exists"] = registry.is_file()
            result["round_log"] = str(log_file)
            result["round_log_exists"] = wait_for_path(log_file, 5.0)
            result["round_log_size"] = log_file.stat().st_size if log_file.exists() else 0

        if found is None:
            result["ok"] = False
            result["error"] = f"{timeout_s:.0f}s 内未见主窗口"
            return result

        if round_dir is None:
            result["ok"] = False
            result["error"] = "GUI 启动后公共根 log/ 下没有出现新的轮次目录"

        user32.PostMessageW(found[0], WM_CLOSE, 0, 0)
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        result["exit_code"] = proc.poll()
        result["exited_cleanly"] = proc.poll() is not None
        result["ok"] = bool(
            result.get("new_round_created")
            and result.get("round_registry_exists")
            and result.get("round_log_exists")
            and result["window"]
            and result["exited_cleanly"]
        )
        if not result["exited_cleanly"]:
            result["error"] = "WM_CLOSE 后 30s 未退出"
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        if proc.poll() is None:
            proc.kill()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="zzz-ddtool exe 冒烟验收")
    parser.add_argument("exe", type=Path, help="zzz-ddtool.exe 路径")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "upgrade-maxed.png",
        help="read-disc 用的盘面截图（真机 15/15 满级展示态）",
    )
    parser.add_argument("--window-timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    exe = args.exe.resolve()
    if not exe.exists():
        print(json.dumps({"ok": False, "error": f"exe 不存在：{exe}"}, ensure_ascii=False))
        return 2
    pkg = exe.parent
    fixture = args.fixture.resolve()
    if not fixture.exists():
        print(json.dumps({"ok": False, "error": f"fixture 不存在：{fixture}"}, ensure_ascii=False))
        return 2

    report = {
        "exe": str(exe),
        "public_root": str(public_root(pkg)),
        "version_txt": (pkg / "version.txt").read_text(encoding="utf-8").strip()
        if (pkg / "version.txt").exists()
        else None,
        "steps": [],
    }
    report["steps"].append(step_resource_check(exe, pkg, fixture))
    report["steps"].append(step_gui_check(exe, pkg, args.window_timeout))
    report["ok"] = all(s.get("ok") for s in report["steps"])

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
