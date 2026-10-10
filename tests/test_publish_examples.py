"""示例发布脚本：生成 manifest.json，可选同步到另一个目录（远端）。

清单是「获取公共示例」的唯一入口：它必须与示例内容一致，否则 sha256 校验会拒绝。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("publish_examples_test", ROOT / "scripts/publish_examples.py")
pe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pe)

YAML = "name: 双暴攻击-示例\n"


def _src(tmp_path: Path) -> Path:
    src = tmp_path / "data" / "examples"
    src.mkdir(parents=True)
    return src


def test_publish_generates_manifest_in_place(tmp_path: Path) -> None:
    src = _src(tmp_path)
    (src / "example.yaml").write_text(YAML, encoding="utf-8")

    assert pe.main(["--src", str(src)]) == 0

    data = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    assert data["schema"] == 1
    entry = data["examples"][0]
    assert entry["file"] == "example.yaml"
    assert entry["title"] == "双暴攻击-示例"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["updated_at"])
    # 清单按文件**字节**算哈希；Windows 上 write_text 会把 \n 落成 \r\n，不能拿源字符串当预期
    assert entry["sha256"] == hashlib.sha256((src / "example.yaml").read_bytes()).hexdigest()


def test_publish_copies_to_dest_when_given(tmp_path: Path) -> None:
    src = _src(tmp_path)
    (src / "example.yaml").write_text(YAML, encoding="utf-8")
    dest = tmp_path / "remote" / "examples"

    assert pe.main(["--src", str(src), "--dest", str(dest)]) == 0

    assert (dest / "example.yaml").read_text(encoding="utf-8") == YAML
    assert json.loads((dest / "manifest.json").read_text(encoding="utf-8"))["examples"][0]["file"] == "example.yaml"


def test_publish_title_falls_back_to_file_stem(tmp_path: Path) -> None:
    src = _src(tmp_path)
    (src / "no-name.yaml").write_text("run:\n  max_discs: 1\n", encoding="utf-8")

    assert pe.main(["--src", str(src)]) == 0

    data = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    assert data["examples"][0]["title"] == "no-name"


def test_publish_fails_when_src_missing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        pe.main(["--src", str(tmp_path / "nope")])


def test_publish_preserves_hand_written_notes(tmp_path: Path) -> None:
    """说明是手写的，重新生成清单不能把它抹掉 —— 否则界面上的"说明"永远是空的。"""
    src = _src(tmp_path)
    (src / "example.yaml").write_text(YAML, encoding="utf-8")
    assert pe.main(["--src", str(src)]) == 0

    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    manifest["examples"][0]["note"] = "入门示例：5 件上限"
    (src / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    assert pe.main(["--src", str(src)]) == 0

    data = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    assert data["examples"][0]["note"] == "入门示例：5 件上限"
