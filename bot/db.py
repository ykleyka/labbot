import aiosqlite

from .utils import gen_code, now_utc, to_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    subgroups_count INTEGER NOT NULL DEFAULT 1,
    sheet_url TEXT
);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    full_name TEXT NOT NULL,
    subgroup INTEGER NOT NULL DEFAULT 1,
    code TEXT NOT NULL UNIQUE,
    tg_id INTEGER UNIQUE,
    is_headman INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    is_subgroup INTEGER NOT NULL DEFAULT 0
);
-- subgroup = 0 для общей пары, иначе номер подгруппы
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    subgroup INTEGER NOT NULL DEFAULT 0,
    starts_at TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    started_notified INTEGER NOT NULL DEFAULT 0,
    finished INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS poll_answers (
    lesson_id INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    answer INTEGER NOT NULL,
    PRIMARY KEY (lesson_id, member_id)
);
-- Очередь живёт между парами: (subject_id, subgroup) — одна очередь
CREATE TABLE IF NOT EXISTS queue_entries (
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    subgroup INTEGER NOT NULL DEFAULT 0,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    excused INTEGER NOT NULL DEFAULT 0,
    excuse_reason TEXT,
    PRIMARY KEY (subject_id, member_id)
);
CREATE TABLE IF NOT EXISTS swap_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    from_member INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    to_member INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def fetchone(self, sql: str, *args) -> dict | None:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, *args) -> list[dict]:
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def execute(self, sql: str, *args) -> int:
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur.lastrowid

    # ---------- группы ----------

    async def create_group(self, name: str, subgroups_count: int) -> int:
        return await self.execute(
            "INSERT INTO groups (name, subgroups_count) VALUES (?, ?)", name, subgroups_count
        )

    async def get_group(self, group_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM groups WHERE id = ?", group_id)

    async def set_sheet_url(self, group_id: int, url: str) -> None:
        await self.execute("UPDATE groups SET sheet_url = ? WHERE id = ?", url, group_id)

    # ---------- участники ----------

    async def _new_code(self) -> str:
        while True:
            code = gen_code()
            if not await self.fetchone("SELECT 1 FROM members WHERE code = ?", code):
                return code

    async def add_member(
        self, group_id: int, full_name: str, subgroup: int, tg_id: int | None = None, is_headman: bool = False
    ) -> dict:
        code = await self._new_code()
        member_id = await self.execute(
            "INSERT INTO members (group_id, full_name, subgroup, code, tg_id, is_headman) VALUES (?, ?, ?, ?, ?, ?)",
            group_id, full_name, subgroup, code, tg_id, int(is_headman),
        )
        return await self.get_member(member_id)

    async def get_member(self, member_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM members WHERE id = ?", member_id)

    async def get_member_by_tg(self, tg_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM members WHERE tg_id = ?", tg_id)

    async def get_member_by_code(self, code: str) -> dict | None:
        return await self.fetchone("SELECT * FROM members WHERE code = ?", code)

    async def link_member(self, member_id: int, tg_id: int) -> None:
        await self.execute("UPDATE members SET tg_id = ? WHERE id = ?", tg_id, member_id)

    async def unlink_member(self, member_id: int) -> None:
        await self.execute("UPDATE members SET tg_id = NULL WHERE id = ?", member_id)

    async def regenerate_code(self, member_id: int) -> str:
        code = await self._new_code()
        await self.execute("UPDATE members SET code = ?, tg_id = NULL WHERE id = ?", code, member_id)
        return code

    async def delete_member(self, member_id: int) -> None:
        await self.execute("DELETE FROM members WHERE id = ?", member_id)

    async def list_members(self, group_id: int) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM members WHERE group_id = ? ORDER BY subgroup, full_name", group_id
        )

    async def scope_members(self, group_id: int, subgroup: int) -> list[dict]:
        """Участники, которых касается очередь: вся группа (subgroup=0) или подгруппа."""
        if subgroup == 0:
            return await self.list_members(group_id)
        return await self.fetchall(
            "SELECT * FROM members WHERE group_id = ? AND subgroup = ? ORDER BY full_name", group_id, subgroup
        )

    async def headmen(self, group_id: int) -> list[dict]:
        return await self.fetchall("SELECT * FROM members WHERE group_id = ? AND is_headman = 1", group_id)

    # ---------- предметы ----------

    async def add_subject(self, group_id: int, name: str, is_subgroup: bool) -> int:
        return await self.execute(
            "INSERT INTO subjects (group_id, name, is_subgroup) VALUES (?, ?, ?)", group_id, name, int(is_subgroup)
        )

    async def get_subject(self, subject_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM subjects WHERE id = ?", subject_id)

    async def list_subjects(self, group_id: int) -> list[dict]:
        return await self.fetchall("SELECT * FROM subjects WHERE group_id = ? ORDER BY name", group_id)

    async def delete_subject(self, subject_id: int) -> None:
        await self.execute("DELETE FROM subjects WHERE id = ?", subject_id)

    # ---------- пары ----------

    async def add_lesson(self, subject_id: int, subgroup: int, starts_at, duration_min: int) -> int:
        return await self.execute(
            "INSERT INTO lessons (subject_id, subgroup, starts_at, duration_min) VALUES (?, ?, ?, ?)",
            subject_id, subgroup, to_iso(starts_at), duration_min,
        )

    async def get_lesson(self, lesson_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM lessons WHERE id = ?", lesson_id)

    async def delete_lesson(self, lesson_id: int) -> None:
        await self.execute("DELETE FROM lessons WHERE id = ?", lesson_id)

    async def group_lessons(self, group_id: int) -> list[dict]:
        """Незавершённые пары группы, по времени."""
        return await self.fetchall(
            """SELECT l.*, s.name AS subject_name, s.is_subgroup FROM lessons l
               JOIN subjects s ON s.id = l.subject_id
               WHERE s.group_id = ? AND l.finished = 0 ORDER BY l.starts_at""",
            group_id,
        )

    async def scope_lessons(self, subject_id: int, subgroup: int) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM lessons WHERE subject_id = ? AND subgroup = ? AND finished = 0 ORDER BY starts_at",
            subject_id, subgroup,
        )

    async def unfinished_lessons(self) -> list[dict]:
        return await self.fetchall("SELECT * FROM lessons WHERE finished = 0 ORDER BY starts_at")

    async def mark_lesson_started(self, lesson_id: int) -> None:
        await self.execute("UPDATE lessons SET started_notified = 1 WHERE id = ?", lesson_id)

    async def mark_lesson_finished(self, lesson_id: int) -> None:
        await self.execute("UPDATE lessons SET finished = 1 WHERE id = ?", lesson_id)

    # ---------- опросы ----------

    async def set_answer(self, lesson_id: int, member_id: int, answer: bool) -> None:
        await self.execute(
            """INSERT INTO poll_answers (lesson_id, member_id, answer) VALUES (?, ?, ?)
               ON CONFLICT(lesson_id, member_id) DO UPDATE SET answer = excluded.answer""",
            lesson_id, member_id, int(answer),
        )

    async def poll_answers(self, lesson_id: int) -> dict[int, int]:
        rows = await self.fetchall("SELECT member_id, answer FROM poll_answers WHERE lesson_id = ?", lesson_id)
        return {r["member_id"]: r["answer"] for r in rows}

    # ---------- обмен местами ----------

    async def create_swap(self, subject_id: int, from_member: int, to_member: int) -> int:
        # Старые запросы этой пары по этому предмету больше не нужны
        await self.execute(
            """UPDATE swap_requests SET status = 'cancelled'
               WHERE subject_id = ? AND from_member = ? AND to_member = ? AND status = 'pending'""",
            subject_id, from_member, to_member,
        )
        return await self.execute(
            "INSERT INTO swap_requests (subject_id, from_member, to_member, created_at) VALUES (?, ?, ?, ?)",
            subject_id, from_member, to_member, to_iso(now_utc()),
        )

    async def get_swap(self, swap_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM swap_requests WHERE id = ?", swap_id)

    async def set_swap_status(self, swap_id: int, status: str) -> None:
        await self.execute("UPDATE swap_requests SET status = ? WHERE id = ?", status, swap_id)
