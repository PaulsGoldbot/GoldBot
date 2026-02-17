import os
import json
import yfinance as yf
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TICKER = "SGLN.L"
PERCENT_THRESHOLD = 0.02
STATE_FILE = "state.json"


def get_price():
    data = yf.Ticker(TICKER)
    price = data.history(period="1d")["Close"].iloc[-1]
    return float(price)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "last_price": None,
            "last_low": None,
            "last_high": None,
            "trend": None,
            "position": "OUT"
        }
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


async def send_alert(text, context):
    chat_id = int(os.getenv("CHAT_ID"))
    await context.bot.send_message(chat_id=chat_id, text=text)


async def check_gold(context):
    state = load_state()
    current_price = get_price()
    last_price = state["last_price"]

    print("Checking gold… Current: {}, Last: {}".format(current_price, last_price))

    if last_price is None:
        state["last_price"] = current_price
        state["last_low"] = current_price
        state["last_high"] = current_price
        save_state(state)
        print("First run — saved initial price.")
        return

    last_low = state["last_low"]
    last_high = state["last_high"]
    trend = state["trend"]
    position = state["position"]

    if current_price < last_low:
        last_low = current_price
    if current_price > last_high:
        last_high = current_price

    move_from_low = (current_price - last_low) / last_low if last_low > 0 else None
    move_from_high = (current_price - last_high) / last_high if last_high > 0 else None

    if move_from_low is not None and move_from_low >= PERCENT_THRESHOLD:
        trend = "UP"
        position = "IN"

        msg = (
            "BUY signal triggered. Your rule says buy now.\n"
            "Gold has risen 2% from the last low.\n"
            "Last low: £{:.2f}\n"
            "Current price: £{:.2f}\n"
            "You are now marked as IN the market."
        ).format(last_low, current_price)

        await send_alert(msg, context)
        last_high = current_price

    elif move_from_high is not None and move_from_high <= -PERCENT_THRESHOLD:
        trend = "DOWN"
        position = "OUT"

        msg = (
            "SELL signal triggered. Your rule says sell now.\n"
            "Gold has fallen 2% from the last high.\n"
            "Last high: £{:.2f}\n"
            "Current price: £{:.2f}\n"
            "You are now marked as OUT of the market."
        ).format(last_high, current_price)

        await send_alert(msg, context)
        last_low = current_price

    state["last_price"] = current_price
    state["last_low"] = last_low
    state["last_high"] = last_high
    state["trend"] = trend
    state["position"] = position
    save_state(state)


async def start(update, context):
    await update.message.reply_text(
        "Bot is running.\n"
        "I check gold every 5 minutes and use your 2% rule to trigger BUY and SELL signals."
    )


async def status(update, context):
    state = load_state()
    price = state["last_price"]
    last_low = state["last_low"]
    last_high = state["last_high"]
    trend = state["trend"]

    if price is None:
        await update.message.reply_text("No price data yet.")
        return

    if trend == "UP":
        change = (price - last_low) / last_low * 100
        msg = (
            "You are currently IN the market.\n"
            "Gold is trending upward.\n"
            "Last low: £{:.2f}\n"
            "Current price: £{:.2f}\n"
            "Change from last low: {:.2f}%."
        ).format(last_low, price, change)

    elif trend == "DOWN":
        change = (price - last_high) / last_high * 100
        msg = (
            "You are currently OUT of the market.\n"
            "Gold is trending downward.\n"
            "Last high: £{:.2f}\n"
            "Current price: £{:.2f}\n"
            "Change from last high: {:.2f}%."
        ).format(last_high, price, change)

    else:
        msg = (
            "Trend not established yet.\n"
            "Last price: £{:.2f}\n"
            "Waiting for a clear 2% move."
        ).format(price)

    await update.message.reply_text(msg)


if __name__ == "__main__":
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))

    app.job_queue.run_repeating(check_gold, interval=300, first=5)

    print("Bot started — polling Telegram…")
    app.run_polling()
