"""Probe: measure what Deriv's public API actually provides, before relying on it.

Answers, from the live service rather than from documentation:
  * which endpoint works;
  * exact symbol codes for the Volatility Indices, and whether gold is served;
  * each index's tick schedule, pip size, price level and per-tick volatility;
  * the price-rounding ratio (grid / per-tick move) that caused the starter kit's false signal;
  * the largest page ticks_history returns, and how far back tick history goes.
Writes <data>/reports/probe_<UTC>.json and <data>/config/symbols.json.
"""
from __future__ import annotations

import math
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import DEFAULT_ENDPOINTS, DerivError, connect_any
from .storage import save_json

VOL_RE = re.compile(r"^(R_(\d+)|1HZ(\d+)V)$")
CORE = ["R_10", "R_25", "R_50", "R_75", "R_100",
        "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V"]
DEPTH_DAYS = [1, 7, 30, 90, 180, 365, 730, 1095, 1825]
SECONDS_PER_YEAR_365 = 365 * 86400


def nominal_vol(symbol: str) -> float | None:
    m = VOL_RE.match(symbol)
    if not m:
        return None
    return float(m.group(2) or m.group(3)) / 100.0


def symbol_field(entry: dict, *names):
    for n in names:
        if n in entry and entry[n] not in (None, ""):
            return entry[n]
    return None


def classify_symbols(entries: list[dict]) -> dict:
    vols, gold, other = [], [], 0
    for e in entries:
        sym = symbol_field(e, "symbol", "underlying_symbol")
        name = str(symbol_field(e, "display_name", "underlying_symbol_name", "name") or "")
        if not sym:
            continue
        if VOL_RE.match(sym):
            vols.append({"symbol": sym, "display_name": name})
        elif "XAU" in sym.upper() or "gold" in name.lower():
            gold.append({"symbol": sym, "display_name": name,
                         "open": symbol_field(e, "exchange_is_open", "is_open")})
        else:
            other += 1
    return {"volatility": vols, "gold": gold, "other_count": other}


def tick_stats(times: list[int], prices: list[float], pip: int | None, symbol: str) -> dict:
    if len(times) < 50:
        return {"n": len(times), "note": "too few ticks"}
    diffs = [b - a for a, b in zip(times, times[1:])]
    interval = statistics.median(diffs)
    rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    sd = statistics.pstdev(rets) if len(rets) > 2 else float("nan")
    level = statistics.median(prices)
    grid = 10.0 ** (-pip) if pip is not None else float("nan")
    tick_move = level * sd
    ann_365 = sd * math.sqrt(SECONDS_PER_YEAR_365 / interval) if interval > 0 else float("nan")
    nom = nominal_vol(symbol)
    zero_moves = sum(1 for a, b in zip(prices, prices[1:]) if a == b) / (len(prices) - 1)
    return {
        "n": len(times),
        "interval_median_s": interval,
        "interval_share_at_median": sum(1 for d in diffs if d == interval) / len(diffs),
        "interval_max_s": max(diffs),
        "pip_size_decimals": pip,
        "price_level": level,
        "per_tick_log_return_sd": sd,
        "annualised_vol_365d_basis": ann_365,
        "nominal_vol_from_name": nom,
        "ratio_measured_to_nominal": (ann_365 / nom) if nom else None,
        "zero_move_share": zero_moves,
        "rounding_ratio_grid_over_tick_move": grid / tick_move if tick_move > 0 else None,
        "rounding_verdict": (
            "negligible (<0.05)" if tick_move > 0 and grid / tick_move < 0.05 else
            "MATTERS: null models must simulate rounding" if tick_move > 0 else "unknown"),
    }


async def _history(client, symbol, **kw):
    req = {"ticks_history": symbol, "style": "ticks", **kw}
    resp = await client.request(req)
    h = resp.get("history") or {}
    return [int(t) for t in h.get("times") or []], [float(p) for p in h.get("prices") or []], resp.get("pip_size")


async def run_probe(data_dir: Path, urls: list[str] | None = None, verbose=print) -> dict:
    t0 = time.time()
    report: dict = {"started_utc": datetime.now(timezone.utc).isoformat(), "errors": []}
    client, url, attempts = await connect_any(urls or DEFAULT_ENDPOINTS)
    report["endpoint"] = url
    report["endpoint_attempts"] = attempts
    verbose(f"[1/5] Connected: {url}")
    try:
        # --- symbols
        entries = []
        for req in ({"active_symbols": "brief", "product_type": "basic"}, {"active_symbols": "brief"}):
            try:
                resp = await client.request(req)
                entries = resp.get("active_symbols") or []
                if entries:
                    report["active_symbols_request"] = req
                    report["active_symbols_sample_keys"] = sorted(entries[0].keys())
                    break
            except DerivError as e:
                report["errors"].append(f"active_symbols {req}: {e}")
        classes = classify_symbols(entries)
        report["symbols"] = classes
        vol_syms = [v["symbol"] for v in classes["volatility"]]
        verbose(f"[2/5] {len(entries)} symbols listed; {len(vol_syms)} Volatility Indices; "
                f"gold: {[g['symbol'] for g in classes['gold']] or 'not found'}")

        # --- per-symbol tick schedule and rounding
        report["tick_stats"] = {}
        for sym in vol_syms + [g["symbol"] for g in classes["gold"]]:
            try:
                times, prices, pip = await _history(client, sym, end="latest", count=1000)
                report["tick_stats"][sym] = tick_stats(times, prices, pip, sym)
            except DerivError as e:
                report["tick_stats"][sym] = {"error": str(e)}
        verbose(f"[3/5] Tick schedule measured for {len(report['tick_stats'])} symbols")

        # --- page size the server honours
        ref = "1HZ100V" if "1HZ100V" in vol_syms else (vol_syms[0] if vol_syms else None)
        report["reference_symbol"] = ref
        if ref:
            caps = {}
            for c in (5000, 20000, 100000):
                try:
                    times, _, _ = await _history(client, ref, end="latest", count=c,
                                                 start=int(time.time()) - 3 * 86400)
                    caps[str(c)] = len(times)
                except DerivError as e:
                    caps[str(c)] = f"error: {e}"
            report["page_size_test"] = caps
            got = [v for v in caps.values() if isinstance(v, int)]
            report["history_page_size"] = max(got) if got else 1000
            verbose(f"[4/5] Largest page returned: {report['history_page_size']} ticks")

            # --- how far back tick history goes
            depth = {}
            now = int(time.time())
            for d in DEPTH_DAYS:
                end = now - d * 86400
                try:
                    times, _, _ = await _history(client, ref, start=end - 600, end=end, count=50)
                    depth[str(d)] = len(times)
                except DerivError as e:
                    depth[str(d)] = f"error: {e}"
            report["tick_depth_test_days_back"] = depth
            ok = [d for d in DEPTH_DAYS if isinstance(depth.get(str(d)), int) and depth[str(d)] > 0]
            report["tick_history_days_available_at_least"] = max(ok) if ok else 0
            verbose(f"[5/5] Tick history reaches back at least {report['tick_history_days_available_at_least']} days "
                    f"(tested up to {DEPTH_DAYS[-1]})")
    finally:
        await client.close()

    # --- config for the collector, derived from measurements, never typed by hand
    core = [s for s in CORE if s in report.get("tick_stats", {}) and "interval_median_s" in report["tick_stats"][s]]
    gold = [g["symbol"] for g in report.get("symbols", {}).get("gold", [])][:1]
    cfg = {
        "endpoint": report["endpoint"],
        "collect": core + gold,
        "watchdog": core,                      # always-open synthetics only
        "intervals_s": {s: report["tick_stats"][s]["interval_median_s"] for s in core},
        "history_page_size": report.get("history_page_size", 1000),
        "generated_from_probe_utc": report["started_utc"],
    }
    report["collector_config"] = cfg
    report["duration_s"] = round(time.time() - t0, 1)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_json(Path(data_dir) / "reports" / f"probe_{stamp}.json", report)
    save_json(Path(data_dir) / "config" / "symbols.json", cfg)
    return report


def summarise(report: dict) -> str:
    lines = ["", "=== PROBE SUMMARY ===", f"Endpoint: {report.get('endpoint')}"]
    cfg = report.get("collector_config", {})
    lines.append(f"Will collect: {', '.join(cfg.get('collect', [])) or 'NOTHING (problem)'}")
    lines.append(f"History page size: {report.get('history_page_size')}  |  "
                 f"tick history depth: >= {report.get('tick_history_days_available_at_least')} days")
    lines.append("")
    lines.append(f"{'symbol':10} {'tick s':>6} {'pip':>4} {'price':>12} {'ann.vol':>8} {'nominal':>8} {'rounding':>9}")
    for s, st in report.get("tick_stats", {}).items():
        if "interval_median_s" not in st:
            lines.append(f"{s:10} {st.get('error') or st.get('note')}")
            continue
        rr = st["rounding_ratio_grid_over_tick_move"]
        nom = st["nominal_vol_from_name"]
        lines.append(f"{s:10} {st['interval_median_s']:>6} {st['pip_size_decimals']!s:>4} "
                     f"{st['price_level']:>12.4f} {st['annualised_vol_365d_basis']:>8.3f} "
                     f"{(f'{nom:.2f}' if nom else '-'):>8} {(f'{rr:.4f}' if rr else '-'):>9}")
    if report.get("errors"):
        lines.append("")
        lines.append("Non-fatal errors: " + "; ".join(report["errors"]))
    lines.append("")
    lines.append("Note: 1000 ticks is a small sample; ann.vol here is a sanity check, not a test.")
    return "\n".join(lines)
