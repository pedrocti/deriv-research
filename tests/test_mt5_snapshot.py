"""The Windows MT5 script, tested against a stand-in for the MetaTrader5 package."""
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from dresearch import cli, dashboard
from dresearch.storage import save_json

SPEC = importlib.util.spec_from_file_location("mt5_snapshot", Path(__file__).parents[1] / "windows" / "mt5_snapshot.py")
snap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snap)

TICK = np.dtype([("time", "i8"), ("bid", "f8"), ("ask", "f8"), ("last", "f8"), ("volume", "u8"),
                 ("time_msc", "i8"), ("flags", "u4"), ("volume_real", "f8")])


class FakeMT5:
    COPY_TICKS_ALL = 0
    __version__ = "5.0.6231"

    def __init__(self, offset_s=0):
        self.offset = offset_s
        self.calls = []
        self.names = ["Volatility 75 Index", "Volatility 75 (1s) Index", "XAUUSD", "EURUSD",
                      "Volatility 75 (1s) Index Extra", "Boom 1000 Index"]

    def __getattr__(self, name):          # any trading call would land here and fail loudly
        raise AssertionError(f"unexpected MT5 call: {name}")

    def initialize(self): return True
    def shutdown(self): pass
    def last_error(self): return (0, "ok")
    def account_info(self):
        return SimpleNamespace(trade_mode=0, server="Deriv-Demo", company="Deriv.com Limited",
                               currency="USD", leverage=1000, login=12345678, name="SECRET NAME", balance=10000)
    def terminal_info(self): return SimpleNamespace(build=5000, connected=True, path=r"C:\Users\secret")
    def symbols_get(self): return [SimpleNamespace(name=n) for n in self.names]
    def symbol_select(self, name, on): return True
    def symbol_info(self, name):
        return SimpleNamespace(digits=2, point=0.01, spread=150, spread_float=False, trade_tick_size=0.01,
                               trade_tick_value=0.01, trade_contract_size=1.0, volume_min=0.001,
                               volume_max=1.0, volume_step=0.001, swap_long=-7.0, swap_short=-7.0,
                               swap_mode=1, swap_rollover3days=3, trade_calc_mode=0, trade_mode=4,
                               trade_stops_level=0, margin_initial=0.0, currency_margin="USD",
                               currency_profit="USD", path="Derived\\\\Volatility")

    def _ticks(self, n, end_ms):
        a = np.zeros(n, TICK)
        a["time_msc"] = end_ms - np.arange(n)[::-1] * 1000
        a["bid"] = 4800.0
        a["ask"] = 4801.5
        return a

    def copy_ticks_range(self, name, frm, to, flags):
        now_ms = int((time.time() + self.offset) * 1000)
        return self._ticks(90_000, now_ms)

    def copy_ticks_from(self, name, frm, count, flags):
        return self._ticks(3, int((time.time() + self.offset) * 1000))


def test_snapshot_records_specs_spreads_offset_and_no_identity(tmp_path):
    mt5 = FakeMT5(offset_s=2 * 3600)
    rep = snap.run(mt5, minutes=0.05, out_dir=str(tmp_path))
    saved = json.loads(Path(rep["_path"]).read_text())
    assert set(saved["symbols"]) == {"R_75", "1HZ75V", "frxXAUUSD"}
    assert "Volatility 75 (1s) Index Extra" not in json.dumps(saved), "only exact names match"
    h = saved["symbols"]["1HZ75V"]["history_24h"]
    assert h["median_points"] == 1.5 and abs(h["median_pct_of_price"] - 100 * 1.5 / 4800.75) < 1e-9
    assert saved["server_utc_offset_hours"] == 2.0
    assert saved["account"]["mode"] == "DEMO"
    text = json.dumps(saved)
    assert "12345678" not in text and "SECRET NAME" not in text and "10000" not in text
    assert r"C:\\Users" not in text
    assert "R_10" in saved["not_found"]


def test_import_and_dashboard_verdict(tmp_path):
    rep = snap.run(FakeMT5(), minutes=0.02, out_dir=str(tmp_path))
    data = tmp_path / "data"
    save_json(data / "config" / "symbols.json", {"collect": ["R_75"], "watchdog": ["R_75"],
                                                  "intervals_s": {"R_75": 2}, "endpoint": "x"})
    assert cli.main(["--data", str(data), "import-mt5", rep["_path"]]) == 0
    econ = [{"horizon_min": h, "no_edge_if_round_trip_cost_above_pct": c}
            for h, c in ((1, 0.001), (5, 0.0044), (15, 0.0076), (60, 0.05))]
    save_json(data / "reports" / "analysis_discovery_x.json",
              {"mode": "DISCOVERY", "window_utc": ["2026-07-31T00:00", "2026-10-29T18:00"],
               "symbols": {"R_75": {"status": "CONSISTENT WITH A FAIR RANDOM PROCESS", "deviations": [],
                                    "H2_vol": {"ratio": 1.0}, "economics": econ}}})
    page = dashboard.render(dashboard.gather(data))
    # spread 1.5/4800.75 = 0.0312%: above break-even at 1, 5, 15 min; below it at 60 min
    assert page.count("no edge</td>") == 3 and page.count("open</td>") == 1
    assert "0.0312%" in page


def test_swap_annualised_for_gold_points_mode():
    # Pedro's real Deriv XAUUSD specs (30 Oct 2026): -45 / +20 points, point 0.01, 100 oz
    specs = {"swap_mode": 1, "swap_long": -45.0, "swap_short": 20.0, "point": 0.01}
    sw = snap.swap_annual_pct(specs, 3980.0)
    assert abs(sw["long_pct_per_year"] - (-45 * 0.01 / 3980 * 100 * 365)) < 1e-9
    assert round(sw["long_pct_per_year"], 2) == -4.13 and round(sw["short_pct_per_year"], 2) == 1.83
    assert snap.swap_annual_pct({"swap_mode": 5, "swap_long": -20.0, "swap_short": -15.0}, 1)["long_pct_per_year"] == -20.0


def test_crypto_names_match():
    m = snap.match_symbols(["BTCUSD", "ETHUSD", "BTCUSDT", "XAUUSD"])
    assert m == {"BTCUSD": "cryBTCUSD", "ETHUSD": "cryETHUSD", "XAUUSD": "frxXAUUSD"}
