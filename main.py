import os
import re
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# 🔐 Sekret z Railway → Variables
SECRET = os.getenv("WEBHOOK_TOKEN")


# =========================
# 🔎 PARSER SYGNAŁU
# =========================
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

    # wielkość pozycji (np. @ 0.2)
    size_match = re.search(r"@\s*([0-9.]+)", text_lower)
    size = float(size_match.group(1)) if size_match else None

    # timeframe (np. M15)
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


# =========================
# 🌐 WEBHOOK
# =========================
@app.post("/webhook")
async def webhook(request: Request):
    token = request.query_params.get("token")

    # 1️⃣ Zabezpieczenie
    if token != SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    # 2️⃣ Odczyt body (działa też gdy EMPTY)
    raw_body = await request.body()
    text = raw_body.decode("utf-8") if raw_body else "EMPTY"

    # 3️⃣ Log surowy
    print("📩 Webhook received")
    print("Raw body:", text)

    # 4️⃣ PARSOWANIE
    parsed = parse_signal(text)
    print("🧠 Parsed signal:", parsed)

    return {
        "status": "ok",
        "parsed": parsed
    }
