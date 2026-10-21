"""Функции старосты: группа, предметы, пары и опросы, редактирование очереди, участники, Google Таблицы."""
import asyncio
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..config import DEFAULT_LESSON_MINUTES
from ..db import Database
from ..queue_service import QueueService
from ..services import (
    first_active_id, guard_headman, notify, notify_if_first_changed, own_subject, render_queue, send_poll,
)
from ..sheets import HEADER, SheetsClient, SheetsError, parse_sheet_rows, sheet_title
from ..utils import (
    edit, fmt_dt, kb, lesson_bounds, normalize_name, now_utc, parse_lesson_input, parse_members,
    scope_title, send_long,
)

router = Router(name="admin")

NOT_COMMAND = F.text & ~F.text.startswith("/")
BACK_ADMIN = ("Назад к управлению", "adm")


class CreateGroup(StatesGroup):
    name = State()
    subgroups = State()
    own_name = State()
    members = State()


class AddSubject(StatesGroup):
    name = State()
    kind = State()


class AddLesson(StatesGroup):
    when = State()


class MovePos(StatesGroup):
    pos = State()


class AddMembers(StatesGroup):
    text = State()


class SetSheet(StatesGroup):
    url = State()


class DeleteGroup(StatesGroup):
    confirm = State()


def members_format_hint(subgroups_count: int) -> str:
    if subgroups_count == 1:
        return "Каждый участник с новой строки, например:\n<code>Иванов Иван\nПетрова Мария</code>"
    return ("Каждый участник с новой строки в формате <code>ФИО; подгруппа</code>, например:\n"
            "<code>Иванов Иван; 1\nПетрова Мария; 2</code>")


def codes_text(members: list[dict], bot_username: str) -> str:
    lines = []
    for m in members:
        lines.append(f"• {escape(m['full_name'])} (подгр. {m['subgroup']}) — <code>{m['code']}</code>")
    lines.append(
        f"\nУчастник входит так: отправляет боту /start и код, "
        f"или открывает ссылку <code>https://t.me/{bot_username}?start=КОД</code>."
    )
    return "\n".join(lines)


async def subgroups_of(db: Database, subject: dict) -> list[int]:
    if not subject["is_subgroup"]:
        return [0]
    group = await db.get_group(subject["group_id"])
    return list(range(1, group["subgroups_count"] + 1))


# ======================= создание группы =======================

@router.message(Command("creategroup"))
async def cmd_creategroup(message: Message, state: FSMContext, member: dict | None):
    if member:
        await message.answer("Вы уже состоите в группе. Чтобы создать новую, сначала выйдите: /logout")
        return
    await state.set_state(CreateGroup.name)
    await message.answer("Создание группы.\n<b>Шаг 1/4.</b> Введите название группы (например, ИВТ-21).\n/cancel — отмена")


@router.message(CreateGroup.name, NOT_COMMAND)
async def cg_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:64])
    await state.set_state(CreateGroup.subgroups)
    await message.answer("<b>Шаг 2/4.</b> На сколько подгрупп делится группа? Отправьте число от 1 до 10.")


@router.message(CreateGroup.subgroups, NOT_COMMAND)
async def cg_subgroups(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= 10:
        await message.answer("Нужно число от 1 до 10.")
        return
    count = int(text)
    await state.update_data(subgroups=count)
    await state.set_state(CreateGroup.own_name)
    hint = "" if count == 1 else " и номер вашей подгруппы через «;», например: <code>Иванов Иван; 1</code>"
    await message.answer(f"<b>Шаг 3/4.</b> Введите ваше ФИО{hint}.")


@router.message(CreateGroup.own_name, NOT_COMMAND)
async def cg_own_name(message: Message, state: FSMContext):
    data = await state.get_data()
    parsed, errors = parse_members(message.text, data["subgroups"])
    if errors or len(parsed) != 1:
        example = "Иванов Иван" if data["subgroups"] == 1 else f"Иванов Иван; 1 (подгруппа от 1 до {data['subgroups']})"
        await message.answer(f"Не получилось разобрать. Пример: <code>{example}</code>")
        return
    await state.update_data(own=parsed[0])
    await state.set_state(CreateGroup.members)
    await message.answer(
        "<b>Шаг 4/4.</b> Отправьте список остальных участников группы.\n"
        + members_format_hint(data["subgroups"])
        + "\n\nЕсли хотите добавить участников позже, отправьте «-»."
    )


@router.message(CreateGroup.members, NOT_COMMAND)
async def cg_members(message: Message, state: FSMContext, bot: Bot, db: Database):
    data = await state.get_data()
    count = data["subgroups"]
    if message.text.strip() == "-":
        parsed, errors = [], []
    else:
        parsed, errors = parse_members(message.text, count)
    if errors:
        bad = "\n".join(f"• {escape(e)}" for e in errors[:15])
        await message.answer(f"Не удалось разобрать строки:\n{bad}\n\n{members_format_hint(count)}\n\n"
                             "Отправьте список целиком ещё раз.")
        return
    if await db.get_member_by_tg(message.from_user.id):
        await state.clear()
        await message.answer("Вы уже состоите в группе.")
        return

    group_id = await db.create_group(data["name"], count)
    own_name, own_sg = data["own"]
    headman = await db.add_member(group_id, own_name, own_sg, tg_id=message.from_user.id, is_headman=True)
    created = [await db.add_member(group_id, name, sg) for name, sg in parsed]
    await state.clear()

    me = await bot.get_me()
    text = f"Группа <b>{escape(data['name'])}</b> создана!\nВаш код (на случай повторного входа): <code>{headman['code']}</code>\n"
    if created:
        text += "\n<b>Коды участников</b> (разошлите каждому его код):\n" + codes_text(created, me.username)
    text += "\n\nДальше добавьте предметы: /menu → Управление → Предметы."
    await send_long(message, text)


# ======================= меню старосты =======================

@router.callback_query(F.data == "adm")
async def cb_admin(call: CallbackQuery, state: FSMContext, db: Database, member: dict | None):
    await state.clear()
    if not await guard_headman(call, member):
        return
    group = await db.get_group(member["group_id"])
    members = await db.list_members(group["id"])
    subjects = await db.list_subjects(group["id"])
    linked = sum(1 for m in members if m["tg_id"])
    text = (
        f"<b>Управление группой {escape(group['name'])}</b>\n"
        f"Участников: {len(members)} (вошли в бота: {linked})\n"
        f"Подгрупп: {group['subgroups_count']} · Предметов: {len(subjects)}"
    )
    await edit(call, text, kb(
        [("Предметы", "as"), ("Пары и опросы", "al")],
        [("Редактировать очередь", "aq")],
        [("Участники", "am"), ("Google Таблицы", "ag")],
        [("Удалить группу", "agdel")],
        [("Главное меню", "m:main")],
    ))
    await call.answer()


# ======================= удаление группы =======================

@router.callback_query(F.data == "agdel")
async def cb_group_delete(call: CallbackQuery, state: FSMContext, db: Database, member: dict | None):
    if not await guard_headman(call, member):
        return
    group = await db.get_group(member["group_id"])
    await state.set_state(DeleteGroup.confirm)
    await edit(
        call,
        f"<b>Удаление группы {escape(group['name'])}</b>\n\n"
        "Будут безвозвратно удалены все участники, их коды, предметы, пары и очереди. "
        "Участники получат уведомление и больше не смогут войти.\n\n"
        f"Чтобы подтвердить, отправьте название группы: <code>{escape(group['name'])}</code>",
        kb([("Отмена", "adm")]),
    )
    await call.answer()


@router.message(DeleteGroup.confirm, NOT_COMMAND)
async def group_delete_confirm(message: Message, state: FSMContext, bot: Bot, db: Database,
                               member: dict | None):
    if not member or not member["is_headman"]:
        await state.clear()
        return
    group = await db.get_group(member["group_id"])
    if normalize_name(message.text) != normalize_name(group["name"]):
        await message.answer("Название не совпадает. Отправьте его ещё раз или /cancel, чтобы отменить.")
        return
    await state.clear()
    others = [m for m in await db.list_members(group["id"]) if m["tg_id"] and m["id"] != member["id"]]
    await db.delete_group(group["id"])
    for m in others:
        await notify(bot, m["tg_id"], f"Староста удалил группу {escape(group['name'])}. Очереди больше недоступны.")
    await message.answer(
        f"Группа {escape(group['name'])} удалена.\nЧтобы создать новую, отправьте /creategroup."
    )


# ======================= предметы =======================

async def subjects_admin_view(db: Database, member: dict):
    subjects = await db.list_subjects(member["group_id"])
    lines = ["<b>Предметы</b>", ""]
    if not subjects:
        lines.append("Пока нет ни одного предмета.")
    for s in subjects:
        kind = "по подгруппам" if s["is_subgroup"] else "общий"
        lines.append(f"• {escape(s['name'])} — {kind}")
    rows = [[("Добавить предмет", "asa")]]
    rows += [[(f"{s['name']}", f"asd:{s['id']}")] for s in subjects]
    rows.append([BACK_ADMIN])
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "as")
async def cb_subjects(call: CallbackQuery, state: FSMContext, db: Database, member: dict | None):
    await state.clear()
    if not await guard_headman(call, member):
        return
    text, markup = await subjects_admin_view(db, member)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "asa")
async def cb_subject_add(call: CallbackQuery, state: FSMContext, member: dict | None):
    if not await guard_headman(call, member):
        return
    await state.set_state(AddSubject.name)
    await edit(call, "Введите название предмета:", kb([("Отмена", "as")]))
    await call.answer()


@router.message(AddSubject.name, NOT_COMMAND)
async def subject_name(message: Message, state: FSMContext, db: Database, member: dict | None):
    if not member or not member["is_headman"]:
        await state.clear()
        return
    name = " ".join(message.text.split())[:60]
    existing = {normalize_name(s["name"]) for s in await db.list_subjects(member["group_id"])}
    if normalize_name(name) in existing:
        await message.answer("Такой предмет уже есть. Введите другое название или /cancel.")
        return
    group = await db.get_group(member["group_id"])
    if group["subgroups_count"] == 1:
        await state.clear()
        await db.add_subject(member["group_id"], name, False)
        text, markup = await subjects_admin_view(db, member)
        await message.answer(f"Предмет «{escape(name)}» добавлен.")
        await message.answer(text, reply_markup=markup)
        return
    await state.update_data(name=name)
    await state.set_state(AddSubject.kind)
    await message.answer(
        f"Как проходят лабораторные по «{escape(name)}»?",
        reply_markup=kb([("Вся группа вместе (общая очередь)", "ast:0")],
                        [("По подгруппам (своя очередь у каждой)", "ast:1")],
                        [("Отмена", "as")]),
    )


@router.callback_query(AddSubject.kind, F.data.startswith("ast:"))
async def cb_subject_kind(call: CallbackQuery, state: FSMContext, db: Database, member: dict | None):
    if not await guard_headman(call, member):
        return
    data = await state.get_data()
    await state.clear()
    await db.add_subject(member["group_id"], data["name"], call.data == "ast:1")
    text, markup = await subjects_admin_view(db, member)
    await edit(call, text, markup)
    await call.answer(f"Предмет «{data['name']}» добавлен")


@router.callback_query(F.data.startswith("asd:"))
async def cb_subject_delete(call: CallbackQuery, db: Database, member: dict | None):
    if not await guard_headman(call, member):
        return
    subject = await own_subject(db, member, int(call.data.split(":")[1]))
    if not subject:
        await call.answer("Предмет не найден", show_alert=True)
        return
    await edit(call, f"Удалить предмет «{escape(subject['name'])}»? Его очереди и пары тоже удалятся.",
               kb([("Да, удалить", f"asdy:{subject['id']}"), ("Назад", "as")]))
    await call.answer()


@router.callback_query(F.data.startswith("asdy:"))
async def cb_subject_delete_confirm(call: CallbackQuery, db: Database, member: dict | None):
    if not await guard_headman(call, member):
        return
    subject = await own_subject(db, member, int(call.data.split(":")[1]))
    if subject:
        await db.delete_subject(subject["id"])
    text, markup = await subjects_admin_view(db, member)
    await edit(call, text, markup)
    await call.answer("Удалено")


# ======================= пары и опросы =======================

def lesson_label(lesson: dict, subject_name: str) -> str:
    sub = f" (подгр. {lesson['subgroup']})" if lesson["subgroup"] else ""
    return f"{fmt_dt(lesson_bounds(lesson)[0])} · {subject_name}{sub}"


async def lessons_admin_view(db: Database, member: dict):
    lessons = (await db.group_lessons(member["group_id"]))[:30]
    rows = [[("Добавить пару", "ala")]]
    rows += [[(lesson_label(l, l["subject_name"]), f"alv:{l['id']}")] for l in lessons]
    rows.append([BACK_ADMIN])
    text = "<b>Пары</b>\nВыберите пару, чтобы отправить опрос или посмотреть ответы."
    if not lessons:
        text += "\n\nЗапланированных пар нет."
    return text, kb(*rows)


@router.callback_query(F.data == "al")
async def cb_lessons(call: CallbackQuery, state: FSMContext, db: Database, member: dict | None):
    await state.clear()
    if not await guard_headman(call, member):
        return
    text, markup = await lessons_admin_view(db, member)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "ala")
async def cb_lesson_add(call: CallbackQuery, db: Database, member: dict | None):
    if not await guard_headman(call, member):
        return
    subjects = await db.list_subjects(member["group_id"])
    if not subjects:
        await call.answer("Сначала добавьте предмет", show_alert=True)
        return
    rows = [[(f"{s['name']}", f"alas:{s['id']}")] for s in subjects]
    rows.append([("Назад", "al")])
    await edit(call, "По какому предмету пара?", kb(*rows))
    await call.answer()


async def ask_lesson_time(call: CallbackQuery, state: FSMContext, subject: dict, sg: int):
    await state.set_state(AddLesson.when)
    await state.update_data(subject_id=subject["id"], subgroup=sg)
    await edit(
        call,
        f"Пара: <b>{escape(scope_title(subject, sg))}</b>\n\n"
        f"Введите дату и время начала: <code>ДД.ММ ЧЧ:ММ</code> или <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>.\n"
        f"Через пробел можно указать длительность в минутах (по умолчанию {DEFAULT_LESSON_MINUTES}).\n"
        f"Например: <code>24.10 10:40</code> или <code>24.10.2026 10:40 95</code>",
        kb([("Отмена", "al")]),
    )


@router.callback_query(F.data.startswith("alas:"))
async def cb_lesson_subject(call: CallbackQuery, state: FSMContext, db: Database, member: dict | None):
    if not await guard_headman(call, member):
        return
    subject = await own_subject(db, member, int(call.data.split(":")[1]))
    if not subject:
        await call.answer("Предмет не найден", show_alert=True)
        return
    subgroups = await subgroups_of(db, subject)
    if subgroups == [0]:
        await ask_lesson_time(call, state, subject, 0)
    else:
        rows = [[(f"Подгруппа {sg}", f"alag:{subject['id']}:{sg}") for sg in subgroups]]
        rows.append([("Назад", "ala")])
        await edit(call, f"Для какой подгруппы пара по «{escape(subject['name'])}»?", kb(*rows))
    await call.answer()


@router.callback_query(F.data.startswith("alag:"))
async def cb_lesson_subgroup(call: CallbackQuery, state: FSMContext, db: Database, member: dict | None):
    if not await guard_headman(call, member):
        return
    _, sid, sg = call.data.split(":")
    subject = await own_subject(db, member, int(sid))
    if not subject or int(sg) not in await subgroups_of(db, subject):
        await call.answer("Предмет не найден", show_alert=True)
        return
    await ask_lesson_time(call, state, subject, int(sg))
    await call.answer()


async def lesson_view(db: Database, lesson: dict, subject: dict):
    members = await db.scope_members(subject["group_id"], lesson["subgroup"])
    answers = await db.poll_answers(lesson["id"])
    start, end = lesson_bounds(lesson)
    status = "идёт сейчас" if start <= now_utc() < end else "запланирована"
    text = (
        f"<b>{escape(scope_title(subject, lesson['subgroup']))}</b>\n"
        f"Начало: {fmt_dt(start)}\nДлительность: {lesson['duration_min']} мин · {status}\n"
        f"Ответили на опрос: {len(answers)} из {len(members)}"
    )
    lid = lesson["id"]
    markup = kb(
        [("Отправить опрос", f"alp:{lid}")],
        [("Ответы на опрос", f"alr:{lid}")],
        [("Удалить пару", f"ald:{lid}")],
        [("К парам", "al")],
    )
    return text, markup


@router.message(AddLesson.when, NOT_COMMAND)
async def lesson_when(message: Message, state: FSMContext, db: Database, member: dict | None):
    if not member or not member["is_headman"]:
        await state.clear()
        return
    parsed = parse_lesson_input(message.text)
    if not parsed:
        await message.answer("Не понял дату. Пример: <code>24.10 10:40</code>. Попробуйте ещё раз или /cancel.")
        return
    start, duration = parsed
    duration = duration or DEFAULT_LESSON_MINUTES
    if not 10 <= duration <= 600:
        await message.answer("Длительность должна быть от 10 до 600 минут.")
        return
    if start <= now_utc():
        await message.answer("Это время уже прошло. Введите будущую дату.")
        return
    data = await state.get_data()
    await state.clear()
    subject = await own_subject(db, member, data["subject_id"])
    if not subject:
        return
    lesson_id = await db.add_lesson(subject["id"], data["subgroup"], start, duration)
    text, markup = await lesson_view(db, await db.get_lesson(lesson_id), subject)
    await message.answer("Пара добавлена. Можно сразу отправить опрос.")
    await message.answer(text, reply_markup=markup)


async def load_lesson(call: CallbackQuery, db: Database, member: dict | None):
    if not await guard_headman(call, member):
        return None, None
    lesson = await db.get_lesson(int(call.data.split(":")[1]))
    subject = await own_subject(db, member, lesson["subject_id"]) if lesson else None
    if not subject:
        await call.answer("Пара не найдена", show_alert=True)
        return None, None
    return lesson, subject


@router.callback_query(F.data.startswith("alv:"))
async def cb_lesson_view(call: CallbackQuery, db: Database, member: dict | None):
    lesson, subject = await load_lesson(call, db, member)
    if not lesson:
        return
    text, markup = await lesson_view(db, lesson, subject)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("alp:"))
async def cb_lesson_poll(call: CallbackQuery, bot: Bot, db: Database, member: dict | None):
    lesson, subject = await load_lesson(call, db, member)
    if not lesson:
        return
    if lesson["finished"]:
        await call.answer("Пара уже прошла", show_alert=True)
        return
    await call.answer("Отправляю опрос…")
    sent, total = await send_poll(bot, db, lesson, subject)
    text, markup = await lesson_view(db, lesson, subject)
    note = f"\n\nОпрос доставлен: {sent} из {total}."
    if sent < total:
        note += " Остальные ещё не вошли в бота или заблокировали его."
    await edit(call, text + note, markup)


@router.callback_query(F.data.startswith("alr:"))
async def cb_lesson_results(call: CallbackQuery, db: Database, member: dict | None):
    lesson, subject = await load_lesson(call, db, member)
    if not lesson:
        return
    members = await db.scope_members(subject["group_id"], lesson["subgroup"])
    answers = await db.poll_answers(lesson["id"])
    yes = [m for m in members if answers.get(m["id"]) == 1]
    no = [m for m in members if answers.get(m["id"]) == 0]
    none = [m for m in members if m["id"] not in answers]

    def block(title, items):
        names = "\n".join(f"  • {escape(m['full_name'])}" for m in items) or "  —"
        return f"{title} ({len(items)}):\n{names}"

    text = "\n\n".join([
        f"<b>Ответы: {escape(scope_title(subject, lesson['subgroup']))}</b>, {fmt_dt(lesson_bounds(lesson)[0])}",
        block("Будут сдавать", yes),
        block("Не будут", no),
        block("Не ответили", none),
    ])
    await edit(call, text[:4000], kb([("К паре", f"alv:{lesson['id']}")]))
    await call.answer()


@router.callback_query(F.data.startswith("ald:"))
async def cb_lesson_delete(call: CallbackQuery, db: Database, member: dict | None):
    lesson, subject = await load_lesson(call, db, member)
    if not lesson:
        return
    await edit(call, f"Удалить пару {escape(lesson_label(lesson, subject['name']))}?",
               kb([("Да, удалить", f"aldy:{lesson['id']}"), ("Назад", f"alv:{lesson['id']}")]))
    await call.answer()


@router.callback_query(F.data.startswith("aldy:"))
async def cb_lesson_delete_confirm(call: CallbackQuery, db: Database, member: dict | None):
    lesson, _ = await load_lesson(call, db, member)
    if not lesson:
        return
    await db.delete_lesson(lesson["id"])
    text, markup = await lessons_admin_view(db, member)
    await edit(call, text, markup)
    await call.answer("Пара удалена")


# ======================= редактирование очереди =======================

@router.callback_query(F.data == "aq")
async def cb_edit_choose(call: CallbackQuery, state: FSMContext, db: Database, member: dict | None):
    await state.clear()
    if not await guard_headman(call, member):
        return
    subjects = await db.list_subjects(member["group_id"])
    if not subjects:
        await call.answer("Сначала добавьте предмет", show_alert=True)
        return
    rows = [[(f"{s['name']}", f"aqs:{s['id']}")] for s in subjects]
    rows.append([BACK_ADMIN])
    await edit(call, "Очередь какого предмета редактировать?", kb(*rows))
    await call.answer()


@router.callback_query(F.data.startswith("aqs:"))
async def cb_edit_subject(call: CallbackQuery, state: FSMContext, db: Database, qs: QueueService,
                          member: dict | None):
    if not await guard_headman(call, member):
        return
    subject = await own_subject(db, member, int(call.data.split(":")[1]))
    if not subject:
        await call.answer("Предмет не найден", show_alert=True)
        return
    subgroups = await subgroups_of(db, subject)
    if subgroups == [0]:
        text, markup = await edit_view(db, qs, subject, 0)
        await edit(call, text, markup)
    else:
        rows = [[(f"Подгруппа {sg}", f"e:{subject['id']}:{sg}") for sg in subgroups], [("Назад", "aq")]]
        await edit(call, f"Очередь какой подгруппы по «{escape(subject['name'])}»?", kb(*rows))
    await call.answer()


async def edit_view(db: Database, qs: QueueService, subject: dict, sg: int):
    sid = subject["id"]
    text = await render_queue(db, qs, subject, sg)
    text += "\n\nНажмите на участника, чтобы переместить его, отметить сдачу или убрать из очереди."
    rows = [
        [(f"{i}. {e['full_name']}" + (" (уваж.)" if e["excused"] else ""), f"eo:{sid}:{e['member_id']}")]
        for i, e in enumerate(await qs.entries(sid, sg), 1)
    ]
    rows.append([("Добавить участника", f"ea:{sid}:{sg}"), ("Очистить", f"ec:{sid}:{sg}")])
    rows.append([("Обновить", f"e:{sid}:{sg}"), ("К предметам", "aq")])
    return text, kb(*rows)


async def load_scope(call: CallbackQuery, db: Database, member: dict | None):
    """callback 'xx:<sid>:<sg>' -> (subject, sg)."""
    if not await guard_headman(call, member):
        return None, None
    _, sid, sg = call.data.split(":")[:3]
    subject = await own_subject(db, member, int(sid))
    if not subject or int(sg) not in await subgroups_of(db, subject):
        await call.answer("Очередь не найдена", show_alert=True)
        return None, None
    return subject, int(sg)


async def load_entry(db: Database, member: dict, sid: int, mid: int):
    """Возвращает (subject, target, sg) для участника очереди, если он из группы старосты."""
    subject = await own_subject(db, member, sid)
    target = await db.get_member(mid)
    if not subject or not target or target["group_id"] != member["group_id"]:
        return None, None, None
    sg = target["subgroup"] if subject["is_subgroup"] else 0
    return subject, target, sg


@router.callback_query(F.data.startswith("e:"))
async def cb_edit_view(call: CallbackQuery, state: FSMContext, db: Database, qs: QueueService,
                       member: dict | None):
    await state.clear()
    subject, sg = await load_scope(call, db, member)
    if not subject:
        return
    text, markup = await edit_view(db, qs, subject, sg)
    await edit(call, text, markup)
    await call.answer()


async def entry_view(qs: QueueService, subject: dict, target: dict, sg: int):
    sid, mid = subject["id"], target["id"]
    entries = await qs.entries(sid, sg)
    entry = next((e for e in entries if e["member_id"] == mid), None)
    if entry is None:
        return None, None
    pos = entries.index(entry) + 1
    excuse = f"да ({escape(entry['excuse_reason'] or '')})" if entry["excused"] else "нет"
    text = (
        f"<b>{escape(scope_title(subject, sg))}</b>\n\n"
        f"Участник: <b>{escape(target['full_name'])}</b>\n"
        f"Место: {pos} из {len(entries)}\n"
        f"Уважительная причина: {excuse}"
    )
    p = f"em:{sid}:{mid}"
    markup = kb(
        [("В начало", f"{p}:top"), ("Выше", f"{p}:up"), ("Ниже", f"{p}:down"), ("В конец", f"{p}:end")],
        [("На место №…", f"{p}:pos")],
        [("Сдал", f"{p}:done"), ("Уваж. причина", f"{p}:exc"), ("Убрать", f"{p}:del")],
        [("К очереди", f"e:{sid}:{sg}")],
    )
    return text, markup


@router.callback_query(F.data.startswith("eo:"))
async def cb_entry(call: CallbackQuery, db: Database, qs: QueueService, member: dict | None):
    if not await guard_headman(call, member):
        return
    _, sid, mid = call.data.split(":")
    subject, target, sg = await load_entry(db, member, int(sid), int(mid))
    text, markup = await entry_view(qs, subject, target, sg) if subject else (None, None)
    if not text:
        await call.answer("Участника уже нет в очереди", show_alert=True)
        return
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("em:"))
async def cb_entry_op(call: CallbackQuery, state: FSMContext, bot: Bot, db: Database, qs: QueueService,
                      member: dict | None):
    if not await guard_headman(call, member):
        return
    _, sid, mid, op = call.data.split(":")
    subject, target, sg = await load_entry(db, member, int(sid), int(mid))
    if not subject or await qs.position(subject["id"], sg, target["id"]) is None:
        await call.answer("Участника уже нет в очереди", show_alert=True)
        return
    sid, mid = subject["id"], target["id"]
    title = escape(scope_title(subject, sg))

    if op == "pos":
        await state.set_state(MovePos.pos)
        await state.update_data(subject_id=sid, member_id=mid)
        await edit(call, f"На какое место поставить {escape(target['full_name'])}? Отправьте число.",
                   kb([("Отмена", f"eo:{sid}:{mid}")]))
        await call.answer()
        return

    old_first = await first_active_id(qs, sid, sg)
    answer = "Готово"
    if op in ("done", "del"):
        await qs.remove(sid, sg, mid)
        msg = "Староста отметил, что вы сдали лабораторную" if op == "done" else "Староста убрал вас из очереди"
        await notify(bot, target["tg_id"], f"{msg} ({title}).")
        await notify_if_first_changed(bot, db, qs, subject, sg, old_first)
        text, markup = await edit_view(db, qs, subject, sg)
        await edit(call, text, markup)
        await call.answer("Убран из очереди")
        return

    if op == "exc":
        entries = await qs.entries(sid, sg)
        entry = next(e for e in entries if e["member_id"] == mid)
        await qs.set_excused(sid, mid, not entry["excused"], "отмечено старостой")
        answer = "Отметка снята" if entry["excused"] else "Отмечена уважительная причина"
    else:
        if op == "top":
            pos = await qs.move(sid, sg, mid, 0)
        elif op == "up":
            pos = await qs.move_by(sid, sg, mid, -1)
        elif op == "down":
            pos = await qs.move_by(sid, sg, mid, 1)
        else:
            pos = await qs.move_to_end(sid, sg, mid)
        answer = f"Теперь место {pos}"
        await notify(bot, target["tg_id"], f"Староста переместил вас в очереди {title}: теперь ваше место {pos}.")
    await notify_if_first_changed(bot, db, qs, subject, sg, old_first)
    text, markup = await entry_view(qs, subject, target, sg)
    await edit(call, text, markup)
    await call.answer(answer)


@router.message(MovePos.pos, NOT_COMMAND)
async def move_pos(message: Message, state: FSMContext, bot: Bot, db: Database, qs: QueueService,
                   member: dict | None):
    if not member or not member["is_headman"]:
        await state.clear()
        return
    if not message.text.strip().isdigit():
        await message.answer("Нужно число. Попробуйте ещё раз или /cancel.")
        return
    data = await state.get_data()
    await state.clear()
    subject, target, sg = await load_entry(db, member, data["subject_id"], data["member_id"])
    if not subject:
        return
    old_first = await first_active_id(qs, subject["id"], sg)
    pos = await qs.move(subject["id"], sg, target["id"], int(message.text.strip()) - 1)
    if pos is None:
        await message.answer("Участника уже нет в очереди.")
        return
    await notify(bot, target["tg_id"],
                 f"Староста переместил вас в очереди {escape(scope_title(subject, sg))}: теперь ваше место {pos}.")
    await notify_if_first_changed(bot, db, qs, subject, sg, old_first)
    text, markup = await entry_view(qs, subject, target, sg)
    await message.answer(text, reply_markup=markup)


async def add_candidates_view(db: Database, qs: QueueService, subject: dict, sg: int):
    in_queue = set(await qs.ids(subject["id"], sg))
    candidates = [m for m in await db.scope_members(subject["group_id"], sg) if m["id"] not in in_queue]
    rows = [[(m["full_name"], f"eaa:{subject['id']}:{sg}:{m['id']}")] for m in candidates]
    rows.append([("К очереди", f"e:{subject['id']}:{sg}")])
    text = "Кого добавить в конец очереди?" if candidates else "Все участники уже в очереди."
    return text, kb(*rows)


@router.callback_query(F.data.startswith("ea:"))
async def cb_edit_add(call: CallbackQuery, db: Database, qs: QueueService, member: dict | None):
    subject, sg = await load_scope(call, db, member)
    if not subject:
        return
    text, markup = await add_candidates_view(db, qs, subject, sg)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("eaa:"))
async def cb_edit_add_member(call: CallbackQuery, db: Database, qs: QueueService, member: dict | None):
    subject, sg = await load_scope(call, db, member)
    if not subject:
        return
    target = await db.get_member(int(call.data.split(":")[3]))
    if not target or target["group_id"] != member["group_id"] or (sg and target["subgroup"] != sg):
        await call.answer("Участник не найден", show_alert=True)
        return
    pos, _ = await qs.add(subject["id"], sg, target["id"])
    text, markup = await add_candidates_view(db, qs, subject, sg)
    await edit(call, text, markup)
    await call.answer(f"{target['full_name']}: место {pos}")


@router.callback_query(F.data.startswith("ec:"))
async def cb_edit_clear(call: CallbackQuery, db: Database, member: dict | None):
    subject, sg = await load_scope(call, db, member)
    if not subject:
        return
    await edit(call, f"Очистить очередь {escape(scope_title(subject, sg))} полностью?",
               kb([("Да, очистить", f"ecy:{subject['id']}:{sg}"), ("Назад", f"e:{subject['id']}:{sg}")]))
    await call.answer()


@router.callback_query(F.data.startswith("ecy:"))
async def cb_edit_clear_confirm(call: CallbackQuery, db: Database, qs: QueueService, member: dict | None):
    subject, sg = await load_scope(call, db, member)
    if not subject:
        return
    await qs.clear(subject["id"], sg)
    text, markup = await edit_view(db, qs, subject, sg)
    await edit(call, text, markup)
    await call.answer("Очередь очищена")


# ======================= участники =======================

async def members_view(db: Database, member: dict):
    group = await db.get_group(member["group_id"])
    members = await db.list_members(group["id"])
    lines = [f"<b>Участники ({len(members)})</b>", ""]
    current_sg = None
    for m in members:
        if group["subgroups_count"] > 1 and m["subgroup"] != current_sg:
            current_sg = m["subgroup"]
            lines.append(f"<b>Подгруппа {current_sg}</b>")
        role = " (староста)" if m["is_headman"] else ""
        status = "" if m["tg_id"] else " · ещё не вошёл"
        lines.append(f"{escape(m['full_name'])}{role} — <code>{m['code']}</code>{status}")
    markup = kb(
        [("Добавить", "ama"), ("Удалить", "amr")],
        [("Выдать новый код", "amc")],
        [BACK_ADMIN],
    )
    return "\n".join(lines), markup


@router.callback_query(F.data == "am")
async def cb_members(call: CallbackQuery, state: FSMContext, db: Database, member: dict | None):
    await state.clear()
    if not await guard_headman(call, member):
        return
    text, markup = await members_view(db, member)
    if len(text) > 4000:
        await call.message.answer(text[:4000])
        text = "Список выше."
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "ama")
async def cb_members_add(call: CallbackQuery, state: FSMContext, db: Database, member: dict | None):
    if not await guard_headman(call, member):
        return
    group = await db.get_group(member["group_id"])
    await state.set_state(AddMembers.text)
    await edit(call, "Отправьте новых участников.\n" + members_format_hint(group["subgroups_count"]),
               kb([("Отмена", "am")]))
    await call.answer()


@router.message(AddMembers.text, NOT_COMMAND)
async def members_add(message: Message, state: FSMContext, bot: Bot, db: Database, member: dict | None):
    if not member or not member["is_headman"]:
        await state.clear()
        return
    group = await db.get_group(member["group_id"])
    parsed, errors = parse_members(message.text, group["subgroups_count"])
    if errors or not parsed:
        bad = "\n".join(f"• {escape(e)}" for e in errors[:15])
        await message.answer(f"Не удалось разобрать:\n{bad}\n\n{members_format_hint(group['subgroups_count'])}")
        return
    await state.clear()
    created = [await db.add_member(group["id"], name, sg) for name, sg in parsed]
    me = await bot.get_me()
    text, markup = await members_view(db, member)
    await send_long(message, "Добавлены участники:\n" + codes_text(created, me.username))
    await send_long(message, text, reply_markup=markup)


async def pick_member_view(db: Database, member: dict, prefix: str, title: str):
    members = [m for m in await db.list_members(member["group_id"]) if m["id"] != member["id"]]
    rows = [[(f"{m['full_name']} (подгр. {m['subgroup']})", f"{prefix}:{m['id']}")] for m in members]
    rows.append([("Назад", "am")])
    return title, kb(*rows)


async def load_group_member(call: CallbackQuery, db: Database, member: dict | None) -> dict | None:
    if not await guard_headman(call, member):
        return None
    target = await db.get_member(int(call.data.split(":")[1]))
    if not target or target["group_id"] != member["group_id"] or target["id"] == member["id"]:
        await call.answer("Участник не найден", show_alert=True)
        return None
    return target


@router.callback_query(F.data == "amr")
async def cb_members_remove(call: CallbackQuery, db: Database, member: dict | None):
    if not await guard_headman(call, member):
        return
    text, markup = await pick_member_view(db, member, "amrd", "Кого удалить из группы?")
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("amrd:"))
async def cb_members_remove_ask(call: CallbackQuery, db: Database, member: dict | None):
    target = await load_group_member(call, db, member)
    if not target:
        return
    await edit(call, f"Удалить {escape(target['full_name'])} из группы? Он пропадёт из всех очередей.",
               kb([("Да, удалить", f"amry:{target['id']}"), ("Назад", "amr")]))
    await call.answer()


@router.callback_query(F.data.startswith("amry:"))
async def cb_members_remove_confirm(call: CallbackQuery, db: Database, member: dict | None):
    target = await load_group_member(call, db, member)
    if not target:
        return
    await db.delete_member(target["id"])
    text, markup = await pick_member_view(
        db, member, "amrd", f"Удалён: {escape(target['full_name'])}. Кого ещё удалить?"
    )
    await edit(call, text, markup)
    await call.answer("Удалён")


@router.callback_query(F.data == "amc")
async def cb_members_code(call: CallbackQuery, db: Database, member: dict | None):
    if not await guard_headman(call, member):
        return
    text, markup = await pick_member_view(
        db, member, "amcn",
        "Кому выдать новый код? Старый код перестанет работать, а участник выйдет из бота.",
    )
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("amcn:"))
async def cb_members_code_new(call: CallbackQuery, bot: Bot, db: Database, member: dict | None):
    target = await load_group_member(call, db, member)
    if not target:
        return
    code = await db.regenerate_code(target["id"])
    await notify(bot, target["tg_id"], "Староста сбросил ваш код входа. Попросите у него новый и отправьте /start.")
    await edit(call, f"Новый код для {escape(target['full_name'])}: <code>{code}</code>",
               kb([("К участникам", "am")]))
    await call.answer()


# ======================= Google Таблицы =======================

async def queue_tables(db: Database, qs: QueueService, group_id: int) -> dict[str, tuple[dict, int]]:
    """Название листа -> (предмет, подгруппа) для всех очередей группы."""
    tables = {}
    for subject in await db.list_subjects(group_id):
        for sg in await subgroups_of(db, subject):
            tables[sheet_title(scope_title(subject, sg))] = (subject, sg)
    return tables


@router.callback_query(F.data == "ag")
async def cb_google(call: CallbackQuery, state: FSMContext, db: Database, sheets: SheetsClient,
                    member: dict | None):
    await state.clear()
    if not await guard_headman(call, member):
        return
    group = await db.get_group(member["group_id"])
    lines = ["<b>Google Таблицы</b>", ""]
    if not sheets.available:
        lines.append("На сервере бота не настроен ключ сервисного аккаунта Google (см. README).")
    else:
        lines.append(f"1. Создайте Google Таблицу и откройте доступ <b>редактора</b> для\n"
                     f"<code>{escape(sheets.service_email() or '?')}</code>")
        lines.append("2. Укажите ссылку на неё кнопкой ниже.")
    lines.append(f"\nТаблица: {escape(group['sheet_url']) if group['sheet_url'] else 'не указана'}")
    lines.append(
        "\n<b>Экспорт</b> создаёт отдельный лист для каждой очереди.\n"
        "<b>Импорт</b> берёт листы с теми же названиями и <b>заменяет</b> очереди в боте "
        "порядком из таблицы (по ФИО, столбец B). Чтобы отметить уважительную причину, "
        "заполните столбец D."
    )
    rows = [[("Указать таблицу", "agl")]]
    if group["sheet_url"]:
        rows.append([("Экспорт в таблицу", "age"), ("Импорт из таблицы", "agi")])
    rows.append([BACK_ADMIN])
    await edit(call, "\n".join(lines), kb(*rows))
    await call.answer()


@router.callback_query(F.data == "agl")
async def cb_google_link(call: CallbackQuery, state: FSMContext, member: dict | None):
    if not await guard_headman(call, member):
        return
    await state.set_state(SetSheet.url)
    await edit(call, "Отправьте ссылку на Google Таблицу (https://docs.google.com/spreadsheets/…):",
               kb([("Отмена", "ag")]))
    await call.answer()


@router.message(SetSheet.url, NOT_COMMAND)
async def google_link(message: Message, state: FSMContext, db: Database, member: dict | None):
    if not member or not member["is_headman"]:
        await state.clear()
        return
    url = message.text.strip()
    if "docs.google.com/spreadsheets/" not in url:
        await message.answer("Это не похоже на ссылку на Google Таблицу. Попробуйте ещё раз или /cancel.")
        return
    await state.clear()
    await db.set_sheet_url(member["group_id"], url)
    await message.answer("Ссылка сохранена.", reply_markup=kb([("К Google Таблицам", "ag")]))


@router.callback_query(F.data == "age")
async def cb_google_export(call: CallbackQuery, db: Database, qs: QueueService, sheets: SheetsClient,
                           member: dict | None):
    if not await guard_headman(call, member):
        return
    group = await db.get_group(member["group_id"])
    if not group["sheet_url"]:
        await call.answer("Сначала укажите таблицу", show_alert=True)
        return
    await call.answer("Экспортирую…")
    data = {}
    for title, (subject, sg) in (await queue_tables(db, qs, group["id"])).items():
        rows = [HEADER]
        for i, e in enumerate(await qs.entries(subject["id"], sg), 1):
            reason = (e["excuse_reason"] or "да") if e["excused"] else ""
            rows.append([i, e["full_name"], e["subgroup"], reason])
        data[title] = rows
    try:
        await asyncio.to_thread(sheets.export, group["sheet_url"], data)
    except SheetsError as e:
        await call.message.answer(f"Ошибка: {escape(str(e))}")
        return
    await call.message.answer(f"Экспортировано очередей: {len(data)}.")


@router.callback_query(F.data == "agi")
async def cb_google_import_ask(call: CallbackQuery, member: dict | None):
    if not await guard_headman(call, member):
        return
    await edit(call, "Очереди в боте будут заменены данными из таблицы. Продолжить?",
               kb([("Да, импортировать", "agiy"), ("Назад", "ag")]))
    await call.answer()


@router.callback_query(F.data == "agiy")
async def cb_google_import(call: CallbackQuery, db: Database, qs: QueueService, sheets: SheetsClient,
                           member: dict | None):
    if not await guard_headman(call, member):
        return
    group = await db.get_group(member["group_id"])
    if not group["sheet_url"]:
        await call.answer("Сначала укажите таблицу", show_alert=True)
        return
    await call.answer("Импортирую…")
    try:
        sheet_data = await asyncio.to_thread(sheets.read_all, group["sheet_url"])
    except SheetsError as e:
        await call.message.answer(f"Ошибка: {escape(str(e))}")
        return

    report = []
    for title, (subject, sg) in (await queue_tables(db, qs, group["id"])).items():
        if title not in sheet_data:
            continue
        by_name = {normalize_name(m["full_name"]): m for m in await db.scope_members(group["id"], sg)}
        items, unknown, seen = [], [], set()
        for name, reason in parse_sheet_rows(sheet_data[title]):
            m = by_name.get(normalize_name(name))
            if not m:
                unknown.append(name)
            elif m["id"] not in seen:
                seen.add(m["id"])
                items.append((m["id"], reason or None))
        await qs.replace(subject["id"], sg, items)
        line = f"• {escape(title)}: {len(items)} чел."
        if unknown:
            line += f"\n  не найдены в группе: {escape(', '.join(unknown[:10]))}"
        report.append(line)

    if not report:
        await call.message.answer("В таблице нет листов с названиями очередей. Сначала сделайте экспорт.")
        return
    await send_long(call.message, "Импорт завершён:\n" + "\n".join(report))
