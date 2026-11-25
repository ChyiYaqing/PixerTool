#!/usr/bin/env python3
"""Make a daily English card from TV subtitles, as a PNG for pixer_upload.py.

  extract SUBTITLES.srt          list candidate lines to curate into a pool
  card POOL.json -o card.png     render today's card: one line a day, no repeats
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import random
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from string import Template

WIDTH, HEIGHT = 1872, 1404
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
RENDER_TIMEOUT = 60.0
DEFAULT_STATE = Path.home() / ".pixer_english_state.json"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"
# A trailing "..." means the line trails off, so it is not a complete sentence.
SENTENCE_END = re.compile(r'(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bDr)(?<!\.)[.!?]["”]?$')
# A sentence ends at . ! or ?, perhaps inside a closing quote, when the next word
# starts a new sentence; a lowercase word means the sentence carries on.
SENTENCE_BREAK = re.compile(
    r'(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bDr\.)(?:(?<=[.!?])|(?<=[.!?]["”]))\s+(?=[A-Z0-9"“]|[\'‘][A-Z])'
)
WEAK_STARTS = ("and ", "but ", "so ", "or ", "because ", "that ", "which ", "just ")
FILLERS = (" like,", " kind of ", " sort of ", " you know", " um,", " uh,")

PAGE = Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><style>
  html, body { margin: 0; padding: 0; }
  body { width: 1872px; height: 1404px; background: #fff; color: #111;
         font-family: "Helvetica Neue", -apple-system, "PingFang SC", sans-serif;
         display: flex; flex-direction: column; box-sizing: border-box; padding: 110px 140px; }
  .top { display: flex; justify-content: space-between; align-items: baseline;
         font-size: 40px; letter-spacing: 2px; color: #555; text-transform: uppercase; }
  .top .rule { flex: 1; border-bottom: 3px solid #222; margin: 0 40px 14px; }
  .mid { flex: 1; text-align: center; display: flex; flex-direction: column; justify-content: center; }
  .quote { font-family: Georgia, "Times New Roman", serif; font-size: ${size}px;
           line-height: 1.28; font-weight: 500; text-wrap: balance; }
  .quote .q { color: #888; }
  .nowrap { white-space: nowrap; }
  .gloss { margin-top: 70px; font-size: 52px; line-height: 1.5; color: #222; }
  .gloss b { font-family: Georgia, serif; }
  .gloss .cn { color: #555; }
  .bottom { display: flex; justify-content: space-between; align-items: center;
            font-size: 40px; color: #555; }
  .src { font-style: italic; }
</style></head><body>
  <div class="top"><span>每日英语 · Daily English</span><span class="rule"></span><span>${date}</span></div>
  <div class="mid">
    <div class="quote"><span class="q">“</span>${quote}<span class="q">”</span></div>
    <div class="gloss"><b>${term}</b> &nbsp;<span class="cn">${cn}</span></div>
  </div>
  <div class="bottom"><span class="src">— ${source}</span><span>G+ Pixer</span></div>
</body></html>
""")


def parse_srt(text: str) -> list[str]:
    """Return the dialogue as sentences, joining sentences that run across cues."""
    chunks: list[str] = []
    current: list[str] = []
    for block in re.split(r"\n\s*\n", text.lstrip("﻿").replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        for line in lines[2:]:
            line = re.sub(r"\s+", " ", re.sub(r"<[^>]+>|\{[^}]*\}", "", line)).strip()
            if line.startswith("-"):  # a new speaker
                if current:
                    chunks.append(" ".join(current))
                    current = []
                line = line.lstrip("- ")
            if line:
                current.append(line)
    if current:
        chunks.append(" ".join(current))
    return [s for chunk in chunks for s in SENTENCE_BREAK.split(chunk) if s]


def teachable_words(sentence: str) -> list[str]:
    """Long words worth a gloss, skipping names and contractions."""
    found: list[str] = []
    for index, raw in enumerate(sentence.split()):
        word = raw.strip(".,!?;:\"()")
        if len(word) < 8 or not re.fullmatch(r"[A-Za-z]+(-[A-Za-z]+)*", word):
            continue
        if index > 0 and word[0].isupper():
            continue
        if word.lower() not in (w.lower() for w in found):
            found.append(word)
    return found


def candidates(sentences: list[str]) -> list[tuple[str, str]]:
    """Complete, mid-length sentences with exactly one word worth teaching."""
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for sentence in sentences:
        if not 7 <= len(sentence.split()) <= 14 or sentence in seen:
            continue
        if not sentence[0].isupper() or not SENTENCE_END.search(sentence):
            continue
        lowered = " " + sentence.lower()
        if sentence.count('"') % 2 or lowered[1:].startswith(WEAK_STARTS):
            continue
        if sum(lowered.count(filler) for filler in FILLERS) >= 2:
            continue
        words = teachable_words(sentence)
        if len(words) == 1:
            seen.add(sentence)
            result.append((sentence, words[0]))
    return result


def load_pool(path: Path) -> tuple[str, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("source"), str) or not cards \
            or not isinstance(cards, list):
        raise ValueError(f"{path} needs a \"source\" and a list of cards")
    quotes: set[str] = set()
    for number, card in enumerate(cards, 1):
        if not isinstance(card, dict) or not all(
            isinstance(card.get(key), str) and card[key] for key in ("quote", "term", "cn")
        ):
            raise ValueError(f"card {number} in {path} needs quote, term and cn")
        if card["quote"] in quotes:
            raise ValueError(f"card {number} in {path} repeats an earlier quote")
        quotes.add(card["quote"])
    return data["source"], cards


def pick_card(cards: list[dict], state: dict, today: str, rng: random.Random) -> tuple[dict, dict]:
    """Keep one card all day; show every card once before any repeats."""
    by_quote = {card["quote"]: card for card in cards}
    if state.get("date") == today and state.get("quote") in by_quote:
        return by_quote[state["quote"]], state
    shown = [quote for quote in state.get("shown", []) if quote in by_quote]
    fresh = [card for card in cards if card["quote"] not in shown]
    if not fresh:
        shown = []
        fresh = [card for card in cards if card["quote"] != state.get("quote")] or list(cards)
    card = rng.choice(fresh)
    return card, {"date": today, "quote": card["quote"], "shown": shown + [card["quote"]]}


def card_page(card: dict, source: str, day: dt.date) -> str:
    quote = card["quote"].replace("'", "’")
    # Keep hyphenated words like "11-year-old" on one line.
    markup = re.sub(r"\S+-\S+", r'<span class="nowrap">\g<0></span>', html.escape(quote))
    return PAGE.substitute(
        size=132 if len(quote) <= 60 else 112,
        date=day.strftime("%b %d").upper(),
        quote=markup,
        term=html.escape(card["term"]),
        cn=html.escape(card["cn"]),
        source=html.escape(source),
    )


def render(page: str, output: Path, chrome: str) -> None:
    with tempfile.TemporaryDirectory(prefix="pixer-english-") as folder:
        source = Path(folder) / "card.html"
        shot = Path(folder) / "card.png"
        source.write_text(page, encoding="utf-8")
        command = [
            chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1", f"--user-data-dir={Path(folder) / 'profile'}",
            f"--window-size={WIDTH},{HEIGHT}", "--default-background-color=FFFFFFFF",
            f"--screenshot={shot}", source.as_uri(),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=RENDER_TIMEOUT)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Chrome not found at {chrome}; pass --chrome") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Chrome did not finish within {RENDER_TIMEOUT:g} seconds") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Chrome failed: {exc.stderr.decode(errors='replace').strip()}") from exc
        image = shot.read_bytes() if shot.is_file() else b""
        if not image:
            raise RuntimeError("Chrome did not write a screenshot")
        # Check the size, and that the file ends with IEND, so a cut-off file fails too.
        if len(image) < 24 or image[:8] != PNG_SIGNATURE or image[12:16] != b"IHDR" \
                or struct.unpack(">II", image[16:24]) != (WIDTH, HEIGHT) or not image.endswith(PNG_END):
            raise RuntimeError(f"Chrome did not write a {WIDTH}x{HEIGHT} PNG")
        output.write_bytes(image)


def save_state(path: Path, state: dict) -> None:
    """Replace the state file atomically, through a temporary file of this run's own."""
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract", help="list candidate lines from an .srt file")
    extract.add_argument("subtitles", type=Path)
    card = commands.add_parser("card", help="render today's card from a pool")
    card.add_argument("pool", type=Path)
    card.add_argument("-o", "--output", type=Path, default=Path("daily-english.png"))
    card.add_argument("--state", type=Path, default=DEFAULT_STATE,
                      help=f"remembers which lines have shown (default: {DEFAULT_STATE})")
    card.add_argument("--date", type=dt.date.fromisoformat, default=None,
                      help="pretend today is YYYY-MM-DD")
    card.add_argument("--chrome", default=CHROME, help="Chrome or Chromium binary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "extract":
            text = args.subtitles.read_text(encoding="utf-8", errors="replace")
            for sentence, word in candidates(parse_srt(text)):
                print(f"{word}\t{sentence}")
            return 0
        source, cards = load_pool(args.pool)
        day = args.date or dt.date.today()
        state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.is_file() else {}
        card, state = pick_card(cards, state, day.isoformat(), random.Random())
        render(card_page(card, source, day), args.output, args.chrome)
        save_state(args.state, state)
        print(f"{args.output}: {card['quote']}")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
