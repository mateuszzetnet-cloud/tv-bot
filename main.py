print("VERSION 40 STABLE")

import sqlite3
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException

# ==================================================
# 🔧 APP
# ==================================================
app = FastAPI()

DB_FILE = "trading.db"
START_BALANCE = 1000.0

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
            close_price REAL,
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

        if con.execute("SELECT COUNT(*) FROM balance").fetchone()[0] == 0:
            con.execute(
                "INSERT INTO balance VALUES (?, ?)",
                (datetime.utcnow().isoformat(), START_BALANCE)
            )

init_db()

# ==================================================
# 💰 BALANCE
# ==================================================
def get_balance():
    row = db().execute(
        "SELECT balance FROM balance ORDER BY time DESC LIMIT 1"
    ).fetchone()

    return row[0] if row else START_BALANCE

def update_balance(new_balance):
    with db() as con:
        con.execute(
            "INSERT INTO balance VALUES (?, ?)",
            (datetime.utcnow().isoformat(), new_balance)
        )

# ==================================================
# 📥 OPEN TRADE (WEBHOOK)
# ==================================================
@app.post("/webhook")
async def webhook(request: Request):

    text = (await request.body()).decode().lower()

    if "buy" in text:
        action = "buy"
    elif "sell" in text:
        action = "sell"
    else:
        return {"status": "ignored"}

    symbol = "XAUUSD"
    strategy = "MANUAL"

    entry_price = 2000.0
    if "price:" in text:
        try:
            entry_price = float(text.split("price:")[1].strip())
        except:
            pass

    lot = 0.1

    with db() as con:
        con.execute("""
            INSERT INTO trades
            (symbol, strategy, action, entry_price, close_price, lot, status, pnl, time_open)
            VALUES (?, ?, ?, ?, NULL, ?, 'OPEN', 0, ?)
        """, (
            symbol,
            strategy,
            action,
            entry_price,
            lot,
            datetime.utcnow().isoformat()
        ))

    return {"status": "trade_opened"}

# ==================================================
# 🔒 CLOSE TRADE
# ==================================================
@app.post("/close/{trade_id}")
def close_trade(trade_id: int, close_price: float):

    trade = db().execute("""
        SELECT action, entry_price, lot
        FROM trades
        WHERE id=? AND status='OPEN'
    """, (trade_id,)).fetchone()

    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    action, entry_price, lot = trade

    if action == "buy":
        pnl = (close_price - entry_price) * lot
    else:
        pnl = (entry_price - close_price) * lot

    with db() as con:
        con.execute("""
            UPDATE trades
            SET status='CLOSED',
                close_price=?,
                pnl=?,
                time_close=?
            WHERE id=?
        """, (
            close_price,
            pnl,
            datetime.utcnow().isoformat(),
            trade_id
        ))

    new_balance = get_balance() + pnl
    update_balance(new_balance)

    return {
        "closed_trade": trade_id,
        "pnl": round(pnl, 2),
        "new_balance": round(new_balance, 2)
    }

# ==================================================
# 📊 DASHBOARD
# ==================================================
@app.get("/dashboard")
def dashboard():

    open_trades = db().execute("""
        SELECT id, symbol, strategy, action, entry_price, lot
        FROM trades
        WHERE status='OPEN'
    """).fetchall()

    closed = db().execute("""
        SELECT pnl FROM trades WHERE status='CLOSED'
    """).fetchall()

    total_pnl = sum(r[0] for r in closed) if closed else 0

    return {
        "balance": get_balance(),
        "open_trades": open_trades,
        "closed_trades": len(closed),
        "total_pnl": round(total_pnl, 2)
    }

# ==================================================
# 📊 STATS
# ==================================================
@app.get("/stats")
def stats():

    closed = db().execute("""
        SELECT pnl FROM trades WHERE status='CLOSED'
    """).fetchall()

    wins = [r for r in closed if r[0] > 0]

    winrate = 0
    if closed:
        winrate = round(len(wins) / len(closed) * 100, 1)

    return {
        "balance": get_balance(),
        "total_trades": len(closed),
        "winrate": winrate
    }

# ==================================================
# 📈 EQUITY
# ==================================================
@app.get("/equity")
def equity():

    rows = db().execute("""
        SELECT time, balance FROM balance
        ORDER BY time ASC
    """).fetchall()

    return [
        {
            "time": r[0],
            "balance": r[1]
        }
        for r in rows
    ]

# ==================================================
# 📋 ALL TRADES
# ==================================================
@app.get("/trades")
def trades():

    rows = db().execute("""
        SELECT id, symbol, strategy, action,
               entry_price, close_price,
               lot, status, pnl,
               time_open, time_close
        FROM trades
        ORDER BY id DESC
    """).fetchall()

    return rows
