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

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")
app = Flask(__name__)

DB_PATH = "bot.db"
FREE_TOKENS = 40
TOKEN_EMOJI = "🍼"
GENERATED_DIR = "generated_images"

os.makedirs(GENERATED_DIR, exist_ok=True)

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
    "google/gemini-3.1-flash-image-preview": "🍌 Nano Banana 2",
    "google/gemini-3-pro-image-preview": "🍌 Nano Banana Pro"
}

PROMPT_ONLY_COSTS = {
    "google/gemini-3.1-flash-image-preview": 6,
    "google/gemini-3-pro-image-preview": 10
}

PHOTO_PROMPT_COSTS = {
    "google/gemini-3.1-flash-image-preview": 8,
    "google/gemini-3-pro-image-preview": 12
}

DEFAULT_MODEL = "google/gemini-3-flash-preview"
DEFAULT_IMAGE_MODEL = "google/gemini-3.1-flash-image-preview"

PAY_PLANS = {
    "small": {"label": f"800 {TOKEN_EMOJI}", "amount": 250, "tokens": 800},
    "medium": {"label": f"1800 {TOKEN_EMOJI}", "amount": 400, "tokens": 1800},
    "large": {"label": f"4000 {TOKEN_EMOJI}", "amount": 750, "tokens": 4000}
}


# =========================
# DB
# =========================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            model TEXT NOT NULL DEFAULT 'google/gemini-3-flash-preview',
            free_tokens INTEGER NOT NULL DEFAULT 40,
            paid_tokens INTEGER NOT NULL DEFAULT 0,
            image_mode INTEGER NOT NULL DEFAULT 0,
            image_model TEXT NOT NULL DEFAULT 'google/gemini-3.1-flash-image-preview',
            image_flow TEXT DEFAULT '',
            pending_image_prompt TEXT DEFAULT ''
        )
    """)

    conn.commit()

    existing_columns = [row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()]

    if "image_mode" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN image_mode INTEGER NOT NULL DEFAULT 0")
    if "image_model" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN image_model TEXT NOT NULL DEFAULT 'google/gemini-3.1-flash-image-preview'")
    if "image_flow" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN image_flow TEXT DEFAULT ''")
    if "pending_image_prompt" not in existing_columns:
        cur.execute("ALTER TABLE users ADD COLUMN pending_image_prompt TEXT DEFAULT ''")

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

    conn.commit()
    conn.close()


def ensure_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if not row:
        cur.execute("""
            INSERT INTO users (
                user_id, model, free_tokens, paid_tokens,
                image_mode, image_model, image_flow, pending_image_prompt
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, DEFAULT_MODEL, FREE_TOKENS, 0, 0, DEFAULT_IMAGE_MODEL, "", ""))
        conn.commit()

    conn.close()


def get_user_data(user_id: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT model, free_tokens, paid_tokens, image_mode, image_model, image_flow, pending_image_prompt
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

    return {
        "model": model,
        "free_tokens": row[1],
        "paid_tokens": row[2],
        "image_mode": bool(row[3]),
        "image_model": image_model,
        "image_flow": row[5] or "",
        "pending_image_prompt": row[6] or ""
    }


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


def reset_free_tokens(user_id: int):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET free_tokens = ? WHERE user_id = ?", (FREE_TOKENS, user_id))
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


def consume_tokens(user_id: int, cost: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT free_tokens, paid_tokens
        FROM users
        WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()

    if not row:
        conn.close()
        return False, None, cost

    free_tokens, paid_tokens = row
    total = free_tokens + paid_tokens

    if total < cost:
        conn.close()
        return False, "limit", cost

    if paid_tokens >= cost:
        cur.execute("""
            UPDATE users
            SET paid_tokens = paid_tokens - ?
            WHERE user_id = ?
        """, (cost, user_id))
        conn.commit()
        conn.close()
        return True, "paid", cost

    if paid_tokens > 0:
        remaining_cost = cost - paid_tokens
        cur.execute("""
            UPDATE users
            SET paid_tokens = 0,
                free_tokens = free_tokens - ?
            WHERE user_id = ?
        """, (remaining_cost, user_id))
        conn.commit()
        conn.close()
        return True, "mixed", cost

    cur.execute("""
        UPDATE users
        SET free_tokens = free_tokens - ?
        WHERE user_id = ?
    """, (cost, user_id))
    conn.commit()
    conn.close()
    return True, "free", cost


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


# =========================
# UI
# =========================
def get_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🧠 Спросить", "⚙️ Модели")
    kb.row("🍌 Nano Banana", "📊 Баланс")
    kb.row("💳 Купить", "🔄 Сброс")
    kb.row("❌ Выйти из режима")
    return kb


def get_models_keyboard():
    kb = types.InlineKeyboardMarkup()
    for model_id, model_name in TEXT_MODELS.items():
        cost = TEXT_MODEL_COSTS.get(model_id, 1)
        kb.add(types.InlineKeyboardButton(
            f"{model_name} — {cost} {TOKEN_EMOJI}",
            callback_data=f"model:{model_id}"
        ))
    return kb


def get_nano_models_keyboard():
    kb = types.InlineKeyboardMarkup()
    for model_id, model_name in IMAGE_MODELS.items():
        prompt_cost = PROMPT_ONLY_COSTS.get(model_id, 1)
        photo_cost = PHOTO_PROMPT_COSTS.get(model_id, 1)
        kb.add(types.InlineKeyboardButton(
            f"{model_name} — {prompt_cost}/{photo_cost} {TOKEN_EMOJI}",
            callback_data=f"imgmodel:{model_id}"
        ))
    return kb


def get_nano_actions_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🎨 По промту", callback_data="imgflow:prompt_only"))
    kb.add(types.InlineKeyboardButton("🖼 По фото + промту", callback_data="imgflow:photo_plus_prompt"))
    return kb


def get_payments_keyboard():
    kb = types.InlineKeyboardMarkup()
    for plan_key, plan in PAY_PLANS.items():
        kb.add(types.InlineKeyboardButton(
            f"{plan['label']} — {plan['amount']} ₽",
            callback_data=f"payplan:{plan_key}"
        ))
    return kb


# =========================
# Helpers
# =========================
def format_balance_text(user_id: int):
    data = get_user_data(user_id)
    free_tokens = data["free_tokens"]
    paid_tokens = data["paid_tokens"]
    total_tokens = free_tokens + paid_tokens

    return (
        "📊 *Твой баланс*\n\n"
        f"🆓 Бесплатных: *{free_tokens}* {TOKEN_EMOJI}\n"
        f"💳 Платных: *{paid_tokens}* {TOKEN_EMOJI}\n"
        f"📦 Всего: *{total_tokens}* {TOKEN_EMOJI}"
    )


def safe_edit_message(chat_id, message_id, text, reply_markup=None):
    try:
        bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.warning("Не удалось отредактировать сообщение: %s", e)
        bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="Markdown")


def get_image_cost(model: str, flow: str):
    if flow == "prompt_only":
        return PROMPT_ONLY_COSTS.get(model, 1)
    if flow == "photo_plus_prompt":
        return PHOTO_PROMPT_COSTS.get(model, 1)
    return 1


def extract_data_url_parts(data_url: str):
    match = re.match(r"^data:(image\/[a-zA-Z0-9.+-]+);base64,(.+)$", data_url, re.DOTALL)
    if not match:
        return None, None
    mime_type = match.group(1)
    b64_data = match.group(2)
    return mime_type, b64_data


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
    with open(file_path, "rb") as f:
        bot.send_photo(chat_id, photo=f, caption=caption_preview)

    with open(file_path, "rb") as f:
        bot.send_document(chat_id, document=f, caption=caption_file)


# =========================
# OpenRouter text
# =========================
def call_openrouter_text(model: str, message: str, max_retries: int = 3):
    url = "https://openrouter.ai/api/v1/chat/completions"
    fallback_model = DEFAULT_MODEL

    data = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Отвечай кратко, по делу, на русском языке."},
            {"role": "user", "content": message}
        ],
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
        return call_openrouter_text(fallback_model, message, max_retries=1) + " ⚡"

    return "⚠️ Не удалось получить ответ от AI. Попробуй ещё раз позже."


# =========================
# OpenRouter image generation
# =========================
def generate_image_openrouter(model: str, prompt_text: str, input_image_data_url: str | None = None, max_retries: int = 2):
    url = "https://openrouter.ai/api/v1/chat/completions"

    content_parts = [{"type": "text", "text": prompt_text.strip()}]
    if input_image_data_url:
        content_parts.append({"type": "image_url", "image_url": {"url": input_image_data_url}})

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": content_parts
            }
        ],
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
                        return {
                            "ok": True,
                            "image_data_url": data_url,
                            "text": content_text
                        }

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

    return {
        "ok": False,
        "error": "⚠️ Модель не вернула изображение. Попробуй другой промт или другую модель."
    }


# =========================
# YooKassa
# =========================
def create_yookassa_payment(user_id: int, plan_key: str):
    if not YOOKASSA_ENABLED:
        logger.warning("YooKassa не настроена")
        return None, None

    if plan_key not in PAY_PLANS:
        return None, None

    plan = PAY_PLANS[plan_key]
    idempotence_key = str(uuid.uuid4())

    payload = {
        "amount": {
            "value": f"{plan['amount']}.00",
            "currency": "RUB"
        },
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

    user_data = get_user_data(payment["user_id"])
    total_tokens = user_data["free_tokens"] + user_data["paid_tokens"]

    try:
        bot.send_message(
            payment["user_id"],
            f"✅ Оплата прошла успешно!\n\n"
            f"Пакет: *{PAY_PLANS[payment['plan_key']]['label']}*\n"
            f"Начислено: *{payment['tokens_count']}* {TOKEN_EMOJI}\n"
            f"📦 Всего на балансе: *{total_tokens}* {TOKEN_EMOJI}",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logger.warning("Не удалось отправить сообщение об оплате: %s", e)

    logger.info("Платеж %s успешно применен", payment_id)
    return True


# =========================
# Processing text
# =========================
def process_text_question(message):
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    model = user_data["model"]

    if model not in TEXT_MODELS:
        model = DEFAULT_MODEL
        set_user_model(user_id, model)
        user_data = get_user_data(user_id)

    model_name = TEXT_MODELS.get(model, model)
    model_cost = TEXT_MODEL_COSTS.get(model, 1)

    total_tokens = user_data["free_tokens"] + user_data["paid_tokens"]
    if total_tokens < model_cost:
        bot.send_message(
            message.chat.id,
            f"❌ Недостаточно {TOKEN_EMOJI} для модели *{model_name}*.\n\n"
            f"Стоимость запроса: *{model_cost}* {TOKEN_EMOJI}\n"
            f"Попробуй пополнить баланс или выбрать более дешевую модель.",
            reply_markup=get_main_keyboard()
        )
        return

    msg = bot.send_message(message.chat.id, "🤖 Думаю... ⏳")
    bot.send_chat_action(message.chat.id, "typing")

    answer = call_openrouter_text(model, message.text)

    success, source, charged = consume_tokens(user_id, model_cost)
    if not success:
        safe_edit_message(
            message.chat.id,
            msg.message_id,
            f"❌ Не удалось списать {TOKEN_EMOJI}. Попробуй ещё раз.",
            reply_markup=get_main_keyboard()
        )
        return

    updated = get_user_data(user_id)
    total_tokens = updated["free_tokens"] + updated["paid_tokens"]

    if source == "paid":
        source_text = "💳 Списано из платных"
    elif source == "free":
        source_text = "🆓 Списано из бесплатных"
    else:
        source_text = "🪙 Списано из общего баланса"

    safe_edit_message(
        message.chat.id,
        msg.message_id,
        f"🤖 *{model_name}*\n\n"
        f"{answer}\n\n"
        f"{source_text}\n"
        f"💸 Списано: *{charged}* {TOKEN_EMOJI}\n"
        f"📦 Осталось: *{total_tokens}* {TOKEN_EMOJI}",
        reply_markup=get_main_keyboard()
    )


# =========================
# Processing Nano Banana
# =========================
def process_nano_prompt_only(message):
    user_id = message.from_user.id
    data = get_user_data(user_id)
    model = data["image_model"]
    model_name = IMAGE_MODELS.get(model, model)
    flow = "prompt_only"
    cost = get_image_cost(model, flow)
    total_tokens = data["free_tokens"] + data["paid_tokens"]

    if total_tokens < cost:
        bot.send_message(
            message.chat.id,
            f"❌ Недостаточно {TOKEN_EMOJI} для *{model_name}*.\n\n"
            f"Стоимость генерации: *{cost}* {TOKEN_EMOJI}",
            reply_markup=get_main_keyboard()
        )
        return

    prompt_text = (message.text or "").strip()
    if not prompt_text:
        bot.send_message(message.chat.id, "Напиши промт одним сообщением.", reply_markup=get_main_keyboard())
        return

    wait_msg = bot.send_message(
        message.chat.id,
        f"🍌 Генерирую изображение через *{model_name}*...\n"
        f"Режим: *по промту*"
    )

    result = generate_image_openrouter(model=model, prompt_text=prompt_text)

    if not result["ok"]:
        safe_edit_message(
            message.chat.id,
            wait_msg.message_id,
            result["error"],
            reply_markup=get_main_keyboard()
        )
        return

    success, source, charged = consume_tokens(user_id, cost)
    if not success:
        safe_edit_message(
            message.chat.id,
            wait_msg.message_id,
            f"❌ Не удалось списать {TOKEN_EMOJI} после генерации.",
            reply_markup=get_main_keyboard()
        )
        return

    file_path = save_generated_image_from_data_url(result["image_data_url"], prefix="prompt")
    if not file_path:
        safe_edit_message(
            message.chat.id,
            wait_msg.message_id,
            "❌ Не удалось сохранить сгенерированное изображение.",
            reply_markup=get_main_keyboard()
        )
        return

    updated = get_user_data(user_id)
    total_left = updated["free_tokens"] + updated["paid_tokens"]

    safe_edit_message(
        message.chat.id,
        wait_msg.message_id,
        f"✅ Изображение готово.\n"
        f"💸 Списано: *{charged}* {TOKEN_EMOJI}\n"
        f"📦 Осталось: *{total_left}* {TOKEN_EMOJI}\n\n"
        f"Ниже отправляю превью и оригинал файлом.",
        reply_markup=get_main_keyboard()
    )

    send_generated_image_both(
        chat_id=message.chat.id,
        file_path=file_path,
        caption_preview=f"🍌 {model_name}\n🎨 Генерация по промту",
        caption_file="📎 Оригинал без сжатия"
    )


def process_nano_photo_plus_prompt(message):
    user_id = message.from_user.id
    data = get_user_data(user_id)
    model = data["image_model"]
    model_name = IMAGE_MODELS.get(model, model)
    flow = "photo_plus_prompt"
    cost = get_image_cost(model, flow)
    total_tokens = data["free_tokens"] + data["paid_tokens"]

    if total_tokens < cost:
        bot.send_message(
            message.chat.id,
            f"❌ Недостаточно {TOKEN_EMOJI} для *{model_name}*.\n\n"
            f"Стоимость генерации: *{cost}* {TOKEN_EMOJI}",
            reply_markup=get_main_keyboard()
        )
        return

    prompt_text = (message.caption or "").strip()
    if not prompt_text:
        bot.send_message(
            message.chat.id,
            "🖼 Отправь фото *с подписью*, что нужно изменить или сгенерировать.",
            reply_markup=get_main_keyboard()
        )
        return

    wait_msg = bot.send_message(
        message.chat.id,
        f"🍌 Генерирую изображение через *{model_name}*...\n"
        f"Режим: *по фото + промту*"
    )

    try:
        input_image_data_url = telegram_photo_to_data_url(message)
    except Exception as e:
        logger.exception("Ошибка получения фото из Telegram: %s", e)
        safe_edit_message(
            message.chat.id,
            wait_msg.message_id,
            "❌ Не удалось скачать фото из Telegram.",
            reply_markup=get_main_keyboard()
        )
        return

    result = generate_image_openrouter(
        model=model,
        prompt_text=prompt_text,
        input_image_data_url=input_image_data_url
    )

    if not result["ok"]:
        safe_edit_message(
            message.chat.id,
            wait_msg.message_id,
            result["error"],
            reply_markup=get_main_keyboard()
        )
        return

    success, source, charged = consume_tokens(user_id, cost)
    if not success:
        safe_edit_message(
            message.chat.id,
            wait_msg.message_id,
            f"❌ Не удалось списать {TOKEN_EMOJI} после генерации.",
            reply_markup=get_main_keyboard()
        )
        return

    file_path = save_generated_image_from_data_url(result["image_data_url"], prefix="photo")
    if not file_path:
        safe_edit_message(
            message.chat.id,
            wait_msg.message_id,
            "❌ Не удалось сохранить сгенерированное изображение.",
            reply_markup=get_main_keyboard()
        )
        return

    updated = get_user_data(user_id)
    total_left = updated["free_tokens"] + updated["paid_tokens"]

    safe_edit_message(
        message.chat.id,
        wait_msg.message_id,
        f"✅ Изображение готово.\n"
        f"💸 Списано: *{charged}* {TOKEN_EMOJI}\n"
        f"📦 Осталось: *{total_left}* {TOKEN_EMOJI}\n\n"
        f"Ниже отправляю превью и оригинал файлом.",
        reply_markup=get_main_keyboard()
    )

    send_generated_image_both(
        chat_id=message.chat.id,
        file_path=file_path,
        caption_preview=f"🍌 {model_name}\n🖼 Генерация по фото + промту",
        caption_file="📎 Оригинал без сжатия"
    )


# =========================
# Telegram handlers
# =========================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    ensure_user(message.from_user.id)
    payment_text = (
        f"💳 Покупка {TOKEN_EMOJI} уже доступна."
        if YOOKASSA_ENABLED
        else f"💳 Пополнение скоро будет доступно."
    )

    bot.send_message(
        message.chat.id,
        "Привет! Я AI-бот 🤖\n\n"
        "Что я умею:\n"
        "• отвечать на вопросы\n"
        "• генерировать изображения через Nano Banana\n"
        "• менять модели\n\n"
        "Быстрый старт:\n"
        "• *🧠 Спросить* — обычный чат\n"
        "• *🍌 Nano Banana* — генерация изображений\n"
        "• *⚙️ Модели* — смена текстовой модели\n\n"
        f"{payment_text}",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(commands=["restart"])
def cmd_restart(message):
    reset_free_tokens(message.from_user.id)
    bot.send_message(
        message.chat.id,
        f"🔄 Бесплатные {TOKEN_EMOJI} сброшены до *{FREE_TOKENS}*.\n"
        f"Платные {TOKEN_EMOJI} не изменены.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "🔄 Сброс")
def btn_restart(message):
    reset_free_tokens(message.from_user.id)
    bot.send_message(
        message.chat.id,
        f"🔄 Бесплатные {TOKEN_EMOJI} сброшены до *{FREE_TOKENS}*.\n"
        f"Платные {TOKEN_EMOJI} сохранены.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "📊 Баланс")
def btn_balance(message):
    bot.send_message(
        message.chat.id,
        format_balance_text(message.from_user.id),
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "⚙️ Модели")
def btn_model(message):
    data = get_user_data(message.from_user.id)
    current_name = TEXT_MODELS.get(data["model"], data["model"])
    current_cost = TEXT_MODEL_COSTS.get(data["model"], 1)

    bot.send_message(
        message.chat.id,
        f"⚙️ Текущая модель: *{current_name}*\n"
        f"Стоимость запроса: *{current_cost}* {TOKEN_EMOJI}\n\n"
        f"Выбери другую модель:",
        reply_markup=get_models_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "🍌 Nano Banana")
def btn_nano_banana(message):
    set_image_mode(message.from_user.id, True)
    set_image_flow(message.from_user.id, "")
    set_pending_image_prompt(message.from_user.id, "")

    data = get_user_data(message.from_user.id)
    current_image_name = IMAGE_MODELS.get(data["image_model"], data["image_model"])

    bot.send_message(
        message.chat.id,
        f"🍌 *Nano Banana*\n\n"
        f"Текущая модель: *{current_image_name}*\n"
        f"По промту: *{PROMPT_ONLY_COSTS.get(data['image_model'], 1)}* {TOKEN_EMOJI}\n"
        f"По фото + промту: *{PHOTO_PROMPT_COSTS.get(data['image_model'], 1)}* {TOKEN_EMOJI}\n\n"
        f"Сначала выбери модель или сразу режим генерации:",
        reply_markup=get_nano_models_keyboard()
    )

    bot.send_message(
        message.chat.id,
        "Выбери сценарий:",
        reply_markup=get_nano_actions_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "❌ Выйти из режима")
def btn_exit_mode(message):
    clear_image_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "✅ Режим изображений выключен.\nТеперь бот снова работает как обычный чат.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "💳 Купить")
def btn_payments(message):
    bot.send_message(
        message.chat.id,
        "Выбери пакет пополнения:",
        reply_markup=get_payments_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "🧠 Спросить")
def btn_ai(message):
    clear_image_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "Напиши вопрос одним сообщением.",
        reply_markup=get_main_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("model:"))
def callback_model(call):
    model = call.data.split("model:", 1)[1]

    if model not in TEXT_MODELS:
        bot.answer_callback_query(call.id, "Неизвестная модель")
        return

    set_user_model(call.from_user.id, model)
    model_name = TEXT_MODELS[model]
    model_cost = TEXT_MODEL_COSTS.get(model, 1)

    bot.answer_callback_query(call.id, f"Выбрана {model_name}")
    safe_edit_message(
        call.message.chat.id,
        call.message.message_id,
        f"✅ Текстовая модель изменена на *{model_name}*\n"
        f"Стоимость запроса: *{model_cost}* {TOKEN_EMOJI}",
        reply_markup=get_main_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("imgmodel:"))
def callback_image_model(call):
    model = call.data.split("imgmodel:", 1)[1]

    if model not in IMAGE_MODELS:
        bot.answer_callback_query(call.id, "Неизвестная image-модель")
        return

    set_image_model(call.from_user.id, model)

    model_name = IMAGE_MODELS[model]
    prompt_cost = PROMPT_ONLY_COSTS.get(model, 1)
    photo_cost = PHOTO_PROMPT_COSTS.get(model, 1)

    bot.answer_callback_query(call.id, f"Выбрана {model_name}")
    safe_edit_message(
        call.message.chat.id,
        call.message.message_id,
        f"✅ Выбрана модель *{model_name}*\n"
        f"🎨 По промту: *{prompt_cost}* {TOKEN_EMOJI}\n"
        f"🖼 По фото + промту: *{photo_cost}* {TOKEN_EMOJI}",
        reply_markup=get_main_keyboard()
    )

    bot.send_message(
        call.message.chat.id,
        "Теперь выбери сценарий генерации:",
        reply_markup=get_nano_actions_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("imgflow:"))
def callback_image_flow(call):
    flow = call.data.split("imgflow:", 1)[1]
    user_id = call.from_user.id
    data = get_user_data(user_id)
    model = data["image_model"]
    model_name = IMAGE_MODELS.get(model, model)

    if flow not in {"prompt_only", "photo_plus_prompt"}:
        bot.answer_callback_query(call.id, "Неизвестный режим")
        return

    set_image_mode(user_id, True)
    set_image_flow(user_id, flow)
    set_pending_image_prompt(user_id, "")

    bot.answer_callback_query(call.id, "Режим выбран")

    if flow == "prompt_only":
        cost = get_image_cost(model, flow)
        safe_edit_message(
            call.message.chat.id,
            call.message.message_id,
            f"🎨 *{model_name}*\n"
            f"Режим: *по промту*\n"
            f"Стоимость: *{cost}* {TOKEN_EMOJI}\n\n"
            f"Теперь напиши промт одним сообщением.",
            reply_markup=get_main_keyboard()
        )
    else:
        cost = get_image_cost(model, flow)
        safe_edit_message(
            call.message.chat.id,
            call.message.message_id,
            f"🖼 *{model_name}*\n"
            f"Режим: *по фото + промту*\n"
            f"Стоимость: *{cost}* {TOKEN_EMOJI}\n\n"
            f"Теперь отправь фото с подписью, что нужно сгенерировать или изменить.",
            reply_markup=get_main_keyboard()
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("payplan:"))
def callback_payplan(call):
    plan_key = call.data.split("payplan:", 1)[1]

    if plan_key not in PAY_PLANS:
        bot.answer_callback_query(call.id, "Неизвестный пакет")
        return

    plan = PAY_PLANS[plan_key]

    if not YOOKASSA_ENABLED:
        bot.answer_callback_query(call.id, "Пакет выбран")
        bot.send_message(
            call.message.chat.id,
            f"💳 Ты выбрал пакет:\n\n"
            f"*{plan['label']}* — *{plan['amount']} ₽*\n\n"
            f"ЮKassa пока не подключена.\n"
            f"Скоро здесь появится ссылка на оплату.",
            reply_markup=get_main_keyboard()
        )
        return

    payment_id, confirmation_url = create_yookassa_payment(call.from_user.id, plan_key)

    if not payment_id or not confirmation_url:
        bot.answer_callback_query(call.id, "Ошибка создания платежа")
        bot.send_message(
            call.message.chat.id,
            "❌ Не удалось создать платеж. Попробуй позже.",
            reply_markup=get_main_keyboard()
        )
        return

    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"💳 Пакет: *{plan['label']}*\n"
        f"💵 Стоимость: *{plan['amount']} ₽*\n\n"
        f"Перейди по ссылке для оплаты:\n{confirmation_url}\n\n"
        f"После успешной оплаты {TOKEN_EMOJI} начислятся автоматически.",
        reply_markup=get_main_keyboard(),
        disable_web_page_preview=True
    )


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    data = get_user_data(message.from_user.id)

    if not data["image_mode"]:
        bot.send_message(
            message.chat.id,
            "Если хочешь работать с изображениями, сначала нажми *🍌 Nano Banana*.",
            reply_markup=get_main_keyboard()
        )
        return

    if data["image_flow"] == "photo_plus_prompt":
        process_nano_photo_plus_prompt(message)
        return

    bot.send_message(
        message.chat.id,
        "Сейчас выбран другой режим.\nЕсли хочешь генерацию по фото, выбери *🖼 По фото + промту*.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(content_types=["text"])
def handle_text(message):
    text = (message.text or "").strip()

    ignored_buttons = {
        "🧠 Спросить",
        "⚙️ Модели",
        "🍌 Nano Banana",
        "📊 Баланс",
        "💳 Купить",
        "🔄 Сброс",
        "❌ Выйти из режима"
    }

    if text in ignored_buttons:
        return

    data = get_user_data(message.from_user.id)

    if data["image_mode"] and data["image_flow"] == "prompt_only":
        process_nano_prompt_only(message)
        return

    if data["image_mode"] and data["image_flow"] == "photo_plus_prompt":
        bot.send_message(
            message.chat.id,
            "🖼 Сейчас включен режим *по фото + промту*.\nОтправь фото с подписью.",
            reply_markup=get_main_keyboard()
        )
        return

    process_text_question(message)


# =========================
# Flask routes
# =========================
@app.route("/", methods=["GET"])
def home():
    return {
        "status": "ok",
        "service": "telegram-bot",
        "telegram_webhook": True,
        "yookassa_enabled": YOOKASSA_ENABLED
    }, 200


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    if "application/json" not in (request.headers.get("content-type") or ""):
        abort(403)

    json_string = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "OK", 200


@app.route("/yookassa/webhook", methods=["POST"])
def yookassa_webhook():
    if not YOOKASSA_ENABLED:
        return "disabled", 200

    data = request.get_json(silent=True)

    if not data:
        logger.warning("Пустой webhook от YooKassa")
        return "bad request", 400

    event = data.get("event")
    obj = data.get("object", {})
    payment_id = obj.get("id")

    logger.info("YooKassa webhook event=%s payment_id=%s", event, payment_id)

    if event == "payment.succeeded" and payment_id:
        apply_payment_if_needed(payment_id)
        return "ok", 200

    if event == "payment.canceled" and payment_id:
        update_payment_status(payment_id, "canceled")
        return "ok", 200

    return "ok", 200


# =========================
# Startup
# =========================
def setup_telegram_webhook():
    if not RENDER_EXTERNAL_HOSTNAME:
        logger.warning("RENDER_EXTERNAL_HOSTNAME не найден, Telegram webhook не установлен")
        return

    webhook_url = f"https://{RENDER_EXTERNAL_HOSTNAME}/{TELEGRAM_TOKEN}"

    try:
        bot.remove_webhook()
        time.sleep(1)
        success = bot.set_webhook(url=webhook_url)

        if success:
            logger.info("Telegram webhook установлен: %s", webhook_url)
        else:
            logger.error("Не удалось установить Telegram webhook: %s", webhook_url)

    except Exception as e:
        logger.exception("Ошибка установки Telegram webhook: %s", e)


init_db()
setup_telegram_webhook()

if __name__ == "__main__":
    logger.info("🚀 Bot started on port %s", PORT)
    logger.info("YooKassa enabled: %s", YOOKASSA_ENABLED)
    app.run(host="0.0.0.0", port=PORT, debug=False)