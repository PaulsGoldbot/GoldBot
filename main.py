import os
import json
import yfinance as yf
from datetime import datetime
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

BASE_THRESHOLD = 0.02  # 2% base
VOL_LOW = 0.01         # low volatility threshold (1% daily std)
VOL_HIGH = 0.03        # high volatility threshold (3% daily std)

# List of commodities to track
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


def state_file_for(ticker: str) -> str:
    return f"state_{ticker.replace('.', '_')}.json"


def default_state() -> dict:
    return {
        "last_price": None,
        "last_buy_price": None,
        "last_sell_price": None,
        "holding_value": 0.0,
        "threshold_pct": BASE_THRESHOLD,
        "pending_order": None,      # "BUY" or "SELL" or None
        "pending_price": None,
        "buy_trigger": None,
        "sell_trigger": None,
        "last_volatility": None,
        "last_updated": None,
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

    # Ensure all keys exist (fresh structure)
    base = default_state()
    base.update(data)
    return base


def save_state(ticker: str, state: dict) -> None:
    filename = state_file_for(ticker)
    with open(filename, "w") as f:
        json.dump(state, f)


def get_price(ticker: str) -> float:
    data = yf.Ticker(ticker)
    hist = data.history(period="1d")
    price = hist["Close"].iloc[-1]
    return float(price)


def get_volatility_and_price(ticker: str):
    """
    Returns (current_price, daily_volatility_std) using last 10 days.
    If volatility cannot be computed, returns (price, None).
    """
    data = yf.Ticker(ticker)
    hist = data.history(period="11d")  # need at least 2 points for returns
    if hist.empty or len(hist["Close"]) < 2:
        price = float(hist["Close"].iloc[-1]) if not hist.empty else None
        return price, None

    closes = hist["Close"]
    returns = closes.pct_change().dropna()
    vol = float(returns.std()) if not returns.empty else None
    current_price = float(closes.iloc[-1])
    return current_price, vol


def adapt_threshold(volatility: float | None) -> float:
    """
    Simple volatility-adaptive threshold:
    - vol < VOL_LOW  -> slightly lower threshold (more sensitive)
    - vol > VOL_HIGH -> higher threshold (more conservative)
    - otherwise      -> base threshold
    """
    if volatility is None:
        return BASE_THRESHOLD

    if volatility < VOL_LOW:
        return max(0.01, BASE_THRESHOLD * 0.75)  # e.g. 1.5% if base is 2%
    if volatility > VOL_HIGH:
        return BASE_THRESHOLD * 1.5              # e.g. 3% if base is 2%
    return BASE_THRESHOLD


async def send_alert(text: str, context: ContextTypes.DEFAULT_TYPE, reply_markup=None):
    chat_id = int(os.getenv("CHAT_ID"))
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


def build_confirmation_keyboard(action: str, ticker: str) -> InlineKeyboardMarkup:
    # action: "BUY" or "SELL"
    yes_data = f"CONFIRM|{action}|{ticker}|YES"
    no_data = f"CONFIRM|{action}|{ticker}|NO"
    keyboard = [
        [
            InlineKeyboardButton("Yes", callback_data=yes_data),
            InlineKeyboardButton("No", callback_data=no_data),
        ]
    ]
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

    threshold_pct = adapt_threshold(vol)
    state["threshold_pct"] = threshold_pct
    state["last_price"] = current_price
    state["last_volatility"] = vol
    state["last_updated"] = datetime.utcnow().isoformat()

    last_buy = state["last_buy_price"]
    last_sell = state["last_sell_price"]
    pending_order = state["pending_order"]
    pending_price = state["pending_price"]

    print(
        f"Checking {name} ({ticker})… "
        f"Price: {current_price:.4f}, "
        f"Last buy: {last_buy}, Last sell: {last_sell}, "
        f"Threshold: {threshold_pct:.4f}, Vol: {vol}"
    )

    # If an order is already pending, do not trigger another
    if pending_order is not None:
        save_state(ticker, state)
        return

    # Determine triggers based on last buy/sell
    buy_trigger = None
    sell_trigger = None

    if last_sell is not None:
        buy_trigger = last_sell * (1 + threshold_pct)
        state["buy_trigger"] = buy_trigger

    if last_buy is not None:
        sell_trigger = last_buy * (1 - threshold_pct)
        state["sell_trigger"] = sell_trigger

    # BUY signal: price has risen X% above last sell
    if buy_trigger is not None and current_price >= buy_trigger:
        state["pending_order"] = "BUY"
        state["pending_price"] = current_price

        msg = (
            f"{name} ({ticker}) — BUY signal.\n\n"
            f"Rule: Wait {threshold_pct*100:.2f}% above your last SELL price.\n"
            f"Last sell: £{last_sell:.2f}\n"
            f"Trigger:  £{buy_trigger:.2f}\n"
            f"Current:  £{current_price:.2f}\n\n"
            f"Did you BUY {name} now?"
        )
        keyboard = build_confirmation_keyboard("BUY", ticker)
        await send_alert(msg, context, reply_markup=keyboard)

    # SELL signal: price has fallen X% below last buy
    elif sell_trigger is not None and current_price <= sell_trigger:
        state["pending_order"] = "SELL"
        state["pending_price"] = current_price

        msg = (
            f"{name} ({ticker}) — SELL signal.\n\n"
            f"Rule: Wait {threshold_pct*100:.2f}% below your last BUY price.\n"
            f"Last buy: £{last_buy:.2f}\n"
            f"Trigger:  £{sell_trigger:.2f}\n"
            f"Current:  £{current_price:.2f}\n\n"
            f"Did you SELL {name} now?"
        )
        keyboard = build_confirmation_keyboard("SELL", ticker)
        await send_alert(msg, context, reply_markup=keyboard)

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
        "I check these commodities every 5 minutes using your adaptive 2% rule:",
    ]
    for ticker, name in COMMODITIES.items():
        lines.append(f"- {name} ({ticker})")
    lines.append("\nCommands:")
    lines.append("/status – show current state")
    lines.append("/setholding <ticker> <amount> – set holding value in £")
    lines.append("/updateholding <ticker> <delta> – adjust holding by +/− amount")
    await update.message.reply_text("\n".join(lines))


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = []
    for ticker, name in COMMODITIES.items():
        state = load_state(ticker)
        price = state["last_price"]
        last_buy = state["last_buy_price"]
        last_sell = state["last_sell_price"]
        holding = state["holding_value"]
        threshold_pct = state["threshold_pct"]
        buy_trigger = state["buy_trigger"]
        sell_trigger = state["sell_trigger"]
        pending_order = state["pending_order"]
        vol = state["last_volatility"]
        updated = state["last_updated"]

        if price is None:
            parts.append(f"{name} ({ticker}): No price data yet.")
            continue

        msg_lines = [
            f"{name} ({ticker})",
            f"Current price: £{price:.2f}",
            f"Holding: £{holding:.2f}",
            f"Threshold: {threshold_pct*100:.2f}%",
        ]

        if vol is not None:
            msg_lines.append(f"Volatility (10d std): {vol*100:.2f}%")

        if last_buy is not None:
            msg_lines.append(f"Last BUY: £{last_buy:.2f}")
        if last_sell is not None:
            msg_lines.append(f"Last SELL: £{last_sell:.2f}")

        if buy_trigger is not None:
            msg_lines.append(f"BUY trigger:  £{buy_trigger:.2f}")
        if sell_trigger is not None:
            msg_lines.append(f"SELL trigger: £{sell_trigger:.2f}")

        if pending_order is not None:
            msg_lines.append(f"Pending order: {pending_order} at £{state['pending_price']:.2f}")

        if updated is not None:
            msg_lines.append(f"Last updated: {updated} UTC")

        parts.append("\n".join(msg_lines))

    await update.message.reply_text("\n\n".join(parts))


def parse_ticker_and_amount(args):
    if len(args) != 2:
        return None, None
    ticker = args[0].upper()
    try:
        amount = float(args[1])
    except ValueError:
        return ticker, None
    return ticker, amount


async def setholding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setholding <ticker> <amount>")
        return

    ticker, amount = parse_ticker_and_amount(context.args)
    if ticker not in COMMODITIES:
        await update.message.reply_text("Unknown ticker. Use one of: " + ", ".join(COMMODITIES.keys()))
        return
    if amount is None:
        await update.message.reply_text("Amount must be a number. Example: /setholding SGLN.L 400")
        return

    state = load_state(ticker)
    state["holding_value"] = amount
    save_state(ticker, state)

    await update.message.reply_text(
        f"Holding for {COMMODITIES[ticker]} ({ticker}) set to £{amount:.2f}."
    )


async def updateholding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /updateholding <ticker> <delta>")
        return

    ticker, delta = parse_ticker_and_amount(context.args)
    if ticker not in COMMODITIES:
        await update.message.reply_text("Unknown ticker. Use one of: " + ", ".join(COMMODITIES.keys()))
        return
    if delta is None:
        await update.message.reply_text("Delta must be a number. Example: /updateholding SGLN.L +50")
        return

    state = load_state(ticker)
    state["holding_value"] = float(state.get("holding_value", 0.0)) + delta
    save_state(ticker, state)

    await update.message.reply_text(
        f"Holding for {COMMODITIES[ticker]} ({ticker}) updated by £{delta:.2f}. "
        f"New holding: £{state['holding_value']:.2f}."
    )


async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data  # "CONFIRM|BUY|SGLN.L|YES"
    try:
        prefix, action, ticker, answer = data.split("|")
    except ValueError:
        await query.edit_message_text("Invalid confirmation data.")
        return

    if prefix != "CONFIRM":
        await query.edit_message_text("Unknown action.")
        return

    if ticker not in COMMODITIES:
        await query.edit_message_text("Unknown ticker in confirmation.")
        return

    state = load_state(ticker)
    pending_order = state.get("pending_order")
    pending_price = state.get("pending_price")

    name = COMMODITIES[ticker]

    if pending_order is None or pending_price is None:
        await query.edit_message_text(
            f"{name} ({ticker}) — no pending order found. Maybe already handled."
        )
        return

    if answer == "YES":
        if action == "BUY" and pending_order == "BUY":
            state["last_buy_price"] = pending_price
            # After a confirmed BUY, you are in the market; holding value is managed by your commands
            msg = (
                f"{name} ({ticker}) — BUY confirmed.\n"
                f"Recorded BUY price: £{pending_price:.2f}.\n"
                f"Use /setholding or /updateholding to adjust your £ holding."
            )
        elif action == "SELL" and pending_order == "SELL":
            state["last_sell_price"] = pending_price
            # After a confirmed SELL, you are out of the market; we do not force holding to 0
            msg = (
                f"{name} ({ticker}) — SELL confirmed.\n"
                f"Recorded SELL price: £{pending_price:.2f}.\n"
                f"Use /setholding or /updateholding if you changed your £ holding."
            )
        else:
            msg = f"{name} ({ticker}) — mismatch between pending order and confirmation."
    else:  # answer == "NO"
        msg = (
            f"{name} ({ticker}) — you chose NO.\n"
            f"The signal is ignored; no BUY/SELL recorded."
        )

    # Clear pending state regardless of YES/NO
    state["pending_order"] = None
    state["pending_price"] = None
    save_state(ticker, state)

    await query.edit_message_text(msg)


if __name__ == "__main__":
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("setholding", setholding))
    app.add_handler(CommandHandler("updateholding", updateholding))
    app.add_handler(CallbackQueryHandler(handle_confirmation, pattern=r"^CONFIRM\|"))

    # Check all commodities every 5 minutes
    app.job_queue.run_repeating(check_all, interval=300, first=5)

    print("Upgraded bot started — polling Telegram…")
    app.run_polling()
