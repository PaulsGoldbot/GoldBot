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

# 4 fixed pots per commodity (percent moves)
POT_CONFIG = {
    "A": 4.0,
    "B": 6.0,
    "C": 8.0,
    "D": 10.0,
}


# -----------------------------
# PRICE NORMALISATION (PERMANENT FIX)
# -----------------------------

def normalize_price(p):
    """
    Permanent fix for yfinance scaling issues.
    Ensures prices always return to the correct ETF scale.
    """

    if p is None:
        return None

    # Case 1: yfinance returns NAV × 100 or pence (e.g., 7776 instead of 77.76)
    if p > 500:
        return p / 100

    # Case 2: yfinance returns NAV × 1000 (rare)
    if p > 5000:
        return p / 1000

    # Case 3: yfinance returns £0.75 instead of £75
    if p < 1:
        return p * 100

    # Case 4: already correct
    return p


# -----------------------------
# STATE HANDLING
# -----------------------------

def state_file_for(ticker: str) -> str:
    return f"state_{ticker.replace('.', '_')}.json"


def default_pots() -> dict:
    # One structure per pot A–D
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
        # 4 fixed pots per commodity
        "pots": default_pots(),
        # for pot engine: which pot is pending (if any)
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

    # Ensure pots structure exists and has all pots
    if "pots" not in base or not isinstance(base["pots"], dict):
        base["pots"] = default_pots()
    else:
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
# PRICE + VOLATILITY FETCH
# -----------------------------

def get_volatility_and_price(ticker: str):
    """
    Fetches price + 10-day volatility and applies permanent scale normalisation.
    """

    data = yf.Ticker(ticker)
    hist = data.history(period="11d")

    if hist.empty or len(hist["Close"]) < 2:
        price = float(hist["Close"].iloc[-1]) if not hist.empty else None
        return normalize_price(price), None

    closes = hist["Close"].astype(float)

    # Normalise all historical prices
    closes = closes.apply(normalize_price)

    returns = closes.pct_change().dropna()
    vol = float(returns.std()) if not returns.empty else None
    current_price = float(closes.iloc[-1])

    if vol is not None:
        vol = min(vol, VOL_MAX)

    return current_price, vol


# -----------------------------
# THRESHOLD ADAPTATION
# -----------------------------

def adapt_threshold(volatility: float | None) -> float:
    if volatility is None:
        return BASE_THRESHOLD
    if volatility < VOL_LOW:
        return max(0.01, BASE_THRESHOLD * 0.75)
    if volatility > VOL_HIGH:
        return BASE_THRESHOLD * 1.5
    return BASE_THRESHOLD


# -----------------------------
# ALERT + KEYBOARD HELPERS
# -----------------------------

async def send_alert(text: str, context: ContextTypes.DEFAULT_TYPE, reply_markup=None):
    chat_id = int(os.getenv("CHAT_ID"))
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


def build_confirmation_keyboard(action: str, ticker: str) -> InlineKeyboardMarkup:
    # Normal engine confirmation
    yes_data = f"CONFIRM|{action}|{ticker}|YES"
    no_data = f"CONFIRM|{action}|{ticker}|NO"
    keyboard = [[InlineKeyboardButton("Yes", callback_data=yes_data),
                 InlineKeyboardButton("No", callback_data=no_data)]]
    return InlineKeyboardMarkup(keyboard)


def build_pot_confirmation_keyboard(action: str, ticker: str, pot: str) -> InlineKeyboardMarkup:
    # Pot engine confirmation
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

    # Apply test mode threshold or adaptive threshold
    threshold_pct = 0.01 if state.get("test_mode") else adapt_threshold(vol)

    state["threshold_pct"] = threshold_pct
    state["last_price"] = current_price
    state["last_volatility"] = vol
    state["last_updated"] = datetime.now(timezone.utc).isoformat()

    last_buy = state["last_buy_price"]
    last_sell = state["last_sell_price"]
    pending_order = state["pending_order"]

    # First-time baseline
    if last_buy is None and last_sell is None:
        state["last_buy_price"] = current_price
        last_buy = current_price
        print(f"Initialized baseline for {name} at £{current_price:.2f}")

    # If waiting for user confirmation, do nothing
    if pending_order is not None:
        save_state(ticker, state)
        return

    # Re-enable SELL signals after recovery (normal engine)
    if last_buy is not None and current_price > last_buy:
        if state.get("ignore_sell_until_recovery"):
            print(f"{name} ({ticker}) — price recovered above last BUY, re-enabling SELL signals.")
        state["ignore_sell_until_recovery"] = False

    # -----------------------------
    # NORMAL ENGINE (0–3%) — CORRECTED
    # -----------------------------
    # BUY trigger (after a SELL): buy when price DROPS by threshold from last SELL
    buy_trigger = last_sell * (1 - threshold_pct) if last_sell else None

    # SELL trigger (after a BUY): sell when price RISES by threshold from last BUY
    sell_trigger = None
    if last_buy is not None and not state.get("ignore_sell_until_recovery", False):
        sell_trigger = last_buy * (1 + threshold_pct)

    state["buy_trigger"] = buy_trigger
    state["sell_trigger"] = sell_trigger

    # NORMAL BUY signal
    if buy_trigger is not None and current_price <= buy_trigger:
        state["pending_order"] = "BUY"
        state["pending_price"] = current_price
        msg = (
            f"{name} ({ticker}) — BUY signal (normal engine).\n\n"
            f"Last sell: £{last_sell:.2f}\n"
            f"Trigger: £{buy_trigger:.2f}\n"
            f"Current: £{current_price:.2f}\n\n"
            f"Did you BUY {name} now?"
        )
        await send_alert(msg, context, reply_markup=build_confirmation_keyboard("BUY", ticker))

    # NORMAL SELL signal
    elif sell_trigger is not None and current_price >= sell_trigger:
        state["pending_order"] = "SELL"
        state["pending_price"] = current_price
        msg = (
            f"{name} ({ticker}) — SELL signal (normal engine).\n\n"
            f"Last buy: £{last_buy:.2f}\n"
            f"Trigger: £{sell_trigger:.2f}\n"
            f"Current: £{current_price:.2f}\n\n"
            f"Did you SELL {name} now?"
        )
        await send_alert(msg, context, reply_markup=build_confirmation_keyboard("SELL", ticker))

    # -----------------------------
    # POT ENGINE (4/6/8/10%)
    # -----------------------------
    # Only fire pot signals if no normal pending order
    if state["pending_order"] is None:
        pots = state.get("pots", {})
        for pot_name, pct in POT_CONFIG.items():
            p = pots.get(pot_name, {})
            last_buy_price = p.get("last_buy_price")
            last_buy_amount = p.get("last_buy_amount")
            last_sell_price = p.get("last_sell_price")
            holding = p.get("holding", False)

            # POT SELL: if holding and price up by pct from last_buy_price
            if holding and last_buy_price is not None:
                target_sell = last_buy_price * (1 + pct / 100.0)
                if current_price >= target_sell:
                    # Estimate grown amount based on pct
                    grown_amount = (
                        last_buy_amount * (1 + pct / 100.0)
                        if last_buy_amount is not None
                        else None
                    )
                    p["last_sell_price"] = current_price
                    p["last_grown_amount"] = grown_amount
                    p["holding"] = False

                    msg_lines = [
                        f"{name} ({ticker}) — SELL signal — Pot {pot_name} (+{pct:.1f}%).",
                    ]
                    if last_buy_amount is not None:
                        msg_lines.append(f"Last buy amount: £{last_buy_amount:.2f}")
                    if grown_amount is not None:
                        msg_lines.append(f"Estimated grown amount: £{grown_amount:.2f}")
                    msg_lines.append(f"Did you SELL {name} — Pot {pot_name}?")

                    state["pending_order"] = "POT_SELL"
                    state["pending_price"] = current_price
                    state["pending_pot"] = pot_name

                    await send_alert(
                        "\n".join(msg_lines),
                        context,
                        reply_markup=build_pot_confirmation_keyboard("SELL", ticker, pot_name),
                    )
                    break  # one signal at a time

            # POT BUY: if not holding and price down by pct from last_sell_price
            if (not holding) and last_sell_price is not None and state["pending_order"] is None:
                target_buy = last_sell_price * (1 - pct / 100.0)
                if current_price <= target_buy:
                    grown_amount = p.get("last_grown_amount")
                    msg_lines = [
                        f"{name} ({ticker}) — BUY signal — Pot {pot_name} (-{pct:.1f}%).",
                    ]
                    if grown_amount is not None:
                        msg_lines.append(f"Last grown amount: £{grown_amount:.2f}")
                    msg_lines.append(f"Did you BUY {name} — Pot {pot_name}?")

                    state["pending_order"] = "POT_BUY"
                    state["pending_price"] = current_price
                    state["pending_pot"] = pot_name

                    await send_alert(
                        "\n".join(msg_lines),
                        context,
                        reply_markup=build_pot_confirmation_keyboard("BUY", ticker, pot_name),
                    )
                    break  # one signal at a time

        state["pots"] = pots

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
        "Bot is running.",
        "Commands:",
        "/status – show current state",
        "/setholding <ticker> <amount>",
        "/updateholding <ticker> <delta>",
        "/setbuy <ticker> <price>",
        "/setsell <ticker> <price>",
        "/setpot <ticker> <pot> <amount>",
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
            f"Holding (normal): £{s['holding_value']:.2f}",
            f"Threshold (normal): {s['threshold_pct']*100:.2f}%",
        ]

        if s["last_buy_price"] is not None:
            msg.append(f"Last BUY (normal): £{s['last_buy_price']:.2f}")
        if s["last_sell_price"] is not None:
            msg.append(f"Last SELL (normal): £{s['last_sell_price']:.2f}")

        if s["buy_trigger"] is not None:
            msg.append(f"BUY trigger (normal): £{s['buy_trigger']:.2f}")
        if s["sell_trigger"] is not None:
            msg.append(f"SELL trigger (normal): £{s['sell_trigger']:.2f}")

        if s["pending_order"]:
            if s.get("pending_pot"):
                msg.append(
                    f"Pending: {s['pending_order']} (Pot {s['pending_pot']}) at £{s['pending_price']:.2f}"
                )
            else:
                msg.append(f"Pending: {s['pending_order']} at £{s['pending_price']:.2f}")

        if s["ignore_sell_until_recovery"]:
            msg.append("SELL signals ignored until recovery (normal).")

        if s["test_mode"]:
            msg.append("TEST MODE ACTIVE")

        # Pot status
        pots = s.get("pots", {})
        for pot_name, pct in POT_CONFIG.items():
            p = pots.get(pot_name, {})
            line = [f"Pot {pot_name} ({pct:.1f}%):"]
            lbp = p.get("last_buy_price")
            lba = p.get("last_buy_amount")
            lsp = p.get("last_sell_price")
            lga = p.get("last_grown_amount")
            holding = p.get("holding", False)

            if holding:
                line.append("STATE: HOLDING")
            else:
                line.append("STATE: SOLD")

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


def parse_ticker_and_value(args):
    if len(args) != 2:
        return None, None
    ticker = args[0].upper()
    try:
        value = float(args[1])
    except ValueError:
        return ticker, None
    return ticker, value


async def setholding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setholding <ticker> <amount>")
        return

    ticker = context.args[0].upper()
    if ticker not in COMMODITIES:
        await update.message.reply_text("Unknown ticker.")
        return

    try:
        amount = float(context.args[1])
    except:
        await update.message.reply_text("Amount must be a number.")
        return

    s = load_state(ticker)
    s["holding_value"] = amount
    save_state(ticker, s)

    await update.message.reply_text(f"Holding for {ticker} set to £{amount:.2f}.")


async def updateholding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /updateholding <ticker> <delta>")
        return

    ticker = context.args[0].upper()
    if ticker not in COMMODITIES:
        await update.message.reply_text("Unknown ticker.")
        return

    try:
        delta = float(context.args[1])
    except:
        await update.message.reply_text("Delta must be a number.")
        return

    s = load_state(ticker)
    s["holding_value"] = float(s.get("holding_value", 0.0)) + delta
    save_state(ticker, s)

    await update.message.reply_text(
        f"Holding for {ticker} updated by £{delta:.2f}. "
        f"New holding: £{s['holding_value']:.2f}."
    )


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


async def setpot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manually override a pot's amount if needed.
    Usage: /setpot <ticker> <pot> <amount>
    Example: /setpot SSLN.L A 13.80
    """
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /setpot <ticker> <pot> <amount>")
        return

    ticker = context.args[0].upper()
    pot = context.args[1].upper()
    if ticker not in COMMODITIES:
        await update.message.reply_text("Unknown ticker.")
        return
    if pot not in POT_CONFIG:
        await update.message.reply_text("Pot must be one of: A, B, C, D.")
        return

    try:
        amount = float(context.args[2])
    except:
        await update.message.reply_text("Amount must be a number.")
        return

    s = load_state(ticker)
    pots = s.get("pots", {})
    p = pots.get(pot, {})
    p["last_buy_amount"] = amount
    p["holding"] = True
    pots[pot] = p
    s["pots"] = pots
    save_state(ticker, s)

    await update.message.reply_text(
        f"Pot {pot} for {ticker} set to amount £{amount:.2f} (marked as HOLDING)."
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


# -----------------------------
# CONFIRMATION HANDLER
# -----------------------------

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    # RESETALL
    if data.startswith("RESETALL"):
        _, answer = data.split("|")
        if answer == "YES":
            for ticker in COMMODITIES:
                save_state(ticker, default_state())
            await query.edit_message_text("All commodities reset.")
        else:
            await query.edit_message_text("Reset cancelled.")
        return

    # POT confirmations
    if data.startswith("POT"):
        try:
            prefix, action, ticker, pot, answer = data.split("|")
        except ValueError:
            await query.edit_message_text("Invalid pot confirmation.")
            return

        if ticker not in COMMODITIES or pot not in POT_CONFIG:
            await query.edit_message_text("Unknown ticker or pot.")
            return

        s = load_state(ticker)
        name = COMMODITIES[ticker]
        pots = s.get("pots", {})
        p = pots.get(pot, {})
        pending_order = s.get("pending_order")
        pending_price = s.get("pending_price")
        pending_pot = s.get("pending_pot")

        if pending_order is None or pending_pot != pot:
            await query.edit_message_text("No pending pot order.")
            return

        if answer == "YES":
            if action == "BUY":
                # One-step: use grown amount automatically if available
                grown_amount = p.get("last_grown_amount")
                p["last_buy_price"] = pending_price
                if grown_amount is not None:
                    p["last_buy_amount"] = grown_amount
                p["holding"] = True
                pots[pot] = p
                s["pots"] = pots
                msg = (
                    f"{name} — Pot {pot} BUY confirmed at £{pending_price:.2f}.\n"
                )
                if grown_amount is not None:
                    msg += f"Pot amount set to last grown amount: £{grown_amount:.2f}."
                else:
                    msg += "Pot amount recorded without grown amount (no previous cycle)."
            elif action == "SELL":
                p["last_sell_price"] = pending_price
                p["holding"] = False
                pots[pot] = p
                s["pots"] = pots
                msg = f"{name} — Pot {pot} SELL confirmed at £{pending_price:.2f}."
            else:
                msg = "Mismatch."
        else:
            msg = f"{name} — Pot {pot} action cancelled."

        s["pending_order"] = None
        s["pending_price"] = None
        s["pending_pot"] = None

        # Reset test mode if active
        if s.get("test_mode"):
            original = s.get("original_threshold", BASE_THRESHOLD)
            s["threshold_pct"] = original
            s["test_mode"] = False
            msg += f"\nTEST COMPLETE — threshold restored to {original*100:.2f}%."

        save_state(ticker, s)
        await query.edit_message_text(msg)
        return

    # BUY/SELL confirmation (normal engine)
    try:
        prefix, action, ticker, answer = data.split("|")
    except ValueError:
        await query.edit_message_text("Invalid confirmation.")
        return

    if prefix != "CONFIRM":
        await query.edit_message_text("Unknown action.")
        return

    if ticker not in COMMODITIES:
        await query.edit_message_text("Unknown ticker.")
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
    s["pending_pot"] = None

    # Reset test mode
    if s.get("test_mode"):
        original = s.get("original_threshold", BASE_THRESHOLD)
        s["threshold_pct"] = original
        s["test_mode"] = False
        msg += f"\nTEST COMPLETE — threshold restored to {original*100:.2f}%."

    save_state(ticker, s)
    await query.edit_message_text(msg)


# -----------------------------
# TEST MODE COMMAND
# -----------------------------

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ticker = TEST_TICKER
    name = COMMODITIES[ticker]

    state = load_state(ticker)
    state["original_threshold"] = state.get("threshold_pct", BASE_THRESHOLD)
    state["threshold_pct"] = 0.01
    state["test_mode"] = True
    save_state(ticker, state)

    await update.message.reply_text(
        f"TEST MODE ENABLED for {name} ({ticker}).\n"
        f"Threshold forced to 1%.\n"
        f"The bot will trigger a BUY/SELL cycle as soon as conditions are met.\n"
        f"After confirmation, threshold will automatically reset."
    )


# -----------------------------
# MAIN APPLICATION SETUP
# -----------------------------

if __name__ == "__main__":
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("setbuy", setbuy))
    app.add_handler(CommandHandler("setsell", setsell))
    app.add_handler(CommandHandler("reset", reset_one))
    app.add_handler(CommandHandler("resetall", resetall))
    app.add_handler(CommandHandler("setholding", setholding))
    app.add_handler(CommandHandler("updateholding", updateholding))
    app.add_handler(CommandHandler("setpot", setpot))
    app.add_handler(CommandHandler("test", test_command))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(handle_confirmation))

    # Background price checks every 5 minutes
    app.job_queue.run_repeating(check_all, interval=300, first=5)

    print("Upgraded bot started — polling Telegram…")
    app.run_polling()
