import os
import sqlite3
import requests
import logging
from datetime import datetime, date, timedelta
from fastapi import FastAPI, Request, HTTPException

# ==================================================
# 🔧 APP
# ==================================================
app = FastAPI()
logging.basicConfig(level=logging.INFO)

WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

DB_FILE = "trading.db"
START_BALANCE = 1_000.0  # LIVE TARGET

# ==================================================
# ⚠️ RISK CONFIG
# ==================================================
RISK_PER_TRADE = 0.01
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10

MAX_TRADES_PER_SYMBOL = 1
SYMBOL_COOLDOWN_MIN = 5

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
            reason TEXT,
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
            "engine_status": "LEARNING",   # <--- KLUCZ
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

    return {
        "symbol": symbol,
        "action": action,
        "raw": text.strip()
    }

# ==================================================
# 📈 MARKET DATA
# ==================================================
def get_price(symbol):
    try:
        r = requests.get(
            "https://api.twelvedata.com/price",
            params={"symbol": SYMBOL_MAP[symbol], "apikey": TWELVE_API_KEY},
            timeout=5
        ).json()
        return float(r["price"])
    except Exception:
        return None

# ==================================================
# 💰 BALANCE
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

def calculate_lot():
    bal = get_balance()
    risk = bal * RISK_PER_TRADE
    return round(max(risk / (SL_POINTS * POINT_VALUE), 0.01), 2)

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
# 🧠 ETAP 19 — PERFORMANCE ANALYTICS
# ==================================================
def performance_stats(symbol):
    rows = db().execute("""
        SELECT pnl, time_close FROM trades
        WHERE symbol=? AND status='CLOSED'
    """, (symbol,)).fetchall()

    if len(rows) < 10:
        return None

    pnls = [r[0] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    winrate = len(wins) / len(pnls)
    expectancy = sum(pnls) / len(pnls)

    balance = START_BALANCE
    peak = balance
    max_dd = 0

    for p in pnls:
        balance += p
        peak = max(peak, balance)
        dd = (peak - balance) / peak
        max_dd = max(max_dd, dd)

    days = {
        r[1][:10] for r in rows if r[1]
    }

    return {
        "trades": len(pnls),
        "winrate": winrate,
        "expectancy": expectancy,
        "max_dd": max_dd,
        "days": len(days)
    }

# ==================================================
# 🧠 ETAP 20 — LIVE GATE
# ==================================================
def check_live_ready(symbol):
    stats = performance_stats(symbol)
    if not stats:
        return False

    return (
        stats["trades"] >= 100 and
        stats["winrate"] >= 0.55 and
        stats["expectancy"] > 0 and
        stats["max_dd"] < 0.08 and
        stats["days"] >= 30
    )

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
    if price is None:
        return {"status": "no_price"}

    # LEARNING MODE
    if check_live_ready(parsed["symbol"]):
        set_state("engine_status", "LIVE")

    open_trade(parsed, price)

    return {
        "status": "paper_trade",
        "engine": get_state("engine_status")
    }

# ==================================================
# 📊 STATS
# ==================================================
@app.get("/stats")
def stats():
    symbol = "XAUUSD"
    return {
        "engine_status": get_state("engine_status"),
        "balance": get_balance(),
        "performance": performance_stats(symbol)
    }

@app.get("/trades")
def trades(limit: int = 50):
    cur = db().execute("""
        SELECT symbol, action, entry_price, exit_price, pnl, status, time_open
        FROM trades
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    return cur.fetchall()
