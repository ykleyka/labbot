import asyncio

from .db import Database


class QueueService:
    """Операции с очередью. Очередь задаётся парой (subject_id, subgroup),
    subgroup = 0 для общего предмета. Все изменения идут под одним замком,
    чтобы одновременные нажатия не перемешали позиции."""

    def __init__(self, db: Database):
        self.db = db
        self.lock = asyncio.Lock()

    async def entries(self, subject_id: int, subgroup: int) -> list[dict]:
        return await self.db.fetchall(
            """SELECT q.member_id, q.excused, q.excuse_reason, m.full_name, m.tg_id, m.subgroup
               FROM queue_entries q JOIN members m ON m.id = q.member_id
               WHERE q.subject_id = ? AND q.subgroup = ? ORDER BY q.position, q.rowid""",
            subject_id, subgroup,
        )

    async def ids(self, subject_id: int, subgroup: int) -> list[int]:
        return [e["member_id"] for e in await self.entries(subject_id, subgroup)]

    async def position(self, subject_id: int, subgroup: int, member_id: int) -> int | None:
        """Место в очереди, начиная с 1, или None."""
        ids = await self.ids(subject_id, subgroup)
        return ids.index(member_id) + 1 if member_id in ids else None

    async def first_active(self, subject_id: int, subgroup: int) -> dict | None:
        """Первый в очереди, кто не отсутствует по уважительной причине."""
        for e in await self.entries(subject_id, subgroup):
            if not e["excused"]:
                return e
        return None

    async def _write(self, subject_id: int, ids: list[int]) -> None:
        for pos, member_id in enumerate(ids, 1):
            await self.db.conn.execute(
                "UPDATE queue_entries SET position = ? WHERE subject_id = ? AND member_id = ?",
                (pos, subject_id, member_id),
            )
        await self.db.conn.commit()

    async def add(self, subject_id: int, subgroup: int, member_id: int) -> tuple[int, bool]:
        """В конец очереди. Возвращает (место, добавлен ли сейчас)."""
        async with self.lock:
            ids = await self.ids(subject_id, subgroup)
            if member_id in ids:
                return ids.index(member_id) + 1, False
            # на случай, если участник сменил подгруппу и остался в старой очереди
            await self.db.conn.execute(
                "DELETE FROM queue_entries WHERE subject_id = ? AND member_id = ?", (subject_id, member_id)
            )
            await self.db.conn.execute(
                "INSERT INTO queue_entries (subject_id, subgroup, member_id, position) VALUES (?, ?, ?, ?)",
                (subject_id, subgroup, member_id, len(ids) + 1),
            )
            await self._write(subject_id, ids + [member_id])
            return len(ids) + 1, True

    async def remove(self, subject_id: int, subgroup: int, member_id: int) -> bool:
        async with self.lock:
            ids = await self.ids(subject_id, subgroup)
            if member_id not in ids:
                return False
            await self.db.conn.execute(
                "DELETE FROM queue_entries WHERE subject_id = ? AND member_id = ?", (subject_id, member_id)
            )
            ids.remove(member_id)
            await self._write(subject_id, ids)
            return True

    async def move(self, subject_id: int, subgroup: int, member_id: int, index: int) -> int | None:
        """Ставит участника на место index (с 0; выход за границы обрезается). Возвращает новое место с 1."""
        async with self.lock:
            ids = await self.ids(subject_id, subgroup)
            if member_id not in ids:
                return None
            ids.remove(member_id)
            index = max(0, min(index, len(ids)))
            ids.insert(index, member_id)
            await self._write(subject_id, ids)
            return index + 1

    async def move_by(self, subject_id: int, subgroup: int, member_id: int, delta: int) -> int | None:
        pos = await self.position(subject_id, subgroup, member_id)
        if pos is None:
            return None
        return await self.move(subject_id, subgroup, member_id, pos - 1 + delta)

    async def move_to_end(self, subject_id: int, subgroup: int, member_id: int) -> int | None:
        return await self.move(subject_id, subgroup, member_id, 10**9)

    async def swap(self, subject_id: int, subgroup: int, a: int, b: int) -> bool:
        async with self.lock:
            ids = await self.ids(subject_id, subgroup)
            if a not in ids or b not in ids:
                return False
            i, j = ids.index(a), ids.index(b)
            ids[i], ids[j] = ids[j], ids[i]
            await self._write(subject_id, ids)
            return True

    async def set_excused(self, subject_id: int, member_id: int, excused: bool, reason: str | None = None) -> None:
        await self.db.execute(
            "UPDATE queue_entries SET excused = ?, excuse_reason = ? WHERE subject_id = ? AND member_id = ?",
            int(excused), reason if excused else None, subject_id, member_id,
        )

    async def clear_excused(self, subject_id: int, subgroup: int) -> None:
        await self.db.execute(
            "UPDATE queue_entries SET excused = 0, excuse_reason = NULL WHERE subject_id = ? AND subgroup = ?",
            subject_id, subgroup,
        )

    async def clear(self, subject_id: int, subgroup: int) -> None:
        async with self.lock:
            await self.db.execute(
                "DELETE FROM queue_entries WHERE subject_id = ? AND subgroup = ?", subject_id, subgroup
            )

    async def replace(self, subject_id: int, subgroup: int, items: list[tuple[int, str | None]]) -> None:
        """Полностью заменяет очередь: items = [(member_id, причина или None)]."""
        async with self.lock:
            conn = self.db.conn
            await conn.execute(
                "DELETE FROM queue_entries WHERE subject_id = ? AND subgroup = ?", (subject_id, subgroup)
            )
            for pos, (member_id, reason) in enumerate(items, 1):
                await conn.execute(
                    "DELETE FROM queue_entries WHERE subject_id = ? AND member_id = ?", (subject_id, member_id)
                )
                await conn.execute(
                    """INSERT INTO queue_entries (subject_id, subgroup, member_id, position, excused, excuse_reason)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (subject_id, subgroup, member_id, pos, int(bool(reason)), reason or None),
                )
            await conn.commit()
