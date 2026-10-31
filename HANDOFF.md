# Handoff prompt: Deriv research project

> Paste this whole file to any AI agent or developer who takes over. It is written to be read cold.

---

You are taking over a **research project**, not a trading-bot build. Read this fully before acting.

## 1. Who you are working for and how

- **Owner:** Pedro Isong, a founder and self-taught full-stack developer in Abuja, Nigeria (timezone Africa/Lagos, UTC+1). He **directs**; he is not the engineer.
- **He wants:** direct, blunt, data-grounded answers, and to be challenged rather than validated. Short messages. Exact commands to copy and paste, with the expected output. No long documents unless he asks.
- **Never:** promise profits; report a test or result you did not actually run; ask for passwords, API tokens, MT5 logins or private keys; place trades. Everything in Phase 0 is **read-only**.
- **Before asking him anything,** check whether you can find it out yourself. When you must ask, group the questions and give a sensible default for each.

## 2. The question the project answers

**Do Deriv's Volatility Indices (synthetic, RNG-generated prices) contain any pattern big enough to beat trading costs on MT5?**

- **Prior:** almost certainly not. Deriv states that a cryptographically secure RNG generates these prices at a constant volatility, and Deriv is the counterparty. The project exists to test that claim with evidence, not to assume an edge.
- **Possible outcomes:**
  - **NO EDGE:** precise enough data that any pattern is provably smaller than the spread. Stop or redirect.
  - **INCONCLUSIVE:** the report states how much more data is needed.
  - **CANDIDATE:** survives the statistics, the cost check, and a one-time holdout.
- **Even a CANDIDATE only earns a strategy design and a long demo trial, not real money.**
- Pedro once asked to "start making money in two weeks". He was told plainly that this system will not trade in two weeks, and that the likely verdict is NO EDGE. Hold that line.

## 3. What exists (all built and tested)

**Code:** <https://github.com/pedrocti/deriv-research> (public; contains no secrets and no data). Python package `dresearch`.

| Component | What it does | File |
| --- | --- | --- |
| probe | Measures what Deriv's public API serves; writes the collector config | `src/dresearch/probe.py` |
| collector | 24/7 live ticks plus an hourly re-download of each closed hour (gap repair) | `src/dresearch/collector.py` |
| backfill | Resumable history download, newest first, 120 requests/min | `src/dresearch/backfill.py` |
| audit | Daily data quality: completeness ≥ 98%, live-vs-history agreement, gap-vs-price-move bias test. Seals days (gzip + SHA-256) | `src/dresearch/audit.py` |
| analysis | Phase 0 tests against a simulated no-pattern null; break-even costs | `src/dresearch/analysis.py` (its docstring **is the protocol**) |
| dashboard | Read-only status page behind a secret URL | `src/dresearch/dashboard.py` |
| MT5 snapshot | Windows-only, read-only: spreads, swaps, specs, server clock offset | `windows/mt5_snapshot.py`, guide `docs/SETUP_WINDOWS.md` |
| tests | 22 pytest tests against a fake Deriv server and a fake MT5 | `tests/` |

The client refuses trading calls in code (`FORBIDDEN_CALLS` in `client.py`), and a test proves it. The MT5 script imports no order functions.

## 4. Infrastructure

**VPS:** Hostinger, Ubuntu **25.04**, 2 vCPU, 7.8 GB RAM, about 90 GB free, public IP `31.97.14.36`, clock synced to UTC.
- Ubuntu 25.04 reached end of life in January 2026, so its apt repositories are gone. **Install no apt packages.** Use the venv only.
- The VPS also runs Pedro's **production sites** (pm2 and PostgreSQL 17). Never touch them.
- Everything here runs as the unprivileged user `dresearch`, with systemd caps (memory and CPU limits, `ProtectSystem=strict`, writable only under `/var/lib/dresearch`).

**Paths:**
- code: `/opt/dresearch/src`
- venv: `/opt/dresearch/venv`
- data: `/var/lib/dresearch/{raw,cache,config,reports,logs}`
- `dresearch` command: `/usr/local/bin/dresearch`, which runs as the service user

**Services:**

| Unit | Role |
| --- | --- |
| `dresearch-collector` | always on |
| `dresearch-audit.timer` | daily at 00:20 UTC |
| `dresearch-backfill` | one-off download |
| `dresearch-dashboard` | always on, port 8787; `ufw allow 8787/tcp` was added |
| `dresearch-analyze` | oneshot, about 1 hour |

**Dashboard link:** run `dresearch dashboard-url` on the VPS. Treat the link as private. To rotate it: `rm /var/lib/dresearch/config/dashboard.json && systemctl restart dresearch-dashboard`.

**Updating the VPS after you push code:**

```bash
git -C /opt/dresearch/src pull -q && bash /opt/dresearch/src/deploy/install.sh
```

Use this rather than the `curl` raw URL, which can serve a cached old installer for about 5 minutes.

**Deriv API:**
- `wss://api.derivws.com/trading/v1/options/ws/public`: no authentication, no app ID.
- Documented limits are 220 calls/min and 14,400/hour.
- `ticks_history` returns at most **1000 ticks per call**, latest first, so long ranges are paged backwards.
- `active_symbols` rejects `product_type`; that error is harmless.

## 5. Measured facts (from Pedro's VPS, 29 Oct 2026)

- **History depth:** at least 1,825 days (the deepest point tested).
- **Collected:** `R_10, R_25, R_50, R_75, R_100` (tick every 2 s) and `1HZ10V … 1HZ100V` (every 1 s), plus `frxXAUUSD` (gold, weekdays).
  - `WLDXAU` is a gold *basket*, not spot gold. It was collected for about 10 minutes by mistake, then fixed; the data is harmless and ignored.
- **Volatility** measured on a 1,000-tick sample matches each label on a 365-day year: for example 0.746 for 1HZ75V and 1.009 for 1HZ100V.
- **Rounding ratio** (price step ÷ typical tick move):
  - R_100: 0.072
  - 1HZ10V: 0.058
  - 1HZ100V: 0.047
  - all others: below 0.01
  - Above about 0.05, rounding fakes tiny negative autocorrelation. The analysis null simulates rounding for exactly this reason. The starter kit Pedro first supplied fell for it, reporting p ≈ 1e-120 on a fair coin.

## 6. Project state at handoff (29 Oct 2026, evening, Lagos)

| Item | State |
| --- | --- |
| Collector | Running since about 18:20 UTC, 29 Oct. HEALTHY |
| Backfill | 90 days, started about 18:27 UTC, ETA about 9 h. Check the dashboard tile or `dresearch status` |
| **Protocol frozen** | **T0 = 2026-10-29T19:11:01Z**, code `d58ab9e`, analysis version 1.2 (`/var/lib/dresearch/config/freeze.json`). `analysis.py` has not changed since the freeze; later commits touched only the CLI, the dashboard and the Windows script |
| Discovery data | The 90 days before T0 |
| Holdout | Everything after T0. Opens **2026-11-26 19:11 UTC**. **One use only**: the code refuses a second run |
| First audit | 2026-10-30 00:20 UTC |
| Analysis | **Not yet run.** It is waiting for the backfill to reach 100% |
| MT5 snapshot | **Done** 29 Oct, 20:42 UTC, demo account on server Deriv-Demo, all 11 instruments found and imported. Median round-trip spread: V10 0.0038%, V10(1s) 0.0040%, V25 0.0113%, V25(1s) 0.0076%, V50 0.0262%, V50(1s) 0.0251%, V75 0.0336%, V75(1s) 0.0350%, V100 0.0531%, V100(1s) 0.0350%, gold 0.0037%. That is roughly 0.0004% per point of volatility. Commission is still unknown |

## 6b. Update 30 Oct 2026 (morning, Lagos)

**First discovery run:** finished 06:19 UTC. Report: `reports/analysis_discovery_*.json`.
- **All 10 indices: consistent with a fair random process on all 8 tests.** Volatility vs label ranges from 0.992 to 1.005.
- **Spread vs break-even:** above break-even (**no edge**) at 1, 5 and 15 minutes for every index. At 60 minutes it is **open** for 9 indices (break-even is 1.0 to 2.5 times the spread) and **no edge** for R_50.
- **But coverage was only about 70%.** Deriv refused 6,575 backfill requests for history older than about 60 days, with an undocumented limit called `RateLimit ... ticks_history_expensive`. Backfill 0.2.x logged those refusals and moved on, so 31 Jul to 30 Aug is mostly missing (2 or 3 hours per day).
  - The verdict is still unbiased: gaps are never bridged, and the null uses the same mask.
  - But it has less power, which is part of why the 60-minute column stays open.

**Fixed in commit 56ddfdf:**
- a refused request is retried page by page, keeping the pages already received;
- after a refusal: exponential wait, a halved request rate, and requests spaced evenly;
- nothing is skipped, and "finished" now means complete.

**Next step:**
1. Pedro updates the VPS and runs `systemctl start dresearch-backfill` to fill the holes.
2. When status shows finished, run `systemctl start dresearch-analyze` again, on the same 90-day window with unchanged analysis code. Record both runs.
3. The speed of Deriv's limit on older history is unknown. Measure it from `events.csv` (`backfill_rate_limited` lines) before proposing a 365-day extension.

## 6c. Track A final (31 Oct 2026, 00:55 Lagos): NO EDGE, closed

- **Second discovery run on complete data:** coverage 98.9%, same 90-day window, analysis code unchanged since the freeze.
- **All 10 indices are consistent with a fair random process on all 8 tests.** Measured volatility is within 0.5% of each label (0.995 to 1.005).

Break-even round-trip cost, in index points, for the simple momentum/reversal rule (95% bound), against the MT5 spread:

| Index | 1 min | 5 min | 15 min | 60 min | MT5 spread |
| --- | --- | --- | --- | --- | --- |
| R_10 | 0.0025 | 0.0175 | 0.0620 | 0.342 | 0.188 |
| R_25 | 0.0046 | 0.0458 | 0.0736 | 0.326 | 0.331 |
| R_50 | 0.00025 | 0.0012 | 0.0041 | 0.0208 | 0.023 |
| R_75 | 0.132 | 0.868 | 2.478 | 12.11 | 12.09 |
| R_100 | 0.0033 | 0.0184 | 0.0538 | 0.3125 | 0.300 |
| 1HZ10V | 0.0081 | 0.0232 | 0.0828 | 0.461 | 0.38 |
| 1HZ25V | 1.66 | 7.81 | 19.65 | 135.4 | 70.05 |
| 1HZ50V | 0.86 | 3.70 | 8.21 | 64.33 | 51.75 |
| 1HZ75V | 0.029 | 0.117 | 0.305 | 1.734 | 1.71 |
| 1HZ100V | 0.0080 | 0.0295 | 0.0923 | 0.8325 | 0.42 |

**Verdict:**
- **No edge** at 1 to 15 minutes on every index; the spread is 3 to over 100 times the break-even.
- At 60 minutes, R_25 and R_50 are no edge. The other eight are open, with break-even 1.0 to 2.0 times the spread. No IC was significant, so the best case is roughly covering the spread. That is not an edge. **Decision: no bot for Volatility Indices.**
- **Remaining:** keep the collector running, then run `dresearch analyze --holdout` once, on or after 2026-11-26 19:11 UTC, as formal confirmation. After that the Track A services can be stopped.
- **Measured download speed for history older than 60 days:** about 400 symbol-hours per hour, with rare refusals once requests were spaced out.

## 6d. Track B (gold, possibly crypto): started 31 Oct 2026

Pedro chose to research gold, possibly with crypto. Nothing has been built for it yet. Plan:
1. Run a cost kill-check first, using swaps from the MT5 snapshot.
2. Probe Deriv's free candle history depth for frxXAUUSD and crypto.
3. Preregister a small time-series-momentum hypothesis set.
4. Walk-forward validation with a deflated Sharpe ratio and costs.
5. A forward demo period as the holdout.

Do not reuse the synthetics null model: gold and crypto have real drift and volatility clustering.

## 6e. Track B results (31 Oct 2026, protocol v1.0, commit 03008c3; run on code 0d50cd5)

Data:
- gold monthly 1971-10 to 2026-09 (World Bank);
- T-bill rate (FRED TB3MS, fetched via the curl fallback);
- BTC, ETH and PAXG daily from Binance, checksum-verified;
- Deriv costs from MT5 snapshot 2026-10-31: XAUUSD swap -4.24 / +1.88 %/yr; BTCUSD -20 / -20 %/yr; ETHUSD -15 / -15 %/yr; spreads BTC 0.0032%, ETH 0.026%, XAU 0.0038%.

**Gold, primary L=12, after costs:**

| Period | Return/yr | Sharpe | NW t | p | Max DD |
| --- | --- | --- | --- | --- | --- |
| Discovery 1971-2012 | 7.7% | 0.68 | 3.85 | <0.001 | -35% |
| Holdout 2013+ | 3.0% | 0.32 | 1.01 | 0.156 | -38% |
| Holdout, costs doubled | 1.8% | 0.22 | — | — | — |

- Buy-and-hold CFD in the holdout: Sharpe 0.38.
- L=3 holdout Sharpe 0.45 (p 0.066); L=6 holdout Sharpe 0.54 (p 0.040). These are **not** to be adopted post hoc.
- **Verdict: INCONCLUSIVE.** The edge decayed by about half after publication and is no better than holding gold.

**Crypto, BTC+ETH, after costs:**
- Discovery: Holm p 0.047 for L=7 and L=28.
- Holdout 2023+: L=28 Sharpe 0.43 (p 0.19); L=7 Sharpe -0.13; L=90 Sharpe 0.09.
- **Verdict: INCONCLUSIVE.**

**PAXG (information only):** L=60 Sharpe 0.70, L=120 Sharpe 0.65, over 5.8 years.

**Options put to Pedro:**
1. A preregistered diversified multi-market trend portfolio (World Bank commodities, FRED FX, plus Deriv costs per instrument). Recommended if continuing.
2. A forward demo of the v1.0 primary rules (slow evidence).
3. Stop.

## 6f. Track C: Ethereum hours-to-days, sized for $100 (protocol v1.0, committed 31 Oct 2026 before any result)

Pedro's brief after Track B: his own rules, holds of hours to days, gold/BTC/ETH, max loss 10%, account $100.
Position-size check: at minimum lots only ETH (0.1) fits a $100 account with ~2% risk per trade; gold (0.01 lot = 1 oz)
needs ~$2,300 and BTC (0.01) ~$1,200. So ETH is tested; BTC runs as information only.
Rules are the docstring of `src/dresearch/swing.py` (S1 crash bounce, S2 daily follow-through, S3 hour of day;
0.9%/1.8% stops; Holm across five variants; holdout from 2023-01-01; CANDIDATE needs discovery Holm p<0.05,
holdout mean>0 with p<0.10, and still >0 at 2x costs). Data: Binance ETHUSDT/BTCUSDT 1h (`dresearch external`).
Simulator check: tests show random entries on a fair price earn no significant profit at 0.9%, 1.8% or no stop
(an earlier test harness with shallow fixed wicks made tight stops look profitable; bars are now cut from a finer path).
Run: `dresearch swing`. Result: (to be recorded here).

## 7. Methodology (summary; details in the `analysis.py` docstring)

**Null model N1:**
- a martingale price at the measured per-tick volatility, estimated from 5-minute moves so rounding can't inflate it;
- rounded to Deriv's quoted decimals;
- the same missing-tick mask as the real data;
- restarted from the real price every 6 hours, so the price level (and therefore the rounding distortion) matches. The return across each restart is excluded from both real and simulated data;
- 100 simulations.

**Eight tests per index**, controlled with Holm at 1%, plus Benjamini–Hochberg across indices:
- drift;
- tick autocorrelation, lags 1–50;
- 1-minute and 5-minute autocorrelation;
- variance ratios at 1, 5, 15 and 60 minutes;
- autocorrelation of absolute 1-minute returns (volatility clustering);
- hour-of-day effects on direction and on move size (scale-free).

Spreads are pooled across exchangeable components (lags, hours). Non-exchangeable components use Student-t tails with an inflation factor of √(1+1/B).

**Economic test:** the IC of past versus next h-minute return, for h in {1, 5, 15, 60}. Its 95% bound gives the **break-even round-trip cost** c_min = U · σ_h · √(2/π). If the MT5 spread alone is above c_min, the verdict for that horizon is **NO EDGE**. Commission only adds cost, so it can't reverse that verdict.

**Calibration (measured):** false-alarm rate on fair walks, 120 runs each, with 40 simulations:

| Rounding | False alarms |
| --- | --- |
| none | 2/120 |
| 0.07 | 2/120 |
| 0.5 | 4/120 |

Planted patterns (tick memory, slow trend, drift) are detected. Full size, 7.8 M ticks per 1s index: about 4 minutes per index on a full core, peak memory about 0.9 GB.

**Known limitations**, in priority order:
1. The economic test covers only the simplest momentum/reversal rule family.
2. Commission is unknown, since MT5 doesn't expose it in symbol info.
3. The analysis uses API spot prices. Whether MT5 quotes equal spot ± spread is not yet verified (a tick-matching step is a to-do).
4. There is no parameter-stability (H8) test yet.
5. Variance-ratio and autocorrelation inference is calibrated by simulation, not by the closed-form Lo–MacKinlay or Lobato statistics.
6. If you change the tests, **do not** reuse the discovery verdict as if it were preregistered. Bump `ANALYSIS_VERSION`, write down the deviation and why, and never touch the holdout to choose between versions.

## 8. What to do next, in order

1. **When the backfill shows 100%:** `systemctl start dresearch-analyze`. About an hour later, results appear on the dashboard under **Pattern tests** and in `/var/lib/dresearch/reports/analysis_discovery_*.json`. Read them yourself before summarising for Pedro.
2. **MT5 snapshot: done.** Repeat it only if Deriv changes its spreads. Pedro's PC runs Python 3.14.5 with MetaTrader5 5.0.6231, which has a cp314 wheel. The script is in `C:\Users\user\deriv-research`. Once the analysis has run, the dashboard shows **no edge / open** per index and horizon.
3. **Interpret honestly:**
   - **No deviations, and the spread above break-even at every horizon:** the discovery verdict is NO EDGE. Say so plainly and recommend stopping Track A. Do not build a bot.
   - **A deviation:** first suspect an artefact (rounding, feed construction, gaps). Check the audit's missingness test, compare MT5 with the API, and replicate only on the holdout after 26 Nov. Never trade on a discovery-only result.
   - **"Open" horizons:** state how much more data would settle them. Extending the backfill (`BACKFILL_DAYS` in the service, or `dresearch backfill --days 365`) is cheap.
4. **Track B (gold) is undeveloped.** `frxXAUUSD` ticks are collected for free. A credible gold edge would be low-frequency (for example time-series momentum), needing long daily data, swap costs and a power analysis (a Sharpe ratio of 0.4 needs about 25 years for t = 2). Don't reuse the synthetics null for gold: gold has real drift and volatility clustering.
5. Keep the daily audit green, and check `journalctl -u dresearch-collector -n 50` if the dashboard says "Needs attention".

## 9. Command cheat sheet (VPS, as root)

```bash
dresearch status                                  # health + download progress
dresearch dashboard-url                           # private status page link
systemctl start dresearch-analyze                 # Phase 0 tests (after backfill = 100%)
journalctl -u dresearch-analyze -n 30             # its output
dresearch audit --day 2026-10-30 --no-seal        # re-print an audit
dresearch import-mt5 /tmp/mt5_snapshot.json       # load the Windows snapshot
systemctl status dresearch-collector dresearch-backfill dresearch-dashboard
```

## 10. Development

```bash
git clone https://github.com/pedrocti/deriv-research && cd deriv-research
pip install -e .[dev] && pytest -q          # 22 tests, about 20 s, no internet needed
```

- Pinned versions: `websockets==15.0.1`, `numpy==2.5.3`, `scipy==1.18.1`; Windows uses `MetaTrader5==5.0.6231`.
- Commit as Pedro's project. Push to `main`, then have Pedro run the update command in section 4.

There is also a longer research plan, a Claude doc titled "Phase 0 Research Plan — Deriv Volatility Indices & Gold" (revision 2). This file supersedes it where they differ.
