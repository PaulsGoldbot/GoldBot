import os
import json
import yfinance as yf
from datetime import datetime, timezone
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

BASE_THRESHOLD = 0.02
VOL_LOW = 0.01
VOL_HIGH = 0.03
VOL_MAX = 0.20

COMMODITIES = {
    "SGLN.L": "Gold",
    "SSLN.L": "Silver",
    "BRNT.L": "Oil",
    "NGAS.L": "Natural Gas",
    "COPA.L": "Copper",
    "PHPT.L": "Platinum",
    "PHPD.L": "Palladium",
    "CMOD.L": "Commodities Basket",
}

TEST_TICKER = "SGLN.L"


def state_file_for(ticker: str) -> str:
    return f"state_{ticker.replace('.', '_')}.json"


def default_state() -> dict:
    return {
        "last_price": None,
        "last_buy_price": None,
        "last_sell_price": None,
        "holding_value": 0.0,
        "threshold_pct": BASE_THRESHOLD,
        "pending_order": None,
        "pending_price": None,
        "buy_trigger": None,
        "sell_trigger": None,
        "last_volatility": None,
        "last_updated": None,
        "test_mode": False,
        "original_threshold": BASE_THRESHOLD,
        "ignore_sell_until_recovery": False,
    }


def load_state(ticker: str) -> dict:
    filename = state_file_for(ticker)
    if not os.path.exists(filename):
        return default_state()
    try:
        with open(filename, "r") as f:
            data = json.load(f)
    except Exception:
        return default_state()

    base = default_state()
    base.update(data)
    return base


def save_state(ticker: str, state: dict) -> None:
    filename = state_file_for(ticker)
    with open(filename, "w") as f:
        json.dump(state, f)


def get_volatility_and_price(ticker: str):
    data = yf.Ticker(ticker)
    hist = data.history(period="11d")
    if hist.empty or len(hist["Close"]) < 2:
        price = float(hist["Close"].iloc[-1]) if not hist.empty else None
        return price, None

    closes = hist["Close"]
    returns = closes.pct_change().dropna()
    vol = float(returns.std()) if not returns.empty else None
    current_price = float(closes.iloc[-1])

    if vol is not None:
        vol = min(vol, VOL_MAX)

    return current_price, vol


def adapt_threshold(volatility: float | None) -> float:
    if volatility is None:
        return BASE_THRESHOLD
    if volatility < VOL_LOW:
        return max(0.01, BASE_THRESHOLD * 0.75)
    if volatility > VOL_HIGH:
        return BASE_THRESHOLD * 1.5
    return BASE_THRESHOLD


async def send_alert(text: str, context: ContextTypes.DEFAULT_TYPE, reply_markup=None):
    chat_id = int(os.getenv("CHAT_ID"))
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


def build_confirmation_keyboard(action: str, ticker: str) -> InlineKeyboardMarkup:
    yes_data = f"CONFIRM|{action}|{ticker}|YES"
    no_data = f"CONFIRM|{action}|{ticker}|NO"
    keyboard = [[InlineKeyboardButton("Yes", callback_data=yes_data),
                 InlineKeyboardButton("No", callback_data=no_data)]]
    return InlineKeyboardMarkup(keyboard)


def build_resetall_keyboard() -> InlineKeyboardMarkup:
    yes_data = "RESETALL|YES"
    no_data = "RESETALL|NO"
    keyboard = [[InlineKeyboardButton("Yes", callback_data=yes_data),
                 InlineKeyboardButton("No", callback_data=no_data)]]
    return InlineKeyboardMarkup(keyboard)


async def check_one_commodity(ticker: str, name: str, context: ContextTypes.DEFAULT_TYPE):
    state = load_state(ticker)

    try:
        current_price, vol = get_volatility_and_price(ticker)
    except Exception as e:
        print(f"Error fetching price/vol for {name} ({ticker}): {e}")
        return

    if current_price is None:
        print(f"No price data for {name} ({ticker})")
        return

    threshold_pct = 0.01 if state.get("test_mode") else adapt_threshold(vol)

    state["threshold_pct"] = threshold_pct
    state["last_price"] = current_price
    state["last_volatility"] = vol
    state["last_updated"] = datetime.now(timezone.utc).isoformat()

    last_buy = state["last_buy_price"]
    last_sell = state["last_sell_price"]
    pending_order = state["pending_order"]

    if last_buy is None and last_sell is None:
        state["last_buy_price"] = current_price
        last_buy = current_price
        print(f"Initialized baseline for {name} at £{current_price:.2f}")

    if pending_order is not None:
        save_state(ticker, state)
        return

    if last_buy is not None and current_price > last_buy:
        if state.get("ignore_sell_until_recovery"):
            print(f"{name} ({ticker}) — price recovered above last BUY, re-enabling SELL signals.")
        state["ignore_sell_until_recovery"] = False

    buy_trigger = last_sell * (1 + threshold_pct) if last_sell else None
    sell_trigger = None

    if last_buy is not None and not state.get("ignore_sell_until_recovery", False):
        sell_trigger = last_buy * (1 - threshold_pct)

    state["buy_trigger"] = buy_trigger
    state["sell_trigger"] = sell_trigger

    if buy_trigger is not None and current_price >= buy_trigger:
        state["pending_order"] = "BUY"
        state["pending_price"] = current_price
        msg = (
            f"{name} ({ticker}) — BUY signal.\n\n"
            f"Last sell: £{last_sell:.2f}\n"
            f"Trigger: £{buy_trigger:.2f}\n"
            f"Current: £{current_price:.2f}\n\n"
            f"Did you BUY {name} now?"
        )
        await send_alert(msg, context, reply_markup=build_confirmation_keyboard("BUY", ticker))

    elif sell_trigger is not None and current_price <= sell_trigger:
        state["pending_order"] = "SELL"
        state["pending_price"] = current_price
        msg = (
            f"{name} ({ticker}) — SELL signal.\n\n"
            f"Last buy: £{last_buy:.2f}\n"
            f"Trigger: £{sell_trigger:.2f}\n"
            f"Current: £{current_price:.2f}\n\n"
            f"Did you SELL {name} now?"
        )
        await send_alert(msg, context, reply_markup=build_confirmation_keyboard("SELL", ticker))

    save_state(ticker, state)


async def check_all(context: ContextTypes.DEFAULT_TYPE):
    for ticker, name in COMMODITIES.items():
        try:
            await check_one_commodity(ticker, name, context)
        except Exception as e:
            print(f"Error checking {name} ({ticker}): {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "Bot is running.",
        "Commands:",
        "/status – show current state",
        "/setholding <ticker> <amount>",
        "/updateholding <ticker> <delta>",
        "/setbuy <ticker> <price>",
        "/setsell <ticker> <price>",
        "/reset <ticker>",
        "/resetall",
        "/test – run a 1% test cycle on Gold",
    ]
    await update.message.reply_text("\n".join(lines))


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = []
    for ticker, name in COMMODITIES.items():
        s = load_state(ticker)
        if s["last_price"] is None:
            parts.append(f"{name} ({ticker}): No data yet.")
            continue

        msg = [
            f"{name} ({ticker})",
            f"Price: £{s['last_price']:.2f}",
            f"Holding: £{s['holding_value']:.2f}",
            f"Threshold: {s['threshold_pct']*100:.2f}%",
        ]

        if s["last_buy_price"] is not None:
            msg.append(f"Last BUY: £{s['last_buy_price']:.2f}")
        if s["last_sell_price"] is not None:
            msg.append(f"Last SELL: £{s['last_sell_price']:.2f}")

        if s["buy_trigger"] is not None:
            msg.append(f"BUY trigger: £{s['buy_trigger']:.2f}")
        if s["sell_trigger"] is not None:
            msg.append(f"SELL trigger: £{s['sell_trigger']:.2f}")

        if s["pending_order"]:
            msg.append(f"Pending: {s['pending_order']} at £{s['pending_price']:.2f}")

        if s["ignore_sell_until_recovery"]:
            msg.append("SELL signals ignored until recovery.")

        if s["test_mode"]:
            msg.append("TEST MODE ACTIVE")

        msg.append(f"Updated: {s['last_updated']} UTC")

        parts.append("\n".join(msg))

    await update.message.reply_text("\n\n".join(parts))


def parse_ticker_and_value(args):
    if len(args) != 2:
        return None, None
    ticker = args[0].upper()
    try:
        value = float(args[1])
    except ValueError:
        return ticker, None
    return ticker, value


async def setbuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ticker, price = parse_ticker_and_value(context.args)
    if ticker not in COMMODITIES:
        await update.message.reply_text("Unknown ticker.")
        return
    if price is None:
        await update.message.reply_text("Price must be a number.")
        return

    s = load_state(ticker)
    s["last_buy_price"] = price
    save_state(ticker, s)

    await update.message.reply_text(f"BUY price for {ticker} set to £{price:.2f}.")


async def setsell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ticker, price = parse_ticker_and_value(context.args)
    if ticker not in COMMODITIES:
        await update.message.reply_text("Unknown ticker.")
        return
    if price is None:
        await update.message.reply_text("Price must be a number.")
        return

    s = load_state(ticker)
    s["last_sell_price"] = price
    save_state(ticker, s)

    await update.message.reply_text(f"SELL price for {ticker} set to £{price:.2f}.")


async def reset_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /reset <ticker>")
        return

    ticker = context.args[0].upper()
    if ticker not in COMMODITIES:
        await update.message.reply_text("Unknown ticker.")
        return

    save_state(ticker, default_state())
    await update.message.reply_text(f"{ticker} reset.")


async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Are you sure you want to wipe ALL commodities?",
        reply_markup=build_resetall_keyboard()
    )


async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    # RESETALL confirmation
    if data.startswith("RESETALL"):
        _, answer = data.split("|")
        if answer == "YES":
            for ticker in COMMODITIES:
                save_state(ticker, default_state())
            await query.edit_message_text("All commodities reset.")
        else:
            await query.edit_message_text("Reset cancelled.")
        return

    # BUY/SELL confirmation
    try:
        prefix, action, ticker, answer = data.split("|")
    except ValueError:
        await query.edit_message_text("Invalid confirmation.")
        return

    if prefix != "CONFIRM":
        await query.edit_message_text("Unknown action.")
        return

    s = load_state(ticker)
    pending_order = s.get("pending_order")
    pending_price = s.get("pending_price")
    name = COMMODITIES[ticker]

    if pending_order is None:
        await query.edit_message_text("No pending order.")
        return

    if answer == "YES":
        if action == "BUY":
            s["last_buy_price"] = pending_price
            msg = f"{name} — BUY confirmed at £{pending_price:.2f}."
        elif action == "SELL":
            s["last_sell_price"] = pending_price
            s["ignore_sell_until_recovery"] = False
            msg = f"{name} — SELL confirmed at £{pending_price:.2f}."
        else:
            msg = "Mismatch."
    else:
        msg = f"{name} — action cancelled."
        if action == "SELL":
            s["ignore_sell_until_recovery"] = True

    s["pending_order"] = None
    s["pending_price"] = None

    if s.get("test_mode"):
        original = s.get("original_threshold", BASE_THRESHOLD)
        s["threshold_pct"] = original
        s["test_mode"] = False
        msg += f"\nTEST COMPLETE — threshold restored to {original*100:.2f}%."

    save_state(ticker, s)
    await query.edit_message_text(msg)


if __name__ == "__main__":
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("setbuy", setbuy))
    app.add_handler(CommandHandler("setsell", setsell))
    app.add_handler(CommandHandler("reset", reset_one))
    app.add_handler(CommandHandler("resetall", resetall))
    app.add_handler(CommandHandler("setholding", setholding))
    app.add_handler(CommandHandler("updateholding", updateholding))
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CallbackQueryHandler(handle_confirmation))

    app.job_queue.run_repeating(check_all, interval=300, first=5)

    print("Upgraded bot started — polling Telegram…")
    app.run_polling()
