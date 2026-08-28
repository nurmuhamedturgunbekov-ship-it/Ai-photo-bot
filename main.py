import asyncio
import logging
import os
from datetime import datetime
from urllib.parse import quote

import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BufferedInputFile
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

logging.basicConfig(level=logging.INFO)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())

DB_PATH = "users.db"

# ==================== БАЗА ДАННЫХ ====================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                is_admin INTEGER DEFAULT 0,
                generations INTEGER DEFAULT 0,
                first_seen TEXT,
                last_seen TEXT
            )
        """)
        await db.commit()

async def add_or_update_user(user_id: int, username: str | None, full_name: str):
    now = datetime.now().isoformat()
    is_admin = 1 if user_id in ADMIN_IDS else 0

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        exists = await cursor.fetchone()

        if exists:
            await db.execute("""
                UPDATE users 
                SET username = ?, full_name = ?, last_seen = ?, is_admin = ?
                WHERE user_id = ?
            """, (username, full_name, now, is_admin, user_id))
        else:
            await db.execute("""
                INSERT INTO users (user_id, username, full_name, is_admin, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, username, full_name, is_admin, now, now))
        await db.commit()

async def increment_generations(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET generations = generations + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ==================== СОСТОЯНИЯ ====================

class EditPhoto(StatesGroup):
    waiting_for_prompt = State()

# ==================== ХЭНДЛЕРЫ ====================

@dp.message(CommandStart())
async def start(message: Message):
    user = message.from_user
    await add_or_update_user(user.id, user.username, user.full_name)

    admin_text = "\n\n🔑 <b>Ты администратор</b> — у тебя полный безлимитный доступ." if is_admin(user.id) else ""

    await message.answer(
        f"Привет, <b>{user.first_name}</b>! 👋\n\n"
        "Я умею:\n"
        "1️⃣ Генерировать картинки по тексту\n"
        "2️⃣ Изменять твои фото по описанию\n\n"
        "<b>Как пользоваться:</b>\n"
        "• Просто напиши текст → получишь картинку\n"
        "• Отправь фото → потом напиши, что с ним сделать\n\n"
        "Команды: /help /stats"
        + admin_text
    )

@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "<b>Инструкция:</b>\n\n"
        "🖼 <b>Текст → картинка</b>\n"
        "Просто напиши описание картинки\n\n"
        "✏️ <b>Изменить фото</b>\n"
        "1. Отправь любое фото\n"
        "2. Напиши, что с ним сделать\n\n"
        "📊 /stats — твоя статистика"
    )

@dp.message(Command("stats"))
async def stats(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT generations, first_seen, is_admin FROM users WHERE user_id = ?",
            (message.from_user.id,)
        )
        row = await cursor.fetchone()

    if row:
        gens, first, admin = row
        status = "Администратор (безлимит)" if admin else "Обычный пользователь"
        await message.answer(
            f"<b>Статус:</b> {status}\n"
            f"Сгенерировано: <b>{gens}</b>\n"
            f"Первый заход: {first[:10]}"
        )
    else:
        await message.answer("Статистика пока пустая")

# ===== Генерация по тексту =====
@dp.message(F.text & ~F.text.startswith("/"))
async def text_to_image(message: Message):
    prompt = message.text.strip()
    if len(prompt) < 2:
        return await message.answer("Напиши описание")

    wait = await message.answer("⏳ Генерирую...")

    try:
        encoded = quote(prompt)

        if is_admin(message.from_user.id):
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?width=1024&height=1024&nologo=true&model=flux&safe=false"
            )
        else:
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?width=1024&height=1024&nologo=true&model=flux"
            )

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=90) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    await message.answer_photo(
                        photo=BufferedInputFile(data, filename="image.jpg"),
                        caption=f"🖼 {prompt}"
                    )
                    await increment_generations(message.from_user.id)
                    await wait.delete()
                else:
                    await wait.edit_text("Не получилось. Попробуй другой промпт.")
    except Exception as e:
        logging.error(e)
        await wait.edit_text("Ошибка генерации.")

# ===== Получили фото =====
@dp.message(F.photo)
async def photo_received(message: Message, state: FSMContext):
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"

    await state.update_data(photo_url=file_url)
    await state.set_state(EditPhoto.waiting_for_prompt)

    await message.answer(
        "Фото получил! ✅\n\n"
        "Теперь напиши, <b>что с ним сделать</b>."
    )

# ===== Изменение фото =====
@dp.message(EditPhoto.waiting_for_prompt, F.text)
async def edit_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photo_url = data.get("photo_url")
    prompt = message.text.strip()

    await state.clear()
    wait = await message.answer("⏳ Изменяю фото...")

    try:
        encoded = quote(prompt)

        if is_admin(message.from_user.id):
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?model=kontext&image={quote(photo_url)}&width=1024&height=1024&nologo=true&safe=false"
            )
        else:
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?model=kontext&image={quote(photo_url)}&width=1024&height=1024&nologo=true"
            )

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=120) as resp:
                if resp.status == 200:
                    img_data = await resp.read()
                    await message.answer_photo(
                        photo=BufferedInputFile(img_data, filename="edited.jpg"),
                        caption=f"✏️ {prompt}"
                    )
                    await increment_generations(message.from_user.id)
                    await wait.delete()
                else:
                    await wait.edit_text("Не удалось изменить фото.")
    except Exception as e:
        logging.error(e)
        await wait.edit_text("Ошибка при обработке.")

# ==================== ЗАПУСК ====================

async def main():
    await init_db()
    print(f"Бот запущен. Админы: {ADMIN_IDS}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
