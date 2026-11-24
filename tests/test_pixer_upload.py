import importlib.util
import io
import socket
import threading
import unittest
import unittest.mock
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "pixer_upload.py"
SPEC = importlib.util.spec_from_file_location("pixer_upload", MODULE_PATH)
pixer_upload = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pixer_upload)


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
