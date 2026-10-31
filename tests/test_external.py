"""External long-history importer, with a fake web (no internet)."""
import hashlib
import io
import zipfile
from datetime import date

import pytest

from dresearch import external

GOLD_CSV = b"Date,Price\n1971-07,40.95\n1971-08,42.7\n1971-09,42.0\n2026-09,3950.5\n"


def kline_zip(rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("k.csv", "\n".join(",".join(map(str, r)) for r in rows))
    return buf.getvalue()


def fake_web(files):
    def get(url):
        return files.get(url)
    return get


def build(tamper=False):
    files = {external.GOLD_MONTHLY_URL: GOLD_CSV}
    for sym in external.BINANCE_SYMBOLS:
        for (y, m), t in (((2017, 8), 1501545600000), ((2017, 9), 1504224000000),
                          ((2025, 1), 1735689600000000)):          # 2025+: microseconds
            name = f"{sym}-1d-{y}-{m:02d}.zip"
            url = f"{external.BINANCE_BASE}/{sym}/1d/{name}"
            z = kline_zip([[t, 100.0, 110.0, 90.0, 105.0, 12.5, t + 1, 0, 0, 0, 0, 0]])
            files[url] = z
            files[url + ".CHECKSUM"] = (("0" * 64) if tamper else hashlib.sha256(z).hexdigest()).encode() + f"  {name}".encode()
    return files


def test_imports_gold_and_crypto_with_provenance(tmp_path):
    m = external.run_external(tmp_path, get=fake_web(build()), today=date(2026, 10, 31),
                              verbose=lambda *a: None, pause=0)
    gold = (tmp_path / "external" / "gold_monthly_worldbank.csv").read_text().splitlines()
    assert gold[0] == "month,usd_per_oz,pegged_era" and gold[1].endswith(",1") and gold[3].endswith(",0")
    btc = (tmp_path / "external" / "binance_BTCUSDT_1d.csv").read_text().splitlines()
    assert len(btc) == 4
    assert btc[3].startswith("1735689600,"), "microsecond timestamps normalised to seconds"
    info = m["binance_BTCUSDT_1d.csv"]
    assert info["months_verified"] == 3 and info["first"] == "2017-08-01"
    assert any("2017-10" in g for g in info["gaps"]), "a month missing after listing is recorded"
    assert m["gold_monthly_worldbank.csv"]["rows"] == 4 and "PDDL" in m["gold_monthly_worldbank.csv"]["licence"]


def test_checksum_mismatch_stops_the_import(tmp_path):
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        external.run_external(tmp_path, get=fake_web(build(tamper=True)), today=date(2026, 10, 31),
                              verbose=lambda *a: None, pause=0)


def test_blocked_gold_source_writes_nothing(tmp_path):
    files = build()
    files[external.GOLD_MONTHLY_URL] = b"<!DOCTYPE html><html>verify you are human</html>"
    with pytest.raises(SystemExit, match="did not return CSV"):
        external.run_external(tmp_path, get=fake_web(files), today=date(2026, 10, 31),
                              verbose=lambda *a: None, pause=0)
    assert not (tmp_path / "external" / "gold_monthly_worldbank.csv").exists()
