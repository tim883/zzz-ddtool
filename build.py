"""打包脚本：PyInstaller onedir + 复制运行资源（不含任务 YAML）+ 写 version.txt。

用法（家用 / 工作两机跑同一套）::

    uv pip install --python .venv\\Scripts\\python.exe -r requirements-lock.txt
    .venv\\Scripts\\python.exe build.py
    .venv\\Scripts\\python.exe build.py --outdir dist/恢复接续-20261002

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
PROTECTED_DIRS = (".git", ".venv", "src", "tests", "scripts", "data", "user", "手册", "dev-docs", "build")
PRESERVED_MARKERS = (".git", ".zzz-ddtool-root", "pyproject.toml", "user")


def _read_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def copy_runtime_data(source: Path, target: Path) -> None:
    """只丢最外层 `data/` 直属的 YAML（任务存档），放行 `data/examples/` 下的示例。"""
    root = source.resolve()

    def ignore_plans(directory: str, names: list[str]) -> list[str]:
        if Path(directory).resolve() != root:
            return []
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


def _build_paths(output: str) -> tuple[Path, Path, Path]:
    """在清理前验证实际路径，避免输出覆盖源码、存档或链接指向的目录。"""
    root = ROOT.resolve()
    outdir = Path(output)
    outdir = (outdir if outdir.is_absolute() else root / outdir).resolve()
    pkg_dir = (outdir / PKG_NAME).resolve()
    work_dir = (root / "build").resolve()

    if pkg_dir != outdir / PKG_NAME:
        raise ValueError(f"目标包目录通过链接指向了其他位置：{pkg_dir}")
    if root.is_relative_to(pkg_dir):
        raise ValueError(f"目标包目录包含源码仓库：{pkg_dir}")
    for name in PROTECTED_DIRS:
        protected = (root / name).resolve()
        if pkg_dir.is_relative_to(protected) or protected.is_relative_to(pkg_dir):
            raise ValueError(f"目标包目录与受保护目录 {name} 重叠：{pkg_dir}")
    if work_dir != root / "build":
        raise ValueError(f"build 中间目录通过链接指向了其他位置：{work_dir}")
    for target in (pkg_dir, work_dir):
        if target.exists() and not target.is_dir():
            raise ValueError(f"清理目标不是目录：{target}")
        if any((target / name).exists() for name in PRESERVED_MARKERS):
            raise ValueError(f"清理目标含源码或用户数据，拒绝删除：{target}")
    return outdir, pkg_dir, work_dir


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        outdir, pkg_dir, work_dir = _build_paths(args.outdir)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"打包目录无效：{exc}", file=sys.stderr)
        return 2
    version = _read_version()
    print(f"打包版本：{version}")
    print(f"输出目录：{pkg_dir}")

    # 只清理本次目标目录与 PyInstaller 中间目录；outdir 下的历史包一律不动
    shutil.rmtree(work_dir, ignore_errors=True)
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
            str(work_dir),
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
