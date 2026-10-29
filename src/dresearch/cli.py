"""Command line:  dresearch probe | collect | audit [--day YYYY-MM-DD] | status"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

DEFAULT_DATA = os.environ.get("DRESEARCH_DATA", "/var/lib/dresearch")


def cmd_probe(args) -> int:
    from .probe import run_probe, summarise
    report = asyncio.run(run_probe(Path(args.data)))
    print(summarise(report))
    print(f"\nSaved: {args.data}/config/symbols.json and {args.data}/reports/")
    return 0 if report.get("collector_config", {}).get("collect") else 2


def cmd_collect(args) -> int:
    from .collector import Collector
    asyncio.run(Collector(Path(args.data)).run())
    return 0


def cmd_backfill(args) -> int:
    from .backfill import run_backfill
    state = asyncio.run(run_backfill(Path(args.data), args.days, per_minute=args.per_minute))
    print(f"Backfill {'finished' if state['finished'] else 'stopped early (re-run to resume)'}: "
          f"{state['hours_written']} hours written, {state['hours_empty']} unavailable, {state['errors']} errors")
    return 0 if state["finished"] else 1


def cmd_audit(args) -> int:
    from .audit import run_audit, summarise
    report = run_audit(Path(args.data), args.day, seal=not args.no_seal)
    print(summarise(report))
    return 0 if report["overall"] == "OK" else 1


def _last_live_epoch(data: Path, sym: str):
    d = data / "raw" / "live" / sym
    if not d.exists():
        return None, 0
    days = sorted(p for p in d.iterdir() if p.is_dir())
    if not days:
        return None, 0
    files = sorted(p for p in days[-1].iterdir() if p.suffix == ".csv")
    if not files:
        return None, 0
    with open(files[-1], "rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - 200))
        last = fh.read().decode("utf-8", "ignore").strip().splitlines()[-1]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = 0
    if days[-1].name == today:
        for f in days[-1].iterdir():
            if f.suffix == ".csv":
                with open(f, "rb") as fh:
                    count += max(0, sum(1 for _ in fh) - 1)
    try:
        return int(last.split(",")[0]), count
    except ValueError:
        return None, count


def cmd_status(args) -> int:
    from .storage import load_json
    data = Path(args.data)
    cfg = load_json(data / "config" / "symbols.json", None)
    if not cfg:
        print("Not set up yet: run  dresearch probe")
        return 2
    try:
        svc = subprocess.run(["systemctl", "is-active", "dresearch-collector"],
                             capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        svc = "unknown"
    now = time.time()
    print(f"Collector service: {svc}")
    print(f"{'symbol':10} {'last tick':>12} {'ticks today':>12}")
    healthy = svc == "active"
    for sym in cfg["collect"]:
        e, n = _last_live_epoch(data, sym)
        age = f"{now - e:.0f}s ago" if e else "never"
        if sym in cfg.get("watchdog", []) and (not e or now - e > 60):
            healthy = False
        print(f"{sym:10} {age:>12} {n:>12,}")
    du = shutil.disk_usage(data)
    used = sum(p.stat().st_size for p in (data / "raw").rglob("*") if p.is_file()) if (data / "raw").exists() else 0
    print(f"Data stored: {used / 1e6:,.1f} MB   Disk free: {du.free / 1e9:,.1f} GB")
    bf = load_json(data / "reports" / "backfill_progress.json", None)
    if bf:
        done = bf.get("hours_done_before_start", 0) + bf.get("hours_processed", 0)
        eta = "finished" if bf.get("finished") else f"about {bf.get('eta_hours', '?')} h left"
        reached = str(bf.get("oldest_hour_reached_utc", "-"))[:10]
        print(f"History download: {done:,}/{bf.get('hours_total', 0):,} symbol-hours ({eta}), "
              f"reached back to {reached}")
    reports = sorted((data / "reports").glob("audit_*.json")) if (data / "reports").exists() else []
    if reports:
        print(f"Latest audit: {reports[-1].name} -> {load_json(reports[-1], {}).get('overall')}")
    print("HEALTHY" if healthy else "CHECK NEEDED (see: journalctl -u dresearch-collector -n 50)")
    return 0 if healthy else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="dresearch", description="Deriv research data tools (read-only, no trading).")
    p.add_argument("--data", default=DEFAULT_DATA, help="data directory (default %(default)s)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="measure what the Deriv API provides; writes the collector config")
    sub.add_parser("collect", help="run the 24/7 recorder (normally via systemd)")
    a = sub.add_parser("audit", help="data-quality report for one UTC day (default: yesterday)")
    a.add_argument("--day")
    a.add_argument("--no-seal", action="store_true", help="don't compress/hash finished days")
    b = sub.add_parser("backfill", help="download past ticks (resumable; newest first)")
    b.add_argument("--days", type=int, default=90)
    b.add_argument("--per-minute", type=int, default=120, help="request rate cap (Deriv limit is 220)")
    sub.add_parser("status", help="is it running, and is data arriving?")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return {"probe": cmd_probe, "collect": cmd_collect, "audit": cmd_audit,
            "backfill": cmd_backfill, "status": cmd_status}[args.cmd](args)
