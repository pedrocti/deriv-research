"""Daily data-quality audit. Decides whether a day of data is fit for research.

Per symbol and UTC day it reports:
  completeness   unique ticks (live + history) / ticks expected from the fixed schedule
  live_completeness   the live feed on its own
  agreement      share of ticks present in both copies with identical prices
  gaps           runs of missing ticks in the combined record
  missingness    are live gaps linked to price moves? (biased data if yes)
Status OK requires completeness >= 98%, agreement >= 99.99% and no significant
missingness link (p >= 0.01). Synthetic indices only; gold is counted, not graded.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy import stats

from .storage import day_files, load_json, read_rows, save_json, seal_closed_days

MIN_COMPLETENESS = 0.98
MIN_AGREEMENT = 0.9999
MISSINGNESS_ALPHA = 0.01


def load(data_dir: Path, source: str, symbol: str, day: str) -> tuple[dict[int, str], int]:
    ticks: dict[int, str] = {}
    rows = 0
    for p in day_files(data_dir, source, symbol, day):
        for r in read_rows(p):
            rows += 1
            ticks[int(r["epoch"])] = r["quote"]
    return ticks, rows


def missingness_test(epochs: np.ndarray, union: dict[int, str], interval: float) -> dict:
    """Is a live gap more likely right after a large price move? Mann-Whitney on |log return|."""
    if len(epochs) < 100:
        return {"p_value": None, "note": "too few live ticks"}
    gap_after = np.diff(epochs) > 1.5 * interval
    n_gaps = int(gap_after.sum())
    if n_gaps < 5:
        return {"p_value": None, "gaps": n_gaps, "note": "fewer than 5 live gaps: nothing to test"}
    prev = [union.get(int(e - interval)) for e in epochs[:-1]]
    cur = [union[int(e)] for e in epochs[:-1]]
    absr = np.array([abs(math.log(float(c) / float(p))) if p else np.nan for p, c in zip(prev, cur)])
    ok = np.isfinite(absr)
    a, b = absr[ok & gap_after], absr[ok & ~gap_after]
    if len(a) < 5 or len(b) < 5:
        return {"p_value": None, "gaps": n_gaps, "note": "insufficient comparable returns"}
    res = stats.mannwhitneyu(a, b, alternative="two-sided")
    return {"p_value": float(res.pvalue), "gaps": n_gaps,
            "median_abs_return_before_gap": float(np.median(a)),
            "median_abs_return_otherwise": float(np.median(b))}


def gap_runs(sorted_epochs: np.ndarray, interval: float) -> list[tuple[int, int, int]]:
    """(first missing epoch, last missing epoch, ticks missing) for each hole."""
    out = []
    d = np.diff(sorted_epochs)
    for i in np.nonzero(d > 1.5 * interval)[0]:
        a, b = int(sorted_epochs[i]), int(sorted_epochs[i + 1])
        out.append((a + int(interval), b - int(interval), int(round((b - a) / interval)) - 1))
    return out


def audit_symbol(data_dir: Path, symbol: str, day: str, interval: float | None, graded: bool) -> dict:
    live, live_rows = load(data_dir, "live", symbol, day)
    hist, _ = load(data_dir, "history", symbol, day)
    union = {**hist, **live}
    res: dict = {"live_ticks": len(live), "history_ticks": len(hist), "combined_ticks": len(union),
                 "live_duplicate_rows": live_rows - len(live)}
    if not union:
        res["status"] = "NO DATA" if graded else "NO DATA (market may be closed)"
        return res
    both = set(live) & set(hist)
    res["agreement"] = (sum(1 for t in both if float(live[t]) == float(hist[t])) / len(both)) if both else None
    if not graded or not interval:
        res["status"] = "COUNTED (not graded)"
        return res
    ep = np.array(sorted(union), dtype=np.int64)
    day_start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    span_start, span_end = max(day_start, int(ep[0])), min(day_start + 86400 - 1, int(ep[-1]))
    expected_day = 86400 / interval
    expected_span = (span_end - span_start) / interval + 1
    # The schedule is fixed to the clock (epoch % interval == 0), the same grid the analysis uses.
    on = ep % int(interval) == 0
    off_grid = int(np.sum(~on))
    n_on = int(np.sum(on))
    live_arr = np.array(sorted(live), dtype=np.int64)
    n_live_on = int(np.sum(live_arr % int(interval) == 0))
    runs = gap_runs(ep, interval)
    live_ep = np.array(sorted(live), dtype=np.int64)
    res.update({
        "interval_s": interval,
        "off_schedule_ticks": off_grid,
        "completeness_full_day": n_on / expected_day,
        "completeness_within_recorded_span": n_on / expected_span,
        "live_completeness_within_recorded_span": n_live_on / expected_span,
        "gap_count": len(runs),
        "ticks_missing": int(sum(r[2] for r in runs)),
        "largest_gaps": [{"from_utc": datetime.fromtimestamp(a, timezone.utc).isoformat(),
                          "to_utc": datetime.fromtimestamp(b, timezone.utc).isoformat(), "missing": n}
                         for a, b, n in sorted(runs, key=lambda r: -r[2])[:5]],
        "missingness": missingness_test(live_ep, union, interval),
    })
    problems = []
    if res["completeness_full_day"] < MIN_COMPLETENESS:
        problems.append(f"completeness {res['completeness_full_day']:.2%} < {MIN_COMPLETENESS:.0%}")
    if res["agreement"] is not None and res["agreement"] < MIN_AGREEMENT:
        problems.append(f"live/history price agreement {res['agreement']:.4%}")
    p = res["missingness"].get("p_value")
    if p is not None and p < MISSINGNESS_ALPHA:
        problems.append(f"gaps linked to price moves (p={p:.3g})")
    if off_grid:
        problems.append(f"{off_grid} ticks off the {interval:g}s schedule")
    res["status"] = "OK" if not problems else "WARN: " + "; ".join(problems)
    return res


def run_audit(data_dir: Path, day: str | None = None, seal: bool = True) -> dict:
    data_dir = Path(data_dir)
    cfg = load_json(data_dir / "config" / "symbols.json", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day = day or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    watch = set(cfg.get("watchdog", []))
    intervals = cfg.get("intervals_s", {})
    report = {"day": day, "generated_utc": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    for sym in cfg.get("collect", []):
        report["symbols"][sym] = audit_symbol(data_dir, sym, day, intervals.get(sym), sym in watch)
    graded = [r for s, r in report["symbols"].items() if s in watch]
    report["overall"] = ("OK" if graded and all(r.get("status") == "OK" for r in graded)
                         else "ATTENTION" if graded else "NO SYMBOLS")
    if seal:
        report["files_sealed"] = seal_closed_days(data_dir, today)
    save_json(data_dir / "reports" / f"audit_{day}.json", report)
    return report


def summarise(report: dict) -> str:
    lines = ["", f"=== DATA AUDIT {report['day']} (UTC) — overall: {report['overall']} ===",
             f"{'symbol':10} {'complete':>9} {'live':>7} {'agree':>8} {'gaps':>5}  status"]
    for s, r in report["symbols"].items():
        c = r.get("completeness_full_day")
        lc = r.get("live_completeness_within_recorded_span")
        a = r.get("agreement")
        lines.append(f"{s:10} {(f'{c:.2%}' if c is not None else '-'):>9} "
                     f"{(f'{lc:.1%}' if lc is not None else '-'):>7} "
                     f"{(f'{a:.3%}' if a is not None else '-'):>8} {r.get('gap_count', '-')!s:>5}  {r['status']}")
    return "\n".join(lines)
