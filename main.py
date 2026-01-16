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

# === RISK ===
RISK_PER_TRADE = 0.01
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10

TP_POINTS = 20
SL_POINTS = 10
POINT_VALUE = 1.0

# === ADAPTIVE ENGINE ===
AUTO_REGISTER_ENABLED = 1
MAX_TRADES_PER_DAY = 5
MIN_TRADES_FOR_EVAL = 10
MIN_WINRATE = 0.45
COOLDOWN_DAYS = 2

# === SYMBOL MAP (FX + METALS + CRYPTO) ===
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
            total_trades INTEGER,
            wins INTEGER,
            losses INTEGER,
            winrate REAL,
            cooldown_until TEXT,
            created_at TEXT,
            last_trade_at TEXT
        )
        """)

        # === FEATURE STORE (STEP 16) ===
        con.execute("""
        CREATE TABLE IF NOT EXISTS features (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            action TEXT,
            price REAL,
            sma200 REAL,
            distance REAL,
            pnl REAL,
            timestamp TEXT
        )
        """)

        if con.execute("SELECT COUNT(*) FROM balance").fetchone()[0] == 0:
            con.execute(
                "INSERT INTO balance VALUES (?, ?)",
                (datetime.utcnow().isoformat(), START_BALANCE)
            )

        defaults = {
            "engine_status": "ACTIVE",
            "peak_balance": str(START_BALANCE),
            "daily_date": str(date.today()),
            "daily_pnl": "0"
        }

        for k, v in defaults.items():
            con.execute("INSERT OR IGNORE INTO engine_state VALUES (?, ?)", (k, v))

init_db()

# ==================================================
# 🔎 PARSER (MULTI SYMBOL)
# ==================================================
def parse_signal(text: str):
    t = text.lower()
    action = "buy" if "buy" in t else "sell" if "sell" in t else None
    if not action:
        return None

    symbol = None
    for s in SYMBOL_MAP:
        if s.lower() in t:
            symbol = s
            break

    if not symbol:
        return None

    return {"symbol": symbol, "action": action}

# ==================================================
# 🧠 SYMBOL ENGINE
# ==================================================
def register_symbol(symbol):
    if not AUTO_REGISTER_ENABLED:
        return
    with db() as con:
        con.execute("""
            INSERT OR IGNORE INTO symbols
            VALUES (?, 1, 0, 0, 0, 0, NULL, ?, NULL)
        """, (symbol, datetime.utcnow().isoformat()))

def symbol_allowed(symbol):
    row = db().execute("""
        SELECT enabled, cooldown_until FROM symbols WHERE symbol=?
    """, (symbol,)).fetchone()

    if not row:
        return False

    enabled, cooldown = row

    if cooldown and datetime.utcnow() >= datetime.fromisoformat(cooldown):
        db().execute("""
            UPDATE symbols SET enabled=1, cooldown_until=NULL WHERE symbol=?
        """, (symbol,)).connection.commit()
        enabled = 1

    return bool(enabled)

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

# ==================================================
# 📦 TRADES + FEATURE STORE
# ==================================================
def calculate_lot():
    return round(max((get_balance() * RISK_PER_TRADE) / SL_POINTS, 0.01), 2)

def store_features(symbol, action, price, sma, pnl):
    db().execute("""
        INSERT INTO features
        (symbol, action, price, sma200, distance, pnl, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        symbol, action, price, sma,
        price - sma if sma else 0,
        pnl,
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

    register_symbol(parsed["symbol"])

    if not symbol_allowed(parsed["symbol"]):
        return {"status": "symbol_blocked"}

    price = get_price(parsed["symbol"])
    sma = get_sma200(parsed["symbol"])

    if not price or not sma:
        return {"status": "no_data"}

    if parsed["action"] == "buy" and price > sma:
        pnl = TP_POINTS * calculate_lot()
    elif parsed["action"] == "sell" and price < sma:
        pnl = TP_POINTS * calculate_lot()
    else:
        pnl = -SL_POINTS * calculate_lot()

    update_balance(pnl)
    store_features(parsed["symbol"], parsed["action"], price, sma, pnl)

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
            "SELECT symbol, enabled, total_trades, winrate FROM symbols"
        ))
    }
