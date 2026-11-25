# Pixer local upload protocol

This protocol was recovered from `Pixer! 3.06.2` (`com.gigazone.main.pixer`).
The relevant decompiled methods are:

- `EditActivity.eternityImage`: grayscale dithering and 4-bit packing
- `ShareActivity.UploadImageTask.doInBackground`: direct-mode destination
- `ShareActivity.UploadImageTask.uploadFile(byte[], InetAddress, int, int, int)`: TCP framing

## Image format

The panel is 7.8" at 1872 x 1404, which the app addresses in either
orientation.

- Landscape input canvas: 1872 x 1404 pixels
- Portrait input canvas: 1404 x 1872 pixels, rotated while packing
- 16 grayscale levels (0 through 15)
- Two pixels per byte; the first pixel occupies the low nibble
- Payload size: 1,314,144 bytes in both orientations

The app first converts RGB with integer weights 19595, 38469, and 7471
(approximately Rec. 601). It then uses a one-dimensional error buffer that
persists between rows. Its quantization divisor is 17, producing 16 levels
from 8-bit grayscale.

## Transport

- Direct-mode host: `192.168.1.1`
- TCP port: `6000`
- No authentication or encryption

The sender opens one TCP connection and writes, without separators:

1. `#file#0008`
2. Payload length as eight zero-padded decimal digits
3. `imagebin`
4. Packed image bytes
5. `#MOVE#d`

For a normal Pixer image the complete header is:

```text
#file#000801314144imagebin
```

The Android app treats receipt of any six bytes after `#MOVE#d` as success; it
does not inspect their content. A 7.8" Pixer running stock firmware answers
`#end1#`.

## Status queries

The app's hotspot check (`MainActivityV2.checkPixer`) sends each command
without a separator and treats its next read of up to 64 bytes as the reply.
On a 7.8" frame with MCU firmware `GBT_170206-1_7.8A`, reached over a home
network, each reply arrived in a single read with no terminator, 8 to 210 ms
after the command:

| Command | Reply | Measured reply |
| --- | --- | --- |
| `#TEST#` | Greeting | `Hello PC!` |
| `bleVersion` | BLE firmware version | `GBT_BLE_v.A.14` |
| `iteVersion` | Display controller firmware version | `GBT_v.A.35` |
| `mcuVersion` | MCU firmware version | `GBT_170206-1_7.8A` |
| `batteryLevel` | Battery percentage | `100` |

When the greeting is longer than 9 characters, the app takes the text after
the first 9 as the BLE version and sends `bleVersion` without reading a
reply. That firmware variant has not been observed. PixerTool reads the BLE
version this way only when the longer greeting starts with `Hello PC!`, then
sends `bleVersion` and discards anything that arrives within 0.3 seconds. When the MCU version
has at least three `_`-separated fields, the app reads the second field, with
`-` removed, as a build number (`1702061` above).

TCP does not preserve message boundaries, so PixerTool keeps reading a reply
until the frame has been quiet for 0.3 seconds, 64 bytes have arrived, or the
connection closes.

The app's device page (`FrameInfoActivity.ConnTask`) instead sends `#STAT#`
to the address and port advertised by the mDNS service `_gigazone._tcp`, and
reads until `#END#`. The same frame answered with three comma-separated
fields: the 25-byte `MAC@serial` device identifier, the battery percentage,
and `#END#`. PixerTool does not send this query.

The app also sends commands that unpair the frame (`#TOFF#`, `#CLBT#`) and
that query or install firmware (`mcuImage`, `sendFile`). PixerTool does not
send them.

## Dormant wake command

The APK also contains an unused UDP/6000 wake routine. It sends `#WAKE#N` and
expects `#WKED#`. Because no live call site exists in 3.06.2, the Mac tool does
not depend on it.

## Wi-Fi setup

The Android app's `PairingActivity` uses TCP port 6000 on the frame's hotspot.
It sends `#SSID#` and reads a two-digit ASCII count, then one 33-byte record per
network. Each record contains a 32-byte NUL-padded SSID and one ASCII security
type. Type `0` represents an open network.

Stock direct-mode firmware can return a fixed `00` followed by one blank
33-byte record with type `0`. PixerTool recognizes that response and uses
type `2` for the requested WPA2-Personal network.

The 134-byte setup request contains these fields in order. Text fields use UTF-8.

| Field | Bytes |
| --- | ---: |
| `#UPDT#` | 6 |
| SSID, NUL-padded | 32 |
| Password, NUL-padded or all zero for an open network | 32 |
| Account identifier, NUL-padded | 50 |
| Security type (`2` for WPA/WPA2-Personal) | 1 |
| Token with four decimal and eight lowercase hexadecimal digits | 12 |
| Official server selector (`1`) | 1 |

PixerTool uses `pixer@pixer.invalid` as a local account identifier. The frame
returns `#SMAC#` followed by a 25-byte device identifier in the form
`MAC@serial`, which the sender acknowledges with `#MACS#`. The original app
then checked connection status through its cloud service. PixerTool uses the
router's client list to confirm association separately. The setup request
travels in plaintext over the hotspot.

## Display refresh: whole-panel only

The panel is driven by an ITE IT8951 controller: `res/raw/ite.bin` in the app
carries the header `ITEEPD8951_A0100` and the string `GBT_v.A.35`, which equals
the frame's reported `iteVersion`. The IT8951 itself supports partial-area
updates and fast waveforms — `ite.bin` contains `Display(x,y,w,h)`,
`ChangeWaveformMode`, `a2 mode`, `5-bits wbf`, `IT8951FillRectTest` and
`IT8951WbfTest` — but those belong to the controller's built-in test console.

The main MCU firmware (`res/raw/pixer.bin`) does not expose any of that over
the network. Its image path reads the byte after `#MOVE#` only to log it
(`#MOVE#: %c`), then runs one fixed `Display, ITE_UPDATE` followed by
`WiFi, ITE_DONE`. `pixer.bin` holds no display area, partial, fast or refresh
strings; the only `region` hits are unrelated ARM MPU memory-fault messages
(`A_invalidRegionId`, `A_unalignedBaseAddr`). The Android app sends `#MOVE#d`
from a single call site (`ShareActivity`, always `d`), so `d` is not a mode
selector, and the app has no text-push or region command of its own.

Conclusion: with the shipped firmware, every upload triggers a full-panel
refresh (about 5–10 seconds, with flashing). Partial or fast refresh is not
reachable through the network protocol. The waveform images in the app
(`waveform_new.bin`, `ite_new.bin`) only tune the physical drive curves; they
do not change the MCU's full-refresh call path, and rewriting them risks
physically damaging the panel. The only path to partial refresh would be custom
MCU firmware, which PixerTool does not attempt: it never sends firmware or
unpair commands (`#TOFF#`, `#CLBT#`, and the `ite_new.bin` / `pixer.bin` /
`waveform_new.bin` upload paths).

Two questions remain open and would need a disassembler (Ghidra/IDA) on
`pixer.bin` rather than string searches to answer: whether the `#MOVE#` handler
has an unused `switch` over its byte (for example `#MOVE#f`/`p`/`a`), and
whether the MCU ever issues the IT8951 `DPY_AREA` command (`0x0037`). Neither is
exercised by the app, and using either would still likely require new firmware.

Design consequence: treat the frame as slow media. Refresh once or a few times a
day, and keep a fixed template so that most of the panel is unchanged between
images, which softens the visible flash.
