import os
import time
import uuid
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
FREE_LIMIT = 20

AVAILABLE_MODELS = {
    "google/gemini-2.0-flash-exp": "Gemini Flash",
    "openai/gpt-4o-mini": "GPT-4o Mini",
    "deepseek/deepseek-chat": "DeepSeek Chat"
}

DEFAULT_MODEL = "google/gemini-2.0-flash-exp"

PAY_PLANS = {
    "50": {
        "label": "50 запросов",
        "amount": 250,
        "requests": 50
    },
    "100": {
        "label": "100 запросов",
        "amount": 400,
        "requests": 100
    },
    "200": {
        "label": "200 запросов",
        "amount": 750,
        "requests": 200
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
            model TEXT NOT NULL DEFAULT 'google/gemini-2.0-flash-exp',
            free_used INTEGER NOT NULL DEFAULT 0,
            paid_balance INTEGER NOT NULL DEFAULT 0
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
            requests_count INTEGER NOT NULL,
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
            INSERT INTO users (user_id, model, free_used, paid_balance)
            VALUES (?, ?, ?, ?)
        """, (user_id, DEFAULT_MODEL, 0, 0))
        conn.commit()

    conn.close()


def get_user_data(user_id: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT model, free_used, paid_balance
        FROM users
        WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()

    conn.close()

    return {
        "model": row[0],
        "free_used": row[1],
        "paid_balance": row[2]
    }


def set_user_model(user_id: int, model: str):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET model = ? WHERE user_id = ?", (model, user_id))
    conn.commit()
    conn.close()


def reset_free_limit(user_id: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET free_used = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def add_paid_balance(user_id: int, requests_count: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET paid_balance = paid_balance + ? WHERE user_id = ?",
        (requests_count, user_id)
    )
    conn.commit()
    conn.close()


def consume_one_request(user_id: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT free_used, paid_balance
        FROM users
        WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()

    if not row:
        conn.close()
        return False, None

    free_used, paid_balance = row

    if paid_balance > 0:
        cur.execute("""
            UPDATE users
            SET paid_balance = paid_balance - 1
            WHERE user_id = ?
        """, (user_id,))
        conn.commit()
        conn.close()
        return True, "paid"

    if free_used < FREE_LIMIT:
        cur.execute("""
            UPDATE users
            SET free_used = free_used + 1
            WHERE user_id = ?
        """, (user_id,))
        conn.commit()
        conn.close()
        return True, "free"

    conn.close()
    return False, "limit"


def create_payment_record(payment_id: str, idempotence_key: str, user_id: int, plan_key: str, amount: int, requests_count: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO payments
        (payment_id, idempotence_key, user_id, plan_key, amount, requests_count, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        payment_id,
        idempotence_key,
        user_id,
        plan_key,
        amount,
        requests_count,
        "pending"
    ))

    conn.commit()
    conn.close()


def get_payment_by_id(payment_id: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT payment_id, user_id, plan_key, amount, requests_count, status
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
        "requests_count": row[4],
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
    kb.row("📊 Остаток", "💳 Пополнить баланс")
    kb.row("🔄 Restart")
    return kb


def get_models_keyboard():
    kb = types.InlineKeyboardMarkup()
    for model_id, model_name in AVAILABLE_MODELS.items():
        kb.add(types.InlineKeyboardButton(model_name, callback_data=f"model:{model_id}"))
    return kb


def get_payments_keyboard():
    kb = types.InlineKeyboardMarkup()
    for plan_key, plan in PAY_PLANS.items():
        kb.add(
            types.InlineKeyboardButton(
                f"{plan['label']} — {plan['amount']} ₽",
                callback_data=f"payplan:{plan_key}"
            )
        )
    return kb


# =========================
# OpenRouter
# =========================
def call_openrouter(model: str, message: str, max_retries: int = 3):
    url = "https://openrouter.ai/api/v1/chat/completions"
    fallback_model = "google/gemini-2.0-flash-exp"

    data = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Отвечай кратко, по делу, на русском языке."},
            {"role": "user", "content": message}
        ],
        "max_tokens": 400,
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
                timeout=60
            )

            if resp.status_code == 200:
                payload = resp.json()
                return payload["choices"][0]["message"]["content"]

            if resp.status_code == 429:
                wait_time = 2 ** attempt
                logger.warning("OpenRouter 429, retry in %s sec", wait_time)
                time.sleep(wait_time)
                continue

            logger.error("OpenRouter error %s: %s", resp.status_code, resp.text)

        except requests.exceptions.Timeout:
            logger.warning("OpenRouter timeout %s/%s", attempt + 1, max_retries)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

        except Exception as e:
            logger.exception("Ошибка запроса к OpenRouter: %s", e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

    if model != fallback_model:
        logger.warning("Переключение на fallback model: %s", fallback_model)
        return call_openrouter(fallback_model, message, max_retries=1) + " ⚡"

    return "⚠️ Не удалось получить ответ от AI. Попробуй ещё раз позже."


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
            "requests_count": str(plan["requests"])
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
            requests_count=plan["requests"]
        )

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

    add_paid_balance(payment["user_id"], payment["requests_count"])
    update_payment_status(payment_id, "succeeded")

    user_data = get_user_data(payment["user_id"])

    try:
        bot.send_message(
            payment["user_id"],
            f"✅ Оплата прошла успешно!\n\n"
            f"Тариф: *{PAY_PLANS[payment['plan_key']]['label']}*\n"
            f"Начислено: *{payment['requests_count']}* запросов\n"
            f"Платный баланс: *{user_data['paid_balance']}*",
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
    free_left = max(FREE_LIMIT - data["free_used"], 0)
    paid_left = data["paid_balance"]
    total_left = free_left + paid_left

    return (
        f"📊 *Твой баланс запросов*\n\n"
        f"🆓 Бесплатных осталось: *{free_left}* из *{FREE_LIMIT}*\n"
        f"💳 Платных осталось: *{paid_left}*\n"
        f"📦 Всего доступно: *{total_left}*"
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


# =========================
# Message processing
# =========================
def process_question(message):
    user_id = message.from_user.id
    user_data_before = get_user_data(user_id)

    can_use, source = consume_one_request(user_id)

    if not can_use:
        bot.send_message(
            message.chat.id,
            "❌ Запросы закончились.\n\n"
            "Попробуй нажать *💳 Пополнить баланс* или сбросить бесплатный лимит через /restart.",
            reply_markup=get_main_keyboard()
        )
        return

    msg = bot.send_message(message.chat.id, "🤖 Думаю... ⏳")
    bot.send_chat_action(message.chat.id, "typing")

    answer = call_openrouter(user_data_before["model"], message.text)
    updated = get_user_data(user_id)

    free_left = max(FREE_LIMIT - updated["free_used"], 0)
    paid_left = updated["paid_balance"]

    model_name = AVAILABLE_MODELS.get(updated["model"], updated["model"])
    source_text = "💳 Списано из платного баланса" if source == "paid" else "🆓 Списано из бесплатного лимита"

    safe_edit_message(
        message.chat.id,
        msg.message_id,
        f"🤖 *{model_name}*\n\n"
        f"{answer}\n\n"
        f"{source_text}\n"
        f"🆓 Бесплатных осталось: *{free_left}*\n"
        f"💳 Платных осталось: *{paid_left}*",
        reply_markup=get_main_keyboard()
    )


# =========================
# Telegram handlers
# =========================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    ensure_user(message.from_user.id)
    payment_text = "Оплата уже доступна." if YOOKASSA_ENABLED else "Сейчас доступен выбор тарифа, оплата будет подключена позже."

    bot.send_message(
        message.chat.id,
        "Привет! Я AI-бот.\n\n"
        "Просто напиши вопрос, и я отвечу.\n"
        "Сначала тратится платный баланс, затем бесплатный лимит.\n\n"
        f"{payment_text}",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(commands=["restart"])
def cmd_restart(message):
    reset_free_limit(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "🔄 Бесплатный лимит сброшен.\nПлатный баланс не изменен.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "🔄 Restart")
def btn_restart(message):
    reset_free_limit(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "🔄 Бесплатный лимит сброшен.\nПлатный баланс сохранен.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "📊 Остаток")
def btn_balance(message):
    bot.send_message(
        message.chat.id,
        format_balance_text(message.from_user.id),
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "⚙️ Модель")
def btn_model(message):
    data = get_user_data(message.from_user.id)
    current_name = AVAILABLE_MODELS.get(data["model"], data["model"])

    bot.send_message(
        message.chat.id,
        f"Текущая модель: *{current_name}*\n\nВыбери новую:",
        reply_markup=get_models_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "💳 Пополнить баланс")
def btn_payments(message):
    bot.send_message(
        message.chat.id,
        "Выбери тариф пополнения:",
        reply_markup=get_payments_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "🧠 Спросить AI")
def btn_ai(message):
    bot.send_message(
        message.chat.id,
        "Напиши вопрос одним сообщением.",
        reply_markup=get_main_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("model:"))
def callback_model(call):
    model = call.data.split("model:", 1)[1]

    if model not in AVAILABLE_MODELS:
        bot.answer_callback_query(call.id, "Неизвестная модель")
        return

    set_user_model(call.from_user.id, model)
    model_name = AVAILABLE_MODELS[model]

    bot.answer_callback_query(call.id, f"Выбрана {model_name}")
    safe_edit_message(
        call.message.chat.id,
        call.message.message_id,
        f"✅ Модель изменена на *{model_name}*",
        reply_markup=get_main_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("payplan:"))
def callback_payplan(call):
    plan_key = call.data.split("payplan:", 1)[1]

    if plan_key not in PAY_PLANS:
        bot.answer_callback_query(call.id, "Неизвестный тариф")
        return

    plan = PAY_PLANS[plan_key]

    if not YOOKASSA_ENABLED:
        bot.answer_callback_query(call.id, "Тариф выбран")
        bot.send_message(
            call.message.chat.id,
            f"💳 Ты выбрал тариф:\n\n"
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
        f"💳 Тариф: *{plan['label']}*\n"
        f"💵 Стоимость: *{plan['amount']} ₽*\n\n"
        f"Перейди по ссылке для оплаты:\n{confirmation_url}\n\n"
        f"После успешной оплаты баланс начислится автоматически.",
        reply_markup=get_main_keyboard(),
        disable_web_page_preview=True
    )


@bot.message_handler(content_types=["text"])
def handle_text(message):
    text = (message.text or "").strip()

    ignored_buttons = {
        "🧠 Спросить AI",
        "⚙️ Модель",
        "📊 Остаток",
        "💳 Пополнить баланс",
        "🔄 Restart"
    }

    if text in ignored_buttons:
        return

    process_question(message)


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