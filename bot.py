import asyncio
import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# -----------------------------
# Environment variables
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MANAGER_CHAT_ID_RAW = os.getenv("MANAGER_CHAT_ID", "0")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
REPORT_TIME = os.getenv("DAILY_REPORT_TIME", os.getenv("REPORT_TIME", "20:00"))
FEEDBACK_DB_PATH = os.getenv("FEEDBACK_DB_PATH", "feedback.db")

try:
    MANAGER_CHAT_ID = int(MANAGER_CHAT_ID_RAW)
except ValueError:
    MANAGER_CHAT_ID = 0

# -----------------------------
# Conversation states
# -----------------------------
LOCATION, COMMENT_TYPE, COMMENT_TEXT, PHOTO_CHOICE, PHOTO_UPLOAD, NEXT_ACTION = range(6)

# -----------------------------
# Bot options
# -----------------------------
LOCATIONS = ["Кафетерий 1440"]

# Single-location bot for Cafeteria 1440.
DEFAULT_LOCATION = "Кафетерий 1440"
START_LOCATION_MAP = {}

COMMENT_TYPES = [
    "👍 Сегодня понравилось",
    "✨ Хотелось бы добавить",
    "🔧 Стоит поправить",
    "💬 Просто комментарий",
]

PHOTO_ENABLED_TYPES = {
    "👍 Сегодня понравилось",
    "🔧 Стоит поправить",
}

PHOTO_ACTIONS = [
    "📷 Добавить фото",
    "➡️ Без фото",
]

PHOTO_UPLOAD_ACTIONS = [
    "➡️ Пропустить фото",
]

NEXT_ACTIONS = [
    "➕ Добавить ещё комментарий",
    "✅ Закончить",
]

FINAL_ACTIONS = [
    "➕ Новый отзыв",
    "❌ Закрыть",
]


def get_tz() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def make_keyboard(options: list[str]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[option] for option in options],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Выберите вариант",
    )


def escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def now_dt() -> datetime:
    return datetime.now(get_tz())


def now_text() -> str:
    return now_dt().strftime("%d.%m.%Y %H:%M")


def today_key() -> str:
    return now_dt().strftime("%Y-%m-%d")


def human_date(date_key: str) -> str:
    try:
        return datetime.strptime(date_key, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return date_key


# -----------------------------
# Database
# -----------------------------

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(FEEDBACK_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                report_date TEXT NOT NULL,
                location TEXT NOT NULL,
                comment_type TEXT NOT NULL,
                comment TEXT NOT NULL,
                photo_file_id TEXT,
                has_photo INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Safe migration for databases created by older versions.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(feedback)").fetchall()}
        if "photo_file_id" not in columns:
            conn.execute("ALTER TABLE feedback ADD COLUMN photo_file_id TEXT")
        if "has_photo" not in columns:
            conn.execute("ALTER TABLE feedback ADD COLUMN has_photo INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_reports (
                report_date TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL,
                feedback_count INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def save_feedback(location: str, comment_type: str, comment: str) -> int:
    dt = now_dt()
    with db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO feedback (created_at, report_date, location, comment_type, comment)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                dt.isoformat(timespec="seconds"),
                dt.strftime("%Y-%m-%d"),
                location,
                comment_type,
                comment,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def attach_photo_to_feedback(feedback_id: int, photo_file_id: str) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE feedback
            SET photo_file_id = ?, has_photo = 1
            WHERE id = ?
            """,
            (photo_file_id, feedback_id),
        )
        conn.commit()


def get_feedback_for_date(date_key: str) -> list[dict]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, report_date, location, comment_type, comment, photo_file_id, has_photo
            FROM feedback
            WHERE report_date = ?
            ORDER BY id ASC
            """,
            (date_key,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_counts(rows: list[dict]) -> dict:
    by_location: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for row in rows:
        by_location[row["location"]] = by_location.get(row["location"], 0) + 1
        by_type[row["comment_type"]] = by_type.get(row["comment_type"], 0) + 1
    return {
        "total": len(rows),
        "by_location": by_location,
        "by_type": by_type,
        "with_photo": sum(1 for row in rows if row.get("has_photo")),
    }


def report_was_sent(date_key: str) -> bool:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT report_date FROM daily_reports WHERE report_date = ?",
            (date_key,),
        ).fetchone()
    return row is not None


def mark_report_sent(date_key: str, feedback_count: int) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_reports (report_date, sent_at, feedback_count)
            VALUES (?, ?, ?)
            """,
            (date_key, now_dt().isoformat(timespec="seconds"), feedback_count),
        )
        conn.commit()


# -----------------------------
# AI report
# -----------------------------

def rows_to_text(rows: list[dict]) -> str:
    lines = []
    for i, row in enumerate(rows, start=1):
        lines.append(
            f"{i}. Площадка: {row['location']} | "
            f"Тип: {row['comment_type']} | "
            f"Фото: {'да' if row.get('has_photo') else 'нет'} | "
            f"Комментарий: {row['comment']}"
        )
    return "\n".join(lines)


def build_ai_prompt(rows: list[dict], date_key: str) -> str:
    counts = get_counts(rows)
    return f"""
Ты готовишь ежедневную управленческую сводку по отзывам сотрудников о кафетерии 1440.

Дата: {human_date(date_key)}
Всего отзывов: {counts['total']}
Отзывов с фото: {counts.get('with_photo', 0)}

Задача:
- не пересказывай каждый отзыв отдельно;
- объединяй похожие комментарии;
- обязательно отделяй похвалу от замечаний;
- учитывай площадки;
- если несколько отзывов говорят об одном и том же, прямо отметь повторяемость;
- если данных мало, аккуратно напиши, что выводы предварительные;
- не придумывай факты, блюда или проблемы, которых нет в отзывах;
- если к отзывам были приложены фото, отметь это только как факт; содержимое фото не анализируй, если оно не описано в тексте;
- пиши кратко, по делу, в стиле краткого управленческого отчета для ответственных за кафетерий, ассортимент и продажи.

Структура отчета строго такая:

📊 Итоги обратной связи за {human_date(date_key)}

Получено отзывов: N

👍 Что сегодня понравилось
• ...

✨ Что сотрудники хотели бы добавить
• ...

⚠️ Что стоит поправить
• ...

🔥 Потенциальные точки роста продаж
• ...

🚨 Повторяющиеся зоны внимания
• ...

📌 Что проверить завтра
1. ...
2. ...
3. ...

Общее настроение дня: 🟢 позитивное / 🟡 смешанное / 🔴 требует внимания

Отзывы за день:
{rows_to_text(rows)}
""".strip()


def parse_openai_response(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"].strip()

    parts: list[str] = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts).strip()


def call_openai(prompt: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

    body = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "temperature": 0.2,
    }
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=data,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))

    text = parse_openai_response(payload)
    if not text:
        raise RuntimeError("OpenAI returned empty response")
    return text


def build_fallback_report(rows: list[dict], date_key: str, reason: str | None = None) -> str:
    counts = get_counts(rows)

    def format_counts(title: str, data: dict[str, int]) -> str:
        if not data:
            return f"{title}\n• нет данных"
        ordered = sorted(data.items(), key=lambda x: (-x[1], x[0]))
        lines = [title]
        lines.extend([f"• {name}: {count}" for name, count in ordered])
        return "\n".join(lines)

    examples = rows[:10]
    example_lines = [
        f"• {r['location']} / {r['comment_type']}"
        f"{' / 📷 фото' if r.get('has_photo') else ''}: {r['comment']}"
        for r in examples
    ]

    report = (
        f"📊 Итоги обратной связи за {human_date(date_key)}\n\n"
        f"Получено отзывов: {counts['total']}\n"
        f"Отзывов с фото: {counts.get('with_photo', 0)}\n\n"
        f"{format_counts('📍 По площадкам:', counts['by_location'])}\n\n"
        f"{format_counts('💬 По типам отзывов:', counts['by_type'])}\n\n"
        "📝 Последние отзывы:\n"
        f"{chr(10).join(example_lines) if example_lines else '• нет отзывов'}"
    )
    if reason:
        report += (
            "\n\n⚠️ AI-сводку не удалось подготовить. "
            "Показана базовая статистика."
        )
    return report


def build_daily_report(rows: list[dict], date_key: str) -> str:
    if not rows:
        return f"📊 Итоги обратной связи за {human_date(date_key)}\n\nЗа сегодня отзывов не было."

    try:
        prompt = build_ai_prompt(rows, date_key)
        return call_openai(prompt)
    except Exception as exc:
        logging.exception("Failed to build AI report: %s", exc)
        return build_fallback_report(rows, date_key, reason=str(exc))


async def send_daily_report_to_chat(
    bot: Bot,
    chat_id: int,
    date_key: str | None = None,
    mark_sent: bool = False,
    quiet_if_empty: bool = False,
) -> None:
    date_key = date_key or today_key()
    rows = get_feedback_for_date(date_key)

    if quiet_if_empty and not rows:
        logging.info("No feedback for %s; daily report skipped", date_key)
        return

    report = build_daily_report(rows, date_key)
    await bot.send_message(
        chat_id=chat_id,
        text=report,
        parse_mode=None,
        disable_web_page_preview=True,
    )

    if mark_sent:
        mark_report_sent(date_key, len(rows))
        logging.info("Daily report for %s sent to chat %s", date_key, chat_id)


def seconds_until_next_report() -> float:
    tz = get_tz()
    now = datetime.now(tz)
    try:
        hour, minute = [int(part) for part in REPORT_TIME.split(":", 1)]
    except Exception:
        hour, minute = 20, 0

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 1)


async def daily_report_loop(application: Application) -> None:
    await asyncio.sleep(5)
    while True:
        sleep_seconds = seconds_until_next_report()
        logging.info("Next daily report in %.0f seconds", sleep_seconds)
        await asyncio.sleep(sleep_seconds)

        date_key = today_key()
        if report_was_sent(date_key):
            logging.info("Daily report for %s already sent; skipping", date_key)
            continue

        if MANAGER_CHAT_ID == 0:
            logging.warning("MANAGER_CHAT_ID is 0; daily report skipped")
            continue

        try:
            await send_daily_report_to_chat(
                application.bot,
                MANAGER_CHAT_ID,
                date_key=date_key,
                mark_sent=True,
                quiet_if_empty=True,
            )
        except Exception as exc:
            logging.exception("Failed to send scheduled daily report: %s", exc)


# -----------------------------
# Conversation
# -----------------------------
async def ask_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["location"] = DEFAULT_LOCATION
    await update.message.reply_text(
        "👋 Добро пожаловать!\n\n"
        "Помогите нам сделать кафетерий удобнее и вкуснее.\n\n"
        "Это займет меньше минуты.\n\n"
        "Персональные данные указывать не требуется.",
    )
    return await ask_comment_type(update, context)


async def ask_comment_type(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str | None = None) -> int:
    text = "💬 Что хотите отметить?"
    if prefix:
        text = f"{prefix}\n\n{text}"
    await update.message.reply_text(
        text,
        reply_markup=make_keyboard(COMMENT_TYPES),
    )
    return COMMENT_TYPE


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["location"] = DEFAULT_LOCATION

    await update.message.reply_text(
        "👋 Добро пожаловать!\n\n"
        "Помогите нам сделать кафетерий удобнее и вкуснее.\n\n"
        "Это займет меньше минуты.\n\n"
        "Персональные данные указывать не требуется."
    )
    return await ask_comment_type(update, context)


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Technical command. It is intentionally not shown in the public command menu."""
    chat_id_value = update.effective_chat.id
    await update.message.reply_text(
        f"ID этого чата:\n{chat_id_value}\n\n"
        "Скопируйте это число в переменную MANAGER_CHAT_ID."
    )


async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Готовлю сводку за сегодня…")
    try:
        await send_daily_report_to_chat(
            context.bot,
            update.effective_chat.id,
            date_key=today_key(),
            mark_sent=False,
            quiet_if_empty=False,
        )
    except Exception as exc:
        logging.exception("Failed to send manual daily report: %s", exc)
        await update.message.reply_text(
            "Не удалось подготовить сводку. Проверьте логи Railway и OPENAI_API_KEY."
        )


async def choose_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    location = update.message.text

    if location not in LOCATIONS:
        await update.message.reply_text(
            "Пожалуйста, выберите площадку кнопкой ниже:",
            reply_markup=make_keyboard(LOCATIONS),
        )
        return LOCATION

    context.user_data["location"] = location
    return await ask_comment_type(update, context)


async def choose_comment_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    comment_type = update.message.text

    if comment_type not in COMMENT_TYPES:
        await update.message.reply_text(
            "Пожалуйста, выберите вариант кнопкой ниже:",
            reply_markup=make_keyboard(COMMENT_TYPES),
        )
        return COMMENT_TYPE

    context.user_data["comment_type"] = comment_type

    if comment_type == "👍 Сегодня понравилось":
        prompt = (
            "👍 Что сегодня было особенно удачным?\n\n"
            "Например:\n"
            "— вкусный флэт уайт;\n"
            "— понравился бамбл;\n"
            "— хороший круассан;\n"
            "— вкусный чизкейк;\n"
            "— свежий салат;\n"
            "— спасибо бариста.\n\n"
            "Напишите комментарий своими словами.\n\n"
            "Персональные данные указывать не требуется."
        )
    elif comment_type == "✨ Хотелось бы добавить":
        prompt = (
            "✨ Чего вам не хватает в кафетерии?\n\n"
            "Напишите конкретно — так нам проще понять, что стоит попробовать.\n\n"
            "Например:\n"
            "— матча латте;\n"
            "— Coca-Cola Zero;\n"
            "— баскский чизкейк;\n"
            "— моти;\n"
            "— круассан с индейкой;\n"
            "— боул с курицей;\n"
            "— протеиновый пудинг;\n"
            "— фрукты;\n"
            "— мороженое.\n\n"
            "Персональные данные указывать не требуется."
        )
    elif comment_type == "🔧 Стоит поправить":
        prompt = (
            "🔧 Что сегодня можно было сделать лучше?\n\n"
            "Например:\n"
            "— к обеду закончились салаты;\n"
            "— кофе был слишком горячий;\n"
            "— круассан был суховат;\n"
            "— в пасте мало соуса;\n"
            "— не хватило приборов;\n"
            "— долго ждал(а) заказ.\n\n"
            "Персональные данные указывать не требуется."
        )
    else:
        prompt = (
            "💬 Напишите всё, что считаете важным.\n\n"
            "Это может быть пожелание, благодарность или любое наблюдение по работе кафетерия.\n\n"
            "Персональные данные указывать не требуется."
        )

    await update.message.reply_text(
        prompt,
        reply_markup=ReplyKeyboardRemove(),
    )
    return COMMENT_TEXT


def format_feedback_message(location: str, comment_type: str, comment: str) -> str:
    return (
        "📩 <b>Новый комментарий по кафетерию</b>\n\n"
        f"📍 <b>{escape_html(location)}</b>\n"
        f"{escape_html(comment_type)}\n"
        f"💬 {escape_html(comment)}\n"
        f"🕒 {escape_html(now_text())}"
    )


def format_photo_caption(location: str, comment_type: str, comment: str) -> str:
    return (
        "📷 <b>Фото к комментарию по кафетерию</b>\n\n"
        f"📍 <b>{escape_html(location)}</b>\n"
        f"{escape_html(comment_type)}\n"
        f"💬 {escape_html(comment)}\n"
        f"🕒 {escape_html(now_text())}"
    )


async def ask_next_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Хотите оставить ещё один комментарий?",
        reply_markup=make_keyboard(NEXT_ACTIONS),
    )
    return NEXT_ACTION


async def photo_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = (update.message.text or "").strip()

    if action == "➡️ Без фото":
        return await ask_next_action(update, context)

    if action == "📷 Добавить фото":
        await update.message.reply_text(
            "📷 Пришлите фото одним сообщением.\n\n"
            "Фото будет отправлено в чат руководителей вместе с вашим комментарием.\n"
            "Если передумали — нажмите «Пропустить фото».",
            reply_markup=make_keyboard(PHOTO_UPLOAD_ACTIONS),
        )
        return PHOTO_UPLOAD

    await update.message.reply_text(
        "Пожалуйста, выберите вариант кнопкой ниже:",
        reply_markup=make_keyboard(PHOTO_ACTIONS),
    )
    return PHOTO_CHOICE


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == "➡️ Пропустить фото":
        return await ask_next_action(update, context)

    if not update.message.photo:
        await update.message.reply_text(
            "Пришлите, пожалуйста, именно фото или нажмите «Пропустить фото».",
            reply_markup=make_keyboard(PHOTO_UPLOAD_ACTIONS),
        )
        return PHOTO_UPLOAD

    photo = update.message.photo[-1]
    photo_file_id = photo.file_id

    feedback_id = context.user_data.get("last_feedback_id")
    location = context.user_data.get("last_location", context.user_data.get("location", "Не указана"))
    comment_type = context.user_data.get("last_comment_type", context.user_data.get("comment_type", "Не указано"))
    comment = context.user_data.get("last_comment", "")

    if feedback_id:
        attach_photo_to_feedback(int(feedback_id), photo_file_id)

    if MANAGER_CHAT_ID == 0:
        logging.warning("MANAGER_CHAT_ID is 0; photo was not forwarded")
        await update.message.reply_text(
            "Фото получено, но группа руководителей пока не настроена.",
        )
    else:
        try:
            await context.bot.send_photo(
                chat_id=MANAGER_CHAT_ID,
                photo=photo_file_id,
                caption=format_photo_caption(location, comment_type, comment),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logging.exception("Failed to send feedback photo to manager chat: %s", exc)
            await update.message.reply_text(
                "Фото получено, но не удалось отправить его в группу руководителей.\n"
                "Проверьте MANAGER_CHAT_ID и права бота в группе."
            )

    await update.message.reply_text("✅ Фото добавлено к отзыву.")
    return await ask_next_action(update, context)

async def receive_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    comment = (update.message.text or "").strip()

    if not comment:
        await update.message.reply_text("Напишите, пожалуйста, комментарий текстом.")
        return COMMENT_TEXT

    location = context.user_data.get("location", "Не указана")
    comment_type = context.user_data.get("comment_type", "Не указано")

    # Save first: even if Telegram forwarding fails, the feedback will still be available for reports.
    feedback_id = save_feedback(location, comment_type, comment)
    context.user_data["last_feedback_id"] = feedback_id
    context.user_data["last_comment"] = comment
    context.user_data["last_location"] = location
    context.user_data["last_comment_type"] = comment_type

    manager_message = format_feedback_message(location, comment_type, comment)

    if MANAGER_CHAT_ID == 0:
        logging.warning("MANAGER_CHAT_ID is 0; feedback was not forwarded")
        context.user_data["last_forwarded"] = False
        await update.message.reply_text(
            "Комментарий получен, но группа руководителей пока не настроена.\n\n"
            "Нужно указать MANAGER_CHAT_ID в Railway.",
        )
    else:
        try:
            await context.bot.send_message(
                chat_id=MANAGER_CHAT_ID,
                text=manager_message,
                parse_mode=ParseMode.HTML,
            )
            context.user_data["last_forwarded"] = True
        except Exception as exc:
            logging.exception("Failed to send feedback to manager chat: %s", exc)
            context.user_data["last_forwarded"] = False
            await update.message.reply_text(
                "Комментарий получен, но не удалось отправить его в группу руководителей.\n"
                "Проверьте MANAGER_CHAT_ID и права бота в группе."
            )

    if comment_type in PHOTO_ENABLED_TYPES:
        await update.message.reply_text(
            "✅ Спасибо! Комментарий отправлен.\n\nХотите добавить фото к этому комментарию?",
            reply_markup=make_keyboard(PHOTO_ACTIONS),
        )
        return PHOTO_CHOICE

    await update.message.reply_text("✅ Спасибо! Комментарий отправлен.")
    return await ask_next_action(update, context)


async def next_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = update.message.text

    if action == "➕ Добавить ещё комментарий":
        # Keep location, reset only the comment category.
        context.user_data.pop("comment_type", None)
        await update.message.reply_text(
            "💬 Что ещё хотите отметить?",
            reply_markup=make_keyboard(COMMENT_TYPES),
        )
        return COMMENT_TYPE

    if action == "✅ Закончить":
        context.user_data.clear()
        await update.message.reply_text(
            "🙌 Спасибо за обратную связь!\n\n"
            "Ваши комментарии помогают нам лучше понимать, что нравится сотрудникам и чего не хватает в кафетерии.\n\n"
            "Будем рады вашим новым комментариям!",
            reply_markup=make_keyboard(FINAL_ACTIONS),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Пожалуйста, выберите действие кнопкой ниже:",
        reply_markup=make_keyboard(NEXT_ACTIONS),
    )
    return NEXT_ACTION


async def close_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text(
        "Готово. Если захотите оставить новый комментарий позже — просто снова откройте этого бота.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def start_new_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start a new feedback flow from the persistent button.

    This must be a ConversationHandler entry point. If we only show the
    location keyboard from unknown_private, the next button click will not
    belong to an active conversation and will be treated as an unrelated text.
    """
    context.user_data.clear()
    return await start(update, context)


async def unknown_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()

    if text == "➕ Новый отзыв":
        # Normally this is handled by the ConversationHandler entry point.
        # This fallback is only for unusual cases.
        await update.message.reply_text(
            "Чтобы оставить новый комментарий, нажмите /start.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if text == "❌ Закрыть":
        await close_dialog(update, context)
        return

    await update.message.reply_text(
        "Диалог сейчас не активен.\n\n"
        "Чтобы оставить новый комментарий, нажмите «➕ Новый отзыв».",
        reply_markup=make_keyboard(FINAL_ACTIONS),
    )


async def post_init(application: Application) -> None:
    # Public menu for users: only one command.
    # /id and /daily_report still work as hidden technical commands, but they are not shown in the menu.
    await application.bot.set_my_commands(
        [BotCommand("start", "оставить комментарий")]
    )
    application.create_task(daily_report_loop(application))


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )

    init_db()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^➕ Новый отзыв$"), start_new_feedback),
        ],
        states={
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_location)],
            COMMENT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_comment_type)],
            COMMENT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_comment)],
            PHOTO_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, photo_choice)],
            PHOTO_UPLOAD: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_photo),
            ],
            NEXT_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, next_action)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("id", chat_id))
    application.add_handler(CommandHandler("daily_report", daily_report_command))
    application.add_handler(conversation)
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, unknown_private))

    logging.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
