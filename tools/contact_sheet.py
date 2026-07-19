"""Render every fetched icon into one labelled PNG for eyeball verification.

    python -m tools.contact_sheet [out.png]

A slug returning HTTP 200 only proves the file exists - not that it is the
right art. This sheet makes a wrong guess obvious at a glance.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "icons"

CELL = 64
PAD = 8
LABEL_H = 26
COLS = 8
HEADER_H = 30
BG = (24, 26, 31)
FG = (225, 228, 235)
HEAD_FG = (255, 196, 84)


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "assets" / "contact_sheet.png"
    groups = [d for d in sorted(ASSETS.iterdir()) if d.is_dir()]
    font = load_font(11)
    head_font = load_font(16)

    cell_w = CELL + PAD * 2
    cell_h = CELL + LABEL_H + PAD
    height = sum(HEADER_H + ((len(list(g.glob("*.png"))) + COLS - 1) // COLS) * cell_h for g in groups) + PAD
    sheet = Image.new("RGB", (COLS * cell_w, height), BG)
    draw = ImageDraw.Draw(sheet)

    y = PAD
    for group in groups:
        files = sorted(group.glob("*.png"))
        draw.text((PAD, y), f"{group.name.upper()}  ({len(files)})", font=head_font, fill=HEAD_FG)
        y += HEADER_H
        for i, f in enumerate(files):
            col, row = i % COLS, i // COLS
            x = col * cell_w + PAD
            iy = y + row * cell_h
            sheet.paste(Image.open(f).convert("RGB"), (x, iy))
            label = f.stem
            if len(label) > 15:
                label = label[:14] + "…"
            draw.text((x, iy + CELL + 3), label, font=font, fill=FG)
        y += ((len(files) + COLS - 1) // COLS) * cell_h

    sheet.save(out)
    print(f"wrote {out}  ({sheet.width}x{sheet.height})")


if __name__ == "__main__":
    main()
