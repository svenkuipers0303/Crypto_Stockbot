# 🤖 Binance Crypto Trading Bot

A 24/7 Python trading bot for Binance with three configurable strategies,
full risk management, paper trading mode, and a backtester.

---

## ⚡ Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set your Binance API keys
Either edit `bot.py` (the `CONFIG` block at the top):
```python
"api_key":    "your_key_here",
"api_secret": "your_secret_here",
```
Or use environment variables (recommended — never hardcode keys):
```bash
export BINANCE_API_KEY="your_key_here"
export BINANCE_API_SECRET="your_secret_here"
```

### 3. Backtest first (no API key needed for public data)
```bash
python backtest.py --symbol BTCUSDT --strategy ma --interval 1h --days 90
python backtest.py --symbol ETHUSDT --strategy rsi --interval 4h
python backtest.py --symbol BTCUSDT --strategy bollinger
```

### 4. Paper trade (safe — no real money)
```bash
python bot.py                                         # BTC/USDT, MA strategy
python bot.py --symbol ETHUSDT --strategy rsi
python bot.py --symbol BTCUSDT --strategy bollinger --interval 1h
```

### 5. Go live (real money — only after testing!)
```bash
python bot.py --live --symbol BTCUSDT --strategy ma
```

---

## 📊 Strategies

| Strategy | Signal | Best for |
|---|---|---|
| `ma` | Golden/death cross of fast & slow MA | Trending markets |
| `rsi` | Bounce from oversold / drop from overbought | Ranging markets |
| `bollinger` | Price touching lower/upper band | Volatile markets |

---

## ⚙️ Configuration (bot.py → CONFIG)

| Key | Default | Description |
|---|---|---|
| `symbol` | BTCUSDT | Trading pair |
| `interval` | 15m | Candle timeframe |
| `order_size_usdt` | 50 | USD per trade |
| `stop_loss_pct` | 2.0 | % loss before exit |
| `take_profit_pct` | 4.0 | % gain before exit |
| `ma_fast` / `ma_slow` | 9 / 21 | MA crossover periods |
| `rsi_oversold` / `rsi_overbought` | 30 / 70 | RSI thresholds |
| `bb_period` / `bb_std` | 20 / 2.0 | Bollinger Band settings |
| `poll_seconds` | 60 | How often to check |

---

## 🖥️ Running 24/7 on a VPS

```bash
# Option A: screen
screen -S bot
python bot.py --live
# Ctrl+A, D to detach

# Option B: systemd service (recommended for production)
# Create /etc/systemd/system/cryptobot.service:
[Unit]
Description=Crypto Trading Bot
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/crypto_bot
ExecStart=/usr/bin/python3 bot.py --live
Restart=always
Environment=BINANCE_API_KEY=xxx
Environment=BINANCE_API_SECRET=xxx

[Install]
WantedBy=multi-user.target

# Then:
sudo systemctl enable cryptobot
sudo systemctl start cryptobot
sudo journalctl -u cryptobot -f   # follow logs
```

---

## 🔑 Binance API Key Setup

1. Go to **Binance → Account → API Management**
2. Create a new API key
3. Enable **"Enable Spot & Margin Trading"**
4. **Restrict access to your VPS IP** (important security step!)
5. **NEVER enable withdrawals** on a trading bot key

---

## ⚠️ Risk Disclaimer

- No trading strategy guarantees profit
- Always backtest before trading real money
- Start with small position sizes
- Crypto markets are highly volatile — you can lose your entire investment
- The authors are not financial advisors

---

## 📁 Files

```
crypto_bot/
├── bot.py           # Main trading bot
├── backtest.py      # Historical strategy tester
├── requirements.txt # Python dependencies
├── README.md        # This file
└── bot.log          # Created at runtime
```
