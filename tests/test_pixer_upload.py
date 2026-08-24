import importlib.util
import unittest
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


if __name__ == "__main__":
    unittest.main()
