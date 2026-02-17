import os
import json
import yfinance as yf
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# -----------------------------
# CONFIG
# -----------------------------
TICKER = "SGLN.L"
PERCENT_THRESHOLD = 0.02  # 2%
STATE_FILE = "state.json"


# -----------------------------
# PRICE FETCHER
# -----------------------------
def get_price():
    data = yf.Ticker(TICKER)
    price = data.history(period="1d")["Close"].iloc[-1]
    return float(price)


# -----------------------------
# STATE MANAGEMENT
# -----------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "last_price": None,
            "last_low": None,
            "last_high": None,
            "trend": None,      # "UP" or "DOWN"
            "position": "OUT",  # "IN" or "OUT"
        }
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state
    app.job_queue.run_repeating(check_gold, interval=300, first=5)

    print("Bot started — polling Telegram…")
    app.run_polling()

