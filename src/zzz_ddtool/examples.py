"""随包示例的投放，以及从远端取公共示例。

示例的语义：示例是**用户自己的存档**，不是程序管理的模板。
启动时只把包里缺的示例种进 ``user/plan/``，此后用户改、删都不再干预；要拿新
示例走界面「获取公共示例」的主动通道，它同样从不覆盖本地文件，也**不把已经
是最新内容的再要一遍**（本地状态判定见 ``local_state``）。

「只种一次」用 ``user/.examples-seeded`` 标记，与 ``userdata.migrate_plans()``
的 ``.plans-imported`` 是两条独立的生命周期。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

from zzz_ddtool import paths, updater
from zzz_ddtool.userdata import copy_missing

SEEDED_MARKER = ".examples-seeded"


def shipped_examples() -> list[Path]:
    """包里自带的示例：``data/examples/*.yaml``，按文件名排序。"""
    source = paths.data_dir() / "examples"
    if not source.is_dir():
        return []
    return sorted(source.glob("*.yaml"))


def seed_examples() -> list[Path]:
    """把尚未种下的示例复制进 ``user/plan/``，返回本次新复制的目标路径。

    标记存在即直接返回：用户删掉的示例不复活，用户改过的示例不覆盖。
    磁盘失败不抛给界面 —— 投放失败不该让客户端起不来；但**只要有失败的项就不写
    "已投放"标记**，否则一次瞬时失败（磁盘满、权限、杀软占用）会让示例永久缺失。
    """
    marker = paths.user_dir() / SEEDED_MARKER
    if marker.exists():
        return []

    copied: list[Path] = []
    failed = False
    for source in shipped_examples():
        target = paths.plans_dir() / source.name
        try:
            if target.exists():
                continue
            copy_missing(source, target)
        except OSError:
            failed = True
            continue
        copied.append(target)

    if not failed:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch(exist_ok=True)
        except OSError:
            pass
    return copied


# --- 公共示例：从远端清单拉取 -------------------------------------------------
# 远端就是本仓库自身（已公开），清单与示例同目录：
#   .../raw/master/data/examples/manifest.json
#   .../raw/master/data/examples/<file>

EXAMPLES_MANIFEST_URL = f"{updater.GITHUB_RAW}/data/examples/manifest.json"

_MANIFEST_SCHEMA = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ExamplesError(Exception):
    """远端清单或示例不可用。消息面向用户，调用方直接展示。"""


@dataclass(frozen=True)
class ExampleItem:
    file: str
    title: str
    updated_at: str
    note: str
    sha256: str
    content: bytes


# 本地状态：界面列表与「要不要下载」共用同一处判定，避免各写一遍比较口径。
STATE_MISSING = "不存在"
STATE_LATEST = "已是最新"
STATE_PARALLEL = "将并列保存"
STATE_UNKNOWN = "未知"


def local_state(item: ExampleItem) -> str:
    """``user/plan/`` 里这份示例相对远端内容的状态。

    只比字节：本地与远端同内容才算"已是最新"（这就是"不用再下"的唯一条件）。
    """
    target = paths.plans_dir() / item.file
    try:
        if not target.exists():
            return STATE_MISSING
        return STATE_LATEST if target.read_bytes() == item.content else STATE_PARALLEL
    except OSError:
        return STATE_UNKNOWN


def needs_download(item: ExampleItem) -> bool:
    """还需不需要下载：已经是本地最新内容的**不再要求下载**。

    ``未知``（本地读不出来）算需要处理 —— 交给 ``save_example`` 去碰真实文件、
    按它的规则报错，而不是在这里当成功跳过。
    """
    return local_state(item) != STATE_LATEST


def _is_plain_yaml_name(name: str) -> bool:
    """只接受"纯文件名 + ``.yaml``"。

    这条是路径穿越的唯一闸门：挡掉 ``../``、``..\\``、绝对路径、盘符、NUL，
    也挡掉 ``.yaml`` 这种只有后缀、没有主名的名字。
    """
    if not name or "\x00" in name or Path(name).name != name:
        return False
    return Path(name).stem not in ("", ".") and Path(name).suffix == ".yaml"


def _is_calendar_date(text: str) -> bool:
    """必须是真实存在的日期 —— 只对形状做正则不够（``2026-13-45`` 也长得像日期）。"""
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _parse_manifest(payload: object) -> list[dict]:
    """先把整份清单校验完 —— 非法清单不得触发任何下载或落盘。"""
    if not isinstance(payload, dict) or payload.get("schema") != _MANIFEST_SCHEMA:
        raise ExamplesError("远端示例清单版本不认识（schema 不匹配）")
    raw = payload.get("examples")
    if not isinstance(raw, list):
        raise ExamplesError("远端示例清单缺少 examples 列表")

    entries: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ExamplesError("远端示例清单里有非法条目")
        name = str(entry.get("file") or "")
        updated_at = str(entry.get("updated_at") or "")
        digest = str(entry.get("sha256") or "")
        if not _is_plain_yaml_name(name):
            raise ExamplesError(f"远端示例清单里的文件名不合法：{name!r}")
        if not _is_calendar_date(updated_at):
            raise ExamplesError(f"远端示例清单里的时间不合法：{name} -> {updated_at!r}")
        if not _SHA256_RE.fullmatch(digest):
            raise ExamplesError(f"远端示例清单里的 sha256 不合法：{name}")
        if name in seen:
            raise ExamplesError(f"远端示例清单里 {name} 重复")
        seen.add(name)
        entries.append(
            {
                "file": name,
                "title": str(entry.get("title") or Path(name).stem),
                "updated_at": updated_at,
                "note": str(entry.get("note") or ""),
                "sha256": digest,
            }
        )
    return entries


def fetch_examples(url: str = EXAMPLES_MANIFEST_URL) -> list[ExampleItem]:
    """下载并校验远端清单与每个示例。

    任何问题都抛 ``ExamplesError``，且**不写入任何文件**（全部在临时目录里做完）。
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="zzz-examples-"))
    try:
        manifest_path = tmp / "manifest.json"
        try:
            updater._download(url, manifest_path)
        except Exception as exc:
            raise ExamplesError(f"取不到远端示例清单：{exc}") from exc
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ExamplesError(f"远端示例清单读不出来：{exc}") from exc

        entries = _parse_manifest(payload)
        base = url.rsplit("/", 1)[0]
        items: list[ExampleItem] = []
        for entry in entries:
            blob = tmp / entry["file"]
            # 示例名可以是中文，但 HTTP 请求行只认 ASCII：路径段必须先百分号编码，
            # 否则 urllib 直接抛 UnicodeEncodeError，清单取得到、示例却一份都下不动
            # （2026-10-14 实测）。文件名由 _is_plain_yaml_name 保证不含 "/"，
            # quote 不会跨段编码，拼不出别的路径。
            try:
                updater._download(f"{base}/{quote(entry['file'])}", blob)
            except Exception as exc:
                raise ExamplesError(f"取不到示例 {entry['file']}：{exc}") from exc
            content = blob.read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ExamplesError(f"示例 {entry['file']} 的内容与清单不符")
            items.append(ExampleItem(content=content, **entry))
        return items
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def save_example(item: ExampleItem) -> Path:
    """把示例落到 ``user/plan/``，**从不覆盖**已有的同名文件。

    - 目标不存在 → 写 ``<file>``
    - 目标已存在且内容相同 → 不重复落盘
    - 目标已存在但内容不同 → 并列写 ``<stem>-<updated_at><ext>``，原文件不动
    """
    return _place_example(item)[0]


def _place_example(item: ExampleItem) -> tuple[Path, bool]:
    """同 ``save_example``，另报"这次到底写没写盘"（内容相同就是没写）。"""
    plans = paths.plans_dir()
    plans.mkdir(parents=True, exist_ok=True)
    target = plans / item.file
    if target.exists():
        if target.read_bytes() == item.content:
            return target, False
        target = plans / f"{target.stem}-{item.updated_at}{target.suffix}"
        if target.exists():
            if target.read_bytes() == item.content:
                return target, False
            raise ExamplesError(f"{target.name} 已存在且内容不同，未覆盖")
    _atomic_write(target, item.content)
    return target, True


def _atomic_write(target: Path, content: bytes) -> None:
    """先写同目录临时文件再改名 —— 中途失败不留半截文件，也不动已有文件。"""
    tmp = target.with_name(f".{target.name}.part")
    try:
        with open(tmp, "wb") as stream:
            stream.write(content)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class SaveReport:
    """一次"下载勾选项"的结果：真写了几份、几份已经是最新、哪些没保存。

    ``skipped`` 单独计数是因为**内容相同本来就不落盘**：把它算进"已保存"会让界面
    报出一次并不存在的下载（用户看到"已保存 3 项"，磁盘上却一个文件都没多）。
    """

    written: int = 0
    skipped: int = 0
    problems: tuple[str, ...] = ()

    def message(self) -> str:
        """面向用户的整句结果，界面直接展示。"""
        parts: list[str] = []
        if self.written:
            parts.append(f"已保存 {self.written} 项")
        if self.skipped:
            parts.append(f"{self.skipped} 项已是本地最新，未重复下载")
        head = "；".join(parts) if parts else "没有需要下载的示例"
        if not self.problems:
            return f"{head}。"
        return f"{head}；以下未保存：\n" + "\n".join(self.problems)


def save_all_examples(items: list[ExampleItem]) -> SaveReport:
    """逐个落盘并汇总。

    单项失败不影响其余，而且**什么异常都不往外抛** —— 它跑在 Qt 槽里，
    让意外异常逃出去就是界面上的未捕获异常。
    """
    written = 0
    skipped = 0
    problems: list[str] = []
    for item in items:
        try:
            _, wrote = _place_example(item)
        except Exception as exc:  # noqa: BLE001 - 槽边界，任何异常都要变成可展示的结果
            problems.append(f"{item.file}：{exc}")
            continue
        if wrote:
            written += 1
        else:
            skipped += 1
    return SaveReport(written=written, skipped=skipped, problems=tuple(problems))
