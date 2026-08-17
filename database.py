import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "bot.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            full_name TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            product TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def upsert_user(tg_id, username, full_name):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO users (tg_id, username, full_name) VALUES (?, ?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name
        """,
        (tg_id, username, full_name),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_transaction(tg_id, product, amount):
    conn = get_conn()
    user = conn.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    if not user:
        conn.close()
        raise ValueError("User not found")
    conn.execute(
        "INSERT INTO transactions (user_id, product, amount) VALUES (?, ?, ?)",
        (user["id"], product, amount),
    )
    conn.commit()
    conn.close()


def get_leaderboard(period="all"):
    conn = get_conn()
    since = None
    if period == "day":
        since = (datetime.utcnow() - timedelta(days=1)).isoformat()
    elif period == "week":
        since = (datetime.utcnow() - timedelta(days=7)).isoformat()
    elif period == "month":
        since = (datetime.utcnow() - timedelta(days=30)).isoformat()

    query = """
        SELECT u.full_name, u.username, SUM(t.amount) as total, COUNT(t.id) as count
        FROM transactions t JOIN users u ON u.id = t.user_id
    """
    params = []
    if since:
        query += " WHERE t.created_at >= ?"
        params.append(since)
    query += " GROUP BY u.id ORDER BY total DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_config(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_config(key):
    conn = get_conn()
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None
