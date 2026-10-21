"""Подключается последним: ловит всё, что не обработали другие роутеры."""
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..db import Database
from .common import try_login

router = Router(name="fallback")

CODE_RE = re.compile(r"^[A-Za-z0-9]{6}$")


@router.message(F.text)
async def any_text(message: Message, state: FSMContext, db: Database, member: dict | None):
    if member:
        await message.answer("Не понимаю 🤔 Откройте меню: /menu")
    elif CODE_RE.match(message.text.strip()):
        await try_login(message, state, db, message.text)
    else:
        await message.answer("Чтобы начать, отправьте /start")


@router.callback_query()
async def any_callback(call: CallbackQuery):
    await call.answer("Эта кнопка устарела. Откройте /menu", show_alert=True)
