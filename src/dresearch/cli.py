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


def cmd_dashboard_serve(args) -> int:
    from .dashboard import make_server
    srv = make_server(Path(args.data), port=args.port)
    logging.getLogger("dresearch").info("dashboard listening on port %s", args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_dashboard_url(args) -> int:
    from .dashboard import dashboard_url
    print(dashboard_url(Path(args.data), args.port))
    return 0


def _code_commit(src: str = os.environ.get("DRESEARCH_SRC", "/opt/dresearch/src")) -> str | None:
    """Read the installed git commit without running git (the service user can't)."""
    try:
        g = Path(src) / ".git"
        head = (g / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head
        ref = head.split(" ", 1)[1]
        if (g / ref).exists():
            return (g / ref).read_text().strip()
        for line in (g / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    except OSError:
        return None
    return None


def cmd_freeze(args) -> int:
    from .analysis import freeze
    data = Path(args.data)
    existed = (data / "config" / "freeze.json").exists()
    rec = freeze(data, _code_commit())
    print(("Already frozen" if existed else "Protocol frozen") + f" at {rec['T0_utc']} (code {str(rec.get('code_commit'))[:7]}).")
    print("Data before this moment is DISCOVERY data. Data after it is the HOLDOUT, usable once,")
    print(f"no earlier than 28 days from the freeze.")
    return 0


def cmd_analyze(args) -> int:
    from .analysis import run_analysis, summarise
    report = run_analysis(Path(args.data), days=args.days, sims=args.sims, holdout=args.holdout)
    print(summarise(report))
    return 0


def cmd_import_mt5(args) -> int:
    import json
    from .storage import save_json
    src = Path(args.file)
    try:
        rep = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Could not read {src}: {exc}")
        return 2
    if not isinstance(rep, dict) or "symbols" not in rep or "account" not in rep:
        print("That file is not an mt5_snapshot JSON.")
        return 2
    stamp = str(rep.get("created_utc", ""))[:19].replace(":", "").replace("-", "")
    dest = Path(args.data) / "reports" / f"mt5_snapshot_{stamp or 'unknown'}.json"
    save_json(dest, rep)
    print(f"Imported {len(rep['symbols'])} instruments from a {rep['account'].get('mode')} account "
          f"({rep['account'].get('server')}) -> {dest}")
    for code, r in sorted(rep["symbols"].items()):
        h = r.get("history_24h") or {}
        pct = h.get("median_pct_of_price")
        print(f"  {code:10} median spread {h.get('median_points', '-')} pts"
              + (f" = {pct:.4f}% of price" if pct is not None else ""))
    print("The status page now compares these spreads with the break-even costs.")
    return 0


def cmd_candles(args) -> int:
    from .candles import run_candles
    rep = asyncio.run(run_candles(Path(args.data), hourly_days=args.hourly_days))
    print("\nSaved to", Path(args.data) / "candles")
    return 0 if rep.get("symbols") else 1


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
        eta = ("finished" if bf.get("finished") else
               str(bf.get("status")) if str(bf.get("status", "")).startswith(("waiting", "finished with", "stopped"))
               else f"about {bf.get('eta_hours', '?')} h left")
        if bf.get("rate_limited"):
            eta += f"; Deriv rate-limited {bf['rate_limited']:,} requests, all retried"
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
    cd = sub.add_parser("candles", help="Track B: download gold and crypto candle history")
    cd.add_argument("--hourly-days", type=int, default=730)
    im = sub.add_parser("import-mt5", help="load an mt5_snapshot JSON made on your Windows PC")
    im.add_argument("file")
    sub.add_parser("freeze", help="fix the analysis protocol and start the holdout clock (once)")
    z = sub.add_parser("analyze", help="run the Phase 0 pattern tests (normally via dresearch-analyze)")
    z.add_argument("--days", type=int, default=90)
    z.add_argument("--sims", type=int, default=100)
    z.add_argument("--holdout", action="store_true", help="the one-time test on data after the freeze")
    sub.add_parser("status", help="is it running, and is data arriving?")
    for name in ("dashboard-serve", "dashboard-url"):
        d = sub.add_parser(name, help="serve the status page" if name.endswith("serve") else "print the private page link")
        d.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8787")))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return {"probe": cmd_probe, "collect": cmd_collect, "audit": cmd_audit,
            "backfill": cmd_backfill, "status": cmd_status,
            "dashboard-serve": cmd_dashboard_serve, "dashboard-url": cmd_dashboard_url,
            "freeze": cmd_freeze, "analyze": cmd_analyze, "import-mt5": cmd_import_mt5,
            "candles": cmd_candles}[args.cmd](args)
