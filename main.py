import os
import re
import requests
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# ==================================================
# 🔐 ZMIENNE ŚRODOWISKOWE (Railway → Variables)
# ==================================================
WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")


# ==================================================
# 🔎 PARSER SYGNAŁU (TradingView / text)
# ==================================================
def parse_signal(text: str):
    if not text:
        return None

    text_lower = text.lower()

    # akcja
    action = None
    if "buy" in text_lower:
        action = "buy"
    elif "sell" in text_lower:
        action = "sell"

    # symbol
    symbol_match = re.search(r"(xauusd|eurusd|btcusdt|ethusdt)", text_lower)
    symbol = symbol_match.group(1).upper() if symbol_match else "UNKNOWN"

    # size (np. @ 0.2 albo @0.4)
    size_match = re.search(r"@\s*([0-9.]+)", text_lower)
    size = float(size_match.group(1)) if size_match else None

    # timeframe (M1, M5, M15, itp.)
    tf_match = re.search(r"\((m\d+)", text_lower)
    timeframe = tf_match.group(1).upper() if tf_match else None

    # confidence
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
        return None

    url = "https://api.twelvedata.com/price"
    params = {
        "symbol": symbol,
        "apikey": TWELVE_API_KEY
    }

    r = requests.get(url, params=params, timeout=10)
    data = r.json()

    if "price" not in data:
        raise Exception(f"TwelveData error: {data}")

    return float(data["price"])


# ==================================================
# 🌐 WEBHOOK
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):
    token = request.query_params.get("token")

    # 1️⃣ Zabezpieczenie webhooka
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    # 2️⃣ Odczyt body (działa nawet gdy EMPTY)
    raw_body = await request.body()
    text = raw_body.decode("utf-8") if raw_body else "EMPTY"

    # 3️⃣ Log surowy (Railway → Logs)
    print("📩 Webhook received")
    print("Raw body:", text)

    # 4️⃣ Parsowanie sygnału
    parsed = parse_signal(text)
    print("🧠 Parsed signal:", parsed)

    # 5️⃣ Cena rynkowa (jeśli symbol znany)
    price = None
    if parsed and parsed["symbol"] != "UNKNOWN":
        try:
            price = get_live_price(parsed["symbol"])
        except Exception as e:
            print("❌ TwelveData error:", e)

    return {
        "status": "ok",
        "parsed": parsed,
        "live_price": price
    }
