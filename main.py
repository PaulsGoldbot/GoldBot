import os
import json
import yfinance as yf
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


TICKER = "SGLN.L"
ALERT_MOVE = 50  # £50 movement alert


def get_price():
    data = yf.Ticker(TICKER)
    price = data.history(period="1d")["Close"].iloc[-1]
    return float(price)


def load_last_price():
    if not os.path.exists("state.json"):
        return None
    with open("state.json", "r") as f:
        return json.load(f).get("last_price")


def save_last_price(price):
    with open("state.json", "w") as f:
        json.dump({"last_price": price}, f)


async def send_alert(text, context: ContextTypes.DEFAULT_TYPE, chat_id):
    await context.bot.send_message(chat_id=chat_id, text=text)


async def check_gold(context: ContextTypes.DEFAULT_TYPE):
    current_price = get_price()
    last_price = load_last_price()

    print(f"Checking gold… Current: {current_price}, Last: {last_price}")

    if last_price is None:
        save_last_price(current_price)
        print("First run — saved initial price.")
        return

    price_diff = current_price - last_price

    if abs(price_diff) >= ALERT_MOVE:
        if price_diff > 0:
            alert = (
                f"📈 SELL Signal\n\n"
                f"Gold has risen by £{price_diff:.2f}\n"
                f"Old price: £{last_price:.2f}\n"
                f"New price: £{current_price:.2f}"
            )
        else:
            alert = (
                f"📉 BUY Signal\n\n"
                f"Gold has dropped by £{abs(price_diff):.2f}\n"
                f"Old price: £{last_price:.2f}\n"
                f"New price: £{current_price:.2f}"
            )

        chat_id = int(os.getenv("CHAT_ID"))
        await send_alert(alert, context, chat_id)

    save_last_price(current_price)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is running and checking gold every 5 minutes.")


async def main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    CHAT_ID = int(os.getenv("CHAT_ID"))

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    # Run gold check every 5 minutes
    app.job_queue.run_repeating(check_gold, interval=300, first=5)

    print("Bot started — polling Telegram…")
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
