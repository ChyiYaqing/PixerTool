#!/usr/bin/env python3
"""Write a 1872 x 1404 grayscale test card for the Pixer panel as a PGM file.

Top third: the panel's 16 gray levels, black to white, one block each.
Middle third: a smooth ramp, which shows how each --dither setting renders.
Bottom third: black and white stripes 1, 2, 3 and 4 pixels wide, vertical
above and horizontal below.
"""

from __future__ import annotations

import argparse
from pathlib import Path

WIDTH, HEIGHT = 1872, 1404
BAND = HEIGHT // 3
STRIPE_WIDTHS = (1, 2, 3, 4)


def test_card() -> bytes:
    steps = bytes(17 * (column * 16 // WIDTH) for column in range(WIDTH))
    ramp = bytes(column * 255 // (WIDTH - 1) for column in range(WIDTH))
    cell = WIDTH // len(STRIPE_WIDTHS)

    def stripe(position: int, width: int) -> int:
        return 0 if (position // width) % 2 == 0 else 255

    vertical = bytes(stripe(column % cell, STRIPE_WIDTHS[column // cell]) for column in range(WIDTH))
    rows = [steps] * BAND + [ramp] * BAND
    half = (HEIGHT - 2 * BAND) // 2
    rows += [vertical] * half
    for row in range(HEIGHT - 2 * BAND - half):
        rows.append(bytes(stripe(row, STRIPE_WIDTHS[column // cell]) for column in range(WIDTH)))
    return b"".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path, help="PGM file to write, for example card.pgm")
    output = parser.parse_args(argv).output
    output.write_bytes(b"P5\n%d %d\n255\n" % (WIDTH, HEIGHT) + test_card())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
