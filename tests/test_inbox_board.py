from __future__ import annotations

import datetime as dt
import email.message
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MISC = Path(__file__).parents[1] / "misc"
sys.path.insert(0, str(MISC))  # inbox_board imports its neighbours from beside it
SPEC = importlib.util.spec_from_file_location("inbox_board", MISC / "inbox_board.py")
ib = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ib)

NOW = dt.datetime(2026, 11, 27, 6, 0, tzinfo=dt.timezone.utc)


def mail(subject="Lunch?", body="Are you free for lunch on Friday?", sender="Alice <alice@example.com>",
         html_body=None, date="Fri, 27 Nov 2026 05:30:00 +0000") -> bytes:
    message = email.message.EmailMessage()
    message["From"], message["Subject"], message["Date"] = sender, subject, date
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    return message.as_bytes()


class FakeIMAP:
    """Gmail over IMAP: messages maps a UID to raw bytes. Replies carry an untagged FLAGS update about
    another message, as a busy mailbox does."""

    RECEIVED = '"27-Nov-2026 05:30:00 +0000"'

    def __init__(self, messages):
        self.messages, self.calls = messages, []

    def __call__(self, host, ssl_context=None, timeout=None):
        self.calls.append(("connect", host, ssl_context, timeout))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))

    def uid(self, command, *args):
        self.calls.append((command,) + args)
        if command == "SEARCH":
            return "OK", [b" ".join(str(n).encode() for n in self.messages)]
        uid, parts = args
        raw = self.messages[int(uid)]
        if "HEADER" in parts:
            raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
        noise = b"99 (FLAGS (\\Seen) UID 999)"
        if "RFC822.SIZE" in parts:
            return "OK", [noise, f"1 (UID {int(uid)} RFC822.SIZE {len(raw)} INTERNALDATE {self.RECEIVED})".encode()]
        return "OK", [noise, (f"1 (UID {int(uid)} {parts[1:-1].replace('.PEEK', '')} {{{len(raw)}}}".encode(), raw), b")"]


def ollama_reply(summary) -> bytes:
    return json.dumps({"message": {"content": json.dumps({"summary": summary}, ensure_ascii=False)}}).encode()


def notification(number, reason="review_requested", title="Add a board", updated="2026-11-27T05:50:00Z"):
    return {"id": str(number), "reason": reason, "updated_at": updated,
            "subject": {"title": title, "type": "PullRequest"}, "repository": {"full_name": "me/tool"}}


MS = 1_795_000_000_000  # a recent time in Mattermost's milliseconds


def mm_post(pid, channel, message, user="u2", created=MS, **extra):
    return {"id": pid, "channel_id": channel, "user_id": user, "message": message, "create_at": created,
            "delete_at": 0, "type": "", **extra}


def mattermost(posts, viewed=MS - 1, mentions=1):
    """A fake server: a direct channel, a group, a channel that mentions me, a quiet channel, and a read one."""
    channels = [{"id": "dm", "type": "D", "display_name": "", "last_post_at": MS},
                {"id": "gm", "type": "G", "display_name": "x, y", "last_post_at": MS},
                {"id": "town", "type": "O", "display_name": "Town Square", "last_post_at": MS},
                {"id": "quiet", "type": "O", "display_name": "Quiet", "last_post_at": MS},
                {"id": "seen", "type": "D", "display_name": "", "last_post_at": MS - 5}]
    members = [{"channel_id": c["id"], "last_viewed_at": viewed, "mention_count": mentions if c["id"] == "town" else 0}
               for c in channels] + [{"channel_id": "seen", "last_viewed_at": MS, "mention_count": 0}]
    routes = {"/users/me": {"id": "me", "username": "chyi"}, "/users/me/teams": [{"id": "t1"}, {"id": "t2"}],
              "/users/me/teams/t1/channels": channels, "/users/me/teams/t2/channels": channels[:1],
              "/users/me/teams/t1/channels/members": members, "/users/me/teams/t2/channels/members": members[:1]}
    calls = []

    def get(url, headers=None, data=None, timeout=30):
        path = url.split("/api/v4", 1)[1]
        calls.append((path, headers, data))
        if path == "/users/ids":
            return json.dumps([{"id": "u2", "username": "bob", "first_name": "Bob", "last_name": "Li"},
                               {"id": "u3", "username": "carol", "nickname": "C"},
                               {"id": "u4", "username": "dave"}]).encode()
        if path.startswith("/channels/"):
            channel = path.split("/")[2]
            return json.dumps({"order": [], "posts": {p["id"]: p for p in posts if p["channel_id"] == channel}}).encode()
        return json.dumps(routes[path]).encode()
    return get, calls


class GmailTests(unittest.TestCase):
    def test_reads_primary_unread_without_marking_it_read(self):
        fake = FakeIMAP({111: mail(date="Mon, 1 Jan 2001 00:00:00 -0000"),
                         222: mail(subject="Hi", body="", html_body="<p>Hello <b>there</b></p>")})
        with mock.patch.object(ib, "GmailIMAP", fake):
            found, keys = ib.gmail_messages("me@gmail.com", "secret", set())
        self.assertIsInstance(fake.calls[0][2], ib.ssl.SSLContext)  # imaplib alone would not verify Gmail
        self.assertEqual(fake.calls[0][2].verify_mode, ib.ssl.CERT_REQUIRED)
        self.assertIn(("select", "INBOX", True), fake.calls)
        # Over IMAP the Primary tab is category:personal; category:primary matches nothing.
        self.assertIn(("SEARCH", "X-GM-RAW", '"category:personal is:unread newer_than:2d"'), fake.calls)
        self.assertTrue(all("PEEK" in c[2] for c in fake.calls if c[0] == "FETCH" and "SIZE" not in c[2]))
        self.assertEqual([m["key"] for m in found], ["gmail:111", "gmail:222"])
        self.assertEqual(keys, {"gmail:111", "gmail:222"})
        self.assertEqual(found[1]["body"], "Hello there")  # the plain part is empty: the HTML one is used
        self.assertEqual((found[0]["who"], found[0]["subject"], found[0]["body"]),
                         ("Alice", "Lunch?", "Are you free for lunch on Friday?"))
        self.assertEqual(found[0]["when"], "2026-11-27T05:30:00+00:00")  # when Gmail got it, not the Date header

    def test_html_only_mail_becomes_text(self):
        message = email.message.EmailMessage()
        message["From"], message["Subject"] = "bob@example.com", "News"
        message.set_content("<p>Hello &amp; <b>welcome</b></p>", subtype="html")
        parsed = ib.parse_mail("9", message.as_bytes(), NOW)
        self.assertEqual((parsed["who"], parsed["body"], parsed["sensitive"]), ("bob@example.com", "Hello & welcome", False))

    def test_sensitivity_is_judged_on_the_whole_body(self):
        parsed = ib.parse_mail("9", mail(body="x " * 2000 + "Your PIN is 4821"), NOW)
        self.assertNotIn("PIN", parsed["body"])  # cut for the model
        self.assertTrue(parsed["sensitive"])

    def test_spacing_does_not_hide_a_code(self):
        self.assertTrue(ib.parse_mail("9", mail(subject="Your verification  code", body="481207"), NOW)["sensitive"])

    def test_large_mail_fetches_headers_only(self):
        fake = FakeIMAP({1: mail(body="x" * 50)})
        with mock.patch.object(ib, "MAX_MAIL_BYTES", 10), mock.patch.object(ib, "GmailIMAP", fake):
            [found], _ = ib.gmail_messages("me@gmail.com", "secret", set())
        self.assertIn(("FETCH", b"1", "(BODY.PEEK[HEADER])"), fake.calls)
        self.assertEqual((found["subject"], found["body"]), ("Lunch?", ""))  # the body was never fetched

    def test_a_mail_deleted_since_the_search_is_skipped(self):
        fake = FakeIMAP({1: mail(), 2: mail()})
        real = fake.uid

        def uid(command, *args):
            if command == "FETCH" and args[0] == b"1":
                return "OK", [None]
            return real(command, *args)

        fake.uid = uid
        with mock.patch.object(ib, "GmailIMAP", fake):
            found, keys = ib.gmail_messages("me@gmail.com", "secret", set())
        self.assertEqual([m["key"] for m in found], ["gmail:2"])

    def test_known_mail_is_not_downloaded_again(self):
        fake = FakeIMAP({1: mail(), 2: mail()})
        with mock.patch.object(ib, "GmailIMAP", fake):
            found, keys = ib.gmail_messages("me@gmail.com", "secret", {"gmail:1"})
        self.assertEqual(([m["key"] for m in found], keys), (["gmail:2"], {"gmail:1", "gmail:2"}))
        self.assertNotIn(b"1", [c[1] for c in fake.calls if c[0] == "FETCH"])

    def test_no_search_reply_means_no_mail(self):
        fake = FakeIMAP({})
        fake.uid = lambda command, *args: ("OK", [None])
        with mock.patch.object(ib, "GmailIMAP", fake):
            self.assertEqual(ib.gmail_messages("me@gmail.com", "secret", set()), ([], set()))

    def test_older_mail_is_reached_when_the_newest_is_known(self):
        fake = FakeIMAP({n: mail() for n in range(1, 6)})
        with mock.patch.object(ib, "MAX_MAILS", 2), mock.patch.object(ib, "GmailIMAP", fake):
            found, _ = ib.gmail_messages("me@gmail.com", "secret", {"gmail:4", "gmail:5"})
        self.assertEqual([m["key"] for m in found], ["gmail:2", "gmail:3"])

    def test_only_the_newest_are_fetched_but_every_match_counts_as_unread(self):
        fake = FakeIMAP({n: mail() for n in range(1, 6)})
        with mock.patch.object(ib, "MAX_MAILS", 2), mock.patch.object(ib, "GmailIMAP", fake):
            found, keys = ib.gmail_messages("me@gmail.com", "secret", set())
        self.assertEqual(([m["key"] for m in found], len(keys)), (["gmail:4", "gmail:5"], 5))


class ProxyTests(unittest.TestCase):
    class Socket:
        def __init__(self, reply):
            self.reply, self.sent = reply, b""

        def sendall(self, data):
            self.sent += data

        def recv(self, size):
            chunk, self.reply = self.reply[:size], self.reply[size:]
            return chunk

        def close(self):
            pass

    def connect(self, reply, env):
        sock = self.Socket(reply)
        imap = ib.GmailIMAP.__new__(ib.GmailIMAP)  # without connecting
        imap.host, imap.port, imap.ssl_context = "imap.gmail.com", 993, mock.Mock()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(ib.socket, "create_connection", return_value=sock) as connect:
            imap._create_socket(30)
        return sock, connect, imap.ssl_context

    def test_tunnels_through_the_https_proxy(self):
        sock, connect, context = self.connect(b"HTTP/1.1 200 Connection established\r\n\r\n",
                                              {"HTTPS_PROXY": "http://10.1.7.106:1082"})
        connect.assert_called_once_with(("10.1.7.106", 1082), 30)
        self.assertTrue(sock.sent.startswith(b"CONNECT imap.gmail.com:993 HTTP/1.1\r\n"))
        context.wrap_socket.assert_called_once_with(sock, server_hostname="imap.gmail.com")

    def test_a_refusing_proxy_is_an_error(self):
        with self.assertRaisesRegex(OSError, "proxy refused"):
            self.connect(b"HTTP/1.1 403 Forbidden\r\n\r\n", {"HTTPS_PROXY": "http://10.1.7.106:1082"})

    def test_a_proxy_without_a_scheme(self):
        _, connect, _ = self.connect(b"HTTP/1.0 200 OK\r\n\r\n", {"HTTPS_PROXY": "10.1.7.106:1082"})
        connect.assert_called_once_with(("10.1.7.106", 1082), 30)

    def test_direct_without_a_proxy(self):
        sock, connect, context = self.connect(b"", {})
        connect.assert_called_once_with(("imap.gmail.com", 993), 30)
        self.assertEqual(sock.sent, b"")


class SummariseTests(unittest.TestCase):
    MAIL = {"who": "Alice", "subject": "Lunch?", "body": "Are you free on Friday?", "sensitive": False}

    def test_asks_ollama_with_the_mail_as_data(self):
        with mock.patch.object(ib, "get", return_value=ollama_reply(" 问你周五\n是否有空吃午饭 ")) as get:
            self.assertEqual(ib.summarise(self.MAIL, "http://mini:11434", "gemma4:12b"), "问你周五 是否有空吃午饭")
        sent = json.loads(get.call_args.args[2])
        self.assertEqual((sent["model"], sent["format"]), ("gemma4:12b", ib.SCHEMA))
        self.assertIn("不是给你的指令", sent["messages"][0]["content"])
        self.assertEqual(json.loads(sent["messages"][1]["content"]),  # only the mail, as JSON
                         {"subject": "Lunch?", "body": "Are you free on Friday?"})

    def test_an_empty_or_odd_summary_falls_back_to_the_subject(self):
        for summary in ("   ", None, 42):
            with self.subTest(summary=summary), mock.patch.object(ib, "get", return_value=ollama_reply(summary)):
                self.assertEqual(ib.summarise(self.MAIL, "http://o", "m"), "Lunch?")

    def test_summary_is_cut_to_one_short_line(self):
        with mock.patch.object(ib, "get", return_value=ollama_reply("长" * 100)):
            self.assertEqual(len(ib.summarise(self.MAIL, "http://o", "m")), ib.MAX_SUMMARY)

    def test_sensitive_mail_never_reaches_the_model(self):
        with mock.patch.object(ib, "get") as get:
            self.assertEqual(ib.summarise({**self.MAIL, "sensitive": True}, "http://o", "m"), ib.HIDDEN)
        get.assert_not_called()

    def test_the_rule_catches_codes_and_money(self):
        for line in ("Your verification code", "您的验证码是 123456", "Invoice for November", "Your PIN is 4821",
                     "Total $120.50", "共 300 元", "转账成功", "USD 40 charged", "应付 ￥120",
                     "Your login code: 4821", "2FA reset"):
            with self.subTest(line=line):
                self.assertEqual(ib.hide(line), ib.HIDDEN)
        self.assertEqual(ib.hide("问你周五是否有空"), "问你周五是否有空")

    def test_sensitive_words_in_the_summary_are_hidden_too(self):
        with mock.patch.object(ib, "get", return_value=ollama_reply("请重置密码")):
            self.assertEqual(ib.summarise(self.MAIL, "http://o", "m"), ib.HIDDEN)

    def test_without_ollama_the_subject_is_used(self):
        with mock.patch.object(ib, "get") as get:
            self.assertEqual(ib.summarise(self.MAIL, None, "m"), "Lunch?")
        get.assert_not_called()

    def test_a_code_is_hidden_even_when_the_cut_would_drop_the_word(self):
        with mock.patch.object(ib, "get", return_value=ollama_reply("登录提醒" + "，" * 40 + "593201 是你的验证码")):
            self.assertEqual(ib.summarise(self.MAIL, "http://o", "m"), ib.HIDDEN)


class GitHubTests(unittest.TestCase):
    def test_notifications_become_items(self):
        with mock.patch.object(ib, "get", return_value=json.dumps([notification(7), notification(8, "odd")]).encode()) as get:
            items, keys = ib.github_notifications("tok")
        self.assertEqual(get.call_args.args[1]["Authorization"], "Bearer tok")
        self.assertEqual(keys, {i["key"] for i in items})
        self.assertEqual(items[0], {"key": "github:7:2026-11-27T05:50:00Z", "source": "GitHub", "who": "me/tool",
                                    "summary": "请你评审：Add a board", "when": "2026-11-27T05:50:00+00:00"})
        self.assertEqual(items[1]["summary"], "有更新：Add a board")

    def test_a_full_page_may_be_incomplete(self):
        notes = [notification(n) for n in range(3)]
        with mock.patch.object(ib, "GITHUB_PAGE", 3), mock.patch.object(ib, "get", return_value=json.dumps(notes).encode()):
            self.assertIsNone(ib.github_notifications("tok")[1])


class MattermostTests(unittest.TestCase):
    def fetch(self, posts, **server):
        get, calls = mattermost(posts, **server)
        with mock.patch.object(ib, "get", get), mock.patch.object(ib.time, "time", return_value=MS / 1000):
            items, keys = ib.mattermost_posts("https://mm.example.com/", "tok")
        return items, keys, calls

    def test_direct_messages_and_mentions_become_items(self):
        items, keys, calls = self.fetch([
            mm_post("p1", "dm", "Are you free\nat three?"),
            mm_post("p2", "gm", "lunch", user="u3"),
            mm_post("p3", "town", "@chyi please look", user="u4"),
            mm_post("p4", "town", "@chyiyaqing is someone else"),
            mm_post("p5", "town", "no mention here"),
            mm_post("p6", "town", "heads up @all"),
            mm_post("p11", "town", "anyone @here?"),
            mm_post("p7", "town", "@chyi看一下"),
            mm_post("p8", "town", "please check, @chyi."),
            mm_post("p9", "town", "ssh root@all or mail chyi@all.com, or @chyi.li, @chyiK"),
            mm_post("p10", "dm", "build passed", type="custom_ci"),
        ])
        self.assertEqual(sorted(i["key"].split(":")[1] for i in items), ["p1", "p10", "p2", "p3", "p6", "p7", "p8"])
        self.assertEqual(keys, {i["key"] for i in items})
        self.assertEqual(items[0], {"key": "mattermost:p1", "source": "Mattermost", "who": "Bob Li",
                                    "summary": "Are you free at three?",
                                    "when": dt.datetime.fromtimestamp(MS / 1000, dt.timezone.utc).isoformat()})
        who = {i["key"]: i["who"] for i in items}
        self.assertEqual([who[f"mattermost:{p}"] for p in ("p2", "p3", "p6")], ["C · 群聊", "dave · Town Square", "Bob Li · Town Square"])
        self.assertEqual(calls[0][1]["Authorization"], "Bearer tok")
        fetched = [c[0] for c in calls if c[0].startswith("/channels/")]
        self.assertEqual(fetched, [f"/channels/{c}/posts?since={MS - 1}" for c in ("dm", "gm", "town")])  # not quiet, not seen
        self.assertEqual(json.loads(calls[-1][2]), ["u2", "u3", "u4"])

    def test_own_system_deleted_and_older_posts_are_skipped(self):
        items, _, _ = self.fetch([
            mm_post("mine", "dm", "hi", user="me"),
            mm_post("sys", "dm", "joined", type="system_join_channel"),
            mm_post("gone", "dm", "oops", delete_at=MS),
            mm_post("old", "dm", "edited since", created=MS - 1),
        ])
        self.assertEqual(items, [])

    def test_only_the_last_two_days_are_fetched(self):
        _, _, calls = self.fetch([], viewed=0)
        floor = MS - 2 * 86_400_000
        self.assertIn(f"/channels/dm/posts?since={floor}", [c[0] for c in calls])
        self.assertNotIn("/users/ids", [c[0] for c in calls])  # nobody to name
        with mock.patch.object(ib, "MATTERMOST_DAYS", 0):  # the floor passes every last post: nothing to fetch
            _, _, calls = self.fetch([], viewed=0)
        self.assertFalse([c for c in calls if c[0].startswith("/channels/")])

    def test_a_full_reply_may_be_incomplete(self):
        with mock.patch.object(ib, "MATTERMOST_PAGE", 2):
            items, keys, _ = self.fetch([mm_post("a", "dm", "1"), mm_post("b", "dm", "2")])
        self.assertEqual((len(items), keys), (2, None))

    def test_codes_are_hidden_and_long_lines_cut(self):
        items, _, _ = self.fetch([mm_post("a", "dm", "x" * 60 + " 验证码 123456"), mm_post("b", "dm", "y" * 60),
                                  mm_post("c", "dm", "")])
        self.assertEqual([i["summary"] for i in items], [ib.HIDDEN, "y" * ib.MAX_SUMMARY, "（附件）"])


class CardPageTests(unittest.TestCase):
    def test_newest_first_escaped_and_capped(self):
        items = [{"source": "Gmail", "who": f"<p{n}>", "summary": f"s{n}&",
                  "when": (NOW - dt.timedelta(minutes=n)).isoformat()} for n in (90, 5, 30, 2000, 60, 10, 15)]
        page = ib.card_page(items, NOW, "m")
        self.assertLess(page.index("&lt;p5&gt;"), page.index("&lt;p10&gt;"))
        self.assertIn("s5&amp;", page)
        self.assertNotIn("&lt;p2000&gt;", page)  # only the newest five show
        self.assertIn("另有 2 条", page)
        self.assertIn("7 条新提醒", page)
        self.assertIn("1 小时前", page)

    def test_ago(self):
        for minutes, label in ((0, "0 分钟前"), (59, "59 分钟前"), (60, "1 小时前"), (1500, "1 天前")):
            self.assertEqual(ib.ago((NOW - dt.timedelta(minutes=minutes)).isoformat(), NOW), label)


class PollTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config, self.cards = self.root / "config", self.root / "cards"
        self.config.mkdir()
        self.cards.mkdir()
        (self.config / "gmail").write_text("me@gmail.com\nabcd efgh ijkl mnop\n")
        (self.config / "github-token").write_text("tok\n")
        self.rendered, self.render_error = [], None

    def run_poll(self, mails, notes, mail_error=None):
        fake = FakeIMAP(mails)

        def get(url, headers=None, data=None, timeout=30):
            if "/notifications" in url:
                return json.dumps(notes).encode()
            if "/api/v4" in url:
                return self.mattermost(url, headers, data, timeout)
            return ollama_reply("摘要")

        def connect(host, ssl_context=None, timeout=None):
            if mail_error:
                raise mail_error
            return fake(host, ssl_context, timeout)

        def render(page, out, chrome):
            if self.render_error:
                raise self.render_error
            self.rendered.append(page)
            out.write_bytes(b"png")

        with mock.patch.object(ib, "GmailIMAP", connect), mock.patch.object(ib, "get", get), \
                mock.patch.object(ib, "render", render), mock.patch("sys.stderr"):
            new = ib.poll(self.cards, self.config, "http://o", "m", "chrome")
        return new, fake

    def pending(self):
        return [i["key"] for i in ib.read_state(self.cards)["pending"]]

    def test_new_items_make_a_card_and_are_not_repeated(self):
        new, fake = self.run_poll({111: mail()}, [notification(7)])
        self.assertEqual([i["key"] for i in new], ["gmail:111", "github:7:2026-11-27T05:50:00Z"])
        self.assertIn(("login", "me@gmail.com", "abcdefghijklmnop"), fake.calls)  # spaces dropped
        self.assertTrue((self.cards / "pending.png").is_file())
        saved = ib.read_state(self.cards)["pending"][0]
        self.assertEqual(set(saved), {"key", "source", "who", "summary", "when"})  # no mail text on disk
        new, _ = self.run_poll({111: mail()}, [notification(7)])
        self.assertEqual((new, len(self.rendered)), ([], 1))

    def test_items_read_elsewhere_leave_the_card(self):
        self.run_poll({111: mail(), 222: mail()}, [notification(7)])
        self.run_poll({222: mail()}, [])
        self.assertEqual(self.pending(), ["gmail:222"])
        self.run_poll({}, [])
        self.assertFalse((self.cards / "pending.png").exists())
        self.assertEqual(self.pending(), [])

    def test_ollama_failing_once_means_subjects_for_the_rest(self):
        calls = []

        def get(url, headers=None, data=None, timeout=30):
            if "/notifications" in url:
                return b"[]"
            calls.append(url)
            raise OSError("asleep")

        with mock.patch.object(ib, "GmailIMAP", FakeIMAP({1: mail(subject="A"), 2: mail(subject="B")})), \
                mock.patch.object(ib, "get", get), mock.patch.object(ib, "render", lambda p, o, c: o.write_bytes(b"x")), \
                mock.patch("sys.stderr"):
            new = ib.poll(self.cards, self.config, "http://o", "m", "chrome")
        self.assertEqual(([i["summary"] for i in new], len(calls)), (["A", "B"], 1))

    def test_a_lost_card_is_rendered_again_and_a_taken_one_is_not_repeated(self):
        self.run_poll({111: mail()}, [])
        (self.cards / "pending.png").unlink()  # as if power failed before the card was saved
        self.run_poll({111: mail()}, [])
        self.assertEqual(self.pending(), ["gmail:111"])
        self.assertTrue((self.cards / "pending.png").is_file())
        ib.done(self.cards, ib.take(self.cards))
        new, _ = self.run_poll({111: mail()}, [])
        self.assertEqual((new, self.pending()), ([], []))  # not shown a second time

    def test_a_failed_upload_shows_the_items_again(self):
        self.run_poll({111: mail()}, [])
        self.assertIsNotNone(ib.take(self.cards))  # the upload then fails: no done
        card = ib.take(self.cards)
        self.assertEqual(card.read_bytes(), b"png")
        ib.done(self.cards, card)
        self.assertEqual((self.pending(), ib.take(self.cards)), ([], None))
        self.assertFalse((self.cards / "pending.png").exists())
        self.assertFalse(card.exists())

    def test_overlapping_rotations_clear_only_what_each_showed(self):
        self.run_poll({111: mail()}, [])
        first = ib.take(self.cards)  # waits for the frame...
        self.run_poll({111: mail()}, [notification(7)])
        second = ib.take(self.cards)  # ...while the next rotation takes A+B
        self.assertNotEqual(first, second)
        ib.done(self.cards, first)
        self.assertEqual(self.pending(), ["github:7:2026-11-27T05:50:00Z"])  # B was not on the first card
        ib.done(self.cards, second)
        self.assertEqual(self.pending(), [])

    def test_items_beyond_the_card_wait_for_the_next_one(self):
        mails = {n: mail(subject=f"S{n}") for n in range(1, 8)}
        self.run_poll(mails, [])
        self.assertIn("另有 2 条", self.rendered[-1])
        ib.done(self.cards, ib.take(self.cards))
        self.assertEqual(len(self.pending()), 2)  # not on that card, so not cleared
        self.run_poll(mails, [])
        self.assertNotIn("另有", self.rendered[-1])
        ib.done(self.cards, ib.take(self.cards))
        self.assertEqual(self.pending(), [])

    def test_old_failed_copies_are_cleaned_up(self):
        self.run_poll({111: mail()}, [])
        cards = [ib.take(self.cards) for _ in range(5)]
        self.assertEqual([c.exists() for c in cards], [False, False, True, True, True])
        self.assertEqual(len(ib.read_state(self.cards)["taken"]), 3)
        orphan = self.cards / "shown-0123456789ab.png"
        orphan.write_bytes(b"left by a crash before the state was written")
        ib.take(self.cards)
        self.assertFalse(orphan.exists())
        self.assertEqual(len([c for c in self.cards.glob("shown-*.png") if ib.COPY.fullmatch(c.name)]), 3)

    def test_take_leaves_other_files_alone(self):
        self.run_poll({111: mail()}, [])
        (self.cards / "shown-family.png").write_bytes(b"mine")
        (self.cards / "shown-0123456789ab.png").mkdir()
        self.assertIsNotNone(ib.take(self.cards))
        self.assertTrue((self.cards / "shown-family.png").exists())
        self.assertTrue((self.cards / "shown-0123456789ab.png").is_dir())

    def test_items_that_came_after_the_take_stay_and_are_drawn_again(self):
        self.run_poll({111: mail()}, [])
        card = ib.take(self.cards)
        self.run_poll({111: mail()}, [notification(7)])  # B arrives while A is on its way to the frame
        ib.done(self.cards, card)
        self.assertEqual(self.pending(), ["github:7:2026-11-27T05:50:00Z"])
        self.assertIsNone(ib.take(self.cards))  # pending.png still shows A too
        self.run_poll({111: mail()}, [notification(7)])
        self.assertNotIn("Lunch", self.rendered[-1].split("<ul>")[1])
        self.assertIsNotNone(ib.take(self.cards))

    def test_done_after_another_card_changes_nothing(self):
        self.run_poll({111: mail()}, [])
        card = ib.take(self.cards)
        ib.done(self.cards, self.root / "trending" / "card-3.png")  # the take's card never went up
        ib.done(self.cards, self.cards / "shown-1.png")  # not one this folder handed out
        self.assertEqual(self.pending(), ["gmail:111"])
        self.assertTrue(card.exists())

    def test_a_card_not_yet_drawn_is_never_taken_and_is_drawn_next_time(self):
        self.run_poll({111: mail()}, [])
        (self.cards / "pending.png").write_bytes(b"old card")
        state = ib.read_state(self.cards)
        ib.write_json(self.cards / "state.json", {**state, "drawn": False})  # power failed before the card
        self.assertIsNone(ib.take(self.cards))
        self.run_poll({111: mail()}, [])
        self.assertEqual(len(self.rendered), 2)
        self.run_poll({111: mail()}, [])
        self.assertEqual(len(self.rendered), 2)  # up to date: not drawn again
        self.assertIsNotNone(ib.take(self.cards))

    def test_a_render_cut_short_never_shows_an_old_card_and_is_redone(self):
        self.run_poll({111: mail()}, [])
        self.render_error = OSError("power cut")
        with self.assertRaises(OSError):
            self.run_poll({111: mail()}, [notification(7)])  # B arrives; drawing A+B fails
        self.render_error = None
        self.assertIsNone(ib.take(self.cards))  # the card still shows only A: not taken
        (self.config / "github-token").unlink()
        with self.assertRaisesRegex(RuntimeError, "every source failed"):
            self.run_poll({}, [], mail_error=OSError("imap down"))  # nothing answers, but the card is redrawn
        self.assertEqual(self.pending(), ["gmail:111", "github:7:2026-11-27T05:50:00Z"])
        self.assertIn("Add a board", self.rendered[-1])
        self.assertIsNotNone(ib.take(self.cards))

    def test_a_stale_card_goes_when_nothing_is_pending(self):
        (self.cards / "pending.png").write_bytes(b"stale")  # drawn, then power failed before the state
        self.assertIsNone(ib.take(self.cards))
        self.run_poll({}, [])
        self.assertFalse((self.cards / "pending.png").exists())

    def test_a_failed_source_keeps_its_items(self):
        self.run_poll({111: mail()}, [notification(7)])
        self.run_poll({}, [], mail_error=OSError("imap down"))
        self.assertEqual(self.pending(), ["gmail:111"])  # GitHub answered "none unread"; Gmail did not answer

    def test_every_source_failing_is_an_error(self):
        (self.config / "github-token").unlink()
        with self.assertRaisesRegex(RuntimeError, "every source failed"):
            self.run_poll({}, [], mail_error=OSError("imap down"))

    def test_mattermost_joins_the_card_and_leaves_when_read(self):
        (self.config / "mattermost").write_text("https://mm.example.com\nmmtok\n")
        self.mattermost, _ = mattermost([mm_post("p1", "dm", "hello")])
        with mock.patch.object(ib.time, "time", return_value=MS / 1000):
            new, _ = self.run_poll({}, [])
            self.assertEqual([(i["key"], i["summary"]) for i in new], [("mattermost:p1", "hello")])
            self.mattermost, _ = mattermost([mm_post("p1", "dm", "hello")], viewed=MS)  # read on the phone
            self.run_poll({}, [])
        self.assertEqual(self.pending(), [])

    def test_missing_config_skips_that_source(self):
        (self.config / "gmail").unlink()
        new, fake = self.run_poll({111: mail()}, [notification(7)])
        self.assertEqual(([i["source"] for i in new], fake.calls), (["GitHub"], []))


class TakeAndMainTests(unittest.TestCase):
    def test_take_then_done_from_the_command_line(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sys.stdout") as out:
            cards = Path(folder)
            self.assertEqual(ib.main(["take", folder]), 1)  # nothing pending: quiet
            (cards / "pending.png").write_bytes(b"png")
            (cards / "state.json").write_text(json.dumps({"seen": ["a"], "pending": [{"key": "a", "when": "2026-11-27T05:00:00+00:00"}], "drawn": True}))
            self.assertEqual(ib.main(["take", folder]), 0)
            card = "".join(c.args[0] for c in out.write.call_args_list).strip()
            self.assertEqual(Path(card).read_bytes(), b"png")
            self.assertEqual(ib.main(["done", folder, card]), 0)
            self.assertEqual(ib.read_state(cards)["pending"], [])
            self.assertEqual(ib.main(["take", folder]), 1)

    def test_card_goes_only_with_done(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sys.stderr"):
            for argv in (["done", folder], ["take", folder, "x.png"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    ib.main(argv)

    def test_a_busy_lock_returns_at_once(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sys.stderr"), \
                open(Path(folder) / ".lock", "w") as held, mock.patch.object(ib, "poll") as poll:
            ib.fcntl.flock(held, ib.fcntl.LOCK_EX)  # another poll is running
            (Path(folder) / "pending.png").write_bytes(b"png")
            self.assertEqual((ib.main(["poll", folder]), ib.main(["take", folder])), (1, 1))
            poll.assert_not_called()
            self.assertTrue((Path(folder) / "pending.png").exists())

    def test_the_folder_is_private(self):
        with tempfile.TemporaryDirectory() as folder:
            cards = Path(folder) / "cards"
            self.assertEqual(ib.main(["take", str(cards)]), 1)
            self.assertEqual(cards.stat().st_mode & 0o777, 0o700)
            cards.chmod(0o755)  # made before, by hand
            ib.main(["take", str(cards)])
            self.assertEqual(cards.stat().st_mode & 0o777, 0o700)

    def test_poll_errors_exit_1(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sys.stderr"):
            with mock.patch.object(ib, "poll", side_effect=RuntimeError("every source failed")):
                self.assertEqual(ib.main(["poll", folder]), 1)


if __name__ == "__main__":
    unittest.main()
