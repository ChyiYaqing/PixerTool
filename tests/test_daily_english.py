import contextlib
import datetime as dt
import importlib.util
import io
import json
import random
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "misc" / "daily_english.py"
SPEC = importlib.util.spec_from_file_location("daily_english", MODULE_PATH)
daily_english = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daily_english)

SRT = """﻿1
00:00:01,000 --> 00:00:02,000
<i>It was a family,
you know,</i>

2
00:00:02,500 --> 00:00:04,000
in all but name.

3
00:00:05,000 --> 00:00:06,000
- Hello.
- I wondered when I'd be seeing you, Mr.

4
00:00:06,500 --> 00:00:07,000
Potter. Welcome back!
"""

POOL = {
    "source": "Show (2022)",
    "cards": [
        {"quote": "First line here.", "term": "first", "cn": "第一"},
        {"quote": "Second line here.", "term": "second", "cn": "第二"},
        {"quote": "Third line here.", "term": "third", "cn": "第三"},
    ],
}

def png(width, height):
    """A complete, decodable white grayscale PNG."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = (b"\x00" + b"\xff" * width) * height
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


VALID_PNG = png(1872, 1404)


def fake_chrome(folder, image=VALID_PNG, name="chrome"):
    """An executable that writes IMAGE wherever Chrome would save its screenshot."""
    path = Path(folder) / name
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "for arg in sys.argv[1:]:\n"
        "    if arg.startswith('--screenshot='):\n"
        f"        open(arg.split('=', 1)[1], 'wb').write(bytes.fromhex('{image.hex()}'))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def failing_chrome(folder):
    path = Path(folder) / "failing"
    path.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('boom')\nsys.exit(3)\n",
                    encoding="utf-8")
    path.chmod(0o755)
    return str(path)


class ParseTests(unittest.TestCase):
    def test_joins_lines_and_cues_into_sentences(self):
        sentences = daily_english.parse_srt(SRT.replace("\n", "\r\n"))
        self.assertEqual(
            sentences,
            [
                "It was a family, you know, in all but name.",
                "Hello.",
                "I wondered when I'd be seeing you, Mr. Potter.",
                "Welcome back!",
            ],
        )

    def test_new_speaker_ends_an_unfinished_line(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\n- Wait\n- What?\n"
        self.assertEqual(daily_english.parse_srt(srt), ["Wait", "What?"])

    def test_splits_after_a_closing_quote(self):
        straight = '1\n00:00:01,000 --> 00:00:02,000\nHe called it "extraordinary." We were amazed.\n'
        curly = ("1\n00:00:01,000 --> 00:00:02,000\nHe called it “extraordinary.”\n\n"
                 "2\n00:00:02,000 --> 00:00:03,000\nWe were amazed.\n")
        self.assertEqual(daily_english.parse_srt(straight),
                         ['He called it "extraordinary."', "We were amazed."])
        self.assertEqual(daily_english.parse_srt(curly),
                         ["He called it “extraordinary.”", "We were amazed."])

    def test_quote_inside_a_sentence_does_not_end_it(self):
        line = "I was absolutely sure when she asked “Why?” and then she left."
        one_cue = f"1\n00:00:01,000 --> 00:00:02,000\n{line}\n"
        two_cues = ("1\n00:00:01,000 --> 00:00:02,000\nI was absolutely sure when she asked “Why?”\n\n"
                    "2\n00:00:02,000 --> 00:00:03,000\nand then she left.\n")
        self.assertEqual(daily_english.parse_srt(one_cue), [line])
        self.assertEqual(daily_english.parse_srt(two_cues), [line])

    def test_apostrophe_word_can_start_a_sentence(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\nI'm like my character. 'Cause I live in a burrow.\n"
        self.assertEqual(daily_english.parse_srt(srt),
                         ["I'm like my character.", "'Cause I live in a burrow."])

    def test_trailing_dots_carry_on_into_lowercase(self):
        srt = ("1\n00:00:01,000 --> 00:00:02,000\nWhat someone is born...\n\n"
               "2\n00:00:02,000 --> 00:00:03,000\nbut what they grow to be.\n")
        self.assertEqual(daily_english.parse_srt(srt), ["What someone is born... but what they grow to be."])

    def test_skips_blocks_without_a_timestamp(self):
        srt = "WEBVTT\n\nnot a cue\nstill not\n\n1\n00:00:01,000 --> 00:00:02,000\nHi.\n"
        self.assertEqual(daily_english.parse_srt(srt), ["Hi."])


class CandidateTests(unittest.TestCase):
    def test_teachable_words_skip_names_contractions_and_short_words(self):
        words = daily_english.teachable_words(
            "Bellatrix wasn't uncontrollable, said Hermione, but self-conscious and shy."
        )
        self.assertEqual(words, ["Bellatrix", "uncontrollable", "self-conscious"])

    def test_keeps_complete_lines_with_one_teachable_word(self):
        sentences = [
            "I was a little intimidated by him.",
            "I was a little intimidated by him.",
            "Short and intimidated.",
            "And then I was a little intimidated by him.",
            "I was, like, kind of a little intimidated by him.",
            "I was a little intimidated and devastated by him.",
            "I was a little scared by him today.",
            "I was a little intimidated by him",
        ]
        self.assertEqual(
            daily_english.candidates(sentences),
            [("I was a little intimidated by him.", "intimidated")],
        )


class PoolTests(unittest.TestCase):
    def write(self, folder, data):
        path = Path(folder) / "pool.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_loads_source_and_cards(self):
        with tempfile.TemporaryDirectory() as folder:
            source, cards = daily_english.load_pool(self.write(folder, POOL))
        self.assertEqual(source, "Show (2022)")
        self.assertEqual(len(cards), 3)

    def test_rejects_incomplete_pools(self):
        broken = [
            {"source": "Show", "cards": []},
            {"cards": POOL["cards"]},
            {"source": "Show", "cards": [{"quote": "Hi.", "term": "hi"}]},
            {"source": "Show", "cards": [{"quote": "Hi.", "term": "", "cn": "嗨"}]},
            {"source": "Show", "cards": "Hi."},
            {"source": "Show", "cards": ["Hi."]},
            {"source": "Show", "cards": [POOL["cards"][0], dict(POOL["cards"][0], term="again")]},
            POOL["cards"],
        ]
        with tempfile.TemporaryDirectory() as folder:
            for data in broken:
                with self.subTest(data=data), self.assertRaises(ValueError):
                    daily_english.load_pool(self.write(folder, data))

    def test_example_pool_loads(self):
        pool = Path(__file__).parents[1] / "misc" / "english" / "example.json"
        _, cards = daily_english.load_pool(pool)
        self.assertEqual(len(cards), 3)


class PickTests(unittest.TestCase):
    cards = POOL["cards"]

    def test_same_card_all_day(self):
        card, state = daily_english.pick_card(self.cards, {}, "2026-11-25", random.Random(1))
        again, same_state = daily_english.pick_card(self.cards, state, "2026-11-25", random.Random(2))
        self.assertEqual(card, again)
        self.assertEqual(state, same_state)

    def test_shows_every_card_before_repeating(self):
        state, seen = {}, []
        for day in range(1, 4):
            card, state = daily_english.pick_card(self.cards, state, f"2026-12-0{day}", random.Random(day))
            seen.append(card["quote"])
        self.assertCountEqual(seen, [card["quote"] for card in self.cards])

    def test_new_round_does_not_repeat_yesterday(self):
        state = {"date": "2026-12-03", "quote": "Third line here.",
                 "shown": [card["quote"] for card in self.cards]}
        for seed in range(20):
            card, new_state = daily_english.pick_card(self.cards, state, "2026-12-04", random.Random(seed))
            self.assertNotEqual(card["quote"], "Third line here.")
            self.assertEqual(new_state["shown"], [card["quote"]])

    def test_forgets_lines_removed_from_the_pool(self):
        state = {"date": "2026-12-01", "quote": "Gone.", "shown": ["Gone.", "First line here."]}
        card, new_state = daily_english.pick_card(self.cards, state, "2026-12-01", random.Random(0))
        self.assertNotEqual(card["quote"], "First line here.")
        self.assertEqual(new_state["shown"], ["First line here.", card["quote"]])

    def test_single_card_pool_repeats(self):
        state = {"date": "2026-12-01", "quote": "First line here.", "shown": ["First line here."]}
        card, _ = daily_english.pick_card(self.cards[:1], state, "2026-12-02", random.Random(0))
        self.assertEqual(card["quote"], "First line here.")


class PageTests(unittest.TestCase):
    def test_escapes_text_and_formats_date(self):
        card = {"quote": "Don't <b>stop</b> & go.", "term": "a<b", "cn": "中&文"}
        page = daily_english.card_page(card, "Show <1>", dt.date(2026, 11, 5))
        self.assertIn("Don’t &lt;b&gt;stop&lt;/b&gt; &amp; go.", page)
        self.assertIn("<b>a&lt;b</b>", page)
        self.assertIn("中&amp;文", page)
        self.assertIn("— Show &lt;1&gt;", page)
        self.assertIn("NOV 05", page)

    def test_keeps_hyphenated_words_together(self):
        card = {"quote": "A devilish 11-year-old boy.", "term": "devilish", "cn": "调皮的"}
        page = daily_english.card_page(card, "Show", dt.date(2026, 11, 5))
        self.assertIn('<span class="nowrap">11-year-old</span>', page)

    def test_long_quotes_use_smaller_type(self):
        short = daily_english.card_page({"quote": "Hi.", "term": "hi", "cn": "嗨"}, "S", dt.date.today())
        long = daily_english.card_page({"quote": "x" * 61, "term": "x", "cn": "x"}, "S", dt.date.today())
        self.assertIn("font-size: 132px", short)
        self.assertIn("font-size: 112px", long)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def test_writes_the_screenshot(self):
        output = self.root / "card.png"
        daily_english.render("<p>hi</p>", output, fake_chrome(self.root))
        self.assertEqual(output.read_bytes(), VALID_PNG)

    def test_reports_chrome_problems(self):
        cases = [
            (str(self.root / "missing"), "not found"),
            (failing_chrome(self.root), "boom"),
            (fake_chrome(self.root, b"", "empty"), "did not write"),
            (fake_chrome(self.root, b"<html>", "html"), "1872x1404 PNG"),
            (fake_chrome(self.root, VALID_PNG[:20], "short"), "1872x1404 PNG"),
            (fake_chrome(self.root, VALID_PNG[:-20], "cut"), "1872x1404 PNG"),
            (fake_chrome(self.root, png(3744, 2808), "retina"), "1872x1404 PNG"),
        ]
        output = self.root / "card.png"
        for chrome, message in cases:
            with self.subTest(chrome=chrome), self.assertRaisesRegex(RuntimeError, message):
                daily_english.render("<p>hi</p>", output, chrome)
        self.assertFalse(output.exists())


class StateTests(unittest.TestCase):
    def test_save_replaces_the_file_and_leaves_no_temporaries(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            (Path(folder) / "state.json.tmp").write_text("left by an old run", encoding="utf-8")
            daily_english.save_state(path, {"quote": "One."})
            daily_english.save_state(path, {"quote": "Two."})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"quote": "Two."})
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()),
                             ["state.json", "state.json.tmp"])


class MainTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.pool = self.root / "pool.json"
        self.pool.write_text(json.dumps(POOL), encoding="utf-8")
        self.state = self.root / "state.json"

    def run_card(self, chrome, date="2026-11-25"):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return daily_english.main([
                "card", str(self.pool), "-o", str(self.root / "out.png"),
                "--state", str(self.state), "--date", date, "--chrome", chrome,
            ])

    def test_card_renders_and_remembers_the_line(self):
        self.assertEqual(self.run_card(fake_chrome(self.root)), 0)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["date"], "2026-11-25")
        self.assertEqual(state["shown"], [state["quote"]])
        self.assertEqual((self.root / "out.png").read_bytes(), VALID_PNG)

    def test_rerun_on_the_same_day_keeps_the_card(self):
        chrome = fake_chrome(self.root)
        self.assertEqual(self.run_card(chrome), 0)
        first = self.state.read_bytes()
        for _ in range(5):
            self.assertEqual(self.run_card(chrome), 0)
            self.assertEqual(self.state.read_bytes(), first)

    def test_failed_render_leaves_state_alone(self):
        self.assertEqual(self.run_card(failing_chrome(self.root)), 1)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.run_card(fake_chrome(self.root), "2026-11-25"), 0)
        before = self.state.read_bytes()
        self.assertEqual(self.run_card(failing_chrome(self.root), "2026-11-26"), 1)
        self.assertEqual(self.state.read_bytes(), before)

    def test_cut_off_screenshot_leaves_output_and_state_alone(self):
        self.assertEqual(self.run_card(fake_chrome(self.root), "2026-11-25"), 0)
        state, image = self.state.read_bytes(), (self.root / "out.png").read_bytes()
        cut = fake_chrome(self.root, VALID_PNG[:-20], "cut")
        self.assertEqual(self.run_card(cut, "2026-11-26"), 1)
        self.assertEqual(self.state.read_bytes(), state)
        self.assertEqual((self.root / "out.png").read_bytes(), image)

    def test_extract_prints_word_and_line(self):
        srt = self.root / "show.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nI was a little intimidated by him.\n",
                       encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(daily_english.main(["extract", str(srt)]), 0)
        self.assertEqual(out.getvalue(), "intimidated\tI was a little intimidated by him.\n")


if __name__ == "__main__":
    unittest.main()
