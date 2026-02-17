import os
import json
import yfinance as yf
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

PERCENT_THRESHOLD = 0.02  # 2%

# List of commodities to track
COMMODITIES = {
    "SGLN.L": "Gold",
    "SSLN.L": "Silver",
    "OILB.L": "Oil",
    "NGAS.L": "Natural Gas",
    "COPA.L": "Copper",
    "PHPT.L": "Platinum",
    "PHPD.L": "Palladium",
    "CMOD.L": "Commodities Basket",
}


def state_file_for(ticker):
    return f"state_{ticker.replace('.', '_')}.json"


def get_price(ticker):
    data = yf.Ticker(ticker)
    price = data.history(period="1d")["Close"].iloc[-1]
    return float(price)


def load_state(ticker):
    filename = state_file_for(ticker)
    if not os.path.exists(filename):
        return {
            "last_price": None,
            "last_low": None,
            "last_high": None,
            "trend": None,
            "position": "OUT",
        }
    with open(filename, "r") as f:
        return json.load(f)


def save_state(ticker, state):
    filename = state_file_for(ticker)
    with open(filename, "w") as f:
        json.dump(state, f)


async def send_alert(text, context):
    chat_id = int(os.getenv("CHAT_ID"))
    await context.bot.send_message(chat_id=chat_id, text=text)


async def check_one_commodity(ticker, name, context):
    state = load_state(ticker)
    current_price = get_price(ticker)
    last_price = state["last_price"]

    print("Checking {} ({})… Current: {}, Last: {}".format(name, ticker, current_price, last_price))

    # First run for this commodity
    if last_price is None:
        state["last_price"] = current_price
        state["last_low"] = current_price
        state["last_high"] = current_price
        save_state(ticker, state)
        print("First run for {} — saved initial price.".format(name))
        return

    last_low = state["last_low"]
    last_high = state["last_high"]
    trend = state["trend"]
    position = state["position"]

    # Update lows/highs
    if current_price < last_low:
        last_low = current_price
    if current_price > last_high:
        last_high = current_price

    move_from_low = (current_price - last_low) / last_low if last_low > 0 else None
    move_from_high = (current_price - last_high) / last_high if last_high > 0 else None

    # BUY signal
    if move_from_low is not None and move_from_low >= PERCENT_THRESHOLD:
        trend = "UP"
        position = "IN"

        msg = (
            "{} ({}) BUY signal triggered.\n"
            "Your rule says buy now.\n"
            "Price has risen 2% from the last low.\n"
            "Last low: £{:.2f}\n"
            "Current price: £{:.2f}\n"
            "You are now marked as IN the market for {}."
        ).format(name, ticker, last_low, current_price, name)

        await send_alert(msg, context)
        last_high = current_price

    # SELL signal
    elif move_from_high is not None and move_from_high <= -PERCENT_THRESHOLD:
        trend = "DOWN"
        position = "OUT"

        msg = (
            "{} ({}) SELL signal triggered.\n"
            "Your rule says sell now.\n"
            "Price has fallen 2% from the last high.\n"
            "Last high: £{:.2f}\n"
            "Current price: £{:.2f}\n"
            "You are now marked as OUT of the market for {}."
        ).format(name, ticker, last_high, current_price, name)

        await send_alert(msg, context)
        last_low = current_price

    state["last_price"] = current_price
    state["last_low"] = last_low
    state["last_high"] = last_high
    state["trend"] = trend
    state["position"] = position
    save_state(ticker, state)


async def check_all(context: ContextTypes.DEFAULT_TYPE):
    for ticker, name in COMMODITIES.items():
        try:
            await check_one_commodity(ticker, name, context)
        except Exception as e:
            print("Error checking {} ({}): {}".format(name, ticker, e))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "Bot is running.",
        "I check these commodities every 5 minutes using your 2% rule:",
    ]
    for ticker, name in COMMODITIES.items():
        lines.append("- {} ({})".format(name, ticker))
    await update.message.reply_text("\n".join(lines))


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = []
    for ticker, name in COMMODITIES.items():
        state = load_state(ticker)
        price = state["last_price"]
        last_low = state["last_low"]
        last_high = state["last_high"]
        trend = state["trend"]
        position = state["position"]

        if price is None:
            parts.append("{} ({}): No price data yet.".format(name, ticker))
            continue

        if trend == "UP":
            change = (price - last_low) / last_low * 100
            msg = (
                "{} ({}): You are IN the market.\n"
                "Trend: UP.\n"
                "Last low: £{:.2f}, Current: £{:.2f}, Change from low: {:.2f}%."
            ).format(name, ticker, last_low, price, change)

        elif trend == "DOWN":
            change = (price - last_high) / last_high * 100
            msg = (
                "{} ({}): You are OUT of the market.\n"
                "Trend: DOWN.\n"
                "Last high: £{:.2f}, Current: £{:.2f}, Change from high: {:.2f}%."
            ).format(name, ticker, last_high, price, change)

        else:
            msg = (
                "{} ({}): Trend not established yet.\n"
                "Last price: £{:.2f}.\n"
                "Waiting for a clear 2% move."
            ).format(name, ticker, price)

        parts.append(msg)

    await update.message.reply_text("\n\n".join(parts))


if __name__ == "__main__":
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))

    # Check all commodities every 5 minutes
    app.job_queue.run_repeating(check_all, interval=300, first=5)

    print("Bot started — polling Telegram…")
    app.run_polling()
