"""示例投放：随包示例只在首次运行时种进 ``user/plan/``，此后用户改、删都不再干预。

用户已确认的语义（方案 A）：示例是**用户自己的存档**，不是程序管理的模板。
要拿新示例走界面的「获取公共示例」，不靠更新偷偷覆盖。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from zzz_ddtool import examples, paths


def _wire(tmp_path: Path, monkeypatch):
    data = tmp_path / "data"
    (data / "examples").mkdir(parents=True)
    user = tmp_path / "user"
    plans = user / "plan"
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "plans_dir", lambda: plans)
    monkeypatch.setattr(paths, "user_dir", lambda: user)
    return data, plans


def test_shipped_examples_ignores_missing_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "nope")
    assert examples.shipped_examples() == []


def test_seed_copies_shipped_examples(tmp_path: Path, monkeypatch) -> None:
    data, plans = _wire(tmp_path, monkeypatch)
    (data / "examples" / "example.yaml").write_text("name: a", encoding="utf-8")

    copied = examples.seed_examples()

    assert [p.name for p in copied] == ["example.yaml"]
    assert (plans / "example.yaml").read_text(encoding="utf-8") == "name: a"


def test_seed_never_overwrites_existing(tmp_path: Path, monkeypatch) -> None:
    data, plans = _wire(tmp_path, monkeypatch)
    (data / "examples" / "example.yaml").write_text("SHIPPED", encoding="utf-8")
    plans.mkdir(parents=True)
    (plans / "example.yaml").write_text("MINE", encoding="utf-8")

    assert examples.seed_examples() == []
    assert (plans / "example.yaml").read_text(encoding="utf-8") == "MINE"


def test_seed_runs_once_and_does_not_resurrect_deleted(tmp_path: Path, monkeypatch) -> None:
    data, plans = _wire(tmp_path, monkeypatch)
    (data / "examples" / "example.yaml").write_text("a", encoding="utf-8")
    examples.seed_examples()

    (plans / "example.yaml").unlink()  # 用户删了
    (data / "examples" / "later.yaml").write_text("b", encoding="utf-8")  # 之后新增的示例

    assert examples.seed_examples() == []
    assert not (plans / "example.yaml").exists()  # 不复活
    assert not (plans / "later.yaml").exists()  # 只种一次；新示例走「获取公共示例」


def test_seed_recreates_missing_plan_dir(tmp_path: Path, monkeypatch) -> None:
    """plans/ 不存在或运行中被删也要能正常投放，不抛 OSError。"""
    data, plans = _wire(tmp_path, monkeypatch)
    (data / "examples" / "example.yaml").write_text("a", encoding="utf-8")
    examples.seed_examples()

    shutil.rmtree(plans)
    (tmp_path / "user" / ".examples-seeded").unlink()

    copied = examples.seed_examples()

    assert [p.name for p in copied] == ["example.yaml"]
    assert (plans / "example.yaml").exists()


def test_copy_missing_is_the_real_no_overwrite_guard(tmp_path: Path) -> None:
    """真正保护用户文件的是 ``copy_missing`` 的 `open("xb")`，而不是上层的 exists 检查。

    上面那条测的是"报不报成已复制"；这条测的是"文件内容到底会不会被改"。
    """
    from zzz_ddtool.userdata import copy_missing

    source = tmp_path / "s.yaml"
    source.write_text("SHIPPED", encoding="utf-8")
    target = tmp_path / "d.yaml"
    target.write_text("MINE", encoding="utf-8")

    copy_missing(source, target)

    assert target.read_text(encoding="utf-8") == "MINE"
