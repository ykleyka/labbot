"""Фоновая задача: начало и конец пар."""
import asyncio
import logging
from html import escape

from aiogram import Bot

from .db import Database
from .queue_service import QueueService
from .services import notify, render_queue
from .utils import lesson_bounds, now_utc

log = logging.getLogger(__name__)


async def tick(bot: Bot, db: Database, qs: QueueService) -> None:
    now = now_utc()
    for lesson in await db.unfinished_lessons():
        start, end = lesson_bounds(lesson)
        subject = await db.get_subject(lesson["subject_id"])
        if subject is None:
            continue
        sid, sg = subject["id"], lesson["subgroup"]

        if now >= end:
            # Пара закончилась: уважительные причины действуют только на одну пару,
            # а кто не успел сдать, остаётся в очереди на своём месте
            await qs.clear_excused(sid, sg)
            await db.mark_lesson_finished(lesson["id"])
            continue

        if now >= start and not lesson["started_notified"]:
            await db.mark_lesson_started(lesson["id"])
            entries = await qs.entries(sid, sg)
            text = "<b>Пара началась!</b>\n\n" + await render_queue(db, qs, subject, sg)
            first = next((e for e in entries if not e["excused"]), None)
            for m in await db.scope_members(subject["group_id"], sg):
                extra = ""
                if first and m["id"] == first["member_id"]:
                    extra = f"\n\n<b>Вы первый — ваша очередь сдавать {escape(subject['name'])}!</b>"
                await notify(bot, m["tg_id"], text + extra)


async def lessons_watcher(bot: Bot, db: Database, qs: QueueService, interval: int = 30) -> None:
    while True:
        try:
            await tick(bot, db, qs)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в планировщике пар")
        await asyncio.sleep(interval)
