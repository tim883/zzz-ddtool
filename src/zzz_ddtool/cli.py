from __future__ import annotations

import argparse
import json
from pathlib import Path

from zzz_ddtool.cards import write_cards_html
from zzz_ddtool.config import load_preset, preset_from_dict
from zzz_ddtool.decide import decide
from zzz_ddtool.models import Disc, Substat
from zzz_ddtool.patterns import enumerate_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zzz-ddtool", description="绝区零驱动盘自动强化辅助")
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="日志里额外打出每一步的步骤号（截图始终全量，不受这个开关影响）",
    )
    sub = parser.add_subparsers(dest="cmd", required=False)

    gui_p = sub.add_parser("gui", help="Open the desktop window")
    gui_p.add_argument("--config", type=Path, help="YAML preset")

    decide_p = sub.add_parser("decide", help="Run upgrade / mark_discard / skip on a disc")
    decide_p.add_argument("--config", type=Path, help="YAML preset")
    decide_p.add_argument("--picks", type=Path, help="Second-pass JSON from preview export")
    decide_p.add_argument("--subs", required=True, help="Comma-separated substats, e.g. 暴击率,暴击伤害,生命值,防御力")
    decide_p.add_argument("--rolls", default="", help="Optional rolls per substat, e.g. 1,2,1,1")
    decide_p.add_argument("--level", type=int, default=0)
    decide_p.add_argument("--rarity", default="S")
    decide_p.add_argument("--locked", action="store_true")
    decide_p.add_argument("--equipped", action="store_true")
    decide_p.add_argument("--discarded", action="store_true")
    decide_p.add_argument("--min-useful", type=int, help="Override min_useful_types")

    cap_p = sub.add_parser("capture", help="Grab the game window into out/captures")
    cap_p.add_argument("--out", type=Path, help="Directory, default project out/captures")

    nav_p = sub.add_parser("goto-warehouse", help="Esc to menu if needed, then click 仓库")
    sub.add_parser("snapshot-pages", help="Click home / menu / warehouse and save named shots")

    run_p = sub.add_parser("run", help="按配置的任务链自动处理驱动盘（强化/弃置）")
    run_p.add_argument("--config", type=Path, required=True, help="YAML preset")

    preview_p = sub.add_parser("preview", help="Enumerate 3/4-line patterns into HTML cards")
    preview_p.add_argument("--config", type=Path, required=True)
    preview_p.add_argument("--out", type=Path, help="HTML path, default workspace out/pattern-cards.html")
    preview_p.add_argument("--show-junk-bonus", action="store_true", help="Also show rolls that landed on junk")

    args = parser.parse_args(argv)
    from zzz_ddtool.logutil import setup

    setup("DEBUG" if args.verbose else "INFO", console=True)
    if args.cmd in (None, "gui"):
        return _cmd_gui(getattr(args, "config", None))
    if args.cmd == "decide":
        return _cmd_decide(args)
    if args.cmd == "capture":
        return _cmd_capture(args)
    if args.cmd == "goto-warehouse":
        return _cmd_goto_warehouse()
    if args.cmd == "snapshot-pages":
        return _cmd_snapshot_pages()
    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "preview":
        return _cmd_preview(args)
    return 1


def _cmd_capture(args: argparse.Namespace) -> int:
    from zzz_ddtool.capture import CaptureError, capture_game

    try:
        dest = capture_game(args.out)
    except CaptureError as exc:
        print(exc)
        return 2
    print(dest)
    return 0


def _cmd_goto_warehouse() -> int:
    from zzz_ddtool.nav import NavError, goto_warehouse

    try:
        dest = goto_warehouse()
    except NavError as exc:
        print(exc)
        return 2
    print(dest)
    return 0


def _cmd_snapshot_pages() -> int:
    from zzz_ddtool.nav import NavError, snapshot_pages

    try:
        pages = snapshot_pages()
    except NavError as exc:
        print(exc)
        return 2
    for name, dest in pages.items():
        print(f"{name}\t{dest}")
    return 0


def _cmd_gui(config: Path | None) -> int:
    from zzz_ddtool.gui import run_gui

    return run_gui(config)


def _cmd_decide(args: argparse.Namespace) -> int:
    preset = _load_preset(args)
    accepted = None
    if args.picks:
        payload = json.loads(args.picks.read_text(encoding="utf-8"))
        accepted = set(payload.get("accepted") or [])
    elif preset.accepted is not None:
        accepted = set(preset.accepted)

    names = [part.strip() for part in args.subs.split(",") if part.strip()]
    roll_parts = [part.strip() for part in args.rolls.split(",") if part.strip()]
    rolls = [int(x) for x in roll_parts] if roll_parts else [1] * len(names)
    if len(rolls) != len(names):
        raise SystemExit("--rolls count must match --subs")

    disc = Disc(
        level=args.level,
        substats=tuple(Substat(name=name, rolls=roll) for name, roll in zip(names, rolls, strict=True)),
        rarity=args.rarity,
        locked=args.locked,
        equipped=args.equipped,
        discarded=args.discarded,
    )
    result = decide(disc, preset, accepted)
    print(
        json.dumps(
            {
                "action": result.action.value,
                "reason": result.reason,
                "score": result.score,
                "useful_types": result.useful_types,
                "max_useful_types": result.max_useful_types,
                "checkpoint": result.checkpoint,
                "origin": result.origin,
                "pattern_id": result.pattern_id,
                "line_count": disc.line_count,
                "empty_slots": disc.empty_slots,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    preset = load_preset(args.config)
    cards = enumerate_all(preset.weights, hide_junk_bonus=not args.show_junk_bonus)
    dest = args.out or _default_preview_path()
    write_cards_html(
        cards,
        dest,
        stage_require=preset.stage_require,
        title=f"{preset.name} 组合预览",
    )
    print(f"wrote {dest} ({len(cards)} cards, filter by per-stage require in the page)")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    import logging
    import sys

    from zzz_ddtool.runner import Runner, RunnerError

    # 日志实时打到控制台，运行过程随时可见
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logging.getLogger("zzz_ddtool").addHandler(handler)

    preset = load_preset(args.config)
    runner = Runner(preset)
    try:
        results = runner.run()
    except RunnerError as exc:
        print(exc)
        return 2
    print(f"处理 {len(results)} 个盘子")
    for r in results:
        print(f"{r.action:14} {r.reason:30} score={r.score:.2f} level={r.level}")
    return 0


def _load_preset(args: argparse.Namespace):
    if args.config:
        preset = load_preset(args.config)
    else:
        preset = preset_from_dict(
            {
                "name": "cli",
                "weights": {"暴击率": 1.0, "暴击伤害": 1.2, "攻击力%": 0.8},
                "stage_require": {
                    "three": {0: 2.0, 3: 3.0, 6: 4.0, 9: 5.0, 12: 6.0, 15: 7.0},
                    "four": {0: 3.0, 3: 5.0, 6: 6.0, 9: 7.0, 12: 8.0, 15: 9.0},
                },
                "min_useful_types": 2,
            }
        )
    if args.min_useful is None:
        return preset
    return preset_from_dict(
        {
            "name": preset.name,
            "weights": dict(preset.weights),
            "stage_require": {key: dict(val) for key, val in preset.stage_require.items()},
            "min_useful_types": args.min_useful,
            "filters": {
                "require_s_rank": preset.require_s_rank,
                "skip_locked": preset.skip_locked,
                "skip_equipped": preset.skip_equipped,
                "skip_discarded": preset.skip_discarded,
            },
            "run": {"max_discs": preset.max_discs},
            "game_filter": {
                "sets": list(preset.filter_sets),
                "slot": preset.filter_slot,
                "slots": [preset.filter_slot],
                "mainstats": list(preset.filter_mainstats),
            },
            "slot_plans": {
                str(slot): dict(plan) for slot, plan in (preset.slot_plans or {}).items()
            },
            "ocr_retry": preset.ocr_retry,
            "accepted": None if preset.accepted is None else list(preset.accepted),
        }
    )


def _default_preview_path() -> Path:
    from zzz_ddtool.paths import out_dir

    return out_dir() / "pattern-cards.html"


if __name__ == "__main__":
    raise SystemExit(main())
