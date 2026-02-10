import yfinance as yf
import json
import os
import requests
from flask import Flask

# Dummy web server so Render keeps the service alive
app = Flask(__name__)

@app.route("/")
def home():
    return "GoldBot is running."

# IMPORTANT: Replace this with your NEW regenerated token
BOT_TOKEN = "REPLACE_ME"
CHAT_ID = 8569426510

TICKER = "SGLN.L"

def get_price():
    data = yf.Ticker(TICKER)
    hist = data.history(period="1d")
    if hist.empty:
        return None
    return float(hist["Close"].iloc[-1])

def load_last_price():
    if not os.path.exists("state.json"):
        return None
    try:
        with open("state.json", "r") as f:
            data = json.load(f)
            return data.get("last_price")
    except:
        return None

def save_last_price(price):
    with open("state.json", "w") as f:
        json.dump({"last_price": price}, f)

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    requests.post(url, data=payload)

# -----------------------------
# MAIN LOGIC
# -----------------------------

current_price = get_price()
last_price = load_last_price()

print("Current price:", current_price)
print("Last saved price:", last_price)

# FIRST RUN — no comparison
if last_price is None:
    print("First run — saving price.")
    save_last_price(current_price)
else:
    price_difference = current_price - last_price
    print("Price difference:", price_difference)

    # Trigger when gold moves £50 or more
    if abs(price_difference) >= 50:

        if price_difference > 0:
            alert_text = (
                f"📈 SELL Signal\n\n"
                f"Gold has risen by £{price_difference:.2f}\n"
                f"Old price: £{last_price:.2f}\n"
                f"New price: £{current_price:.2f}"
            )
        else:
            alert_text = (
                f"📉 BUY Signal\n\n"
                f"Gold has dropped by £{abs(price_difference):.2f}\n"
                f"Old price: £{last_price:.2f}\n"
                f"New price: £{current_price:.2f}"
            )

        print(alert_text)
        send_message(alert_text)

    else:
        print("No alert — gold movement less than £50.")

# Always save the new price at the end
save_last_price(current_price)

# Start the dummy web server
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
