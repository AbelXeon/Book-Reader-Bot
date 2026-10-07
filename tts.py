from __future__ import annotations

import asyncio
import io
import logging
import shutil
import time
import wave
from pathlib import Path

import aiohttp
import edge_tts
from google import genai

import config

log = logging.getLogger("tts")
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


# ----------------- ffmpeg discovery -----------------
def _find_ffmpeg() -> str | None:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:  # bundled binary (useful on Render, which has no ffmpeg)
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = _find_ffmpeg()


# ----------------- Key Managers -----------------
class KeyPool:
    """Rotates through API keys and cools down exhausted ones."""

    def __init__(self, name: str, keys: list[str]):
        self.name = name
        self.keys = keys
        self.blocked_until = {k: 0.0 for k in keys}
        self.penalty = {k: 0 for k in keys}
        self._idx = 0
        self._lock = asyncio.Lock()

    def has_keys(self) -> bool:
        return len(self.keys) > 0

    async def get_active_key(self) -> str | None:
        async with self._lock:
            now = time.time()
            for _ in range(len(self.keys)):
                key = self.keys[self._idx]
                self._idx = (self._idx + 1) % len(self.keys)
                if now >= self.blocked_until[key]:
                    return key
            return None

    def mark_quota_exhausted(self, key: str, backoff_seconds: float):
        if time.time() < self.blocked_until[key]:
            return  # a parallel request already blocked this key
        self.penalty[key] = min(self.penalty[key] + 1, 5)
        cooldown = max(backoff_seconds, 30 * (2 ** (self.penalty[key] - 1)))
        self.blocked_until[key] = time.time() + cooldown
        log.warning(
            "[%s] Key ...%s blocked for %ds due to quota/rate-limit.",
            self.name, key[-6:], int(cooldown),
        )

    def mark_success(self, key: str):
        self.penalty[key] = 0


gemini_pool = KeyPool("Gemini", config.GEMINI_API_KEYS)
deepgram_pool = KeyPool("Deepgram", config.DEEPGRAM_API_KEYS)

_gemini_clients: dict[str, genai.Client] = {
    k: genai.Client(api_key=k) for k in config.GEMINI_API_KEYS
}

_sems: dict[str, asyncio.Semaphore] = {}


class ProviderUnavailable(RuntimeError):
    """Expected skip (no key / all keys cooling down)."""


def _sem(name: str) -> asyncio.Semaphore:
    if name not in _sems:
        limit = {
            "gemini": config.GEMINI_CONC * max(1, len(config.GEMINI_API_KEYS)),
            "deepgram": config.DEEPGRAM_CONC * max(1, len(config.DEEPGRAM_API_KEYS)),
            "edge": config.EDGE_CONC,
        }[name]
        _sems[name] = asyncio.Semaphore(max(1, limit))
    return _sems[name]


# ----------------- shared HTTP session (connection reuse = faster) -----------------
_http: aiohttp.ClientSession | None = None


def _session() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
            connector=aiohttp.TCPConnector(limit=60),
        )
    return _http


async def close_http():
    if _http is not None and not _http.closed:
        await _http.close()


# ----------------- ffmpeg helpers -----------------
async def _ffmpeg(*args: str):
    proc = await asyncio.create_subprocess_exec(
        FFMPEG or "ffmpeg", "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode(errors='ignore')[-300:]}")


async def merge_mp3(files: list[Path], out: Path, copy: bool = False):
    """copy=True joins without re-encoding (instant) - only safe when all files share one format."""
    lst = out.with_suffix(".txt")
    lines = []
    for p in files:
        safe = p.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    lst.write_text("\n".join(lines), encoding="utf-8")
    try:
        if copy:
            try:
                await _ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out))
                return
            except Exception as e:
                log.warning("copy-merge failed, re-encoding instead: %s", e)
        await _ffmpeg(
            "-f", "concat", "-safe", "0", "-i", str(lst),
            "-ac", "1", "-ar", "24000", "-c:a", "libmp3lame", "-b:a", config.AUDIO_BITRATE, str(out),
        )
    finally:
        lst.unlink(missing_ok=True)


async def split_if_big(path: Path) -> list[Path]:
    if path.stat().st_size <= config.MAX_AUDIO_MB * 1024 * 1024:
        return [path]
    pattern = path.with_name(f"{path.stem}_part%02d.mp3")
    await _ffmpeg("-i", str(path), "-f", "segment", "-segment_time", "1800", "-c", "copy", str(pattern))
    parts = sorted(path.parent.glob(f"{path.stem}_part*.mp3"))
    path.unlink(missing_ok=True)
    return parts


# ----------------- Gemini -----------------
def _as_wav(data: bytes) -> bytes:
    if data[:4] == b"RIFF":
        return data
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(data)
    return buf.getvalue()


def _gemini_sync(client: genai.Client, text: str, style: str) -> bytes:
    part: dict = {"text": text}
    if style:
        part["speech_metadata"] = {"style": style}
    resp = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[{"role": "user", "parts": [part]}],
        config={
            "response_modalities": ["AUDIO"],
            "speech_config": {"voice_config": {"voice": config.GEMINI_VOICE}},
        },
    )
    data = resp.candidates[0].content.parts[0].inline_data.data
    if not data:
        raise RuntimeError("Gemini returned empty audio")
    return data


def _is_quota(msg: str) -> bool:
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()


async def gemini_tts(text: str, out: Path, style: str):
    if not gemini_pool.has_keys():
        raise ProviderUnavailable("no GEMINI_API_KEYS configured")

    async with _sem("gemini"):
        for _ in range(len(gemini_pool.keys)):
            key = await gemini_pool.get_active_key()
            if not key:
                break
            client = _gemini_clients[key]
            try:
                data = await asyncio.to_thread(_gemini_sync, client, text, style)
                wav = out.with_suffix(".wav")
                wav.write_bytes(_as_wav(data))
                await _ffmpeg("-i", str(wav), "-ac", "1", "-ar", "24000", "-b:a", config.AUDIO_BITRATE, str(out))
                wav.unlink(missing_ok=True)
                gemini_pool.mark_success(key)
                return
            except Exception as e:
                err_str = str(e)
                if _is_quota(err_str):
                    gemini_pool.mark_quota_exhausted(key, backoff_seconds=60)
                    continue  # immediately try next key in pool
                log.warning("Gemini key ...%s failed: %s", key[-6:], err_str)
                await asyncio.sleep(1)

        raise ProviderUnavailable("all Gemini keys exhausted/cooling down")


# ----------------- Deepgram -----------------
async def deepgram_tts(text: str, out: Path):
    if not deepgram_pool.has_keys():
        raise ProviderUnavailable("no DEEPGRAM_API_KEYS configured")

    async with _sem("deepgram"):
        for _ in range(len(deepgram_pool.keys)):
            key = await deepgram_pool.get_active_key()
            if not key:
                break

            url = f"https://api.deepgram.com/v1/speak?model={config.DEEPGRAM_MODEL}&encoding=mp3"
            headers = {"Authorization": f"Token {key}", "Content-Type": "application/json"}

            try:
                async with _session().post(url, headers=headers, json={"text": text}) as r:
                    if r.status != 200:
                        body = (await r.text())[:200]
                        if r.status in (401, 402, 403):
                            deepgram_pool.mark_quota_exhausted(key, backoff_seconds=3600)
                        elif r.status == 429:
                            deepgram_pool.mark_quota_exhausted(key, backoff_seconds=30)
                        raise RuntimeError(f"Deepgram HTTP {r.status}: {body}")
                    data = await r.read()

                if len(data) < 1000:
                    raise RuntimeError("Deepgram returned empty audio")
                out.write_bytes(data)
                deepgram_pool.mark_success(key)
                return
            except Exception as e:
                log.warning("Deepgram key ...%s error: %s", key[-6:], e)
                continue

        raise ProviderUnavailable("all Deepgram keys exhausted/cooling down")


# ----------------- edge-tts -----------------
async def edge_tts_to_mp3(text: str, out: Path):
    async with _sem("edge"):
        last: Exception | None = None
        for attempt in range(2):
            try:
                await edge_tts.Communicate(text, config.EDGE_VOICE).save(str(out))
                if out.exists() and out.stat().st_size > 1000:
                    return
                raise RuntimeError("edge-tts produced an empty file")
            except Exception as e:
                last = e
                out.unlink(missing_ok=True)
                await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError(f"edge-tts failed: {last}")


# ----------------- Public API -----------------
CHAINS = {
    "auto": ("gemini", "deepgram", "edge"),
    "fast": ("deepgram", "edge", "gemini"),
    "edge": ("edge", "deepgram", "gemini"),
}


async def speak(text: str, out: Path, engine: str = "auto", style: str = "") -> str:
    """Rotates keys within each provider before cascading down the chain."""
    chain = CHAINS.get(engine, CHAINS["auto"])
    tmp = out.with_name(out.stem + ".tmp.mp3")
    last: Exception | None = None

    for rnd in range(4):
        for name in chain:
            try:
                if name == "gemini":
                    await gemini_tts(text, tmp, style)
                elif name == "deepgram":
                    await deepgram_tts(text, tmp)
                else:
                    await edge_tts_to_mp3(text, tmp)

                if not (tmp.exists() and tmp.stat().st_size > 1000):
                    raise RuntimeError("empty audio file")
                tmp.replace(out)
                return name
            except ProviderUnavailable as e:
                log.debug("Skip provider %s: %s", name, e)
            except Exception as e:
                last = e
                log.warning("%s failed (round %d): %s", name, rnd + 1, e)
            tmp.unlink(missing_ok=True)
            tmp.with_suffix(".wav").unlink(missing_ok=True)

        await asyncio.sleep(min(5 * 2**rnd, 40))

    raise RuntimeError(f"All voice engines failed: {last}")