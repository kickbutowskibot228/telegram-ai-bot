"""
database.py — PostgreSQL + Redis для Patriot AI Bot
"""
import os
import json
import logging
import psycopg2
import redis
from psycopg2.pool import ThreadedConnectionPool
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ─── Пулы соединений ──────────────────────────────────────
DB_POOL = ThreadedConnectionPool(
    minconn=5,
    maxconn=20,
    dsn=os.getenv("DATABASE_URL")
)

REDIS = redis.Redis.from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    decode_responses=True
)

@contextmanager
def get_db():
    conn = DB_POOL.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        DB_POOL.putconn(conn)


# ─── Кэш пользователей ────────────────────────────────────
USER_TTL = 120  # секунд

def _user_key(uid): return f"user:{uid}"

def invalidate_user_cache(uid: int):
    REDIS.delete(_user_key(uid))

def get_user(uid: int) -> dict | None:
    cached = REDIS.get(_user_key(uid))
    if cached:
        return json.loads(cached)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (uid,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            user = dict(zip(cols, row))
    REDIS.setex(_user_key(uid), USER_TTL, json.dumps(user, default=str))
    return user

def create_or_update_user(uid: int) -> dict:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id)
                VALUES (%s)
                ON CONFLICT (user_id) DO UPDATE SET user_id = EXCLUDED.user_id
                RETURNING *
            """, (uid,))
            cols = [d[0] for d in cur.description]
            user = dict(zip(cols, cur.fetchone()))
    invalidate_user_cache(uid)
    return user

def get_balance(uid: int) -> int:
    user = get_user(uid)
    if not user:
        return 0
    return (user.get("free_tokens") or 0) + (user.get("paid_tokens") or 0)

def add_tokens(uid: int, free: int = 0, paid: int = 0) -> dict:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users
                SET free_tokens = free_tokens + %s,
                    paid_tokens = paid_tokens + %s
                WHERE user_id = %s
                RETURNING free_tokens, paid_tokens
            """, (free, paid, uid))
            row = cur.fetchone()
    invalidate_user_cache(uid)
    return {"free_tokens": row[0], "paid_tokens": row[1]} if row else {}

def deduct_tokens(uid: int, amount: int) -> int:
    """Списываем сначала paid, потом free. Возвращает остаток."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT free_tokens, paid_tokens FROM users WHERE user_id=%s FOR UPDATE", (uid,))
            row = cur.fetchone()
            if not row:
                return 0
            free, paid = row
            if paid >= amount:
                paid -= amount
                free = free
            elif paid + free >= amount:
                amount -= paid
                paid = 0
                free -= amount
            else:
                paid = 0
                free = 0
            cur.execute("""
                UPDATE users SET free_tokens=%s, paid_tokens=%s
                WHERE user_id=%s RETURNING free_tokens + paid_tokens
            """, (free, paid, uid))
            new_balance = cur.fetchone()[0]
    invalidate_user_cache(uid)
    return new_balance

def set_balance(uid: int, free: int = None, paid: int = None):
    fields, vals = [], []
    if free is not None:
        fields.append("free_tokens = %s"); vals.append(free)
    if paid is not None:
        fields.append("paid_tokens = %s"); vals.append(paid)
    if not fields:
        return
    vals.append(uid)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id = %s", vals)
    invalidate_user_cache(uid)

def update_user_field(uid: int, **fields):
    """Обновить любые поля пользователя: update_user_field(uid, model='gpt-4o')"""
    keys = list(fields.keys())
    vals = list(fields.values())
    set_clause = ", ".join(f"{k} = %s" for k in keys)
    vals.append(uid)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE users SET {set_clause} WHERE user_id = %s", vals)
    invalidate_user_cache(uid)

def ban_user(uid: int, banned: bool = True):
    update_user_field(uid, is_banned=1 if banned else 0)


# ─── История чата ─────────────────────────────────────────
def get_chat_history(uid: int, limit: int = 20) -> list:
    key = f"history:{uid}"
    cached = REDIS.get(key)
    if cached:
        return json.loads(cached)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT role, content FROM chat_history
                WHERE user_id = %s ORDER BY id DESC LIMIT %s
            """, (uid, limit))
            rows = [{"role": r[0], "content": r[1]} for r in cur.fetchall()][::-1]
    REDIS.setex(key, 60, json.dumps(rows))
    return rows

def add_chat_message(uid: int, role: str, content: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_history (user_id, role, content)
                VALUES (%s, %s, %s)
            """, (uid, role, content))
    REDIS.delete(f"history:{uid}")

def clear_chat_history(uid: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chat_history WHERE user_id = %s", (uid,))
    REDIS.delete(f"history:{uid}")


# ─── Блокировки ───────────────────────────────────────────
def acquire_lock(uid: int, reason: str = "") -> bool:
    import time
    with get_db() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO user_locks (user_id, locked_at, reason)
                    VALUES (%s, %s, %s)
                """, (uid, time.time(), reason))
                return True
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                return False

def release_lock(uid: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_locks WHERE user_id = %s", (uid,))

def get_active_locks() -> list:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, locked_at, reason FROM user_locks ORDER BY locked_at")
            return [{"user_id": r[0], "locked_at": r[1], "reason": r[2]}
                    for r in cur.fetchall()]


# ─── Статистика ───────────────────────────────────────────
def get_stats() -> dict:
    key = "bot:stats"
    cached = REDIS.get(key)
    if cached:
        return json.loads(cached)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
            banned = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(SUM(free_tokens + paid_tokens), 0) FROM users")
            total_balance = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(DISTINCT user_id) FROM chat_history "
                "WHERE created_at >= NOW() - INTERVAL \'1 day\'"
            )
            active_today = cur.fetchone()[0]
    stats = {
        "total": total,
        "banned": banned,
        "total_balance": int(total_balance),
        "active_today": active_today
    }
    REDIS.setex(key, 60, json.dumps(stats))
    return stats
