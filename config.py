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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts").strip()
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Kore").strip()
GEMINI_STYLE = os.getenv("GEMINI_STYLE", "calm, clear audiobook narrator").strip()

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "").strip()
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "aura-2-thalia-en").strip()

EDGE_VOICE = os.getenv("EDGE_VOICE", "en-US-AriaNeural").strip()

# Deepgram accepts max 2000 chars per request, so chunks stay under that.
CHUNK_CHARS = min(int(os.getenv("CHUNK_CHARS", "1800")), 1900)
MAX_AUDIO_MB = 48  # Telegram bots can send up to 50 MB

# parallelism
CHUNK_WORKERS = int(os.getenv("CHUNK_WORKERS", "6"))
GEMINI_CONC = int(os.getenv("GEMINI_CONCURRENCY", "3"))
DEEPGRAM_CONC = int(os.getenv("DEEPGRAM_CONCURRENCY", "5"))
EDGE_CONC = int(os.getenv("EDGE_CONCURRENCY", "4"))

ALLOWED = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}