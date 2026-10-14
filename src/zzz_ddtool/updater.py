"""检查更新：读 GitHub 发行版最新版本号，对比本地 version.txt。

匿名访问 GitHub REST API，不带 token（公开仓库 + 低频够用，
带 token 反而会把账号凭证发给所有用户）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from zzz_ddtool.version import current_version

# ---- 远端仓库坐标：换 GitHub 账号或仓库名时，只改这两行 --------------------------
GITHUB_REPO = "tim883/zzz-ddtool"
GITHUB_BRANCH = "master"  # examples.py 的 raw 直链据此拼出

# 用发行版「列表」端点而不是 ``/releases/latest``：仓库可见但还没有任何发行版时，
# GitHub 对 latest 也返回 404，与「仓库不存在/未公开」无法区分；列表端点返回
# 200 + []，一次请求就能把「仓库不可访问」和「暂无版本」分开。
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=10"
GITHUB_RAW = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}"


@dataclass(frozen=True)
class UpdateStatus:
    available: bool          # 是否有更新
    latest_version: str      # 远程最新版本号（无更新/出错时可能为空）
    current_version: str     # 本地版本号
    body: str = ""           # 更新说明
    data_url: str = ""       # 数据更新包 zip（约定文件名以 data 开头）
    program_url: str = ""    # 程序更新包 zip（其余 zip）
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


def _pick_urls(assets: list) -> tuple[str, str]:
    """从发行版附件里挑下载地址。

    约定：zip 文件名以 ``data`` 开头 = 数据包；其余 zip = 程序包。
    缺哪类就返回空串。
    """
    data_url = ""
    program_url = ""
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        url = str(asset.get("browser_download_url") or "")
        if not url or not name.endswith(".zip"):
            continue
        if name.startswith("data"):
            data_url = data_url or url
        else:
            program_url = program_url or url
    return data_url, program_url


def _pick_release(releases: list) -> dict | None:
    """挑最近一个正式发布：跳过 draft 与 prerelease。

    ``/releases`` 列表按 ``created_at`` 倒序返回，所以第一个非预览项就是
    ``/releases/latest`` 会给的那一个。注意 GitHub 的 ``created_at`` 取的是
    **该发行版所用提交的日期**，不是发布时刻——先发新版、几天后再给旧版补热修时
    列表顺序会反过来，补发的那版不会被识别为更新。
    """
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        return release
    return None


def check_for_update() -> UpdateStatus:
    current = current_version()
    req = urllib.request.Request(GITHUB_RELEASES_URL, headers={"User-Agent": "zzz-ddtool"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            releases = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # 列表端点只在"仓库不存在/未公开"时 404；仓库可见但没有发行版返回 200 + []
            return UpdateStatus(False, "", current, error="仓库不可访问（不存在或未公开）")
        if exc.code == 403:
            # 未认证配额 60 次/小时/IP；校园网、小区宽带等共享出口会一起打满
            remaining = str((exc.headers or {}).get("X-RateLimit-Remaining") or "")
            if remaining == "0":
                return UpdateStatus(
                    False, "", current, error="检查过于频繁，请稍后再试（同一网络匿名查询次数已用尽）"
                )
            return UpdateStatus(False, "", current, error="检查失败（HTTP 403）")
        return UpdateStatus(False, "", current, error=f"检查失败（HTTP {exc.code}）")
    except Exception as exc:  # 网络错误、超时、JSON 解析失败等
        return UpdateStatus(False, "", current, error=f"网络错误：{exc}")

    if not isinstance(releases, list):
        return UpdateStatus(False, "", current, error="发行版数据格式异常")
    release = _pick_release(releases)
    if release is None:
        return UpdateStatus(False, "", current, error="暂无可用版本")
    latest = str(release.get("tag_name") or release.get("name") or "").strip()
    body = str(release.get("body") or "")
    if not latest:
        return UpdateStatus(False, "", current, body, error="发行版缺版本号")
    data_url, program_url = _pick_urls(release.get("assets") or [])
    available = _is_newer(latest, current)
    return UpdateStatus(available, latest, current, body, data_url, program_url)


def _download(url: str, dest) -> None:
    import shutil

    req = urllib.request.Request(url, headers={"User-Agent": "zzz-ddtool"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _strip_protected(root: Path) -> list[str]:
    """删除暂存树顶层的 user/、log/、log_test/，返回被删除的条目名。

    xcopy 只把暂存树内容铺到程序目录，所以只需处理顶层。这一步是
    "更新绝不改写用户的 user/ 与 log/、log_test/" 的硬保证：即使发布方失误
    （把跑过的包拿去打 zip），用户侧也不会被覆盖。
    """
    import shutil

    removed: list[str] = []
    for name in ("user", "log", "log_test"):
        target = root / name
        if not target.exists():
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        removed.append(name)
    return removed


def _log_stripped(removed: list[str]) -> None:
    """更新包混进 user//log//log_test/ 是发布方失误：记一条日志，但不打断更新。"""
    if removed:
        from zzz_ddtool.logutil import get_logger

        get_logger(__name__).warning(
            "更新包内含 %s，已忽略：更新不会改写用户的 user/ 与 log/、log_test/",
            "、".join(removed),
        )


def apply_data_update(url: str) -> None:
    """数据更新：下载数据包 zip，剥离 user//log//log_test/ 后覆盖 data/（只碰 data）。"""
    import shutil
    import tempfile
    import zipfile

    from zzz_ddtool.paths import data_dir

    tmp = Path(tempfile.mkdtemp(prefix="zzz-data-"))
    try:
        zip_path = tmp / "data.zip"
        _download(url, zip_path)
        staged = tmp / "new"
        staged.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(staged)
        _log_stripped(_strip_protected(staged))
        data = data_dir()
        data.mkdir(parents=True, exist_ok=True)
        # 覆盖式复制：同名覆盖，不删除 data/ 里多出来的文件
        shutil.copytree(staged, data, dirs_exist_ok=True)
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

    from zzz_ddtool.paths import base_dir

    tmp = Path(tempfile.mkdtemp(prefix="zzz-prog-"))
    try:
        zip_path = tmp / "prog.zip"
        _download(url, zip_path)
        new_dir = tmp / "new"
        new_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(new_dir)
        _log_stripped(_strip_protected(new_dir))

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
