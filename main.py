import os
import json
import requests
import sqlite3
import logging
import time
import sys
from telebot import types
import telebot
from dotenv import load_dotenv

# UTF-8 для консоли
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
YOOKASSA_PROVIDER_TOKEN = os.getenv("YOOKASSA_PROVIDER_TOKEN", "")

if not TELEGRAM_TOKEN or not OPENROUTER_API_KEY:
    raise ValueError("TELEGRAM_TOKEN или OPENROUTER_API_KEY не найден в .env")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

FREE_LIMIT = 20
DB_FILE = "bot_users.db"
DEFAULT_MODEL = "deepseek/deepseek-r1"

AVAILABLE_MODELS = {
    "deepseek/deepseek-r1": "DeepSeek R1 (бесплатно)",
    "qwen/qwen2.5-72b-instruct": "Qwen 2.5 72B",
    "anthropic/claude-haiku-4.5": "Claude Haiku 4.5",
    "google/gemini-2.0-flash-exp": "Gemini 2.0 Flash",
    "openai/gpt-4o-mini": "GPT-4o Mini"
}

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA encoding = 'UTF-8'")
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        requests INTEGER DEFAULT 0,
        current_model TEXT DEFAULT 'deepseek/deepseek-r1'
    )
    """)
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

def get_user_data(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT requests, current_model FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        return {"requests": result[0], "model": result[1]}
    return {"requests": 0, "model": DEFAULT_MODEL}

def increment_request(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, requests) VALUES (?, 0)", (user_id,))
    cursor.execute("UPDATE users SET requests = requests + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def reset_requests(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, requests) VALUES (?, 0)", (user_id,))
    cursor.execute("UPDATE users SET requests = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def set_user_model(user_id, model_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, current_model) VALUES (?, ?)", (user_id, DEFAULT_MODEL))
    cursor.execute("UPDATE users SET current_model = ? WHERE user_id = ?", (model_name, user_id))
    conn.commit()
    conn.close()

def call_openrouter(model, user_message):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Title": "TelegramBot",
        "HTTP-Referer": "http://localhost:8000"
    }
    data = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Ты полезный ассистент. Отвечай кратко, по делу, на русском языке."
            },
            {
                "role": "user",
                "content": user_message
            }
        ],
        "max_tokens": 500,
        "temperature": 0.7
    }
    
    try:
        logger.info(f"Запрос к модели: {model}")
        response = requests.post(url, headers=headers, json=data, timeout=30)
        
        logger.info(f"Статус ответа: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            answer = result["choices"][0]["message"]["content"]
            logger.info(f"Успех. Длина ответа: {len(answer)}")
            return answer
        else:
            error_msg = response.text[:200]
            logger.error(f"Ошибка API {response.status_code}: {error_msg}")
            if response.status_code in [400, 404]:
                return f"Модель '{model}' недоступна. Выбери другую: /model"
            elif response.status_code == 401:
                return "Неверный API ключ. Получи новый: openrouter.ai/keys"
            return f"Ошибка API {response.status_code}: {error_msg}"
            
    except requests.exceptions.Timeout:
        logger.error("Таймаут запроса")
        return "Таймаут. Попробуй позже."
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {str(e)}")
        return f"Ошибка бота: {str(e)}"

# Русские клавиатуры
def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row("🤖 Задать вопрос", "💰 Баланс")
    markup.row("🔄 Перезапуск", "🤖 Сменить модель")
    markup.row("💳 Пополнить")
    return markup

def get_models_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for model_code, model_name in AVAILABLE_MODELS.items():
        markup.add(f"🎯 {model_name}")
    markup.row("⬅️ Назад")
    return markup

def get_topup_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("💳 Купить 50 запросов (100₽)")
    markup.row("⬅️ Назад")
    return markup

user_states = {}

@bot.message_handler(commands=["start"])
def start_handler(message):
    user_id = message.from_user.id
    user_states[user_id] = "started"
    data = get_user_data(user_id)
    left = max(0, FREE_LIMIT - data["requests"])
    model_name = AVAILABLE_MODELS.get(data["model"], data["model"])
    
    bot.send_message(
        message.chat.id,
        f"🤖 *🚀 OpenRouter AI Bot*\n\n"
        f"💰 Доступно запросов: *{left}/{FREE_LIMIT}*\n"
        f"🧠 Текущая модель: *{model_name}*\n\n"
        "Выбери действие из меню:",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(commands=["model"])
@bot.message_handler(func=lambda m: m.text == "🤖 Сменить модель")
def model_menu(message):
    data = get_user_data(message.from_user.id)
    current_name = AVAILABLE_MODELS.get(data["model"], data["model"])
    bot.send_message(
        message.chat.id,
        f"🧠 *Текущая модель:* {current_name}\n\nВыбери новую:",
        parse_mode="Markdown",
        reply_markup=get_models_keyboard()
    )

@bot.message_handler(func=lambda m: m.text.startswith("🎯 "))
def change_model(message):
    selected_name = message.text[2:].strip()
    selected_code = None
    
    for code, name in AVAILABLE_MODELS.items():
        if name == selected_name:
            selected_code = code
            break
    
    user_id = message.from_user.id
    if selected_code:
        set_user_model(user_id, selected_code)
        bot.send_message(
            message.chat.id,
            f"✅ *Модель изменена!*\n🧠 *{selected_name}*\n\nГотово!",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
    else:
        bot.send_message(message.chat.id, "❌ Ошибка выбора", reply_markup=get_models_keyboard())

@bot.message_handler(func=lambda m: m.text == "🤖 Задать вопрос")
def ask_question_handler(message):
    user_id = message.from_user.id
    data = get_user_data(user_id)
    
    if data["requests"] >= FREE_LIMIT:
        model_name = AVAILABLE_MODELS.get(data["model"], data["model"])
        bot.reply_to(
            message,
            f"❌ *Лимит исчерпан!*\n\n"
            f"Использовано: *{data['requests']}/{FREE_LIMIT}*\n"
            f"🧠 Модель: *{model_name}*\n\n"
            "🔄 Перезапуск или 💳 Пополнить",
            parse_mode="Markdown",
            reply_markup=get_topup_keyboard()
        )
        return
    
    bot.send_message(
        message.chat.id,
        "💭 *Напиши свой вопрос ИИ:*",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(message, process_question)

def process_question(message):
    user_id = message.from_user.id
    data = get_user_data(user_id)
    model_code = data["model"]
    
    # Инкрементируем счетчик заранее и проверяем лимит
    increment_request(user_id)
    updated_data = get_user_data(user_id)
    
    if updated_data["requests"] > FREE_LIMIT:
        # Откатываем счетчик при превышении
        reset_requests(user_id)
        bot.send_message(
            message.chat.id,
            f"❌ Лимит превышен ({FREE_LIMIT} запросов)! "
            f"Используй /restart или 💳 пополни баланс.",
            reply_markup=get_main_keyboard()
        )
        return
    
    bot.send_chat_action(message.chat.id, "typing")
    time.sleep(1)
    
    answer = call_openrouter(model_code, message.text)
    
    left = max(0, FREE_LIMIT - updated_data["requests"])
    model_name = AVAILABLE_MODELS.get(model_code, model_code)
    
    bot.send_message(
        message.chat.id,
        f"🤖 *{model_name}:*\n\n{answer}\n\n"
        f"💰 *Осталось запросов: {left}/{FREE_LIMIT}*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(func=lambda m: m.text == "💰 Баланс")
def balance_handler(message):
    data = get_user_data(message.from_user.id)
    left = max(0, FREE_LIMIT - data["requests"])
    model_name = AVAILABLE_MODELS.get(data["model"], data["model"])
    
    bot.reply_to(
        message,
        f"💰 *Твой баланс:* {left}/{FREE_LIMIT} запросов\n"
        f"🧠 *Модель:* {model_name}",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(commands=["restart"])
@bot.message_handler(func=lambda m: m.text == "🔄 Перезапуск")
def restart_handler(message):
    reset_requests(message.from_user.id)
    data = get_user_data(message.from_user.id)
    model_name = AVAILABLE_MODELS.get(data["model"], data["model"])
    bot.send_message(
        message.chat.id,
        f"🔄 *Перезапуск выполнен!*\n\n"
        f"💰 Счётчик сброшен: *{FREE_LIMIT}/{FREE_LIMIT}*\n"
        f"🧠 Модель: *{model_name}*\n\n"
        f"Готов к работе!",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(func=lambda m: m.text == "⬅️ Назад")
def back_handler(message):
    bot.send_message(message.chat.id, "🏠 Главное меню", reply_markup=get_main_keyboard())

@bot.message_handler(func=lambda m: m.text.startswith("💳"))
def payment_handler(message):
    try:
        if not YOOKASSA_PROVIDER_TOKEN:
            bot.reply_to(message, "❌ Платежи временно недоступны")
            return
        
        bot.send_invoice(
            chat_id=message.chat.id,
            title="Пополнение AI Bot",
            description="50 запросов за 100₽",
            provider_token=YOOKASSA_PROVIDER_TOKEN,
            currency="RUB",
            prices=[types.LabeledPrice("50 запросов", 10000)],  # 100₽ = 10000 копеек
            invoice_payload=str(message.from_user.id)  # ИСПРАВЛЕНО: invoice_payload вместо payload
        )
    except Exception as e:
        logger.error(f"Ошибка создания счета: {e}")
        bot.reply_to(message, "❌ Ошибка создания счета. Попробуй позже.")

@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=["successful_payment"])
def payment_success(message):
    try:
        user_id = int(message.successful_payment.invoice_payload)
        reset_requests(user_id)
        data = get_user_data(user_id)
        model_name = AVAILABLE_MODELS.get(data["model"], data["model"])
        
        bot.send_message(
            message.chat.id,
            f"✅ *Оплата прошла!*\n\n"
            f"🎉 Добавлено *50 запросов*\n"
            f"💰 Баланс: *{FREE_LIMIT}/{FREE_LIMIT}*\n"
            f"🧠 Модель: *{model_name}*\n\n"
            f"Возвращаемся в меню...",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logger.error(f"Ошибка обработки оплаты: {e}")
        bot.send_message(message.chat.id, "✅ Оплата прошла, но произошла ошибка. Счетчик сброшен.")

@bot.message_handler(func=lambda m: True)
def default_handler(message):
    bot.reply_to(message, "👋 Используй кнопки меню или /start", reply_markup=get_main_keyboard())

def main():
    init_db()
    logger.info("AI Bot запущен!")
    logger.info(f"OpenRouter ключ: {OPENROUTER_API_KEY[:10]}...")
    logger.info(f"YooKassa токен: {'Да' if YOOKASSA_PROVIDER_TOKEN else 'Нет'}")
    bot.polling(none_stop=True, interval=1, timeout=20)

if __name__ == "__main__":
    main()