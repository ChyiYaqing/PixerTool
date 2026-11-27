import datetime as dt
import http.client
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MISC = Path(__file__).parents[1] / "misc"
sys.path.insert(0, str(MISC))  # news_english imports its neighbours from beside it
SPEC = importlib.util.spec_from_file_location("news_english", MISC / "news_english.py")
ne = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ne)

DAY = dt.date(2026, 11, 27)
STORIES = [
    {"title": "1Password Security Incident",
     "summary": {"title": "1Password 安全事件", "what": "攻击者进入了 CI/CD", "views": ["观点一", "观点二"]}},
    {"title": "Slow Ubuntu security response times",
     "summary": {"title": "Ubuntu 安全响应慢", "what": "补丁变慢", "views": ["观点三", "观点四"]}},
]
PICK = {"story": 1, "scene": "看到 1Password 事件，你想排查 CI 权限", "english": "We need to tighten up our CI permissions.",
        "cn": "我们得收紧 CI 权限。", "phrase": "tighten up", "phrase_cn": "收紧"}


def ollama_reply(picks) -> bytes:
    return json.dumps({"message": {"content": json.dumps({"picks": picks}, ensure_ascii=False)}}).encode()


class LoadStoriesTests(unittest.TestCase):
    def write(self, day):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "cards.json"
        path.write_text(json.dumps({"day": day, "stories": STORIES}), encoding="utf-8")
        return path

    def test_last_nights_cards_load(self):
        self.assertEqual(ne.load_stories(self.write("2026-11-26"), DAY), STORIES)

    def test_other_days_are_refused(self):
        for day in ("2026-11-25", "2026-11-27"):
            with self.subTest(day=day), self.assertRaisesRegex(RuntimeError, "not last night"):
                ne.load_stories(self.write(day), DAY)


class ChooseTests(unittest.TestCase):
    def test_sends_the_stories_and_returns_the_first_pick(self):
        with mock.patch.object(ne, "get", return_value=ollama_reply([{**PICK, "english": " We need to tighten up. "}])) as get:
            pick, story = ne.choose(STORIES, "http://mini:11434", "gemma4:12b")
        self.assertEqual((pick["english"], story), ("We need to tighten up.", STORIES[0]))
        url, _, data = get.call_args.args
        sent = json.loads(data)
        self.assertEqual((url, sent["model"], sent["format"]), ("http://mini:11434/api/chat", "gemma4:12b", ne.SCHEMA))
        prompt = sent["messages"][1]["content"]
        for part in ("新闻 1：1Password Security Incident", "中文：Ubuntu 安全响应慢。补丁变慢", "评论：观点一；观点二"):
            self.assertIn(part, prompt)

    def test_skips_unusable_picks(self):
        bad = [{**PICK, "story": 3}, {**PICK, "story": "1"}, {**PICK, "phrase": "lock down"},
               {**PICK, "english": "word " * 17 + "tighten up"}, {**PICK, "cn": " "}, {**PICK, "phrase": "tighten up our"}, {**PICK, "phrase": "ighten up"}, "not a pick"]
        with mock.patch.object(ne, "get", return_value=ollama_reply([*bad, {**PICK, "story": 2}])):
            _, story = ne.choose(STORIES, "http://o", "m")
        self.assertEqual(story, STORIES[1])

    def test_no_usable_pick_is_an_error(self):
        for picks in ([], [{**PICK, "phrase": "lock down"}]):
            with self.subTest(picks=picks), mock.patch.object(ne, "get", return_value=ollama_reply(picks)):
                with self.assertRaisesRegex(RuntimeError, "no story"):
                    ne.choose(STORIES, "http://o", "m")


class CardPageTests(unittest.TestCase):
    def test_underlines_the_phrase_whatever_its_case_and_escapes_text(self):
        pick = {**PICK, "english": "Let's <b>Tighten Up</b> & ship", "phrase": "tighten up", "cn": "a&b"}
        page = ne.card_page(pick, {**STORIES[0], "summary": {**STORIES[0]["summary"], "title": "<i>"}}, DAY)
        self.assertIn("“Let’s &lt;b&gt;<u>Tighten Up</u>&lt;/b&gt; &amp; ship”", page)
        self.assertIn("Hacker News · &lt;i&gt;", page)
        self.assertIn("a&amp;b", page)
        self.assertIn("NOV 27", page)


class MainTests(unittest.TestCase):
    def test_renders_the_card(self):
        with tempfile.TemporaryDirectory() as folder:
            cards = Path(folder) / "cards.json"
            cards.write_text(json.dumps({"day": (dt.date.today() - dt.timedelta(days=1)).isoformat(), "stories": STORIES}), encoding="utf-8")
            out = Path(folder) / "card.png"
            with mock.patch.object(ne, "get", return_value=ollama_reply([PICK])), \
                    mock.patch.object(ne, "render") as render, mock.patch("sys.stdout"):
                self.assertEqual(ne.main([str(cards), "-o", str(out)]), 0)
        page, output, _ = render.call_args.args
        self.assertIn("<u>tighten up</u>", page)
        self.assertEqual(output, out)

    def test_failures_exit_1(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sys.stderr"):
            cards = Path(folder) / "cards.json"
            self.assertEqual(ne.main([str(cards)]), 1)  # missing
            cards.write_text("null")
            self.assertEqual(ne.main([str(cards)]), 1)  # not the shape prepare writes
            cards.write_text(json.dumps({"day": (dt.date.today() - dt.timedelta(days=1)).isoformat(), "stories": STORIES}))
            with mock.patch.object(ne, "get", side_effect=http.client.IncompleteRead(b"{", 100)):
                self.assertEqual(ne.main([str(cards)]), 1)
            with mock.patch.object(ne, "get", return_value=json.dumps({"message": {"content": "[]"}}).encode()):
                self.assertEqual(ne.main([str(cards)]), 1)


if __name__ == "__main__":
    unittest.main()
