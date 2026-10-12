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

    cap_p = sub.add_parser("capture", help="Grab the game window into log_test 当前轮次的 captures/")
    cap_p.add_argument(
        "--out", type=Path, help="Directory, default current round captures under log_test/"
    )

    read_p = sub.add_parser("read-disc", help="读取已保存的盘面截图，不操作游戏")
    read_p.add_argument("image", type=Path)
    read_p.add_argument(
        "--out", type=Path, help="识别结果 JSON，默认当前 log_test 轮次目录下的 disc-reading.json"
    )

    nav_p = sub.add_parser("goto-warehouse", help="Esc to menu if needed, then click 仓库")
    sub.add_parser("snapshot-pages", help="Click home / menu / warehouse and save named shots")

    run_p = sub.add_parser("run", help="按配置的任务链自动处理驱动盘（强化/弃置）")
    run_p.add_argument("--config", type=Path, required=True, help="YAML preset")
    run_p.add_argument(
        "--start-index",
        type=int,
        default=0,
        metavar="N",
        help="[测试用] 跳过前 N 个内部序号，直接把视口摆到第 N 个并从这里开始；"
        "前面的格标为已跳过、不点击不识别不计数。默认 0（不跳过，正常从第一个开始）。",
    )
    run_p.add_argument(
        "--material-slot",
        type=int,
        default=0,
        metavar="N",
        help="[测试用] 升级时改点第 N 个材料槽（1~3）而不是「阶段添加」：只加一份素材、"
        "吃不满一档，用来复现「素材不足、升完到不了下一档」。默认 0（正常用「阶段添加」）。",
    )

    list_p = sub.add_parser(
        "snapshot-list",
        help="[开发] 进驱动盘仓库→按预设设筛选→分块滚动→逐帧存图（E1/E5/E6 场景图）",
    )
    list_p.add_argument("--config", type=Path, required=True, help="YAML preset")
    list_p.add_argument("--job", type=int, default=0, help="任务链下标，默认 0")
    list_p.add_argument(
        "--scroll",
        type=int,
        default=0,
        metavar="ROWS",
        help="设筛选后向下滚动的总行数，0=只取顶部图",
    )
    list_p.add_argument(
        "--chunk", type=int, default=4, metavar="ROWS", help="每次滚动的行数，默认 4（一屏）"
    )
    list_p.add_argument("--out", type=Path, help="存图目录，默认当前 log_test 轮次的 captures/")
    list_p.add_argument("--name", default="list", help="截图文件名前缀，默认 list")
    preview_p = sub.add_parser("preview", help="Enumerate 3/4-line patterns into HTML cards")
    preview_p.add_argument("--config", type=Path, required=True)
    preview_p.add_argument(
        "--out", type=Path, help="HTML path, default current log_test round pattern-cards.html"
    )
    preview_p.add_argument("--show-junk-bonus", action="store_true", help="Also show rolls that landed on junk")

    args = parser.parse_args(argv)
    from zzz_ddtool.logutil import setup

    _enter_context(args)
    setup("DEBUG" if args.verbose else "INFO", console=True)
    if args.cmd in (None, "gui"):
        return _cmd_gui(getattr(args, "config", None))
    if args.cmd == "decide":
        return _cmd_decide(args)
    if args.cmd == "capture":
        return _cmd_capture(args)
    if args.cmd == "read-disc":
        return _cmd_read_disc(args)
    if args.cmd == "goto-warehouse":
        return _cmd_goto_warehouse()
    if args.cmd == "snapshot-pages":
        return _cmd_snapshot_pages()
    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "snapshot-list":
        return _cmd_snapshot_list(args)
    if args.cmd == "preview":
        return _cmd_preview(args)
    return 1


#: 手动诊断子命令：回放、标定与预览用，日志走 log_test/。
_DIAGNOSTIC_COMMANDS = (
    "capture",
    "read-disc",
    "goto-warehouse",
    "snapshot-pages",
    "snapshot-list",
    "preview",
    "decide",
)


def _enter_context(args: argparse.Namespace) -> None:
    """按入口分类定下日志上下文，先定归属再配日志。

    正式 ``run`` 走生产根 ``log/``；非默认 ``--start-index`` / ``--material-slot``
    是已有测试入口，手动诊断子命令也是测试/开发用途，一并走 ``log_test/``。
    ``gui`` 与无子命令不在这里建轮次：GUI 启动会自己开启动轮次，免得一次启动两个目录。
    """
    from zzz_ddtool import loground
    from zzz_ddtool.logutil import get_logger

    try:
        if args.cmd in (None, "gui"):
            return
        if args.cmd == "run":
            if getattr(args, "start_index", 0) or getattr(args, "material_slot", 0):
                loground.use_test_context("cli-run-test")
            else:
                loground.use_production_context("cli-run", config=str(args.config))
            return
        if args.cmd in _DIAGNOSTIC_COMMANDS:
            loground.use_test_context(args.cmd)
    except OSError as exc:
        # 日志目录建不出来（只读盘、权限不足）：报一声，命令照常往下走
        get_logger("zzz_ddtool.cli").warning("日志轮次建不出来，本轮只写控制台：%s", exc)


def _round_output(name: str) -> Path:
    """当前轮次目录下的默认输出路径；轮次没建起来时退回当前上下文根。"""
    from zzz_ddtool import loground

    round_ = loground.current()
    root = round_.root if round_ is not None else loground.current_root()
    return root / name


def _cmd_capture(args: argparse.Namespace) -> int:
    from zzz_ddtool.capture import CaptureError, capture_game

    try:
        dest = capture_game(args.out)
    except CaptureError as exc:
        print(exc)
        return 2
    print(dest)
    return 0


def _cmd_read_disc(args: argparse.Namespace) -> int:
    from dataclasses import asdict
    from PySide6.QtGui import QImage
    from zzz_ddtool.ocr import parse_disc
    from zzz_ddtool.logutil import get_logger

    dest = args.out or _round_output("disc-reading.json")
    try:
        image = QImage(str(args.image))
        if image.isNull():
            raise ValueError(f"无法读取截图：{args.image}")
        disc = parse_disc(image)
        if disc is None:
            raise ValueError("未识别出盘面")
        payload = {"ok": True, "image": str(args.image.resolve()), "disc": asdict(disc)}
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)}
        get_logger("zzz_ddtool.ocr").exception("截图回放识别失败")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if payload["ok"] else 2


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


def _cmd_snapshot_list(args: argparse.Namespace) -> int:
    """[开发] 取列表实图：进驱动盘仓库 → 按预设设筛选 → 分块滚动 → 逐帧存图。

    只服务开发标定与实图重放（E1/E5/E6 的场景图），不参与生产任务链。
    每滚一屏存一张，滚到列表被端点截停后继续发送也只是停在底部，帧序即现场。
    """
    import time

    from zzz_ddtool.capture import CaptureError, grab_game_image, save_image
    from zzz_ddtool.gamefilter import apply_game_filter
    from zzz_ddtool.nav import NavError, goto_warehouse
    from zzz_ddtool.navigation import load_profile, probe_environment, scroll_rows
    from zzz_ddtool.runner import Runner

    preset = load_preset(args.config)
    jobs = Runner(preset, hotkeys=False)._jobs()
    if not 0 <= args.job < len(jobs):
        print(f"--job 越界：该预设只有 {len(jobs)} 个任务")
        return 2
    job = jobs[args.job]

    try:
        goto_warehouse()
    except NavError as exc:
        print(exc)
        return 2

    try:
        image, _method, hwnd = grab_game_image()
    except CaptureError as exc:
        print(exc)
        return 2

    apply_game_filter(
        hwnd,
        image.width(),
        image.height(),
        slots=[job.slot],
        sets=job.sets,
        mainstats=job.mainstats,
    )
    time.sleep(0.4)
    image = grab_game_image(hwnd)[0]
    dest = save_image(image, hwnd, "printwindow", name=f"{args.name}-top", dest_dir=args.out)
    print(f"top\t{dest}")

    if args.scroll <= 0:
        return 0

    profile = load_profile(probe_environment(hwnd))
    if profile is None:
        print("没有可用的滚轮协议缓存：先在游戏里跑一次 run 走完标定，再取图")
        return 2
    settle_ms = profile.settle_wait_ms or 500
    remaining = int(args.scroll)
    step = max(1, int(args.chunk))
    done = 0
    while remaining > 0:
        rows = min(step, remaining)
        scroll_rows(hwnd, rows, profile)
        time.sleep(settle_ms / 1000.0)
        remaining -= rows
        done += rows
        image = grab_game_image(hwnd)[0]
        dest = save_image(
            image, hwnd, "printwindow", name=f"{args.name}-scroll{done}", dest_dir=args.out
        )
        print(f"scroll{done}\t{dest}")
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
    cards = enumerate_all(preset.effective_weights(), hide_junk_bonus=not args.show_junk_bonus)
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

    from zzz_ddtool import loground, retain
    from zzz_ddtool.logutil import get_logger
    from zzz_ddtool.runner import Runner, RunnerError

    # 日志实时打到控制台，运行过程随时可见
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logging.getLogger("zzz_ddtool").addHandler(handler)

    # 保留轮数跟界面设置一致：CLI 不读设置的话，会用默认值把用户想留的轮次删掉
    try:
        from zzz_ddtool.logprefs import load as load_prefs
        from zzz_ddtool.retain import set_max_rounds

        _, keep_rounds = load_prefs()
        set_max_rounds(keep_rounds)
    except Exception as exc:  # 设置读不出来就按默认保留轮数，不影响任务
        get_logger("zzz_ddtool.cli").warning("读取日志设置失败，按默认保留轮数回收：%s", exc)

    preset = load_preset(args.config)
    # claim_round：本次 CLI 运行只产生一个轮次，涵盖配置装载、任务准备、执行与收尾
    runner = Runner(
        preset,
        start_index=getattr(args, "start_index", 0) or 0,
        material_slot=getattr(args, "material_slot", 0) or 0,
        config_path=str(args.config),
        claim_round=True,
    )
    try:
        try:
            results = runner.run()
        finally:
            # 成功与异常都要收尾：登记结果、释放本轮占用，再按轮数回收旧日志
            loground.finish(runner.outcome, reason=runner.end_reason)
            loground.close_active()
            try:
                retain.retain_rounds()
            except Exception as exc:  # 回收失败不改写命令结果
                get_logger("zzz_ddtool.cli").warning("历史轮次回收失败：%s", exc)
    except RunnerError as exc:
        print(exc)
        return 2
    print(f"{runner.outcome}：处理 {runner.processed_disc_count} 个盘子；{runner.end_reason}")
    for r in results:
        print(f"{r.action:14} {r.reason:30} score={r.score:.2f} level={r.level}")
    return 2 if runner.outcome == "failed" else 0


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
    return _round_output("pattern-cards.html")


if __name__ == "__main__":
    raise SystemExit(main())
