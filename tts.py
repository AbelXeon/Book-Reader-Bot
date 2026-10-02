from __future__ import annotations

import asyncio
import io
import logging
import time
import wave
from pathlib import Path

import edge_tts
from google import genai

import config

log = logging.getLogger("tts")

_client = genai.Client(api_key=config.GEMINI_API_KEY) if config.GEMINI_API_KEY else None
_blocked_until = 0.0  # cooldown after Gemini quota errors


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


def _gemini_sync(text: str) -> bytes:
    part: dict = {"text": text}
    if config.GEMINI_STYLE:
        part["speech_metadata"] = {"style": config.GEMINI_STYLE}
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


async def gemini_tts(text: str, out: Path):
    global _blocked_until
    if _client is None:
        raise RuntimeError("No GEMINI_API_KEY set")
    if time.time() < _blocked_until:
        raise RuntimeError("Gemini cooling down after quota error")

    last: Exception | None = None
    for attempt in range(2):
        try:
            data = await asyncio.to_thread(_gemini_sync, text)
            wav = out.with_suffix(".wav")
            wav.write_bytes(_as_wav(data))
            await _ffmpeg("-i", str(wav), "-ac", "1", "-ar", "24000", "-b:a", "64k", str(out))
            wav.unlink(missing_ok=True)
            return
        except Exception as e:
            last = e
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
                if attempt == 0:
                    await asyncio.sleep(25)
                    continue
                _blocked_until = time.time() + 600
                break
            await asyncio.sleep(2)
    raise RuntimeError(f"Gemini failed: {last}")


# ---------------- edge-tts ----------------
async def edge_tts_to_mp3(text: str, out: Path):
    last: Exception | None = None
    for attempt in range(3):
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
async def speak(text: str, out: Path, engine: str = "auto") -> str:
    """Returns which provider made the audio: 'gemini' or 'edge'."""
    if engine != "edge":
        try:
            await gemini_tts(text, out)
            return "gemini"
        except Exception as e:
            log.warning("Gemini failed, falling back to edge-tts: %s", e)
            out.unlink(missing_ok=True)
    await edge_tts_to_mp3(text, out)
    return "edge"