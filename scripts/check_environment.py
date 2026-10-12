"""两台电脑分别运行，记录实际解释器、依赖和环境变量；不安装或修改环境。

报告默认写进本轮测试日志目录 ``log_test/<时间戳>/environment.json``；
``--out`` 显式给出时按给定路径写。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_NAMES = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "QT_QPA_PLATFORM")

sys.path.insert(0, str(ROOT / "src"))

from zzz_ddtool import loground


def collect_environment() -> dict:
    locked = {}
    for line in (ROOT / "requirements-lock.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "==" in line:
            name, version = line.split("==", 1)
            locked[name.strip()] = version.strip()
    dependencies = {}
    for name in (*locked, "pytest"):
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        expected = locked.get(name)
        dependencies[name] = {
            "installed": actual,
            "locked": expected,
            "matches_lock": actual == expected if expected is not None else None,
        }
    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "architecture": platform.machine(),
            "prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
            "in_venv": sys.prefix != sys.base_prefix,
            "meets_project_minimum": sys.version_info >= (3, 11),
        },
        "command_resolution": {name: shutil.which(name) for name in ("python", "py", "pip", "pytest")},
        # 只记录与 Python/Qt 相关的变量，避免导出凭据或整个环境。
        "environment": {name: os.environ.get(name) for name in ENV_NAMES},
        "python_path_entries": [
            entry for entry in os.environ.get("PATH", "").split(os.pathsep)
            if any(word in entry.lower() for word in ("python", "conda", "venv"))
        ],
        "dependencies": dependencies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None,
                        help="报告输出路径；不给出时写进本轮测试日志目录")
    args = parser.parse_args()
    out = args.out
    if out is None:
        loground.use_test_context("check-environment")
        out = loground.current().root / "environment.json"
    report = collect_environment()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Python {report['python']['version']}: {sys.executable}")
    print(f"venv: {report['python']['in_venv']}")
    for name, item in report["dependencies"].items():
        print(f"{name}: installed={item['installed']} locked={item['locked']}")
    print(f"report: {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
