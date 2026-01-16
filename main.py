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
            "daily_pnl": "0"
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

def calculate_lot():
    bal = get_balance()
    risk = bal * RISK_PER_TRADE
    return round(max(risk / (SL_POINTS * POINT_VALUE), 0.01), 2)

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

    winrate = len(wins) / len(pnls)
    expectancy = sum(pnls) / len(pnls)

    bal = START_BALANCE
    peak = bal
    max_dd = 0

    for p in pnls:
        bal += p
        peak = max(peak, bal)
        max_dd = max(max_dd, (peak - bal) / peak)

    days = {r[1][:10] for r in rows if r[1]}

    return {
        "trades": len(pnls),
        "winrate": winrate,
        "expectancy": expectancy,
        "max_dd": max_dd,
        "days": len(days)
    }

# ==================================================
# 🧠 LIVE GATE
# ==================================================
def check_live_ready(symbol):
    s = performance_stats(symbol)
    if not s:
        return False
    return (
        s["trades"] >= 100 and
        s["winrate"] >= 0.55 and
        s["expectancy"] > 0 and
        s["max_dd"] < 0.08 and
        s["days"] >= 30
    )

# ==================================================
# 🛑 RISK GUARD
# ==================================================
def risk_guard():
    today = str(date.today())
    if get_state("daily_date") != today:
        set_state("daily_date", today)
        set_state("daily_pnl", "0")

    daily_pnl = float(get_state("daily_pnl"))
    if daily_pnl <= -START_BALANCE * MAX_DAILY_LOSS:
        set_state("engine_status", "PAUSED")
        return False

    bal = get_balance()
    peak = float(get_state("peak_balance"))
    if (peak - bal) / peak >= MAX_DRAWDOWN:
        set_state("engine_status", "PAUSED")
        return False

    return True

# ==================================================
# 🔁 CHECK OPEN TRADES
# ==================================================
def check_open_trades(symbol, price):
    trades = db().execute("""
        SELECT id, action, entry_price, tp, sl, lot
        FROM trades
        WHERE symbol=? AND status='OPEN'
    """, (symbol,)).fetchall()

    for t in trades:
        trade_id, action, entry, tp, sl, lot = t

        hit_tp = price >= tp if action == "buy" else price <= tp
        hit_sl = price <= sl if action == "buy" else price >= sl

        if hit_tp or hit_sl:
            exit_price = tp if hit_tp else sl
            pnl = (exit_price - entry) * lot * POINT_VALUE
            if action == "sell":
                pnl *= -1

            db().execute("""
                UPDATE trades
                SET status='CLOSED',
                    exit_price=?,
                    pnl=?,
                    time_close=?
                WHERE id=?
            """, (
                exit_price,
                pnl,
                datetime.utcnow().isoformat(),
                trade_id
            )).connection.commit()

            update_balance(pnl)
            daily = float(get_state("daily_pnl")) + pnl
            set_state("daily_pnl", daily)

# ==================================================
# 📄 OPEN TRADE
# ==================================================
def open_trade(parsed, price):
    lot = calculate_lot()

    if parsed["action"] == "buy":
        tp = price + TP_POINTS
        sl = price - SL_POINTS
    else:
        tp = price - TP_POINTS
        sl = price + SL_POINTS

    db().execute("""
        INSERT INTO trades
        (symbol, action, entry_price, tp, sl, lot, status, pnl, time_open)
        VALUES (?, ?, ?, ?, ?, ?, 'OPEN', 0, ?)
    """, (
        parsed["symbol"],
        parsed["action"],
        price,
        tp,
        sl,
        lot,
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

    check_open_trades(parsed["symbol"], price)

    if not risk_guard():
        return {"status": "paused"}

    if check_live_ready(parsed["symbol"]):
        set_state("engine_status", "LIVE")
    else:
        set_state("engine_status", "PAPER")

    open_trade(parsed, price)

    return {
        "status": "trade_opened",
        "engine": get_state("engine_status")
    }

# ==================================================
# 📊 STATS
# ==================================================
@app.get("/stats")
def stats():
    return {
        "engine_status": get_state("engine_status"),
        "balance": get_balance(),
        "performance": performance_stats("XAUUSD")
    }

@app.get("/trades")
def trades(limit: int = 50):
    return db().execute("""
        SELECT symbol, action, entry_price, exit_price, pnl, status, time_open
        FROM trades
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()
