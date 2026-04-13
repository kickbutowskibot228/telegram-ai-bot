import os
import time
import json
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
            requests INTEGER NOT NULL DEFAULT 0
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
        cur.execute(
            "INSERT INTO users (user_id, model, requests) VALUES (?, ?, ?)",
            (user_id, DEFAULT_MODEL, 0)
        )
        conn.commit()

    conn.close()


def get_user_data(user_id: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT model, requests FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    conn.close()

    return {
        "model": row[0],
        "requests": row[1]
    }


def set_user_model(user_id: int, model: str):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("UPDATE users SET model = ? WHERE user_id = ?", (model, user_id))
    conn.commit()
    conn.close()


def increment_request(user_id: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        "UPDATE users SET requests = requests + 1 WHERE user_id = ?",
        (user_id,)
    )
    conn.commit()
    conn.close()


def reset_requests(user_id: int):
    ensure_user(user_id)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("UPDATE users SET requests = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# =========================
# UI
# =========================
def get_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🧠 Спросить AI", "⚙️ Модель")
    kb.row("📊 Остаток", "🔄 Restart")
    return kb


def get_models_keyboard():
    kb = types.InlineKeyboardMarkup()
    for model_id, model_name in AVAILABLE_MODELS.items():
        kb.add(types.InlineKeyboardButton(model_name, callback_data=f"model:{model_id}"))
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
# Message processing
# =========================
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


def process_question(message):
    user_id = message.from_user.id
    data = get_user_data(user_id)

    if data["requests"] >= FREE_LIMIT:
        reset_requests(user_id)
        bot.send_message(message.chat.id, "❌ Лимит запросов исчерпан. Нажми /restart")
        return

    msg = bot.send_message(message.chat.id, "🤖 Думаю... ⏳")
    bot.send_chat_action(message.chat.id, "typing")

    answer = call_openrouter(data["model"], message.text)

    increment_request(user_id)
    updated = get_user_data(user_id)
    left = max(FREE_LIMIT - updated["requests"], 0)

    model_name = AVAILABLE_MODELS.get(updated["model"], updated["model"])

    safe_edit_message(
        message.chat.id,
        msg.message_id,
        f"🤖 *{model_name}*\n\n{answer}\n\n💰 Осталось запросов: *{left}*",
        reply_markup=get_main_keyboard()
    )


# =========================
# Telegram handlers
# =========================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    ensure_user(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "Привет! Я AI-бот.\n\n"
        "Просто напиши вопрос, и я отвечу.\n"
        "Также можно выбрать модель через меню.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(commands=["restart"])
def cmd_restart(message):
    reset_requests(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "🔄 Лимит сброшен. Можешь снова задавать вопросы.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "🔄 Restart")
def btn_restart(message):
    reset_requests(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "🔄 Лимит сброшен.",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "📊 Остаток")
def btn_balance(message):
    data = get_user_data(message.from_user.id)
    left = max(FREE_LIMIT - data["requests"], 0)
    bot.send_message(
        message.chat.id,
        f"💰 Осталось запросов: *{left}*",
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


@bot.message_handler(content_types=["text"])
def handle_text(message):
    text = (message.text or "").strip()

    ignored_buttons = {"🧠 Спросить AI", "⚙️ Модель", "📊 Остаток", "🔄 Restart"}
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
        "webhook": True
    }, 200


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)

    json_string = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])

    return "OK", 200


# =========================
# Startup
# =========================
def setup_webhook():
    if not RENDER_EXTERNAL_HOSTNAME:
        logger.warning("RENDER_EXTERNAL_HOSTNAME не найден, webhook не установлен")
        return

    webhook_url = f"https://{RENDER_EXTERNAL_HOSTNAME}/{TELEGRAM_TOKEN}"

    try:
        bot.remove_webhook()
        time.sleep(1)
        success = bot.set_webhook(url=webhook_url)

        if success:
            logger.info("Webhook установлен: %s", webhook_url)
        else:
            logger.error("Не удалось установить webhook: %s", webhook_url)

    except Exception as e:
        logger.exception("Ошибка установки webhook: %s", e)


init_db()
setup_webhook()

if __name__ == "__main__":
    logger.info("🚀 Bot started on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)