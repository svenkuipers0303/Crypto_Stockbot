"""
╔══════════════════════════════════════════════════════════════╗
║       BINANCE CRYPTO TRADING BOT v2 — Professional Edition  ║
║  Strategies : MA Crossover | RSI | Bollinger Bands          ║
║  New in v2  : Fees/slippage | ATR sizing | Regime detection ║
║               Multi-timeframe | Sharpe ratio | Closed-candle║
╚══════════════════════════════════════════════════════════════╝

SETUP:
  pip install python-binance pandas numpy

USAGE:
  python bot.py                          # paper trade BTC/USDT, bollinger
  python bot.py --live                   # REAL money — be careful!
  python bot.py --symbol ETHUSDT --strategy rsi
  python bot.py --symbol BTCUSDT --strategy bollinger --interval 4h

⚠️  DISCLAIMER: No bot guarantees profit. Always paper trade first.
    Only risk money you can afford to lose entirely.
"""

import os
import time
import logging
import argparse
import math
from datetime import datetime

import pandas as pd
import numpy as np
from binance.client import Client
from binance.exceptions import BinanceAPIException


# ─────────────────────────────────────────────────────────────
#  CONFIGURATION  — edit these values before running
# ─────────────────────────────────────────────────────────────
CONFIG = {
    # ── Binance API keys ──────────────────────────────────────
    # Leave blank and set env vars instead (safer):
    #   Windows:  $env:BINANCE_API_KEY="xxx"
    #   Linux:    export BINANCE_API_KEY="xxx"
    "api_key":    "",
    "api_secret": "",

    # ── Trading pair & timeframes ─────────────────────────────
    "symbol":       "BTCUSDT",
    "interval":     "4h",     # entry timeframe: 1h, 4h, 1d
    "htf_interval": "1d",     # higher timeframe trend filter

    # ── Strategy ─────────────────────────────────────────────
    # choices: "ma", "rsi", "bollinger"
    "strategy": "bollinger",

    # ── Risk management ───────────────────────────────────────
    "equity":            1000.0,  # total account size in USDT
    "risk_per_trade_pct":   1.0,  # % of equity to risk per trade (1% = $10 on $1000)
    "max_position_pct":    20.0,  # never more than 20% of equity in one trade
    "atr_stop_multiplier":  1.5,  # stop placed 1.5x ATR below entry
    "take_profit_rr":       2.0,  # take profit at 2x the stop distance (2:1 R:R)
    "max_daily_loss_pct":   5.0,  # shut bot down if daily loss exceeds this

    # ── Fees & slippage (realistic) ───────────────────────────
    "fee_pct":      0.001,   # 0.1% Binance taker fee per side
    "slippage_pct": 0.0005,  # 0.05% estimated slippage per side

    # ── MA Crossover settings ─────────────────────────────────
    "ma_fast": 9,
    "ma_slow": 21,

    # ── RSI settings ─────────────────────────────────────────
    "rsi_period":     14,
    "rsi_oversold":   30,
    "rsi_overbought": 70,

    # ── Bollinger Bands settings ──────────────────────────────
    "bb_period": 20,
    "bb_std":     2.0,

    # ── Loop & paper trading ──────────────────────────────────
    "poll_seconds":  60,      # how often to check for signals
    "paper_balance": 1000.0,  # virtual USDT for paper trading
}


# ─────────────────────────────────────────────────────────────
#  LOGGING  — writes to both terminal and bot.log
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bot")


# ─────────────────────────────────────────────────────────────
#  DATA FETCHER
# ─────────────────────────────────────────────────────────────
def fetch_candles(client: Client, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    """Fetch OHLCV candles. Returns only CLOSED candles (drops last row)."""
    raw = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df  = pd.DataFrame(raw, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qav","num_trades","taker_base","taker_quote","ignore"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    # ✅ FIX: drop last row — it's the current unclosed candle
    return df.iloc[:-1].reset_index(drop=True)


# ─────────────────────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────────────────────
def compute_ma(closes: pd.Series, period: int) -> pd.Series:
    return closes.rolling(period).mean()


def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_bollinger(closes: pd.Series, period: int, std: float):
    mid   = closes.rolling(period).mean()
    sigma = closes.rolling(period).std()
    return mid + std * sigma, mid, mid - std * sigma  # upper, mid, lower


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — measures market volatility."""
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — measures trend strength."""
    high, low, close = df["high"], df["low"], df["close"]
    tr       = pd.concat([high - low,
                           (high - close.shift()).abs(),
                           (low  - close.shift()).abs()], axis=1).max(axis=1)
    atr      = tr.rolling(period).mean()
    plus_dm  = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    plus_di  = 100 * plus_dm.rolling(period).mean() / atr
    minus_di = 100 * minus_dm.rolling(period).mean() / atr
    dx       = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9))
    return dx.rolling(period).mean()


# ─────────────────────────────────────────────────────────────
#  MARKET REGIME DETECTION
# ─────────────────────────────────────────────────────────────
def detect_regime(df: pd.DataFrame) -> str:
    """
    Detects whether market is trending or ranging using ADX.
    ADX > 25 = trending  → use MA/breakout strategies
    ADX < 20 = ranging   → use mean-reversion (Bollinger/RSI)
    """
    adx = compute_adx(df).iloc[-1]
    if adx > 25:
        return "trending"
    elif adx < 20:
        return "ranging"
    return "neutral"


# ─────────────────────────────────────────────────────────────
#  FEES & SLIPPAGE
# ─────────────────────────────────────────────────────────────
def apply_costs(price: float, side: str, cfg: dict) -> float:
    """Return realistic fill price after fees + slippage."""
    cost = cfg["fee_pct"] + cfg["slippage_pct"]
    if side == "buy":
        return price * (1 + cost)
    return price * (1 - cost)


# ─────────────────────────────────────────────────────────────
#  ATR-BASED POSITION SIZING
# ─────────────────────────────────────────────────────────────
def atr_position_size(price: float, atr: float, cfg: dict) -> tuple[float, float, float]:
    """
    Calculates position size based on ATR and risk %.
    Returns (usdt_to_spend, stop_price, take_profit_price)
    """
    equity        = cfg["equity"]
    risk_amount   = equity * (cfg["risk_per_trade_pct"] / 100)  # e.g. $10
    stop_distance = atr * cfg["atr_stop_multiplier"]            # e.g. $500
    qty           = risk_amount / stop_distance                  # BTC qty
    usdt          = qty * price                                  # position value

    # Cap at max_position_pct of equity
    max_usdt = equity * (cfg["max_position_pct"] / 100)
    usdt     = min(usdt, max_usdt)

    stop_price   = price - stop_distance
    tp_distance  = stop_distance * cfg["take_profit_rr"]
    tp_price     = price + tp_distance

    return usdt, stop_price, tp_price


# ─────────────────────────────────────────────────────────────
#  HIGHER TIMEFRAME TREND FILTER
# ─────────────────────────────────────────────────────────────
def htf_trend(client: Client, symbol: str, interval: str) -> str:
    """
    Returns 'bull' or 'bear' based on whether price is above/below
    the 50-period MA on the higher timeframe.
    """
    df   = fetch_candles(client, symbol, interval, limit=60)
    ma50 = compute_ma(df["close"], 50).iloc[-1]
    return "bull" if df["close"].iloc[-1] > ma50 else "bear"


# ─────────────────────────────────────────────────────────────
#  STRATEGY SIGNALS  →  "buy" | "sell" | None
# ─────────────────────────────────────────────────────────────
def signal_ma(df: pd.DataFrame, cfg: dict) -> str | None:
    fast = compute_ma(df["close"], cfg["ma_fast"])
    slow = compute_ma(df["close"], cfg["ma_slow"])
    if fast.iloc[-2] < slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]:
        return "buy"
    if fast.iloc[-2] > slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]:
        return "sell"
    return None


def signal_rsi(df: pd.DataFrame, cfg: dict) -> str | None:
    rsi        = compute_rsi(df["close"], cfg["rsi_period"])
    prev, curr = rsi.iloc[-2], rsi.iloc[-1]
    if prev < cfg["rsi_oversold"]   and curr >= cfg["rsi_oversold"]:
        return "buy"
    if prev > cfg["rsi_overbought"] and curr <= cfg["rsi_overbought"]:
        return "sell"
    return None


def signal_bollinger(df: pd.DataFrame, cfg: dict) -> str | None:
    upper, _, lower = compute_bollinger(df["close"], cfg["bb_period"], cfg["bb_std"])
    price, prev     = df["close"].iloc[-1], df["close"].iloc[-2]
    if prev < lower.iloc[-2] and price >= lower.iloc[-1]:
        return "buy"
    if prev > upper.iloc[-2] and price <= upper.iloc[-1]:
        return "sell"
    return None


STRATEGY_MAP = {
    "ma":        signal_ma,
    "rsi":       signal_rsi,
    "bollinger": signal_bollinger,
}

# Which regime suits each strategy
STRATEGY_REGIME = {
    "ma":        "trending",   # MA works best in trending markets
    "rsi":       "ranging",    # RSI works best in ranging markets
    "bollinger": "ranging",    # Bollinger works best in ranging markets
}


# ─────────────────────────────────────────────────────────────
#  PERFORMANCE METRICS
# ─────────────────────────────────────────────────────────────
def performance_metrics(trades: list, initial_balance: float) -> dict:
    if not trades:
        return {}
    pnls       = [t["pnl"] for t in trades]
    returns    = [p / initial_balance for p in pnls]
    r          = pd.Series(returns)
    wins       = [p for p in pnls if p > 0]
    losses     = [p for p in pnls if p <= 0]

    sharpe     = (r.mean() / r.std() * (252 ** 0.5)) if r.std() > 0 else 0
    cumulative = (1 + r).cumprod()
    max_dd     = ((cumulative - cumulative.cummax()) / cumulative.cummax()).min() * 100
    pf         = sum(wins) / abs(sum(losses)) if losses else float("inf")
    win_rate   = len(wins) / len(trades) * 100

    return {
        "trades":         len(trades),
        "win_rate":       round(win_rate, 1),
        "sharpe":         round(sharpe, 2),
        "profit_factor":  round(pf, 2),
        "max_drawdown":   round(max_dd, 2),
        "total_pnl":      round(sum(pnls), 2),
    }


# ─────────────────────────────────────────────────────────────
#  PAPER TRADING ENGINE
# ─────────────────────────────────────────────────────────────
class PaperTrader:
    def __init__(self, balance: float):
        self.balance       = balance
        self.initial_bal   = balance
        self.position      = None
        self.trades        = []
        self.daily_pnl     = 0.0
        self.daily_reset   = datetime.utcnow().date()

    def _check_daily_reset(self):
        today = datetime.utcnow().date()
        if today != self.daily_reset:
            self.daily_pnl   = 0.0
            self.daily_reset = today

    def buy(self, symbol: str, price: float, usdt: float,
            stop: float, tp: float, cfg: dict):
        if self.position:
            log.warning("Already in position — skipping buy.")
            return
        fill_price   = apply_costs(price, "buy", cfg)
        qty          = usdt / fill_price
        self.balance -= usdt
        self.position = {
            "symbol": symbol, "entry": fill_price,
            "qty": qty, "usdt": usdt,
            "stop": stop, "tp": tp,
        }
        log.info(f"📈  PAPER BUY   {qty:.6f} {symbol} @ {fill_price:.2f}  "
                 f"stop={stop:.2f}  tp={tp:.2f}  "
                 f"(size {usdt:.2f} USDT | balance {self.balance:.2f})")

    def sell(self, price: float, reason: str = "signal", cfg: dict = {}):
        if not self.position:
            return
        fill_price   = apply_costs(price, "sell", cfg)
        pnl          = (fill_price - self.position["entry"]) / self.position["entry"] * self.position["usdt"]
        self.balance += self.position["usdt"] + pnl
        self._check_daily_reset()
        self.daily_pnl += pnl
        sign = "+" if pnl >= 0 else ""
        log.info(f"📉  PAPER SELL  {self.position['qty']:.6f} {self.position['symbol']} "
                 f"@ {fill_price:.2f}  PnL {sign}{pnl:.2f} USDT  [{reason}]  "
                 f"Balance {self.balance:.2f}")
        self.trades.append({"entry": self.position["entry"],
                             "exit": fill_price, "pnl": pnl, "reason": reason})
        self.position = None

    def check_stops(self, price: float, cfg: dict):
        if not self.position:
            return
        if price <= self.position["stop"]:
            self.sell(price, reason="STOP-LOSS", cfg=cfg)
        elif price >= self.position["tp"]:
            self.sell(price, reason="TAKE-PROFIT", cfg=cfg)

    def check_daily_limit(self, cfg: dict) -> bool:
        """Returns True if daily loss limit hit — bot should pause."""
        self._check_daily_reset()
        limit = cfg["equity"] * (cfg["max_daily_loss_pct"] / 100)
        if self.daily_pnl < -limit:
            log.warning(f"⛔  Daily loss limit hit ({self.daily_pnl:.2f} USDT) — pausing until tomorrow.")
            return True
        return False

    def stats(self) -> str:
        m = performance_metrics(self.trades, self.initial_bal)
        if not m:
            return "No closed trades yet."
        return (f"Trades:{m['trades']}  Win:{m['win_rate']}%  "
                f"Sharpe:{m['sharpe']}  PF:{m['profit_factor']}  "
                f"MaxDD:{m['max_drawdown']}%  PnL:{m['total_pnl']:+.2f}  "
                f"Balance:{self.balance:.2f}")


# ─────────────────────────────────────────────────────────────
#  LIVE TRADING ENGINE
# ─────────────────────────────────────────────────────────────
class LiveTrader:
    def __init__(self, client: Client, symbol: str):
        self.client   = client
        self.symbol   = symbol
        self.position = None

    def _round_qty(self, qty: float, step: float) -> float:
        precision = int(round(-math.log(step, 10), 0))
        return round(math.floor(qty / step) * step, precision)

    def _check_duplicate(self, symbol: str) -> bool:
        """Prevent placing a buy if we already have an open order."""
        try:
            orders = self.client.get_open_orders(symbol=symbol)
            return len(orders) > 0
        except BinanceAPIException:
            return False

    def buy(self, symbol: str, price: float, usdt: float,
            stop: float, tp: float, cfg: dict):
        if self.position:
            log.warning("Already in position — skipping buy.")
            return
        if self._check_duplicate(symbol):
            log.warning("Open orders already exist — skipping buy.")
            return
        try:
            info     = self.client.get_symbol_info(symbol)
            lot_size = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
            step     = float(lot_size["stepSize"])
            qty      = self._round_qty(usdt / price, step)
            order    = self.client.order_market_buy(symbol=symbol, quantity=qty)
            self.position = {"symbol": symbol, "entry": price,
                             "qty": qty, "stop": stop, "tp": tp, "usdt": usdt}
            log.info(f"✅  LIVE BUY  {qty} {symbol} @ ~{price:.2f}  "
                     f"stop={stop:.2f}  tp={tp:.2f}  order={order['orderId']}")
        except BinanceAPIException as e:
            log.error(f"BUY failed: {e}")

    def sell(self, price: float, reason: str = "signal", cfg: dict = {}):
        if not self.position:
            return
        try:
            qty   = self.position["qty"]
            order = self.client.order_market_sell(symbol=self.position["symbol"], quantity=qty)
            log.info(f"✅  LIVE SELL {qty} {self.position['symbol']} @ ~{price:.2f}  "
                     f"[{reason}]  order={order['orderId']}")
            self.position = None
        except BinanceAPIException as e:
            log.error(f"SELL failed: {e}")

    def check_stops(self, price: float, cfg: dict):
        if not self.position:
            return
        if price <= self.position["stop"]:
            self.sell(price, reason="STOP-LOSS", cfg=cfg)
        elif price >= self.position["tp"]:
            self.sell(price, reason="TAKE-PROFIT", cfg=cfg)

    def check_daily_limit(self, cfg: dict) -> bool:
        return False  # In live mode, manage this via Binance directly

    def stats(self) -> str:
        return "Live mode — check Binance for full trade history."


# ─────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────
def run(cfg: dict, live: bool):
    api_key    = cfg["api_key"]    or os.getenv("BINANCE_API_KEY",    "")
    api_secret = cfg["api_secret"] or os.getenv("BINANCE_API_SECRET", "")

    if live and (not api_key or not api_secret):
        raise ValueError("Live mode requires BINANCE_API_KEY and BINANCE_API_SECRET.")

    client      = Client(api_key, api_secret)
    strategy_fn = STRATEGY_MAP.get(cfg["strategy"])
    if not strategy_fn:
        raise ValueError(f"Unknown strategy '{cfg['strategy']}'. Choose: ma, rsi, bollinger")

    ideal_regime = STRATEGY_REGIME[cfg["strategy"]]
    trader       = LiveTrader(client, cfg["symbol"]) if live else PaperTrader(cfg["paper_balance"])
    mode         = "🔴 LIVE" if live else "📄 PAPER"

    log.info("=" * 65)
    log.info(f"  {mode} BOT v2 STARTED")
    log.info(f"  Symbol     : {cfg['symbol']}")
    log.info(f"  Interval   : {cfg['interval']} (HTF: {cfg['htf_interval']})")
    log.info(f"  Strategy   : {cfg['strategy'].upper()} (best in {ideal_regime} markets)")
    log.info(f"  Risk/trade : {cfg['risk_per_trade_pct']}% of equity")
    log.info(f"  ATR stop   : {cfg['atr_stop_multiplier']}x ATR  |  R:R {cfg['take_profit_rr']}:1")
    log.info(f"  Fees       : {cfg['fee_pct']*100:.2f}%  |  Slippage: {cfg['slippage_pct']*100:.3f}%")
    log.info("=" * 65)

    iteration   = 0
    last_signal = None

    while True:
        try:
            iteration += 1

            # ── Fetch data ────────────────────────────────────
            df    = fetch_candles(client, cfg["symbol"], cfg["interval"], limit=200)
            price = df["close"].iloc[-1]
            atr   = compute_atr(df).iloc[-1]

            # ── Check stops ───────────────────────────────────
            trader.check_stops(price, cfg)

            # ── Check daily loss limit ────────────────────────
            if trader.check_daily_limit(cfg):
                time.sleep(3600)  # sleep 1h before checking again
                continue

            # ── Market regime filter ──────────────────────────
            regime = detect_regime(df)
            regime_ok = (regime == ideal_regime or regime == "neutral")

            # ── Higher timeframe trend filter ─────────────────
            trend = htf_trend(client, cfg["symbol"], cfg["htf_interval"])

            # ── Signal generation ─────────────────────────────
            signal = strategy_fn(df, cfg)

            # ── Multi-timeframe filter ────────────────────────
            # Only take buy signals when HTF is bullish
            # Only take sell signals when HTF is bearish
            if signal == "buy"  and trend != "bull": signal = None
            if signal == "sell" and trend != "bear": signal = None

            # ── Regime filter ─────────────────────────────────
            if signal and not regime_ok:
                log.info(f"[{iteration:>5}] Signal '{signal}' filtered — regime={regime} "
                         f"(need {ideal_regime})")
                signal = None

            log.info(f"[{iteration:>5}] Price={price:.2f}  ATR={atr:.2f}  "
                     f"Signal={signal or '—':>4}  Regime={regime}  "
                     f"HTF={trend}  {'IN POSITION' if trader.position else 'flat'}")

            # ── Execute signal ────────────────────────────────
            if signal == "buy" and not trader.position and signal != last_signal:
                usdt, stop, tp = atr_position_size(price, atr, cfg)
                trader.buy(cfg["symbol"], price, usdt, stop, tp, cfg)

            elif signal == "sell" and trader.position:
                trader.sell(price, reason="signal", cfg=cfg)

            last_signal = signal

            # ── Periodic stats ────────────────────────────────
            if iteration % 20 == 0:
                log.info(f"  ── STATS: {trader.stats()}")

        except BinanceAPIException as e:
            log.error(f"Binance API error: {e} — retrying in 60s")
            time.sleep(60)
        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            log.info(f"Final stats: {trader.stats()}")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e} — retrying in 60s")
            time.sleep(60)

        time.sleep(cfg["poll_seconds"])


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Binance Crypto Trading Bot v2")
    parser.add_argument("--live",     action="store_true",        help="Enable live trading")
    parser.add_argument("--symbol",   default=CONFIG["symbol"],   help="e.g. BTCUSDT")
    parser.add_argument("--strategy", default=CONFIG["strategy"], choices=["ma","rsi","bollinger"])
    parser.add_argument("--interval", default=CONFIG["interval"], help="e.g. 1h 4h 1d")
    args = parser.parse_args()

    CONFIG["symbol"]   = args.symbol
    CONFIG["strategy"] = args.strategy
    CONFIG["interval"] = args.interval

    if args.live:
        print("\n⚠️  WARNING: LIVE MODE — real money will be traded!")
        print("   Press Ctrl+C within 5 seconds to abort...\n")
        time.sleep(5)

    run(CONFIG, live=args.live)
