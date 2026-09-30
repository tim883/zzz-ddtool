"""打包脚本：PyInstaller onedir + 复制 data/ 到 exe 同级 + 写 version.txt。

家用 / 工作两机跑同一套：`python build.py`（先建 venv 并按 requirements-lock.txt 装依赖）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "zzz-ddtool"


def _read_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def main() -> int:
    version = _read_version()
    print(f"打包版本：{version}")

    # 清理旧产物
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    shutil.rmtree(ROOT / "dist", ignore_errors=True)

    # PyInstaller 打包（onedir）
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "zzz-ddtool.spec"],
        cwd=ROOT,
        check=True,
    )

    # 复制 data/ 到 exe 同级（数据独立更新，改套装名/词条名不用重打 exe）
    shutil.copytree(ROOT / "data", DIST / "data", dirs_exist_ok=True)

    # 写 version.txt（exe 同级，程序启动读它取本地版本号）
    (DIST / "version.txt").write_text(version + "\n", encoding="utf-8")

    print(f"完成：{DIST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
