"""Command line entry point: `pitv <command>`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import db as dbm
from .config import load_config


def _open(cfg):
    cfg.ensure_dirs()
    conn = dbm.connect(cfg.db_path)
    dbm.init_db(conn)
    return conn


def cmd_init(cfg, args) -> int:
    _open(cfg)
    print(f"Database ready at {cfg.db_path}")
    return 0


def cmd_fake_library(cfg, args) -> int:
    from .devtools import build_fake_library
    root = Path(args.dir).resolve()
    paths = build_fake_library(root, seed=args.seed, max_episodes_per_show=args.max_episodes)
    conn = _open(cfg)
    if args.register:
        with dbm.tx(conn):
            for stype, name, p, cat in (("tv", "TV Shows", paths["tv"], "general"), ("movie", "Movies", paths["movies"], "general"),
                                        ("advert", "Adverts", paths["pitv"] / "Adverts", "general"),
                                        ("ident", "Idents", paths["pitv"] / "Idents", "general"),
                                        ("tv", "Sport", paths["sport"], "sport"),
                                        ("music", "Music videos", paths["music"], "general")):
                exists = conn.execute("SELECT id FROM sources WHERE path = ?", (str(p),)).fetchone()
                if not exists:
                    conn.execute("INSERT INTO sources(type, name, path, category) VALUES (?,?,?,?)",
                                 (stype, name, str(p), cat))
        print("Registered sources:")
        for r in conn.execute("SELECT id, type, name, path FROM sources"):
            print(f"  {r['id']:>2}  {r['type']:<7} {r['name']:<10} {r['path']}")
    print(f"Fake library written under {root}")
    return 0


def cmd_source(cfg, args) -> int:
    conn = _open(cfg)
    if args.action == "add":
        with dbm.tx(conn):
            conn.execute("INSERT INTO sources(type, name, path, remote) VALUES (?,?,?,?)",
                         (args.type, args.name, str(Path(args.path).resolve()), args.remote))
        print("Added.")
    for r in conn.execute("SELECT * FROM sources ORDER BY id"):
        print(f"  {r['id']:>2}  {r['type']:<7} {'on ' if r['enabled'] else 'off'} {r['name']:<12}"
              f" {r['path']}  {r['last_scan_summary'] or ''}")
    return 0


def cmd_scan(cfg, args) -> int:
    from .library.scanner import scan_all
    from .logsetup import setup_logging
    setup_logging(cfg, "scan")
    conn = _open(cfg)
    last = [""]

    def progress(msg: str, done: int, total: int) -> None:
        line = f"\r  {done}/{total} {msg[:70]:<70}"
        if line != last[0]:
            sys.stdout.write(line)
            sys.stdout.flush()
            last[0] = line

    run_id = scan_all(conn, progress if not args.quiet else (lambda *a: None), cfg.ffprobe_binary)
    sys.stdout.write("\n")
    row = conn.execute("SELECT * FROM run_log WHERE id = ?", (run_id,)).fetchone()
    print(f"Scan {row['status']}: {row['summary']}")
    counts = conn.execute("SELECT kind, COUNT(*) AS n FROM media WHERE missing = 0 GROUP BY kind").fetchall()
    print("  " + ", ".join(f"{r['kind']}s: {r['n']}" for r in counts))
    return 0 if row["status"] != "error" else 1


def cmd_schedule(cfg, args) -> int:
    from .scheduler.build import build_horizon, parse_day
    from .logsetup import setup_logging
    setup_logging(cfg, "schedule")
    conn = _open(cfg)
    start = parse_day(args.start) if args.start else None
    result = build_horizon(conn, start_day=start, days=args.days, force=args.force,
                           channel_numbers=args.channel, seed=args.seed,
                           progress=(lambda m: print("  " + m)) if not args.quiet else None)
    print(f"Schedule {result['status']}: {result['summary']}")
    return 0


def cmd_listing(cfg, args) -> int:
    from .scheduler.listing import print_listing
    conn = _open(cfg)
    print_listing(conn, day=args.day, channel_numbers=args.channel, show_ads=args.ads,
                  overnight=args.overnight)
    return 0


def cmd_web(cfg, args) -> int:
    import uvicorn
    from .web.app import create_app
    app = create_app(cfg)
    uvicorn.run(app, host=args.host or cfg.web_host, port=args.port or cfg.web_port,
                log_level="info", access_log=False, log_config=None)
    return 0


def cmd_play(cfg, args) -> int:
    from .player.controller import run_player
    return run_player(cfg, channel=args.channel, keyboard=args.keyboard, now_override=args.now)


def cmd_reset_schedule(cfg, args) -> int:
    conn = _open(cfg)
    with dbm.tx(conn):
        conn.execute("DELETE FROM schedule")
        if args.history:
            conn.execute("DELETE FROM history")
    print("Schedule cleared.")
    return 0


def cmd_content_manifest(cfg, args) -> int:
    import json as _json
    from .content import manifest
    conn = _open(cfg)
    data = manifest(conn, days=args.days)
    text = _json.dumps(data, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        print(f"{len(data['items'])} items, {len(data['wanted'])} wanted -> {args.out}")
    else:
        print(text)
    return 0


def cmd_content_report(cfg, args) -> int:
    import json as _json
    from .content import apply_report
    conn = _open(cfg)
    data = _json.loads(Path(args.file).read_text())
    print(apply_report(conn, data))
    return 0


def cmd_readiness(cfg, args) -> int:
    from .logsetup import setup_logging
    from .readiness import check
    setup_logging(cfg, "schedule")
    conn = _open(cfg)
    r = check(conn, days=args.days, substitute=not args.no_substitute)
    print(f"Readiness {r['status']}: {r['summary']}")
    for n in r["notes"]:
        print("  " + n)
    return 0 if r["status"] != "error" else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pitv", description="PiTV: 1980s television for the Raspberry Pi")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)

    f = sub.add_parser("fake-library", help="generate a tiny fake media library for development")
    f.add_argument("dir")
    f.add_argument("--seed", type=int, default=1)
    f.add_argument("--max-episodes", type=int, default=None, help="cap episodes per show")
    f.add_argument("--register", action="store_true", help="register the folders as sources")
    f.set_defaults(func=cmd_fake_library)

    s = sub.add_parser("source", help="list or add sources")
    s.add_argument("action", choices=["list", "add"], nargs="?", default="list")
    s.add_argument("--type", choices=["tv", "movie", "advert", "ident"])
    s.add_argument("--name")
    s.add_argument("--path")
    s.add_argument("--remote")
    s.set_defaults(func=cmd_source)

    sc = sub.add_parser("scan", help="scan all enabled sources")
    sc.add_argument("--quiet", action="store_true")
    sc.set_defaults(func=cmd_scan)

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
