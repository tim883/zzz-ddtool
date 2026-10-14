"""示例投放：随包示例只在首次运行时种进 ``user/plan/``，此后用户改、删都不再干预。

示例的语义：示例是**用户自己的存档**，不是程序管理的模板。
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


# --- 公共示例获取（阶段二）-------------------------------------------------
# 本节新增需要 hash / json / pytest，就近导入（模块级，pytest 收集前已执行）。

import hashlib
import json

import pytest

_FETCH_BASE = "https://example.invalid/examples"
_MANIFEST_URL = f"{_FETCH_BASE}/manifest.json"
_EXAMPLE_URL = f"{_FETCH_BASE}/a.yaml"


def _manifest(items: list[dict]) -> bytes:
    return json.dumps({"schema": 1, "examples": items}).encode("utf-8")


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _patch_fetch(monkeypatch, payload_map: dict[str, bytes]) -> None:
    def fake(url: str, dest) -> None:
        Path(dest).write_bytes(payload_map[url])

    monkeypatch.setattr(examples.updater, "_download", fake)


def test_fetch_downloads_and_verifies_sha256(monkeypatch) -> None:
    content = b"name: ok\n"
    manifest = _manifest(
        [
            {
                "file": "a.yaml",
                "title": "示例",
                "updated_at": "2026-10-10",
                "note": "说明",
                "sha256": _sha(content),
            }
        ]
    )
    _patch_fetch(monkeypatch, {_MANIFEST_URL: manifest, _EXAMPLE_URL: content})

    items = examples.fetch_examples(_MANIFEST_URL)

    assert [(i.file, i.title, i.updated_at, i.content) for i in items] == [
        ("a.yaml", "示例", "2026-10-10", content)
    ]


def test_fetch_percent_encodes_non_ascii_file_name(monkeypatch) -> None:
    """文件名可以是中文，但**请求地址必须是纯 ASCII**：非 ASCII 要百分号编码。

    2026-10-14 实测：公共示例改成中文文件名后，原样拼出来的地址让 ``urllib`` 抛
    ``UnicodeEncodeError: 'ascii' codec can't encode ...``——清单取得到（它的路径
    是 ASCII），三份示例却一份都下不动。这条钉住"文件名进地址前先编码"。
    """
    from urllib.parse import quote

    content = b"name: ok\n"
    name = "example-异常攻精.yaml"
    manifest = _manifest(
        [
            {
                "file": name,
                "title": "异常攻精",
                "updated_at": "2026-10-14",
                "note": "",
                "sha256": _sha(content),
            }
        ]
    )
    encoded_url = f"{_FETCH_BASE}/{quote(name)}"
    seen: list[str] = []

    def fake(url: str, dest) -> None:
        seen.append(url)
        assert url.isascii(), f"请求地址必须纯 ASCII，实际是 {url!r}"
        Path(dest).write_bytes({_MANIFEST_URL: manifest, encoded_url: content}[url])

    monkeypatch.setattr(examples.updater, "_download", fake)

    items = examples.fetch_examples(_MANIFEST_URL)

    assert [i.file for i in items] == [name]
    assert seen == [_MANIFEST_URL, encoded_url]


def test_fetch_rejects_path_traversal_file_name(monkeypatch) -> None:
    """远端可指定任意文件名，绝不能拼出 user/plan/ 之外的路径。"""
    content = b"x"
    manifest = _manifest(
        [
            {
                "file": "../../evil.yaml",
                "title": "t",
                "updated_at": "2026-10-10",
                "note": "",
                "sha256": _sha(content),
            }
        ]
    )
    _patch_fetch(monkeypatch, {_MANIFEST_URL: manifest, _EXAMPLE_URL: content})

    # 必须由"文件名校验"拒绝，而不是因为取不到那个 URL —— 否则测试会为错误的原因通过
    with pytest.raises(examples.ExamplesError, match="文件名不合法"):
        examples.fetch_examples(_MANIFEST_URL)


def test_fetch_rejects_sha256_mismatch(monkeypatch) -> None:
    manifest = _manifest(
        [
            {
                "file": "a.yaml",
                "title": "t",
                "updated_at": "2026-10-10",
                "note": "",
                "sha256": "0" * 64,
            }
        ]
    )
    _patch_fetch(monkeypatch, {_MANIFEST_URL: manifest, _EXAMPLE_URL: b"different"})

    with pytest.raises(examples.ExamplesError):
        examples.fetch_examples(_MANIFEST_URL)


def test_fetch_rejects_bad_updated_at_and_duplicates(monkeypatch) -> None:
    content = b"x"
    entry = {
        "file": "a.yaml",
        "title": "t",
        "updated_at": "10/10/2026",
        "note": "",
        "sha256": _sha(content),
    }
    _patch_fetch(monkeypatch, {_MANIFEST_URL: _manifest([entry]), _EXAMPLE_URL: content})
    with pytest.raises(examples.ExamplesError):
        examples.fetch_examples(_MANIFEST_URL)

    good = dict(entry, updated_at="2026-10-10")
    _patch_fetch(monkeypatch, {_MANIFEST_URL: _manifest([good, good]), _EXAMPLE_URL: content})
    with pytest.raises(examples.ExamplesError):
        examples.fetch_examples(_MANIFEST_URL)


def test_fetch_rejects_wrong_schema_and_missing_fields(monkeypatch) -> None:
    _patch_fetch(monkeypatch, {_MANIFEST_URL: json.dumps({"schema": 2, "examples": []}).encode("utf-8")})
    with pytest.raises(examples.ExamplesError):
        examples.fetch_examples(_MANIFEST_URL)

    bad = _manifest([{"file": "a.yaml", "title": "t", "updated_at": "2026-10-10"}])
    _patch_fetch(monkeypatch, {_MANIFEST_URL: bad})
    with pytest.raises(examples.ExamplesError):
        examples.fetch_examples(_MANIFEST_URL)


def test_save_example_writes_plain_when_absent(tmp_path: Path, monkeypatch) -> None:
    plans = tmp_path / "user" / "plan"
    monkeypatch.setattr(paths, "plans_dir", lambda: plans)
    item = examples.ExampleItem("a.yaml", "t", "2026-10-10", "", "x" * 64, b"NEW")

    assert examples.save_example(item).name == "a.yaml"
    assert (plans / "a.yaml").read_text(encoding="utf-8") == "NEW"


def test_save_example_skips_identical_and_never_overwrites(tmp_path: Path, monkeypatch) -> None:
    plans = tmp_path / "user" / "plan"
    plans.mkdir(parents=True)
    (plans / "a.yaml").write_text("MINE", encoding="utf-8")
    monkeypatch.setattr(paths, "plans_dir", lambda: plans)
    item = examples.ExampleItem("a.yaml", "t", "2026-10-10", "", "x" * 64, b"NEW")

    saved = examples.save_example(item)

    assert saved.name == "a-2026-10-10.yaml"
    assert (plans / "a.yaml").read_text(encoding="utf-8") == "MINE"
    # 同样内容再存一次：并列文件已存在且相同 → 不重复落盘
    assert examples.save_example(item) == saved
    assert sorted(p.name for p in plans.iterdir()) == ["a-2026-10-10.yaml", "a.yaml"]


def test_save_example_leaves_no_partial_file_when_write_fails(tmp_path: Path, monkeypatch) -> None:
    """写盘失败不得留下半截 YAML，也不能破坏已有文件。"""
    plans = tmp_path / "user" / "plan"
    plans.mkdir(parents=True)
    (plans / "a.yaml").write_text("MINE", encoding="utf-8")
    monkeypatch.setattr(paths, "plans_dir", lambda: plans)
    item = examples.ExampleItem("a.yaml", "t", "2026-10-10", "", "x" * 64, b"NEW")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(examples.os, "replace", boom)

    with pytest.raises(OSError):
        examples.save_example(item)
    assert (plans / "a.yaml").read_text(encoding="utf-8") == "MINE"
    assert sorted(p.name for p in plans.iterdir()) == ["a.yaml"]


def test_save_all_examples_counts_ok_and_reports_each_failure(tmp_path: Path, monkeypatch) -> None:
    """一条失败不影响其余；失败项按名字报出来。"""
    plans = tmp_path / "user" / "plan"
    plans.mkdir(parents=True)
    # b.yaml 与它的并列目标都已存在、内容都不同 → save_example 必然拒绝
    (plans / "b.yaml").write_text("MINE", encoding="utf-8")
    (plans / "b-2026-10-10.yaml").write_text("OTHER", encoding="utf-8")
    monkeypatch.setattr(paths, "plans_dir", lambda: plans)

    good = examples.ExampleItem("a.yaml", "t", "2026-10-10", "", "x" * 64, b"NEW")
    bad = examples.ExampleItem("b.yaml", "t", "2026-10-10", "", "y" * 64, b"NEW")

    report = examples.save_all_examples([good, bad])

    assert (report.written, report.skipped) == (1, 0)
    assert len(report.problems) == 1
    assert "b.yaml" in report.problems[0]
    assert (plans / "a.yaml").read_text(encoding="utf-8") == "NEW"


def test_save_all_examples_reports_identical_as_skipped_not_saved(tmp_path: Path, monkeypatch) -> None:
    """本地内容相同=**没有落盘**，就不能报成"已保存"。

    用户场景：三份示例本地都已是最新，点「下载」后界面报"已保存 3 项"，而
    `user/plan/` 里一个文件都没动 —— 用户据此以为程序没认出他已有的文件。
    """
    plans = tmp_path / "user" / "plan"
    plans.mkdir(parents=True)
    (plans / "a.yaml").write_bytes(b"NEW")
    monkeypatch.setattr(paths, "plans_dir", lambda: plans)
    item = examples.ExampleItem("a.yaml", "t", "2026-10-10", "", "x" * 64, b"NEW")

    report = examples.save_all_examples([item])

    assert (report.written, report.skipped, report.problems) == (0, 1, ())
    assert "已保存" not in report.message()
    assert "未重复下载" in report.message()


def test_local_state_and_needs_download(tmp_path: Path, monkeypatch) -> None:
    """三种本地状态的判定口径，以及"还要不要下"只由"内容是否相同"决定。"""
    plans = tmp_path / "user" / "plan"
    plans.mkdir(parents=True)
    (plans / "same.yaml").write_bytes(b"NEW")
    (plans / "diff.yaml").write_bytes(b"MINE")
    monkeypatch.setattr(paths, "plans_dir", lambda: plans)

    same = examples.ExampleItem("same.yaml", "t", "2026-10-10", "", "a" * 64, b"NEW")
    diff = examples.ExampleItem("diff.yaml", "t", "2026-10-10", "", "b" * 64, b"NEW")
    missing = examples.ExampleItem("none.yaml", "t", "2026-10-10", "", "c" * 64, b"NEW")

    assert examples.local_state(same) == "已是最新"
    assert examples.local_state(diff) == "将并列保存"
    assert examples.local_state(missing) == "不存在"
    assert [examples.needs_download(item) for item in (same, diff, missing)] == [False, True, True]


# --- 修复轮（独立评审发现）--------------------------------------------------

def test_seed_does_not_mark_done_when_a_copy_failed(tmp_path: Path, monkeypatch) -> None:
    """首投遇到瞬时写盘失败时**不能**落下"已投放"标记，否则示例永久缺失。"""
    import zzz_ddtool.userdata as userdata

    data, plans = _wire(tmp_path, monkeypatch)
    (data / "examples" / "example.yaml").write_text("a", encoding="utf-8")
    marker = tmp_path / "user" / ".examples-seeded"

    def disk_full(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(examples, "copy_missing", disk_full)
    assert examples.seed_examples() == []
    assert not marker.exists()

    # 磁盘恢复后重试应当补上
    monkeypatch.setattr(examples, "copy_missing", userdata.copy_missing)
    assert [p.name for p in examples.seed_examples()] == ["example.yaml"]
    assert (plans / "example.yaml").exists()


def test_fetch_rejects_impossible_or_trailing_newline_date(monkeypatch) -> None:
    """Spec 要求"合法日期"：2026-13-45 与带尾随换行的都不行。"""
    content = b"x"
    for bad_date in ("2026-13-45", "2026-10-10\n", "2026-02-30"):
        manifest = _manifest(
            [
                {
                    "file": "a.yaml",
                    "title": "t",
                    "updated_at": bad_date,
                    "note": "",
                    "sha256": _sha(content),
                }
            ]
        )
        _patch_fetch(monkeypatch, {_MANIFEST_URL: manifest, _EXAMPLE_URL: content})
        with pytest.raises(examples.ExamplesError, match="时间"):
            examples.fetch_examples(_MANIFEST_URL)


def test_fetch_rejects_dot_only_or_nul_file_names(monkeypatch) -> None:
    content = b"x"
    for bad_name in (".yaml", "a\x00b.yaml", "a.yaml\n"):
        manifest = _manifest(
            [
                {
                    "file": bad_name,
                    "title": "t",
                    "updated_at": "2026-10-10",
                    "note": "",
                    "sha256": _sha(content),
                }
            ]
        )
        _patch_fetch(monkeypatch, {_MANIFEST_URL: manifest, _EXAMPLE_URL: content})
        with pytest.raises(examples.ExamplesError, match="文件名"):
            examples.fetch_examples(_MANIFEST_URL)


def test_save_all_examples_absorbs_unexpected_exception(tmp_path: Path, monkeypatch) -> None:
    """单项的意外异常也必须被收纳成"该条失败"，不能从界面槽里逃出去。"""
    plans = tmp_path / "user" / "plan"
    monkeypatch.setattr(paths, "plans_dir", lambda: plans)
    item = examples.ExampleItem("a.yaml", "t", "2026-10-10", "", "x" * 64, b"NEW")

    def boom(*args, **kwargs):
        raise ValueError("bad path")

    monkeypatch.setattr(examples, "_atomic_write", boom)

    report = examples.save_all_examples([item])

    assert (report.written, report.skipped) == (0, 0)
    assert len(report.problems) == 1
    assert "bad path" in report.problems[0]
