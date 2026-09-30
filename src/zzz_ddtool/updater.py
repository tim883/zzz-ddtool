"""检查更新：读 Gitee 发行版最新版本号，对比本地 version.txt。

匿名访问 Gitee OpenAPI v5，不带 token（公开仓库 + 低频够用，
带 token 反而会把账号凭证发给所有用户）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from zzz_ddtool.version import current_version

GITEE_LATEST_URL = "https://gitee.com/api/v5/repos/qzl2077/zzz-ddtool/releases/latest"


@dataclass(frozen=True)
class UpdateStatus:
    available: bool          # 是否有更新
    latest_version: str      # 远程最新版本号（无更新/出错时可能为空）
    current_version: str     # 本地版本号
    body: str = ""           # 更新说明
    error: str | None = None # 出错信息（网络错误 / 无发行版等）


def _parse_version(v: str) -> tuple[int, ...]:
    v = v.strip().lstrip("vV")
    parts: list[int] = []
    for seg in v.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def check_for_update() -> UpdateStatus:
    current = current_version()
    req = urllib.request.Request(GITEE_LATEST_URL, headers={"User-Agent": "zzz-ddtool"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return UpdateStatus(False, "", current, error="暂无可用版本")
        return UpdateStatus(False, "", current, error=f"检查失败（HTTP {exc.code}）")
    except Exception as exc:  # 网络错误、超时、JSON 解析失败等
        return UpdateStatus(False, "", current, error=f"网络错误：{exc}")

    latest = str(data.get("tag_name") or data.get("name") or "").strip()
    body = str(data.get("body") or "")
    if not latest:
        return UpdateStatus(False, "", current, body, error="发行版缺版本号")
    available = _is_newer(latest, current)
    return UpdateStatus(available, latest, current, body)


def _download(url: str, dest) -> None:
    import shutil

    req = urllib.request.Request(url, headers={"User-Agent": "zzz-ddtool"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def apply_data_update(url: str) -> None:
    """数据更新：下载数据包 zip，解压覆盖 data/（只碰 data，不碰 user）。"""
    import shutil
    import tempfile
    import zipfile
    from pathlib import Path

    from zzz_ddtool.paths import data_dir

    tmp = Path(tempfile.mkdtemp(prefix="zzz-data-"))
    try:
        zip_path = tmp / "data.zip"
        _download(url, zip_path)
        data = data_dir()
        data.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(data)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def apply_program_update(url: str) -> None:
    """程序更新：下载完整包 zip，解压到临时目录，写替身 bat 替换后重启。

    主程序退出后，替身 bat 等待 2 秒、覆盖程序目录、拉起新 exe、删自己。
    （初版，需真机验证）
    """
    import shutil
    import subprocess
    import tempfile
    import zipfile
    from pathlib import Path

    from zzz_ddtool.paths import base_dir

    tmp = Path(tempfile.mkdtemp(prefix="zzz-prog-"))
    try:
        zip_path = tmp / "prog.zip"
        _download(url, zip_path)
        new_dir = tmp / "new"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(new_dir)

        app_dir = base_dir()
        bat = app_dir / "_update.bat"
        bat.write_text(
            "@echo off\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'xcopy /E /I /Y "{new_dir}" "{app_dir}"\r\n'
            f'start "" "{app_dir / "zzz-ddtool.exe"}"\r\n'
            f'del "%~f0"\r\n',
            encoding="ascii",
        )
        subprocess.Popen(["cmd", "/c", str(bat)])
        raise SystemExit(0)
    finally:
        # 注意：不能删 tmp，替身 bat 还要从 new_dir 拷贝
        pass
