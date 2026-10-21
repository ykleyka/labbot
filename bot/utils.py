import re
import secrets
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import TZ

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
# Без похожих символов (0/O, 1/I), чтобы код было легко переписать
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

DT_RE = re.compile(
    r"^\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?\s+(\d{1,2})[:.](\d{2})(?:\s+(\d{1,3}))?\s*$"
)
MEMBER_RE = re.compile(r"^(.+?)(?:\s*[;,|]\s*|\s+)(\d{1,2})\s*$")
NUMBERING_RE = re.compile(r"^\d+\s*[.)]\s*")


def kb(*rows) -> InlineKeyboardMarkup:
    """kb([("Текст", "data"), ...], [...]) — строки инлайн-клавиатуры."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
            if row
        ]
    )


async def edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение с кнопками, а если не вышло — отправляет новое."""
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await call.message.answer(text, reply_markup=markup)


async def send_long(message: Message, text: str, **kwargs) -> None:
    """Отправляет длинный текст частями (лимит Telegram — 4096 символов)."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 4000:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    for i, chunk in enumerate(chunks):
        await message.answer(chunk, **(kwargs if i == len(chunks) - 1 else {}))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def fmt_dt(dt: datetime) -> str:
    local = dt.astimezone(TZ)
    return f"{local:%d.%m.%Y} ({WEEKDAYS[local.weekday()]}) {local:%H:%M}"


def fmt_time(dt: datetime) -> str:
    return f"{dt.astimezone(TZ):%H:%M}"


def lesson_bounds(lesson: dict) -> tuple[datetime, datetime]:
    start = from_iso(lesson["starts_at"])
    return start, start + timedelta(minutes=lesson["duration_min"])


def parse_lesson_input(text: str) -> tuple[datetime, int | None] | None:
    """'24.10 10:40', '24.10.2026 10:40' или '24.10 10:40 90' -> (начало, длительность)."""
    m = DT_RE.match(text)
    if not m:
        return None
    day, month, year, hour, minute, duration = m.groups()
    now = datetime.now(TZ)
    y = now.year if not year else (2000 + int(year) if len(year) == 2 else int(year))
    try:
        dt = datetime(y, int(month), int(day), int(hour), int(minute), tzinfo=TZ)
        # Без года: если дата уже прошла, значит имеется в виду следующий год
        if not year and dt < now - timedelta(days=1):
            dt = dt.replace(year=y + 1)
    except ValueError:
        return None
    return dt, (int(duration) if duration else None)


def parse_members(text: str, subgroups_count: int) -> tuple[list[tuple[str, int]], list[str]]:
    """Строки 'ФИО; подгруппа' -> ([(ФИО, подгруппа)], [ошибочные строки])."""
    result, errors = [], []
    for raw in text.splitlines():
        line = NUMBERING_RE.sub("", raw.strip())
        if not line:
            continue
        m = MEMBER_RE.match(line)
        if m:
            name, subgroup = m.group(1).strip(), int(m.group(2))
        else:
            name, subgroup = line, (1 if subgroups_count == 1 else None)
        name = " ".join(name.split())[:100]
        if not name or subgroup is None or not 1 <= subgroup <= subgroups_count:
            errors.append(raw.strip())
            continue
        result.append((name, subgroup))
    return result, errors


def normalize_name(name: str) -> str:
    return " ".join(name.split()).casefold().replace("ё", "е")


def gen_code(length: int = 6) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def scope_title(subject: dict, subgroup: int) -> str:
    return f"{subject['name']} (подгруппа {subgroup})" if subgroup else subject["name"]
