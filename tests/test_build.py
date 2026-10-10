"""build.py 打包脚本的参数解析与清理行为回归。

核心保护点：重打包**不得**删除 dist/ 下的历史包（spec §8.1、plan 阶段 6），
且产物结构保持 exe + _internal/ + data/ + version.txt。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

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


def _assert_rejected_before_cleanup(monkeypatch, output: Path | str) -> None:
    cleanup = Mock()
    package = Mock()
    monkeypatch.setattr(build, "shutil", types.SimpleNamespace(rmtree=cleanup))
    monkeypatch.setattr(build, "subprocess", types.SimpleNamespace(run=package))
    assert build.main(["--outdir", str(output)]) == 2
    cleanup.assert_not_called()
    package.assert_not_called()


@pytest.mark.parametrize("output", ["..", ".git", ".venv", "src", "tests", "scripts", "data", "user", "手册", "dev-docs", "build"])
def test_protected_output_is_rejected_before_any_cleanup(tmp_path: Path, monkeypatch, output) -> None:
    root = tmp_path / build.PKG_NAME
    root.mkdir()
    _fake_repo(root)
    monkeypatch.setattr(build, "ROOT", root)
    _assert_rejected_before_cleanup(monkeypatch, output)
    assert (root / "pyproject.toml").is_file()


def test_output_containing_repository_is_rejected(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / build.PKG_NAME / "source"
    root.mkdir(parents=True)
    _fake_repo(root)
    monkeypatch.setattr(build, "ROOT", root)
    _assert_rejected_before_cleanup(monkeypatch, tmp_path)


@pytest.mark.parametrize("marker", [".git", ".zzz-ddtool-root", "pyproject.toml", "user"])
def test_existing_source_or_user_data_in_package_is_preserved(tmp_path: Path, monkeypatch, marker) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _fake_repo(root)
    outdir = tmp_path / "release"
    pkg = outdir / build.PKG_NAME
    pkg.mkdir(parents=True)
    sentinel = pkg / marker
    sentinel.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(build, "ROOT", root)
    _assert_rejected_before_cleanup(monkeypatch, outdir)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_existing_user_data_in_work_directory_prevents_all_cleanup(tmp_path: Path, monkeypatch) -> None:
    root = _fake_repo(tmp_path)
    (root / "build" / "user").mkdir(parents=True)
    monkeypatch.setattr(build, "ROOT", root)
    _assert_rejected_before_cleanup(monkeypatch, root / "dist")


@pytest.mark.parametrize("destination", ["data/zzz-ddtool", "dist/history"])
def test_linked_package_is_rejected_before_cleanup(tmp_path: Path, monkeypatch, destination) -> None:
    root = _fake_repo(tmp_path)
    outdir = root / "dist"
    target = tmp_path / destination
    resolve = Path.resolve

    def resolve_package(path, *args, **kwargs):
        return target if path == outdir / build.PKG_NAME else resolve(path, *args, **kwargs)

    monkeypatch.setattr(build, "ROOT", root)
    monkeypatch.setattr(Path, "resolve", resolve_package)
    _assert_rejected_before_cleanup(monkeypatch, outdir)


def test_linked_work_directory_is_rejected_before_cleanup(tmp_path: Path, monkeypatch) -> None:
    root = _fake_repo(tmp_path)
    target = tmp_path / "src"
    resolve = Path.resolve

    def resolve_work(path, *args, **kwargs):
        return target if path == root / "build" else resolve(path, *args, **kwargs)

    monkeypatch.setattr(build, "ROOT", root)
    monkeypatch.setattr(Path, "resolve", resolve_work)
    _assert_rejected_before_cleanup(monkeypatch, root / "dist")


def test_external_output_directory_remains_supported(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _fake_repo(root)
    outdir = tmp_path / "release"
    monkeypatch.setattr(build, "ROOT", root)
    _stub_pyinstaller(monkeypatch, outdir)
    assert build.main(["--outdir", str(outdir)]) == 0
    assert (outdir / build.PKG_NAME / "zzz-ddtool.exe").is_file()


def test_examples_are_packaged_but_loose_yaml_is_not(tmp_path: Path) -> None:
    """data/ 直属的 YAML 是任务存档、不出厂；data/examples/ 下的示例要随包发。"""
    src = tmp_path / "data"
    (src / "examples").mkdir(parents=True)
    (src / "examples" / "example.yaml").write_text("name: x", encoding="utf-8")
    (src / "sets.json").write_text("[]", encoding="utf-8")
    (src / "test-material-short.yaml").write_text("name: dev", encoding="utf-8")

    out = tmp_path / "out"
    build.copy_runtime_data(src, out)

    assert (out / "examples" / "example.yaml").exists()
    assert (out / "sets.json").exists()
    assert not (out / "test-material-short.yaml").exists()


def test_nested_directory_named_data_keeps_its_yaml(tmp_path: Path) -> None:
    """过滤只作用于本次复制的最外层：嵌套的 data/ 目录不该被连带丢掉。"""
    src = tmp_path / "data"
    (src / "examples" / "data").mkdir(parents=True)
    (src / "examples" / "data" / "nested.yaml").write_text("name: n", encoding="utf-8")

    out = tmp_path / "out"
    build.copy_runtime_data(src, out)

    assert (out / "examples" / "data" / "nested.yaml").exists()
