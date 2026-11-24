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

<p align="center">
  <img src="misc/images/pixer-hello.jpg" width="620"
       alt="A G+ Pixer frame displaying an image sent by PixerTool">
</p>

## The device

| | |
| --- | --- |
| Model | G+ Pixer (`com.gigazone.main.pixer`, app "Pixer!") |
| Panel | 7.8", 1872 x 1404, 16 grayscale levels |
| Wi-Fi | 802.11 b/g/n, 1T1R |
| Battery | Li-ion 8000 mAh |
| Regions | CN / US / JP / RU / TW |

A single image is 1,314,144 bytes on the wire. The frame's single-stream
802.11n radio has been measured at about 0.3 MB/s, so roughly four seconds per
upload. The frame acknowledges as soon as it holds the image and redraws its
panel afterwards.

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
- A connection to the Pixer hotspot (for example, `Pixer 0939`) or a network that can reach the configured frame

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

### Find the frame on your network

```bash
python3 pixer_upload.py --discover
```

This connects to port 6000 on every address in a /24 network and lists the
hosts that answer the status greeting `#TEST#` with `Hello PC!`. Any other
service listening on that port also receives `#TEST#`, so scan only networks
you are allowed to probe. A scan of a /24 takes about four seconds.

By default the scan covers the /24 around the address this computer uses for
its default route, or for the frame's hotspot when there is none. To scan
another network, name it, for example `--discover 10.1.0.0/22`. A sleeping
frame does not answer, so press its button once before scanning.

### Read battery and firmware status

```bash
python3 pixer_upload.py --status
```

This reports the battery level and the BLE, display, and MCU firmware
versions. It works on the frame's hotspot and, with `--host FRAME_IP`, on a
network the frame has joined. The query is read-only.

```text
Pixer at 10.1.7.102:6000
  Battery: 100%
  BLE firmware: GBT_BLE_v.A.14
  Display firmware: GBT_v.A.35
  MCU firmware: GBT_170206-1_7.8A
```

### Join an existing Wi-Fi network

Connect to the hotspot broadcast by your own frame, then run this one-time
setup command:

```bash
python3 pixer_upload.py --setup-wifi Home-WiFi
```

Use a 2.4 GHz WPA2-Personal network. The command prompts for the password
without echoing it and sends it over the frame's hotspot in plaintext. Stock
direct-mode firmware can report an empty Wi-Fi scan even with networks in
range, so the command uses the network name you provide. The frame may drop
its hotspot while joining.

A setup acknowledgement means the frame received the settings. Join the
target network, then run `--discover` to confirm the frame joined and find its
IP address. The router's client list shows the same. From the computer that
will run the scheduled job, check TCP reachability and send a photo:

```bash
python3 pixer_upload.py --probe --host FRAME_IP
python3 pixer_upload.py photo.jpg --host FRAME_IP
```

If both commands work, reserve the frame's IP address on the router and use the
upload command in the scheduler. Repeat the check after a normal restart and
after the frame has been idle to confirm it reconnects and accepts unattended
uploads. LAN upload and wake behavior depend on the frame firmware.

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--orientation {auto,landscape,portrait}` | `auto` | Chosen from the source image's aspect ratio when `auto`. |
| `--fit {cover,contain}` | `cover` | `cover` crops to fill the panel; `contain` letterboxes on white. |
| `--host` / `--port` | `192.168.1.1` / `6000` | Frame address and port. Defaults are for direct mode. |
| `--output-bin PATH` | — | Write the packed payload to a file. |
| `--dry-run` | off | Convert only; never open a socket. |
| `--probe` | off | Test TCP reachability and exit. |
| `--status` | off | Read the battery level and firmware versions, then exit. |
| `--discover [NETWORK]` | off | List frames on NETWORK, or on this computer's /24 network, then exit. |
| `--setup-wifi SSID` | off | Send Wi-Fi settings to the frame over its hotspot. |

Before converting anything, an upload checks that the frame is actually
answering, so a wrong Wi-Fi network fails in a few seconds instead of after the
image has been processed. The transfer then reports itself on one line, and the
gap before the frame answers is shown as a running count rather than silence:

![PixerTool uploading an image](misc/images/upload-demo.gif)

The uploader waits up to three minutes for the acknowledgement, which in
practice arrives within a second or two of the last byte. Keep the frame
powered and stay connected until it appears. When output is piped to a file,
progress degrades to one plain line per 10% instead of redrawing.

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

Use `--setup-wifi` to change the frame's Wi-Fi settings.

## Scope and legal note

This is an interoperability tool for hardware its owners already possess. It
reimplements a documented wire format from scratch; no decompiled or otherwise
proprietary code from the original application is included in this repository.
It is not affiliated with, endorsed by, or supported by the makers of Pixer.

## License

[MIT](LICENSE)
