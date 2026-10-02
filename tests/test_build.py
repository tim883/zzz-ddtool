"""build.py 打包脚本的参数解析与清理行为回归。

核心保护点：重打包**不得**删除 dist/ 下的历史包（spec §8.1、plan 阶段 6），
且产物结构保持 exe + _internal/ + data/ + version.txt。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import build  # noqa: E402


def _fake_repo(tmp_path: Path) -> Path:
    """伪造一个最小仓库根：pyproject.toml + data/。"""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "zzz-ddtool"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "sets.json").write_text("{}", encoding="utf-8")
    return tmp_path


def _stub_pyinstaller(monkeypatch, outdir: Path) -> None:
    """打桩 subprocess.run：模拟 PyInstaller 在 outdir/ 下生成 onedir 包。"""

    def fake_run(cmd, cwd=None, check=False):  # noqa: ANN001, ARG001
        pkg = outdir / build.PKG_NAME
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "zzz-ddtool.exe").write_bytes(b"MZ")
        (pkg / "_internal").mkdir(exist_ok=True)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(build, "subprocess", types.SimpleNamespace(run=fake_run))


def test_default_outdir_is_repo_dist() -> None:
    args = build._parse_args([])
    assert Path(args.outdir) == build.DEFAULT_OUTDIR
    assert build.DEFAULT_OUTDIR == REPO_ROOT / "dist"


def test_outdir_can_be_overridden() -> None:
    args = build._parse_args(["--outdir", "dist/恢复接续-20261002"])
    assert args.outdir == "dist/恢复接续-20261002"


def test_build_keeps_sibling_packages(tmp_path: Path, monkeypatch) -> None:
    """--outdir 下的历史包必须原样保留，不被本次清理波及。"""
    root = _fake_repo(tmp_path)
    outdir = root / "dist"
    old = outdir / "旧包-20260101"
    old.mkdir(parents=True)
    (old / "keep.txt").write_text("old", encoding="utf-8")

    monkeypatch.setattr(build, "ROOT", root)
    _stub_pyinstaller(monkeypatch, outdir)

    assert build.main(["--outdir", str(outdir)]) == 0

    assert (old / "keep.txt").read_text(encoding="utf-8") == "old"
    pkg = outdir / build.PKG_NAME
    assert (pkg / "zzz-ddtool.exe").exists()
    assert (pkg / "_internal").is_dir()
    assert (pkg / "version.txt").read_text(encoding="utf-8").strip() == "9.9.9"
    assert (pkg / "data" / "sets.json").exists()


def test_build_clears_only_target_package(tmp_path: Path, monkeypatch) -> None:
    """目标包目录会被重建（旧内容清掉），但兄弟目录不受影响。"""
    root = _fake_repo(tmp_path)
    outdir = root / "dist"
    pkg = outdir / build.PKG_NAME
    pkg.mkdir(parents=True)
    (pkg / "stale.dll").write_bytes(b"stale")
    sibling = outdir / "另一个包-20260101"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("old", encoding="utf-8")

    monkeypatch.setattr(build, "ROOT", root)
    _stub_pyinstaller(monkeypatch, outdir)

    assert build.main(["--outdir", str(outdir)]) == 0

    assert not (pkg / "stale.dll").exists()  # 目标目录已重建
    assert (sibling / "keep.txt").exists()  # 兄弟目录保留
