"""本地版本号：读 exe 同级（开发态=仓库根）的 version.txt。

打包脚本会从 pyproject.toml 的 version 自动写入 version.txt，
保证两处一致。文件缺失或为空时回退到内置默认（与 pyproject 一致）。
"""

from __future__ import annotations

from zzz_ddtool.paths import version_file

# 与 pyproject.toml 的 version 保持一致，仅作 version.txt 缺失时的兜底。
FALLBACK_VERSION = "0.1.0"


def current_version() -> str:
    f = version_file()
    try:
        text = f.read_text(encoding="utf-8").strip()
    except OSError:
        return FALLBACK_VERSION
    return text or FALLBACK_VERSION
