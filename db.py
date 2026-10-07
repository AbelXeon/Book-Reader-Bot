import asyncio
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

import config  # noqa: F401  (loads .env)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    engine  TEXT NOT NULL DEFAULT 'auto'
);
CREATE TABLE IF NOT EXISTS jobs (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    chat_id         BIGINT NOT NULL,
    title           TEXT NOT NULL,
    engine          TEXT NOT NULL DEFAULT 'auto',
    status          TEXT NOT NULL DEFAULT 'pending',
    progress_msg_id BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chapters (
    id     BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx    INTEGER NOT NULL,
    title  TEXT NOT NULL,
    text   TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS ix_chapters_job ON chapters(job_id, idx);
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id BIGINT PRIMARY KEY,
    style   TEXT NOT NULL DEFAULT ''
);
"""


def _clean_dsn(url: str) -> str:
    """Neon adds channel_binding=require, which asyncpg doesn't understand -> drop it."""
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit(parts._replace(query=urlencode(q)))


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        _pool = await asyncpg.create_pool(
            _clean_dsn(DATABASE_URL),
            min_size=1,
            max_size=5,
            statement_cache_size=0,  # required for Neon's pooled (pgbouncer) endpoint
            max_inactive_connection_lifetime=240,
            command_timeout=60,
        )
    return _pool


async def _run(sql, *args, fetch=None):
    pool = await _get_pool()
    last: Exception | None = None
    for attempt in range(3):  # Neon may drop idle connections; retry transparently
        try:
            async with pool.acquire() as con:
                if fetch == "one":
                    row = await con.fetchrow(sql, *args)
                    return dict(row) if row else None
                if fetch == "all":
                    rows = await con.fetch(sql, *args)
                    return [dict(r) for r in rows]
                await con.execute(sql, *args)
                return None
        except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, ConnectionResetError, OSError) as e:
            last = e
            await asyncio.sleep(1 + attempt)
    raise last


async def init_db():
    pool = await _get_pool()
    async with pool.acquire() as con:
        await con.execute(SCHEMA)


async def close_db():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------- users ----------
async def get_engine(user_id: int) -> str:
    row = await _run("SELECT engine FROM users WHERE user_id=$1", user_id, fetch="one")
    return row["engine"] if row else "auto"


async def set_engine(user_id: int, engine: str):
    await _run(
        "INSERT INTO users (user_id, engine) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET engine=EXCLUDED.engine",
        user_id, engine,
    )


# ---------- voice style ----------
async def get_style(user_id: int) -> str:
    row = await _run("SELECT style FROM user_prefs WHERE user_id=$1", user_id, fetch="one")
    return row["style"] if row else ""


async def set_style(user_id: int, style: str):
    await _run(
        "INSERT INTO user_prefs (user_id, style) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET style=EXCLUDED.style",
        user_id, style,
    )


# ---------- jobs ----------
async def create_job(user_id, chat_id, title, engine, chapters) -> int:
    pool = await _get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            job_id = await con.fetchval(
                "INSERT INTO jobs (user_id, chat_id, title, engine) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                user_id, chat_id, title, engine,
            )
            await con.executemany(
                "INSERT INTO chapters (job_id, idx, title, text) VALUES ($1, $2, $3, $4)",
                [(job_id, i, c.title, c.text) for i, c in enumerate(chapters)],
            )
    return job_id


async def get_job(job_id: int):
    return await _run("SELECT * FROM jobs WHERE id=$1", job_id, fetch="one")


async def set_job_status(job_id: int, status: str):
    await _run("UPDATE jobs SET status=$1 WHERE id=$2", status, job_id)


async def set_progress_msg(job_id: int, message_id: int):
    await _run("UPDATE jobs SET progress_msg_id=$1 WHERE id=$2", message_id, job_id)


async def active_job_for_user(user_id: int):
    return await _run(
        "SELECT * FROM jobs WHERE user_id=$1 AND status IN ('queued','running') "
        "ORDER BY id DESC LIMIT 1",
        user_id, fetch="one",
    )


async def latest_failed_job(user_id: int):
    return await _run(
        "SELECT * FROM jobs WHERE user_id=$1 AND status='failed' ORDER BY id DESC LIMIT 1",
        user_id, fetch="one",
    )


async def requeue_running():
    await _run("UPDATE jobs SET status='queued' WHERE status='running'")


async def queued_job_ids():
    rows = await _run("SELECT id FROM jobs WHERE status='queued' ORDER BY id", fetch="all")
    return [r["id"] for r in rows]


# ---------- chapters ----------
async def chapter_list(job_id: int):
    """Light listing (no text) for the selection screen."""
    return await _run(
        "SELECT id, idx, title, status, LENGTH(text) AS chars "
        "FROM chapters WHERE job_id=$1 ORDER BY idx",
        job_id, fetch="all",
    )


async def skip_by_idx(job_id: int, idxs: list[int]):
    if not idxs:
        return
    await _run(
        "UPDATE chapters SET status='skipped' WHERE job_id=$1 AND idx = ANY($2::int[])",
        job_id, idxs,
    )


async def toggle_chapter(job_id: int, idx: int):
    await _run(
        "UPDATE chapters SET status = CASE status WHEN 'skipped' THEN 'pending' ELSE 'skipped' END "
        "WHERE job_id=$1 AND idx=$2 AND status IN ('pending','skipped')",
        job_id, idx,
    )


async def set_all_chapters(job_id: int, status: str):
    await _run(
        "UPDATE chapters SET status=$1 WHERE job_id=$2 AND status IN ('pending','skipped')",
        status, job_id,
    )


async def invert_chapters(job_id: int):
    await _run(
        "UPDATE chapters SET status = CASE status WHEN 'skipped' THEN 'pending' ELSE 'skipped' END "
        "WHERE job_id=$1 AND status IN ('pending','skipped')",
        job_id,
    )


async def pending_chapters(job_id: int):
    return await _run(
        "SELECT * FROM chapters WHERE job_id=$1 AND status NOT IN ('done','skipped') ORDER BY idx",
        job_id, fetch="all",
    )


async def selected_ids(job_id: int) -> list[int]:
    rows = await _run(
        "SELECT id FROM chapters WHERE job_id=$1 AND status<>'skipped' ORDER BY idx",
        job_id, fetch="all",
    )
    return [r["id"] for r in rows]


async def mark_chapter_done(chapter_id: int):
    await _run("UPDATE chapters SET status='done' WHERE id=$1", chapter_id)


async def progress(job_id: int):
    row = await _run(
        "SELECT COUNT(*) FILTER (WHERE status='done') AS done, COUNT(*) AS total "
        "FROM chapters WHERE job_id=$1 AND status<>'skipped'",
        job_id, fetch="one",
    )
    return int(row["done"] or 0), int(row["total"] or 0)