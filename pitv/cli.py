"""Command line entry point: `pitv <command>`."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import db as dbm
from .config import Config, load_config

Args = argparse.Namespace


def _open(cfg: Config) -> sqlite3.Connection:
    cfg.ensure_dirs()
    conn = dbm.connect(cfg.db_path)
    dbm.init_db(conn)
    return conn


def cmd_init(cfg: Config, args: Args) -> int:
    _open(cfg)
    print(f"Database ready at {cfg.db_path}")
    return 0


def cmd_fake_library(cfg: Config, args: Args) -> int:
    from .catalogue import import_and_place
    from .devtools import build_fake_library
    root = Path(args.dir).resolve()
    lib = build_fake_library(root, seed=args.seed, max_episodes_per_show=args.max_episodes)
    print(f"Fake library written under {root}; index at {lib['index_path']}")
    if args.import_index:
        conn = _open(cfg)
        print(import_and_place(conn, lib["index"], str(lib["index_path"]))["summary"])
    return 0


def cmd_test_signal(cfg: Config, args: Args) -> int:
    from .devtools import make_player_test_signal
    print(f"Test signal written to {make_player_test_signal(Path(args.out)) if args.out else make_player_test_signal()}")
    return 0


def cmd_catalogue(cfg: Config, args: Args) -> int:
    """Import pitv_content's library index: from a file, or from its API (index file fallback)."""
    from .catalogue import import_and_place, refresh
    from .logsetup import setup_logging
    setup_logging(cfg, "catalogue")
    conn = _open(cfg)
    if args.file:
        try:
            result = import_and_place(conn, json.loads(Path(args.file).read_text()), args.file)
        except (OSError, ValueError) as exc:   # unreadable, not JSON, or not a library index
            print(f"Import failed: {exc}")
            return 1
        print(f"Catalogue: {result['summary']}")
        return 0
    result = refresh(conn, reindex=args.reindex)
    print(f"Catalogue {result['status']}: {result['summary']}")
    return 0 if result["status"] != "error" else 1


def cmd_schedule(cfg: Config, args: Args) -> int:
    from .logsetup import setup_logging
    from .scheduler.horizon import build_horizon
    from .scheduler.slots import parse_day
    setup_logging(cfg, "schedule")
    conn = _open(cfg)
    start = parse_day(args.start) if args.start else None
    result = build_horizon(conn, start_day=start, days=args.days, force=args.force,
                           channel_numbers=args.channel, seed=args.seed,
                           progress=(lambda m: print("  " + m)) if not args.quiet else None)
    print(f"Schedule {result['status']}: {result['summary']}")
    return 0


def cmd_listing(cfg: Config, args: Args) -> int:
    from .scheduler.listing import print_listing
    conn = _open(cfg)
    print_listing(conn, day=args.day, channel_numbers=args.channel, show_ads=args.ads,
                  overnight=args.overnight)
    return 0


def cmd_web(cfg: Config, args: Args) -> int:
    import uvicorn

    from .web.app import create_app
    app = create_app(cfg)
    uvicorn.run(app, host=args.host or cfg.web_host, port=args.port or cfg.web_port,
                log_level="info", access_log=False, log_config=None)
    return 0


def cmd_play(cfg: Config, args: Args) -> int:
    from .player.controller import run_player
    return run_player(cfg, channel=args.channel, keyboard=args.keyboard, now_override=args.now)


def cmd_reset_schedule(cfg: Config, args: Args) -> int:
    conn = _open(cfg)
    with dbm.tx(conn):
        conn.execute("DELETE FROM schedule")
        if args.history:
            conn.execute("DELETE FROM history")
    print("Schedule cleared.")
    return 0


def cmd_content_manifest(cfg: Config, args: Args) -> int:
    from .content import manifest
    conn = _open(cfg)
    data = manifest(conn, days=args.days)
    text = json.dumps(data, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        print(f"{len(data['items'])} items, {len(data['wanted'])} wanted -> {args.out}")
    else:
        print(text)
    return 0


def cmd_content_report(cfg: Config, args: Args) -> int:
    from .content import apply_report
    conn = _open(cfg)
    try:
        print(apply_report(conn, json.loads(Path(args.file).read_text())))
    except (OSError, TypeError, ValueError) as exc:
        print(f"Report not applied: {exc}")
        return 1
    return 0


def cmd_readiness(cfg: Config, args: Args) -> int:
    from .logsetup import setup_logging
    from .readiness import check
    setup_logging(cfg, "schedule")
    conn = _open(cfg)
    r = check(conn, days=args.days, substitute=not args.no_substitute)
    print(f"Readiness {r['status']}: {r['summary']}")
    for n in r["notes"]:
        print("  " + n)
    return 0 if r["status"] != "error" else 1


def cmd_idents(cfg: Config, args: Args) -> int:
    """Make a fifteen second ident for each channel from its name and colour (pitv/idents.py)."""
    from . import idents
    conn = _open(cfg)
    out = Path(args.out) if args.out else cfg.data_dir / "idents"
    made = idents.make_all(conn, out, Path(args.voices) if args.voices else out / "voices",
                           {int(n) for n in args.channel} if args.channel else None)
    for path in made:
        print(path)
    print(f"{len(made)} ident(s) in {out}. Point an idents source at that folder in Admin, Sources, and they are indexed "
          f"with the rest; each channel's folder (ch1, ch2, ...) is what ties a film to its channel.")
    return 0


def cmd_doctor(cfg: Config, args: Args) -> int:
    """The state of the whole television in one report; exit 1 when anything needs attention."""
    from . import doctor
    conn = _open(cfg)
    doc = doctor.report(conn, cfg)
    text = json.dumps(doc, indent=1, default=str) if args.json else doctor.render(doc)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"Report written to {args.out} ({len(doc['findings'])} finding(s))")
    else:
        print(text)
    return 1 if doc["findings"] else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pitv", description="PiTV: 1980s television for the Raspberry Pi")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)
    dr = sub.add_parser("doctor", help="one report on services, player, schedule, cache, bands, pitv_content and logs")
    dr.add_argument("--json", action="store_true", help="the whole report as JSON rather than a summary")
    dr.add_argument("--out", help="write the report to this file")
    dr.set_defaults(func=cmd_doctor)

    idn = sub.add_parser("idents", help="make a 15 second ident for each channel from its name and colour")
    idn.add_argument("--out", help="folder to write ch<number>/<name> ident.mp4 into (default: <data>/idents)")
    idn.add_argument("--voices", help="folder of voiceover recordings named <number>.wav or <short name>.wav (default: <out>/voices)")
    idn.add_argument("--channel", action="append", help="only this channel number; may be given more than once")
    idn.set_defaults(func=cmd_idents)

    f = sub.add_parser("fake-library", help="generate a tiny fake library and its library index for development")
    f.add_argument("dir")
    f.add_argument("--seed", type=int, default=1)
    f.add_argument("--max-episodes", type=int, default=None, help="cap episodes per show")
    f.add_argument("--import", dest="import_index", action="store_true", help="import the generated index")
    f.set_defaults(func=cmd_fake_library)

    ts = sub.add_parser("test-signal", help="regenerate the test signal shipped in pitv/assets (needs ffmpeg)")
    ts.add_argument("--out", help="write here instead of pitv/assets/test_signal.mp4")
    ts.set_defaults(func=cmd_test_signal)

    ca = sub.add_parser("catalogue", help="import pitv_content's library index")
    ca.add_argument("--file", help="import this index file instead of asking pitv_content")
    ca.add_argument("--reindex", action="store_true", help="ask pitv_content to re-index its sources first")
    ca.set_defaults(func=cmd_catalogue)

    sh = sub.add_parser("schedule", help="build the schedule horizon")
    sh.add_argument("--start", help="first broadcast day YYYY-MM-DD (default: today)")
    sh.add_argument("--days", type=int, default=None)
    sh.add_argument("--channel", type=int, action="append", help="only these channel numbers")
    sh.add_argument("--force", action="store_true", help="rebuild days that already have a schedule")
    sh.add_argument("--seed", type=int, default=None)
    sh.add_argument("--quiet", action="store_true")
    sh.set_defaults(func=cmd_schedule)

    li = sub.add_parser("listing", help="print a day's listing")
    li.add_argument("--day", help="YYYY-MM-DD (default: today)")
    li.add_argument("--channel", type=int, action="append")
    li.add_argument("--ads", action="store_true", help="include adverts and idents")
    li.add_argument("--overnight", action="store_true", help="include the overnight replay")
    li.set_defaults(func=cmd_listing)

    rs = sub.add_parser("reset-schedule", help="delete the whole schedule")
    rs.add_argument("--history", action="store_true", help="also delete airing history")
    rs.set_defaults(func=cmd_reset_schedule)

    cm = sub.add_parser("content-manifest", help="what pitv_content should cache/fetch for the next day(s)")
    cm.add_argument("--days", type=int, default=1)
    cm.add_argument("--out", help="write JSON here instead of stdout")
    cm.set_defaults(func=cmd_content_manifest)
    cr = sub.add_parser("content-report", help="apply a pitv_content report JSON file")
    cr.add_argument("file")
    cr.set_defaults(func=cmd_content_report)

    rd = sub.add_parser("readiness", help="verify scheduled files exist; substitute and rebalance what is missing")
    rd.add_argument("--days", type=int, default=1)
    rd.add_argument("--no-substitute", action="store_true")
    rd.set_defaults(func=cmd_readiness)

    w = sub.add_parser("web", help="run the web interface")
    w.add_argument("--host")
    w.add_argument("--port", type=int)
    w.set_defaults(func=cmd_web)

    pl = sub.add_parser("play", help="run the player")
    pl.add_argument("--channel", type=int, default=None, help="start on this channel (default: last used)")
    pl.add_argument("--keyboard", action="store_true", help="read keys from the terminal (desktop)")
    pl.add_argument("--now", help="pretend the clock reads this local time (YYYY-MM-DDTHH:MM)")
    pl.set_defaults(func=cmd_play)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
