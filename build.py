"""打包脚本：PyInstaller onedir + 复制运行资源（不含任务 YAML）+ 写 version.txt。

用法（家用 / 工作两机跑同一套）::

    .venv\\Scripts\\pip install -r requirements-lock.txt          # 一次性
    python build.py                                     # -> dist/zzz-ddtool/
    python build.py --outdir dist/恢复接续-20261002      # -> dist/恢复接续-20261002/zzz-ddtool/

产物目录名固定为 zzz-ddtool（由 zzz-ddtool.spec 的 COLLECT name 决定）；
`--outdir` 是它的父目录，用来把每次发布放进独立目录、互不覆盖。

本脚本**只清理本次目标目录与 build/ 中间目录**，绝不删除整个 dist/：
spec §8.1 明确要求不要跑会清空 dist 的旧式构建清理流程，plan 阶段 6 也要求保留旧包。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG_NAME = "zzz-ddtool"  # 必须与 zzz-ddtool.spec 的 COLLECT(name=...) 一致
DEFAULT_OUTDIR = ROOT / "dist"


def _read_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def copy_runtime_data(source: Path, target: Path) -> None:
    """YAML files are saved task plans, not release resources."""
    def ignore_plans(directory: str, names: list[str]) -> list[str]:
        return [name for name in names if Path(name).suffix.lower() in {".yaml", ".yml"}]

    shutil.copytree(source, target, ignore=ignore_plans, dirs_exist_ok=True)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="打包 zzz-ddtool（onedir）")
    parser.add_argument(
        "--outdir",
        default=str(DEFAULT_OUTDIR),
        help=f"包的父目录，产物为 <outdir>/{PKG_NAME}/（默认 {DEFAULT_OUTDIR}）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    outdir = Path(args.outdir)
    if not outdir.is_absolute():
        outdir = (ROOT / outdir).resolve()
    pkg_dir = outdir / PKG_NAME
    version = _read_version()
    print(f"打包版本：{version}")
    print(f"输出目录：{pkg_dir}")

    # 只清理本次目标目录与 PyInstaller 中间目录；outdir 下的历史包一律不动
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    shutil.rmtree(pkg_dir, ignore_errors=True)
    outdir.mkdir(parents=True, exist_ok=True)

    # PyInstaller 打包（onedir）
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(outdir),
            "--workpath",
            str(ROOT / "build"),
            "zzz-ddtool.spec",
        ],
        cwd=ROOT,
        check=True,
    )

    # 只复制运行资源；任务存档留在公共根目录的 user/plan。
    copy_runtime_data(ROOT / "data", pkg_dir / "data")

    # 写 version.txt（exe 同级，程序启动读它取本地版本号）
    (pkg_dir / "version.txt").write_text(version + "\n", encoding="utf-8")

    print(f"完成：{pkg_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
