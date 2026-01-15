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
# ⚙️ RISK
# ==================================================
ACCOUNT_BALANCE = 10_000
RISK_PERCENT = 0.01

# ==================================================
# SYMBOL MAP
# ==================================================
SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
}

# ==================================================
# PARSER
# ==================================================
def parse_signal(text):
    if not text:
        return None

    t = text.lower()
    return {
        "symbol": "XAUUSD" if "xauusd" in t else "UNKNOWN",
        "action": "buy" if "buy" in t else "sell" if "sell" in t else None,
        "confidence": "HIGH" if "high" in t else "NORMAL",
        "timeframe": "M15"
    }

# ==================================================
# DATA
# ==================================================
def td_request(endpoint, params):
    r = requests.get(
        f"https://api.twelvedata.com/{endpoint}",
        params={**params, "apikey": TWELVE_API_KEY},
        timeout=5
    )
    return r.json()

def get_price(symbol):
    return float(td_request("price", {"symbol": SYMBOL_MAP[symbol]})["price"])

def get_sma200(symbol):
    data = td_request("sma", {
        "symbol": SYMBOL_MAP[symbol],
        "interval": "H1",
        "time_period": 200
    })
    return float(data["values"][-1]["sma"])

def get_stochastic(symbol):
    data = td_request("stoch", {
        "symbol": SYMBOL_MAP[symbol],
        "interval": "M15"
    })
    last = data["values"][-1]
    return float(last["slow_k"]), float(last["slow_d"])

def get_atr(symbol):
    data = td_request("atr", {
        "symbol": SYMBOL_MAP[symbol],
        "interval": "M15",
        "time_period": 14
    })
    return float(data["values"][-1]["atr"])

# ==================================================
# TRADE FILTER
# ==================================================
def evaluate_trade(parsed, price):
    reasons = []

    if parsed["confidence"] != "HIGH":
        reasons.append("confidence_not_high")

    sma200 = get_sma200(parsed["symbol"])
    stochastic_k, stochastic_d = get_stochastic(parsed["symbol"])

    if parsed["action"] == "buy":
        if price <= sma200:
            reasons.append("price_below_sma200")
        if stochastic_k > 20:
            reasons.append("stochastic_not_oversold")

    if parsed["action"] == "sell":
        if price >= sma200:
            reasons.append("price_above_sma200")
        if stochastic_k < 80:
            reasons.append("stochastic_not_overbought")

    return reasons

# ==================================================
# SL / TP / SIZE
# ==================================================
def sl_tp(price, atr, action):
    sl = price - atr * 1.5 if action == "buy" else price + atr * 1.5
    tp = price + atr * 3 if action == "buy" else price - atr * 3
    return sl, tp

def position_size(price, sl):
    risk_usd = ACCOUNT_BALANCE * RISK_PERCENT
    return round(risk_usd / (abs(price - sl) * 100), 2)

# ==================================================
# WEBHOOK
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):
    if request.query_params.get("token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)

    text = (await request.body()).decode()
    parsed = parse_signal(text)

    price = get_price(parsed["symbol"])
    atr = get_atr(parsed["symbol"])

    reject_reasons = evaluate_trade(parsed, price)

    if reject_reasons:
        return {
            "status": "rejected",
            "reasons": reject_reasons
        }

    sl, tp = sl_tp(price, atr, parsed["action"])
    size = position_size(price, sl)

    return {
        "status": "approved",
        "symbol": parsed["symbol"],
        "action": parsed["action"],
        "price": round(price, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "size_lots": size
    }
