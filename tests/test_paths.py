from zzz_ddtool import paths


def test_standalone_program_uses_its_own_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "zzz-ddtool.exe"))
    assert paths.user_dir() == tmp_path / "user"
    assert paths.plans_dir() == tmp_path / "user" / "plan"
