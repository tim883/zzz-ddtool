#!/usr/bin/env python
"""一轮真机结果分析：把 ``log/<轮次>/perf.jsonl`` 折算成可核对的耗时口径。

只读，不操作游戏、不改动任何运行数据。用于三类判定：

1. **`return_warehouse` 逐边**：每件强化盘的两次 Esc 各属哪条边、实际等待多久、
   下限到点时页面是否已经就绪（ready 早于下限即"在凑下限"）。
2. **切页置顶**：`reset_to_top` 两次页签点击的实际墙钟，以及认页循环是否变慢。
3. **重复抓帧/识别**：同一帧被识别两次的区域（历史缺陷，已修，用于回归确认）。

用法：

    .venv/Scripts/python.exe scripts/analyze_round.py --latest
    .venv/Scripts/python.exe scripts/analyze_round.py --round log/20261012-202521-215
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

#: 层级边名（与 actionwait.RETURN_EDGE_IDS 一致，独立书写避免本脚本依赖 src）。
_EDGES = ("modal_preview", "modal_warehouse", "preview_warehouse")

#: 逐边运行时常量；与 runner._RETURN_EDGE_FLOOR_MS 同步，落后即视为已改代码。
_RUNTIME_EDGE_FLOOR_MS = {
    "modal_preview": 1680,
    "modal_warehouse": 1680,
    "preview_warehouse": 680,
}

#: 单帧复用回归针对的恢复路径读取区域：仓库标题数量、恢复列等级。
#: 这两块在改动前会被同一帧读两遍（先测布局、再核对数量/位置）。
_RECOVERY_READ_REGIONS = ("(60, 105, 420, 175)", "(100, 200, 230, 945)")


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _iter_inputs(rows: list[dict]) -> dict[str, dict]:
    """执行输入记录按 input_id 去重（收尾汇总行会重复列一次）。"""
    found: dict[str, dict] = {}
    for row in rows:
        details = row.get("details")
        if not isinstance(details, dict):
            continue
        for item in details.get("inputs") or ():
            if isinstance(item, dict) and item.get("input_id"):
                found.setdefault(item["input_id"], item)
    return found


def _edge_of(state_key: object) -> str | None:
    """从 wait 样本的 state_key 反推这次 Esc 所在的边。

    state_key 形如 ``["return_warehouse", before_layer, after_layer]``；允许的三条边
    由 before→after 唯一确定。构不出允许边（含 unknown）返回 None，按未知统计。
    """
    if not isinstance(state_key, list) or len(state_key) < 3:
        return None
    _kind, before, after = state_key[0], state_key[1], state_key[2]
    if before == "modal" and after == "preview":
        return "modal_preview"
    if before == "modal" and after == "warehouse":
        return "modal_warehouse"
    if before == "preview" and after == "warehouse":
        return "preview_warehouse"
    return None


def _region_of(details: dict) -> str | None:
    """识别事件的目标区域：details 里以 region/area/rect 记一行区域（没有则 None）。"""
    for key in ("region", "area", "rect"):
        value = details.get(key)
        if isinstance(value, (list, tuple)) and len(value) == 4:
            return "(" + ", ".join(str(int(v)) for v in value) + ")"
    return None


def _roi_summary(rows: list[dict]) -> Counter:
    """perf 侧的识别批次按 roi_ids 汇总（不含帧标识，只作总量参考）。"""
    counter: Counter = Counter()
    for row in rows:
        if row.get("component") != "recognize_batch":
            continue
        details = row.get("details") or {}
        for roi in details.get("roi_ids") or ():
            counter[str(roi)] += 1
    return counter


def _duplicate_reads(round_dir: Path) -> tuple[dict[str, list[str]], int]:
    """从 trace 日志找「同一帧被识别两次同一区域」。

    识别行形如 ``识别 (60, 105, 420, 175) → 1 行（1920x1080，158ms，图#266a00000001）``；
    末尾的「图#<帧 id>」是同一帧的标识。perf 的 recognize_batch 只有 roi_ids，
    不含帧标识，所以这项只能从 trace 判。
    """
    log = round_dir / "ddtool.log"
    if not log.is_file():
        return {}, 0
    pattern = re.compile(
        r"识别\s*\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)\s*→\s*\d+\s*行"
        r"（[^）]*图#([0-9a-fA-F]+)）"
    )
    seen: dict[str, list[str]] = defaultdict(list)
    total = 0
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.search(line)
        if not match:
            continue
        total += 1
        region = f"({match.group(1)}, {match.group(2)}, {match.group(3)}, {match.group(4)})"
        seen[match.group(5)].append(region)
    return {k: v for k, v in seen.items() if len(v) > len(set(v))}, total


def analyze(round_dir: Path) -> int:
    perf = round_dir / "perf.jsonl"
    if not perf.is_file():
        print(f"[错误] 找不到 {perf}", file=sys.stderr)
        return 2

    rows = _load_jsonl(perf)
    inputs = _iter_inputs(rows)
    waits = [r for r in rows if r.get("component") == "wait_total"]
    probes = [r for r in rows if r.get("component") == "probe"]

    meta = {}
    round_json = round_dir / "round.json"
    if round_json.is_file():
        meta = json.loads(round_json.read_text(encoding="utf-8"))

    print("=" * 78)
    print(f"轮次 {round_dir.name}  状态={meta.get('outcome')}  "
          f"结束原因={meta.get('end_reason')}")
    if meta.get("started_at") and meta.get("ended_at"):
        print(f"起止 {meta['started_at']} → {meta['ended_at']}")
    print(f"perf 事件 {len(rows)}  执行输入 {len(inputs)}  wait_total {len(waits)}  probe {len(probes)}")
    print("=" * 78)

    # ---------------------------------------------------------------- 逐边 Esc
    print("\n【1】return_warehouse 逐边（每件强化盘两次 Esc）\n")
    by_edge: dict[str, list[dict]] = defaultdict(list)
    unknown = 0
    for wait in waits:
        details = wait.get("details") or {}
        if details.get("kind") != "return_warehouse":
            continue
        samples = [s for s in (details.get("samples") or []) if isinstance(s, dict)]
        edge = None
        for sample in samples:
            edge = _edge_of(sample.get("state_key"))
            if edge:
                break
        if edge is None:
            unknown += 1
            continue
        by_edge[edge].append({"dur_ms": wait.get("duration_ms") or 0.0, "samples": samples})

    header = (f"{'边':<20}{'次数':>5}{'合计s':>8}{'平均ms':>9}"
              f"{'下限ms':>8}{'探针':>6}{'首帧ready':>10}  判定")
    print(header)
    total_esc = 0.0
    for edge in _EDGES:
        items = by_edge.get(edge) or []
        if not items:
            print(f"{edge:<20}{0:>5}{'—':>8}{'—':>9}{_RUNTIME_EDGE_FLOOR_MS[edge]:>8}"
                  f"{'—':>6}{'—':>10}  本轮未出现")
            continue
        dur = sum(i["dur_ms"] for i in items) / 1000.0
        total_esc += dur
        probes_n = sum(len(i["samples"]) for i in items)
        first_ready = sum(1 for i in items if i["samples"] and i["samples"][0].get("ready"))
        avg = sum(i["dur_ms"] for i in items) / len(items)
        floor = _RUNTIME_EDGE_FLOOR_MS[edge]
        # 首帧就 ready 却仍等满下限 = 在凑下限，属于可继续压缩的部分。
        verdict = "首帧即就绪，等待由下限决定" if first_ready == len(items) else (
            "就绪本身需要时间，下限不是主因" if first_ready == 0 else "两类混合")
        print(f"{edge:<20}{len(items):>5}{dur:>8.1f}{avg:>9.0f}{floor:>8}"
              f"{probes_n:>6}{f'{first_ready}/{len(items)}':>10}  {verdict}")
    if unknown:
        print(f"{'(未知边)':<20}{unknown:>5}   ← state_key 构不出允许边，按未知统计")
    per_disc = total_esc / max(1, len(by_edge.get("modal_preview") or []))
    print(f"\n  各边合计 {total_esc:.1f}s；按每件强化盘 2 次 Esc 计约 {per_disc:.1f}s/件")

    # ------------------------------------------------------------ 切页置顶
    print("\n【2】切页置顶（reset_to_top：驱动盘→音擎→驱动盘）\n")
    recovery_clicks = [i for i in inputs.values()
                       if i.get("phase") == "recovery" and i.get("api") == "click_client"]
    if recovery_clicks:
        durations = sorted((c.get("ended_at") or 0) - (c.get("started_at") or 0)
                           for c in recovery_clicks)
        print(f"  恢复阶段页签点击 {len(durations)} 次；单次 {durations[0] * 1000:.0f}~"
              f"{durations[-1] * 1000:.0f}ms，中位 {durations[len(durations) // 2] * 1000:.0f}ms")
        print("  改前每次点击后固定 sleep(1.1s)，两次合计 2.2s；现在点完即认页。")
    else:
        print("  本轮没有切页置顶（单屏任务不需要恢复）")

    # -------------------------------------------------- 重复抓帧 / 重复识别
    print("\n【3】重复抓帧/识别（历史缺陷回归：同一帧被识别两次）\n")
    duplicates, read_total = _duplicate_reads(round_dir)
    targeted = {k: [r for r in v if r in _RECOVERY_READ_REGIONS] for k, v in duplicates.items()}
    targeted = {k: v for k, v in targeted.items() if v}
    other = {k: v for k, v in duplicates.items() if k not in targeted}
    print(f"  trace 中带帧标识的识别 {read_total} 次")
    print(f"  恢复路径区域 {_RECOVERY_READ_REGIONS} 的重复读取：{len(targeted)} 例")
    for image_id, regions in list(targeted.items())[:5]:
        print(f"    图#{image_id}: {Counter(regions)}")
    if read_total == 0:
        print("  （本轮无带帧标识的识别行，无法判定）")
    elif not targeted:
        print("  ✅ 无重复：恢复路径单帧复用生效（历史缺陷不再出现）")
    else:
        print("  ⚠️ 恢复路径仍在同一帧重复识别；改动前的旧轮次这里应出现标题区与左列各两次")
    if other:
        print(f"\n  其他区域的同帧重复 {len(other)} 例（Esc 等待重拍等预期行为，非本次缺陷）：")
        for image_id, regions in list(other.items())[:3]:
            print(f"    图#{image_id}: {Counter(regions)}")

    # ------------------------------------------------------------ 抓图总量
    print("\n【4】动作耗时与抓图总量\n")
    per_kind: dict[str, list[float]] = defaultdict(list)
    for item in inputs.values():
        per_kind[str(item.get("kind"))].append(
            ((item.get("ended_at") or 0) - (item.get("started_at") or 0)) * 1000)
    print(f"{'kind':<28}{'次数':>5}{'合计s':>9}{'平均ms':>9}")
    for kind, values in sorted(per_kind.items(), key=lambda kv: -sum(kv[1]))[:12]:
        print(f"{kind:<28}{len(values):>5}{sum(values) / 1000:>9.2f}"
              f"{sum(values) / len(values):>9.0f}")
    captures = [r for r in rows if r.get("component") == "capture"]
    print(f"\n  capture {len(captures)} 次，合计 "
          f"{sum(r.get('duration_ms') or 0 for r in captures) / 1000:.1f}s")
    return 0


def _latest_round(log_root: Path) -> Path | None:
    candidates = [p for p in log_root.iterdir()
                  if p.is_dir() and len(p.name) == 19 and p.name[8] == "-"]
    return max(candidates, key=lambda p: p.name) if candidates else None


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--round", type=Path, help="轮次目录（log/<时间戳编号>）")
    group.add_argument("--latest", action="store_true", help="分析 log/ 下最新一轮")
    parser.add_argument("--log-root", type=Path, default=repo / "log",
                        help="log 根目录（默认仓库内 log/）")
    args = parser.parse_args(argv)

    if args.latest:
        target = _latest_round(args.log_root)
        if target is None:
            print(f"[错误] {args.log_root} 下没有轮次目录", file=sys.stderr)
            return 2
    else:
        target = args.round if args.round.is_absolute() else repo / args.round
    return analyze(target)


if __name__ == "__main__":
    raise SystemExit(main())
