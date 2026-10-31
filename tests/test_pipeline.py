"""End-to-end tests against the local fake Deriv server (no internet needed)."""
import asyncio
import math
import time
from datetime import datetime, timezone

import numpy as np
import pytest

from dresearch import audit, probe
from dresearch.client import DerivClient, connect_any
from dresearch.collector import Collector
from dresearch.history import fetch_ticks
from dresearch.storage import LiveWriter, hour_sealed, load_json, write_history_hour
from fake_deriv import FakeDeriv, price

T_DAY = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())


def run(coro):
    return asyncio.run(coro)


def test_history_paging_ignores_server_page_cap():
    """Server honours only 1000 per page; we ask for 5000. Must still get the whole hour, no duplicates."""
    async def go():
        fake = FakeDeriv(now=T_DAY + 7200, history_floor=T_DAY, page_cap=1000)
        url = await fake.start()
        c = DerivClient(url); await c.connect()
        ticks, pip = await fetch_ticks(c, "1HZ75V", T_DAY, T_DAY + 3599, page=5000)
        r75, _ = await fetch_ticks(c, "R_75", T_DAY, T_DAY + 3599, page=5000)
        await c.close(); await fake.stop()
        return ticks, pip, r75
    ticks, pip, r75 = run(go())
    assert len(ticks) == 3600 and min(ticks) == T_DAY and max(ticks) == T_DAY + 3599
    assert len(r75) == 1800
    assert pip == 2


def test_trading_calls_are_refused():
    async def go():
        fake = FakeDeriv(now=T_DAY, history_floor=T_DAY)
        url = await fake.start()
        c = DerivClient(url); await c.connect()
        try:
            for call in ({"buy": 1, "price": 10}, {"proposal": 1}, {"authorize": "x"}):
                with pytest.raises(PermissionError):
                    await c.request(call)
        finally:
            await c.close(); await fake.stop()
        return fake.requests
    sent = run(go())
    assert not any(k in r for r in sent for k in ("buy", "proposal", "authorize")), "nothing reached the server"


def test_endpoint_fallback():
    async def go():
        fake = FakeDeriv(now=T_DAY, history_floor=T_DAY)
        url = await fake.start()
        c, used, attempts = await connect_any(["ws://127.0.0.1:9", url])
        await c.close(); await fake.stop()
        return used, attempts, url
    used, attempts, url = run(go())
    assert used == url and attempts[0]["ok"] is False and attempts[1]["ok"] is True


def test_probe_measures_and_writes_config(tmp_path):
    async def go():
        now = int(time.time())
        fake = FakeDeriv(now=now, history_floor=now - 40 * 86400, page_cap=1000)
        url = await fake.start()
        rep = await probe.run_probe(tmp_path, urls=[url], verbose=lambda *a: None)
        await fake.stop()
        return rep
    rep = run(go())
    cfg = load_json(tmp_path / "config" / "symbols.json", None)
    assert cfg["collect"] == ["R_75", "1HZ75V", "frxXAUUSD"]
    assert cfg["watchdog"] == ["R_75", "1HZ75V"]
    assert cfg["intervals_s"] == {"R_75": 2, "1HZ75V": 1}
    assert rep["history_page_size"] == 1000
    assert rep["tick_history_days_available_at_least"] == 30      # floor is 40 days back
    st = rep["tick_stats"]["1HZ75V"]
    assert st["pip_size_decimals"] == 2 and st["interval_share_at_median"] == 1.0
    assert "frxEURUSD" not in cfg["collect"]
    assert [g["symbol"] for g in rep["symbols"]["gold"]] == ["frxXAUUSD"]   # not frxAUDCAD, not WLDXAU
    assert "PROBE SUMMARY" in probe.summarise(rep)


def test_collector_survives_drop_and_history_repairs_gap(tmp_path):
    """Feed drops mid-stream. Live record has a hole; the hourly history copy must fill it."""
    cfg = {"endpoint": "", "collect": ["R_75", "1HZ75V", "frxXAUUSD"], "watchdog": ["R_75", "1HZ75V"],
           "intervals_s": {"R_75": 2, "1HZ75V": 1}, "history_page_size": 5000}
    six = T_DAY + 6 * 3600

    async def go():
        fake = FakeDeriv(now=six, history_floor=T_DAY, page_cap=1000, drop_after_ticks=500, step_s=0.002)
        url = await fake.start()
        cfg["endpoint"] = url
        from dresearch.storage import save_json
        save_json(tmp_path / "config" / "symbols.json", cfg)
        col = Collector(tmp_path, config=cfg, clock=lambda: fake.now, lookback_hours=8,
                        watchdog_s=5, reconcile_every_s=0.2, backoff_start=0.05)
        col.urls = [url]
        task = asyncio.create_task(col.run())
        await asyncio.sleep(0.5)                 # connect + subscribe + backfill hours 0-5
        fake.stop_at = six + 600
        clock = asyncio.create_task(fake.clock())
        await clock
        await asyncio.sleep(0.3)
        fake.now = six + 3600 + 180              # an hour later: hour 06 is now closed and settled
        for _ in range(100):
            if all(hour_sealed(tmp_path, s, six) for s in ("R_75", "1HZ75V")):
                break
            await asyncio.sleep(0.1)
        col.stop.set()
        await task
        await fake.stop()
        return fake
    fake = run(go())
    assert fake.dropped
    rep = audit.run_audit(tmp_path, "2026-10-01", seal=False)
    for sym, n_hour in (("1HZ75V", 3600), ("R_75", 1800)):
        r = rep["symbols"][sym]
        assert r["history_ticks"] == 7 * n_hour, sym           # hours 00-06 re-downloaded
        assert r["completeness_within_recorded_span"] == pytest.approx(1.0), sym
        assert r["gap_count"] == 0, sym
        assert r["live_completeness_within_recorded_span"] < 1.0, "the drop should leave a live hole"
        assert r["agreement"] == 1.0
        assert r["off_schedule_ticks"] == 0
    events = (tmp_path / "logs" / "events.csv").read_text()
    assert events.count(",connected,") >= 2 and "disconnected" in events


def _write_live(tmp_path, sym, epochs, prices, pip=2):
    w = LiveWriter(tmp_path)
    for e, p in zip(epochs, prices):
        w.write(sym, int(e), f"{p:.{pip}f}", 0, "s")
    w.close()


@pytest.mark.parametrize("biased", [False, True])
def test_missingness_detects_gaps_linked_to_price_moves(tmp_path, biased):
    rng = np.random.default_rng(3 if biased else 4)
    n = 20000
    ep = T_DAY + np.arange(n)
    px = 5000 * np.exp(np.cumsum(rng.normal(0, 2e-4, n)))
    hist = {int(e): float(f"{p:.2f}") for e, p in zip(ep, px)}
    absr = np.abs(np.diff(np.log(px), prepend=np.log(px[0])))
    if biased:      # the feed tends to drop right after big moves
        drop = (absr > np.quantile(absr, 0.97)) & (rng.random(n) < 0.5)
    else:           # random drops
        drop = rng.random(n) < 0.015
    drop_next = np.roll(drop, 1); drop_next[0] = False
    keep = ~drop_next
    _write_live(tmp_path, "1HZ75V", ep[keep], px[keep])
    write_history_hour(tmp_path, "1HZ75V", T_DAY, {e: hist[e] for e in hist if e < T_DAY + 3600}, 2)
    from dresearch.storage import save_json
    save_json(tmp_path / "config" / "symbols.json",
              {"collect": ["1HZ75V"], "watchdog": ["1HZ75V"], "intervals_s": {"1HZ75V": 1}})
    r = audit.run_audit(tmp_path, "2026-10-01", seal=False)["symbols"]["1HZ75V"]
    p = r["missingness"]["p_value"]
    if biased:
        assert p < 1e-6 and "gaps linked to price moves" in r["status"]
    else:
        assert p > 0.01


def test_seal_compresses_and_hashes(tmp_path):
    from dresearch.storage import seal_closed_days, read_rows
    _write_live(tmp_path, "R_75", [T_DAY, T_DAY + 2], [1.0, 2.0])
    n = seal_closed_days(tmp_path, "2026-10-02")
    assert n == 1
    gz = list((tmp_path / "raw" / "live" / "R_75" / "2026-10-01").glob("*.csv.gz"))
    assert len(gz) == 1 and [r["epoch"] for r in read_rows(gz[0])] == [str(T_DAY), str(T_DAY + 2)]
    assert "sha256" in (tmp_path / "raw" / "MANIFEST.jsonl").read_text()
    assert seal_closed_days(tmp_path, "2026-10-01") == 0     # today's files are never sealed


def test_backfill_newest_first_resumable_and_depth_limited(tmp_path):
    """History exists only for the last 30 hours; ask for 2 days. Then re-run: nothing re-downloaded."""
    from dresearch.backfill import run_backfill, plan_hours
    from dresearch.storage import save_json
    now = T_DAY + 3 * 86400 + 1800

    async def go(fake):
        url = await fake.start()
        save_json(tmp_path / "config" / "symbols.json",
                  {"endpoint": url, "collect": ["R_75", "1HZ75V"], "watchdog": ["R_75", "1HZ75V"],
                   "intervals_s": {"R_75": 2, "1HZ75V": 1}, "history_page_size": 1000})
        st = await run_backfill(tmp_path, days=2, per_minute=6000, clock=lambda: now, verbose=lambda *a: None)
        await fake.stop()
        return st

    fake = FakeDeriv(now=now, history_floor=now - 30 * 3600, page_cap=1000)
    st = run(go(fake))
    hours = plan_hours(now, 2)
    assert hours == sorted(hours, reverse=True) and len(hours) == 48
    assert st["finished"] and st["errors"] == 0
    # hours fully after the floor are written; earlier ones are reported as unavailable, not faked
    assert st["hours_written"] >= 2 * 27 and st["hours_empty"] >= 2 * 18
    assert hour_sealed(tmp_path, "1HZ75V", hours[0]) and not hour_sealed(tmp_path, "1HZ75V", hours[-1])
    n_requests = len(fake.requests)

    fake2 = FakeDeriv(now=now, history_floor=now - 30 * 3600, page_cap=1000)
    st2 = run(go(fake2))
    assert st2["hours_written"] == 0, "already-downloaded hours must not be fetched again"
    hist_calls = sum(1 for r in fake2.requests if "ticks_history" in r)
    assert hist_calls <= 2 * 21, f"only the unavailable hours are retried, got {hist_calls}"


@pytest.mark.parametrize("allowed_per_s", [40.0, 5.0])
def test_backfill_waits_out_rate_limits_and_never_skips(tmp_path, allowed_per_s):
    """Older history is rate-limited (as Deriv did on 30 Oct 2026). Every hour must still arrive,
    including when the limit is stricter than our request rate, so multi-page hours could livelock
    if a refusal restarted the hour."""
    from dresearch.backfill import run_backfill
    from dresearch.storage import save_json
    now = T_DAY + 2 * 86400 + 1800
    fake = FakeDeriv(now=now, history_floor=T_DAY - 86400, page_cap=1000,
                     expensive_age_s=12 * 3600, expensive_per_s=allowed_per_s)

    async def go():
        url = await fake.start()
        save_json(tmp_path / "config" / "symbols.json",
                  {"endpoint": url, "collect": ["R_75", "1HZ75V"], "watchdog": ["R_75", "1HZ75V"],
                   "intervals_s": {"R_75": 2, "1HZ75V": 1}, "history_page_size": 1000})
        st = await run_backfill(tmp_path, days=1, per_minute=100000, clock=lambda: now,
                                verbose=lambda *a: None, backoff_start=0.02, backoff_max=0.1, min_rate=600)
        await fake.stop()
        return st

    st = run(go())
    assert fake.rate_limited > 0 and st["rate_limited"] > 0, "the limit was actually hit"
    assert st["finished"] and st["hours_skipped"] == 0 and st["errors"] == 0
    assert st["hours_written"] == 2 * 24, "every hour of every symbol downloaded"
    events = (tmp_path / "logs" / "events.csv").read_text()
    assert "backfill_rate_limited" in events and "backfill_error" not in events


def test_audit_counts_only_on_schedule_ticks(tmp_path):
    """An off-schedule tick must be flagged, never push completeness above 100%."""
    from dresearch.storage import save_json
    save_json(tmp_path / "config" / "symbols.json",
              {"collect": ["R_75"], "watchdog": ["R_75"], "intervals_s": {"R_75": 2}})
    ticks = {T_DAY + 2 * i: 100.0 + i * 0.01 for i in range(1800)}
    ticks[T_DAY + 1001] = 150.0                                  # odd second: off the 2 s schedule
    write_history_hour(tmp_path, "R_75", T_DAY, ticks, 2)
    r = audit.run_audit(tmp_path, "2026-10-01", seal=False)["symbols"]["R_75"]
    assert r["completeness_within_recorded_span"] == pytest.approx(1.0)
    assert r["off_schedule_ticks"] == 1 and "off the 2s schedule" in r["status"]
