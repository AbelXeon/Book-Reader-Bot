from __future__ import annotations

import asyncio
import io
import logging
import time
import wave
from pathlib import Path

import aiohttp
import edge_tts
from google import genai

import config

log = logging.getLogger("tts")
logging.getLogger("google_genai.models").setLevel(logging.ERROR)  # hide AFC warning spam

_client = genai.Client(api_key=config.GEMINI_API_KEY) if config.GEMINI_API_KEY else None

_blocked_until = {"gemini": 0.0, "deepgram": 0.0}
_penalty = {"gemini": 0}
_GEMINI_BLOCK_STEPS = [0, 60, 120, 300, 600]  # seconds, grows on repeated quota errors

_sems: dict[str, asyncio.Semaphore] = {}


class ProviderUnavailable(RuntimeError):
    """Expected skip (no key / cooling down). Not logged as a warning."""


def _sem(name: str) -> asyncio.Semaphore:
    if name not in _sems:
        limit = {
            "gemini": config.GEMINI_CONC,
            "deepgram": config.DEEPGRAM_CONC,
            "edge": config.EDGE_CONC,
        }[name]
        _sems[name] = asyncio.Semaphore(max(1, limit))
    return _sems[name]


# ---------------- ffmpeg helpers ----------------
async def _ffmpeg(*args: str):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode(errors='ignore')[-300:]}")


async def merge_mp3(files: list[Path], out: Path):
    lst = out.with_suffix(".txt")
    lines = []
    for p in files:
        safe = p.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    lst.write_text("\n".join(lines), encoding="utf-8")
    await _ffmpeg(
        "-f", "concat", "-safe", "0", "-i", str(lst),
        "-ac", "1", "-ar", "24000", "-c:a", "libmp3lame", "-b:a", "64k", str(out),
    )
    lst.unlink(missing_ok=True)


async def split_if_big(path: Path) -> list[Path]:
    if path.stat().st_size <= config.MAX_AUDIO_MB * 1024 * 1024:
        return [path]
    pattern = path.with_name(f"{path.stem}_part%02d.mp3")
    await _ffmpeg("-i", str(path), "-f", "segment", "-segment_time", "1800", "-c", "copy", str(pattern))
    parts = sorted(path.parent.glob(f"{path.stem}_part*.mp3"))
    path.unlink(missing_ok=True)
    return parts


# ---------------- Gemini ----------------
def _as_wav(data: bytes) -> bytes:
    if data[:4] == b"RIFF":
        return data
    buf = io.BytesIO()  # raw 16-bit PCM, 24 kHz, mono
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(data)
    return buf.getvalue()


def _gemini_sync(text: str, style: str) -> bytes:
    part: dict = {"text": text}
    if style:
        part["speech_metadata"] = {"style": style}
    resp = _client.models.generate_content(
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
    if _client is None:
        raise ProviderUnavailable("no GEMINI_API_KEY")
    if time.time() < _blocked_until["gemini"]:
        raise ProviderUnavailable("gemini cooling down")

    async with _sem("gemini"):
        if time.time() < _blocked_until["gemini"]:  # may have been blocked while waiting
            raise ProviderUnavailable("gemini cooling down")
        last: Exception | None = None
        for attempt in range(2):
            try:
                data = await asyncio.to_thread(_gemini_sync, text, style)
                wav = out.with_suffix(".wav")
                wav.write_bytes(_as_wav(data))
                await _ffmpeg("-i", str(wav), "-ac", "1", "-ar", "24000", "-b:a", "64k", str(out))
                wav.unlink(missing_ok=True)
                _penalty["gemini"] = 0
                return
            except Exception as e:
                last = e
                if _is_quota(str(e)):
                    _penalty["gemini"] = min(_penalty["gemini"] + 1, len(_GEMINI_BLOCK_STEPS) - 1)
                    _blocked_until["gemini"] = time.time() + _GEMINI_BLOCK_STEPS[_penalty["gemini"]]
                    break  # don't wait, let the next engine take over
                await asyncio.sleep(2)
        raise RuntimeError(f"Gemini failed: {last}")


# ---------------- Deepgram ----------------
async def deepgram_tts(text: str, out: Path):
    if not config.DEEPGRAM_API_KEY:
        raise ProviderUnavailable("no DEEPGRAM_API_KEY")
    if time.time() < _blocked_until["deepgram"]:
        raise ProviderUnavailable("deepgram cooling down")

    url = f"https://api.deepgram.com/v1/speak?model={config.DEEPGRAM_MODEL}&encoding=mp3"
    headers = {"Authorization": f"Token {config.DEEPGRAM_API_KEY}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=120)

    async with _sem("deepgram"):
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json={"text": text}) as r:
                if r.status != 200:
                    body = (await r.text())[:200]
                    if r.status in (401, 402, 403):  # bad key / no credit
                        _blocked_until["deepgram"] = time.time() + 3600
                    elif r.status == 429:  # concurrency limit
                        _blocked_until["deepgram"] = time.time() + 20
                    raise RuntimeError(f"Deepgram {r.status}: {body}")
                data = await r.read()
    if len(data) < 1000:
        raise RuntimeError("Deepgram returned empty audio")
    out.write_bytes(data)


# ---------------- edge-tts ----------------
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


# ---------------- public API ----------------
CHAINS = {
    "auto": ("gemini", "deepgram", "edge"),
    "fast": ("deepgram", "edge", "gemini"),
    "edge": ("edge", "deepgram", "gemini"),
}


async def speak(text: str, out: Path, engine: str = "auto", style: str = "") -> str:
    """Returns which provider made the audio. Tries every engine, several rounds."""
    chain = CHAINS.get(engine, CHAINS["auto"])
    tmp = out.with_name(out.stem + ".tmp.mp3")  # write to temp, rename when complete
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
                log.debug("skip %s: %s", name, e)
            except Exception as e:
                last = e
                log.warning("%s failed (round %d): %s", name, rnd + 1, e)
            tmp.unlink(missing_ok=True)
            tmp.with_suffix(".wav").unlink(missing_ok=True)
        await asyncio.sleep(min(5 * 2**rnd, 40))  # 5s, 10s, 20s, 40s

    raise RuntimeError(f"All voice engines failed: {last}")