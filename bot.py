import asyncio
import logging
import re
import shutil
import time
import uuid
from collections import Counter
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
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
ENGINES = ("auto", "fast", "edge")
SAMPLE_TEXT = (
    "Chapter one. It was a quiet evening, and the old house stood silent "
    "at the end of the road. Nobody knew that the story was about to begin."
)

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ICON = {"gemini": "🎙", "deepgram": "⚡", "edge": "🆓"}


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


# ---------------- live progress ----------------
class Progress:
    """Real progress: counters are updated by the workers, a ticker renders them."""

    def __init__(self, bot: Bot, job: dict, total_chunks: int, total_chapters: int):
        self.bot = bot
        self.job = job
        self.total = max(1, total_chunks)
        self.done = 0
        self.ch_no = 0
        self.ch_total = total_chapters
        self.ch_title = ""
        self.stage = "Starting…"
        self.providers: Counter = Counter()
        self.start = time.monotonic()
        self.frame = 0
        self.hold_until = 0.0

    @staticmethod
    def _t(sec: float) -> str:
        m, s = divmod(int(sec), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def text(self, final: bool = False) -> str:
        pct = min(1.0, self.done / self.total)
        width = 14
        filled = int(width * pct)
        bar = "▰" * filled + "▱" * (width - filled)
        elapsed = time.monotonic() - self.start
        eta = ""
        if not final and self.done >= 3 and self.done < self.total:
            eta = f" • ETA {self._t(elapsed / self.done * (self.total - self.done))}"
        self.frame = (self.frame + 1) % len(SPIN)
        head = "✅" if final else SPIN[self.frame]
        lines = [
            f"{head} {self.job['title'][:60]}",
            f"{bar} {int(pct * 100)}%",
            f"📖 Chapter {self.ch_no}/{self.ch_total}: {self.ch_title[:40]}",
            f"🧩 {self.done}/{self.total} parts • ⏱ {self._t(elapsed)}{eta}",
        ]
        if self.providers:
            lines.append(" • ".join(f"{ICON.get(k, '')} {k} {v}" for k, v in self.providers.items()))
        lines.append("Finished." if final else f"▸ {self.stage}")
        return "\n".join(lines)

    async def render(self, final: bool = False):
        mid = self.job.get("progress_msg_id")
        if not mid or time.monotonic() < self.hold_until:
            return
        try:
            await self.bot.edit_message_text(
                self.text(final), chat_id=self.job["chat_id"], message_id=mid
            )
        except TelegramRetryAfter as e:
            self.hold_until = time.monotonic() + e.retry_after + 1
        except Exception:
            pass
        if not final:
            try:
                await self.bot.send_chat_action(self.job["chat_id"], "record_voice")
            except Exception:
                pass


async def ticker(prog: Progress):
    while True:
        await asyncio.sleep(3)
        await prog.render()


# ---------------- commands ----------------
@router.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "📚 Send me a PDF and I'll turn it into an audiobook, one MP3 per chapter.\n\n"
        "/engine – choose voice engine (⚡ Fast is quickest)\n"
        "/style – set how the voice sounds\n"
        "/sample – hear a short test with your settings\n"
        "/status – progress of current book\n"
        "/cancel – stop current book\n"
        "/resume – continue a failed book"
    )


@router.message(Command("engine"))
async def cmd_engine(m: Message):
    cur = await db.get_engine(m.from_user.id)
    await m.answer(
        f"Current engine: {cur}\n\n"
        "🎙 auto = Gemini voice + your style, falls back to Deepgram/Edge\n"
        "⚡ fast = Deepgram first, much faster for long books\n"
        "🆓 edge = Edge first (free), falls back to the others",
        reply_markup=kb(
            [("🎙 Auto", "eng:auto"), ("⚡ Fast", "eng:fast"), ("🆓 Edge", "eng:edge")]
        ),
    )


@router.callback_query(F.data.startswith("eng:"))
async def cb_engine(cb: CallbackQuery):
    engine = cb.data.split(":")[1]
    if engine not in ENGINES:
        await cb.answer("Unknown engine.", show_alert=True)
        return
    await db.set_engine(cb.from_user.id, engine)
    await cb.message.edit_text(f"✅ Engine set to: {engine}")
    await cb.answer()


@router.message(Command("style"))
async def cmd_style(m: Message, command: CommandObject):
    uid = m.from_user.id
    arg = (command.args or "").strip()
    if not arg:
        cur = await db.get_style(uid) or f"{config.GEMINI_STYLE} (default)"
        await m.answer(
            f"🎭 Current voice style:\n{cur}\n\n"
            "Change it:\n/style warm, slow, deep British storyteller\n"
            "Reset: /style reset\n"
            "Test it: /sample\n\n"
            "Note: the style is used by the Gemini voice (🎙 auto engine)."
        )
        return
    if arg.lower() == "reset":
        await db.set_style(uid, "")
        await m.answer("✅ Style reset to default.")
        return
    await db.set_style(uid, arg[:300])
    await m.answer("✅ Style saved. Use /sample to hear it.")


@router.message(Command("sample"))
async def cmd_sample(m: Message, bot: Bot):
    engine = await db.get_engine(m.from_user.id)
    style = await db.get_style(m.from_user.id) or config.GEMINI_STYLE
    status = await m.answer("🎧 Making a short sample…")
    out = config.WORK_DIR / f"sample_{uuid.uuid4().hex}.mp3"
    try:
        used = await speak(SAMPLE_TEXT, out, engine, style)
        await bot.send_audio(
            m.chat.id, FSInputFile(out), title="Voice sample", performer="Audiobook",
            caption=f"engine: {engine} • voice: {used}",
        )
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ Sample failed: {str(e)[:200]}")
    finally:
        out.unlink(missing_ok=True)


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


async def process_chapter(
    bot: Bot, job: dict, ch: dict, total: int, work: Path, prog: Progress, style: str
):
    ch_dir = work / f"ch{ch['idx']:03d}"
    ch_dir.mkdir(parents=True, exist_ok=True)
    chunks = split_text(ch["text"], config.CHUNK_CHARS)
    files = [ch_dir / f"{n:04d}.mp3" for n in range(len(chunks))]
    used: set[str] = set()

    prog.ch_no = ch["idx"] + 1
    prog.ch_total = total
    prog.ch_title = ch["title"]
    prog.stage = "Generating voice (parallel)"

    gate = asyncio.Semaphore(config.CHUNK_WORKERS)

    async def one(n: int, chunk: str):
        async with gate:
            if await is_cancelled(job["id"]):
                raise Cancelled()
            f = files[n]
            if f.exists() and f.stat().st_size > 1000:  # cached from a previous run
                prog.done += 1
                return
            name = await speak(chunk, f, job["engine"], style)
            used.add(name)
            prog.providers[name] += 1
            prog.done += 1

    tasks = [asyncio.create_task(one(n, c)) for n, c in enumerate(chunks)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    prog.stage = "Merging chapter"
    merged = ch_dir / "chapter.mp3"
    await merge_mp3(files, merged)
    final = work / f"{ch['idx'] + 1:02d} - {safe_name(ch['title'])}.mp3"
    merged.replace(final)

    prog.stage = "Uploading to Telegram"
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
    style = (await db.get_style(job["user_id"])) or config.GEMINI_STYLE
    total_chunks = sum(len(split_text(c["text"], config.CHUNK_CHARS)) for c in chapters)

    prog = Progress(bot, job, total_chunks, total)
    tick = asyncio.create_task(ticker(prog))
    try:
        for ch in chapters:
            if await is_cancelled(job_id):
                raise Cancelled()
            base = prog.done
            for attempt in range(3):  # retry a chapter before giving up (chunks are cached)
                try:
                    await process_chapter(bot, job, ch, total, work, prog, style)
                    break
                except Cancelled:
                    raise
                except Exception:
                    log.exception("Chapter %s failed (attempt %d)", ch["idx"] + 1, attempt + 1)
                    if attempt == 2:
                        raise
                    prog.done = base
                    prog.stage = f"Retrying chapter (attempt {attempt + 2}/3)"
                    await asyncio.sleep(10 * (attempt + 1))
    except Cancelled:
        tick.cancel()
        shutil.rmtree(work, ignore_errors=True)
        await bot.send_message(job["chat_id"], "🛑 Cancelled.")
        return
    except Exception as e:
        tick.cancel()
        log.exception("Job %s failed", job_id)
        await db.set_job_status(job_id, "failed")
        await bot.send_message(
            job["chat_id"],
            f"❌ Stopped: {str(e)[:300]}\nFinished audio is saved. Send /resume to continue.",
        )
        return

    tick.cancel()
    await db.set_job_status(job_id, "done")
    shutil.rmtree(work, ignore_errors=True)
    prog.done = prog.total
    await prog.render(final=True)
    await bot.send_message(
        job["chat_id"], f"✅ Done: {job['title']} • took {Progress._t(time.monotonic() - prog.start)}"
    )


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

    log.info(
        "Voice engines ready -> gemini: %s | deepgram: %s | edge: yes",
        bool(config.GEMINI_API_KEY), bool(config.DEEPGRAM_API_KEY),
    )

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