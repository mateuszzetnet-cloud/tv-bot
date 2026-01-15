import os
import re
import sqlite3
import requests
import logging
from datetime import datetime, date
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

# === RISK ===
RISK_PER_TRADE = 0.01      # 1%
MAX_DAILY_LOSS = 0.03      # 3%
MAX_DRAWDOWN = 0.10        # 10%

TP_POINTS = 20
SL_POINTS = 10
POINT_VALUE = 1.0

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

        con.execute("""
        CREATE TABLE IF NOT EXISTS engine_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        if con.execute("SELECT COUNT(*) FROM balance").fetchone()[0] == 0:
            con.execute(
                "INSERT INTO balance VALUES (?, ?)",
                (datetime.utcnow().isoformat(), START_BALANCE)
            )

        defaults = {
            "engine_status": "ACTIVE",
            "peak_balance": str(START_BALANCE),
            "daily_date": str(date.today()),
            "daily_pnl": "0"
        }

        for k, v in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO engine_state VALUES (?, ?)",
                (k, v)
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

    symbol = "XAUUSD" if "xauusd" in t else None
    if symbol not in SYMBOL_MAP:
        return None

    confidence = "HIGH" if "high" in t else "NORMAL"

    return {
        "symbol": symbol,
        "action": action,
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
    d = safe_request(
        "https://api.twelvedata.com/price",
        {"symbol": SYMBOL_MAP[symbol], "apikey": TWELVE_API_KEY}
    )
    return float(d["price"]) if d and "price" in d else None

def get_sma200(symbol, interval="15min"):
    d = safe_request(
        "https://api.twelvedata.com/sma",
        {
            "symbol": SYMBOL_MAP[symbol],
            "interval": interval,
            "time_period": 200,
            "apikey": TWELVE_API_KEY
        }
    )
    if d and "values" in d:
        return float(d["values"][0]["sma"])
    if interval == "15min":
        return get_sma200(symbol, "1h")
    return None

# ==================================================
# 📊 ENGINE STATE
# ==================================================
def get_state(key):
    return db().execute(
        "SELECT value FROM engine_state WHERE key=?", (key,)
    ).fetchone()[0]

def set_state(key, value):
    db().execute(
        "UPDATE engine_state SET value=? WHERE key=?",
        (str(value), key)
    ).connection.commit()

# ==================================================
# 📊 BALANCE & RISK
# ==================================================
def get_balance():
    return db().execute(
        "SELECT balance FROM balance ORDER BY time DESC LIMIT 1"
    ).fetchone()[0]

def update_balance(delta):
    bal = get_balance() + delta
    db().execute(
        "INSERT INTO balance VALUES (?, ?)",
        (datetime.utcnow().isoformat(), bal)
    ).connection.commit()

    peak = float(get_state("peak_balance"))
    if bal > peak:
        set_state("peak_balance", bal)

    return bal

def calculate_lot():
    bal = get_balance()
    risk = bal * RISK_PER_TRADE
    lot = risk / (SL_POINTS * POINT_VALUE)
    return round(max(lot, 0.01), 2)

# ==================================================
# 🧠 STRATEGY
# ==================================================
def evaluate_trade(parsed, price, sma):
    if parsed["confidence"] != "HIGH":
        return False
    if price is None or sma is None:
        return False
    if parsed["action"] == "buy" and price < sma:
        return False
    if parsed["action"] == "sell" and price > sma:
        return False
    return True

# ==================================================
# 📄 PAPER ENGINE
# ==================================================
def open_trade(parsed, price):
    lot = calculate_lot()
    db().execute("""
        INSERT INTO trades
        (symbol, action, entry_price, lot, status, pnl, time_open)
        VALUES (?, ?, ?, ?, 'OPEN', 0, ?)
    """, (
        parsed["symbol"],
        parsed["action"],
        price,
        lot,
        datetime.utcnow().isoformat()
    )).connection.commit()

def close_trade(trade_id, pnl, price):
    db().execute("""
        UPDATE trades
        SET status='CLOSED',
            exit_price=?,
            pnl=?,
            time_close=?
        WHERE id=?
    """, (
        price,
        pnl,
        datetime.utcnow().isoformat(),
        trade_id
    )).connection.commit()

    update_balance(pnl)
    set_state("daily_pnl", float(get_state("daily_pnl")) + pnl)

def manage_trades(symbol, price, new_action):
    cur = db().execute("""
        SELECT id, action, entry_price, lot
        FROM trades WHERE status='OPEN' AND symbol=?
    """, (symbol,))

    for tid, action, entry, lot in cur.fetchall():
        dir = 1 if action == "buy" else -1
        diff = (price - entry) * dir

        if diff >= TP_POINTS:
            close_trade(tid, TP_POINTS * lot * POINT_VALUE, price)
        elif diff <= -SL_POINTS:
            close_trade(tid, -SL_POINTS * lot * POINT_VALUE, price)
        elif new_action != action:
            close_trade(tid, diff * lot * POINT_VALUE, price)

# ==================================================
# 🚦 RISK LOCKS
# ==================================================
def check_risk_locks():
    today = str(date.today())

    if get_state("daily_date") != today:
        set_state("daily_date", today)
        set_state("daily_pnl", 0)
        set_state("engine_status", "ACTIVE")

    bal = get_balance()
    peak = float(get_state("peak_balance"))
    daily_pnl = float(get_state("daily_pnl"))

    if daily_pnl <= -bal * MAX_DAILY_LOSS:
        set_state("engine_status", "DAILY_LOCK")

    if (peak - bal) / peak >= MAX_DRAWDOWN:
        set_state("engine_status", "DD_LOCK")

    return get_state("engine_status")

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
    sma = get_sma200(parsed["symbol"])

    manage_trades(parsed["symbol"], price, parsed["action"])

    status = check_risk_locks()
    if status != "ACTIVE":
        return {"status": "blocked", "engine": status}

    if evaluate_trade(parsed, price, sma):
        open_trade(parsed, price)
        return {"status": "opened", "balance": get_balance()}

    return {"status": "rejected"}

# ==================================================
# 📊 STATS
# ==================================================
@app.get("/stats")
def stats():
    cur = db().execute("SELECT status, COUNT(*) FROM trades GROUP BY status")
    trades = dict(cur.fetchall())

    return {
        "balance": get_balance(),
        "engine_status": get_state("engine_status"),
        "daily_pnl": float(get_state("daily_pnl")),
        "peak_balance": float(get_state("peak_balance")),
        "trades": trades
    }
