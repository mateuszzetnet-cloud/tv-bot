import os
import re
import sqlite3
import requests
import logging
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException

# ==================================================
# 🔧 APP
# ==================================================
app = FastAPI()
logging.basicConfig(level=logging.INFO)

WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

DB_FILE = "trading.db"
START_BALANCE = 10_000.0

TP_POINTS = 20
SL_POINTS = 10
POINT_VALUE = 1.0  # $ per point per 1 lot

SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
}

# ==================================================
# 🗄️ DATABASE
# ==================================================
def db():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            action TEXT,
            entry_price REAL,
            exit_price REAL,
            lot REAL,
            status TEXT,
            pnl REAL,
            time_open TEXT,
            time_close TEXT
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS balance (
            time TEXT,
            balance REAL
        )
        """)

        cur = con.execute("SELECT COUNT(*) FROM balance")
        if cur.fetchone()[0] == 0:
            con.execute(
                "INSERT INTO balance VALUES (?, ?)",
                (datetime.utcnow().isoformat(), START_BALANCE)
            )

init_db()

# ==================================================
# 🔎 PARSER
# ==================================================
def parse_signal(text: str):
    t = text.lower()

    action = "buy" if "buy" in t else "sell" if "sell" in t else None
    if not action:
        return None

    symbol_match = re.search(r"(xauusd)", t)
    symbol = symbol_match.group(1).upper() if symbol_match else None
    if symbol not in SYMBOL_MAP:
        return None

    size_match = re.search(r"@\s*([0-9.]+)", t)
    lot = float(size_match.group(1)) if size_match else 0.1

    confidence = "HIGH" if "high" in t else "NORMAL"

    return {
        "symbol": symbol,
        "action": action,
        "lot": lot,
        "confidence": confidence,
        "raw": text.strip()
    }

# ==================================================
# 📈 MARKET DATA
# ==================================================
def safe_request(url, params):
    try:
        r = requests.get(url, params=params, timeout=5)
        return r.json()
    except Exception:
        return None

def get_price(symbol):
    data = safe_request(
        "https://api.twelvedata.com/price",
        {"symbol": SYMBOL_MAP[symbol], "apikey": TWELVE_API_KEY}
    )
    return float(data["price"]) if data and "price" in data else None

def get_sma200(symbol, interval="15min"):
    data = safe_request(
        "https://api.twelvedata.com/sma",
        {
            "symbol": SYMBOL_MAP[symbol],
            "interval": interval,
            "time_period": 200,
            "apikey": TWELVE_API_KEY
        }
    )
    if data and "values" in data:
        return float(data["values"][0]["sma"])
    if interval == "15min":
        return get_sma200(symbol, "1h")
    return None

# ==================================================
# 🧠 STRATEGY
# ==================================================
def evaluate_trade(parsed, price, sma200):
    reasons = []

    if parsed["confidence"] != "HIGH":
        reasons.append("low_confidence")

    if price is None or sma200 is None:
        reasons.append("no_market_data")

    if price and sma200:
        if parsed["action"] == "buy" and price < sma200:
            reasons.append("below_sma200")
        if parsed["action"] == "sell" and price > sma200:
            reasons.append("above_sma200")

    return "approved" if not reasons else "rejected", reasons

# ==================================================
# 📊 BALANCE
# ==================================================
def get_balance():
    cur = db().execute("SELECT balance FROM balance ORDER BY time DESC LIMIT 1")
    return cur.fetchone()[0]

def update_balance(delta):
    new_balance = get_balance() + delta
    db().execute(
        "INSERT INTO balance VALUES (?, ?)",
        (datetime.utcnow().isoformat(), new_balance)
    ).connection.commit()

# ==================================================
# 📄 PAPER TRADING ENGINE
# ==================================================
def open_trade(parsed, price):
    with db() as con:
        con.execute("""
        INSERT INTO trades
        (symbol, action, entry_price, lot, status, pnl, time_open)
        VALUES (?, ?, ?, ?, 'OPEN', 0, ?)
        """, (
            parsed["symbol"],
            parsed["action"],
            price,
            parsed["lot"],
            datetime.utcnow().isoformat()
        ))

def close_trade(trade_id, exit_price, pnl):
    with db() as con:
        con.execute("""
        UPDATE trades
        SET status='CLOSED',
            exit_price=?,
            pnl=?,
            time_close=?
        WHERE id=?
        """, (
            exit_price,
            pnl,
            datetime.utcnow().isoformat(),
            trade_id
        ))
    update_balance(pnl)

def manage_open_trades(symbol, current_price, new_signal_action=None):
    cur = db().execute("""
        SELECT id, action, entry_price, lot
        FROM trades
        WHERE status='OPEN' AND symbol=?
    """, (symbol,))

    for trade_id, action, entry, lot in cur.fetchall():
        direction = 1 if action == "buy" else -1
        diff = (current_price - entry) * direction

        # TP / SL
        if diff >= TP_POINTS:
            pnl = TP_POINTS * lot * POINT_VALUE
            close_trade(trade_id, current_price, pnl)

        elif diff <= -SL_POINTS:
            pnl = -SL_POINTS * lot * POINT_VALUE
            close_trade(trade_id, current_price, pnl)

        # Opposite signal
        elif new_signal_action and new_signal_action != action:
            pnl = diff * lot * POINT_VALUE
            close_trade(trade_id, current_price, pnl)

# ==================================================
# 🌐 WEBHOOK
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):
    if request.query_params.get("token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)

    body = (await request.body()).decode()
    parsed = parse_signal(body)
    if not parsed:
        return {"status": "ignored"}

    price = get_price(parsed["symbol"])
    sma200 = get_sma200(parsed["symbol"])
    decision, reasons = evaluate_trade(parsed, price, sma200)

    if price:
        manage_open_trades(parsed["symbol"], price, parsed["action"])

    if decision == "approved":
        open_trade(parsed, price)

    return {
        "status": decision,
        "price": price,
        "sma200": sma200,
        "reasons": reasons,
        "balance": get_balance()
    }

# ==================================================
# 📊 STATS
# ==================================================
@app.get("/stats")
def stats():
    cur = db().execute("SELECT status, COUNT(*) FROM trades GROUP BY status")
    trades = dict(cur.fetchall())

    cur = db().execute("SELECT SUM(pnl) FROM trades WHERE status='CLOSED'")
    total_pnl = cur.fetchone()[0] or 0

    return {
        "balance": get_balance(),
        "total_pnl": total_pnl,
        "trades": trades
    }
