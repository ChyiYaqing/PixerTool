# PixerTool

Upload a photo from macOS or Linux straight to a **G+ Pixer** e-ink photo frame,
without the discontinued mobile app.

The Pixer app was pulled from the app stores, which left the frames unable to
receive new images. PixerTool reimplements the frame's local upload protocol —
recovered from the last published APK — as a single Python script with no
third-party dependencies.

```bash
python3 pixer_upload.py photo.jpg
```

## The device

| | |
| --- | --- |
| Model | G+ Pixer (`com.gigazone.main.pixer`, app "Pixer!") |
| Panel | 7.8", 1872 x 1404, 16 grayscale levels |
| Wi-Fi | 802.11 b/g/n, 1T1R |
| Battery | Li-ion 8000 mAh |
| Regions | CN / US / JP / RU / TW |

A single image is 1,314,144 bytes on the wire. Over the frame's single-stream
802.11n radio that is a few seconds of transfer, followed by tens of seconds of
panel refresh.

## How it works

The frame runs a plain TCP server on `192.168.1.1:6000` while in direct mode.
An image is sent as a 1872 x 1404 (or 1404 x 1872) buffer of 16 grayscale
levels, two pixels packed per byte, preceded by a short ASCII header:

```text
#file#000801314144imagebin<1314144 bytes><#MOVE#d>
```

Getting an image to look right on the panel means matching the app's pipeline
exactly: integer Rec. 601 grayscale, a one-dimensional error-diffusion dither
with a divisor of 17, and Java's truncate-toward-zero integer division. Those
steps are reproduced byte-for-byte here and covered by unit tests.

The full wire format is documented in [PROTOCOL.md](PROTOCOL.md).

## Requirements

- Python 3.9 or newer (no packages to install)
- `ffmpeg` and `ffprobe` on `PATH` — `brew install ffmpeg`
- The computer joined to the Pixer's own Wi-Fi network (for example, `Pixer 0939`)

## Usage

Wake the frame with one short press, connect to its Wi-Fi, then check that the
upload service is reachable:

```bash
python3 pixer_upload.py --probe
```

Convert an image without touching the frame, and keep the result for
inspection:

```bash
python3 pixer_upload.py photo.jpg --dry-run --output-bin photo.imagebin
```

Upload for real:

```bash
python3 pixer_upload.py photo.jpg
```

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--orientation {auto,landscape,portrait}` | `auto` | Chosen from the source image's aspect ratio when `auto`. |
| `--fit {cover,contain}` | `cover` | `cover` crops to fill the panel; `contain` letterboxes on white. |
| `--host` / `--port` | `192.168.1.1` / `6000` | Frame address in direct mode. |
| `--output-bin PATH` | — | Write the packed payload to a file. |
| `--dry-run` | off | Convert only; never open a socket. |
| `--probe` | off | Test TCP reachability and exit. |

Before converting anything, an upload checks that the frame is actually
answering, so a wrong Wi-Fi network fails in a few seconds instead of after the
image has been processed. The transfer then reports itself on one line:

```text
Prepared 1872x1404, 1314144 bytes
  [████████████░░░░░░░░░░░░░░░░]  45%  0.56/1.25 MB  1.4 MB/s
```

Once every byte is sent, the panel can take tens of seconds to refresh, so the
wait is shown with a running count rather than silence:

```text
  [████████████████████████████] 100%  1.25/1.25 MB  1.4 MB/s
  Waiting for the frame to refresh... 18s
Pixer acknowledged the upload: #end1#
```

Keep the frame powered and stay connected until that acknowledgement appears.
When output is piped to a file, progress degrades to one plain line per 10%
instead of redrawing.

## Troubleshooting

Failures name the likely cause rather than the raw socket error. If the
computer is still on its usual network:

```text
error: cannot reach Pixer at 192.168.1.1:6000: no response within 5 seconds.
  This computer is at 10.1.7.18, a different network from 192.168.1.1. Join the
  frame's own Wi-Fi network (for example "Pixer 0939"), then retry.
```

| Message | Meaning |
| --- | --- |
| `a different network from 192.168.1.1` | Not joined to the frame's Wi-Fi. |
| `on the same network ... probably asleep` | Right network; wake the frame with one short press. |
| `nothing is listening on port 6000` | A device answered — usually a home router that also uses `192.168.1.1`. |
| `connection to Pixer was lost after sending N of M bytes` | The link dropped mid-image; the frame keeps its previous picture. Upload again. |
| `did not acknowledge completion` | All bytes arrived; the panel may still be refreshing. Look at it before retrying. |

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests pin the encoder against values taken from the decompiled app —
grayscale weights, dither output, nibble packing order (including the portrait
rotation), and the header format.

## Recovering a stuck frame

- Short press — power on / wake
- Hold 5 seconds — ordinary restart
- Hold 15 seconds — factory reset
- The restart-pin shipped with the frame drives its pinhole reset

PixerTool never touches Wi-Fi settings or firmware. A successful upload only
replaces the displayed image.

## Scope and legal note

This is an interoperability tool for hardware its owners already possess. It
reimplements a documented wire format from scratch; no decompiled or otherwise
proprietary code from the original application is included in this repository.
It is not affiliated with, endorsed by, or supported by the makers of Pixer.

## License

[MIT](LICENSE)
