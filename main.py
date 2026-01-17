import os
import sqlite3
import requests
import logging
from datetime import datetime, date
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()
logging.basicConfig(level=logging.INFO)

WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

DB_FILE = "trading.db"
START_BALANCE = 1_000.0

# =========================
# RISK CONFIG
# =========================
BASE_RISK = 0.01
MIN_RISK = 0.005
MAX_RISK = 0.02

TP_POINTS = 20
SL_POINTS = 10
POINT_VALUE = 1.0

SYMBOL_MAP = {"XAUUSD": "XAU/USD"}

# =========================
# DATABASE
# =========================
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

        if con.execute("SELECT COUNT(*) FROM balance").fetchone()[0] == 0:
            con.execute(
                "INSERT INTO balance VALUES (?, ?)",
                (datetime.utcnow().isoformat(), START_BALANCE)
            )

        defaults = {
            "adaptive_risk": str(BASE_RISK),
        }
        for k, v in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO engine_state VALUES (?, ?)",
                (k, v)
            )

        # 🔧 migrate legacy trades
        con.execute("""
            UPDATE trades
            SET strategy='LEGACY'
            WHERE strategy IS NULL
        """)

init_db()

# =========================
# ENGINE STATE
# =========================
def get_state(key):
    return db().execute(
        "SELECT value FROM engine_state WHERE key=?", (key,)
    ).fetchone()[0]

def set_state(key, value):
    db().execute(
        "UPDATE engine_state SET value=? WHERE key=?",
        (str(value), key)
    ).connection.commit()

# =========================
# MARKET DATA
# =========================
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

# =========================
# BALANCE & RISK
# =========================
def get_balance():
    return db().execute(
        "SELECT balance FROM balance ORDER BY time DESC LIMIT 1"
    ).fetchone()[0]

def adaptive_risk():
    rows = db().execute("""
        SELECT pnl FROM trades
        WHERE status='CLOSED'
        ORDER BY id DESC LIMIT 20
    """).fetchall()

    if len(rows) < 10:
        return MIN_RISK

    pnls = [r[0] for r in rows]
    winrate = len([p for p in pnls if p > 0]) / len(pnls)

    risk = BASE_RISK
    if winrate > 0.6:
        risk += 0.005
    if winrate > 0.7:
        risk += 0.005

    risk = max(MIN_RISK, min(MAX_RISK, risk))
    set_state("adaptive_risk", risk)
    return risk

def calculate_lot():
    bal = get_balance()
    risk = adaptive_risk()
    return round(max((bal * risk) / (SL_POINTS * POINT_VALUE), 0.01), 2)

# =========================
# STRATEGY FILTER
# =========================
def strategy_allowed(strategy: str):
    rows = db().execute("""
        SELECT pnl FROM trades
        WHERE strategy=? AND status='CLOSED'
        ORDER BY id DESC LIMIT 30
    """, (strategy,)).fetchall()

    if len(rows) < 20:
        return True

    winrate = len([r for r in rows if r[0] > 0]) / len(rows)
    return winrate >= 0.45

# =========================
# WEBHOOK
# =========================
@app.post("/webhook")
async def webhook(request: Request):
    if request.query_params.get("token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)

    text = (await request.body()).decode().upper()

    action = "BUY" if "BUY" in text else "SELL" if "SELL" in text else None
    if not action:
        return {"status": "ignored"}

    strategy = "UNKNOWN"
    if "STRAT:" in text:
        strategy = text.split("STRAT:")[1].strip().split()[0]

    if not strategy_allowed(strategy):
        return {"status": "strategy_blocked", "strategy": strategy}

    symbol = "XAUUSD"
    price = get_price(symbol)
    if not price:
        return {"status": "no_price"}

    lot = calculate_lot()

    db().execute("""
        INSERT INTO trades
        (symbol, strategy, action, entry_price, lot, status, pnl, time_open)
        VALUES (?, ?, ?, ?, ?, 'OPEN', 0, ?)
    """, (
        symbol, strategy, action.lower(), price, lot,
        datetime.utcnow().isoformat()
    )).connection.commit()

    return {
        "status": "opened",
        "strategy": strategy,
        "risk": float(get_state("adaptive_risk"))
    }

# =========================
# ENDPOINTS
# =========================
@app.get("/stats")
def stats():
    return {
        "balance": get_balance(),
        "adaptive_risk": float(get_state("adaptive_risk"))
    }

@app.get("/trades")
def trades(limit: int = 50):
    rows = db().execute("""
        SELECT symbol, strategy, action, entry_price, exit_price,
               pnl, status, time_open
        FROM trades
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return rows
