import os
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
START_BALANCE = 1_000.0

# ==================================================
# ⚠️ RISK
# ==================================================
BASE_RISK = 0.01
MAX_DRAWDOWN = 0.10

TP_POINTS = 20
SL_POINTS = 10
POINT_VALUE = 1.0

# ==================================================
# 🌍 SYMBOLS & STRATEGIES
# ==================================================
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
            strategy TEXT,
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

        if con.execute("SELECT COUNT(*) FROM balance").fetchone()[0] == 0:
            con.execute(
                "INSERT INTO balance VALUES (?, ?)",
                (datetime.utcnow().isoformat(), START_BALANCE)
            )

        con.execute("""
            INSERT OR IGNORE INTO engine_state VALUES ('engine_status','LEARNING')
        """)

        for s in SYMBOL_MAP:
            for strat in STRATEGIES:
                con.execute("""
                    INSERT OR IGNORE INTO strategy_state
                    VALUES (?, ?, 1.0, 'ACTIVE', NULL)
                """, (s, strat))

init_db()

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

def calculate_lot(weight):
    bal = get_balance()
    risk = bal * BASE_RISK * weight
    return round(max(risk / (SL_POINTS * POINT_VALUE), 0.01), 2)

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
# 🧠 PERFORMANCE EVAL (ETAP 28)
# ==================================================
def evaluate_strategy(symbol, strategy):
    rows = db().execute("""
        SELECT pnl FROM trades
        WHERE symbol=? AND strategy=? AND status='CLOSED'
    """, (symbol, strategy)).fetchall()

    if len(rows) < 30:
        return

    pnls = [r[0] for r in rows]
    wins = [p for p in pnls if p > 0]
    winrate = len(wins) / len(pnls)
    expectancy = sum(pnls) / len(pnls)

    balance = START_BALANCE
    peak = balance
    max_dd = 0
    for p in pnls:
        balance += p
        peak = max(peak, balance)
        max_dd = max(max_dd, (peak - balance) / peak)

    status = "ACTIVE"
    weight = 1.0

    if max_dd > 0.12:
        status = "DISABLED"
        weight = 0
    elif winrate < 0.45 or expectancy < 0:
        status = "DEGRADED"
        weight = 0.3

    db().execute("""
        UPDATE strategy_state
        SET weight=?, status=?, last_eval=?
        WHERE symbol=? AND strategy=?
    """, (
        weight, status, datetime.utcnow().isoformat(), symbol, strategy
    )).connection.commit()

# ==================================================
# 📄 PAPER ENGINE
# ==================================================
def open_trade(symbol, strategy, action, price):
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

def close_trade(trade_id, pnl, price):
    db().execute("""
        UPDATE trades
        SET status='CLOSED', exit_price=?, pnl=?, time_close=?
        WHERE id=?
    """, (
        price, pnl, datetime.utcnow().isoformat(), trade_id
    )).connection.commit()
    update_balance(pnl)

# ==================================================
# 🌐 WEBHOOK
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
            evaluate_strategy(symbol, strat)
            open_trade(symbol, strat, action, price)
            opened.append(f"{symbol}:{strat}")

    return {"opened": opened}

# ==================================================
# 📊 ENDPOINTS
# ==================================================
@app.get("/stats")
def stats():
    cur = db().execute("""
        SELECT strategy, status, weight FROM strategy_state
    """)
    return {
        "balance": get_balance(),
        "strategies": cur.fetchall()
    }

@app.get("/trades")
def trades(limit: int = 50):
    cur = db().execute("""
        SELECT symbol, strategy, action, pnl, status, time_open
        FROM trades ORDER BY id DESC LIMIT ?
    """, (limit,))
    return cur.fetchall()
