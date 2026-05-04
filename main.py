import os
import re
import time
import uuid
import base64
import sqlite3
import logging
import mimetypes
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from collections import OrderedDict
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import telebot
from flask import Flask, request, abort
from telebot import types
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ============================================================
# КОНФИГ
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL")

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()}

YOOKASSA_ENABLED = all([YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, YOOKASSA_RETURN_URL])
PORT = int(os.environ.get("PORT", 10000))
WORKER_POOL_SIZE = int(os.getenv("WORKER_POOL_SIZE", "32"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN")
if not OPENROUTER_API_KEY:
    raise RuntimeError("Не задан OPENROUTER_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown", threaded=False)
app = Flask(__name__)

DB_PATH = "bot.db"
FREE_TOKENS = 100
FREE_RESET_COOLDOWN_DAYS = 3
TOKEN_EMOJI = "🍼"
GENERATED_DIR = "generated_images"
GENERATED_VIDEOS_DIR = "generated_videos"
CHAT_HISTORY_LIMIT = 12
USER_LOCK_TTL = 20 * 60  # 20 минут

os.makedirs(GENERATED_DIR, exist_ok=True)
os.makedirs(GENERATED_VIDEOS_DIR, exist_ok=True)

# ============================================================
# КНОПКИ ГЛАВНОГО МЕНЮ
# ============================================================
BTN_AI    = "🧠 GPT┃Gemini┃Claude"
BTN_PHOTO = "🖼 Photo модели"
BTN_VIDEO = "🎬 Video модели"
BTN_BALANCE = "📊 Баланс"
BTN_TOPUP   = "💳 Пополнение"
BTN_RESET   = "🔄 Сброс"
BTN_SUPPORT = "🛟 Поддержка"

MENU_BUTTONS = {BTN_AI, BTN_PHOTO, BTN_VIDEO, BTN_BALANCE, BTN_TOPUP, BTN_SUPPORT, BTN_RESET}

SUPPORT_USERNAME = "ai_patriot_support"
SUPPORT_URL = f"https://t.me/{SUPPORT_USERNAME}"

# ============================================================
# КОНФИГ МОДЕЛЕЙ
#
# Чтобы добавить новую модель — добавьте блок в нужный CONFIG-словарь.
# Всё остальное (кнопки, стоимости, роутинг) обновится автоматически.
# ============================================================

# ------ ТЕКСТОВЫЕ МОДЕЛИ ------
TEXT_MODELS_CONFIG = {
    "google/gemini-3-flash-preview": {
        "name": "Gemini 3 Flash",
        "cost": 1,
        "emoji": "⚡",
        "description": "Быстрая и дешёвая",
    },
    "openai/gpt-5.5": {
        "name": "GPT-5.5",
        "cost": 1,
        "emoji": "🤖",
        "description": "Мощная модель OpenAI",
    },
    "anthropic/claude-sonnet-4.6": {
        "name": "Claude Sonnet 4.6",
        "cost": 2,
        "emoji": "🎭",
        "description": "Умная и точная",
    },
    "anthropic/claude-opus-4.6": {
        "name": "Claude Opus 4.6",
        "cost": 7,
        "emoji": "👑",
        "description": "Лучшая от Anthropic",
    },
    "moonshotai/kimi-k2.6": {
        "name": "Kimi 2.6",
        "cost": 1,
        "emoji": "🌝",
        "description": "Moonshot AI",
    },
    # ═══ Добавить текстовую модель — скопируй блок выше ═══
}

# ------ МОДЕЛИ ИЗОБРАЖЕНИЙ ------
# cost_text  — генерация по тексту
# cost_photo — редактирование фото
IMAGE_MODELS_CONFIG = {
    "google/gemini-3-pro-image-preview": {
        "name": "Nano Banana Pro",
        "emoji": "🍌",
        "cost_text": 15,
        "cost_photo": 20,
        "description": "Генерация и редактирование",
    },
    "openai/gpt-5.4-image-2": {
        "name": "GPT Image-2",
        "emoji": "🖼",
        "cost_text": 15,
        "cost_photo": 20,
        "description": "Новейшая модель для генерации изображений от OpenAI",
    },
    # ═══ Добавить модель изображений — скопируй блок выше ═══
}

# ------ МОДЕЛИ ВИДЕО ------
# costs: {длительность_в_секундах: стоимость_в_токенах}
VIDEO_MODELS_CONFIG = {
    "kwaivgi/kling-v3.0-pro": {
        "name": "Kling V3 Pro",
        "emoji": "🎬",
        "costs": {5: 100, 10: 200},
        "description": "Качественная генерация видео",
        "max_duration": 10,
        # Формат передачи первого кадра в OpenRouter API
        "image_input_format": "frame_images",
    },
    "minimax/hailuo-2.3": {
        "name": "MiniMax Hailuo",
        "emoji": "🆙",
        "costs": {5: 50, 10: 100},
        "description": "Быстрая альтернатива",
        "max_duration": 10,
        # MiniMax принимает image_url на верхнем уровне
        "image_input_format": "image_url",
    },
    "bytedance/seedance-2.0": {
        "name": "Seedance 2.0",
        "emoji": "🎥",
        "costs": {5: 105, 10: 205},
        "description": "От ByteDance",
        "max_duration": 10,
        # Seedance принимает first_frame_image отдельным полем
        "image_input_format": "first_frame_image",
    },
    # ═══ Добавить видео модель — скопируй блок выше ═══
}

# ------ ДЕФОЛТЫ ------
DEFAULT_TEXT_MODEL      = "google/gemini-3-flash-preview"
DEFAULT_IMAGE_MODEL     = "google/gemini-3-pro-image-preview"
DEFAULT_VIDEO_MODEL     = "kwaivgi/kling-v3.0-pro"
DEFAULT_VIDEO_DURATION  = 5
DEFAULT_VIDEO_ASPECT_RATIO = "16:9"

# ============================================================
# АВТОГЕНЕРАЦИЯ словарей — НЕ ТРОГАТЬ
# ============================================================
TEXT_MODELS      = {k: v["name"] for k, v in TEXT_MODELS_CONFIG.items()}
TEXT_MODEL_COSTS = {k: v["cost"] for k, v in TEXT_MODELS_CONFIG.items()}

IMAGE_MODELS       = {k: f"{v['emoji']} {v['name']}" for k, v in IMAGE_MODELS_CONFIG.items()}
PROMPT_ONLY_COSTS  = {k: v["cost_text"]  for k, v in IMAGE_MODELS_CONFIG.items()}
PHOTO_PROMPT_COSTS = {k: v["cost_photo"] for k, v in IMAGE_MODELS_CONFIG.items()}

VIDEO_MODELS      = {k: f"{v['emoji']} {v['name']}" for k, v in VIDEO_MODELS_CONFIG.items()}
VIDEO_PROMPT_COSTS = {k: v["costs"] for k, v in VIDEO_MODELS_CONFIG.items()}

DEFAULT_MODEL = DEFAULT_TEXT_MODEL

PAY_PLANS = {
    "mini":  {"label": f"100 {TOKEN_EMOJI}",  "amount": 10, "tokens": 100},
    "basic": {"label": f"300 {TOKEN_EMOJI}", "amount": 550, "tokens": 300},
    "plus":  {"label": f"1000 {TOKEN_EMOJI}", "amount": 1850, "tokens": 1000},
    "pro":   {"label": f"3000 {TOKEN_EMOJI}", "amount": 5500, "tokens": 3000},
}

VIDEO_POLL_INTERVAL    = 15
VIDEO_POLL_MAX_ATTEMPTS = 60


# ============================================================
# HTTP CLIENT
# ============================================================
def _build_http_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3, backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
    )
    adapter = HTTPAdapter(pool_connections=50, pool_maxsize=100, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"Connection": "keep-alive"})
    return s

HTTP = _build_http_session()
OPENROUTER_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
}


# ============================================================
# DB: WAL + connection per thread + индексы
# ============================================================
_db_local = threading.local()

def _get_conn() -> sqlite3.Connection:
    conn = getattr(_db_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(
            DB_PATH, timeout=30, isolation_level=None, check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA cache_size=-20000;")
        conn.execute("PRAGMA busy_timeout=30000;")
        _db_local.conn = conn
    return conn


@contextmanager
def db_tx():
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        try: conn.execute("ROLLBACK;")
        except Exception: pass
        raise


def init_db():
    conn = _get_conn()
    conn.executescript("""
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
            video_model TEXT NOT NULL DEFAULT 'kwaivgi/kling-v3.0-pro',
            video_flow TEXT DEFAULT 'prompt_only',
            video_duration INTEGER NOT NULL DEFAULT 5,
            video_aspect_ratio TEXT NOT NULL DEFAULT '16:9',
            last_free_reset_at TEXT DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT UNIQUE, idempotence_key TEXT UNIQUE,
            user_id INTEGER NOT NULL, plan_key TEXT NOT NULL,
            amount INTEGER NOT NULL, tokens_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS generation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_uuid TEXT UNIQUE, user_id INTEGER NOT NULL,
            provider TEXT NOT NULL, kind TEXT NOT NULL,
            model TEXT NOT NULL, flow TEXT NOT NULL DEFAULT '',
            prompt_text TEXT DEFAULT '', cost INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'created',
            provider_generation_id TEXT DEFAULT '',
            polling_url TEXT DEFAULT '', file_path TEXT DEFAULT '',
            error_text TEXT DEFAULT '', charged INTEGER NOT NULL DEFAULT 0,
            chat_id INTEGER DEFAULT 0, wait_msg_id INTEGER DEFAULT 0,
            next_poll_at REAL DEFAULT 0, attempts INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_locks (
            user_id INTEGER PRIMARY KEY,
            locked_at REAL NOT NULL,
            reason TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ix_chat_user   ON chat_history(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS ix_jobs_poll   ON generation_jobs(kind, status, next_poll_at);
        CREATE INDEX IF NOT EXISTS ix_jobs_user   ON generation_jobs(user_id);
        CREATE INDEX IF NOT EXISTS ix_payments_user ON payments(user_id);
        CREATE INDEX IF NOT EXISTS ix_locks_age   ON user_locks(locked_at);
    """)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(generation_jobs)").fetchall()}
    for col, ddl in [
        ("chat_id",     "ALTER TABLE generation_jobs ADD COLUMN chat_id INTEGER DEFAULT 0"),
        ("wait_msg_id", "ALTER TABLE generation_jobs ADD COLUMN wait_msg_id INTEGER DEFAULT 0"),
        ("next_poll_at","ALTER TABLE generation_jobs ADD COLUMN next_poll_at REAL DEFAULT 0"),
        ("attempts",    "ALTER TABLE generation_jobs ADD COLUMN attempts INTEGER DEFAULT 0"),
    ]:
        if col not in existing:
            try: conn.execute(ddl)
            except sqlite3.OperationalError: pass


# ============================================================
# LRU КЭШ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================
class UserCache:
    def __init__(self, maxsize=5000, ttl=30):
        self.maxsize, self.ttl = maxsize, ttl
        self._data = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if not item: return None
            ts, val = item
            if time.time() - ts > self.ttl:
                self._data.pop(key, None); return None
            self._data.move_to_end(key)
            return val

    def set(self, key, value):
        with self._lock:
            self._data[key] = (time.time(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def invalidate(self, key):
        with self._lock:
            self._data.pop(key, None)

user_cache = UserCache(maxsize=5000, ttl=30)


# ============================================================
# RATE LIMIT
# ============================================================
_rate_lock = threading.Lock()
_rate_data: dict = {}

def rate_limit_ok(user_id: int, max_per_minute: int = 20) -> bool:
    now = time.time()
    with _rate_lock:
        bucket = _rate_data.setdefault(user_id, [])
        bucket[:] = [t for t in bucket if now - t < 60]
        if len(bucket) >= max_per_minute: return False
        bucket.append(now)
    return True


# ============================================================
# USER LOCKS (через БД — переживают рестарты)
# ============================================================
_user_busy_lock = threading.Lock()

def try_acquire_user(user_id: int, reason: str = "") -> bool:
    now = time.time()
    cutoff = now - USER_LOCK_TTL
    with _user_busy_lock:
        try:
            with db_tx() as c:
                c.execute("DELETE FROM user_locks WHERE locked_at < ?", (cutoff,))
                row = c.execute(
                    "SELECT locked_at, reason FROM user_locks WHERE user_id = ?",
                    (user_id,)
                ).fetchone()
                if row:
                    logger.info("🔒 Lock held user=%s since %.0fs (%s)", user_id, now-row[0], row[1])
                    return False
                c.execute(
                    "INSERT INTO user_locks (user_id, locked_at, reason) VALUES (?, ?, ?)",
                    (user_id, now, reason)
                )
                logger.info("🔐 Lock acquired user=%s (%s)", user_id, reason)
                return True
        except Exception as e:
            logger.exception("try_acquire_user: %s", e)
            return True  # fail-open


def release_user(user_id: int):
    with _user_busy_lock:
        try:
            with db_tx() as c:
                c.execute("DELETE FROM user_locks WHERE user_id = ?", (user_id,))
            logger.info("🔓 Lock released user=%s", user_id)
        except Exception as e:
            logger.warning("release_user: %s", e)


def cleanup_stale_locks():
    cutoff = time.time() - USER_LOCK_TTL
    try:
        with db_tx() as c:
            cur = c.execute("DELETE FROM user_locks WHERE locked_at < ?", (cutoff,))
            removed = getattr(cur, 'rowcount', 0) or 0
            if removed > 0:
                logger.info("🧹 Stale locks removed: %d", removed)
    except Exception as e:
        logger.warning("cleanup_stale_locks: %s", e)


# ============================================================
# ОПЕРАЦИИ С БД
# ============================================================
def ensure_user(user_id: int):
    conn = _get_conn()
    if conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
        return
    with db_tx() as c:
        c.execute("""
            INSERT OR IGNORE INTO users (
                user_id, model, free_tokens, paid_tokens,
                image_mode, image_model, image_flow, pending_image_prompt,
                video_mode, video_model, video_flow, video_duration,
                video_aspect_ratio, last_free_reset_at
            ) VALUES (?, ?, ?, 0, 0, ?, '', '', 0, ?, 'prompt_only', ?, ?, NULL)
        """, (user_id, DEFAULT_MODEL, FREE_TOKENS, DEFAULT_IMAGE_MODEL,
              DEFAULT_VIDEO_MODEL, DEFAULT_VIDEO_DURATION, DEFAULT_VIDEO_ASPECT_RATIO))


def get_user_data(user_id: int) -> dict:
    cached = user_cache.get(user_id)
    if cached is not None:
        return cached
    ensure_user(user_id)
    row = _get_conn().execute("""
        SELECT model, free_tokens, paid_tokens, image_mode, image_model,
               image_flow, pending_image_prompt, video_mode, video_model,
               video_flow, video_duration, video_aspect_ratio, last_free_reset_at
        FROM users WHERE user_id=?
    """, (user_id,)).fetchone()
    data = {
        "model":       row[0] if row[0] in TEXT_MODELS else DEFAULT_MODEL,
        "free_tokens": row[1],
        "paid_tokens": row[2],
        "image_mode":  bool(row[3]),
        "image_model": row[4] if row[4] in IMAGE_MODELS else DEFAULT_IMAGE_MODEL,
        "image_flow":  row[5] or "",
        "pending_image_prompt": row[6] or "",
        "video_mode":  bool(row[7]),
        "video_model": row[8] if row[8] in VIDEO_MODELS else DEFAULT_VIDEO_MODEL,
        "video_flow":  row[9] if row[9] in ("prompt_only", "photo_plus_prompt") else "prompt_only",
        "video_duration":     row[10] if row[10] in (5, 10) else DEFAULT_VIDEO_DURATION,
        "video_aspect_ratio": row[11] if row[11] in ("16:9","9:16","1:1") else DEFAULT_VIDEO_ASPECT_RATIO,
        "last_free_reset_at": row[12],
    }
    user_cache.set(user_id, data)
    return data


def _update_user(user_id: int, **fields):
    if not fields: return
    cols = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [user_id]
    with db_tx() as c:
        c.execute(f"UPDATE users SET {cols} WHERE user_id=?", values)
    user_cache.invalidate(user_id)


def set_user_model(uid, v):    _update_user(uid, model=v)
def set_image_mode(uid, v):    _update_user(uid, image_mode=1 if v else 0)
def set_image_model(uid, v):   _update_user(uid, image_model=v)
def set_image_flow(uid, v):    _update_user(uid, image_flow=v)
def set_pending_image_prompt(uid, v): _update_user(uid, pending_image_prompt=v)
def set_video_mode(uid, v):    _update_user(uid, video_mode=1 if v else 0)
def set_video_model(uid, v):   _update_user(uid, video_model=v)
def set_video_flow(uid, v):    _update_user(uid, video_flow=v)
def set_video_duration(uid, v):
    if v not in (5, 10): v = DEFAULT_VIDEO_DURATION
    _update_user(uid, video_duration=v)
def set_video_aspect_ratio(uid, v):
    if v not in ("16:9","9:16","1:1"): v = DEFAULT_VIDEO_ASPECT_RATIO
    _update_user(uid, video_aspect_ratio=v)

def clear_image_state(uid):
    _update_user(uid, image_mode=0, image_flow='', pending_image_prompt='')

def clear_video_state(uid):
    _update_user(uid, video_mode=0, video_model=DEFAULT_VIDEO_MODEL,
                 video_flow='', video_duration=DEFAULT_VIDEO_DURATION,
                 video_aspect_ratio=DEFAULT_VIDEO_ASPECT_RATIO)

def get_total_tokens(user_id):
    d = get_user_data(user_id)
    return d["free_tokens"] + d["paid_tokens"]

def balance_line(user_id):
    return f"💰 Твой баланс: *{get_total_tokens(user_id)}* {TOKEN_EMOJI}"


def can_reset_free_tokens(user_id):
    d = get_user_data(user_id)
    if not d["last_free_reset_at"]: return True, None
    try:
        last = datetime.fromisoformat(d["last_free_reset_at"])
    except Exception:
        return True, None
    nxt = last + timedelta(days=FREE_RESET_COOLDOWN_DAYS)
    now = datetime.utcnow()
    return (True, None) if now >= nxt else (False, nxt - now)


def format_timedelta_ru(delta):
    s = max(0, int(delta.total_seconds()))
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    parts = []
    if d: parts.append(f"{d} д.")
    if h: parts.append(f"{h} ч.")
    if m or not parts: parts.append(f"{m} мин.")
    return " ".join(parts)


def reset_free_tokens(user_id: int):
    """
    Сбрасывает ТОЛЬКО бесплатные токены до FREE_TOKENS.
    Платные токены НЕ ТРОГАЕТ.
    """
    with db_tx() as c:
        # Сначала читаем текущий баланс для лога
        row = c.execute(
            "SELECT free_tokens, paid_tokens FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        old_free = row[0] if row else 0
        paid     = row[1] if row else 0

        c.execute("""
            UPDATE users
            SET free_tokens = ?,
                last_free_reset_at = ?
            WHERE user_id = ?
        """, (FREE_TOKENS, datetime.utcnow().isoformat(), user_id))

        logger.info(
            "reset_free_tokens: user=%s free %d→%d paid=%d (unchanged)",
            user_id, old_free, FREE_TOKENS, paid
        )
    user_cache.invalidate(user_id)


def add_paid_tokens(user_id, amount):
    with db_tx() as c:
        c.execute("UPDATE users SET paid_tokens = paid_tokens + ? WHERE user_id=?",
                  (amount, user_id))
    user_cache.invalidate(user_id)


def refund_tokens(user_id, amount):
    if amount > 0:
        add_paid_tokens(user_id, amount)


def add_chat_message(user_id, role, content):
    with db_tx() as c:
        c.execute("INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)",
                  (user_id, role, content))


def get_chat_history(user_id, limit=CHAT_HISTORY_LIMIT):
    rows = _get_conn().execute("""
        SELECT role, content FROM chat_history WHERE user_id=?
        ORDER BY id DESC LIMIT ?
    """, (user_id, limit)).fetchall()
    rows = list(reversed(rows))
    return [{"role": r[0], "content": r[1]}
            for r in rows if r[0] in ("user", "assistant") and r[1]]


def clear_chat_history(user_id):
    with db_tx() as c:
        c.execute("DELETE FROM chat_history WHERE user_id=?", (user_id,))


def consume_tokens(user_id: int, cost: int):
    with db_tx() as c:
        row = c.execute("SELECT free_tokens, paid_tokens FROM users WHERE user_id=?",
                        (user_id,)).fetchone()
        if not row: return False, None, cost
        free, paid = row
        if free + paid < cost: return False, "limit", cost
        if paid >= cost:
            c.execute("UPDATE users SET paid_tokens = paid_tokens - ? WHERE user_id=?", (cost, user_id))
            src = "paid"
        elif paid > 0:
            rem = cost - paid
            c.execute("UPDATE users SET paid_tokens=0, free_tokens=free_tokens-? WHERE user_id=?",
                      (rem, user_id))
            src = "mixed"
        else:
            c.execute("UPDATE users SET free_tokens = free_tokens - ? WHERE user_id=?", (cost, user_id))
            src = "free"
    user_cache.invalidate(user_id)
    return True, src, cost


def create_generation_job(user_id, provider, kind, model, flow, prompt_text, cost,
                          chat_id=0, wait_msg_id=0):
    job_uuid = uuid.uuid4().hex
    with db_tx() as c:
        c.execute("""INSERT INTO generation_jobs
            (job_uuid, user_id, provider, kind, model, flow, prompt_text, cost,
             status, chat_id, wait_msg_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?,
 'created', ?, ?)""",
            (job_uuid, user_id, provider, kind, model, flow or '',
             prompt_text or '', cost, chat_id, wait_msg_id))
    return job_uuid


def update_generation_job(job_uuid, **fields):
    allowed = {"status","provider_generation_id","polling_url","file_path",
               "error_text","charged","next_poll_at","attempts"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields: return
    cols = ", ".join(f"{k}=?" for k in fields) + ", updated_at=CURRENT_TIMESTAMP"
    values = list(fields.values()) + [job_uuid]
    with db_tx() as c:
        c.execute(f"UPDATE generation_jobs SET {cols} WHERE job_uuid=?", values)


def create_payment_record(payment_id, idem_key, user_id, plan_key, amount, tokens_count):
    with db_tx() as c:
        c.execute("""INSERT INTO payments (payment_id, idempotence_key, user_id,
                     plan_key, amount, tokens_count, status)
                     VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                  (payment_id, idem_key, user_id, plan_key, amount, tokens_count))


def get_payment_by_id(pid):
    row = _get_conn().execute("""
        SELECT payment_id, user_id, plan_key, amount, tokens_count, status
        FROM payments WHERE payment_id=?
    """, (pid,)).fetchone()
    if not row: return None
    return {"payment_id": row[0], "user_id": row[1], "plan_key": row[2],
            "amount": row[3], "tokens_count": row[4], "status": row[5]}


def update_payment_status(pid, status):
    with db_tx() as c:
        c.execute("UPDATE payments SET status=? WHERE payment_id=?", (status, pid))


def is_admin(user_id): return user_id in ADMIN_IDS

def require_admin(message):
    if is_admin(message.from_user.id): return True
    safe_send_message(message.chat.id, "⛔ У тебя нет доступа к админ-командам.")
    return False


def get_user_balance_info(user_id):
    ensure_user(user_id)
    row = _get_conn().execute("""
        SELECT user_id, model, free_tokens, paid_tokens, image_mode,
               image_model, last_free_reset_at FROM users WHERE user_id=?
    """, (user_id,)).fetchone()
    if not row: return None
    return {"user_id": row[0], "model": row[1], "free_tokens": row[2],
            "paid_tokens": row[3], "total_tokens": row[2]+row[3],
            "image_mode": bool(row[4]), "image_model": row[5],
            "last_free_reset_at": row[6]}


def get_users_list(limit=20):
    rows = _get_conn().execute("""
        SELECT user_id, model, free_tokens, paid_tokens FROM users
        ORDER BY user_id DESC LIMIT ?
    """, (limit,)).fetchall()
    return [{"user_id": r[0], "model": r[1], "free_tokens": r[2],
             "paid_tokens": r[3], "total_tokens": r[2]+r[3]} for r in rows]


def admin_add_tokens(user_id, amount):
    with db_tx() as c:
        c.execute("UPDATE users SET paid_tokens = paid_tokens + ? WHERE user_id=?",
                  (amount, user_id))
    user_cache.invalidate(user_id)


# ============================================================
# KEYBOARDS
# ============================================================
def get_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN_AI)
    kb.row(BTN_PHOTO, BTN_VIDEO)
    kb.row(BTN_BALANCE, BTN_TOPUP)
    kb.row(BTN_SUPPORT, BTN_RESET)
    return kb

def get_current_keyboard(user_id): return get_main_keyboard()


def get_text_models_keyboard():
    """Inline-клавиатура выбора текстовой модели."""
    kb = types.InlineKeyboardMarkup()
    for model_id, cfg in TEXT_MODELS_CONFIG.items():
        kb.add(types.InlineKeyboardButton(
            f"{cfg.get('emoji','🤖')} {cfg['name']} — {cfg['cost']} {TOKEN_EMOJI}  {cfg.get('description','')}",
            callback_data=f"model:{model_id}"
        ))
    return kb


def get_image_models_keyboard(current_model_id: str = ""):
    """
    Inline-клавиатура выбора модели изображений.
    Автоматически строится из IMAGE_MODELS_CONFIG.
    """
    kb = types.InlineKeyboardMarkup()
    for model_id, cfg in IMAGE_MODELS_CONFIG.items():
        mark = "✅ " if model_id == current_model_id else ""
        kb.add(types.InlineKeyboardButton(
            f'{mark}{cfg["emoji"]} {cfg["name"]}: {cfg["cost_text"]}-{cfg["cost_photo"]}{TOKEN_EMOJI}',
            callback_data=f"imgmodel:{model_id}"
        ))
    return kb


def get_video_models_keyboard(current_model_id: str = ""):
    """
    Inline-клавиатура выбора модели видео.
    Автоматически строится из VIDEO_MODELS_CONFIG.
    """
    kb = types.InlineKeyboardMarkup()
    for model_id, cfg in VIDEO_MODELS_CONFIG.items():
        mark = "✅ " if model_id == current_model_id else ""
        costs_str = "-".join(str(c) for c in cfg["costs"].values()) + TOKEN_EMOJI
        kb.add(types.InlineKeyboardButton(
            f"{mark}{cfg['emoji']} {cfg['name']}: {costs_str}",
            callback_data=f"videomodel:{model_id}"
        ))
    return kb


def get_video_settings_keyboard(user_id: int):
    """Настройки длительности и соотношения сторон для выбранной видео-модели."""
    d = get_user_data(user_id)
    dur, ar = d["video_duration"], d["video_aspect_ratio"]
    cost = get_video_cost(d["video_model"], dur)
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(
            f"{'✅ ' if dur==5 else ''}5с", callback_data="videoduration:5"),
        types.InlineKeyboardButton(
            f"{'✅ ' if dur==10 else ''}10с", callback_data="videoduration:10"),
    )
    kb.row(
        types.InlineKeyboardButton(
            f"{'✅ ' if ar=='16:9' else ''}16:9", callback_data="videoaspect:16:9"),
        types.InlineKeyboardButton(
            f"{'✅ ' if ar=='9:16' else ''}9:16", callback_data="videoaspect:9:16"),
        types.InlineKeyboardButton(
            f"{'✅ ' if ar=='1:1' else ''}1:1",  callback_data="videoaspect:1:1"),
    )
    kb.add(types.InlineKeyboardButton(
        f"🎬 Начать генерацию ({cost} {TOKEN_EMOJI})",
        callback_data="videostart:go"
    ))
    return kb


def get_payments_keyboard():
    kb = types.InlineKeyboardMarkup()
    for k, p in PAY_PLANS.items():
        kb.add(types.InlineKeyboardButton(
            f"{p['label']} — {p['amount']} ₽",
            callback_data=f"payplan_{k}"   # ← добавить подчёркивание
        ))
    return kb


def format_balance_text(user_id):
    return balance_line(user_id)


# ============================================================
# TELEGRAM SAFE SEND
# ============================================================
def _extract_retry_after(e):
    m = re.search(r"retry after (\d+)", str(e), re.IGNORECASE)
    return int(m.group(1)) if m else None


def _tg_call(func, *args, max_attempts=3, base_delay=1.5, **kwargs):
    last = None
    for i in range(max_attempts):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last = e
            ra = _extract_retry_after(e)
            sleep_for = (ra + 1) if ra else base_delay * (i + 1)
            logger.warning("TG attempt %d/%d: %s", i + 1, max_attempts, e)
            if i < max_attempts - 1:
                time.sleep(sleep_for)
    raise last


def safe_send_message(chat_id, text, **kw):
    return _tg_call(bot.send_message, chat_id, text, **kw)

def safe_send_photo(chat_id, file_path, caption=None, parse_mode=None):
    with open(file_path, "rb") as f:
        return _tg_call(bot.send_photo, chat_id, photo=f,
                        caption=caption, parse_mode=parse_mode)

def safe_send_document(chat_id, file_path, caption=None, parse_mode=None):
    with open(file_path, "rb") as f:
        return _tg_call(bot.send_document, chat_id, document=f,
                        caption=caption, parse_mode=parse_mode)

def safe_send_video(chat_id, file_path, caption=None, parse_mode=None,
                    supports_streaming=True, **_):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Video not found: {file_path}")
    size_bytes = os.path.getsize(file_path)
    size_mb = size_bytes / (1024 * 1024)
    logger.info("📹 Sending video: %s (%.2f MB)", file_path, size_mb)
    if size_bytes > 50 * 1024 * 1024:
        raise ValueError(f"Video too large: {size_mb:.2f} MB (max 50 MB)")
    if size_bytes < 1024:
        raise ValueError(f"Video too small (broken?): {size_bytes} bytes")
    with open(file_path, "rb") as f:
        return _tg_call(bot.send_video, chat_id, f, caption=caption,
                        parse_mode=parse_mode,
                        supports_streaming=supports_streaming, timeout=120)


def safe_edit_message(chat_id, message_id, text, reply_markup=None):
    try:
        mk = reply_markup if isinstance(reply_markup, types.InlineKeyboardMarkup) else None
        bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id,
                              parse_mode="Markdown", reply_markup=mk)
    except Exception as e:
        logger.warning("edit fallback: %s", e)
        try:
            bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception: pass


def get_image_cost(model, flow):
    if flow == "prompt_only":      return PROMPT_ONLY_COSTS.get(model, 1)
    if flow == "photo_plus_prompt": return PHOTO_PROMPT_COSTS.get(model, 1)
    return 1

def get_video_cost(model, duration):
    return VIDEO_PROMPT_COSTS.get(model, {}).get(duration, 40)


def extract_data_url_parts(data_url):
    m = re.match(r"^data:(image\/[\w.+-]+);base64,(.+)$", data_url, re.DOTALL)
    return (m.group(1), m.group(2)) if m else (None, None)


def save_generated_image_from_data_url(data_url, prefix="nano"):
    mime, b64 = extract_data_url_parts(data_url)
    if not mime or not b64: return None
    ext = mimetypes.guess_extension(mime) or ".png"
    if ext == ".jpe": ext = ".jpg"
    fn  = f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
    fp  = os.path.join(GENERATED_DIR, fn)
    with open(fp, "wb") as f:
        f.write(base64.b64decode(b64))
    return fp


def telegram_photo_to_data_url(message):
    photo = message.photo[-1]
    fi   = bot.get_file(photo.file_id)
    data = bot.download_file(fi.file_path)
    return f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"


def send_generated_image_both(chat_id, file_path, caption_preview, caption_file):
    safe_send_photo(chat_id, file_path, caption=caption_preview)
    safe_send_document(chat_id, file_path, caption=caption_file)


# ============================================================
# OPENROUTER API
# ============================================================
def call_openrouter_text(model, user_message, history=None, max_retries=2):
    url = "https://openrouter.ai/api/v1/chat/completions"
    messages = [{"role": "system", "content": "Отвечай кратко, по делу, на русском языке."}]
    if history: messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    data = {"model": model, "messages": messages, "max_tokens": 500, "temperature": 0.7}
    for attempt in range(max_retries):
        try:
            r = HTTP.post(url, headers=OPENROUTER_HEADERS, json=data, timeout=90)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
            if r.status_code == 429:
                time.sleep(2 ** attempt); continue
            logger.error("OR text %s: %s", r.status_code, r.text[:300])
        except Exception as e:
            logger.exception("OR text err: %s", e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    if model != DEFAULT_MODEL:
        return call_openrouter_text(DEFAULT_MODEL, user_message, history, 1) + " ⚡"
    return "⚠️ Не удалось получить ответ от AI. Попробуй позже."


def generate_image_openrouter(model, prompt_text, input_image_data_url=None, max_retries=2):
    url   = "https://openrouter.ai/api/v1/chat/completions"
    parts = [{"type": "text", "text": prompt_text.strip()}]
    if input_image_data_url:
        parts.append({"type": "image_url", "image_url": {"url": input_image_data_url}})
    payload = {"model": model,
               "messages": [{"role": "user", "content": parts}],
               "modalities": ["image", "text"]}
    for attempt in range(max_retries):
        try:
            r = HTTP.post(url, headers=OPENROUTER_HEADERS, json=payload, timeout=180)
            if r.status_code == 200:
                res = r.json()
                msg  = (res.get("choices") or [{}])[0].get("message", {})
                imgs = msg.get("images") or []
                if imgs:
                    du = imgs[0].get("image_url", {}).get("url")
                    if du and du.startswith("data:image/"):
                        return {"ok": True, "image_data_url": du,
                                "text": msg.get("content") or "Готово"}
                logger.error("OR no image: %s", str(res)[:400])
            elif r.status_code == 429:
                time.sleep(2 ** attempt); continue
            else:
                logger.error("OR image %s: %s", r.status_code, r.text[:300])
        except Exception as e:
            logger.exception("OR image err: %s", e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    return {"ok": False, "error": "⚠️ Модель не вернула изображение. Попробуй другой промт."}


def submit_openrouter_video_generation(model, prompt, duration=5, aspect_ratio="16:9",
                                       input_image_data_url=None):
    """
    Отправляет запрос на генерацию видео в OpenRouter.

    Формат передачи первого кадра (image-to-video) зависит от модели
    и берётся из VIDEO_MODELS_CONFIG[model]["image_input_format"].

    Поддерживаемые форматы:
      "frame_images"      — Kling: поле frame_images[] с frame_type=first_frame
      "image_url"         — MiniMax: поле image_url на верхнем уровне payload
      "first_frame_image" — Seedance: поле first_frame_image на верхнем уровне
      "none" / отсутствует — модель не поддерживает image-to-video
    """
    url     = "https://openrouter.ai/api/v1/videos"
    payload = {
        "model":        model,
        "prompt":       prompt,
        "duration":     duration,
        "aspect_ratio": aspect_ratio,
    }

    # Добавляем фото в нужном формате если оно передано
    if input_image_data_url:
        model_cfg    = VIDEO_MODELS_CONFIG.get(model, {})
        img_format   = model_cfg.get("image_input_format", "frame_images")

        if img_format == "frame_images":
            # Kling и совместимые
            payload["frame_images"] = [{
                "type":       "image_url",
                "image_url":  {"url": input_image_data_url},
                "frame_type": "first_frame",
            }]

        elif img_format == "image_url":
            # MiniMax Hailuo
            payload["image_url"] = input_image_data_url

        elif img_format == "first_frame_image":
            # Seedance
            payload["first_frame_image"] = input_image_data_url

        elif img_format == "none":
            # Модель не поддерживает image-to-video — предупреждаем и продолжаем без фото
            logger.warning(
                "Model %s does not support image-to-video, ignoring photo", model
            )

        else:
            # Неизвестный формат — пробуем frame_images как fallback
            logger.warning(
                "Unknown image_input_format '%s' for model %s, using frame_images as fallback",
                img_format, model
            )
            payload["frame_images"] = [{
                "type":       "image_url",
                "image_url":  {"url": input_image_data_url},
                "frame_type": "first_frame",
            }]

    logger.info(
        "Video submit: model=%s duration=%s ar=%s has_image=%s format=%s",
        model, duration, aspect_ratio,
        bool(input_image_data_url),
        VIDEO_MODELS_CONFIG.get(model, {}).get("image_input_format", "frame_images")
            if input_image_data_url else "n/a"
    )

    try:
        r = HTTP.post(url, headers=OPENROUTER_HEADERS, json=payload, timeout=60)

        if r.status_code not in (200, 202):
            # Детальное логирование ошибки для диагностики
            logger.error(
                "OR video submit error: model=%s status=%d body=%s",
                model, r.status_code, r.text[:500]
            )
            # Пытаемся извлечь читаемую причину из ответа
            try:
                err_body = r.json()
                err_msg  = (err_body.get("error", {}).get("message")
                            or err_body.get("message")
                            or r.text[:200])
            except Exception:
                err_msg = r.text[:200]

            return {"ok": False, "error": f"Ошибка API: {err_msg}"}

        d = r.json()
        return {
            "ok":          True,
            "id":          d.get("id"),
            "polling_url": d.get("polling_url"),
            "status":      d.get("status", "pending"),
        }

    except requests.exceptions.Timeout:
        logger.error("OR video submit timeout: model=%s", model)
        return {"ok": False, "error": "Таймаут при запуске генерации. Попробуй ещё раз."}
    except Exception as e:
        logger.exception("submit video err: %s", e)
        return {"ok": False, "error": "Ошибка при запуске генерации видео."}


def poll_openrouter_video(polling_url):
    try:
        r = HTTP.get(polling_url,
                     headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                     timeout=30)
        if r.status_code != 200:
            return {"ok": False, "error": r.text[:200]}
        d = r.json()
        return {"ok": True, "status": d.get("status"),
                "unsigned_urls": d.get("unsigned_urls", []),
                "error_message": d.get("error")}
    except Exception as e:
        logger.exception("poll err: %s", e)
        return {"ok": False, "error": "poll error"}


def download_openrouter_video_content(gen_id, index=0, prefix="video"):
    fn  = f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp4"
    fp  = os.path.join(GENERATED_VIDEOS_DIR, fn)
    url = f"https://openrouter.ai/api/v1/videos/{gen_id}/content?index={index}"
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            logger.info("📥 Video download attempt %d/%d gen_id=%s", attempt+1, max_attempts, gen_id)
            with HTTP.get(url,
                          headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                          timeout=600, stream=True) as r:
                if r.status_code != 200:
                    logger.error("video dl %d: %s", r.status_code, r.text[:200])
                    if attempt < max_attempts - 1:
                        time.sleep(3*(attempt+1)); continue
                    return None
                expected = int(r.headers.get("Content-Length", 0))
                written  = 0
                with open(fp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk); written += len(chunk)
                logger.info("📥 Downloaded %d bytes (%.2f MB)", written, written/1024/1024)
                if written < 10 * 1024:
                    logger.error("File too small: %d bytes", written)
                    try: os.remove(fp)
                    except Exception: pass
                    if attempt < max_attempts - 1:
                        time.sleep(3*(attempt+1)); continue
                    return None
                if expected > 0 and abs(written - expected) > 1024:
                    logger.error("Size mismatch: got %d expected %d", written, expected)
                    try: os.remove(fp)
                    except Exception: pass
                    if attempt < max_attempts - 1:
                        time.sleep(3*(attempt+1)); continue
                    return None
                return fp
        except requests.exceptions.Timeout:
            logger.warning("Video download timeout attempt %d", attempt+1)
            if attempt < max_attempts - 1: time.sleep(5*(attempt+1))
        except Exception as e:
            logger.exception("Video download err attempt %d: %s", attempt+1, e)
            if attempt < max_attempts - 1: time.sleep(3*(attempt+1))
    logger.error("❌ All download attempts failed gen_id=%s", gen_id)
    return None


# ============================================================
# YOOKASSA
# ============================================================
def create_yookassa_payment(user_id, plan_key):
    if not YOOKASSA_ENABLED or plan_key not in PAY_PLANS:
        return None, None

    plan = PAY_PLANS[plan_key]
    idem = str(uuid.uuid4())

    payload = {
        "amount": {"value": f"{plan['amount']}.00", "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": YOOKASSA_RETURN_URL
        },
        "description": "Patriot AI — пополнение токенов",
        "metadata": {
            "user_id": str(user_id),
            "plan_key": plan_key,
            "tokens_count": str(plan['tokens'])
        }
    }

    try:
        r = HTTP.post(
            "https://api.yookassa.ru/v3/payments",
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            headers={
                "Content-Type": "application/json",
                "Idempotence-Key": idem
            },
            json=payload,
            timeout=30
        )
        if r.status_code not in (200, 201):
            return None, None

        d = r.json()
        create_payment_record(d["id"], idem, user_id, plan_key, plan["amount"], plan["tokens"])
        return d["id"], d["confirmation"]["confirmation_url"]
    except Exception as e:
        logger.exception("YK err: %s", e)
        return None, None


def apply_payment_if_needed(payment_id):
    p = get_payment_by_id(payment_id)
    if not p: return False
    if p["status"] == "succeeded": return True
    add_paid_tokens(p["user_id"], p["tokens_count"])
    update_payment_status(payment_id, "succeeded")
    try:
        total = get_total_tokens(p["user_id"])
        safe_send_message(p["user_id"],
            f"✅ Оплата прошла!\n\n"
            f"Пакет: *{PAY_PLANS[p['plan_key']]['label']}*\n"
            f"Начислено: *{p['tokens_count']}* {TOKEN_EMOJI}\n"
            f"💰 Баланс: *{total}* {TOKEN_EMOJI}",
            reply_markup=get_main_keyboard())
    except Exception as e:
        logger.warning("notify pay: %s", e)
    return True


# ============================================================
# WORKERS POOL
# ============================================================
executor = ThreadPoolExecutor(max_workers=WORKER_POOL_SIZE, thread_name_prefix="worker")

def submit_task(fn, *args, **kwargs):
    def _wrap():
        try: fn(*args, **kwargs)
        except Exception as e: logger.exception("task err: %s", e)
    executor.submit(_wrap)


# ============================================================
# BUSINESS LOGIC
# ============================================================
def process_text_question(message):
    user_id   = message.from_user.id
    user_text = (message.text or "").strip()
    if not user_text:
        safe_send_message(message.chat.id, "Напиши текстовый вопрос.",
                          reply_markup=get_main_keyboard()); return

    d     = get_user_data(user_id)
    model = d["model"] if d["model"] in TEXT_MODELS else DEFAULT_MODEL
    cost  = TEXT_MODEL_COSTS.get(model, 1)

    if get_total_tokens(user_id) < cost:
        safe_send_message(message.chat.id,
            f"❌ Недостаточно токенов для *{TEXT_MODELS.get(model, model)}*.\n\n"
            f"Стоимость: *{cost}* {TOKEN_EMOJI}\n{balance_line(user_id)}",
            reply_markup=get_main_keyboard()); return

    wait = safe_send_message(message.chat.id, "🤖 Думаю... ⏳")
    try: bot.send_chat_action(message.chat.id, "typing")
    except Exception: pass

    history = get_chat_history(user_id)
    answer  = call_openrouter_text(model, user_text, history=history)

    ok, _, charged = consume_tokens(user_id, cost)
    if not ok:
        safe_edit_message(message.chat.id, wait.message_id,
            f"❌ Не удалось списать токены.\n\n{balance_line(user_id)}")
        return

    add_chat_message(user_id, "user", user_text)
    add_chat_message(user_id, "assistant", answer)
    safe_edit_message(message.chat.id, wait.message_id,
        f"🤖 *{TEXT_MODELS.get(model, model)}*\n\n{answer}\n\n"
        f"💸 Списано: *{charged}* {TOKEN_EMOJI}\n{balance_line(user_id)}")


def process_nano_request(message):
    user_id = message.from_user.id
    if not try_acquire_user(user_id, reason="image_generation"):
        safe_send_message(message.chat.id,
            "⏳ У тебя уже идёт генерация — дождись результата.",
            reply_markup=get_main_keyboard())
        return
    try:
        d          = get_user_data(user_id)
        model      = d["image_model"]
        model_name = IMAGE_MODELS.get(model, model)
        is_photo   = (message.content_type == "photo")

        if is_photo:
            flow        = "photo_plus_prompt"
            prompt_text = (message.caption or "").strip()
            if not prompt_text:
                safe_send_message(message.chat.id,
                    "📸 Отправь фото *с подписью* — опиши что сделать.",
                    reply_markup=get_main_keyboard()); return
        else:
            flow        = "prompt_only"
            prompt_text = (message.text or "").strip()
            if not prompt_text:
                safe_send_message(message.chat.id, "✍️ Напиши текстовый запрос.",
                                  reply_markup=get_main_keyboard()); return

        cost = get_image_cost(model, flow)
        if get_total_tokens(user_id) < cost:
            safe_send_message(message.chat.id,
                f"❌ Недостаточно токенов для *{model_name}*.\n"
                f"Стоимость: *{cost}* {TOKEN_EMOJI}\n{balance_line(user_id)}",
                reply_markup=get_main_keyboard()); return

        job  = create_generation_job(user_id, "openrouter", "image", model, flow,
                                     prompt_text, cost, chat_id=message.chat.id)
        wait = safe_send_message(message.chat.id,
            f"🖼 {'Обрабатываю' if is_photo else 'Генерирую'}... ⏳\n"
            f"Модель: *{model_name}*")

        input_img = None
        if is_photo:
            try:
                input_img = telegram_photo_to_data_url(message)
            except Exception as e:
                update_generation_job(job, status="failed", error_text=str(e))
                safe_edit_message(message.chat.id, wait.message_id,
                                  "❌ Не удалось скачать фото.")
                return

        res = generate_image_openrouter(model, prompt_text, input_img)
        if not res["ok"]:
            update_generation_job(job, status="failed", error_text=res["error"])
            safe_edit_message(message.chat.id, wait.message_id, res["error"])
            return

        fp = save_generated_image_from_data_url(
            res["image_data_url"], prefix="photo" if is_photo else "prompt")
        if not fp:
            update_generation_job(job, status="failed", error_text="save_failed")
            safe_edit_message(message.chat.id, wait.message_id, "❌ Не удалось сохранить.")
            return

        update_generation_job(job, status="completed", file_path=fp)

        try:
            caption = (f"{model_name}\n"
                       f"{'🖼 Редактирование' if is_photo else '🎨 Генерация'}")
            send_generated_image_both(message.chat.id, fp, caption, "📎 Оригинал без сжатия")
        except Exception as e:
            update_generation_job(job, status="failed", error_text=f"send:{e}")
            safe_edit_message(message.chat.id, wait.message_id,
                "❌ Готово, но отправить не удалось. Токены не списаны.")
            return

        ok, _, charged = consume_tokens(user_id, cost)
        if not ok:
            update_generation_job(job, status="failed", error_text="charge_failed")
            safe_edit_message(message.chat.id, wait.message_id,
                f"⚠️ Отправлено, но не удалось списать.\n{balance_line(user_id)}")
            return

        update_generation_job(job, status="delivered", charged=1)
        safe_edit_message(message.chat.id, wait.message_id,
            f"✅ Готово.\n💸 Списано: *{charged}* {TOKEN_EMOJI}\n{balance_line(user_id)}")
    finally:
        release_user(user_id)


def submit_video_job(message):
    user_id = message.from_user.id
    if not try_acquire_user(user_id, reason="video_generation"):
        safe_send_message(message.chat.id,
            "⏳ У тебя уже идёт генерация видео.",
            reply_markup=get_main_keyboard())
        return
    acquired = True
    try:
        d          = get_user_data(user_id)
        model      = d["video_model"]
        model_cfg  = VIDEO_MODELS_CONFIG.get(model, {})
        model_name = VIDEO_MODELS.get(model, model)
        dur, ar    = d["video_duration"], d["video_aspect_ratio"]
        cost       = get_video_cost(model, dur)
        is_photo   = (message.content_type == "photo")

        # ── Проверка: модель поддерживает image-to-video? ──────────────────
        if is_photo and model_cfg.get("image_input_format", "frame_images") == "none":
            safe_send_message(message.chat.id,
                f"⚠️ Модель *{model_name}* не поддерживает генерацию видео по фото.\n\n"
                f"Отправь текстовый запрос или выбери другую модель.",
                reply_markup=get_main_keyboard())
            return
        # ───────────────────────────────────────────────────────────────────

        if is_photo:
            flow   = "photo_plus_prompt"
            prompt = (message.caption or "").strip()
            if not prompt:
                safe_send_message(message.chat.id, "📸 Отправь фото с подписью.",
                                  reply_markup=get_main_keyboard()); return
        else:
            flow   = "prompt_only"
            prompt = (message.text or "").strip()
            if not prompt:
                safe_send_message(message.chat.id, "✍️ Напиши запрос.",
                                  reply_markup=get_main_keyboard()); return

        if get_total_tokens(user_id) < cost:
            safe_send_message(message.chat.id,
                f"❌ Недостаточно токенов для *{model_name}*.\n"
                f"Стоимость: *{cost}* {TOKEN_EMOJI}\n{balance_line(user_id)}",
                reply_markup=get_main_keyboard()); return

        wait = safe_send_message(message.chat.id,
            f"🎬 Ставлю в очередь...\n"
            f"Модель: *{model_name}*  ⏱ *{dur}с*  📐 *{ar}*\n"
            f"Обычно 1–5 минут.")
        job = create_generation_job(user_id, "openrouter", "video", model, flow,
                                    prompt, cost, chat_id=message.chat.id,
                                    wait_msg_id=wait.message_id)

        input_img = None
        if is_photo:
            try:
                input_img = telegram_photo_to_data_url(message)
            except Exception as e:
                update_generation_job(job, status="failed", error_text=str(e))
                safe_edit_message(message.chat.id, wait.message_id, "❌ Ошибка фото.")
                return

        res = submit_openrouter_video_generation(model, prompt, dur, ar, input_img)
        if not res["ok"]:
            update_generation_job(job, status="failed", error_text=res["error"])
            safe_edit_message(message.chat.id, wait.message_id,
                f"❌ {res['error']}")
            return

        update_generation_job(job, status="submitted",
                              provider_generation_id=res.get("id") or "",
                              polling_url=res.get("polling_url") or "",
                              next_poll_at=time.time() + VIDEO_POLL_INTERVAL)
        safe_edit_message(message.chat.id, wait.message_id,
            f"🎬 Запущено!\n"
            f"Модель: *{model_name}*  ⏱ *{dur}с*  📐 *{ar}*\n"
            f"💰 Токены спишутся после готовности.")
        acquired = False
    finally:
        if acquired:
            release_user(user_id)


# ============================================================
# VIDEO BACKGROUND POLLER
# ============================================================
def _finalize_video_job(job_row):
    job_uuid = job_row["job_uuid"]
    user_id  = job_row["user_id"]
    chat_id  = job_row["chat_id"]
    model    = job_row["model"]
    cost     = job_row["cost"]
    gen_id   = job_row["provider_generation_id"]
    try:
        prefix = "photo_video" if job_row["flow"] == "photo_plus_prompt" else "text_video"
        fp = download_openrouter_video_content(gen_id, 0, prefix=prefix)

        if not fp:
            update_generation_job(job_uuid, status="failed", error_text="download_failed")
            safe_send_message(chat_id,
                "❌ Не удалось скачать видео. Токены не списаны.",
                reply_markup=get_main_keyboard())
            return

        if not os.path.exists(fp) or os.path.getsize(fp) < 10 * 1024:
            update_generation_job(job_uuid, status="failed", error_text="file_corrupted")
            safe_send_message(chat_id,
                "❌ Файл повреждён. Токены не списаны, попробуй ещё раз.",
                reply_markup=get_main_keyboard())
            try:
                if os.path.exists(fp): os.remove(fp)
            except Exception: pass
            return

        file_size_mb = os.path.getsize(fp) / (1024 * 1024)
        logger.info("📦 Video ready: %s (%.2f MB)", fp, file_size_mb)
        update_generation_job(job_uuid, status="completed", file_path=fp)

        video_sent = False
        send_error  = None
        try:
            safe_send_video(chat_id, fp,
                caption=f"🎬 Готово! *{VIDEO_MODELS.get(model, model)}*  ({file_size_mb:.1f} MB)",
                parse_mode="Markdown", supports_streaming=True)
            video_sent = True
            logger.info("✅ Video sent via send_video")
        except Exception as e:
            send_error = e
            logger.warning("send_video failed → try send_document: %s", e)

        if not video_sent:
            try:
                safe_send_document(chat_id, fp,
                    caption=f"🎬 Готово ({file_size_mb:.1f} MB) 📎 файл",
                    parse_mode=None)
                video_sent = True
                logger.info("✅ Video sent via send_document")
            except Exception as e:
                logger.exception("send_document also failed: %s", e)
                send_error = e

        if not video_sent:
            err_text = str(send_error)[:300]
            update_generation_job(job_uuid, status="failed", error_text=f"send_failed:{err_text}")
            safe_send_message(chat_id,
                f"❌ Видео ({file_size_mb:.1f} MB) сгенерировано, но не удалось отправить.\n\n"
                f"Возможные причины:\n"
                f"• Файл больше 50 MB (лимит Telegram)\n"
                f"• Временный сбой API\n\n"
                f"💰 Токены не списаны.",
                reply_markup=get_main_keyboard())
            return

        ok, _, ch = consume_tokens(user_id, cost)
        if not ok:
            update_generation_job(job_uuid, status="failed", error_text="charge_failed")
            safe_send_message(chat_id,
                f"⚠️ Видео отправлено, но списание не прошло.\n{balance_line(user_id)}",
                reply_markup=get_main_keyboard())
            return

        update_generation_job(job_uuid, status="delivered", charged=1)
        safe_send_message(chat_id,
            f"💸 Списано *{ch}* {TOKEN_EMOJI}\n{balance_line(user_id)}",
            parse_mode="Markdown", reply_markup=get_main_keyboard())

    except Exception as e:
        logger.exception("❌ _finalize_video_job error: %s", e)
        update_generation_job(job_uuid, status="failed", error_text=f"unexpected:{str(e)[:200]}")
        try:
            safe_send_message(chat_id,
                "❌ Непредвиденная ошибка при обработке видео. Токены не списаны.",
                reply_markup=get_main_keyboard())
        except Exception: pass
    finally:
        release_user(user_id)


def _poll_video_job(job_row):
    job_uuid = job_row["job_uuid"]
    user_id  = job_row["user_id"]
    attempts = job_row["attempts"] or 0
    try:
        if attempts >= VIDEO_POLL_MAX_ATTEMPTS:
            update_generation_job(job_uuid, status="timeout", error_text="poll_timeout")
            try:
                safe_send_message(job_row["chat_id"],
                    "⏳ Видео не успело сгенерироваться. Токены не списаны.",
                    reply_markup=get_main_keyboard())
            except Exception: pass
            release_user(user_id)
            return

        res = poll_openrouter_video(job_row["polling_url"])
        update_generation_job(job_uuid, attempts=attempts + 1)

        if not res["ok"]: return

        status = res.get("status")
        if status in ("pending", "in_progress", None):
            update_generation_job(job_uuid, status=status or "polling")
            return

        if status == "failed":
            update_generation_job(job_uuid, status="failed",
                                  error_text=res.get("error_message") or "video_failed")
            try:
                safe_send_message(job_row["chat_id"],
                    f"❌ Генерация не удалась.\n{res.get('error_message') or ''}",
                    reply_markup=get_main_keyboard())
            except Exception: pass
            release_user(user_id)
            return

        if status == "completed":
            _finalize_video_job(job_row)
            return

        logger.warning("Unknown video status: %s job=%s", status, job_uuid)

    except Exception as e:
        logger.exception("❌ _poll_video_job error: %s", e)
        try: release_user(user_id)
        except Exception: pass


def video_poller_loop():
    logger.info("video_poller started")
    while True:
        try:
            now  = time.time()
            rows = _get_conn().execute("""
                SELECT job_uuid, user_id, chat_id, wait_msg_id, model, cost,
                       polling_url, provider_generation_id, flow, attempts
                FROM generation_jobs
                WHERE kind='video'
                  AND status IN ('submitted','polling','pending','in_progress')
                  AND polling_url != ''
                  AND next_poll_at <= ?
                ORDER BY next_poll_at ASC LIMIT 50
            """, (now,)).fetchall()
            for r in rows:
                with db_tx() as c:
                    c.execute("UPDATE generation_jobs SET next_poll_at=? WHERE job_uuid=?",
                              (now + VIDEO_POLL_INTERVAL, r[0]))
                submit_task(_poll_video_job, {
                    "job_uuid": r[0], "user_id": r[1], "chat_id": r[2],
                    "wait_msg_id": r[3], "model": r[4], "cost": r[5],
                    "polling_url": r[6], "provider_generation_id": r[7],
                    "flow": r[8], "attempts": r[9],
                })
        except Exception as e:
            logger.exception("poller loop: %s", e)
        time.sleep(5)


# ============================================================
# HANDLERS — команды
# ============================================================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    logger.info("🚀 /start user=%s", message.from_user.id)
    uid = message.from_user.id
    ensure_user(uid)          # создаёт пользователя если нет
    clear_chat_history(uid)   # очищает историю чата
    clear_image_state(uid)    # сбрасывает режим изображений
    clear_video_state(uid)    # сбрасывает режим видео
    # НЕТ reset_free_tokens здесь!
    safe_send_message(message.chat.id,
        "Я Patriot AI 🦸🏼‍♂️\n\n"
        f"*{BTN_AI}* — текстовые модели\n"
        f"*{BTN_PHOTO}* — генерация и редактирование изображений\n"
        f"*{BTN_VIDEO}* — генерация видео",
        reply_markup=get_main_keyboard())


@bot.message_handler(commands=["restart"])
def cmd_restart(message):
    uid = message.from_user.id
    ok, wait = can_reset_free_tokens(uid)
    if not ok:
        safe_send_message(message.chat.id,
            f"⏳ Бесплатные токены можно сбрасывать раз в *{FREE_RESET_COOLDOWN_DAYS}* дня.\n\n"
            f"Попробуй снова через: *{format_timedelta_ru(wait)}*",
            reply_markup=get_main_keyboard())
        return

    # Запоминаем платные ДО сброса — чтобы показать в сообщении
    d_before   = get_user_data(uid)
    paid_before = d_before["paid_tokens"]

    reset_free_tokens(uid)

    d_after = get_user_data(uid)
    total   = d_after["free_tokens"] + d_after["paid_tokens"]

    # Формируем наглядное сообщение
    lines = [
        f"🔄 *Бесплатные токены восстановлены!*\n",
        f"🎁 Бесплатные: *{d_after['free_tokens']}* {TOKEN_EMOJI} _(сброшены до {FREE_TOKENS})_",
    ]
    if paid_before > 0:
        lines.append(
            f"💳 Платные: *{paid_before}* {TOKEN_EMOJI} _(не изменились)_"
        )
    lines.append(f"\n💰 Итоговый баланс: *{total}* {TOKEN_EMOJI}")

    safe_send_message(message.chat.id,
        "\n".join(lines),
        reply_markup=get_main_keyboard())


@bot.message_handler(commands=["newchat"])
def cmd_newchat(message):
    clear_chat_history(message.from_user.id)
    safe_send_message(message.chat.id, "🧹 История очищена.",
                      reply_markup=get_main_keyboard())


@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    safe_send_message(message.chat.id, f"Твой ID: `{message.from_user.id}`")


@bot.message_handler(commands=["models"])
def cmd_models(message):
    """Справка по всем доступным моделям."""
    lines = ["📋 *Доступные модели:*\n"]
    lines.append("*🗣 Текст:*")
    uid = message.from_user.id
    d   = get_user_data(uid)
    for mid, cfg in TEXT_MODELS_CONFIG.items():
        mark = "✅ " if d["model"] == mid else ""
        lines.append(f"{mark}{cfg.get('emoji','🤖')} *{cfg['name']}* — {cfg['cost']} {TOKEN_EMOJI}\n"
                     f"   `{mid}`")
    lines.append("\n*🖼 Изображения:*")
    for mid, cfg in IMAGE_MODELS_CONFIG.items():
        mark = "✅ " if d["image_model"] == mid else ""
        lines.append(f"{mark}{cfg['emoji']} *{cfg['name']}* — "
                     f"текст: {cfg['cost_text']} | фото: {cfg['cost_photo']} {TOKEN_EMOJI}\n"
                     f"   `{mid}`")
    lines.append("\n*🎬 Видео:*")
    for mid, cfg in VIDEO_MODELS_CONFIG.items():
        mark  = "✅ " if d["video_model"] == mid else ""
        costs = " / ".join(f"{s}с={c}{TOKEN_EMOJI}" for s, c in cfg["costs"].items())
        lines.append(f"{mark}{cfg['emoji']} *{cfg['name']}* — {costs}\n"
                     f"   `{mid}`")
    safe_send_message(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not require_admin(message): return
    safe_send_message(message.chat.id,
        "🛠 *Админ-режим*\n\n"
        "/users — последние\n"
        "/user ID — баланс\n"
        "/addtokens ID AMOUNT — начислить\n"
        "/locks — активные блокировки\n"
        "/unlock [ID] — снять блокировку\n"
        "/models — список всех моделей")


@bot.message_handler(commands=["user"])
def cmd_user_info(message):
    if not require_admin(message): return
    parts = message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        safe_send_message(message.chat.id, "Использование: `/user USER_ID`"); return
    info = get_user_balance_info(int(parts[1]))
    if not info:
        safe_send_message(message.chat.id, "Пользователь не найден."); return
    safe_send_message(message.chat.id,
        f"👤 `{info['user_id']}`\n"
        f"🧠 Модель: *{TEXT_MODELS.get(info['model'], info['model'])}*\n"
        f"🎁 Free: *{info['free_tokens']}* {TOKEN_EMOJI}\n"
        f"💳 Paid: *{info['paid_tokens']}* {TOKEN_EMOJI}\n"
        f"💰 Итого: *{info['total_tokens']}* {TOKEN_EMOJI}")


@bot.message_handler(commands=["users"])
def cmd_users(message):
    if not require_admin(message): return
    users = get_users_list(20)
    if not users:
        safe_send_message(message.chat.id, "Пользователей нет."); return
    lines = ["🧾 *Последние пользователи:*\n"]
    for u in users:
        lines.append(f"`{u['user_id']}` — *{u['total_tokens']}* {TOKEN_EMOJI} "
                     f"— {TEXT_MODELS.get(u['model'], u['model'])}")
    safe_send_message(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["addtokens"])
def cmd_addtokens(message):
    if not require_admin(message): return
    parts = message.text.strip().split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        safe_send_message(message.chat.id, "Использование: `/addtokens USER_ID AMOUNT`"); return
    uid, amount = int(parts[1]), int(parts[2])
    if amount <= 0: return
    admin_add_tokens(uid, amount)
    total = get_total_tokens(uid)
    safe_send_message(message.chat.id,
        f"✅ {uid} +{amount} {TOKEN_EMOJI}\n💰 Баланс: *{total}* {TOKEN_EMOJI}")
    try:
        safe_send_message(uid,
            f"🎁 Начислено *{amount}* {TOKEN_EMOJI}\n💰 Баланс: *{total}* {TOKEN_EMOJI}",
            reply_markup=get_main_keyboard())
    except Exception as e:
        logger.warning("notify %s: %s", uid, e)


@bot.message_handler(commands=["locks"])
def cmd_locks(message):
    if not require_admin(message): return
    try:
        rows = _get_conn().execute("""
            SELECT user_id, locked_at, reason FROM user_locks
            ORDER BY locked_at DESC LIMIT 50
        """).fetchall()
        if not rows:
            safe_send_message(message.chat.id, "✅ Активных lock'ов нет."); return
        now   = time.time()
        lines = ["🔒 *Активные lock'и:*\n"]
        for uid, locked_at, reason in rows:
            age = int(now - locked_at)
            m, s = divmod(age, 60)
            lines.append(f"`{uid}` — {m}м {s}с — {reason or '—'}")
        safe_send_message(message.chat.id, "\n".join(lines))
    except Exception as e:
        safe_send_message(message.chat.id, f"Ошибка: {e}")


@bot.message_handler(commands=["unlock"])
def cmd_unlock(message):
    if not require_admin(message): return
    parts = message.text.strip().split()
    if len(parts) == 1:
        try:
            with db_tx() as c:
                cnt = c.execute("SELECT COUNT(*) FROM user_locks").fetchone()[0]
                c.execute("DELETE FROM user_locks")
            safe_send_message(message.chat.id, f"🔓 Сняты все lock'и: *{cnt}*")
        except Exception as e:
            safe_send_message(message.chat.id, f"Ошибка: {e}")
        return
    if len(parts) == 2 and parts[1].isdigit():
        release_user(int(parts[1]))
        safe_send_message(message.chat.id, f"🔓 Lock снят для `{parts[1]}`")
        return
    safe_send_message(message.chat.id,
        "`/unlock` — все\n`/unlock USER_ID` — конкретный")


# ============================================================
# HANDLERS — кнопки главного меню
# ============================================================
@bot.message_handler(func=lambda m: m.text == BTN_RESET)
def btn_restart(message):
    uid = message.from_user.id
    ok, wait = can_reset_free_tokens(uid)

    if not ok:
        safe_send_message(message.chat.id,
            f"⏳ Бесплатные токены можно сбрасывать раз в *{FREE_RESET_COOLDOWN_DAYS}* дня.\n\n"
            f"Попробуй снова через: *{format_timedelta_ru(wait)}*",
            reply_markup=get_main_keyboard())
        return

    # Показываем текущий баланс и предупреждение ЧТО именно сбросится
    d    = get_user_data(uid)
    free = d["free_tokens"]
    paid = d["paid_tokens"]

    lines = [
        f"🔄 *Сброс бесплатных токенов*\n",
        f"Текущий баланс:",
        f"  🎁 Бесплатные: *{free}* {TOKEN_EMOJI}",
    ]
    if paid > 0:
        lines.append(f"  💳 Платные: *{paid}* {TOKEN_EMOJI}")
    lines += [
        f"\nПосле сброса:",
        f"  🎁 Бесплатные станут: *{FREE_TOKENS}* {TOKEN_EMOJI}",
    ]
    if paid > 0:
        lines.append(f"  💳 Платные останутся: *{paid}* {TOKEN_EMOJI} _(не изменятся)_")

    # Inline-кнопка подтверждения
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        f"✅ Да, сбросить бесплатные",
        callback_data="confirm_reset_free"
    ))
    kb.add(types.InlineKeyboardButton(
        "❌ Отмена",
        callback_data="cancel_reset"
    ))

    safe_send_message(message.chat.id,
        "\n".join(lines),
        reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == BTN_BALANCE)
def btn_balance(message):
    safe_send_message(message.chat.id,
        format_balance_text(message.from_user.id),
        reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda m: m.text == BTN_AI)
def btn_text_models(message):
    uid = message.from_user.id
    clear_image_state(uid); clear_video_state(uid)
    d   = get_user_data(uid)
    cfg = TEXT_MODELS_CONFIG.get(d["model"], {})
    safe_send_message(message.chat.id,
        f"🧠 *Текстовый режим*\n\n"
        f"Текущая: *{cfg.get('emoji','🤖')} {cfg.get('name', d['model'])}*  "
        f"— {cfg.get('cost',1)} {TOKEN_EMOJI}\n"
        f"{balance_line(uid)}\n\n"
        f"Выбери модель и задай вопрос:",
        reply_markup=get_text_models_keyboard())


@bot.message_handler(func=lambda m: m.text == BTN_PHOTO)
def btn_photo_models(message):
    """
    Кнопка 🖼 Фото модели — показывает список всех моделей изображений.
    Пользователь выбирает модель inline-кнопкой, после чего
    включается image_mode и он может слать текст / фото.
    """
    uid = message.from_user.id
    clear_video_state(uid)
    set_image_mode(uid, True)
    d   = get_user_data(uid)
    safe_send_message(message.chat.id,
        f"🖼 *Выбери модель изображений:*\n\n"
        f"Текущая: *{IMAGE_MODELS.get(d['image_model'], d['image_model'])}*\n"
        f"{balance_line(uid)}\n\n"
        f"После выбора модели отправь:\n"
        f"✍️ Текст — генерация изображения\n"
        f"📸 Фото *с подписью* — редактирование",
        reply_markup=get_image_models_keyboard(current_model_id=d["image_model"]))


@bot.message_handler(func=lambda m: m.text == BTN_VIDEO)
def btn_video_models(message):
    """
    Кнопка 🎬 Видео модели — показывает список всех моделей видео.
    Пользователь выбирает модель inline-кнопкой, после чего
    появляются настройки длительности / соотношения сторон.
    """
    uid = message.from_user.id
    clear_image_state(uid)
    set_video_mode(uid, True)
    d   = get_user_data(uid)
    safe_send_message(message.chat.id,
        f"🎬 *Выбери модель видео:*\n\n"
        f"Текущая: *{VIDEO_MODELS.get(d['video_model'], d['video_model'])}*\n"
        f"{balance_line(uid)}",
        reply_markup=get_video_models_keyboard(current_model_id=d["video_model"]))


@bot.message_handler(func=lambda m: m.text == BTN_TOPUP, content_types=['text'])
def btn_payments(message):
    safesendmessage(
        message.chat.id,
        f"{balance_line(message.from_user.id)}\n\nВыберите пакет токенов:",
        reply_markup=get_payments_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == BTN_SUPPORT)
def btn_support(message):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Перейти в поддержку",
                                       url="https://t.me/ai_patriot_support"))
    safe_send_message(message.chat.id,
        '🛟 <b>Поддержка</b>\n'
        '<a href="https://t.me/ai_patriot_support">@ai_patriot_support</a>',
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)


# ============================================================
# CALLBACKS
# ============================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("model:"))
def callback_text_model(call):
    mid = call.data.split(":", 1)[1]
    if mid not in TEXT_MODELS_CONFIG:
        bot.answer_callback_query(call.id, "Неизвестная модель"); return
    cfg = TEXT_MODELS_CONFIG[mid]
    set_user_model(call.from_user.id, mid)
    clear_chat_history(call.from_user.id)
    bot.answer_callback_query(call.id, f"Выбрана: {cfg['name']}")
    safe_edit_message(call.message.chat.id, call.message.message_id,
        f"{cfg.get('emoji','🤖')} *{cfg['name']}*\n"
        f"{cfg.get('description','')}\n\n"
        f"Стоимость: *{cfg['cost']}* {TOKEN_EMOJI} за запрос\n"
        f"{balance_line(call.from_user.id)}\n\n"
        f"История очищена. Задай вопрос — бот ответит.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("imgmodel:"))
def callback_image_model(call):
    """Пользователь выбрал модель изображений."""
    mid = call.data.split(":", 1)[1]
    if mid not in IMAGE_MODELS_CONFIG:
        bot.answer_callback_query(call.id, "Неизвестная модель"); return
    uid = call.from_user.id
    cfg = IMAGE_MODELS_CONFIG[mid]
    set_image_model(uid, mid)
    set_image_mode(uid, True)
    bot.answer_callback_query(call.id, f"Выбрана: {cfg['name']}")
    safe_edit_message(call.message.chat.id, call.message.message_id,
        f"{cfg['emoji']} *{cfg['name']}*\n"
        f"{cfg.get('description','')}\n\n"
        f"Генерация по тексту: *{cfg['cost_text']}* {TOKEN_EMOJI}\n"
        f"Редактирование фото: *{cfg['cost_photo']}* {TOKEN_EMOJI}\n"
        f"{balance_line(uid)}\n\n"
        f"✍️ Отправь текст — сгенерирую изображение.\n"
        f"📸 Отправь фото *с подписью* — отредактирую.",
        reply_markup=get_image_models_keyboard(current_model_id=mid))


@bot.callback_query_handler(func=lambda c: c.data.startswith("videomodel:"))
def callback_video_model(call):
    """Пользователь выбрал модель видео — показываем настройки."""
    mid = call.data.split(":", 1)[1]
    if mid not in VIDEO_MODELS_CONFIG:
        bot.answer_callback_query(call.id, "Неизвестная модель"); return
    uid = call.from_user.id
    cfg = VIDEO_MODELS_CONFIG[mid]
    set_video_model(uid, mid)
    set_video_mode(uid, True)
    # Сбросим длительность до дефолтной для новой модели
    set_video_duration(uid, DEFAULT_VIDEO_DURATION)
    set_video_aspect_ratio(uid, DEFAULT_VIDEO_ASPECT_RATIO)
    bot.answer_callback_query(call.id, f"Выбрана: {cfg['name']}")
    safe_edit_message(call.message.chat.id, call.message.message_id,
        f"{cfg['emoji']} *{cfg['name']}*\n"
        f"{cfg.get('description','')}\n\n"
        f"{balance_line(uid)}\n\n"
        f"Выбери длительность и формат, затем нажми *Начать*:",
        reply_markup=get_video_settings_keyboard(uid))


@bot.callback_query_handler(func=lambda c: c.data.startswith("videoduration:"))
def callback_video_duration(call):
    try: dur = int(call.data.split(":", 1)[1])
    except ValueError: bot.answer_callback_query(call.id); return
    if dur not in (5, 10): bot.answer_callback_query(call.id); return
    set_video_duration(call.from_user.id, dur)
    bot.answer_callback_query(call.id, f"{dur}с")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
            reply_markup=get_video_settings_keyboard(call.from_user.id))
    except Exception: pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("videoaspect:"))
def callback_video_aspect(call):
    ar = call.data.split(":", 1)[1]
    if ar not in ("16:9","9:16","1:1"):
        bot.answer_callback_query(call.id); return
    set_video_aspect_ratio(call.from_user.id, ar)
    bot.answer_callback_query(call.id, ar)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
            reply_markup=get_video_settings_keyboard(call.from_user.id))
    except Exception: pass


@bot.callback_query_handler(func=lambda c: c.data == "videostart:go")
def callback_video_start(call):
    """Пользователь нажал 'Начать генерацию' — показываем инструкцию."""
    uid = call.from_user.id
    d   = get_user_data(uid)
    cfg = VIDEO_MODELS_CONFIG.get(d["video_model"], {})
    cost = get_video_cost(d["video_model"], d["video_duration"])
    bot.answer_callback_query(call.id, "Готово!")
    safe_edit_message(call.message.chat.id, call.message.message_id,
        f"{cfg.get('emoji','🎬')} *{cfg.get('name', d['video_model'])}*\n"
        f"⏱ Длительность: *{d['video_duration']}с*  📐 Формат: *{d['video_aspect_ratio']}*\n"
        f"Стоимость: *{cost}* {TOKEN_EMOJI}\n"
        f"{balance_line(uid)}\n\n"
        f"Теперь отправь:\n"
        f"✍️ Текст — text-to-video\n"
        f"📸 Фото *с подписью* — image-to-video",
        reply_markup=get_video_settings_keyboard(uid))


@bot.callback_query_handler(func=lambda c: c.data.startswith("payplan_"))
def callback_payplan(call):
    userid = call.from_user.id
    raw_data = call.data or ""
    plankey = raw_data[len("payplan_"):].strip()

    logger.info("payplan callback: raw=%r plankey=%r available=%s",
                raw_data, plankey, list(PAY_PLANS.keys()))

    if plankey not in PAY_PLANS:
        bot.answer_callback_query(call.id, "Неверный тариф")
        return

    bot.answer_callback_query(call.id, "⏳ Создаём платёж...")

    payment_id, confirmation_url = create_yookassa_payment(userid, plankey)
    if not payment_id or not confirmation_url:
        safe_edit_message(
            call.message.chat.id,
            call.message.message_id,
            "❌ Не удалось создать платёж. Попробуйте позже."
        )
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💳 Оплатить", url=confirmation_url))

    safe_edit_message(
        call.message.chat.id,
        call.message.message_id,
        f"*{PAY_PLANS[plankey]['label']}* — {PAY_PLANS[plankey]['amount']} ₽\n\nНажми кнопку ниже для оплаты:"
    )
    safe_send_message(
        call.message.chat.id,
        "👇 Перейди по кнопке для оплаты:",
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda c: c.data == "confirm_reset_free")
def callback_confirm_reset(call):
    uid = call.from_user.id
    ok, wait = can_reset_free_tokens(uid)

    if not ok:
        bot.answer_callback_query(call.id, "Сброс пока недоступен")
        safe_edit_message(call.message.chat.id, call.message.message_id,
            f"⏳ Сброс недоступен.\nПопробуй через: *{format_timedelta_ru(wait)}*")
        return

    # Запоминаем платные ДО сброса
    d_before    = get_user_data(uid)
    paid_before = d_before["paid_tokens"]

    reset_free_tokens(uid)
    bot.answer_callback_query(call.id, "✅ Бесплатные токены восстановлены!")

    d_after = get_user_data(uid)
    total   = d_after["free_tokens"] + d_after["paid_tokens"]

    lines = [
        f"✅ *Бесплатные токены восстановлены!*\n",
        f"🎁 Бесплатные: *{FREE_TOKENS}* {TOKEN_EMOJI}",
    ]
    if paid_before > 0:
        lines.append(
            f"💳 Платные: *{paid_before}* {TOKEN_EMOJI} _(не изменились)_"
        )
    lines.append(f"\n💰 Итоговый баланс: *{total}* {TOKEN_EMOJI}")

    safe_edit_message(call.message.chat.id, call.message.message_id,
        "\n".join(lines))

    # Отправляем клавиатуру отдельным сообщением
    safe_send_message(call.message.chat.id,
        "Выбери действие:",
        reply_markup=get_main_keyboard())


@bot.callback_query_handler(func=lambda c: c.data == "cancel_reset")
def callback_cancel_reset(call):
    bot.answer_callback_query(call.id, "Отменено")
    safe_edit_message(call.message.chat.id, call.message.message_id,
        "❌ Сброс отменён.")
    safe_send_message(call.message.chat.id,
        "Выбери действие:",
        reply_markup=get_main_keyboard())


# ============================================================
# ROUTING — входящие сообщения
# ============================================================
def _check_rate(uid, chat_id) -> bool:
    if not rate_limit_ok(uid):
        try: safe_send_message(chat_id, "⏳ Слишком много запросов. Подожди минуту.")
        except Exception: pass
        return False
    return True


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    logger.info("🖼 photo: user=%s", message.from_user.id)
    uid = message.from_user.id
    if not _check_rate(uid, message.chat.id): return
    d   = get_user_data(uid)
    if d["video_mode"]:
        submit_task(submit_video_job, message); return
    if d["image_mode"]:
        submit_task(process_nano_request, message); return
    safe_send_message(message.chat.id,
        f"Сначала выбери режим:\n{BTN_PHOTO} — изображения\n{BTN_VIDEO} — видео",
        reply_markup=get_main_keyboard())


@bot.message_handler(
    func=lambda m: bool(m.text) and not m.text.startswith("/") and m.text not in MENU_BUTTONS,
    content_types=["text"]
)
def handle_text(message):
    logger.info("💬 text: user=%s text=%r",
                message.from_user.id, (message.text or "")[:50])
    if not message.text or message.text.startswith("/"): return
    uid = message.from_user.id
    if not _check_rate(uid, message.chat.id): return
    d   = get_user_data(uid)
    if d["video_mode"]:
        submit_task(submit_video_job, message); return
    if d["image_mode"]:
        submit_task(process_nano_request, message); return
    submit_task(process_text_question, message)


# ============================================================
# FLASK WEBHOOK
# ============================================================
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    if request.headers.get("content-type") != "application/json":
        logger.warning("Webhook: wrong content-type")
        abort(403)
    try:
        raw    = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(raw)
        if update is None:
            logger.warning("Webhook: de_json=None")
            return "", 200
        logger.info("📨 update_id=%s", update.update_id)
        bot.process_new_updates([update])
        logger.info("✅ processed update_id=%s", update.update_id)
    except Exception:
        logger.exception("❌ Webhook error")
    return "", 200


@app.route("/yookassa/webhook", methods=["POST"])
def yookassa_webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        obj  = data.get("object", {})
        pid  = obj.get("id")
        if pid and obj.get("status") == "succeeded":
            executor.submit(apply_payment_if_needed, pid)
        return "", 200
    except Exception as e:
        logger.exception("yk webhook: %s", e)
        return "", 200


@app.route("/healthz", methods=["GET"])
def health():
    return {"ok": True, "ts": int(time.time())}, 200


@app.route("/", methods=["GET"])
def index():
    return "Bot is running"

def setup_webhook():
    if RENDER_EXTERNAL_HOSTNAME:
        url = f"https://{RENDER_EXTERNAL_HOSTNAME}/{TELEGRAM_TOKEN}"
        try:
            bot.remove_webhook(); time.sleep(1)
            bot.set_webhook(url=url, max_connections=100, drop_pending_updates=False)
            logger.info("Webhook: %s", url)
        except Exception as e:
            logger.exception("webhook set: %s", e)


# ============================================================
# BACKGROUND WORKERS
# ============================================================
def cleanup_old_files_loop():
    while True:
        try:
            now    = time.time()
            cutoff = now - 2 * 60 * 60  # 2 часа
            for folder in (GENERATED_DIR, GENERATED_VIDEOS_DIR):
                try:
                    for name in os.listdir(folder):
                        path = os.path.join(folder, name)
                        try:
                            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                                os.remove(path)
                                logger.info("🧹 Removed: %s", path)
                        except Exception as e:
                            logger.warning("cleanup item %s: %s", path, e)
                except Exception as e:
                    logger.warning("cleanup folder %s: %s", folder, e)
            cleanup_stale_locks()
        except Exception as e:
            logger.exception("cleanup loop: %s", e)
        time.sleep(30 * 60)


def start_background_workers():
    threading.Thread(target=video_poller_loop,    name="video-poller",  daemon=True).start()
    threading.Thread(target=cleanup_old_files_loop, name="file-cleaner", daemon=True).start()


# ============================================================
# STARTUP
# ============================================================
_initialized = False
_init_lock   = threading.Lock()


def log_registered_handlers():
    try:
        logger.info("📋 Handlers: %d message, %d callback",
                    len(bot.message_handlers), len(bot.callback_query_handlers))
    except Exception as e:
        logger.warning("log_registered_handlers: %s", e)


def _initialize_once():
    global _initialized
    with _init_lock:
        if _initialized:
            logger.info("⏭  Already initialized"); return
        try:
            logger.info("🔧 DB init...")
            init_db()
            logger.info("🔧 Starting workers...")
            start_background_workers()
            log_registered_handlers()
            logger.info("🔧 Webhook setup...")
            setup_webhook()
            _initialized = True
            logger.info("✅ Ready (PID=%s)", os.getpid())
        except Exception as e:
            logger.exception("❌ Init failed: %s", e)
            raise


_initialize_once()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)