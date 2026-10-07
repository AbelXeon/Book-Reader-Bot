import asyncio
import logging
import math
import os
import re
import shutil
import time
import uuid
from collections import Counter
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import config
import db
import tts
from pdf_utils import NoTextError, parse_pdf, split_text
from server import keepalive, run_webhook, start_health_server
from tts import merge_mp3, speak, split_if_big

log = logging.getLogger("bot")
router = Router()
queue: asyncio.Queue[int] = asyncio.Queue()
MAX_DOWNLOAD = 20 * 1024 * 1024  # Telegram cloud Bot API download limit
ENGINES = ("auto", "fast", "edge")
PER_PAGE = 8
SAMPLE_TEXT = (
    "Chapter one. It was a quiet evening, and the old house stood silent "
    "at the end of the road. Nobody knew that the story was about to begin."
)

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ICON = {"gemini": "🎙", "deepgram": "⚡", "edge": "🆓"}

# menu buttons (under the input field)
BTN_ENGINE = "🎙 Engine"
BTN_STYLE = "🎭 Style"
BTN_SAMPLE = "🎧 Sample"
BTN_STATUS = "📊 Status"
BTN_LIVE = "🟢 Live"
BTN_RESUME = "▶️ Resume"
BTN_CANCEL = "🛑 Cancel"
BTN_HELP = "❓ Help"

CANCELS: set[int] = set()  # job ids being cancelled (fast in-memory check)
ACTIVE: dict[int, "Progress"] = {}  # running jobs -> live progress
AWAITING_STYLE: set[int] = set()  # users who pressed the Style button

JUNK = re.compile(
    r"\b(copyright|table of contents|contents|index|bibliography|acknowledg\w*|"
    r"about the author|also by|dedication|colophon|title page|half title|permissions)\b",
    re.I,
)


class Cancelled(Exception):
    pass


class Aborted(Exception):
    """Another chapter failed for good; stop spending quota."""


# ---------------- helpers ----------------
def allowed(uid: int) -> bool:
    return not config.ALLOWED or uid in config.ALLOWED


def fmt_duration(chars: int) -> str:
    mins = int(chars / 900)  # ~900 chars of text per minute of speech
    if mins < 1:
        return "<1m"
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def fmt_secs(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")


def safe_name(name: str) -> str:
    s = re.sub(r"[^\w\- ]+", "", name).strip()
    return (s or "chapter")[:60]


def is_junk(title: str) -> bool:
    return len(title) <= 40 and bool(JUNK.search(title))


def is_cancelled(job_id: int) -> bool:
    return job_id in CANCELS


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ENGINE), KeyboardButton(text=BTN_STYLE)],
            [KeyboardButton(text=BTN_SAMPLE), KeyboardButton(text=BTN_STATUS)],
            [KeyboardButton(text=BTN_LIVE), KeyboardButton(text=BTN_RESUME)],
            [KeyboardButton(text=BTN_CANCEL), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Send a PDF to make an audiobook 📚",
    )


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and not allowed(user.id):
            return None
        return await handler(event, data)


# ---------------- live progress ----------------
class Progress:
    """Real progress: workers update counters, a ticker renders them."""

    def __init__(self, bot: Bot, job: dict, total_chunks: int, total_chapters: int):
        self.bot = bot
        self.job = job
        self.total = max(1, total_chunks)
        self.done = 0
        self.sent = 0
        self.ch_total = total_chapters
        self.stage = "Starting…"
        self.upload = ""
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
            f"🧩 {self.done}/{self.total} parts • ⏱ {self._t(elapsed)}{eta}",
            f"📤 Sent {self.sent}/{self.ch_total} chapters",
        ]
        if self.providers:
            lines.append(" • ".join(f"{ICON.get(k, '')} {k} {v}" for k, v in self.providers.items()))
        if final:
            lines.append("Finished.")
        else:
            lines.append(f"▸ {self.stage}")
            if self.upload:
                lines.append(f"▸ {self.upload}")
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


# ---------------- commands & menu ----------------
@router.message(Command("start"))
@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_start(m: Message):
    AWAITING_STYLE.discard(m.from_user.id)
    await m.answer(
        "📚 Send me a PDF and I'll turn it into an audiobook, one MP3 per chapter.\n"
        "You'll pick which sections to include before I start.\n\n"
        "🎙 Engine – ⚡ Fast is quickest, 🎙 Auto has the best voice\n"
        "🎭 Style – describe how the voice should sound\n"
        "🎧 Sample – hear a short test with your settings\n"
        "📊 Status – live progress\n"
        "🟢 Live – keep the server awake (Render)\n"
        "▶️ Resume – continue a stopped book\n"
        "🛑 Cancel – stop the current book",
        reply_markup=main_menu(),
    )


@router.message(Command("engine"))
@router.message(F.text == BTN_ENGINE)
async def cmd_engine(m: Message):
    AWAITING_STYLE.discard(m.from_user.id)
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
@router.message(F.text == BTN_STYLE)
async def cmd_style(m: Message, command: CommandObject = None):
    uid = m.from_user.id
    arg = (command.args or "").strip() if command else ""
    if not arg:
        AWAITING_STYLE.add(uid)
        cur = await db.get_style(uid) or f"{config.GEMINI_STYLE} (default)"
        await m.answer(
            f"🎭 Current voice style:\n{cur}\n\n"
            "✍️ Send your new style as your next message, for example:\n"
            "warm, slow, deep British storyteller\n\n"
            "Send 'reset' for the default or 'cancel' to keep it.\n"
            "The style is used by the Gemini voice (🎙 Auto engine)."
        )
        return
    AWAITING_STYLE.discard(uid)
    if arg.lower() == "reset":
        await db.set_style(uid, "")
        await m.answer("✅ Style reset to default.")
        return
    await db.set_style(uid, arg[:300])
    await m.answer("✅ Style saved. Tap 🎧 Sample to hear it.")


@router.message(Command("sample"))
@router.message(F.text == BTN_SAMPLE)
async def cmd_sample(m: Message, bot: Bot):
    AWAITING_STYLE.discard(m.from_user.id)
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
@router.message(F.text == BTN_STATUS)
async def cmd_status(m: Message):
    AWAITING_STYLE.discard(m.from_user.id)
    job = await db.active_job_for_user(m.from_user.id)
    if not job:
        await m.answer("No book running right now.")
        return
    prog = ACTIVE.get(job["id"])
    if prog:
        await m.answer(prog.text())
        return
    done, total = await db.progress(job["id"])
    await m.answer(f"🎧 {job['title']}\n{done}/{total} chapters done • {job['status']}")


@router.message(Command("cancel"))
@router.message(F.text == BTN_CANCEL)
async def cmd_cancel(m: Message):
    AWAITING_STYLE.discard(m.from_user.id)
    job = await db.active_job_for_user(m.from_user.id)
    if not job:
        await m.answer("Nothing to cancel.")
        return
    CANCELS.add(job["id"])
    await db.set_job_status(job["id"], "cancelled")
    await m.answer("🛑 Cancelling…")


@router.message(Command("resume"))
@router.message(F.text == BTN_RESUME)
async def cmd_resume(m: Message):
    AWAITING_STYLE.discard(m.from_user.id)
    if await db.active_job_for_user(m.from_user.id):
        await m.answer("A book is already running.")
        return
    job = await db.latest_failed_job(m.from_user.id)
    if not job:
        await m.answer("No stopped book to resume.")
        return
    CANCELS.discard(job["id"])
    await db.set_job_status(job["id"], "queued")
    queue.put_nowait(job["id"])
    await m.answer(f"▶️ Resuming: {job['title']}")


# ---------------- Live (keep Render awake) ----------------
def live_text() -> str:
    on_render = bool(config.PUBLIC_URL)
    mode = "webhook on Render (wakes on any message)" if (on_render and config.USE_WEBHOOK) else (
        "polling" if not on_render else "polling on Render (may sleep!)"
    )
    lines = ["🟢 Live", f"Mode: {mode}"]
    if not on_render:
        lines.append("Running locally, so there's nothing to keep awake.")
        return "\n".join(lines)
    rem = keepalive.remaining()
    lines.append(f"Keep-awake: {'ON • ' + fmt_secs(rem) + ' left' if rem > 0 else 'OFF'}")
    lines.append(f"Book running: {'yes (auto keep-awake)' if ACTIVE else 'no'}")
    if keepalive.last_ts:
        ago = fmt_secs(time.time() - keepalive.last_ts)
        lines.append(f"Last self-ping: {'✅' if keepalive.last_ok else '⚠️ failed'} {ago} ago")
    lines.append("\nIf I ever sleep, just send any message and I'll wake up (~1 min).")
    return "\n".join(lines)


def live_kb() -> InlineKeyboardMarkup:
    return kb(
        [("🟢 1h", "live:1"), ("🟢 3h", "live:3"), ("🟢 6h", "live:6")],
        [("🔄 Ping now", "live:ping"), ("🔴 Off", "live:off")],
    )


@router.message(Command("live"))
@router.message(F.text == BTN_LIVE)
async def cmd_live(m: Message):
    AWAITING_STYLE.discard(m.from_user.id)
    await m.answer(live_text(), reply_markup=live_kb())


@router.callback_query(F.data.startswith("live:"))
async def cb_live(cb: CallbackQuery):
    action = cb.data.split(":")[1]
    note = None
    if action in ("1", "3", "6"):
        keepalive.keep_awake(float(action))
        if keepalive.enabled:
            await keepalive.ping_now()
        note = f"Keeping awake for {action}h"
    elif action == "off":
        keepalive.stop()
        note = "Keep-awake off"
    elif action == "ping":
        ok = await keepalive.ping_now() if keepalive.enabled else False
        note = "✅ Server answered" if ok else "⚠️ Ping failed (not on Render?)"
    try:
        await cb.message.edit_text(live_text(), reply_markup=live_kb())
    except TelegramBadRequest:
        pass
    await cb.answer(note)


# ---------------- PDF upload + chapter selection ----------------
def sel_text(job: dict, chs: list[dict], extra: str = "") -> str:
    chosen = [c for c in chs if c["status"] != "skipped"]
    chars = sum(c["chars"] for c in chosen)
    lines = [
        f"📚 {job['title'][:80]}",
        f"{len(chs)} sections • ✅ {len(chosen)} selected • ~{fmt_duration(chars)} of audio",
    ]
    if extra:
        lines.append(extra)
    lines += ["", "Tap a section to include ☑️ or skip ⬜ it, then press Start."]
    return "\n".join(lines)


def sel_kb(job_id: int, chs: list[dict], page: int) -> InlineKeyboardMarkup:
    pages = max(1, math.ceil(len(chs) / PER_PAGE))
    page = max(0, min(page, pages - 1))
    rows: list[list[tuple[str, str]]] = []
    for c in chs[page * PER_PAGE : (page + 1) * PER_PAGE]:
        mark = "⬜" if c["status"] == "skipped" else "☑️"
        label = f"{mark} {c['idx'] + 1}. {c['title'][:30]} · {fmt_duration(c['chars'])}"
        rows.append([(label, f"tg:{job_id}:{c['idx']}:{page}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(("◀️", f"pg:{job_id}:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(("▶️", f"pg:{job_id}:{page + 1}"))
        rows.append(nav)
    rows.append([
        ("✅ All", f"all:{job_id}:{page}"),
        ("⬜ None", f"none:{job_id}:{page}"),
        ("🔁 Invert", f"inv:{job_id}:{page}"),
    ])
    n = sum(1 for c in chs if c["status"] != "skipped")
    rows.append([(f"▶️ Start ({n})", f"go:{job_id}"), ("✖️ Cancel", f"no:{job_id}")])
    return kb(*rows)


@router.message(F.document)
async def on_document(m: Message, bot: Bot):
    AWAITING_STYLE.discard(m.from_user.id)
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

    skip = [i for i, c in enumerate(book.chapters) if is_junk(c.title)]
    if len(skip) == len(book.chapters):
        skip = []
    await db.skip_by_idx(job_id, skip)

    job = await db.get_job(job_id)
    chs = await db.chapter_list(job_id)
    extra = f"{book.pages} pages • found by: {book.method} • engine: {engine}"
    if skip:
        extra += f"\n🚫 Pre-skipped {len(skip)} junk section(s) (copyright/index/etc.). Tap to bring back."
    await status.edit_text(sel_text(job, chs, extra), reply_markup=sel_kb(job_id, chs, 0))


async def _pending_job(cb: CallbackQuery, job_id: int):
    job = await db.get_job(job_id)
    if not job or job["user_id"] != cb.from_user.id or job["status"] != "pending":
        await cb.answer("This selection is no longer active.", show_alert=True)
        return None
    return job


async def _refresh_selection(cb: CallbackQuery, job: dict, page: int):
    chs = await db.chapter_list(job["id"])
    try:
        await cb.message.edit_text(sel_text(job, chs), reply_markup=sel_kb(job["id"], chs, page))
    except TelegramBadRequest:
        pass  # message not modified
    await cb.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data.startswith("tg:"))
async def cb_toggle(cb: CallbackQuery):
    _, job_id, idx, page = cb.data.split(":")
    job = await _pending_job(cb, int(job_id))
    if not job:
        return
    await db.toggle_chapter(job["id"], int(idx))
    await _refresh_selection(cb, job, int(page))


@router.callback_query(F.data.startswith("pg:"))
async def cb_page(cb: CallbackQuery):
    _, job_id, page = cb.data.split(":")
    job = await _pending_job(cb, int(job_id))
    if job:
        await _refresh_selection(cb, job, int(page))


@router.callback_query(F.data.startswith(("all:", "none:", "inv:")))
async def cb_bulk(cb: CallbackQuery):
    action, job_id, page = cb.data.split(":")
    job = await _pending_job(cb, int(job_id))
    if not job:
        return
    if action == "all":
        await db.set_all_chapters(job["id"], "pending")
    elif action == "none":
        await db.set_all_chapters(job["id"], "skipped")
    else:
        await db.invert_chapters(job["id"])
    await _refresh_selection(cb, job, int(page))


@router.callback_query(F.data.startswith("go:"))
async def cb_go(cb: CallbackQuery, bot: Bot):
    job_id = int(cb.data.split(":")[1])
    job = await _pending_job(cb, job_id)
    if not job:
        return
    if await db.active_job_for_user(cb.from_user.id):
        await cb.answer("A book is already running. Use 🛑 Cancel first.", show_alert=True)
        return
    chs = await db.chapter_list(job_id)
    n = sum(1 for c in chs if c["status"] != "skipped")
    if n == 0:
        await cb.answer("Select at least one section first.", show_alert=True)
        return
    msg = await bot.send_message(job["chat_id"], "⏳ Queued…")
    await db.set_progress_msg(job_id, msg.message_id)
    await db.set_job_status(job_id, "queued")
    queue.put_nowait(job_id)
    await cb.message.edit_text(f"✅ Started with {n} section(s). I'll send each chapter as soon as it's ready.")
    await cb.answer()


@router.callback_query(F.data.startswith("no:"))
async def cb_no(cb: CallbackQuery):
    job_id = int(cb.data.split(":")[1])
    job = await db.get_job(job_id)
    if job and job["user_id"] == cb.from_user.id and job["status"] == "pending":
        await db.set_job_status(job_id, "cancelled")
    await cb.message.edit_text("✖️ Cancelled.")
    await cb.answer()


# plain text (must stay LAST among message handlers)
@router.message(F.text & ~F.text.startswith("/"))
async def on_text(m: Message):
    uid = m.from_user.id
    if uid in AWAITING_STYLE:
        AWAITING_STYLE.discard(uid)
        text = m.text.strip()
        if text.lower() == "cancel":
            await m.answer("👍 Style unchanged.")
        elif text.lower() == "reset":
            await db.set_style(uid, "")
            await m.answer("✅ Style reset to default.")
        else:
            await db.set_style(uid, text[:300])
            await m.answer("✅ Style saved. Tap 🎧 Sample to hear it.")
        return
    await m.answer("Send me a PDF 📚 or use the buttons below.", reply_markup=main_menu())


# ---------------- worker ----------------
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


async def build_chapter(
    job: dict, ch: dict, chunks: list[str], ordn: int, work: Path, prog: Progress,
    style: str, gate: asyncio.Semaphore, ahead: asyncio.Semaphore,
    merge_gate: asyncio.Semaphore, abort: asyncio.Event, state: dict,
):
    """Generates all chunks of one chapter (sharing the global worker pool) and merges them."""
    await ahead.acquire()
    ch_dir = work / f"ch{ch['idx']:03d}"
    ch_dir.mkdir(parents=True, exist_ok=True)
    files = [ch_dir / f"{n:04d}.mp3" for n in range(len(chunks))]

    for attempt in range(3):
        counted = 0
        used = [""] * len(chunks)

        async def one(n: int, chunk: str):
            nonlocal counted
            async with gate:
                if is_cancelled(job["id"]):
                    raise Cancelled()
                if abort.is_set():
                    raise Aborted()
                f = files[n]
                if f.exists() and f.stat().st_size > 1000:  # cached from a previous run
                    used[n] = "cached"
                else:
                    name = await speak(chunk, f, job["engine"], style)
                    used[n] = name
                    prog.providers[name] += 1
                prog.done += 1
                counted += 1

        tasks = [asyncio.create_task(one(n, c)) for n, c in enumerate(chunks)]
        try:
            await asyncio.gather(*tasks)
            providers = set(used)
            same_format = len(providers) == 1 and "cached" not in providers
            async with merge_gate:
                merged = ch_dir / "chapter.mp3"
                await merge_mp3(files, merged, copy=same_format)
            final = work / f"{ordn:02d} - {safe_name(ch['title'])}.mp3"
            merged.replace(final)
            parts = await split_if_big(final)
            voice = "+".join(sorted(providers - {"cached"})) or "cached"
            return parts, voice, ch_dir
        except BaseException as e:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(e, (Cancelled, Aborted, asyncio.CancelledError)):
                raise
            prog.done -= counted
            log.exception("Chapter %s failed (attempt %d)", ch["idx"] + 1, attempt + 1)
            if attempt == 2:
                state.setdefault("error", e)
                abort.set()
                raise
            await asyncio.sleep(5 * (attempt + 1))


async def process_job(bot: Bot, job_id: int):
    job = await db.get_job(job_id)
    if not job or job["status"] not in ("queued", "running"):
        return
    await db.set_job_status(job_id, "running")
    CANCELS.discard(job_id)
    work = config.WORK_DIR / str(job_id)
    work.mkdir(parents=True, exist_ok=True)

    chapters = await db.pending_chapters(job_id)
    sel_ids = await db.selected_ids(job_id)
    total = len(sel_ids)
    ordinal = {cid: i + 1 for i, cid in enumerate(sel_ids)}
    style = (await db.get_style(job["user_id"])) or config.GEMINI_STYLE
    plans = {c["id"]: split_text(c["text"], config.CHUNK_CHARS) for c in chapters}
    total_chunks = sum(len(v) for v in plans.values())

    prog = Progress(bot, job, total_chunks, total)
    prog.sent = total - len(chapters)
    prog.stage = f"Generating voice ({config.CHUNK_WORKERS} workers)"
    ACTIVE[job_id] = prog
    tick = asyncio.create_task(ticker(prog))

    gate = asyncio.Semaphore(config.CHUNK_WORKERS)
    ahead = asyncio.Semaphore(max(1, config.LOOKAHEAD))
    merge_gate = asyncio.Semaphore(2)
    abort = asyncio.Event()
    state: dict = {}
    builders = [
        asyncio.create_task(
            build_chapter(job, c, plans[c["id"]], ordinal[c["id"]], work, prog,
                          style, gate, ahead, merge_gate, abort, state)
        )
        for c in chapters
    ]

    cancelled = False
    failed: Exception | None = None
    try:
        for c, task in zip(chapters, builders):
            parts, voice, ch_dir = await task
            if is_cancelled(job_id):
                raise Cancelled()
            n = ordinal[c["id"]]
            prog.upload = f"Uploading chapter {n}: {c['title'][:30]}"
            for i, p in enumerate(parts, 1):
                suffix = f" (part {i}/{len(parts)})" if len(parts) > 1 else ""
                await send_file(
                    bot, job["chat_id"], p, f"{n}. {c['title']}{suffix}",
                    f"Chapter {n}/{total} • {voice}",
                )
                p.unlink(missing_ok=True)
            prog.upload = ""
            shutil.rmtree(ch_dir, ignore_errors=True)
            await db.mark_chapter_done(c["id"])
            prog.sent += 1
            ahead.release()
    except Cancelled:
        cancelled = True
    except Aborted:
        failed = state.get("error") or RuntimeError("a chapter failed")
    except Exception as e:
        log.exception("Job %s failed", job_id)
        failed = state.get("error") or e
    finally:
        tick.cancel()
        for t in builders:
            t.cancel()
        await asyncio.gather(*builders, return_exceptions=True)
        ACTIVE.pop(job_id, None)
        CANCELS.discard(job_id)

    if cancelled:
        shutil.rmtree(work, ignore_errors=True)
        await bot.send_message(job["chat_id"], "🛑 Cancelled.")
        return
    if failed:
        await db.set_job_status(job_id, "failed")
        await bot.send_message(
            job["chat_id"],
            f"❌ Stopped: {str(failed)[:300]}\nChapters already sent are done. Tap ▶️ Resume to continue.",
        )
        return

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
    if not tts.FFMPEG:
        raise SystemExit("ffmpeg not found. Install it (or pip install imageio-ffmpeg).")

    log.info(
        "Voice engines -> gemini keys: %d | deepgram keys: %d | edge: yes",
        len(config.GEMINI_API_KEYS), len(config.DEEPGRAM_API_KEYS),
    )

    await db.init_db()
    await db.requeue_running()
    for jid in await db.queued_job_ids():
        queue.put_nowait(jid)

    bot = Bot(config.BOT_TOKEN, session=AiohttpSession(timeout=600))
    dp = Dispatcher()
    dp.update.outer_middleware(AccessMiddleware())
    dp.include_router(router)

    keepalive.busy = lambda: bool(ACTIVE) or not queue.empty()
    keepalive.start()
    bg = asyncio.create_task(worker(bot))  # keep a reference

    try:
        if config.PUBLIC_URL and config.USE_WEBHOOK:
            await run_webhook(bot, dp)
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            if os.getenv("PORT"):
                await start_health_server()
            await dp.start_polling(bot)
    finally:
        bg.cancel()
        await tts.close_http()


if __name__ == "__main__":
    asyncio.run(main())