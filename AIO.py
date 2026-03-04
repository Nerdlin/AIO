from dotenv import load_dotenv
import json
import os
import re
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
import pytz
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup,
                           InlineKeyboardButton, FSInputFile)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from openai import OpenAI
from app_utils import (
    generate_unique_code,
    validate_email,
    validate_phone,
    sanitize_filename,
    get_user_storage_dir,
    resolve_user_file_path,
)

load_dotenv()

API_TOKEN = os.getenv("API_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
client: Optional[OpenAI] = OpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None

logging.basicConfig(level=logging.INFO)

bot: Optional[Bot] = Bot(token=API_TOKEN) if API_TOKEN else None
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

DATA_FILE = 'users_data.json'
TASKS_FILE = 'tasks_data.json'
FILE_STORAGE_PATH = Path('user_files')
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024
FILE_STORAGE_PATH.mkdir(parents=True, exist_ok=True)

almaty_tz = pytz.timezone('Asia/Almaty')

user_events = {}
conversation_history = {}

DISCORD_INVITE_PATTERN = re.compile(r'https://discord.gg/Gy4xbacfES', re.IGNORECASE)

start_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Регистрация"), KeyboardButton(text="Мои данные"), KeyboardButton(text="Редактировать данные")],
        [KeyboardButton(text="Создать задачу"), KeyboardButton(text="Показать расписание"), KeyboardButton(text="Удалить задачу")],
        [KeyboardButton(text="GPT чат"), KeyboardButton(text="Загрузить файл"), KeyboardButton(text="Файлы")]
    ],
    resize_keyboard=True
)

edit_data_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Имя", callback_data="edit_name")],
        [InlineKeyboardButton(text="Фамилия", callback_data="edit_surname")],
        [InlineKeyboardButton(text="Телефон", callback_data="edit_phone")],
        [InlineKeyboardButton(text="Email", callback_data="edit_email")]
    ]
)

cancel_registration_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Отменить регистрацию", callback_data="cancel_registration")]
    ]
)

close_gpt_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Закрыть GPT", callback_data="close_gpt")]
    ]
)

TASKS_LOCK = asyncio.Lock()
GPT_HISTORY_MAX = 12
gpt_semaphore = asyncio.Semaphore(1)

def load_user_data():
    try:
        if not os.path.exists(DATA_FILE):
            return {}
        with open(DATA_FILE, 'r', encoding='utf-8') as file:
            return json.load(file)
    except Exception as e:
        logging.exception("Ошибка при загрузке данных: %s", e)
        return {}

def save_user_data(user_data):
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as file:
            json.dump(user_data, file, ensure_ascii=False, indent=4)
    except Exception as e:
        logging.exception("Ошибка при сохранении данных: %s", e)

def is_user_registered(user_id):
    all_user_data = load_user_data()
    return str(user_id) in all_user_data

def get_bot() -> Bot:
    if bot is None:
        raise RuntimeError("Не задан API_TOKEN в .env")
    return bot

def clear_chat_history(user_id):
    conversation_history[user_id] = []

def contains_prohibited_link(text):
    return bool(DISCORD_INVITE_PATTERN.search(text))

def load_tasks():
    if not os.path.exists(TASKS_FILE):
        return {}
    try:
        with open(TASKS_FILE, 'r', encoding='utf-8') as f:
            raw = f.read().strip()
            if not raw:
                return {}
            data = json.loads(raw)
        for uid, tasks in data.items():
            converted = []
            for t in tasks:
                try:
                    dt = datetime.fromisoformat(t.get('date_iso', ''))
                    if dt.tzinfo is None:
                        dt = almaty_tz.localize(dt)
                    converted.append({'name': t.get('name', 'Без названия'), 'date': dt})
                except Exception:
                    continue
            data[uid] = converted
        return data
    except json.JSONDecodeError:
        logging.warning("Файл задач поврежден, создаю пустой.")
        return {}
    except Exception as e:
        logging.exception("Ошибка загрузки задач: %s", e)
        return {}

def save_tasks():
    serializable = {}
    for uid, tasks in user_events.items():
        serializable[uid] = [{'name': t['name'], 'date_iso': t['date'].isoformat()} for t in tasks]
    try:
        with open(TASKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(serializable, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logging.exception("Ошибка сохранения задач: %s", e)

async def save_tasks_async():
    async with TASKS_LOCK:
        save_tasks()

user_events = load_tasks()

async def check_events():
    while True:
        now = datetime.now(almaty_tz)
        for user_id, events in list(user_events.items()):
            for event in list(events):
                if event['date'] <= now:
                    try:
                        await get_bot().send_message(int(user_id), f"Напоминание: '{event['name']}' наступило!")
                    except Exception as e:
                        logging.warning(f"Не удалось отправить напоминание {user_id}: {e}")
                    events.remove(event)
                    await save_tasks_async()
        await asyncio.sleep(60)

@dp.message(Command("start"))
async def send_welcome(message: types.Message):
    user_id = message.from_user.id
    clear_chat_history(user_id)
    await message.reply("Привет! Я AIO и помогу тебе с задачами, файлами и GPT. Используй меню ниже.", reply_markup=start_keyboard)

@dp.message(Command("cancel"))
async def cancel_any_state(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=start_keyboard)

@dp.message(F.text == "Регистрация")
async def register_command(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if is_user_registered(user_id):
        await message.answer("Вы уже зарегистрированы. Регистрация повторно невозможна.")
    else:
        await message.answer("Введите ваше имя:", reply_markup=cancel_registration_kb)
        await state.set_state(Registration.name)

class Registration(StatesGroup):
    name = State()
    surname = State()
    phone = State()
    email = State()
    confirmation = State()

@dp.message(Registration.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Введите вашу фамилию:", reply_markup=cancel_registration_kb)
    await state.set_state(Registration.surname)

@dp.message(Registration.surname)
async def process_surname(message: Message, state: FSMContext):
    await state.update_data(surname=message.text)
    await message.answer("Введите ваш номер телефона:", reply_markup=cancel_registration_kb)
    await state.set_state(Registration.phone)

@dp.message(Registration.phone)
async def process_phone(message: Message, state: FSMContext):
    if not validate_phone(message.text):
        await message.answer("Номер телефона неверен. Формат: только цифры, можно '+' в начале, длина 10-15.")
        return
    await state.update_data(phone=message.text)
    await message.answer("Введите вашу электронную почту:", reply_markup=cancel_registration_kb)
    await state.set_state(Registration.email)

@dp.message(Registration.email)
async def process_email(message: Message, state: FSMContext):
    if not validate_email(message.text):
        await message.answer("Неправильный формат email. Пожалуйста, введите корректный адрес.")
        return
    await state.update_data(email=message.text)
    user_data = await state.get_data()

    confirmation_message = (
        f"Пожалуйста, подтвердите введенные данные:\n"
        f"Имя: {user_data['name']}\n"
        f"Фамилия: {user_data['surname']}\n"
        f"Телефон: {user_data['phone']}\n"
        f"Email: {user_data['email']}\n\n"
        f"Если все верно, введите 'да'. Если нет, введите 'нет'."
    )

    await message.answer(confirmation_message, reply_markup=cancel_registration_kb)
    await state.set_state(Registration.confirmation)

@dp.message(Registration.confirmation)
async def process_confirmation(message: Message, state: FSMContext):
    if message.text.lower() == 'да':
        user_data = await state.get_data()
        user_id = str(message.from_user.id)
        unique_code = generate_unique_code()
        user_data['user_id'] = user_id
        user_data['unique_code'] = unique_code

        all_user_data = load_user_data()
        all_user_data[user_id] = user_data
        save_user_data(all_user_data)

        await message.answer(f"Ваши данные сохранены. Ваш уникальный код: {unique_code}")
        await state.clear()
    elif message.text.lower() == 'нет':
        await message.answer("Вы хотите отредактировать данные. Введите 'Регистрация' для повторного ввода.")
        await state.clear()

@dp.callback_query(F.data == "cancel_registration")
async def cancel_registration(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Регистрация отменена.", reply_markup=start_keyboard)
    await cb.answer()

@dp.message(F.text == "Мои данные")
async def show_user_data(message: Message):
    user_id = str(message.from_user.id)
    all_user_data = load_user_data()

    if user_id in all_user_data:
        user_data = all_user_data[user_id]
        data_message = (
            f"Ваши данные:\n"
            f"Имя: {user_data['name']}\n"
            f"Фамилия: {user_data['surname']}\n"
            f"Телефон: {user_data['phone']}\n"
            f"Email: {user_data['email']}\n"
            f"Уникальный код: {user_data['unique_code']}"
        )
        await message.answer(data_message)
    else:
        await message.answer("Вы не зарегистрированы.")

@dp.message(F.text == "Редактировать данные")
async def edit_user_data(message: Message):
    user_id = str(message.from_user.id)
    all_user_data = load_user_data()

    if user_id in all_user_data:
        await message.answer("Что вы хотите изменить?", reply_markup=edit_data_kb)
    else:
        await message.answer("Вы не зарегистрированы.")

@dp.callback_query(lambda callback_query: callback_query.data.startswith('edit_'))
async def process_edit_selection(callback_query: types.CallbackQuery, state: FSMContext):
    field_to_edit = callback_query.data.split('_')[1]
    await state.update_data(edit_field=field_to_edit)
    await callback_query.message.answer(f"Введите новое значение для {field_to_edit}:")
    await state.set_state(EditData.new_value)

class EditData(StatesGroup):
    edit_field = State()
    new_value = State()

@dp.message(EditData.new_value)
async def process_new_value(message: Message, state: FSMContext):
    user_id = str(message.from_user.id)
    all_user_data = load_user_data()

    if user_id in all_user_data:
        data = await state.get_data()
        field = data['edit_field']
        all_user_data[user_id][field] = message.text
        save_user_data(all_user_data)
        await message.answer(f"{field.capitalize()} успешно обновлено.")
        await state.clear()
    else:
        await message.answer("Вы не зарегистрированы.")

@dp.message(F.text == "Загрузить файл")
async def prompt_file_upload(message: types.Message):
    await message.answer("Отправь файл. Имя будет безопасно сохранено.")

@dp.message(F.document)
async def handle_file_upload(message: types.Message):
    document = message.document
    if document.file_size and document.file_size > MAX_FILE_SIZE_BYTES:
        max_size_mb = MAX_FILE_SIZE_BYTES // (1024 * 1024)
        await message.answer(f"Файл слишком большой. Максимум: {max_size_mb} МБ.")
        return
    file_info = await get_bot().get_file(document.file_id)
    safe_name = sanitize_filename(document.file_name)
    file_path = resolve_user_file_path(FILE_STORAGE_PATH, message.from_user.id, safe_name)
    await get_bot().download(file_info, destination=file_path)
    await message.answer(f"Файл '{safe_name}' сохранён.")

def create_file_keyboard(files):
    if not files:
        return None
    rows = [[InlineKeyboardButton(text=fn, callback_data=f"download::{fn}")]
            for fn in files]
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(lambda c: c.data.startswith('download::'))
async def send_file(callback_query: types.CallbackQuery):
    file_name = callback_query.data.split('::')[1]
    try:
        file_path = resolve_user_file_path(FILE_STORAGE_PATH, callback_query.from_user.id, file_name)
    except ValueError:
        await callback_query.message.answer("Некорректное имя файла.")
        return
    if file_path.exists():
        input_file = FSInputFile(file_path)
        await get_bot().send_document(chat_id=callback_query.from_user.id, document=input_file)
        await callback_query.answer()
    else:
        await callback_query.message.answer("Файл не найден.")

@dp.message(F.text == "Файлы")
async def list_user_files(message: types.Message):
    user_dir = get_user_storage_dir(FILE_STORAGE_PATH, message.from_user.id)
    files = sorted([p.name for p in user_dir.iterdir() if p.is_file()])
    if files:
        kb = create_file_keyboard(files)
        await message.answer("Выбери файл:", reply_markup=kb)
    else:
        await message.answer("Нет файлов.")

@dp.message(F.text == "GPT чат")
async def start_gpt_chat(message: types.Message, state: FSMContext):
    clear_chat_history(message.from_user.id)
    await message.answer("Вы в GPT чате. Напишите сообщение. /cancel или кнопка для выхода.", reply_markup=close_gpt_kb)
    await state.set_state(GPTQuestionState.waiting_for_question)

class GPTQuestionState(StatesGroup):
    waiting_for_question = State()

async def trim_history(user_id: int):
    history = conversation_history.get(user_id, [])
    while len(history) > GPT_HISTORY_MAX * 2:
        history = history[2:]
    conversation_history[user_id] = history

async def ask_gpt(user_id: int, user_input: str):
    if client is None:
        return "OPENAI_API_KEY не задан. GPT-чат недоступен."
    await trim_history(user_id)
    history = conversation_history.get(user_id, [])
    messages = history + [{"role": "user", "content": user_input}]
    attempts = 3
    backoff_base = 2
    last_error = None

    for attempt in range(attempts):
        def _call():
            return client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.7,
                max_tokens=300
            )
        try:
            async with gpt_semaphore:
                response = await asyncio.to_thread(_call)
            answer = response.choices[0].message.content.strip()
            history.extend([
                {"role": "user", "content": user_input},
                {"role": "assistant", "content": answer}
            ])
            conversation_history[user_id] = history
            return answer
        except Exception as e:
            err_text = str(e)
            last_error = err_text
            if any(k in err_text.lower() for k in ["quota", "insufficient_quota"]):
                return "Недостаточно квоты OpenAI."
            if "429" in err_text or "rate limit" in err_text.lower():
                await asyncio.sleep(backoff_base ** attempt)
                continue
            if "timeout" in err_text.lower():
                await asyncio.sleep(backoff_base ** attempt)
                continue
            return f"Ошибка GPT: {err_text}"
    return f"Ошибка GPT после повторов: {last_error}"

@dp.message(GPTQuestionState.waiting_for_question)
async def gpt_multi_turn(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text
    if contains_prohibited_link(text):
        await message.answer("Ссылка запрещена.")
        return
    await message.answer("Обрабатываю...")
    reply = await ask_gpt(user_id, text)
    await message.answer(f"AIO:\n{reply}", reply_markup=close_gpt_kb)

@dp.callback_query(lambda c: c.data == "close_gpt")
async def close_gpt_session(callback_query: types.CallbackQuery, state: FSMContext):
    await state.clear()
    clear_chat_history(callback_query.from_user.id)
    await callback_query.message.answer("GPT чат закрыт.", reply_markup=start_keyboard)

@dp.message(F.text == "Создать задачу")
async def create_task(message: types.Message, state: FSMContext):
    await state.set_state(ScheduleForm.event_name)
    await message.reply("Название задачи?", reply_markup=start_keyboard)

class ScheduleForm(StatesGroup):
    event_name = State()
    event_date = State()

@dp.message(ScheduleForm.event_name)
async def process_task_name(message: types.Message, state: FSMContext):
    await state.update_data(event_name=message.text)
    await state.set_state(ScheduleForm.event_date)
    await message.reply("Дата и время в формате 'YYYY-MM-DD HH:MM' (часовой пояс Алматы).")

@dp.message(ScheduleForm.event_date)
async def process_task_date(message: types.Message, state: FSMContext):
    data = await state.get_data()
    event_name = data.get("event_name")
    user_id = message.from_user.id
    event_date = message.text
    try:
        dt = almaty_tz.localize(datetime.strptime(event_date, '%Y-%m-%d %H:%M'))
        if dt <= datetime.now(almaty_tz):
            await message.reply("Время уже прошло. Укажи будущее.")
            return
        user_events.setdefault(str(user_id), []).append({'name': event_name, 'date': dt})
        await save_tasks_async()
        await message.reply(f"Задача '{event_name}' на {event_date} добавлена.")
    except ValueError:
        await message.reply("Неверный формат. Пример: 2025-12-31 14:30")
    await state.clear()

@dp.message(F.text == "Показать расписание")
async def show_schedule(message: types.Message):
    uid = str(message.from_user.id)
    tasks = user_events.get(uid, [])
    if not tasks:
        await message.reply("Расписание пусто.")
        return
    lines = ["Ваши задачи:"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t['name']} - {t['date'].strftime('%Y-%m-%d %H:%M')}")
    await message.reply("\n".join(lines))

class TaskDeletion(StatesGroup):
    waiting_index = State()

@dp.message(F.text == "Удалить задачу")
async def delete_task(message: types.Message, state: FSMContext):
    uid = str(message.from_user.id)
    tasks = user_events.get(uid, [])
    if not tasks:
        await message.reply("Нет задач.")
        return
    lines = ["Номер для удаления:"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t['name']} - {t['date'].strftime('%Y-%m-%d %H:%M')}")
    await message.reply("\n".join(lines))
    await state.set_state(TaskDeletion.waiting_index)

@dp.message(TaskDeletion.waiting_index)
async def process_task_deletion(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.reply("Введите номер (цифра). /cancel для выхода.")
        return
    uid = str(message.from_user.id)
    idx = int(message.text) - 1
    tasks = user_events.get(uid, [])
    if 0 <= idx < len(tasks):
        deleted = tasks.pop(idx)
        await save_tasks_async()
        await message.reply(f"Удалено: {deleted['name']}")
    else:
        await message.reply("Неверный номер.")
    await state.clear()

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    await message.answer("Команды: /start /cancel /help. Используйте клавиатуру для функций.")

@dp.message()
async def fallback_handler(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        return
    await message.answer("Не понял. Используй меню или /help.")

async def main():
    if not API_TOKEN:
        raise RuntimeError("Отсутствует API_TOKEN в .env")
    if not OPENAI_KEY:
        logging.warning("OPENAI_API_KEY не найден. GPT-чат будет недоступен.")

    asyncio.create_task(check_events())
    await dp.start_polling(get_bot())

if __name__ == "__main__":
    asyncio.run(main())


