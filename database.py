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
            monthly_goal REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            product TEXT NOT NULL,
            category TEXT,
            buyer_username TEXT,
            amount REAL NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # миграции для баз, созданных до появления новых колонок
    try:
        conn.execute("ALTER TABLE users ADD COLUMN monthly_goal REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN category TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN buyer_username TEXT")
    except sqlite3.OperationalError:
        pass
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


def add_transaction(tg_id, product, amount, category=None, buyer_username=None):
    conn = get_conn()
    user = conn.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    if not user:
        conn.close()
        raise ValueError("User not found")
    conn.execute(
        "INSERT INTO transactions (user_id, product, category, buyer_username, amount) VALUES (?, ?, ?, ?, ?)",
        (user["id"], product, category, buyer_username, amount),
    )
    conn.commit()
    conn.close()


def get_buyer_total(buyer_username):
    """Суммарная сумма покупок конкретного покупателя (по юзернейму, без учёта регистра)."""
    if not buyer_username:
        return 0.0
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as total FROM transactions WHERE LOWER(buyer_username)=LOWER(?)",
        (buyer_username,),
    ).fetchone()
    conn.close()
    return row["total"]


def get_buyer_stats(buyer_username):
    """Детальная статистика по покупателю: сколько всего потратил и что именно покупал."""
    conn = get_conn()
    total_row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as total, COUNT(*) as count FROM transactions WHERE LOWER(buyer_username)=LOWER(?)",
        (buyer_username,),
    ).fetchone()
    items = conn.execute(
        """
        SELECT product, category, amount, created_at FROM transactions
        WHERE LOWER(buyer_username)=LOWER(?)
        ORDER BY created_at DESC LIMIT 20
        """,
        (buyer_username,),
    ).fetchall()
    conn.close()
    return {
        "total": total_row["total"],
        "count": total_row["count"],
        "items": [dict(i) for i in items],
    }


def _period_start(period):
    now = datetime.utcnow()
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start - timedelta(days=start.weekday())
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


def get_leaderboard(period="all"):
    conn = get_conn()
    since = _period_start(period)

    query = """
        SELECT u.full_name, u.username, SUM(t.amount) as total, COUNT(t.id) as count
        FROM transactions t JOIN users u ON u.id = t.user_id
    """
    params = []
    if since:
        query += " WHERE t.created_at >= ?"
        params.append(since.strftime("%Y-%m-%d %H:%M:%S"))
    query += " GROUP BY u.id ORDER BY total DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_stats(tg_id):
    conn = get_conn()
    user = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    if not user:
        conn.close()
        return None
    user_id = user["id"]

    def sum_since(since):
        query = "SELECT COALESCE(SUM(amount),0) as total, COUNT(*) as count FROM transactions WHERE user_id=?"
        params = [user_id]
        if since:
            query += " AND created_at >= ?"
            params.append(since)
        row = conn.execute(query, params).fetchone()
        return row["total"], row["count"]

    day_since = _period_start("day").strftime("%Y-%m-%d %H:%M:%S")
    week_since = _period_start("week").strftime("%Y-%m-%d %H:%M:%S")
    month_since = _period_start("month").strftime("%Y-%m-%d %H:%M:%S")

    total_day, count_day = sum_since(day_since)
    total_week, count_week = sum_since(week_since)
    total_month, count_month = sum_since(month_since)
    total_all, count_all = sum_since(None)

    # ранг в общем зачёте (all-time)
    rank_row = conn.execute(
        """
        SELECT COUNT(*) + 1 as rank FROM (
            SELECT user_id, SUM(amount) as total FROM transactions GROUP BY user_id
        ) sub WHERE sub.total > ?
        """,
        (total_all,),
    ).fetchone()
    total_workers = conn.execute("SELECT COUNT(DISTINCT user_id) as c FROM transactions").fetchone()["c"]
    monthly_goal = user["monthly_goal"] or 0

    conn.close()
    streak = get_user_streak(tg_id)
    return {
        "full_name": user["full_name"],
        "username": user["username"],
        "day": {"total": total_day, "count": count_day},
        "week": {"total": total_week, "count": count_week},
        "month": {"total": total_month, "count": count_month},
        "all": {"total": total_all, "count": count_all},
        "rank": rank_row["rank"] if total_all else None,
        "total_workers": total_workers,
        "streak": streak,
        "monthly_goal": monthly_goal,
    }


def get_user_streak(tg_id):
    """Считает, сколько дней подряд (включая сегодня либо вчера) есть хотя бы одна продажа."""
    conn = get_conn()
    user = conn.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    if not user:
        conn.close()
        return 0
    rows = conn.execute(
        "SELECT DISTINCT date(created_at) as d FROM transactions WHERE user_id=? ORDER BY d DESC",
        (user["id"],),
    ).fetchall()
    conn.close()
    if not rows:
        return 0

    dates = [datetime.strptime(r["d"], "%Y-%m-%d").date() for r in rows]
    today = datetime.utcnow().date()
    streak = 0
    cursor = today
    # если сегодня продаж ещё не было, стрик мог продолжаться со вчера
    if dates[0] != today:
        cursor = today - timedelta(days=1)
    for d in dates:
        if d == cursor:
            streak += 1
            cursor -= timedelta(days=1)
        elif d < cursor:
            break
    return streak


def set_monthly_goal(tg_id, amount):
    conn = get_conn()
    conn.execute("UPDATE users SET monthly_goal=? WHERE tg_id=?", (amount, tg_id))
    conn.commit()
    conn.close()


def get_top_tg_ids(period="all", limit=3):
    conn = get_conn()
    since = _period_start(period)
    since = since.strftime("%Y-%m-%d %H:%M:%S") if since else None

    query = """
        SELECT u.tg_id, SUM(t.amount) as total
        FROM transactions t JOIN users u ON u.id = t.user_id
    """
    params = []
    if since:
        query += " WHERE t.created_at >= ?"
        params.append(since)
    query += " GROUP BY u.id ORDER BY total DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [r["tg_id"] for r in rows]


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
