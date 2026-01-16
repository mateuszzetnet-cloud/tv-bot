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
START_BALANCE = 10_000.0

# === RISK ===
RISK_PER_TRADE = 0.01
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10

TP_POINTS = 20
SL_POINTS = 10
POINT_VALUE = 1.0

# === ADAPTATION ===
AUTO_REGISTER_ENABLED = 1
MAX_TRADES_PER_DAY = 5
MIN_TRADES_FOR_EVAL = 10
MIN_WINRATE = 0.45
COOLDOWN_DAYS = 2

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
            con.execute(
                "INSERT OR IGNORE INTO engine_state VALUES (?, ?)",
                (k, v)
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

    symbol = "XAUUSD" if "xauusd" in t else None
    if symbol not in SYMBOL_MAP:
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
        SELECT enabled, cooldown_until, last_trade_at FROM symbols WHERE symbol=?
    """, (symbol,)).fetchone()

    if not row:
        return False

    enabled, cooldown, last_trade = row

    # auto re-enable after cooldown
    if cooldown and datetime.utcnow() >= datetime.fromisoformat(cooldown):
        db().execute("""
            UPDATE symbols SET enabled=1, cooldown_until=NULL WHERE symbol=?
        """, (symbol,)).connection.commit()
        enabled = 1

    if not enabled:
        return False

    # daily trade limit
    if last_trade:
        if datetime.fromisoformat(last_trade).date() == date.today():
            cnt = db().execute("""
                SELECT COUNT(*) FROM trades
                WHERE symbol=? AND DATE(time_open)=?
            """, (symbol, date.today().isoformat())).fetchone()[0]

            if cnt >= MAX_TRADES_PER_DAY:
                return False

    return True

def update_symbol_after_trade(symbol, pnl):
    with db() as con:
        total, wins, losses = con.execute("""
            SELECT total_trades, wins, losses FROM symbols WHERE symbol=?
        """, (symbol,)).fetchone()

        total += 1
        wins += 1 if pnl > 0 else 0
        losses += 1 if pnl <= 0 else 0

        winrate = wins / total
        enabled = 1
        cooldown = None

        if total >= MIN_TRADES_FOR_EVAL and winrate < MIN_WINRATE:
            enabled = 0
            cooldown = (datetime.utcnow() + timedelta(days=COOLDOWN_DAYS)).isoformat()
            logging.warning(f"[AUTO] {symbol} disabled (winrate={winrate:.2f})")

        con.execute("""
            UPDATE symbols
            SET total_trades=?, wins=?, losses=?, winrate=?,
                enabled=?, cooldown_until=?, last_trade_at=?
            WHERE symbol=?
        """, (
            total, wins, losses, winrate,
            enabled, cooldown, datetime.utcnow().isoformat(), symbol
        ))

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
# 📦 TRADES
# ==================================================
def calculate_lot():
    return round(max((get_balance() * RISK_PER_TRADE) / SL_POINTS, 0.01), 2)

def open_trade(parsed, price):
    db().execute("""
        INSERT INTO trades
        (symbol, action, entry_price, lot, status, pnl, time_open)
        VALUES (?, ?, ?, ?, 'OPEN', 0, ?)
    """, (
        parsed["symbol"],
        parsed["action"],
        price,
        calculate_lot(),
        datetime.utcnow().isoformat()
    )).connection.commit()

def close_trade(tid, symbol, pnl, price):
    db().execute("""
        UPDATE trades
        SET status='CLOSED', exit_price=?, pnl=?, time_close=?
        WHERE id=?
    """, (price, pnl, datetime.utcnow().isoformat(), tid)).connection.commit()

    update_balance(pnl)
    update_symbol_after_trade(symbol, pnl)

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

    if price and sma:
        if parsed["action"] == "buy" and price > sma:
            open_trade(parsed, price)
            return {"status": "opened"}
        if parsed["action"] == "sell" and price < sma:
            open_trade(parsed, price)
            return {"status": "opened"}

    return {"status": "rejected"}

# ==================================================
# 📊 STATS
# ==================================================
@app.get("/stats")
def stats():
    return {
        "balance": get_balance(),
        "symbols": [
            dict(row) for row in db().execute("""
                SELECT symbol, enabled, total_trades, winrate,
                       cooldown_until, last_trade_at
                FROM symbols
            """)
        ]
    }
