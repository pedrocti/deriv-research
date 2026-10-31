"""Track B candle download, against the fake Deriv server."""
import asyncio
import time

from dresearch.candles import describe, pick_symbols, run_candles
from fake_deriv import FakeDeriv


def test_pick_symbols_spot_gold_and_crypto_only():
    entries = [{"symbol": s} for s in ("frxAUDCAD", "WLDXAU", "frxXAUUSD", "cryETHUSD", "cryBTCUSD", "cryLTCUSD", "R_75")]
    assert pick_symbols(entries) == ["frxXAUUSD", "cryBTCUSD", "cryETHUSD"]


def test_downloads_full_daily_history_through_paging(tmp_path):
    now = int(time.time()) // 86400 * 86400
    fake = FakeDeriv(now=now, history_floor=now - 400 * 86400, page_cap=1000)
    fake.candle_floor = {"frxXAUUSD": now - 3000 * 86400, "cryBTCUSD": now - 2000 * 86400,
                         "cryETHUSD": now - 1500 * 86400}

    async def go():
        url = await fake.start()
        rep = await run_candles(tmp_path, hourly_days=60, verbose=lambda *a: None, urls=[url])
        await fake.stop()
        return rep

    rep = asyncio.run(go())
    gold = rep["symbols"]["frxXAUUSD"]
    assert gold["daily"]["n"] == 3001, "3,000 days needs 4 pages of 1,000: paging must continue"
    assert abs(gold["daily"]["years"] - 3000 / 365.25) < 0.01
    assert rep["symbols"]["cryBTCUSD"]["daily"]["n"] == 2001
    assert 59 * 24 <= gold["hourly"]["n"] <= 60 * 24 + 1   # window counts back from the real clock
    lines = (tmp_path / "candles" / "frxXAUUSD_86400.csv").read_text().splitlines()
    assert lines[0] == "epoch,open,high,low,close" and len(lines) == 3002
    assert "WLDXAU" not in rep["symbols"]


def test_describe_reports_gaps():
    d = describe({0: (1, 1, 1, 1), 86400: (1, 1, 1, 1), 5 * 86400: (1, 1, 1, 1)}, 86400)
    assert d["n"] == 3 and d["gaps_over_1_step"] == 1 and d["longest_gap_days"] == 4.0
