"""把 ``build.py`` 的产物打成发布 zip。

两个包，命名与语义对齐发版手册 §3：

- **程序包** ``zzz-ddtool-v<version>.zip``：根 = 包目录内容（exe / ``_internal/`` / ``data/`` / ``version.txt``）
- **数据包** ``data.zip``：根 = ``data/`` 里的内容

出包前校验：包目录必须存在且顶层含程序文件、**不得含顶层 ``user/`` 与 ``log/``**、
``version.txt`` 必须与 ``pyproject.toml`` 一致。任一不满足即非零退出、**不产出任何 zip**。

更新器侧还有一层剥离兜底（``updater._strip_protected``）；这一层的作用是"别让脏包出门"。
"""

from __future__ import annotations

import argparse
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_NAME = "zzz-ddtool"
FORBIDDEN_TOP = ("user", "log")
REQUIRED_TOP = ("zzz-ddtool.exe", "_internal", "data", "version.txt")


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="打发布 zip（程序包 / 数据包）")
    parser.add_argument("--pkg", default=str(ROOT / "dist" / PKG_NAME), help="待发布的包目录")
    parser.add_argument("--outdir", default=None, help="zip 输出目录，默认=包目录的父目录")
    parser.add_argument("--only", choices=("program", "data"), default=None, help="只出一个包")
    return parser.parse_args(argv)


def _fail(message: str) -> None:
    print(f"出包中止：{message}", file=sys.stderr)
    raise SystemExit(2)


def _validate(pkg: Path, only: str | None) -> None:
    if not pkg.is_dir():
        _fail(f"包目录不存在：{pkg}")
    for name in FORBIDDEN_TOP:
        if (pkg / name).exists():
            _fail(f"包目录含顶层 {name}/（跑过的包不能发）：{pkg / name}")
    if only != "data":
        missing = [name for name in REQUIRED_TOP if not (pkg / name).exists()]
        if missing:
            _fail(f"包目录缺少 {missing}：{pkg}")
    elif not (pkg / "data").is_dir():
        _fail(f"包目录缺少 data/：{pkg}")

    version_file = pkg / "version.txt"
    if version_file.exists():
        text = version_file.read_text(encoding="utf-8").strip()
        expected = _project_version()
        if text != expected:
            _fail(f"version.txt（{text}）与 pyproject.toml（{expected}）不一致")
    elif only != "data":
        _fail("包目录缺少 version.txt")


def _write_zip(zip_path: Path, base: Path) -> None:
    """把 ``base`` 里的**内容**写成 zip 根（不套一层），且只写文件条目。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(base.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(base).as_posix())


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    pkg = Path(args.pkg).resolve()
    outdir = Path(args.outdir).resolve() if args.outdir else pkg.parent
    _validate(pkg, args.only)

    version = _project_version()
    if args.only != "data":
        program_zip = outdir / f"{PKG_NAME}-v{version}.zip"
        _write_zip(program_zip, pkg)
        print(f"程序包：{program_zip}")
    if args.only != "program":
        data_zip = outdir / "data.zip"
        _write_zip(data_zip, pkg / "data")
        print(f"数据包：{data_zip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
