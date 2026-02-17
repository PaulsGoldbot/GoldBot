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


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# -----------------------------
# ALERT SENDER
# -----------------------------
async def send_alert(text, context: ContextTypes.DEFAULT_TYPE):
    chat_id = int(os.getenv("CHAT_ID"))
    await context.bot.send_message(chat_id=chat_id, text=text)


# -----------------------------
# CORE TREND LOGIC
# -----------------------------
async def check_gold(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    current_price = get_price()
    last_price = state["last_price"]

    print(f"Checking gold… Current: {current_price}, Last: {last_price}")

    # First run
    if last_price is None:
        state["last_price"] = current_price
        state["last_low"] = current_price
        state["last_high"] = current_price
        save_state(state)
        print("First run — saved initial price, high, and low.")
        return

    last_low = state["last_low"]
    last_high = state["last_high"]
    trend = state["trend"]
    position = state["position"]

    # Update lows/highs
    if current_price < last_low:
        last_low = current_price
    if current_price > last_high:
        last_high = current
