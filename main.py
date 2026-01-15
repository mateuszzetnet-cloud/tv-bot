import os
import re
import json
import requests
import logging
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException

# ==================================================
# 🔧 APP + LOGGING
# ==================================================
app = FastAPI()

logging.basicConfig(level=logging.WARNING)

WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

if not WEBHOOK_SECRET or not TWELVE_API_KEY:
    logging.warning("Missing environment variables")

SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
}

LOG_FILE = "trades_log.jsonl"

# ==================================================
# 🔎 PARSER
# ==================================================
def parse_signal(text: str):
    if not text:
        return None

    t = text.lower()

    action = "buy" if "buy" in t else "sell" if "sell" in t else None
    if not action:
        return None

    symbol_match = re.search(r"(xauusd)", t)
    symbol = symbol_match.group(1).upper() if symbol_match else None
    if symbol not in SYMBOL_MAP:
        return None

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
        "raw": text.strip()
    }

# ==================================================
# 📈 MARKET DATA
# ==================================================
def safe_request(url, params):
    try:
        r = requests.get(url, params=params, timeout=5)
        data = r.json()
        if "status" in data and data["status"] == "error":
            return None
        return data
    except Exception:
        return None

def get_live_price(symbol: str):
    data = safe_request(
        "https://api.twelvedata.com/price",
        {"symbol": SYMBOL_MAP[symbol], "apikey": TWELVE_API_KEY}
    )
    if not data or "price" not in data:
        return None
    return float(data["price"])

def get_sma200(symbol: str, interval="15min"):
    data = safe_request(
        "https://api.twelvedata.com/sma",
        {
            "symbol": SYMBOL_MAP[symbol],
            "interval": interval,
            "time_period": 200,
            "apikey": TWELVE_API_KEY
        }
    )

    if data and "values" in data and data["values"]:
        return float(data["values"][0]["sma"])

    # 🔁 fallback M15 → H1
    if interval == "15min":
        return get_sma200(symbol, "1h")

    return None

# ==================================================
# 🧠 EVALUATION
# ==================================================
def evaluate_trade(parsed, price, sma200):
    reasons = []

    if parsed["confidence"] != "HIGH":
        reasons.append("low_confidence")

    if price is None:
        reasons.append("no_price")

    if sma200 is None:
        reasons.append("no_sma200")

    if price and sma200:
        if parsed["action"] == "buy" and price < sma200:
            reasons.append("price_below_sma200")
        if parsed["action"] == "sell" and price > sma200:
            reasons.append("price_above_sma200")

    decision = "approved" if not reasons else "rejected"
    return decision, reasons

# ==================================================
# 🧾 STORAGE
# ==================================================
def log_trade(data: dict):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(data) + "\n")
    except Exception:
        logging.warning("Failed to write trade log")

def load_trades():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r") as f:
        return [json.loads(line) for line in f if line.strip()]

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
    }

    for t in trades:
        stats[t["decision"]] += 1
        stats["confidence"][t["confidence"]] += 1

        for r in t["reasons"]:
            stats["rejection_reasons"][r] = stats["rejection_reasons"].get(r, 0) + 1

    return stats

# ==================================================
# 🌐 WEBHOOK
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):
    token = request.query_params.get("token")
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    body = (await request.body()).decode("utf-8").strip()
    parsed = parse_signal(body)

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

    return {
        "status": "ok",
        "decision": decision,
        "reasons": reasons,
        "price": price,
        "sma200": sma200
    }

# ==================================================
# 📊 STATS ENDPOINT
# ==================================================
@app.get("/stats")
def stats():
    return calculate_stats(load_trades())
