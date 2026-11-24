#!/usr/bin/env python3
"""Configure Wi-Fi or upload an image to a G+ Pixer frame."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import secrets
import shutil
import socket
import subprocess
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


DEFAULT_HOST = "192.168.1.1"
DEFAULT_PORT = 6000
PROBE_TIMEOUT = 5.0
PAIRING_TIMEOUT = 30.0
CHUNK_SIZE = 32 * 1024
BAR_WIDTH = 28
REDRAW_INTERVAL = 0.1
MEGABYTE = 1024 * 1024
WIFI_HINT = 'Join the frame\'s own Wi-Fi network (for example "Pixer 0939"), then retry.'
LANDSCAPE_SIZE = (1872, 1404)
PORTRAIT_SIZE = (1404, 1872)
ACK_SIZE = 6
STATUS_REPLY_SIZE = 64
STATUS_GREETING = "Hello PC!"
STATUS_QUIET_GAP = 0.3
DISCOVER_TIMEOUT = 1.0
DISCOVER_WORKERS = 64
DISCOVER_MAX_HOSTS = 1024
# Error-diffusion kernels as (dx, dy, weight); Atkinson drops 2/8 of the error.
DIFFUSION_KERNELS = {
    "floyd-steinberg": ((1, 0, 7 / 16), (-1, 1, 3 / 16), (0, 1, 5 / 16), (1, 1, 1 / 16)),
    "atkinson": tuple((dx, dy, 1 / 8) for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2))),
    "none": (),
}
DITHER_METHODS = ("app", *DIFFUSION_KERNELS)


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


def rgb_to_gray(rgb: bytes, width: int, height: int, rounded: bool = False) -> bytes:
    """Match the app's integer Rec. 601 grayscale conversion.

    The app's weights sum to 65535 and truncate, so white becomes 254. The
    rounded variant keeps neutral grays exact.
    """
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError(f"RGB buffer is {len(rgb)} bytes; expected {expected}")
    bias = 32768 if rounded else 0
    gray = bytearray(width * height)
    for pixel in range(width * height):
        source = pixel * 3
        gray[pixel] = (
            (rgb[source] * 19595)
            + (rgb[source + 1] * 38469)
            + (rgb[source + 2] * 7471)
            + bias
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


def diffuse_16_levels(gray: bytes, width: int, height: int, method: str) -> bytearray:
    """Quantize to 16 levels, spreading each pixel's error over a 2-D kernel."""
    expected = width * height
    if len(gray) != expected:
        raise ValueError(f"gray buffer is {len(gray)} bytes; expected {expected}")
    kernel = DIFFUSION_KERNELS[method]
    pad = 2
    rows = [[0.0] * (width + 2 * pad) for _ in range(3)]
    levels = bytearray(expected)
    for row in range(height):
        errors = rows[0]
        row_offset = row * width
        for column in range(width):
            value = gray[row_offset + column] + errors[column + pad]
            level = min(15, max(0, int(value / 17 + 0.5)))
            levels[row_offset + column] = level
            error = value - level * 17
            for dx, dy, weight in kernel:
                rows[dy][column + pad + dx] += error * weight
        rows = rows[1:] + [[0.0] * (width + 2 * pad)]
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


def build_payload(
    image_path: Path, orientation: str, fit: str, dither: str = "app"
) -> tuple[bytes, int, int]:
    width, height = target_size(image_path, orientation)
    rgb = convert_to_rgb(image_path, width, height, fit)
    if dither == "app":
        levels = dither_16_levels(rgb_to_gray(rgb, width, height), width, height)
    else:
        gray = rgb_to_gray(rgb, width, height, rounded=True)
        levels = diffuse_16_levels(gray, width, height, dither)
    return pack_4bit(levels, width, height), width, height


def file_header(payload_length: int) -> bytes:
    if payload_length < 0 or payload_length > 99_999_999:
        raise ValueError("payload length does not fit the Pixer header")
    return f"#file#0008{payload_length:08d}imagebin".encode("ascii")


class Progress:
    """Draw upload progress on one line, or as plain lines when redirected."""

    def __init__(self, total: int, stream=None) -> None:
        self.total = max(total, 1)
        self.stream = sys.stderr if stream is None else stream
        self.interactive = self.stream.isatty()
        self.started = time.monotonic()
        self.last_draw = 0.0
        self.next_percent = 10
        self.announced_wait = False
        self.dirty = False

    def _redraw(self, text: str) -> None:
        self.stream.write(f"\r{text}\x1b[K")
        self.stream.flush()
        self.dirty = True

    def sent(self, count: int) -> None:
        now = time.monotonic()
        percent = (count * 100) // self.total
        if not self.interactive:
            if percent >= self.next_percent:
                self.next_percent = percent - (percent % 10) + 10
                print(
                    f"Sent {percent}% "
                    f"({count / MEGABYTE:.2f}/{self.total / MEGABYTE:.2f} MB)",
                    file=self.stream,
                    flush=True,
                )
            return
        if count < self.total and now - self.last_draw < REDRAW_INTERVAL:
            return
        self.last_draw = now
        filled = (BAR_WIDTH * count) // self.total
        bar = ("\u2588" * filled) + ("\u2591" * (BAR_WIDTH - filled))
        rate = count / max(now - self.started, 0.001) / MEGABYTE
        self._redraw(
            f"  [{bar}] {percent:3d}%  "
            f"{count / MEGABYTE:.2f}/{self.total / MEGABYTE:.2f} MB  {rate:.1f} MB/s"
        )

    def waiting(self) -> None:
        """Show that the frame is refreshing, which can take tens of seconds."""
        elapsed = time.monotonic() - self.started
        if not self.interactive:
            if not self.announced_wait:
                self.announced_wait = True
                print("Waiting for the frame to refresh", file=self.stream, flush=True)
            return
        if time.monotonic() - self.last_draw < REDRAW_INTERVAL:
            return
        self.last_draw = time.monotonic()
        self._redraw(f"  Waiting for the frame to refresh... {elapsed:.0f}s")

    def close(self) -> None:
        """End the current line so the next stage starts a fresh one."""
        self.last_draw = 0.0
        if self.dirty:
            self.stream.write("\n")
            self.stream.flush()
            self.dirty = False


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
    if host != DEFAULT_HOST:
        return (
            "Check the frame's current IP address and whether the network allows "
            "this computer to reach it. The frame may also be asleep."
        )
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


def printable(reply: bytes) -> str:
    """Decode a device reply with control and non-ASCII bytes escaped."""
    text = reply.strip(b"\x00\r\n ").decode("latin-1")
    return text.encode("unicode_escape").decode("ascii")


def read_reply(sock: socket.socket, wait: float) -> bytes:
    """Read one status reply. TCP has no message boundaries, so after the first
    bytes arrive, keep collecting until the frame goes quiet."""
    reply = bytearray()
    sock.settimeout(wait)
    try:
        while len(reply) < STATUS_REPLY_SIZE:
            chunk = sock.recv(STATUS_REPLY_SIZE - len(reply))
            if not chunk:
                break
            reply.extend(chunk)
            sock.settimeout(STATUS_QUIET_GAP)
    except socket.timeout:
        if not reply:
            raise
    finally:
        sock.settimeout(wait)
    return bytes(reply)


def discard_reply(sock: socket.socket, window: float) -> None:
    """Read and drop whatever arrives within window seconds."""
    deadline = time.monotonic() + window
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            if not sock.recv(STATUS_REPLY_SIZE):
                return
    except socket.timeout:
        pass


def query(sock: socket.socket, command: bytes, timeout: float) -> str:
    name = command.decode("ascii")
    try:
        sock.sendall(command)
        reply = read_reply(sock, timeout)
    except socket.timeout as exc:
        raise PixerError(f"Pixer did not answer {name} within {timeout:.0f} seconds") from exc
    except OSError as exc:
        raise PixerError(f"the connection to Pixer failed during {name}: {exc}") from exc
    if not reply:
        raise PixerError(f"Pixer closed the connection after {name}")
    return printable(reply)


def battery_percent(text: str) -> str:
    if not text.isdigit() or int(text) > 100:
        raise PixerError(f"Pixer returned an unreadable battery level: {text!r}")
    return f"{int(text)}%"


def read_status(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> dict[str, str]:
    """Query firmware versions and battery, as MainActivityV2.checkPixer does."""
    with connect_to_pixer(host, port, timeout) as sock:
        greeting = query(sock, b"#TEST#", timeout)
        if greeting == STATUS_GREETING:
            ble = query(sock, b"bleVersion", timeout)
        elif greeting.startswith(STATUS_GREETING):
            # The app reads the BLE version from a longer greeting, then still
            # sends bleVersion without reading. Discard any reply so it cannot
            # be mistaken for the next answer.
            ble = greeting[len(STATUS_GREETING):]
            try:
                sock.sendall(b"bleVersion")
                discard_reply(sock, STATUS_QUIET_GAP)
                sock.settimeout(timeout)
            except OSError as exc:
                raise PixerError(f"the connection to Pixer failed during bleVersion: {exc}") from exc
        else:
            raise PixerError(f"unexpected reply to #TEST#: {greeting!r}")
        status = {
            "BLE firmware": ble,
            "Display firmware": query(sock, b"iteVersion", timeout),
            "MCU firmware": query(sock, b"mcuVersion", timeout),
        }
        battery = query(sock, b"batteryLevel", timeout)
    return {"Battery": battery_percent(battery), **status}


def is_pixer(host: str, port: int, timeout: float = DISCOVER_TIMEOUT) -> bool:
    """Tell a Pixer from any other service on the port by its #TEST# greeting."""
    expected = STATUS_GREETING.encode("ascii")
    reply = b""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(b"#TEST#")
            deadline = time.monotonic() + timeout
            while len(reply) < len(expected):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                chunk = sock.recv(len(expected) - len(reply))
                if not chunk:
                    break
                reply += chunk
    except OSError:
        return False
    return reply == expected


def local_network() -> ipaddress.IPv4Network:
    """The /24 around the address this computer uses for its default route,
    or for the frame's hotspot when there is no default route."""
    source = local_source_address("192.0.2.1") or local_source_address(DEFAULT_HOST)
    if source is None:
        raise PixerError("this computer has no network route; pass a network such as 10.1.7.0/24")
    return ipaddress.ip_network(f"{source}/24", strict=False)


def discover(network: ipaddress.IPv4Network, port: int, timeout: float = DISCOVER_TIMEOUT) -> list[str]:
    if network.num_addresses > DISCOVER_MAX_HOSTS:
        raise PixerError(
            f"{network} has {network.num_addresses} addresses; scan at most {DISCOVER_MAX_HOSTS}"
        )
    hosts = [str(host) for host in network.hosts()]
    with ThreadPoolExecutor(DISCOVER_WORKERS) as pool:
        answers = pool.map(lambda host: is_pixer(host, port, timeout), hosts)
        return [host for host, answered in zip(hosts, answers) if answered]


def wifi_field(value: str, name: str) -> bytes:
    encoded = value.encode("utf-8")
    if not 1 <= len(encoded) <= 32 or b"\x00" in encoded:
        raise PixerError(f"{name} must be 1 to 32 UTF-8 bytes with no NUL bytes")
    return encoded.ljust(32, b"\x00")


def setup_wifi(ssid: str) -> bytes:
    ssid_field = wifi_field(ssid, "Wi-Fi name")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass(f"WPA2 password for {ssid!r}: ")
    except (EOFError, getpass.GetPassWarning) as exc:
        raise PixerError("enter the Wi-Fi password in an interactive terminal") from exc
    if len(password.encode("utf-8")) < 8:
        raise PixerError("the WPA2 password must be at least 8 bytes long")
    password_field = wifi_field(password, "Wi-Fi password")
    account = b"pixer@pixer.invalid".ljust(50, b"\x00")
    token = f"{secrets.randbelow(10_000):04d}{secrets.randbits(32):08x}".encode("ascii")
    settings = b"#UPDT#" + ssid_field + password_field + account + b"2" + token + b"1"

    with connect_to_pixer(DEFAULT_HOST, DEFAULT_PORT, PROBE_TIMEOUT) as sock, sock.makefile("rb") as reply:
        sock.settimeout(PAIRING_TIMEOUT)
        try:
            sock.sendall(b"#SSID#")
            count = reply.read(2)
            if len(count) != 2 or not count.isdigit():
                raise PixerError("Pixer did not return a valid Wi-Fi scan count")
            # Stock direct-mode firmware sends one blank record with count 00.
            scan_size = 33 * max(1, int(count))
            scan = reply.read(scan_size)
            if len(scan) != scan_size or (count == b"00" and scan != bytes(32) + b"0"):
                raise PixerError("Pixer did not return the expected Wi-Fi scan response")
        except OSError as exc:
            raise PixerError("could not read the Pixer Wi-Fi scan response") from exc
        try:
            sock.sendall(settings)
            if reply.read(6) != b"#SMAC#":
                raise PixerError("missing #SMAC# response")
            device_id = reply.read(25)
            if len(device_id) != 25 or device_id[12:13] != b"@":
                raise PixerError("incomplete device identifier")
            sock.sendall(b"#MACS#")
        except (OSError, PixerError) as exc:
            raise PixerError(
                f"Wi-Fi setup could not be confirmed ({exc}). Check whether the "
                "frame joined the target network before trying again."
            ) from exc
        return device_id


def upload_payload(
    payload: bytes,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    connect_timeout: float = 10.0,
    ack_timeout: float = 180.0,
    progress_stream=None,
) -> bytes:
    header = file_header(len(payload))
    sent = 0
    progress = Progress(len(payload), stream=progress_stream)
    try:
        with connect_to_pixer(host, port, connect_timeout) as sock:
            sock.sendall(header)
            view = memoryview(payload)
            while sent < len(payload):
                end = min(sent + CHUNK_SIZE, len(payload))
                sock.sendall(view[sent:end])
                sent = end
                progress.sent(sent)
            sock.sendall(b"#MOVE#d")
            progress.close()
            progress.started = time.monotonic()

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
                progress.waiting()
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
        network = "its Wi-Fi" if host == DEFAULT_HOST else "a network that can reach it"
        raise PixerError(
            f"the connection to Pixer was lost after sending {sent} of "
            f"{len(payload)} bytes ({percent}%): {exc}\n"
            f"  Keep the frame powered and stay on {network}, then upload again."
        ) from exc
    finally:
        progress.close()


def tcp_port(text: str) -> int:
    port = int(text)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def battery_threshold(text: str) -> int:
    level = int(text)
    if not 1 <= level <= 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return level


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload an image directly to a G+ Pixer frame"
    )
    parser.add_argument("image", nargs="?", type=Path, help="JPEG, PNG, HEIC, or other ffmpeg-readable image")
    parser.add_argument("--host", help=f"frame address (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=tcp_port, default=DEFAULT_PORT)
    parser.add_argument(
        "--orientation",
        choices=("auto", "landscape", "portrait"),
        default="auto",
    )
    parser.add_argument("--fit", choices=("cover", "contain"), default="cover")
    parser.add_argument(
        "--dither",
        choices=DITHER_METHODS,
        default="app",
        help="how to reduce the image to 16 grays (default: app, the original app's)",
    )
    parser.add_argument("--output-bin", type=Path, help="save converted imagebin data")
    parser.add_argument("--dry-run", action="store_true", help="convert only; do not contact Pixer")
    parser.add_argument(
        "--min-battery",
        type=battery_threshold,
        metavar="PERCENT",
        help="skip the upload when the frame's battery is below PERCENT",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--probe", action="store_true", help="only test the TCP upload port")
    mode.add_argument("--status", action="store_true", help="read battery level and firmware versions")
    mode.add_argument(
        "--discover",
        nargs="?",
        const="",
        metavar="NETWORK",
        help="find frames on NETWORK (default: this computer's /24)",
    )
    mode.add_argument("--setup-wifi", metavar="SSID", help="join a WPA2 network from the frame's hotspot")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    host_given = args.host is not None
    if not host_given:
        args.host = DEFAULT_HOST
    try:
        if args.min_battery is not None and (
            args.dry_run
            or args.probe
            or args.status
            or args.discover is not None
            or args.setup_wifi is not None
        ):
            raise PixerError("--min-battery only applies to uploads")
        if args.setup_wifi is not None:
            if args.image is not None or args.dry_run or args.output_bin is not None:
                raise PixerError("--setup-wifi cannot be combined with image conversion")
            if (args.host, args.port) != (DEFAULT_HOST, DEFAULT_PORT):
                raise PixerError("--setup-wifi requires the frame's default hotspot address")
            device_id = setup_wifi(args.setup_wifi)
            print(f"Pixer acknowledged the Wi-Fi settings (ID {device_id!r}).")
            print("Join that network and run --discover to find the frame's IP address.")
            return 0
        if args.probe:
            probe_device(args.host, args.port)
            print(f"Pixer upload service is reachable at {args.host}:{args.port}")
            return 0
        if args.status or args.discover is not None:
            if args.image is not None or args.dry_run or args.output_bin is not None:
                flag = "--status" if args.status else "--discover"
                raise PixerError(f"{flag} cannot be combined with image conversion")
        if args.discover is not None:
            if host_given:
                raise PixerError("--discover finds the host itself; use --discover NETWORK instead of --host")
            try:
                network = (
                    ipaddress.IPv4Network(args.discover, strict=False)
                    if args.discover
                    else local_network()
                )
            except ValueError as exc:
                raise PixerError(f"not an IPv4 network: {args.discover!r}") from exc
            print(f"Scanning {network} for Pixer frames on port {args.port}...", flush=True)
            found = discover(network, args.port)
            if not found:
                raise PixerError(
                    f"no Pixer frame answered on {network}. Wake the frame with one "
                    "short press and try again."
                )
            for host in found:
                print(f"Found Pixer at {host}")
            return 0
        if args.status:
            status = read_status(args.host, args.port)
            print(f"Pixer at {args.host}:{args.port}")
            for label, value in status.items():
                print(f"  {label}: {value}")
            return 0
        if args.image is None:
            raise PixerError("an image path is required unless --probe is used")
        if not args.image.is_file():
            raise PixerError(f"image does not exist: {args.image}")
        if args.min_battery is not None:
            battery = int(read_status(args.host, args.port)["Battery"].removesuffix("%"))
            if battery < args.min_battery:
                raise PixerError(
                    f"the frame's battery is {battery}%, below --min-battery "
                    f"{args.min_battery}%; upload skipped. Charge the frame."
                )
            print(f"Battery {battery}%", flush=True)
        elif not args.dry_run:
            probe_device(args.host, args.port)

        payload, width, height = build_payload(
            args.image, args.orientation, args.fit, args.dither
        )
        print(f"Prepared {width}x{height}, {len(payload)} bytes", flush=True)
        if args.output_bin:
            args.output_bin.write_bytes(payload)
            print(f"Saved imagebin: {args.output_bin}", flush=True)
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
        if args.setup_wifi is not None:
            operation = "Wi-Fi setup"
        elif args.discover is not None:
            operation = "scan"
        else:
            operation = "upload"
        print(f"\ninterrupted before the {operation} finished", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
