import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
WORK_DIR = DATA_DIR / "work"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "bot.db"
for _d in (WORK_DIR, UPLOAD_DIR):
    _d.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()


# Support comma-separated keys or single key fallback
def _parse_keys(env_plural: str, env_single: str) -> list[str]:
    raw = os.getenv(env_plural, "").strip() or os.getenv(env_single, "").strip()
    return [k.strip() for k in raw.split(",") if k.strip()]


GEMINI_API_KEYS = _parse_keys("GEMINI_API_KEYS", "GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts").strip()
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Kore").strip()
GEMINI_STYLE = os.getenv("GEMINI_STYLE", "calm, clear audiobook narrator").strip()

DEEPGRAM_API_KEYS = _parse_keys("DEEPGRAM_API_KEYS", "DEEPGRAM_API_KEY")
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "aura-2-thalia-en").strip()

EDGE_VOICE = os.getenv("EDGE_VOICE", "en-US-AriaNeural").strip()

# Backward-compatible flags for bot logging
GEMINI_API_KEY = GEMINI_API_KEYS[0] if GEMINI_API_KEYS else ""
DEEPGRAM_API_KEY = DEEPGRAM_API_KEYS[0] if DEEPGRAM_API_KEYS else ""

# Deepgram accepts max 2000 chars per request
CHUNK_CHARS = min(int(os.getenv("CHUNK_CHARS", "1800")), 1900)
MAX_AUDIO_MB = 48
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "48k").strip()

# Parallelism
# CONCURRENCY values are PER KEY (multiplied by how many keys you have).
CHUNK_WORKERS = int(os.getenv("CHUNK_WORKERS", "16"))
GEMINI_CONC = int(os.getenv("GEMINI_CONCURRENCY", "3"))
DEEPGRAM_CONC = int(os.getenv("DEEPGRAM_CONCURRENCY", "5"))
EDGE_CONC = int(os.getenv("EDGE_CONCURRENCY", "4"))
LOOKAHEAD = int(os.getenv("CHAPTER_LOOKAHEAD", "4"))  # chapters generated ahead of the uploader

# Hosting (Render sets PORT and RENDER_EXTERNAL_URL automatically)
PUBLIC_URL = (os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "1").strip() != "0"
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_SECRET = (
    os.getenv("WEBHOOK_SECRET") or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:40]
).strip()

ALLOWED = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}