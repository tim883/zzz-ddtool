from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def handbook_dir() -> Path:
    return repo_root() / "手册"


def assets_dir() -> Path:
    return Path(__file__).resolve().parent / "assets"


def configs_dir() -> Path:
    return repo_root() / "configs"


def archives_file() -> Path:
    """权重+档位要求临时存档（所有槽位方案共用的命名模板）。"""
    return configs_dir() / "archives.json"


def out_dir() -> Path:
    return repo_root() / "out"


def captures_dir() -> Path:
    return out_dir() / "captures"


def logs_dir() -> Path:
    return out_dir() / "logs"
