"""生成示例清单 ``manifest.json``，并可同步到远端目录。

``manifest.json`` 是「获取公共示例」的唯一入口：``examples.EXAMPLES_MANIFEST_URL``
指向本仓库 ``data/examples/manifest.json``，所以清单必须与示例内容一致
（程序侧会逐个核对 sha256，不一致就拒绝落盘）。

默认**就地**生成（源目录即输出目录）；给 ``--dest`` 时才把示例与清单复制过去。

``note``（界面上显示的"说明"）由开发者手写，**重新生成时会按文件名保留**；
脚本不猜文案，只在没有既有说明时留空。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT / "data" / "examples"
SCHEMA = 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成示例清单并同步到远端目录")
    parser.add_argument("--src", default=str(DEFAULT_SRC), help="示例来源目录")
    parser.add_argument("--dest", default=None, help="同步目标目录；默认与源同目录（就地生成清单）")
    return parser.parse_args(argv)


def _fail(message: str) -> None:
    print(f"示例发布中止：{message}", file=sys.stderr)
    raise SystemExit(2)


def _read_title(path: Path) -> str:
    """取 YAML 顶层 ``name``；读不出来就退回文件名主干。"""
    try:
        import yaml

        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=loader) or {}
        name = str(data.get("name") or "").strip() if isinstance(data, dict) else ""
    except Exception:
        name = ""
    return name or path.stem


def _load_notes(manifest_path: Path) -> dict[str, str]:
    """读回既有清单里手写的说明，按文件名索引。"""
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    notes: dict[str, str] = {}
    for entry in payload.get("examples") or []:
        if isinstance(entry, dict) and entry.get("file"):
            note = str(entry.get("note") or "")
            if note:
                notes[str(entry["file"])] = note
    return notes


def _build_manifest(src: Path, notes: dict[str, str]) -> dict:
    examples = []
    for path in sorted(src.glob("*.yaml")):
        examples.append(
            {
                "file": path.name,
                "title": _read_title(path),
                "updated_at": date.fromtimestamp(path.stat().st_mtime).isoformat(),
                "note": notes.get(path.name, ""),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {"schema": SCHEMA, "examples": examples}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    src = Path(args.src).resolve()
    if not src.is_dir():
        _fail(f"示例来源目录不存在：{src}")
    dest = Path(args.dest).resolve() if args.dest else src
    dest.mkdir(parents=True, exist_ok=True)

    if dest != src:
        for path in sorted(src.glob("*.yaml")):
            shutil.copy2(path, dest / path.name)

    manifest_path = dest / "manifest.json"
    manifest = _build_manifest(src, _load_notes(manifest_path))
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"清单：{manifest_path}（{len(manifest['examples'])} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
