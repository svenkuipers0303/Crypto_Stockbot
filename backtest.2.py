"""
backtest.py v2 — Realistic backtester with fees, slippage,
ATR sizing, regime filter, Sharpe ratio, and walk-forward analysis.

USAGE:
  python backtest.py --symbol BTCUSDT --strategy bollinger --interval 4h --days 180
  python backtest.py --symbol BTCUSDT --strategy bollinger --walkforward
"""

import os
import argparse
import pandas as pd
import numpy as np
from binance.client import Client

from bot import (
    fetch_candles, compute_atr, detect_regime, atr_position_size,
    apply_costs, signal_ma, signal_rsi, signal_bollinger,
    STRATEGY_MAP, STRATEGY_REGIME, performance_metrics, CONFIG
)


def run_backtest(df: pd.DataFrame, strategy_fn, cfg: dict,
                 label: str = "BACKTEST", verbose: bool = True) -> dict:
    """
    Runs a single backtest pass on a DataFrame of candles.
    Returns a dict of performance metrics.
    """
    balance      = cfg["paper_balance"]
    initial_bal  = balance
    position     = None
    trades       = []
    equity_curve = [balance]
    ideal_regime = STRATEGY_REGIME[cfg["strategy"]]

    for i in range(60, len(df)):
        window = df.iloc[:i+1]
        price  = window["close"].iloc[-1]
        atr    = compute_atr(window).iloc[-1]

        # ── Check stops ───────────────────────────────────────
        if position:
            if price <= position["stop"]:
                fill  = apply_costs(price, "sell", cfg)
                pnl   = (fill - position["entry"]) / position["entry"] * position["usdt"]
                balance += position["usdt"] + pnl
                trades.append({"entry": position["entry"], "exit": fill,
                                "pnl": pnl, "reason": "SL"})
                position = None

            elif price >= position["tp"]:
                fill  = apply_costs(price, "sell", cfg)
                pnl   = (fill - position["entry"]) / position["entry"] * position["usdt"]
                balance += position["usdt"] + pnl
                trades.append({"entry": position["entry"], "exit": fill,
                                "pnl": pnl, "reason": "TP"})
                position = None

        # ── Regime filter ─────────────────────────────────────
        regime    = detect_regime(window)
        regime_ok = (regime == ideal_regime or regime == "neutral")

        # ── Signal ────────────────────────────────────────────
        signal = strategy_fn(window, cfg) if regime_ok else None

        # ── Execute ───────────────────────────────────────────
        if signal == "buy" and not position:
            usdt, stop, tp = atr_position_size(price, atr, cfg)
            usdt           = min(usdt, balance)
            if usdt < 1:
                equity_curve.append(balance)
                continue
            fill     = apply_costs(price, "buy", cfg)
            balance -= usdt
            position = {"entry": fill, "usdt": usdt, "stop": stop, "tp": tp}

        elif signal == "sell" and position:
            fill  = apply_costs(price, "sell", cfg)
            pnl   = (fill - position["entry"]) / position["entry"] * position["usdt"]
            balance += position["usdt"] + pnl
            trades.append({"entry": position["entry"], "exit": fill,
                            "pnl": pnl, "reason": "SIG"})
            position = None

        equity_curve.append(balance + (position["usdt"] if position else 0))

    # Close any open position at last price
    if position:
        fill  = apply_costs(df["close"].iloc[-1], "sell", cfg)
        pnl   = (fill - position["entry"]) / position["entry"] * position["usdt"]
        balance += position["usdt"] + pnl
        trades.append({"entry": position["entry"], "exit": fill,
                        "pnl": pnl, "reason": "END"})

    metrics = performance_metrics(trades, initial_bal)
    metrics["final_balance"] = round(balance, 2)
    metrics["roi"]            = round((balance - initial_bal) / initial_bal * 100, 2)

    # Buy-and-hold comparison
    bah = (df["close"].iloc[-1] - df["close"].iloc[0]) / df["close"].iloc[0] * 100
    metrics["buy_and_hold"] = round(bah, 2)
    metrics["beat_bah"]     = metrics["roi"] > bah
    metrics["trades_list"]  = trades

    if verbose:
        _print_results(label, metrics, cfg, trades)

    return metrics


def _print_results(label, m, cfg, trades):
    print(f"\n{'='*62}")
    print(f"  {label}")
    print(f"  Strategy : {cfg['strategy'].upper()}  |  Fees: {cfg['fee_pct']*100:.2f}%  "
          f"Slippage: {cfg['slippage_pct']*100:.3f}%")
    print(f"{'='*62}")

    if not trades:
        print("  No trades executed.\n")
        return

    print(f"  Total trades   : {m.get('trades', 0)}")
    print(f"  Win rate       : {m.get('win_rate', 0):.1f}%")
    print(f"  Sharpe ratio   : {m.get('sharpe', 0):.2f}  "
          f"({'✅ Good' if m.get('sharpe',0) > 1 else '⚠️ Low — needs improvement'})")
    print(f"  Profit factor  : {m.get('profit_factor', 0):.2f}  "
          f"({'✅ Good' if m.get('profit_factor',0) > 1.3 else '⚠️ Low'})")
    print(f"  Max drawdown   : {m.get('max_drawdown', 0):.2f}%")
    print(f"  Total PnL      : {m.get('total_pnl', 0):+.2f} USDT")
    print(f"  Final balance  : {m.get('final_balance', 0):.2f} USDT")
    print(f"  ROI            : {m.get('roi', 0):+.2f}%")
    print(f"\n  Buy-and-hold   : {m.get('buy_and_hold', 0):+.2f}%")
    beat = m.get('beat_bah', False)
    print(f"  {'✅ Bot beat buy-and-hold!' if beat else '❌ Buy-and-hold was better — tune strategy.'}")

    print(f"\n  {'#':>3}  {'Entry':>10}  {'Exit':>10}  {'PnL':>8}  Reason")
    print(f"  {'─'*52}")
    for i, t in enumerate(trades, 1):
        sign = "+" if t["pnl"] >= 0 else ""
        print(f"  {i:>3}  {t['entry']:>10.2f}  {t['exit']:>10.2f}  "
              f"{sign}{t['pnl']:>7.2f}  {t['reason']}")
    print()


def walk_forward(df: pd.DataFrame, strategy_fn, cfg: dict,
                 train_pct: float = 0.7):
    """
    Walk-forward analysis:
    Split data into train (70%) and test (30%).
    A strategy must work on BOTH — not just the period you optimized on.
    """
    split     = int(len(df) * train_pct)
    df_train  = df.iloc[:split].reset_index(drop=True)
    df_test   = df.iloc[split:].reset_index(drop=True)

    print("\n" + "="*62)
    print("  WALK-FORWARD ANALYSIS")
    print("  Train period: first 70% of data (in-sample)")
    print("  Test period:  last  30% of data (out-of-sample)")
    print("="*62)

    m_train = run_backtest(df_train, strategy_fn, cfg, label="IN-SAMPLE (train)")
    m_test  = run_backtest(df_test,  strategy_fn, cfg, label="OUT-OF-SAMPLE (test)")

    print("\n  SUMMARY:")
    print(f"  In-sample  ROI: {m_train['roi']:+.2f}%  |  Sharpe: {m_train.get('sharpe',0):.2f}")
    print(f"  Out-sample ROI: {m_test['roi']:+.2f}%   |  Sharpe: {m_test.get('sharpe',0):.2f}")

    if m_test["roi"] > 0 and m_test.get("sharpe", 0) > 0.5:
        print("\n  ✅ Strategy shows out-of-sample edge — promising!")
    else:
        print("\n  ❌ Strategy fails out-of-sample — likely overfitted. Do not go live.")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Realistic Backtest v2")
    parser.add_argument("--symbol",      default="BTCUSDT")
    parser.add_argument("--strategy",    default="bollinger", choices=["ma","rsi","bollinger"])
    parser.add_argument("--interval",    default="4h")
    parser.add_argument("--days",        default=180, type=int)
    parser.add_argument("--walkforward", action="store_true", help="Run walk-forward analysis")
    args = parser.parse_args()

    api_key    = CONFIG["api_key"]    or os.getenv("BINANCE_API_KEY", "")
    api_secret = CONFIG["api_secret"] or os.getenv("BINANCE_API_SECRET", "")
    client     = Client(api_key, api_secret)

    cfg              = CONFIG.copy()
    cfg["strategy"]  = args.strategy

    limit = min(1000, args.days * 6)
    df    = fetch_candles(client, args.symbol, args.interval, limit=limit)

    if args.walkforward:
        walk_forward(df, STRATEGY_MAP[args.strategy], cfg)
    else:
        run_backtest(df, STRATEGY_MAP[args.strategy], cfg,
                     label=f"BACKTEST | {args.symbol} | {args.interval} | {args.days}d")
