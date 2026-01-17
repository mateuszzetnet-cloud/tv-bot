import os
import sqlite3
import requests
import logging
from datetime import datetime, date, timedelta
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()
logging.basicConfig(level=logging.INFO)

WEBHOOK_SECRET = os.getenv("WEBHOOK_TOKEN")
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

DB_FILE = "trading.db"
START_BALANCE = 1_000.0

# =========================
# ⚠️ GLOBAL RISK LIMITS
# =========================
BASE_RISK = 0.01
MIN_RISK = 0.005
MAX_RISK = 0.02

MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10

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
            "engine_status": "LEARNING",
            "peak_balance": str(START_BALANCE),
            "daily_date": str(date.today()),
            "daily_pnl": "0",
            "adaptive_risk": str(BASE_RISK)
        }
        for k, v in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO engine_state VALUES (?, ?)",
                (k, v)
            )

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

def update_balance(pnl):
    bal = get_balance() + pnl
    db().execute(
        "INSERT INTO balance VALUES (?, ?)",
        (datetime.utcnow().isoformat(), bal)
    ).connection.commit()

    peak = float(get_state("peak_balance"))
    if bal > peak:
        set_state("peak_balance", bal)

def adaptive_risk():
    rows = db().execute("""
        SELECT pnl FROM trades
        WHERE status='CLOSED'
        ORDER BY id DESC LIMIT 20
    """).fetchall()

    if len(rows) < 10:
        return MIN_RISK

    pnls = [r[0] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    winrate = len(wins) / len(pnls)
    streak_loss = sum(1 for p in pnls[:3] if p < 0)

    risk = BASE_RISK

    if winrate > 0.6:
        risk += 0.005
    if winrate > 0.7:
        risk += 0.005
    if streak_loss >= 2:
        risk -= 0.005

    risk = max(MIN_RISK, min(MAX_RISK, risk))
    set_state("adaptive_risk", risk)
    return risk

def calculate_lot():
    bal = get_balance()
    risk = adaptive_risk()
    return round(max((bal * risk) / (SL_POINTS * POINT_VALUE), 0.01), 2)

# =========================
# PAPER ENGINE
# =========================
def open_trade(symbol, action, price):
    lot = calculate_lot()
    db().execute("""
        INSERT INTO trades
        (symbol, action, entry_price, lot, status, pnl, time_open)
        VALUES (?, ?, ?, ?, 'OPEN', 0, ?)
    """, (
        symbol, action, price, lot,
        datetime.utcnow().isoformat()
    )).connection.commit()

# =========================
# WEBHOOK
# =========================
@app.post("/webhook")
async def webhook(request: Request):
    if request.query_params.get("token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)

    text = (await request.body()).decode().lower()
    action = "buy" if "buy" in text else "sell" if "sell" in text else None
    if not action:
        return {"status": "ignored"}

    symbol = "XAUUSD"
    price = get_price(symbol)
    if not price:
        return {"status": "no_price"}

    open_trade(symbol, action, price)

    return {
        "status": "opened",
        "risk": float(get_state("adaptive_risk")),
        "balance": get_balance()
    }

# =========================
# STATS
# =========================
@app.get("/stats")
def stats():
    return {
        "engine": get_state("engine_status"),
        "balance": get_balance(),
        "adaptive_risk": float(get_state("adaptive_risk"))
    }
