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
# ⚠️ RISK CONFIG
# ==================================================
RISK_PER_TRADE = 0.01
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10

TP_POINTS = 20
SL_POINTS = 10
POINT_VALUE = 1.0

TRAIL_START = 10
TRAIL_DISTANCE = 6
PARTIAL_CLOSE_PCT = 0.5

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
            tp REAL,
            sl REAL,
            lot REAL,
            remaining_lot REAL,
            stage INTEGER,
            status TEXT,
            pnl REAL,
            exit_price REAL,
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

init_db()

# ==================================================
# 📊 ENGINE STATE / BALANCE
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

def calculate_lot():
    bal = get_balance()
    risk = bal * RISK_PER_TRADE
    return round(max(risk / (SL_POINTS * POINT_VALUE), 0.01), 2)

# ==================================================
# 🔎 PARSER / MARKET
# ==================================================
def parse_signal(text: str):
    t = text.lower()
    action = "buy" if "buy" in t else "sell" if "sell" in t else None
    if not action:
        return None
    return {"symbol": "XAUUSD", "action": action}

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
# 🔁 TRADE MANAGEMENT
# ==================================================
def manage_trades(symbol, price):
    rows = db().execute("""
        SELECT id, action, entry_price, sl, lot, remaining_lot, stage
        FROM trades WHERE status='OPEN' AND symbol=?
    """, (symbol,)).fetchall()

    for t in rows:
        tid, action, entry, sl, lot, rem, stage = t
        dir = 1 if action == "buy" else -1
        move = (price - entry) * dir

        # 🔹 PARTIAL CLOSE
        if stage == 0 and move >= TP_POINTS:
            closed_lot = lot * PARTIAL_CLOSE_PCT
            pnl = closed_lot * TP_POINTS * POINT_VALUE
            update_balance(pnl)

            db().execute("""
                UPDATE trades
                SET remaining_lot=?, stage=1
                WHERE id=?
            """, (lot - closed_lot, tid)).connection.commit()

        # 🔹 TRAILING STOP
        if move >= TRAIL_START:
            new_sl = price - TRAIL_DISTANCE if action == "buy" else price + TRAIL_DISTANCE
            better = new_sl > sl if action == "buy" else new_sl < sl
            if better:
                db().execute(
                    "UPDATE trades SET sl=?, stage=2 WHERE id=?",
                    (new_sl, tid)
                ).connection.commit()

        # 🔹 STOP HIT
        hit_sl = price <= sl if action == "buy" else price >= sl
        if hit_sl:
            pnl = (sl - entry) * rem * POINT_VALUE * dir
            update_balance(pnl)

            db().execute("""
                UPDATE trades
                SET status='CLOSED',
                    pnl=?,
                    exit_price=?,
                    time_close=?
                WHERE id=?
            """, (pnl, sl, datetime.utcnow().isoformat(), tid)).connection.commit()

# ==================================================
# 📄 OPEN TRADE
# ==================================================
def open_trade(parsed, price):
    lot = calculate_lot()
    tp = price + TP_POINTS if parsed["action"] == "buy" else price - TP_POINTS
    sl = price - SL_POINTS if parsed["action"] == "buy" else price + SL_POINTS

    db().execute("""
        INSERT INTO trades
        (symbol, action, entry_price, tp, sl, lot, remaining_lot, stage, status, pnl, time_open)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'OPEN', 0, ?)
    """, (
        parsed["symbol"],
        parsed["action"],
        price, tp, sl,
        lot, lot,
        datetime.utcnow().isoformat()
    )).connection.commit()

# ==================================================
# 🌐 WEBHOOK
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):
    if request.query_params.get("token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)

    parsed = parse_signal((await request.body()).decode())
    if not parsed:
        return {"status": "ignored"}

    price = get_price(parsed["symbol"])
    if not price:
        return {"status": "no_price"}

    manage_trades(parsed["symbol"], price)
    open_trade(parsed, price)

    return {"status": "ok", "balance": get_balance()}

# ==================================================
# 📊 STATS
# ==================================================
@app.get("/stats")
def stats():
    return {
        "engine_status": get_state("engine_status"),
        "balance": get_balance()
    }
# ==================================================
# 📊 PERFORMANCE ANALYTICS (FIX)
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
        dd = (peak - balance) / peak
        max_dd = max(max_dd, dd)

    days = {
        r[1][:10] for r in rows if r[1]
    }

    return {
        "trades": len(pnls),
        "winrate": round(winrate, 3),
        "expectancy": round(expectancy, 2),
        "max_dd": round(max_dd, 3),
        "days": len(days)
    }

# ==================================================
# 📄 TRADES ENDPOINT (FIX)
# ==================================================
@app.get("/trades")
def trades(limit: int = 50):
    cur = db().execute("""
        SELECT
            id,
            symbol,
            action,
            entry_price,
            exit_price,
            pnl,
            status,
            stage,
            time_open,
            time_close
        FROM trades
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    return cur.fetchall()

# ==================================================
# 📊 STATS (EXTENDED)
# ==================================================
@app.get("/stats")
def stats():
    symbol = "XAUUSD"
    return {
        "engine_status": get_state("engine_status"),
        "balance": get_balance(),
        "performance": performance_stats(symbol)
    }
