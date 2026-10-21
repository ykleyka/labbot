"""Действия участника: просмотр очереди, запись, отмена, обмен местами, ответы на опрос."""
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..db import Database
from ..queue_service import QueueService
from ..services import (
    NOT_AUTH, active_lesson, first_active_id, guard, member_scope, notify, notify_headmen,
    notify_if_first_changed, own_subject, poll_markup, poll_text, render_queue,
)
from ..utils import edit, fmt_dt, kb, lesson_bounds, scope_title

router = Router(name="member")

NOT_COMMAND = F.text & ~F.text.startswith("/")


class Excuse(StatesGroup):
    reason = State()


# ---------- списки ----------

async def subjects_view(db: Database, member: dict):
    subjects = await db.list_subjects(member["group_id"])
    if not subjects:
        text = "📚 Предметов пока нет."
        if member["is_headman"]:
            text += " Добавьте их: 👑 Управление → 📚 Предметы."
        return text, kb([("⬅️ Главное меню", "m:main")])
    rows = [[(f"📘 {s['name']}", f"q:{s['id']}")] for s in subjects]
    rows.append([("⬅️ Главное меню", "m:main")])
    return "📋 Выберите предмет:", kb(*rows)


async def lessons_view(db: Database, member: dict):
    lessons = [
        l for l in await db.group_lessons(member["group_id"])
        if l["subgroup"] in (0, member["subgroup"])
    ][:15]
    lines = ["📅 <b>Ближайшие пары</b>", ""]
    if not lessons:
        lines.append("Пары пока не запланированы.")
    for l in lessons:
        sub = f" (подгр. {l['subgroup']})" if l["subgroup"] else ""
        lines.append(f"• {fmt_dt(lesson_bounds(l)[0])} — {escape(l['subject_name'])}{sub}")
    return "\n".join(lines), kb([("⬅️ Главное меню", "m:main")])


async def queue_view(db: Database, qs: QueueService, member: dict, subject: dict):
    sid = subject["id"]
    sg = member_scope(subject, member)
    text = await render_queue(db, qs, subject, sg, viewer_id=member["id"])
    pos = await qs.position(sid, sg, member["id"])
    if pos is None:
        text += "\n\nВы не записаны в очередь."
        rows = [[("✋ Буду сдавать", f"j:{sid}")]]
    else:
        text += f"\n\nВаше место: <b>{pos}</b>"
        rows = [
            [("🚫 Отменить сдачу", f"c:{sid}"), ("✅ Я сдал", f"dn:{sid}")],
            [("🔁 Поменяться местами", f"sw:{sid}")],
        ]
    rows.append([("🔄 Обновить", f"q:{sid}"), ("⬅️ К предметам", "m:queues")])
    return text, kb(*rows)


@router.message(Command("queue"))
async def cmd_queue(message: Message, db: Database, member: dict | None):
    if not member:
        await message.answer(NOT_AUTH)
        return
    text, markup = await subjects_view(db, member)
    await message.answer(text, reply_markup=markup)


@router.message(Command("lessons"))
async def cmd_lessons(message: Message, db: Database, member: dict | None):
    if not member:
        await message.answer(NOT_AUTH)
        return
    text, markup = await lessons_view(db, member)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "m:queues")
async def cb_queues(call: CallbackQuery, db: Database, member: dict | None):
    if not await guard(call, member):
        return
    text, markup = await subjects_view(db, member)
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "m:lessons")
async def cb_lessons(call: CallbackQuery, db: Database, member: dict | None):
    if not await guard(call, member):
        return
    text, markup = await lessons_view(db, member)
    await edit(call, text, markup)
    await call.answer()


async def load_subject(call: CallbackQuery, db: Database, member: dict | None) -> dict | None:
    """Проверяет вход и достаёт предмет из callback_data вида 'xx:<subject_id>...'."""
    if not await guard(call, member):
        return None
    subject = await own_subject(db, member, int(call.data.split(":")[1]))
    if not subject:
        await call.answer("Предмет не найден", show_alert=True)
    return subject


@router.callback_query(F.data.startswith("q:"))
async def cb_queue(call: CallbackQuery, state: FSMContext, db: Database, qs: QueueService, member: dict | None):
    await state.clear()
    subject = await load_subject(call, db, member)
    if not subject:
        return
    text, markup = await queue_view(db, qs, member, subject)
    await edit(call, text, markup)
    await call.answer()


# ---------- запись и отмена ----------

@router.callback_query(F.data.startswith("j:"))
async def cb_join(call: CallbackQuery, db: Database, qs: QueueService, member: dict | None):
    subject = await load_subject(call, db, member)
    if not subject:
        return
    pos, added = await qs.add(subject["id"], member_scope(subject, member), member["id"])
    text, markup = await queue_view(db, qs, member, subject)
    await edit(call, text, markup)
    await call.answer(f"Вы записаны, ваше место: {pos}" if added else "Вы уже в очереди")


@router.callback_query(F.data.startswith("c:"))
async def cb_cancel(call: CallbackQuery, db: Database, qs: QueueService, member: dict | None):
    subject = await load_subject(call, db, member)
    if not subject:
        return
    sid, sg = subject["id"], member_scope(subject, member)
    if await qs.position(sid, sg, member["id"]) is None:
        await call.answer("Вас нет в очереди", show_alert=True)
        return
    if await active_lesson(db, sid, sg):
        text = (
            "Сейчас идёт пара. Если вы отмените сдачу, то переместитесь в <b>конец очереди</b>.\n"
            "Если вы отсутствуете по уважительной причине, ваше место сохранится до следующей пары."
        )
        markup = kb(
            [("🟡 Уважительная причина", f"cx:{sid}:1")],
            [("↩️ Переместиться в конец", f"cx:{sid}:0")],
            [("⬅️ Назад", f"q:{sid}")],
        )
    else:
        text = "Убрать вас из очереди? Записаться снова можно в любой момент, но уже в конец."
        markup = kb([("✅ Да, убрать", f"cx:{sid}:0"), ("⬅️ Назад", f"q:{sid}")])
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("cx:"))
async def cb_cancel_confirm(
    call: CallbackQuery, state: FSMContext, bot: Bot, db: Database, qs: QueueService, member: dict | None
):
    subject = await load_subject(call, db, member)
    if not subject:
        return
    sid, sg = subject["id"], member_scope(subject, member)
    excused = call.data.split(":")[2] == "1"
    if await qs.position(sid, sg, member["id"]) is None:
        await call.answer("Вас нет в очереди", show_alert=True)
        return

    if not await active_lesson(db, sid, sg):
        await qs.remove(sid, sg, member["id"])
        answer = "Вы убраны из очереди"
    elif excused:
        await state.set_state(Excuse.reason)
        await state.update_data(subject_id=sid)
        await edit(call, "✍️ Напишите коротко причину отсутствия (её увидит староста):",
                   kb([("⬅️ Отмена", f"q:{sid}")]))
        await call.answer()
        return
    else:
        old_first = await first_active_id(qs, sid, sg)
        await qs.set_excused(sid, member["id"], False)
        pos = await qs.move_to_end(sid, sg, member["id"])
        await notify_if_first_changed(bot, db, qs, subject, sg, old_first)
        answer = f"Вы перемещены в конец очереди (место {pos})"

    text, markup = await queue_view(db, qs, member, subject)
    await edit(call, text, markup)
    await call.answer(answer)


@router.message(Excuse.reason, NOT_COMMAND)
async def excuse_reason(message: Message, state: FSMContext, bot: Bot, db: Database, qs: QueueService,
                        member: dict | None):
    data = await state.get_data()
    await state.clear()
    if not member:
        return
    subject = await own_subject(db, member, data["subject_id"])
    if not subject:
        return
    sid, sg = subject["id"], member_scope(subject, member)
    if await qs.position(sid, sg, member["id"]) is None:
        await message.answer("Вас уже нет в очереди.")
        return
    reason = message.text.strip()[:200]
    old_first = await first_active_id(qs, sid, sg)
    await qs.set_excused(sid, member["id"], True, reason)
    await notify_if_first_changed(bot, db, qs, subject, sg, old_first)
    await notify_headmen(
        bot, db, member["group_id"],
        f"🟡 <b>{escape(member['full_name'])}</b> отсутствует по уважительной причине "
        f"({escape(scope_title(subject, sg))}): {escape(reason)}",
        exclude_id=member["id"],
    )
    text, markup = await queue_view(db, qs, member, subject)
    await message.answer("✅ Отмечено. Ваше место в очереди сохранится до следующей пары.")
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("dn:"))
async def cb_done(call: CallbackQuery, db: Database, member: dict | None):
    subject = await load_subject(call, db, member)
    if not subject:
        return
    sid = subject["id"]
    await edit(call, "Подтвердите, что вы сдали лабораторную: вас уберут из очереди.",
               kb([("✅ Да, сдал", f"dny:{sid}"), ("⬅️ Назад", f"q:{sid}")]))
    await call.answer()


@router.callback_query(F.data.startswith("dny:"))
async def cb_done_confirm(call: CallbackQuery, bot: Bot, db: Database, qs: QueueService, member: dict | None):
    subject = await load_subject(call, db, member)
    if not subject:
        return
    sid, sg = subject["id"], member_scope(subject, member)
    old_first = await first_active_id(qs, sid, sg)
    removed = await qs.remove(sid, sg, member["id"])
    await notify_if_first_changed(bot, db, qs, subject, sg, old_first)
    text, markup = await queue_view(db, qs, member, subject)
    await edit(call, text, markup)
    await call.answer("🎉 Поздравляем со сдачей!" if removed else "Вас нет в очереди")


# ---------- обмен местами ----------

@router.callback_query(F.data.startswith("sw:"))
async def cb_swap_list(call: CallbackQuery, db: Database, qs: QueueService, member: dict | None):
    subject = await load_subject(call, db, member)
    if not subject:
        return
    sid, sg = subject["id"], member_scope(subject, member)
    entries = await qs.entries(sid, sg)
    my_pos = next((i for i, e in enumerate(entries, 1) if e["member_id"] == member["id"]), None)
    if my_pos is None:
        await call.answer("Вас нет в очереди", show_alert=True)
        return
    rows = [
        [(f"{i}. {e['full_name']}", f"sr:{sid}:{e['member_id']}")]
        for i, e in enumerate(entries, 1)
        if e["member_id"] != member["id"] and e["tg_id"]
    ]
    if not rows:
        await call.answer("Поменяться не с кем: в очереди нет других участников, вошедших в бота", show_alert=True)
        return
    rows.append([("⬅️ Назад", f"q:{sid}")])
    await edit(call, f"🔁 Вы на {my_pos}-м месте. С кем хотите поменяться?\n"
                     f"Выше — пойти раньше, ниже — пойти позже.", kb(*rows))
    await call.answer()


@router.callback_query(F.data.startswith("sr:"))
async def cb_swap_request(call: CallbackQuery, bot: Bot, db: Database, qs: QueueService, member: dict | None):
    subject = await load_subject(call, db, member)
    if not subject:
        return
    sid, sg = subject["id"], member_scope(subject, member)
    target = await db.get_member(int(call.data.split(":")[2]))
    my_pos = await qs.position(sid, sg, member["id"])
    target_pos = await qs.position(sid, sg, target["id"]) if target else None
    if my_pos is None or target_pos is None or target["group_id"] != member["group_id"]:
        await call.answer("Кого-то из вас уже нет в этой очереди", show_alert=True)
        return

    swap_id = await db.create_swap(sid, member["id"], target["id"])
    direction = "раньше" if target_pos > my_pos else "позже"
    sent = await notify(
        bot, target["tg_id"],
        f"🔁 <b>{escape(member['full_name'])}</b> (место {my_pos}) предлагает поменяться местами "
        f"в очереди <b>{escape(scope_title(subject, sg))}</b>.\n"
        f"Вы сейчас на месте {target_pos}. Если согласитесь, пойдёте {direction}.",
        kb([("✅ Согласен", f"swa:{swap_id}"), ("❌ Отказаться", f"swd:{swap_id}")]),
    )
    if not sent:
        await db.set_swap_status(swap_id, "cancelled")
        await call.answer("Не удалось отправить запрос: участник недоступен", show_alert=True)
        return
    text, markup = await queue_view(db, qs, member, subject)
    await edit(call, text, markup)
    await call.answer(f"Запрос отправлен: {target['full_name']}", show_alert=True)


async def load_swap(call: CallbackQuery, db: Database, member: dict | None) -> dict | None:
    if not await guard(call, member):
        return None
    swap = await db.get_swap(int(call.data.split(":")[1]))
    if not swap or swap["to_member"] != member["id"]:
        await call.answer("Запрос не найден", show_alert=True)
        return None
    if swap["status"] != "pending":
        await call.answer("Этот запрос уже неактуален", show_alert=True)
        await call.message.edit_reply_markup(reply_markup=None)
        return None
    return swap


@router.callback_query(F.data.startswith("swa:"))
async def cb_swap_accept(call: CallbackQuery, bot: Bot, db: Database, qs: QueueService, member: dict | None):
    swap = await load_swap(call, db, member)
    if not swap:
        return
    subject = await db.get_subject(swap["subject_id"])
    requester = await db.get_member(swap["from_member"])
    sg = member_scope(subject, member)
    if not requester or member_scope(subject, requester) != sg:
        await db.set_swap_status(swap["id"], "cancelled")
        await edit(call, "Обмен невозможен: участник больше не в этой очереди.")
        await call.answer()
        return

    old_first = await first_active_id(qs, subject["id"], sg)
    if not await qs.swap(subject["id"], sg, requester["id"], member["id"]):
        await db.set_swap_status(swap["id"], "cancelled")
        await edit(call, "Обмен невозможен: кого-то из вас уже нет в очереди.")
        await call.answer()
        return
    await db.set_swap_status(swap["id"], "accepted")
    my_pos = await qs.position(subject["id"], sg, member["id"])
    their_pos = await qs.position(subject["id"], sg, requester["id"])
    title = escape(scope_title(subject, sg))
    await edit(call, f"✅ Вы поменялись местами с {escape(requester['full_name'])} ({title}). Ваше место: {my_pos}.")
    await notify(bot, requester["tg_id"],
                 f"✅ {escape(member['full_name'])} согласился поменяться местами ({title}). Ваше место: {their_pos}.")
    await notify_if_first_changed(bot, db, qs, subject, sg, old_first)
    await call.answer()


@router.callback_query(F.data.startswith("swd:"))
async def cb_swap_decline(call: CallbackQuery, bot: Bot, db: Database, member: dict | None):
    swap = await load_swap(call, db, member)
    if not swap:
        return
    await db.set_swap_status(swap["id"], "declined")
    subject = await db.get_subject(swap["subject_id"])
    requester = await db.get_member(swap["from_member"])
    await edit(call, "Вы отказались от обмена.")
    if requester:
        await notify(bot, requester["tg_id"],
                     f"❌ {escape(member['full_name'])} отказался меняться местами ({escape(subject['name'])}).")
    await call.answer()


# ---------- опрос ----------

@router.callback_query(F.data.startswith("pa:"))
async def cb_poll_answer(call: CallbackQuery, bot: Bot, db: Database, qs: QueueService, member: dict | None):
    if not await guard(call, member):
        return
    _, lesson_id, answer = call.data.split(":")
    lesson = await db.get_lesson(int(lesson_id))
    subject = await db.get_subject(lesson["subject_id"]) if lesson else None
    if not subject or subject["group_id"] != member["group_id"] or lesson["finished"]:
        await call.answer("Эта пара уже прошла или удалена", show_alert=True)
        await call.message.edit_reply_markup(reply_markup=None)
        return
    sg = member_scope(subject, member)
    if lesson["subgroup"] != sg:
        await call.answer("Этот опрос не для вашей подгруппы", show_alert=True)
        return

    await db.set_answer(lesson["id"], member["id"], answer == "1")
    if answer == "1":
        pos, _ = await qs.add(subject["id"], sg, member["id"])
        result = f"✅ Ваш ответ: буду сдавать. Ваше место в очереди: <b>{pos}</b>"
    else:
        old_first = await first_active_id(qs, subject["id"], sg)
        await qs.remove(subject["id"], sg, member["id"])
        await notify_if_first_changed(bot, db, qs, subject, sg, old_first)
        result = "❌ Ваш ответ: не буду сдавать. Вы не в очереди."
    await edit(call, f"{poll_text(lesson, subject)}\n\n{result}\n<i>Ответ можно изменить.</i>",
               poll_markup(lesson["id"]))
    await call.answer("Ответ сохранён")
