import os
import sqlite3
import requests
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException

# ==================================================
# APP
# ==================================================
app = FastAPI()
logging.basicConfig(level=logging.INFO)

WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

DB_FILE = "trading.db"
START_BALANCE = 1_000.0

BASE_RISK = 0.01
TP_POINTS = 20
SL_POINTS = 10
POINT_VALUE = 1.0

SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "BTCUSD": "BTC/USD",
    "ETHUSD": "ETH/USD",
}

STRATEGIES = [
    "SMA200_TREND",
    "SMA200_PULLBACK",
    "BREAKOUT",
    "MEAN_REVERSION",
    "MOMENTUM",
    "REVERSAL",
]

# ==================================================
# DB
# ==================================================
def db():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            strategy TEXT,
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
        CREATE TABLE IF NOT EXISTS strategy_state (
            symbol TEXT,
            strategy TEXT,
            weight REAL,
            status TEXT,
            last_eval TEXT,
            PRIMARY KEY (symbol, strategy)
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS trade_context (
            symbol TEXT,
            strategy TEXT,
            hour INTEGER,
            weekday INTEGER,
            blocked_until TEXT,
            PRIMARY KEY (symbol, strategy, hour, weekday)
        )
        """)

        if con.execute("SELECT COUNT(*) FROM balance").fetchone()[0] == 0:
            con.execute(
                "INSERT INTO balance VALUES (?, ?)",
                (datetime.utcnow().isoformat(), START_BALANCE)
            )

        for s in SYMBOL_MAP:
            for strat in STRATEGIES:
                con.execute("""
                    INSERT OR IGNORE INTO strategy_state
                    VALUES (?, ?, 1.0, 'ACTIVE', NULL)
                """, (s, strat))

init_db()

# ==================================================
# UTILS
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

def calculate_lot(weight):
    risk = get_balance() * BASE_RISK * weight
    return round(max(risk / (SL_POINTS * POINT_VALUE), 0.01), 2)

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
# CONTEXT LOGIC (ETAP 29)
# ==================================================
def is_context_blocked(symbol, strategy):
    now = datetime.utcnow()
    h = now.hour
    wd = now.weekday()

    row = db().execute("""
        SELECT blocked_until FROM trade_context
        WHERE symbol=? AND strategy=? AND hour=? AND weekday=?
    """, (symbol, strategy, h, wd)).fetchone()

    if not row or not row[0]:
        return False

    return datetime.fromisoformat(row[0]) > now

def penalize_context(symbol, strategy):
    now = datetime.utcnow()
    h = now.hour
    wd = now.weekday()
    block_until = (now + timedelta(hours=6)).isoformat()

    db().execute("""
        INSERT OR REPLACE INTO trade_context
        VALUES (?, ?, ?, ?, ?)
    """, (symbol, strategy, h, wd, block_until)).connection.commit()

def losing_streak(symbol, strategy):
    rows = db().execute("""
        SELECT pnl FROM trades
        WHERE symbol=? AND strategy=? AND status='CLOSED'
        ORDER BY id DESC LIMIT 3
    """, (symbol, strategy)).fetchall()

    return len(rows) == 3 and all(r[0] <= 0 for r in rows)

# ==================================================
# TRADING
# ==================================================
def open_trade(symbol, strategy, action, price):
    if is_context_blocked(symbol, strategy):
        return

    state = db().execute("""
        SELECT weight, status FROM strategy_state
        WHERE symbol=? AND strategy=?
    """, (symbol, strategy)).fetchone()

    if not state or state[1] == "DISABLED":
        return

    lot = calculate_lot(state[0])

    db().execute("""
        INSERT INTO trades
        (symbol, strategy, action, entry_price, lot, status, pnl, time_open)
        VALUES (?, ?, ?, ?, ?, 'OPEN', 0, ?)
    """, (
        symbol, strategy, action, price, lot,
        datetime.utcnow().isoformat()
    )).connection.commit()

def close_trade(trade_id, symbol, strategy, pnl, price):
    db().execute("""
        UPDATE trades
        SET status='CLOSED', exit_price=?, pnl=?, time_close=?
        WHERE id=?
    """, (
        price, pnl, datetime.utcnow().isoformat(), trade_id
    )).connection.commit()

    update_balance(pnl)

    if pnl <= 0 and losing_streak(symbol, strategy):
        penalize_context(symbol, strategy)

# ==================================================
# WEBHOOK
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):
    if request.query_params.get("token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)

    body = (await request.body()).decode().lower()
    action = "buy" if "buy" in body else "sell" if "sell" in body else None
    if not action:
        return {"status": "ignored"}

    opened = []

    for symbol in SYMBOL_MAP:
        price = get_price(symbol)
        if price is None:
            continue

        for strat in STRATEGIES:
            open_trade(symbol, strat, action, price)
            opened.append(f"{symbol}:{strat}")

    return {"opened": opened}

# ==================================================
# API
# ==================================================
@app.get("/stats")
def stats():
    return {
        "balance": get_balance(),
        "blocked_contexts": db().execute(
            "SELECT * FROM trade_context WHERE blocked_until IS NOT NULL"
        ).fetchall()
    }

@app.get("/trades")
def trades(limit: int = 50):
    return db().execute("""
        SELECT symbol, strategy, action, pnl, status, time_open
        FROM trades ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
