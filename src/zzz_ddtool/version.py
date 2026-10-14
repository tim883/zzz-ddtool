"""本地版本号：读 exe 同级（开发态=仓库根）的 version.txt。

打包脚本会从 pyproject.toml 的 version 自动写入 version.txt，
保证两处一致。文件缺失或为空时回退到内置默认（与 pyproject 一致）。
"""

from __future__ import annotations

from zzz_ddtool.paths import version_file

# 与 pyproject.toml 的 version 保持一致，仅作 version.txt 缺失时的兜底。
#
# 两处的一致性由 scripts/release.py 的前置检查与 tests/test_release.py 的守卫用例把住：
# build.py 只把 pyproject 的版本写进包内 version.txt，package_release.py 只核对这两者，
# **这里漏改不会被它们发现**（2026-10-14 发现它漂到了 0.1.0，落后两个版本）。
FALLBACK_VERSION = "1.0.1"


def current_version() -> str:
    f = version_file()
    try:
        text = f.read_text(encoding="utf-8").strip()
    except OSError:
        return FALLBACK_VERSION
    return text or FALLBACK_VERSION
