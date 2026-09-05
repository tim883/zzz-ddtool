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
    parser = argparse.ArgumentParser(prog="zzz-ddtool", description="Drive disc upgrade helper")
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

    preview_p = sub.add_parser("preview", help="Enumerate 3/4-line patterns into HTML cards")
    preview_p.add_argument("--config", type=Path, required=True)
    preview_p.add_argument("--out", type=Path, help="HTML path, default workspace out/pattern-cards.html")
    preview_p.add_argument("--show-junk-bonus", action="store_true", help="Also show rolls that landed on junk")

    args = parser.parse_args(argv)
    if args.cmd in (None, "gui"):
        return _cmd_gui(getattr(args, "config", None))
    if args.cmd == "decide":
        return _cmd_decide(args)
    if args.cmd == "preview":
        return _cmd_preview(args)
    return 1


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
            "ocr_retry": preset.ocr_retry,
            "accepted": None if preset.accepted is None else list(preset.accepted),
        }
    )


def _default_preview_path() -> Path:
    # src/zzz_ddtool/cli.py → workspace/out
    workspace = Path(__file__).resolve().parents[3]
    return workspace / "out" / "pattern-cards.html"


if __name__ == "__main__":
    raise SystemExit(main())
