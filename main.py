import yfinance as yf
import json
import os
import requests

BOT_TOKEN = "8304590973:AAG06qnKh1By6Plsnzlfgj3PWMoRtmXUlNI"
CHAT_ID = 8569426510


TICKER = "SGLN.L"
THRESHOLD_PERCENT = 0  # alert when price moves 5%

def get_price():
    data = yf.Ticker(TICKER)
    price = data.history(period="1d")["Close"].iloc[-1]
    return float(price)

def load_last_price():
    if not os.path.exists("state.json"):
        return None
    with open("state.json", "r") as f:
        data = json.load(f)
        return data.get("last_price")

def save_last_price(price):
    with open("state.json", "w") as f:
        json.dump({"last_price": price}, f)

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    requests.post(url, data=payload)


current_price = get_price()
last_price = load_last_price()

print("Current price:", current_price)
print("Last saved price:", last_price)

if last_price is None:
    print("First run — saving price.")
    save_last_price(current_price)
else:
    change = current_price - last_price
    percent_change = (change / last_price) * 100

    print("Price difference:", change)
    print("Percent change:", percent_change)

price_difference = current_price - last_price

price_difference = current_price - last_price

# Trigger when gold moves £50 or more
if abs(price_difference) >= 50:

    if price_difference > 0:
        # Price has risen £50 or more → SELL signal
        alert_text = (
            f"📈 SELL Signal\n\n"
            f"Gold has risen by £{price_difference:.2f}\n"
            f"Old price: £{last_price:.2f}\n"
            f"New price: £{current_price:.2f}"
        )
    else:
        # Price has dropped £50 or more → BUY signal
        alert_text = (
            f"📉 BUY Signal\n\n"
            f"Gold has dropped by £{abs(price_difference):.2f}\n"
            f"Old price: £{last_price:.2f}\n"
            f"New price: £{current_price:.2f}"
        )

    print(alert_text)
    send_message(alert_text)
    save_last_price(current_price)

else:
    print("No alert — gold movement less than £50.")


