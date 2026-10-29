"""Read-only MT5 snapshot for the Deriv research project. Runs on Windows.

What it records, for each Volatility Index (2s and 1s versions of 10/25/50/75/100) and gold:
  * contract specs: digits, point, contract size, volume limits, swaps, calc mode
  * the REAL bid/ask spread: from the last 24 h of MT5 ticks, plus a live sample
  * the MT5 server clock offset from UTC
  * account type (demo/real), server and currency. It does NOT record your login
    number, name, balance or anything that identifies you.

It never places, changes or closes an order: it imports no order functions.
Output: mt5_snapshot_<UTC time>.json in the folder you run it from.

Usage (in PowerShell, in the folder with this file):
    py mt5_snapshot.py                 # specs + 24 h spreads + 5-minute live sample
    py mt5_snapshot.py --minutes 15    # longer live sample
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

TARGETS = [
    (re.compile(r"^Volatility (10|25|50|75|100) Index$"), lambda n: f"R_{n}"),
    (re.compile(r"^Volatility (10|25|50|75|100) \(1s\) Index$"), lambda n: f"1HZ{n}V"),
    (re.compile(r"^XAUUSD$"), lambda n: "frxXAUUSD"),
]
SPEC_FIELDS = ["digits", "point", "spread", "spread_float", "trade_tick_size", "trade_tick_value",
               "trade_contract_size", "volume_min", "volume_max", "volume_step", "swap_long",
               "swap_short", "swap_mode", "swap_rollover3days", "trade_calc_mode", "trade_mode",
               "trade_stops_level", "margin_initial", "currency_margin", "currency_profit", "path"]
ACCOUNT_MODES = {0: "DEMO", 1: "CONTEST", 2: "REAL"}


def match_symbols(names: list[str]) -> dict[str, str]:
    """MT5 symbol name -> Deriv API code, for the instruments this project studies."""
    out = {}
    for name in names:
        for rx, code in TARGETS:
            m = rx.match(name)
            if m:
                out[name] = code(m.group(1) if m.groups() else "")
    return out


def spread_stats(bids, asks) -> dict:
    sp, rel = [], []
    for b, a in zip(bids, asks):
        if b > 0 and a > 0 and a >= b:
            sp.append(a - b)
            rel.append((a - b) / ((a + b) / 2))
    if not sp:
        return {"n": 0}
    rel_sorted = sorted(rel)
    sp_sorted = sorted(sp)
    q = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))]
    return {"n": len(sp), "median_points": statistics.median(sp), "p95_points": q(sp_sorted, 0.95),
            "max_points": sp_sorted[-1], "min_points": sp_sorted[0],
            "median_pct_of_price": 100 * statistics.median(rel), "p95_pct_of_price": 100 * q(rel_sorted, 0.95),
            "share_constant": sp.count(statistics.median(sp)) / len(sp)}


def fields(obj, names) -> dict:
    return {n: getattr(obj, n) for n in names if hasattr(obj, n)}


def run(mt5, minutes: float, out_dir: str = ".") -> dict:
    if not mt5.initialize():
        raise SystemExit(f"Could not connect to MT5 ({mt5.last_error()}).\n"
                         "Open MetaTrader 5, log in to your Deriv DEMO account, then run this again.")
    try:
        acc = mt5.account_info()
        term = mt5.terminal_info()
        if acc is None:
            raise SystemExit("MT5 is open but not logged in. Log in to your Deriv demo account first.")
        report = {
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "tool": "mt5_snapshot.py 1.0 (read-only)",
            "account": {"mode": ACCOUNT_MODES.get(acc.trade_mode, str(acc.trade_mode)),
                        "server": acc.server, "company": acc.company, "currency": acc.currency,
                        "leverage": acc.leverage},
            "terminal": {"build": getattr(term, "build", None), "connected": getattr(term, "connected", None)},
            "mt5_package_version": getattr(mt5, "__version__", None),
            "symbols": {},
        }
        names = [s.name for s in (mt5.symbols_get() or [])]
        found = match_symbols(names)
        expected = {f"R_{n}" for n in (10, 25, 50, 75, 100)} | {f"1HZ{n}V" for n in (10, 25, 50, 75, 100)} | {"frxXAUUSD"}
        report["not_found"] = sorted(expected - set(found.values()))
        print(f"Account: {report['account']['mode']} on {acc.server}. Found {len(found)} of 11 instruments.")
        now = datetime.now(timezone.utc)
        offsets = []
        for name, code in sorted(found.items(), key=lambda kv: kv[1]):
            mt5.symbol_select(name, True)
            info = mt5.symbol_info(name)
            rec = {"mt5_name": name, "specs": fields(info, SPEC_FIELDS) if info else None}
            # The terminal returns ticks in SERVER time; ask for a wide window and keep the last 24 h.
            ticks = mt5.copy_ticks_range(name, now - timedelta(hours=30), now + timedelta(hours=14),
                                         mt5.COPY_TICKS_ALL)
            if ticks is not None and len(ticks):
                last_ms = int(ticks[-1]["time_msc"])
                cut = last_ms - 24 * 3600 * 1000
                sel = [t for t in ticks if int(t["time_msc"]) >= cut]
                rec["history_24h"] = spread_stats([float(t["bid"]) for t in sel], [float(t["ask"]) for t in sel])
                rec["history_24h"]["first_server_ms"] = int(sel[0]["time_msc"])
                rec["history_24h"]["last_server_ms"] = last_ms
                if code.startswith("1HZ"):
                    offsets.append(last_ms / 1000 - time.time())
            else:
                rec["history_24h"] = {"n": 0, "error": str(mt5.last_error())}
            report["symbols"][code] = rec
            print(f"  {code:10} {name:28} 24h spread median "
                  f"{rec['history_24h'].get('median_points', '-')} pts")
        if offsets:
            off = statistics.median(offsets)
            report["server_clock_offset_s"] = off
            report["server_utc_offset_hours"] = round(off / 900) * 0.25
        # live sample: pull every tick the terminal received (not just the latest quote)
        print(f"Sampling live spreads for {minutes:g} minutes ... (keep MT5 open)")
        end = time.time() + minutes * 60
        last = {name: None for name in found}
        live = {name: ([], []) for name in found}
        while time.time() < end:
            for name in found:
                frm = last[name] or (datetime.now(timezone.utc) - timedelta(minutes=1) +
                                     timedelta(seconds=report.get("server_clock_offset_s", 0)))
                ts = mt5.copy_ticks_from(name, frm, 100000, mt5.COPY_TICKS_ALL)
                if ts is not None and len(ts):
                    for t in ts:
                        live[name][0].append(float(t["bid"]))
                        live[name][1].append(float(t["ask"]))
                    last[name] = datetime.fromtimestamp(int(ts[-1]["time_msc"]) / 1000 + 0.001, timezone.utc)
            time.sleep(2)
        for name, code in found.items():
            report["symbols"][code]["live_sample"] = spread_stats(*live[name])
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = f"{out_dir.rstrip('/')}/mt5_snapshot_{stamp}.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        print(f"\nSaved {path}")
        print(f"{'index':10} {'median spread':>14} {'% of price':>11}")
        for code, rec in sorted(report["symbols"].items()):
            h = rec.get("history_24h", {})
            pct = h.get("median_pct_of_price")
            pct_txt = f"{pct:.4f}%" if pct is not None else "-"
            print(f"{code:10} {str(h.get('median_points', '-')):>14} {pct_txt:>11}")
        report["_path"] = path
        return report
    finally:
        mt5.shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Read-only MT5 spread and spec snapshot (no trading).")
    p.add_argument("--minutes", type=float, default=5)
    a = p.parse_args(argv)
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("The MetaTrader5 package is missing. Run:  py -m pip install MetaTrader5==5.0.6231")
        return 2
    run(mt5, a.minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
