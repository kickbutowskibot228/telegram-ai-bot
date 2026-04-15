import os
import time
import uuid
import base64
import sqlite3
import logging
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

TEXT_MODELS = {
    "openai/gpt-5.2": "GPT-5.2",
    "openai/gpt-5.4": "GPT-5.4",
    "google/gemini-3-flash-preview": "Gemini 3 Flash",
    "deepseek/deepseek-v3.2": "🐳 DeepSeek V3.2",
    "anthropic/claude-opus-4.6": "Claude Opus 4.6",
    "anthropic/claude-sonnet-4.6": "Claude Sonnet 4.6"
}

TEXT_MODEL_COSTS = {
    "deepseek/deepseek-v3.2": 1,
    "google/gemini-3-flash-preview": 2,
    "openai/gpt-5.2": 5,
    "openai/gpt-5.4": 7,
    "anthropic/claude-sonnet-4.6": 8,
    "anthropic/claude-opus-4.6": 12
}

IMAGE_MODELS = {
    "google/gemini-3.1-flash-image-preview": "🍌 Nano Banana 2",
    "google/gemini-3-pro-image-preview": "🍌 Nano Banana Pro"
}

IMAGE_MODEL_COSTS = {
    "google/gemini-3.1-flash-image-preview": 4,
    "google/gemini-3-pro-image-preview": 8
}

DEFAULT_MODEL = "google/gemini-3-flash-preview"
DEFAULT_IMAGE_MODEL = "google/gemini-3.1-flash-image-preview"

PAY_PLANS = {
    "small": {
        "label": "800 токенов",
        "amount": 250,
        "tokens": 800
    },
    "medium": {
        "label": "1800 токенов",
        "amount": 400,
        "tokens": 1800
    },
    "large": {
        "label": "4000 токенов",
        "amount": 750,
        "tokens": 4000
    }
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
            image_model TEXT NOT NULL DEFAULT 'google/gemini-3.1-flash-image-preview'
        )
    """)

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
            INSERT INTO users (user_id, model, free_tokens, paid_tokens, image_mode, image_model)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, DEFAULT_MODEL, FREE_TOKENS, 0, 0, DEFAULT_IMAGE_MODEL))
        conn.commit()

    conn.close()


def get_user_data(user_id: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT model, free_tokens, paid_tokens, image_mode, image_model
        FROM users
        WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()

    conn.close()

    return {
        "model": row[0],
        "free_tokens": row[1],
        "paid_tokens": row[2],
        "image_mode": bool(row[3]),
        "image_model": row[4]
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
    kb.row("🧠 Спросить AI", "⚙️ Модель")
    kb.row("🖼 Режим изображений", "📊 Баланс токенов")
    kb.row("💳 Купить токены", "🔄 Restart")
    kb.row("❌ Выйти из режима изображений")
    return kb


def get_models_keyboard():
    kb = types.InlineKeyboardMarkup()
    for model_id, model_name in TEXT_MODELS.items():
        cost = TEXT_MODEL_COSTS.get(model_id, 1)
        kb.add(types.InlineKeyboardButton(
            f"{model_name} — {cost} ток.",
            callback_data=f"model:{model_id}"
        ))
    return kb


def get_image_models_keyboard():
    kb = types.InlineKeyboardMarkup()
    for model_id, model_name in IMAGE_MODELS.items():
        cost = IMAGE_MODEL_COSTS.get(model_id, 1)
        kb.add(types.InlineKeyboardButton(
            f"{model_name} — {cost} ток.",
            callback_data=f"imgmodel:{model_id}"
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


# =========================
# OpenRouter
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


def telegram_photo_to_data_url(message):
    photo = message.photo[-1]
    file_info = bot.get_file(photo.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    encoded = base64.b64encode(downloaded_file).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"


def call_openrouter_image(model: str, prompt_text: str, image_data_url: str, max_retries: int = 2):
    url = "https://openrouter.ai/api/v1/chat/completions"

    final_prompt = prompt_text.strip() if prompt_text and prompt_text.strip() else "Опиши, что изображено на картинке."

    data = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": final_prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url}}
                ]
            }
        ],
        "max_tokens": 700,
        "temperature": 0.5
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
                timeout=120
            )

            if resp.status_code == 200:
                payload = resp.json()
                return payload["choices"][0]["message"]["content"]

            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue

            logger.error("OpenRouter image error %s: %s", resp.status_code, resp.text)

        except Exception as e:
            logger.exception("Ошибка image-запроса к OpenRouter: %s", e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

    return "⚠️ Не удалось обработать изображение. Попробуй ещё раз позже."


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
        "description": f"Пополнение баланса токенов AI-бота: {plan['label']}",
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
            f"Начислено: *{payment['tokens_count']}* токенов\n"
            f"💳 Платных токенов: *{user_data['paid_tokens']}*\n"
            f"📦 Всего токенов: *{total_tokens}*",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logger.warning("Не удалось отправить сообщение об оплате: %s", e)

    logger.info("Платеж %s успешно применен", payment_id)
    return True


# =========================
# Helpers
# =========================
def format_balance_text(user_id: int):
    data = get_user_data(user_id)
    free_tokens = data["free_tokens"]
    paid_tokens = data["paid_tokens"]
    total_tokens = free_tokens + paid_tokens

    lines = [
        "📊 *Твой баланс токенов*",
        "",
        f"🆓 Бесплатных токенов: *{free_tokens}*",
        f"💳 Платных токенов: *{paid_tokens}*",
        f"📦 Всего токенов: *{total_tokens}*",
        "",
        "💰 Текстовые модели:"
    ]

    for model_id, model_name in TEXT_MODELS.items():
        cost = TEXT_MODEL_COSTS.get(model_id, 1)
        lines.append(f"• {model_name}: *{cost}* ток.")

    lines.append("")
    lines.append("🖼 Модели изображений:")

    for model_id, model_name in IMAGE_MODELS.items():
        cost = IMAGE_MODEL_COSTS.get(model_id, 1)
        lines.append(f"• {model_name}: *{cost}* ток.")

    return "\n".join(lines)


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


# =========================
# Processing
# =========================
def process_text_question(message):
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    model = user_data["model"]
    model_name = TEXT_MODELS.get(model, model)
    model_cost = TEXT_MODEL_COSTS.get(model, 1)

    total_tokens = user_data["free_tokens"] + user_data["paid_tokens"]
    if total_tokens < model_cost:
        bot.send_message(
            message.chat.id,
            f"❌ Недостаточно токенов для модели *{model_name}*.\n\n"
            f"Стоимость запроса: *{model_cost}* ток.\n"
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
            "❌ Не удалось списать токены. Попробуй ещё раз.",
            reply_markup=get_main_keyboard()
        )
        return

    updated = get_user_data(user_id)
    free_tokens = updated["free_tokens"]
    paid_tokens = updated["paid_tokens"]
    total_tokens = free_tokens + paid_tokens

    if source == "paid":
        source_text = "💳 Списано из платных токенов"
    elif source == "free":
        source_text = "🆓 Списано из бесплатных токенов"
    else:
        source_text = "🪙 Списано из общего баланса токенов"

    safe_edit_message(
        message.chat.id,
        msg.message_id,
        f"🤖 *{model_name}*\n\n"
        f"{answer}\n\n"
        f"{source_text}\n"
        f"💸 Списано: *{charged}* ток.\n"
        f"🆓 Бесплатных: *{free_tokens}*\n"
        f"💳 Платных: *{paid_tokens}*\n"
        f"📦 Всего: *{total_tokens}*",
        reply_markup=get_main_keyboard()
    )


def process_image_question(message):
    user_id = message.from_user.id
    user_data = get_user_data(user_id)
    image_model = user_data["image_model"]
    model_name = IMAGE_MODELS.get(image_model, image_model)
    model_cost = IMAGE_MODEL_COSTS.get(image_model, 1)

    total_tokens = user_data["free_tokens"] + user_data["paid_tokens"]
    if total_tokens < model_cost:
        bot.send_message(
            message.chat.id,
            f"❌ Недостаточно токенов для image-модели *{model_name}*.\n\n"
            f"Стоимость запроса: *{model_cost}* ток.",
            reply_markup=get_main_keyboard()
        )
        return

    msg = bot.send_message(message.chat.id, f"🖼 Обрабатываю изображение через *{model_name}*...")

    try:
        image_data_url = telegram_photo_to_data_url(message)
    except Exception as e:
        logger.exception("Ошибка скачивания фото из Telegram: %s", e)
        safe_edit_message(
            message.chat.id,
            msg.message_id,
            "❌ Не удалось получить изображение из Telegram.",
            reply_markup=get_main_keyboard()
        )
        return

    prompt_text = message.caption or "Опиши, что изображено на картинке."

    answer = call_openrouter_image(image_model, prompt_text, image_data_url)

    success, source, charged = consume_tokens(user_id, model_cost)
    if not success:
        safe_edit_message(
            message.chat.id,
            msg.message_id,
            "❌ Не удалось списать токены после обработки изображения.",
            reply_markup=get_main_keyboard()
        )
        return

    updated = get_user_data(user_id)
    free_tokens = updated["free_tokens"]
    paid_tokens = updated["paid_tokens"]
    total_tokens = free_tokens + paid_tokens

    if source == "paid":
        source_text = "💳 Списано из платных токенов"
    elif source == "free":
        source_text = "🆓 Списано из бесплатных токенов"
    else:
        source_text = "🪙 Списано из общего баланса токенов"

    safe_edit_message(
        message.chat.id,
        msg.message_id,
        f"🖼 *{model_name}*\n\n"
        f"{answer}\n\n"
        f"{source_text}\n"
        f"💸 Списано: *{charged}* ток.\n"
        f"🆓 Бесплатных: *{free_tokens}*\n"
        f"💳 Платных: *{paid_tokens}*\n"
        f"📦 Всего: *{total_tokens}*",
        reply_markup=get_main_keyboard()
    )


# =========================
# Telegram handlers
# =========================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    ensure_user(message.from_user.id)
    payment_text = "Покупка токенов уже доступна." if YOOKASSA_ENABLED else "Сейчас доступен выбор пакета токенов, оплата будет подключена позже."

    bot.send_message(
        message.chat.id,
        "Привет! Я AI-бот.\n\n"
        "Есть 2 режима:\n"
        "• обычный текстовый чат\n"
        "• отдельный режим работы с изображениями\n\n"
        f"{payment_text}",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(commands=["restart"])
def cmd_restart(message):
    reset_free_tokens(message.from_user.id)
    bot.send_message(
        message.chat.id,
        f"🔄 Бесплатные токены сброшены до *{FREE_TOKENS}*.\n"
        f"Платные токены не изменены.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "🔄 Restart")
def btn_restart(message):
    reset_free_tokens(message.from_user.id)
    bot.send_message(
        message.chat.id,
        f"🔄 Бесплатные токены сброшены до *{FREE_TOKENS}*.\n"
        f"Платные токены сохранены.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "📊 Баланс токенов")
def btn_balance(message):
    bot.send_message(
        message.chat.id,
        format_balance_text(message.from_user.id),
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "⚙️ Модель")
def btn_model(message):
    data = get_user_data(message.from_user.id)
    current_name = TEXT_MODELS.get(data["model"], data["model"])
    current_cost = TEXT_MODEL_COSTS.get(data["model"], 1)

    bot.send_message(
        message.chat.id,
        f"Текущая текстовая модель: *{current_name}*\n"
        f"Стоимость запроса: *{current_cost}* ток.\n\n"
        f"Выбери новую:",
        reply_markup=get_models_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "🖼 Режим изображений")
def btn_image_mode(message):
    set_image_mode(message.from_user.id, True)
    data = get_user_data(message.from_user.id)
    current_image_name = IMAGE_MODELS.get(data["image_model"], data["image_model"])

    bot.send_message(
        message.chat.id,
        f"🖼 Режим изображений включен.\n"
        f"Текущая модель: *{current_image_name}*\n\n"
        f"Выбери image-модель или сразу отправь фото с подписью.",
        reply_markup=get_image_models_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "❌ Выйти из режима изображений")
def btn_exit_image_mode(message):
    set_image_mode(message.from_user.id, False)
    bot.send_message(
        message.chat.id,
        "✅ Режим изображений выключен.\nТеперь бот снова работает как обычный текстовый чат.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "💳 Купить токены")
def btn_payments(message):
    bot.send_message(
        message.chat.id,
        "Выбери пакет токенов:",
        reply_markup=get_payments_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "🧠 Спросить AI")
def btn_ai(message):
    set_image_mode(message.from_user.id, False)
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
        f"Стоимость запроса: *{model_cost}* ток.",
        reply_markup=get_main_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("imgmodel:"))
def callback_image_model(call):
    model = call.data.split("imgmodel:", 1)[1]

    if model not in IMAGE_MODELS:
        bot.answer_callback_query(call.id, "Неизвестная image-модель")
        return

    set_image_model(call.from_user.id, model)
    set_image_mode(call.from_user.id, True)

    model_name = IMAGE_MODELS[model]
    model_cost = IMAGE_MODEL_COSTS.get(model, 1)

    bot.answer_callback_query(call.id, f"Выбрана {model_name}")
    safe_edit_message(
        call.message.chat.id,
        call.message.message_id,
        f"✅ Image-модель изменена на *{model_name}*\n"
        f"Стоимость запроса: *{model_cost}* ток.\n\n"
        f"Теперь отправь фото с подписью или без подписи.",
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
        f"После успешной оплаты токены начислятся автоматически.",
        reply_markup=get_main_keyboard(),
        disable_web_page_preview=True
    )


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    data = get_user_data(message.from_user.id)

    if not data["image_mode"]:
        bot.send_message(
            message.chat.id,
            "🖼 Чтобы работать с изображениями, сначала включи кнопку *Режим изображений*.",
            reply_markup=get_main_keyboard()
        )
        return

    process_image_question(message)


@bot.message_handler(content_types=["text"])
def handle_text(message):
    text = (message.text or "").strip()

    ignored_buttons = {
        "🧠 Спросить AI",
        "⚙️ Модель",
        "🖼 Режим изображений",
        "📊 Баланс токенов",
        "💳 Купить токены",
        "🔄 Restart",
        "❌ Выйти из режима изображений"
    }

    if text in ignored_buttons:
        return

    data = get_user_data(message.from_user.id)

    if data["image_mode"]:
        bot.send_message(
            message.chat.id,
            "🖼 Сейчас включен режим изображений.\nОтправь фото с подписью или выключи режим кнопкой *❌ Выйти из режима изображений*.",
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