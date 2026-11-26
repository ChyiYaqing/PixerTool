import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MISC = Path(__file__).parents[1] / "misc"
sys.path.insert(0, str(MISC))  # github_trending imports daily_english from beside it
SPEC = importlib.util.spec_from_file_location("github_trending", MISC / "github_trending.py")
trending = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trending)

ARTICLE = """<article class="Box-row">
  <div class="float-right"><a href="/sponsors/alice">Sponsor</a>
    <a href="/login?return_to=%2Falice%2Ftool"><svg class="octicon"><path d="M8 .25a.75"></path></svg> Star</a></div>
  <h2 class="h3 lh-condensed">
    <a data-hydro-click="{}" href="/alice/tool" class="Link"><svg><path d="M2 2.5"></path></svg>
      <span class="text-normal">alice /</span> tool</a></h2>
  <p class="col-9 color-fg-muted my-1">
    Fast &amp; small <b>tool</b>
  </p>
  <div class="f6 color-fg-muted mt-2">
    <span itemprop="programmingLanguage">Rust</span>
    <a href="/alice/tool/stargazers" class="Link"><svg aria-label="star"><path d="M8"></path></svg>
      12,345</a>
    <a href="/alice/tool/forks" class="Link"><svg><path d="M5"></path></svg> 67</a>
    <span class="float-sm-right"><svg><path d="M8"></path></svg> 1,234 stars today</span>
  </div>
</article>"""

BARE = """<article class="Box-row">
  <h2 class="h3"><a href="/bob/notes" class="Link"><svg><path d="M2"></path></svg> bob / notes</a></h2>
  <div class="f6"><a href="/bob/notes/stargazers"><svg><path d="M8"></path></svg> 9</a>
    <span><svg><path d="M8"></path></svg> 3 stars today</span></div>
</article>"""


def ollama_reply(content) -> bytes:
    return json.dumps({"message": {"content": json.dumps(content, ensure_ascii=False)}}).encode()


class ParseTrendingTests(unittest.TestCase):
    def test_reads_each_field(self):
        [repo] = trending.parse_trending(ARTICLE)
        self.assertEqual(repo, {"name": "alice/tool", "description": "Fast & small tool", "language": "Rust",
                                "stars": "12,345", "today": "1,234"})

    def test_description_skips_the_icons_path_elements(self):
        # <path ...> starts with "<p"; matching it would pull the Sponsor/Star buttons into the description.
        [repo] = trending.parse_trending(ARTICLE)
        self.assertNotIn("Star", repo["description"])

    def test_missing_description_and_language_are_empty(self):
        repos = trending.parse_trending(ARTICLE + BARE)
        self.assertEqual([r["name"] for r in repos], ["alice/tool", "bob/notes"])
        self.assertEqual((repos[1]["description"], repos[1]["language"], repos[1]["today"]), ("", "", "3"))

    def test_page_without_repositories_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "layout may have changed"):
            trending.parse_trending("<html><body>rate limited</body></html>")


class CleanReadmeTests(unittest.TestCase):
    def test_drops_code_images_and_tags(self):
        body = "# Tool\n\n![badge](https://x/b.svg)<p align=center>Hi</p>\n\n\n\n```sh\nrm -rf /\n```\nEnd"
        self.assertEqual(trending.clean_readme(body), "# Tool\n\nHi\n\nEnd")

    def test_cuts_long_readmes(self):
        self.assertEqual(len(trending.clean_readme("a" * 50000)), trending.README_CHARS)


class SummariseTests(unittest.TestCase):
    def test_sends_schema_and_returns_trimmed_summary(self):
        reply = ollama_reply({"tagline": " 一个小工具 ", "points": ["快", " 小 ", "", "多余"]})
        with mock.patch.object(trending, "get", return_value=reply) as get:
            summary = trending.summarise("README", "http://mini:11434", "gemma4:e4b")
        self.assertEqual(summary, {"tagline": "一个小工具", "points": ["快", "小"]})
        url, _, data = get.call_args.args
        self.assertEqual(url, "http://mini:11434/api/chat")
        sent = json.loads(data)
        self.assertEqual((sent["model"], sent["format"], sent["stream"]), ("gemma4:e4b", trending.SCHEMA, False))
        self.assertIn("<readme>\nREADME\n</readme>", sent["messages"][1]["content"])

    def test_points_as_a_string_is_an_error(self):
        # Iterating a string would turn its first characters into three "points".
        with mock.patch.object(trending, "get", return_value=ollama_reply({"tagline": "x", "points": "快小多"})):
            with self.assertRaises(ValueError):
                trending.summarise("README", "http://mini:11434", "m")

    def test_empty_summary_is_an_error(self):
        with mock.patch.object(trending, "get", return_value=ollama_reply({"tagline": " ", "points": ["x"]})):
            with self.assertRaises(ValueError):
                trending.summarise("README", "http://mini:11434", "m")


class CardPageTests(unittest.TestCase):
    REPO = {"name": "alice/<tool>", "description": "Fast & small", "language": "Rust", "stars": "1", "today": "2"}

    def test_escapes_text_from_the_web(self):
        page = trending.card_page({**self.REPO, "summary": {"tagline": "<b>x</b>", "points": ["a&b"]}},
                                  1, 10, dt.date(2026, 11, 26), "m")
        self.assertIn("&lt;tool&gt;", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)
        self.assertIn("<li>a&amp;b</li>", page)
        self.assertIn("第 1 / 10 名", page)

    def test_without_summary_shows_the_description(self):
        page = trending.card_page(self.REPO, 2, 10, dt.date(2026, 11, 26), "m")
        self.assertIn('<div class="tagline">Fast &amp; small</div>', page)
        self.assertIn("未能生成摘要", page)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.root)
        self.cards = self.root / "cards"
        quiet = mock.patch("sys.stderr")  # prepare reports the missing README there
        quiet.start()
        self.addCleanup(quiet.stop)

    def fake_get(self, url, headers=None, data=None, timeout=30):
        if url == trending.TRENDING:
            return (ARTICLE + BARE).encode()
        if "api.github.com" in url:
            if "bob" in url:
                raise OSError("404")
            return b"# alice tool"
        return ollama_reply({"tagline": "小工具", "points": ["快"]})

    def test_renders_one_card_per_repo_and_survives_a_missing_readme(self):
        rendered = []
        with mock.patch.object(trending, "get", self.fake_get), \
                mock.patch.object(trending, "render", lambda page, out, chrome: (rendered.append(page),
                                                                                out.write_bytes(b"png"))):
            repos = trending.prepare(self.cards, "http://o", "m", "chrome", dt.date(2026, 11, 26))
        self.assertEqual(sorted(p.name for p in self.cards.iterdir()), ["01.png", "02.png", "cards.json"])
        self.assertEqual(repos[0]["summary"], {"tagline": "小工具", "points": ["快"]})
        self.assertNotIn("summary", repos[1])
        self.assertIn("未能生成摘要", rendered[1])
        self.assertEqual(json.loads((self.cards / "cards.json").read_text())["day"], "2026-11-26")

    def test_failed_fetch_keeps_the_previous_cards(self):
        self.cards.mkdir()
        (self.cards / "01.png").write_bytes(b"old")
        with mock.patch.object(trending, "get", side_effect=OSError("timed out")):
            with self.assertRaises(OSError):
                trending.prepare(self.cards, "http://o", "m", "chrome", dt.date(2026, 11, 26))
        self.assertEqual((self.cards / "01.png").read_bytes(), b"old")

    def test_failed_render_keeps_the_previous_cards(self):
        self.cards.mkdir()
        (self.cards / "01.png").write_bytes(b"old")
        with mock.patch.object(trending, "get", self.fake_get), \
                mock.patch.object(trending, "render", side_effect=RuntimeError("Chrome failed")):
            with self.assertRaises(RuntimeError):
                trending.prepare(self.cards, "http://o", "m", "chrome", dt.date(2026, 11, 26))
        self.assertEqual((self.cards / "01.png").read_bytes(), b"old")


    def test_malformed_replies_cost_one_summary(self):
        replies = iter([ollama_reply({"tagline": None, "points": ["x"]}), b"not json"])

        def get(url, headers=None, data=None, timeout=30):
            if url == trending.TRENDING:
                return (ARTICLE + BARE).encode()
            return b"# readme" if "api.github.com" in url else next(replies)

        with mock.patch.object(trending, "get", get), \
                mock.patch.object(trending, "render", lambda page, out, chrome: out.write_bytes(b"png")):
            repos = trending.prepare(self.cards, "http://o", "m", "chrome", dt.date(2026, 11, 26))
        self.assertEqual([("summary" in r) for r in repos], [False, False])
        self.assertTrue((self.cards / "02.png").is_file())

    def test_failed_swap_restores_the_previous_cards(self):
        self.cards.mkdir()
        (self.cards / "01.png").write_bytes(b"old")
        rename = Path.rename

        def flaky(path, target):
            if path.name == "cards.new":
                raise OSError("disk full")
            return rename(path, target)

        with mock.patch.object(trending, "get", self.fake_get), \
                mock.patch.object(trending, "render", lambda page, out, chrome: out.write_bytes(b"png")), \
                mock.patch.object(Path, "rename", flaky):
            with self.assertRaises(OSError):
                trending.prepare(self.cards, "http://o", "m", "chrome", dt.date(2026, 11, 26))
        self.assertEqual((self.cards / "01.png").read_bytes(), b"old")

    def test_replaces_yesterdays_cards(self):
        self.cards.mkdir()
        (self.cards / "03.png").write_bytes(b"old")
        (self.cards / "next").write_text("2")
        with mock.patch.object(trending, "get", self.fake_get), \
                mock.patch.object(trending, "render", lambda page, out, chrome: out.write_bytes(b"png")):
            trending.prepare(self.cards, "http://o", "m", "chrome", dt.date(2026, 11, 26))
        self.assertEqual(sorted(p.name for p in self.cards.iterdir()), ["01.png", "02.png", "cards.json"])
        self.assertFalse(self.cards.with_name("cards.old").exists())


class NextCardTests(unittest.TestCase):
    def test_cycles_through_the_cards(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for name in ("01.png", "02.png", "03.png", "cards.json"):
                (folder / name).write_bytes(b"")
            shown = [trending.next_card(folder).name for _ in range(4)]
        self.assertEqual(shown, ["01.png", "02.png", "03.png", "01.png"])

    def test_pointer_past_the_end_wraps(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "01.png").write_bytes(b"")
            (folder / "02.png").write_bytes(b"")
            (folder / "next").write_text("7")
            self.assertEqual(trending.next_card(folder).name, "02.png")

    def test_empty_folder_is_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "run prepare first"):
                trending.next_card(Path(folder))


class MainTests(unittest.TestCase):
    def test_next_prints_the_card(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sys.stdout") as out:
            (Path(folder) / "01.png").write_bytes(b"")
            self.assertEqual(trending.main(["next", folder]), 0)
        self.assertIn("01.png", "".join(call.args[0] for call in out.write.call_args_list))

    def test_errors_exit_1(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sys.stderr"):
            self.assertEqual(trending.main(["next", folder]), 1)


if __name__ == "__main__":
    unittest.main()
