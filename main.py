from fastapi import FastAPI, Request
from datetime import datetime
import pandas as pd
import os

app = FastAPI()

DATA_DIR = "data"
DATA_FILE = f"{DATA_DIR}/signals.csv"

os.makedirs(DATA_DIR, exist_ok=True)

# inicjalizacja pliku
if not os.path.exists(DATA_FILE):
    df = pd.DataFrame(columns=[
        "timestamp",
        "symbol",
        "timeframe",
        "strategy",
        "direction",
        "price",
        "raw_payload"
    ])
    df.to_csv(DATA_FILE, index=False)


@app.get("/")
def root():
    return {"status": "ok", "service": "tradingview-webhook"}


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    row = {
        "timestamp": datetime.utcnow().isoformat(),
        "symbol": data.get("symbol"),
        "timeframe": data.get("timeframe"),
        "strategy": data.get("strategy"),
        "direction": data.get("direction"),
        "price": data.get("price"),
        "raw_payload": str(data)
    }

    df = pd.DataFrame([row])
    df.to_csv(DATA_FILE, mode="a", header=False, index=False)

    print("WEBHOOK SAVED:", row)

    return {"received": True}
