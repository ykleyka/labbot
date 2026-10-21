"""Сквозной тест: прогоняет реальные хендлеры через Dispatcher с поддельным Telegram API.
Запуск: python -m tests.test_flow"""
import asyncio
import itertools
import os
import tempfile
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.db import Database
from bot.handlers import admin, common, fallback, member
from bot.middlewares import MemberMiddleware
from bot.queue_service import QueueService
from bot.scheduler import tick
from bot.sheets import parse_sheet_rows
from bot.utils import parse_lesson_input, parse_members


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list = []

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="Bot", username="test_bot")
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(message_id=1, date=datetime.now(), chat=Chat(id=method.chat_id or 1, type="private"),
                           text=method.text)
        return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError


ids = itertools.count(1)


def user(uid):
    return User(id=uid, is_bot=False, first_name=f"u{uid}")


async def text(dp, bot, uid, value):
    msg = Message(message_id=next(ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
                  from_user=user(uid), text=value)
    await dp.feed_update(bot, Update(update_id=next(ids), message=msg))


async def click(dp, bot, uid, data):
    msg = Message(message_id=next(ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
                  from_user=user(1), text="menu")
    cq = CallbackQuery(id=str(next(ids)), from_user=user(uid), chat_instance="x", data=data, message=msg)
    await dp.feed_update(bot, Update(update_id=next(ids), callback_query=cq))


def messages_to(session, uid):
    return [m.text for m in session.sent if isinstance(m, SendMessage) and m.chat_id == uid]


async def main():
    # --- чистые функции ---
    assert parse_members("1. Иванов Иван; 2\nПетров  Пётр 1\n\nБез группы", 2) == (
        [("Иванов Иван", 2), ("Петров Пётр", 1)], ["Без группы"])
    assert parse_members("Иванов Иван", 1) == ([("Иванов Иван", 1)], [])
    dt, dur = parse_lesson_input("24.10.2030 10:40 95")
    assert (dt.day, dt.hour, dt.minute, dur) == (24, 10, 40, 95)
    assert parse_lesson_input("завтра") is None
    assert parse_sheet_rows([["№", "ФИО", "Подгруппа", "Уважительная причина"], ["1", "Иванов", "1", ""],
                             ["", "", "", ""], ["2", "Петров", "1", "болеет"]]) == [("Иванов", ""), ("Петров", "болеет")]

    tmp = tempfile.mkdtemp()
    db = Database(os.path.join(tmp, "test.db"))
    await db.connect()
    qs = QueueService(db)
    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"], dp["qs"] = db, qs

    class FakeSheets:
        available = True
        rows = {}

        def service_email(self):
            return "bot@project.iam.gserviceaccount.com"

        def export(self, url, tables):
            self.rows = tables

        def read_all(self, url):
            return self.rows

    dp["sheets"] = sheets = FakeSheets()
    dp.message.outer_middleware(MemberMiddleware())
    dp.callback_query.outer_middleware(MemberMiddleware())
    dp.include_routers(common.router, member.router, admin.router, fallback.router)

    HEAD, IVAN, PETR, ANNA = 100, 101, 102, 103

    # --- создание группы ---
    await text(dp, bot, HEAD, "/creategroup")
    await text(dp, bot, HEAD, "ИВТ-21")
    await text(dp, bot, HEAD, "2")
    await text(dp, bot, HEAD, "Старостин Сергей; 1")
    await text(dp, bot, HEAD, "Иванов Иван; 1\nПетров Пётр; 1\nСидорова Анна; 2")
    members = {m["full_name"]: m for m in await db.list_members(1)}
    assert len(members) == 4 and members["Старостин Сергей"]["is_headman"] == 1
    head_id, ivan_id, petr_id, anna_id = (members[n]["id"] for n in
                                          ["Старостин Сергей", "Иванов Иван", "Петров Пётр", "Сидорова Анна"])

    # --- вход по коду: обычный, по ссылке, неверный код, чужой код ---
    await text(dp, bot, IVAN, "/start")
    await text(dp, bot, IVAN, members["Иванов Иван"]["code"])
    await text(dp, bot, PETR, f"/start {members['Петров Пётр']['code']}")
    await text(dp, bot, ANNA, "/start")
    await text(dp, bot, ANNA, "XXXXXX")
    assert "не найден" in messages_to(session, ANNA)[-1]
    await text(dp, bot, ANNA, members["Иванов Иван"]["code"])
    assert "уже привязан" in messages_to(session, ANNA)[-1]
    await text(dp, bot, ANNA, members["Сидорова Анна"]["code"].lower())
    assert (await db.get_member_by_tg(IVAN))["id"] == ivan_id
    assert (await db.get_member_by_tg(PETR))["id"] == petr_id
    assert (await db.get_member_by_tg(ANNA))["id"] == anna_id

    # --- предметы: общий и по подгруппам ---
    await click(dp, bot, HEAD, "asa")
    await text(dp, bot, HEAD, "Матан")
    await click(dp, bot, HEAD, "ast:0")
    await click(dp, bot, HEAD, "asa")
    await text(dp, bot, HEAD, "Физика")
    await click(dp, bot, HEAD, "ast:1")
    subjects = {s["name"]: s for s in await db.list_subjects(1)}
    matan, phys = subjects["Матан"]["id"], subjects["Физика"]["id"]
    assert subjects["Физика"]["is_subgroup"] == 1
    # не-староста не может управлять
    await click(dp, bot, IVAN, "asa")
    assert (await dp.storage.get_state(key=_key(bot, IVAN))) is None

    # --- запись в очередь ---
    for uid in (IVAN, PETR, HEAD):
        await click(dp, bot, uid, f"j:{matan}")
    await click(dp, bot, IVAN, f"j:{matan}")  # повторно — без дублей
    assert await qs.ids(matan, 0) == [ivan_id, petr_id, head_id]

    # --- обмен местами ---
    await click(dp, bot, HEAD, f"sr:{matan}:{ivan_id}")
    swap_msg = messages_to(session, IVAN)[-1]
    assert "предлагает поменяться" in swap_msg
    await click(dp, bot, PETR, "swa:1")  # чужой запрос принять нельзя
    assert await qs.ids(matan, 0) == [ivan_id, petr_id, head_id]
    await click(dp, bot, IVAN, "swa:1")
    assert await qs.ids(matan, 0) == [head_id, petr_id, ivan_id]
    await click(dp, bot, IVAN, "swa:1")  # повторно — уже неактуален
    assert await qs.ids(matan, 0) == [head_id, petr_id, ivan_id]

    # --- отмена вне пары: удаление из очереди ---
    await click(dp, bot, IVAN, f"cx:{matan}:0")
    assert await qs.ids(matan, 0) == [head_id, petr_id]
    await click(dp, bot, IVAN, f"j:{matan}")
    assert await qs.ids(matan, 0) == [head_id, petr_id, ivan_id]

    # --- идёт пара ---
    now = datetime.now(timezone.utc)
    await db.add_lesson(matan, 0, now - timedelta(minutes=10), 90)
    await tick(bot, db, qs)
    assert any("Пара началась" in t and "Вы первый" in t for t in messages_to(session, HEAD))
    # Пётр отменяет без причины -> в конец
    await click(dp, bot, PETR, f"cx:{matan}:0")
    assert await qs.ids(matan, 0) == [head_id, ivan_id, petr_id]
    # Староста отсутствует по уважительной причине -> место сохраняется, Иван получает «ваша очередь»
    await click(dp, bot, HEAD, f"cx:{matan}:1")
    await text(dp, bot, HEAD, "заболел")
    assert await qs.ids(matan, 0) == [head_id, ivan_id, petr_id]
    assert (await qs.entries(matan, 0))[0]["excused"] == 1
    assert "Подошла ваша очередь" in messages_to(session, IVAN)[-1]
    # Иван сдал -> следующий Пётр
    await click(dp, bot, IVAN, f"dny:{matan}")
    assert await qs.ids(matan, 0) == [head_id, petr_id]
    assert "Подошла ваша очередь" in messages_to(session, PETR)[-1]

    # --- пара закончилась: уважительная причина сброшена, место сохранено ---
    await db.execute("UPDATE lessons SET starts_at = ? WHERE subject_id = ?",
                     (now - timedelta(hours=3)).replace(microsecond=0).isoformat(), matan)
    await tick(bot, db, qs)
    entries = await qs.entries(matan, 0)
    assert [e["member_id"] for e in entries] == [head_id, petr_id] and entries[0]["excused"] == 0

    # --- пара по подгруппе + опрос ---
    await click(dp, bot, HEAD, "ala")
    await click(dp, bot, HEAD, f"alas:{phys}")
    await click(dp, bot, HEAD, f"alag:{phys}:2")
    await text(dp, bot, HEAD, "01.01.2020 10:00")  # прошедшая дата отклоняется
    assert "уже прошло" in messages_to(session, HEAD)[-1]
    await text(dp, bot, HEAD, "24.10.2030 10:40")
    lesson = (await db.scope_lessons(phys, 2))[0]
    await click(dp, bot, HEAD, f"alp:{lesson['id']}")
    assert any("Опрос" in t for t in messages_to(session, ANNA))
    assert not any("Опрос" in t for t in messages_to(session, IVAN))  # другая подгруппа
    await click(dp, bot, IVAN, f"pa:{lesson['id']}:1")  # чужая подгруппа
    assert await qs.ids(phys, 1) == [] and await qs.ids(phys, 2) == []
    await click(dp, bot, ANNA, f"pa:{lesson['id']}:1")
    assert await qs.ids(phys, 2) == [anna_id]
    await click(dp, bot, ANNA, f"pa:{lesson['id']}:0")
    assert await qs.ids(phys, 2) == []
    assert await db.poll_answers(lesson["id"]) == {anna_id: 0}

    # --- редактирование очереди старостой ---
    await click(dp, bot, HEAD, f"eaa:{matan}:0:{ivan_id}")
    await click(dp, bot, HEAD, f"eaa:{matan}:0:{anna_id}")
    assert await qs.ids(matan, 0) == [head_id, petr_id, ivan_id, anna_id]
    await click(dp, bot, HEAD, f"em:{matan}:{anna_id}:top")
    assert await qs.ids(matan, 0) == [anna_id, head_id, petr_id, ivan_id]
    await click(dp, bot, HEAD, f"em:{matan}:{anna_id}:down")
    assert await qs.ids(matan, 0) == [head_id, anna_id, petr_id, ivan_id]
    await click(dp, bot, HEAD, f"em:{matan}:{head_id}:pos")
    await text(dp, bot, HEAD, "3")
    assert await qs.ids(matan, 0) == [anna_id, petr_id, head_id, ivan_id]
    await click(dp, bot, HEAD, f"em:{matan}:{petr_id}:done")
    assert await qs.ids(matan, 0) == [anna_id, head_id, ivan_id]
    await click(dp, bot, IVAN, f"em:{matan}:{anna_id}:end")  # не староста
    assert await qs.ids(matan, 0) == [anna_id, head_id, ivan_id]

    # --- Google Таблицы: экспорт -> правка -> импорт ---
    await db.set_sheet_url(1, "https://docs.google.com/spreadsheets/d/abc")
    await click(dp, bot, HEAD, "age")
    assert [r[1] for r in sheets.rows["Матан"][1:]] == ["Сидорова Анна", "Старостин Сергей", "Иванов Иван"]
    assert set(sheets.rows) == {"Матан", "Физика (подгруппа 1)", "Физика (подгруппа 2)"}
    sheets.rows["Матан"] = [["№", "ФИО"], ["1", "иванов  иван"], ["2", "Кто-то Левый"], ["3", "Петров Пётр", "", "болеет"]]
    await click(dp, bot, HEAD, "agiy")
    entries = await qs.entries(matan, 0)
    assert [e["member_id"] for e in entries] == [ivan_id, petr_id]
    assert entries[1]["excused"] == 1 and entries[1]["excuse_reason"] == "болеет"
    assert "Кто-то Левый" in messages_to(session, HEAD)[-1]

    # --- участники: новый код и удаление ---
    await click(dp, bot, HEAD, f"amcn:{petr_id}")
    assert await db.get_member_by_tg(PETR) is None
    await click(dp, bot, HEAD, f"amry:{ivan_id}")
    assert await db.get_member(ivan_id) is None and await qs.ids(matan, 0) == [petr_id]

    # ни одна кнопка не должна была уйти в fallback
    from aiogram.methods import AnswerCallbackQuery
    stale = [m for m in session.sent if isinstance(m, AnswerCallbackQuery) and "устарела" in (m.text or "")]
    assert not stale, stale

    await db.close()
    print("Все проверки пройдены ✅")


def _key(bot, uid):
    from aiogram.fsm.storage.base import StorageKey
    return StorageKey(bot_id=bot.id, chat_id=uid, user_id=uid)


if __name__ == "__main__":
    asyncio.run(main())
