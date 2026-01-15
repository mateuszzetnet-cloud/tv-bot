import os
import re
import sqlite3
import requests
import logging
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException

# ==================================================
# 🔧 APP CONFIG
# ==================================================
app = FastAPI()
logging.basicConfig(level=logging.INFO)

ENGINE_VERSION = 2
WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

DB_FILE = "trading.db"
START_BALANCE = 10_000.0

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
        # trades
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
            time_close TEXT,
            engine_version INTEGER
        )
        """)

        # balance history
        con.execute("""
        CREATE TABLE IF NOT EXISTS balance (
            time TEXT,
            balance REAL,
            engine_version INTEGER
        )
        """)

        # RESET paper account (ENGINE v2)
        con.execute("DELETE FROM trades WHERE engine_version < ?", (ENGINE_VERSION,))
        con.execute("DELETE FROM balance WHERE engine_version < ?", (ENGINE_VERSION,))

        cur = con.execute(
            "SELECT COUNT(*) FROM balance WHERE engine_version = ?",
            (ENGINE_VERSION,)
        )
        if cur.fetchone()[0] == 0:
            con.execute(
                "INSERT INTO balance VALUES (?, ?, ?)",
                (datetime.utcnow().isoformat(), START_BALANCE, ENGINE_VERSION)
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

    if data and "values" in data and data["values"]:
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
    cur = db().execute(
        "SELECT balance FROM balance WHERE engine_version = ? ORDER BY time DESC LIMIT 1",
        (ENGINE_VERSION,)
    )
    return cur.fetchone()[0]

def update_balance(new_balance):
    db().execute(
        "INSERT INTO balance VALUES (?, ?, ?)",
        (datetime.utcnow().isoformat(), new_balance, ENGINE_VERSION)
    ).connection.commit()

# ==================================================
# 📄 PAPER TRADING ENGINE
# ==================================================
def open_trade(parsed, price):
    with db() as con:
        con.execute("""
        INSERT INTO trades
        (symbol, action, entry_price, lot, status, pnl, time_open, engine_version)
        VALUES (?, ?, ?, ?, 'OPEN', 0, ?, ?)
        """, (
            parsed["symbol"],
            parsed["action"],
            price,
            parsed["lot"],
            datetime.utcnow().isoformat(),
            ENGINE_VERSION
        ))

def close_trade(trade_id, price):
    with db() as con:
        trade = con.execute(
            "SELECT action, entry_price, lot FROM trades WHERE id = ? AND status = 'OPEN'",
            (trade_id,)
        ).fetchone()

        if not trade:
            return

        action, entry_price, lot = trade

        pnl = (price - entry_price) * lot if action == "buy" else (entry_price - price) * lot
        new_balance = get_balance() + pnl

        con.execute("""
        UPDATE trades
        SET status='CLOSED', exit_price=?, pnl=?, time_close=?
        WHERE id=?
        """, (
            price,
            pnl,
            datetime.utcnow().isoformat(),
            trade_id
        ))

        update_balance(new_balance)

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

    if decision == "approved":
        open_trade(parsed, price)

    return {
        "status": decision,
        "price": price,
        "sma200": sma200,
        "reasons": reasons
    }

# ==================================================
# 📊 STATS
# ==================================================
@app.get("/stats")
def stats():
    con = db()

    trades = con.execute("""
        SELECT status, COUNT(*) 
        FROM trades 
        WHERE engine_version = ?
        GROUP BY status
    """, (ENGINE_VERSION,)).fetchall()

    open_trades = con.execute("""
        SELECT id, symbol, action, entry_price, lot, time_open
        FROM trades
        WHERE status='OPEN' AND engine_version = ?
    """, (ENGINE_VERSION,)).fetchall()

    return {
        "engine_version": ENGINE_VERSION,
        "balance": get_balance(),
        "trades": dict(trades),
        "open_positions": [
            {
                "id": t[0],
                "symbol": t[1],
                "action": t[2],
                "entry": t[3],
                "lot": t[4],
                "time": t[5]
            } for t in open_trades
        ]
    }
