import os
import re
import requests
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# ==================================================
# 🔐 ENV
# ==================================================
WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

# ==================================================
# 🔁 SYMBOL MAP
# ==================================================
SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
}

# ==================================================
# 🔎 PARSER
# ==================================================
def parse_signal(text: str):
    if not text:
        return None

    t = text.lower()

    action = "buy" if "buy" in t else "sell" if "sell" in t else None
    symbol = "XAUUSD" if "xauusd" in t else "UNKNOWN"

    size_match = re.search(r"@\s*([0-9.]+)", t)
    size = float(size_match.group(1)) if size_match else None

    tf_match = re.search(r"\((m\d+)\)", t)
    timeframe = tf_match.group(1).upper() if tf_match else None

    confidence = "HIGH" if "high" in t else "NORMAL"

    return {
        "symbol": symbol,
        "action": action,
        "size": size,
        "timeframe": timeframe,
        "confidence": confidence,
        "raw": text
    }

# ==================================================
# 📈 LIVE PRICE
# ==================================================
def get_live_price(symbol):
    r = requests.get(
        "https://api.twelvedata.com/price",
        params={
            "symbol": SYMBOL_MAP[symbol],
            "apikey": TWELVE_API_KEY
        },
        timeout=5
    )
    return float(r.json()["price"])

# ==================================================
# 📊 SMA200
# ==================================================
def get_sma200(symbol, timeframe):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": SYMBOL_MAP[symbol],
            "interval": timeframe.lower(),
            "outputsize": 200,
            "apikey": TWELVE_API_KEY
        },
        timeout=8
    )

    candles = r.json().get("values", [])
    closes = [float(c["close"]) for c in candles]

    if len(closes) < 200:
        raise Exception("Not enough data for SMA200")

    return sum(closes) / len(closes)

# ==================================================
# 📉 STOCHASTIC (14,3)
# ==================================================
def get_stochastic(symbol, timeframe):
    r = requests.get(
        "https://api.twelvedata.com/stochastic",
        params={
            "symbol": SYMBOL_MAP[symbol],
            "interval": timeframe.lower(),
            "apikey": TWELVE_API_KEY
        },
        timeout=8
    )

    values = r.json().get("values", [])
    if len(values) < 2:
        raise Exception("Not enough stochastic data")

    k_now = float(values[0]["k"])
    k_prev = float(values[1]["k"])

    return k_now, k_prev

# ==================================================
# 🧠 EVALUATE TRADE (PRO CORE)
# ==================================================
def evaluate_trade(parsed, price, sma200, k_now, k_prev):
    if parsed["confidence"] != "HIGH":
        return {"decision": "REJECT", "reason": "Low confidence"}

    # BUY LOGIC
    if parsed["action"] == "buy":
        if price <= sma200:
            return {"decision": "REJECT", "reason": "Price below SMA200"}
        if not (k_now < 20 and k_now > k_prev):
            return {"decision": "REJECT", "reason": "Stochastic not rising from oversold"}
        return {"decision": "BUY", "reason": "Trend + momentum confirmed"}

    # SELL LOGIC
    if parsed["action"] == "sell":
        if price >= sma200:
            return {"decision": "REJECT", "reason": "Price above SMA200"}
        if not (k_now > 80 and k_now < k_prev):
            return {"decision": "REJECT", "reason": "Stochastic not falling from overbought"}
        return {"decision": "SELL", "reason": "Trend + momentum confirmed"}

    return {"decision": "REJECT", "reason": "Invalid action"}

# ==================================================
# 🌐 WEBHOOK
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):
    token = request.query_params.get("token")
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    body = await request.body()
    text = body.decode() if body else ""

    print("📩 Webhook:", text)

    parsed = parse_signal(text)
    print("🧠 Parsed:", parsed)

    try:
        price = get_live_price(parsed["symbol"])
        sma200 = get_sma200(parsed["symbol"], parsed["timeframe"])
        k_now, k_prev = get_stochastic(parsed["symbol"], parsed["timeframe"])

        decision = evaluate_trade(parsed, price, sma200, k_now, k_prev)

    except Exception as e:
        decision = {"decision": "ERROR", "reason": str(e)}
        price = sma200 = None

    print("⚙️ Decision:", decision)

    return {
        "status": "ok",
        "parsed": parsed,
        "decision": decision,
        "live_price": price,
        "sma200": sma200,
        "stochastic": {
            "k_now": k_now if 'k_now' in locals() else None,
            "k_prev": k_prev if 'k_prev' in locals() else None
        }
    }
