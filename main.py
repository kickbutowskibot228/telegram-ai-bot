import os
import re
import time
import uuid
import base64
import sqlite3
import logging
import mimetypes
import requests
import telebot

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timedelta
from flask import Flask, request, abort
from telebot import types
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL")

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {
    int(x.strip())
    for x in ADMIN_IDS_RAW.split(",")
    if x.strip().isdigit()
}

YOOKASSA_ENABLED = all([
    YOOKASSA_SHOP_ID,
    YOOKASSA_SECRET_KEY,
    YOOKASSA_RETURN_URL
])

PORT = int(os.environ.get("PORT", 10000))

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN")

if not OPENROUTER_API_KEY:
    raise RuntimeError("Не задан OPENROUTER_API_KEY")

HTTP = requests.Session()

retry = Retry(
    total=2,
    connect=2,
    read=2,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"])
)

adapter = HTTPAdapter(
    pool_connections=50,
    pool_maxsize=50,
    max_retries=retry
)

HTTP.mount("https://", adapter)
HTTP.mount("http://", adapter)

OPENROUTER_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json"
}


bot = telebot.TeleBot(
    TELEGRAM_TOKEN,
    parse_mode="Markdown",
    threaded=True,
    num_threads=16
)

app = Flask(__name__)
WEBHOOK_EXECUTOR = ThreadPoolExecutor(max_workers=8)


DB_PATH = "bot.db"
FREE_TOKENS = 40
FREE_RESET_COOLDOWN_DAYS = 3
TOKEN_EMOJI = "🍼"
GENERATED_DIR = "generated_images"

CHAT_HISTORY_LIMIT = 12

os.makedirs(GENERATED_DIR, exist_ok=True)

GENERATED_VIDEOS_DIR = "generated_videos"
os.makedirs(GENERATED_VIDEOS_DIR, exist_ok=True)

BTN_AI = "🧠 GPT/Gemini/Claude"
BTN_NANO = "🍌 Nano Banana"
BTN_VIDEO = "🎬 Kling"
BTN_BALANCE = "📊 Баланс"
BTN_TOPUP = "💳 Пополнение"
BTN_RESET = "🔄 Сброс"
BTN_EXIT = "❌ Выйти из режима"
BTN_SUPPORT = "🛟 Поддержка"

SUPPORT_USERNAME = "ai_patriot_support"
SUPPORT_URL = f"https://t.me/{SUPPORT_USERNAME}"

TEXT_MODELS = {
    "openai/gpt-5.2": "GPT-5.2",
    "google/gemini-3-flash-preview": "Gemini 3 Flash",
    "anthropic/claude-opus-4.6": "Claude Opus 4.6",
    "anthropic/claude-sonnet-4.6": "Claude Sonnet 4.6"
}

TEXT_MODEL_COSTS = {
    "google/gemini-3-flash-preview": 2,
    "openai/gpt-5.2": 5,
    "anthropic/claude-sonnet-4.6": 8,
    "anthropic/claude-opus-4.6": 12
}

IMAGE_MODELS = {
    "google/gemini-3-pro-image-preview": "🍌 Nano Banana Pro"
}

PROMPT_ONLY_COSTS = {
    "google/gemini-3-pro-image-preview": 10
}

PHOTO_PROMPT_COSTS = {
    "google/gemini-3-pro-image-preview": 12
}

DEFAULT_MODEL = "google/gemini-3-flash-preview"
DEFAULT_IMAGE_MODEL = "google/gemini-3-pro-image-preview"

VIDEO_MODELS = {
    "kwaivgi/kling-video-o1": "🎬 Kling Video O1"
}

VIDEO_PROMPT_COSTS = {
    "kwaivgi/kling-video-o1": {
        5: 40,
        10: 70
    }
}

DEFAULT_VIDEO_MODEL = "kwaivgi/kling-video-o1"
DEFAULT_VIDEO_DURATION = 5
DEFAULT_VIDEO_ASPECT_RATIO = "16:9"

VIDEO_POLL_INTERVAL = 20
VIDEO_POLL_MAX_ATTEMPTS = 48

PAY_PLANS = {
    "small": {"label": f"800 {TOKEN_EMOJI}", "amount": 250, "tokens": 800},
    "medium": {"label": f"1800 {TOKEN_EMOJI}", "amount": 400, "tokens": 1800},
    "large": {"label": f"4000 {TOKEN_EMOJI}", "amount": 750, "tokens": 4000}
}


@contextmanager
def db_connection(commit=False):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")

    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=30000")

    # users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            model TEXT NOT NULL DEFAULT 'google/gemini-3-flash-preview',
            free_tokens INTEGER NOT NULL DEFAULT 40,
            paid_tokens INTEGER NOT NULL DEFAULT 0,

            image_mode INTEGER NOT NULL DEFAULT 0,
            image_model TEXT NOT NULL DEFAULT 'google/gemini-3-pro-image-preview',
            image_flow TEXT DEFAULT '',
            pending_image_prompt TEXT DEFAULT '',

            video_mode INTEGER NOT NULL DEFAULT 0,
            video_model TEXT NOT NULL DEFAULT 'kwaivgi/kling-video-o1',
            video_flow TEXT DEFAULT 'prompt_only',
            video_duration INTEGER NOT NULL DEFAULT 5,
            video_aspect_ratio TEXT NOT NULL DEFAULT '16:9',

            last_free_reset_at TEXT DEFAULT NULL
        )
    """)

    conn.commit()

    existing_columns = [row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()]

    if "image_mode" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN image_mode INTEGER NOT NULL DEFAULT 0")
    if "image_model" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN image_model TEXT NOT NULL DEFAULT 'google/gemini-3-pro-image-preview'")
    if "image_flow" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN image_flow TEXT DEFAULT ''")
    if "pending_image_prompt" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN pending_image_prompt TEXT DEFAULT ''")

    if "video_mode" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN video_mode INTEGER NOT NULL DEFAULT 0")
    if "video_model" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN video_model TEXT NOT NULL DEFAULT 'kwaivgi/kling-video-o1'")
    if "video_flow" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN video_flow TEXT DEFAULT 'prompt_only'")
    if "video_duration" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN video_duration INTEGER NOT NULL DEFAULT 5")
    if "video_aspect_ratio" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN video_aspect_ratio TEXT NOT NULL DEFAULT '16:9'")

    if "last_free_reset_at" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN last_free_reset_at TEXT DEFAULT NULL")

    # payments
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT UNIQUE,
            idempotence_key TEXT UNIQUE,
            user_id INTEGER NOT NULL,
            plan_key TEXT NOT NULL,
            amount INTEGER NOT NULL,
            tokens_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # chat_history
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # generation_jobs — ВАЖНО: тоже до conn.close()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS generation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_uuid TEXT UNIQUE,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            kind TEXT NOT NULL,
            model TEXT NOT NULL,
            flow TEXT NOT NULL DEFAULT '',
            prompt_text TEXT DEFAULT '',
            cost INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'created',
            provider_generation_id TEXT DEFAULT '',
            polling_url TEXT DEFAULT '',
            file_path TEXT DEFAULT '',
            error_text TEXT DEFAULT '',
            charged INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_history_user_id_id
        ON chat_history(user_id, id)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_generation_jobs_user_status
        ON generation_jobs(user_id, status)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_payments_status
        ON payments(status)
    """)

    conn.commit()
    conn.close()


def ensure_user(user_id: int):
    with db_connection(commit=True) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO users (
                user_id, model, free_tokens, paid_tokens,
                image_mode, image_model, image_flow, pending_image_prompt,
                video_mode, video_model, video_flow, video_duration, video_aspect_ratio,
                last_free_reset_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            DEFAULT_MODEL,
            FREE_TOKENS,
            0,
            0,
            DEFAULT_IMAGE_MODEL,
            "",
            "",
            0,
            DEFAULT_VIDEO_MODEL,
            "prompt_only",
            DEFAULT_VIDEO_DURATION,
            DEFAULT_VIDEO_ASPECT_RATIO,
            None
        ))
        conn.commit()

    conn.close()

def get_user_data(user_id: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT model,
               free_tokens,
               paid_tokens,
               image_mode,
               image_model,
               image_flow,
               pending_image_prompt,
               video_mode,
               video_model,
               video_flow,
               video_duration,
               video_aspect_ratio,
               last_free_reset_at
        FROM users
        WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()

    model = row[0]
    if model not in TEXT_MODELS:
        set_user_model(user_id, DEFAULT_MODEL)
        model = DEFAULT_MODEL

    image_model = row[4]
    if image_model not in IMAGE_MODELS:
        set_image_model(user_id, DEFAULT_IMAGE_MODEL)
        image_model = DEFAULT_IMAGE_MODEL

    video_model = row[8]
    if video_model not in VIDEO_MODELS:
        set_video_model(user_id, DEFAULT_VIDEO_MODEL)
        video_model = DEFAULT_VIDEO_MODEL

    video_duration = row[10] if row[10] in (5, 10) else DEFAULT_VIDEO_DURATION
    if row[10] != video_duration:
        set_video_duration(user_id, video_duration)

    video_aspect_ratio = row[11] if row[11] in ("16:9", "9:16", "1:1") else DEFAULT_VIDEO_ASPECT_RATIO
    if row[11] != video_aspect_ratio:
        set_video_aspect_ratio(user_id, video_aspect_ratio)

    video_flow = row[9] if row[9] in ("prompt_only", "photo_plus_prompt") else "prompt_only"
    if row[9] != video_flow:
        set_video_flow(user_id, video_flow)

    return {
        "model": model,
        "free_tokens": row[1],
        "paid_tokens": row[2],
        "image_mode": bool(row[3]),
        "image_model": image_model,
        "image_flow": row[5] or "",
        "pending_image_prompt": row[6] or "",
        "video_mode": bool(row[7]),
        "video_model": video_model,
        "video_flow": video_flow,
        "video_duration": video_duration,
        "video_aspect_ratio": video_aspect_ratio,
        "last_free_reset_at": row[12],
    }


def get_total_tokens(user_id: int):
    data = get_user_data(user_id)
    return data["free_tokens"] + data["paid_tokens"]


def balance_line(user_id: int):
    return f"💰 Твой баланс: *{get_total_tokens(user_id)}* {TOKEN_EMOJI}"


def set_user_model(user_id: int, model: str):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET model = ? WHERE user_id = ?", (model, user_id))
    conn.commit()
    conn.close()


def set_image_mode(user_id: int, enabled: bool):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET image_mode = ? WHERE user_id = ?", (1 if enabled else 0, user_id))
    conn.commit()
    conn.close()


def set_image_model(user_id: int, model: str):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET image_model = ? WHERE user_id = ?", (model, user_id))
    conn.commit()
    conn.close()


def set_image_flow(user_id: int, image_flow: str):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET image_flow = ? WHERE user_id = ?", (image_flow, user_id))
    conn.commit()
    conn.close()


def set_pending_image_prompt(user_id: int, prompt: str):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET pending_image_prompt = ? WHERE user_id = ?", (prompt, user_id))
    conn.commit()
    conn.close()


def clear_image_state(user_id: int):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET image_mode = 0,
            image_flow = '',
            pending_image_prompt = ''
        WHERE user_id = ?
    """, (user_id,))
    conn.commit()
    conn.close()

def set_video_mode(user_id: int, enabled: bool):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET video_mode = ? WHERE user_id = ?", (1 if enabled else 0, user_id))
    conn.commit()
    conn.close()


def set_video_model(user_id: int, model: str):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET video_model = ? WHERE user_id = ?", (model, user_id))
    conn.commit()
    conn.close()


def set_video_flow(user_id: int, flow: str):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET video_flow = ? WHERE user_id = ?", (flow, user_id))
    conn.commit()
    conn.close()


def set_video_duration(user_id: int, duration: int):
    ensure_user(user_id)
    if duration not in (5, 10):
        duration = DEFAULT_VIDEO_DURATION

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET video_duration = ? WHERE user_id = ?", (duration, user_id))
    conn.commit()
    conn.close()


def set_video_aspect_ratio(user_id: int, aspect_ratio: str):
    ensure_user(user_id)
    if aspect_ratio not in ("16:9", "9:16", "1:1"):
        aspect_ratio = DEFAULT_VIDEO_ASPECT_RATIO

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET video_aspect_ratio = ? WHERE user_id = ?", (aspect_ratio, user_id))
    conn.commit()
    conn.close()


def clear_video_state(user_id: int):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET video_mode = 0,
            video_model = ?,
            video_flow = '',
            video_duration = ?,
            video_aspect_ratio = ?
        WHERE user_id = ?
    """, (DEFAULT_VIDEO_MODEL, DEFAULT_VIDEO_DURATION, DEFAULT_VIDEO_ASPECT_RATIO, user_id))
    conn.commit()
    conn.close()


def get_last_free_reset_at(user_id: int):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT last_free_reset_at FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()

    if not row or not row[0]:
        return None

    try:
        return datetime.fromisoformat(row[0])
    except Exception:
        return None


def set_last_free_reset_at(user_id: int, dt: datetime):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET last_free_reset_at = ? WHERE user_id = ?",
        (dt.isoformat(), user_id)
    )
    conn.commit()
    conn.close()


def can_reset_free_tokens(user_id: int):
    last_reset = get_last_free_reset_at(user_id)
    if last_reset is None:
        return True, None

    next_reset_at = last_reset + timedelta(days=FREE_RESET_COOLDOWN_DAYS)
    now = datetime.utcnow()

    if now >= next_reset_at:
        return True, None

    return False, next_reset_at - now


def format_timedelta_ru(delta: timedelta):
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = 0

    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days} д.")
    if hours > 0:
        parts.append(f"{hours} ч.")
    if minutes > 0 or not parts:
        parts.append(f"{minutes} мин.")

    return " ".join(parts)


def reset_free_tokens(user_id: int):
    ensure_user(user_id)
    now = datetime.utcnow()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET free_tokens = ?,
            last_free_reset_at = ?
        WHERE user_id = ?
    """, (FREE_TOKENS, now.isoformat(), user_id))
    conn.commit()
    conn.close()


def add_paid_tokens(user_id: int, tokens_count: int):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET paid_tokens = paid_tokens + ? WHERE user_id = ?",
        (tokens_count, user_id)
    )
    conn.commit()
    conn.close()


def add_chat_message(user_id: int, role: str, content: str):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO chat_history (user_id, role, content)
        VALUES (?, ?, ?)
    """, (user_id, role, content))

    conn.commit()
    conn.close()


def get_chat_history(user_id: int, limit: int = CHAT_HISTORY_LIMIT):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT role, content
        FROM chat_history
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))

    rows = cur.fetchall()
    conn.close()

    rows.reverse()

    messages = []
    for role, content in rows:
        if role in ("user", "assistant") and content:
            messages.append({
                "role": role,
                "content": content
            })

    return messages


def clear_chat_history(user_id: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))

    conn.commit()
    conn.close()


def consume_tokens(user_id: int, cost: int):
    ensure_user(user_id)

    if cost <= 0:
        return True, "free", 0

    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")

        cur.execute("""
            SELECT free_tokens, paid_tokens
            FROM users
            WHERE user_id = ?
        """, (user_id,))
        row = cur.fetchone()

        if not row:
            conn.rollback()
            return False, None, cost

        free_tokens, paid_tokens = row
        total = free_tokens + paid_tokens

        if total < cost:
            conn.rollback()
            return False, "limit", cost

        paid_used = min(paid_tokens, cost)
        free_used = cost - paid_used

        cur.execute("""
            UPDATE users
            SET paid_tokens = paid_tokens - ?,
                free_tokens = free_tokens - ?
            WHERE user_id = ?
        """, (paid_used, free_used, user_id))

        conn.commit()

    if paid_used == cost:
        source = "paid"
    elif paid_used > 0:
        source = "mixed"
    else:
        source = "free"

    return True, source, cost


def refund_tokens(user_id: int, amount: int):
    ensure_user(user_id)

    if amount <= 0:
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET paid_tokens = paid_tokens + ? WHERE user_id = ?",
        (amount, user_id)
    )
    conn.commit()
    conn.close()


def create_generation_job(user_id: int, provider: str, kind: str, model: str, flow: str, prompt_text: str, cost: int):
    job_uuid = uuid.uuid4().hex

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO generation_jobs
        (job_uuid, user_id, provider, kind, model, flow, prompt_text, cost, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        job_uuid,
        user_id,
        provider,
        kind,
        model,
        flow or "",
        prompt_text or "",
        cost,
        "created"
    ))
    conn.commit()
    conn.close()
    return job_uuid


def update_generation_job(job_uuid: str, **fields):
    if not fields:
        return

    allowed_fields = {
        "status",
        "provider_generation_id",
        "polling_url",
        "file_path",
        "error_text",
        "charged"
    }

    updates = []
    values = []

    for key, value in fields.items():
        if key in allowed_fields:
            updates.append(f"{key} = ?")
            values.append(value)

    if not updates:
        return

    updates.append("updated_at = CURRENT_TIMESTAMP")
    values.append(job_uuid)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        f"UPDATE generation_jobs SET {', '.join(updates)} WHERE job_uuid = ?",
        values
    )
    conn.commit()
    conn.close()


def create_payment_record(payment_id: str, idempotence_key: str, user_id: int, plan_key: str, amount: int, tokens_count: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO payments
        (payment_id, idempotence_key, user_id, plan_key, amount, tokens_count, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        payment_id,
        idempotence_key,
        user_id,
        plan_key,
        amount,
        tokens_count,
        "pending"
    ))

    conn.commit()
    conn.close()


def get_payment_by_id(payment_id: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT payment_id, user_id, plan_key, amount, tokens_count, status
        FROM payments
        WHERE payment_id = ?
    """, (payment_id,))
    row = cur.fetchone()

    conn.close()

    if not row:
        return None

    return {
        "payment_id": row[0],
        "user_id": row[1],
        "plan_key": row[2],
        "amount": row[3],
        "tokens_count": row[4],
        "status": row[5]
    }


def update_payment_status(payment_id: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE payments SET status = ? WHERE payment_id = ?", (status, payment_id))
    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def require_admin(message):
    if is_admin(message.from_user.id):
        return True

    bot.send_message(message.chat.id, "⛔ У тебя нет доступа к админ-командам.")
    return False


def get_user_balance_info(user_id: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, model, free_tokens, paid_tokens, image_mode, image_model, last_free_reset_at
        FROM users
        WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "user_id": row[0],
        "model": row[1],
        "free_tokens": row[2],
        "paid_tokens": row[3],
        "total_tokens": row[2] + row[3],
        "image_mode": bool(row[4]),
        "image_model": row[5],
        "last_free_reset_at": row[6]
    }


def get_users_list(limit: int = 20):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, model, free_tokens, paid_tokens
        FROM users
        ORDER BY user_id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()

    result = []
    for row in rows:
        result.append({
            "user_id": row[0],
            "model": row[1],
            "free_tokens": row[2],
            "paid_tokens": row[3],
            "total_tokens": row[2] + row[3]
        })
    return result


def admin_add_tokens(user_id: int, amount: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET paid_tokens = paid_tokens + ?
        WHERE user_id = ?
    """, (amount, user_id))
    conn.commit()
    conn.close()


def get_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN_AI)
    kb.row(BTN_NANO, BTN_VIDEO)
    kb.row(BTN_BALANCE, BTN_TOPUP)
    kb.row(BTN_SUPPORT, BTN_RESET)
    return kb


def get_image_mode_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN_NANO)
    kb.row(BTN_BALANCE, BTN_TOPUP)
    kb.row(BTN_EXIT, BTN_RESET)
    return kb

def get_video_mode_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN_VIDEO)
    kb.row(BTN_BALANCE, BTN_TOPUP)
    kb.row(BTN_EXIT, BTN_RESET)
    return kb

def get_current_keyboard(user_id: int):
    data = get_user_data(user_id)

    if data.get("video_mode"):
        return get_video_mode_keyboard()

    if data.get("image_mode"):
        return get_image_mode_keyboard()

    return get_main_keyboard()


def get_models_keyboard():
    kb = types.InlineKeyboardMarkup()
    for model_id, model_name in TEXT_MODELS.items():
        cost = TEXT_MODEL_COSTS.get(model_id, 1)
        kb.add(types.InlineKeyboardButton(
            f"{model_name} — {cost} {TOKEN_EMOJI}",
            callback_data=f"model:{model_id}"
        ))
    return kb


def get_payments_keyboard():
    kb = types.InlineKeyboardMarkup()
    for plan_key, plan in PAY_PLANS.items():
        kb.add(types.InlineKeyboardButton(
            f"{plan['label']} — {plan['amount']} ₽",
            callback_data=f"payplan:{plan_key}"
        ))
    return kb

def getklingvideokeyboard(userid: int):
    data = getuserdata(userid)
    model = data["videomodel"]
    duration = data["videoduration"]
    aspectratio = data["videoaspectratio"]
    cost = getvideocost(model, duration)

    kb = types.InlineKeyboardMarkup()

    kb.row(
        types.InlineKeyboardButton(
            f"{'✅ ' if duration == 5 else ''}5 сек",
            callback_data="videoduration5"
        ),
        types.InlineKeyboardButton(
            f"{'✅ ' if duration == 10 else ''}10 сек",
            callback_data="videoduration10"
        ),
    )

    kb.row(
        types.InlineKeyboardButton(
            f"{'✅ ' if aspectratio == '169' else ''}16:9",
            callback_data="videoaspect169"
        ),
        types.InlineKeyboardButton(
            f"{'✅ ' if aspectratio == '916' else ''}9:16",
            callback_data="videoaspect916"
        ),
        types.InlineKeyboardButton(
            f"{'✅ ' if aspectratio == '11' else ''}1:1",
            callback_data="videoaspect11"
        ),
    )

    kb.add(
        types.InlineKeyboardButton(
            f"ℹ️ Стоимость: {cost} {TOKEN_EMOJI}",
            callback_data="videohelpshow"
        )
    )
    return kb


def format_balance_text(user_id: int):
    return balance_line(user_id)


def safe_edit_message(chat_id, message_id, text, reply_markup=None):
    inline_markup = reply_markup if isinstance(reply_markup, types.InlineKeyboardMarkup) else None

    try:
        bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="Markdown",
            reply_markup=inline_markup
        )
        return
    except Exception as e:
        logger.warning("Не удалось отредактировать Markdown-сообщение: %s", e)

    try:
        bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode=None,
            reply_markup=inline_markup
        )
        return
    except Exception as e:
        logger.warning("Не удалось отредактировать plain-сообщение: %s", e)

    try:
        bot.send_message(
            chat_id,
            text,
            parse_mode=None,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.exception("Не удалось отправить fallback-сообщение: %s", e)


def _extract_retry_after_seconds(error: Exception) -> int | None:
    text = str(error)
    match = re.search(r"retry after (\d+)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def telegram_api_call_with_retry(func, *args, max_attempts=3, base_delay=2, **kwargs):
    last_error = None

    for attempt in range(max_attempts):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_error = e
            retry_after = _extract_retry_after_seconds(e)

            if retry_after:
                sleep_for = retry_after + 1
            else:
                sleep_for = base_delay * (attempt + 1)

            logger.warning(
                "Telegram API ошибка, попытка %s/%s: %s",
                attempt + 1,
                max_attempts,
                e
            )

            if attempt < max_attempts - 1:
                time.sleep(sleep_for)

    raise last_error


def safe_send_photo(chat_id: int, file_path: str, caption: str | None = None, parse_mode: str | None = None):
    with open(file_path, "rb") as f:
        return telegram_api_call_with_retry(
            bot.send_photo,
            chat_id,
            photo=f,
            caption=caption,
            parse_mode=parse_mode
        )


def safe_send_document(chat_id: int, file_path: str, caption: str | None = None, parse_mode: str | None = None):
    with open(file_path, "rb") as f:
        return telegram_api_call_with_retry(
            bot.send_document,
            chat_id,
            document=f,
            caption=caption,
            parse_mode=parse_mode
        )


def safe_send_video(chat_id: int, file_path: str, caption: str | None = None, parse_mode: str | None = None):
    with open(file_path, "rb") as f:
        return telegram_api_call_with_retry(
            bot.send_video,
            chat_id,
            f,
            caption=caption,
            parse_mode=parse_mode
        )


def safe_send_message(chat_id: int, text: str, **kwargs):
    return telegram_api_call_with_retry(
        bot.send_message,
        chat_id,
        text,
        **kwargs
    )


def get_image_cost(model: str, flow: str):
    if flow == "prompt_only":
        return PROMPT_ONLY_COSTS.get(model, 1)
    if flow == "photo_plus_prompt":
        return PHOTO_PROMPT_COSTS.get(model, 1)
    return 1

def get_video_cost(model: str, duration: int):
    model_prices = VIDEO_PROMPT_COSTS.get(model, {})
    return model_prices.get(duration, 40)


def extract_data_url_parts(data_url: str):
    match = re.match(r"^data:(image\/[a-zA-Z0-9.+-]+);base64,(.+)$", data_url, re.DOTALL)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def save_generated_image_from_data_url(data_url: str, prefix: str = "nano"):
    mime_type, b64_data = extract_data_url_parts(data_url)
    if not mime_type or not b64_data:
        return None

    extension = mimetypes.guess_extension(mime_type) or ".png"
    if extension == ".jpe":
        extension = ".jpg"

    filename = f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}{extension}"
    file_path = os.path.join(GENERATED_DIR, filename)

    image_bytes = base64.b64decode(b64_data)
    with open(file_path, "wb") as f:
        f.write(image_bytes)

    return file_path


def telegram_photo_to_data_url(message):
    photo = message.photo[-1]
    file_info = bot.get_file(photo.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    encoded = base64.b64encode(downloaded_file).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"


def send_generated_image_both(chat_id: int, file_path: str, caption_preview: str, caption_file: str):
    safe_send_photo(
        chat_id=chat_id,
        file_path=file_path,
        caption=caption_preview
    )

    safe_send_document(
        chat_id=chat_id,
        file_path=file_path,
        caption=caption_file
    )


def call_openrouter_text(model: str, user_message: str, history=None, max_retries: int = 3):
    url = "https://openrouter.ai/api/v1/chat/completions"
    fallback_model = DEFAULT_MODEL

    messages = [
        {"role": "system", "content": "Отвечай кратко, по делу, на русском языке."}
    ]

    if history:
        messages.extend(history)

    messages.append({"role": "user", "content": user_message})

    data = {
        "model": model,
        "messages": messages,
        "max_tokens": 500,
        "temperature": 0.7
    }

    for attempt in range(max_retries):
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json"
                },
                json=data,
                timeout=90
            )

            if resp.status_code == 200:
                payload = resp.json()
                return payload["choices"][0]["message"]["content"]

            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue

            logger.error("OpenRouter text error %s: %s", resp.status_code, resp.text)

        except Exception as e:
            logger.exception("Ошибка text-запроса к OpenRouter: %s", e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

    if model != fallback_model:
        return call_openrouter_text(fallback_model, user_message, history=history, max_retries=1) + " ⚡"

    return "⚠️ Не удалось получить ответ от AI. Попробуй ещё раз позже."


def generate_image_openrouter(model: str, prompt_text: str, input_image_data_url: str | None = None, max_retries: int = 2):
    url = "https://openrouter.ai/api/v1/chat/completions"

    content_parts = [{"type": "text", "text": prompt_text.strip()}]
    if input_image_data_url:
        content_parts.append({"type": "image_url", "image_url": {"url": input_image_data_url}})

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content_parts}],
        "modalities": ["image", "text"]
    }

    for attempt in range(max_retries):
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=180
            )

            if resp.status_code == 200:
                result = resp.json()
                choices = result.get("choices", [])
                if not choices:
                    logger.error("OpenRouter: choices отсутствуют: %s", result)
                    continue

                message = choices[0].get("message", {})
                images = message.get("images", [])

                if images:
                    first_image = images[0]
                    image_url_obj = first_image.get("image_url", {})
                    data_url = image_url_obj.get("url")
                    if data_url and data_url.startswith("data:image/"):
                        content_text = message.get("content", "") or "Изображение сгенерировано."
                        return {"ok": True, "image_data_url": data_url, "text": content_text}

                logger.error("OpenRouter не вернул images: %s", result)

            elif resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            else:
                logger.error("OpenRouter image generation error %s: %s", resp.status_code, resp.text)

        except Exception as e:
            logger.exception("Ошибка генерации изображения через OpenRouter: %s", e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

    return {"ok": False, "error": "⚠️ Модель не вернула изображение. Попробуй другой промт или другую модель."}

def submit_openrouter_video_generation(
    model: str,
    prompt: str,
    duration: int = 5,
    aspect_ratio: str = "16:9",
    input_image_data_url: str | None = None
):
    url = "https://openrouter.ai/api/v1/videos"

    payload = {
        "model": model,
        "prompt": prompt,
        "duration": duration,
        "aspect_ratio": aspect_ratio
    }

    if input_image_data_url:
        payload["frame_images"] = [
            {
                "type": "image_url",
                "image_url": {"url": input_image_data_url},
                "frame_type": "first_frame"
            }
        ]

    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=60
        )

        if resp.status_code not in (200, 202):
            logger.error("OpenRouter video submit error %s: %s", resp.status_code, resp.text)
            return {"ok": False, "error": f"Не удалось поставить видео в очередь: {resp.text[:500]}"}

        data = resp.json()
        return {
            "ok": True,
            "id": data.get("id"),
            "polling_url": data.get("polling_url"),
            "status": data.get("status", "pending")
        }

    except Exception as e:
        logger.exception("Ошибка submit video generation: %s", e)
        return {"ok": False, "error": "Ошибка при запуске генерации видео."}

def poll_openrouter_video(polling_url: str):
    try:
        resp = requests.get(
            polling_url,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}"
            },
            timeout=60
        )

        if resp.status_code != 200:
            logger.error("OpenRouter video poll error %s: %s", resp.status_code, resp.text)
            return {"ok": False, "error": "Ошибка проверки статуса видео."}

        data = resp.json()
        return {
            "ok": True,
            "status": data.get("status"),
            "unsigned_urls": data.get("unsigned_urls", []),
            "error_message": data.get("error"),
            "raw": data
        }

    except Exception as e:
        logger.exception("Ошибка polling video generation: %s", e)
        return {"ok": False, "error": "Ошибка polling статуса видео."}

def download_openrouter_video_content(generation_id: str, index: int = 0, prefix: str = "kling"):
    filename = f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
    file_path = os.path.join(GENERATED_VIDEOS_DIR, filename)
    url = f"https://openrouter.ai/api/v1/videos/{generation_id}/content?index={index}"

    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}"
            },
            timeout=300,
            stream=True
        )

        if resp.status_code != 200:
            logger.error("OpenRouter content download error %s: %s", resp.status_code, resp.text[:500])
            return None

        with open(file_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        return file_path

    except Exception as e:
        logger.exception("Ошибка скачивания видео через content endpoint: %s", e)
        return None

def download_video_file(video_url: str, prefix: str = "kling"):
    filename = f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
    file_path = os.path.join(GENERATED_VIDEOS_DIR, filename)

    try:
        resp = requests.get(video_url, timeout=300)
        if resp.status_code != 200:
            logger.error("Video download error %s: %s", resp.status_code, resp.text[:500])
            return None

        with open(file_path, "wb") as f:
            f.write(resp.content)

        return file_path

    except Exception as e:
        logger.exception("Ошибка скачивания видео: %s", e)
        return None




def create_yookassa_payment(user_id: int, plan_key: str):
    if not YOOKASSA_ENABLED:
        logger.warning("YooKassa не настроена")
        return None, None

    if plan_key not in PAY_PLANS:
        return None, None

    plan = PAY_PLANS[plan_key]
    idempotence_key = str(uuid.uuid4())

    payload = {
        "amount": {"value": f"{plan['amount']}.00", "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": YOOKASSA_RETURN_URL
        },
        "description": f"Пополнение баланса AI-бота: {plan['label']}",
        "metadata": {
            "user_id": str(user_id),
            "plan_key": plan_key,
            "tokens_count": str(plan["tokens"])
        }
    }

    headers = {
        "Content-Type": "application/json",
        "Idempotence-Key": idempotence_key
    }

    try:
        resp = requests.post(
            "https://api.yookassa.ru/v3/payments",
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            headers=headers,
            json=payload,
            timeout=30
        )

        if resp.status_code not in (200, 201):
            logger.error("YooKassa error %s: %s", resp.status_code, resp.text)
            return None, None

        data = resp.json()
        payment_id = data["id"]
        confirmation_url = data["confirmation"]["confirmation_url"]

        create_payment_record(
            payment_id=payment_id,
            idempotence_key=idempotence_key,
            user_id=user_id,
            plan_key=plan_key,
            amount=plan["amount"],
            tokens_count=plan["tokens"]
        )

        logger.info("Создан платеж payment_id=%s user_id=%s plan=%s", payment_id, user_id, plan_key)
        return payment_id, confirmation_url

    except Exception as e:
        logger.exception("Ошибка создания платежа YooKassa: %s", e)
        return None, None



def apply_payment_if_needed(payment_id: str):
    payment = get_payment_by_id(payment_id)
    if not payment:
        logger.warning("Платеж %s не найден в БД", payment_id)
        return False

    if payment["status"] == "succeeded":
        logger.info("Платеж %s уже обработан", payment_id)
        return True

    add_paid_tokens(payment["user_id"], payment["tokens_count"])
    update_payment_status(payment_id, "succeeded")

    total_tokens = get_total_tokens(payment["user_id"])

    try:
        bot.send_message(
            payment["user_id"],
            f"✅ Оплата прошла успешно!\n\n"
            f"Пакет: *{PAY_PLANS[payment['plan_key']]['label']}*\n"
            f"Начислено: *{payment['tokens_count']}* {TOKEN_EMOJI}\n"
            f"💰 Твой баланс: *{total_tokens}* {TOKEN_EMOJI}",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logger.warning("Не удалось отправить сообщение об оплате: %s", e)

    logger.info("Платеж %s успешно применен", payment_id)
    return True


def process_text_question(message):
    user_id = message.from_user.id
    user_text = (message.text or "").strip()

    if not user_text:
        bot.send_message(
            message.chat.id,
            "Напиши текстовый вопрос.",
            reply_markup=get_main_keyboard()
        )
        return

    user_data = get_user_data(user_id)
    model = user_data["model"]

    if model not in TEXT_MODELS:
        model = DEFAULT_MODEL
        set_user_model(user_id, model)
        user_data = get_user_data(user_id)

    model_name = TEXT_MODELS.get(model, model)
    model_cost = TEXT_MODEL_COSTS.get(model, 1)
    total_tokens = get_total_tokens(user_id)

    if total_tokens < model_cost:
        bot.send_message(
            message.chat.id,
            f"❌ Недостаточно токенов для модели *{model_name}*.\n\n"
            f"Стоимость запроса: *{model_cost}* {TOKEN_EMOJI}\n"
            f"💰 Твой баланс: *{total_tokens}* {TOKEN_EMOJI}",
            reply_markup=get_main_keyboard()
        )
        return

    msg = bot.send_message(message.chat.id, "🤖 Думаю... ⏳")
    bot.send_chat_action(message.chat.id, "typing")

    history = get_chat_history(user_id, limit=CHAT_HISTORY_LIMIT)
    answer = call_openrouter_text(model, user_text, history=history)

    success, source, charged = consume_tokens(user_id, model_cost)
    if not success:
        safe_edit_message(
            message.chat.id,
            msg.message_id,
            f"❌ Не удалось списать токены. Попробуй ещё раз.\n\n{balance_line(user_id)}",
            reply_markup=None
        )
        bot.send_message(
            message.chat.id,
            "Выбери действие:",
            reply_markup=get_main_keyboard()
        )
        return

    add_chat_message(user_id, "user", user_text)
    add_chat_message(user_id, "assistant", answer)

    total_left = get_total_tokens(user_id)

    safe_edit_message(
        message.chat.id,
        msg.message_id,
        f"🤖 *{model_name}*\n\n"
        f"{answer}\n\n"
        f"💸 Списано: *{charged}* {TOKEN_EMOJI}\n"
        f"💰 Твой баланс: *{total_left}* {TOKEN_EMOJI}",
        reply_markup=None
    )

    bot.send_message(
        message.chat.id,
        "Можешь продолжать диалог — бот помнит последние сообщения.",
        reply_markup=get_main_keyboard()
    )


def processnanomessage(message):
    userid = message.from_user.id
    data = getuserdata(userid)

    model = data["imagemodel"]
    modelname = IMAGEMODELS.get(model, model)

    has_photo = bool(getattr(message, "photo", None))
    prompttext = (message.caption if has_photo else (message.text or "")).strip()

    if not prompttext:
        bot.send_message(
            message.chat.id,
            "Нужен промт. Отправь текст или фото с подписью.",
            reply_markup=getimagemodekeyboard()
        )
        return

    flow = "photoplusprompt" if has_photo else "promptonly"
    cost = getimagecost(model, flow)
    totaltokens = gettotaltokens(userid)

    if totaltokens < cost:
        bot.send_message(
            message.chat.id,
            f"{modelname}.\nНужно: {cost} {TOKEN_EMOJI}\nУ тебя: {totaltokens} {TOKEN_EMOJI}",
            reply_markup=getimagemodekeyboard()
        )
        return

    jobuuid = creategenerationjob(
        userid=userid,
        provider="openrouter",
        kind="image",
        model=model,
        flow=flow,
        prompttext=prompttext,
        cost=cost
    )

    waitmsg = bot.send_message(
        message.chat.id,
        f"{modelname} генерирует изображение..."
    )

    inputimagedataurl = None
    if has_photo:
        try:
            inputimagedataurl = telegramphototodataurl(message)
        except Exception as e:
            logger.exception("Telegram photo error: %s", e)
            updategenerationjob(jobuuid, {
                "status": "failed",
                "errortext": f"telegramphotofailed: {e}"
            })
            safeeditmessage(
                message.chat.id,
                waitmsg.message_id,
                "Не удалось обработать фото из Telegram.",
                replymarkup=None
            )
            bot.send_message(
                message.chat.id,
                "Попробуй отправить фото ещё раз.",
                reply_markup=getimagemodekeyboard()
            )
            return

    result = generateimageopenrouter(
        model=model,
        prompttext=prompttext,
        inputimagedataurl=inputimagedataurl
    )

    if not result["ok"]:
        updategenerationjob(jobuuid, {
            "status": "failed",
            "errortext": result["error"]
        })
        safeeditmessage(
            message.chat.id,
            waitmsg.message_id,
            result["error"],
            replymarkup=None
        )
        bot.send_message(
            message.chat.id,
            "Генерация не удалась.",
            reply_markup=getimagemodekeyboard()
        )
        return

    filepath = savegeneratedimagefromdataurl(
        result["imagedataurl"],
        prefix="photo" if has_photo else "prompt"
    )

    if not filepath:
        updategenerationjob(jobuuid, {
            "status": "failed",
            "errortext": "savefailed"
        })
        safeeditmessage(
            message.chat.id,
            waitmsg.message_id,
            "Не удалось сохранить изображение.",
            replymarkup=None
        )
        bot.send_message(
            message.chat.id,
            "Ошибка сохранения результата.",
            reply_markup=getimagemodekeyboard()
        )
        return

    updategenerationjob(jobuuid, {
        "status": "completed",
        "filepath": filepath
    })

    try:
        sendgeneratedimageboth(
            chatid=message.chat.id,
            filepath=filepath,
            captionpreview=f"{modelname}",
            captionfile=""
        )
    except Exception as e:
        logger.exception("Telegram send failed: %s", e)
        updategenerationjob(jobuuid, {
            "status": "failed",
            "errortext": f"telegramsendfailed: {e}"
        })
        safeeditmessage(
            message.chat.id,
            waitmsg.message_id,
            "Ошибка отправки результата в Telegram.",
            replymarkup=None
        )
        bot.send_message(
            message.chat.id,
            "Результат не удалось отправить.",
            reply_markup=getimagemodekeyboard()
        )
        return

    success, source, charged = consumetokens(userid, cost)
    if not success:
        updategenerationjob(jobuuid, {
            "status": "failed",
            "errortext": "chargefailed"
        })
        safeeditmessage(
            message.chat.id,
            waitmsg.message_id,
            f"Не удалось списать токены.\n{balanceline(userid)}",
            replymarkup=None
        )
        bot.send_message(
            message.chat.id,
            "Недостаточно токенов.",
            reply_markup=getimagemodekeyboard()
        )
        return

    updategenerationjob(jobuuid, {
        "status": "delivered",
        "charged": 1
    })

    totalleft = gettotaltokens(userid)
    safeeditmessage(
        message.chat.id,
        waitmsg.message_id,
        f"Готово.\nСписано: {charged} {TOKEN_EMOJI}\nОсталось: {totalleft} {TOKEN_EMOJI}",
        replymarkup=None
    )

    bot.send_message(
        message.chat.id,
        "Отправь следующий промт или фото с подписью.",
        reply_markup=getimagemodekeyboard()
    )


def processklingmessage(message):
    userid = message.from_user.id
    userdata = getuserdata(userid)

    model = userdata["videomodel"]
    modelname = VIDEOMODELS.get(model, model)
    duration = userdata["videoduration"]
    aspectratio = userdata["videoaspectratio"]

    has_photo = bool(getattr(message, "photo", None))
    prompttext = (message.caption if has_photo else (message.text or "")).strip()

    if not prompttext:
        bot.send_message(
            message.chat.id,
            "Нужен промт. Отправь текст или фото с подписью.",
            reply_markup=getvideomodekeyboard()
        )
        return

    flow = "photoplusprompt" if has_photo else "promptonly"
    cost = getvideocost(model, duration)
    totaltokens = gettotaltokens(userid)

    if totaltokens < cost:
        bot.send_message(
            message.chat.id,
            f"{modelname}.\nНужно: {cost} {TOKEN_EMOJI}\nУ тебя: {totaltokens} {TOKEN_EMOJI}",
            parse_mode="Markdown",
            reply_markup=getvideomodekeyboard()
        )
        return

    jobuuid = creategenerationjob(
        userid=userid,
        provider="openrouter",
        kind="video",
        model=model,
        flow=flow,
        prompttext=prompttext,
        cost=cost
    )

    waitmsg = bot.send_message(
        message.chat.id,
        (
            f"{modelname} генерирует видео...\n"
            f"Длительность: {duration} сек\n"
            f"Формат: {aspectratio}\n"
            f"Обычно это занимает до 15 минут."
        ),
        parse_mode="Markdown"
    )

    inputimagedataurl = None
    if has_photo:
        try:
            inputimagedataurl = telegramphototodataurl(message)
        except Exception as e:
            logger.exception("Kling Telegram photo error: %s", e)
            updategenerationjob(jobuuid, {
                "status": "failed",
                "errortext": f"telegramphotofailed: {e}"
            })
            safeeditmessage(
                message.chat.id,
                waitmsg.message_id,
                "Не удалось обработать фото из Telegram.",
                replymarkup=None
            )
            bot.send_message(
                message.chat.id,
                "Попробуй отправить фото ещё раз.",
                reply_markup=getvideomodekeyboard()
            )
            return

    submitresult = submitopenroutervideogeneration(
        model=model,
        prompt=prompttext,
        duration=duration,
        aspectratio=aspectratio,
        inputimagedataurl=inputimagedataurl
    )

    if not submitresult["ok"]:
        updategenerationjob(jobuuid, {
            "status": "failed",
            "errortext": submitresult["error"]
        })
        safeeditmessage(
            message.chat.id,
            waitmsg.message_id,
            f"Ошибка запуска генерации:\n{submitresult['error']}",
            replymarkup=None
        )
        bot.send_message(
            message.chat.id,
            "Не удалось запустить генерацию.",
            reply_markup=getvideomodekeyboard()
        )
        return

    generationid = submitresult.get("id")
    pollingurl = submitresult.get("pollingurl")

    updategenerationjob(jobuuid, {
        "status": "submitted",
        "providergenerationid": generationid or "",
        "pollingurl": pollingurl or ""
    })

    safeeditmessage(
        message.chat.id,
        waitmsg.message_id,
        (
            f"Генерация запущена.\n"
            f"Длительность: {duration} сек\n"
            f"Формат: {aspectratio}\n"
            f"Ожидаю результат..."
        ),
        replymarkup=None
    )

    if not generationid or not pollingurl:
        updategenerationjob(jobuuid, {
            "status": "failed",
            "errortext": "missinggenerationidorpollingurl"
        })
        bot.send_message(
            message.chat.id,
            "OpenRouter не вернул generation id или polling url.",
            reply_markup=getvideomodekeyboard()
        )
        return

    for _ in range(VIDEOPOLLMAXATTEMPTS):
        time.sleep(VIDEOPOLLINTERVAL)
        pollresult = pollopenroutervideo(pollingurl)

        if not pollresult["ok"]:
            continue

        status = pollresult.get("status")
        updategenerationjob(jobuuid, {"status": status or "polling"})

        if status in ("pending", "in_progress", "inprogress"):
            continue

        if status == "failed":
            updategenerationjob(jobuuid, {
                "status": "failed",
                "errortext": pollresult.get("errormessage") or "videogenerationfailed"
            })
            bot.send_message(
                message.chat.id,
                f"Генерация завершилась ошибкой.\n{pollresult.get('errormessage') or ''}",
                reply_markup=getvideomodekeyboard()
            )
            return

        if status == "completed":
            filepath = downloadopenroutervideocontent(
                generationid,
                index=0,
                prefix="klingphoto" if has_photo else "klingtext"
            )

            if not filepath:
                updategenerationjob(jobuuid, {
                    "status": "failed",
                    "errortext": "downloadfailed"
                })
                bot.send_message(
                    message.chat.id,
                    "Не удалось скачать видео.",
                    reply_markup=getvideomodekeyboard()
                )
                return

            updategenerationjob(jobuuid, {
                "status": "completed",
                "filepath": filepath
            })

            try:
                safesendvideo(
                    message.chat.id,
                    filepath,
                    caption=f"{modelname}",
                    parsemode="Markdown"
                )
                safesenddocument(
                    message.chat.id,
                    filepath,
                    caption=""
                )
            except Exception as e:
                logger.exception("Kling Telegram send failed: %s", e)
                updategenerationjob(jobuuid, {
                    "status": "failed",
                    "errortext": f"telegramsendfailed: {e}"
                })
                bot.send_message(
                    message.chat.id,
                    "Ошибка отправки результата в Telegram.",
                    reply_markup=getvideomodekeyboard()
                )
                return

            success, source, charged = consumetokens(userid, cost)
            if not success:
                updategenerationjob(jobuuid, {
                    "status": "failed",
                    "errortext": "chargefailed"
                })
                bot.send_message(
                    message.chat.id,
                    f"Не удалось списать токены.\n{balanceline(userid)}",
                    reply_markup=getvideomodekeyboard()
                )
                return

            updategenerationjob(jobuuid, {
                "status": "delivered",
                "charged": 1
            })

            bot.send_message(
                message.chat.id,
                f"Готово.\nСписано: {charged} {TOKEN_EMOJI}\n{balanceline(userid)}",
                parse_mode="Markdown",
                reply_markup=getvideomodekeyboard()
            )
            return

    updategenerationjob(jobuuid, {
        "status": "timeout",
        "errortext": "polltimeout"
    })
    bot.send_message(
        message.chat.id,
        "Время ожидания результата истекло. Попробуй позже.",
        reply_markup=getvideomodekeyboard()
    )


@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_id = message.from_user.id
    ensure_user(user_id)
    clear_chat_history(user_id)
    clear_image_state(user_id)
    clear_video_state(user_id)

    bot.send_message(
        message.chat.id,
        "Я Patriot AI 🦸🏼‍♂️\n\n"
        "Нажми 🧠 GPT/Gemini/Claude, чтобы выбрать модель и задать вопрос\n"
        "Нажми 🍌 Nano Banana, чтобы сгенерировать или отредактировать изображение\n"
        "Нажми 🎬 Kling, чтобы сгенерировать видео",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(commands=["restart"])
def cmd_restart(message):
    user_id = message.from_user.id
    allowed, wait_delta = can_reset_free_tokens(user_id)

    if not allowed:
        bot.send_message(
            message.chat.id,
            f"⏳ Бесплатные токены можно сбрасывать только раз в *{FREE_RESET_COOLDOWN_DAYS} дня*.\n\n"
            f"Попробуй снова через: *{format_timedelta_ru(wait_delta)}*",
            reply_markup=get_main_keyboard()
        )
        return

    reset_free_tokens(user_id)
    bot.send_message(
        message.chat.id,
        f"🔄 Бесплатные токены восстановлены.\n\n{balance_line(user_id)}",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(commands=["newchat"])
def cmd_newchat(message):
    clear_chat_history(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "🧹 История диалога очищена.\nТеперь начинаем новый разговор с чистого контекста.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    bot.send_message(
        message.chat.id,
        f"Твой Telegram ID: `{message.from_user.id}`",
        parse_mode="Markdown"
    )


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not require_admin(message):
        return

    bot.send_message(
        message.chat.id,
        "🛠 *Админ-режим*\n\n"
        "Доступные команды:\n"
        "/admin — показать это меню\n"
        "/myid — показать твой Telegram ID\n"
        "/users — последние пользователи\n"
        "/user USER_ID — посмотреть баланс пользователя\n"
        "/addtokens USER_ID AMOUNT — начислить токены\n\n"
        "Примеры:\n"
        "`/user 123456789`\n"
        "`/addtokens 123456789 500`",
        parse_mode="Markdown"
    )


@bot.message_handler(commands=["user"])
def cmd_user_info(message):
    if not require_admin(message):
        return

    parts = message.text.strip().split()

    if len(parts) != 2 or not parts[1].isdigit():
        bot.send_message(
            message.chat.id,
            "Использование: `/user USER_ID`",
            parse_mode="Markdown"
        )
        return

    target_user_id = int(parts[1])
    info = get_user_balance_info(target_user_id)

    if not info:
        bot.send_message(message.chat.id, "Пользователь не найден.")
        return

    model_name = TEXT_MODELS.get(info["model"], info["model"])
    image_model_name = IMAGE_MODELS.get(info["image_model"], info["image_model"])

    last_reset_text = info["last_free_reset_at"] if info["last_free_reset_at"] else "никогда"

    bot.send_message(
        message.chat.id,
        f"👤 *Пользователь:* `{info['user_id']}`\n"
        f"🧠 Модель: *{model_name}*\n"
        f"🍌 Image model: *{image_model_name}*\n"
        f"🎁 Бесплатные: *{info['free_tokens']}* {TOKEN_EMOJI}\n"
        f"💳 Платные: *{info['paid_tokens']}* {TOKEN_EMOJI}\n"
        f"💰 Текущий баланс: *{info['total_tokens']}* {TOKEN_EMOJI}\n"
        f"🖼 Режим изображений: *{'вкл' if info['image_mode'] else 'выкл'}*\n"
        f"🔄 Последний сброс free-токенов: `{last_reset_text}`",
        parse_mode="Markdown"
    )


@bot.message_handler(commands=["users"])
def cmd_users(message):
    if not require_admin(message):
        return

    users = get_users_list(limit=20)

    if not users:
        bot.send_message(message.chat.id, "Пользователей пока нет.")
        return

    lines = ["🧾 *Последние пользователи:*\n"]

    for user in users:
        model_name = TEXT_MODELS.get(user["model"], user["model"])
        lines.append(
            f"`{user['user_id']}` — *{user['total_tokens']}* {TOKEN_EMOJI} "
            f"(free: {user['free_tokens']}, paid: {user['paid_tokens']}) — {model_name}"
        )

    bot.send_message(
        message.chat.id,
        "\n".join(lines),
        parse_mode="Markdown"
    )


@bot.message_handler(commands=["addtokens"])
def cmd_addtokens(message):
    if not require_admin(message):
        return

    parts = message.text.strip().split()

    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        bot.send_message(
            message.chat.id,
            "Использование: `/addtokens USER_ID AMOUNT`",
            parse_mode="Markdown"
        )
        return

    target_user_id = int(parts[1])
    amount = int(parts[2])

    if amount <= 0:
        bot.send_message(message.chat.id, "Количество токенов должно быть больше 0.")
        return

    admin_add_tokens(target_user_id, amount)
    total_tokens = get_total_tokens(target_user_id)

    bot.send_message(
        message.chat.id,
        f"✅ Пользователю `{target_user_id}` начислено *{amount}* {TOKEN_EMOJI}\n"
        f"💰 Новый баланс: *{total_tokens}* {TOKEN_EMOJI}",
        parse_mode="Markdown"
    )

    try:
        bot.send_message(
            target_user_id,
            f"🎁 Администратор начислил тебе *{amount}* {TOKEN_EMOJI}\n"
            f"💰 Твой баланс: *{total_tokens}* {TOKEN_EMOJI}",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logger.warning("Не удалось уведомить пользователя %s: %s", target_user_id, e)


@bot.message_handler(func=lambda m: m.text == BTN_RESET)
def btn_restart(message):
    user_id = message.from_user.id
    current_keyboard = get_current_keyboard(user_id)

    allowed, wait_delta = can_reset_free_tokens(user_id)

    if not allowed:
        wait_text = format_timedelta_ru(wait_delta)
        bot.send_message(
            message.chat.id,
            f"⏳ Бесплатные токены можно сбрасывать только раз в *{FREE_RESET_COOLDOWN_DAYS} дня*.\n\n"
            f"Попробуй снова через: *{wait_text}*",
            reply_markup=current_keyboard
        )
        return

    reset_free_tokens(user_id)

    bot.send_message(
        message.chat.id,
        f"🔄 Бесплатные токены восстановлены.\n\n"
        f"{balance_line(user_id)}",
        reply_markup=current_keyboard
    )


@bot.message_handler(func=lambda m: m.text == BTN_BALANCE)
def btn_balance(message):
    current_keyboard = get_current_keyboard(message.from_user.id)

    bot.send_message(
        message.chat.id,
        format_balance_text(message.from_user.id),
        reply_markup=current_keyboard
    )


@bot.message_handler(func=lambda m: m.text == BTN_AI)
def btn_text_models(message):
    user_id = message.from_user.id

    clear_image_state(user_id)
    clear_video_state(user_id)

    data = get_user_data(user_id)
    current_name = TEXT_MODELS.get(data["model"], data["model"])
    current_cost = TEXT_MODEL_COSTS.get(data["model"], 1)

    bot.send_message(
        message.chat.id,
        f"🧠 *Текстовый режим*\n\n"
        f"Текущая модель: *{current_name}*\n"
        f"Стоимость запроса: *{current_cost}* {TOKEN_EMOJI}\n"
        f"{balance_line(user_id)}\n\n"
        f"Выбери модель ниже, а потом просто напиши вопрос одним сообщением.",
        reply_markup=get_models_keyboard()
    )

    bot.send_message(
        message.chat.id,
        "После выбора модели просто отправь сообщение с вопросом.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == BTNNANO)
def btnnanobananamessage(message):
    userid = message.from_user.id
    clearvideostate(userid)
    setimagemode(userid, True)
    setimagemodel(userid, DEFAULTIMAGEMODEL)
    setpendingimageprompt(userid, "")

    bot.send_message(
        message.chat.id,
        (
            "📷 *Nano Banana*\n\n"
            "Отправь:\n"
            "- текстовый промт, или\n"
            "- фото с подписью.\n\n"
        ),
        parse_mode="Markdown",
        reply_markup=getimagemodekeyboard()
    )


@bot.message_handler(func=lambda m: m.text == BTN_EXIT)
def btn_exit_mode(message):
    user_id = message.from_user.id
    clear_image_state(user_id)
    clear_video_state(user_id)

    bot.send_message(
        message.chat.id,
        "✅ Режим изображений/видео выключен.\nТеперь бот снова работает как обычный чат.",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(func=lambda m: m.text == BTNVIDEO)
def btnklingvideomessage(message):
    userid = message.from_user.id
    clearimagestate(userid)
    setvideomode(userid, True)
    setvideomodel(userid, DEFAULTVIDEOMODEL)
    setvideoduration(userid, DEFAULTVIDEODURATION)
    setvideoaspectratio(userid, DEFAULTVIDEOASPECTRATIO)

    modelname = VIDEOMODELS[DEFAULTVIDEOMODEL]
    cost = getvideocost(DEFAULTVIDEOMODEL, DEFAULTVIDEODURATION)

    bot.send_message(
        message.chat.id,
        (
            f"🎬 *Kling Video*\n\n"
            f"Модель: {modelname}\n"
            f"Длительность: {DEFAULTVIDEОДURATION} сек\n"
            f"Формат: {DEFAULTVIDEOASPECTRATIO}\n"
            f"Стоимость: {cost} {TOKEN_EMOJI}\n"
            f"Баланс: {balanceline(userid)}\n\n"
            f"Отправь:\n"
            f"- текстовый промт, или\n"
            f"- фото с подписью.\n\n"
            f"Я сам определю режим генерации."
        ),
        parse_mode="Markdown",
        reply_markup=getklingvideokeyboard(userid)
    )

    bot.send_message(
        message.chat.id,
        "⚙️ Настройки видео:",
        reply_markup=get_kling_video_keyboard(user_id)
    )

@bot.message_handler(func=lambda m: m.text == BTN_TOPUP)
def btn_payments(message):
    user_id = message.from_user.id
    current_keyboard = get_current_keyboard(user_id)

    bot.send_message(
        message.chat.id,
        f"{balance_line(user_id)}\n\n"
        f"Выбери пакет пополнения:",
        reply_markup=get_payments_keyboard()
    )

    bot.send_message(
        message.chat.id,
        "После выбора пакета откроется ссылка на оплату.\n"
        "После успешной оплаты токены будут автоматически начислены.",
        reply_markup=current_keyboard
    )


@bot.message_handler(func=lambda m: m.text == BTN_SUPPORT)
def btn_support(message):
    current_keyboard = get_current_keyboard(message.from_user.id)

    support_text = (
        "🛟 <b>Поддержка</b>\n\n"
        "Если возникли вопросы, проблемы со списанием токенов или генерацией,напишите в аккаунт поддержки:\n"
        '<a href="https://t.me/ai_patriot_support">@ai_patriot_support</a>\n\n'
    )

    inline_kb = types.InlineKeyboardMarkup()
    inline_kb.add(
        types.InlineKeyboardButton(
            "Перейти в поддержку",
            url="https://t.me/ai_patriot_support"
        )
    )

    bot.send_message(
        message.chat.id,
        support_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=inline_kb
    )

    bot.send_message(
        message.chat.id,
        "Если хочешь, можешь вернуться к работе с ботом:",
        reply_markup=current_keyboard
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("model:"))
def callback_model(call):
    model_id = call.data.split("model:", 1)[1]
    user_id = call.from_user.id

    if model_id not in TEXT_MODELS:
        bot.answer_callback_query(call.id, "Неизвестная модель")
        return

    set_user_model(user_id, model_id)
    clear_chat_history(user_id)

    model_name = TEXT_MODELS[model_id]
    cost = TEXT_MODEL_COSTS.get(model_id, 1)

    bot.answer_callback_query(call.id, f"Модель установлена: {model_name}")

    safe_edit_message(
        call.message.chat.id,
        call.message.message_id,
        f"🧠 *Текстовый режим*\n\n"
        f"Текущая модель: *{model_name}*\n"
        f"Стоимость запроса: *{cost}* {TOKEN_EMOJI}\n"
        f"{balance_line(user_id)}\n\n"
        f"История диалога очищена для новой модели.\nТеперь просто отправь сообщение с вопросом.",
        reply_markup=None
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("payplan:"))
def callback_payplan(call):
    plan_key = call.data.split("payplan:", 1)[1]
    user_id = call.from_user.id

    if plan_key not in PAY_PLANS:
        bot.answer_callback_query(call.id, "Неизвестный пакет")
        return

    bot.answer_callback_query(call.id, "Создаю ссылку на оплату...")

    payment_id, confirmation_url = create_yookassa_payment(user_id, plan_key)

    if not payment_id or not confirmation_url:
        safe_edit_message(
            call.message.chat.id,
            call.message.message_id,
            "❌ Не удалось создать ссылку на оплату. Попробуй позже.",
            reply_markup=None
        )
        return

    safe_edit_message(
        call.message.chat.id,
        call.message.message_id,
        f"💳 Пакет: *{PAY_PLANS[plan_key]['label']}*\n"
        f"Сумма: *{PAY_PLANS[plan_key]['amount']} ₽*\n\n"
        f"Нажми на кнопку ниже, чтобы перейти к оплате.",
        reply_markup=None
    )

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💳 Оплатить", url=confirmation_url))
    bot.send_message(
        call.message.chat.id,
        "Ссылка на оплату:",
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("videoduration"))
def callbackvideoduration(call):
    value = call.data.split("videoduration", 1)[1]
    userid = call.from_user.id

    try:
        duration = int(value)
    except ValueError:
        bot.answer_callback_query(call.id, "Некорректная длительность")
        return

    if duration not in (5, 10):
        bot.answer_callback_query(call.id, "Доступно только 5 или 10 секунд")
        return

    setvideoduration(userid, duration)
    data = getuserdata(userid)
    bot.answer_callback_query(call.id, f"Длительность: {duration} сек")

    safeeditmessage(
        call.message.chat.id,
        call.message.message_id,
        (
            f"🎬 Kling Video\n\n"
            f"Длительность: {data['videoduration']} сек\n"
            f"Формат: {data['videoaspectratio']}\n"
            f"Стоимость: {getvideocost(data['videomodel'], data['videoduration'])} {TOKEN_EMOJI}\n"
            f"Баланс: {balanceline(userid)}\n\n"
            f"Отправь текстовый промт или фото с подписью."
        ),
        replymarkup=getklingvideokeyboard(userid)
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("videoaspect"))
def callbackvideoaspect(call):
    aspectratio = call.data.split("videoaspect", 1)[1]
    userid = call.from_user.id

    if aspectratio not in ("169", "916", "11"):
        bot.answer_callback_query(call.id, "Некорректный формат")
        return

    setvideoaspectratio(userid, aspectratio)
    data = getuserdata(userid)
    bot.answer_callback_query(call.id, f"Формат: {aspectratio}")

    safeeditmessage(
        call.message.chat.id,
        call.message.message_id,
        (
            f"🎬 Kling Video\n\n"
            f"Длительность: {data['videoduration']} сек\n"
            f"Формат: {data['videoaspectratio']}\n"
            f"Стоимость: {getvideocost(data['videomodel'], data['videoduration'])} {TOKEN_EMOJI}\n"
            f"Баланс: {balanceline(userid)}\n\n"
            f"Отправь текстовый промт или фото с подписью."
        ),
        replymarkup=getklingvideokeyboard(userid)
    )


@bot.callback_query_handler(func=lambda call: call.data == "videohelpshow")
def callbackvideohelpcall(call):
    userid = call.from_user.id
    data = getuserdata(userid)

    bot.answer_callback_query(call.id, "Параметры Kling Video")

    safeeditmessage(
        call.message.chat.id,
        call.message.message_id,
        (
            f"🎬 Kling Video\n\n"
            f"Длительность: {data['videoduration']} сек\n"
            f"Формат: {data['videoaspectratio']}\n"
            f"Стоимость: {getvideocost(data['videomodel'], data['videoduration'])} {TOKEN_EMOJI}\n"
            f"Баланс: {balanceline(userid)}\n\n"
            f"Отправь текстовый промт или фото с подписью."
        ),
        replymarkup=getklingvideokeyboard(userid)
    )

@bot.message_handler(content_types=["photo"])
def handlephotomessage(message):
    data = getuserdata(message.from_user.id)

    if data.get("videomode"):
        processklingmessage(message)
        return

    if data.get("imagemode"):
        processnanomessage(message)
        return

    bot.send_message(
        message.chat.id,
        "Выбери режим: Nano Banana или Kling.",
        reply_markup=getmainkeyboard()
    )


@bot.message_handler(content_types=["text"])
def handletextmessage(message):
    text = (message.text or "").strip()

    if text.startswith("/"):
        return

    if text == BTNNANO:
        btnnanobananamessage(message)
        return

    if text == BTNVIDEO:
        btnklingvideomessage(message)
        return

    if text == BTNTOPUP:
        btnpaymentsmessage(message)
        return

    if text == BTNSUPPORT:
        btnsupportmessage(message)
        return

    if text == BTNBALANCE:
        btnbalancemessage(message)
        return

    if text == BTNRESET:
        btnrestartmessage(message)
        return

    if text == BTNEXIT:
        btnexitmodemessage(message)
        return

    if text == BTNAI:
        btntextmodelsmessage(message)
        return

    userid = message.from_user.id
    data = getuserdata(userid)

    if data.get("videomode"):
        processklingmessage(message)
        return

    if data.get("imagemode"):
        processnanomessage(message)
        return

    processtextquestionmessage(message)


def process_update_safe(update):
    try:
        bot.process_new_updates([update])
    except Exception as e:
        logger.exception("Ошибка обработки update: %s", e)


@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    if not request.is_json:
        abort(403)

    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        WEBHOOK_EXECUTOR.submit(process_update_safe, update)
        return "ok", 200
    except Exception as e:
        logger.exception("Ошибка в webhook: %s", e)
        return "error", 500


@app.route("/", methods=["GET"])
def index():
    return "Bot is running"


def setup_webhook():
    if RENDER_EXTERNAL_HOSTNAME:
        webhook_url = f"https://{RENDER_EXTERNAL_HOSTNAME}/{TELEGRAM_TOKEN}"
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=webhook_url)

        info = bot.get_webhook_info()
        logger.info(
            "Webhook установлен: %s | pending=%s | last_error=%s",
            info.url,
            info.pending_update_count,
            info.last_error_message
        )
    else:
        logger.warning("RENDER_EXTERNAL_HOSTNAME не задан, webhook не будет установлен")


if __name__ == "__main__":
    init_db()
    setup_webhook()
    app.run(host="0.0.0.0", port=PORT)