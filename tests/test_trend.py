"""Known-answer tests for the Track B trend protocol."""
import math

import numpy as np
import pytest

from dresearch import trend
from dresearch.storage import save_json


def bt(px, L=12, fin_long=0.03, fin_short=0.01, spread=0.0, ppy=12, cap=2.0, tv=0.10, vw=24, vm=12):
    n = len(px)
    return trend.backtest(px, L, vw, vm, tv, cap, ppy, spread, np.full(n, fin_long), np.full(n, fin_short))


def test_no_lookahead_future_prices_never_change_past_positions():
    rng = np.random.default_rng(1)
    px = 100 * np.exp(np.cumsum(rng.normal(0, 0.04, 400)))
    w1 = bt(px)["w"]
    px2 = px.copy()
    px2[250:] *= np.exp(np.cumsum(rng.normal(0, 0.2, 150)))      # rewrite the future wildly
    w2 = bt(px2)["w"]
    assert np.array_equal(w1[:251], w2[:251]), "position at t may use prices up to t-1 only"


def test_random_walk_has_no_edge_on_average():
    sharpes = []
    for seed in range(60):
        rng = np.random.default_rng(100 + seed)
        px = 100 * np.exp(np.cumsum(rng.normal(0, 0.04, 600)))
        g = bt(px, fin_long=0, fin_short=0)["gross"][40:]
        sharpes.append(g.mean() / g.std() * math.sqrt(12))
    assert abs(np.mean(sharpes)) < 0.1, np.mean(sharpes)


def test_real_trend_is_detected():
    rng = np.random.default_rng(7)
    n, r = 660, np.zeros(660)
    e = rng.normal(0, 0.04, n)
    for t in range(1, n):                                       # persistent drift component
        r[t] = 0.9 * r[t - 1] + 0.004 * rng.normal()
    px = 100 * np.exp(np.cumsum(r + e))
    s = trend.summary(bt(px, fin_long=0, fin_short=0)["net"][40:], 12, 3)
    assert s["sharpe"] > 0.4 and s["p_one_sided"] < 0.05, s


def test_financing_and_spread_accounting():
    rng = np.random.default_rng(3)
    px = 100 * np.exp(np.cumsum(0.02 + rng.normal(0, 0.01, 120)))    # steady uptrend -> long
    b = bt(px, fin_long=0.06, fin_short=0.02, spread=0.001)
    t = 60
    assert b["w"][t] > 0
    assert b["fin"][t] == pytest.approx(-b["w"][t] * 0.06 / 12)
    assert b["trade"][t] == pytest.approx(abs(b["w"][t] - b["w"][t - 1]) * 0.001)
    down = 100 * np.exp(np.cumsum(-0.02 + rng.normal(0, 0.01, 120)))  # downtrend -> short earns credit
    b2 = bt(down, fin_long=0.06, fin_short=0.02)
    assert b2["w"][t] < 0 and b2["fin"][t] == pytest.approx(-b2["w"][t] * 0.02 / 12) and b2["fin"][t] > 0
    assert np.all(np.abs(b["w"]) <= 2.0 + 1e-12)


def test_verdict_rules():
    good = {"n": 160, "ann_return_pct": 4.0, "sharpe": 0.5, "p_one_sided": 0.05}
    weak = {"n": 160, "ann_return_pct": 1.0, "sharpe": 0.2, "p_one_sided": 0.25}
    neg = {"n": 160, "ann_return_pct": -1.0, "sharpe": -0.1, "p_one_sided": 0.7}
    assert trend.verdict(0.01, good, {"sharpe": 0.3}) == "CANDIDATE"
    assert trend.verdict(0.01, good, {"sharpe": -0.1}) == "INCONCLUSIVE"
    assert trend.verdict(0.01, weak, {"sharpe": 0.1}) == "INCONCLUSIVE"
    assert trend.verdict(0.01, neg, {"sharpe": -0.2}) == "NOT SUPPORTED"
    assert trend.verdict(0.20, good, {"sharpe": 0.3}) == "NOT SUPPORTED"


def _write_external(ext, rng):
    ext.mkdir(parents=True)
    months = [f"{y}-{m:02d}" for y in range(1965, 2027) for m in range(1, 13) if f"{y}-{m:02d}" <= "2026-09"]
    gold = 35 * np.exp(np.cumsum(rng.normal(0.005, 0.045, len(months))))
    with open(ext / "gold_monthly_worldbank.csv", "w") as fh:
        fh.write("month,usd_per_oz,pegged_era\n")
        for m, p in zip(months, gold):
            fh.write(f"{m},{p},{int(m < '1971-09')}\n")
    with open(ext / "tbill_3m_monthly_fred.csv", "w") as fh:
        fh.write("month,pct_per_year\n")
        for m in months:
            fh.write(f"{m},4.0\n")
    days = np.arange(1502928000, 1502928000 + 3332 * 86400, 86400)
    for sym, n0 in (("BTCUSDT", 4000.0), ("ETHUSDT", 300.0), ("PAXGUSDT", 1900.0)):
        d = days if sym != "PAXGUSDT" else days[-2225:]
        px = n0 * np.exp(np.cumsum(rng.normal(0.0005, 0.035, len(d))))
        with open(ext / f"binance_{sym}_1d.csv", "w") as fh:
            fh.write("epoch,open,high,low,close,volume\n")
            for t, p in zip(d, px):
                fh.write(f"{t},{p},{p},{p},{p},1\n")


def test_end_to_end_pending_then_with_crypto_costs(tmp_path):
    _write_external(tmp_path / "external", np.random.default_rng(11))
    r = trend.run_trend(tmp_path)
    assert r["gold"]["verdict"] in ("CANDIDATE", "NOT SUPPORTED", "INCONCLUSIVE")
    assert r["gold"]["first_month"] == "1970-01"
    assert r["crypto"]["verdict"].startswith("PENDING")
    assert "GOLD VERDICT" in trend.summarise(r)
    save_json(tmp_path / "reports" / "mt5_snapshot_x.json", {"account": {}, "symbols": {
        c: {"swap_annual": {"long_pct_per_year": -20.0, "short_pct_per_year": -10.0},
            "history_24h": {"median_pct_of_price": 0.08}} for c in ("cryBTCUSD", "cryETHUSD")}})
    r2 = trend.run_trend(tmp_path)
    assert not r2["crypto"]["verdict"].startswith("PENDING")
    k = r2["crypto"]["lookbacks"]["28"]
    assert k["costs_2x"]["full"]["ann_return_pct"] < k["costs"]["full"]["ann_return_pct"]
