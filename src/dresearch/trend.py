"""Track B protocol: does time-series momentum (trend-following) earn money on gold and
crypto AFTER Deriv's real costs?  Fixed and committed BEFORE any Track B result was seen.

WHY THIS RULE: "who pays you?" -> investors who under-react to information and then
chase it (behavioural), documented across asset classes (Moskowitz, Ooi & Pedersen 2012,
"Time series momentum"). It is the one candidate with a published economic rationale.
We test it; we do not search for new rules. Anything not listed here is out of scope.

RULE (identical for every asset; only the time unit differs)
  signal_t   = sign(P[t-1] / P[t-1-L] - 1)                 (uses only past prices)
  weight_t   = signal_t * min(cap, target_vol / realised_vol[t-1])
  gross_t    = weight_t * (P[t] / P[t-1] - 1)
  financing  = long : -|w| * (rf + markup) * dt   (gold: historical 3-month T-bill rate)
               short: +|w| * (rf - markup) * dt
               crypto: Deriv's quoted yearly swap for long/short, constant
  trading    = |w_t - w_{t-1}| * spread_pct (round trip = one spread)
  net_t      = gross_t + financing_t - trading_t

GOLD  (monthly; months from 1971-10, after the $35 peg ended in Aug 1971)
  L in {3, 6, 12} months, PRIMARY L = 12 (the canonical published horizon)
  realised vol: last 24 monthly returns (min 12); target 10%/yr; cap 2x
  markup 1.15%/yr each side: half the gap between Deriv's XAUUSD swaps measured
    30 Oct 2026 (long -4.13%/yr, short +1.83%/yr) -> neutral ~2.98%, markup ~1.15%
  spread 0.015% per unit traded (2x the measured 0.0073%, conservative)
  DISCOVERY  1971-10 .. 2012-12  (before the 2012 publication)
  HOLDOUT    2013-01 .. latest    (after publication: tests whether it survived)

CRYPTO (daily; BTCUSDT and ETHUSDT from Binance, traded as Deriv CFDs)
  L in {7, 28, 90} days; evaluated on the equal-weight BTC+ETH portfolio
  realised vol: last 60 daily returns (min 30); target 20%/yr; cap 1x
  costs: Deriv's crypto swaps and spreads from the MT5 snapshot. Until those are
    imported, crypto results are reported GROSS and the crypto verdict is PENDING.
  DISCOVERY  2017-09 .. 2022-12      HOLDOUT  2023-01 .. latest

PAXG (gold token, daily 2020-08..): L in {20, 60, 120} days, reported for information
only; it never decides a verdict (too short, and it overlaps the gold holdout).

STATISTICS
  mean net return, annualised return and volatility, Sharpe ratio, max drawdown,
  Newey-West t-statistic (lag 3 monthly, 10 daily), one-sided p for mean > 0.
  Holm correction across the 3 lookbacks within each market, in DISCOVERY.
  Benchmarks shown alongside: buy-and-hold via CFD (long financing paid), and cash (0).

DECISION (per market; fixed now)
  CANDIDATE     DISCOVERY: the primary (gold) / best-after-Holm (crypto) lookback has
                Holm-adjusted one-sided p < 0.05; AND HOLDOUT: same lookback, net mean
                > 0 with one-sided p < 0.10; AND HOLDOUT net Sharpe still > 0 when costs
                are doubled.  -> next step is a forward demo trial, never real money.
  NOT SUPPORTED DISCOVERY fails, or HOLDOUT net mean <= 0.
  INCONCLUSIVE  anything else (typically: holdout positive but too short to be sure).
Power, stated up front: at a true Sharpe of 0.4 the gold holdout (~14 years) gives
t ~ 1.5 on average, so INCONCLUSIVE is a likely honest outcome, not a failure of the test.
"""
from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import stats

from .storage import load_json, save_json

TREND_VERSION = "1.0"
GOLD = {"lookbacks": [3, 6, 12], "primary": 12, "vol_window": 24, "vol_min": 12, "target_vol": 0.10,
        "cap": 2.0, "markup": 0.0115, "spread": 0.00015, "periods_per_year": 12, "nw_lag": 3,
        "start": "1971-10", "holdout_start": "2013-01"}
CRYPTO = {"lookbacks": [7, 28, 90], "vol_window": 60, "vol_min": 30, "target_vol": 0.20, "cap": 1.0,
          "periods_per_year": 365, "nw_lag": 10, "start": "2017-09-01", "holdout_start": "2023-01-01",
          "default_spread": 0.001}
PAXG = {"lookbacks": [20, 60, 120], "vol_window": 60, "vol_min": 30, "target_vol": 0.10, "cap": 2.0,
        "periods_per_year": 365, "nw_lag": 10, "spread": 0.0003}


# ------------------------------------------------------------------ the rule
def backtest(prices: np.ndarray, L: int, vol_window: int, vol_min: int, target_vol: float, cap: float,
             periods_per_year: int, spread: float, fin_long: np.ndarray, fin_short: np.ndarray) -> dict:
    """Returns per-period arrays. fin_long/fin_short: financing rate per year at each period
    (fin_long > 0 is a cost when long; fin_short > 0 is a credit when short)."""
    n = len(prices)
    ret = np.full(n, np.nan)
    ret[1:] = prices[1:] / prices[:-1] - 1
    w = np.zeros(n)
    for t in range(1, n):
        lo = t - 1 - L
        if lo < 0:
            continue
        past = ret[max(1, t - vol_window):t]               # returns up to and including t-1
        past = past[np.isfinite(past)]
        if len(past) < vol_min:
            continue
        vol = float(np.std(past, ddof=1)) * math.sqrt(periods_per_year)
        if vol <= 0:
            continue
        sig = np.sign(prices[t - 1] / prices[lo] - 1)
        w[t] = sig * min(cap, target_vol / vol)
    dt = 1.0 / periods_per_year
    gross = w * np.nan_to_num(ret)
    fin = np.where(w > 0, -w * fin_long * dt, -w * fin_short * dt)   # w<0: -w*fin_short>0 is a credit
    trade = np.abs(np.diff(np.concatenate([[0.0], w]))) * spread
    net = gross + fin - trade
    return {"w": w, "ret": ret, "gross": gross, "fin": fin, "trade": trade, "net": net}


def newey_west_t(x: np.ndarray, lag: int) -> float:
    x = np.asarray(x, float)
    n = len(x)
    if n < 10:
        return float("nan")
    m = x.mean()
    e = x - m
    s = float(np.dot(e, e)) / n
    for k in range(1, lag + 1):
        s += 2 * (1 - k / (lag + 1)) * float(np.dot(e[:-k], e[k:])) / n
    return m / math.sqrt(s / n) if s > 0 else float("nan")


def summary(x: np.ndarray, ppy: int, lag: int) -> dict:
    x = np.asarray(x, float)
    if len(x) < 10:
        return {"n": int(len(x))}
    eq = np.cumprod(1 + x)
    dd = float(np.min(eq / np.maximum.accumulate(eq) - 1))
    t = newey_west_t(x, lag)
    sd = float(np.std(x, ddof=1))
    return {"n": int(len(x)), "years": round(len(x) / ppy, 2),
            "ann_return_pct": 100 * float((eq[-1]) ** (ppy / len(x)) - 1),
            "ann_vol_pct": 100 * sd * math.sqrt(ppy),
            "sharpe": float(x.mean() / sd * math.sqrt(ppy)) if sd > 0 else float("nan"),
            "max_drawdown_pct": 100 * dd, "nw_t": t,
            "p_one_sided": float(stats.norm.sf(t)) if math.isfinite(t) else float("nan")}


def holm(p: dict[str, float]) -> dict[str, float]:
    items = sorted(p.items(), key=lambda kv: kv[1])
    out, run = {}, 0.0
    for i, (k, v) in enumerate(items):
        run = max(run, min(1.0, (len(items) - i) * v))
        out[k] = run
    return out


def verdict(disc_p_holm: float, hold: dict, hold_2x: dict) -> str:
    if not (disc_p_holm < 0.05):
        return "NOT SUPPORTED"
    if not hold.get("n") or not (hold.get("ann_return_pct", -1) > 0 and np.isfinite(hold.get("sharpe", np.nan))
                                 and hold["sharpe"] > 0):
        return "NOT SUPPORTED"
    if hold["p_one_sided"] < 0.10 and hold_2x.get("sharpe", -1) > 0:
        return "CANDIDATE"
    return "INCONCLUSIVE"


# ------------------------------------------------------------------ data
def read_monthly(path: Path, col: str) -> dict[str, float]:
    with open(path, encoding="utf-8") as fh:
        return {r["month"]: float(r[col]) for r in csv.DictReader(fh)}


def read_daily(path: Path) -> tuple[list[str], np.ndarray]:
    dates, px = [], []
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            dates.append(datetime.fromtimestamp(int(r["epoch"]), timezone.utc).date().isoformat())
            px.append(float(r["close"]))
    return dates, np.array(px)


def _split(labels: list[str], x: np.ndarray, start: str, hold: str):
    lab = np.array(labels)
    d = (lab >= start) & (lab < hold)
    h = lab >= hold
    return x[d], x[h], x[(lab >= start)]


# ------------------------------------------------------------------ markets
def run_gold(ext: Path) -> dict:
    gold = read_monthly(ext / "gold_monthly_worldbank.csv", "usd_per_oz")
    tb = read_monthly(ext / "tbill_3m_monthly_fred.csv", "pct_per_year")
    months = sorted(m for m in gold if m >= "1970-01")      # warm-up before 1971-10 for the lookback
    months = [m for m in months if m in tb]
    px = np.array([gold[m] for m in months])
    rf = np.array([tb[m] / 100 for m in months])
    rf_prev = np.concatenate([[rf[0]], rf[:-1]])            # rate known at the start of the month
    c = GOLD
    res = {"protocol": {k: v for k, v in c.items()}, "lookbacks": {}, "first_month": months[0],
           "last_month": months[-1]}
    disc_p = {}
    for L in c["lookbacks"]:
        out = {}
        for mult in (1, 2):
            b = backtest(px, L, c["vol_window"], c["vol_min"], c["target_vol"], c["cap"], 12,
                         c["spread"] * mult, rf_prev + c["markup"] * mult, rf_prev - c["markup"] * mult)
            d, h, f = _split(months, b["net"], c["start"], c["holdout_start"])
            key = "costs" if mult == 1 else "costs_2x"
            out[key] = {"discovery": summary(d, 12, c["nw_lag"]), "holdout": summary(h, 12, c["nw_lag"]),
                        "full": summary(f, 12, c["nw_lag"])}
            if mult == 1:
                _, _, wf = _split(months, b["w"], c["start"], c["holdout_start"])
                out["share_of_months_long"] = float(np.mean(wf > 0))
                gd, gh, _ = _split(months, b["gross"], c["start"], c["holdout_start"])
                out["gross_holdout"] = summary(gh, 12, c["nw_lag"])
        res["lookbacks"][str(L)] = out
        disc_p[str(L)] = out["costs"]["discovery"].get("p_one_sided", 1.0)
    # buy & hold through a CFD: always long 1x, paying long financing
    ret = np.concatenate([[0.0], px[1:] / px[:-1] - 1])
    bh_net = ret - (rf_prev + c["markup"]) / 12
    d, h, f = _split(months, bh_net, c["start"], c["holdout_start"])
    res["buy_and_hold_cfd"] = {"discovery": summary(d, 12, 3), "holdout": summary(h, 12, 3)}
    ph = holm(disc_p)
    res["discovery_p_holm"] = ph
    P = str(c["primary"])
    res["verdict"] = verdict(ph[P], res["lookbacks"][P]["costs"]["holdout"],
                             res["lookbacks"][P]["costs_2x"]["holdout"])
    res["verdict_lookback"] = P
    return res


def run_crypto(ext: Path, costs: dict | None) -> dict:
    c = CRYPTO
    series = {}
    for sym in ("BTCUSDT", "ETHUSDT"):
        dates, px = read_daily(ext / f"binance_{sym}_1d.csv")
        series[sym] = dict(zip(dates, px))
    dates = sorted(set(series["BTCUSDT"]) & set(series["ETHUSDT"]))
    have_costs = bool(costs)
    res = {"protocol": c, "costs_used": costs or "PENDING: import the MT5 snapshot with BTCUSD/ETHUSD",
           "lookbacks": {}, "first_day": dates[0], "last_day": dates[-1]}
    disc_p = {}
    for L in c["lookbacks"]:
        out = {}
        for mult in ((1, 2) if have_costs else (0,)):
            nets = []
            for sym, code in (("BTCUSDT", "cryBTCUSD"), ("ETHUSDT", "cryETHUSD")):
                px = np.array([series[sym][d] for d in dates])
                k = (costs or {}).get(code, {})
                # snapshot convention: negative % = you pay. backtest convention: fin_long > 0 is a
                # cost when long, fin_short > 0 is a credit when short.
                fin_long = -k.get("long_pct_per_year", 0.0) / 100
                fin_short = k.get("short_pct_per_year", 0.0) / 100
                spread = k.get("spread_pct", c["default_spread"] * 100) / 100
                if mult == 2:                     # "costs doubled": one more unit of every cost
                    fin_long, fin_short, spread = fin_long + abs(fin_long), fin_short - abs(fin_short), 2 * spread
                if mult == 0:
                    fin_long = fin_short = spread = 0.0
                fl, fs, sp = np.full(len(px), fin_long), np.full(len(px), fin_short), spread
                b = backtest(px, L, c["vol_window"], c["vol_min"], c["target_vol"], c["cap"], 365, sp, fl, fs)
                nets.append(b["net"] if mult else b["gross"])
            port = 0.5 * (nets[0] + nets[1])
            d, h, f = _split(dates, port, c["start"], c["holdout_start"])
            key = {0: "gross", 1: "costs", 2: "costs_2x"}[mult]
            out[key] = {"discovery": summary(d, 365, c["nw_lag"]), "holdout": summary(h, 365, c["nw_lag"]),
                        "full": summary(f, 365, c["nw_lag"])}
        res["lookbacks"][str(L)] = out
        disc_p[str(L)] = out["costs" if have_costs else "gross"]["discovery"].get("p_one_sided", 1.0)
    ph = holm(disc_p)
    best = min(ph, key=ph.get)
    res["discovery_p_holm"] = ph
    res["verdict_lookback"] = best
    res["verdict"] = (verdict(ph[best], res["lookbacks"][best]["costs"]["holdout"],
                              res["lookbacks"][best]["costs_2x"]["holdout"]) if have_costs
                      else "PENDING (costs not imported; gross results shown)")
    return res


def run_paxg(ext: Path) -> dict:
    dates, px = read_daily(ext / "binance_PAXGUSDT_1d.csv")
    c = PAXG
    out = {}
    zero = np.zeros(len(px))
    for L in c["lookbacks"]:
        b = backtest(px, L, c["vol_window"], c["vol_min"], c["target_vol"], c["cap"], 365, c["spread"],
                     zero + 0.04, zero + 0.02)
        out[str(L)] = summary(b["net"][L + c["vol_min"]:], 365, c["nw_lag"])
    return {"note": "information only (financing approximated at -4%/+2% per year)", "lookbacks": out,
            "first_day": dates[0], "last_day": dates[-1]}


def crypto_costs_from_snapshot(data: Path) -> dict | None:
    rep = Path(data) / "reports"
    snaps = sorted(rep.glob("mt5_snapshot_*.json")) if rep.exists() else []
    for sp in reversed(snaps):
        s = load_json(sp, {}).get("symbols", {})
        out = {}
        for code in ("cryBTCUSD", "cryETHUSD"):
            r = s.get(code) or {}
            sw = r.get("swap_annual") or {}
            h = r.get("history_24h") or {}
            if "long_pct_per_year" in sw and h.get("median_pct_of_price") is not None:
                out[code] = {"long_pct_per_year": sw["long_pct_per_year"],
                             "short_pct_per_year": sw["short_pct_per_year"],
                             "spread_pct": h["median_pct_of_price"]}
        if len(out) == 2:
            out["source"] = sp.name
            return out
    return None


def run_trend(data_dir: Path, code_commit: str | None = None) -> dict:
    data = Path(data_dir)
    ext = data / "external"
    report = {"trend_version": TREND_VERSION, "code_commit": code_commit,
              "generated_utc": datetime.now(timezone.utc).isoformat(),
              "gold": run_gold(ext), "crypto": run_crypto(ext, crypto_costs_from_snapshot(data)),
              "paxg": run_paxg(ext)}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_json(data / "reports" / f"trend_{stamp}.json", report)
    return report


def summarise(r: dict) -> str:
    def row(name, s):
        if not s or s.get("n", 0) < 10:
            return f"  {name:26} (too little data)"
        return (f"  {name:26} {s['years']:5.1f}y  return {s['ann_return_pct']:6.1f}%/yr  vol {s['ann_vol_pct']:5.1f}%  "
                f"Sharpe {s['sharpe']:5.2f}  maxDD {s['max_drawdown_pct']:6.1f}%  t {s['nw_t']:5.2f}  p {s['p_one_sided']:.3f}")
    g = r["gold"]
    out = [f"=== TREND TEST v{r['trend_version']} ===", "",
           f"GOLD (monthly, {GOLD['start']} to {g['last_month']}); costs = T-bill + 1.15% markup, spread 0.015%"]
    for L, v in g["lookbacks"].items():
        tag = " (PRIMARY)" if L == g["verdict_lookback"] else ""
        out.append(f" lookback {L} months{tag}: long {100 * v['share_of_months_long']:.0f}% of months; "
                   f"Holm p (discovery) {g['discovery_p_holm'][L]:.3f}")
        out += [row("discovery 1971-2012", v["costs"]["discovery"]), row("holdout 2013-now", v["costs"]["holdout"]),
                row("holdout, costs doubled", v["costs_2x"]["holdout"])]
    out += [" buy & hold via CFD (paying long financing):",
            row("discovery", g["buy_and_hold_cfd"]["discovery"]), row("holdout", g["buy_and_hold_cfd"]["holdout"]),
            f" GOLD VERDICT: {g['verdict']}", ""]
    c = r["crypto"]
    out.append(f"CRYPTO (BTC+ETH equal weight, daily, {CRYPTO['start']} to {c['last_day']}); costs: "
               f"{'from ' + c['costs_used']['source'] if isinstance(c['costs_used'], dict) else c['costs_used']}")
    for L, v in c["lookbacks"].items():
        k = "costs" if "costs" in v else "gross"
        out.append(f" lookback {L} days ({k}): Holm p (discovery) {c['discovery_p_holm'][L]:.3f}")
        out += [row("discovery 2017-2022", v[k]["discovery"]), row("holdout 2023-now", v[k]["holdout"])]
    out += [f" CRYPTO VERDICT: {c['verdict']}", "",
            "PAXG gold token (information only):"]
    for L, s in r["paxg"]["lookbacks"].items():
        out.append(row(f"lookback {L} days", s))
    return "\n".join(out)
