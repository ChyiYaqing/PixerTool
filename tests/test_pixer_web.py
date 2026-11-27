import http.client
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import pixer_web  # noqa: E402

pixer = pixer_web.pixer
JPEG = b"\xff\xd8\xff\xe0 jpeg bytes"


def setUpModule():
    # Keep the tests' frame locks away from real uploads on this computer. Set the
    # attribute directly: some tests end with patch.stopall, which would undo a patch.
    folder = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(folder.cleanup)
    unittest.addModuleCleanup(setattr, pixer, "LOCK_DIR", pixer.LOCK_DIR)
    pixer.LOCK_DIR = Path(folder.name)
    unittest.addModuleCleanup(setattr, pixer, "SHOWN_PATH", pixer.SHOWN_PATH)
    pixer.SHOWN_PATH = Path(folder.name) / "shown.png"


class WebUploadTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch.multiple(
            pixer, build_payload=unittest.mock.DEFAULT, upload_payload=unittest.mock.DEFAULT,
            remember_shown=unittest.mock.DEFAULT,
        )
        self.mocks = patcher.start()
        self.addCleanup(patcher.stop)
        self.seen = []

        def build(path, orientation, fit):
            self.seen.append((path, path.read_bytes(), orientation, fit))
            return b"payload", 1872, 1404

        self.mocks["build_payload"].side_effect = build
        self.mocks["upload_payload"].return_value = b"#end1#"

        self.server = pixer_web.PixerWebServer(("127.0.0.1", 0), "10.0.0.5", 6000)
        self.server.RequestHandlerClass.log_message = lambda *args: None
        thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()

    def post(self, body=JPEG, query="", headers=None):
        status, data = self.request("POST", "/upload" + query, body, headers)
        return status, json.loads(data)

    def test_page_offers_a_photo_picker(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'type="file" accept="image/*"', body)

    def test_unknown_path_is_not_found(self):
        self.assertEqual(self.request("GET", "/etc/passwd")[0], 404)
        self.assertEqual(self.request("POST", "/upload/../x", b"x")[0], 404)

    def test_photo_is_converted_and_sent_to_the_frame(self):
        status, result = self.post(JPEG, "?fit=contain")

        self.assertEqual((status, result["ok"]), (200, True))
        (path, data, orientation, fit), = self.seen
        self.assertEqual((data, orientation, fit), (JPEG, "auto", "contain"))
        self.assertFalse(path.exists())
        upload = self.mocks["upload_payload"]
        self.assertEqual(upload.call_args.args, (b"payload", "10.0.0.5", 6000))
        self.mocks["remember_shown"].assert_called_once_with(b"payload", 1872, 1404)

    def test_the_shown_image_is_served_fresh(self):
        pixer.SHOWN_PATH.unlink(missing_ok=True)
        self.assertEqual(self.request("GET", "/shown.png")[0], 404)
        pixer.SHOWN_PATH.write_bytes(b"\x89PNG fake")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", "/shown.png")
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"\x89PNG fake"))
        self.assertEqual(response.getheader("Content-Type"), "image/png")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertIn(b'href="/shown.png"', self.request("GET", "/")[1])

    def test_fit_defaults_to_cover(self):
        self.post()
        self.assertEqual(self.seen[0][3], "cover")

    def test_unknown_fit_is_rejected(self):
        status, result = self.post(query="?fit=stretch")
        self.assertEqual((status, result["ok"]), (400, False))
        self.mocks["build_payload"].assert_not_called()

    def test_empty_upload_asks_for_a_photo(self):
        status, result = self.post(b"")
        self.assertEqual(status, 400)
        self.assertIn("Choose a photo", result["message"])

    def test_oversized_upload_is_refused_before_reading(self):
        size = str(pixer_web.MAX_UPLOAD + 1)
        status, result = self.post(JPEG, headers={"Content-Length": size})
        self.assertEqual(status, 413)
        self.mocks["build_payload"].assert_not_called()

    def test_unconvertible_photo_is_explained(self):
        self.mocks["build_payload"].side_effect = pixer.PixerError("ffmpeg conversion failed: bad data")
        status, result = self.post()
        self.assertEqual(status, 400)
        self.assertIn("could not be converted: ffmpeg conversion failed", result["message"])
        self.mocks["upload_payload"].assert_not_called()

    def test_conversion_error_hides_the_temporary_path(self):
        def fail(path, orientation, fit):
            raise pixer.PixerError(f"could not read image dimensions: {path}: Invalid data")

        self.mocks["build_payload"].side_effect = fail
        _, result = self.post()
        self.assertIn("dimensions: the photo: Invalid data", result["message"])
        self.assertNotIn("pixer-web-", result["message"])

    def test_frame_failure_is_explained(self):
        self.mocks["upload_payload"].side_effect = pixer.PixerError("cannot reach Pixer at 10.0.0.5:6000")
        status, result = self.post()
        self.assertEqual((status, result["ok"]), (502, False))
        self.assertIn("cannot reach Pixer", result["message"])
        self.mocks["remember_shown"].assert_not_called()

    def raw_connection(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        return connection

    def test_busy_frame_refuses_before_the_body_arrives(self):
        connection = self.raw_connection()
        with self.server.frame_lock:
            connection.putrequest("POST", "/upload")
            connection.putheader("Content-Length", str(10 * 1024 * 1024))
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            self.assertIn("Another photo is being sent", json.loads(response.read())["message"])
        self.mocks["build_payload"].assert_not_called()
        self.assertEqual(self.post()[0], 200)

    def test_frame_held_by_another_process_is_refused_before_the_body_arrives(self):
        self.server.min_battery = 20
        connection = self.raw_connection()
        with unittest.mock.patch.object(pixer, "read_battery", return_value=80) as battery:
            with pixer.frame_lock("10.0.0.5", 6000, wait=0):
                connection.putrequest("POST", "/upload")
                connection.putheader("Content-Length", str(10 * 1024 * 1024))
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertIn("busy with another upload", json.loads(response.read())["message"])
            self.mocks["build_payload"].assert_not_called()
            battery.assert_not_called()
            self.mocks["upload_payload"].assert_not_called()
            self.assertEqual(self.post()[0], 200)

    def test_an_unopenable_lock_file_is_explained(self):
        refused = PermissionError(13, "Permission denied", "/tmp/pixer-10.0.0.5-6000.lock")
        with unittest.mock.patch.object(pixer, "frame_lock", side_effect=refused):
            status, result = self.post()
        self.assertEqual((status, result["ok"]), (500, False))
        self.assertIn("Could not reserve the frame: [Errno 13] Permission denied", result["message"])
        self.mocks["build_payload"].assert_not_called()
        self.assertEqual(self.post()[0], 200)

    def test_low_battery_is_refused_without_uploading(self):
        self.server.min_battery = 20
        with unittest.mock.patch.object(pixer, "read_battery", return_value=19) as battery:
            status, result = self.post()
        battery.assert_called_once_with("10.0.0.5", 6000)
        self.assertEqual((status, result["ok"]), (503, False))
        self.assertIn("battery is 19%, below 20%", result["message"])
        self.mocks["upload_payload"].assert_not_called()

    def test_battery_at_the_threshold_uploads(self):
        self.server.min_battery = 20
        with unittest.mock.patch.object(pixer, "read_battery", return_value=20):
            self.assertEqual(self.post()[0], 200)
        self.mocks["upload_payload"].assert_called_once()

    def test_battery_is_not_read_without_a_threshold(self):
        with unittest.mock.patch.object(pixer, "read_battery") as battery:
            self.assertEqual(self.post()[0], 200)
        battery.assert_not_called()

    def test_frame_is_free_before_the_reply_is_sent(self):
        # A client may send the next photo as soon as it reads the reply.
        locked = []
        reply = pixer_web.UploadHandler.reply

        def recording_reply(handler, status, message):
            locked.append(handler.server.frame_lock.locked())
            reply(handler, status, message)

        with unittest.mock.patch.object(pixer_web.UploadHandler, "reply", recording_reply):
            statuses = [self.post()[0], self.post(b"not a photo")[0]]
            self.mocks["upload_payload"].side_effect = pixer.PixerError("cannot reach Pixer")
            statuses.append(self.post()[0])
        self.assertEqual(statuses, [200, 400, 502])
        self.assertEqual(locked, [False, False, False])

    def test_stalled_upload_times_out_and_frees_the_frame(self):
        with unittest.mock.patch.object(pixer_web.UploadHandler, "timeout", 0.2):
            connection = self.raw_connection()
            connection.putrequest("POST", "/upload")
            connection.putheader("Content-Length", "100")
            connection.endheaders(JPEG)
            response = connection.getresponse()
            self.assertEqual(response.status, 408)
        self.assertEqual(self.post()[0], 200)

    def test_trickling_upload_hits_the_overall_deadline(self):
        connection = self.raw_connection()
        stop = threading.Event()

        def trickle(sock):
            while not stop.wait(0.05):
                try:
                    sock.sendall(b"\xff")
                except OSError:
                    return

        # Shorten the deadline before the headers go out: the server reads it
        # as soon as they arrive.
        with unittest.mock.patch.object(pixer_web, "UPLOAD_DEADLINE", 0.4):
            connection.putrequest("POST", "/upload")
            connection.putheader("Content-Length", "1000")
            connection.endheaders()
            # Keep our own reference: reading the response sets connection.sock to None.
            sender = threading.Thread(target=trickle, args=(connection.sock,), daemon=True)
            sender.start()
            started = time.monotonic()
            try:
                response = connection.getresponse()
            finally:
                stop.set()
                sender.join()
        self.assertEqual(response.status, 408)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.post()[0], 200)

    def test_malformed_host_is_refused(self):
        self.assertEqual(self.post(headers={"Host": "[not-an-ip"})[0], 403)

    def test_only_jpeg_and_png_reach_ffmpeg(self):
        png = b"\x89PNG\r\n\x1a\n rest"
        self.assertEqual(self.post(png)[0], 200)
        cases = {
            b"#EXTM3U\n#EXT-X-VERSION:3\nfile:///etc/passwd\n": "Only JPEG and PNG",
            b"\x00\x00\x00\x18ftypheic\x00\x00": "HEIC photos are not supported",
        }
        for body, message in cases.items():
            with self.subTest(message=message):
                status, result = self.post(body)
                self.assertEqual(status, 400)
                self.assertIn(message, result["message"])
        self.assertEqual(len(self.seen), 1)

    def test_other_websites_cannot_upload(self):
        port = self.server.server_address[1]
        refused = [
            {"Origin": "http://evil.example"},
            {"Origin": "null"},
            {"Host": f"evil.example:{port}"},
            {"Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}"},
        ]
        for headers in refused:
            with self.subTest(headers=headers):
                self.assertEqual(self.post(headers=headers)[0], 403)
        self.mocks["build_payload"].assert_not_called()

    def test_the_page_itself_and_shortcuts_can_upload(self):
        port = self.server.server_address[1]
        allowed = [
            {},
            {"Origin": f"http://127.0.0.1:{port}"},
            {"Host": f"pixer-mac.local:{port}", "Origin": f"http://pixer-mac.local:{port}"},
        ]
        for headers in allowed:
            with self.subTest(headers=headers):
                self.assertEqual(self.post(headers=headers)[0], 200)


class StartupTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch("sys.stdout", out), unittest.mock.patch("sys.stderr", err):
            status = pixer_web.main(argv)
        return status, out.getvalue(), err.getvalue()

    def test_finds_the_frame_when_no_host_is_given(self):
        with unittest.mock.patch.object(pixer, "local_network") as network, \
                unittest.mock.patch.object(pixer, "discover", return_value=["10.1.7.102"]), \
                unittest.mock.patch("sys.stdout", io.StringIO()):
            network.return_value = pixer.ipaddress.IPv4Network("10.1.7.0/24")
            self.assertEqual(pixer_web.find_frame(6000), "10.1.7.102")

    def test_refuses_to_guess_between_frames(self):
        with unittest.mock.patch.object(pixer, "local_network"), \
                unittest.mock.patch.object(pixer, "discover", return_value=["10.0.0.2", "10.0.0.3"]), \
                unittest.mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaisesRegex(pixer.PixerError, "several frames"):
                pixer_web.find_frame(6000)

    def test_no_frame_found_fails_before_serving(self):
        with unittest.mock.patch.object(pixer, "local_network"), \
                unittest.mock.patch.object(pixer, "discover", return_value=[]), \
                unittest.mock.patch.object(pixer_web, "PixerWebServer") as server:
            status, _, err = self.run_main([])
        self.assertEqual(status, 1)
        self.assertIn("pass --host", err)
        server.assert_not_called()

    def test_prints_the_address_to_open_on_a_phone(self):
        with unittest.mock.patch.object(pixer_web, "PixerWebServer") as server, \
                unittest.mock.patch.object(pixer, "local_source_address", return_value="10.1.7.18"):
            server.return_value.serve_forever.side_effect = KeyboardInterrupt
            status, out, _ = self.run_main(["--host", "10.1.7.102"])
        self.assertEqual(status, 0)
        server.assert_called_once_with(("0.0.0.0", 8000), "10.1.7.102", 6000, None)
        self.assertIn("open http://10.1.7.18:8000", out)

    def test_passes_the_battery_threshold_to_the_server(self):
        with unittest.mock.patch.object(pixer_web, "PixerWebServer") as server, \
                unittest.mock.patch.object(pixer, "local_source_address", return_value="10.1.7.18"):
            server.return_value.serve_forever.side_effect = KeyboardInterrupt
            self.run_main(["--host", "10.1.7.102", "--min-battery", "20"])
        server.assert_called_once_with(("0.0.0.0", 8000), "10.1.7.102", 6000, 20)


if __name__ == "__main__":
    unittest.main()
