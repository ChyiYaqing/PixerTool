#!/usr/bin/env python3
"""Serve a web page that sends photos from any phone on the network to a Pixer frame."""

from __future__ import annotations

import argparse
import io
import ipaddress
import json
import socket
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pixer_upload as pixer

DEFAULT_WEB_PORT = 8000
MAX_UPLOAD = 50 * 1024 * 1024
FITS = ("cover", "contain")
READ_TIMEOUT = 30.0
UPLOAD_DEADLINE = 120.0

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Send to Pixer</title>
<style>
  body { font: 17px/1.4 -apple-system, system-ui, sans-serif; margin: 0 auto;
         max-width: 28rem; padding: 1.5rem; color: #222; background: #f4f4f2; }
  h1 { font-size: 1.4rem; margin: 0 0 1rem; }
  label.pick { display: block; padding: 1rem; border: 2px dashed #999; border-radius: 12px;
               text-align: center; background: #fff; cursor: pointer; }
  input[type=file] { display: none; }
  img { display: none; width: 100%; margin-top: 1rem; border-radius: 8px; filter: grayscale(1); }
  fieldset { border: 0; padding: 0; margin: 1rem 0; display: flex; gap: 1.5rem; }
  button { width: 100%; padding: 1rem; font-size: 1.1rem; border: 0; border-radius: 12px;
           background: #222; color: #fff; }
  button:disabled { background: #999; }
  p#status { min-height: 3em; white-space: pre-wrap; }
  .error { color: #b00020; }
</style>
</head>
<body>
<h1>Send to Pixer</h1>
<label class="pick">Choose a photo (JPEG or PNG)<input id="photo" type="file" accept="image/*"></label>
<img id="preview" alt="">
<fieldset>
  <label><input type="radio" name="fit" value="cover" checked> Fill the frame</label>
  <label><input type="radio" name="fit" value="contain"> Show whole photo</label>
</fieldset>
<button id="send" disabled>Send</button>
<p id="status"></p>
<script>
const photo = document.getElementById("photo");
const preview = document.getElementById("preview");
const send = document.getElementById("send");
const status = document.getElementById("status");
photo.addEventListener("change", () => {
  const file = photo.files[0];
  send.disabled = !file;
  status.textContent = "";
  if (file) { preview.src = URL.createObjectURL(file); preview.style.display = "block"; }
});
send.addEventListener("click", async () => {
  const fit = document.querySelector("input[name=fit]:checked").value;
  send.disabled = true;
  photo.disabled = true;
  status.className = "";
  status.textContent = "Sending... the frame redraws in about 10 seconds.";
  try {
    const response = await fetch("/upload?fit=" + fit, { method: "POST", body: photo.files[0] });
    const result = await response.json();
    status.className = result.ok ? "" : "error";
    status.textContent = result.message;
  } catch (error) {
    status.className = "error";
    status.textContent = "Could not reach the computer running pixer_web.py.";
  }
  photo.disabled = false;
  send.disabled = false;
});
</script>
</body>
</html>
"""


def image_problem(data: bytes) -> str | None:
    """Accept only JPEG and PNG, so ffmpeg never probes playlists or other formats."""
    if data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if data[4:8] == b"ftyp":
        return "HEIC photos are not supported yet. Send a JPEG or PNG instead."
    return "Only JPEG and PNG photos are supported."


def same_site(headers) -> bool:
    """Refuse uploads from other websites, including via DNS rebinding."""
    host = headers.get("Host", "")
    try:
        name = urlsplit("//" + host).hostname or ""
    except ValueError:
        return False
    if name != "localhost" and not name.endswith(".local"):
        try:
            ipaddress.ip_address(name)
        except ValueError:
            return False
    origin = headers.get("Origin")
    return origin is None or origin == f"http://{host}"


class UploadHandler(BaseHTTPRequestHandler):
    server: "PixerWebServer"
    timeout = READ_TIMEOUT

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = PAGE.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        if url.path != "/upload":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not same_site(self.headers):
            self.reply(HTTPStatus.FORBIDDEN, "Open this page from the computer's address instead.")
            return
        fit = parse_qs(url.query).get("fit", ["cover"])[0]
        if fit not in FITS:
            self.reply(HTTPStatus.BAD_REQUEST, f"fit must be one of: {', '.join(FITS)}")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0:
            self.reply(HTTPStatus.BAD_REQUEST, "Choose a photo first.")
            return
        if length > MAX_UPLOAD:
            self.close_connection = True
            self.reply(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"The photo is larger than {MAX_UPLOAD // pixer.MEGABYTE} MB.",
            )
            return
        if not self.server.frame_lock.acquire(blocking=False):
            self.close_connection = True
            self.reply(HTTPStatus.CONFLICT, "Another photo is being sent. Try again in a moment.")
            return
        try:
            # Hold the frame against other processes too, such as a scheduled
            # upload, before reading the photo: a busy frame is refused at once.
            with pixer.frame_lock(self.server.frame_host, self.server.frame_port, wait=0):
                status, message = self.receive(length, fit)
        except pixer.FrameBusy:
            self.close_connection = True
            status, message = HTTPStatus.CONFLICT, "The frame is busy with another upload. Try again in a minute."
        except OSError as exc:
            # The lock file itself could not be opened, for example another user's.
            self.close_connection = True
            status, message = HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not reserve the frame: {exc}"
        finally:
            # Free the frame before replying: a client may send its next photo as
            # soon as it reads the reply.
            self.server.frame_lock.release()
        self.reply(status, message)

    def receive(self, length: int, fit: str) -> tuple[HTTPStatus, str]:
        try:
            image = self.read_body(length)
        except socket.timeout:
            self.close_connection = True
            return HTTPStatus.REQUEST_TIMEOUT, "The upload took too long. Try again."
        if len(image) != length:
            self.close_connection = True
            return HTTPStatus.BAD_REQUEST, "The photo did not arrive completely."
        problem = image_problem(image)
        if problem:
            return HTTPStatus.BAD_REQUEST, problem
        return self.send_to_frame(image, fit)

    def read_body(self, length: int) -> bytes:
        """Read the photo, giving up after UPLOAD_DEADLINE however steadily it trickles in."""
        deadline = time.monotonic() + UPLOAD_DEADLINE
        body = bytearray()
        while len(body) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("upload deadline passed")
            self.connection.settimeout(min(self.timeout, remaining))
            chunk = self.rfile.read1(min(length - len(body), 64 * 1024))
            if not chunk:
                break
            body.extend(chunk)
        return bytes(body)

    def send_to_frame(self, image: bytes, fit: str) -> tuple[HTTPStatus, str]:
        with tempfile.TemporaryDirectory(prefix="pixer-web-") as folder:
            path = Path(folder) / "upload"
            path.write_bytes(image)
            try:
                payload, _, _ = pixer.build_payload(path, "auto", fit)
            except (pixer.PixerError, ValueError, OSError) as exc:
                detail = str(exc).replace(str(path), "the photo")
                return HTTPStatus.BAD_REQUEST, f"This photo could not be converted: {detail}"
        host, port, minimum = self.server.frame_host, self.server.frame_port, self.server.min_battery
        try:
            if minimum is not None:
                battery = pixer.read_battery(host, port)
                if battery < minimum:
                    return (
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        f"The frame's battery is {battery}%, below {minimum}%. "
                        "Charge the frame, then send the photo again.",
                    )
            pixer.upload_payload(payload, host, port, progress_stream=io.StringIO())
        except (pixer.PixerError, OSError) as exc:
            return HTTPStatus.BAD_GATEWAY, f"The frame did not take the photo: {exc}"
        return HTTPStatus.OK, "The frame received the photo and is redrawing."

    def reply(self, status: HTTPStatus, message: str) -> None:
        body = json.dumps({"ok": status == HTTPStatus.OK, "message": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.log_message("%s", message)


class PixerWebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], frame_host: str, frame_port: int, min_battery: int | None = None
    ) -> None:
        super().__init__(address, UploadHandler)
        self.frame_host = frame_host
        self.frame_port = frame_port
        self.min_battery = min_battery
        self.frame_lock = threading.Lock()


def find_frame(port: int) -> str:
    network = pixer.local_network()
    print(f"Scanning {network} for a Pixer frame...", flush=True)
    found = pixer.discover(network, port)
    if not found:
        raise pixer.PixerError(
            f"no Pixer frame answered on {network}. Wake the frame with one short "
            "press, or pass --host."
        )
    if len(found) > 1:
        raise pixer.PixerError(f"found several frames ({', '.join(found)}); pick one with --host")
    return found[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="frame address (default: find it on this computer's /24)")
    parser.add_argument("--port", type=pixer.tcp_port, default=pixer.DEFAULT_PORT, help="frame port")
    parser.add_argument(
        "--web-port",
        type=pixer.tcp_port,
        default=DEFAULT_WEB_PORT,
        help=f"port for the web page (default: {DEFAULT_WEB_PORT})",
    )
    parser.add_argument(
        "--min-battery",
        type=pixer.battery_threshold,
        metavar="PERCENT",
        help="refuse photos when the frame's battery is below PERCENT",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        host = args.host or find_frame(args.port)
        server = PixerWebServer(("0.0.0.0", args.web_port), host, args.port, args.min_battery)
    except (pixer.PixerError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    address = pixer.local_source_address(host) or "this-computer"
    print(f"Sending photos to the Pixer frame at {host}:{args.port}")
    print(f"On a phone on the same network, open http://{address}:{args.web_port}")
    print("Press Ctrl-C to stop.", flush=True)
    with server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
