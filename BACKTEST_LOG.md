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
