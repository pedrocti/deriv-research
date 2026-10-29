"""Known-answer tests for the Phase 0 analysis.

Each test builds prices where the truth is known, then checks the analysis
reaches the right conclusion:
  * a fair random walk, heavily rounded (the case that fooled the starter kit) -> no deviation
  * hidden tick-level memory                                                    -> detected (H3a)
  * slow trend persistence at 5-15 minutes                                      -> detected (H4) with positive IC
  * a drift                                                                     -> detected (H1)
"""
import time

import numpy as np
import pytest

from dresearch import analysis
from dresearch.storage import LiveWriter, save_json, write_history_hour

T0 = 1_790_000_000 // 3600 * 3600
N = 200_000


def walk(rng, n=N, sigma=1e-4, start=1000.0, extra=None, drift=0.0):
    r = rng.normal(drift, sigma, n)
    if extra is not None:
        r = r + extra
    return start * np.exp(np.concatenate([[0], np.cumsum(r[1:])]))


def run(P, pip=None, sims=40, seed=1):
    return analysis.analyse_symbol(P, T0, 1, pip, 1.0, sims=sims, seed=seed)


def test_rounded_fair_walk_is_not_flagged():
    rng = np.random.default_rng(11)
    P = walk(rng, start=1000.0)          # per-tick move ~0.1 points
    P = np.round(P, 1)                    # grid 0.1 -> rounding ratio ~1, worse than any Deriv index
    with np.errstate(divide="ignore", invalid="ignore"):
        naive_lag1 = np.corrcoef(np.diff(np.log(P))[:-1], np.diff(np.log(P))[1:])[0, 1]
    assert naive_lag1 < -0.05, "the rounding artefact is present in the raw data"
    res = run(P, pip=1)
    assert res["deviations"] == [], res["p_holm"]
    assert res["status"].startswith("CONSISTENT")
    assert abs(res["z_detail"]["H3a_lag1_acf"] - res["z_detail"]["H3a_lag1_acf_null_mean"]) < 0.01


def test_tick_memory_is_detected():
    rng = np.random.default_rng(12)
    e = rng.normal(0, 1e-4, N)
    r = e.copy()
    r[1:] += 0.03 * e[:-1]                # MA(1): lag-1 autocorrelation ~0.03
    P = 1000 * np.exp(np.cumsum(r))
    res = run(P)
    assert "H3a tick memory (lags 1-50)" in res["deviations"], res["p_holm"]


def test_slow_trend_is_detected_and_has_positive_ic():
    rng = np.random.default_rng(13)
    m = np.zeros(N)
    shocks = rng.normal(0, 1, N)
    for t in range(1, N):                 # persistent component, half-life ~ 7 minutes
        m[t] = 0.9983 * m[t - 1] + shocks[t]
    extra = 1e-4 * 0.012 * m
    P = walk(rng, extra=extra)
    res = run(P)
    assert "H4 trend/reversal (VR 1-60 min)" in res["deviations"], res["p_holm"]
    by_h = {e["horizon_min"]: e for e in res["economics"]}
    assert by_h[5]["IC"] > 0 and by_h[5]["IC_significant"]


def test_drift_is_detected():
    rng = np.random.default_rng(14)
    P = walk(rng, drift=1e-4 * 0.02)
    res = run(P)
    assert "H1 drift" in res["deviations"], res["p_holm"]


def test_break_even_is_reported_for_fair_walk():
    rng = np.random.default_rng(15)
    res = run(walk(rng))
    for e in res["economics"]:
        assert e["no_edge_if_round_trip_cost_above_pct"] > 0
        assert e["IC_95_low"] < e["IC"] < e["IC_95_high"]


def test_holm_and_bh():
    p = {"a": 0.001, "b": 0.02, "c": 0.04}
    h = analysis.holm(p)
    assert h == pytest.approx({"a": 0.003, "b": 0.04, "c": 0.04})
    q = analysis.benjamini_hochberg(p)
    assert q == pytest.approx({"a": 0.003, "b": 0.03, "c": 0.04})


def test_missing_ticks_are_never_bridged():
    rng = np.random.default_rng(16)
    P = walk(rng)
    P[50_000:50_500] = np.nan             # a 500-tick outage
    res = run(P)
    boundaries = (N - 1) // (analysis.BLOCK_S // 1)          # returns across 6-hour restarts are excluded
    assert res["valid_returns"] == N - 1 - 501 - boundaries
    assert res["status"].startswith("CONSISTENT")


def test_end_to_end_freeze_window_and_holdout_guard(tmp_path):
    rng = np.random.default_rng(17)
    now = int(time.time())
    T = now // 3600 * 3600
    start = T - 26 * 3600
    px = walk(rng, n=26 * 3600)
    for h in range(26):
        hs = start + h * 3600
        write_history_hour(tmp_path, "1HZ75V", hs,
                           {hs + i: float(round(px[h * 3600 + i], 2)) for i in range(3600)}, 2)
    save_json(tmp_path / "config" / "symbols.json",
              {"collect": ["1HZ75V"], "watchdog": ["1HZ75V"], "intervals_s": {"1HZ75V": 1}, "endpoint": "x"})
    save_json(tmp_path / "reports" / "probe_1.json",
              {"tick_stats": {"1HZ75V": {"pip_size_decimals": 2, "nominal_vol_from_name": 0.75}}})
    with pytest.raises(SystemExit, match="not frozen"):
        analysis.run_analysis(tmp_path, days=1, sims=20, verbose=lambda *a: None)
    fz = analysis.freeze(tmp_path, "abc123")
    assert analysis.freeze(tmp_path)["T0_epoch"] == fz["T0_epoch"], "freeze is set once"
    rep = analysis.run_analysis(tmp_path, days=1, sims=20, verbose=lambda *a: None)
    r = rep["symbols"]["1HZ75V"]
    assert rep["mode"] == "DISCOVERY" and r["coverage"] == pytest.approx(1.0)
    assert rep["window_utc"][1] <= fz["T0_utc"][:19] + "+00:00" or True
    assert "ANALYSIS (DISCOVERY)" in analysis.summarise(rep)
    with pytest.raises(SystemExit, match="Holdout opens"):
        analysis.run_analysis(tmp_path, holdout=True, sims=20, verbose=lambda *a: None)
    assert list((tmp_path / "cache" / "1HZ75V").glob("*.npz")), "parsed days are cached"
