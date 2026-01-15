import os
import re
import json
import requests
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# ==================================================
# 🔐 ZMIENNE ŚRODOWISKOWE
# ==================================================
WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

# ==================================================
# 🔁 MAPOWANIE SYMBOLI
# ==================================================
SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
}

# ==================================================
# 🔎 PARSER SYGNAŁU
# ==================================================
def parse_signal(text: str):
    if not text or text == "EMPTY":
        return None

    t = text.lower()

    action = "buy" if "buy" in t else "sell" if "sell" in t else None
    symbol_match = re.search(r"(xauusd)", t)
    symbol = symbol_match.group(1).upper() if symbol_match else "UNKNOWN"

    size_match = re.search(r"@\s*([0-9.]+)", t)
    size = float(size_match.group(1)) if size_match else None

    tf_match = re.search(r"\((m\d+)\)", t)
    timeframe = tf_match.group(1).upper() if tf_match else "M15"

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
def get_live_price(symbol: str):
    mapped = SYMBOL_MAP.get(symbol)
    url = "https://api.twelvedata.com/price"
    r = requests.get(url, params={"symbol": mapped, "apikey": TWELVE_API_KEY}, timeout=5)
    return float(r.json()["price"])

# ==================================================
# 📊 SMA200 (M15 → fallback H1)
# ==================================================
def get_sma200(symbol: str, interval="15min"):
    mapped = SYMBOL_MAP.get(symbol)
    url = "https://api.twelvedata.com/sma"
    params = {
        "symbol": mapped,
        "interval": interval,
        "time_period": 200,
        "apikey": TWELVE_API_KEY
    }
    r = requests.get(url, params=params, timeout=5)
    data = r.json()

    if "values" in data:
        return float(data["values"][0]["sma"])

    if interval == "15min":
        return get_sma200(symbol, "1h")

    return None

# ==================================================
# 🧠 EVALUATE TRADE
# ==================================================
def evaluate_trade(parsed, price, sma200):
    reasons = []

    if parsed["confidence"] != "HIGH":
        reasons.append("low_confidence")

    if sma200:
        if parsed["action"] == "buy" and price < sma200:
            reasons.append("price_below_sma200")
        if parsed["action"] == "sell" and price > sma200:
            reasons.append("price_above_sma200")

    decision = "approved" if not reasons else "rejected"
    return decision, reasons

# ==================================================
# 🧾 LEARNING MEMORY (LOG)
# ==================================================
def log_trade(data: dict):
    with open("trades_log.jsonl", "a") as f:
        f.write(json.dumps(data) + "\n")

# ==================================================
# 🌐 WEBHOOK
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):
    token = request.query_params.get("token")
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    body = await request.body()
    text = body.decode("utf-8") if body else "EMPTY"

    parsed = parse_signal(text)
    if not parsed:
        return {"status": "ignored"}

    price = get_live_price(parsed["symbol"])
    sma200 = get_sma200(parsed["symbol"])

    decision, reasons = evaluate_trade(parsed, price, sma200)

    trade_log = {
        "time": datetime.utcnow().isoformat(),
        "symbol": parsed["symbol"],
        "action": parsed["action"],
        "price": price,
        "sma200": sma200,
        "confidence": parsed["confidence"],
        "decision": decision,
        "reasons": reasons
    }

    log_trade(trade_log)

    return {
        "status": "ok",
        "decision": decision,
        "reasons": reasons
    }
