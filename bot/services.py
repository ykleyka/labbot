"""Общая логика для хендлеров: отрисовка очереди, поиск пар, уведомления."""
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from .db import Database
from .queue_service import QueueService
from .utils import fmt_dt, fmt_time, kb, lesson_bounds, now_utc, scope_title

log = logging.getLogger(__name__)

NOT_AUTH = "Сначала войдите в бота: /start"


async def guard(call: CallbackQuery, member: dict | None) -> bool:
    if member is None:
        await call.answer(NOT_AUTH, show_alert=True)
        return False
    return True


async def guard_headman(call: CallbackQuery, member: dict | None) -> bool:
    if member is None or not member["is_headman"]:
        await call.answer("Это доступно только старосте", show_alert=True)
        return False
    return True


async def own_subject(db: Database, member: dict, subject_id: int) -> dict | None:
    subject = await db.get_subject(subject_id)
    return subject if subject and subject["group_id"] == member["group_id"] else None


def member_scope(subject: dict, member: dict) -> int:
    """Номер очереди участника по предмету: 0 — общая, иначе подгруппа."""
    return member["subgroup"] if subject["is_subgroup"] else 0


async def active_lesson(db: Database, subject_id: int, subgroup: int) -> dict | None:
    now = now_utc()
    for lesson in await db.scope_lessons(subject_id, subgroup):
        start, end = lesson_bounds(lesson)
        if start <= now < end:
            return lesson
    return None


async def next_lesson(db: Database, subject_id: int, subgroup: int) -> dict | None:
    now = now_utc()
    for lesson in await db.scope_lessons(subject_id, subgroup):
        if lesson_bounds(lesson)[0] > now:
            return lesson
    return None


async def render_queue(
    db: Database, qs: QueueService, subject: dict, subgroup: int, viewer_id: int | None = None
) -> str:
    entries = await qs.entries(subject["id"], subgroup)
    lines = [f"<b>{escape(scope_title(subject, subgroup))}</b>"]

    active = await active_lesson(db, subject["id"], subgroup)
    if active:
        lines.append(f"Идёт пара (до {fmt_time(lesson_bounds(active)[1])})")
    else:
        nxt = await next_lesson(db, subject["id"], subgroup)
        lines.append(f"Следующая пара: {fmt_dt(lesson_bounds(nxt)[0])}" if nxt else "Пары не запланированы")
    lines.append("")

    if not entries:
        lines.append("Очередь пуста.")
    for i, e in enumerate(entries, 1):
        mark = " — уваж. причина" if e["excused"] else ""
        me = " (вы)" if viewer_id == e["member_id"] else ""
        lines.append(f"{i}. {escape(e['full_name'])}{mark}{me}")
    return "\n".join(lines)


async def notify(bot: Bot, tg_id: int | None, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    if not tg_id:
        return False
    try:
        await bot.send_message(tg_id, text, reply_markup=markup)
        return True
    except TelegramAPIError as e:
        log.warning("Не удалось отправить сообщение %s: %s", tg_id, e)
        return False


async def notify_headmen(bot: Bot, db: Database, group_id: int, text: str, exclude_id: int | None = None) -> None:
    for h in await db.headmen(group_id):
        if h["id"] != exclude_id:
            await notify(bot, h["tg_id"], text)


async def notify_if_first_changed(
    bot: Bot, db: Database, qs: QueueService, subject: dict, subgroup: int, old_first_id: int | None
) -> None:
    """Во время пары сообщает новому первому в очереди, что пора сдавать."""
    if not await active_lesson(db, subject["id"], subgroup):
        return
    first = await qs.first_active(subject["id"], subgroup)
    if first and first["member_id"] != old_first_id:
        await notify(
            bot, first["tg_id"],
            f"Подошла ваша очередь сдавать лабораторную по <b>{escape(subject['name'])}</b>!",
        )


async def first_active_id(qs: QueueService, subject_id: int, subgroup: int) -> int | None:
    first = await qs.first_active(subject_id, subgroup)
    return first["member_id"] if first else None


# ---------- опрос ----------

def poll_text(lesson: dict, subject: dict) -> str:
    start, _ = lesson_bounds(lesson)
    return (
        f"<b>Опрос</b>\n"
        f"Пара по <b>{escape(scope_title(subject, lesson['subgroup']))}</b>\n"
        f"{fmt_dt(start)}\n\n"
        f"Будете сдавать лабораторную на этой паре?\n"
        f"«Да» — вы встанете в конец очереди (если ещё не в ней), «Нет» — уберём вас из очереди."
    )


def poll_markup(lesson_id: int) -> InlineKeyboardMarkup:
    return kb([("Буду сдавать", f"pa:{lesson_id}:1"), ("Не буду", f"pa:{lesson_id}:0")])


async def send_poll(bot: Bot, db: Database, lesson: dict, subject: dict) -> tuple[int, int]:
    """Рассылает опрос всем участникам очереди пары. Возвращает (доставлено, всего)."""
    members = await db.scope_members(subject["group_id"], lesson["subgroup"])
    text, markup = poll_text(lesson, subject), poll_markup(lesson["id"])
    sent = 0
    for m in members:
        if await notify(bot, m["tg_id"], text, markup):
            sent += 1
    return sent, len(members)
