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
# 🔁 MAPOWANIE SYMBOLI
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
# 🧠 EVALUATE TRADE (RULE-BASED CORE)
# ==================================================
def evaluate_trade(parsed: dict, live_price: float):
    """
    Zwraca decyzję na podstawie reguł
    """

    decision = "NO_TRADE"
    reasons = []
    score = 0

    # 1️⃣ Confidence
    if parsed["confidence"] == "HIGH":
        score += 1
    else:
        reasons.append("Low confidence")

    # 2️⃣ Placeholder SMA200 (docelowo z historycznych danych)
    sma200_trend = "above"  # symulacja
    if parsed["action"] == "buy" and sma200_trend == "above":
        score += 1
    elif parsed["action"] == "sell" and sma200_trend == "below":
        score += 1
    else:
        reasons.append("Against SMA200")

    # 3️⃣ Placeholder stochastic
    stochastic_signal = "bullish"  # symulacja
    if parsed["action"] == "buy" and stochastic_signal == "bullish":
        score += 1
    elif parsed["action"] == "sell" and stochastic_signal == "bearish":
        score += 1
    else:
        reasons.append("Stochastic not aligned")

    # ✅ Decyzja końcowa
    if score >= 3:
        decision = "BUY" if parsed["action"] == "buy" else "SELL"

    return {
        "decision": decision,
        "score": score,
        "reasons": reasons
    }

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

    live_price = None
    evaluation = None

    if parsed and parsed["symbol"] != "UNKNOWN":
        try:
            live_price = get_live_price(parsed["symbol"])
            print("✅ Live price:", live_price)

            evaluation = evaluate_trade(parsed, live_price)
            print("🧪 Evaluation:", evaluation)

        except Exception as e:
            print("❌ Error:", e)

    return {
        "status": "ok",
        "parsed": parsed,
        "live_price": live_price,
        "evaluation": evaluation
    }
