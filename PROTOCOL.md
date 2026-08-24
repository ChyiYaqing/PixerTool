# Pixer local upload protocol

This protocol was recovered from `Pixer! 3.06.2` (`com.gigazone.main.pixer`).
The relevant decompiled methods are:

- `EditActivity.eternityImage`: grayscale dithering and 4-bit packing
- `ShareActivity.UploadImageTask.doInBackground`: direct-mode destination
- `ShareActivity.UploadImageTask.uploadFile(byte[], InetAddress, int, int, int)`: TCP framing

## Image format

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
does not inspect their content.

## Dormant wake command

The APK also contains an unused UDP/6000 wake routine. It sends `#WAKE#N` and
expects `#WKED#`. Because no live call site exists in 3.06.2, the Mac tool does
not depend on it.
