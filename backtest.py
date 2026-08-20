"""
Backtest engine for crypto_bot strategies.

Imports signal logic directly from bot.py — no duplication.
Runs a realistic walk-forward simulation over historical Binance data.

Features:
  - 60/20/20 out-of-sample split (train / validation / test)
  - Walk-forward consistency test (6 non-overlapping windows)
  - Robustness tests (2x/3x fees, slippage, score threshold, ATR mult, TP ratio)
  - Automated live-readiness check with objective go/no-go criteria
  - Richer metrics: expectancy, Calmar, by-year, by-quarter, by-regime
  - Universe mode: test across multiple random crypto pairs

Usage:
  python backtest.py                                  # BTCUSDT 365d all strategies
  python backtest.py --symbol SOLUSDT --days 180
  python backtest.py --strategy trend_pullback --days 1095
  python backtest.py --symbols BTCUSDT,ETHUSDT,SOLUSDT  # specific list
  python backtest.py --universe 10                    # 10 random top-50 USDT pairs
  python backtest.py --universe 15 --universe-seed 42 # reproducible random pick
  python backtest.py --no-robustness                  # skip slow robustness runs
  python backtest.py --output results.json

Limitations (be aware):
  - Fear & Greed history is not fetched — uses a fixed neutral value (50).
    Use --fg 30 / --fg 70 to test conservative/greedy assumptions.
  - Exits use end-of-candle price checks (stop/TP vs high/low).
    If both stop and TP are hit in the same candle, stop takes priority (pessimistic).
  - 4H entries fill at the OPEN of the candle AFTER the signal fires.
  - Universe/multi-symbol mode skips robustness tests automatically (speed).
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import (
    CONFIG,
    compute_ma, compute_atr, compute_adx,
    compute_signal_score, select_strategy, atr_position_size,
    STRATEGY_MAP,
)

try:
    from binance.client import Client
except ImportError:
    print("ERROR: python-binance not installed.  pip install python-binance")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────
#  FETCH HISTORICAL CANDLES  (handles Binance 1000-candle limit)
# ─────────────────────────────────────────────────────────────
def fetch_history(client: Client, symbol: str, interval: str, days: int) -> pd.DataFrame:
    ms_per_day  = 86_400_000
    end_ms      = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms    = end_ms - days * ms_per_day
    all_candles = []
    cursor      = start_ms

    while cursor < end_ms:
        raw = client.get_klines(
            symbol=symbol, interval=interval,
            startTime=cursor, endTime=end_ms, limit=1000,
        )
        if not raw:
            break
        all_candles.extend(raw)
        cursor = raw[-1][0] + 1
        if len(raw) < 1000:
            break
        time.sleep(0.12)

    if not all_candles:
        raise RuntimeError(f"No candle data returned for {symbol} {interval}")

    df = pd.DataFrame(all_candles, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "num_trades", "taker_base", "taker_quote", "ignore",
    ])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.iloc[:-1].reset_index(drop=True)


# ─────────────────────────────────────────────────────────────
#  UNIVERSE SYMBOL PICKER
# ─────────────────────────────────────────────────────────────
_STABLECOIN_PAIRS = {
    "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDTUSDT",
    "DAIUSDT",  "EURUSDT",  "GBPUSDT",  "AUDUSDT",   "BRLLUSDT",
}

def fetch_universe_symbols(client: Client, n: int, seed: int = None) -> list[str]:
    """
    Return N randomly-chosen symbols from the top 50 Binance USDT pairs by
    24 h quote volume.  Stablecoins and leveraged tokens are excluded.
    """
    tickers = client.get_ticker()
    usdt = [
        t for t in tickers
        if (t["symbol"].endswith("USDT")
            and t["symbol"] not in _STABLECOIN_PAIRS
            and "UP" not in t["symbol"] and "DOWN" not in t["symbol"]
            and "BULL" not in t["symbol"] and "BEAR" not in t["symbol"]
            and float(t.get("quoteVolume", 0)) > 0)
    ]
    usdt.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    pool = [t["symbol"] for t in usdt[:50]]
    rng  = random.Random(seed)
    return rng.sample(pool, min(n, len(pool)))


# ─────────────────────────────────────────────────────────────
#  WALK-FORWARD SIMULATION
# ─────────────────────────────────────────────────────────────
WARMUP = 220   # bars before first signal (indicators need ~200 bars)

def simulate(df_4h: pd.DataFrame, df_1d: pd.DataFrame,
             strategy_name: str, cfg: dict, fg_val: int = 50,
             cooldown_bars: int = 0, entry_delay: int = 1,
             use_limit_orders: bool = False) -> list:
    """
    Walk forward over df_4h one candle at a time.
    Signal fires at candle i → entry at open of candle i+entry_delay.
    use_limit_orders: place limit at signal-candle close; fill only if next bar's low ≤ limit.
    cooldown_bars: number of bars to skip re-entry after a stop-loss.
    Returns all completed trade dicts (each includes entry_bar for windowing).
    """
    fee       = cfg.get("fee_pct", 0.001) + cfg.get("slippage_pct", 0.0005)
    maker_fee = cfg.get("maker_fee_pct", 0.0002) + cfg.get("slippage_pct", 0.0005)
    balance = cfg.get("paper_balance", 200.0)
    trades  = []
    pos     = None
    cooldown_until = 0   # bar index after which re-entry is allowed

    for i in range(WARMUP, len(df_4h)):
        candle = df_4h.iloc[i]

        # ── Check exits ──────────────────────────────────────
        if pos is not None:
            if i == pos["entry_bar"]:
                continue
            lo, hi = candle["low"], candle["high"]

            # ── Update peak & trailing stop ───────────────────
            pos["peak"] = max(pos.get("peak", pos["entry_fill"]), hi)
            if cfg.get("trailing_stop_enabled", True):
                atr_e = pos.get("atr_entry", 0)
                if atr_e > 0:
                    peak  = pos["peak"]
                    entry = pos["entry_fill"]
                    be_at    = entry + atr_e * cfg.get("breakeven_at_atr_mult", 1.0)
                    trail_at = entry + atr_e * cfg.get("trail_from_atr_mult",   2.0)
                    if peak >= trail_at:
                        new_stop = peak - atr_e * cfg.get("trail_stop_atr_mult", 1.0)
                        pos["stop"] = max(pos["stop"], round(new_stop, 8))
                    elif peak >= be_at:
                        pos["stop"] = max(pos["stop"], round(entry, 8))

            exit_price = None
            reason     = None

            # ── Time-stop (stale position) ────────────────────
            time_stop = cfg.get("time_stop_bars", 0)
            if time_stop > 0 and (i - pos["entry_bar"]) >= time_stop:
                exit_price = candle["close"]
                reason     = "TIME-STOP"
            elif lo <= pos["stop"]:
                exit_price = pos["stop"]
                reason     = "STOP-LOSS"
            elif hi >= pos["tp"]:
                exit_price = pos["tp"]
                reason     = "TAKE-PROFIT"
            if exit_price is not None:
                fill      = exit_price * (1 - fee)
                pnl       = (fill - pos["entry_fill"]) / pos["entry_fill"] * pos["usdt"]
                balance  += pos["usdt"] + pnl
                ts_val    = pos["entry_ts"]
                yr        = ts_val.year  if hasattr(ts_val, "year")  else int(str(ts_val)[:4])
                mo        = ts_val.month if hasattr(ts_val, "month") else int(str(ts_val)[5:7])
                hold_bars = i - pos["entry_bar"]

                try:
                    entry_dt = pd.to_datetime(str(pos["entry_ts"]))
                    exit_dt  = pd.to_datetime(str(candle["open_time"]))
                    hold_h   = round((exit_dt - entry_dt).total_seconds() / 3600, 1)
                except Exception:
                    hold_h = hold_bars * 4.0

                trades.append({
                    "strategy":    pos["strategy"],
                    "regime":      pos["regime"],
                    "score":       pos["score"],
                    "entry_price": pos["entry_fill"],
                    "entry_ts":    str(pos["entry_ts"])[:16],
                    "entry_bar":   pos["entry_bar"],
                    "exit_price":  round(fill, 8),
                    "exit_bar":    i,
                    "exit_ts":     str(candle["open_time"])[:16],
                    "pnl":         round(pnl, 4),
                    "pnl_pct":     round(pnl / pos["usdt"] * 100, 2),
                    "usdt":        pos["usdt"],
                    "reason":      reason,
                    "hold_bars":   hold_bars,
                    "hold_hours":  hold_h,
                    "balance":     round(balance, 2),
                    "year":        str(yr),
                    "month":       f"{yr}-{mo:02d}",
                    "quarter":     f"Q{(mo - 1) // 3 + 1} {yr}",
                })
                if reason == "STOP-LOSS" and cooldown_bars > 0:
                    cooldown_until = i + cooldown_bars
                pos = None
            continue

        if i >= len(df_4h) - entry_delay:
            continue

        # ── Cooldown gate ────────────────────────────────────
        if i < cooldown_until:
            continue

        # ── Signal detection ──────────────────────────────────
        df_slice = df_4h.iloc[:i + 1].copy()
        ts       = candle["open_time"]
        df_htf   = df_1d[df_1d["open_time"] <= ts].copy()
        if len(df_htf) < 55:
            continue

        price   = df_slice["close"].iloc[-1]
        atr_val = compute_atr(df_slice).iloc[-1]
        if atr_val == 0 or np.isnan(atr_val):
            continue

        vol_pct  = atr_val / price * 100
        adx_val  = compute_adx(df_slice).iloc[-1]
        regime   = "trending" if adx_val > 25 else ("ranging" if adx_val < 20 else "neutral")
        htf_bull = df_htf["close"].iloc[-1] > compute_ma(df_htf["close"], 50).iloc[-1]

        if strategy_name == "auto":
            sel       = select_strategy(regime, htf_bull, fg_val, vol_pct, cfg)
            effective = sel.get("strategy")
        else:
            if not htf_bull:
                continue
            effective = strategy_name

        if not effective:
            continue
        if vol_pct > cfg.get("max_volatility_pct", 4.0):
            continue

        score_data = compute_signal_score(df_slice, df_htf, cfg, effective, fg_val)
        if not score_data.get("trade_allowed"):
            continue

        # ── Enter: limit (conditional fill at close) or market (open of next bar) ──
        if use_limit_orders:
            limit_price   = candle["close"]
            next_c        = df_4h.iloc[i + 1]
            if next_c["low"] > limit_price:
                continue  # price never pulled back to limit — no fill
            entry_ref     = limit_price
            fill          = limit_price * (1 + maker_fee)
            entry_bar_idx = i + 1
        else:
            next_c        = df_4h.iloc[i + entry_delay]
            entry_ref     = next_c["open"]
            fill          = entry_ref * (1 + fee)
            entry_bar_idx = i + entry_delay

        # ── Dynamic TP by regime ──────────────────────────────
        if cfg.get("dynamic_tp_by_regime", True):
            tp_rr = (cfg.get("tp_rr_trending", 3.0) if regime == "trending"
                     else cfg.get("tp_rr_ranging", 1.5))
            cfg_entry = {**cfg, "take_profit_rr": tp_rr}
        else:
            cfg_entry = cfg

        usdt, stop_raw, tp_raw = atr_position_size(
            entry_ref, atr_val, cfg_entry, equity_override=balance
        )
        if usdt <= 0 or balance < usdt or round(fill, 8) <= 0:
            continue

        balance -= usdt
        pos = {
            "strategy":   effective,
            "regime":     regime,
            "score":      score_data["total"],
            "entry_fill": round(fill, 8),
            "entry_ts":   next_c["open_time"],
            "entry_bar":  entry_bar_idx,
            "stop":       round(stop_raw, 8),
            "tp":         round(tp_raw, 8),
            "usdt":       round(usdt, 4),
            "atr_entry":  round(atr_val, 8),
            "peak":       round(fill, 8),
        }

    return trades


# ─────────────────────────────────────────────────────────────
#  METRICS
# ─────────────────────────────────────────────────────────────
def calc_metrics(trades: list, initial_balance: float) -> dict:
    empty = {
        "trades": 0, "total_return": 0.0, "final_balance": initial_balance,
        "profit_factor": 0.0, "win_rate": 0.0, "max_drawdown": 0.0,
        "sharpe": 0.0, "expectancy": 0.0, "calmar": 0.0, "total_pnl": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0, "largest_win": 0.0, "largest_loss": 0.0,
        "win_loss_ratio": 0.0, "streak_max_win": 0, "streak_max_loss": 0,
        "tp_hit_rate": 0.0, "sl_hit_rate": 0.0, "avg_hold_hours": 0.0,
        "fees_est": 0.0, "median_pnl": 0.0,
        "by_regime": {}, "by_year": {}, "by_quarter": {}, "by_month": {},
        "equity_curve": [initial_balance],
    }
    if not trades:
        return empty

    pnls   = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # ── Equity curve + drawdown ───────────────────────────────
    eq     = initial_balance
    peak   = eq
    max_dd = 0.0
    equity = [eq]
    for t in trades:
        eq    += t["pnl"]
        peak   = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100 if peak > 0 else 0)
        equity.append(round(eq, 2))

    # ── Sharpe ────────────────────────────────────────────────
    r      = pd.Series(pnls) / initial_balance
    sharpe = (r.mean() / r.std() * (252 * 6) ** 0.5) if r.std() > 0 else 0.0

    # ── Calmar ────────────────────────────────────────────────
    try:
        total_days = max(1, (pd.to_datetime(trades[-1]["exit_ts"]) -
                             pd.to_datetime(trades[0]["entry_ts"])).days)
    except Exception:
        total_days = 365
    annual_ret = (eq - initial_balance) / initial_balance * (365 / total_days) * 100
    calmar     = annual_ret / max_dd if max_dd > 0 else 0.0

    # ── Streaks ───────────────────────────────────────────────
    streak_win = streak_loss = cur_win = cur_loss = 0
    for p in pnls:
        if p > 0:
            cur_win   += 1
            cur_loss   = 0
            streak_win = max(streak_win, cur_win)
        else:
            cur_loss    += 1
            cur_win      = 0
            streak_loss  = max(streak_loss, cur_loss)

    # ── TP / SL rates ─────────────────────────────────────────
    tp_count = sum(1 for t in trades if t.get("reason") == "TAKE-PROFIT")
    sl_count = sum(1 for t in trades if t.get("reason") == "STOP-LOSS")
    n        = len(trades)

    # ── Holding time ─────────────────────────────────────────
    hold_hours = [t.get("hold_hours", t.get("hold_bars", 1) * 4.0) for t in trades]
    avg_hold_h = round(sum(hold_hours) / len(hold_hours), 1) if hold_hours else 0.0

    # ── Estimated fees (round-trip entry + exit) ──────────────
    # fee_pct not passed here; approximate using 0.15% round-trip
    fees_est = round(sum(t["usdt"] * 0.0015 * 2 for t in trades if "usdt" in t), 2)

    # ── Breakdown helper ──────────────────────────────────────
    def _breakdown(key):
        d: dict = {}
        for t in trades:
            k = t.get(key, "?")
            d.setdefault(k, {"trades": 0, "wins": 0, "pnl": 0.0, "pnls": []})
            d[k]["trades"] += 1
            d[k]["wins"]   += 1 if t["pnl"] > 0 else 0
            d[k]["pnl"]    += t["pnl"]
            d[k]["pnls"].append(t["pnl"])
        out = {}
        for k, v in d.items():
            ps  = v["pnls"]
            w   = [p for p in ps if p > 0]
            l   = [p for p in ps if p <= 0]
            exp = round(sum(ps) / len(ps), 4) if ps else 0.0
            out[k] = {
                "trades":   v["trades"],
                "win_rate": round(v["wins"] / v["trades"] * 100, 1),
                "pnl":      round(v["pnl"], 2),
                "expectancy": exp,
                "avg_win":  round(sum(w) / len(w), 2) if w else 0.0,
                "avg_loss": round(sum(l) / len(l), 2) if l else 0.0,
                "largest_win":  round(max(ps), 2) if ps else 0.0,
                "largest_loss": round(min(ps), 2) if ps else 0.0,
                "profit_factor": round(sum(w) / abs(sum(l)), 2) if l else 999.0,
            }
        return out

    return {
        "trades":          n,
        "win_rate":        round(len(wins) / n * 100, 1),
        "avg_win":         round(sum(wins)   / len(wins),   2) if wins   else 0.0,
        "avg_loss":        round(sum(losses) / len(losses), 2) if losses else 0.0,
        "largest_win":     round(max(pnls), 2),
        "largest_loss":    round(min(pnls), 2),
        "win_loss_ratio":  round(
            (sum(wins)/len(wins)) / abs(sum(losses)/len(losses)), 2
        ) if wins and losses else 0.0,
        "median_pnl":      round(float(pd.Series(pnls).median()), 4),
        "profit_factor":   round(sum(wins) / abs(sum(losses)), 2) if losses else 999.0,
        "max_drawdown":    round(max_dd, 2),
        "sharpe":          round(sharpe, 2),
        "expectancy":      round(sum(pnls) / n, 4),
        "calmar":          round(calmar, 2),
        "total_pnl":       round(sum(pnls), 2),
        "total_return":    round((eq - initial_balance) / initial_balance * 100, 2),
        "final_balance":   round(eq, 2),
        "streak_max_win":  streak_win,
        "streak_max_loss": streak_loss,
        "tp_hit_rate":     round(tp_count / n * 100, 1),
        "sl_hit_rate":     round(sl_count / n * 100, 1),
        "avg_hold_hours":  avg_hold_h,
        "fees_est":        fees_est,
        "equity_curve":    equity,
        "by_regime":       _breakdown("regime"),
        "by_year":         _breakdown("year"),
        "by_quarter":      _breakdown("quarter"),
        "by_month":        _breakdown("month"),
    }


# ─────────────────────────────────────────────────────────────
#  60 / 20 / 20 SPLIT ANALYSIS
# ─────────────────────────────────────────────────────────────
def run_split_analysis(all_trades: list, initial_balance: float) -> dict:
    """Train=60%, Validation=20%, Test=20% split by entry bar order."""
    if not all_trades:
        return {}
    bars = sorted(set(t["entry_bar"] for t in all_trades))
    n    = len(bars)
    b60  = bars[int(n * 0.60)]
    b80  = bars[int(n * 0.80)]

    train = [t for t in all_trades if t["entry_bar"] <  b60]
    val   = [t for t in all_trades if b60 <= t["entry_bar"] < b80]
    test  = [t for t in all_trades if t["entry_bar"] >= b80]

    return {
        "train":      calc_metrics(train, initial_balance),
        "validation": calc_metrics(val,   initial_balance),
        "test":       calc_metrics(test,  initial_balance),
        "n_train":    len(train),
        "n_val":      len(val),
        "n_test":     len(test),
    }


# ─────────────────────────────────────────────────────────────
#  WALK-FORWARD CONSISTENCY (6 windows)
# ─────────────────────────────────────────────────────────────
def run_walk_forward(all_trades: list, initial_balance: float,
                     n_windows: int = 6) -> dict:
    """Divide trades into N time windows; check per-window profitability."""
    if not all_trades:
        return {}
    bars = sorted(set(t["entry_bar"] for t in all_trades))
    n    = len(bars)
    size = max(1, n // n_windows)

    windows = []
    for w in range(n_windows):
        lo_idx = w * size
        if lo_idx >= n:
            break
        lo = bars[lo_idx]
        hi_idx = min((w + 1) * size, n)
        hi = bars[hi_idx - 1] + 1 if hi_idx <= n else bars[-1] + 1
        wt = [t for t in all_trades if lo <= t["entry_bar"] < hi]
        if not wt:
            continue
        m = calc_metrics(wt, initial_balance)
        windows.append({
            "window":        w + 1,
            "trades":        m["trades"],
            "win_rate":      m["win_rate"],
            "profit_factor": m["profit_factor"],
            "total_pnl":     m["total_pnl"],
            "max_drawdown":  m["max_drawdown"],
            "profitable":    m["total_pnl"] > 0,
        })

    if not windows:
        return {}
    profitable = sum(1 for w in windows if w["profitable"])
    avg_pf     = sum(w["profit_factor"] for w in windows) / len(windows)

    return {
        "windows":           windows,
        "n_windows":         len(windows),
        "profitable_windows": profitable,
        "avg_profit_factor": round(avg_pf, 2),
        "consistency_pct":   round(profitable / len(windows) * 100, 1),
    }


# ─────────────────────────────────────────────────────────────
#  ROBUSTNESS TESTS
# ─────────────────────────────────────────────────────────────
def run_robustness_tests(df_4h: pd.DataFrame, df_1d: pd.DataFrame,
                         strategy_name: str, base_cfg: dict,
                         fg_val: int = 50) -> dict:
    """Re-run simulation under varied parameters to test fragility."""
    ib = base_cfg.get("paper_balance", 200.0)

    cfg_fee   = base_cfg["fee_pct"]
    cfg_slip  = base_cfg.get("slippage_pct", 0.0005)
    cfg_maker = base_cfg.get("maker_fee_pct", 0.0002)

    # Tuple format: (cfg_overrides, entry_delay, cooldown_bars, use_limit_orders)
    # use_limit_orders=None means "inherit base_cfg's own use_limit_orders setting" —
    # every scenario should stress-test whatever execution mode the bot actually runs
    # live (CONFIG["use_limit_orders"]), not silently fall back to market orders.
    # Only the dedicated "limit orders" scenario forces it on, to answer "what if
    # limit orders were enabled" even when the baseline doesn't already use them.
    fee_scenarios = {
        "baseline":           ({}, 1, 0, None),
        "2x fees":            ({"fee_pct": cfg_fee * 2, "maker_fee_pct": cfg_maker * 2}, 1, 0, None),
        "3x fees":            ({"fee_pct": cfg_fee * 3, "maker_fee_pct": cfg_maker * 3}, 1, 0, None),
        "0.5% slippage":      ({"slippage_pct": 0.005}, 1, 0, None),
        "worse entry +0.1%":  ({"slippage_pct": cfg_slip + 0.001}, 1, 0, None),
        "limit orders":       ({"maker_fee_pct": 0.0002}, 1, 0, True),   # maker fee + conditional fill
        "score_thr=65":       ({"score_threshold": 65}, 1, 0, None),
        "score_thr=75":       ({"score_threshold": 75}, 1, 0, None),
        "score_thr=80":       ({"score_threshold": 80}, 1, 0, None),
        "ATR_mult=2.0":       ({"atr_stop_multiplier": 2.0}, 1, 0, None),
        "TP_RR=1.5":          ({"take_profit_rr": 1.5}, 1, 0, None),
        "TP_RR=3.0":          ({"take_profit_rr": 3.0}, 1, 0, None),
        "delayed entry +1":   ({}, 2, 0, None),   # enter 2 candles after signal
        "cooldown 1 bar":     ({}, 1, 1, None),
        "cooldown 2 bars":    ({}, 1, 2, None),
        "no trail stop":      ({"trailing_stop_enabled": False}, 1, 0, None),
        "no dyn TP":          ({"dynamic_tp_by_regime": False},  1, 0, None),
    }

    results = {}
    for name, (overrides, delay, cooldown, use_lim) in fee_scenarios.items():
        cfg = {**base_cfg, **overrides}
        eff_use_lim = base_cfg.get("use_limit_orders", False) if use_lim is None else use_lim
        trades = simulate(df_4h, df_1d, strategy_name, cfg, fg_val,
                          cooldown_bars=cooldown, entry_delay=delay,
                          use_limit_orders=eff_use_lim)
        m      = calc_metrics(trades, ib)
        results[name] = {
            "profit_factor":   m["profit_factor"],
            "total_pnl":       m["total_pnl"],
            "win_rate":        m["win_rate"],
            "trades":          m["trades"],
            "max_drawdown":    m["max_drawdown"],
            "expectancy":      m["expectancy"],
            "streak_max_loss": m["streak_max_loss"],
        }
    return results


# ─────────────────────────────────────────────────────────────
#  CONCENTRATION ANALYSIS
# ─────────────────────────────────────────────────────────────
def concentration_analysis(trades: list) -> dict:
    """
    Detects whether profit is concentrated in a few trades, one period,
    or one regime — which would make the edge fragile.
    """
    if not trades:
        return {}

    pnls       = [t["pnl"] for t in trades]
    total_pnl  = sum(pnls)
    winners    = sorted([t for t in trades if t["pnl"] > 0],
                        key=lambda x: x["pnl"], reverse=True)

    def _pct_of_total(subset_pnl):
        return round(subset_pnl / total_pnl * 100, 1) if total_pnl > 0 else 0.0

    # Top N trades contribution
    top1_pnl  = winners[0]["pnl"] if winners else 0.0
    top5_pnl  = sum(t["pnl"] for t in winners[:5])
    top10_n   = max(1, len(winners) // 10)
    top10_pnl = sum(t["pnl"] for t in winners[:top10_n])

    # Monthly / quarterly concentration
    by_month = {}
    by_qtr   = {}
    for t in trades:
        m = t.get("month",   t.get("year", "?"))
        q = t.get("quarter", t.get("year", "?"))
        by_month.setdefault(m, 0.0)
        by_month[m] += t["pnl"]
        by_qtr.setdefault(q, 0.0)
        by_qtr[q]   += t["pnl"]

    best_month_pnl = max(by_month.values()) if by_month else 0.0
    best_qtr_pnl   = max(by_qtr.values())   if by_qtr   else 0.0
    best_month     = max(by_month, key=by_month.get) if by_month else "?"
    best_qtr       = max(by_qtr,   key=by_qtr.get)   if by_qtr   else "?"

    profitable_months  = sum(1 for v in by_month.values() if v > 0)
    losing_months      = sum(1 for v in by_month.values() if v <= 0)
    profitable_qtrs    = sum(1 for v in by_qtr.values()   if v > 0)

    top1_pct  = _pct_of_total(top1_pnl)
    top5_pct  = _pct_of_total(top5_pnl)
    top10_pct = _pct_of_total(top10_pnl)
    best_month_pct = _pct_of_total(best_month_pnl)
    best_qtr_pct   = _pct_of_total(best_qtr_pnl)

    # Fragility flags
    flags = []
    if top1_pct  > 30: flags.append(f"Top 1 trade = {top1_pct:.0f}% of total profit")
    if top5_pct  > 60: flags.append(f"Top 5 trades = {top5_pct:.0f}% of total profit")
    if top10_pct > 80: flags.append(f"Top 10% of trades = {top10_pct:.0f}% of profit")
    if best_month_pct > 50: flags.append(f"Best month ({best_month}) = {best_month_pct:.0f}% of profit")
    if best_qtr_pct   > 60: flags.append(f"Best quarter ({best_qtr}) = {best_qtr_pct:.0f}% of profit")
    if profitable_months < losing_months:
        flags.append(f"Losing months ({losing_months}) > profitable months ({profitable_months})")

    return {
        "top1_trade_pct":      top1_pct,
        "top5_trades_pct":     top5_pct,
        "top10pct_trades_pct": top10_pct,
        "best_month":          best_month,
        "best_month_pct":      best_month_pct,
        "best_quarter":        best_qtr,
        "best_quarter_pct":    best_qtr_pct,
        "profitable_months":   profitable_months,
        "losing_months":       losing_months,
        "profitable_quarters": profitable_qtrs,
        "fragility_flags":     flags,
        "fragile":             len(flags) >= 2,
    }


# ─────────────────────────────────────────────────────────────
#  RISK MODEL RESEARCH
# ─────────────────────────────────────────────────────────────
def run_risk_model_research(df_4h: pd.DataFrame, df_1d: pd.DataFrame,
                             strategy_name: str, base_cfg: dict,
                             fg_val: int = 50) -> dict:
    """
    Re-run simulation at different risk_per_trade_pct values.
    Reports return, max drawdown, longest losing streak, and a risk-of-ruin estimate.
    Never tests above 1.5%.
    """
    ib      = base_cfg.get("paper_balance", 200.0)
    results = {}

    for risk_pct in [0.25, 0.50, 0.75, 1.00, 1.25, 1.50]:
        cfg    = {**base_cfg, "risk_per_trade_pct": risk_pct}
        trades = simulate(df_4h, df_1d, strategy_name, cfg, fg_val,
                          use_limit_orders=cfg.get("use_limit_orders", False))
        m      = calc_metrics(trades, ib)

        # Simple risk-of-ruin approximation (Kelly-based):
        # R = (1 - win_rate/100) ** streak_max_loss
        win_r = m["win_rate"] / 100
        loss_r = 1 - win_r
        streak = m.get("streak_max_loss", 1)
        ror_approx = round(loss_r ** streak * 100, 1)

        results[f"{risk_pct:.2f}%"] = {
            "risk_pct":        risk_pct,
            "trades":          m["trades"],
            "total_return":    m["total_return"],
            "final_balance":   m["final_balance"],
            "max_drawdown":    m["max_drawdown"],
            "streak_max_loss": m["streak_max_loss"],
            "profit_factor":   m["profit_factor"],
            "expectancy":      m["expectancy"],
            "ror_approx_pct":  ror_approx,
        }
    return results


# ─────────────────────────────────────────────────────────────
#  LIVE READINESS CHECK
# ─────────────────────────────────────────────────────────────
def check_live_readiness(strategy_name: str, split: dict, wf: dict,
                         robustness: dict, initial_balance: float,
                         full_metrics: dict = None,
                         concentration: dict = None) -> dict:
    """
    Automated go/no-go.  Returns {ready, score, checks, blocked_by, reason}.
    Blocking checks (weight ≥ 2) must ALL pass for ready=True.
    """
    checks        = {}
    total_weight  = 0
    passed_weight = 0

    def chk(name, condition, label, weight=1):
        nonlocal passed_weight, total_weight
        total_weight  += weight
        passed_weight += weight if condition else 0
        checks[name] = {"pass": condition, "label": label, "weight": weight}

    test_m  = split.get("test",  {})
    val_m   = split.get("validation", {})
    train_m = split.get("train", {})
    n_total = (split.get("n_train", 0) + split.get("n_val", 0) + split.get("n_test", 0))
    m       = full_metrics or {}
    conc    = concentration or {}

    # ── Critical checks (weight ≥ 2) must all pass ────────────
    chk("pf_test",      test_m.get("profit_factor",  0) > 1.4,
        f"OOS test PF {test_m.get('profit_factor', 0):.2f} > 1.4 required",         weight=3)
    chk("trade_count",  n_total >= 100,
        f"Total trades {n_total} ≥ 100 required",                                    weight=2)
    chk("oos_positive", val_m.get("total_pnl", 0) > 0,
        f"Validation PnL {val_m.get('total_pnl', 0):+.2f} must be positive",        weight=2)
    chk("max_dd",       test_m.get("max_drawdown", 100) < 15,
        f"Test max DD {test_m.get('max_drawdown', 100):.1f}% < 15% required",       weight=2)
    pf_2x = robustness.get("2x fees", {}).get("profit_factor", 0)
    chk("pf_2x_fees",   pf_2x > 1.2,
        f"PF with 2x fees {pf_2x:.2f} > 1.2 required",                              weight=2)
    wf_ok = wf.get("consistency_pct", 0) >= 50
    chk("walk_forward", wf_ok,
        f"Walk-forward consistency {wf.get('consistency_pct', 0):.0f}% ≥ 50%",     weight=2)

    # Positive expectancy required
    exp_val = m.get("expectancy", 0)
    chk("expectancy",   exp_val > 0,
        f"Expectancy {exp_val:+.3f} must be positive",                               weight=2)

    # ── Bonus checks (weight 1) ───────────────────────────────
    chk("win_rate",     test_m.get("win_rate", 0) > 40,
        f"Test win rate {test_m.get('win_rate', 0):.1f}% > 40% required",           weight=1)
    chk("pf_train",     train_m.get("profit_factor", 0) > 1.2,
        f"Train PF {train_m.get('profit_factor', 0):.2f} > 1.2 required",           weight=1)

    # Max losing streak < 8
    streak = m.get("streak_max_loss", 0)
    chk("streak",       streak < 8,
        f"Max losing streak {streak} < 8 required",                                  weight=1)

    # Concentration: top 5 trades should not dominate
    top5 = conc.get("top5_trades_pct", 0)
    chk("concentration", top5 <= 60,
        f"Top 5 trades = {top5:.0f}% of profit (need ≤ 60% — not concentrated)",   weight=1)

    score      = round(passed_weight / total_weight * 100, 1) if total_weight > 0 else 0
    blocked_by = [name for name, d in checks.items() if not d["pass"] and d["weight"] >= 2]
    ready      = len(blocked_by) == 0 and score >= 80

    # ── Specific rejection reasons ─────────────────────────────
    reasons = []
    if not checks.get("pf_test", {}).get("pass"):
        reasons.append(f"OOS test PF {test_m.get('profit_factor', 0):.2f} < 1.4 target")
    if not checks.get("trade_count", {}).get("pass"):
        reasons.append(f"Only {n_total} trades tested (need 100+ for statistical confidence)")
    if not checks.get("oos_positive", {}).get("pass"):
        reasons.append(f"Validation period lost money ({val_m.get('total_pnl', 0):+.2f})")
    if not checks.get("pf_2x_fees", {}).get("pass"):
        reasons.append(f"Edge disappears at 2x fees (PF {pf_2x:.2f}) — too fee-sensitive")
    if not checks.get("walk_forward", {}).get("pass"):
        reasons.append(f"Walk-forward only {wf.get('consistency_pct', 0):.0f}% consistent")
    if not checks.get("expectancy", {}).get("pass"):
        reasons.append(f"Negative expectancy ({exp_val:+.3f}) per trade")
    if conc.get("fragile"):
        reasons.extend(conc.get("fragility_flags", []))

    return {
        "strategy":   strategy_name,
        "ready":      ready,
        "score":      score,
        "checks":     checks,
        "blocked_by": blocked_by,
        "reasons":    reasons,
        "reason": (
            "All critical criteria met — strategy may be ready for paper live testing"
            if not blocked_by
            else f"Blocked by: {', '.join(blocked_by)}"
        ),
    }


# ─────────────────────────────────────────────────────────────
#  CONSOLE OUTPUT
# ─────────────────────────────────────────────────────────────
G   = "\033[92m"
R   = "\033[91m"
Y   = "\033[93m"
B   = "\033[94m"
C   = "\033[96m"
DIM = "\033[2m"
RST = "\033[0m"

def _cpct(v):
    s = f"{'+' if v >= 0 else ''}{v:.1f}%"
    return (G + s + RST) if v > 0 else (R + s + RST) if v < 0 else s

def _cpf(v):
    s = f"{v:.2f}"
    return (G + s + RST) if v >= 1.5 else (Y + s + RST) if v >= 1.0 else (R + s + RST)


def print_extra_metrics(m: dict) -> None:
    print(f"\n  {C}── Extended Metrics ──{RST}\n")
    print(f"  Largest win    : {G}+{m.get('largest_win', 0):.2f}{RST}  "
          f"  Largest loss   : {R}{m.get('largest_loss', 0):.2f}{RST}")
    print(f"  Win/loss ratio : {m.get('win_loss_ratio', 0):.2f}  "
          f"  Median PnL     : {m.get('median_pnl', 0):+.3f}")
    print(f"  TP hit rate    : {G}{m.get('tp_hit_rate', 0):.1f}%{RST}  "
          f"  SL hit rate    : {R}{m.get('sl_hit_rate', 0):.1f}%{RST}")
    print(f"  Max win streak : {G}{m.get('streak_max_win', 0)}{RST}  "
          f"  Max loss streak: {R}{m.get('streak_max_loss', 0)}{RST}")
    print(f"  Avg hold time  : {m.get('avg_hold_hours', 0):.1f} hours  "
          f"  Est. fees paid : ~${m.get('fees_est', 0):.2f}")
    print()


def print_concentration(conc: dict) -> None:
    print(f"\n  {C}── Concentration Analysis ──{RST}\n")
    if not conc:
        print(f"  {DIM}Not enough data{RST}\n")
        return
    print(f"  Top 1 trade  : {conc['top1_trade_pct']:>5.1f}% of total profit")
    print(f"  Top 5 trades : {conc['top5_trades_pct']:>5.1f}% of total profit")
    print(f"  Top 10%      : {conc['top10pct_trades_pct']:>5.1f}% of total profit")
    print(f"  Best month   : {conc['best_month']:<10}  {conc['best_month_pct']:>5.1f}% of profit")
    print(f"  Best quarter : {conc['best_quarter']:<10}  {conc['best_quarter_pct']:>5.1f}% of profit")
    print(f"  Profitable months : {conc['profitable_months']}  |  "
          f"Losing months : {conc['losing_months']}")
    if conc.get("fragility_flags"):
        print(f"\n  {Y}⚠ Fragility flags:{RST}")
        for flag in conc["fragility_flags"]:
            print(f"    {R}•{RST} {flag}")
    verdict = f"{R}FRAGILE{RST}" if conc.get("fragile") else f"{G}OK{RST}"
    print(f"\n  Concentration verdict: {verdict}\n")


def print_risk_model(risk: dict) -> None:
    print(f"\n  {C}── Risk Model Research ──{RST}\n")
    print(f"  {'Risk%':<8} {'Trades':>7} {'Return':>9} {'MaxDD':>8} "
          f"{'PF':>7} {'MaxLoss':>9} {'RoR~%':>8}")
    print("  " + "─" * 60)
    for label, r in risk.items():
        col    = G if r["total_return"] > 0 else R
        dd_col = R if r["max_drawdown"] > 20 else (Y if r["max_drawdown"] > 10 else G)
        ret_s  = f"{'+' if r['total_return'] >= 0 else ''}{r['total_return']:.1f}%"
        dd_s   = f"-{r['max_drawdown']:.1f}%"
        ror    = r.get("ror_approx_pct", 0)
        ror_c  = R if ror > 5 else (Y if ror > 1 else G)
        print(f"  {label:<8} {r['trades']:>7} {col}{ret_s:>9}{RST} "
              f"{dd_col}{dd_s:>8}{RST} {_cpf(r['profit_factor']):>14} "
              f"  streak {r['streak_max_loss']:>2}  {ror_c}{ror:>6.1f}%{RST}")
    print()


def print_results_table(results: dict) -> None:
    sep = "─" * 84
    print(f"  {'Strategy':<20} {'Trades':>7} {'Win%':>7} {'PF':>7} "
          f"{'Expect':>8} {'MaxDD':>8} {'Return':>9} {'Calmar':>7}")
    print("  " + sep)
    for name, m in results.items():
        if m.get("trades", 0) == 0:
            print(f"  {name:<20} {DIM}(no trades){RST}")
            continue
        dd  = f"-{m['max_drawdown']:.1f}%"
        exp = f"{m.get('expectancy', 0):+.3f}"
        print(f"  {name:<20} {m['trades']:>7} {m['win_rate']:>6.1f}% "
              f"{_cpf(m['profit_factor']):>14} {exp:>8} {R}{dd:<8}{RST} "
              f"{_cpct(m['total_return']):>16} {m.get('calmar', 0):>7.1f}")
    print("  " + sep + "\n")


def print_split(split: dict) -> None:
    print(f"\n  {C}── Out-of-Sample Split Analysis (60 / 20 / 20) ──{RST}\n")
    print(f"  {'Split':<14} {'Trades':>7} {'Win%':>7} {'PF':>7} {'PnL':>10} {'MaxDD':>7}")
    print("  " + "─" * 54)
    for name in ("train", "validation", "test"):
        m = split.get(name, {})
        if not m or m.get("trades", 0) == 0:
            print(f"  {name:<14} {DIM}(no trades){RST}")
            continue
        col     = G if m["total_pnl"] > 0 else R
        pnl_str = f"{'+' if m['total_pnl'] >= 0 else ''}{m['total_pnl']:.2f}"
        print(f"  {name:<14} {m['trades']:>7} {m['win_rate']:>6.1f}% "
              f"{_cpf(m['profit_factor']):>14} {col}{pnl_str:>10}{RST} "
              f"{R}-{m['max_drawdown']:.1f}%{RST}")
    print()


def print_walk_forward(wf: dict) -> None:
    print(f"\n  {C}── Walk-Forward Consistency ──{RST}\n")
    if not wf.get("windows"):
        print(f"  {DIM}No walk-forward data (too few trades){RST}\n")
        return
    print(f"  {'Win':>6} {'Trades':>7} {'Win%':>7} {'PF':>7} {'PnL':>10}  Status")
    print("  " + "─" * 50)
    for w in wf["windows"]:
        col  = G if w["profitable"] else R
        stat = f"{col}{'PROFIT' if w['profitable'] else 'LOSS  '}{RST}"
        sgn  = "+" if w["total_pnl"] >= 0 else ""
        print(f"  {w['window']:>6} {w['trades']:>7} {w['win_rate']:>6.1f}% "
              f"{_cpf(w['profit_factor']):>14} {sgn}{w['total_pnl']:>9.2f}  {stat}")
    print(f"\n  Profitable: {wf['profitable_windows']}/{wf['n_windows']} windows "
          f"({wf['consistency_pct']:.0f}%)  |  Avg PF: {_cpf(wf['avg_profit_factor'])}\n")


def print_robustness(robustness: dict) -> None:
    print(f"\n  {C}── Robustness Tests ──{RST}\n")
    print(f"  {'Scenario':<20} {'Trades':>7} {'Win%':>7} {'PF':>7} {'PnL':>10} {'MaxDD':>7}")
    print("  " + "─" * 62)
    baseline_pf = robustness.get("baseline", {}).get("profit_factor", 0)
    for name, m in robustness.items():
        if not m:
            continue
        col = G if m["total_pnl"] > 0 else R
        sgn = "+" if m["total_pnl"] >= 0 else ""
        # Flag scenarios where PF dropped more than 30% vs baseline
        warn = f" {Y}▼{RST}" if (baseline_pf > 0 and m["profit_factor"] < baseline_pf * 0.7) else ""
        # Highlight limit orders scenario
        highlight = f" {G}✓ SOLVES{RST}" if name == "limit orders" and m["profit_factor"] > 1.2 else ""
        print(f"  {name:<20} {m['trades']:>7} {m['win_rate']:>6.1f}% "
              f"{_cpf(m['profit_factor']):>14} {col}{sgn}{m['total_pnl']:>9.2f}{RST} "
              f"{R}-{m['max_drawdown']:.1f}%{RST}{warn}{highlight}")
    print()


def print_live_readiness(lr: dict) -> None:
    col   = G if lr["ready"] else R
    label = f"{col}{'✓  READY' if lr['ready'] else '✗  NOT READY'}{RST}"
    print(f"\n  {C}── Live Readiness: {lr['strategy'].upper()} ──{RST}")
    print(f"  Verdict: {label}  |  Readiness score: {lr['score']:.0f}/100\n")
    for name, c in lr["checks"].items():
        tick = f"{G}✓{RST}" if c["pass"] else f"{R}✗{RST}"
        wt   = "★" * c["weight"]
        print(f"  {tick} {wt:<6}  {c['label']}")
    if lr.get("blocked_by"):
        print(f"\n  {R}Blocked by critical check(s):{RST} {', '.join(lr['blocked_by'])}")
    # Detailed rejection reasons
    reasons = lr.get("reasons", [])
    if reasons:
        print(f"\n  {Y}Why not ready:{RST}")
        for r in reasons:
            print(f"    • {r}")
    print()


def print_by_year(results: dict) -> None:
    years = sorted(set(yr for m in results.values() for yr in m.get("by_year", {})))
    if not years:
        return
    print(f"\n  {C}── PnL by Year ──{RST}\n")
    print(f"  {'Strategy':<20} " + "".join(f"{yr:>12}" for yr in years))
    print("  " + "─" * (20 + 12 * len(years)))
    for strat, m in results.items():
        if m.get("trades", 0) == 0:
            continue
        row = f"  {strat:<20}"
        for yr in years:
            d   = m.get("by_year", {}).get(yr, {})
            pnl = d.get("pnl")
            if pnl is None:
                row += f"{'—':>12}"
            else:
                col = G if pnl > 0 else R
                row += f"{col}{'+' if pnl >= 0 else ''}{pnl:>10.2f}{RST}"
        print(row)
    print()


def print_regime_breakdown(results: dict) -> None:
    print(f"\n  {C}── Regime Breakdown ──{RST}\n")
    print(f"  {'Strategy':<20} {'Regime':<12} {'Trades':>7} {'Win%':>7} {'PnL':>10}")
    print("  " + "─" * 60)
    for strat, m in results.items():
        if m.get("trades", 0) == 0:
            continue
        for reg, d in sorted(m.get("by_regime", {}).items()):
            col = G if d["pnl"] > 0 else R
            sgn = "+" if d["pnl"] >= 0 else ""
            print(f"  {strat:<20} {reg:<12} {d['trades']:>7} "
                  f"{d['win_rate']:>6.1f}% {col}{sgn}{d['pnl']:>9.2f}{RST}")
    print()


def print_trade_log(trades_by_strategy: dict, n: int = 20) -> None:
    all_t = [{**t, "_strat": s} for s, ts in trades_by_strategy.items() for t in ts]
    if not all_t:
        return
    all_t.sort(key=lambda x: x["entry_ts"])
    recent = all_t[-n:]
    print(f"  Last {len(recent)} completed trades (chronological):\n")
    print(f"  {'#':<4} {'Date':<18} {'Strategy':<18} "
          f"{'Entry':>10} {'Exit':>10} {'PnL':>9} {'Reason'}")
    print("  " + "─" * 80)
    for j, t in enumerate(recent, 1):
        pnl  = t["pnl"]
        col  = G if pnl > 0 else R
        sgn  = "+" if pnl >= 0 else ""
        print(f"  {j:<4} {t['entry_ts']:<18} {t['_strat']:<18} "
              f"{t['entry_price']:>10.2f} {t['exit_price']:>10.2f} "
              f"{col}{sgn}{pnl:>8.2f}{RST}  {t['reason']}")
    print()


# ─────────────────────────────────────────────────────────────
#  UNIVERSE SUMMARY (multi-symbol mode)
# ─────────────────────────────────────────────────────────────
def print_universe_summary(rows: list) -> None:
    """
    rows = list of dicts:
      symbol, strategy, trades, win_rate, profit_factor,
      total_return, readiness_score, ready, max_drawdown
    """
    print(f"\n{'═'*90}")
    print(f"  {'UNIVERSE SUMMARY':^88}")
    print(f"{'═'*90}\n")
    print(f"  {'Symbol':<12} {'Strategy':<18} {'Trades':>7} {'Win%':>7} "
          f"{'PF':>7} {'Return':>9} {'MaxDD':>7} {'Score':>7} {'Ready':>7}")
    print("  " + "─" * 86)

    profitable = 0
    pf_sum     = 0.0
    ready_n    = 0

    for r in rows:
        col   = G if r["total_return"] > 0 else R
        rcol  = G if r["ready"] else R
        ret_s = f"{'+' if r['total_return'] >= 0 else ''}{r['total_return']:.1f}%"
        pf_s  = f"{r['profit_factor']:.2f}"
        pf_col = G if r["profit_factor"] >= 1.5 else (Y if r["profit_factor"] >= 1.0 else R)
        rd    = f"-{r['max_drawdown']:.1f}%"
        print(f"  {r['symbol']:<12} {r['strategy']:<18} {r['trades']:>7} "
              f"{r['win_rate']:>6.1f}% {pf_col}{pf_s:>7}{RST} "
              f"{col}{ret_s:>9}{RST} {R}{rd:>7}{RST} "
              f"{r['readiness_score']:>7.0f} "
              f"{rcol}{'YES' if r['ready'] else 'NO':>7}{RST}")
        if r["total_return"] > 0:
            profitable += 1
        pf_sum += r["profit_factor"]
        if r["ready"]:
            ready_n += 1

    n = len(rows)
    avg_pf = pf_sum / n if n else 0
    print("  " + "─" * 86)
    pf_col = G if avg_pf >= 1.5 else (Y if avg_pf >= 1.0 else R)
    print(f"\n  Symbols tested : {n}")
    print(f"  Profitable     : {profitable}/{n}  ({profitable/n*100:.0f}%)")
    print(f"  Live-ready     : {ready_n}/{n}")
    print(f"  Average PF     : {pf_col}{avg_pf:.2f}{RST}")
    if n >= 5:
        verdict = (
            "STRONG edge — strategy generalises well across the universe"
            if profitable / n >= 0.7 and avg_pf >= 1.2
            else "WEAK edge — strategy is inconsistent across symbols"
            if profitable / n < 0.5
            else "MARGINAL — profitable on majority but not convincingly"
        )
        col = G if profitable / n >= 0.7 else (Y if profitable / n >= 0.5 else R)
        print(f"  Universe verdict: {col}{verdict}{RST}")
    print(f"\n{'═'*90}\n")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def _run_one_symbol(client, symbol, days, strategies, cfg, fg_val,
                    skip_robustness, verbose=True):
    """Fetch data and run all strategies for a single symbol. Returns (results_dict, all_data)."""
    initial_balance = cfg.get("paper_balance", 200.0)
    try:
        if verbose:
            print(f"\n{B}Fetching {days}d of 4H candles for {symbol}...{RST}")
        df_4h = fetch_history(client, symbol, "4h", days + 60)
        if verbose:
            print(f"{B}Fetching daily candles for HTF trend filter...{RST}")
        df_1d = fetch_history(client, symbol, "1d", days + 120)
        if verbose:
            print(f"  {len(df_4h)} × 4H candles,  {len(df_1d)} × 1D candles\n")
    except Exception as e:
        print(f"  {R}ERROR fetching {symbol}: {e}{RST}")
        return None, None

    results, trades_by_strat, splits_by_strat = {}, {}, {}
    wf_by_strat, robust_by_strat, readiness_by_strat = {}, {}, {}
    concentration_by_strat = {}

    if verbose:
        print(f"{'═'*80}")
        print(f"  Backtest  ▸  {symbol}  4H  |  {days}d  |  Start balance: ${initial_balance:.0f}")
        print(f"{'═'*80}\n")

    for strat in strategies:
        if verbose:
            print(f"  {B}Running {strat}...{RST}", flush=True)
        t0     = time.time()
        trades = simulate(df_4h, df_1d, strat, cfg, fg_val=fg_val,
                          use_limit_orders=cfg.get("use_limit_orders", False))
        m      = calc_metrics(trades, initial_balance)
        results[strat]         = m
        trades_by_strat[strat] = trades
        if verbose:
            print(f"  ↳ {m['trades']} trades  PF {m.get('profit_factor', 0):.2f}  "
                  f"({time.time() - t0:.1f}s)")

        splits_by_strat[strat] = run_split_analysis(trades, initial_balance)
        wf_by_strat[strat]     = run_walk_forward(trades, initial_balance, n_windows=6)

        conc = concentration_analysis(trades)

        if not skip_robustness and m["trades"] >= 10:
            if verbose:
                print(f"  {DIM}Robustness tests for {strat}...{RST}", flush=True)
            robust_by_strat[strat] = run_robustness_tests(
                df_4h, df_1d, strat, cfg, fg_val=fg_val
            )
        else:
            robust_by_strat[strat] = {}

        readiness_by_strat[strat] = check_live_readiness(
            strat, splits_by_strat[strat], wf_by_strat[strat],
            robust_by_strat[strat], initial_balance,
            full_metrics=m, concentration=conc,
        )
        concentration_by_strat[strat] = conc

    # Risk model research for the best-performing strategy
    risk_model = {}
    if not skip_robustness:
        best_s = max(
            (s for s in strategies if results[s].get("trades", 0) > 0),
            key=lambda s: results[s].get("profit_factor", 0),
            default=None,
        )
        if best_s:
            if verbose:
                print(f"  {DIM}Risk model research for {best_s}...{RST}", flush=True)
            risk_model = run_risk_model_research(df_4h, df_1d, best_s, cfg, fg_val=fg_val)

    all_data = {
        "results":               results,
        "trades_by_strat":       trades_by_strat,
        "splits_by_strat":       splits_by_strat,
        "wf_by_strat":           wf_by_strat,
        "robust_by_strat":       robust_by_strat,
        "readiness_by_strat":    readiness_by_strat,
        "concentration_by_strat": concentration_by_strat,
        "risk_model":            risk_model,
    }
    return results, all_data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest crypto_bot with OOS split, walk-forward & robustness tests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--symbol",         default="BTCUSDT",
                        help="Single symbol (default BTCUSDT)")
    parser.add_argument("--symbols",        default=None,
                        help="Comma-separated list of symbols, e.g. BTCUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--universe",       type=int, default=0, metavar="N",
                        help="Pick N random symbols from top-50 Binance USDT pairs by volume")
    parser.add_argument("--universe-seed",  type=int, default=None,
                        help="Random seed for --universe selection (reproducibility)")
    parser.add_argument("--days",           type=int, default=365)
    parser.add_argument("--strategy",       default="all",
                        choices=["all", "bollinger", "trend_pullback", "breakout", "auto"])
    parser.add_argument("--fg",             type=int, default=50)
    parser.add_argument("--output",         default="backtest_results.json")
    parser.add_argument("--no-robustness",  action="store_true",
                        help="Skip robustness tests (faster)")
    args = parser.parse_args()

    cfg             = dict(CONFIG)
    initial_balance = cfg.get("paper_balance", 200.0)

    api_key    = cfg.get("api_key",    "") or os.environ.get("BINANCE_KEY",    "")
    api_secret = cfg.get("api_secret", "") or os.environ.get("BINANCE_SECRET", "")
    client     = Client(api_key, api_secret)

    # ── Build symbol list ─────────────────────────────────────
    if args.universe > 0:
        print(f"{B}Fetching Binance ticker for universe selection...{RST}")
        symbol_list = fetch_universe_symbols(client, args.universe, seed=args.universe_seed)
        print(f"  Universe ({args.universe} symbols): {', '.join(symbol_list)}\n")
    elif args.symbols:
        symbol_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbol_list = [args.symbol]

    multi = len(symbol_list) > 1
    if multi:
        skip_robustness = True   # too slow for many symbols
        print(f"{Y}Multi-symbol mode: robustness tests disabled for speed.{RST}")
        print(f"Running {len(symbol_list)} symbols × strategies...\n")
    else:
        skip_robustness = args.no_robustness

    strategies = (
        ["bollinger", "trend_pullback", "breakout", "auto"]
        if args.strategy == "all"
        else [args.strategy]
    )

    def _serial(obj):
        if hasattr(obj, "isoformat"): return obj.isoformat()
        if isinstance(obj, (np.integer, np.floating)): return obj.item()
        return str(obj)

    # ── Multi-symbol universe mode ────────────────────────────
    if multi:
        universe_rows = []
        all_symbol_data = {}

        for sym in symbol_list:
            cfg["symbols"] = [sym]
            print(f"\n{'─'*60}")
            print(f"  {C}▸ {sym}{RST}")
            results, all_data = _run_one_symbol(
                client, sym, args.days, strategies, cfg,
                args.fg, skip_robustness, verbose=False,
            )
            if results is None:
                continue

            all_symbol_data[sym] = all_data
            readiness = all_data["readiness_by_strat"]

            # pick best strategy per symbol
            ranked = sorted(
                [(s, readiness[s]) for s in strategies if results[s].get("trades", 0) > 0],
                key=lambda x: x[1]["score"], reverse=True,
            )
            if not ranked:
                print(f"  {DIM}No trades generated — skipping{RST}")
                continue

            best_s, best_r = ranked[0]
            m = results[best_s]
            rob = all_data["robust_by_strat"].get(best_s, {})
            pf_2x = rob.get("2x fees", {}).get("profit_factor", float("nan"))
            print(f"  Best: {best_s}  |  {m['trades']} trades  "
                  f"PF {m['profit_factor']:.2f}  Return {m['total_return']:+.1f}%  "
                  f"Score {best_r['score']:.0f}/100")

            universe_rows.append({
                "symbol":          sym,
                "strategy":        best_s,
                "trades":          m["trades"],
                "win_rate":        m["win_rate"],
                "profit_factor":   m["profit_factor"],
                "total_return":    m["total_return"],
                "max_drawdown":    m["max_drawdown"],
                "readiness_score": best_r["score"],
                "ready":           best_r["ready"],
            })

        print_universe_summary(universe_rows)

        out = {
            "mode":     "universe",
            "symbols":  symbol_list,
            "days":     args.days,
            "fg_val":   args.fg,
            "run_at":   datetime.now(timezone.utc).isoformat(),
            "universe": universe_rows,
        }
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.output)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, default=_serial, indent=2)
        print(f"  Results saved → {out_path}\n")
        return

    # ── Single-symbol mode (original behaviour) ───────────────
    symbol = symbol_list[0]
    cfg["symbols"] = [symbol]
    results, all_data = _run_one_symbol(
        client, symbol, args.days, strategies, cfg,
        args.fg, skip_robustness, verbose=True,
    )
    if results is None:
        return

    trades_by_strat      = all_data["trades_by_strat"]
    splits_by_strat      = all_data["splits_by_strat"]
    wf_by_strat          = all_data["wf_by_strat"]
    robust_by_strat      = all_data["robust_by_strat"]
    readiness_by_strat   = all_data["readiness_by_strat"]
    concentration_by_str = all_data.get("concentration_by_strat", {})

    print()
    print_results_table(results)
    print_regime_breakdown(results)
    print_by_year(results)

    risk_model = all_data.get("risk_model", {})

    for strat in strategies:
        if results[strat].get("trades", 0) == 0:
            continue
        print(f"\n  {'─'*72}")
        print(f"  {C}  {strat.upper()}  {'─'*60}{RST}")
        print_extra_metrics(results[strat])
        print_split(splits_by_strat.get(strat, {}))
        print_walk_forward(wf_by_strat.get(strat, {}))
        if robust_by_strat.get(strat):
            print_robustness(robust_by_strat[strat])
        print_concentration(concentration_by_str.get(strat, {}))
        print_live_readiness(readiness_by_strat[strat])

    if risk_model:
        print_risk_model(risk_model)

    print_trade_log(trades_by_strat)

    ranked = sorted(
        [(s, r) for s, r in readiness_by_strat.items() if results[s].get("trades", 0) > 0],
        key=lambda x: x[1]["score"], reverse=True,
    )
    if ranked:
        top_s, top_r = ranked[0]
        col = G if top_r["ready"] else R
        print(f"\n  {'═'*72}")
        print(f"  OVERALL VERDICT: {col}{'READY FOR PAPER LIVE' if top_r['ready'] else 'NOT READY FOR LIVE'}{RST}")
        print(f"  Best strategy: {top_s}  (readiness {top_r['score']:.0f}/100)")
        if not top_r["ready"] and top_r.get("reasons"):
            print(f"\n  {Y}Not live-ready because:{RST}")
            for reason in top_r["reasons"]:
                print(f"    • {reason}")
        print(f"  {'═'*72}\n")

    best_r = ranked[0][1] if ranked else {}

    # ── Verdicts summary (strategy_verdicts.json) ─────────────
    verdicts = {}
    for s in strategies:
        lr = readiness_by_strat.get(s, {})
        m  = results.get(s, {})
        if m.get("trades", 0) == 0:
            verdicts[s] = {"verdict": "NO_DATA", "reason": "No trades generated"}
            continue
        pf = m.get("profit_factor", 0)
        if pf < 1.0:
            v = "REJECT"
        elif not lr.get("ready") and lr.get("score", 0) < 50:
            v = "NOT_READY"
        elif not lr.get("ready"):
            v = "PROMISING"
        else:
            v = "READY"
        verdicts[s] = {
            "verdict":         v,
            "readiness_score": lr.get("score", 0),
            "profit_factor":   pf,
            "trades":          m.get("trades", 0),
            "reasons":         lr.get("reasons", []),
            "blocked_by":      lr.get("blocked_by", []),
        }

    # ── Save backtest_results.json ────────────────────────────
    out = {
        "symbol":          symbol,
        "days":            args.days,
        "fg_val":          args.fg,
        "initial_balance": initial_balance,
        "run_at":          datetime.now(timezone.utc).isoformat(),
        "config": {
            "fee_pct":            cfg.get("fee_pct"),
            "slippage_pct":       cfg.get("slippage_pct"),
            "atr_stop_multiplier":cfg.get("atr_stop_multiplier"),
            "take_profit_rr":     cfg.get("take_profit_rr"),
            "score_threshold":    cfg.get("score_threshold"),
            "risk_per_trade_pct": cfg.get("risk_per_trade_pct"),
        },
        "live_readiness": {
            "ready":           best_r.get("ready", False),
            "best_strategy":   ranked[0][0] if ranked else None,
            "readiness_score": best_r.get("score", 0),
            "reason":          best_r.get("reason", ""),
            "reasons":         best_r.get("reasons", []),
            "blocked_by":      best_r.get("blocked_by", []),
            "strategies": {
                s: {k: v for k, v in r.items() if k != "checks"}
                for s, r in readiness_by_strat.items()
            },
        },
        "verdicts":      verdicts,
        "risk_model":    risk_model,
        "strategies": {
            k: {
                **{kk: vv for kk, vv in results[k].items() if kk != "equity_curve"},
                "equity_curve":      results[k].get("equity_curve", []),
                "trades":            trades_by_strat.get(k, []),
                "split_analysis": {
                    split: {kk: vv for kk, vv in sd.items() if kk != "equity_curve"}
                    for split, sd in splits_by_strat.get(k, {}).items()
                    if isinstance(sd, dict)
                },
                "walk_forward":      wf_by_strat.get(k, {}),
                "robustness":        robust_by_strat.get(k, {}),
                "concentration":     concentration_by_str.get(k, {}),
                "live_readiness":    readiness_by_strat.get(k, {}),
            }
            for k in strategies
        },
    }

    base_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(base_dir, args.output)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, default=_serial, indent=2)
    print(f"  Results saved → {out_path}")

    # ── Save strategy_verdicts.json ───────────────────────────
    vdict_path = os.path.join(base_dir, "strategy_verdicts.json")
    with open(vdict_path, "w", encoding="utf-8") as f:
        json.dump({
            "symbol": symbol, "days": args.days,
            "run_at": datetime.now(timezone.utc).isoformat(),
            "verdicts": verdicts,
        }, f, default=_serial, indent=2)
    print(f"  Verdicts saved → {vdict_path}\n")


if __name__ == "__main__":
    main()
