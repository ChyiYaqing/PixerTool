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
