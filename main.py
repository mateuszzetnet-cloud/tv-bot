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
START_BALANCE = 1000.0

# ==================================================
# RISK / CAPITAL CONFIG
# ==================================================
BASE_RISK = 0.01
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10

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
                    VALUES (?, ?, 1.0, 1.0, 'ACTIVE')
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

    peak = float(get_state("peak_balance"))
    if bal > peak:
        set_state("peak_balance", bal)

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
# PERFORMANCE / STATS
# ==================================================
def performance_stats(symbol):
    rows = db().execute("""
        SELECT pnl, time_close FROM trades
        WHERE symbol=? AND status='CLOSED'
    """, (symbol,)).fetchall()

    if len(rows) < 5:
        return None

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

    days = {r[1][:10] for r in rows if r[1]}

    return {
        "trades": len(pnls),
        "winrate": round(winrate, 3),
        "expectancy": round(expectancy, 2),
        "max_dd": round(max_dd, 3),
        "days": len(days)
    }

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
# ADAPTIVE TP / SL + CAPITAL SCALING (ETAP 31)
# ==================================================
def adaptive_levels(symbol, strategy):
    tp_mult, sl_mult = db().execute("""
        SELECT tp_mult, sl_mult FROM strategy_state
        WHERE symbol=? AND strategy=?
    """, (symbol, strategy)).fetchone()

    return BASE_TP * tp_mult, BASE_SL * sl_mult

def scaled_risk():
    stats = performance_stats("XAUUSD")
    if not stats:
        return BASE_RISK

    if stats["winrate"] > 0.6 and stats["max_dd"] < 0.05:
        return min(BASE_RISK * 1.5, 0.02)

    if stats["max_dd"] > 0.08:
        return BASE_RISK * 0.5

    return BASE_RISK

# ==================================================
# TRADING
# ==================================================
def open_trade(symbol, strategy, action, price):
    if get_state("engine_status") == "LOCKED":
        return

    if is_context_blocked(symbol, strategy):
        return

    tp, sl = adaptive_levels(symbol, strategy)
    risk = scaled_risk()
    lot = round((get_balance() * risk) / (sl * POINT_VALUE), 2)

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

    if pnl <= 0:
        penalize_context(symbol, strategy)

# ==================================================
# RISK LOCKS
# ==================================================
def check_risk_locks():
    today = str(date.today())
    if get_state("daily_date") != today:
        set_state("daily_date", today)
        set_state("daily_pnl", 0)

    bal = get_balance()
    peak = float(get_state("peak_balance"))

    if (peak - bal) / peak >= MAX_DRAWDOWN:
        set_state("engine_status", "LOCKED")

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

    check_risk_locks()

    for symbol in SYMBOL_MAP:
        price = get_price(symbol)
        if not price:
            continue

        for strat in STRATEGIES:
            open_trade(symbol, strat, action, price)

    return {"status": "ok", "engine": get_state("engine_status")}

# ==================================================
# ENDPOINTS
# ==================================================
@app.get("/stats")
def stats():
    return {
        "engine": get_state("engine_status"),
        "balance": get_balance(),
        "performance": performance_stats("XAUUSD")
    }

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
