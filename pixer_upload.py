#!/usr/bin/env python3
"""Upload an image directly to a G+ Pixer frame over its local Wi-Fi."""

from __future__ import annotations

import argparse
import ipaddress
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_HOST = "192.168.1.1"
DEFAULT_PORT = 6000
PROBE_TIMEOUT = 5.0
WIFI_HINT = 'Join the frame\'s own Wi-Fi network (for example "Pixer 0939"), then retry.'
LANDSCAPE_SIZE = (1872, 1404)
PORTRAIT_SIZE = (1404, 1872)
ACK_SIZE = 6


class PixerError(RuntimeError):
    """Raised for image conversion or device communication failures."""


def java_div(value: int, divisor: int) -> int:
    """Integer division truncated toward zero, matching Java."""
    quotient = abs(value) // abs(divisor)
    return -quotient if (value < 0) != (divisor < 0) else quotient


def probe_image_size(image_path: Path) -> tuple[int, int]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise PixerError("ffprobe was not found; install ffmpeg first")
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=s=x:p=0",
        str(image_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    try:
        width_text, height_text = result.stdout.strip().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError) as exc:
        detail = result.stderr.strip() or "unsupported or unreadable image"
        raise PixerError(f"could not read image dimensions: {detail}") from exc
    if width <= 0 or height <= 0:
        raise PixerError("image dimensions must be positive")
    return width, height


def target_size(image_path: Path, orientation: str) -> tuple[int, int]:
    if orientation == "landscape":
        return LANDSCAPE_SIZE
    if orientation == "portrait":
        return PORTRAIT_SIZE
    width, height = probe_image_size(image_path)
    return LANDSCAPE_SIZE if width >= height else PORTRAIT_SIZE


def convert_to_rgb(
    image_path: Path, width: int, height: int, fit: str = "cover"
) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PixerError("ffmpeg was not found")
    if fit == "cover":
        transform = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},format=rgb24"
        )
    else:
        transform = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white,format=rgb24"
        )
    command = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(image_path),
        "-vf",
        transform,
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PixerError(f"ffmpeg conversion failed: {detail}")
    expected = width * height * 3
    if len(result.stdout) != expected:
        raise PixerError(
            f"ffmpeg returned {len(result.stdout)} bytes; expected {expected}"
        )
    return result.stdout


def rgb_to_gray(rgb: bytes, width: int, height: int) -> bytes:
    """Match the app's default integer Rec. 601 grayscale conversion."""
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError(f"RGB buffer is {len(rgb)} bytes; expected {expected}")
    gray = bytearray(width * height)
    for pixel in range(width * height):
        source = pixel * 3
        gray[pixel] = (
            (rgb[source] * 19595)
            + (rgb[source + 1] * 38469)
            + (rgb[source + 2] * 7471)
        ) >> 16
    return bytes(gray)


def dither_16_levels(gray: bytes, width: int, height: int) -> bytearray:
    """Replicate EditActivity.eternityImage's 16-level error diffusion."""
    expected = width * height
    if len(gray) != expected:
        raise ValueError(f"gray buffer is {len(gray)} bytes; expected {expected}")
    levels = bytearray(expected)
    errors = [0] * width
    for row in range(height):
        row_offset = row * width
        for column in range(width):
            index = row_offset + column
            component = gray[index] + java_div(errors[column] + 2, 4)
            level = java_div(component, 17)
            remainder = component - (level * 17)
            if remainder > 8:
                level += 1
                remainder -= 17
            level = min(15, max(0, level))
            levels[index] = level
            errors[column] = remainder
            if column > 0:
                errors[column - 1] += remainder
            if column + 1 < width:
                errors[column + 1] += remainder * 2
    return levels


def pack_4bit(levels: bytes, width: int, height: int) -> bytes:
    """Pack pixels exactly as the Android app, rotating portrait data."""
    if len(levels) != width * height:
        raise ValueError("level buffer does not match dimensions")
    if width % 2 != 0 or height % 2 != 0:
        raise ValueError("Pixer image dimensions must be even")
    packed = bytearray((width * height) // 2)
    if width > height:
        half_width = width // 2
        for row in range(height):
            source_row = row * width
            output_row = row * half_width
            for pair in range(half_width):
                low_index = source_row + (pair * 2)
                packed[output_row + pair] = (
                    levels[low_index] | (levels[low_index + 1] << 4)
                )
    else:
        half_height = height // 2
        for column in range(width - 1, -1, -1):
            output_row = (width - 1 - column) * half_height
            for pair in range(half_height):
                low_index = column + (width * pair * 2)
                high_index = low_index + width
                packed[output_row + pair] = (
                    levels[low_index] | (levels[high_index] << 4)
                )
    return bytes(packed)


def build_payload(image_path: Path, orientation: str, fit: str) -> tuple[bytes, int, int]:
    width, height = target_size(image_path, orientation)
    rgb = convert_to_rgb(image_path, width, height, fit)
    gray = rgb_to_gray(rgb, width, height)
    levels = dither_16_levels(gray, width, height)
    return pack_4bit(levels, width, height), width, height


def file_header(payload_length: int) -> bytes:
    if payload_length < 0 or payload_length > 99_999_999:
        raise ValueError("payload length does not fit the Pixer header")
    return f"#file#0008{payload_length:08d}imagebin".encode("ascii")


def local_source_address(host: str) -> str | None:
    """Address this computer would use to reach host; no packets are sent."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def network_advice(host: str) -> str:
    """Explain, from the local routing table, why the frame is out of reach."""
    try:
        target = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        return WIFI_HINT
    source = local_source_address(host)
    if source is None:
        return f"This computer has no route to {host}. {WIFI_HINT}"
    if ipaddress.IPv4Address(source) not in ipaddress.ip_network(
        f"{target}/24", strict=False
    ):
        return (
            f"This computer is at {source}, a different network from {host}. "
            f"{WIFI_HINT}"
        )
    return (
        f"This computer is at {source}, on the same network as {host}, so the "
        "frame is probably asleep. Press its button once and try again."
    )


def unreachable_message(host: str, port: int, timeout: float, exc: OSError) -> str:
    if isinstance(exc, ConnectionRefusedError):
        reason = f"something answered at {host}, but nothing is listening on port {port}"
    elif isinstance(exc, (socket.timeout, TimeoutError)):
        reason = f"no response within {timeout:.0f} seconds"
    else:
        reason = str(exc)
    return (
        f"cannot reach Pixer at {host}:{port}: {reason}.\n"
        f"  {network_advice(host)}"
    )


def connect_to_pixer(host: str, port: int, timeout: float) -> socket.socket:
    try:
        return socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise PixerError(unreachable_message(host, port, timeout, exc)) from exc


def probe_device(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> None:
    connect_to_pixer(host, port, timeout).close()


def upload_payload(
    payload: bytes,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    connect_timeout: float = 10.0,
    ack_timeout: float = 180.0,
) -> bytes:
    header = file_header(len(payload))
    sent = 0
    try:
        with connect_to_pixer(host, port, connect_timeout) as sock:
            sock.sendall(header)
            view = memoryview(payload)
            chunk_size = 64 * 1024
            next_report = 10
            while sent < len(payload):
                end = min(sent + chunk_size, len(payload))
                sock.sendall(view[sent:end])
                sent = end
                percent = (sent * 100) // len(payload)
                if percent >= next_report:
                    print(f"Sent {percent}%", file=sys.stderr)
                    next_report += 10
            sock.sendall(b"#MOVE#d")

            deadline = time.monotonic() + ack_timeout
            acknowledgement = bytearray()
            while len(acknowledgement) < ACK_SIZE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PixerError(
                        "image data was sent, but Pixer did not acknowledge "
                        f"completion within {ack_timeout:.0f} seconds.\n"
                        "  The frame may still be refreshing; look at it before "
                        "uploading again."
                    )
                sock.settimeout(min(1.0, remaining))
                try:
                    chunk = sock.recv(ACK_SIZE - len(acknowledgement))
                except socket.timeout:
                    continue
                if not chunk:
                    raise PixerError("Pixer closed the connection before acknowledging")
                acknowledgement.extend(chunk)
            return bytes(acknowledgement)
    except PixerError:
        raise
    except OSError as exc:
        percent = (sent * 100) // len(payload) if payload else 0
        raise PixerError(
            f"the connection to Pixer was lost after sending {sent} of "
            f"{len(payload)} bytes ({percent}%): {exc}\n"
            "  Keep the frame powered and stay on its Wi-Fi, then upload again."
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload an image directly to a G+ Pixer frame"
    )
    parser.add_argument("image", nargs="?", type=Path, help="JPEG, PNG, HEIC, or other ffmpeg-readable image")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--orientation",
        choices=("auto", "landscape", "portrait"),
        default="auto",
    )
    parser.add_argument("--fit", choices=("cover", "contain"), default="cover")
    parser.add_argument("--output-bin", type=Path, help="save converted imagebin data")
    parser.add_argument("--dry-run", action="store_true", help="convert only; do not contact Pixer")
    parser.add_argument("--probe", action="store_true", help="only test TCP port 6000")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.probe:
            probe_device(args.host, args.port)
            print(f"Pixer upload service is reachable at {args.host}:{args.port}")
            return 0
        if args.image is None:
            raise PixerError("an image path is required unless --probe is used")
        if not args.image.is_file():
            raise PixerError(f"image does not exist: {args.image}")
        if not args.dry_run:
            probe_device(args.host, args.port)

        payload, width, height = build_payload(args.image, args.orientation, args.fit)
        print(f"Prepared {width}x{height}, {len(payload)} bytes")
        if args.output_bin:
            args.output_bin.write_bytes(payload)
            print(f"Saved imagebin: {args.output_bin}")
        if args.dry_run:
            return 0

        acknowledgement = upload_payload(payload, args.host, args.port)
        print(
            "Pixer acknowledged the upload: "
            + acknowledgement.decode("ascii", errors="backslashreplace")
        )
        return 0
    except (PixerError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted before the upload finished", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
