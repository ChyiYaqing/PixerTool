from __future__ import annotations

import datetime as dt
import http.client
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MISC = Path(__file__).parents[1] / "misc"
sys.path.insert(0, str(MISC))  # hacker_news imports daily_english and github_trending from beside it
SPEC = importlib.util.spec_from_file_location("hacker_news", MISC / "hacker_news.py")
hn = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hn)

DAY = dt.date(2026, 11, 26)
SUMMARY = {"tech": True, "title": "标题", "what": "发生了什么", "views": ["观点一", "观点二"]}


def ollama_reply(content) -> bytes:
    return json.dumps({"message": {"content": json.dumps(content, ensure_ascii=False)}}).encode()


class FakeHN:
    """Serves topstories, items and Ollama replies; replies maps a story id to a reply or an exception."""

    def __init__(self, items: dict, top: list, replies: dict | None = None):
        self.items, self.top, self.replies = items, top, replies or {}

    def __call__(self, url, headers=None, data=None, timeout=30):
        if url.endswith("/topstories.json"):
            return json.dumps(self.top).encode()
        if "/item/" in url:
            return json.dumps(self.items.get(int(url.rsplit("/", 1)[1].split(".")[0]))).encode()
        story = json.loads(data)["messages"][1]["content"]
        for number, reply in self.replies.items():
            if f"标题：{self.items[number]['title']}" in story:
                if isinstance(reply, Exception):
                    raise reply
                return ollama_reply(reply)
        return ollama_reply(SUMMARY)


def story(number, score, kids=(901, 902), **extra):
    return {number: {"id": number, "type": "story", "title": f"Story {number}", "score": score,
                     "descendants": 5, "url": f"https://www.example.com/{number}", "kids": list(kids), **extra}}


COMMENTS = {901: {"id": 901, "type": "comment", "text": "First <i>take</i> &amp; more"},
            902: {"id": 902, "type": "comment", "text": "Second take"},
            903: {"id": 903, "type": "comment", "dead": True, "text": "spam"},
            904: {"id": 904, "type": "comment", "deleted": True}}


class TopStoriesTests(unittest.TestCase):
    def test_sorts_by_score_and_drops_dead_jobs_and_missing(self):
        items = {**story(1, 10), **story(2, 300), **story(3, 50, dead=True),
                 4: {"id": 4, "type": "job", "title": "Hiring", "score": 999}}
        with mock.patch.object(hn, "get", FakeHN(items, [1, 2, 3, 4, 5])):  # 5 has vanished (null)
            self.assertEqual([s["id"] for s in hn.top_stories()], [2, 1])

    def test_one_failed_item_skips_only_that_story(self):
        fake = FakeHN({**story(1, 10), **story(2, 20)}, [1, 2])

        def get(url, headers=None, data=None, timeout=30):
            if url.endswith("/item/1.json"):
                raise http.client.IncompleteRead(b"{", 100)  # not an OSError
            return fake(url, headers, data, timeout)

        with mock.patch.object(hn, "get", get), mock.patch("sys.stderr"):
            self.assertEqual([s["id"] for s in hn.top_stories()], [2])

    def test_titles_arrive_as_html_and_become_plain_text(self):
        with mock.patch.object(hn, "get", FakeHN(story(1, 10, title="R&amp;D &lt;3"), [1])):
            [top] = hn.top_stories()
        self.assertEqual(top["title"], "R&D <3")


class CommentsTests(unittest.TestCase):
    def test_keeps_live_comments_as_plain_text(self):
        items = {**COMMENTS, **story(1, 10, kids=[903, 901, 904, 902])}
        with mock.patch.object(hn, "get", FakeHN(items, [1])):
            self.assertEqual(hn.comments(items[1]), ["First take & more", "Second take"])


class SummariseTests(unittest.TestCase):
    STORY = {"title": "Show HN: Tool", "url": "https://x.dev", "text": "<p>Body</p>"}

    def test_sends_schema_story_and_comments(self):
        with mock.patch.object(hn, "get", return_value=ollama_reply(
                {**SUMMARY, "title": " 标题 ", "views": [" 一 ", "", "二", "三", "四"]})) as get:
            summary = hn.summarise(self.STORY, ["c1", "c2"], "http://mini:11434", "gemma4:e4b")
        self.assertEqual(summary, {"tech": True, "title": "标题", "what": "发生了什么", "views": ["一", "二"]})
        url, _, data = get.call_args.args
        sent = json.loads(data)
        self.assertEqual((url, sent["model"], sent["format"]), ("http://mini:11434/api/chat", "gemma4:e4b", hn.SCHEMA))
        prompt = sent["messages"][1]["content"]
        for part in ("标题：Show HN: Tool", "链接：https://x.dev", "正文：Body", "评论 1：c1", "评论 2：c2"):
            self.assertIn(part, prompt)

    def test_reply_off_schema_is_an_error(self):
        for bad in ({**SUMMARY, "tech": "yes"}, {**SUMMARY, "views": "一二三"}, {**SUMMARY, "views": [1, 2]},
                    {**SUMMARY, "title": None}):
            with self.subTest(bad=bad), mock.patch.object(hn, "get", return_value=ollama_reply(bad)):
                with self.assertRaises(ValueError):
                    hn.summarise(self.STORY, ["c"], "http://o", "m")

    def test_empty_summary_is_an_error(self):
        for bad in ({**SUMMARY, "what": " "}, {**SUMMARY, "views": ["观点一", "  "]}):
            with self.subTest(bad=bad), mock.patch.object(hn, "get", return_value=ollama_reply(bad)):
                with self.assertRaises(ValueError):
                    hn.summarise(self.STORY, ["c"], "http://o", "m")


class CardPageTests(unittest.TestCase):
    def test_escapes_text_from_the_web(self):
        page = hn.card_page({"title": "<script>", "url": "https://www.a.com/x", "score": 7, "descendants": 3,
                             "summary": {**SUMMARY, "title": "<b>标题</b>", "views": ["a&b"]}}, 2, 8, DAY, "m")
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;b&gt;标题&lt;/b&gt;", page)
        self.assertIn("<li>a&amp;b</li>", page)
        self.assertIn('<div class="host">a.com</div>', page)
        self.assertIn("第 2 / 8 条", page)

    def test_ask_hn_without_url_shows_hn_as_host(self):
        page = hn.card_page({"title": "Ask HN: x", "summary": SUMMARY}, 1, 1, DAY, "m")
        self.assertIn('<div class="host">news.ycombinator.com</div>', page)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.cards = self.root / "cards"
        quiet = mock.patch("sys.stderr")  # prepare reports skipped stories there
        quiet.start()
        self.addCleanup(quiet.stop)
        self.rendered = []

    def run_prepare(self, fake):
        def render(page, out, chrome):
            self.rendered.append(page)
            out.write_bytes(b"png")

        with mock.patch.object(hn, "get", fake), mock.patch.object(hn, "render", render):
            return hn.prepare(self.cards, "http://o", "m", "chrome", DAY)

    def test_skips_non_tech_quiet_and_failed_stories(self):
        items = {**COMMENTS, **story(1, 400), **story(2, 300), **story(3, 200, kids=[901]),
                 **story(4, 100), **story(5, 50)}
        fake = FakeHN(items, [5, 4, 3, 2, 1], {2: {**SUMMARY, "tech": False}, 4: OSError("Ollama asleep")})
        stories = self.run_prepare(fake)
        self.assertEqual([s["id"] for s in stories], [1, 5])  # 2 is politics, 3 has one comment, 4 failed
        self.assertEqual(sorted(p.name for p in self.cards.iterdir()), ["01.png", "02.png", "cards.json"])
        self.assertIn("第 2 / 2 条", self.rendered[1])
        saved = json.loads((self.cards / "cards.json").read_text())
        self.assertEqual((saved["day"], saved["stories"][0]["summary"]), ("2026-11-26", SUMMARY))

    def test_stops_at_count(self):
        items = {**COMMENTS}
        for number in range(1, 12):
            items.update(story(number, number))
        with mock.patch.object(hn, "COUNT", 3):
            stories = self.run_prepare(FakeHN(items, list(range(1, 12))))
        self.assertEqual([s["id"] for s in stories], [11, 10, 9])

    def test_nothing_summarised_keeps_the_previous_cards(self):
        self.cards.mkdir()
        (self.cards / "01.png").write_bytes(b"old")
        items = {**COMMENTS, **story(1, 10)}
        with self.assertRaisesRegex(RuntimeError, "no story"):
            self.run_prepare(FakeHN(items, [1], {1: ValueError("bad reply")}))
        self.assertEqual((self.cards / "01.png").read_bytes(), b"old")

    def test_failed_front_page_keeps_the_previous_cards(self):
        self.cards.mkdir()
        (self.cards / "01.png").write_bytes(b"old")
        with self.assertRaises(OSError):
            self.run_prepare(mock.Mock(side_effect=OSError("timed out")))
        self.assertEqual((self.cards / "01.png").read_bytes(), b"old")

    def test_replaces_the_previous_cards(self):
        self.cards.mkdir()
        (self.cards / "05.png").write_bytes(b"old")
        (self.cards / "next").write_text("4")
        self.run_prepare(FakeHN({**COMMENTS, **story(1, 10)}, [1]))
        self.assertEqual(sorted(p.name for p in self.cards.iterdir()), ["01.png", "cards.json"])
        self.assertFalse(self.cards.with_name("cards.old").exists())


class MainTests(unittest.TestCase):
    def test_next_prints_the_card(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sys.stdout") as out:
            (Path(folder) / "01.png").write_bytes(b"")
            self.assertEqual(hn.main(["next", folder]), 0)
        self.assertIn("01.png", "".join(call.args[0] for call in out.write.call_args_list))

    def test_errors_exit_1(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sys.stderr"):
            self.assertEqual(hn.main(["next", folder]), 1)


if __name__ == "__main__":
    unittest.main()
