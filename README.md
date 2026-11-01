# deriv-research

Read-only data tools for testing whether Deriv Volatility Indices have any tradable edge.
**Nothing in this repository can place a trade.** Calls such as `buy`, `proposal` and `authorize` are refused in code, and a test proves it.

## What it does

- **`dresearch probe`** measures what Deriv's public API actually provides: the endpoint, symbol codes, tick schedule, price rounding, page size, how far back history goes, and whether gold is served. It writes the collector config.
- **`dresearch collect`** records every tick 24/7 as a systemd service. It reconnects by itself. It re-downloads every closed hour, which repairs gaps and gives an independent check of the live feed.
- **`dresearch audit`** is the daily data-quality report: completeness, gaps, live-vs-history agreement, and whether gaps are linked to price moves. It also compresses and hashes finished days.
- **`dresearch backfill --days N`** downloads past ticks: newest first, resumable, throttled. It normally runs as the `dresearch-backfill` service.
- **`dresearch status`** tells you whether it is running and whether data is arriving.

## Install on the VPS (Ubuntu, as root)

```bash
curl -fsSL https://raw.githubusercontent.com/pedrocti/deriv-research/main/deploy/install.sh | bash
dresearch probe
systemctl enable --now dresearch-collector dresearch-audit.timer
dresearch status
```

**Download history (runs in the background, about 9 h for 90 days):** `systemctl start dresearch-backfill`

**Status page in your browser:** `systemctl enable --now dresearch-dashboard`, then `dresearch dashboard-url` prints your private link (port 8787).

**Pattern tests (Phase 0):** run `dresearch freeze` once, before any analysis, to fix the protocol and start the holdout clock. Then, after the history download finishes, run `systemctl start dresearch-analyze` (about 1 hour). Results appear on the status page. The protocol is the docstring at the top of `src/dresearch/analysis.py`.

Measured false-alarm rate on fair random walks, 200,000 ticks, 40 simulations, Holm 1% per index: 2/120 unrounded, 2/120 at Deriv-level rounding (0.07), 4/120 at heavy rounding (0.5).

**Track B (gold, crypto):** `dresearch candles` downloads daily candles for spot gold, Bitcoin and Ethereum, as far back as Deriv serves them, plus 2 years of hourly candles, into `/var/lib/dresearch/candles/`. The Windows snapshot also reads BTCUSD and ETHUSD and prints the yearly cost of holding each position overnight (swap).

**Track B test:** `dresearch external` imports the long history (gold monthly from 1960 via the World Bank, the US T-bill rate from FRED, and Bitcoin, Ethereum and PAXG daily from Binance, all checksum- or hash-recorded). `dresearch trend` then runs the preregistered trend-following test. Its rules are the docstring at the top of `src/dresearch/trend.py`, committed before any result was seen.

**Track C test:** `dresearch swing` runs the preregistered Ethereum hours-to-days test sized for a $100 account (rules: docstring of `src/dresearch/swing.py`, committed before any result). Needs the hourly data from `dresearch external`.

**Demo journal:** `dresearch journal-url` prints the private link to the trade journal (served by the status page service); `dresearch journal` prints the weekly review numbers. Coaching handoff: `docs/COACH.md`.

**Update to a new version:** run the same `curl ... | bash` line again. It restarts the collector itself.

**Stop collecting:** `systemctl disable --now dresearch-collector dresearch-audit.timer`

## Where things are

| What | Where |
| --- | --- |
| Raw ticks (never edited) | `/var/lib/dresearch/raw/{live,history}/<SYMBOL>/<YYYY-MM-DD>/<HH>.csv[.gz]` |
| Hashes of sealed files | `/var/lib/dresearch/raw/MANIFEST.jsonl` |
| Connection events | `/var/lib/dresearch/logs/events.csv` |
| Probe and audit reports | `/var/lib/dresearch/reports/` |
| Service log | `journalctl -u dresearch-collector -n 50` |

All timestamps are UTC Unix epochs as sent by Deriv.

## Limits that are enforced

The collector is capped at 512 MB of RAM and half a CPU core, runs at low priority, and runs as an unprivileged user that can write only to `/var/lib/dresearch`. It opens no network ports. API usage stays under Deriv's documented 220 calls a minute.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `probe` says "no endpoint reachable" | Run `curl -sI https://api.derivws.com` on the VPS. If it prints nothing, outbound HTTPS is blocked |
| `status` shows CHECK NEEDED | `journalctl -u dresearch-collector -n 50` and send the output |
| A symbol says "never" | It may not exist on your account's feed. Check the probe summary |
| Audit says `WARN: completeness` | Normal on the first, partial day. Look again after the first full UTC day |

## Development

```bash
pip install -e .[dev] && pytest -q
```

The tests run against `tests/fake_deriv.py`, a local imitation of Deriv's documented message formats, so they need no internet.
