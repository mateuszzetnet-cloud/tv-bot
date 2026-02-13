print("VERSION 39 LIVE")

import os
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
    return db().execute(
        "SELECT balance FROM balance ORDER BY time DESC LIMIT 1"
    ).fetchone()[0]

def update_balance(new_balance):
    db().execute(
        "INSERT INTO balance VALUES (?, ?)",
        (datetime.utcnow().isoformat(), new_balance)
    ).connection.commit()

# ==================================================
# 📊 STRATEGY METRICS
# ==================================================
def strategy_metrics(strategy):
    rows = db().execute("""
        SELECT pnl FROM trades
        WHERE strategy=? AND status='CLOSED'
    """, (strategy,)).fetchall()

    if not rows:
        return 0, 0

    pnls = [r[0] for r in rows]
    wins = [p for p in pnls if p > 0]

    winrate = round(len(wins) / len(pnls) * 100, 1)
    return winrate, len(pnls)

# ==================================================
# 📥 OTWARCIE TRADE (z webhook)
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

    entry_price = float(text.split("price:")[1].strip()) if "price:" in text else 2000.0
    lot = 0.1

    db().execute("""
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
    )).connection.commit()

    return {"status": "trade_opened"}

# ==================================================
# 🔒 RĘCZNE ZAMKNIĘCIE TRADE
# ==================================================
@app.post("/close/{trade_id}")
def close_trade(trade_id: int, close_price: float):

    trade = db().execute("""
        SELECT action, entry_price, lot, strategy
        FROM trades
        WHERE id=? AND status='OPEN'
    """, (trade_id,)).fetchone()

    if not trade:
        raise HTTPException(404, "Trade not found")

    action, entry_price, lot, strategy = trade

    if action == "buy":
        pnl = (close_price - entry_price) * lot
    else:
        pnl = (entry_price - close_price) * lot

    db().execute("""
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
    )).connection.commit()

    # aktualizacja balansu
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
# 📋 LISTA TRADE
# ==================================================
@app.get("/trades")
def trades():
    return db().execute("""
        SELECT id, symbol, action, entry_price, close_price,
               lot, status, pnl
        FROM trades ORDER BY id DESC
    """).fetchall()
