import aiosqlite

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    engine  TEXT NOT NULL DEFAULT 'auto'
);
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    chat_id         INTEGER NOT NULL,
    title           TEXT NOT NULL,
    engine          TEXT NOT NULL DEFAULT 'auto',
    status          TEXT NOT NULL DEFAULT 'pending',
    progress_msg_id INTEGER,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS chapters (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx    INTEGER NOT NULL,
    title  TEXT NOT NULL,
    text   TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS ix_chapters_job ON chapters(job_id, idx);

-- NEW (additive): per-user voice style text
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id INTEGER PRIMARY KEY,
    style   TEXT NOT NULL DEFAULT ''
);
"""


async def _run(sql, params=(), fetch=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        if fetch == "one":
            row = await cur.fetchone()
            return dict(row) if row else None
        if fetch == "all":
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        await db.commit()
        return cur.lastrowid


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


# ---------- users ----------
async def get_engine(user_id: int) -> str:
    row = await _run("SELECT engine FROM users WHERE user_id=?", (user_id,), "one")
    return row["engine"] if row else "auto"


async def set_engine(user_id: int, engine: str):
    await _run(
        "INSERT INTO users (user_id, engine) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET engine=excluded.engine",
        (user_id, engine),
    )


# ---------- voice style (NEW) ----------
async def get_style(user_id: int) -> str:
    row = await _run("SELECT style FROM user_prefs WHERE user_id=?", (user_id,), "one")
    return row["style"] if row else ""


async def set_style(user_id: int, style: str):
    await _run(
        "INSERT INTO user_prefs (user_id, style) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET style=excluded.style",
        (user_id, style),
    )


# ---------- jobs ----------
async def create_job(user_id, chat_id, title, engine, chapters) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        cur = await db.execute(
            "INSERT INTO jobs (user_id, chat_id, title, engine) VALUES (?, ?, ?, ?)",
            (user_id, chat_id, title, engine),
        )
        job_id = cur.lastrowid
        await db.executemany(
            "INSERT INTO chapters (job_id, idx, title, text) VALUES (?, ?, ?, ?)",
            [(job_id, i, c.title, c.text) for i, c in enumerate(chapters)],
        )
        await db.commit()
        return job_id


async def get_job(job_id: int):
    return await _run("SELECT * FROM jobs WHERE id=?", (job_id,), "one")


async def set_job_status(job_id: int, status: str):
    await _run("UPDATE jobs SET status=? WHERE id=?", (status, job_id))


async def set_progress_msg(job_id: int, message_id: int):
    await _run("UPDATE jobs SET progress_msg_id=? WHERE id=?", (message_id, job_id))


async def active_job_for_user(user_id: int):
    return await _run(
        "SELECT * FROM jobs WHERE user_id=? AND status IN ('queued','running') "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
        "one",
    )


async def latest_failed_job(user_id: int):
    return await _run(
        "SELECT * FROM jobs WHERE user_id=? AND status='failed' ORDER BY id DESC LIMIT 1",
        (user_id,),
        "one",
    )


async def requeue_running():
    await _run("UPDATE jobs SET status='queued' WHERE status='running'")


async def queued_job_ids():
    rows = await _run("SELECT id FROM jobs WHERE status='queued' ORDER BY id", (), "all")
    return [r["id"] for r in rows]


# ---------- chapters ----------
async def pending_chapters(job_id: int):
    return await _run(
        "SELECT * FROM chapters WHERE job_id=? AND status!='done' ORDER BY idx",
        (job_id,),
        "all",
    )


async def mark_chapter_done(chapter_id: int):
    await _run("UPDATE chapters SET status='done' WHERE id=?", (chapter_id,))


async def progress(job_id: int):
    row = await _run(
        "SELECT SUM(status='done') AS done, COUNT(*) AS total FROM chapters WHERE job_id=?",
        (job_id,),
        "one",
    )
    return int(row["done"] or 0), int(row["total"] or 0)