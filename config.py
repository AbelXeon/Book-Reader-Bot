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
EDGE_VOICE = os.getenv("EDGE_VOICE", "en-US-AriaNeural").strip()

CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "2500"))
MAX_AUDIO_MB = 48  # Telegram bots can send up to 50 MB

ALLOWED = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}