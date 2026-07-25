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
   winners into scratches. NOT YET CONFIRMED under the 2x-fees stress scenario
   combined — that specific combination (no trail stop AND 2x fees together) was
   queued as the next test (`test_no_trail.py` in this repo, run via
   `_run_one_symbol` from `backtest.py` with `cfg["trailing_stop_enabled"] = False`)
   but may not have finished before this log was written. **Check for its
   results first — if not run yet, run it before anything else.**
2. **Strategy is very sensitive to execution quality** (slippage/entry-delay
   scenarios collapse PF toward 0.3-0.5). This means the raw edge is thin
   relative to costs — tightening entry quality (fewer, better trades) may
   matter more than tweaking exits.

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
