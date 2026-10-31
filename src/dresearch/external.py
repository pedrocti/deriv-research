"""Track B long history from free, openly licensed sources (Deriv serves only ~1 year).

Sources (verified 31 Oct 2026):
  * Gold, monthly, 1960-present: datahub.io "gold-prices" (World Bank Pink Sheet from
    1960; licence ODC-PDDL-1.0). Gold was pegged at $35 until Aug 1971, so analysis
    must start after that; rows are stored raw and flagged, not dropped.
  * BTCUSDT, ETHUSDT, PAXGUSDT daily klines: data.binance.vision monthly zips, each
    verified against its published SHA-256 .CHECKSUM (repository licence MIT).
    PAXG is a gold-backed token: a daily gold proxy from 2020.
Stooq was tried and is NOT used: it blocks automated downloads with a browser check,
and we do not work around access controls.

Outputs: <data>/external/*.csv plus <data>/external/MANIFEST.json (URL, sha256,
row count, retrieved time, licence) so every number can be traced to its source.
"""
from __future__ import annotations

import csv
import hashlib
import io
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from .storage import load_json, save_json

GOLD_MONTHLY_URL = "https://datahub.io/core/gold-prices/_r/-/data/monthly.csv"
# 3-month US Treasury bill rate, monthly average, % per year (Federal Reserve, public domain).
# Used to charge historically realistic overnight financing in the gold test.
TBILL_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=TB3MS"
TBILL_FALLBACK_URL = "https://fred.stlouisfed.org/data/TB3MS.txt"     # same series, plain-text format
BINANCE_BASE = "https://data.binance.vision/data/spot/monthly/klines"
BINANCE_SYMBOLS = ["BTCUSDT", "ETHUSDT", "PAXGUSDT"]
USER_AGENT = "dresearch/0.3 (research; read-only)"


def http_get(url: str, timeout: float = 90, attempts: int = 3) -> bytes | None:
    """GET a URL; None on 404. Timeouts and server errors are retried (5 s, 15 s), then raised."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code < 500 or i == attempts - 1:
                raise
        except (TimeoutError, OSError):
            if i == attempts - 1:
                raise
        time.sleep(5 * 3 ** i)
    return None


def months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def parse_gold_monthly(raw: bytes) -> list[tuple[str, float]]:
    rows = []
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
        d = (r.get("Date") or r.get("date") or "").strip()
        p = (r.get("Price") or r.get("price") or "").strip()
        if d and p:
            rows.append((d[:7], float(p)))
    return sorted(rows)


def parse_tbill(raw: bytes) -> list[tuple[str, float]]:
    """FRED TB3MS as CSV (observation_date,TB3MS) or as FRED's .txt (header, then 'YYYY-MM-DD  v')."""
    import re
    text = raw.decode("utf-8-sig")
    if "TB3MS" not in text.splitlines()[0]:
        rows = [(m.group(1), float(m.group(2))) for m in
                re.finditer(r"^(\d{4}-\d{2})-\d{2}\s+([0-9.]+)\s*$", text, re.M)]
        return sorted(rows)
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        d = (r.get("observation_date") or r.get("DATE") or r.get("date") or "").strip()
        v = (r.get("TB3MS") or "").strip()
        if d and v and v != ".":
            rows.append((d[:7], float(v)))
    return sorted(rows)


def parse_kline_zip(raw: bytes) -> list[tuple[int, float, float, float, float, float]]:
    """Binance kline CSV inside a zip. Open time is ms, or microseconds from 2025."""
    out = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            for line in z.read(name).decode().splitlines():
                parts = line.split(",")
                if not parts or not parts[0].strip().isdigit():
                    continue                      # header row in some files
                t = int(parts[0])
                t = t // 1000 if t > 10 ** 14 else t       # microseconds -> ms
                out.append((t // 1000, float(parts[1]), float(parts[2]), float(parts[3]),
                            float(parts[4]), float(parts[5])))
    return out


def fetch_binance_daily(symbol: str, first: date, last: date, get=http_get,
                        pause: float = 0.15, verbose=print) -> tuple[list, list[str]]:
    rows, files = [], []
    started = False
    for y, m in months(first, last):
        name = f"{symbol}-1d-{y}-{m:02d}.zip"
        url = f"{BINANCE_BASE}/{symbol}/1d/{name}"
        raw = get(url)
        if raw is None:
            if started:
                verbose(f"    {name}: missing (404) after listing began; recorded as a gap")
                files.append(f"{name}: 404")
            continue
        started = True
        chk = get(url + ".CHECKSUM")
        if chk is None:
            raise RuntimeError(f"{name}: no CHECKSUM file published")
        expected = chk.decode().split()[0].lower()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise RuntimeError(f"{name}: checksum mismatch ({actual} != {expected})")
        rows += parse_kline_zip(raw)
        files.append(f"{name}: sha256 {actual[:16]}... ok")
        time.sleep(pause)
    dedup = {r[0]: r for r in rows}
    return [dedup[k] for k in sorted(dedup)], files


def run_external(data_dir: Path, get=http_get, today: date | None = None, verbose=print,
                 pause: float = 0.15) -> dict:
    out_dir = Path(data_dir) / "external"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = today or datetime.now(timezone.utc).date()
    last_full = date(today.year - (today.month == 1), 12 if today.month == 1 else today.month - 1, 1)
    manifest = load_json(out_dir / "MANIFEST.json", {}) or {}
    now_iso = datetime.now(timezone.utc).isoformat()

    verbose("Gold, monthly (World Bank via datahub.io) ...")
    raw = get(GOLD_MONTHLY_URL)
    if raw is None or raw.lstrip()[:1] == b"<":
        raise SystemExit("Gold source did not return CSV (blocked or moved). Nothing written.")
    gold = parse_gold_monthly(raw)
    path = out_dir / "gold_monthly_worldbank.csv"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("month,usd_per_oz,pegged_era\n")
        for mth, p in gold:
            fh.write(f"{mth},{p!r},{int(mth < '1971-09')}\n")
    manifest["gold_monthly_worldbank.csv"] = {
        "url": GOLD_MONTHLY_URL, "sha256_source": hashlib.sha256(raw).hexdigest(),
        "rows": len(gold), "first": gold[0][0] if gold else None, "last": gold[-1][0] if gold else None,
        "licence": "ODC-PDDL-1.0 (datahub.io core/gold-prices; World Bank Pink Sheet from 1960)",
        "note": "pegged_era=1 before 1971-09 (USD/gold peg): exclude from trend tests",
        "retrieved_utc": now_iso}
    verbose(f"  {len(gold)} months, {gold[0][0]} to {gold[-1][0]}" if gold else "  no rows!")

    verbose("US 3-month Treasury bill rate, monthly (FRED TB3MS) ...")
    failures = []
    tb, used = [], None
    for url in (TBILL_URL, TBILL_FALLBACK_URL):
        try:
            raw = get(url)
        except Exception as exc:                       # timeouts, resets: try the other format
            verbose(f"  {url}: {type(exc).__name__}; trying the next source")
            continue
        if raw and raw.lstrip()[:1] != b"<":
            tb = parse_tbill(raw)
            if tb:
                used = url
                break
    if used:
        with open(out_dir / "tbill_3m_monthly_fred.csv", "w", encoding="utf-8") as fh:
            fh.write("month,pct_per_year\n")
            for mth, v in tb:
                fh.write(f"{mth},{v!r}\n")
        manifest["tbill_3m_monthly_fred.csv"] = {
            "url": used, "sha256_source": hashlib.sha256(raw).hexdigest(), "rows": len(tb),
            "first": tb[0][0], "last": tb[-1][0],
            "licence": "Public domain (Board of Governors of the Federal Reserve System via FRED)",
            "retrieved_utc": now_iso}
        verbose(f"  {len(tb)} months, {tb[0][0]} to {tb[-1][0]}")
    else:
        failures.append("T-bill rate (FRED unreachable): the gold test cannot run until this succeeds")
        verbose("  FAILED: FRED did not answer in either format. Continuing with the crypto data.")

    for sym in BINANCE_SYMBOLS:
        verbose(f"{sym}, daily (Binance public data, checksum-verified) ...")
        rows, files = fetch_binance_daily(sym, date(2017, 1, 1), last_full, get=get, pause=pause, verbose=verbose)
        path = out_dir / f"binance_{sym}_1d.csv"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("epoch,open,high,low,close,volume\n")
            for r in rows:
                fh.write(",".join(repr(x) if isinstance(x, float) else str(x) for x in r) + "\n")
        first = datetime.fromtimestamp(rows[0][0], timezone.utc).date().isoformat() if rows else None
        lastd = datetime.fromtimestamp(rows[-1][0], timezone.utc).date().isoformat() if rows else None
        manifest[path.name] = {"url_pattern": f"{BINANCE_BASE}/{sym}/1d/{sym}-1d-YYYY-MM.zip",
                               "rows": len(rows), "first": first, "last": lastd,
                               "months_verified": sum(1 for f in files if f.endswith("ok")),
                               "gaps": [f for f in files if "404" in f],
                               "licence": "Binance public data (github.com/binance/binance-public-data, MIT)",
                               "retrieved_utc": now_iso}
        verbose(f"  {len(rows):,} days, {first} to {lastd}, "
                f"{manifest[path.name]['months_verified']} monthly files checksum-verified")
    save_json(out_dir / "MANIFEST.json", manifest)
    if failures:
        raise SystemExit("Finished with problems:\n  - " + "\n  - ".join(failures) +
                         "\nRe-run `dresearch external` later; completed files are kept.")
    return manifest
