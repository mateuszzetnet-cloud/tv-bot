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
START_BALANCE = 1_000.0

# ==================================================
# ⚠️ RISK CONFIG (ETAP 35)
# ==================================================
BASE_RISK = 0.01
MIN_RISK = 0.003
MAX_RISK = 0.02

MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10

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
            "engine_status": "LEARNING",
            "peak_balance": str(START_BALANCE),
            "daily_date": str(date.today()),
            "daily_pnl": "0",
            "current_risk": str(BASE_RISK)
        }

        for k, v in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO engine_state VALUES (?, ?)",
                (k, v)
            )

init_db()

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

def calculate_dynamic_risk():
    """ETAP 35 — dynamiczne skalowanie ryzyka"""
    cur = db().execute("""
        SELECT pnl FROM trades WHERE status='CLOSED'
        ORDER BY id DESC LIMIT 50
    """).fetchall()

    if len(cur) < 20:
        return BASE_RISK

    pnls = [r[0] for r in cur]
    wins = [p for p in pnls if p > 0]

    winrate = len(wins) / len(pnls)
    expectancy = sum(pnls) / len(pnls)

    balance = get_balance()
    peak = float(get_state("peak_balance"))
    dd = (peak - balance) / peak if peak > 0 else 0

    risk = BASE_RISK

    if winrate > 0.58 and expectancy > 0:
        risk += 0.002

    if winrate > 0.62:
        risk += 0.002

    if dd > 0.05:
        risk -= 0.003

    risk = max(MIN_RISK, min(MAX_RISK, risk))
    set_state("current_risk", risk)
    return risk

def calculate_lot():
    bal = get_balance()
    risk = calculate_dynamic_risk()
    risk_amount = bal * risk
    lot = risk_amount / (SL_POINTS * POINT_VALUE)
    return round(max(lot, 0.01), 2)

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

    open_trade(parsed, price)

    return {
        "status": "paper_trade",
        "engine": get_state("engine_status"),
        "risk": float(get_state("current_risk"))
    }

# ==================================================
# 📊 STATS / DASHBOARD
# ==================================================
@app.get("/stats")
def stats():
    return {
        "engine_status": get_state("engine_status"),
        "balance": get_balance(),
        "risk": float(get_state("current_risk")),
        "peak_balance": float(get_state("peak_balance"))
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
