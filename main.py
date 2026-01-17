import os
import sqlite3
import requests
import logging
from datetime import datetime, date, timedelta
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
BASE_TP = 20
BASE_SL = 10
POINT_VALUE = 1.0

SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
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
            tp_mult REAL,
            sl_mult REAL,
            status TEXT,
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

        defaults = {
            "engine_status": "LEARNING",
            "peak_balance": str(START_BALANCE),
            "daily_date": str(date.today()),
            "daily_pnl": "0"
        }

        for k, v in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO engine_state VALUES (?, ?)",
                (k, v)
            )

        for s in SYMBOL_MAP:
            for strat in STRATEGIES:
                con.execute("""
                    INSERT OR IGNORE INTO strategy_state
                    VALUES (?, ?, 1.0, 1.0, 1.0, 'ACTIVE')
                """, (s, strat))

init_db()

# ==================================================
# STATE / BALANCE
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

# ==================================================
# PRICE
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
# CONTEXT BLOCK
# ==================================================
def is_context_blocked(symbol, strategy):
    now = datetime.utcnow()
    row = db().execute("""
        SELECT blocked_until FROM trade_context
        WHERE symbol=? AND strategy=? AND hour=? AND weekday=?
    """, (symbol, strategy, now.hour, now.weekday())).fetchone()

    return row and row[0] and datetime.fromisoformat(row[0]) > now

def penalize_context(symbol, strategy):
    now = datetime.utcnow()
    db().execute("""
        INSERT OR REPLACE INTO trade_context
        VALUES (?, ?, ?, ?, ?)
    """, (
        symbol, strategy, now.hour, now.weekday(),
        (now + timedelta(hours=6)).isoformat()
    )).connection.commit()

# ==================================================
# ADAPTIVE TP / SL — ETAP 30
# ==================================================
def adaptive_levels(symbol, strategy):
    row = db().execute("""
        SELECT tp_mult, sl_mult FROM strategy_state
        WHERE symbol=? AND strategy=?
    """, (symbol, strategy)).fetchone()

    return (
        BASE_TP * row[0],
        BASE_SL * row[1]
    )

def update_adaptive(symbol, strategy):
    rows = db().execute("""
        SELECT pnl FROM trades
        WHERE symbol=? AND strategy=? AND status='CLOSED'
        ORDER BY id DESC LIMIT 20
    """, (symbol, strategy)).fetchall()

    if len(rows) < 10:
        return

    pnls = [r[0] for r in rows]
    winrate = len([p for p in pnls if p > 0]) / len(pnls)

    tp_mult = 1.1 if winrate > 0.6 else 0.9
    sl_mult = 0.9 if winrate < 0.45 else 1.0

    db().execute("""
        UPDATE strategy_state
        SET tp_mult=?, sl_mult=?
        WHERE symbol=? AND strategy=?
    """, (tp_mult, sl_mult, symbol, strategy)).connection.commit()

# ==================================================
# TRADING
# ==================================================
def open_trade(symbol, strategy, action, price):
    if is_context_blocked(symbol, strategy):
        return

    tp, sl = adaptive_levels(symbol, strategy)
    lot = round((get_balance() * BASE_RISK) / (sl * POINT_VALUE), 2)

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
    update_adaptive(symbol, strategy)

    if pnl <= 0:
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

    for symbol in SYMBOL_MAP:
        price = get_price(symbol)
        if price is None:
            continue

        for strat in STRATEGIES:
            open_trade(symbol, strat, action, price)

    return {"status": "accepted", "engine": get_state("engine_status")}

# ==================================================
# DASHBOARD
# ==================================================
@app.get("/dashboard")
def dashboard():
    return {
        "engine": get_state("engine_status"),
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
