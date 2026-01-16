import os
import re
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
START_BALANCE = 10_000.0

# === GLOBAL RISK ===
RISK_PER_TRADE = 0.01
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10

# === DEFAULT TP / SL (fallback) ===
DEFAULT_TP = 20
DEFAULT_SL = 10
POINT_VALUE = 1.0

AUTO_REGISTER_ENABLED = 1
MIN_TRADES_FOR_OPT = 20
OPT_LOOKBACK = 50
TP_RANGE = (10, 60)
SL_RANGE = (5, 30)

SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
    "XAGUSD": "XAG/USD",
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "BTCUSD": "BTC/USD",
    "ETHUSD": "ETH/USD",
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

        con.execute("""
        CREATE TABLE IF NOT EXISTS symbols (
            symbol TEXT PRIMARY KEY,
            enabled INTEGER,
            created_at TEXT
        )
        """)

        # === TP / SL PER SYMBOL (ETAP 17) ===
        con.execute("""
        CREATE TABLE IF NOT EXISTS symbol_params (
            symbol TEXT PRIMARY KEY,
            tp INTEGER,
            sl INTEGER,
            last_update TEXT
        )
        """)

        if con.execute("SELECT COUNT(*) FROM balance").fetchone()[0] == 0:
            con.execute(
                "INSERT INTO balance VALUES (?, ?)",
                (datetime.utcnow().isoformat(), START_BALANCE)
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

    for s in SYMBOL_MAP:
        if s.lower() in t:
            return {"symbol": s, "action": action}
    return None

# ==================================================
# 🧠 SYMBOL REGISTRATION
# ==================================================
def register_symbol(symbol):
    if not AUTO_REGISTER_ENABLED:
        return
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO symbols VALUES (?, 1, ?)",
            (symbol, datetime.utcnow().isoformat())
        )
        con.execute(
            "INSERT OR IGNORE INTO symbol_params VALUES (?, ?, ?, ?)",
            (symbol, DEFAULT_TP, DEFAULT_SL, datetime.utcnow().isoformat())
        )

# ==================================================
# 📈 MARKET DATA
# ==================================================
def safe_request(url, params):
    try:
        return requests.get(url, params=params, timeout=5).json()
    except Exception:
        return None

def get_price(symbol):
    d = safe_request("https://api.twelvedata.com/price", {
        "symbol": SYMBOL_MAP[symbol],
        "apikey": TWELVE_API_KEY
    })
    return float(d["price"]) if d and "price" in d else None

def get_sma200(symbol):
    d = safe_request("https://api.twelvedata.com/sma", {
        "symbol": SYMBOL_MAP[symbol],
        "interval": "15min",
        "time_period": 200,
        "apikey": TWELVE_API_KEY
    })
    return float(d["values"][0]["sma"]) if d and "values" in d else None

# ==================================================
# 💰 RISK / LOT
# ==================================================
def get_balance():
    return db().execute(
        "SELECT balance FROM balance ORDER BY time DESC LIMIT 1"
    ).fetchone()[0]

def calculate_lot(sl_points):
    risk = get_balance() * RISK_PER_TRADE
    return round(max(risk / (sl_points * POINT_VALUE), 0.01), 2)

# ==================================================
# 🧠 TP / SL OPTIMIZATION (ETAP 17)
# ==================================================
def optimize_symbol(symbol):
    rows = db().execute("""
        SELECT pnl FROM trades
        WHERE symbol=? AND status='CLOSED'
        ORDER BY time_close DESC
        LIMIT ?
    """, (symbol, OPT_LOOKBACK)).fetchall()

    if len(rows) < MIN_TRADES_FOR_OPT:
        return

    wins = [r[0] for r in rows if r[0] > 0]
    losses = [abs(r[0]) for r in rows if r[0] < 0]

    winrate = len(wins) / len(rows)
    avg_win = sum(wins) / len(wins) if wins else DEFAULT_TP
    avg_loss = sum(losses) / len(losses) if losses else DEFAULT_SL

    tp = int(min(max(avg_win, TP_RANGE[0]), TP_RANGE[1]))
    sl = int(min(max(avg_loss, SL_RANGE[0]), SL_RANGE[1]))

    db().execute("""
        UPDATE symbol_params
        SET tp=?, sl=?, last_update=?
        WHERE symbol=?
    """, (tp, sl, datetime.utcnow().isoformat(), symbol)).connection.commit()

# ==================================================
# 📦 TRADE EXECUTION
# ==================================================
def execute_trade(symbol, action, price, sma):
    row = db().execute(
        "SELECT tp, sl FROM symbol_params WHERE symbol=?",
        (symbol,)
    ).fetchone()

    tp, sl = row if row else (DEFAULT_TP, DEFAULT_SL)
    lot = calculate_lot(sl)

    if action == "buy":
        pnl = tp * lot if price > sma else -sl * lot
    else:
        pnl = tp * lot if price < sma else -sl * lot

    db().execute(
        "INSERT INTO trades VALUES (NULL, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?)",
        (
            symbol, action,
            price, price,
            lot, pnl,
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat()
        )
    ).connection.commit()

    db().execute(
        "INSERT INTO balance VALUES (?, ?)",
        (datetime.utcnow().isoformat(), get_balance() + pnl)
    ).connection.commit()

    optimize_symbol(symbol)

    return pnl

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

    register_symbol(parsed["symbol"])

    price = get_price(parsed["symbol"])
    sma = get_sma200(parsed["symbol"])
    if not price or not sma:
        return {"status": "no_data"}

    pnl = execute_trade(parsed["symbol"], parsed["action"], price, sma)

    return {
        "status": "executed",
        "symbol": parsed["symbol"],
        "pnl": pnl,
        "balance": get_balance()
    }

# ==================================================
# 📊 STATS
# ==================================================
@app.get("/stats")
def stats():
    return {
        "balance": get_balance(),
        "symbols": list(db().execute(
            "SELECT symbol, tp, sl FROM symbol_params"
        ))
    }
