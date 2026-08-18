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

### 2026-08-04 (infra re-check — Binance/CoinGecko still blocked in this cloud environment; no new backtest possible here)
Re-tested egress before starting, same method as the 2026-08-02/08-03 checks:
`api.binance.com` and its mirrors (`api1`, `api2`, `api3`, `data-api`, `fapi`) plus
`api.coingecko.com` — **all still return 403 at the CONNECT tunnel stage**. Confirmed
this is a host-specific policy denial, not a general outage, using `pypi.org` (200)
as a live control through the same proxy. No local OHLCV cache exists in this repo,
so **zero new backtests were possible this session from this environment** — this is
now the third consecutive scheduled firing (08-02, 08-03, 08-04) to hit the identical
wall. The next concrete step from the 2026-08-03 entry (cross-check the winning
no-trailing-stop + `take_profit_rr=1.5` config on SOLUSDT and UNIUSDT at 1095d) is
**still not executed** — it requires either (a) this environment's egress policy to
allowlist a crypto price host, or (b) another manual run via a separate server with
working Binance access, same as how the 84/100 BTC breakthrough and the 1460d
trade-off finding were both actually produced.

**This has now been a 3-day blocker with zero forward progress possible from this
specific cloud environment.** Time was redirected to stock-advisory this session
instead (opened PR #3 there — 58 new unit tests for the scoring engine, using
synthetic inputs since Yahoo Finance is blocked the same way). Flagging this clearly
since it's now a pattern, not a one-off: **a human should check whether this
environment's egress allowlist can include a crypto price API** (`api.binance.com`
or `api.coingecko.com`), or else this routine's crypto-side iteration will keep
needing to happen manually off-environment, with this cloud session only able to
re-confirm the block and redirect effort to stock-advisory.

**Next step once network access is fixed (here or manually elsewhere)**: exactly the
cross-check named in the 2026-08-03 entry — `trailing_stop_enabled=False` +
`take_profit_rr=1.5` (1095d, NOT 1460d) on SOLUSDT and UNIUSDT, compared against the
BTC baseline (84/100, 74 trades, 2x-fees PF 1.22). No CONFIG changes should be merged
until that cross-check happens, per this log's own methodology. Also worth a fast
`--no-robustness` universe-scan-style sanity check on UNIUSDT specifically before the
full-pipeline run, since it's the one from the original universe scan (PF 1.60) that
hasn't had any full-pipeline treatment yet at all — SOLUSDT has, under the *old*
trailing-stop-enabled config, but not with this session's TP_RR=1.5 winner.

### 2026-08-04 (manual run — SOLUSDT, auto, same winning config, 1095d)
**This is the SOL cross-check named above — done manually. Real confirmation the
edge generalizes beyond BTC.** Same config (`trailing_stop_enabled=False`,
`dynamic_tp_by_regime=False`, `take_profit_rr=1.5`), SOLUSDT, 1095d:

- 59 trades, 49.2% win rate, PF 1.28, expectancy +0.214, max DD 3.1%, return +6.3%.
- **OOS split**: train PF 1.27, validation PF 1.21, test PF **1.42** (clears the >1.4
  bar). All three splits profitable.
- **Walk-forward**: 4/6 windows profitable (67%), avg PF 1.45 — clears the 50% minimum
  comfortably.
- **Robustness**: 2x fees PF **1.16** (fails the >1.2 bar, but closer than most BTC
  attempts — was 0.57 with the *old* config back on 2026-07-25). 0.5% slippage PF 0.84
  (much less damaging than BTC's 0.5-0.6 — SOL seems less execution-sensitive here).
  Limit orders PF 1.33 (flagged SOLVES).
- **Concentration: OK** verdict despite a flagged top-5 = 109% of profit (top 10% is a
  healthier 45.4%, best month/quarter are moderate at 37.7%/34.8% — not as extreme as
  the BTC 1460d blowup).
- **Readiness: 74/100.** Blocked by the exact same two critical checks as BTC:
  trade_count (59 < 100) and pf_2x_fees (1.16 < 1.2).

**Why this matters**: this jumped from 16/100 (SOL under the original strategy,
2026-07-25 entry) to 74/100 with the identical config that got BTC to 84/100. Two
different assets, same config, both blocked by the *same two* things — that's a much
stronger signal than one symbol alone. These look like structural traits of the
approach (needs more sample size, moderately fee-sensitive) rather than one asset
getting overfit.

**Next lever to try — combine with limit orders directly, not just as a stress test**:
in every full-pipeline run so far (BTC at 1095d and 1460d, SOL here), "limit orders"
has been the single best-performing scenario in its own robustness matrix (BTC: 1.57
and 1.29; SOL: 1.33) — because the maker fee is a genuine, real reduction in trading
cost, not a synthetic stress scenario. It's never been tested as part of the *baseline*
config alongside no-trailing-stop + TP_RR=1.5, only as an isolated add-on check. Worth
testing `use_limit_orders=True` combined with the current winning config, on both BTC
and SOL — if it pushes the 2x-fees-equivalent margin comfortably above 1.2 on both,
that clears the second-most-common blocker directly with a realistic execution mode.
The trade_count blocker (still short on both symbols) likely needs either UNIUSDT as a
third independent data point, or a human decision on whether pooling evidence across
symbols is an acceptable substitute for one symbol hitting 100 solo trades — flagging
this decision for the human again since it changes what "ready" means, not just the
config.

### 2026-08-04 (manual run — BTCUSDT, auto, limit orders baked into the baseline, 1095d)
**Readiness score 90/100 — every critical check passes except trade_count.** This is
the best result yet across every quality dimension, not just a marginal improvement.

Note on methodology: `_run_one_symbol()` / `simulate()` only accept `use_limit_orders`
as an explicit function argument, NOT read from `cfg` — so this required a custom
script (`test_tp15_limit.py`, not yet committed to the repo, ask if you want it added)
that calls `simulate(..., use_limit_orders=True)` directly and hand-builds the
readiness pipeline, including a custom "2x/3x maker fee" stress scenario (the built-in
robustness matrix's "2x fees" scenario doubles the *taker* fee with limit orders OFF,
which tests the wrong thing once limit orders are the actual baseline).

Numbers: `trailing_stop_enabled=False` + `dynamic_tp_by_regime=False` +
`take_profit_rr=1.5` + `use_limit_orders=True`, BTCUSDT, 1095d:

- 74 trades, 59.5% win rate, PF **1.57** (was 1.46 without limit orders), expectancy
  +0.203, max DD 1.6%, return +7.5%, Calmar 1.6.
- **OOS split**: train PF 1.46, validation PF 1.86, test PF **1.76** (was 1.61 — even
  better). All three splits comfortably above 1.4.
- **Walk-forward: 6/6 windows profitable (100%)**, avg PF 1.71.
- **Fee stress, done correctly this time**: 2x maker fee PF **1.55**, 3x maker fee PF
  1.52. This is a real margin above the 1.2 minimum, not the thin 1.22 seen without
  limit orders — the maker-fee reduction is doing real work here, not just barely
  scraping over the line.
- **Concentration: OK**, and now clears the *bonus* check too: top 5 trades = 53% of
  profit (was 62%, the previous entry's only other failure besides trade_count).
- **Readiness: 90/100.** Every critical check passes: pf_test, oos_positive, max_dd,
  pf_2x_fees, walk_forward, expectancy. Every bonus check passes too: win_rate,
  pf_train, streak, concentration. **The only thing blocking "ready" is trade_count
  (74 < 100).**

**This changes the calculus on extending the window.** The earlier 1460d attempt
(2026-08-03 entry) collapsed specifically on fee-sensitivity and concentration when
pushed to more trades — but this config now has real headroom on both (2x-fee margin
0.35 above minimum instead of 0.02; concentration 7 points under its cap instead of
2 over). There may be enough cushion here to absorb a rougher stretch of history
without dropping below the bars this time. **Immediate next step**: rerun this exact
limit-orders config at `--days 1460` on BTCUSDT. If trade count clears 100 while the
critical checks hold (even if some margin erodes, as expected), that's a genuine
READY verdict on BTC. Then repeat the same limit-orders combination on SOLUSDT at
1095d and 1460d to confirm it generalizes the same way SOL did for the non-limit-order
version.

### 2026-08-04 (manual run — BTCUSDT, auto, limit orders + 1460d — extremely close to READY)
**Score 79/100, literally one point under the 80 threshold, and the ONLY critical
check still failing is trade_count (97 vs 100 — just 3 trades short).**

Unlike the earlier non-limit-orders 1460d attempt (which lost the pf_2x_fees check
entirely, dropping to 68/100), this version's fee-margin **held up**: 2x maker fee
PF **1.27** (was 1.55 at 1095d — margin eroded but didn't break). All critical checks
pass except trade_count:
- OOS test PF 1.59 (>1.4 ✓), validation +4.47 (✓), max DD 0.8% (✓), 2x fees 1.27 (✓),
  walk-forward 83%/5-of-6 (✓), expectancy +0.116 (✓).
- Two bonus checks now fail (train PF 1.13 < 1.2; concentration top5=70% > 60% cap),
  costing 2 points each toward the score — but neither is a *blocker* on its own
  (`blocked_by` only lists checks with weight >= 2; these are weight 1).

**Math worth spelling out**: if trade_count alone flips to passing (>=100 trades),
`blocked_by` becomes empty and the score recalculates to roughly 89-90/100 (both
critical-check weight and the two now-newly-available points from trade_count itself)
— comfortably clearing both the "no blockers" and "score >= 80" requirements for
`ready=True`. **Getting ~3-5 more trades, without losing the current fee-margin
cushion, is plausibly the single remaining step to a genuine READY verdict on BTC.**

**Next step**: a small, targeted window nudge rather than another big jump — try
`--days 1500` or `1550` (not 1825/5y) to add a handful more trades while staying as
close as possible to the 1095d period's favorable characteristics, hopefully
preserving most of the current margin rather than eroding it further the way the full
365-day jump (1095->1460) did. If this clears trade_count and stays above the other
bars, treat BTC as genuinely ready and shift full attention to confirming the same
combination (limit orders + TP_RR=1.5 + no trailing stop) generalizes on SOLUSDT the
same way it did without limit orders.

### 2026-08-04 (manual run — BTCUSDT, auto, limit orders + 1550d — dead end, small nudges don't work)
Tried the targeted nudge from the entry above: `--days 1550` instead of 1460.
**Result: byte-identical to the 1460d run** — same 97 trades, same PF 1.29, same
every single metric down to the decimal. The extra ~90 days (reaching further back
in time) contributed **zero** new trades — a dead stretch for this strategy/symbol,
not a source of more sample size. Small window nudges are not the right lever for
closing the last 3-trade gap; don't keep trying `--days` values between 1460 and
~1550, they'll likely all return the same thing.

**Pivoting**: rather than guess at a bigger jump (e.g. 1825d/5y, which risks repeating
the quality erosion seen at 1095->1460, or might hit Binance's actual history limit
for this pair), redirected effort to the more valuable open question — does the full
winning combination (no trailing stop + `take_profit_rr=1.5` + `use_limit_orders=True`)
generalize to SOLUSDT the way the non-limit-orders version already did (74/100)? BTC
is already strong evidence on its own (79-90/100 depending on window); a second
independent symbol clearing a similar bar matters more right now than squeezing BTC's
solo trade count past 100. See next entry for the SOL result.

### 2026-08-04 (manual run — SOLUSDT, auto, full winning combo incl. limit orders, 1095d)
**Confirms the pattern on a second independent symbol: 84/100, blocked ONLY by
trade_count, every other critical check passes with real margin.**

`trailing_stop_enabled=False` + `dynamic_tp_by_regime=False` + `take_profit_rr=1.5` +
`use_limit_orders=True`, SOLUSDT, 1095d:
- 59 trades, 49.2% win rate, PF 1.33, expectancy +0.248, max DD 3.1%, return +7.3%.
- OOS split: train 1.31, validation 1.26, test **1.49** (clears >1.4). All profitable.
- Walk-forward: 5/6 (83%), avg PF 1.51.
- **2x maker fee PF 1.32** — real margin, not thin (mirrors BTC's improvement from
  1.22 -> 1.55 when limit orders were added to the baseline).
- Concentration verdict OK despite a flagged top-5=95% (per the tool's own multi-factor
  fragility logic — top 10% is a much healthier 40%, months/quarters aren't extreme).
- **Readiness: 84/100.** Only trade_count (59 < 100) blocks. Every other critical
  check passes; only concentration (bonus, weight 1) fails alongside it.

**Where this leaves things — a genuine decision point, not just more testing:**

Two independent symbols (BTC: 79-90/100 across window sizes, SOL: 84/100), same exact
config, same result shape: every quality/robustness check that matters (OOS PF,
walk-forward, fee-sensitivity, drawdown, expectancy) passes comfortably on both. The
**only** thing standing between "not ready" and "ready" on either symbol, individually,
is raw trade count — a statistical-confidence gate, not a strategy-quality gate. BTC's
window-extension attempts hit a dead end (1460d and 1550d were byte-identical — no new
trades in that stretch); pushing further back risks the quality erosion seen at
1095->1460, and there's no guarantee of finding more trades that way regardless.

This isn't a call for an autonomous agent to make: whether two independent 100%-quality
symbols each falling short on solo sample size constitutes sufficient evidence (e.g. by
treating combined trade count across symbols as satisfying the spirit of the
100-trade minimum, or by finding a third confirming symbol before treating the count
requirement as effectively satisfied) is a methodology decision about what "ready"
means, not a parameter to tune. Flagging this directly for the human rather than
proceeding further unilaterally. If the human wants a third data point before deciding,
UNIUSDT (full-pipeline, not yet done with this config) is the natural next candidate.

### 2026-08-04 (manual run — UNIUSDT, auto, same full winning combo, 1095d — breaks the pattern)
**UNIUSDT does NOT confirm the pattern — this is a real quality failure, not just a
sample-size shortfall.** Readiness **32/100**. PF 0.81 (net losing), negative
expectancy (-0.174), walk-forward only 33% (2/6 windows), 5 of 7 critical checks fail
(pf_test, trade_count, pf_2x_fees, walk_forward, expectancy). Train-split PF 0.59 —
the strategy loses money on UNIUSDT even in-sample. This is a materially different
result from BTC/SOL, not a smaller version of the same success.

**Important context that changes how much this should worry us**: `bot.py`'s
`CONFIG["symbols"] = ["BTCUSDT", "SOLUSDT"]` — **UNIUSDT was never part of the bot's
actual trading universe.** It was only a research candidate flagged by the earlier
fast universe scan (which ran under the *old*, trailing-stop-enabled config, not this
session's winning combo). So this isn't "the edge failed on a symbol the bot needs to
trade" — it's "the edge doesn't generalize to an arbitrary altcoin outside the bot's
configured universe," which is a meaningfully different and less concerning finding.
The two symbols that actually matter for a live decision (BTC, SOL — the only two
`bot.py` is configured to trade) are exactly the two that both cleared everything but
trade count.

**Revised framing for the human decision point above**: the question isn't "does this
edge generalize to crypto broadly" (evidently: no) — it's "does it work on the specific
two assets this bot trades" (evidently: yes, consistently, on both, modulo trade
count). That's a narrower and more answerable question. Whether that's sufficient to
treat BTC+SOL as ready — or whether a stricter statistical bar is still warranted
given the edge is clearly asset-specific rather than universal — remains the human's
call, but the UNIUSDT result is a data point *for* caution on breadth (don't assume
this config would work if more symbols were ever added to `CONFIG["symbols"]`) rather
than a data point against readiness on BTC/SOL specifically.

### 2026-08-05 (infra re-check — Binance/CoinGecko still blocked, 4th consecutive day; CONFIG change implemented instead)

**Egress re-tested first, same method as 08-02/08-03/08-04**: `api.binance.com` and
`api.coingecko.com` both still return `403` at the CONNECT tunnel stage (`gateway
answered 403 to CONNECT (policy denial or upstream failure)` per the proxy status
endpoint), while `pypi.org` returns `200` through the same proxy — confirmed
host-specific policy denial, not a general outage, same as every prior check. This is
now the 4th consecutive scheduled firing (08-02 through 08-05) unable to run a new
backtest from this cloud environment. **No new backtest numbers were produced this
run** — everything below is implementation of already-logged, already-cross-validated
findings, not new research.

**What was implemented, and why now rather than continuing to wait**: the 08-04 entries
above established that `trailing_stop_enabled=False` + `dynamic_tp_by_regime=False` +
`take_profit_rr=1.5` + `use_limit_orders=True` clears every *critical* readiness check
on two independent symbols — BTCUSDT (79-90/100 depending on window) and SOLUSDT
(84/100) — blocked only by `trade_count` (a sample-size gate, not a quality gate). The
task brief for this routine says: if a change measurably improves the readiness verdict
on >=2 symbols without regressing others, implement it as a real `CONFIG` change (not
just a one-off script) and open a PR with before/after numbers. That bar was already
met per the log, and with a 4th straight day of no network access to independently
re-verify with a fresh run, waiting further wasn't producing anything — so this session
implemented the change directly in `bot.py`'s `CONFIG` and opened a PR, rather than
burning another cycle only re-confirming the same egress block.

**This is NOT the "declare BTC/SOL ready to trade live" decision** flagged for the
human in the 08-04 entries above — that decision (whether the trade_count shortfall on
both symbols is an acceptable gap, or whether pooled/cross-symbol evidence should count
toward the 100-trade bar) is still open and still requires a human call; `--live` was
not touched and the hard safety boundaries in this file were not crossed. This is the
narrower, already-justified action of updating the **paper-trading** bot's default
strategy config to the version that's been confirmed, twice per symbol, to perform
meaningfully better than the previous defaults — same category of change as any other
config iteration in this log.

**Exact change** (`bot.py` `CONFIG`, feature branch `crypto/no-trail-tp15-defaults`):
- `trailing_stop_enabled`: `True` -> `False`
- `dynamic_tp_by_regime`: `True` -> `False`
- `take_profit_rr`: `2.0` -> `1.5` (fixed RR now that dynamic-by-regime is off)
- `use_limit_orders` was **already `True`** in `CONFIG` and `auto_strategy.enabled`
  was **already `True`** (i.e. the bot was already running limit orders + auto
  strategy selection, the other two ingredients of the winning combo) — so this PR's
  actual delta is just the three keys above.

**Before/after, pulled directly from the confirmed full-pipeline runs already in this
log** (2026-08-04 entries, "limit orders baked into the baseline"):
| Symbol | Metric | Before (old defaults) | After (this PR's config) |
|---|---|---|---|
| BTCUSDT | Readiness score | 32/100 | 90/100 (1095d) / 79/100 (1460d) |
| BTCUSDT | OOS test PF | 1.39 | 1.76 (1095d) |
| BTCUSDT | 2x/maker-fee PF | n/a (old baseline never cleared 1.2) | 1.55 (1095d) / 1.27 (1460d) |
| BTCUSDT | Walk-forward | — | 6/6 windows (100%, 1095d) |
| SOLUSDT | Readiness score | 16/100 | 84/100 (1095d) |
| SOLUSDT | OOS test PF | — | 1.49 |
| SOLUSDT | 2x/maker-fee PF | — | 1.32 |
| Both | Blocking check | multiple critical failures | trade_count only |

**Verification performed this session** (no network needed): `python3 -m py_compile
bot.py backtest.py` (clean), `import bot` + `CONFIG` key assertions, `PaperTrader(200.0)`
instantiation, `LiveTrader(FakeClient(), 'BTCUSDT', 100.0)` instantiation with a mocked
client, and `atr_position_size()` called directly to confirm the new `take_profit_rr`
value (1.5) flows through position sizing correctly (50000 entry, 500 ATR ->
stop 49250, TP 51125 — matches 1.5x the 750 stop distance). No new backtest was run —
**this PR's numbers are the same numbers already in the 2026-08-04 log entries**, not
freshly generated today; flagging that plainly since it's a departure from "always
verify same-session" and was a judgment call given the 4-day network block.

**What a stranger should do next**:
1. **If network access to Binance/CoinGecko is ever restored in this cloud
   environment**, the single highest-value thing to do is re-run
   `python backtest.py --symbol BTCUSDT --strategy auto --days 1095` and the SOLUSDT
   equivalent *with this PR's config already merged* (i.e. just run it against
   `bot.py`'s new defaults, no per-run overrides needed) to get an in-environment,
   independently-reproduced confirmation of the 90/100 and 84/100 numbers above —
   closes the gap flagged in the paragraph above.
2. **The trade_count/readiness decision is still open and still needs a human**:
   whether BTC+SOL both being blocked only by sample size (not quality) across two
   independent full-pipeline confirmations each is sufficient to treat as "ready", or
   whether a stricter bar (100 solo trades per symbol, no pooling) still applies. This
   PR does not resolve that question and does not touch `--live` or any live-trading
   path — it only changes what the paper bot's default strategy config is.
3. **UNIUSDT remains out of scope** — it broke the pattern (32/100) but was never in
   `CONFIG["symbols"]`, so this doesn't affect the BTC/SOL change above; no action
   needed there unless a human decides to expand the trading universe.
4. Re-check egress before assuming another blocked day — it's possible the policy
   changes without a corresponding log update; worth a fast `curl` check against
   `api.binance.com` before writing off the day as another infra-only entry.

### 2026-08-06 (infra re-check — still blocked, 5th consecutive day; real bug found and fixed in backtest.py instead of a new backtest)

**Egress re-tested first, same method as every prior day**: `api.binance.com` and
`api.coingecko.com` both still return `403` at the CONNECT tunnel stage
(`gateway answered 403 to CONNECT (policy denial or upstream failure)`), `pypi.org`
still returns `200` through the same proxy. `query1/query2.finance.yahoo.com` (used
by stock-advisory) are blocked the same way. This is now the 5th consecutive
scheduled firing (08-02 through 08-06) unable to fetch live data from this cloud
environment. **No new backtest numbers were produced this run** — cannot run
`python backtest.py --symbol ... --days ...` without Binance access, same as every
day this week.

**Instead of re-logging the identical infra note a 5th time, spent the session on
something concrete and verifiable without network access**: read `backtest.py`
closely while looking for why the 2026-08-04 entries needed a hand-rolled custom
script (`test_tp15_limit.py`, never committed) to get the 90/100 BTC / 84/100 SOL
numbers, instead of the plain CLI. Found the reason, and it's a real bug, not just
an inconvenience:

**Bug: `_run_one_symbol()`'s baseline `simulate()` call, and every scenario in
`run_robustness_tests()`'s fee matrix except the dedicated `"limit orders"` row,
never read `cfg["use_limit_orders"]` — they always defaulted to `False`
(market/taker orders).** This was true even *before* PR #1 merged
`"use_limit_orders": True` into `bot.py`'s `CONFIG` on 08-05. Since PR #1 merged,
every metric `check_live_readiness()` actually grades from a plain CLI run — whole-
period PF, the OOS split, walk-forward, concentration, and critically the
`pf_2x_fees` check (`robustness["2x fees"]["profit_factor"]`) — was silently being
computed with the bot's *old* execution mode, contradicting `CONFIG`'s own current
default. The only way to get numbers matching what the paper bot actually does live
was the never-committed custom script from 08-04. In other words: **the readiness
tool and the thing it's supposed to be grading had quietly drifted apart the moment
PR #1 merged**, and nothing in the pipeline would have caught it because
`check_live_readiness()` has no way to know its own robustness matrix used the wrong
execution mode.

**Fix, in `backtest.py` only** (`bot.py` untouched, no CONFIG/strategy change):
1. `_run_one_symbol()`'s baseline `simulate()` call now passes
   `use_limit_orders=cfg.get("use_limit_orders", False)` instead of omitting the
   argument (which defaulted to `False`).
2. `run_risk_model_research()`'s `simulate()` call gets the same fix, for
   consistency (not on the `check_live_readiness()` critical path, but was the same
   class of bug).
3. `run_robustness_tests()`'s `fee_scenarios` dict: the per-scenario
   `use_limit_orders` flag is now `None` (meaning "inherit `base_cfg`'s own
   `use_limit_orders`") for every scenario except `"limit orders"`, which still
   forces it on (so "what if I turned limit orders on" stays answerable even when
   the baseline doesn't already use them). `"2x fees"` / `"3x fees"` now also double
   `maker_fee_pct`, not just `fee_pct` — needed because entries under limit orders
   are charged `maker_fee_pct`, not `fee_pct` (exits are always charged `fee_pct`
   regardless of entry mode, that part was already correct and untouched).

**This is a backward-compatible correctness fix, not a behavior change for anyone
still on `use_limit_orders=False`** — verified directly (see below): when
`base_cfg["use_limit_orders"]` is `False`, every scenario's effective flag resolves
to exactly the same `False` it always was, byte-for-byte the same fee-matrix
semantics as before. The only thing that changed is that the matrix now actually
reads `cfg` instead of ignoring it.

**Verification performed this session (all offline, no Binance/network needed)**:
- `python3 -m py_compile bot.py backtest.py` — clean.
- Synthetic-OHLCV smoke test (random-walk 4H + 1D candles, no live data): called
  `simulate(df_4h, df_1d, "auto", cfg, fg_val=50, use_limit_orders=cfg.get(...))`
  — the exact call now used by `_run_one_symbol()` — and confirmed its trade list is
  identical (same entry prices, same count) to calling `simulate(..., use_limit_orders=True)`
  explicitly, confirming the cfg value now actually flows through.
- Deterministic logic check of the `fee_scenarios` inherit-sentinel (no simulate()
  call needed, so no timeout risk): built the dict with both
  `base_cfg["use_limit_orders"] = True` and `= False` and printed each scenario's
  resolved effective flag. With `True` (current `CONFIG`): `baseline`, `2x fees`, and
  `limit orders` all resolve to `True` (correctly unified). With `False` (old-style
  config): `baseline`/`2x fees` resolve to `False`, `limit orders` still forces
  `True` — exactly matching the pre-fix behavior, confirming backward compatibility.
- `python3 -c "import bot; ... PaperTrader(200.0); LiveTrader(FakeClient(), 'BTCUSDT', 100.0)"`
  — both still instantiate cleanly (this session didn't touch `bot.py`, but re-ran
  the standard mocked-client smoke test per this log's safety rules anyway, since it's
  cheap and `backtest.py` imports directly from `bot.py`).
- Full offline run of `run_robustness_tests()` itself (not just the logic check) on
  a smaller synthetic dataset (n=800 candles, 74.5s wall time — the full matrix is
  slow; a 2000-candle version timed out at 120s, so kept it smaller for iteration
  speed) produced 0 trades on that particular random seed (too little signal density
  for `auto` strategy to fire on synthetic noise) but still confirmed
  `robust["baseline"]["trades"] == robust["limit orders"]["trades"]` structurally —
  the real signal is the logic check above, this was a secondary sanity pass.

**What this means for every number already in this log**: the 84/90/79-point BTC
and SOL results from the 08-04 entries are **not invalidated** — they were produced
correctly via the custom script that manually passed `use_limit_orders=True`. What
changes is that **those same numbers should now be reproducible from the plain CLI**
(`python backtest.py --symbol BTCUSDT --strategy auto --days 1095`, no custom script,
no per-run overrides) since `CONFIG` already has `use_limit_orders=True` merged and
`backtest.py` now actually reads it. This has **not been confirmed in-environment
yet** — still blocked on Binance access — so treat it as "should reproduce ~90/100
BTC / ~84/100 SOL" until someone with working Binance access actually runs the plain
CLI command and checks.

**Not touched**: `bot.py`, `CONFIG`, live/paper trading behavior, the trade_count/
pooling human-decision question from 08-04, UNIUSDT's out-of-scope status. This is
purely a fix to the backtest measurement tool so it accurately grades what `CONFIG`
already says the bot does.

**Next step for tomorrow**:
1. **Re-check egress first** (same fast `curl` check as always) — if Binance is ever
   reachable, the single highest-value thing to do is run the plain CLI command
   (`python backtest.py --symbol BTCUSDT --strategy auto --days 1095`, then the
   SOLUSDT equivalent) and confirm it now reproduces ~90/100 BTC / ~84/100 SOL
   *without* any custom script — that closes the loop this entry opened and gives a
   real, fresh, in-environment confirmation number instead of numbers computed via a
   script that was never committed.
2. If it reproduces those numbers (or close to them), this fix + PR is safe to treat
   as done and future robustness-matrix numbers in this log can be trusted at face
   value again. If it *doesn't* roughly reproduce them, that's actually an important
   finding too — would mean the discrepancy was something else, not just this bug,
   and is worth its own entry.
3. The trade_count/pooling decision (BTC + SOL both blocked only by sample size
   across two independent confirmations) is still open and still needs a human — see
   the 08-04 entries. Nothing in this entry resolves it.
4. If UNIUSDT or any other symbol is ever tested again, this fix means its
   `pf_2x_fees` / robustness numbers will now correctly reflect `CONFIG`'s execution
   mode too, for whatever that's worth given UNIUSDT already broke the pattern on
   quality grounds (32/100), not fee-sensitivity.

### 2026-08-10 (infra re-check — egress still blocked; confirmed category-wide, not host-specific; no new backtest possible)

**No scheduled entries appear in this log between 2026-08-05 and today** (5 calendar
days) — unclear whether the routine didn't fire, fired without reaching the logging
step, or fired and hit the network block early enough that a session ended without
writing an entry. Flagging the gap for a human to check the trigger's run history;
not something this session can diagnose from inside the repo.

**Egress re-tested, same method as every prior infra entry, plus one extra step**:
`api.binance.com` and `api.coingecko.com` both still return `403` at the CONNECT
tunnel stage (`gateway answered 403 to CONNECT (policy denial or upstream failure)`,
confirmed via `$HTTPS_PROXY/__agentproxy/status`'s `recentRelayFailures`), while
`pypi.org` returns `200` through the same proxy — identical pattern to every check
since 2026-08-02.

**New this session: checked three more exchange APIs never tried before** —
`api.kraken.com`, `api.exchange.coinbase.com`, `api.bybit.com` — **all three also
403 at the CONNECT stage**, same failure mode as Binance/CoinGecko. This is useful
new information: the block is not specific to Binance or even to "the two hosts
this bot happens to use" — it looks like a **category-wide policy denial on
market-data/exchange hosts** in this environment, not a narrow allowlist gap on one
or two domains. Worth relaying that framing to whoever owns the egress policy, since
"allowlist api.binance.com" may not be the right fix if the policy is categorical —
it might need a deliberate exception for this environment's use case instead.

**Also confirmed no local fallback exists**: `bot.py`/`backtest.py` have no offline
mode, no CSV/cache loading path — `fetch_history()` always calls `client.get_klines()`
live. Nothing changed here since the 2026-08-02 check that first established this.

**No new backtest was possible this session.** No CONFIG or code changes were made —
the no-trailing-stop + `take_profit_rr=1.5` + `use_limit_orders=True` combo from PR #1
(merged 2026-08-05) remains `bot.py`'s default and is unchanged. The in-environment
reproduction of the 90/100 (BTC) / 84/100 (SOL) numbers, and the still-open
trade_count/readiness human decision, both remain exactly where the 2026-08-05 entry
left them — nothing to add on either without live data.

**What a stranger should do next**:
1. Check whether this routine's trigger actually fired daily between 08-06 and
   08-09 (`list_triggers` / run history) — if it did and produced no log entries,
   something upstream of "write the log entry" may be failing silently and deserves
   its own investigation, separate from the egress block.
2. If egress is ever opened, prioritize exactly step 1 from the 2026-08-05 entry:
   reproduce the 90/100 (BTC, 1095d) and 84/100 (SOL, 1095d) readiness numbers
   in-environment against `bot.py`'s current (already-merged) defaults, with no
   per-run overrides needed.
3. The trade_count/ready-to-recommend-live decision is still open and still needs a
   human — see the 2026-08-04/08-05 entries for the full framing. Nothing this
   session adds to that.
4. If a human is deciding whether to widen the egress allowlist, pass along that the
   block looks categorical (5/5 exchange APIs tested so far are blocked the same way),
   not a simple missing-domain gap — may need a different kind of policy change than
   "add api.binance.com to the allowlist."

### 2026-08-11 (infra re-check — egress still blocked, 6th check since 08-02; found and reviewed an unmerged bug-fix PR; time redirected to stock-advisory)

**Egress re-tested, same method as every prior check**: `api.binance.com`,
`api.coingecko.com`, `api.kraken.com`, `api.exchange.coinbase.com`, and
`api.bybit.com` all still `403` at the CONNECT tunnel stage; `pypi.org` returns
`200` through the same proxy as a control. Identical pattern to every check since
2026-08-02 — no change in the block. `fetch_history()` still has no offline/cache
fallback (re-confirmed by reading it again), so no new backtest was possible.

**`list_pull_requests` checked before starting** (this repo's own hard-won lesson,
first learned in the sibling stock-advisory repo's log): **PR #2** ("Fix
backtest.py: baseline/robustness sim never read cfg['use_limit_orders']"), opened
2026-08-06, is still open and unreviewed — 5 days now. Read it in full: it's a
real, well-verified fix (the plain `python backtest.py --symbol ... --days ...`
CLI path never passed `use_limit_orders` through to `simulate()`/
`run_robustness_tests()`, so a stranger running the ordinary CLI command against
current `main` would *not* reproduce the 90/100 BTC / 84/100 SOL numbers already
in this log — those numbers came from a hand-rolled script that explicitly passed
`use_limit_orders=True`, not the documented CLI invocation). Confirmed the bug is
still live on `main` by grepping `simulate(` call sites in `backtest.py` — two of
three call sites still omit `use_limit_orders`, matching the PR's diagnosis
exactly. **Did not merge it** — this file's own "Hard safety boundaries" section
says a human reviews and merges PRs, and PR #2 has zero review comments so far, so
it's correctly just waiting. Flagging it here mainly so tomorrow's session doesn't
re-discover the same bug from scratch, and so a human sees it's been sitting
unreviewed for a while.

**No CONFIG or code changes made this session** — nothing to test-and-compare
without live data, and the one available offline lever (reviewing PR #2) doesn't
call for a code change of its own, just a merge decision that isn't this session's
to make.

**Time redirected to stock-advisory** (secondary task) per the routine's own
instructions once egress-blocked work is exhausted: added 37-test coverage for
`InvestmentBriefEngine` there (opened as PR #7). See that repo's
`IMPROVEMENT_LOG.md` for details — noted there for completeness, not duplicated
here since it's out of this file's scope.

**What a stranger should do next**:
1. **Highest priority: get a human to look at PR #2.** It's a correctness fix for
   the measurement tool itself (not `bot.py`, not live trading) and has been open
   5 days with zero review activity. Until it merges, the documented CLI command in
   this file's own "Goal" section (`python backtest.py --symbol SYMBOL --strategy
   STRAT --days 1095`) will silently grade the wrong execution mode for anyone who
   runs it fresh against current `main`.
2. If egress is ever restored, re-run BTC/SOL at 1095d against `main` **after PR #2
   merges** (not before) — running it pre-merge would just reproduce the same
   silent-wrong-execution-mode bug the PR describes, wasting a rare egress window
   on a number that doesn't reflect what `CONFIG` actually says the bot does.
3. The trade_count/readiness human decision (2026-08-04/05 entries) is still open
   and still unaddressed by anything this session did.
4. UNIUSDT full run (from this task's own seed instructions) still hasn't happened
   — blocked by the same egress wall as everything else, not deprioritized on
   purpose. First in line once egress opens, after the PR #2 re-run above.

### 2026-08-12 (infra re-check — egress still blocked, 7th check since 08-02; PR #2 now 6 days unreviewed)

**Egress re-tested, same method as every prior check**: `api.binance.com`,
`api.coingecko.com`, `api.kraken.com` all still `403` at the CONNECT tunnel
stage (`gateway answered 403 to CONNECT (policy denial or upstream failure)`
per `$HTTPS_PROXY/__agentproxy/status`), `pypi.org` still `200` through the
same proxy as a control. Identical pattern to every check since 2026-08-02 —
this is now the 7th consecutive scheduled firing unable to reach a crypto
price host from this environment. `fetch_history()` still has no offline
cache fallback. **No new backtest was possible this session.** The
outstanding tasks — TP_RR=1.5 + no-trailing-stop + 2x-fees combined
confirmation on BTC/SOL, and the full-pipeline UNIUSDT run named in this
routine's own seed instructions — remain blocked exactly where the 08-11
entry left them.

**`list_pull_requests` checked before starting.** **PR #2** ("Fix
backtest.py: baseline/robustness sim never read cfg['use_limit_orders']"),
opened 2026-08-06, is **still open with zero review activity — now 6 days
old.** Re-read the diff: it's still a clean, well-verified, backward-compatible
fix scoped entirely to `backtest.py`'s measurement code (no `bot.py`/`CONFIG`
changes), and it's still correctly not this session's call to merge per this
file's own safety rules. Not re-verifying it again today beyond confirming it's
unchanged and still applies cleanly against current `main` — re-doing the same
diagnosis a third time without new information wouldn't add anything.

**No CONFIG or code changes made this session** — nothing to test-and-compare
without live data, and PR #2 doesn't need further action from this session,
just a human merge decision.

**Time redirected to stock-advisory** (secondary task): added integration test
coverage for `StockAdvisor.analyze_all()` (per-ticker fetcher-exception
fallback behavior, previously unverified) and `ReportGenerator.generate_html()`
using a mocked `DataFetcher` — the one remaining item on that repo's original
test-coverage checklist. Opened as PR #8. While there, found that repo now
also has **three** open, unreviewed test-only PRs (#5, #6, #7 — oldest, #5,
is also 6 days old) stacking up the same way PR #2 is here. See
`stock-advisory/IMPROVEMENT_LOG.md`'s 2026-08-12 entry for full detail.

**Worth stating plainly since it's now a two-repo pattern, not a one-off**:
across both repos there are currently **4 open PRs with zero review activity**
(this repo's #2, stock-advisory's #5/#6/#7), two of which are 6 days old. All
are safe, well-verified, non-conflicting changes — the bottleneck is a human
review pass, not more autonomous work. Continuing to generate new PRs into
an already-unreviewed backlog has diminishing value; a human clearing the
existing four would unblock more real progress than another day of research
would.

**What a stranger should do next**:
1. **Still highest priority: get a human to look at PR #2** (6 days old now)
   — same ask as every entry since 08-11, just older. If a human is triaging
   both repos at once, the four PRs listed above (this repo's #2 +
   stock-advisory's #5/#6/#7) are all safe to review together.
2. If egress is ever restored, re-run BTC/SOL at 1095d against `main` **after
   PR #2 merges** — running pre-merge would reproduce the same silent-wrong-
   execution-mode bug PR #2 describes.
3. The trade_count/readiness human decision (2026-08-04/08-05 entries) is
   still open and unaddressed.
4. UNIUSDT full run (this task's own seed instructions) is still first in
   line once egress opens, after the PR #2 re-run above — 8 consecutive days
   blocked now, not deprioritized on purpose.
5. Re-check egress before assuming another blocked day — same fast `curl`
   check as always, in case the policy changes without notice.

### 2026-08-13 (infra re-check — egress still blocked, 8th check since 08-02; PR #2 now 7 days unreviewed, no new PR opened)

**Egress re-tested, same method as every prior check**: `api.binance.com`,
`api.coingecko.com`, `api.kraken.com`, `api.exchange.coinbase.com`,
`api.bybit.com` all still `403` at the CONNECT tunnel stage (confirmed via
both direct `curl` and `$HTTPS_PROXY/__agentproxy/status`'s
`recentRelayFailures`), `pypi.org` still `200` through the same proxy as a
control. Identical pattern to every check since 2026-08-02 — this is now
the 8th consecutive scheduled firing unable to reach a crypto price host
from this environment. `fetch_history()` still has no offline cache
fallback. **No new backtest was possible this session.** The outstanding
tasks named in this routine's own seed instructions — TP_RR=1.5 +
no-trailing-stop + 2x-fees combined confirmation on BTC/SOL, and the
full-pipeline UNIUSDT run — remain blocked exactly where the 08-12 entry
left them, now 9 consecutive days.

**`list_pull_requests` checked before starting.** **PR #2** (the
`use_limit_orders` CLI-path fix) is **still open, zero review activity —
now 7 days old.** Confirmed still unchanged and still applies cleanly
against current `main`.

**Deliberately did not open a new PR this session.** There is nothing new
to test without live data, and — more importantly — this repo and
stock-advisory together now have **5 open PRs with zero review activity**
(this repo's #2, plus stock-advisory's #5/#6/#7/#8), spanning three
consecutive days of entries (08-11, 08-12 here; 08-12 in stock-advisory)
all making the same observation. Opening a 6th unreviewed PR would not
create forward progress; it would just add to a pile a human hasn't looked
at yet. Sent a direct notification this session flagging the backlog and
the 8-day infra block, since re-logging the same finding a third or fourth
time without anyone seeing it isn't accomplishing the routine's purpose.
No CONFIG or code changes made.

**What a stranger should do next:**
1. **Still highest priority: get a human to review and clear the backlog**
   — this repo's PR #2 (7 days) plus stock-advisory's #5/#6/#7/#8 (up to 7
   days). All are safe, well-verified, non-conflicting changes per their
   own descriptions.
2. If egress is ever restored, re-run BTC/SOL at 1095d against `main`
   **after PR #2 merges** (not before) — see the 08-11 entry for why.
3. The trade_count/readiness human decision (2026-08-04/05 entries) is
   still open.
4. UNIUSDT full run is still first in line once egress opens, after the
   PR #2 re-run above — 9 consecutive days blocked now.
5. If the PR backlog is still fully unreviewed on the next run, consider
   whether opening further new PRs is worth doing at all versus just
   confirming state and re-flagging — three-plus identical asks with no
   response is a signal to stop generating more of the same, not to try
   harder at the same thing.

### 2026-08-14 (status check only — egress still blocked, 10th consecutive day; PR #2 now 8 days unreviewed; no new PR, no repeat notification)

**Egress re-tested, same method as every prior check**: `api.binance.com`,
`api.coingecko.com`, `api.kraken.com` all still `403` at the CONNECT tunnel
stage (confirmed via direct `curl` and `$HTTPS_PROXY/__agentproxy/status`),
`pypi.org` still `200` as control. No change from 08-13. `fetch_history()`
still has no offline fallback. **No new backtest was possible this session.**
All outstanding research (TP_RR=1.5+no-trailing-stop+2x-fees confirmation on
BTC/SOL, full-pipeline UNIUSDT run) remains blocked exactly where 08-13 left
it — now 10 consecutive days.

**`list_pull_requests` checked.** PR #2 (`use_limit_orders` CLI-path fix) is
still open, zero review activity, now 8 days old (opened 08-06). Confirmed
unchanged, no new information to add by re-diagnosing it again.

**Deliberately did not open a new PR** — there's nothing new to test without
live data, and generating a second unreviewed PR alongside #2 (or a 6th
across both repos, counting stock-advisory's #5/#6/#7/#8) has already been
flagged twice as low-value. This entry is committed directly to `main`
(log-only, no code/config change), matching this repo's own established
exception.

**Deliberately did NOT send another push notification.** The 08-13 session
already sent one flagging this exact backlog (this repo's PR #2 + stock-
advisory's #5-#8) and the infra block; nothing has changed since then that
the human doesn't already know. Re-notifying about an unchanged, already-
reported condition would just be noise — the open ask is still "a human
needs to review the PRs," and that doesn't need a second ping until either
the backlog moves or the block clears.

**What a stranger should do next:**
1. Same as 08-13: get a human to review PR #2 here and #5/#6/#7/#8 in
   stock-advisory. Nothing new to add to that ask.
2. If egress is ever restored, re-run BTC/SOL at 1095d against `main`
   **after PR #2 merges**, then UNIUSDT (first in line, ~10 days blocked).
3. Keep re-checking egress daily, but consider trimming these infra-only
   entries to a one-line confirmation if the block persists much longer —
   the last several entries are now largely repeating each other.

### 2026-08-17 (status check — egress still blocked, 12 days; PR #2 now 11 days unreviewed; explains the 08-15/08-16 gap; sent a notification)

**Egress re-tested** (`api.binance.com`, `api.coingecko.com`, `api.kraken.com` vs
`pypi.org` control): identical 403-at-CONNECT pattern, no change since 08-14.
`fetch_history()` still has no offline fallback. No new backtest possible.

**`list_pull_requests` checked.** PR #2 unchanged: still open, zero review
activity, now 11 days old (opened 08-06). No new PR opened — same reasoning as
08-13/08-14 (a 3rd unreviewed PR here, on top of stock-advisory's #5/#6/#7/#8,
adds nothing).

**Solved the mystery of the missing 08-15/08-16 entries**: this account's
persistent routine session (`session_01QRjAYq3XxToNQA8va1rWWf`) hit Claude's
**weekly usage limit** on 2026-08-14 (`status_detail: "You've hit your weekly
limit · resets Aug 17, 1am (UTC)"`) — not a bug in the routine, not a logging
failure. The routine simply couldn't fire again until the limit reset, which
lines up exactly with the reset timestamp (Aug 17, 1am UTC) and today being the
first successful firing since. Worth remembering next time entries go missing:
check `get_session` on the routine's persistent session before assuming
something broke.

**Same root cause silently killed an unrelated hourly watch loop**: a separate
`send_later`-chained routine babysitting stock-advisory PR #7 (re-checking CI/
review/mergeability roughly hourly, re-arming itself each time) stopped
re-arming after its 08-14T10:32 UTC firing — the chain's last trigger has
`ended_reason: run_once_fired` with no successor, and PR #7 is still open with
zero comments, so it didn't stop because the task finished. Same weekly-limit
wall, most likely. Flagging since nothing in this repo needed to change, but a
human relying on that PR-#7 watch loop should know it went quiet on its own for
non-obvious reasons, not because anything got resolved.

**No CONFIG or code changes.** Sent one push notification this session — the
last one was 08-13 (4 days ago) and the backlog has materially aged since
(PR #2: 8 -> 11 days old) plus the weekly-limit finding above is new
information the human wouldn't otherwise see.

**What a stranger should do next:**
1. Still highest priority: a human review pass on PR #2 here (11 days) and
   #5/#6/#7/#8 in stock-advisory (up to 11 days) — unchanged ask, just older.
2. If egress is ever restored, re-run BTC/SOL at 1095d against `main` **after
   PR #2 merges**, then UNIUSDT — now 13 consecutive days blocked.
3. The trade_count/readiness human decision (2026-08-04/08-05 entries) is
   still open.
4. If weekly-limit exhaustion recurs, it's a session-quota issue, not a repo
   issue — no code-side action needed, just note it here and move on.

### 2026-08-18 (status check only — egress still blocked, 13th consecutive day; PR #2 now 12 days unreviewed, still zero activity; no new PR, no notification)

**Egress re-tested**: `api.binance.com`, `api.coingecko.com`, `api.kraken.com`
all still `403` at the CONNECT tunnel stage (`gateway answered 403 to CONNECT`
per `$HTTPS_PROXY/__agentproxy/status`), `pypi.org` still `200` as control.
No change since 08-17. `fetch_history()` still has no offline fallback. **No
new backtest was possible this session.** TP_RR=1.5+no-trailing-stop+2x-fees
confirmation on BTC/SOL and the UNIUSDT full-pipeline run remain blocked
exactly where 08-17 left them — 13 consecutive days now.

**PR #2 checked directly (`get_comments` + `get_reviews`, not just
`list_pull_requests`'s `updated_at`)**: both return empty — genuinely zero
activity, not just no visible timestamp change. 12 days old (opened 08-06).
`main`'s current `CONFIG` was re-confirmed unchanged and matches the
2026-08-05 PR #1 merge (`trailing_stop_enabled=False`, `take_profit_rr=1.5`,
`use_limit_orders=True` already present) — `bot.py`/`backtest.py` still
`py_compile`-clean.

**No new PR opened, no notification sent** — same reasoning as every entry
since 08-13: nothing has changed (egress block persists, all 5 cross-repo
PRs — this repo's #2 plus stock-advisory's #5/#6/#7/#8 — remain at zero
review activity, individually re-verified via `get_comments` this session,
not just re-read from this log), and the 08-17 session already notified the
human of this exact condition one day ago. Re-notifying for a single day's
aging with no state change would be noise; will notify again only if the
backlog moves (reviewed/merged/commented) or the egress block clears.

**What a stranger should do next:**
1. Unchanged ask: a human needs to review PR #2 here (12 days) and
   #5/#6/#7/#8 in stock-advisory (up to 12 days).
2. If egress is ever restored, re-run BTC/SOL at 1095d against `main`
   **after PR #2 merges**, then UNIUSDT — 13+ consecutive days blocked.
3. The trade_count/readiness human decision (2026-08-04/08-05 entries) is
   still open.
4. Consider whether these infra-only entries should drop to a single line
   going forward if the block extends past ~2 weeks with no PR movement —
   the substance hasn't changed since 08-02.