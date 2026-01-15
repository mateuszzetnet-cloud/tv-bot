import os
import re
import sqlite3
import requests
import logging
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException

# ==================================================
# 🔧 APP + CONFIG
# ==================================================
app = FastAPI()
logging.basicConfig(level=logging.INFO)

WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

DB_FILE = "trading.db"
START_BALANCE = 10_000.0

# Paper trading params
SL_PCT = 0.003   # 0.3%
TP_PCT = 0.006   # 0.6%
PIP_VALUE = 100  # XAUUSD approx

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
            time TEXT,
            symbol TEXT,
            action TEXT,
            lot REAL,
            price REAL,
            sma200 REAL,
            confidence TEXT,
            decision TEXT
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER,
            symbol TEXT,
            action TEXT,
            lot REAL,
            entry_price REAL,
            sl REAL,
            tp REAL,
            entry_time TEXT,
            status TEXT,
            exit_price REAL,
            exit_time TEXT,
            pnl REAL
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
        data = r.json()
        if isinstance(data, dict) and data.get("status") == "error":
            return None
        return data
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

    if price is None:
        reasons.append("no_price")

    if sma200 is None:
        reasons.append("no_sma200")

    if price and sma200:
        if parsed["action"] == "buy" and price < sma200:
            reasons.append("price_below_sma200")
        if parsed["action"] == "sell" and price > sma200:
            reasons.append("price_above_sma200")

    return ("approved" if not reasons else "rejected"), reasons

# ==================================================
# 📊 BALANCE
# ==================================================
def get_balance():
    cur = db().execute(
        "SELECT balance FROM balance ORDER BY time DESC LIMIT 1"
    )
    return cur.fetchone()[0]

def update_balance(delta):
    new_balance = get_balance() + delta
    with db() as con:
        con.execute(
            "INSERT INTO balance VALUES (?, ?)",
            (datetime.utcnow().isoformat(), new_balance)
        )

# ==================================================
# 🧾 STORAGE
# ==================================================
def save_trade(parsed, price, sma200, decision):
    with db() as con:
        cur = con.execute("""
        INSERT INTO trades
        (time, symbol, action, lot, price, sma200, confidence, decision)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            parsed["symbol"],
            parsed["action"],
            parsed["lot"],
            price,
            sma200,
            parsed["confidence"],
            decision
        ))
        return cur.lastrowid

def open_position(trade_id, parsed, price):
    if parsed["action"] == "buy":
        sl = price * (1 - SL_PCT)
        tp = price * (1 + TP_PCT)
    else:
        sl = price * (1 + SL_PCT)
        tp = price * (1 - TP_PCT)

    with db() as con:
        con.execute("""
        INSERT INTO positions
        (trade_id, symbol, action, lot, entry_price, sl, tp, entry_time, status, pnl)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 0)
        """, (
            trade_id,
            parsed["symbol"],
            parsed["action"],
            parsed["lot"],
            price,
            sl,
            tp,
            datetime.utcnow().isoformat()
        ))

# ==================================================
# 🔁 POSITION CHECKER (SL / TP)
# ==================================================
def check_positions():
    con = db()
    positions = con.execute("""
        SELECT id, action, lot, entry_price, sl, tp
        FROM positions WHERE status='OPEN'
    """).fetchall()

    price = get_price("XAUUSD")
    if price is None:
        return

    for pid, action, lot, entry, sl, tp in positions:
        exit_reason = None

        if action == "buy":
            if price <= sl:
                exit_reason = "SL"
            elif price >= tp:
                exit_reason = "TP"
        else:
            if price >= sl:
                exit_reason = "SL"
            elif price <= tp:
                exit_reason = "TP"

        if exit_reason:
            pnl = (
                (price - entry) * lot * PIP_VALUE
                if action == "buy"
                else (entry - price) * lot * PIP_VALUE
            )

            with con:
                con.execute("""
                UPDATE positions SET
                    status='CLOSED',
                    exit_price=?,
                    exit_time=?,
                    pnl=?
                WHERE id=?
                """, (
                    price,
                    datetime.utcnow().isoformat(),
                    pnl,
                    pid
                ))

            update_balance(pnl)

# ==================================================
# 🌐 WEBHOOK
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):
    if request.query_params.get("token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid token")

    check_positions()

    body = (await request.body()).decode("utf-8")
    parsed = parse_signal(body)
    if not parsed:
        return {"status": "ignored"}

    price = get_price(parsed["symbol"])
    sma200 = get_sma200(parsed["symbol"])
    decision, reasons = evaluate_trade(parsed, price, sma200)

    trade_id = save_trade(parsed, price, sma200, decision)

    if decision == "approved":
        open_position(trade_id, parsed, price)

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
    trades = dict(con.execute(
        "SELECT decision, COUNT(*) FROM trades GROUP BY decision"
    ).fetchall())

    positions = dict(con.execute(
        "SELECT status, COUNT(*) FROM positions GROUP BY status"
    ).fetchall())

    return {
        "balance": get_balance(),
        "trades": trades,
        "positions": positions
    }

@app.get("/positions")
def positions():
    cur = db().execute("""
        SELECT id, symbol, action, lot, entry_price, sl, tp, status, pnl
        FROM positions ORDER BY id DESC
    """)
    return [
        {
            "id": r[0],
            "symbol": r[1],
            "action": r[2],
            "lot": r[3],
            "entry": r[4],
            "sl": r[5],
            "tp": r[6],
            "status": r[7],
            "pnl": r[8],
        }
        for r in cur.fetchall()
    ]
