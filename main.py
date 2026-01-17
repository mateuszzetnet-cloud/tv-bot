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
# ⚠️ RISK CONFIG
# ==================================================
RISK_PER_TRADE = 0.01
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10

TP_POINTS = 20
SL_POINTS = 10
POINT_VALUE = 1.0

SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
}

DISABLE_DD = 0.08
COOLDOWN_DAYS = 14
TEST_MIN_TRADES = 30
TEST_MIN_WINRATE = 0.55

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
        CREATE TABLE IF NOT EXISTS strategy_state (
            symbol TEXT PRIMARY KEY,
            status TEXT,
            disabled_until TEXT,
            last_reason TEXT
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
            con.execute("""
                INSERT OR IGNORE INTO strategy_state
                VALUES (?, 'ACTIVE', NULL, NULL)
            """, (s,))

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
# 🧠 PERFORMANCE
# ==================================================
def performance_stats(symbol):
    rows = db().execute("""
        SELECT pnl, time_close FROM trades
        WHERE symbol=? AND status='CLOSED'
    """, (symbol,)).fetchall()

    if len(rows) < 10:
        return None

    pnls = [r[0] for r in rows]
    wins = [p for p in pnls if p > 0]

    balance = START_BALANCE
    peak = balance
    max_dd = 0

    for p in pnls:
        balance += p
        peak = max(peak, balance)
        dd = (peak - balance) / peak
        max_dd = max(max_dd, dd)

    days = {r[1][:10] for r in rows if r[1]}

    return {
        "trades": len(pnls),
        "winrate": len(wins) / len(pnls),
        "expectancy": sum(pnls) / len(pnls),
        "max_dd": max_dd,
        "days": len(days)
    }

# ==================================================
# 🧠 STRATEGY STATE MANAGER (ETAP 26)
# ==================================================
def update_strategy_state(symbol):
    stats = performance_stats(symbol)
    if not stats:
        return

    cur = db().execute("""
        SELECT status, disabled_until FROM strategy_state WHERE symbol=?
    """, (symbol,))
    status, disabled_until = cur.fetchone()

    now = datetime.utcnow()

    if status == "DISABLED":
        if disabled_until and now >= datetime.fromisoformat(disabled_until):
            db().execute("""
                UPDATE strategy_state
                SET status='TESTING', disabled_until=NULL
                WHERE symbol=?
            """, (symbol,))
            db().connection.commit()
        return

    if stats["max_dd"] >= DISABLE_DD:
        until = (now + timedelta(days=COOLDOWN_DAYS)).isoformat()
        db().execute("""
            UPDATE strategy_state
            SET status='DISABLED',
                disabled_until=?,
                last_reason='max_dd'
            WHERE symbol=?
        """, (until, symbol))
        db().connection.commit()
        return

    if status == "TESTING":
        if (
            stats["trades"] >= TEST_MIN_TRADES and
            stats["winrate"] >= TEST_MIN_WINRATE and
            stats["expectancy"] > 0
        ):
            db().execute("""
                UPDATE strategy_state
                SET status='ACTIVE', last_reason=NULL
                WHERE symbol=?
            """, (symbol,))
            db().connection.commit()

# ==================================================
# 📄 TRADING
# ==================================================
def open_trade(symbol, action, price):
    lot = round((get_balance() * RISK_PER_TRADE) / (SL_POINTS * POINT_VALUE), 2)
    db().execute("""
        INSERT INTO trades
        (symbol, action, entry_price, lot, status, pnl, time_open)
        VALUES (?, ?, ?, ?, 'OPEN', 0, ?)
    """, (symbol, action, price, lot, datetime.utcnow().isoformat()))
    db().connection.commit()

def close_trade(trade_id, pnl, price):
    db().execute("""
        UPDATE trades
        SET status='CLOSED',
            exit_price=?,
            pnl=?,
            time_close=?
        WHERE id=?
    """, (price, pnl, datetime.utcnow().isoformat(), trade_id))
    db().connection.commit()
    update_balance(pnl)

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

    update_strategy_state(parsed["symbol"])

    state = db().execute("""
        SELECT status FROM strategy_state WHERE symbol=?
    """, (parsed["symbol"],)).fetchone()[0]

    if state == "DISABLED":
        return {"status": "blocked", "reason": "strategy_disabled"}

    price = get_price(parsed["symbol"])
    if price:
        open_trade(parsed["symbol"], parsed["action"], price)

    return {"status": "ok", "strategy_state": state}

# ==================================================
# 📊 DASHBOARD
# ==================================================
@app.get("/dashboard")
def dashboard():
    strategies = []
    for s in SYMBOL_MAP:
        stats = performance_stats(s)
        row = db().execute("""
            SELECT status, disabled_until FROM strategy_state WHERE symbol=?
        """, (s,)).fetchone()

        strategies.append({
            "symbol": s,
            "status": row[0],
            "disabled_until": row[1],
            "stats": stats
        })

    return {
        "engine": {
            "mode": get_state("engine_status"),
            "balance": get_balance()
        },
        "strategies": strategies
    }

# ==================================================
# 📊 STATS & TRADES
# ==================================================
@app.get("/stats")
def stats():
    return {
        "engine_status": get_state("engine_status"),
        "balance": get_balance()
    }

@app.get("/trades")
def trades(limit: int = 50):
    cur = db().execute("""
        SELECT symbol, action, entry_price, exit_price, pnl, status, time_open
        FROM trades ORDER BY id DESC LIMIT ?
    """, (limit,))
    return cur.fetchall()
