from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF


class NoTextError(Exception):
    """PDF has no extractable text (probably scanned)."""


@dataclass
class Chapter:
    title: str
    text: str


@dataclass
class ParsedBook:
    title: str
    chapters: list[Chapter]
    pages: int
    chars: int
    method: str


# ---------------- header / footer cleaning ----------------
PAGE_NUM = re.compile(
    r"^[\s\-–—\[\(]*(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?[\s\-–—\]\)]*$", re.I
)


def _norm(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip().lower())


def strip_headers_footers(pages: list[list[str]]) -> list[list[str]]:
    n = len(pages)
    repeated: set[str] = set()
    if n >= 6:
        counter: Counter = Counter()
        for lines in pages:
            nonempty = [l for l in lines if l.strip()]
            edge = nonempty[:3] + nonempty[-3:]
            for norm in {_norm(l) for l in edge}:
                counter[norm] += 1
        threshold = max(3, int(n * 0.35))
        repeated = {k for k, c in counter.items() if c >= threshold and k}

    cleaned = []
    for lines in pages:
        idxs = [i for i, l in enumerate(lines) if l.strip()]
        edge_idx = set(idxs[:3] + idxs[-3:])
        kept = []
        for i, l in enumerate(lines):
            if not l.strip() or PAGE_NUM.match(l):
                continue
            if i in edge_idx and _norm(l) in repeated:
                continue
            kept.append(l.strip())
        cleaned.append(kept)
    return cleaned


# ---------- text cleaning ----------
def clean_text(t: str) -> str:
    t = unicodedata.normalize("NFKC", t)
    t = t.replace("\u00ad", "")
    t = re.sub(r"[•●▪■◆►▶]", " ", t)
    t = re.sub(r"(\w)-\n(?=[a-z])", r"\1", t)  # re-join hyphenated words
    t = re.sub(r"(?<!\n)\n(?!\n)", " ", t)  # hard-wrapped lines -> spaces
    t = re.sub(r"\s+", " ", t)
    return t.strip()


# ---------- chunking ----------
_SENT = re.compile(r"(?<=[.!?…])\s+")


def split_text(text: str, max_chars: int = 2500) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for s in _SENT.split(text):
        s = s.strip()
        if not s:
            continue
        while len(s) > max_chars:  # very long sentence: hard split
            cut = s.rfind(" ", 0, max_chars)
            if cut < max_chars * 0.5:
                cut = max_chars
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(s[:cut].strip())
            s = s[cut:].strip()
        if len(cur) + len(s) + 1 <= max_chars:
            cur = f"{cur} {s}".strip()
        else:
            if cur:
                chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return [c for c in chunks if re.search(r"\w", c)]


# ---------------- chapter detection ----------------
CH_RE = re.compile(
    r"^\s*(chapter|unit|lesson|module)\s+"
    r"(\d{1,3}|[ivxlc]{1,6}|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
    r"\b[\s:.\-–—]*(.{0,80})$",
    re.I,
)


def _toc_starts(toc, n: int) -> list[tuple[str, int]]:
    if not toc:
        return []
    for lvl in sorted({t[0] for t in toc}):
        ents, seen = [], set()
        for l, title, page in toc:
            if l != lvl or page < 1:
                continue
            p = min(page - 1, n - 1)
            if p in seen:
                continue
            seen.add(p)
            ents.append((title.strip() or "Untitled", p))
        if 3 <= len(ents) <= 150:
            return sorted(ents, key=lambda e: e[1])
    return []


def _regex_starts(raw_pages: list[list[str]]) -> list[tuple[str, int]]:
    starts, last_key = [], None
    for i, lines in enumerate(raw_pages):
        head = [l for l in lines if l.strip()][:5]
        for l in head:
            m = CH_RE.match(l)
            if not m:
                continue
            key = f"{m.group(1).lower()} {m.group(2).lower()}"
            if key != last_key:  # running headers repeat the same key every page
                rest = m.group(3).strip()
                title = f"{m.group(1).title()} {m.group(2)}" + (f": {rest}" if rest else "")
                starts.append((title, i))
                last_key = key
            break
    return starts


def _build(starts, page_texts, n) -> list[Chapter]:
    chapters: list[Chapter] = []
    if starts and starts[0][1] > 0:
        front = clean_text("\n".join(page_texts[: starts[0][1]]))
        if len(front) >= 1500:
            chapters.append(Chapter("Introduction", front))
    for i, (title, start) in enumerate(starts):
        nxt = starts[i + 1][1] if i + 1 < len(starts) else n
        end = nxt - 1 if nxt > start else start
        chapters.append(Chapter(title, clean_text("\n".join(page_texts[start : end + 1]))))
    return chapters


def _merge_tiny(chs: list[Chapter], min_chars: int = 400) -> list[Chapter]:
    out: list[Chapter] = []
    carry = ""
    for c in chs:
        text = f"{carry} {c.text}".strip() if carry else c.text
        carry = ""
        if len(text) < min_chars:
            carry = text
            continue
        out.append(Chapter(c.title, text))
    if carry:
        if out:
            out[-1].text += " " + carry
        else:
            out.append(Chapter("Full text", carry))
    return out


# ---------------- main entry ----------------
def parse_pdf(path: Path, fallback_title: str = "Book") -> ParsedBook:
    doc = fitz.open(path)
    try:
        n = len(doc)
        raw_pages = [p.get_text("text").splitlines() for p in doc]
        toc = doc.get_toc(simple=True)
        meta_title = ((doc.metadata or {}).get("title") or "").strip()
    finally:
        doc.close()

    pages = strip_headers_footers(raw_pages)
    page_texts = ["\n".join(p) for p in pages]
    total = sum(len(t) for t in page_texts)
    if n == 0 or total < max(200, 40 * n):
        raise NoTextError()

    method = "bookmarks"
    starts = _toc_starts(toc, n)
    if not starts:
        method = "chapter headings"
        starts = _regex_starts(raw_pages)
        if len(starts) < 3:
            starts = []
    if not starts:
        method = "page groups"
        step = 12
        starts = [(f"Part {i // step + 1}", i) for i in range(0, n, step)]

    chapters = _merge_tiny(_build(starts, page_texts, n))
    chapters = [c for c in chapters if len(c.text) >= 50]
    if not chapters:
        raise NoTextError()

    for c in chapters:  # speak the chapter title first
        c.title = c.title[:100]
        c.text = f"{c.title}. {c.text}"

    return ParsedBook(
        title=meta_title or fallback_title,
        chapters=chapters,
        pages=n,
        chars=sum(len(c.text) for c in chapters),
        method=method,
    )