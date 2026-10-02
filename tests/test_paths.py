from zzz_ddtool import paths


def test_archive_location_is_independent_of_executable(tmp_path, monkeypatch):
    (tmp_path / ".zzz-ddtool-root").touch()
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "version1" / "zzz-ddtool.exe"))
    target = paths.archives_file()
    assert target == tmp_path / "user" / "archives.json"
    assert paths.legacy_archives_file() == tmp_path / "version1" / "user" / "archives.json"
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "version2" / "zzz-ddtool.exe"))
    assert paths.archives_file() == target
    assert paths.legacy_archives_file() == tmp_path / "version2" / "user" / "archives.json"


def test_standalone_program_uses_its_own_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "zzz-ddtool.exe"))
    assert paths.user_dir() == tmp_path / "user"
    assert paths.plans_dir() == tmp_path / "user" / "plan"
