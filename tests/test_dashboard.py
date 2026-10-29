"""Dashboard: renders from real-shaped files, and the server answers only the secret path."""
import threading
import time
import urllib.error
import urllib.request

import numpy as np

from dresearch import dashboard
from dresearch.storage import LiveWriter, save_json, EventLog


def _seed(tmp_path, now):
    save_json(tmp_path / "config" / "symbols.json",
              {"collect": ["R_75", "1HZ75V", "frxXAUUSD"], "watchdog": ["R_75", "1HZ75V"],
               "intervals_s": {"R_75": 2, "1HZ75V": 1}, "endpoint": "x"})
    w = LiveWriter(tmp_path)
    rng = np.random.default_rng(0)
    for sym, iv in (("R_75", 2), ("1HZ75V", 1)):
        px = 5000 * np.exp(np.cumsum(rng.normal(0, 1e-4, 1800)))
        for i, p in enumerate(px):
            w.write(sym, int(now) - (1800 - i) * iv, f"{p:.2f}", 0, "s")
    w.close()
    save_json(tmp_path / "reports" / "backfill_progress.json",
              {"days": 90, "hours_total": 21600, "hours_done_before_start": 0, "hours_processed": 5400,
               "eta_hours": 6.5, "finished": False, "oldest_hour_reached_utc": "2026-08-01T00:00:00+00:00"})
    EventLog(tmp_path).log("connected", "wss://example session=abc")


def test_render_contains_plain_summary_and_escapes(tmp_path):
    now = time.time()
    _seed(tmp_path, now)
    g = dashboard.gather(tmp_path, now)
    page = dashboard.render(g)
    assert "Deriv Research Monitor" in page and "no way to place a trade" in page
    assert "25%" in page                      # 5400 / 21600 history download
    assert page.count('<svg class="spark"') == 2
    assert "Volatility 75 (1s)" in page and "Gold / US dollar" in page
    # untrusted text from disk is escaped
    EventLog(tmp_path).log("connected", "<script>alert(1)</script>")
    assert "<script>alert" not in dashboard.render(dashboard.gather(tmp_path, now))


def test_server_only_answers_secret_path(tmp_path):
    _seed(tmp_path, time.time())
    srv = dashboard.make_server(tmp_path, host="127.0.0.1", port=0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    token = dashboard.get_token(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/{token}/") as r:
            assert r.status == 200 and b"Deriv Research Monitor" in r.read()
            assert "default-src 'none'" in r.headers["Content-Security-Policy"]
        for bad in ("/", "/index.html", f"/{token}x/", f"/{token}/../config/symbols.json", "/config/dashboard.json"):
            try:
                urllib.request.urlopen(base + bad)
                raise AssertionError(f"{bad} should be 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404, bad
        req = urllib.request.Request(f"{base}/{token}/", data=b"x", method="POST")
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            assert e.code == 405
    finally:
        srv.shutdown()
    assert len(token) >= 20
