import importlib.util
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
            pixer_upload.upload_payload(b"\x11" * (2 * 1024 * 1024), "127.0.0.1", port)
        message = str(caught.exception)
        self.assertIn("connection to Pixer was lost", message)
        self.assertIn("2097152 bytes", message)


if __name__ == "__main__":
    unittest.main()
