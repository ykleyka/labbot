"""Вход по коду, главное меню, помощь."""
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..db import Database
from ..services import NOT_AUTH, guard
from ..utils import edit, kb

router = Router(name="common")

NOT_COMMAND = F.text & ~F.text.startswith("/")


class Auth(StatesGroup):
    code = State()


HELP_TEXT = (
    "ℹ️ <b>Как пользоваться ботом</b>\n\n"
    "<b>Участникам</b>\n"
    "• 📋 <b>Очереди</b> — посмотреть очередь по предмету, записаться («Буду сдавать»), "
    "отменить сдачу, отметить «Я сдал» или предложить кому-то поменяться местами.\n"
    "• Если отменить сдачу <i>во время пары</i>, вы переместитесь в конец очереди. "
    "Если вы отсутствуете по уважительной причине, укажите её: место сохранится.\n"
    "• Перед парой староста может прислать опрос: «Да» ставит вас в конец очереди, "
    "«Нет» убирает из неё.\n\n"
    "<b>Старосте</b>\n"
    "• /creategroup — создать группу и получить коды участников.\n"
    "• 👑 Управление: предметы, пары и опросы, редактирование очереди, участники, Google Таблицы.\n\n"
    "Команды: /menu, /queue, /lessons, /logout, /cancel"
)


async def main_menu(db: Database, member: dict):
    group = await db.get_group(member["group_id"])
    subgroup = f" · подгруппа {member['subgroup']}" if group["subgroups_count"] > 1 else ""
    role = "\n👑 Вы староста" if member["is_headman"] else ""
    text = (
        f"👤 <b>{escape(member['full_name'])}</b>\n"
        f"Группа: {escape(group['name'])}{subgroup}{role}\n\nВыберите действие:"
    )
    rows = [[("📋 Очереди", "m:queues")], [("📅 Ближайшие пары", "m:lessons")]]
    if member["is_headman"]:
        rows.append([("👑 Управление группой", "adm")])
    rows.append([("ℹ️ Помощь", "m:help")])
    return text, kb(*rows)


async def try_login(message: Message, state: FSMContext, db: Database, raw_code: str) -> None:
    code = raw_code.strip().upper()
    found = await db.get_member_by_code(code)
    if not found:
        await message.answer("❌ Код не найден. Проверьте его и отправьте ещё раз (или /cancel).")
        return
    if found["tg_id"] and found["tg_id"] != message.from_user.id:
        await message.answer("⚠️ Этот код уже привязан к другому аккаунту. Попросите старосту выдать новый код.")
        return
    await db.link_member(found["id"], message.from_user.id)
    await state.clear()
    member = await db.get_member(found["id"])
    await message.answer(f"✅ Вы вошли как <b>{escape(member['full_name'])}</b>.")
    text, markup = await main_menu(db, member)
    await message.answer(text, reply_markup=markup)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, db: Database, member: dict | None):
    await state.clear()
    await message.answer("Действие отменено.")
    if member:
        text, markup = await main_menu(db, member)
        await message.answer(text, reply_markup=markup)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, db: Database, member: dict | None):
    await state.clear()
    if member:
        text, markup = await main_menu(db, member)
        await message.answer(text, reply_markup=markup)
        return
    if command.args:  # вход по ссылке t.me/<бот>?start=КОД
        await try_login(message, state, db, command.args)
        return
    await state.set_state(Auth.code)
    await message.answer(
        "👋 Привет! Это бот для очереди на сдачу лабораторных.\n\n"
        "🔑 Отправьте <b>код участника</b>, который выдал староста.\n"
        "👑 Если вы староста и группы ещё нет, отправьте /creategroup."
    )


@router.message(Auth.code, NOT_COMMAND)
async def auth_code(message: Message, state: FSMContext, db: Database):
    await try_login(message, state, db, message.text)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, db: Database, member: dict | None):
    await state.clear()
    if not member:
        await message.answer(NOT_AUTH)
        return
    text, markup = await main_menu(db, member)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "m:main")
async def cb_main(call: CallbackQuery, state: FSMContext, db: Database, member: dict | None):
    await state.clear()
    if not await guard(call, member):
        return
    text, markup = await main_menu(db, member)
    await edit(call, text, markup)
    await call.answer()


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


@router.callback_query(F.data == "m:help")
async def cb_help(call: CallbackQuery):
    await edit(call, HELP_TEXT, kb([("⬅️ Главное меню", "m:main")]))
    await call.answer()


@router.message(Command("logout"))
async def cmd_logout(message: Message, state: FSMContext, db: Database, member: dict | None):
    await state.clear()
    if not member:
        await message.answer("Вы и так не вошли в бота.")
        return
    await db.unlink_member(member["id"])
    await message.answer(
        f"Вы вышли из бота. Чтобы войти снова, отправьте /start и код <code>{member['code']}</code>."
    )
