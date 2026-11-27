import importlib.util
import io
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
import zlib
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "pixer_upload.py"
SPEC = importlib.util.spec_from_file_location("pixer_upload", MODULE_PATH)
pixer_upload = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pixer_upload)

CARD_PATH = Path(__file__).parents[1] / "misc" / "make_test_card.py"
CARD_SPEC = importlib.util.spec_from_file_location("make_test_card", CARD_PATH)
make_test_card = importlib.util.module_from_spec(CARD_SPEC)
assert CARD_SPEC.loader is not None
CARD_SPEC.loader.exec_module(make_test_card)


def setUpModule():
    # Keep the tests' frame locks away from real uploads on this computer. Set the
    # attribute directly: some tests end with patch.stopall, which would undo a patch.
    folder = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(folder.cleanup)
    unittest.addModuleCleanup(setattr, pixer_upload, "LOCK_DIR", pixer_upload.LOCK_DIR)
    pixer_upload.LOCK_DIR = Path(folder.name)
    unittest.addModuleCleanup(setattr, pixer_upload, "SHOWN_PATH", pixer_upload.SHOWN_PATH)
    pixer_upload.SHOWN_PATH = Path(folder.name) / "shown" / "shown.png"


class PixerEncodingTests(unittest.TestCase):
    def test_java_div_truncates_toward_zero(self):
        self.assertEqual(pixer_upload.java_div(-3, 4), 0)
        self.assertEqual(pixer_upload.java_div(-5, 4), -1)
        self.assertEqual(pixer_upload.java_div(5, 4), 1)

    def test_dither_exact_gray_levels(self):
        gray = bytes([0, 17, 34, 51, 68, 85, 102, 119])
        self.assertEqual(
            pixer_upload.dither_16_levels(gray, 8, 1),
            bytearray(range(8)),
        )

    def test_rgb_to_gray_matches_android_integer_weights(self):
        rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])
        self.assertEqual(
            pixer_upload.rgb_to_gray(rgb, 4, 1),
            bytes([76, 149, 29, 254]),
        )

    def test_landscape_packing_low_nibble_first(self):
        levels = bytes(range(8))
        packed = pixer_upload.pack_4bit(levels, 4, 2)
        self.assertEqual(packed, bytes([0x10, 0x32, 0x54, 0x76]))

    def test_portrait_packing_rotates_like_android(self):
        levels = bytes(range(8))
        packed = pixer_upload.pack_4bit(levels, 2, 4)
        self.assertEqual(packed, bytes([0x31, 0x75, 0x20, 0x64]))

    def test_protocol_header(self):
        self.assertEqual(
            pixer_upload.file_header(1_314_144),
            b"#file#000801314144imagebin",
        )


def read_png(data: bytes) -> tuple[int, int, bytes]:
    """Width, height and pixels of an 8-bit grayscale PNG without filters."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks, at = {}, 8
    while at < len(data):
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        chunks[kind] = data[at + 8:at + 8 + length]
        at += 12 + length
    width, height, depth, color = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, color) == (8, 0)
    rows = zlib.decompress(chunks[b"IDAT"])
    return width, height, b"".join(rows[y * (width + 1) + 1:(y + 1) * (width + 1)] for y in range(height))


class ShownImageTests(unittest.TestCase):
    def test_unpacking_undoes_packing_both_ways_round(self):
        for width, height in ((6, 4), (4, 6)):
            with self.subTest(size=(width, height)):
                levels = bytes((i * 7 + i // 3) % 16 for i in range(width * height))
                packed = pixer_upload.pack_4bit(levels, width, height)
                self.assertEqual(pixer_upload.unpack_4bit(packed, width, height), levels)

    def test_png_holds_the_levels_as_grays(self):
        width, height, pixels = read_png(pixer_upload.gray_png(bytes([0, 1, 15, 8, 0, 15]), 3, 2))
        self.assertEqual((width, height, pixels), (3, 2, bytes([0, 17, 255, 136, 0, 255])))

    def test_the_shown_image_is_kept_and_a_bad_one_only_warns(self):
        packed = pixer_upload.pack_4bit(bytes(range(16)) * 2, 4, 8)
        pixer_upload.remember_shown(packed, 4, 8)
        self.assertEqual(read_png(pixer_upload.SHOWN_PATH.read_bytes())[:2], (4, 8))
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            pixer_upload.remember_shown(b"short", 4, 8)
        self.assertIn("could not keep", err.getvalue())
        self.assertEqual(list(pixer_upload.SHOWN_PATH.parent.glob("*")), [])  # no leftovers, and no stale picture


class DitherTests(unittest.TestCase):
    METHODS = ("floyd-steinberg", "atkinson", "none")

    def test_rounded_gray_keeps_neutral_grays_exact(self):
        rgb = bytes(value for value in (0, 17, 128, 254, 255) for _ in range(3))
        self.assertEqual(pixer_upload.rgb_to_gray(rgb, 5, 1), bytes([0, 16, 127, 253, 254]))
        self.assertEqual(
            pixer_upload.rgb_to_gray(rgb, 5, 1, rounded=True), bytes([0, 17, 128, 254, 255])
        )

    def test_exact_levels_pass_through_every_method(self):
        gray = bytes(17 * level for level in range(16))
        for method in self.METHODS:
            with self.subTest(method=method):
                self.assertEqual(
                    pixer_upload.diffuse_16_levels(gray, 16, 1, method), bytearray(range(16))
                )

    def test_white_stays_white(self):
        gray = bytes([255]) * (64 * 8)
        for method in self.METHODS:
            with self.subTest(method=method):
                self.assertEqual(
                    set(pixer_upload.diffuse_16_levels(gray, 64, 8, method)), {15}
                )

    def test_floyd_steinberg_turns_a_half_level_into_a_checkerboard(self):
        levels = pixer_upload.diffuse_16_levels(bytes([9] * 4), 2, 2, "floyd-steinberg")
        self.assertEqual(levels, bytearray([1, 0, 0, 1]))

    def test_atkinson_reaches_two_pixels_right_and_two_rows_down(self):
        gray = bytes([9, 0, 9, 0, 0, 0, 9, 0, 0])
        levels = pixer_upload.diffuse_16_levels(gray, 3, 3, "atkinson")
        self.assertEqual(levels, bytearray([1, 0, 0, 0, 0, 0, 0, 0, 0]))

    def test_none_rounds_to_the_nearest_level(self):
        levels = pixer_upload.diffuse_16_levels(bytes([8, 9, 25, 26]), 4, 1, "none")
        self.assertEqual(levels, bytearray([0, 1, 1, 2]))

    def test_floyd_steinberg_preserves_average_brightness(self):
        width, height = 64, 64
        gray = bytes([100]) * (width * height)
        levels = pixer_upload.diffuse_16_levels(gray, width, height, "floyd-steinberg")
        self.assertAlmostEqual(sum(levels) * 17 / len(levels), 100, delta=1)
        self.assertEqual(set(levels), {5, 6})

    def payload(self, *dither):
        # Gray 9 truncates to 8 in the app's grayscale, which changes the result.
        with unittest.mock.patch.multiple(
            pixer_upload, target_size=unittest.mock.DEFAULT, convert_to_rgb=unittest.mock.DEFAULT
        ) as mocks:
            mocks["target_size"].return_value = (4, 2)
            mocks["convert_to_rgb"].return_value = bytes([9]) * (4 * 2 * 3)
            payload, _, _ = pixer_upload.build_payload(Path("x"), "auto", "cover", *dither)
        return payload

    def test_app_dither_is_the_default_and_matches_the_app(self):
        self.assertEqual(self.payload(), bytes.fromhex("10100101"))
        self.assertEqual(self.payload("app"), bytes.fromhex("10100101"))

    def test_other_methods_use_the_rounded_grayscale(self):
        self.assertEqual(self.payload("none"), bytes.fromhex("11111111"))
        self.assertEqual(self.payload("floyd-steinberg"), bytes.fromhex("01011010"))

    def test_cli_passes_the_dither_method_through(self):
        with unittest.mock.patch.object(pixer_upload, "build_payload") as build, \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            build.return_value = (b"\x00", 2, 1)
            status = pixer_upload.main([__file__, "--dry-run", "--dither", "atkinson"])
        self.assertEqual(status, 0)
        build.assert_called_once_with(Path(__file__), "auto", "cover", "atkinson")

    def test_cli_rejects_an_unknown_method(self):
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO), \
                self.assertRaises(SystemExit):
            pixer_upload.main([__file__, "--dry-run", "--dither", "ordered"])


class TestCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.card = make_test_card.test_card()
        cls.width = make_test_card.WIDTH

    def pixel(self, column, row):
        return self.card[row * self.width + column]

    def test_card_fills_the_landscape_panel(self):
        self.assertEqual((make_test_card.WIDTH, make_test_card.HEIGHT), pixer_upload.LANDSCAPE_SIZE)
        self.assertEqual(len(self.card), make_test_card.WIDTH * make_test_card.HEIGHT)

    def test_top_band_shows_all_sixteen_levels_in_order(self):
        top = self.card[: self.width]
        self.assertEqual(sorted(set(top)), [17 * level for level in range(16)])
        self.assertEqual(list(top), sorted(top))
        self.assertEqual(self.pixel(0, 0), 0)
        self.assertEqual(self.pixel(self.width - 1, 0), 255)

    def test_middle_band_ramps_from_black_to_white(self):
        row = make_test_card.BAND + 1
        self.assertEqual(self.pixel(0, row), 0)
        self.assertEqual(self.pixel(self.width - 1, row), 255)
        self.assertGreater(len(set(self.card[row * self.width : (row + 1) * self.width])), 250)

    def test_script_writes_a_binary_pgm(self):
        with unittest.mock.patch.object(Path, "write_bytes") as write:
            self.assertEqual(make_test_card.main(["card.pgm"]), 0)
        data = write.call_args.args[0]
        self.assertTrue(data.startswith(b"P5\n1872 1404\n255\n"))
        self.assertTrue(data.endswith(self.card))

    def test_help_does_not_write_a_file(self):
        with unittest.mock.patch.object(Path, "write_bytes") as write, \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO), \
                self.assertRaises(SystemExit):
            make_test_card.main(["--help"])
        write.assert_not_called()

    def test_stripes_alternate_at_each_width(self):
        cell = self.width // 4
        vertical_row = 2 * make_test_card.BAND
        horizontal_start = vertical_row + (make_test_card.HEIGHT - vertical_row) // 2
        for index, width in enumerate((1, 2, 3, 4)):
            start = index * cell
            with self.subTest(width=width):
                across = [self.pixel(start + offset, vertical_row) for offset in range(2 * width)]
                self.assertEqual(across, [0] * width + [255] * width)
                down = [self.pixel(start, horizontal_start + offset) for offset in range(2 * width)]
                self.assertEqual(down, [0] * width + [255] * width)


class ImageSizeTests(unittest.TestCase):
    def probe(self, stdout):
        result = unittest.mock.Mock(stdout=stdout, stderr="")
        with unittest.mock.patch.object(pixer_upload.shutil, "which", return_value="ffprobe"), \
                unittest.mock.patch.object(pixer_upload.subprocess, "run", return_value=result):
            return pixer_upload.probe_image_size(Path("photo.jpg"))

    def test_plain_image_keeps_its_size(self):
        self.assertEqual(self.probe("width=400\nheight=200\n"), (400, 200))

    def test_exif_rotated_photo_reports_the_shown_size(self):
        for rotation in ("-90", "90", "270"):
            with self.subTest(rotation=rotation):
                stdout = f"width=4032\nheight=3024\nrotation={rotation}\n"
                self.assertEqual(self.probe(stdout), (3024, 4032))

    def test_tilted_display_matrix_keeps_its_size(self):
        self.assertEqual(self.probe("width=400\nheight=200\nrotation=45\n"), (400, 200))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_real_jpeg_with_exif_orientation(self):
        with tempfile.TemporaryDirectory() as folder:
            plain = Path(folder) / "plain.jpg"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=white:s=400x200",
                 "-frames:v", "1", str(plain)],
                check=True,
            )
            # Big-endian EXIF with one IFD0 entry: Orientation (0x0112) = 6.
            tiff = b"MM\x00\x2a" + struct.pack(">IHHHIHHI", 8, 1, 0x0112, 3, 1, 6, 0, 0)
            app1 = b"Exif\x00\x00" + tiff
            data = plain.read_bytes()
            rotated = Path(folder) / "rotated.jpg"
            rotated.write_bytes(data[:2] + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + data[2:])
            self.assertEqual(pixer_upload.probe_image_size(plain), (400, 200))
            self.assertEqual(pixer_upload.probe_image_size(rotated), (200, 400))

    def test_upside_down_photo_keeps_its_size(self):
        self.assertEqual(self.probe("width=400\nheight=200\nrotation=180\n"), (400, 200))

    def test_portrait_phone_photo_chooses_portrait(self):
        with unittest.mock.patch.object(pixer_upload, "probe_image_size", return_value=(3024, 4032)):
            self.assertEqual(pixer_upload.target_size(Path("x"), "auto"), pixer_upload.PORTRAIT_SIZE)

    def test_stuck_ffmpeg_is_stopped(self):
        timeout = pixer_upload.subprocess.TimeoutExpired(["/opt/bin/ffprobe"], 60)
        with unittest.mock.patch.object(pixer_upload.shutil, "which", return_value="/opt/bin/ffprobe"), \
                unittest.mock.patch.object(pixer_upload.subprocess, "run", side_effect=timeout) as run:
            with self.assertRaisesRegex(pixer_upload.PixerError, "ffprobe took longer than 60 seconds"):
                pixer_upload.probe_image_size(Path("photo.jpg"))
        self.assertEqual(run.call_args.kwargs["timeout"], pixer_upload.FFMPEG_TIMEOUT)

    def test_unreadable_image_is_reported(self):
        with self.assertRaisesRegex(pixer_upload.PixerError, "could not read image dimensions"):
            self.probe("")


class TerminalStream(io.StringIO):
    """A StringIO that claims to be a terminal."""

    def isatty(self):
        return True


class ProgressTests(unittest.TestCase):
    def test_redirected_output_stays_line_based(self):
        stream = io.StringIO()
        progress = pixer_upload.Progress(1000, stream=stream)
        for count in range(100, 1001, 100):
            progress.sent(count)
        progress.close()
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[0], "Sent 10% (0.00/0.00 MB)")
        self.assertEqual(lines[-1], "Sent 100% (0.00/0.00 MB)")
        self.assertNotIn("\r", stream.getvalue())

    def test_terminal_output_redraws_one_line(self):
        stream = TerminalStream()
        progress = pixer_upload.Progress(1000, stream=stream)
        progress.sent(500)
        progress.sent(1000)
        progress.close()
        output = stream.getvalue()
        self.assertEqual(output.count("\r"), 2)
        self.assertEqual(output.count("\n"), 1)
        self.assertIn("\u2588" * 14 + "\u2591" * 14, output)
        self.assertIn("100%", output)

    def test_completion_keeps_the_finished_bar(self):
        stream = TerminalStream()
        progress = pixer_upload.Progress(1000, stream=stream)
        progress.sent(1000)
        progress.close()
        progress.waiting()
        progress.close()
        # str.splitlines would also break on the carriage returns.
        first, second, _ = stream.getvalue().split("\n")
        self.assertIn("100%", first)
        self.assertIn("Waiting for the frame to refresh", second)

    def test_close_is_quiet_when_nothing_was_drawn(self):
        stream = TerminalStream()
        pixer_upload.Progress(1000, stream=stream).close()
        self.assertEqual(stream.getvalue(), "")

    def test_wait_notice_is_printed_once_when_redirected(self):
        stream = io.StringIO()
        progress = pixer_upload.Progress(1000, stream=stream)
        for _ in range(5):
            progress.waiting()
        self.assertEqual(
            stream.getvalue().splitlines(), ["Waiting for the frame to refresh"]
        )


class ConnectionDiagnosticsTests(unittest.TestCase):
    def source_address(self, address):
        patcher = unittest.mock.patch.object(
            pixer_upload, "local_source_address", return_value=address
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_advice_when_mac_is_on_another_network(self):
        self.source_address("10.1.7.18")
        advice = pixer_upload.network_advice("192.168.1.1")
        self.assertIn("10.1.7.18", advice)
        self.assertIn("a different network", advice)
        self.assertIn("Wi-Fi", advice)

    def test_advice_when_mac_is_on_the_pixer_network(self):
        self.source_address("192.168.1.42")
        advice = pixer_upload.network_advice("192.168.1.1")
        self.assertIn("same network", advice)
        self.assertIn("asleep", advice)

    def test_advice_without_any_route(self):
        self.source_address(None)
        self.assertIn("no route", pixer_upload.network_advice("192.168.1.1"))

    def test_timeout_message_explains_the_silence(self):
        self.source_address("10.1.7.18")
        message = pixer_upload.unreachable_message(
            "192.168.1.1", 6000, 5.0, socket.timeout("timed out")
        )
        self.assertIn("no response within 5 seconds", message)
        self.assertIn("a different network", message)
        self.assertNotIn("timed out", message)

    def test_refused_message_points_at_the_wrong_device(self):
        self.source_address("192.168.1.20")
        message = pixer_upload.unreachable_message(
            "192.168.1.1", 6000, 5.0, ConnectionRefusedError(61, "Connection refused")
        )
        self.assertIn("nothing is listening on port 6000", message)

    def test_probe_reports_a_closed_port(self):
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            port = spare.getsockname()[1]
        with self.assertRaises(pixer_upload.PixerError) as caught:
            pixer_upload.probe_device("127.0.0.1", port, timeout=2.0)
        self.assertIn(f"cannot reach Pixer at 127.0.0.1:{port}", str(caught.exception))

    def test_upload_fails_before_sending_when_unreachable(self):
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            port = spare.getsockname()[1]
        with self.assertRaises(pixer_upload.PixerError) as caught:
            pixer_upload.upload_payload(b"\x00" * 16, "127.0.0.1", port, 2.0)
        self.assertIn("cannot reach Pixer", str(caught.exception))


class WifiSetupTests(unittest.TestCase):
    EMPTY_SCAN = b"00" + bytes(32) + b"0"
    DEVICE_ID = b"0123456789AB@1234TEST5678"

    def setUp(self):
        connection = unittest.mock.patch.object(pixer_upload, "connect_to_pixer")
        self.socket = connection.start().return_value.__enter__.return_value
        self.addCleanup(connection.stop)
        password = unittest.mock.patch.object(pixer_upload.getpass, "getpass", return_value="example-password")
        password.start()
        self.addCleanup(password.stop)

    def test_setup_accepts_the_stock_empty_scan_and_25_byte_device_id(self):
        self.socket.makefile.return_value = io.BytesIO(
            self.EMPTY_SCAN + b"#SMAC#" + self.DEVICE_ID
        )

        self.assertEqual(pixer_upload.setup_wifi("Home-WiFi"), self.DEVICE_ID)
        scan, settings, acknowledgement = [call.args[0] for call in self.socket.sendall.call_args_list]
        self.assertEqual(scan, b"#SSID#")
        self.assertEqual(len(settings), 134)
        self.assertEqual(settings[:6], b"#UPDT#")
        self.assertEqual(settings[6:38], b"Home-WiFi".ljust(32, b"\x00"))
        self.assertEqual(settings[38:70], b"example-password".ljust(32, b"\x00"))
        self.assertEqual(settings[70:120], b"pixer@pixer.invalid".ljust(50, b"\x00"))
        self.assertRegex(settings[120:], rb"^2[0-9]{4}[0-9a-f]{8}1$")
        self.assertEqual(acknowledgement, b"#MACS#")

    def test_setup_withholds_settings_on_unexpected_scan(self):
        self.socket.makefile.return_value = io.BytesIO(b"00")

        with self.assertRaisesRegex(pixer_upload.PixerError, "expected Wi-Fi scan response"):
            pixer_upload.setup_wifi("Home-WiFi")

        self.socket.sendall.assert_called_once_with(b"#SSID#")

    def test_setup_reports_uncertainty_when_configuration_is_not_acknowledged(self):
        self.socket.makefile.return_value = io.BytesIO(self.EMPTY_SCAN + b"#SMAC#short")

        with self.assertRaisesRegex(pixer_upload.PixerError, "Wi-Fi setup could not be confirmed"):
            pixer_upload.setup_wifi("Home-WiFi")

        self.assertEqual(self.socket.sendall.call_count, 2)


class ScriptedSocket:
    """Answer each command with scripted chunks, then fall silent."""

    def __init__(self, replies):
        self.replies = replies
        self.sent = []
        self.pending = []
        self.timeout = None
        self.send_timeouts = []

    def sendall(self, data):
        self.sent.append(data)
        self.send_timeouts.append(self.timeout)
        self.pending = list(self.replies.get(data, []))

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, size):
        if not self.pending:
            raise socket.timeout("timed out")
        chunk = self.pending.pop(0)
        if len(chunk) > size:
            self.pending.insert(0, chunk[size:])
        return chunk[:size]


class StatusTests(unittest.TestCase):
    DIRECT = {
        b"#TEST#": [b"Hello PC!"],
        b"bleVersion": [b"1.0.7"],
        b"iteVersion": [b"2.3"],
        b"mcuVersion": [b"mcu_test_version"],
        b"batteryLevel": [b"85"],
    }

    def frame(self, replies):
        connection = unittest.mock.patch.object(pixer_upload, "connect_to_pixer")
        self.connect = connection.start()
        self.addCleanup(connection.stop)
        sock = ScriptedSocket(replies)
        self.connect.return_value.__enter__.return_value = sock
        return sock

    def test_direct_status_follows_the_app_query_order(self):
        sock = self.frame(self.DIRECT)

        status = pixer_upload.read_status("192.168.1.1", 6000)

        self.assertEqual(
            sock.sent,
            [b"#TEST#", b"bleVersion", b"iteVersion", b"mcuVersion", b"batteryLevel"],
        )
        self.assertEqual(
            status,
            {
                "Battery": "85%",
                "BLE firmware": "1.0.7",
                "Display firmware": "2.3",
                "MCU firmware": "mcu_test_version",
            },
        )

    def test_direct_status_joins_a_reply_split_across_segments(self):
        self.frame({**self.DIRECT, b"#TEST#": [b"Hello ", b"PC!"], b"batteryLevel": [b"8", b"5"]})

        status = pixer_upload.read_status("192.168.1.1", 6000)

        self.assertEqual(status["Battery"], "85%")
        self.assertEqual(status["BLE firmware"], "1.0.7")

    def test_long_greeting_supplies_the_ble_version(self):
        sock = self.frame({**self.DIRECT, b"#TEST#": [b"Hello PC!1.0.8"], b"bleVersion": []})

        status = pixer_upload.read_status("192.168.1.1", 6000)

        self.assertEqual(status["BLE firmware"], "1.0.8")
        self.assertEqual(status["Display firmware"], "2.3")
        self.assertIn(b"bleVersion", sock.sent)

    def test_long_greeting_discards_a_late_ble_reply(self):
        self.frame({**self.DIRECT, b"#TEST#": [b"Hello PC!1.0.8"]})

        status = pixer_upload.read_status("192.168.1.1", 6000)

        self.assertEqual(status["BLE firmware"], "1.0.8")
        self.assertEqual(status["Display firmware"], "2.3")

    def test_every_command_is_sent_with_the_full_timeout(self):
        sock = self.frame({**self.DIRECT, b"#TEST#": [b"Hello PC!1.0.8"]})
        sock.timeout = 5.0

        pixer_upload.read_status("192.168.1.1", 6000, timeout=5.0)

        self.assertEqual(sock.send_timeouts, [5.0] * 5)

    def test_device_text_cannot_inject_terminal_control_codes(self):
        self.frame({**self.DIRECT, b"iteVersion": [b"2.3\x1b[2K\rBattery: 100%"]})

        status = pixer_upload.read_status("192.168.1.1", 6000)

        self.assertEqual(status["Display firmware"], "2.3\\x1b[2K\\rBattery: 100%")

    def test_direct_status_stops_on_an_unexpected_greeting(self):
        sock = self.frame({b"#TEST#": [b"#end1#"]})

        with self.assertRaisesRegex(pixer_upload.PixerError, "unexpected reply to #TEST#"):
            pixer_upload.read_status("192.168.1.1", 6000)

        self.assertEqual(sock.sent, [b"#TEST#"])

    def test_direct_status_rejects_impossible_battery_levels(self):
        for level in (b"full", b"101"):
            with self.subTest(level=level):
                self.frame({**self.DIRECT, b"batteryLevel": [level]})
                with self.assertRaisesRegex(pixer_upload.PixerError, "battery level"):
                    pixer_upload.read_status("192.168.1.1", 6000)

    def test_direct_status_names_the_command_that_went_unanswered(self):
        self.frame({**self.DIRECT, b"mcuVersion": []})

        with self.assertRaisesRegex(pixer_upload.PixerError, "did not answer mcuVersion"):
            pixer_upload.read_status("192.168.1.1", 6000)

    def test_cli_prints_the_status_for_any_host(self):
        self.frame(self.DIRECT)
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(pixer_upload.main(["--status", "--host", "10.0.0.5"]), 0)

        self.connect.assert_called_once_with("10.0.0.5", 6000, pixer_upload.PROBE_TIMEOUT)
        self.assertIn("Pixer at 10.0.0.5:6000", out.getvalue())
        self.assertIn("  Battery: 85%", out.getvalue())
        self.assertIn("  MCU firmware: mcu_test_version", out.getvalue())

    def test_cli_status_refuses_dry_run_without_connecting(self):
        self.frame({})
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(pixer_upload.main(["--status", "--dry-run"]), 1)

        self.assertIn("cannot be combined", err.getvalue())
        self.connect.assert_not_called()


class DiscoveryTests(unittest.TestCase):
    def answer(self, *greeting):
        sock = ScriptedSocket({b"#TEST#": list(greeting)})
        connection = unittest.mock.patch.object(pixer_upload.socket, "create_connection")
        connection.start().return_value.__enter__.return_value = sock
        self.addCleanup(connection.stop)
        return sock

    def test_a_frame_is_recognized_by_its_greeting(self):
        sock = self.answer(b"Hello PC!")

        self.assertTrue(pixer_upload.is_pixer("10.0.0.5", 6000))
        self.assertEqual(sock.sent, [b"#TEST#"])

    def test_another_service_on_the_port_is_not_a_frame(self):
        self.answer(b"X11 rejected")

        self.assertFalse(pixer_upload.is_pixer("10.0.0.5", 6000))

    def test_silent_or_closed_hosts_are_not_frames(self):
        self.answer()
        self.assertFalse(pixer_upload.is_pixer("10.0.0.5", 6000))

        with unittest.mock.patch.object(
            pixer_upload.socket, "create_connection", side_effect=ConnectionRefusedError()
        ):
            self.assertFalse(pixer_upload.is_pixer("10.0.0.6", 6000))

    def test_discover_returns_frames_in_address_order(self):
        frames = {"10.0.0.3", "10.0.0.6"}
        with unittest.mock.patch.object(
            pixer_upload, "is_pixer", side_effect=lambda host, port, timeout: host in frames
        ) as check:
            found = pixer_upload.discover(pixer_upload.ipaddress.ip_network("10.0.0.0/29"), 6000)

        self.assertEqual(found, ["10.0.0.3", "10.0.0.6"])
        self.assertEqual(check.call_count, 6)

    def test_a_long_greeting_still_identifies_a_frame(self):
        self.answer(b"Hello PC!1.0.8")

        self.assertTrue(pixer_upload.is_pixer("10.0.0.5", 6000))

    def test_a_trickling_reply_cannot_hold_a_probe_past_its_deadline(self):
        self.answer(*[bytes([c]) for c in b"Hello PC!"])
        clock = iter(range(0, 100))
        with unittest.mock.patch.object(pixer_upload.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertFalse(pixer_upload.is_pixer("10.0.0.5", 6000, timeout=3))

    def test_discover_refuses_a_huge_network_without_listing_it(self):
        with unittest.mock.patch.object(pixer_upload.ipaddress.IPv4Network, "hosts") as hosts:
            with self.assertRaisesRegex(pixer_upload.PixerError, "scan at most"):
                pixer_upload.discover(pixer_upload.ipaddress.ip_network("0.0.0.0/0"), 6000)

        hosts.assert_not_called()

    def test_default_network_is_the_local_slash_24(self):
        with unittest.mock.patch.object(pixer_upload, "local_source_address", return_value="10.1.7.49"):
            self.assertEqual(str(pixer_upload.local_network()), "10.1.7.0/24")

    def test_default_network_falls_back_to_the_hotspot_route(self):
        routes = {"192.0.2.1": None, "192.168.1.1": "192.168.1.20"}
        with unittest.mock.patch.object(pixer_upload, "local_source_address", side_effect=routes.get):
            self.assertEqual(str(pixer_upload.local_network()), "192.168.1.0/24")

    def test_cli_without_any_route_fails_before_scanning(self):
        with unittest.mock.patch.object(pixer_upload, "local_source_address", return_value=None), \
                unittest.mock.patch.object(pixer_upload, "discover") as scan, \
                unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(pixer_upload.main(["--discover"]), 1)

        scan.assert_not_called()
        self.assertIn("no network route", err.getvalue())

    def test_cli_named_network_skips_the_route_lookup(self):
        with unittest.mock.patch.object(pixer_upload, "local_network") as default, \
                unittest.mock.patch.object(pixer_upload, "discover", return_value=["10.2.0.9"]), \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(pixer_upload.main(["--discover", "10.2.0.0/24"]), 0)

        default.assert_not_called()

    def test_cli_rejects_host_with_discover(self):
        with unittest.mock.patch.object(pixer_upload, "discover") as scan, \
                unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(pixer_upload.main(["--discover", "--host", "10.1.7.102"]), 1)
            self.assertEqual(pixer_upload.main(["--discover", "--host", "192.168.1.1"]), 1)

        scan.assert_not_called()
        self.assertIn("instead of --host", err.getvalue())

    def test_cli_rejects_an_impossible_port(self):
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit):
                pixer_upload.main(["--probe", "--port", "70000"])

        self.assertIn("between 1 and 65535", err.getvalue())

    def test_cli_reports_found_frames(self):
        with unittest.mock.patch.object(pixer_upload, "discover", return_value=["10.1.7.102"]) as scan, \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(pixer_upload.main(["--discover", "10.1.7.0/24"]), 0)

        scan.assert_called_once_with(pixer_upload.ipaddress.ip_network("10.1.7.0/24"), 6000)
        self.assertIn("Found Pixer at 10.1.7.102", out.getvalue())

    def test_cli_explains_an_empty_scan(self):
        with unittest.mock.patch.object(pixer_upload, "discover", return_value=[]), \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO), \
                unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(pixer_upload.main(["--discover", "10.1.7.0/24"]), 1)

        self.assertIn("short press", err.getvalue())

    def test_cli_rejects_a_malformed_network(self):
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(pixer_upload.main(["--discover", "10.1.7"]), 1)

        self.assertIn("not an IPv4 network", err.getvalue())


class MinimumBatteryTests(unittest.TestCase):
    def setUp(self):
        self.image = Path(__file__)
        patches = {
            "read_status": unittest.mock.DEFAULT,
            "probe_device": unittest.mock.DEFAULT,
            "build_payload": unittest.mock.DEFAULT,
            "upload_payload": unittest.mock.DEFAULT,
        }
        patcher = unittest.mock.patch.multiple(pixer_upload, **patches)
        self.mocks = patcher.start()
        self.addCleanup(patcher.stop)
        self.mocks["build_payload"].return_value = (b"\x00" * 4, 4, 2)
        self.mocks["upload_payload"].return_value = b"#end1#"
        self.out = unittest.mock.patch("sys.stdout", new_callable=io.StringIO).start()
        self.err = unittest.mock.patch("sys.stderr", new_callable=io.StringIO).start()
        self.addCleanup(unittest.mock.patch.stopall)
        pixer_upload.SHOWN_PATH.unlink(missing_ok=True)

    def battery(self, level):
        self.mocks["read_status"].return_value = {"Battery": f"{level}%", "MCU firmware": "x"}

    def test_low_battery_skips_conversion_and_upload(self):
        self.battery(12)

        self.assertEqual(pixer_upload.main([str(self.image), "--min-battery", "20"]), 1)

        self.assertIn("12%", self.err.getvalue())
        self.assertIn("below --min-battery 20%", self.err.getvalue())
        self.mocks["build_payload"].assert_not_called()
        self.mocks["upload_payload"].assert_not_called()
        self.assertFalse(pixer_upload.SHOWN_PATH.exists())

    def test_a_successful_upload_is_kept_as_the_shown_image(self):
        self.battery(80)
        self.assertEqual(pixer_upload.main([str(self.image), "--min-battery", "20"]), 0)
        self.assertEqual(read_png(pixer_upload.SHOWN_PATH.read_bytes()), (4, 2, bytes(8)))

    def test_battery_at_the_threshold_uploads(self):
        self.battery(20)

        self.assertEqual(pixer_upload.main([str(self.image), "--min-battery", "20"]), 0)

        self.mocks["read_status"].assert_called_once_with("192.168.1.1", 6000)
        self.mocks["probe_device"].assert_not_called()
        self.mocks["upload_payload"].assert_called_once()
        self.assertIn("Battery 20%", self.out.getvalue())

    def test_without_the_option_uploads_as_before(self):
        self.assertEqual(pixer_upload.main([str(self.image)]), 0)

        self.mocks["read_status"].assert_not_called()
        self.mocks["probe_device"].assert_called_once_with("192.168.1.1", 6000)
        self.mocks["upload_payload"].assert_called_once()

    def test_the_option_is_refused_outside_uploads(self):
        for argv in (
            [str(self.image), "--dry-run"],
            ["--status"],
            ["--probe"],
            ["--discover", "10.0.0.0/30"],
            ["--discover"],
            ["--setup-wifi", "Home-WiFi"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(pixer_upload.main(argv + ["--min-battery", "20"]), 1)
        self.assertIn("--min-battery only applies to uploads", self.err.getvalue())
        self.mocks["read_status"].assert_not_called()
        self.mocks["upload_payload"].assert_not_called()

    def test_an_unreadable_battery_skips_the_upload(self):
        self.mocks["read_status"].side_effect = pixer_upload.PixerError("Pixer did not answer batteryLevel")

        self.assertEqual(pixer_upload.main([str(self.image), "--min-battery", "20"]), 1)

        self.assertIn("did not answer batteryLevel", self.err.getvalue())
        self.mocks["build_payload"].assert_not_called()
        self.mocks["upload_payload"].assert_not_called()

    def test_an_upload_gives_up_while_another_holds_the_frame(self):
        self.battery(80)
        with unittest.mock.patch.object(pixer_upload, "LOCK_WAIT", 0.05), \
                pixer_upload.frame_lock("192.168.1.1", 6000, wait=0):
            self.assertEqual(pixer_upload.main([str(self.image), "--min-battery", "20"]), 1)

        self.assertIn("still running", self.err.getvalue())
        self.mocks["read_status"].assert_not_called()
        self.mocks["upload_payload"].assert_not_called()

    def test_a_dry_run_does_not_wait_for_the_frame(self):
        with pixer_upload.frame_lock("192.168.1.1", 6000, wait=0):
            self.assertEqual(pixer_upload.main([str(self.image), "--dry-run"]), 0)

    def test_the_threshold_must_be_a_percentage(self):
        for value in ("0", "101", "half"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                pixer_upload.main([str(self.image), "--min-battery", value])
        self.assertIn("between 1 and 100", self.err.getvalue())


class FrameLockTests(unittest.TestCase):
    def test_a_second_holder_is_refused_until_the_first_lets_go(self):
        with pixer_upload.frame_lock("10.0.0.5", 6000, wait=0):
            with self.assertRaisesRegex(pixer_upload.FrameBusy, "10.0.0.5:6000 is still running"):
                with pixer_upload.frame_lock("10.0.0.5", 6000, wait=0):
                    pass
        with pixer_upload.frame_lock("10.0.0.5", 6000, wait=0):
            pass

    def test_other_frames_are_not_blocked(self):
        with pixer_upload.frame_lock("10.0.0.5", 6000, wait=0):
            with pixer_upload.frame_lock("10.0.0.6", 6000, wait=0):
                pass

    def test_a_waiter_gets_the_frame_when_the_holder_finishes(self):
        holding = pixer_upload.frame_lock("10.0.0.5", 6000, wait=0)
        holding.__enter__()
        timer = threading.Timer(0.2, holding.__exit__, (None, None, None))
        timer.start()
        self.addCleanup(timer.join)

        with unittest.mock.patch.object(pixer_upload, "LOCK_POLL", 0.01):
            with pixer_upload.frame_lock("10.0.0.5", 6000, wait=5):
                pass

    def test_waiting_never_sleeps_past_the_deadline(self):
        with pixer_upload.frame_lock("10.0.0.5", 6000, wait=0), \
                unittest.mock.patch.object(pixer_upload.time, "sleep") as sleep:
            with self.assertRaises(pixer_upload.FrameBusy):
                with pixer_upload.frame_lock("10.0.0.5", 6000, wait=0.05):
                    pass
        self.assertTrue(sleep.call_args_list)
        self.assertTrue(all(call.args[0] <= 0.05 for call in sleep.call_args_list))

    def test_another_process_holding_the_frame_is_seen(self):
        script = (
            "import importlib.util, sys\n"
            "from pathlib import Path\n"
            "spec = importlib.util.spec_from_file_location('pixer_upload', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "module.LOCK_DIR = Path(sys.argv[2])\n"
            "with module.frame_lock('10.0.0.5', 6000, wait=0):\n"
            "    print('held', flush=True)\n"
            "    sys.stdin.read()\n"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", script, str(MODULE_PATH), str(pixer_upload.LOCK_DIR)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(holder.wait, 5)
        self.assertEqual(holder.stdout.readline(), "held\n")

        with self.assertRaises(pixer_upload.FrameBusy):
            with pixer_upload.frame_lock("10.0.0.5", 6000, wait=0):
                pass
        holder.stdin.close()
        holder.wait(5)
        holder.stdout.close()
        with pixer_upload.frame_lock("10.0.0.5", 6000, wait=0):
            pass


class TransferFailureTests(unittest.TestCase):
    def test_dropped_connection_reports_progress(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def hang_up():
            connection, _ = listener.accept()
            connection.close()

        worker = threading.Thread(target=hang_up)
        worker.start()
        self.addCleanup(listener.close)
        self.addCleanup(worker.join)

        with self.assertRaises(pixer_upload.PixerError) as caught:
            pixer_upload.upload_payload(
                b"\x11" * (2 * 1024 * 1024),
                "127.0.0.1",
                port,
                progress_stream=io.StringIO(),
            )
        message = str(caught.exception)
        self.assertIn("connection to Pixer was lost", message)
        self.assertIn("2097152 bytes", message)


if __name__ == "__main__":
    unittest.main()
