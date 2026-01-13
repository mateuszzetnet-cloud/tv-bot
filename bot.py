from fastapi import FastAPI, Request
import requests
import json
import os
from datetime import datetime, timedelta

app = FastAPI()

# =========================
# 🔐 TELEGRAM
# =========================
TELEGRAM_TOKEN = "8520432441:AAEBpEcOme1hqFpdk5tbWd9Cjfm0e4oII4Y"
CHAT_ID = "5756730815"

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    requests.post(url, json=payload)

# =========================
# 📁 STORAGE
# =========================
DATA_DIR = "data"
TRADES_FILE = f"{DATA_DIR}/trades.json"
WEIGHTS_FILE = f"{DATA_DIR}/weights.json"

os.makedirs(DATA_DIR, exist_ok=True)

if not os.path.exists(TRADES_FILE):
    with open(TRADES_FILE, "w") as f:
        json.dump([], f)

if not os.path.exists(WEIGHTS_FILE):
    with open(WEIGHTS_FILE, "w") as f:
        json.dump({
            "SMC": 1.0,
            "JAPAN": 1.0,
            "SCALP": 1.0,
            "PSND": 1.0,
            "TREND": 1.0,
            "MEAN": 1.0
        }, f)

# =========================
# 📊 HELPERS
# =========================
def load_json(path):
    with open(path, "r") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def now():
    return datetime.utcnow()

# =========================
# 🧠 LEARNING ENGINE
# =========================
def update_weights():
    trades = load_json(TRADES_FILE)
    weights = load_json(WEIGHTS_FILE)

    week_ago = now() - timedelta(days=7)
    recent = [t for t in trades if datetime.fromisoformat(t["time"]) > week_ago]

    stats = {}
    for t in recent:
        stats.setdefault(t["strategy"], []).append(t["pnl"])

    for strat, pnls in stats.items():
        avg = sum(pnls) / len(pnls)
        if avg > 0:
            weights[strat] += 0.1
        else:
            weights[strat] -= 0.1

        weights[strat] = max(0.3, min(weights[strat], 2.0))

    save_json(WEIGHTS_FILE, weights)

# =========================
# 🧮 PNL SIMULATION
# =========================
def simulate_trade(entry, sl, tp, side):
    if side == "LONG":
        return (tp - entry) if tp > entry else (sl - entry)
    else:
        return (entry - tp) if tp < entry else (entry - sl)

# =========================
# 🌐 API
# =========================
@app.get("/")
def root():
    return {"status": "ok"}

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    raw = body.decode("utf-8")

    try:
        data = json.loads(raw)
    except:
        send_telegram("❌ Błąd JSON z TradingView")
        return {"error": "invalid json"}

    symbol = data.get("symbol")
    strategy = data.get("strategy")
    side = data.get("side")
    entry = float(data.get("price"))
    sl = float(data.get("sl"))
    tp = float(data.get("tp"))

    weights = load_json(WEIGHTS_FILE)
    weight = weights.get(strategy, 1.0)

    # ❌ FILTR JAKOŚCI
    if weight < 0.5:
        return {"ignored": "low quality strategy"}

    pnl = simulate_trade(entry, sl, tp, side)

    trade = {
        "symbol": symbol,
        "strategy": strategy,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "pnl": pnl,
        "time": now().isoformat()
    }

    trades = load_json(TRADES_FILE)
    trades.append(trade)
    save_json(TRADES_FILE, trades)

    send_telegram(
        f"📊 {symbol}\n"
        f"🧠 {strategy} (w:{round(weight,2)})\n"
        f"➡️ {side}\n"
        f"Entry: {entry}\n"
        f"SL: {sl}\n"
        f"TP: {tp}\n"
        f"PnL(sim): {round(pnl,2)}"
    )

    update_weights()

    return {"ok": True}
