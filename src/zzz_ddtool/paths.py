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


def log_dir() -> Path:
    """所有运行时产出的根目录，第一层就叫 log。

    文本和图片分开两个明确路径：
        log/ddtool.log            文本日志
        log/captures/<时间戳>/    本轮截图
    """
    return repo_root() / "log"


def captures_dir() -> Path:
    """图片日志：一轮一个子目录。"""
    return log_dir() / "captures"


def logs_dir() -> Path:
    """文本日志目录（里面是 ddtool.log）。"""
    return log_dir() / "text"


def status_file() -> Path:
    """运行状态（给界面读的临时文件）。"""
    return log_dir() / "run-status.json"


def debug_dir() -> Path:
    """调试用的零散截图（标定、探测等一次性产物）。"""
    return log_dir() / "debug"
