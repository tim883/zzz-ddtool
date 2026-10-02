from pathlib import Path

import pytest

from zzz_ddtool import paths
from zzz_ddtool.userdata import migrate_plans, persistent_config_path


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "tool"
    root.mkdir()
    monkeypatch.setattr(paths, "user_data_root", lambda: root)
    monkeypatch.setattr(paths, "base_dir", lambda: root)
    return root


def test_example_and_other_presets_migrate_equally(data_root):
    old = data_root / "data"
    old.mkdir()
    for name in ("example.yaml", "example3.yaml", "my-task.yml"):
        (old / name).write_text("name: saved\n", encoding="utf-8")
    migrate_plans()
    assert {path.name for path in paths.plans_dir().iterdir()} == {"example.yaml", "example3.yaml", "my-task.yml"}
    saved = paths.plans_dir() / "example.yaml"
    saved.write_text("name: edited\n", encoding="utf-8")
    (paths.plans_dir() / "example3.yaml").unlink()
    migrate_plans()
    assert saved.read_text(encoding="utf-8") == "name: edited\n"
    assert not (paths.plans_dir() / "example3.yaml").exists()


def test_version_local_config_is_imported_without_changing_source(data_root):
    old = data_root / "dist" / "old-version" / "data" / "example3.yaml"
    old.parent.mkdir(parents=True)
    old.write_text("name: important\n", encoding="utf-8")
    saved = persistent_config_path(old)
    assert saved == paths.plans_dir() / "example3.yaml"
    assert saved.read_bytes() == old.read_bytes()
    saved.write_text("name: new\n", encoding="utf-8")
    assert old.read_text(encoding="utf-8") == "name: important\n"
    old.unlink()
    assert persistent_config_path(old) == saved


def test_import_preserves_conflicting_names(data_root):
    saved = paths.plans_dir() / "example3.yaml"
    saved.parent.mkdir(parents=True)
    saved.write_text("name: existing\n", encoding="utf-8")
    old = data_root / "data" / "example3.yaml"
    old.parent.mkdir()
    old.write_text("name: imported\n", encoding="utf-8")
    imported = persistent_config_path(old)
    assert imported != saved and imported.parent == saved.parent
    assert imported.read_bytes() == old.read_bytes()
    assert saved.read_text(encoding="utf-8") == "name: existing\n"
    assert persistent_config_path(old) == imported


def test_explicit_external_files_are_not_relocated(data_root, tmp_path):
    external = tmp_path / "my-task.yaml"
    external.write_text("name: external\n", encoding="utf-8")
    assert persistent_config_path(external) == external


def test_build_excludes_all_yaml_from_runtime_resources(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("ddtool_build", Path(__file__).resolve().parents[1] / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    source, target = tmp_path / "source", tmp_path / "release"
    source.mkdir()
    (source / "stats.json").write_text("{}", encoding="utf-8")
    (source / "example.yaml").write_text("name: saved", encoding="utf-8")
    (source / "other.YML").write_text("name: saved", encoding="utf-8")
    build.copy_runtime_data(source, target)
    assert [path.name for path in target.iterdir()] == ["stats.json"]
