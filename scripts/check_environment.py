"""两台电脑分别运行，记录实际解释器、依赖和环境变量；不安装或修改环境。"""

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
    parser.add_argument("--out", type=Path, default=ROOT / "log" / "debug" / "environment.json")
    args = parser.parse_args()
    report = collect_environment()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Python {report['python']['version']}: {sys.executable}")
    print(f"venv: {report['python']['in_venv']}")
    for name, item in report["dependencies"].items():
        print(f"{name}: installed={item['installed']} locked={item['locked']}")
    print(f"report: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
