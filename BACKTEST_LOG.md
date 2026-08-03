# Backtest / Live-Readiness Log

Read this file first, every run. Append a dated entry at the bottom before you finish.
This is the only memory that survives between runs — each cloud session starts fresh
with zero conversation history, so if it isn't written here, it's lost.

## Goal

Get `bot.py`'s trading strategy to a state where `backtest.py`'s automated
`check_live_readiness()` verdict says READY — on more than one symbol, not just
one that happened to overfit. Currently: paper-trading only, nowhere close to ready.

## The objective bar (backtest.py: check_live_readiness())

ALL of these must pass (any one failing blocks readiness regardless of score):
- OOS test profit factor > 1.4
- Total trades >= 100
- Validation-period PnL positive
- Test max drawdown < 15%
- PF at 2x fees > 1.2 (robustness scenario — simulates worse execution/slippage)
- Walk-forward consistency >= 50% of 6 windows profitable
- Expectancy per trade positive

Plus score >= 80/100 (adds win rate > 40%, train PF > 1.2, max losing streak < 8,
top-5-trades concentration <= 60% of profit).

Run it via: `python backtest.py --symbol SYMBOL --strategy STRAT --days 1095`
(single symbol, NOT `--no-robustness` — we need the full robustness matrix,
not the fast multi-symbol scan, to get a real readiness verdict).

## What's been tried so far (as of 2026-07-25)

### Single-symbol full backtests (1095d, with robustness)
| Symbol | Strategy | Trades | OOS PF | Readiness | Verdict |
|---|---|---|---|---|---|
| BTCUSDT | trend_pullback | 110 | 0.96 | 26/100 | NOT READY |
| SOLUSDT | trend_pullback | 76 | 0.45 | 16/100 | NOT READY |
| BTCUSDT | auto | 83 | 1.39 | 32/100 | NOT READY (closest single number, but see concentration below) |

Common failure pattern across all three: fee-sensitive (PF collapses below 1.0 at 2x
fees), validation period loses money, walk-forward only 17-33% consistent.

BTC `auto` specifically showed severe profit concentration: top 1 trade = 289% of
total profit, top 5 trades = 717%, more losing months (15) than winning (13) — the
apparent edge is a couple of lucky trades, not a real one.

### Universe scan (12 random top-50 USDT pairs, 730d, auto strategy, no-robustness)
Run 1 (before backtest.py fix): crashed on UUSDT — `run_walk_forward()` had an
IndexError when a symbol had fewer than 6 distinct entry-bar windows (fixed —
see git history, function now breaks out of the loop instead of indexing past
the end of `bars`). Also found and fixed a divide-by-zero: price fields were
rounded to 4 decimals, which zeroes out for sub-cent tokens like LUNC, corrupting
PF into `inf`/`nan`. Both fixes are in this repo now.

Run 2 (after fix): 9/12 symbols produced trades (3 had no trades — likely too
thin/new for the lookback). Results:

| Symbol | Trades | PF | Return | Score |
|---|---|---|---|---|
| UNIUSDT | 33 | **1.60** | +6.2% | 68 |
| ETHUSDT | 49 | 0.74 | -3.2% | 26 |
| SOLUSDT | 43 | 0.88 | -1.2% | 32 |
| PROMUSDT | 32 | 0.86 | -1.2% | 32 |
| TONUSDT | 19 | 0.79 | -1.2% | 42 |
| ZAMAUSDT | 10 | 0.68 | -0.8% | 26 |
| EULUSDT | 6 | 0.75 | -0.7% | 63 |
| UTKUSDT | 14 | 0.16 | -5.6% | 16 |
| ENAUSDT | 11 | 0.12 | -5.4% | 16 |

**Universe verdict: WEAK edge — 0/9 ready, only 1/9 profitable, average PF 0.73.**
UNIUSDT stands out (PF 1.60) but is one symbol out of a random sample — could be
noise. Worth a full single-symbol run on UNIUSDT to see if it holds up under the
OOS split / walk-forward / robustness treatment, not just the fast scan.

### Robustness matrix findings (single-variable stress tests from baseline)
Ran on BTCUSDT for both `trend_pullback` and `auto`:

| Scenario | trend_pullback PF | auto PF |
|---|---|---|
| baseline | 0.78 | 1.05 |
| **no trail stop** | **0.99** | **1.31** (PnL +1.52 -> +10.47, win rate 42%->48%) |
| wider stop (ATR mult 2.0) | 0.86 | 1.10 |
| limit orders (maker fee) | 0.78 (flat) | 1.10 |
| 0.5% slippage | 0.27 | 0.40 |
| delayed entry +1 bar | 0.49 | 0.76 |
| 2x fees | 0.61 | 0.90 |
| 3x fees | 0.53 | 0.69 |

Two takeaways:
1. **Disabling the trailing stop (`trailing_stop_enabled: False` in CONFIG) was
   the single biggest improvement found so far.** Hypothesis: the trailing stop
   is getting shaken out by normal noise before trades reach full TP, converting
   winners into scratches.
2. **Strategy is very sensitive to execution quality** (slippage/entry-delay
   scenarios collapse PF toward 0.3-0.5). This means the raw edge is thin
   relative to costs — tightening entry quality (fewer, better trades) may
   matter more than tweaking exits.

### CONFIRMED: full readiness run with trailing stop disabled (BTCUSDT, auto, 1095d)
This was run through the complete pipeline (OOS split, walk-forward, robustness,
concentration, readiness check) via `_run_one_symbol` with
`cfg["trailing_stop_enabled"] = False` — not just the single-scenario robustness
table above. See the 2026-07-25 run-log entry at the bottom for full numbers.

**Bottom line: disabling the trailing stop is a confirmed, real improvement —
readiness score 32/100 -> 53/100 — but still NOT READY.** Validation PnL flipped
from losing to profitable, walk-forward consistency improved to exactly the 50%
minimum. Still blocked by: OOS test-period PF (0.72, need >1.4), trade count
(71, need >=100), and PF at 2x fees (1.07, need >1.2 — close but not there).
Concentration is now flagged FRAGILE (top 5 trades = 131% of profit). See the
run-log entry for next-step ideas (stacking wider stop / limit orders / no-dyn-TP
on top of no-trailing-stop, since each individually pushed PF well above 1.2).

## Methodology — follow this discipline

1. **One variable at a time.** Change one thing in `CONFIG` (bot.py) or one
   strategy parameter, rerun a full single-symbol backtest with robustness,
   compare the readiness verdict to the previous entry in this log. Don't stack
   multiple changes — you won't know what worked.
2. **Cross-check across symbols before declaring anything solid.** A change
   that only fixes BTC risks being overfit to one asset's history. Test at
   least BTC, SOL, and 1-2 of the more promising universe symbols (UNIUSDT is
   the current best candidate).
3. **Every run, append a dated entry to this file**: what you tried, the
   before/after numbers, whether it helped, and what to try next. Be specific
   with numbers — future-you (tomorrow's run) has no other memory of this.
4. **Trade quality over trade quantity** is worth testing given how fee-sensitive
   everything is: try raising `score_threshold`, tightening RSI/volume filters,
   or requiring stronger regime alignment, to see if fewer/better trades survive
   the 2x-fee stress test better than the current signal set.

## Hard safety boundaries — do not cross these, ever

- **Never set `--live` or enable real trading in any form.** This project only
  ever runs in paper mode until a human explicitly decides otherwise.
- **Never add, modify, or commit real API keys/credentials** (Binance, Telegram,
  or otherwise). `cryptobot.service` has placeholder values — leave them as
  placeholders.
- **Never touch deployment/infra files** in a way that would affect the actual
  running paper-trading bot (there's a live paper bot running on a separate
  server this repo doesn't have access to — you can't reach it, and shouldn't
  try to).
- **Never push directly to `main`.** Always work on a feature branch and open
  a PR. A human reviews and merges. If `gh` isn't available/authenticated in
  the sandbox, push the branch and leave clear instructions in this log for
  opening the PR manually.
- If a change makes `bot.py` non-functional (syntax errors, crashes on import,
  breaks the live-trading class interfaces), do not merge it — that's exactly
  the kind of bug that was found and fixed in `LiveTrader` recently (missing
  method params caused it to error-loop forever without ever placing a trade).
  Sanity-test with a syntax check and, if you touch `LiveTrader`/`PaperTrader`,
  a quick mocked-client smoke test before committing.

---

## Run log

### 2026-07-25 (seed entry — written by the setup session, not an actual run)
Repo seeded with this log and the fixes described above (walk-forward IndexError,
divide-by-zero on low-price symbols, LiveTrader kill-switch/param gaps). No new
backtest iteration performed as part of this entry — next run should start with
the no-trail-stop + 2x-fees combined test described above.

### 2026-07-25 (manual run, same setup session — BTCUSDT, auto, trailing_stop_enabled=False)
Ran `_run_one_symbol` directly (not the CLI) with `cfg["trailing_stop_enabled"] = False`,
BTCUSDT, 1095 days, full robustness matrix. Full numbers:

- **Whole-period**: 71 trades, 46.5% win rate, PF 1.25, expectancy +0.119, max DD 2.7%,
  return +4.2%, Calmar 0.5.
- **OOS split**: train PF 1.22 (+4.90), validation PF **2.30** (+5.55, now positive —
  this was -1.64 with trailing stop enabled), test PF **0.72** (-2.02, still weak/losing).
- **Walk-forward**: 3/6 windows profitable (50%, meets the minimum exactly), avg PF 1.52.
  Windows 2/3/6 lost money, windows 1/4/5 won — no obvious pattern by window index alone,
  worth checking if losing windows cluster in a specific market regime.
- **Robustness** (scenarios below are relative to this no-trail-stop baseline, i.e.
  "2x fees" here means "no trail stop AND 2x fees together"):
  - 2x fees: PF 1.07 (fails the >1.2 bar, but much better than the 0.90 it was with
    trailing stop enabled)
  - 3x fees: PF 0.92
  - 0.5% slippage: PF 0.63 (still the most damaging single scenario)
  - **limit orders: PF 1.33** (flagged "SOLVES" by the tool — maker fee + conditional
    fill clears the 2x-fee-equivalent bar on its own)
  - **ATR_mult=2.0 (wider stop): PF 1.47**
  - **no dyn TP: PF 1.50** (best single-lever result in this matrix)
  - delayed entry +1 bar: PF 0.98 (execution-timing sensitivity persists)
- **Concentration: FRAGILE.** Top 1 trade = 29.5% of profit, top 5 = **131%** of
  profit, top 10% = 84%. 15 profitable months vs 12 losing months.
- **Readiness verdict: 53/100, NOT READY.** Blocked by 3 critical checks:
  - `pf_test`: OOS test PF 0.72 < 1.4 required
  - `trade_count`: 71 < 100 required
  - `pf_2x_fees`: 1.07 < 1.2 required
  Passing: oos_positive, max_dd, walk_forward (exactly at the 50% line), expectancy,
  pf_train, streak. Failing bonus check: win_rate (test period 40.0%, needs >40%
  strictly), concentration.

**Verdict: real, confirmed improvement over trailing-stop-enabled (32->53 readiness),
but not there yet.** Three specific next steps, in priority order:

1. **Stack the individually-promising levers together** on top of no-trailing-stop:
   try `no trail stop + ATR_mult=2.0` and `no trail stop + no dyn TP` (each alone hit
   PF 1.47-1.50) combined with the 2x-fees stress test, to see if stacking clears the
   1.2 bar with more margin than the current 1.07. Also try `no trail stop + limit
   orders` combined with 2x fees specifically (limit orders alone already clears 1.33,
   worth confirming it holds under stress too).
2. **Trade count is short (71 vs 100 minimum).** Try a longer backtest window (e.g.
   `--days 1460`, ~4 years, if Binance has that much history for BTCUSDT) to accumulate
   more trades without changing the strategy itself — cheapest fix to try first since
   it requires no code change.
3. **Cross-check on SOLUSDT and UNIUSDT** with `trailing_stop_enabled=False` before
   concluding this generalizes — everything above is BTC-only so far. UNIUSDT in
   particular showed PF 1.60 in the universe scan and deserves the same full-pipeline
   treatment (OOS split / walk-forward / robustness), not just the fast scan number.

If the stacked-lever test on BTC clears the pf_2x_fees bar with a reasonable margin
(not just barely over 1.2) and the trade-count fix gets to >=100 trades, re-run the
full readiness check — that combination could plausibly clear most/all critical
checks. Concentration (top5=131%) will likely need separate attention (tighter entry
filters?) even if the above works.

### 2026-08-02 (manual run — BTCUSDT, auto, stacked: no trailing stop + no dynamic TP + ATR_mult=2.0, 1460d)
**Result: the stacking hypothesis was WRONG. Stacking hurt more than it helped.**
Readiness score dropped to **47/100** (vs 53/100 for no-trailing-stop alone). Full numbers:

- 75 trades (barely more than the 71 at 1095d — extending the window to 4 years
  did NOT meaningfully add trades; this strategy is inherently low-frequency in
  this data, not window-limited. **Deprioritize "longer window" as a fix for the
  trade-count shortfall** — it's not working.)
- Train PF fell to **0.97** (was 1.22, now fails its own >1.2 bonus check — new
  failure that wasn't there before)
- OOS test PF 0.74 (basically unchanged, still fails)
- 2x fees PF fell to **1.00** (was 1.07 with no-trailing-stop alone — stacking made
  fee-sensitivity worse, not better)
- Concentration got markedly worse: top 5 trades = **193%** of profit (was 131%),
  top 1 trade = 43% (was 29.5%), best quarter = 99% of all profit. More fragile,
  not less.

**Why this makes sense in hindsight**: `no dynamic TP` reverts to CONFIG's flat
`take_profit_rr` (2.0), it does NOT mean "no take profit" — combined with the wider
ATR stop, trades take longer to resolve and the strategy leans harder on a handful
of big winners to stay profitable. The three levers don't compose additively; they
change the trade's risk/reward shape in ways that fight each other.

**New lead worth chasing, found inside this run's own robustness matrix** (these
are single-variable overrides *on top of* the stacked baseline, so read with that
in mind — but still informative): `TP_RR=3.0` scenario hit **PF 1.43** with fewer,
presumably higher-quality trades (70). `TP_RR=1.5` hit PF 1.27 with *more* trades
(81 — closer to the 100 minimum, win rate 55.6%). This suggests explicitly setting
a **fixed** `take_profit_rr` (not just disabling dynamic TP, which falls back to
2.0) is worth testing directly, combined with no-trailing-stop but WITHOUT the
wider ATR stop this time (since ATR_mult=2.0 didn't demonstrably help here and may
have contributed to the concentration problem via longer average hold times).

**Revised next step**: test `trailing_stop_enabled=False` + `dynamic_tp_by_regime=False`
+ `take_profit_rr=1.5` explicitly (not 2.0 default, not 3.0) on BTCUSDT at 1095d
(revert to the shorter window — 1460d didn't help and this makes runs faster) —
`TP_RR=1.5` had the best trade-count-vs-PF tradeoff of the two seen here. If that
doesn't clear the bar either, `TP_RR=3.0` is the fallback to try next. Either way,
drop the wider ATR stop from the combination — it wasn't earning its keep.

Also unresolved from before: still haven't cross-checked SOLUSDT or UNIUSDT with
any of these configs — everything so far is BTC-only. Worth doing once a BTC config
looks genuinely promising, not before (no point cross-checking something that
isn't working yet).

### 2026-08-02 (infra note — scheduled cloud routine appears unable to push)
The daily cloud routine (this repo + stock-advisory) has fired on schedule every
day since 2026-07-25 (confirmed via `last_fired_at` on the trigger) but has
produced **zero commits, branches, or PRs** on either repo in 8 days. A minimal
diagnostic task (create a branch, write one file, commit, push — nothing else)
also produced no branch after 10+ minutes, which is far longer than that task
should take if it succeeded. This points to a write-permission gap on the GitHub
App connection (it very likely has read/clone access but not push access), not a
problem with the research task itself. Not yet fixed — if you're an agent reading
this and hit the same wall (git push hangs, fails, or silently does nothing),
that's a known issue, not something to debug further on your end. A human needs
to check the GitHub App's permission scope in the repo settings.

### 2026-08-02 (infra update — push now works; new BLOCKING issue found: Binance API unreachable)

**Push issue above appears resolved, at least in this session's environment.**
Tested directly: `git checkout -b`, edit, commit, `git push -u origin <branch>`
completed in a few seconds and the branch showed up on GitHub (`new branch`
confirmation from the remote, PR-creation link printed). So raw `git push` is
not the blocker today — no need to route around it via the GitHub API tools.
(Minor: deleting that same throwaway branch afterward via `git push origin
--delete` got a 403 — branch *creation*/push works, deletion apparently
doesn't have permission. Left a harmless empty branch
`infra-test-push-<timestamp>` on the remote with one throwaway commit, no PR
opened against it — safe to delete manually, not worth more automated effort
chasing the delete permission.)

**New, more serious blocker: this environment's network egress policy blocks
Binance's API entirely**, which means **zero new backtests could be run this
session** — the revised next step from the previous entry (no-trailing-stop +
no-dynamic-tp + TP_RR=1.5 on BTCUSDT) was never executed, nor was the
UNIUSDT/SOLUSDT cross-check.

Evidence: `backtest.py`'s `Client("", "")` init (just pinging Binance) throws
`ProxyError: ... Tunnel connection failed: 403 Forbidden` immediately. Checked
the proxy diagnostic endpoint directly (`curl $HTTPS_PROXY/__agentproxy/status`)
— it logs `recentRelayFailures: [{kind: "connect_rejected", detail: "gateway
answered 403 to CONNECT (policy denial or upstream failure)", host:
"api.binance.com:443"}]`. Tried `api.binance.com`, `api1.binance.com`,
`data-api.binance.com`, `fapi.binance.com` directly with `curl` — all 403 at
the CONNECT tunnel stage. For contrast, `pypi.org` and `api.github.com` both
return 200 through the same proxy, so this isn't a general outage — it's a
host-specific egress allowlist that simply doesn't include Binance. Per the
proxy's own README: `403/407 = organization policy denial, do not retry or
route around it, report the blocked host` — so this was not treated as
something to work around (e.g. no attempt to scrape prices from elsewhere).

**Also checked stock-advisory's data source for the same problem**: Yahoo
Finance (`query1.finance.yahoo.com`, `query2.finance.yahoo.com`,
`finance.yahoo.com`, used by `yfinance`) is **also blocked the same way**
(403 at CONNECT). So today's stock-advisory work also can't pull live
tickers — pivoted that repo's iteration to test coverage using synthetic
inputs instead (see IMPROVEMENT_LOG.md), which doesn't need network access.

**No local candle cache exists** in this repo (`fetch_history()` always hits
the live API, nothing in `reports/`, `portfolio.json`, or elsewhere caches
OHLCV data) — so there's no offline fallback for backtesting today.

**Action needed from a human**: allowlist `api.binance.com` (and ideally the
other Binance hosts above, for resilience) in this environment's egress
policy — same ask for `query1.finance.yahoo.com` / `query2.finance.yahoo.com`
if stock-advisory's live-data work should also run in this environment. Until
that's done, every future firing of this routine will hit the identical wall
on the primary crypto task — worth checking whether this is fixable in this
session's environment settings before the next scheduled run, otherwise the
routine will keep burning a cycle for nothing here every day.

**Next step once network access is fixed**: pick up exactly where entry
2026-08-02 (stacked-lever test) above left off — run
`trailing_stop_enabled=False` + `dynamic_tp_by_regime=False` +
`take_profit_rr=1.5` on BTCUSDT at 1095d (not 1460d — the longer window
didn't add trades), then cross-check on SOLUSDT and UNIUSDT if BTC clears
or nearly clears the readiness bar. Nothing about the strategy or CONFIG
changed in this entry — this was a pure infra-diagnosis run.

(Note: the network egress block above was fixed the same day, outside this
environment — the GitHub App had write access sorted out, and the TP_RR=1.5
test below was run manually via direct Binance access on a separate server,
not through this cloud environment. Egress to Binance/Yahoo from *this*
sandboxed environment specifically may still be unresolved — worth re-checking
before assuming future scheduled runs here can fetch live data.)

### 2026-08-02 (manual run — BTCUSDT, auto, no trailing stop + fixed TP_RR=1.5, 1095d)
**BREAKTHROUGH: readiness score 84/100 — every critical check passes except one.**

Full numbers:
- **Whole-period**: 74 trades, 59.5% win rate, PF 1.46, expectancy +0.169, max DD 1.7%,
  return +6.2%, Calmar 1.2.
- **OOS split**: train PF 1.37 (+6.77), validation PF 1.71 (+3.04), test PF **1.61**
  (+2.68). All three splits profitable with PF > 1.3 — the test/OOS period has been
  the weak link in every prior attempt (0.72-0.74); here it's the second-best split.
- **Walk-forward: 6/6 windows profitable (100% consistency)**, avg PF 1.58. Every
  single window won, no exceptions — first time this has happened.
- **Robustness**: 2x fees PF **1.22** (clears the >1.2 bar, but thin margin — worth
  noting, not comfortable headroom), 3x fees PF 1.01 (marginal), 0.5% slippage PF 0.61
  (still the most damaging scenario — execution-quality sensitivity persists),
  limit orders PF 1.57 (flagged "SOLVES"), delayed entry +1 bar PF 0.86.
- **Concentration: OK** (flipped from FRAGILE). Top 1 trade = 13.8% of profit, top 5 =
  61.6% (barely over the 60% bonus-check line), top 10% = 50.9%. 16 profitable months
  vs 11 losing — much healthier ratio than any prior test.
- **Readiness: score 84/100** (clears the >=80 bar). **Only blocking failure:
  trade_count (74 < 100)** — every other critical check (pf_test, oos_positive,
  max_dd, pf_2x_fees, walk_forward, expectancy) passes. Only bonus check failing is
  concentration (62% vs 60%), which isn't a blocker on its own.

**This is the closest result yet, by a wide margin.** The remaining gap is a sample-size
problem, not a strategy-quality problem — every quality signal (OOS PF, walk-forward,
concentration, expectancy) is now solid.

**Immediate next step**: extend the backtest window (try `--days 1460`, and further if
data allows) with this exact config. Unlike the earlier stacked-lever test (wider ATR
stop meant trades held longer, so a longer window barely added trades — 71 to 75), this
config's tighter TP_RR=1.5 resolves trades faster, so a longer window should add
proportionally more trades this time. If trade count clears 100 while the other metrics
hold up, this could be genuinely READY on BTC. Then: cross-check on SOLUSDT and UNIUSDT
before treating it as generalized rather than BTC-specific. Also keep an eye on the
2x-fees margin (1.22 vs the 1.2 minimum is thin) and the 0.5%-slippage sensitivity —
neither is a blocker today, but both are worth monitoring as the config gets tested
further, since they're the two spots this result is least comfortable.

### 2026-08-03 (infra re-check — Binance/CoinGecko still blocked in this cloud environment; no new backtest possible here)
Re-tested egress from this sandboxed environment before starting: `api.binance.com`
plus its mirrors (`api1`, `api2`, `api3`, `data-api`, `fapi`.binance.com) and, as an
alternative data source, `api.coingecko.com` — **all six return 403 at the CONNECT
tunnel stage**, identical to the 2026-08-02 finding. This is not Binance-specific,
it's a market-data-host egress policy denial in this environment. Per the proxy's own
README (403/407 = organization policy, do not retry or route around it), no attempt
was made to scrape prices from elsewhere. No local OHLCV cache exists in this repo to
fall back on, so **zero new backtests were possible this session from this
environment**.

**This is a persistent, actionable blocker, not a one-off.** Every scheduled firing
since ~2026-07-25 that depended on this environment for live data has hit the
identical wall. Until a human allowlists a crypto price source in this environment's
egress policy, this repo's daily cloud iteration cannot make forward progress on new
backtests *here* — it can only re-confirm the same block. Time was redirected to
stock-advisory instead (see that repo's IMPROVEMENT_LOG.md). The good news: this
doesn't block progress overall — see the entry immediately below, which answers
exactly the question this session couldn't reach, done manually via a separate
server with working Binance access.

### 2026-08-03 (manual run — BTCUSDT, auto, same config, 1460d instead of 1095d)
**Extending the window is a real trade-off, not a clean win.** Trade count did climb
to 97 (much closer to 100, confirming the "faster-cycling config adds more trades over
more history" hypothesis) — but quality degraded enough that readiness actually
**dropped, from 84/100 to 68/100**:

- PF fell to 1.20 (was 1.46). Train-split PF fell to 1.06 (was 1.37, now fails its own
  >1.2 bonus check).
- **2x-fees PF fell to 1.00 (was 1.22) — this critical check now fails again.**
- Walk-forward dropped to 5/6 windows (83%, was 100%) — still clears the 50% minimum
  comfortably, but window 1 lost money (-2.60, PF 0.73).
- **Concentration reverted to FRAGILE**: top 5 trades = 96% of total profit, top 10% =
  96% (was 62%/51% at 1095d). A tiny number of trades now account for almost the
  entire result.
- Readiness: 68/100, blocked by trade_count (97 < 100) AND pf_2x_fees (1.00 < 1.2).

**Interpretation**: the extra ~13 months of history (reaching back further, likely into
a rougher stretch — 2023-01 shows up as the best single month at 36% of total profit,
suggesting some of the added trades are lumpy/concentrated wins in a choppier period)
adds trade volume but appears to dilute the strategy's edge rather than confirm it.
The 1095d window may be capturing something closer to the strategy's actual sweet
spot rather than being merely "too short." Don't assume more history = better from
here on; test it, don't assume it.

**Revised next step — deprioritize "extend BTC's window further"**: instead, cross-check
the ORIGINAL winning config (no trailing stop + `take_profit_rr=1.5`, **1095d, not
1460d**) on SOLUSDT and UNIUSDT. This answers the more important open question (does
this edge generalize beyond BTC, or is it BTC-specific) rather than continuing to
chase BTC's own trade count into a regime that hurts quality. If either symbol
independently produces a strong, high-readiness result at 1095d, that's stronger
evidence for the strategy than squeezing one symbol past 100 trades at the cost of
concentration/fee-sensitivity. If both SOL and UNI also look strong, it may be worth
testing whether *pooling* evidence across symbols (rather than one symbol needing
100 solo trades) is a more sensible readiness bar going forward — worth a discussion
with the human before changing the bar itself, since check_live_readiness() currently
evaluates one symbol at a time by design.

**No CONFIG changes have been merged yet** — the TP_RR=1.5 config is promising (84/100
at 1095d on BTC) but not yet cross-validated across symbols, and the 1460d attempt
above shows it's not simply "more history = better." Per this log's own methodology
(only merge changes confirmed on >=2 symbols), it stays untouched pending the SOL/UNI
cross-check below.
