"""Known-answer tests for the Track C (Ethereum hours-to-days) protocol."""
import numpy as np
import pytest

from dresearch import swing

T0 = 1_504_224_000            # 2017-09-01 00:00 UTC


def bars(closes, wick=0.001):
    c = np.asarray(closes, float)
    o = np.concatenate([[c[0]], c[:-1]])
    return {"t": T0 + 3600 * np.arange(len(c)), "o": o, "c": c,
            "h": np.maximum(o, c) * (1 + wick), "l": np.minimum(o, c) * (1 - wick)}


def path_bars(n, sigma_h, rng, sub=20):
    """Hourly bars cut from a finer fair (martingale) price path, so highs, lows and gaps
    through a stop look like real ones. Shallow fixed wicks under-state how often price jumps
    past a stop, which flatters tight stops."""
    s = sigma_h / np.sqrt(sub)
    path = 1000 * np.exp(np.concatenate([[0.0], np.cumsum(rng.normal(-s * s / 2, s, n * sub))]))
    seg = path[:n * sub + 1]
    blocks = np.lib.stride_tricks.sliding_window_view(seg, sub + 1)[::sub][:n]
    return {"t": T0 + 3600 * np.arange(n), "o": blocks[:, 0], "c": blocks[:, -1],
            "h": blocks.max(1), "l": blocks.min(1)}


def test_stop_time_and_gap_exits_with_costs():
    d = bars([100] * 5 + [100, 101, 102, 103, 104, 105])
    tr = swing.simulate(d, [(4, +1, 3)], stop=0.05)[0]
    assert tr["reason"] == "time" and tr["hours"] == 3
    hs = swing.COSTS["half_spread"] + swing.COSTS["slippage"]
    assert tr["entry"] == pytest.approx(d["o"][5] * (1 + hs))
    assert tr["exit"] == pytest.approx(d["c"][7] * (1 - hs))
    swap = swing.COSTS["swap_per_year"] / 8760 * 3
    assert tr["net"] == pytest.approx((tr["exit"] - tr["entry"]) / tr["entry"] - swap)

    d2 = bars([100, 100, 100, 99.5, 98, 97])               # drifts into a 1% stop
    s = swing.simulate(d2, [(1, +1, 4)], stop=0.01)[0]
    assert s["reason"] == "stop" and s["net"] < -0.01

    d3 = bars([100, 100, 100, 100, 100])
    d3["o"][3] = 90.0                                       # opens far below the stop: worse fill
    g = swing.simulate(d3, [(1, +1, 4)], stop=0.01)[0]
    assert g["reason"] == "stop (gapped)" and g["net"] < -0.09


def test_short_trades_and_one_position_at_a_time():
    d = bars([100, 100, 99, 98, 97, 96, 95, 94])
    trs = swing.simulate(d, [(0, -1, 4), (1, -1, 4), (6, -1, 1)], stop=0.05)
    assert len(trs) == 2 and trs[0]["net"] > 0, "overlapping signal skipped; short profits in a fall"


def test_s1_signals_never_use_future_prices():
    rng = np.random.default_rng(2)
    c = 1000 * np.exp(np.cumsum(rng.normal(0, 0.01, 3000)))
    a = set(i for i, _, _ in swing.signals_s1(bars(c)) if i < 2000)
    c2 = c.copy()
    c2[2001:] *= np.exp(np.cumsum(rng.normal(0, 0.05, 999)))
    b = set(i for i, _, _ in swing.signals_s1(bars(c2)) if i < 2000)
    assert a == b


def test_random_entries_lose_the_costs_at_every_stop_size():
    """No free money from the simulator: random entries on a fair price must not show a
    significant profit with tight, wide or no stops."""
    rng = np.random.default_rng(1)
    n = 120_000
    d = path_bars(n, 0.008, rng)
    sig = sorted((int(i), int(rng.choice([-1, 1])), 24) for i in rng.choice(n - 100, 4000, replace=False))
    for stop in (0.009, 0.018, 0.5):
        st = swing.trade_stats(swing.simulate(d, sig, stop), stop)
        assert st["p_one_sided"] > 0.05 and st["mean_net_pct"] < 0.05, (stop, st)


def test_random_walk_s1_finds_nothing():
    d = path_bars(40_000, 0.008, np.random.default_rng(4))
    st = swing.trade_stats(swing.simulate(d, swing.signals_s1(d), 0.018), 0.018)
    assert st["p_one_sided"] > 0.05, st


def test_planted_crash_bounce_is_detected():
    rng = np.random.default_rng(5)
    r = rng.normal(0, 0.008, 40000)
    crash = rng.random(40000) < 0.003
    r[crash] = -0.05
    for i in np.nonzero(crash)[0]:                         # rebound over the next 6 hours
        if i + 7 < len(r):
            r[i + 2:i + 8] += 0.006
    d = bars(1000 * np.exp(np.cumsum(r)), wick=0.002)
    st = swing.trade_stats(swing.simulate(d, swing.signals_s1(d), 0.018), 0.018)
    assert st["mean_net_pct"] > 0 and st["p_one_sided"] < 0.01, st


def test_account_sim_halts_month_and_stops_overall():
    base = {"dir": 1, "hours": 1}
    t = 1_700_000_000
    losses = [{**base, "t": t + i * 3600, "entry": 2000.0, "exit": 1960.0} for i in range(10)]   # -$4 each
    a = swing.account_sim(losses)
    assert a["months_halted"] == 1 and a["trades_taken"] == 3 and a["end"] < 89


def test_end_to_end(tmp_path):
    rng = np.random.default_rng(9)
    n = 24 * 365 * 6
    c = 300 * np.exp(np.cumsum(rng.normal(0, 0.008, n)))
    ext = tmp_path / "external"
    ext.mkdir()
    o = np.concatenate([[c[0]], c[:-1]])
    with open(ext / "binance_ETHUSDT_1h.csv", "w") as fh:
        fh.write("epoch,open,high,low,close,volume\n")
        for i in range(n):
            fh.write(f"{T0 + 3600 * i},{o[i]},{max(o[i], c[i]) * 1.002},{min(o[i], c[i]) * 0.998},{c[i]},1\n")
    r = swing.run_swing(tmp_path)
    assert set(r["ETH"]["variants"]) and all(v["verdict"] in ("CANDIDATE", "NOT SUPPORTED", "INCONCLUSIVE")
                                             for v in r["ETH"]["variants"].values())
    assert "ETHEREUM HOURS-TO-DAYS TEST" in swing.summarise(r)
