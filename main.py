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

MAX_TRADES_PER_SYMBOL = 1
SYMBOL_COOLDOWN_MIN = 5

TP_POINTS = 20
SL_POINTS = 10
POINT_VALUE = 1.0

# ==================================================
# 🌍 SYMBOLS
# ==================================================
SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "BTCUSD": "BTC/USD",
    "ETHUSD": "ETH/USD",
}

# ==================================================
# 🧠 STRATEGIES
# ==================================================
STRATEGIES = [
    "SMA200_TREND",
    "SMA200_PULLBACK",
    "BREAKOUT",
    "MEAN_REVERSION",
    "MOMENTUM",
    "REVERSAL",
]

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
            strategy TEXT,
            action TEXT,
            entry_price REAL,
            exit_price REAL,
            lot REAL,
            status TEXT,
            pnl REAL,
            reason TEXT,
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
            status TEXT,
            disabled_until TEXT,
            last_reason TEXT,
            PRIMARY KEY (symbol, strategy)
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
                    VALUES (?, ?, 'ACTIVE', NULL, NULL)
                """, (s, strat))

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

def calculate_lot():
    bal = get_balance()
    risk = bal * RISK_PER_TRADE
    return round(max(risk / (SL_POINTS * POINT_VALUE), 0.01), 2)

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
# 🧠 STRATEGY LOGIC (PLACEHOLDER)
# ==================================================
def strategy_signal(strategy, symbol, action):
    """
    Etap 27: strategie logiczne jako moduły
    Na razie każda reaguje na impuls z webhooka
    """
    return action

# ==================================================
# 📄 PAPER ENGINE
# ==================================================
def open_trade(symbol, strategy, action, price, reason):
    lot = calculate_lot()
    db().execute("""
        INSERT INTO trades
        (symbol, strategy, action, entry_price, lot, status, pnl, reason, time_open)
        VALUES (?, ?, ?, ?, ?, 'OPEN', 0, ?, ?)
    """, (
        symbol,
        strategy,
        action,
        price,
        lot,
        reason,
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

def manage_trades(symbol, price):
    if price is None:
        return

    cur = db().execute("""
        SELECT id, action, entry_price, lot
        FROM trades WHERE status='OPEN' AND symbol=?
    """, (symbol,))

    for tid, action, entry, lot in cur.fetchall():
        direction = 1 if action == "buy" else -1
        diff = (price - entry) * direction

        if diff >= TP_POINTS:
            close_trade(tid, TP_POINTS * lot * POINT_VALUE, price)
        elif diff <= -SL_POINTS:
            close_trade(tid, -SL_POINTS * lot * POINT_VALUE, price)

# ==================================================
# 🚦 RISK LOCKS
# ==================================================
def check_risk():
    today = str(date.today())

    if get_state("daily_date") != today:
        set_state("daily_date", today)
        set_state("daily_pnl", 0)
        set_state("engine_status", "LEARNING")

    bal = get_balance()
    peak = float(get_state("peak_balance"))

    if (peak - bal) / peak >= MAX_DRAWDOWN:
        set_state("engine_status", "DD_LOCK")

# ==================================================
# 🌐 WEBHOOK
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):
    if request.query_params.get("token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)

    body = (await request.body()).decode().lower()
    action = "buy" if "buy" in body else "sell" if "sell" in body else None
    if not action:
        return {"status": "ignored"}

    check_risk()
    engine = get_state("engine_status")

    results = []

    for symbol in SYMBOL_MAP:
        price = get_price(symbol)
        manage_trades(symbol, price)

        for strat in STRATEGIES:
            state = db().execute("""
                SELECT status FROM strategy_state
                WHERE symbol=? AND strategy=?
            """, (symbol, strat)).fetchone()[0]

            if state != "ACTIVE":
                continue

            signal = strategy_signal(strat, symbol, action)
            if not signal:
                continue

            open_trade(symbol, strat, signal, price, "webhook_impulse")
            results.append(f"{symbol}:{strat}")

    return {
        "engine": engine,
        "opened": results
    }

# ==================================================
# 📊 STATS
# ==================================================
@app.get("/stats")
def stats():
    cur = db().execute("""
        SELECT strategy, COUNT(*) FROM trades
        GROUP BY strategy
    """)
    return {
        "engine_status": get_state("engine_status"),
        "balance": get_balance(),
        "trades_by_strategy": dict(cur.fetchall())
    }

@app.get("/trades")
def trades(limit: int = 50):
    cur = db().execute("""
        SELECT symbol, strategy, action, pnl, status, time_open
        FROM trades
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    return cur.fetchall()

@app.get("/dashboard")
def dashboard():
    cur = db().execute("""
        SELECT symbol, strategy, status
        FROM strategy_state
    """)
    return cur.fetchall()
