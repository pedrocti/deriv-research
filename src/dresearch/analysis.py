"""Phase 0 analysis: does a Volatility Index deviate from a fair random process,
and could any deviation pay for its trading costs?

Design (fixed before any real data was analysed; see PROTOCOL in the README):

* Null model N1 ("Deriv's advertised process"): a driftless-in-price random walk
  with the measured per-tick volatility, rounded to the index's quoted decimals,
  with the SAME missing-tick pattern as the real data, restarted from the real price
  every 6 hours so its price level (and therefore its rounding distortion) matches. Every statistic is
  computed identically on the real series and on `sims` simulated series; each
  statistic is then standardised against its simulated mean and spread. This is
  what stops price rounding from faking mean reversion (it fooled the starter kit).
* Tests per index (one family, Holm-corrected at 1%):
    H1 drift            mean log return per tick
    H3a tick memory     autocorrelation, lags 1-50 (max |z|, Sidak-combined)
    H3b 1-min memory    autocorrelation of 1-min returns, lags 1-10
    H3c 5-min memory    autocorrelation of 5-min returns, lags 1-10
    H4  trend/reversal  variance ratios at 1, 5, 15, 60 min (max |z|)
    H5  volatility      autocorrelation of |1-min returns|, lags 1-50 (Deriv claims constant volatility)
    H6a hour-of-day     mean 1-min return by UTC hour (chi-square of 24 z-scores)
    H6b hour-of-day     size of 1-min moves by UTC hour
  H2 (measured vs advertised volatility) is reported as a fidelity check, not an edge test.
* Economic test, per horizon h in {1, 5, 15, 60} min: the simplest momentum/reversal rule
  (trade the sign of the last h-minute return, hold h minutes). Its information
  coefficient IC (correlation of past and next h-return) has a 95% bound U. A rule of
  this kind needs IC > c / (sigma_h * sqrt(2/pi)) to beat a round-trip cost c, so it is
  conclusively unprofitable at every cost above  c_min = U * sigma_h * sqrt(2/pi).
  c_min is reported in % and in index points, to compare with the real MT5 spread.
* Data: everything strictly before the freeze time T0 is discovery data. Data after T0
  is the holdout; it can be analysed once, no earlier than 28 days after T0.
"""
from __future__ import annotations

import gzip
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy import stats

from .storage import day_files, load_json, save_json

HORIZONS_MIN = [1, 5, 15, 60]
ACF_LAGS_TICK = 50
ACF_LAGS_AGG = 10
FAMILY_ALPHA = 0.01
HOLDOUT_MIN_DAYS = 28
SQRT_2_OVER_PI = math.sqrt(2 / math.pi)
BLOCK_S = 6 * 3600        # simulated walks restart from the real price every 6 h (level matching)


# ------------------------------------------------------------------ loading
def _read_day(data: Path, sym: str, day: str) -> tuple[np.ndarray, np.ndarray]:
    cache = data / "cache" / sym / f"{day}.npz"
    files = day_files(data, "history", sym, day) + day_files(data, "live", sym, day)
    if not files:
        return np.empty(0, np.int64), np.empty(0)
    sig = sum(p.stat().st_size for p in files) * 1000 + len(files)
    if cache.exists():
        z = np.load(cache)
        if int(z["sig"]) == sig:
            return z["e"], z["p"]
    ticks: dict[int, float] = {}
    for p in files:                                  # history first, live fills any hole
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt", encoding="utf-8") as fh:
            next(fh, None)
            for line in fh:
                a, _, rest = line.partition(",")
                q = rest.split(",", 1)[0]
                try:
                    ticks.setdefault(int(a), float(q))
                except ValueError:
                    pass
    e = np.fromiter(sorted(ticks), np.int64, len(ticks))
    pr = np.array([ticks[k] for k in e], dtype=np.float64)
    if day < datetime.now(timezone.utc).strftime("%Y-%m-%d"):
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez(cache, e=e, p=pr, sig=np.int64(sig))
    return e, pr


def load_grid(data: Path, sym: str, t0: int, t1: int, interval: int) -> dict:
    """Prices on the fixed tick schedule [t0, t1); NaN where a tick is missing."""
    n = (t1 - t0) // interval
    P = np.full(n, np.nan)
    off_grid = 0
    day = datetime.fromtimestamp(t0, timezone.utc).date()
    last = datetime.fromtimestamp(t1 - 1, timezone.utc).date()
    while day <= last:
        e, p = _read_day(data, sym, day.isoformat())
        if len(e):
            m = (e >= t0) & (e < t1)
            e, p = e[m], p[m]
            on = (e - t0) % interval == 0
            off_grid += int((~on).sum())
            P[(e[on] - t0) // interval] = p[on]
        day += timedelta(days=1)
    return {"P": P, "t0": t0, "interval": interval, "off_grid": off_grid,
            "coverage": float(np.isfinite(P).mean()) if n else 0.0}


# ------------------------------------------------------------------ statistics
def _acf(x: np.ndarray, valid: np.ndarray, lags: int, cnt: np.ndarray | None) -> np.ndarray:
    """Autocorrelation at lags 1..lags allowing holes (x is 0 where invalid)."""
    m = x[valid].mean() if valid.any() else 0.0
    y = np.where(valid, x - m, 0.0)
    var = float(np.dot(y, y)) / max(1, int(valid.sum()))
    out = np.empty(lags)
    n = len(y)
    for k in range(1, lags + 1):
        c = cnt[k - 1] if cnt is not None else n - k
        out[k - 1] = (float(np.dot(y[:-k], y[k:])) / max(1, c)) / var if var > 0 else 0.0
    return out


def _pair_counts(valid: np.ndarray, lags: int) -> np.ndarray | None:
    if valid.all():
        return None
    v = valid.astype(np.float64)
    return np.array([float(np.dot(v[:-k], v[k:])) for k in range(1, lags + 1)])


def _block_sums(r: np.ndarray, valid: np.ndarray, h: int):
    """Overlapping h-tick sums S[t] = r[t..t+h-1], and whether each window is complete."""
    cs = np.concatenate([[0.0], np.cumsum(np.where(valid, r, 0.0))])
    cv = np.concatenate([[0], np.cumsum(valid.astype(np.int64))])
    s = cs[h:] - cs[:-h]
    ok = (cv[h:] - cv[:-h]) == h
    return s, ok


class Context:
    """Things that depend only on the missing-tick pattern, shared by real data and sims."""

    def __init__(self, valid: np.ndarray, t0: int, interval: int):
        self.valid = valid
        self.interval = interval
        self.n = int(valid.sum())
        self.cnt_tick = _pair_counts(valid, ACF_LAGS_TICK)
        self.h_ticks = {hm: max(1, hm * 60 // interval) for hm in HORIZONS_MIN}
        h1 = self.h_ticks[1]
        starts = np.arange(0, len(valid) - h1 + 1, h1)
        self.hour_1m = ((t0 + starts * interval) // 3600) % 24


def compute_stats(r: np.ndarray, ctx: Context) -> dict[str, np.ndarray]:
    v = ctx.valid
    rv = np.where(v, r, 0.0)
    out: dict[str, np.ndarray] = {}
    out["H1_mean"] = np.array([rv.sum() / ctx.n])
    out["H3a_acf"] = _acf(rv, v, ACF_LAGS_TICK, ctx.cnt_tick)
    for name, hm in (("H3b_acf1m", 1), ("H3c_acf5m", 5)):
        h = ctx.h_ticks[hm]
        s, ok = _block_sums(r, v, h)
        s, ok = s[::h], ok[::h]
        out[name] = _acf(np.where(ok, s, 0.0), ok, ACF_LAGS_AGG, _pair_counts(ok, ACF_LAGS_AGG))
    var1 = float(np.dot(rv - out["H1_mean"][0] * v, rv - out["H1_mean"][0] * v)) / ctx.n
    vr, ic, sig_h = [], [], []
    for hm in HORIZONS_MIN:
        h = ctx.h_ticks[hm]
        s, ok = _block_sums(r, v, h)
        sv = s[ok]
        vr.append(float(np.var(sv)) / (h * var1) if len(sv) > 2 and var1 > 0 else 1.0)
        sig_h.append(float(np.std(sv)) if len(sv) > 2 else 0.0)
        past, fut = s[:-h], s[h:]
        both = ok[:-h] & ok[h:]
        ic.append(float(np.corrcoef(past[both], fut[both])[0, 1]) if both.sum() > 10 else 0.0)
    out["H4_vr"] = np.array(vr)
    out["IC"] = np.array(ic)
    out["sigma_h"] = np.array(sig_h)
    # 1-minute returns: rounding is ~8x smaller relative to the move than at tick level
    h1 = ctx.h_ticks[1]
    s1, ok1 = _block_sums(r, v, h1)
    s1, ok1 = s1[::h1], ok1[::h1]
    a1 = np.abs(s1)
    out["H5_absacf"] = _acf(np.where(ok1, a1, 0.0), ok1, ACF_LAGS_TICK, _pair_counts(ok1, ACF_LAGS_TICK))
    hv = ctx.hour_1m[ok1]
    cnt = np.bincount(hv, minlength=24).astype(float)
    out["H6a_hourmean"] = np.bincount(hv, weights=s1[ok1], minlength=24) / np.maximum(cnt, 1)
    # relative to the overall average move, so an error in the overall volatility level
    # (which shifts every hour together) cannot masquerade as an hour-of-day pattern
    hab = np.bincount(hv, weights=a1[ok1], minlength=24) / np.maximum(cnt, 1)
    out["H6b_hourabs"] = hab / (a1[ok1].mean() if ok1.any() else 1.0)
    out["per_tick_sd"] = np.array([math.sqrt(var1)])
    return out


def simulate_returns(P_real: np.ndarray, sigma: float, pip: int | None,
                     rng: np.random.Generator, block: int) -> np.ndarray:
    """N1: martingale price, constant volatility, quoted to `pip` decimals, same holes.

    The walk restarts from the real price at the start of every `block` ticks, so the
    simulated price LEVEL tracks the real one. Rounding distorts small moves by an
    amount that depends on the level, so this keeps the rounding effect comparable.
    The return across each restart is excluded from real and simulated data alike.
    """
    n = len(P_real)
    z = rng.standard_normal(n) * sigma - 0.5 * sigma * sigma
    starts = np.arange(0, n, block)
    z[starts] = 0.0
    seg = np.cumsum(z)
    seg -= np.repeat(seg[starts], np.diff(np.append(starts, n)))
    anchor = np.empty(n)
    fin = np.isfinite(P_real)
    for b0 in starts:
        blk = P_real[b0:b0 + block]
        ok = np.isfinite(blk)
        anchor[b0:b0 + block] = blk[ok][0] if ok.any() else np.nan
    latent = anchor * np.exp(seg)
    q = np.round(latent, pip) if pip is not None else latent
    q[~fin] = np.nan
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.diff(np.log(q))


# ------------------------------------------------------------------ inference
def _z_pooled(obs: np.ndarray, sims: np.ndarray, weights: np.ndarray | None = None) -> np.ndarray:
    """Standardise components that share one null spread (e.g. autocorrelation lags, or
    hourly means after scaling by sqrt(count)). Pooling over components gives a precise
    spread estimate even with few simulations, so normal tails are justified."""
    w = np.ones(obs.shape[-1]) if weights is None else weights
    mu = sims.mean(axis=0)
    resid = (sims - mu) * w
    sd = float(resid.std(ddof=1)) * math.sqrt(1 + 1 / len(sims))
    return np.where(sd > 0, (obs - mu) * w / sd, 0.0)


def _p_components(obs: np.ndarray, sims: np.ndarray) -> np.ndarray:
    """Two-sided p per component when each has its own spread: Student-t with B-1 df,
    inflated by sqrt(1+1/B), because the spread itself is estimated from B simulations."""
    B = len(sims)
    mu = sims.mean(axis=0)
    sd = sims.std(axis=0, ddof=1) * math.sqrt(1 + 1 / B)
    t = np.where(sd > 0, (obs - mu) / sd, 0.0)
    return 2 * stats.t.sf(np.abs(t), B - 1)


def _sidak_from_z(z: np.ndarray) -> float:
    k = len(z)
    p1 = 2 * stats.norm.sf(np.max(np.abs(z)))
    return float(1 - (1 - p1) ** k) if p1 > 1e-12 else float(k * p1)


def _sidak_from_p(p: np.ndarray) -> float:
    k = len(p)
    p1 = float(np.min(p))
    return float(1 - (1 - p1) ** k) if p1 > 1e-12 else float(k * p1)


def holm(pvals: dict[str, float]) -> dict[str, float]:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    adj, running = {}, 0.0
    for i, (k, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        adj[k] = running
    return adj


def benjamini_hochberg(pvals: dict[str, float]) -> dict[str, float]:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    q, prev = {}, 1.0
    for i in range(m - 1, -1, -1):
        k, p = items[i]
        prev = min(prev, p * m / (i + 1))
        q[k] = prev
    return q


def analyse_symbol(P: np.ndarray, t0: int, interval: int, pip: int | None,
                   nominal_vol: float | None, sims: int = 100, seed: int = 20261029,
                   progress=lambda *_: None) -> dict:
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.diff(np.log(P))
    block = max(1, BLOCK_S // interval)
    valid = np.isfinite(r) & ((np.arange(len(r)) + 1) % block != 0)
    if valid.sum() < 10_000:
        return {"status": "INSUFFICIENT DATA", "valid_returns": int(valid.sum())}
    ctx = Context(valid, t0, interval)
    r = np.where(valid, r, np.nan)
    obs = compute_stats(r, ctx)
    # Latent per-tick volatility from 5-minute moves: rounding inflates tick-level variance
    # (by about grid^2/6 per tick) but adds almost nothing over 5 minutes.
    h5 = ctx.h_ticks[5]
    s5, ok5 = _block_sums(r, valid, h5)
    sigma = float(np.std(s5[ok5][::h5]) / math.sqrt(h5)) if ok5.sum() > 100 * h5 else float(obs["per_tick_sd"][0])
    rng = np.random.default_rng(seed)
    sim_stats: dict[str, list] = {k: [] for k in obs}
    t_start = time.time()
    for b in range(sims):
        s = compute_stats(simulate_returns(P, sigma, pip, rng, block), ctx)
        for k in obs:
            sim_stats[k].append(s[k])
        if (b + 1) % 10 == 0:
            progress(b + 1, sims, time.time() - t_start)
    S = {k: np.vstack(v) for k, v in sim_stats.items()}
    hour_w = np.sqrt(np.maximum(np.bincount(ctx.hour_1m[_block_sums(r, valid, ctx.h_ticks[1])[1][::ctx.h_ticks[1]]],
                                            minlength=24), 1).astype(float))
    Z = {"H3a_acf": _z_pooled(obs["H3a_acf"], S["H3a_acf"]),
         "H3b_acf1m": _z_pooled(obs["H3b_acf1m"], S["H3b_acf1m"]),
         "H3c_acf5m": _z_pooled(obs["H3c_acf5m"], S["H3c_acf5m"]),
         "H5_absacf": _z_pooled(obs["H5_absacf"], S["H5_absacf"]),
         "H6a_hourmean": _z_pooled(obs["H6a_hourmean"], S["H6a_hourmean"], hour_w),
         "H6b_hourabs": _z_pooled(obs["H6b_hourabs"], S["H6b_hourabs"], hour_w)}

    p = {
        "H1 drift": float(_p_components(obs["H1_mean"], S["H1_mean"])[0]),
        "H3a tick memory (lags 1-50)": _sidak_from_z(Z["H3a_acf"]),
        "H3b 1-min memory": _sidak_from_z(Z["H3b_acf1m"]),
        "H3c 5-min memory": _sidak_from_z(Z["H3c_acf5m"]),
        "H4 trend/reversal (VR 1-60 min)": _sidak_from_p(_p_components(obs["H4_vr"], S["H4_vr"])),
        "H5 volatility clustering": _sidak_from_z(Z["H5_absacf"]),
        "H6a hour-of-day direction": float(stats.chi2.sf(float(np.sum(Z["H6a_hourmean"] ** 2)), 24)),
        "H6b hour-of-day move size": float(stats.chi2.sf(float(np.sum(Z["H6b_hourabs"] ** 2)), 23)),
    }
    p_holm = holm(p)
    deviations = [k for k, v in p_holm.items() if v < FAMILY_ALPHA]

    level = float(np.nanmedian(P[-min(len(P), 86400 // interval):]))
    econ = []
    for i, hm in enumerate(HORIZONS_MIN):
        ic = float(obs["IC"][i])
        sd_null = float(S["IC"][:, i].std(ddof=1)) * math.sqrt(1 + 1 / sims)
        tq = float(stats.t.ppf(0.975, sims - 1))
        lo, hi = ic - tq * sd_null, ic + tq * sd_null
        U = max(abs(lo), abs(hi))
        sig_h = float(obs["sigma_h"][i])
        c_min = U * sig_h * SQRT_2_OVER_PI
        econ.append({"horizon_min": hm, "IC": ic, "IC_95_low": lo, "IC_95_high": hi,
                     "IC_significant": bool(lo > 0 or hi < 0),
                     "sigma_h_pct": 100 * sig_h,
                     "no_edge_if_round_trip_cost_above_pct": 100 * c_min,
                     "no_edge_if_round_trip_cost_above_points": c_min * level})
    secs_per_year = 365 * 86400
    ann = sigma * math.sqrt(secs_per_year / interval)        # from 5-min moves: unaffected by rounding
    return {
        "status": "DEVIATION FOUND (investigate; not yet an edge)" if deviations
        else "CONSISTENT WITH A FAIR RANDOM PROCESS",
        "deviations": deviations,
        "valid_returns": int(valid.sum()), "coverage": float(np.isfinite(P).mean()),
        "price_level": level, "pip": pip, "sims": sims,
        "H2_vol": {"measured_annual_365d": ann, "nominal": nominal_vol,
                   "ratio": ann / nominal_vol if nominal_vol else None},
        "p_values": p, "p_holm": p_holm,
        "z_detail": {"H3a_max_abs_z": float(np.max(np.abs(Z["H3a_acf"]))),
                     "note": "z-scores standardised against the level-matched, rounded null",
                     "H3a_lag1_acf": float(obs["H3a_acf"][0]),
                     "H3a_lag1_acf_null_mean": float(S["H3a_acf"][:, 0].mean()),
                     "H4_vr": dict(zip([f"{h}m" for h in HORIZONS_MIN], map(float, obs["H4_vr"]))),
                     "H5_max_abs_z": float(np.max(np.abs(Z["H5_absacf"])))},
        "economics": econ,
    }


# ------------------------------------------------------------------ orchestration
def freeze(data: Path, git_commit: str | None = None) -> dict:
    p = data / "config" / "freeze.json"
    existing = load_json(p, None)
    if existing:
        return existing
    rec = {"T0_epoch": int(time.time()), "T0_utc": datetime.now(timezone.utc).isoformat(),
           "code_commit": git_commit, "protocol": "analysis.py module docstring, version below",
           "analysis_version": ANALYSIS_VERSION}
    save_json(p, rec)
    return rec


ANALYSIS_VERSION = "1.2"


def run_analysis(data: Path, days: int = 90, sims: int = 100, holdout: bool = False,
                 symbols: list[str] | None = None, verbose=print) -> dict:
    data = Path(data)
    cfg = load_json(data / "config" / "symbols.json", None)
    fz = load_json(data / "config" / "freeze.json", None)
    if not cfg:
        raise SystemExit("No config. Run  dresearch probe  first.")
    if not fz:
        raise SystemExit("The protocol is not frozen yet. Run  dresearch freeze  once, then re-run.")
    T0 = int(fz["T0_epoch"])
    now = int(time.time())
    marker = data / "reports" / "HOLDOUT_USED.json"
    if holdout:
        if marker.exists():
            raise SystemExit(f"The holdout was already analysed ({load_json(marker, {}).get('utc')}). "
                             "It can be used once only.")
        if now - T0 < HOLDOUT_MIN_DAYS * 86400:
            raise SystemExit(f"Holdout opens {HOLDOUT_MIN_DAYS} days after the freeze "
                             f"({datetime.fromtimestamp(T0 + HOLDOUT_MIN_DAYS * 86400, timezone.utc):%Y-%m-%d}).")
        t0, t1 = (T0 // 3600 + 1) * 3600, (now // 3600 - 1) * 3600
    else:
        t1 = T0 // 3600 * 3600
        t0 = t1 - days * 86400
    probe_stats = {}
    rep_dir = data / "reports"
    probes = sorted(rep_dir.glob("probe_*.json")) if rep_dir.exists() else []
    if probes:
        probe_stats = load_json(probes[-1], {}).get("tick_stats", {})
    syms = symbols or list(cfg.get("watchdog", []))
    report = {"mode": "HOLDOUT" if holdout else "DISCOVERY", "analysis_version": ANALYSIS_VERSION,
              "freeze": fz, "window_utc": [datetime.fromtimestamp(t0, timezone.utc).isoformat(),
                                           datetime.fromtimestamp(t1, timezone.utc).isoformat()],
              "started_utc": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    prog = rep_dir / "analysis_progress.json"
    for i, sym in enumerate(syms):
        interval = int((cfg.get("intervals_s") or {}).get(sym, 1))
        st = probe_stats.get(sym, {})
        pip = st.get("pip_size_decimals")
        pip = int(pip) if pip is not None else None
        nominal = st.get("nominal_vol_from_name")
        verbose(f"[{i + 1}/{len(syms)}] {sym}: loading {days if not holdout else '-'} days ...")
        g = load_grid(data, sym, t0, t1, interval)
        if g["coverage"] < 0.5:
            report["symbols"][sym] = {"status": "INSUFFICIENT DATA", "coverage": g["coverage"]}
            verbose(f"      coverage {g['coverage']:.0%}: skipped (need 50%+; is the download finished?)")
            continue

        def progress(b, n, el, sym=sym, i=i):
            save_json(prog, {"symbol": sym, "symbol_index": i + 1, "symbols": len(syms),
                             "sims_done": b, "sims": n, "elapsed_s": round(el),
                             "updated_utc": datetime.now(timezone.utc).isoformat()})
        res = analyse_symbol(g["P"], t0, interval, pip, nominal, sims=sims, progress=progress)
        res["off_schedule_ticks"] = g["off_grid"]
        report["symbols"][sym] = res
        verbose(f"      coverage {g['coverage']:.1%}  ->  {res['status']}")
    allp = {f"{s} | {k}": v for s, r in report["symbols"].items() for k, v in (r.get("p_values") or {}).items()}
    report["bh_q_values"] = benjamini_hochberg(allp) if allp else {}
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_json(rep_dir / f"analysis_{report['mode'].lower()}_{stamp}.json", report)
    if holdout:
        save_json(marker, {"utc": report["finished_utc"]})
    prog.unlink(missing_ok=True)
    return report


def summarise(report: dict) -> str:
    lines = ["", f"=== ANALYSIS ({report['mode']}) {report['window_utc'][0][:10]} to {report['window_utc'][1][:10]} ===",
             f"{'index':10} {'coverage':>8} {'vol/label':>9}  result"]
    for s, r in report["symbols"].items():
        cov = r.get("coverage")
        vr = (r.get("H2_vol") or {}).get("ratio")
        lines.append(f"{s:10} {(f'{cov:.1%}' if cov is not None else '-'):>8} {(f'{vr:.3f}' if vr else '-'):>9}  {r['status']}")
        for d in r.get("deviations", []):
            lines.append(f"{'':21}  - {d}: Holm p = {r['p_holm'][d]:.2g}")
    lines += ["", "Break-even: the simple momentum/reversal rule cannot make money if your round-trip cost",
              "(spread + commission) is above these levels, in index points (95% confidence):",
              f"{'index':10} " + " ".join(f"{str(h) + ' min':>12}" for h in HORIZONS_MIN)]
    for s, r in report["symbols"].items():
        if r.get("economics"):
            lines.append(f"{s:10} " + " ".join(f"{e['no_edge_if_round_trip_cost_above_points']:>12.4g}"
                                                for e in r["economics"]))
    return "\n".join(lines)
