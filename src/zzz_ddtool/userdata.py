"""Keep saved task plans outside versioned program directories."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from zzz_ddtool import paths


def _copy_missing(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination = target.open("xb")
    except FileExistsError:
        return
    try:
        with destination, source.open("rb") as stream:
            shutil.copyfileobj(stream, destination)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _import_plan(source: Path) -> Path:
    target = paths.plans_dir() / source.name
    if target.exists() and source.exists() and target.read_bytes() != source.read_bytes():
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
        target = target.with_name(f"{source.stem}-import-{digest}{source.suffix}")
    if source.exists():
        _copy_missing(source, target)
    return target


def migrate_plans() -> None:
    marker = paths.user_dir() / ".plans-imported"
    if marker.exists():
        return
    for directory in paths.legacy_plan_dirs():
        if directory.is_dir() and directory.resolve() != paths.plans_dir().resolve():
            for source in sorted(directory.iterdir()):
                if source.is_file() and source.suffix.lower() in {".yaml", ".yml"}:
                    _import_plan(source)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def persistent_config_path(path: Path) -> Path:
    """Relocate version-local YAML; explicitly chosen external files stay external."""
    source = path.resolve()
    if source.is_relative_to(paths.plans_dir().resolve()):
        return path
    legacy = source.parent in {directory.resolve() for directory in paths.legacy_plan_dirs()}
    version_local = source.is_relative_to(paths.user_data_root().resolve())
    old_package = source.parent.name == "data" and (source.parent.parent / "zzz-ddtool.exe").is_file()
    if source.suffix.lower() in {".yaml", ".yml"} and (legacy or version_local or old_package):
        return _import_plan(source)
    return path
