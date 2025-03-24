from dotenv import load_dotenv
import json
import os
import re
import openai
import asyncio
import logging
import random
import string
from datetime import datetime, timedelta
import pytz
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup,
                           InlineKeyboardButton, FSInputFile)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext

#! Устанавливаем токен вашего бота и API-ключ OpenAI
load_dotenv()

API_TOKEN = os.getenv("API_TOKEN")
openai.api_key = os.getenv("OPENAI_API_KEY")


#! Логирование
logging.basicConfig(level=logging.INFO)

#! Инициализация бота и диспетчера
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

#! Файл для хранения данных и папка для файлов пользователей
DATA_FILE = 'users_data.json'
TOTAL_USERS_FILE = 'total_users_count.json'  #! Новый файл для хранения общего числа пользователей
FILE_STORAGE_PATH = 'user_files'
if not os.path.exists(FILE_STORAGE_PATH):
    os.makedirs(FILE_STORAGE_PATH)

#! Часовой пояс Алматы
almaty_tz = pytz.timezone('Asia/Almaty')

#! Словари для хранения событий и активных пользователей
user_events = {}
active_users = set()

conversation_history = {}

DISCORD_INVITE_PATTERN = re.compile(r'https://discord.gg/Gy4xbacfES', re.IGNORECASE)

#! Клавиатуры
start_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Создать задачу"), KeyboardButton(text="Показать расписание")],
        [KeyboardButton(text="Удалить задачу")],
        [KeyboardButton(text="Счетчик пользователей")],
        [KeyboardButton(text="Регистрация"), KeyboardButton(text="Задать вопрос GPT")],
        [KeyboardButton(text="Мои данные"), KeyboardButton(text="Редактировать данные")],
        [KeyboardButton(text="Загрузить файл"), KeyboardButton(text="Получить файл")]
    ],
    resize_keyboard=True
)

#! Инлайн-клавиатуры для редактирования данных и отмены регистрации
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

#! --- Загрузка и сохранение общего числа пользователей ---
def load_total_users_count():
    if os.path.exists(TOTAL_USERS_FILE):
        with open(TOTAL_USERS_FILE, 'r') as file:
            return int(file.read())
    return 0

def save_total_users_count(count):
    with open(TOTAL_USERS_FILE, 'w') as file:
        file.write(str(count))

#! Инициализация общего числа пользователей при старте
total_users_count = load_total_users_count()

#! --- Добавление пользователя ---
def add_new_user(user_id):
    global total_users_count
    if user_id not in active_users:
        active_users.add(user_id)
        total_users_count += 1
        save_total_users_count(total_users_count)  # Сохраняем количество пользователей

#! --- Функции для работы с данными пользователя ---
def load_user_data():
    try:
        if not os.path.exists(DATA_FILE):
            return {}
        with open(DATA_FILE, 'r', encoding='utf-8') as file:
            return json.load(file)
    except Exception as e:
        print(f"Ошибка при загрузке данных: {str(e)}")
        return {}

def save_user_data(user_data):
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as file:
            json.dump(user_data, file, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Ошибка при сохранении данных: {str(e)}")

def is_user_registered(user_id):
    all_user_data = load_user_data()
    return str(user_id) in all_user_data

def generate_unique_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def validate_email(email):
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    return re.match(pattern, email)

def clear_chat_history(user_id):
    conversation_history[user_id] = []

def contains_prohibited_link(text):
    return bool(DISCORD_INVITE_PATTERN.search(text))

#! --- Функция для проверки и уведомления задач ---
async def check_events():
    while True:
        now = datetime.now(almaty_tz)
        for user_id, events in list(user_events.items()):
            for event in events:
                event_time = event['date']
                #! Если время задачи пришло, уведомляем пользователя
                if event_time <= now:
                    await bot.send_message(user_id, f"Напоминание: Время для задачи '{event['name']}' наступило!")
                    events.remove(event)  #! Удаляем задачу после отправки уведомления
        await asyncio.sleep(60)  #! Проверяем задачи каждые 60 секунд

#! --- Команды бота ---
@dp.message(Command("start"))
async def send_welcome(message: types.Message):
    user_id = message.from_user.id
    clear_chat_history(user_id)

    # !Добавляем нового пользователя
    add_new_user(user_id)

    await message.reply("Привет! Я AIO который будет тебе помагать. Обращяйся за помощью в любое время 😊", reply_markup=start_keyboard)

#! --- Регистрация пользователя ---
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

        #! Добавляем пользователя после успешной регистрации
        add_new_user(user_id)

        await message.answer(f"Ваши данные сохранены. Ваш уникальный код: {unique_code}")
        await state.clear()
    elif message.text.lower() == 'нет':
        await message.answer("Вы хотите отредактировать данные. Введите 'Регистрация' для повторного ввода.")
        await state.clear()

#! --- Просмотр данных пользователя ---
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

#! --- Редактирование данных пользователя ---
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

#! Определяем состояния для процесса редактирования данных
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

#! --- Работа с файлами ---
@dp.message(F.text == "Загрузить файл")
async def prompt_file_upload(message: types.Message):
    await message.answer("Пожалуйста, загрузите файл (например, .docx):")

@dp.message(F.document)
async def handle_file_upload(message: types.Message):
    document = message.document
    file_info = await bot.get_file(document.file_id)
    file_path = f"{FILE_STORAGE_PATH}/{document.file_name}"
    await bot.download(file_info, destination=file_path)
    await message.answer(f"Файл {document.file_name} успешно загружен и сохранен.")

def create_file_keyboard(files):
    if not files:
        return None

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=file_name, callback_data=f"download_{file_name}") for file_name in files]
    ])
    return keyboard

@dp.callback_query(lambda callback_query: callback_query.data.startswith('download_'))
async def send_file(callback_query: types.CallbackQuery):
    file_name = callback_query.data.split('_')[1]
    file_path = f"{FILE_STORAGE_PATH}/{file_name}"

    if os.path.exists(file_path):
        input_file = FSInputFile(file_path)
        await bot.send_document(chat_id=callback_query.from_user.id, document=input_file)
        await callback_query.answer()
    else:
        await callback_query.message.answer("Файл не найден.")

@dp.message(F.text == "Получить файл")
async def list_user_files(message: types.Message):
    files = os.listdir(FILE_STORAGE_PATH)
    if files:
        keyboard = create_file_keyboard(files)
        await message.answer("Выберите файл для скачивания:", reply_markup=keyboard)
    else:
        await message.answer("Нет доступных файлов для скачивания.")

#! --- Работа с GPT ---
@dp.message(F.text == "Задать вопрос GPT")
async def ask_gpt_command(message: types.Message, state: FSMContext):
    await message.answer("Задайте ваш вопрос:", reply_markup=close_gpt_kb)
    await state.set_state(GPTQuestionState.waiting_for_question)

#! Определяем состояния для процесса работы с GPT
class GPTQuestionState(StatesGroup):
    waiting_for_question = State()


@dp.message(GPTQuestionState.waiting_for_question)
async def gpt_auto_reply(message: types.Message, state: FSMContext):
    user_question = message.text
    await message.answer("Обрабатываю ваш запрос...")

    gpt_response = await ask_gpt(user_question)
    await message.answer(f"AIO:\n{gpt_response}", reply_markup=close_gpt_kb)

async def ask_gpt(prompt):
    try:
        response = await asyncio.to_thread(openai.ChatCompletion.create,
                                           model="gpt-3.5-turbo",
                                           messages=[{"role": "user", "content": prompt}],
                                           max_tokens=50,
                                           n=1,
                                           temperature=0.7)
        return response['choices'][0]['message']['content'].strip()
    except Exception as e:
        return f"Ошибка при запросе: {str(e)}"

@dp.callback_query(lambda callback_query: callback_query.data == "close_gpt")
async def close_gpt_session(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.answer("Сеанс GPT завершен.", reply_markup=start_keyboard)
    await state.clear()

#! --- Работа с задачами ---
@dp.message(F.text == "Создать задачу")
async def create_task(message: types.Message, state: FSMContext):
    await state.set_state(ScheduleForm.event_name)
    await message.reply("Пожалуйста, введи название задачи.", reply_markup=start_keyboard)

#! Определение состояний для работы с задачами
class ScheduleForm(StatesGroup):
    event_name = State()
    event_date = State()


@dp.message(ScheduleForm.event_name)
async def process_task_name(message: types.Message, state: FSMContext):
    await state.update_data(event_name=message.text)
    await state.set_state(ScheduleForm.event_date)
    await message.reply("Теперь укажи дату и время в формате '2024-09-30 15:00'.")

@dp.message(ScheduleForm.event_date)
async def process_task_date(message: types.Message, state: FSMContext):
    data = await state.get_data()
    event_name = data.get("event_name")
    event_date = message.text
    user_id = message.from_user.id

    try:
        event_date_obj = almaty_tz.localize(datetime.strptime(event_date, '%Y-%m-%d %H:%M'))
        if event_date_obj <= datetime.now(almaty_tz):
            await message.reply("Указанное время уже прошло. Пожалуйста, укажи будущее время.")
            return

        if user_id not in user_events:
            user_events[user_id] = []
        user_events[user_id].append({'name': event_name, 'date': event_date_obj})

        await message.reply(f"Задача '{event_name}' на {event_date} добавлена.")
    except ValueError:
        await message.reply("Неверный формат даты. Пожалуйста, укажи в формате '2024-09-30 15:00'.")

    await state.clear()

@dp.message(F.text == "Показать расписание")
async def show_schedule(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_events or len(user_events[user_id]) == 0:
        await message.reply("Ваше расписание пусто.")
    else:
        schedule = "Ваше расписание:\n"
        for event in user_events[user_id]:
            schedule += f"{event['name']} - {event['date'].strftime('%Y-%m-%d %H:%M')}\n"
        await message.reply(schedule)

@dp.message(F.text == "Удалить задачу")
async def delete_task(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_events or len(user_events[user_id]) == 0:
        await message.reply("У вас нет задач для удаления.")
    else:
        task_list = "Выберите номер задачи для удаления:\n"
        for idx, event in enumerate(user_events[user_id], start=1):
            task_list += f"{idx}. {event['name']} - {event['date'].strftime('%Y-%m-%d %H:%M')}\n"
        await message.reply(task_list)

@dp.message(lambda message: message.text.isdigit())
async def process_task_deletion(message: types.Message):
    user_id = message.from_user.id
    task_num = int(message.text) - 1

    if user_id in user_events and 0 <= task_num < len(user_events[user_id]):
        deleted_task = user_events[user_id].pop(task_num)
        await message.reply(f"Задача '{deleted_task['name']}' была удалена.")
    else:
        await message.reply("Неверный номер задачи.")

@dp.message(F.text == "Счетчик пользователей")
async def show_user_count(message: types.Message):
    await message.reply(f"Общее количество пользователей: {total_users_count}")

#! Запуск бота
async def main():
    asyncio.create_task(check_events())  #! Запускаем задачу проверки событий
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
