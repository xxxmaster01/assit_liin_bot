from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from contextlib import asynccontextmanager
import sqlite3
import threading
import time
from datetime import datetime, timezone
import dateparser
import os

# === Настройки ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my-secret")
BASE_WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000")

# === База данных ===
DB_PATH = "reminders.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            text TEXT,
            remind_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# === Telegram бот ===
bot = Bot(token=BOT_TOKEN)
router = Router()

@router.message()
async def handle_message(message: Message):
    chat_id = message.chat.id
    text = message.text.strip()

    # Сохраняем пользователя
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

    if text == "/start":
        await message.answer(
            "✅ Привет! Я бот-напоминалка.\n\n"
            "Теперь ты можешь отправлять мне напоминания из приложения.\n"
            "Пример: «Позвонить маме завтра в 15:00»"
        )
    else:
        await message.answer("ℹ️ Я принимаю напоминания только из приложения. Нажми /start, чтобы подключиться.")

# === FastAPI ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Установка webhook при запуске
    await bot.set_webhook(
        url=f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET
    )
    # Запуск фонового обработчика напоминаний
    threading.Thread(target=check_and_send_reminders, daemon=True).start()
    yield
    # Удаление webhook при остановке
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)

# Webhook для Telegram
telegram_app = Dispatcher()
telegram_app.include_router(router)
SimpleRequestHandler(
    dispatcher=telegram_app,
    bot=bot,
    secret_token=WEBHOOK_SECRET
).register(app, WEBHOOK_PATH)

# === API для приложения ===
@app.post("/api/reminders")
async def create_reminder(request: Request):
    data = await request.json()
    chat_id = data.get("telegram_chat_id")
    raw_text = data.get("text", "").strip()

    if not chat_id or not raw_text:
        return {"error": "Нужны telegram_chat_id и text"}

    # Парсим дату из текста (например: "завтра в 15:00")
    now = datetime.now(timezone.utc)
    parsed = dateparser.parse(
        raw_text,
        languages=['ru'],
        settings={
            'PREFER_DATES_FROM': 'future',
            'RELATIVE_BASE': now
        }
    )

    if not parsed:
        return {"error": "Не удалось распознать дату. Попробуйте: 'завтра в 15:00'"}

    # Приводим к UTC
    parsed = parsed.astimezone(timezone.utc)
    remind_at = parsed.strftime("%Y-%m-%d %H:%M")

    # Сохраняем в БД
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO reminders (chat_id, text, remind_at) VALUES (?, ?, ?)",
        (chat_id, raw_text, remind_at)
    )
    conn.commit()
    conn.close()

    return {"status": "ok", "remind_at": remind_at}

# === Фоновая проверка напоминаний ===
def check_and_send_reminders():
    while True:
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("""
                SELECT id, chat_id, text FROM reminders
                WHERE remind_at <= ? AND remind_at > datetime('now', '-1 hour')
            """, (now,))
            rows = cur.fetchall()

            for row in rows:
                rid, chat_id, text = row
                # Отправляем через бота
                import asyncio
                asyncio.run(bot.send_message(chat_id, f"🔔 Напоминание:\n\n{text}"))
                # Удаляем
                conn.execute("DELETE FROM reminders WHERE id = ?", (rid,))

            conn.commit()
            conn.close()
        except Exception as e:
            print("Ошибка в фоне:", e)
        time.sleep(30)  # проверка каждые 30 сек
