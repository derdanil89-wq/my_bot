import asyncio
import logging
import os
import re

import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    PreCheckoutQuery, LabeledPrice, ChatJoinRequest
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = "bot.db"

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------- СОСТОЯНИЯ (FSM) ----------
class RegState(StatesGroup):
    waiting_for_tiktok = State()

class AddChannelState(StatesGroup):
    waiting_for_forward = State()
    waiting_for_type = State()
    waiting_for_link = State()

# ---------- БАЗА ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            tiktok TEXT,
            display_pref TEXT DEFAULT 'name',
            points INTEGER NOT NULL DEFAULT 0,
            is_banned INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            type TEXT DEFAULT 'channel',
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, channel_id)
        );
        """)
        await db.commit()

async def get_user(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        return await cur.fetchone()

async def upsert_user(tg_id: int, username: str | None, first_name: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(tg_id, username, first_name) VALUES(?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (tg_id, username or "", first_name or "")
        )
        await db.commit()

async def get_points(tg_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT points FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return row[0] if row else 0

async def get_user_rank(tg_id: int) -> tuple[int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT points FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if not row: return 0, 0
        points = row[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM users WHERE (points > ? OR (points = ? AND tg_id < ?)) AND is_banned = 0",
            (points, points, tg_id)
        )
        rank = (await cur.fetchone())[0] + 1
        return rank, points

async def get_available_tasks(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, chat_id, title, url, type FROM channels 
            WHERE active=1 AND id NOT IN (
                SELECT channel_id FROM subscriptions WHERE user_id=?
            ) ORDER BY id
        """, (user_id,))
        return await cur.fetchall()

async def get_channel_by_id(channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM channels WHERE id=? AND active=1", (channel_id,))
        return await cur.fetchone()

def parse_chat_id(value: str):
    try: return int(value)
    except ValueError: return value

# ---------- КЛАВИАТУРЫ ----------
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Задания", callback_data="tasks")],
        [
            InlineKeyboardButton(text="🏆 Рейтинг", callback_data="top"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
        ],
        [InlineKeyboardButton(text="🛒 Купить шансы (⭐️)", callback_data="buy_points")]
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
    ])

# ---------- ПОЛЬЗОВАТЕЛЬСКИЕ ХЕНДЛЕРЫ ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    user = await get_user(message.from_user.id)

    if user and user[6] == 1: # is_banned
        await message.answer("⛔️ Ты заблокирован в этом боте.")
        return

    if not user or not user[3]: # Нет TikTok
        await message.answer(
            "👋 Привет! Для участия в конкурсе и получения привета в видео, "
            "ты должен подписаться на мой TikTok.\n\n"
            "📝 Отправь ссылку на свой TikTok профиль или свой @username (например, <code>@my_tiktok</code>):",
            parse_mode="HTML"
        )
        await state.set_state(RegState.waiting_for_tiktok)
        return

    points = await get_points(message.from_user.id)
    await message.answer(
        f"Привет, {message.from_user.first_name}! У тебя <b>{points}</b> шансов.",
        parse_mode="HTML", reply_markup=main_kb()
    )

@dp.message(RegState.waiting_for_tiktok)
async def process_tiktok(message: Message, state: FSMContext):
    text = message.text.strip() if message.text else ""
    tiktok_pattern = r'(?:https?:\/\/)?(?:www\.)?tiktok\.com\/@([a-zA-Z0-9_\.]+)\/?|^@?([a-zA-Z0-9_\.]+)$'
    match = re.match(tiktok_pattern, text)
    
    if not match:
        await message.answer("❌ Это не похоже на TikTok. Отправь ссылку (например, <i>https://tiktok.com/@username</i>) или просто <b>@username</b>.")
        return
    
    tiktok_username = match.group(1) or match.group(2)
    tiktok_clean = f"@{tiktok_username}" if not tiktok_username.startswith('@') else tiktok_username

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET tiktok=? WHERE tg_id=?", (tiktok_clean, message.from_user.id))
        await db.execute("UPDATE users SET points = points + 1 WHERE tg_id=?", (message.from_user.id,))
        await db.commit()
    
    await state.clear()
    await message.answer(
        f"✅ Отлично! Твой TikTok <b>{tiktok_clean}</b> сохранен.\n\n"
        "Добро пожаловать в бота! За первый вход начислен <b>1 шанс</b>.",
        parse_mode="HTML", reply_markup=main_kb()
    )

@dp.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery):
    await callback.answer()
    points = await get_points(callback.from_user.id)
    await callback.message.edit_text(f"Главное меню. У тебя <b>{points}</b> шансов.", parse_mode="HTML", reply_markup=main_kb())

@dp.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user: return
    display_type = "Имя" if user[4] == 'name' else "@username"
    
    text = (
        f"👤 <b>Твой профиль</b>\n\n"
        f"TikTok: {user[3]}\n"
        f"Отображение в рейтинге: <b>{display_type}</b>\n\n"
        f"Выбери, как тебя показывать в рейтинге:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Имя", callback_data="set_display:name"), InlineKeyboardButton(text="Юзернейм", callback_data="set_display:username")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_display:"))
async def cb_set_display(callback: CallbackQuery):
    pref = callback.data.split(":")[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET display_pref=? WHERE tg_id=?", (pref, callback.from_user.id))
        await db.commit()
    await callback.answer("Сохранено!", show_alert=True)
    callback.data = "profile"
    await cb_profile(callback)

# ---------- ЗАДАНИЯ ----------
async def render_task(user_id: int, index: int):
    tasks = await get_available_tasks(user_id)
    if not tasks:
        return "🎉 <b>Ура!</b> Ты выполнил все доступные задания.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]])
    
    if index >= len(tasks): index = 0
    ch_id, chat_id, title, url, ch_type = tasks[index]
    
    text = f"📋 Доступно заданий: <b>{len(tasks)}</b>\n\nКанал: <b>«{title}»</b>\n\n"
    kb_list = []
    
    if ch_type == 'channel':
        text += "1️⃣ Нажми «Подписаться»\n2️⃣ Затем нажми «Проверить»"
        kb_list.append([InlineKeyboardButton(text="🔗 Подписаться", url=url)])
        kb_list.append([InlineKeyboardButton(text="✅ Проверить", callback_data=f"check:{ch_id}:{index}")])
    else:
        text += "1️⃣ Нажми «Перейти», нажми Start в боте\n2️⃣ Затем нажми «Проверить»"
        kb_list.append([InlineKeyboardButton(text="🤖 Перейти в бота", url=url)])
        kb_list.append([InlineKeyboardButton(text="✅ Я нажал Start", callback_data=f"check_bot:{ch_id}:{index}")])
        
    kb_list.append([
        InlineKeyboardButton(text="⬅️ В меню", callback_data="menu"),
        InlineKeyboardButton(text="➡️ Следующее", callback_data=f"next_task:{index+1}")
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_list)

@dp.callback_query(F.data == "tasks")
async def cb_tasks(callback: CallbackQuery):
    await callback.answer()
    text, kb = await render_task(callback.from_user.id, 0)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("next_task:"))
async def cb_next_task(callback: CallbackQuery):
    await callback.answer()
    index = int(callback.data.split(":")[1])
    text, kb = await render_task(callback.from_user.id, index)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

async def award_sub(user_id: int, channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT OR IGNORE INTO subscriptions(user_id, channel_id) VALUES(?, ?)", (user_id, channel_id))
        if cur.rowcount:
            await db.execute("UPDATE users SET points=points+1 WHERE tg_id=?", (user_id,))
            await db.commit()
            return True
        return False

@dp.callback_query(F.data.startswith("check:"))
async def cb_check(callback: CallbackQuery):
    parts = callback.data.split(":")
    ch_id, index = int(parts[1]), int(parts[2])
    
    ch = await get_channel_by_id(ch_id)
    if not ch:
        await callback.answer("Канал не найден.", show_alert=True)
        return

    try:
        member = await bot.get_chat_member(chat_id=parse_chat_id(ch[1]), user_id=callback.from_user.id)
        is_sub = member.status in ("member", "administrator", "creator")
    except Exception:
        await callback.answer("Ошибка. Убедись, что бот админ в канале.", show_alert=True)
        return
    
    if not is_sub:
        await callback.answer("❌ Сначала подпишись на канал!", show_alert=True)
        return
        
    if await award_sub(callback.from_user.id, ch_id):
        await callback.answer("✅ +1 шанс!", show_alert=True)
        text, kb = await render_task(callback.from_user.id, index)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.answer("Ты уже получал за это шанс.")

@dp.callback_query(F.data.startswith("check_bot:"))
async def cb_check_bot(callback: CallbackQuery):
    parts = callback.data.split(":")
    ch_id, index = int(parts[1]), int(parts[2])
    
    if await award_sub(callback.from_user.id, ch_id):
        await callback.answer("✅ +1 шанс! (Доверяем на слово)", show_alert=True)
        text, kb = await render_task(callback.from_user.id, index)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.answer("Ты уже получал за это шанс.")

# ---------- ЗАЯВКИ В ЗАКРЫТЫЕ КАНАЛЫ ----------
@dp.chat_join_request()
async def join_request_handler(request: ChatJoinRequest):
    try:
        await bot.approve_chat_join_request(chat_id=request.chat.id, user_id=request.from_user.id)
        await bot.send_message(request.from_user.id, f"✅ Твоя заявка на канал «{request.chat.title}» одобрена!\nТеперь нажми кнопку «Проверить» в боте.")
    except Exception as e:
        logging.error(f"Ошибка одобрения заявки: {e}")

# ---------- РЕЙТИНГ ----------
@dp.callback_query(F.data == "top")
async def cb_top(callback: CallbackQuery):
    await callback.answer()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id, username, first_name, points, display_pref FROM users WHERE is_banned = 0 ORDER BY points DESC LIMIT 20")
        rows = await cur.fetchall()
    
    lines = ["🏆 <b>Топ-20 по шансам</b>\n"]
    if not rows:
        lines.append("Пока пусто.")
    for i, (tg_id, username, first_name, points, pref) in enumerate(rows, start=1):
        name = first_name if pref == 'name' and first_name else (f"@{username}" if username else f"id{tg_id}")
        if tg_id == callback.from_user.id:
            lines.append(f"<b>{i}. {name} — {points} 👈 (Это ты)</b>")
        else:
            lines.append(f"{i}. {name} — {points}")

    user_in_top = any(row[0] == callback.from_user.id for row in rows)
    if not user_in_top:
        rank, points = await get_user_rank(callback.from_user.id)
        if rank > 0: lines.append(f"\n\nТвое место: <b>{rank}</b> (шансов: {points})")
        
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=back_kb())

# ---------- ПОКУПКА ШАНСОВ (STARS) ----------
@dp.callback_query(F.data == "buy_points")
async def cb_buy_points(callback: CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Купить 1 шанс (1 ⭐️)", callback_data="pay_1")],
        [InlineKeyboardButton(text="Купить 5 шансов (5 ⭐️)", callback_data="pay_5")],
        [InlineKeyboardButton(text="Купить 10 шансов (10 ⭐️)", callback_data="pay_10")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
    ])
    await callback.message.edit_text("🛒 <b>Магазин шансов</b>\n1 Звезда = 1 Шанс.\nВыбери пакет:", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay(callback: CallbackQuery):
    await callback.answer()
    amount = int(callback.data.split("_")[1])
    
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Покупка {amount} шансов",
        description=f"Ты получишь {amount} шансов в рейтинге.",
        payload=f"points_{amount}",
        provider_token="", 
        currency="XTR", 
        prices=[LabeledPrice(label="Шансы", amount=amount)]
    )

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    amount = int(payload.split("_")[1])
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET points = points + ? WHERE tg_id=?", (amount, message.from_user.id))
        await db.commit()
    
    await message.answer(f"🎉 Оплата прошла успешно! Тебе начислено <b>{amount}</b> шансов.", parse_mode="HTML")

# ---------- АДМИНКА ----------
@dp.message(Command("admin"))
async def admin_menu(message: Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer(
        "👑 <b>Админ-панель</b>\n\n"
        "/ban ID - Заблокировать\n"
        "/unban ID - Разблокировать\n"
        "/reset ID - Сбросить шансы и подписки\n"
        "/addpoints ID КОЛ-ВО - Начислить\n"
        "/removepoints ID КОЛ-ВО - Забрать\n"
        "/addchannel - Добавить канал/бота\n"
        "/stats - Статистика по каналам\n"
        "/checkall - Проверить отписки",
        parse_mode="HTML"
    )

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID: return
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Получаем каналы и считаем количество подписок для каждого
        cur = await db.execute("""
            SELECT c.title, c.type, COUNT(s.user_id) as sub_count 
            FROM channels c
            LEFT JOIN subscriptions s ON c.id = s.channel_id
            WHERE c.active = 1
            GROUP BY c.id
            ORDER BY sub_count DESC
        """)
        rows = await cur.fetchall()

    if not rows:
        await message.answer("📊 Пока нет добавленных каналов или подписок.")
        return

    text = "📊 <b>Статистика по каналам</b>\n\n"
    for title, c_type, count in rows:
        icon = "📢" if c_type == 'channel' else "🤖"
        text += f"{icon} «{title}»: <b>{count}</b> чел.\n"

    await message.answer(text, parse_mode="HTML")


@dp.message(Command("addchannel"))
async def cmd_addchannel_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await message.answer(
        "Шаг 1 из 3.\n\nПерешли мне <b>любое сообщение из канала</b>, который хочешь добавить. "
        "(Или отправь @username, если канал публичный).\n\n"
        "<i>Это нужно, чтобы я узнал внутренний ID канала для проверки подписок.</i>",
        parse_mode="HTML"
    )
    await state.set_state(AddChannelState.waiting_for_forward)

@dp.message(AddChannelState.waiting_for_forward)
async def process_addchannel_step1(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    
    chat_id = None
    title = None

    # Если админ переслал сообщение из канала
    if message.forward_from_chat:
        chat_id = message.forward_from_chat.id
        title = message.forward_from_chat.title
    # Если админ просто написал @username
    elif message.text and (message.text.startswith("@") or "t.me/" in message.text):
        text = message.text.strip()
        if "t.me/" in text:
            username = text.split("t.me/")[1].split("/")[0]
            text = "@" + username
        try:
            chat = await bot.get_chat(text)
            chat_id = chat.id
            title = chat.title
        except Exception:
            await message.answer("❌ Не удалось найти канал по этой ссылке. Попробуй переслать сообщение из канала.")
            return
    else:
        await message.answer("❌ Пожалуйста, перешли сообщение из канала или отправь @username.")
        return

    await state.update_data(ch_id=str(chat_id), title=title)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Это Telegram-канал", callback_data="addtype:channel")],
        [InlineKeyboardButton(text="Это Telegram-БОТ", callback_data="addtype:bot")]
    ])
    await message.answer(f"Шаг 2 из 3.\n\nНашел: <b>«{title}»</b> (ID: <code>{chat_id}</code>).\nЧто это?", parse_mode="HTML", reply_markup=kb)
    await state.set_state(AddChannelState.waiting_for_type)

@dp.callback_query(F.data.startswith("addtype:"), AddChannelState.waiting_for_type)
async def process_addchannel_step2(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.answer()
    
    ch_type = callback.data.split(":")[1]
    await state.update_data(ch_type=ch_type)
    
    await callback.message.edit_text(
        "Шаг 3 из 3.\n\n"
        "Отправь мне ссылку, которую увидят пользователи (кнопка «Подписаться»/«Перейти»).\n\n"
        "Это может быть приватная ссылка-приглашение, которую дал тебе админ канала (например, <code>https://t.me/+AbCdEf...</code>)."
    )
    await state.set_state(AddChannelState.waiting_for_link)

@dp.message(AddChannelState.waiting_for_link)
async def process_addchannel_step3(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    
    url = message.text.strip()
    if not (url.startswith("https://") or url.startswith("http://") or url.startswith("t.me/")):
        await message.answer("❌ Отправь правильную ссылку (начиная с https:// или t.me/).")
        return

    if url.startswith("t.me/"):
        url = "https://" + url

    data = await state.get_data()
    ch_id = data.get('ch_id')
    title = data.get('title')
    ch_type = data.get('ch_type')

    async with aiosqlite.connect(DB_PATH) as db:
        # Используем INSERT OR REPLACE, если канал уже был
        await db.execute(
            "INSERT OR REPLACE INTO channels(chat_id, title, url, type, active) VALUES(?, ?, ?, ?, 1)", 
            (ch_id, title, url, ch_type)
        )
        await db.commit()
    
    await state.clear()
    await message.answer(
        f"✅ Успешно!\n\nКанал «{title}» добавлен в задания.\nСсылка для перехода сохранена.", 
        parse_mode="HTML"
    )

# ... (Остальные админ-команды ban, unban, reset, addpoints, removepoints, checkall оставляем без изменений, они были в прошлом коде) ...

@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid = int(message.text.split()[1])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET is_banned = 1 WHERE tg_id=?", (uid,))
            await db.commit()
        await message.answer(f"⛔️ Пользователь {uid} заблокирован.")
    except: await message.answer("Формат: /ban 123456789")

@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid = int(message.text.split()[1])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET is_banned = 0 WHERE tg_id=?", (uid,))
            await db.commit()
        await message.answer(f"✅ Пользователь {uid} разблокирован.")
    except: await message.answer("Формат: /unban 123456789")

@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid = int(message.text.split()[1])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET points = 0 WHERE tg_id=?", (uid,))
            await db.execute("DELETE FROM subscriptions WHERE user_id=?", (uid,))
            await db.commit()
        await message.answer(f"🔄 Пользователь {uid} удален из рейтинга. Может заново выполнить задания.")
    except: await message.answer("Формат: /reset 123456789")

@dp.message(Command("addpoints"))
async def cmd_addpoints(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        args = message.text.split()
        uid, amt = int(args[1]), int(args[2])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET points = points + ? WHERE tg_id=?", (amt, uid))
            await db.commit()
        await message.answer(f"✅ Начислено {amt} шансов пользователю {uid}.")
    except: await message.answer("Формат: /addpoints 123456789 5")

@dp.message(Command("removepoints"))
async def cmd_removepoints(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        args = message.text.split()
        uid, amt = int(args[1]), int(args[2])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET points = points - ? WHERE tg_id=?", (amt, uid))
            await db.commit()
        await message.answer(f"✅ Списано {amt} шансов у пользователя {uid}.")
    except: await message.answer("Формат: /removepoints 123456789 5")

@dp.message(Command("checkall"))
async def cmd_check_all(message: Message):
    if message.from_user.id != ADMIN_ID: return
    status_msg = await message.answer("🔄 Проверяю отписки...")
    
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, channel_id FROM subscriptions")
        subs = await cur.fetchall()

    kicked = 0
    for uid, ch_id in subs:
        ch = await get_channel_by_id(ch_id)
        if ch and ch[4] == 'channel':
            try:
                member = await bot.get_chat_member(chat_id=parse_chat_id(ch[1]), user_id=uid)
                if member.status not in ("member", "administrator", "creator"):
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE users SET points = points - 1 WHERE tg_id=?", (uid,))
                        await db.execute("DELETE FROM subscriptions WHERE user_id=? AND channel_id=?", (uid, ch_id))
                        await db.commit()
                    kicked += 1
            except: pass
            await asyncio.sleep(0.05)
    
    await status_msg.edit_text(f"✅ Проверка завершена. Списано шансов у отписчиков: <b>{kicked}</b>", parse_mode="HTML")

# ---------- ЗАПУСК ----------
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())