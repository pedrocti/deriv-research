"""Track C protocol v1.0: Ethereum, hours to days, sized for a $100 account.
Fixed and committed BEFORE any Track C result was seen. Read this before reading results.

WHY ETHEREUM ONLY: on Deriv MT5 the smallest trade is 0.1 ETH (~$224 at $2,244), versus
0.01 BTC (~$773) and 1 oz gold (~$3,870). Only ETH lets a $100 account keep risk near a
few % per trade. BTC is run with identical rules for information only (it needs ~$1,200).

THE THREE IDEAS (each has a reason someone else might pay us; nothing else is tested)
  S1 CRASH BOUNCE  At the close of hour t, if that hour's return <= -3 x the standard
     deviation of the previous 168 hourly returns (min 120), go LONG at the next hour's
     open, hold up to 24 h.  Reason: cascading forced liquidations overshoot.
  S2 DAY FOLLOW-THROUGH  At each UTC day close, if the day's return >= +2 x the std of the
     previous 30 daily returns go LONG; if <= -2 x go SHORT; enter at the next open, hold
     up to 48 h.  Reason: information is absorbed over days, not instantly.
  S3 HOUR OF DAY  In DISCOVERY only, test the mean return of each UTC hour (24 t-tests).
     If the best hour passes Bonferroni (24 x p < 0.05), trade that hour (direction = sign
     of its mean, 1 h hold) in HOLDOUT. Otherwise S3 = NO SIGNAL.  Reason: scheduled
     institutional flows. Selection happens on discovery data only.
  Stops: S1 and S2 are each tested with a 0.9% stop (= 2% of $100 at 0.1 ETH) and a 1.8%
  stop (= 4%). S3 uses 0.9%. Five variants in all; Holm correction across the five.

EXECUTION (hourly bars, deliberately pessimistic)
  * one position at a time; signals use only data up to the signal bar's close
  * entry at the next bar's open; exit at the stop, or at the close of the last bar held
  * a bar that opens beyond the stop exits at that open (gap); if a bar could hit the stop
    at any point, it is assumed to have done so
  * costs: half of Deriv's measured ETH spread (0.026%) on entry and exit, plus 0.02%
    slippage per side (assumed), plus Deriv's swap of 15%/yr on the position value, charged
    per hour held. Today's costs are applied to all history (an approximation)

PERIODS  DISCOVERY 2017-09-01 .. 2022-12-31     HOLDOUT 2023-01-01 .. latest

DECISION (fixed now; same structure as Track B)
  CANDIDATE     discovery Holm-adjusted one-sided p < 0.05, AND holdout mean net return
                per trade > 0 with one-sided p < 0.10, AND holdout mean still > 0 with
                costs doubled  ->  forward demo trial only
  NOT SUPPORTED discovery fails, or holdout mean <= 0
  INCONCLUSIVE  anything else

ACCOUNT SIMULATION (shown, never used for the decision): $100, always 0.1 ETH, trading
halts for the rest of a month after a -$10 month, and stops for good at -$20 overall.
"""
from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import stats

from .storage import save_json

SWING_VERSION = "1.0"
COSTS = {"half_spread": 0.00013, "slippage": 0.0002, "swap_per_year": 0.15}
DISCOVERY = ("2017-09-01", "2023-01-01")
HOLDOUT_START = "2023-01-01"
VARIANTS = [("S1 crash bounce", "S1", 0.009), ("S1 crash bounce", "S1", 0.018),
            ("S2 day follow-through", "S2", 0.009), ("S2 day follow-through", "S2", 0.018),
            ("S3 hour of day", "S3", 0.009)]
MIN_LOT_ETH = 0.1
MIN_LOT_BTC = 0.01          # Deriv MT5 minimum; used only for the $100 account line


def load_hourly(path: Path) -> dict[str, np.ndarray]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append((int(r["epoch"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    a = np.array(sorted(rows))
    return {"t": a[:, 0].astype(np.int64), "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4]}


def hourly_returns(d) -> np.ndarray:
    r = np.full(len(d["c"]), np.nan)
    consecutive = np.diff(d["t"]) == 3600
    r[1:] = np.where(consecutive, d["c"][1:] / d["c"][:-1] - 1, np.nan)
    return r


def rolling_std_prev(x: np.ndarray, window: int, min_n: int) -> np.ndarray:
    """std of x[t-window .. t-1] (strictly before t)."""
    out = np.full(len(x), np.nan)
    for t in range(min_n, len(x)):
        w = x[max(0, t - window):t]
        w = w[np.isfinite(w)]
        if len(w) >= min_n:
            out[t] = np.std(w, ddof=1)
    return out


# ------------------------------------------------------------------ signals: (bar, direction, hold)
def signals_s1(d) -> list[tuple[int, int, int]]:
    r = hourly_returns(d)
    sd = rolling_std_prev(r, 168, 120)
    idx = np.nonzero(np.isfinite(r) & np.isfinite(sd) & (r <= -3 * sd))[0]
    return [(int(i), +1, 24) for i in idx]


def daily_closes(d) -> tuple[np.ndarray, np.ndarray]:
    """Index of the bar closing each UTC day (the 23:00 bar) and that day's close."""
    hours = (d["t"] // 3600) % 24
    idx = np.nonzero(hours == 23)[0]
    return idx, d["c"][idx]


def signals_s2(d) -> list[tuple[int, int, int]]:
    idx, closes = daily_closes(d)
    days = d["t"][idx] // 86400
    out = []
    rets = np.full(len(idx), np.nan)
    rets[1:] = np.where(np.diff(days) == 1, closes[1:] / closes[:-1] - 1, np.nan)
    for k in range(31, len(idx)):
        prev = rets[k - 30:k]
        prev = prev[np.isfinite(prev)]
        if len(prev) < 20 or not np.isfinite(rets[k]):
            continue
        sd = np.std(prev, ddof=1)
        if rets[k] >= 2 * sd:
            out.append((int(idx[k]), +1, 48))
        elif rets[k] <= -2 * sd:
            out.append((int(idx[k]), -1, 48))
    return out


def select_s3_hour(d, start: str, end: str) -> dict:
    """Discovery-only selection of an hour-of-day effect (bar return = close/open - 1)."""
    lab = labels(d)
    m = (lab >= start) & (lab < end)
    hours = ((d["t"] // 3600) % 24)[m]
    br = (d["c"] / d["o"] - 1)[m]
    best = None
    for h in range(24):
        x = br[hours == h]
        if len(x) < 100:
            continue
        t, p = stats.ttest_1samp(x, 0.0)
        if best is None or p < best["p"]:
            best = {"hour": h, "mean": float(x.mean()), "p": float(p), "n": int(len(x))}
    if best is None:
        return {"hour": None}
    best["p_bonferroni"] = min(1.0, best["p"] * 24)
    best["direction"] = 1 if best["mean"] > 0 else -1
    best["selected"] = best["p_bonferroni"] < 0.05
    return best


def signals_s3(d, sel: dict) -> list[tuple[int, int, int]]:
    """Signal at the bar BEFORE the chosen hour, so entry is at the chosen hour's open."""
    if not sel.get("selected"):
        return []
    hours = (d["t"] // 3600) % 24
    target = sel["hour"]
    idx = np.nonzero(hours == (target - 1) % 24)[0]
    return [(int(i), sel["direction"], 1) for i in idx if i + 1 < len(d["t"])]


# ------------------------------------------------------------------ execution
def simulate(d, sigs, stop: float, cost_mult: float = 1.0) -> list[dict]:
    hs = COSTS["half_spread"] * cost_mult
    slip = COSTS["slippage"] * cost_mult
    swap_h = COSTS["swap_per_year"] * cost_mult / 8760
    o, h, l, c, t = d["o"], d["h"], d["l"], d["c"], d["t"]
    n = len(c)
    trades, busy_until = [], -1
    for i, dr, hold in sigs:
        e = i + 1
        if e >= n or e <= busy_until:
            continue
        entry = o[e] * (1 + dr * (hs + slip))
        stop_px = entry * (1 - dr * stop)
        last = min(n - 1, e + hold - 1)
        exit_px, j, reason = None, e, "time"
        for j in range(e, last + 1):
            if j > e and t[j] - t[j - 1] != 3600:          # data gap: close at the last bar before it
                j -= 1
                exit_px, reason = c[j], "gap"
                break
            if dr > 0:
                if j > e and o[j] <= stop_px:
                    exit_px, reason = o[j], "stop (gapped)"
                    break
                if l[j] <= stop_px:
                    exit_px, reason = stop_px, "stop"
                    break
            else:
                if j > e and o[j] >= stop_px:
                    exit_px, reason = o[j], "stop (gapped)"
                    break
                if h[j] >= stop_px:
                    exit_px, reason = stop_px, "stop"
                    break
        if exit_px is None:
            exit_px, j = c[last], last
        exit_net = exit_px * (1 - dr * (hs + slip))
        hours_held = j - e + 1
        gross = dr * (exit_net - entry) / entry
        net = gross - swap_h * hours_held
        trades.append({"t": int(t[e]), "dir": dr, "entry": float(entry), "exit": float(exit_net),
                       "hours": int(hours_held), "net": float(net), "reason": reason})
        busy_until = j
    return trades


def trade_stats(trades: list[dict], stop: float) -> dict:
    x = np.array([tr["net"] for tr in trades])
    if len(x) < 5:
        return {"n": int(len(x))}
    sd = float(np.std(x, ddof=1))
    t = float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else float("nan")
    return {"n": int(len(x)), "win_rate": float(np.mean(x > 0)), "mean_net_pct": 100 * float(x.mean()),
            "median_net_pct": 100 * float(np.median(x)), "expectancy_R": float(x.mean() / stop),
            "stopped_share": float(np.mean([tr["reason"].startswith("stop") for tr in trades])),
            "t": t, "p_one_sided": float(stats.t.sf(t, len(x) - 1)) if math.isfinite(t) else float("nan")}


def account_sim(trades: list[dict], start_equity: float = 100.0, lot: float = MIN_LOT_ETH) -> dict:
    eq, peak, maxdd = start_equity, start_equity, 0.0
    month_pnl, month, halted_months, stopped, taken = 0.0, None, set(), False, 0
    for tr in trades:
        m = datetime.fromtimestamp(tr["t"], timezone.utc).strftime("%Y-%m")
        if m != month:
            month, month_pnl = m, 0.0
        if stopped or m in halted_months:
            continue
        pnl = (tr["exit"] - tr["entry"]) * tr["dir"] * lot - \
            COSTS["swap_per_year"] / 8760 * tr["hours"] * tr["entry"] * lot
        eq += pnl
        taken += 1
        month_pnl += pnl
        peak = max(peak, eq)
        maxdd = min(maxdd, eq - peak)
        if month_pnl <= -10:
            halted_months.add(m)
        if eq <= start_equity - 20:
            stopped = True
    return {"start": start_equity, "end": round(eq, 2), "trades_taken": taken,
            "max_drawdown_usd": round(maxdd, 2), "months_halted": len(halted_months),
            "stopped_for_good": stopped}


def labels(d) -> np.ndarray:
    return np.array([datetime.fromtimestamp(int(x), timezone.utc).strftime("%Y-%m-%d") for x in d["t"]])


def verdict(p_holm: float, hold: dict, hold2: dict) -> str:
    if not (p_holm < 0.05):
        return "NOT SUPPORTED"
    if hold.get("n", 0) < 5 or not (hold["mean_net_pct"] > 0):
        return "NOT SUPPORTED"
    if hold["p_one_sided"] < 0.10 and hold2.get("mean_net_pct", -1) > 0:
        return "CANDIDATE"
    return "INCONCLUSIVE"


def run_market(path: Path, lot: float = MIN_LOT_ETH) -> dict:
    d = load_hourly(path)
    lab = labels(d)
    sel = select_s3_hour(d, *DISCOVERY)
    sigs = {"S1": signals_s1(d), "S2": signals_s2(d), "S3": signals_s3(d, sel)}

    def split(trs):
        tl = [datetime.fromtimestamp(tr["t"], timezone.utc).strftime("%Y-%m-%d") for tr in trs]
        disc = [tr for tr, x in zip(trs, tl) if DISCOVERY[0] <= x < DISCOVERY[1]]
        hold = [tr for tr, x in zip(trs, tl) if x >= HOLDOUT_START]
        return disc, hold

    res = {"first": str(lab[0]), "last": str(lab[-1]), "bars": int(len(lab)), "s3_selection": sel, "variants": {}}
    for name, key, stop in VARIANTS:
        vk = f"{name} | stop {stop * 100:.1f}%"
        trs = simulate(d, sigs[key], stop)
        trs2 = simulate(d, sigs[key], stop, cost_mult=2.0)
        dsc, hld = split(trs)
        _, hld2 = split(trs2)
        res["variants"][vk] = {"discovery": trade_stats(dsc, stop), "holdout": trade_stats(hld, stop),
                               "holdout_costs_2x": trade_stats(hld2, stop),
                               "account_holdout": account_sim(hld, lot=lot)}
    p = {k: v["discovery"].get("p_one_sided", 1.0) if v["discovery"].get("n", 0) >= 5 else 1.0
         for k, v in res["variants"].items()}
    items = sorted(p.items(), key=lambda kv: kv[1])
    ph, run = {}, 0.0
    for i, (k, v) in enumerate(items):
        run = max(run, min(1.0, (len(items) - i) * (v if math.isfinite(v) else 1.0)))
        ph[k] = run
    for k, v in res["variants"].items():
        v["discovery_p_holm"] = ph[k]
        v["verdict"] = verdict(ph[k], v["holdout"], v["holdout_costs_2x"])
    return res


def run_swing(data_dir: Path, code_commit: str | None = None) -> dict:
    ext = Path(data_dir) / "external"
    if not (ext / "binance_ETHUSDT_1h.csv").exists():
        raise SystemExit("No hourly Ethereum data yet. Run `dresearch external` first.")
    report = {"swing_version": SWING_VERSION, "code_commit": code_commit,
              "generated_utc": datetime.now(timezone.utc).isoformat(), "costs": COSTS,
              "ETH": run_market(ext / "binance_ETHUSDT_1h.csv")}
    btc = ext / "binance_BTCUSDT_1h.csv"
    if btc.exists():
        report["BTC_information_only"] = run_market(btc, lot=MIN_LOT_BTC)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_json(Path(data_dir) / "reports" / f"swing_{stamp}.json", report)
    return report


def summarise(r: dict) -> str:
    def row(name, s):
        if s.get("n", 0) < 5:
            return f"    {name:22} {s.get('n', 0)} trades (too few)"
        return (f"    {name:22} {s['n']:5d} trades  win {100 * s['win_rate']:4.0f}%  mean {s['mean_net_pct']:+.3f}%/trade "
                f"({s['expectancy_R']:+.2f}R)  stopped {100 * s['stopped_share']:3.0f}%  t {s['t']:5.2f}  p {s['p_one_sided']:.3f}")
    out = [f"=== ETHEREUM HOURS-TO-DAYS TEST v{r['swing_version']} ===",
           f"costs: spread {2 * r['costs']['half_spread'] * 100:.3f}% + slippage {r['costs']['slippage'] * 100:.2f}%/side "
           f"+ swap {r['costs']['swap_per_year'] * 100:.0f}%/yr"]
    for mk in ("ETH", "BTC_information_only"):
        if mk not in r:
            continue
        m = r[mk]
        sel = m["s3_selection"]
        out += ["", f"{mk.replace('_', ' ')}  ({m['first']} to {m['last']}, {m['bars']:,} hourly bars)",
                f"  S3 hour picked on discovery data: "
                + (f"{sel['hour']:02d}:00 UTC, direction {'+' if sel.get('direction', 0) > 0 else '-'}, "
                   f"Bonferroni p {sel['p_bonferroni']:.3f} -> {'SELECTED' if sel.get('selected') else 'NO SIGNAL'}"
                   if sel.get("hour") is not None else "none")]
        for k, v in m["variants"].items():
            a = v["account_holdout"]
            out += [f"  {k}   Holm p (discovery) {v['discovery_p_holm']:.3f}   VERDICT: {v['verdict']}",
                    row("discovery 2017-2022", v["discovery"]), row("holdout 2023-now", v["holdout"]),
                    row("holdout, costs x2", v["holdout_costs_2x"]),
                    f"    $100 account, 2023-now: ends ${a['end']:.2f} after {a['trades_taken']} trades, "
                    f"worst drop ${a['max_drawdown_usd']:.2f}, {a['months_halted']} month(s) halted"
                    + (", STOPPED at -$20" if a["stopped_for_good"] else "")]
    return "\n".join(out)
