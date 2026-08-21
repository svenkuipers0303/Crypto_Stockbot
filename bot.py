"""
╔══════════════════════════════════════════════════════════════╗
║   BINANCE CRYPTO TRADING BOT v4 — Transparent Edition       ║
║  Core strategy  : MA | RSI | Bollinger (4H)                 ║
║  Active module  : 1H Pullback to EMA (optional)             ║
║  v4 adds        : Signal scores | Strategy states           ║
║                   Skip analytics | Multi-symbol             ║
╚══════════════════════════════════════════════════════════════╝

SETUP:
  pip install python-binance pandas numpy

USAGE:
  python bot.py          # paper trade all symbols in CONFIG["symbols"]
  python bot.py --live   # REAL money — be careful!

Active strategy (disabled by default):
  Set active_strategy.enabled = True in CONFIG.
  Always paper-test before going live.

Emergency stop:
  Create a file named EMERGENCY_STOP in the bot's working directory.
  The bot pauses every 60s until you delete it.
"""

import json
import math
import os
import time
import logging
import argparse
import urllib.request
from datetime import datetime, timezone, timedelta, date

import pandas as pd
import numpy as np
from binance.client import Client
from binance.exceptions import BinanceAPIException


# ─────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────
CONFIG = {
    # ── Binance API keys (prefer env vars) ───────────────────
    "api_key":    "",
    "api_secret": "",

    # ── Symbols & timeframes ──────────────────────────────────
    "symbols":      ["BTCUSDT", "SOLUSDT"],
    "interval":     "4h",        # core strategy timeframe
    "htf_interval": "1d",        # higher-timeframe trend filter

    # ── Core strategy ─────────────────────────────────────────
    "strategy":     "bollinger",  # ma | rsi | bollinger

    # ── Risk management ───────────────────────────────────────
    "equity":              200.0,  # total account size (split across symbols)
    "risk_per_trade_pct":    1.0,  # % of per-symbol equity to risk
    "max_position_pct":     20.0,
    "atr_stop_multiplier":   1.5,
    "take_profit_rr":        1.5,  # fixed RR now that dynamic_tp_by_regime is off (was 2.0)

    # ── Signal score gate ─────────────────────────────────────
    "score_threshold": 70,         # min score (0-100) required to trade

    # ── Kill switches ─────────────────────────────────────────
    "max_daily_loss_pct":    5.0,
    "max_weekly_loss_pct":  10.0,
    "max_drawdown_pct":     15.0,
    "max_api_errors":         5,
    "max_volatility_pct":    4.0,

    # ── Fees & slippage ───────────────────────────────────────
    "fee_pct":      0.001,
    "slippage_pct": 0.0005,

    # ── Limit orders (maker fee = 0.02% vs taker 0.10%) ─────
    "use_limit_orders":     True,   # place limit at signal-candle close; fill next bar
    "maker_fee_pct":        0.0002, # Binance BNB-discount maker fee
    "limit_order_expiry_h": 4,      # cancel if price doesn't reach limit within N hours

    # ── MA strategy ───────────────────────────────────────────
    "ma_fast": 9,
    "ma_slow": 21,

    # ── RSI strategy ─────────────────────────────────────────
    "rsi_period":     14,
    "rsi_oversold":   30,
    "rsi_overbought": 70,

    # ── Bollinger strategy ────────────────────────────────────
    "bb_period": 20,
    "bb_std":     2.0,

    # ── Loop & paper trading ──────────────────────────────────
    "poll_seconds":  60,
    "paper_balance": 200.0,       # total virtual balance (split across symbols)

    # ── Telegram (prefer env vars TELEGRAM_TOKEN / TELEGRAM_CHAT_ID) ──
    "telegram_token":   "",
    "telegram_chat_id": "",

    # ── Active strategy module (disabled by default) ──────────
    "active_strategy": {
        "enabled":            False,   # flip to True to enable
        "entry_interval":     "1h",    # entry signal timeframe
        "trend_interval":     "4h",    # trend filter timeframe
        "risk_per_trade_pct": 0.5,     # lower risk than core strategy
        "max_open_positions": 2,       # max simultaneous active positions
        "min_signal_score":   75,      # score threshold for active entries
        "min_rr":             2.0,
        "ema_fast":           20,
        "ema_slow":           50,
        "ema_long":           200,
        "rsi_min_buy":        38,      # pullback RSI zone
        "rsi_max_buy":        62,
        "volume_factor":      0.9,     # min volume vs 20-period avg
        "tp1_r":              1.5,     # first take-profit at 1.5R
        "tp2_r":              2.5,     # second take-profit at 2.5R
    },

    # ── Auto strategy selector ─────────────────────────────
    # When enabled, the bot picks the best strategy for the current regime.
    # Backtested findings (730-day BTCUSDT):
    #   breakout trending:      51.6% win, PF ~1.25  ← best edge
    #   trend_pullback ranging: 57.1% win            ← also solid
    #   bollinger all:          32.4% win, PF 0.80   ← disabled (loser)
    "auto_strategy": {
        "enabled":                    True,   # False = use CONFIG["strategy"] always

        # Bollinger disabled in auto — 730d backtest shows PF 0.80.
        # Set True only if you retrain and it shows consistent PF > 1.4.
        "bollinger_enabled_in_auto":  False,

        "trend_pullback_enabled":     True,
        "trend_pullback_trending_only": False,  # works in ranging too per backtest

        # Breakout only in trending regime — ranging/neutral showed poor results.
        "breakout_enabled":           True,
        "breakout_trending_only":     True,

        # Trend Pullback settings (EMA pullback + RSI reset)
        "tp_ema_fast":       20,    # primary pullback target
        "tp_ema_slow":       50,    # deeper pullback target
        "tp_near_pct":       2.5,   # % tolerance for "near EMA"
        "tp_rsi_min":        38,    # RSI reset zone low
        "tp_rsi_max":        62,    # RSI reset zone high
        "tp_volume_factor":  0.9,   # min vol vs 20-bar avg

        # Breakout Continuation settings
        "bo_lookback":       20,    # candles to find consolidation high
        "bo_rsi_max":        75,    # RSI must be below this at breakout
        "bo_volume_factor":  1.2,   # breakout requires strong volume
    },

    # ── Exit improvements ─────────────────────────────────────
    # trailing_stop_enabled=False + dynamic_tp_by_regime=False + take_profit_rr=1.5
    # (fixed) confirmed via full-pipeline backtests (OOS split, walk-forward,
    # robustness, concentration) on both BTCUSDT and SOLUSDT — see BACKTEST_LOG.md
    # 2026-08-04 entries. Readiness score 32->90 (BTC) and 16->84 (SOL); every
    # critical check passes on both symbols except trade_count (sample size).
    "trailing_stop_enabled": False,
    "breakeven_at_atr_mult": 1.0,   # move stop to breakeven when trade is up 1×ATR
    "trail_from_atr_mult":   2.0,   # start trailing when trade is up 2×ATR
    "trail_stop_atr_mult":   1.0,   # trail by 1×ATR below the peak
    "time_stop_bars":        48,    # exit after 48 × 4H bars (8 days) if no TP/SL
    "dynamic_tp_by_regime":  False,
    "tp_rr_trending":        3.0,   # let winners run in trending regimes (unused while dynamic_tp_by_regime=False)
    "tp_rr_ranging":         1.5,   # take profit earlier in ranging regimes (unused while dynamic_tp_by_regime=False)

    # ── Dynamic risk modes ─────────────────────────────────
    # Risk per trade scales with setup quality and market conditions.
    # Never exceeds max_risk_pct. Never uses leverage.
    "risk_modes": {
        "conservative_pct":      0.50,  # weak setups / uncertain markets
        "balanced_pct":          0.75,  # normal bullish/trending conditions
        "aggressive_pct":        1.25,  # high-quality breakout in strong trend
        "max_risk_pct":          1.50,  # hard cap — never exceed

        # Aggressive mode requires ALL of the following:
        "aggressive_min_score":  80,    # minimum signal score (0-100)
        "aggressive_min_rr":     2.5,   # minimum risk/reward ratio
        "aggressive_strategies": ["breakout"],
        "aggressive_regimes":    ["trending"],
    },
}


# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bot")


# ─────────────────────────────────────────────────────────────
#  TELEGRAM ALERTS
# ─────────────────────────────────────────────────────────────
class TelegramAlerter:
    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)

    def send(self, msg: str):
        if not self.enabled:
            return
        try:
            url     = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = json.dumps({"chat_id": self.chat_id, "text": msg}).encode()
            req     = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            log.warning(f"Telegram: {e}")


# ─────────────────────────────────────────────────────────────
#  FEAR & GREED INDEX  (cached 10 min)
# ─────────────────────────────────────────────────────────────
_fg_cache: dict = {"value": 50, "label": "Neutral", "ts": 0.0}

def fetch_fear_greed() -> tuple[int, str]:
    if time.time() - _fg_cache["ts"] < 600:
        return _fg_cache["value"], _fg_cache["label"]
    try:
        with urllib.request.urlopen("https://api.alternative.me/fng/?limit=1", timeout=5) as r:
            d = json.loads(r.read())["data"][0]
        _fg_cache.update({"value": int(d["value"]), "label": d["value_classification"], "ts": time.time()})
    except Exception:
        pass
    return _fg_cache["value"], _fg_cache["label"]


# ─────────────────────────────────────────────────────────────
#  DATA FETCHER
# ─────────────────────────────────────────────────────────────
def fetch_candles(client: Client, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    raw = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df  = pd.DataFrame(raw, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qav","num_trades","taker_base","taker_quote","ignore",
    ])
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df.iloc[:-1].reset_index(drop=True)  # drop unclosed candle


# ─────────────────────────────────────────────────────────────
#  INDICATORS
# ─────────────────────────────────────────────────────────────
def compute_ma(closes: pd.Series, period: int) -> pd.Series:
    return closes.rolling(period).mean()

def compute_ema(closes: pd.Series, period: int) -> pd.Series:
    return closes.ewm(span=period, adjust=False).mean()

def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    d = closes.diff()
    g = d.clip(lower=0).rolling(period).mean()
    l = (-d.clip(upper=0)).rolling(period).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def compute_bollinger(closes: pd.Series, period: int, std: float):
    m = closes.rolling(period).mean()
    s = closes.rolling(period).std()
    return m + std*s, m, m - std*s

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr       = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr      = tr.rolling(period).mean()
    pdm      = h.diff().clip(lower=0)
    mdm      = (-l.diff()).clip(lower=0)
    pdi      = 100 * pdm.rolling(period).mean() / atr
    mdi      = 100 * mdm.rolling(period).mean() / atr
    dx       = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    return dx.rolling(period).mean()


# ─────────────────────────────────────────────────────────────
#  MARKET REGIME
# ─────────────────────────────────────────────────────────────
def detect_regime(df: pd.DataFrame) -> str:
    adx = compute_adx(df).iloc[-1]
    return "trending" if adx > 25 else ("ranging" if adx < 20 else "neutral")


# ─────────────────────────────────────────────────────────────
#  FEES & SLIPPAGE
# ─────────────────────────────────────────────────────────────
def apply_costs(price: float, side: str, cfg: dict, maker: bool = False) -> float:
    fee = cfg.get("maker_fee_pct", 0.0002) if maker else cfg["fee_pct"]
    c   = fee + cfg["slippage_pct"]
    return price * (1 + c) if side == "buy" else price * (1 - c)


# ─────────────────────────────────────────────────────────────
#  ATR POSITION SIZING
# ─────────────────────────────────────────────────────────────
def atr_position_size(price: float, atr: float, cfg: dict,
                      equity_override: float | None = None,
                      risk_pct_override: float | None = None) -> tuple[float, float, float]:
    eq       = equity_override if equity_override is not None else cfg["equity"]
    risk_pct = risk_pct_override if risk_pct_override is not None else cfg["risk_per_trade_pct"]
    risk     = eq * (risk_pct / 100)
    stop_d   = atr * cfg["atr_stop_multiplier"]
    usdt     = min((risk / stop_d) * price, eq * (cfg["max_position_pct"] / 100))
    return usdt, price - stop_d, price + stop_d * cfg["take_profit_rr"]


# ─────────────────────────────────────────────────────────────
#  HTF TREND
# ─────────────────────────────────────────────────────────────
def htf_trend(client: Client, symbol: str, interval: str) -> str:
    df = fetch_candles(client, symbol, interval, limit=60)
    return "bull" if df["close"].iloc[-1] > compute_ma(df["close"], 50).iloc[-1] else "bear"


# ─────────────────────────────────────────────────────────────
#  STRATEGY SIGNALS
# ─────────────────────────────────────────────────────────────
def signal_ma(df: pd.DataFrame, cfg: dict) -> str | None:
    f = compute_ma(df["close"], cfg["ma_fast"])
    s = compute_ma(df["close"], cfg["ma_slow"])
    if f.iloc[-2] < s.iloc[-2] and f.iloc[-1] > s.iloc[-1]: return "buy"
    if f.iloc[-2] > s.iloc[-2] and f.iloc[-1] < s.iloc[-1]: return "sell"
    return None

def signal_rsi(df: pd.DataFrame, cfg: dict) -> str | None:
    r = compute_rsi(df["close"], cfg["rsi_period"])
    p, c = r.iloc[-2], r.iloc[-1]
    if p < cfg["rsi_oversold"]   and c >= cfg["rsi_oversold"]:   return "buy"
    if p > cfg["rsi_overbought"] and c <= cfg["rsi_overbought"]: return "sell"
    return None

def signal_bollinger(df: pd.DataFrame, cfg: dict) -> str | None:
    u, _, l = compute_bollinger(df["close"], cfg["bb_period"], cfg["bb_std"])
    pr, pv  = df["close"].iloc[-1], df["close"].iloc[-2]
    if pv < l.iloc[-2] and pr >= l.iloc[-1]: return "buy"
    if pv > u.iloc[-2] and pr <= u.iloc[-1]: return "sell"
    return None

def signal_trend_pullback(df: pd.DataFrame, cfg: dict) -> str | None:
    """Buy on a pullback to EMA20/50: RSI reset + strong close + EMA alignment + volume."""
    act      = cfg.get("auto_strategy", {})
    fast_p   = act.get("tp_ema_fast", 20)
    slow_p   = act.get("tp_ema_slow", 50)
    near_pct = act.get("tp_near_pct", 2.5) / 100
    rsi_min  = act.get("tp_rsi_min", 38)
    rsi_max  = act.get("tp_rsi_max", 62)
    vol_fac  = act.get("tp_volume_factor", 0.9)

    ema_fast  = compute_ema(df["close"], fast_p).iloc[-1]
    ema_slow  = compute_ema(df["close"], slow_p).iloc[-1]
    price     = df["close"].iloc[-1]
    rsi_cur   = compute_rsi(df["close"]).iloc[-1]
    vol_avg   = df["volume"].rolling(20).mean().iloc[-1]
    vol_cur   = df["volume"].iloc[-1]
    hi, lo    = df["high"].iloc[-1], df["low"].iloc[-1]

    near_fast   = abs(price - ema_fast) / ema_fast <= near_pct
    near_slow   = abs(price - ema_slow) / ema_slow <= near_pct * 1.2
    at_ema      = near_fast or near_slow
    rsi_ok      = rsi_min <= rsi_cur <= rsi_max
    ema_aligned = ema_fast > ema_slow                         # uptrend: fast above slow
    vol_ok      = vol_avg > 0 and vol_cur >= vol_avg * vol_fac
    above_ema   = price > ema_fast                            # price holding above EMA
    rng         = hi - lo
    strong_cls  = (price - lo) / rng >= 0.40 if rng > 0 else False  # close in top 40% of range

    if at_ema and rsi_ok and ema_aligned and vol_ok and above_ema and strong_cls:
        return "buy"
    return None


def signal_breakout(df: pd.DataFrame, cfg: dict) -> str | None:
    """Buy when price closes clearly above consolidation high with volume + EMA alignment."""
    act       = cfg.get("auto_strategy", {})
    lookback  = act.get("bo_lookback", 20)
    rsi_max   = act.get("bo_rsi_max", 75)
    vol_fac   = act.get("bo_volume_factor", 1.2)

    closes     = df["close"]
    price      = closes.iloc[-1]
    resistance = closes.iloc[-(lookback + 1):-1].max()
    rsi_cur    = compute_rsi(closes).iloc[-1]
    vol_avg    = df["volume"].rolling(20).mean().iloc[-1]
    vol_cur    = df["volume"].iloc[-1]
    ema_fast   = compute_ema(closes, 20).iloc[-1]
    ema_slow   = compute_ema(closes, 50).iloc[-1]

    # Require genuine clearance — not just touching resistance
    broke_out   = price > resistance * 1.001
    rsi_ok      = rsi_cur < rsi_max
    vol_spike   = vol_avg > 0 and vol_cur >= vol_avg * vol_fac
    ema_aligned = ema_fast > ema_slow                         # uptrend confirms breakout

    if broke_out and rsi_ok and vol_spike and ema_aligned:
        return "buy"
    return None


def select_strategy(regime: str, htf_bull: bool, fg_val: int,
                    vol_pct: float, cfg: dict) -> dict:
    """
    Select the best strategy for current conditions based on backtest findings.
    730-day BTCUSDT results informed this logic:
      breakout in trending:       51.6% win, best PF
      trend_pullback in ranging:  57.1% win, solid
      bollinger (any regime):     32.4% win, PF 0.80 — disabled
      neutral/ranging breakout:   poor results — excluded
    Returns {strategy, reason, rejected{name:reason}, evaluated{name:{suitable,reason}}}.
    """
    act           = cfg.get("auto_strategy", {})
    max_vol       = cfg.get("max_volatility_pct", 4.0)
    extreme_greed = fg_val > 85

    # ── Universal blockers ────────────────────────────────────
    if not htf_bull:
        r = "HTF (daily) bearish — no long entries for any strategy"
        return {"strategy": None, "reason": r, "rejected": {"all": r}, "evaluated": {}}
    if vol_pct > max_vol:
        r = f"Volatility {vol_pct:.2f}% > limit {max_vol}% — all strategies paused"
        return {"strategy": None, "reason": r, "rejected": {"all": r}, "evaluated": {}}
    if extreme_greed:
        r = f"Fear & Greed {fg_val} = extreme greed — new longs blocked"
        return {"strategy": None, "reason": r, "rejected": {"all": r}, "evaluated": {}}

    evaluated: dict = {}
    rejected:  dict = {}

    # ── Bollinger: disabled in auto unless explicitly re-enabled ─
    bollinger_in_auto = act.get("bollinger_enabled_in_auto", False)
    if not bollinger_in_auto:
        evaluated["bollinger"] = {"suitable": False,
            "reason": "Disabled in auto-selector — 730d backtest PF 0.80 (needs improvement)"}
        rejected["bollinger"]  = "Backtested PF 0.80 — not used until retested with PF > 1.4"
    elif regime == "ranging":
        evaluated["bollinger"] = {"suitable": True,
            "reason": "Ranging market — Bollinger re-enabled in config"}
    else:
        evaluated["bollinger"] = {"suitable": False,
            "reason": f"Market {regime} — Bollinger needs ranging"}
        rejected["bollinger"]  = f"Regime {regime} — Bollinger requires ranging"

    # ── Trend Pullback: works in trending AND ranging ─────────
    tp_enabled      = act.get("trend_pullback_enabled", True)
    tp_trend_only   = act.get("trend_pullback_trending_only", False)
    if tp_enabled:
        if regime == "trending":
            evaluated["trend_pullback"] = {"suitable": True,
                "reason": "Trending + HTF bull — EMA pullback valid (57% win in ranging, solid in trending)"}
        elif regime == "ranging" and not tp_trend_only:
            evaluated["trend_pullback"] = {"suitable": True,
                "reason": "Ranging market — Trend Pullback valid (backtested 57.1% win rate)"}
        else:
            evaluated["trend_pullback"] = {"suitable": False,
                "reason": f"Neutral regime — insufficient edge in neutral markets"}
            rejected["trend_pullback"]  = "Neutral regime — no trade"
    else:
        rejected["trend_pullback"] = "Disabled in config"

    # ── Breakout: trending only (ranging showed poor results) ──
    bo_enabled      = act.get("breakout_enabled", True)
    bo_trend_only   = act.get("breakout_trending_only", True)
    if bo_enabled:
        if regime == "trending":
            evaluated["breakout"] = {"suitable": True,
                "reason": "Trending + HTF bull — Breakout optimal (51.6% win, best backtested edge)"}
        elif not bo_trend_only:
            evaluated["breakout"] = {"suitable": False,
                "reason": f"Regime {regime} — acceptable but not trending"}
            rejected["breakout"]  = f"Regime {regime} — breakout works best in trending"
        else:
            evaluated["breakout"] = {"suitable": False,
                "reason": f"Regime {regime} — breakout restricted to trending (per backtests)"}
            rejected["breakout"]  = f"Trending-only restriction: regime is {regime} (ranging/neutral showed poor results)"
    else:
        rejected["breakout"] = "Disabled in config"

    # ── Selection: backtest-informed priority ─────────────────
    # 1. Trending → Breakout (primary, best backtested edge)
    if regime == "trending" and evaluated.get("breakout", {}).get("suitable"):
        rej = dict(rejected)
        rej.setdefault("trend_pullback", "Breakout takes priority in trending (higher backtested PF)")
        rej.setdefault("bollinger", rejected.get("bollinger", "Not suitable for trending"))
        return {"strategy": "breakout",
                "reason": "Trending regime + HTF bull — Breakout selected (51.6% win rate backtested)",
                "rejected": rej, "evaluated": evaluated}

    # 2. Trending → Trend Pullback (fallback if breakout disabled)
    if regime == "trending" and evaluated.get("trend_pullback", {}).get("suitable"):
        return {"strategy": "trend_pullback",
                "reason": "Trending regime — Trend Pullback selected (breakout unavailable)",
                "rejected": rejected, "evaluated": evaluated}

    # 3. Ranging → Trend Pullback (57.1% win backtested)
    if regime == "ranging" and evaluated.get("trend_pullback", {}).get("suitable"):
        rej = dict(rejected)
        rej.setdefault("breakout", "Breakout restricted to trending regime — ranging showed poor results")
        return {"strategy": "trend_pullback",
                "reason": "Ranging market — Trend Pullback selected (57.1% win in ranging, backtested)",
                "rejected": rej, "evaluated": evaluated}

    # 4. Ranging → Bollinger (only if re-enabled)
    if regime == "ranging" and evaluated.get("bollinger", {}).get("suitable"):
        return {"strategy": "bollinger",
                "reason": "Ranging market — Bollinger selected (re-enabled in config)",
                "rejected": rejected, "evaluated": evaluated}

    # 5. No trade
    if regime == "neutral":
        reason = "Neutral regime (ADX 20-25) — no strategy has proven edge; waiting for trending or ranging"
    elif regime == "ranging":
        reason = ("Ranging market — Bollinger disabled (PF 0.80), "
                  "set bollinger_enabled_in_auto=True in config to re-enable")
    else:
        reason = f"No suitable strategy: {regime} regime, F&G={fg_val}"
    return {"strategy": None, "reason": reason,
            "rejected": rejected, "evaluated": evaluated}


# ─────────────────────────────────────────────────────────────
#  DYNAMIC RISK MODE
# ─────────────────────────────────────────────────────────────
def get_risk_mode(score: int, strategy: str, regime: str, htf_bull: bool,
                  fg_val: int, rr: float, kill_active: bool,
                  drawdown_pct: float, cfg: dict) -> dict:
    """
    Returns {"mode": str, "risk_pct": float, "reason": str, "reduced": bool}.
    Aggressive only when: breakout + trending + HTF bull + score>=80 + rr>=2.5 + no kill switch.
    Falls back to conservative under kill switch, high drawdown, extreme greed, or weak score.
    """
    rm               = cfg.get("risk_modes", {})
    conservative_pct = rm.get("conservative_pct",      0.50)
    balanced_pct     = rm.get("balanced_pct",           0.75)
    aggressive_pct   = rm.get("aggressive_pct",         1.25)
    max_risk_pct     = rm.get("max_risk_pct",           1.50)
    agg_min_score    = rm.get("aggressive_min_score",   80)
    agg_min_rr       = rm.get("aggressive_min_rr",      2.5)
    agg_strategies   = rm.get("aggressive_strategies",  ["breakout"])
    agg_regimes      = rm.get("aggressive_regimes",     ["trending"])

    # ── Hard blockers → conservative ──────────────────────────
    if kill_active:
        return {"mode": "conservative", "risk_pct": conservative_pct,
                "reason": "Kill switch active — risk reduced to minimum", "reduced": True}
    if not htf_bull:
        return {"mode": "conservative", "risk_pct": conservative_pct,
                "reason": "HTF trend bearish — risk reduced to minimum", "reduced": True}
    if fg_val > 85:
        return {"mode": "conservative", "risk_pct": conservative_pct,
                "reason": f"F&G {fg_val} = extreme greed — risk reduced to minimum", "reduced": True}
    if drawdown_pct >= 10.0:
        return {"mode": "conservative", "risk_pct": conservative_pct,
                "reason": f"Drawdown {drawdown_pct:.1f}% ≥ 10% — risk reduced to minimum", "reduced": True}

    # ── Partial reductions ────────────────────────────────────
    if drawdown_pct >= 5.0:
        return {"mode": "conservative", "risk_pct": conservative_pct,
                "reason": f"Drawdown {drawdown_pct:.1f}% ≥ 5% — reduced to conservative", "reduced": True}
    if score < 60:
        return {"mode": "conservative", "risk_pct": conservative_pct,
                "reason": f"Score {score}/100 < 60 — setup too weak for normal risk", "reduced": True}
    if regime == "neutral":
        return {"mode": "conservative", "risk_pct": conservative_pct,
                "reason": "Neutral regime — low-confidence market, risk reduced", "reduced": True}

    # ── Aggressive mode (all conditions must pass) ────────────
    greed_caution = fg_val > 75
    agg_ok = (strategy in agg_strategies and regime in agg_regimes
              and score >= agg_min_score and rr >= agg_min_rr and not greed_caution)
    if agg_ok:
        return {"mode": "aggressive",
                "risk_pct": min(aggressive_pct, max_risk_pct),
                "reason": (f"Breakout + trending + HTF bull + score {score}/100 ≥ {agg_min_score} "
                           f"+ RR {rr:.1f} ≥ {agg_min_rr} — aggressive risk allowed"),
                "reduced": False}

    # ── Balanced (default) ────────────────────────────────────
    if greed_caution:
        reason = f"F&G {fg_val} in greed zone — balanced risk (not aggressive)"
    elif strategy not in agg_strategies:
        reason = f"Strategy '{strategy}' not in aggressive list — balanced risk"
    elif regime not in agg_regimes:
        reason = f"Regime '{regime}' not trending — balanced risk"
    elif score < agg_min_score:
        reason = f"Score {score}/100 < {agg_min_score} for aggressive mode — balanced risk"
    elif rr < agg_min_rr:
        reason = f"RR {rr:.1f} < {agg_min_rr} min for aggressive — balanced risk"
    else:
        reason = "Balanced conditions — standard risk"
    return {"mode": "balanced", "risk_pct": min(balanced_pct, max_risk_pct),
            "reason": reason, "reduced": greed_caution}


STRATEGY_MAP    = {
    "ma":             signal_ma,
    "rsi":            signal_rsi,
    "bollinger":      signal_bollinger,
    "trend_pullback": signal_trend_pullback,
    "breakout":       signal_breakout,
}
STRATEGY_REGIME = {
    "ma":             "trending",
    "rsi":            "ranging",
    "bollinger":      "ranging",
    "trend_pullback": "trending",
    "breakout":       "trending",
}


# ─────────────────────────────────────────────────────────────
#  PERFORMANCE METRICS
# ─────────────────────────────────────────────────────────────
def performance_metrics(trades: list, initial_balance: float) -> dict:
    if not trades:
        return {}
    pnls   = [t["pnl"] for t in trades]
    r      = pd.Series([p / initial_balance for p in pnls])
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    sharpe = (r.mean() / r.std() * (252 ** 0.5)) if r.std() > 0 else 0
    cumul  = (1 + r).cumprod()
    max_dd = ((cumul - cumul.cummax()) / cumul.cummax()).min() * 100
    return {
        "trades":        len(trades),
        "win_rate":      round(len(wins) / len(trades) * 100, 1),
        "avg_win":       round(sum(wins)   / len(wins),   2) if wins   else 0,
        "avg_loss":      round(sum(losses) / len(losses), 2) if losses else 0,
        "sharpe":        round(sharpe, 2),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else float("inf"),
        "max_drawdown":  round(max_dd, 2),
        "total_pnl":     round(sum(pnls), 2),
    }


# ─────────────────────────────────────────────────────────────
#  SIGNAL SCORE  (0-100)
#  Explains exactly WHY a trade was or wasn't taken.
# ─────────────────────────────────────────────────────────────
def compute_signal_score(df: pd.DataFrame, df_htf: pd.DataFrame,
                          cfg: dict, strategy: str, fg_val: int) -> dict:
    """
    Scores the current setup from 0-100.
    Returns {total, threshold, trade_allowed, label, components, missing, waiting_for}.
    """
    comp     = {}
    missing  = []
    waiting  = []
    score    = 0
    ideal_r  = STRATEGY_REGIME.get(strategy, "ranging")

    price    = df["close"].iloc[-1]
    atr_val  = compute_atr(df).iloc[-1]
    rsi_val  = compute_rsi(df["close"]).iloc[-1]
    vol_avg  = df["volume"].rolling(20).mean().iloc[-1]
    vol_cur  = df["volume"].iloc[-1]
    vol_rat  = vol_cur / vol_avg if vol_avg > 0 else 1.0
    adx_val  = compute_adx(df).iloc[-1]
    regime   = "trending" if adx_val > 25 else ("ranging" if adx_val < 20 else "neutral")

    # ── 1. HTF trend (20 pts) ─────────────────────────────────
    ma50 = compute_ma(df_htf["close"], 50).iloc[-1]
    htf_bull = df_htf["close"].iloc[-1] > ma50
    if htf_bull:
        s, lbl, ok = 20, "Daily trend bullish (price > 50MA)", True
    else:
        s, lbl, ok = 0, "Daily trend bearish — buy blocked", False
        missing.append("HTF (daily) trend bearish: price below 50MA")
        waiting.append("Price to reclaim 50MA on the daily chart")
    comp["htf_trend"] = {"score": s, "max": 20, "label": lbl, "pass": ok}
    score += s

    # ── 2. Market regime alignment (15 pts) ───────────────────
    if regime == ideal_r:
        s, lbl, ok = 15, f"Regime '{regime}' — matches {strategy.upper()}", True
    elif regime == "neutral":
        s, lbl, ok = 8, f"Regime 'neutral' — acceptable", True
    else:
        s, lbl, ok = 0, f"Regime '{regime}' — {strategy.upper()} needs '{ideal_r}'", False
        missing.append(f"Regime mismatch: market is {regime}, strategy needs {ideal_r}")
        if ideal_r == "ranging":
            waiting.append(f"ADX to drop below 20 (currently ~{adx_val:.0f}) — market needs to stop trending")
        else:
            waiting.append(f"ADX to rise above 25 (currently ~{adx_val:.0f}) — need a trending market")
    comp["regime"] = {"score": s, "max": 15, "label": lbl, "pass": ok}
    score += s

    # ── 3. Strategy signal (15 pts) ───────────────────────────
    fn  = STRATEGY_MAP.get(strategy)
    raw = fn(df, cfg) if fn else None
    if raw == "buy":
        s, lbl, ok = 15, f"{strategy.upper()} buy signal confirmed", True
    elif raw == "sell":
        s, lbl, ok = 0, f"{strategy.upper()} shows SELL — no long entry", False
        missing.append(f"{strategy.upper()} is currently showing a sell signal — not safe to buy")
    else:
        s, lbl, ok = 0, f"No {strategy.upper()} signal yet", False
        missing.append(f"No {strategy.upper()} entry signal detected")
        act = cfg.get("auto_strategy", {})
        if strategy == "bollinger":
            _, _, lb = compute_bollinger(df["close"], cfg["bb_period"], cfg["bb_std"])
            dist = (price - lb.iloc[-1]) / lb.iloc[-1] * 100
            waiting.append(f"Price to reach lower Bollinger Band ({dist:+.1f}% away)")
        elif strategy == "rsi":
            waiting.append(f"RSI to cross oversold level 30 (currently {rsi_val:.0f})")
        elif strategy == "ma":
            fm = compute_ma(df["close"], cfg["ma_fast"]).iloc[-1]
            sm = compute_ma(df["close"], cfg["ma_slow"]).iloc[-1]
            waiting.append(f"Fast MA({cfg['ma_fast']}) to cross above slow MA({cfg['ma_slow']}) (gap {(fm-sm)/sm*100:+.2f}%)")
        elif strategy == "trend_pullback":
            ema20 = compute_ema(df["close"], act.get("tp_ema_fast", 20)).iloc[-1]
            ema50 = compute_ema(df["close"], act.get("tp_ema_slow", 50)).iloc[-1]
            near  = act.get("tp_near_pct", 2.5)
            rsi_min = act.get("tp_rsi_min", 38)
            rsi_max = act.get("tp_rsi_max", 62)
            dist20  = (price - ema20) / ema20 * 100
            dist50  = (price - ema50) / ema50 * 100
            waiting.append(f"Pullback to EMA20 ({ema20:.0f}, {dist20:+.1f}% away) or EMA50 ({ema50:.0f}, {dist50:+.1f}% away) within ±{near}%")
            if not (rsi_min <= rsi_val <= rsi_max):
                waiting.append(f"RSI to reset into {rsi_min}-{rsi_max} zone (currently {rsi_val:.0f})")
            hi_c = df["high"].iloc[-1]; lo_c = df["low"].iloc[-1]; cl_c = df["close"].iloc[-1]
            rng_c = hi_c - lo_c
            if rng_c > 0 and (cl_c - lo_c) / rng_c < 0.40:
                waiting.append("Strong candle close required: price must close in top 40% of bar range")
            ema20_c = compute_ema(df["close"], act.get("tp_ema_fast", 20)).iloc[-1]
            ema50_c = compute_ema(df["close"], act.get("tp_ema_slow", 50)).iloc[-1]
            if ema20_c <= ema50_c:
                waiting.append(f"EMA20 ({ema20_c:.0f}) must be above EMA50 ({ema50_c:.0f}) for trend alignment")
        elif strategy == "breakout":
            lookback   = act.get("bo_lookback", 20)
            resistance = df["close"].iloc[-(lookback + 1):-1].max()
            dist_pct   = (resistance - price) / price * 100
            vol_fac    = act.get("bo_volume_factor", 1.2)
            vol_avg    = df["volume"].rolling(20).mean().iloc[-1]
            vol_ratio  = df["volume"].iloc[-1] / vol_avg if vol_avg > 0 else 1.0
            if price <= resistance * 1.001:
                waiting.append(f"Price to close >0.1% above resistance {resistance:.0f} ({dist_pct:+.1f}% away)")
            if vol_ratio < vol_fac:
                waiting.append(f"Volume spike required for breakout ({vol_ratio:.1f}x avg, need {vol_fac}x)")
            ema20_bo = compute_ema(df["close"], 20).iloc[-1]
            ema50_bo = compute_ema(df["close"], 50).iloc[-1]
            if ema20_bo <= ema50_bo:
                waiting.append(f"EMA20 ({ema20_bo:.0f}) must be above EMA50 ({ema50_bo:.0f}) for trend alignment")
    comp["signal"] = {"score": s, "max": 15, "label": lbl, "pass": ok}
    score += s

    # ── 4. RSI zone (15 pts) ──────────────────────────────────
    if rsi_val < 30:
        s, lbl, ok = 12, f"RSI {rsi_val:.0f} — oversold, reversal zone", True
    elif rsi_val <= 65:
        s, lbl, ok = 15, f"RSI {rsi_val:.0f} — healthy buy zone (30-65)", True
    elif rsi_val <= 75:
        s, lbl, ok = 5, f"RSI {rsi_val:.0f} — elevated, prefer below 65", False
        missing.append(f"RSI {rsi_val:.0f} slightly high — wait for reset below 65")
    else:
        s, lbl, ok = 0, f"RSI {rsi_val:.0f} — overbought, avoid buying", False
        missing.append(f"RSI {rsi_val:.0f} overbought — wait for reset below 65")
        waiting.append(f"RSI to cool below 65 (currently {rsi_val:.0f})")
    comp["rsi"] = {"score": s, "max": 15, "label": lbl, "pass": ok}
    score += s

    # ── 5. Volume confirmation (15 pts) ───────────────────────
    if vol_rat >= 1.2:
        s, lbl, ok = 15, f"Volume {vol_rat:.1f}x avg — strong confirmation", True
    elif vol_rat >= 1.0:
        s, lbl, ok = 12, f"Volume {vol_rat:.1f}x avg — adequate", True
    elif vol_rat >= 0.7:
        s, lbl, ok = 6, f"Volume {vol_rat:.1f}x avg — below average", False
        missing.append(f"Volume low ({vol_rat:.1f}x avg) — prefer ≥ 1.0x for confirmation")
    else:
        s, lbl, ok = 0, f"Volume {vol_rat:.1f}x avg — very low", False
        missing.append(f"Volume very low ({vol_rat:.1f}x) — no confirmation")
        waiting.append(f"Volume to pick up above {vol_avg:.0f}")
    comp["volume"] = {"score": s, "max": 15, "label": lbl, "pass": ok}
    score += s

    # ── 6. Volatility (10 pts) ────────────────────────────────
    vol_pct = (atr_val / price) * 100
    max_v   = cfg.get("max_volatility_pct", 4.0)
    if vol_pct <= max_v * 0.5:
        s, lbl, ok = 10, f"ATR/price {vol_pct:.2f}% — calm, good", True
    elif vol_pct <= max_v:
        s, lbl, ok = int(10 * (max_v - vol_pct) / (max_v * 0.5)), f"ATR/price {vol_pct:.2f}% — elevated", True
    else:
        s, lbl, ok = 0, f"ATR/price {vol_pct:.2f}% — too volatile (limit {max_v}%)", False
        missing.append(f"Volatility too high ({vol_pct:.2f}%) — bot will skip")
        waiting.append(f"ATR/price to drop below {max_v}% (currently {vol_pct:.2f}%)")
    comp["volatility"] = {"score": s, "max": 10, "label": lbl, "pass": ok}
    score += s

    # ── 7. Fear & Greed (10 pts) ──────────────────────────────
    if 20 <= fg_val <= 70:
        s, lbl, ok = 10, f"F&G {fg_val} — favorable zone", True
    elif fg_val < 20:
        s, lbl, ok = 7, f"F&G {fg_val} — extreme fear (cautious optimism)", True
    elif fg_val <= 85:
        s, lbl, ok = 5, f"F&G {fg_val} — high greed, correction risk", False
        missing.append(f"Fear & Greed {fg_val} in greed territory — elevated reversal risk")
    else:
        s, lbl, ok = 0, f"F&G {fg_val} — extreme greed, avoid new longs", False
        missing.append(f"Fear & Greed {fg_val} = extreme greed — historically risky")
        waiting.append(f"F&G to cool below 85 (currently {fg_val})")
    comp["fear_greed"] = {"score": s, "max": 10, "label": lbl, "pass": ok}
    score += s

    # ── Final assessment ──────────────────────────────────────
    threshold    = cfg.get("score_threshold", 70)
    trade_allowed = score >= threshold and raw == "buy" and htf_bull

    if score >= 85:   label = "Strong setup"
    elif score >= 75: label = "Valid setup"
    elif score >= 60: label = "Setup forming"
    elif score >= 40: label = "Weak setup"
    else:             label = "No setup"

    return {
        "total":            score,
        "threshold":        threshold,
        "trade_allowed":    trade_allowed,
        "label":            label,
        "components":       comp,
        "missing":          missing,
        "waiting_for":      waiting,
        "raw_signal":       raw,
        "regime":           regime,
        "adx":              round(adx_val, 1),
        "htf_bull":         htf_bull,
        "ideal_regime":     ideal_r,
        "active_strategy":  strategy,
    }


def get_strategy_state(score_data: dict, in_position: bool, kill_active: bool,
                        strategy_selection: dict | None = None) -> str:
    if kill_active:
        return "Kill switch active — trading paused"
    if in_position:
        return "Position active — monitoring stop / TP"
    # If auto-selector found no suitable strategy, explain why
    if strategy_selection and strategy_selection.get("strategy") is None:
        return strategy_selection.get("reason", "No suitable strategy for current conditions")
    if not score_data.get("htf_bull", True):
        return "HTF trend bearish — buy entries blocked"
    strategy = score_data.get("active_strategy", "bollinger")
    regime   = score_data.get("regime", "neutral")
    ideal    = score_data.get("ideal_regime", "ranging")
    if regime != ideal and regime != "neutral":
        adx = score_data.get("adx", 0)
        return f"Regime unsuitable — market is {regime} (ADX {adx:.0f}), {strategy} needs {ideal}"
    raw = score_data.get("raw_signal")
    tot = score_data.get("total", 0)
    thr = score_data.get("threshold", 70)
    if raw == "buy" and tot >= thr:
        return "Entry confirmed — executing"
    if raw == "buy":
        return f"Buy signal but score too low ({tot}/{thr}) — waiting"
    # Strategy-specific waiting states
    if strategy == "trend_pullback":
        if tot >= 60:
            return "Trend Pullback forming — waiting for EMA touch + RSI reset"
        return "Trending market — watching for pullback to EMA20/50"
    if strategy == "breakout":
        if tot >= 60:
            return "Breakout forming — watching for resistance close + volume"
        return "Trending market — scanning for breakout above consolidation"
    if tot >= 75:
        return "Conditions good — watching for entry signal"
    if tot >= 60:
        return "Setup forming — most conditions met"
    if tot >= 40:
        return "Weak setup — waiting for better conditions"
    return "No valid setup detected"


# ─────────────────────────────────────────────────────────────
#  SKIP ANALYTICS TRACKER
# ─────────────────────────────────────────────────────────────
class SkipTracker:
    REASONS = [
        "trend_filter", "regime_filter", "signal_filter", "rsi_filter",
        "volume_filter", "volatility_filter", "fear_greed_filter",
        "score_threshold", "kill_switch", "in_position", "no_setup",
    ]

    def __init__(self, path: str, symbols: list):
        self.path = path
        self.data = self._load(symbols)

    def _load(self, symbols: list) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        for sym in symbols:
            if sym not in d:
                d[sym] = {"total_scans": 0, "valid_setups": 0, "trades_opened": 0,
                           "skips": {r: 0 for r in self.REASONS}}
        return d

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
        except Exception as e:
            log.warning(f"SkipTracker save failed: {e}")

    def scan(self, symbol: str):
        if symbol in self.data:
            self.data[symbol]["total_scans"] += 1

    def skip(self, symbol: str, reason: str):
        if symbol in self.data and reason in self.REASONS:
            self.data[symbol]["skips"][reason] += 1
        self._save()

    def valid_setup(self, symbol: str):
        if symbol in self.data:
            self.data[symbol]["valid_setups"] += 1
        self._save()

    def trade_opened(self, symbol: str):
        if symbol in self.data:
            self.data[symbol]["trades_opened"] += 1
        self._save()

    def most_common_skip(self, symbol: str) -> str:
        if symbol not in self.data:
            return "—"
        skips = self.data[symbol]["skips"]
        if not any(skips.values()):
            return "None yet"
        return max(skips, key=skips.get).replace("_", " ")

    def summary(self, symbol: str) -> dict:
        if symbol not in self.data:
            return {}
        d = self.data[symbol]
        return {
            "total_scans":       d["total_scans"],
            "valid_setups":      d["valid_setups"],
            "trades_opened":     d["trades_opened"],
            "most_common_skip":  self.most_common_skip(symbol),
            "skips":             d["skips"],
        }


# ─────────────────────────────────────────────────────────────
#  TRADER STATE PERSISTENCE
#  Survives restarts: an open position or a resting live order is not
#  silently forgotten just because the process (re)started. Saved once per
#  main-loop iteration after all symbols are processed — worst case on an
#  ungraceful crash is losing one poll interval's worth of state, versus
#  losing it entirely on every restart (the prior behavior).
# ─────────────────────────────────────────────────────────────
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trader_state.json")

def _ser_position(pos):
    if not pos:
        return None
    d = dict(pos)
    if isinstance(d.get("entry_ts"), datetime):
        d["entry_ts"] = d["entry_ts"].isoformat()
    return d

def _deser_position(d):
    if not d:
        return None
    d = dict(d)
    if d.get("entry_ts"):
        d["entry_ts"] = datetime.fromisoformat(d["entry_ts"])
    return d

def _ser_pending_order(po):
    if not po:
        return None
    d = dict(po)
    if isinstance(d.get("expires_at"), datetime):
        d["expires_at"] = d["expires_at"].isoformat()
    return d

def _deser_pending_order(d):
    if not d:
        return None
    d = dict(d)
    if d.get("expires_at"):
        d["expires_at"] = datetime.fromisoformat(d["expires_at"])
    return d

PAPER_PENDING_KEY = "_paper_pending_limit_orders"   # not a valid symbol, safe to use as a sentinel key

def save_trader_state(traders: dict, pending_limit_orders: dict | None = None):
    try:
        data = {}
        for sym, t in traders.items():
            data[sym] = {
                "position":      _ser_position(t.position),
                "pending_order": _ser_pending_order(getattr(t, "pending_order", None)),
                "balance":       t.balance,
                "peak_bal":      t.peak_bal,
                "initial_bal":   t.initial_bal,
                "daily_pnl":     t.daily_pnl,
                "weekly_pnl":    t.weekly_pnl,
                "daily_reset":   t.daily_reset.isoformat(),
                "weekly_reset":  t.weekly_reset,
                "trades":        t.trades,
            }
        if pending_limit_orders:
            data[PAPER_PENDING_KEY] = {sym: _ser_pending_order(plo)
                                        for sym, plo in pending_limit_orders.items()}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)   # atomic — never leaves a half-written state file
    except Exception as e:
        log.warning(f"Trader state save failed: {e}")

def load_all_trader_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def restore_trader(trader, saved: dict, sym: str):
    if not saved:
        return
    trader.position = _deser_position(saved.get("position"))
    if hasattr(trader, "pending_order"):
        trader.pending_order = _deser_pending_order(saved.get("pending_order"))
    trader.balance     = saved.get("balance",     trader.balance)
    trader.peak_bal     = saved.get("peak_bal",    trader.peak_bal)
    trader.initial_bal  = saved.get("initial_bal", trader.initial_bal)
    trader.daily_pnl    = saved.get("daily_pnl",   0.0)
    trader.weekly_pnl   = saved.get("weekly_pnl",  0.0)
    if saved.get("daily_reset"):
        trader.daily_reset = date.fromisoformat(saved["daily_reset"])
    trader.weekly_reset = saved.get("weekly_reset", trader.weekly_reset)
    trader.trades = saved.get("trades", [])
    if trader.position:
        log.info(f"[{sym}] Restored open position from disk: entry={trader.position.get('entry')} "
                 f"qty={trader.position.get('qty')}")
    if getattr(trader, "pending_order", None):
        log.info(f"[{sym}] Restored pending order from disk: order_id="
                 f"{trader.pending_order.get('order_id')}")

def restore_pending_limit_orders(saved_state: dict) -> dict:
    """Paper-mode resting limit orders live in run()'s module-level
    `pending_limit_orders` dict, not on a trader object, so they need their
    own restore path alongside restore_trader() (same disk write, different
    part of the state file — see PAPER_PENDING_KEY)."""
    raw = saved_state.get(PAPER_PENDING_KEY) or {}
    restored = {sym: _deser_pending_order(plo) for sym, plo in raw.items()}
    for sym, plo in restored.items():
        log.info(f"[{sym}] Restored pending paper limit order from disk: "
                 f"price={plo.get('price')}")
    return restored


# ─────────────────────────────────────────────────────────────
#  ACTIVE STRATEGY ENGINE  (1H Pullback to EMA)
# ─────────────────────────────────────────────────────────────
class ActiveStrategyEngine:
    def __init__(self, act_cfg: dict, paper_balance: float):
        self.cfg     = act_cfg
        self.enabled = act_cfg.get("enabled", False)
        self.trader  = PaperTrader(paper_balance) if self.enabled else None
        self.positions: dict = {}   # symbol → position dict
        self.open_count = 0

    def analyze(self, client: Client, symbol: str, _price: float,
                 _cfg: dict, fg_val: int) -> dict:
        if not self.enabled:
            return {"enabled": False, "state": "Disabled — set active_strategy.enabled=True to enable"}

        max_pos = self.cfg.get("max_open_positions", 2)
        if self.open_count >= max_pos and symbol not in self.positions:
            return {"enabled": True, "state": f"Max positions reached ({max_pos}) — waiting for exit"}

        try:
            df_1h  = fetch_candles(client, symbol, self.cfg.get("entry_interval", "1h"), limit=100)
            df_4h  = fetch_candles(client, symbol, self.cfg.get("trend_interval", "4h"), limit=200)
        except Exception as e:
            return {"enabled": True, "state": f"Data error: {e}"}

        ef = self.cfg.get("ema_fast", 20)
        es = self.cfg.get("ema_slow", 50)
        el = self.cfg.get("ema_long", 200)

        # 4H EMA stack
        ema50_4h  = compute_ema(df_4h["close"], es).iloc[-1]
        ema200_4h = compute_ema(df_4h["close"], el).iloc[-1]
        p4h       = df_4h["close"].iloc[-1]
        bull_4h   = p4h > ema200_4h and ema50_4h > ema200_4h

        # 1H indicators
        ema20_1h = compute_ema(df_1h["close"], ef).iloc[-1]
        ema50_1h = compute_ema(df_1h["close"], es).iloc[-1]
        rsi_1h   = compute_rsi(df_1h["close"]).iloc[-1]
        vol_avg  = df_1h["volume"].rolling(20).mean().iloc[-1]
        vol_rat  = df_1h["volume"].iloc[-1] / vol_avg if vol_avg > 0 else 1.0
        p1h      = df_1h["close"].iloc[-1]
        p1h_prev = df_1h["close"].iloc[-2]
        atr_1h   = compute_atr(df_1h).iloc[-1]

        comp    = {}
        missing = []
        waiting = []
        s       = 0

        # 4H EMA stack bullish (25 pts)
        if bull_4h:
            comp["4h_ema"] = {"score": 25, "max": 25, "label": f"4H EMA stack bullish (p>{ema200_4h:.0f}, 50>{200})", "pass": True}
            s += 25
        else:
            comp["4h_ema"] = {"score": 0, "max": 25, "label": "4H EMA stack not bullish", "pass": False}
            missing.append("4H trend: price needs to be above 200 EMA and 50 EMA > 200 EMA")
            waiting.append("4H price above 200 EMA and 50 EMA > 200 EMA")

        # 1H pullback to EMA 20 or 50 (20 pts)
        near20    = abs(p1h - ema20_1h) / ema20_1h < 0.006
        near50    = abs(p1h - ema50_1h) / ema50_1h < 0.009
        bull_cls  = p1h > p1h_prev
        if (near20 or near50) and bull_cls:
            lbl = f"1H pullback to {'20' if near20 else '50'} EMA confirmed with bullish close"
            comp["pullback"] = {"score": 20, "max": 20, "label": lbl, "pass": True}
            s += 20
        elif near20 or near50:
            lbl = f"Near {'20' if near20 else '50'} EMA — waiting for bullish close"
            comp["pullback"] = {"score": 8, "max": 20, "label": lbl, "pass": False}
            s += 8
            waiting.append(f"Bullish 1H candle close above {'20' if near20 else '50'} EMA")
        else:
            d20 = (p1h - ema20_1h) / ema20_1h * 100
            d50 = (p1h - ema50_1h) / ema50_1h * 100
            comp["pullback"] = {"score": 0, "max": 20, "label": f"No pullback: {d20:+.1f}% from 20 EMA, {d50:+.1f}% from 50 EMA", "pass": False}
            missing.append(f"No 1H pullback: {d20:+.1f}% from 20 EMA, {d50:+.1f}% from 50 EMA")
            waiting.append(f"Price to pull back to 1H 20 EMA ({d20:+.1f}% away)")

        # RSI pullback zone (20 pts)
        rmin, rmax = self.cfg.get("rsi_min_buy", 38), self.cfg.get("rsi_max_buy", 62)
        if rmin <= rsi_1h <= rmax:
            comp["rsi"] = {"score": 20, "max": 20, "label": f"1H RSI {rsi_1h:.0f} — pullback zone {rmin}-{rmax}", "pass": True}
            s += 20
        elif rsi_1h < rmin:
            comp["rsi"] = {"score": 8, "max": 20, "label": f"1H RSI {rsi_1h:.0f} — below zone (need {rmin}-{rmax})", "pass": False}
            s += 8
            missing.append(f"1H RSI {rsi_1h:.0f} too low — need {rmin}-{rmax}")
        else:
            comp["rsi"] = {"score": 0, "max": 20, "label": f"1H RSI {rsi_1h:.0f} — overbought, no entry", "pass": False}
            missing.append(f"1H RSI {rsi_1h:.0f} too high — wait for reset below {rmax}")
            waiting.append(f"1H RSI to cool to {rmin}-{rmax} (currently {rsi_1h:.0f})")

        # Volume (15 pts)
        vf = self.cfg.get("volume_factor", 0.9)
        if vol_rat >= vf:
            comp["volume"] = {"score": 15, "max": 15, "label": f"1H volume {vol_rat:.1f}x avg", "pass": True}
            s += 15
        else:
            vs = int(15 * vol_rat)
            comp["volume"] = {"score": vs, "max": 15, "label": f"1H volume {vol_rat:.1f}x avg (need {vf}x)", "pass": False}
            s += vs
            missing.append(f"1H volume {vol_rat:.1f}x below {vf}x threshold")

        # Candle quality (10 pts)
        rng  = df_1h["high"].iloc[-1] - df_1h["low"].iloc[-1]
        body = abs(df_1h["close"].iloc[-1] - df_1h["open"].iloc[-1])
        br   = body / rng if rng > 0 else 0
        bull_c = df_1h["close"].iloc[-1] > df_1h["open"].iloc[-1] and br > 0.4
        if bull_c:
            comp["candle"] = {"score": 10, "max": 10, "label": f"Strong bullish 1H candle (body {br:.0%})", "pass": True}
            s += 10
        elif df_1h["close"].iloc[-1] > df_1h["open"].iloc[-1]:
            comp["candle"] = {"score": 5, "max": 10, "label": f"Weak bullish candle (body {br:.0%})", "pass": False}
            s += 5
        else:
            comp["candle"] = {"score": 0, "max": 10, "label": "Bearish candle — wait for bullish close", "pass": False}
            missing.append("Last 1H candle is bearish — wait for bullish close")

        # Fear & Greed (10 pts)
        if fg_val <= 75:
            comp["fear_greed"] = {"score": 10, "max": 10, "label": f"F&G {fg_val} — acceptable", "pass": True}
            s += 10
        else:
            comp["fear_greed"] = {"score": 5, "max": 10, "label": f"F&G {fg_val} — high greed, caution", "pass": False}
            s += 5

        # R:R
        stop_d  = atr_1h * 1.0
        min_rr  = self.cfg.get("min_rr", 2.0)
        rr      = (p1h + stop_d * min_rr - p1h) / stop_d if stop_d > 0 else min_rr
        rr_ok   = rr >= min_rr
        stop_p  = p1h - stop_d
        tp1_p   = p1h + stop_d * self.cfg.get("tp1_r", 1.5)
        tp2_p   = p1h + stop_d * self.cfg.get("tp2_r", 2.5)

        if not rr_ok:
            missing.append(f"Risk/reward {rr:.1f} below minimum {min_rr}")

        thr          = self.cfg.get("min_signal_score", 75)
        in_pos       = bool(self.positions.get(symbol))
        trade_allowed = s >= thr and bull_4h and (near20 or near50) and bull_cls and rr_ok and not in_pos

        # State
        if in_pos:
            state = "Position active"
        elif trade_allowed:
            state = "Entry confirmed — ready to paper long"
        elif bull_4h and s >= 60:
            state = "Setup forming — watching 1H pullback to EMA"
        elif bull_4h:
            state = "4H bullish — waiting for 1H pullback to EMA"
        else:
            state = "4H trend not strong enough for active entries"

        return {
            "enabled":       True,
            "state":         state,
            "score":         s,
            "threshold":     thr,
            "trade_allowed": trade_allowed,
            "components":    comp,
            "missing":       missing,
            "waiting_for":   waiting,
            "stop":          round(stop_p, 4),
            "tp1":           round(tp1_p, 4),
            "tp2":           round(tp2_p, 4),
            "rr":            round(rr, 2),
            "timeframe":     self.cfg.get("entry_interval", "1h"),
            "position":      self.positions.get(symbol),
        }

    def execute(self, symbol: str, price: float, analysis: dict, cfg: dict):
        if not self.enabled or not self.trader:
            return None
        if not analysis.get("trade_allowed") or self.positions.get(symbol):
            return None

        stop = analysis["stop"]
        tp2  = analysis["tp2"]
        tp1  = analysis["tp1"]
        usdt = cfg["equity"] * (self.cfg.get("risk_per_trade_pct", 0.5) / 100)
        usdt = min(usdt, cfg["equity"] * 0.10)

        self.trader.buy(symbol, price, usdt, stop, tp2, cfg)
        self.positions[symbol] = {
            "entry":   price,
            "stop":    stop,
            "tp1":     tp1,
            "tp2":     tp2,
            "usdt":    usdt,
            "tp1_hit": False,
        }
        self.open_count += 1
        return usdt

    def check_exits(self, symbol: str, price: float, cfg: dict) -> str | None:
        pos = self.positions.get(symbol)
        if not pos or not self.trader:
            return None
        if price <= pos["stop"]:
            self.trader.sell(price, reason="ACTIVE-STOP-LOSS", cfg=cfg)
            del self.positions[symbol]
            self.open_count = max(0, self.open_count - 1)
            return "stop"
        if not pos["tp1_hit"] and price >= pos["tp1"]:
            pos["tp1_hit"] = True
            pos["stop"]    = pos["entry"]    # move stop to breakeven
            log.info(f"🎯  ACTIVE TP1 {symbol} @ {price:.2f} — stop → breakeven {pos['entry']:.2f}")
            return "tp1"
        if price >= pos["tp2"]:
            self.trader.sell(price, reason="ACTIVE-TAKE-PROFIT", cfg=cfg)
            del self.positions[symbol]
            self.open_count = max(0, self.open_count - 1)
            return "tp2"
        return None

    def get_summary(self) -> dict:
        if not self.enabled or not self.trader:
            return {"enabled": False}
        m = performance_metrics(self.trader.trades, self.trader.initial_bal)
        return {
            "enabled":         True,
            "open_positions":  self.open_count,
            "trades":          len(self.trader.trades),
            "balance":         round(self.trader.balance, 2),
            "start_balance":   round(self.trader.initial_bal, 2),
            "performance":     m,
        }


# ─────────────────────────────────────────────────────────────
#  DASHBOARD JSON
# ─────────────────────────────────────────────────────────────
def write_dashboard_json(symbol_data: list, fg_val: int, fg_label: str,
                          iteration: int, live: bool, kill_status: dict,
                          last_alert: str, skip_tracker: SkipTracker,
                          active_engine: ActiveStrategyEngine):
    total_bal   = sum(d["balance"]       for d in symbol_data)
    total_start = sum(d["start_balance"] for d in symbol_data)
    skip_summary = {sym: skip_tracker.summary(sym) for sym in [d["symbol"] for d in symbol_data]}

    # Read live readiness from backtest_results.json if available
    live_readiness: dict = {}
    try:
        bt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_results.json")
        if os.path.exists(bt_path):
            with open(bt_path, encoding="utf-8") as f:
                bt_data = json.load(f)
            live_readiness = bt_data.get("live_readiness", {})
    except Exception:
        pass

    data = {
        "updated_at":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "iteration":       iteration,
        "mode":            "LIVE" if live else "PAPER",
        "fg_value":        fg_val,
        "fg_label":        fg_label,
        "total_balance":   round(total_bal,   2),
        "total_start":     round(total_start, 2),
        "kill_switches":   kill_status,
        "last_alert":      last_alert,
        "skip_analytics":  skip_summary,
        "active_summary":  active_engine.get_summary(),
        "live_readiness":  live_readiness,
        "symbols":         symbol_data,
    }

    try:
        with open("dashboard.json", "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
    except Exception as e:
        log.warning(f"dashboard.json write failed: {e}")


def _build_kill_status(trader, emergency, daily, weekly, drawdown, volatility) -> dict:
    return {
        "emergency_stop": emergency,
        "daily_limit":    daily,
        "weekly_limit":   weekly,
        "max_drawdown":   drawdown,
        "volatility":     volatility,
        "drawdown_pct":   getattr(trader, "drawdown_pct", lambda: 0.0)(),
        "daily_pnl":      round(getattr(trader, "daily_pnl",  0.0), 2),
        "weekly_pnl":     round(getattr(trader, "weekly_pnl", 0.0), 2),
    }


# ─────────────────────────────────────────────────────────────
#  PAPER TRADER
# ─────────────────────────────────────────────────────────────
class PaperTrader:
    def __init__(self, balance: float):
        self.balance      = balance
        self.initial_bal  = balance
        self.peak_bal     = balance
        self.position     = None
        self.trades: list = []
        self.daily_pnl    = 0.0
        self.weekly_pnl   = 0.0
        self.daily_reset  = datetime.now(timezone.utc).date()
        self.weekly_reset = datetime.now(timezone.utc).isocalendar()[1]

    def _check_resets(self):
        today = datetime.now(timezone.utc).date()
        week  = datetime.now(timezone.utc).isocalendar()[1]
        if today != self.daily_reset:
            self.daily_pnl  = 0.0
            self.daily_reset = today
        if week != self.weekly_reset:
            self.weekly_pnl  = 0.0
            self.weekly_reset = week

    def buy(self, symbol, price, usdt, stop, tp, cfg, atr=None):
        if self.position:
            log.warning("Already in position — skipping buy.")
            return
        fill          = apply_costs(price, "buy", cfg)
        qty           = usdt / fill
        self.balance -= usdt
        self.position = {"symbol": symbol, "entry": fill, "qty": qty,
                          "usdt": usdt, "stop": stop, "tp": tp,
                          "atr_entry": atr, "peak": fill,
                          "entry_ts": datetime.now(timezone.utc)}
        log.info(f"📈  PAPER BUY   {qty:.6f} {symbol} @ {fill:.2f}  "
                 f"stop={stop:.2f}  tp={tp:.2f}  size={usdt:.2f}  balance={self.balance:.2f}")

    def buy_limit(self, symbol, limit_price, usdt, stop, tp, cfg, atr=None):
        if self.position:
            log.warning("Already in position — skipping limit buy fill.")
            return
        fill          = apply_costs(limit_price, "buy", cfg, maker=True)
        qty           = usdt / fill
        self.balance -= usdt
        self.position = {"symbol": symbol, "entry": fill, "qty": qty,
                          "usdt": usdt, "stop": stop, "tp": tp,
                          "atr_entry": atr, "peak": fill,
                          "entry_ts": datetime.now(timezone.utc)}
        log.info(f"📈  LIMIT FILL  {qty:.6f} {symbol} @ {fill:.2f}  "
                 f"stop={stop:.2f}  tp={tp:.2f}  size={usdt:.2f}  balance={self.balance:.2f}")

    def sell(self, price, reason="signal", cfg={}) -> float | None:
        if not self.position:
            return None
        fill  = apply_costs(price, "sell", cfg)
        pnl   = (fill - self.position["entry"]) / self.position["entry"] * self.position["usdt"]
        self.balance  += self.position["usdt"] + pnl
        self.peak_bal  = max(self.peak_bal, self.balance)
        self._check_resets()
        self.daily_pnl  += pnl
        self.weekly_pnl += pnl
        sign = "+" if pnl >= 0 else ""
        log.info(f"📉  PAPER SELL  {self.position['qty']:.6f} {self.position['symbol']} "
                 f"@ {fill:.2f}  PnL {sign}{pnl:.2f}  [{reason}]  Balance {self.balance:.2f}")
        self.trades.append({
            "symbol": self.position["symbol"], "entry": self.position["entry"],
            "exit": fill, "qty": self.position["qty"], "usdt": self.position["usdt"],
            "stop": self.position["stop"], "tp": self.position["tp"],
            "pnl": round(pnl, 4), "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        self.position = None
        return pnl

    def check_stops(self, price, cfg, candle_high=None) -> float | None:
        if not self.position:
            return None
        pos = self.position

        # ── Update peak ───────────────────────────────────────
        hi = candle_high if candle_high is not None else price
        pos["peak"] = max(pos.get("peak", pos["entry"]), hi)

        # ── Trailing stop ─────────────────────────────────────
        if cfg.get("trailing_stop_enabled", True):
            atr_e = pos.get("atr_entry") or 0
            if atr_e > 0:
                peak  = pos["peak"]
                entry = pos["entry"]
                be_at    = entry + atr_e * cfg.get("breakeven_at_atr_mult", 1.0)
                trail_at = entry + atr_e * cfg.get("trail_from_atr_mult",   2.0)
                if peak >= trail_at:
                    new_stop = peak - atr_e * cfg.get("trail_stop_atr_mult", 1.0)
                    if new_stop > pos["stop"]:
                        log.info(f"[{pos['symbol']}] Trail stop: {pos['stop']:.2f} → {new_stop:.2f}")
                        pos["stop"] = round(new_stop, 4)
                elif peak >= be_at and entry > pos["stop"]:
                    log.info(f"[{pos['symbol']}] Stop to breakeven: {pos['stop']:.2f} → {entry:.2f}")
                    pos["stop"] = round(entry, 4)

        # ── Time-stop ─────────────────────────────────────────
        time_stop_bars = cfg.get("time_stop_bars", 0)
        if time_stop_bars > 0 and "entry_ts" in pos:
            bars_held = (datetime.now(timezone.utc) - pos["entry_ts"]).total_seconds() / (4 * 3600)
            if bars_held >= time_stop_bars:
                return self.sell(price, reason="TIME-STOP", cfg=cfg)

        if price <= pos["stop"]:
            return self.sell(price, reason="STOP-LOSS", cfg=cfg)
        if price >= pos["tp"]:
            return self.sell(price, reason="TAKE-PROFIT", cfg=cfg)
        return None

    def check_daily_limit(self, cfg) -> bool:
        self._check_resets()
        limit = cfg["equity"] * (cfg["max_daily_loss_pct"] / 100)
        if self.daily_pnl < -limit:
            log.warning(f"⛔  Daily loss limit hit ({self.daily_pnl:.2f})")
            return True
        return False

    def check_weekly_limit(self, cfg) -> bool:
        self._check_resets()
        limit = cfg["equity"] * (cfg["max_weekly_loss_pct"] / 100)
        if self.weekly_pnl < -limit:
            log.warning(f"⛔  Weekly loss limit hit ({self.weekly_pnl:.2f})")
            return True
        return False

    def check_max_drawdown(self, cfg) -> bool:
        if self.peak_bal == 0:
            return False
        dd = (self.peak_bal - self.balance) / self.peak_bal * 100
        if dd > cfg["max_drawdown_pct"]:
            log.warning(f"⛔  Max drawdown hit ({dd:.1f}%)")
            return True
        return False

    def drawdown_pct(self) -> float:
        if self.peak_bal == 0:
            return 0.0
        return round((self.peak_bal - self.balance) / self.peak_bal * 100, 2)

    def stats(self) -> str:
        m = performance_metrics(self.trades, self.initial_bal)
        if not m:
            return "No closed trades yet."
        return (f"Trades:{m['trades']}  Win:{m['win_rate']}%  "
                f"PF:{m['profit_factor']}  MaxDD:{m['max_drawdown']}%  "
                f"PnL:{m['total_pnl']:+.2f}  Balance:{self.balance:.2f}")


# ─────────────────────────────────────────────────────────────
#  LIVE TRADER
# ─────────────────────────────────────────────────────────────
class LiveTrader:
    def __init__(self, client, symbol, equity: float):
        self.client        = client
        self.symbol        = symbol
        self.position      = None
        self.pending_order = None   # resting live LIMIT order awaiting fill/expiry
        self.balance       = equity
        self.initial_bal   = equity
        self.peak_bal      = equity
        self.trades: list  = []
        self.daily_pnl     = 0.0
        self.weekly_pnl    = 0.0
        self.daily_reset   = datetime.now(timezone.utc).date()
        self.weekly_reset  = datetime.now(timezone.utc).isocalendar()[1]

    def _check_resets(self):
        today = datetime.now(timezone.utc).date()
        week  = datetime.now(timezone.utc).isocalendar()[1]
        if today != self.daily_reset:
            self.daily_pnl  = 0.0
            self.daily_reset = today
        if week != self.weekly_reset:
            self.weekly_pnl  = 0.0
            self.weekly_reset = week

    def _round_qty(self, qty, step):
        p = int(round(-math.log(step, 10), 0))
        return round(math.floor(qty / step) * step, p)

    def _round_price(self, price, tick):
        p = int(round(-math.log(tick, 10), 0))
        return round(math.floor(price / tick) * tick, p)

    def _symbol_filters(self, symbol):
        """LOT_SIZE step, PRICE_FILTER tick size, and MIN_NOTIONAL, in one lookup."""
        info = self.client.get_symbol_info(symbol)
        filters = {f["filterType"]: f for f in info["filters"]}
        step  = float(filters["LOT_SIZE"]["stepSize"])
        tick  = float(filters.get("PRICE_FILTER", {}).get("tickSize", "0.00000001"))
        min_notional = 0.0
        for key in ("MIN_NOTIONAL", "NOTIONAL"):
            if key in filters:
                min_notional = float(filters[key].get("minNotional") or filters[key].get("notional") or 0)
                break
        return step, tick, min_notional

    @staticmethod
    def _avg_fill(order: dict, qty: float, fallback: float) -> float:
        fills = order.get("fills") or []
        if not fills or qty <= 0:
            return fallback
        return sum(float(f["price"]) * float(f["qty"]) for f in fills) / qty

    def buy(self, symbol, price, usdt, stop, tp, cfg, atr=None):
        if self.position:
            return
        try:
            step, _tick, min_notional = self._symbol_filters(symbol)
            qty = self._round_qty(usdt / price, step)
            if qty <= 0 or qty * price < min_notional:
                log.warning(f"[{symbol}] Order size {qty * price:.2f} USDT below "
                            f"min notional {min_notional:.2f} — skipping buy")
                return
            o    = self.client.order_market_buy(symbol=symbol, quantity=qty)
            fill = self._avg_fill(o, qty, price)
            self.balance -= usdt
            self.position = {"symbol": symbol, "entry": fill, "qty": qty,
                              "stop": stop, "tp": tp, "usdt": usdt,
                              "atr_entry": atr, "peak": fill,
                              "entry_ts": datetime.now(timezone.utc)}
            log.info(f"✅  LIVE BUY  {qty} {symbol} @ ~{fill:.4f}  order={o['orderId']}  balance={self.balance:.2f}")
        except BinanceAPIException as e:
            log.error(f"BUY failed: {e}")

    def place_limit_buy(self, symbol, limit_price, usdt, stop, tp, cfg, atr=None,
                         expires_at=None):
        """Place a REAL resting limit order on the exchange (not a client-side
        simulation) — fills/expiry are discovered by polling check_pending_order()."""
        if self.position or self.pending_order:
            return False
        try:
            step, tick, min_notional = self._symbol_filters(symbol)
            rprice = self._round_price(limit_price, tick)
            qty    = self._round_qty(usdt / rprice, step)
            if qty <= 0 or qty * rprice < min_notional:
                log.warning(f"[{symbol}] Limit order size {qty * rprice:.2f} USDT below "
                            f"min notional {min_notional:.2f} — skipping")
                return False
            price_str = f"{rprice:.8f}".rstrip("0").rstrip(".")
            o = self.client.order_limit_buy(symbol=symbol, quantity=qty, price=price_str)
            self.pending_order = {
                "order_id": o["orderId"], "symbol": symbol, "qty": qty,
                "limit_price": rprice, "stop": stop, "tp": tp,
                "usdt": usdt, "atr": atr, "expires_at": expires_at,
            }
            log.info(f"✅  LIVE LIMIT order placed  {qty} {symbol} @ {rprice}  order={o['orderId']}")
            return True
        except BinanceAPIException as e:
            log.error(f"LIMIT BUY failed: {e}")
            return False

    def check_pending_order(self) -> str:
        """Poll the resting limit order's status.
        Returns 'filled', 'pending', or 'gone' (no pending order / terminal non-fill)."""
        if not self.pending_order:
            return "gone"
        po = self.pending_order
        try:
            o = self.client.get_order(symbol=po["symbol"], orderId=po["order_id"])
        except BinanceAPIException as e:
            log.error(f"[{po['symbol']}] Order status check failed: {e}")
            return "pending"

        status = o["status"]
        if status == "FILLED":
            executed = float(o["executedQty"])
            quote    = float(o["cummulativeQuoteQty"])
            fill     = quote / executed if executed > 0 else po["limit_price"]
            self.balance -= po["usdt"]
            self.position = {"symbol": po["symbol"], "entry": fill, "qty": executed,
                              "stop": po["stop"], "tp": po["tp"], "usdt": po["usdt"],
                              "atr_entry": po["atr"], "peak": fill,
                              "entry_ts": datetime.now(timezone.utc)}
            log.info(f"✅  LIVE LIMIT FILL  {executed} {po['symbol']} @ {fill:.4f}  "
                     f"order={po['order_id']}  balance={self.balance:.2f}")
            self.pending_order = None
            return "filled"
        if status in ("CANCELED", "EXPIRED", "REJECTED", "PENDING_CANCEL"):
            log.info(f"[{po['symbol']}] Live limit order ended without fill (status={status})")
            self.pending_order = None
            return "gone"
        return "pending"   # NEW or PARTIALLY_FILLED — keep waiting

    def cancel_pending_order(self):
        if not self.pending_order:
            return
        po = self.pending_order
        try:
            self.client.cancel_order(symbol=po["symbol"], orderId=po["order_id"])
            log.info(f"[{po['symbol']}] Live limit order cancelled (expired, order={po['order_id']})")
        except BinanceAPIException as e:
            log.error(f"[{po['symbol']}] Cancel order failed: {e}")
        self.pending_order = None

    def sell(self, price, reason="signal", cfg={}):
        if not self.position:
            return None
        pos = self.position
        try:
            o    = self.client.order_market_sell(symbol=pos["symbol"], quantity=pos["qty"])
            fill = self._avg_fill(o, pos["qty"], price)
            pnl  = (fill - pos["entry"]) / pos["entry"] * pos["usdt"]
            self.balance   += pos["usdt"] + pnl
            self.peak_bal   = max(self.peak_bal, self.balance)
            self._check_resets()
            self.daily_pnl  += pnl
            self.weekly_pnl += pnl
            sign = "+" if pnl >= 0 else ""
            log.info(f"✅  LIVE SELL {pos['qty']} {pos['symbol']} @ ~{fill:.4f}  "
                     f"PnL {sign}{pnl:.2f}  [{reason}]  order={o['orderId']}  balance={self.balance:.2f}")
            self.trades.append({
                "symbol": pos["symbol"], "entry": pos["entry"], "exit": fill,
                "qty": pos["qty"], "usdt": pos["usdt"], "stop": pos["stop"], "tp": pos["tp"],
                "pnl": round(pnl, 4), "reason": reason,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            self.position = None
            return pnl
        except BinanceAPIException as e:
            log.error(f"SELL failed: {e}")
            return None

    def check_stops(self, price, cfg, candle_high=None) -> float | None:
        if not self.position:
            return None
        pos = self.position

        hi = candle_high if candle_high is not None else price
        pos["peak"] = max(pos.get("peak", pos["entry"]), hi)

        if cfg.get("trailing_stop_enabled", True):
            atr_e = pos.get("atr_entry") or 0
            if atr_e > 0:
                peak  = pos["peak"]
                entry = pos["entry"]
                be_at    = entry + atr_e * cfg.get("breakeven_at_atr_mult", 1.0)
                trail_at = entry + atr_e * cfg.get("trail_from_atr_mult",   2.0)
                if peak >= trail_at:
                    new_stop = peak - atr_e * cfg.get("trail_stop_atr_mult", 1.0)
                    if new_stop > pos["stop"]:
                        log.info(f"[{pos['symbol']}] Trail stop: {pos['stop']:.4f} → {new_stop:.4f}")
                        pos["stop"] = round(new_stop, 8)
                elif peak >= be_at and entry > pos["stop"]:
                    log.info(f"[{pos['symbol']}] Stop to breakeven: {pos['stop']:.4f} → {entry:.4f}")
                    pos["stop"] = round(entry, 8)

        time_stop_bars = cfg.get("time_stop_bars", 0)
        if time_stop_bars > 0 and "entry_ts" in pos:
            bars_held = (datetime.now(timezone.utc) - pos["entry_ts"]).total_seconds() / (4 * 3600)
            if bars_held >= time_stop_bars:
                return self.sell(price, reason="TIME-STOP", cfg=cfg)

        if price <= pos["stop"]:
            return self.sell(price, reason="STOP-LOSS", cfg=cfg)
        if price >= pos["tp"]:
            return self.sell(price, reason="TAKE-PROFIT", cfg=cfg)
        return None

    def check_daily_limit(self, cfg) -> bool:
        self._check_resets()
        limit = cfg["equity"] * (cfg["max_daily_loss_pct"] / 100)
        if self.daily_pnl < -limit:
            log.warning(f"⛔  Daily loss limit hit ({self.daily_pnl:.2f})")
            return True
        return False

    def check_weekly_limit(self, cfg) -> bool:
        self._check_resets()
        limit = cfg["equity"] * (cfg["max_weekly_loss_pct"] / 100)
        if self.weekly_pnl < -limit:
            log.warning(f"⛔  Weekly loss limit hit ({self.weekly_pnl:.2f})")
            return True
        return False

    def check_max_drawdown(self, cfg) -> bool:
        if self.peak_bal == 0:
            return False
        dd = (self.peak_bal - self.balance) / self.peak_bal * 100
        if dd > cfg["max_drawdown_pct"]:
            log.warning(f"⛔  Max drawdown hit ({dd:.1f}%)")
            return True
        return False

    def drawdown_pct(self) -> float:
        if self.peak_bal == 0:
            return 0.0
        return round((self.peak_bal - self.balance) / self.peak_bal * 100, 2)

    def stats(self) -> str:
        m = performance_metrics(self.trades, self.initial_bal)
        if not m:
            return "No closed trades yet."
        return (f"Trades:{m['trades']}  Win:{m['win_rate']}%  "
                f"PF:{m['profit_factor']}  MaxDD:{m['max_drawdown']}%  "
                f"PnL:{m['total_pnl']:+.2f}  Balance:{self.balance:.2f}")


# ─────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────
def run(cfg: dict, live: bool):
    api_key    = cfg["api_key"]    or os.getenv("BINANCE_API_KEY",    "")
    api_secret = cfg["api_secret"] or os.getenv("BINANCE_API_SECRET", "")
    tg_token   = cfg["telegram_token"]   or os.getenv("TELEGRAM_TOKEN",   "")
    tg_chat    = cfg["telegram_chat_id"] or os.getenv("TELEGRAM_CHAT_ID", "")

    if live and (not api_key or not api_secret):
        raise ValueError("Live mode requires BINANCE_API_KEY and BINANCE_API_SECRET.")

    client      = Client(api_key, api_secret)
    tg          = TelegramAlerter(tg_token, tg_chat)
    strategy_fn = STRATEGY_MAP.get(cfg["strategy"])
    if not strategy_fn:
        raise ValueError(f"Unknown strategy '{cfg['strategy']}'.")

    symbols = cfg.get("symbols", [cfg.get("symbol", "BTCUSDT")])
    n_sym   = len(symbols)
    bal_each     = cfg["paper_balance"] / n_sym
    eq_each      = cfg["equity"] / n_sym

    # One trader per symbol
    traders: dict = {}
    for sym in symbols:
        traders[sym] = LiveTrader(client, sym, eq_each) if live else PaperTrader(bal_each)

    pending_limit_orders: dict = {}   # sym → {price, stop, tp, usdt, expires_at} (paper mode only)

    # Restore any open position / resting order from before a restart, per symbol
    saved_state = load_all_trader_state()
    for sym in symbols:
        restore_trader(traders[sym], saved_state.get(sym), sym)
    if not live:
        pending_limit_orders.update(restore_pending_limit_orders(saved_state))

    act_cfg = cfg.get("active_strategy", {})
    active  = ActiveStrategyEngine(act_cfg, cfg["paper_balance"] * 0.3)  # 30% of balance for active

    skip_path    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skip_analytics.json")
    skip_tracker = SkipTracker(skip_path, symbols)

    mode_str = "🔴 LIVE" if live else "📄 PAPER"
    log.info("=" * 65)
    log.info(f"  {mode_str} BOT v4 STARTED")
    log.info(f"  Symbols    : {', '.join(symbols)}")
    log.info(f"  Interval   : {cfg['interval']} (HTF: {cfg['htf_interval']})")
    log.info(f"  Strategy   : {cfg['strategy'].upper()} | Score threshold: {cfg.get('score_threshold', 70)}/100")
    log.info(f"  Risk       : {cfg['risk_per_trade_pct']}%/trade | Limits: daily {cfg['max_daily_loss_pct']}% / weekly {cfg['max_weekly_loss_pct']}%")
    log.info(f"  Active stg : {'ENABLED' if active.enabled else 'disabled'}")
    log.info(f"  Telegram   : {'enabled' if tg.enabled else 'disabled'}")
    log.info("=" * 65)

    tg.send(f"🤖 Bot v4 STARTED\nMode: {mode_str}\nSymbols: {', '.join(symbols)}\n"
            f"Strategy: {cfg['strategy'].upper()} | Threshold: {cfg.get('score_threshold', 70)}/100")

    iteration             = 0
    api_errors            = 0
    last_alert            = "Bot started"
    fg_val, fg_label      = fetch_fear_greed()

    while True:
        # ── Emergency stop ────────────────────────────────────
        stop_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "EMERGENCY_STOP")
        if os.path.exists(stop_file):
            msg = "🚨 EMERGENCY STOP active — delete EMERGENCY_STOP file to resume."
            log.warning(msg)
            tg.send(msg)
            last_alert = "EMERGENCY STOP active"
            # Write status even while paused
            sym_data = _build_sym_data_paused(traders, symbols, eq_each)
            ks = _build_kill_status(list(traders.values())[0], True, False, False, False, False)
            write_dashboard_json(sym_data, fg_val, fg_label, iteration, live, ks, last_alert, skip_tracker, active)
            time.sleep(60)
            continue

        try:
            iteration += 1

            # ── Refresh Fear & Greed every 10 iterations ─────
            if iteration % 10 == 1:
                fg_val, fg_label = fetch_fear_greed()

            # ── Process each symbol ───────────────────────────
            symbol_data = []
            any_kill_triggered = False

            for sym in symbols:
                trader = traders[sym]

                try:
                    df    = fetch_candles(client, sym, cfg["interval"], limit=200)
                    df_htf= fetch_candles(client, sym, cfg["htf_interval"], limit=60)
                    price = df["close"].iloc[-1]
                    atr   = compute_atr(df).iloc[-1]
                    api_errors = 0
                except Exception as e:
                    log.error(f"[{sym}] Data fetch failed: {e}")
                    api_errors += 1
                    continue

                # ── Active strategy exits ─────────────────────
                exit_result = active.check_exits(sym, price, cfg)
                if exit_result and active.trader and active.trader.trades:
                    t = active.trader.trades[-1]
                    tg.send(f"🎯 ACTIVE {exit_result.upper()} [{sym}]\nPnL: {'+' if t['pnl']>=0 else ''}{t['pnl']:.2f} USDT")
                    last_alert = f"Active {exit_result} [{sym}]: {'+' if t['pnl']>=0 else ''}{t['pnl']:.2f}"

                # ── Core strategy stop checks ─────────────────
                stop_pnl = trader.check_stops(price, cfg, candle_high=df["high"].iloc[-1])
                if stop_pnl is not None and hasattr(trader, "trades") and trader.trades:
                    t = trader.trades[-1]
                    sign = "+" if t["pnl"] >= 0 else ""
                    tg.send(f"{'✅' if t['pnl']>=0 else '❌'} [{sym}] {t['reason']}\n"
                            f"PnL: {sign}{t['pnl']:.2f}  Balance: {trader.balance:.2f}")
                    last_alert = f"{sym} {t['reason']}: {sign}{t['pnl']:.2f} USDT"

                # ── Pending limit order: fill or expire ──────
                if live and getattr(trader, "pending_order", None):
                    po = trader.pending_order
                    if po.get("expires_at") and datetime.now(timezone.utc) >= po["expires_at"]:
                        trader.cancel_pending_order()
                        last_alert = f"Live limit order expired [{sym}]"
                    else:
                        order_status = trader.check_pending_order()
                        if order_status == "filled":
                            skip_tracker.trade_opened(sym)
                            p = trader.position
                            tg.send(f"📈 LIVE LIMIT FILL [{sym}]\nFill: {p['entry']:.2f}  "
                                    f"Size: {p['usdt']:.2f}\nStop: {p['stop']:.2f}  TP: {p['tp']:.2f}")
                            last_alert = f"Live limit fill {sym} @ {p['entry']:.2f}"
                        elif order_status == "gone":
                            last_alert = f"Live limit order ended without fill [{sym}]"
                elif sym in pending_limit_orders and not trader.position:
                    plo = pending_limit_orders[sym]
                    if datetime.now(timezone.utc) >= plo["expires_at"]:
                        log.info(f"[{sym}] Limit order expired (limit was {plo['price']:.2f})")
                        del pending_limit_orders[sym]
                    elif price <= plo["price"] and hasattr(trader, "buy_limit"):
                        trader.buy_limit(sym, plo["price"], plo["usdt"], plo["stop"], plo["tp"], cfg,
                                         atr=plo.get("atr"))
                        skip_tracker.trade_opened(sym)
                        tg.send(f"📈 LIMIT FILL [{sym}]\nFill: {plo['price']:.2f}  Size: {plo['usdt']:.2f}\n"
                                f"Stop: {plo['stop']:.2f}  TP: {plo['tp']:.2f}")
                        last_alert = f"Limit fill {sym} @ {plo['price']:.2f}"
                        del pending_limit_orders[sym]

                # ── Kill switches ─────────────────────────────
                skip_tracker.scan(sym)
                paused = False

                if trader.check_daily_limit(cfg):
                    msg = f"⛔ [{sym}] Daily loss limit hit ({trader.daily_pnl:.2f})"
                    tg.send(msg); last_alert = msg; any_kill_triggered = True; paused = True
                    skip_tracker.skip(sym, "kill_switch")
                elif trader.check_weekly_limit(cfg):
                    msg = f"⛔ [{sym}] Weekly loss limit hit ({trader.weekly_pnl:.2f})"
                    tg.send(msg); last_alert = msg; any_kill_triggered = True; paused = True
                    skip_tracker.skip(sym, "kill_switch")
                elif trader.check_max_drawdown(cfg):
                    msg = f"⛔ [{sym}] Max drawdown hit ({trader.drawdown_pct():.1f}%)"
                    tg.send(msg); last_alert = msg; any_kill_triggered = True; paused = True
                    skip_tracker.skip(sym, "kill_switch")

                # ── Volatility spike ──────────────────────────
                vol_pct = (atr / price) * 100
                if vol_pct > cfg["max_volatility_pct"]:
                    log.info(f"[{sym}] Volatility too high ({vol_pct:.2f}%) — skip")
                    skip_tracker.skip(sym, "volatility_filter")
                    paused = True

                # ── Auto strategy selection ───────────────────
                adx_pre    = compute_adx(df).iloc[-1]
                regime_pre = "trending" if adx_pre > 25 else ("ranging" if adx_pre < 20 else "neutral")
                htf_bull_pre = df_htf["close"].iloc[-1] > compute_ma(df_htf["close"], 50).iloc[-1]

                if cfg.get("auto_strategy", {}).get("enabled", False):
                    strat_sel = select_strategy(regime_pre, htf_bull_pre, fg_val, vol_pct, cfg)
                    effective_strategy = strat_sel["strategy"] or cfg["strategy"]
                else:
                    effective_strategy = cfg["strategy"]
                    strat_sel = {"strategy": effective_strategy,
                                 "reason": "Fixed strategy from config",
                                 "rejected": {}, "evaluated": {}}

                # ── Signal score ──────────────────────────────
                score_data = compute_signal_score(df, df_htf, cfg, effective_strategy, fg_val)
                score_data["strategy_selection"] = strat_sel
                state      = get_strategy_state(score_data, bool(trader.position), paused, strat_sel)
                signal      = score_data["raw_signal"] if strat_sel.get("strategy") else None
                regime      = score_data["regime"]
                trend       = "bull" if score_data["htf_bull"] else "bear"

                # ── Dynamic risk mode ─────────────────────────
                risk_mode_data = get_risk_mode(
                    score       = score_data["total"],
                    strategy    = effective_strategy,
                    regime      = regime,
                    htf_bull    = score_data["htf_bull"],
                    fg_val      = fg_val,
                    rr          = cfg["take_profit_rr"],
                    kill_active = paused,
                    drawdown_pct= trader.drawdown_pct() if hasattr(trader, "drawdown_pct") else 0.0,
                    cfg         = cfg,
                )
                log.info(f"[{sym}] Risk mode: {risk_mode_data['mode']} "
                         f"({risk_mode_data['risk_pct']}%) — {risk_mode_data['reason']}")

                log.info(f"[{sym}] Strategy selected: {effective_strategy} "
                         f"({strat_sel.get('reason', '')})")

                # ── Track skip reasons ────────────────────────
                if not paused and not trader.position and not getattr(trader, "pending_order", None):
                    if not score_data["htf_bull"]:
                        skip_tracker.skip(sym, "trend_filter")
                    elif score_data["components"]["regime"]["score"] == 0:
                        skip_tracker.skip(sym, "regime_filter")
                    elif signal is None:
                        skip_tracker.skip(sym, "signal_filter")
                    elif score_data["components"]["rsi"]["score"] < 10:
                        skip_tracker.skip(sym, "rsi_filter")
                    elif score_data["components"]["volume"]["score"] < 10:
                        skip_tracker.skip(sym, "volume_filter")
                    elif score_data["components"]["fear_greed"]["score"] < 10:
                        skip_tracker.skip(sym, "fear_greed_filter")
                    elif score_data["total"] < cfg.get("score_threshold", 70):
                        skip_tracker.skip(sym, "score_threshold")
                    elif signal == "buy" and score_data["trade_allowed"]:
                        skip_tracker.valid_setup(sym)

                # ── Execute core signal ───────────────────────
                no_trade_reason = ""
                if not paused and not trader.position and not getattr(trader, "pending_order", None):
                    if signal == "buy" and score_data["trade_allowed"]:
                        # Dynamic TP: let winners run in trending, take profit fast in ranging
                        if cfg.get("dynamic_tp_by_regime", True):
                            tp_rr = (cfg.get("tp_rr_trending", 3.0) if regime == "trending"
                                     else cfg.get("tp_rr_ranging", 1.5))
                            cfg_entry = {**cfg, "take_profit_rr": tp_rr}
                        else:
                            cfg_entry = cfg

                        usdt, stop, tp = atr_position_size(
                            price, atr, cfg_entry,
                            equity_override=eq_each,
                            risk_pct_override=risk_mode_data["risk_pct"],
                        )
                        already_pending = (bool(getattr(trader, "pending_order", None)) if live
                                            else sym in pending_limit_orders)
                        use_lim = cfg.get("use_limit_orders", True) and not already_pending
                        if use_lim and live:
                            expires_at = (datetime.now(timezone.utc)
                                          + timedelta(hours=cfg.get("limit_order_expiry_h", 4)))
                            placed = trader.place_limit_buy(sym, price, usdt, stop, tp, cfg,
                                                             atr=atr, expires_at=expires_at)
                            if placed:
                                skip_tracker.trade_opened(sym)
                                tg.send(f"🎯 LIVE limit order placed [{sym}]\nLimit: {price:.2f}  "
                                        f"Size: {usdt:.2f}\nStop: {stop:.2f}  TP: {tp:.2f}\n"
                                        f"Score: {score_data['total']}/100  "
                                        f"Risk: {risk_mode_data['mode']} ({risk_mode_data['risk_pct']}%)")
                                last_alert = f"Live limit placed {sym} @ {price:.2f} (score {score_data['total']})"
                            else:
                                no_trade_reason = "Live limit order placement failed or skipped (see log)"
                        elif use_lim:
                            expires_at = (datetime.now(timezone.utc)
                                          + timedelta(hours=cfg.get("limit_order_expiry_h", 4)))
                            pending_limit_orders[sym] = {
                                "price": price, "stop": stop, "tp": tp,
                                "usdt": usdt, "atr": atr, "expires_at": expires_at,
                            }
                            log.info(f"[{sym}] Limit order placed @ {price:.2f} "
                                     f"(expires in {cfg.get('limit_order_expiry_h', 4)}h)")
                            tg.send(f"🎯 Limit order placed [{sym}]\nLimit: {price:.2f}  "
                                    f"Size: {usdt:.2f}\nStop: {stop:.2f}  TP: {tp:.2f}\n"
                                    f"Score: {score_data['total']}/100  "
                                    f"Risk: {risk_mode_data['mode']} ({risk_mode_data['risk_pct']}%)")
                            last_alert = f"Limit placed {sym} @ {price:.2f} (score {score_data['total']})"
                        else:
                            trader.buy(sym, price, usdt, stop, tp, cfg, atr=atr)
                            skip_tracker.trade_opened(sym)
                            tg.send(f"📈 Core BUY [{sym}]\nPrice: {price:.2f}  Size: {usdt:.2f}\n"
                                    f"Stop: {stop:.2f}  TP: {tp:.2f}\nScore: {score_data['total']}/100\n"
                                    f"Risk: {risk_mode_data['mode']} ({risk_mode_data['risk_pct']}%)")
                            last_alert = (f"Core BUY {sym} @ {price:.2f} "
                                          f"(score {score_data['total']}, risk {risk_mode_data['mode']})")
                    else:
                        top_miss = score_data["missing"][0] if score_data["missing"] else "Waiting for setup"
                        no_trade_reason = top_miss
                        log.info(f"[{sym}] No trade: {top_miss} | Score {score_data['total']}/{cfg.get('score_threshold',70)}")
                elif trader.position:
                    no_trade_reason = "Holding position — monitoring stop and TP"
                elif getattr(trader, "pending_order", None):
                    no_trade_reason = "Live limit order resting — awaiting fill or expiry"
                else:
                    no_trade_reason = f"Kill switch or volatility filter active"

                # ── Active strategy ───────────────────────────
                act_analysis = active.analyze(client, sym, price, cfg, fg_val)
                if active.enabled and not paused and act_analysis.get("trade_allowed"):
                    active.execute(sym, price, act_analysis, cfg)

                # ── Log iteration ─────────────────────────────
                log.info(f"[{sym}] iter={iteration:>5}  price={price:.2f}  score={score_data['total']}/100  "
                         f"state='{state}'  {('IN POS' if trader.position else 'flat')}")

                # ── Build symbol data ─────────────────────────
                metrics = performance_metrics(
                    getattr(trader, "trades", []),
                    getattr(trader, "initial_bal", bal_each),
                )
                pos_data = None
                if trader.position:
                    p = trader.position
                    unr = (price - p["entry"]) / p["entry"] * p.get("usdt", 0)
                    pos_data = {
                        "entry": round(p["entry"], 4), "qty": round(p.get("qty", 0), 6),
                        "usdt": round(p.get("usdt", 0), 2), "stop": round(p["stop"], 4),
                        "tp": round(p["tp"], 4), "unrealized_pnl": round(unr, 2),
                    }

                bal  = round(getattr(trader, "balance",     bal_each), 2)
                strt = round(getattr(trader, "initial_bal", bal_each), 2)

                symbol_data.append({
                    "symbol":          sym,
                    "price":           round(price, 4),
                    "atr":             round(atr, 4),
                    "signal":          signal,
                    "regime":          regime,
                    "htf":             trend,
                    "strategy":        effective_strategy,
                    "strategy_selection": strat_sel,
                    "position":        pos_data,
                    "balance":         bal,
                    "start_balance":   strt,
                    "trades":          len(getattr(trader, "trades", [])),
                    "win_rate":        metrics.get("win_rate", 0),
                    "pnl":             round(bal - strt, 2),
                    "performance":     metrics,
                    "signal_score":    score_data,
                    "strategy_state":  state,
                    "no_trade_reason": no_trade_reason,
                    "active":          act_analysis,
                    "daily_pnl":       round(getattr(trader, "daily_pnl",  0.0), 2),
                    "weekly_pnl":      round(getattr(trader, "weekly_pnl", 0.0), 2),
                    "risk_mode":       risk_mode_data["mode"],
                    "risk_pct":        risk_mode_data["risk_pct"],
                    "risk_reason":     risk_mode_data["reason"],
                    "risk_reduced":    risk_mode_data["reduced"],
                })

            # ── Periodic stats ────────────────────────────────
            if iteration % 20 == 0:
                for sym, t in traders.items():
                    log.info(f"  ── [{sym}] STATS: {t.stats()}")

            # ── Persist trader state (open positions / resting orders) ─
            save_trader_state(traders, pending_limit_orders if not live else None)

            # ── Write dashboard ───────────────────────────────
            ref_trader = list(traders.values())[0]
            ks = _build_kill_status(ref_trader, False, any_kill_triggered,
                                     False, False, False)
            write_dashboard_json(symbol_data, fg_val, fg_label, iteration, live,
                                  ks, last_alert, skip_tracker, active)

            if any_kill_triggered:
                time.sleep(3600)
                continue

        except BinanceAPIException as e:
            api_errors += 1
            log.error(f"Binance API error #{api_errors}: {e}")
            if api_errors >= cfg["max_api_errors"]:
                msg = f"🔴 {api_errors} API errors — pausing 5 min\n{e}"
                tg.send(msg); last_alert = msg
                time.sleep(300); api_errors = 0
            else:
                time.sleep(60)
            continue

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            for sym, t in traders.items():
                log.info(f"  [{sym}] Final: {t.stats()}")
            tg.send(f"🛑 Bot STOPPED")
            break

        except Exception as e:
            api_errors += 1
            log.error(f"Unexpected error: {e}")
            time.sleep(60)
            continue

        time.sleep(cfg["poll_seconds"])


def _build_sym_data_paused(traders: dict, symbols: list, bal_each: float) -> list:
    result = []
    for sym in symbols:
        t = traders[sym]
        result.append({
            "symbol": sym, "price": 0, "atr": 0, "signal": None,
            "regime": "—", "htf": "—", "strategy": "—",
            "position": t.position, "balance": round(getattr(t, "balance", bal_each), 2),
            "start_balance": round(getattr(t, "initial_bal", bal_each), 2),
            "trades": 0, "win_rate": 0, "pnl": 0, "performance": {},
            "signal_score": {}, "strategy_state": "EMERGENCY STOP", "no_trade_reason": "Emergency stop active",
            "strategy_selection": {"strategy": None, "reason": "Emergency stop active", "rejected": {}, "evaluated": {}},
            "active": {"enabled": False, "state": "Paused"}, "daily_pnl": 0, "weekly_pnl": 0,
            "risk_mode": "conservative", "risk_pct": 0.50,
            "risk_reason": "Emergency stop active — risk set to minimum",
            "risk_reduced": True,
        })
    return result


# ─────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Binance Crypto Trading Bot v4")
    parser.add_argument("--live",     action="store_true")
    parser.add_argument("--symbol",   default=None, help="Override symbols list with single symbol")
    parser.add_argument("--strategy", default=None, choices=["ma","rsi","bollinger"])
    parser.add_argument("--interval", default=None)
    args = parser.parse_args()

    if args.symbol:
        CONFIG["symbols"] = [args.symbol]
    if args.strategy:
        CONFIG["strategy"] = args.strategy
    if args.interval:
        CONFIG["interval"] = args.interval

    if args.live:
        print("\n⚠️  WARNING: LIVE MODE — real money will be traded!")
        print("   Press Ctrl+C within 5 seconds to abort...\n")
        time.sleep(5)

    run(CONFIG, live=args.live)
