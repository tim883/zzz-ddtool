"""发布出包脚本：拒绝含 user//log/ 的包目录，并保证 zip 根就是内容本身。

zip 根多套一层会让用户解压错位（发版手册 §3 的关键坑）；含 user//log/ 会把
个人配置与日志发出去，进而在更新时覆盖用户自己的那份。
"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("package_release_test", ROOT / "scripts/package_release.py")
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)


def _clean_pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "zzz-ddtool"
    (pkg / "_internal").mkdir(parents=True)
    (pkg / "data" / "examples").mkdir(parents=True)
    (pkg / "data" / "sets.json").write_text("[]", encoding="utf-8")
    (pkg / "data" / "examples" / "example.yaml").write_text("name: a", encoding="utf-8")
    (pkg / "zzz-ddtool.exe").write_text("exe", encoding="utf-8")
    (pkg / "version.txt").write_text("1.2.3", encoding="utf-8")
    return pkg


def test_refuses_package_with_user_dir(tmp_path: Path, monkeypatch) -> None:
    pkg = _clean_pkg(tmp_path)
    (pkg / "user").mkdir()
    monkeypatch.setattr(pr, "_project_version", lambda: "1.2.3")

    with pytest.raises(SystemExit):
        pr.main(["--pkg", str(pkg), "--outdir", str(tmp_path / "out")])

    assert not list((tmp_path / "out").glob("*.zip"))


def test_refuses_package_with_log_dir(tmp_path: Path, monkeypatch) -> None:
    pkg = _clean_pkg(tmp_path)
    (pkg / "log").mkdir()
    monkeypatch.setattr(pr, "_project_version", lambda: "1.2.3")

    with pytest.raises(SystemExit):
        pr.main(["--pkg", str(pkg), "--outdir", str(tmp_path / "out")])

    assert not list((tmp_path / "out").glob("*.zip"))


def test_refuses_when_version_txt_mismatches_project(tmp_path: Path, monkeypatch) -> None:
    pkg = _clean_pkg(tmp_path)
    monkeypatch.setattr(pr, "_project_version", lambda: "9.9.9")

    with pytest.raises(SystemExit):
        pr.main(["--pkg", str(pkg), "--outdir", str(tmp_path / "out")])


def test_program_zip_root_is_contents_not_nested(tmp_path: Path, monkeypatch) -> None:
    pkg = _clean_pkg(tmp_path)
    monkeypatch.setattr(pr, "_project_version", lambda: "1.2.3")

    assert pr.main(["--pkg", str(pkg), "--outdir", str(tmp_path / "out")]) == 0

    names = zipfile.ZipFile(tmp_path / "out" / "zzz-ddtool-v1.2.3.zip").namelist()
    assert "zzz-ddtool.exe" in names
    assert "version.txt" in names
    assert "data/sets.json" in names
    assert not any(n.startswith("zzz-ddtool/") for n in names)
    assert not any(n.endswith("/") for n in names)  # 不写空目录条目


def test_data_zip_root_is_data_contents(tmp_path: Path, monkeypatch) -> None:
    pkg = _clean_pkg(tmp_path)
    monkeypatch.setattr(pr, "_project_version", lambda: "1.2.3")

    assert pr.main(["--pkg", str(pkg), "--outdir", str(tmp_path / "out"), "--only", "data"]) == 0

    names = zipfile.ZipFile(tmp_path / "out" / "data.zip").namelist()
    assert "sets.json" in names
    assert "examples/example.yaml" in names
    assert not any(n.startswith("data/") for n in names)
    assert not any(n.endswith("/") for n in names)
