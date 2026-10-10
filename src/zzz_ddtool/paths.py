from __future__ import annotations

import sys
from pathlib import Path


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def base_dir() -> Path:
    """程序运行根：开发态=仓库根，打包态=exe 同级目录。"""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """程序自带数据（套装名/词条名 json、套装图）。更新时可覆盖。"""
    return base_dir() / "data"


def user_dir() -> Path:
    """根目录下的用户数据，独立于 dist 中的程序版本。"""
    return user_data_root() / "user"


def user_data_root() -> Path:
    if not _is_frozen():
        return repo_root()
    app = base_dir()
    for parent in (app, *app.parents):
        if (parent / ".zzz-ddtool-root").is_file():
            return parent
    return app


def plans_dir() -> Path:
    return user_dir() / "plan"


def legacy_plan_dirs() -> tuple[Path, ...]:
    return tuple(dict.fromkeys((user_data_root() / "data", base_dir() / "data", user_dir() / "presets", base_dir() / "user" / "presets")))


def version_file() -> Path:
    """本地版本号文件，exe 同级（开发态=仓库根）。打包脚本负责写入。"""
    return base_dir() / "version.txt"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def handbook_dir() -> Path:
    return repo_root() / "手册"


def assets_dir() -> Path:
    return Path(__file__).resolve().parent / "assets"


def last_config_file() -> Path:
    """记着「上次用的是哪份 yaml」，下次启动直接打开它。用户数据，更新永不碰。"""
    return user_dir() / "last-config.json"


def layout_file() -> Path:
    """界面布局记忆（窗口大小/位置、三栏比例、置顶）。用户数据，更新永不碰。"""
    return user_dir() / "layout.json"


def log_settings_file() -> Path:
    """日志设置记忆（日志级别 + 截图张数上限）。用户数据，更新永不碰。"""
    return user_dir() / "log-settings.json"


def log_dir() -> Path:
    """所有运行时产出的根目录，第一层就叫 log。

    与 user/ 用同一条定位规则（``user_data_root()``）：开发态=仓库根/log；
    打包态=公共根/log，周围没有根标记时回退到 exe 同级/log。
    这样把包放进新的版本子目录、删掉旧版本目录，日志仍然保留。
    运行时产出，不随包分发。文本和图片分开两个明确路径：

        log/text/ddtool.log       文本日志
        log/captures/<时间戳>/    本轮截图
    """
    return user_data_root() / "log"


def captures_dir() -> Path:
    """图片日志：一轮一个子目录。"""
    return log_dir() / "captures"


def logs_dir() -> Path:
    """文本日志目录（里面是 ddtool.log）。"""
    return log_dir() / "text"


def status_file() -> Path:
    """运行状态（给界面读的临时文件）。"""
    return log_dir() / "run-status.json"


def stats_dir() -> Path:
    """运行统计（一轮一个 json）。不放 captures 下，免得跟截图回收搅在一起。"""
    return log_dir() / "stats"


def debug_dir() -> Path:
    """调试用的零散截图（标定、探测等一次性产物）。"""
    return log_dir() / "debug"
