"""发布入口脚本：工作区拦截、示例提交与消息选择的离线用例。

不碰真实 git 与网络：所有 git / 子进程调用用替身隔离；被测的是编排决策本身
（哪些路径算"发布之外"、何时拦截、提交什么消息、dry-run 是否真的不写）。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release_entry_test", ROOT / "scripts/release.py")
rel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rel)

MP = pytest.MonkeyPatch


def _fake_git(status: str = "", staged: str = ""):
    """按参数分发：status → 给定 porcelain 文本，diff --cached → 给定文件名列表。"""

    def fake(*args: str) -> str:
        if args[:2] == ("status", "--porcelain"):
            return status
        if args[:3] == ("diff", "--cached", "--name-only"):
            return staged
        raise AssertionError(f"未预期的 git 调用: {args}")

    return fake


# --- 路径分类 -----------------------------------------------------------------


def test_inside_examples_rejects_lookalike_prefixes() -> None:
    assert rel._inside_examples("data/examples/example.yaml")
    assert rel._inside_examples("data/examples")
    assert not rel._inside_examples("data/examplesX/fake.yaml")
    assert not rel._inside_examples("data/examples-old/template.yaml")
    assert not rel._inside_examples("data")


def test_changed_paths_splits_rename_and_unquotes(monkeypatch: MP) -> None:
    porcelain = ' M data/examples/example.yaml\nR  src/old.py -> src/new.py\n?? "带 空格.md"\n'
    monkeypatch.setattr(rel, "_git", lambda *a: porcelain)

    paths = rel._changed_paths()

    assert "data/examples/example.yaml" in paths
    assert "src/old.py" in paths and "src/new.py" in paths
    assert "带 空格.md" in paths


def test_changed_paths_renamed_into_examples_is_inside(monkeypatch: MP) -> None:
    """rename 两端都参与判断：从外面改进来，外面那一端仍算"发布之外"。"""
    porcelain = "R  src/draft.yaml -> data/examples/draft.yaml\n"
    monkeypatch.setattr(rel, "_git", lambda *a: porcelain)

    outside = [p for p in rel._changed_paths() if not rel._inside_examples(p)]

    assert outside == ["src/draft.yaml"]


# --- 工作区拦截 ---------------------------------------------------------------


def test_check_worktree_strict_blocks_outside(monkeypatch: MP) -> None:
    monkeypatch.setattr(rel, "_git", lambda *a: " M src/gui.py\n M data/examples/example.yaml\n")

    with pytest.raises(rel.ReleaseError) as excinfo:
        rel._check_worktree(strict=True)

    message = str(excinfo.value)
    assert "src/gui.py" in message
    assert "data/examples/example.yaml" not in message


def test_check_worktree_loose_only_warns(monkeypatch: MP, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(rel, "_git", lambda *a: " M src/gui.py\n")

    rel._check_worktree(strict=False)  # 不应抛错

    out = capsys.readouterr().out
    assert "src/gui.py" in out
    assert "提醒" in out


def test_check_worktree_clean_passes(monkeypatch: MP, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(rel, "_git", lambda *a: "")

    rel._check_worktree(strict=True)

    assert "通过" in capsys.readouterr().out


# --- 示例提交 -----------------------------------------------------------------


def test_commit_message_prefers_examples_update_when_yaml_changed(monkeypatch: MP) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        rel,
        "_git",
        _fake_git(status=" M data/examples/example.yaml\n", staged="example.yaml\nmanifest.json\n"),
    )
    monkeypatch.setattr(rel, "_git_visible", lambda *a: calls.append(a))

    rel._commit_examples(dry_run=False)

    commits = [c for c in calls if c[0] == "commit"]
    assert len(commits) == 1
    assert "docs(examples): 更新公共示例" in commits[0]
    assert calls[0][:2] == ("add", "--")


def test_commit_message_is_manifest_sync_when_only_manifest(monkeypatch: MP) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        rel,
        "_git",
        _fake_git(status=" M data/examples/manifest.json\n", staged="manifest.json\n"),
    )
    monkeypatch.setattr(rel, "_git_visible", lambda *a: calls.append(a))

    rel._commit_examples(dry_run=False)

    commits = [c for c in calls if c[0] == "commit"]
    assert len(commits) == 1
    assert "chore(examples): 同步示例清单" in commits[0]


def test_commit_skips_when_examples_clean(monkeypatch: MP) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(rel, "_git", _fake_git(status=""))
    monkeypatch.setattr(rel, "_git_visible", lambda *a: calls.append(a))

    rel._commit_examples(dry_run=False)

    assert calls == []


def test_commit_dry_run_never_writes(monkeypatch: MP) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(rel, "_git", _fake_git(status=" M data/examples/example.yaml\n"))
    monkeypatch.setattr(rel, "_git_visible", lambda *a: calls.append(a))

    rel._commit_examples(dry_run=True)

    assert calls == []


# --- 输出目录与预演总结 -------------------------------------------------------


def test_resolve_outdir_default_is_dated() -> None:
    out = rel._resolve_outdir(None)

    assert re.fullmatch(r"发布-\d{8}", out.name)
    assert out.parent.name == "dist"


def test_resolve_outdir_relative_anchors_to_repo_root() -> None:
    assert rel._resolve_outdir("dist/发布-custom") == rel.ROOT / "dist" / "发布-custom"


def test_summary_dry_run_marks_no_writes(capsys: pytest.CaptureFixture) -> None:
    rel._print_summary("examples", rel.ROOT / "dist", "master", "", dry_run=True)

    out = capsys.readouterr().out
    assert "预演结束" in out
    assert "发布完成" not in out
