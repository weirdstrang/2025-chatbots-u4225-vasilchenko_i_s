import asyncio
import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
import httpx
import pytz
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# это удалить
import os
print("🚀 МОЙ URL:", os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'НЕ НАЙДЕН'))
# выше удалить

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


############################################
# Конфигурация и константы
############################################

DATA_FILE = Path("tasks.json")
REMINDER_HOUR_LOCAL = 9  # Отправлять напоминания в 09:00 локального времени

# Состояния для обработчиков диалогов
NEW_TASK_TEXT, NEW_TASK_PRIORITY, NEW_TASK_DEADLINE = range(3)
SET_PRIORITY_CHOOSE_TASK, SET_PRIORITY_CHOOSE_VALUE = range(2)
SET_DEADLINE_CHOOSE_TASK, SET_DEADLINE_ENTER_DATE = range(2)
# Используем уникальные значения для состояния удаления, чтобы исключить пересечения
DELETE_CHOOSE_TASK, DELETE_CONFIRM = 100, 101


############################################
# Модель данных
############################################


@dataclass
class Task:
    task_id: str
    chat_id: int
    text: str
    priority: str  # low | medium | high
    deadline: Optional[str]  # ISO date YYYY-MM-DD

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


############################################
# API для получения шуток
############################################


async def get_random_joke() -> Optional[Dict[str, str]]:
    """
    Получает случайную шутку из API.
    
    Returns:
        Словарь с ключами 'setup' и 'punchline', или None в случае ошибки.
    """
    api_url = "https://official-joke-api.appspot.com/random_joke"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(api_url)
            response.raise_for_status()  # Вызовет исключение при статусе 4xx/5xx
            joke_data = response.json()
            # Проверяем, что в ответе есть нужные поля
            if "setup" in joke_data and "punchline" in joke_data:
                return {
                    "setup": joke_data["setup"],
                    "punchline": joke_data["punchline"]
                }
            else:
                logger.warning("API вернул неожиданную структуру данных: %s", joke_data)
                return None
    except httpx.TimeoutException:
        logger.error("Таймаут при запросе к API шуток")
        return None
    except httpx.RequestError as exc:
        logger.error("Ошибка при запросе к API шуток: %s", exc)
        return None
    except Exception as exc:
        logger.exception("Неожиданная ошибка при получении шутки: %s", exc)
        return None


############################################
# Утилиты хранения данных (JSON)
############################################


def load_tasks() -> List[Task]:
    if not DATA_FILE.exists():
        return []
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        tasks = [Task(**item) for item in raw]
        return tasks
    except Exception as exc:  # Логируем и начинаем с пустого списка
        logger.exception("Не удалось загрузить tasks.json: %s", exc)
        return []


def save_tasks(tasks: List[Task]) -> None:
    # Атомарная запись: записываем во временный файл и переименовываем
    tmp_path = DATA_FILE.with_suffix(".json.tmp")
    raw = [t.to_dict() for t in tasks]
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    tmp_path.replace(DATA_FILE)


def generate_task_id(chat_id: int, now: Optional[datetime] = None) -> str:
    base = now or datetime.utcnow()
    return f"{chat_id}-{int(base.timestamp()*1000)}"


def get_user_tasks(tasks: List[Task], chat_id: int) -> List[Task]:
    return [t for t in tasks if t.chat_id == chat_id]


############################################
# Планирование напоминаний
############################################


def parse_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return None


def make_local_dt(date_only: datetime, tz: pytz.BaseTzInfo, hour: int = REMINDER_HOUR_LOCAL) -> datetime:
    naive = datetime.combine(date_only.date(), time(hour=hour, minute=0, second=0, microsecond=0))
    return tz.localize(naive)


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data: Dict[str, Any] = context.job.data or {}
    chat_id = job_data.get("chat_id")
    text = job_data.get("text")
    deadline = job_data.get("deadline")
    when_label = job_data.get("when_label")  # 'за 14 дней', 'за 7 дней', 'сегодня'

    if not chat_id or not text:
        return

    msg = (
        "Напоминание ✨\n"
        f"Задача: {text}\n"
        + (f"Дедлайн: {deadline}\n" if deadline else "")
        + (f"Срок: {when_label}\n" if when_label else "")
        + "Ты отлично справишься! 💪"
    )
    try:
        await context.bot.send_message(chat_id=chat_id, text=msg)
    except Exception as exc:
        logger.exception("Ошибка отправки напоминания: %s", exc)


async def send_punchline_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет punchline шутки через заданное время после уведомления о создании задачи.
    """
    job_data: Dict[str, Any] = context.job.data or {}
    chat_id = job_data.get("chat_id")
    punchline = job_data.get("punchline")

    if not chat_id or not punchline:
        return

    try:
        await context.bot.send_message(chat_id=chat_id, text=punchline)
    except Exception as exc:
        logger.exception("Ошибка отправки punchline: %s", exc)


def schedule_task_reminders(
    application: Application, task: Task, tz: pytz.BaseTzInfo
) -> None:
    if not task.deadline:
        return
    deadline_dt = parse_date(task.deadline)
    if not deadline_dt:
        return

    now = datetime.now(tz)

    # Даты напоминаний: -14, -7 и 0 дней от дедлайна в 09:00
    dates: List[Tuple[str, datetime]] = []
    for days, label in [(-14, "за 14 дней"), (-7, "за 7 дней"), (0, "сегодня")]:
        target_date = deadline_dt + timedelta(days=days)
        run_at = make_local_dt(target_date, tz)
        dates.append((label, run_at))

    for label, run_at in dates:
        if run_at > now:
            application.job_queue.run_once(
                reminder_job,
                when=run_at,
                data={
                    "chat_id": task.chat_id,
                    "text": task.text,
                    "deadline": task.deadline,
                    "when_label": label,
                },
                name=f"reminder-{task.task_id}-{label}",
            )


def reschedule_all(application: Application, tz: pytz.BaseTzInfo) -> None:
    # Очищаем все существующие задания и пересоздаём
    application.job_queue.scheduler.remove_all_jobs()
    for t in load_tasks():
        schedule_task_reminders(application, t, tz)


############################################
# Обработчики команд
############################################


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! Я дружелюбный бот-органайзер 😊\n\n"
        "Я помогу поставить задачу, приоритет и дедлайн, а также напомню за 14 дней, 7 дней и в день дедлайна.\n\n"
        "Команды:\n"
        "/newtask — поставить задачу\n"
        "/setpriority — установить приоритет задачи\n"
        "/setdeadline — поставить дату дедлайна\n"
        "/list — посмотреть все задачи\n"
        "/deletetask — удалить задачу\n"
        "/getrandomjoke — получить случайную шутку"
    )
    await update.message.reply_text(text)


async def get_random_joke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик команды /getrandomjoke.
    Отправляет пользователю случайную шутку (setup + punchline в одном сообщении).
    """
    # Отправляем сообщение о том, что запрашиваем шутку
    await update.message.reply_text("Запрашиваю шутку... 🎭")
    
    # Получаем шутку из API
    joke = await get_random_joke()
    
    if joke:
        # Формируем сообщение со всей шуткой
        joke_msg = f"🎭 Случайная шутка:\n\n{joke['setup']}\n\n{joke['punchline']}"
        await update.message.reply_text(joke_msg)
    else:
        # Если не удалось получить шутку, отправляем сообщение об ошибке
        await update.message.reply_text(
            "Извините, не удалось получить шутку. Попробуйте позже. 😔"
        )


############################################
# Диалог: новая задача
############################################


async def newtask_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Отправь текст задачи в одном сообщении. Как только отправишь — продолжим!"
    )
    return NEW_TASK_TEXT


async def newtask_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_task_text"] = update.message.text.strip()
    keyboard = [["low", "medium", "high"]]
    await update.message.reply_text(
        "Выбери приоритет задачи:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return NEW_TASK_PRIORITY


async def newtask_receive_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    priority = update.message.text.strip().lower()
    if priority not in {"low", "medium", "high"}:
        await update.message.reply_text(
            "Пожалуйста, выбери один из вариантов: low, medium или high."
        )
        return NEW_TASK_PRIORITY
    context.user_data["new_task_priority"] = priority
    await update.message.reply_text(
        "Укажи дедлайн в формате ГГГГ-ММ-ДД (например, 2025-12-31).\n"
        "Если дедлайн пока не известен — отправь '-', и мы пропустим этот шаг.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return NEW_TASK_DEADLINE


async def newtask_receive_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    text = context.user_data.get("new_task_text", "")
    priority = context.user_data.get("new_task_priority", "medium")
    deadline_input = update.message.text.strip()

    deadline_iso: Optional[str]
    if deadline_input == "-":
        deadline_iso = None
    else:
        dt = parse_date(deadline_input)
        if not dt:
            await update.message.reply_text(
                "Не получилось распознать дату. Введи в формате ГГГГ-ММ-ДД или '-'."
            )
            return NEW_TASK_DEADLINE
        deadline_iso = dt.strftime("%Y-%m-%d")

    # Создаём и сохраняем задачу
    tasks = load_tasks()
    task = Task(
        task_id=generate_task_id(chat_id),
        chat_id=chat_id,
        text=text,
        priority=priority,
        deadline=deadline_iso,
    )
    tasks.append(task)
    save_tasks(tasks)

    # Планируем напоминания для этой задачи
    tz = context.application.bot_data["tz"]
    schedule_task_reminders(context.application, task, tz)

    # Получаем случайную шутку для уведомления
    joke = await get_random_joke()
    
    # Формируем сообщение о создании задачи
    task_msg = (
        "Задача создана! ✨\n"
        f"Текст: {task.text}\n"
        f"Приоритет: {task.priority}\n"
        f"Дедлайн: {task.deadline or 'не указан'}\n"
        "Я напомню в нужные даты."
    )
    
    # Если получили шутку, добавляем setup к сообщению
    if joke:
        task_msg += f"\n\n🎭 А вот шутка для настроения:\n{joke['setup']}"
    
    await update.message.reply_text(task_msg)

    # Если получили шутку, планируем отправку punchline через 10 секунд
    if joke and joke.get("punchline"):
        context.application.job_queue.run_once(
            send_punchline_job,
            when=10.0,  # 10 секунд
            data={
                "chat_id": chat_id,
                "punchline": joke["punchline"],
            },
            name=f"punchline-{task.task_id}",
        )

    # Чистим временные данные диалога
    context.user_data.pop("new_task_text", None)
    context.user_data.pop("new_task_priority", None)
    return ConversationHandler.END


async def newtask_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Создание задачи отменено.", reply_markup=ReplyKeyboardRemove())
    context.user_data.pop("new_task_text", None)
    context.user_data.pop("new_task_priority", None)
    return ConversationHandler.END


############################################
# Диалог: установить приоритет существующей задачи
############################################


def render_tasks_for_choice(tasks: List[Task]) -> str:
    if not tasks:
        return "Задач пока нет. Добавь новую через /newtask"
    lines = ["Список задач:"]
    for idx, t in enumerate(tasks, start=1):
        lines.append(f"{idx}. {t.text} | приоритет: {t.priority} | дедлайн: {t.deadline or '—'}")
    lines.append("\nОтветь номером задачи.")
    return "\n".join(lines)


async def setpriority_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    tasks = get_user_tasks(load_tasks(), chat_id)
    await update.message.reply_text(render_tasks_for_choice(tasks))
    context.user_data["delete_tasks_cache"] = [t.to_dict() for t in tasks]
    return SET_PRIORITY_CHOOSE_TASK


async def setpriority_choose_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    tasks_raw = context.user_data.get("delete_tasks_cache", [])
    if not tasks_raw:
        await update.message.reply_text("Задач нет. Создай через /newtask")
        return ConversationHandler.END
    try:
        idx = int(text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, пришли номер задачи (целое число).")
        return SET_PRIORITY_CHOOSE_TASK

    if not (1 <= idx <= len(tasks_raw)):
        await update.message.reply_text("Неверный номер. Попробуй ещё раз.")
        return SET_PRIORITY_CHOOSE_TASK

    context.user_data["chosen_task_id"] = tasks_raw[idx - 1]["task_id"]
    keyboard = [["low", "medium", "high"]]
    await update.message.reply_text(
        "Выбери новый приоритет:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return SET_PRIORITY_CHOOSE_VALUE


async def setpriority_choose_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    priority = update.message.text.strip().lower()
    if priority not in {"low", "medium", "high"}:
        await update.message.reply_text("Доступно: low, medium, high.")
        return SET_PRIORITY_CHOOSE_VALUE

    chosen_task_id = context.user_data.get("chosen_task_id")
    if not chosen_task_id:
        await update.message.reply_text("Что-то пошло не так. Попробуй снова.")
        return ConversationHandler.END

    tasks = load_tasks()
    updated = False
    for t in tasks:
        if t.task_id == chosen_task_id:
            t.priority = priority
            updated = True
            break
    save_tasks(tasks)

    await update.message.reply_text(
        "Приоритет обновлён! ✨", reply_markup=ReplyKeyboardRemove()
    )

    context.user_data.pop("_tasks_cache", None)
    context.user_data.pop("chosen_task_id", None)
    return ConversationHandler.END


async def setpriority_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Изменение приоритета отменено.", reply_markup=ReplyKeyboardRemove())
    context.user_data.pop("_tasks_cache", None)
    context.user_data.pop("chosen_task_id", None)
    return ConversationHandler.END


############################################
# Диалог: установить дедлайн существующей задачи
############################################


async def setdeadline_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    tasks = get_user_tasks(load_tasks(), chat_id)
    await update.message.reply_text(render_tasks_for_choice(tasks))
    context.user_data["_tasks_cache"] = [t.to_dict() for t in tasks]
    return SET_DEADLINE_CHOOSE_TASK


async def setdeadline_choose_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    tasks_raw = context.user_data.get("_tasks_cache", [])
    if not tasks_raw:
        await update.message.reply_text("Задач нет. Создай через /newtask")
        return ConversationHandler.END
    try:
        idx = int(text)
    except ValueError:
        await update.message.reply_text("Пожалуйста, пришли номер задачи (целое число).")
        return SET_DEADLINE_CHOOSE_TASK

    if not (1 <= idx <= len(tasks_raw)):
        await update.message.reply_text("Неверный номер. Попробуй ещё раз.")
        return SET_DEADLINE_CHOOSE_TASK

    context.user_data["chosen_task_id"] = tasks_raw[idx - 1]["task_id"]
    await update.message.reply_text(
        "Введи дедлайн в формате ГГГГ-ММ-ДД (например, 2025-12-31) или '-' чтобы убрать дедлайн."
    )
    return SET_DEADLINE_ENTER_DATE


async def setdeadline_enter_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    deadline_input = update.message.text.strip()
    chosen_task_id = context.user_data.get("chosen_task_id")
    tasks = load_tasks()
    tz = context.application.bot_data["tz"]

    if deadline_input == "-":
        for t in tasks:
            if t.task_id == chosen_task_id:
                t.deadline = None
                break
        save_tasks(tasks)
        # Перепланируем все напоминания для надёжности
        reschedule_all(context.application, tz)
        await update.message.reply_text("Дедлайн удалён ✨")
        context.user_data.pop("_tasks_cache", None)
        context.user_data.pop("chosen_task_id", None)
        return ConversationHandler.END

    dt = parse_date(deadline_input)
    if not dt:
        await update.message.reply_text("Не удалось распознать дату. Формат: ГГГГ-ММ-ДД или '-'.")
        return SET_DEADLINE_ENTER_DATE

    deadline_iso = dt.strftime("%Y-%m-%d")
    for t in tasks:
        if t.task_id == chosen_task_id:
            t.deadline = deadline_iso
            break
    save_tasks(tasks)

    # Перепланируем все напоминания для надёжности
    reschedule_all(context.application, tz)

    await update.message.reply_text("Дедлайн обновлён! ✨")
    context.user_data.pop("_tasks_cache", None)
    context.user_data.pop("chosen_task_id", None)
    return ConversationHandler.END


async def setdeadline_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Изменение дедлайна отменено.")
    context.user_data.pop("_tasks_cache", None)
    context.user_data.pop("chosen_task_id", None)
    return ConversationHandler.END


############################################
# Просмотр задач
############################################


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    tasks = get_user_tasks(load_tasks(), chat_id)
    if not tasks:
        await update.message.reply_text("Задач пока нет. Добавь через /newtask ✨")
        return
    lines: List[str] = ["Твои задачи:"]
    for idx, t in enumerate(tasks, start=1):
        lines.append(
            f"{idx}. {t.text}\n   приоритет: {t.priority} | дедлайн: {t.deadline or '—'}"
        )
    await update.message.reply_text("\n".join(lines))


############################################
# Удаление задачи
############################################


async def deletetask_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    tasks = get_user_tasks(load_tasks(), chat_id)
    if not tasks:
        await update.message.reply_text("Задач нет. Добавь через /newtask ✨")
        return ConversationHandler.END
    context.user_data["_tasks_cache"] = [t.to_dict() for t in tasks]
    await update.message.reply_text(render_tasks_for_choice(tasks))
    return DELETE_CHOOSE_TASK


async def deletetask_choose_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    tasks_raw = context.user_data.get("_tasks_cache", [])
    try:
        idx = int(text)
    except ValueError:
        await update.message.reply_text("Пришли номер задачи (целое число).")
        return DELETE_CHOOSE_TASK
    if not (1 <= idx <= len(tasks_raw)):
        await update.message.reply_text("Неверный номер. Попробуй снова.")
        return DELETE_CHOOSE_TASK

    chosen = tasks_raw[idx - 1]
    context.user_data["delete_chosen_task_id"] = chosen["task_id"]
    await update.message.reply_text(
        f"Удалить задачу: {chosen['text']}? (да/нет)"
    )
    return DELETE_CONFIRM


async def deletetask_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = update.message.text.strip().lower()
    if answer not in {"да", "нет", "y", "n", "yes", "no"}:
        await update.message.reply_text("Ответь: да или нет")
        return DELETE_CONFIRM

    if answer in {"нет", "n", "no"}:
        await update.message.reply_text("Удаление отменено.")
        context.user_data.pop("_tasks_cache", None)
        context.user_data.pop("chosen_task_id", None)
        return ConversationHandler.END

    chosen_task_id = context.user_data.get("delete_chosen_task_id")
    if not chosen_task_id:
        await update.message.reply_text("Не удалось найти задачу. Попробуй снова.")
        return ConversationHandler.END

    tasks = load_tasks()
    tasks = [t for t in tasks if t.task_id != chosen_task_id]
    save_tasks(tasks)

    # Перепланируем напоминания
    tz = context.application.bot_data["tz"]
    reschedule_all(context.application, tz)

    await update.message.reply_text("Задача удалена ✨")
    context.user_data.pop("delete_tasks_cache", None)
    context.user_data.pop("delete_chosen_task_id", None)
    return ConversationHandler.END


async def deletetask_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Удаление отменено.")
    context.user_data.pop("delete_tasks_cache", None)
    context.user_data.pop("delete_chosen_task_id", None)
    return ConversationHandler.END


############################################
# Точка входа
############################################


def get_timezone() -> pytz.BaseTzInfo:
    # Значение по умолчанию — Europe/Moscow для удобства пользователей в РФ
    tz_name = os.getenv("TIMEZONE", "Europe/Moscow")
    try:
        return pytz.timezone(tz_name)
    except Exception:
        logger.warning("Не удалось применить TIMEZONE=%s, используется Europe/Moscow", tz_name)
        return pytz.timezone("Europe/Moscow")


def build_application() -> Application:
    # Принимаем несколько распространённых имён переменной для удобства
    token = (
        os.getenv("BOT_TOKEN")
        or os.getenv("TOKEN")
        or os.getenv("TELEGRAM_BOT_TOKEN")
    )
    if not token:
        raise RuntimeError("Не указан BOT_TOKEN в переменных окружения (.env)")

    application = ApplicationBuilder().token(token).build()

    # Сохраняем TZ в bot_data
    application.bot_data["tz"] = get_timezone()

    # Команда /start
    application.add_handler(CommandHandler("start", start))
    
    # Команда /getrandomjoke
    application.add_handler(CommandHandler("getrandomjoke", get_random_joke_command))

    # Диалог /newtask
    newtask_conv = ConversationHandler(
        entry_points=[CommandHandler("newtask", newtask_start)],
        states={
            NEW_TASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, newtask_receive_text)],
            NEW_TASK_PRIORITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, newtask_receive_priority)],
            NEW_TASK_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, newtask_receive_deadline)],
        },
        fallbacks=[CommandHandler("cancel", newtask_cancel)],
        name="newtask_conv",
        persistent=False,
    )
    application.add_handler(newtask_conv)

    # Диалог /deletetask (ставим раньше setpriority, чтобы при одновременных состояниях приоритет был у удаления)
    deletetask_conv = ConversationHandler(
        entry_points=[CommandHandler("deletetask", deletetask_start)],
        states={
            DELETE_CHOOSE_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, deletetask_choose_task)],
            DELETE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, deletetask_confirm)],
        },
        fallbacks=[CommandHandler("cancel", deletetask_cancel)],
        name="deletetask_conv",
        persistent=False,
    )
    application.add_handler(deletetask_conv)

    # Диалог /setpriority
    setpriority_conv = ConversationHandler(
        entry_points=[CommandHandler("setpriority", setpriority_start)],
        states={
            SET_PRIORITY_CHOOSE_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, setpriority_choose_task)],
            SET_PRIORITY_CHOOSE_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, setpriority_choose_value)],
        },
        fallbacks=[CommandHandler("cancel", setpriority_cancel)],
        name="setpriority_conv",
        persistent=False,
    )
    application.add_handler(setpriority_conv)

    # Диалог /setdeadline
    setdeadline_conv = ConversationHandler(
        entry_points=[CommandHandler("setdeadline", setdeadline_start)],
        states={
            SET_DEADLINE_CHOOSE_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, setdeadline_choose_task)],
            SET_DEADLINE_ENTER_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, setdeadline_enter_date)],
        },
        fallbacks=[CommandHandler("cancel", setdeadline_cancel)],
        name="setdeadline_conv",
        persistent=False,
    )
    application.add_handler(setdeadline_conv)

    # /list
    application.add_handler(CommandHandler("list", list_tasks))

    return application


async def on_startup(application: Application) -> None:
    # Загружаем задачи и планируем напоминания при запуске
    tz = application.bot_data["tz"]
    reschedule_all(application, tz)
    logger.info("Напоминания перепланированы при запуске")


def main() -> None:
    # Загрузка .env из директории скрипта явно, чтобы избежать ошибок текущей директории
    env_path = Path(__file__).with_name(".env")
    load_dotenv(dotenv_path=env_path)

    # Явно создаём и устанавливаем событийный цикл до инициализации Application/JobQueue
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = build_application()
    application.post_init = on_startup

    # Запускаем бота (asyncio)
    application.run_polling()


if __name__ == "__main__":
    main()
