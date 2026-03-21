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

# -----------------------------
# CONFIG
# -----------------------------

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

# Pot ladder A–E
POT_CONFIG = {
    "A": 3.0,
    "B": 4.0,
    "C": 6.0,
    "D": 8.0,
    "E": 10.0,
}

# -----------------------------
# PRICE NORMALISATION (SAFE)
# -----------------------------
# Logging-only version: NO SCALING APPLIED.
# We return the raw price exactly as Yahoo Finance gives it.

def normalize_price(raw_price: float, ticker: str):
    if raw_price is None:
        return None
    try:
        return float(raw_price)
    except:
        return None

# -----------------------------
# STATE HANDLING
# -----------------------------

def state_file_for(ticker: str) -> str:
    return f"state_{ticker.replace('.', '_')}.json"

def default_pots() -> dict:
    pots = {}
    for pot_name in POT_CONFIG.keys():
        pots[pot_name] = {
            "last_buy_price": None,
            "last_buy_amount": None,
            "last_sell_price": None,
            "last_grown_amount": None,
            "holding": False,
        }
    return pots

def default_state() -> dict:
    return {
        "last_price": None,
        "last_volatility": None,
        "last_updated": None,
        "pots": default_pots(),
        "pending_order": None,
        "pending_price": None,
        "pending_pot": None,
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

    for pot_name in POT_CONFIG.keys():
        if pot_name not in base["pots"]:
            base["pots"][pot_name] = {
                "last_buy_price": None,
                "last_buy_amount": None,
                "last_sell_price": None,
                "last_grown_amount": None,
                "holding": False,
            }

    return base

def save_state(ticker: str, state: dict) -> None:
    filename = state_file_for(ticker)
    with open(filename, "w") as f:
        json.dump(state, f)

# -----------------------------
# PRICE + VOLATILITY FETCH (LOGGING ONLY)
# -----------------------------

def get_volatility_and_price(ticker: str):
    data = yf.Ticker(ticker)
    hist = data.history(period="11d")

    # If no history or only one close
    if hist.empty or len(hist["Close"]) < 2:
        raw_price = float(hist["Close"].iloc[-1]) if not hist.empty else None
        print(f"[{ticker}] RAW latest close: {raw_price}")
        price = normalize_price(raw_price, ticker)
        print(f"[{ticker}] NORMALISED (raw passthrough): {price}")
        return price, None

    closes_raw = hist["Close"].astype(float)

    # Log last 3 raw closes
    try:
        tail_raw = list(closes_raw.tail(3))
        print(f"[{ticker}] RAW closes (last 3): {tail_raw}")
    except Exception as e:
        print(f"[{ticker}] Error logging raw closes: {e}")

    # NO SCALING — raw passthrough
    closes = closes_raw.apply(lambda p: normalize_price(p, ticker))

    returns = closes.pct_change().dropna()
    vol = float(returns.std()) if not returns.empty else None
    current_price = float(closes.iloc[-1])

    print(f"[{ticker}] NORMALISED current price (raw passthrough): {current_price}, volatility: {vol}")

    return current_price, vol

# -----------------------------
# ALERT + KEYBOARD HELPERS
# -----------------------------

async def send_alert(text: str, context: ContextTypes.DEFAULT_TYPE, reply_markup=None):
    chat_id = int(os.getenv("CHAT_ID"))
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

def build_pot_confirmation_keyboard(action: str, ticker: str, pot: str) -> InlineKeyboardMarkup:
    yes_data = f"POT|{action}|{ticker}|{pot}|YES"
    no_data = f"POT|{action}|{ticker}|{pot}|NO"
    keyboard = [[InlineKeyboardButton("Yes", callback_data=yes_data),
                 InlineKeyboardButton("No", callback_data=no_data)]]
    return InlineKeyboardMarkup(keyboard)

def build_resetall_keyboard() -> InlineKeyboardMarkup:
    yes_data = "RESETALL|YES"
    no_data = "RESETALL|NO"
    keyboard = [[InlineKeyboardButton("Yes", callback_data=yes_data),
                 InlineKeyboardButton("No", callback_data=no_data)]]
    return InlineKeyboardMarkup(keyboard)

# -----------------------------
# UNIFIED POT ENGINE (UNCHANGED)
# -----------------------------

async def run_pot_engine(ticker: str, name: str, current_price: float, state: dict,
                         context: ContextTypes.DEFAULT_TYPE):

    if state.get("pending_order") is not None:
        return

    pots = state.get("pots", {})

    for pot_name, pct in POT_CONFIG.items():
        p = pots.get(pot_name, {})
        last_buy_price = p.get("last_buy_price")
        last_buy_amount = p.get("last_buy_amount")
        last_sell_price = p.get("last_sell_price")
        holding = p.get("holding", False)

        # SELL LOGIC
        if holding and last_buy_price is not None:
            target_sell = last_buy_price * (1 + pct / 100.0)

            if current_price >= target_sell:
                grown_amount = (
                    last_buy_amount * (1 + pct / 100.0)
                    if last_buy_amount is not None
                    else None
                )

                p["last_sell_price"] = current_price
                p["last_grown_amount"] = grown_amount
                p["holding"] = False
                pots[pot_name] = p

                msg = [
                    f"{name} ({ticker}) — SELL signal triggered — Pot {pot_name} (+{pct:.1f}%).",
                ]

                if last_buy_amount is not None:
                    msg.append(f"Original pot amount: £{last_buy_amount:.2f}")

                if grown_amount is not None:
                    msg.append(f"Grown pot amount: £{grown_amount:.2f}")
                    msg.append(f"➡️ SELL £{grown_amount:.2f} at approx £{current_price:.2f}")

                msg.append(f"Confirm SELL for {name} — Pot {pot_name}")

                state["pending_order"] = "POT_SELL"
                state["pending_price"] = current_price
                state["pending_pot"] = pot_name

                await send_alert(
                    "\n".join(msg),
                    context,
                    reply_markup=build_pot_confirmation_keyboard("SELL", ticker, pot_name),
                )
                break

        # BUY LOGIC
        if (not holding) and last_sell_price is not None and state.get("pending_order") is None:
            target_buy = last_sell_price * (1 - pct / 100.0)

            if current_price <= target_buy:
                grown_amount = p.get("last_grown_amount")

                msg = [
                    f"{name} ({ticker}) — BUY signal triggered — Pot {pot_name} (-{pct:.1f}%).",
                ]

                if grown_amount is not None:
                    msg.append(f"Reinvest amount: £{grown_amount:.2f}")
                    msg.append(f"➡️ BUY £{grown_amount:.2f} at approx £{current_price:.2f}")

                msg.append(f"Confirm BUY for {name} — Pot {pot_name}")

                state["pending_order"] = "POT_BUY"
                state["pending_price"] = current_price
                state["pending_pot"] = pot_name

                await send_alert(
                    "\n".join(msg),
                    context,
                    reply_markup=build_pot_confirmation_keyboard("BUY", ticker, pot_name),
                )
                break

    state["pots"] = pots

# -----------------------------
# MAIN CHECK LOGIC
# -----------------------------

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

    state["last_price"] = current_price
    state["last_volatility"] = vol
    state["last_updated"] = datetime.now(timezone.utc).isoformat()

    await run_pot_engine(ticker, name, current_price, state, context)

    save_state(ticker, state)

async def check_all(context: ContextTypes.DEFAULT_TYPE):
    for ticker, name in COMMODITIES.items():
        try:
            await check_one_commodity(ticker, name, context)
        except Exception as e:
            print(f"Error checking {name} ({ticker}): {e}")

# -----------------------------
# COMMANDS
# -----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "Bot is running (pots-only, logging-only mode).",
        "Commands:",
        "/status – show current state",
        "/setpot <ticker> <pot> <amount>",
        "/setpotbuy <ticker> <pot> <price>",
        "/reset <ticker>",
        "/resetall",
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
        ]

        if s["last_volatility"] is not None:
            msg.append(f"10-day volatility: {s['last_volatility']*100:.2f}%")

        if s["pending_order"]:
            msg.append(
                f"Pending: {s['pending_order']} (Pot {s['pending_pot']}) at £{s['pending_price']:.2f}"
            )

        pots = s.get("pots", {})
        for pot_name, pct in POT_CONFIG.items():
            p = pots.get(pot_name, {})
            line = [f"Pot {pot_name} ({pct:.1f}%):"]

            holding = p.get("holding", False)
            line.append("STATE: HOLDING" if holding else "STATE: SOLD")

            lbp = p.get("last_buy_price")
            lba = p.get("last_buy_amount")
            lsp = p.get("last_sell_price")
            lga = p.get("last_grown_amount")

            if lba is not None:
                line.append(f"Amount: £{lba:.2f}")
            if lbp is not None:
                line.append(f"Last BUY price: £{lbp:.2f}")
            if lsp is not None:
                line.append(f"Last SELL price: £{lsp:.2f}")
            if lga is not None:
                line.append(f"Last grown amount: £{lga:.2f}")

            msg.append("  " + " | ".join(line))

        msg.append(f"Updated: {s['last_updated']} UTC")
        parts.append("\n".join(msg))

    await update.message.reply_text("\n\n".join(parts))

async def setpot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /setpot <ticker> <pot> <amount>")
        return

    ticker = context.args[0].upper()
    pot = context.args[1].upper()

    if ticker not in COMMODITIES:
        await update.message.reply_text("Unknown ticker.")
        return
    if pot not in POT_CONFIG:
        await update.message.reply_text("Pot must be one of: A, B, C, D, E.")
        return

    try:
        amount = float(context.args[2])
    except:
        await update.message.reply_text("Amount must be a number.")
        return

    s = load_state(ticker)
    p = s["pots"][pot]
    p["last_buy_amount"] = amount
    p["holding"] = True
    s["pots"][pot] = p

    save_state(ticker, s)
    await update.message.reply_text(
        f"Pot {pot} for {ticker} set to amount £{amount:.2f} (marked as HOLDING)."
    )

async def setpotbuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /setpotbuy <ticker> <pot> <price>")
        return

    ticker = context.args[0].upper()
    pot = context.args[1].upper()

    if ticker not in COMMODITIES:
        await update.message.reply_text("Unknown ticker.")
        return
    if pot not in POT_CONFIG:
        await update.message.reply_text("Pot must be one of: A, B, C, D, E.")
        return

    try:
        price = float(context.args[2])
    except:
        await update.message.reply_text("Price must be a number.")
        return

    s = load_state(ticker)
    p = s["pots"][pot]

    if p.get("last_buy_amount") is None:
        await update.message.reply_text(
            f"Pot {pot} for {ticker} has no amount set.\n"
            f"Use /setpot {ticker} {pot} <amount> first."
        )
        return

    p["last_buy_price"] = price
    p["holding"] = True
    s["pots"][pot] = p

    save_state(ticker, s)
    await update.message.reply_text(
        f"Pot {pot} for {ticker} initialised with BUY price £{price:.4f}.\n"
        f"SELL signals will now trigger."
    )

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

    if data.startswith("RESETALL"):
        _, answer = data.split("|")
        if answer == "YES":
            for ticker in COMMODITIES:
                save_state(ticker, default_state())
            await query.edit_message_text("All commodities reset.")
        else:
            await query.edit_message_text("Reset cancelled.")
        return

    if data.startswith("POT"):
        _, action, ticker, pot, answer = data.split("|")

        if ticker not in COMMODITIES or pot not in POT_CONFIG:
            await query.edit_message_text("Unknown ticker or pot.")
            return

        s = load_state(ticker)
        name = COMMODITIES[ticker]
        p = s["pots"][pot]

        if s["pending_order"] is None or s["pending_pot"] != pot:
            await query.edit_message_text("No pending pot order.")
            return

        pending_price = s["pending_price"]

        if answer == "YES":
            if action == "BUY":
                grown_amount = p.get("last_grown_amount")
                p["last_buy_price"] = pending_price
                if grown_amount is not None:
                    p["last_buy_amount"] = grown_amount
                p["holding"] = True
                msg = f"{name} — Pot {pot} BUY confirmed at £{pending_price:.2f}."
            elif action == "SELL":
                p["last_sell_price"] = pending_price
                p["holding"] = False
                msg = f"{name} — Pot {pot} SELL confirmed at £{pending_price:.2f}."
            else:
                msg = "Mismatch."
        else:
            msg = f"{name} — Pot {pot} action cancelled."

        s["pots"][pot] = p
        s["pending_order"] = None
        s["pending_price"] = None
        s["pending_pot"] = None

        save_state(ticker, s)
        await query.edit_message_text(msg)
        return

    await query.edit_message_text("Unknown action.")

# -----------------------------
# MAIN APPLICATION SETUP
# -----------------------------

if __name__ == "__main__":
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset_one))
    app.add_handler(CommandHandler("resetall", resetall))
    app.add_handler(CommandHandler("setpot", setpot))
    app.add_handler(CommandHandler("setpotbuy", setpotbuy))

    app.add_handler(CallbackQueryHandler(handle_confirmation))

    app.job_queue.run_repeating(check_all, interval=300, first=5)

