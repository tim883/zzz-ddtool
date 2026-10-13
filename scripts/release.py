#!/usr/bin/env python
"""发布唯一入口：一次发布 = 一条命令，步骤固定，不会漏。

三个场景（位置参数）：

- ``examples``：只更新公共示例 —— 生成清单 → 入库 → 推送（不打包）
- ``program`` ：发程序版本 —— 生成清单 → 入库 → 打包 → 冒烟 → 打 zip → 推送
- ``data``    ：只发数据包 —— 同 ``program``，zip 只出数据包

固定步骤（每个场景都带、每次都执行）：

1. **生成示例清单**不是检查项、不是补救项，而是发布流程的固定步骤——
   无条件重跑 ``scripts/publish_examples.py``，清单永远跟着 ``data/examples/``
   走，不存在"忘了跑"的窗口。
2. ``data/examples/`` 里未提交的改动由本脚本负责入库（随发布一起提交）。
3. ``program`` / ``data`` 场景要求 ``data/examples/`` **之外**的工作区干净：
   包内容是磁盘快照，必须与推送的提交一致，否则包与仓库对不上。
   ``examples`` 场景只推仓库、不打包，不做硬性拦截（只提醒）。
4. 推送后自动核对远端 hash（``git ls-remote``），一致才算发布完成。

约定入口：**要推仓库 = 跑本脚本**（见仓库根 ``AGENTS.md``）。细节见
``docs/技术/打包与发布.md``。

用法::

    .venv\\Scripts\\python.exe scripts/release.py examples
    .venv\\Scripts\\python.exe scripts/release.py program
    .venv\\Scripts\\python.exe scripts/release.py data --outdir dist/发布-20261013
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
EXAMPLES_DIR = "data/examples"
REMOTE = "gitee"
PKG_NAME = "zzz-ddtool"


class ReleaseError(Exception):
    """发布中止。消息面向用户，调用方直接展示。"""


# --- git 与命令执行 -----------------------------------------------------------


def _git(*args: str) -> str:
    """跑一条只读 git 命令，返回 stdout（UTF-8）。"""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout


def _git_visible(*args: str) -> None:
    """跑一条需要展示输出的 git 命令（add / commit / push），输出直通。"""
    code = subprocess.run(["git", *args], cwd=ROOT).returncode
    if code != 0:
        raise ReleaseError(f"git {' '.join(args)} 失败（退出码 {code}）")


def _run(cmd: list[str]) -> None:
    """跑一个子脚本，输出直通；失败抛 ReleaseError。"""
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    code = subprocess.run(cmd, cwd=ROOT, env=env).returncode
    if code != 0:
        raise ReleaseError(f"{Path(cmd[1]).name} 以退出码 {code} 结束")


def _current_branch() -> str:
    branch = _git("branch", "--show-current").strip()
    if not branch:
        raise ReleaseError("当前不在任何分支上（detached HEAD），无法发布。")
    return branch


# --- 步骤 ---------------------------------------------------------------------


def _changed_paths() -> list[str]:
    """工作区未提交 / 未跟踪的路径清单（rename 两端都算）。

    解析不出来的条目按"在 data/examples/ 之外"处理（保守拦截，宁可拦错）。
    """
    paths: list[str] = []
    for line in _git("status", "--porcelain").splitlines():
        if len(line) < 4:
            continue
        rest = line[3:].strip()
        if " -> " in rest:
            left, right = rest.split(" -> ", 1)
            paths.extend([left.strip('"'), right.strip('"')])
        else:
            paths.append(rest.strip('"'))
    return paths


def _inside_examples(path: str) -> bool:
    return path == EXAMPLES_DIR or path.startswith(EXAMPLES_DIR + "/")


def _check_worktree(strict: bool) -> None:
    outside = [p for p in _changed_paths() if not _inside_examples(p)]
    if not outside:
        print("工作区检查通过：data/examples/ 之外无未提交改动。")
        return
    if strict:
        listing = "\n".join(f"  - {p}" for p in outside)
        raise ReleaseError(
            "工作区有 data/examples/ 之外的未提交改动，包内容会与推送的提交不一致。\n"
            f"先提交或处理这些改动，再发布：\n{listing}"
        )
    print("提醒：data/examples/ 之外还有未提交改动（本次只发布示例，不会被推送）：")
    for path in outside:
        print(f"  - {path}")


def _generate_manifest(dry_run: bool) -> None:
    if dry_run:
        print("将重新生成示例清单（scripts/publish_examples.py）")
        return
    _run([sys.executable, str(SCRIPTS / "publish_examples.py")])


def _commit_examples(dry_run: bool) -> None:
    status = _git("status", "--porcelain", "--", EXAMPLES_DIR).strip()
    if not status:
        print("data/examples/ 无变化，跳过提交。")
        return
    if dry_run:
        print("将把 data/examples/ 下的改动（示例 + 清单）提交入库")
        return
    _git_visible("add", "--", EXAMPLES_DIR)
    staged = _git("diff", "--cached", "--name-only", "--", EXAMPLES_DIR).splitlines()
    has_yaml = any(name.endswith((".yaml", ".yml")) for name in staged)
    message = "docs(examples): 更新公共示例" if has_yaml else "chore(examples): 同步示例清单"
    _git_visible("commit", "-m", message, "--", EXAMPLES_DIR)


def _build(dry_run: bool, outdir: Path) -> None:
    if dry_run:
        print(f"将打包：build.py --outdir {outdir}")
        return
    _run([sys.executable, str(ROOT / "build.py"), "--outdir", str(outdir)])


def _smoke(dry_run: bool, pkg: Path) -> None:
    exe = pkg / f"{PKG_NAME}.exe"
    if dry_run:
        print(f"将冒烟：smoke_exe.py {exe}")
        return
    _run([sys.executable, str(SCRIPTS / "smoke_exe.py"), str(exe)])


def _make_zip(dry_run: bool, pkg: Path, outdir: Path, only: str) -> None:
    if dry_run:
        print(f"将打 zip：package_release.py --only {only}")
        return
    _run(
        [
            sys.executable,
            str(SCRIPTS / "package_release.py"),
            "--pkg",
            str(pkg),
            "--outdir",
            str(outdir),
            "--only",
            only,
        ]
    )


def _push(dry_run: bool, branch: str) -> None:
    if dry_run:
        print(f"将推送：git push {REMOTE} {branch}")
        return
    _git_visible("push", REMOTE, branch)


def _verify_remote(dry_run: bool, branch: str) -> str:
    local = _git("rev-parse", "HEAD").strip()
    if dry_run:
        print(f"将核对：远端 {REMOTE}/{branch} 与本地 {local[:12]}")
        return local
    out = _git("ls-remote", "--heads", REMOTE, branch).strip()
    if not out:
        raise ReleaseError(f"远端 {REMOTE} 上没有分支 {branch}，推送可能未生效。")
    remote = out.split()[0]
    if remote != local:
        raise ReleaseError(f"远端 hash 与本地不一致：本地 {local[:12]} / 远端 {remote[:12]}")
    print(f"核对通过：{REMOTE}/{branch} = {local[:12]}")
    return local


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


# --- 入口 ---------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="发布唯一入口：一次发布 = 一条命令，步骤固定（含生成示例清单），不会漏。",
        epilog="示例：python scripts/release.py examples | program | data",
    )
    parser.add_argument("action", choices=("examples", "program", "data"), help="发布场景")
    parser.add_argument(
        "--outdir",
        default=None,
        help="发布输出目录（program / data 用），默认 dist/发布-<当天日期>",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将执行的步骤与检查结果，不写入任何东西",
    )
    return parser.parse_args(argv)


def _resolve_outdir(raw: str | None) -> Path:
    if raw:
        candidate = Path(raw)
        return candidate if candidate.is_absolute() else ROOT / candidate
    return ROOT / "dist" / f"发布-{date.today().strftime('%Y%m%d')}"


def _print_summary(scenario: str, outdir: Path, branch: str, pushed_hash: str, dry_run: bool) -> None:
    if dry_run:
        print("\n──────── 预演结束（未写入任何东西）────────")
        print("正式发布：去掉 --dry-run 重新运行本命令。")
        return
    print("\n──────── 发布完成 ────────")
    print(f"远端 {REMOTE}/{branch} = {pushed_hash[:12]}")
    if scenario == "program":
        version = _project_version()
        print(f"程序包：{outdir / f'{PKG_NAME}-v{version}.zip'}")
        print(f"下一步：Gitee 网页 →「发行版」→ 新建发行版（标签 v{version}），上传 zip、填写更新说明。")
    elif scenario == "data":
        version = _project_version()
        print(f"数据包：{outdir / 'data.zip'}")
        print(f"下一步：Gitee 网页 →「发行版」→ 新建发行版（标签 v{version}），上传 data.zip、填写更新说明。")
    else:
        print("公共示例已随仓库推送，用户端「获取公共示例」即可取到最新清单。")


def main(argv: list[str] | None = None) -> int:
    # 输出被管道捕获时父进程是块缓冲，子脚本先 flush 会显成乱序；改行缓冲让每步即时按序显示
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = _parse_args(argv)
    scenario = args.action
    dry_run = args.dry_run
    packing = scenario in ("program", "data")
    strict = packing
    outdir = _resolve_outdir(args.outdir)
    pkg = outdir / PKG_NAME
    only = "program" if scenario == "program" else "data"

    try:
        branch = _current_branch()
    except ReleaseError as exc:
        print(f"✘ 发布中止：{exc}")
        return 2

    steps: list[tuple[str, object]] = [
        ("前置检查", lambda: _check_worktree(strict)),
        ("生成示例清单", lambda: _generate_manifest(dry_run)),
        ("提交示例改动", lambda: _commit_examples(dry_run)),
    ]
    if packing:
        steps += [
            ("打包", lambda: _build(dry_run, outdir)),
            ("冒烟", lambda: _smoke(dry_run, pkg)),
            ("打 zip", lambda: _make_zip(dry_run, pkg, outdir, only)),
        ]
    steps += [
        ("推送", lambda: _push(dry_run, branch)),
        ("核对远端", lambda: _verify_remote(dry_run, branch)),
    ]

    pushed_hash = ""
    for index, (name, step) in enumerate(steps, 1):
        print(f"\n── [{index}/{len(steps)}] {name} ──")
        try:
            result = step()  # type: ignore[operator]
        except ReleaseError as exc:
            print(f"\n✘ 发布中止（停在第 {index} 步「{name}」）：{exc}")
            print("  处理后重新运行本命令即可。")
            return 2
        except Exception as exc:  # noqa: BLE001 - 脚本边界，意外错误也要友好收场
            print(f"\n✘ 意外错误（停在第 {index} 步「{name}」）：{type(exc).__name__}: {exc}")
            print("  处理后重新运行本命令即可。")
            return 2
        if name == "核对远端" and isinstance(result, str):
            pushed_hash = result

    _print_summary(scenario, outdir, branch, pushed_hash, dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
