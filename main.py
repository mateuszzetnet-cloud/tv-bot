import os
import re
import json
import requests
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
}

LOG_FILE = "trades_log.jsonl"

# ==================================================
# 🔎 PARSER
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
# 📈 PRICE + SMA
# ==================================================
def get_live_price(symbol: str):
    r = requests.get(
        "https://api.twelvedata.com/price",
        params={"symbol": SYMBOL_MAP[symbol], "apikey": TWELVE_API_KEY},
        timeout=5
    )
    return float(r.json()["price"])

def get_sma200(symbol: str, interval="15min"):
    r = requests.get(
        "https://api.twelvedata.com/sma",
        params={
            "symbol": SYMBOL_MAP[symbol],
            "interval": interval,
            "time_period": 200,
            "apikey": TWELVE_API_KEY
        },
        timeout=5
    )
    data = r.json()
    if "values" in data:
        return float(data["values"][0]["sma"])
    if interval == "15min":
        return get_sma200(symbol, "1h")
    return None

# ==================================================
# 🧠 EVALUATE
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
# 🧾 LOGGING
# ==================================================
def log_trade(data: dict):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(data) + "\n")

def load_trades():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r") as f:
        return [json.loads(line) for line in f]

# ==================================================
# 📊 STATS
# ==================================================
def calculate_stats(trades):
    stats = {
        "total": len(trades),
        "approved": 0,
        "rejected": 0,
        "rejection_reasons": {},
        "confidence": {"HIGH": 0, "NORMAL": 0},
        "sma_relation": {
            "buy_above": 0,
            "buy_below": 0,
            "sell_above": 0,
            "sell_below": 0,
        }
    }

    for t in trades:
        stats[t["decision"]] += 1
        stats["confidence"][t["confidence"]] += 1

        for r in t["reasons"]:
            stats["rejection_reasons"][r] = stats["rejection_reasons"].get(r, 0) + 1

        if t["sma200"]:
            if t["action"] == "buy":
                stats["sma_relation"]["buy_above" if t["price"] > t["sma200"] else "buy_below"] += 1
            if t["action"] == "sell":
                stats["sma_relation"]["sell_above" if t["price"] > t["sma200"] else "sell_below"] += 1

    return stats

# ==================================================
# 🌐 WEBHOOK
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):
    token = request.query_params.get("token")
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    text = (await request.body()).decode("utf-8")
    parsed = parse_signal(text)
    if not parsed:
        return {"status": "ignored"}

    price = get_live_price(parsed["symbol"])
    sma200 = get_sma200(parsed["symbol"])
    decision, reasons = evaluate_trade(parsed, price, sma200)

    log_trade({
        "time": datetime.utcnow().isoformat(),
        **parsed,
        "price": price,
        "sma200": sma200,
        "decision": decision,
        "reasons": reasons
    })

    return {"status": "ok", "decision": decision, "reasons": reasons}

# ==================================================
# 📊 STATS ENDPOINT
# ==================================================
@app.get("/stats")
def stats():
    trades = load_trades()
    return calculate_stats(trades)
