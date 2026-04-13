import os, time, logging, requests, queue
from telebot import types
import telebot
from dotenv import load_dotenv
from flask import Flask, request
from threading import Thread

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
bot = telebot.TeleBot(TELEGRAM_TOKEN)

FREE_LIMIT = 20
# ... (твои DB функции без изменений)

# Flask для Render + 24/7
app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    return {"status": "🤖 AI Bot 24/7 OK", "uptime": True}

@app.route('/health')
def health():
    return "OK"

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

# Запуск Flask в фоне
flask_thread = Thread(target=run_flask)
flask_thread.daemon = True
flask_thread.start()
logger.info("🌐 Flask запущен для Render 24/7")

# Улучшенный OpenRouter без таймаутов
def call_openrouter(model, message, max_retries=3):
    url = "https://openrouter.ai/api/v1/chat/completions"
    data = {
        "model": model,
        "messages": [{"role": "system", "content": "Кратко, по делу, русский."}, 
                     {"role": "user", "content": message}],
        "max_tokens": 400,
        "temperature": 0.7
    }
    
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json"
            }, json=data, timeout=60)
            
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            elif resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
        except requests.exceptions.Timeout:
            logger.warning(f"Таймаут {attempt+1}/3")
            if attempt == max_retries - 1:
                return "⏰ Занято. Быстрый режим:"
        except:
            pass
    
    # Fallback - самая быстрая модель
    return call_openrouter("google/gemini-2.0-flash-exp", message, 1) + " ⚡"

# process_question с прогрессом
def process_question(message):
    user_id = message.from_user.id
    data = get_user_data(user_id)
    
    increment_request(user_id)
    if data["requests"] >= FREE_LIMIT:
        reset_requests(user_id)
        bot.send_message(message.chat.id, "❌ Лимит. /restart")
        return
    
    # Прогресс
    msg = bot.send_message(message.chat.id, "🤖 Думаю... ⏳")
    bot.send_chat_action(message.chat.id, "typing")
    
    model = data["model"]
    answer = call_openrouter(model, message.text)
    
    left = FREE_LIMIT - data["requests"] - 1
    bot.edit_message_text(
        f"🤖 *{AVAILABLE_MODELS.get(model, model)}*\n\n"
        f"{answer}\n\n💰 Осталось: {left}",
        message.chat.id, msg.message_id, parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

# ... (твои остальные хендлеры без изменений)

if __name__ == "__main__":
    logger.info("🚀 AI Bot 24/7 старт!")
    # Webhook лучше polling
    webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{TELEGRAM_TOKEN}"
    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)
    
    # Flask обрабатывает webhook
    @app.route(f'/{TELEGRAM_TOKEN}', methods=['POST'])
    def webhook():
        update = telebot.types.Update.de_json(request.stream.read().decode('utf-8'))
        bot.process_new_updates([update])
        return 'OK'
    
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)