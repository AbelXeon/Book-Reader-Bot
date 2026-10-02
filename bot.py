import asyncio
import logging
import re
import shutil
import uuid
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
import db
from pdf_utils import NoTextError, parse_pdf, split_text
from tts import merge_mp3, speak, split_if_big

log = logging.getLogger("bot")
router = Router()
queue: asyncio.Queue[int] = asyncio.Queue()
MAX_DOWNLOAD = 20 * 1024 * 1024  # Telegram cloud Bot API download limit


class Cancelled(Exception):
    pass


# ---------------- helpers ----------------
def allowed(uid: int) -> bool:
    return not config.ALLOWED or uid in config.ALLOWED


def fmt_duration(chars: int) -> str:
    mins = int(chars / 900)  # ~900 chars of text per minute of speech
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def safe_name(name: str) -> str:
    s = re.sub(r"[^\w\- ]+", "", name).strip()
    return (s or "chapter")[:60]


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and not allowed(user.id):
            return None
        return await handler(event, data)


# ---------------- commands ----------------
@router.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "📚 Send me a PDF and I'll turn it into an audiobook, one MP3 per chapter.\n\n"
        "/engine – choose voice engine\n"
        "/status – progress of current book\n"
        "/cancel – stop current book\n"
        "/resume – continue a failed book"
    )


@router.message(Command("engine"))
async def cmd_engine(m: Message):
    cur = await db.get_engine(m.from_user.id)
    await m.answer(
        f"Current engine: {cur}\n\n"
        "auto = Gemini first, edge-tts if Gemini fails\n"
        "edge = edge-tts only (free, no limits to worry about)",
        reply_markup=kb([("🎙 Auto (Gemini → Edge)", "eng:auto"), ("⚡ Edge only", "eng:edge")]),
    )


@router.callback_query(F.data.startswith("eng:"))
async def cb_engine(cb: CallbackQuery):
    engine = cb.data.split(":")[1]
    await db.set_engine(cb.from_user.id, engine)
    await cb.message.edit_text(f"✅ Engine set to: {engine}")
    await cb.answer()


@router.message(Command("status"))
async def cmd_status(m: Message):
    job = await db.active_job_for_user(m.from_user.id)
    if not job:
        await m.answer("No book running right now.")
        return
    done, total = await db.progress(job["id"])
    await m.answer(f"🎧 {job['title']}\n{done}/{total} chapters done • {job['status']}")


@router.message(Command("cancel"))
async def cmd_cancel(m: Message):
    job = await db.active_job_for_user(m.from_user.id)
    if not job:
        await m.answer("Nothing to cancel.")
        return
    await db.set_job_status(job["id"], "cancelled")
    await m.answer("🛑 Cancelling…")


@router.message(Command("resume"))
async def cmd_resume(m: Message):
    if await db.active_job_for_user(m.from_user.id):
        await m.answer("A book is already running.")
        return
    job = await db.latest_failed_job(m.from_user.id)
    if not job:
        await m.answer("No failed book to resume.")
        return
    await db.set_job_status(job["id"], "queued")
    queue.put_nowait(job["id"])
    await m.answer(f"▶️ Resuming: {job['title']}")


# ---------------- PDF upload ----------------
@router.message(F.document)
async def on_document(m: Message, bot: Bot):
    d = m.document
    name = d.file_name or "book.pdf"
    if not (name.lower().endswith(".pdf") or d.mime_type == "application/pdf"):
        await m.answer("Please send a PDF file.")
        return
    if d.file_size and d.file_size > MAX_DOWNLOAD:
        await m.answer("❌ That PDF is over 20 MB and Telegram doesn't let bots download it. Compress or split it first.")
        return

    status = await m.answer("📥 Downloading…")
    path = config.UPLOAD_DIR / f"{uuid.uuid4().hex}.pdf"
    try:
        tg_file = await bot.get_file(d.file_id)
        await bot.download_file(tg_file.file_path, destination=path)
        await status.edit_text("📖 Reading the book…")
        book = await asyncio.to_thread(parse_pdf, path, Path(name).stem)
    except NoTextError:
        await status.edit_text("❌ No readable text found. This looks like a scanned PDF (OCR isn't supported yet).")
        return
    except Exception as e:
        log.exception("PDF processing failed")
        await status.edit_text(f"❌ Couldn't process this PDF: {str(e)[:200]}")
        return
    finally:
        path.unlink(missing_ok=True)

    engine = await db.get_engine(m.from_user.id)
    job_id = await db.create_job(m.from_user.id, m.chat.id, book.title, engine, book.chapters)

    lines = [
        f"📚 {book.title}",
        f"{book.pages} pages • {len(book.chapters)} chapters • ~{fmt_duration(book.chars)} of audio",
        f"Chapters found by: {book.method} • engine: {engine}",
        "",
    ]
    for i, c in enumerate(book.chapters[:10], 1):
        lines.append(f"{i}. {c.title[:60]}")
    if len(book.chapters) > 10:
        lines.append(f"… and {len(book.chapters) - 10} more")

    await status.edit_text(
        "\n".join(lines),
        reply_markup=kb([("▶️ Start", f"go:{job_id}"), ("✖️ Cancel", f"no:{job_id}")]),
    )


@router.callback_query(F.data.startswith("go:"))
async def cb_go(cb: CallbackQuery, bot: Bot):
    job_id = int(cb.data.split(":")[1])
    job = await db.get_job(job_id)
    if not job or job["user_id"] != cb.from_user.id or job["status"] != "pending":
        await cb.answer("Not available anymore.", show_alert=True)
        return
    if await db.active_job_for_user(cb.from_user.id):
        await cb.answer("A book is already running. Use /cancel first.", show_alert=True)
        return
    msg = await bot.send_message(job["chat_id"], "⏳ Queued…")
    await db.set_progress_msg(job_id, msg.message_id)
    await db.set_job_status(job_id, "queued")
    queue.put_nowait(job_id)
    await cb.message.edit_text("✅ Started. I'll send each chapter as soon as it's ready.")
    await cb.answer()


@router.callback_query(F.data.startswith("no:"))
async def cb_no(cb: CallbackQuery):
    job_id = int(cb.data.split(":")[1])
    job = await db.get_job(job_id)
    if job and job["user_id"] == cb.from_user.id and job["status"] == "pending":
        await db.set_job_status(job_id, "cancelled")
    await cb.message.edit_text("✖️ Cancelled.")
    await cb.answer()


# ---------------- worker ----------------
async def is_cancelled(job_id: int) -> bool:
    job = await db.get_job(job_id)
    return bool(job and job["status"] == "cancelled")


async def update_progress(bot: Bot, job: dict, text: str):
    if not job.get("progress_msg_id"):
        return
    try:
        await bot.edit_message_text(text, chat_id=job["chat_id"], message_id=job["progress_msg_id"])
    except Exception:
        pass  # "message is not modified" etc.


async def send_file(bot: Bot, chat_id: int, path: Path, title: str, caption: str):
    for attempt in range(3):
        try:
            await bot.send_audio(
                chat_id, FSInputFile(path), title=title[:64], performer="Audiobook", caption=caption
            )
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TelegramNetworkError, asyncio.TimeoutError):
            await asyncio.sleep(5 * (attempt + 1))
    raise RuntimeError("Telegram upload failed after 3 tries")


async def process_chapter(bot: Bot, job: dict, ch: dict, total: int, work: Path):
    ch_dir = work / f"ch{ch['idx']:03d}"
    ch_dir.mkdir(parents=True, exist_ok=True)
    chunks = split_text(ch["text"], config.CHUNK_CHARS)
    used: set[str] = set()
    files: list[Path] = []
    head = f"🎧 {job['title']}\nChapter {ch['idx'] + 1}/{total}: {ch['title'][:50]}"

    await update_progress(bot, job, f"{head}\nStarting…")
    for n, chunk in enumerate(chunks):
        if await is_cancelled(job["id"]):
            raise Cancelled()
        f = ch_dir / f"{n:04d}.mp3"
        if not (f.exists() and f.stat().st_size > 1000):  # cached from a previous run
            used.add(await speak(chunk, f, job["engine"]))
        files.append(f)
        if n % 5 == 0:
            await update_progress(bot, job, f"{head}\nPart {n + 1}/{len(chunks)}")

    merged = ch_dir / "chapter.mp3"
    await merge_mp3(files, merged)
    final = work / f"{ch['idx'] + 1:02d} - {safe_name(ch['title'])}.mp3"
    merged.replace(final)

    parts = await split_if_big(final)
    voice = "+".join(sorted(used)) or "cached"
    for i, p in enumerate(parts, 1):
        suffix = f" (part {i}/{len(parts)})" if len(parts) > 1 else ""
        await send_file(
            bot, job["chat_id"], p, f"{ch['idx'] + 1}. {ch['title']}{suffix}",
            f"Chapter {ch['idx'] + 1}/{total} • {voice}",
        )
        p.unlink(missing_ok=True)

    shutil.rmtree(ch_dir, ignore_errors=True)
    await db.mark_chapter_done(ch["id"])


async def process_job(bot: Bot, job_id: int):
    job = await db.get_job(job_id)
    if not job or job["status"] not in ("queued", "running"):
        return
    await db.set_job_status(job_id, "running")
    work = config.WORK_DIR / str(job_id)
    work.mkdir(parents=True, exist_ok=True)
    chapters = await db.pending_chapters(job_id)
    _, total = await db.progress(job_id)

    try:
        for ch in chapters:
            if await is_cancelled(job_id):
                raise Cancelled()
            await process_chapter(bot, job, ch, total, work)
    except Cancelled:
        shutil.rmtree(work, ignore_errors=True)
        await bot.send_message(job["chat_id"], "🛑 Cancelled.")
        return
    except Exception as e:
        log.exception("Job %s failed", job_id)
        await db.set_job_status(job_id, "failed")
        await bot.send_message(
            job["chat_id"],
            f"❌ Stopped: {str(e)[:300]}\nFinished audio is saved. Send /resume to continue.",
        )
        return

    await db.set_job_status(job_id, "done")
    shutil.rmtree(work, ignore_errors=True)
    await update_progress(bot, job, f"✅ {job['title']} finished.")
    await bot.send_message(job["chat_id"], f"✅ Done: {job['title']}")


async def worker(bot: Bot):
    while True:
        job_id = await queue.get()
        try:
            await process_job(bot, job_id)
        except Exception:
            log.exception("Worker error on job %s", job_id)
        finally:
            queue.task_done()


# ---------------- main ----------------
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in .env")
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found. Install it and make sure it's on PATH.")

    await db.init_db()
    await db.requeue_running()
    for jid in await db.queued_job_ids():
        queue.put_nowait(jid)

    bot = Bot(config.BOT_TOKEN, session=AiohttpSession(timeout=600))
    dp = Dispatcher()
    dp.update.outer_middleware(AccessMiddleware())
    dp.include_router(router)

    asyncio.create_task(worker(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())