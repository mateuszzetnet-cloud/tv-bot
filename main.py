import os
import re
import requests
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# ==================================================
# 🔐 ZMIENNE ŚRODOWISKOWE
# ==================================================
WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

# ==================================================
# 🔁 MAPOWANIE SYMBOLI (TradingView → TwelveData)
# ==================================================
SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
    "BTCUSDT": "BTC/USD",
    "ETHUSDT": "ETH/USD",
    "EURUSD": "EUR/USD",
}

# ==================================================
# 🔎 PARSER SYGNAŁU
# ==================================================
def parse_signal(text: str):
    if not text or text == "EMPTY":
        return None

    text_lower = text.lower()

    action = None
    if "buy" in text_lower:
        action = "buy"
    elif "sell" in text_lower:
        action = "sell"

    symbol_match = re.search(r"(xauusd|eurusd|btcusdt|ethusdt)", text_lower)
    symbol = symbol_match.group(1).upper() if symbol_match else "UNKNOWN"

    size_match = re.search(r"@\s*([0-9.]+)", text_lower)
    size = float(size_match.group(1)) if size_match else None

    tf_match = re.search(r"\((m\d+)\)", text_lower)
    timeframe = tf_match.group(1).upper() if tf_match else None

    confidence = "HIGH" if "high" in text_lower else "NORMAL"

    return {
        "source": "tradingview",
        "symbol": symbol,
        "action": action,
        "size": size,
        "timeframe": timeframe,
        "confidence": confidence,
        "raw": text
    }

# ==================================================
# 📈 TWELVE DATA – LIVE PRICE
# ==================================================
def get_live_price(symbol: str):
    if not TWELVE_API_KEY:
        raise Exception("TWELVE_API_KEY not set")

    mapped_symbol = SYMBOL_MAP.get(symbol, symbol)
    print(f"📈 TwelveData symbol: {mapped_symbol}")

    url = "https://api.twelvedata.com/price"
    params = {
        "symbol": mapped_symbol,
        "apikey": TWELVE_API_KEY
    }

    r = requests.get(url, params=params, timeout=5)
    data = r.json()

    if "price" not in data:
        raise Exception(data)

    return float(data["price"])

# ==================================================
# 🧠 ETAP 1 – ENGINE DECYZYJNY
# ==================================================
def evaluate_trade(parsed: dict):
    """
    Zwraca:
    {
        decision: BUY / SELL / REJECT
        reason: tekst
    }
    """

    if not parsed:
        return {"decision": "REJECT", "reason": "Empty signal"}

    if parsed["symbol"] == "UNKNOWN":
        return {"decision": "REJECT", "reason": "Unknown symbol"}

    if parsed["action"] not in ("buy", "sell"):
        return {"decision": "REJECT", "reason": "No action"}

    if parsed["confidence"] != "HIGH":
        return {"decision": "REJECT", "reason": "Low confidence"}

    # ETAP 1 → jeśli przeszedł sanity + confidence
    if parsed["action"] == "buy":
        return {"decision": "BUY", "reason": "Basic rules passed"}

    if parsed["action"] == "sell":
        return {"decision": "SELL", "reason": "Basic rules passed"}

    return {"decision": "REJECT", "reason": "Fallback reject"}

# ==================================================
# 🌐 WEBHOOK
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):
    token = request.query_params.get("token")

    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    raw_body = await request.body()
    text = raw_body.decode("utf-8") if raw_body else "EMPTY"

    print("📩 Webhook received")
    print("Raw body:", text)

    parsed = parse_signal(text)
    print("🧠 Parsed signal:", parsed)

    decision = evaluate_trade(parsed)
    print("⚙️ Decision:", decision)

    price = None
    if parsed and parsed["symbol"] != "UNKNOWN":
        try:
            price = get_live_price(parsed["symbol"])
            print("✅ Live price:", price)
        except Exception as e:
            print("❌ TwelveData error:", e)

    return {
        "status": "ok",
        "parsed": parsed,
        "decision": decision,
        "live_price": price
    }
