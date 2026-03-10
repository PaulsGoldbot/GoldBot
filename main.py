import os
import json
import time
import logging
import threading
from datetime import datetime
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================
# CONFIG
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY")

POLL_INTERVAL_SECONDS = 60  # how often to check prices

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

# Pot ladder percentages
POT_CONFIG = {
    "A": {"up": 3.0, "down": 3.0},
    "B": {"up": 4.0, "down": 4.0},
    "C": {"up": 6.0, "down": 6.0},
    "D": {"up": 8.0, "down": 8.0},
    "E": {"up": 10.0, "down": 10.0},
}

# =========================
# LOGGING
# =========================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# =========================
# STATE HANDLING
# =========================

def state_file_path(ticker: str) -> str:
    safe = ticker.replace(".", "_").upper()
    return os.path.join(DATA_DIR, f"{safe}.json")


def load_state(ticker: str) -> dict:
    path = state_file_path(ticker)
    if not os.path.exists(path):
        # fresh state: all pots empty
        state = {
            "ticker": ticker,
            "pots": {
                pot: {
                    "amount": 0.0,
                    "holding": False,
                    "last_buy_price": None,
                    "last_sell_price": None,
                    "last_grown_amount": None,
                }
                for pot in POT_CONFIG.keys()
            },
            "last_price": None,
        }
        save_state(ticker, state)
        return state
    with open(path, "r") as f:
        return json.load(f)


def save_state(ticker: str, state: dict) -> None:
    path = state_file_path(ticker)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


# =========================
# PRICE FETCHING
# =========================

def fetch_price(ticker: str) -> float | None:
    """
    Uses Alpha Vantage GLOBAL_QUOTE for simplicity.
    """
    try:
        url = (
            "https://www.alphavantage.co/query"
            f"?function=GLOBAL_QUOTE&symbol={ticker}&apikey={ALPHA_VANTAGE_API_KEY}"
        )
        resp = requests.get(url, timeout=10)
        data = resp.json()
        price_str = data.get("Global Quote", {}).get("05. price")
        if price_str is None:
            logger.warning(f"No price in response for {ticker}: {data}")
            return None
        return float(price_str)
    except Exception as e:
        logger.error(f"Error fetching price for {ticker}: {e}")
        return None


# =========================
# TELEGRAM HELPERS
# =========================

async def send_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error sending message: {e}")


# =========================
# COMMAND: /start
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot is running with unified A–E pots.\n\n"
        "Key commands:\n"
        "/setpot <ticker> <pot> <amount>\n"
        "/setpotbuy <ticker> <pot> <price>\n"
        "/status <ticker>\n"
        "/reset <ticker>\n"
        "/resetall <ticker>\n"
    )


# =========================
# COMMAND: /setpot
# =========================

async def setpot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setpot SGLN.L A 25
    Sets the amount for a pot and marks it as holding (but does NOT set buy price).
    """
    if len(context.args) != 3:
        await update.message.reply_text(
            "Usage: /setpot <ticker> <pot> <amount>\nExample: /setpot SGLN.L A 25"
        )
        return

    ticker = context.args[0].upper()
    pot = context.args[1].upper()
    try:
        amount = float(context.args[2])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return

    if pot not in POT_CONFIG:
        await update.message.reply_text(f"Pot must be one of: {', '.join(POT_CONFIG.keys())}")
        return

    state = load_state(ticker)
    state["pots"][pot]["amount"] = amount
    state["pots"][pot]["holding"] = True  # you own this amount
    save_state(ticker, state)

    await update.message.reply_text(
        f"Set pot {pot} for {ticker} to amount {amount}.\n"
        f"Note: You still need to set the initial buy price with /setpotbuy."
    )


# =========================
# NEW COMMAND: /setpotbuy
# =========================

async def setpotbuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setpotbuy SGLN.L A 7.435
    Sets the last_buy_price for a pot and keeps it holding.
    This is the one-time initialisation so the bot can calculate SELL triggers.
    """
    if len(context.args) != 3:
        await update.message.reply_text(
            "Usage: /setpotbuy <ticker> <pot> <price>\nExample: /setpotbuy SGLN.L A 7.435"
        )
        return

    ticker = context.args[0].upper()
    pot = context.args[1].upper()
    try:
        price = float(context.args[2])
    except ValueError:
        await update.message.reply_text("Price must be a number.")
        return

    if pot not in POT_CONFIG:
        await update.message.reply_text(f"Pot must be one of: {', '.join(POT_CONFIG.keys())}")
        return

    state = load_state(ticker)
    pot_state = state["pots"][pot]

    if pot_state["amount"] <= 0:
        await update.message.reply_text(
            f"Pot {pot} for {ticker} has no amount set.\n"
            f"Use /setpot {ticker} {pot} <amount> first."
        )
        return

    pot_state["last_buy_price"] = price
    pot_state["holding"] = True  # explicitly confirm we are holding
    save_state(ticker, state)

    await update.message.reply_text(
        f"Initialised pot {pot} for {ticker} with buy price {price}.\n"
        f"Amount: {pot_state['amount']}\n"
        f"The bot can now calculate SELL triggers for this pot."
    )


# =========================
# COMMAND: /status
# =========================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /status SGLN.L
    Shows all pots for a ticker.
    """
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /status <ticker>\nExample: /status SGLN.L")
        return

    ticker = context.args[0].upper()
    state = load_state(ticker)

    lines = [f"*Status for {ticker}*"]
    lines.append(f"Last price: {state['last_price']}")
    lines.append("")

    for pot, cfg in POT_CONFIG.items():
        p = state["pots"][pot]
        lines.append(
            f"*Pot {pot}* — amount: {p['amount']}, holding: {p['holding']}\n"
            f"  last_buy_price: {p['last_buy_price']}\n"
            f"  last_sell_price: {p['last_sell_price']}\n"
            f"  last_grown_amount: {p['last_grown_amount']}\n"
            f"  up: {cfg['up']}%, down: {cfg['down']}%\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# =========================
# COMMAND: /reset
# =========================

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /reset SGLN.L
    Resets all pots for a ticker.
    """
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /reset <ticker>\nExample: /reset SGLN.L")
        return

    ticker = context.args[0].upper()
    path = state_file_path(ticker)
    if os.path.exists(path):
        os.remove(path)
        await update.message.reply_text(f"State for {ticker} has been reset.")
    else:
        await update.message.reply_text(f"No state file found for {ticker}.")


# =========================
# COMMAND: /resetall
# =========================

async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /resetall SGLN.L
    Same as /reset for now (kept for compatibility).
    """
    await reset(update, context)


# =========================
# INLINE CONFIRMATION HANDLER
# (Single pending action at a time per chat)
# =========================

PENDING_ACTIONS = {}  # chat_id -> dict with action details


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data  # e.g. "CONFIRM_SELL|SGLN.L|A|7.65"
    parts = data.split("|")
    if len(parts) < 2:
        return

    action = parts[0]
    if action not in ("CONFIRM_SELL", "CONFIRM_BUY", "CANCEL"):
        return

    chat_id = query.message.chat_id

    if action == "CANCEL":
        PENDING_ACTIONS.pop(chat_id, None)
        await query.edit_message_text("Action cancelled.")
        return

    if chat_id not in PENDING_ACTIONS:
        await query.edit_message_text("No pending action found.")
        return

    pending = PENDING_ACTIONS.pop(chat_id)
    ticker = pending["ticker"]
    pot = pending["pot"]
    price = pending["price"]
    direction = pending["direction"]  # "SELL" or "BUY"

    state = load_state(ticker)
    pot_state = state["pots"][pot]

    if direction == "SELL":
        # SELL: we exit the position, grow amount, clear holding
        amount = pot_state["amount"]
        grown_amount = amount  # you can plug your growth logic here if needed
        pot_state["last_sell_price"] = price
        pot_state["last_grown_amount"] = grown_amount
        pot_state["holding"] = False
        save_state(ticker, state)

        await query.edit_message_text(
            f"✅ SELL confirmed for {ticker} pot {pot} at {price}.\n"
            f"Last grown amount: {grown_amount}"
        )

    elif direction == "BUY":
        # BUY: we re-enter using the grown amount
        grown_amount = pot_state["last_grown_amount"]
        if grown_amount is None:
            grown_amount = pot_state["amount"]

        pot_state["amount"] = grown_amount
        pot_state["last_buy_price"] = price
        pot_state["holding"] = True
        save_state(ticker, state)

        await query.edit_message_text(
            f"✅ BUY confirmed for {ticker} pot {pot} at {price}.\n"
            f"Amount: {grown_amount}"
        )


# =========================
# POT LOGIC
# =========================

def check_pot_signals_for_ticker(ticker: str, price: float, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    For a given ticker and current price, check all pots and send signals if needed.
    A2 behaviour: only one pending action per chat at a time.
    """
    state = load_state(ticker)
    state["last_price"] = price
    save_state(ticker, state)

    # If there's already a pending action for this chat, do nothing.
    if chat_id in PENDING_ACTIONS:
        return

    for pot, cfg in POT_CONFIG.items():
        p = state["pots"][pot]
        amount = p["amount"]
        holding = p["holding"]
        last_buy_price = p["last_buy_price"]
        last_sell_price = p["last_sell_price"]

        up_pct = cfg["up"]
        down_pct = cfg["down"]

        # If we are holding and have a buy price, check for SELL
        if holding and last_buy_price is not None and amount > 0:
            target_sell = last_buy_price * (1 + up_pct / 100.0)
            if price >= target_sell:
                # Trigger SELL signal
                text = (
                    f"*SELL signal*\n\n"
                    f"{ticker} pot {pot}\n"
                    f"Current price: {price}\n"
                    f"Last buy price: {last_buy_price}\n"
                    f"Target: +{up_pct}% → {target_sell:.4f}\n\n"
                    f"Amount: {amount}\n"
                    f"Last grown amount: {p['last_grown_amount']}\n\n"
                    f"Confirm SELL?"
                )
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ Yes",
                                callback_data=f"CONFIRM_SELL|{ticker}|{pot}|{price}",
                            ),
                            InlineKeyboardButton(
                                "❌ No",
                                callback_data="CANCEL",
                            ),
                        ]
                    ]
                )

                PENDING_ACTIONS[chat_id] = {
                    "ticker": ticker,
                    "pot": pot,
                    "price": price,
                    "direction": "SELL",
                }

                # send async
                asyncio_run_coroutine_threadsafe(
                    context.bot.send_message(
                        chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="Markdown"
                    )
                )
                return  # only one action at a time

        # If we are NOT holding but have a last_sell_price, check for BUY
        if not holding and last_sell_price is not None:
            target_buy = last_sell_price * (1 - down_pct / 100.0)
            if price <= target_buy:
                text = (
                    f"*BUY signal*\n\n"
                    f"{ticker} pot {pot}\n"
                    f"Current price: {price}\n"
                    f"Last sell price: {last_sell_price}\n"
                    f"Target: -{down_pct}% → {target_buy:.4f}\n\n"
                    f"Last grown amount: {p['last_grown_amount']}\n\n"
                    f"Confirm BUY?"
                )
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ Yes",
                                callback_data=f"CONFIRM_BUY|{ticker}|{pot}|{price}",
                            ),
                            InlineKeyboardButton(
                                "❌ No",
                                callback_data="CANCEL",
                            ),
                        ]
                    ]
                )

                PENDING_ACTIONS[chat_id] = {
                    "ticker": ticker,
                    "pot": pot,
                    "price": price,
                    "direction": "BUY",
                }

                asyncio_run_coroutine_threadsafe(
                    context.bot.send_message(
                        chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="Markdown"
                    )
                )
                return  # only one action at a time


# =========================
# BACKGROUND POLLING
# =========================

import asyncio

def asyncio_run_coroutine_threadsafe(coro):
    loop = asyncio.get_event_loop()
    if loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, loop)
    else:
        loop.run_until_complete(coro)


def polling_loop(app, chat_id: int, tickers: list[str]):
    """
    Background loop that periodically checks prices for all tickers.
    """
    async def _tick(context: ContextTypes.DEFAULT_TYPE):
        for ticker in tickers:
            price = fetch_price(ticker)
            if price is None:
                continue
            check_pot_signals_for_ticker(ticker, price, chat_id, context)

    async def _loop():
        while True:
            try:
                await _tick(app.bot)
            except Exception as e:
                logger.error(f"Error in polling loop: {e}")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    asyncio_run_coroutine_threadsafe(_loop())


# =========================
# MAIN
# =========================

def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setpot", setpot))
    application.add_handler(CommandHandler("setpotbuy", setpotbuy))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("resetall", resetall))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # You can hardcode your chat_id and tickers here,
    # or later we can add commands to manage them dynamically.
    chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
    tickers_env = os.getenv("TICKERS", "")
    tickers = [t.strip().upper() for t in tickers_env.split(",") if t.strip()]

    if chat_id != 0 and tickers:
        threading.Thread(
            target=polling_loop, args=(application, chat_id, tickers), daemon=True
        ).start()
        logger.info(f"Started polling loop for tickers: {tickers} to chat_id {chat_id}")
    else:
        logger.warning(
            "No TELEGRAM_CHAT_ID or TICKERS set. Polling loop will not start.\n"
            "Set TELEGRAM_CHAT_ID and TICKERS in Railway variables."
        )

    application.run_polling()


if __name__ == "__main__":
    main()
