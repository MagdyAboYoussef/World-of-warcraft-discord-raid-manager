"""Download the custom role icons, cut their background out, and vendor them.

    python -m tools.vendor_role_icons

Writes assets/role_icons/<role>.png, which is committed to the repository and
copied into place by tools/fetch_icons.py. Run this only when a role's source
image changes; the everyday path never touches the network for these.

Backgrounds are removed by flood-filling inwards from the border rather than by
deleting every pale pixel. That distinction matters: a healer icon is a white
cross on white, and "make white transparent" would erase the cross along with
the background. Only the pale region *connected to the edge* is background.
"""

from __future__ import annotations

import io
import sys
from collections import deque
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402
from PIL import Image  # noqa: E402

from bot.data.specs import ROLE_ICON_SOURCE_URLS, VENDORED_ROLE_ICONS  # noqa: E402

DEST = Path(__file__).resolve().parents[1] / "assets" / "role_icons"
SIZE = 64

#: How far a pixel may sit from the background colour and still count as
#: background. Generous enough for JPEG ringing around a hard edge, tight enough
#: not to swallow a pale icon.
TOLERANCE = 60

#: Pixels this close to the background get partial alpha instead of full, which
#: is what stops the cut-out edge looking like a jigsaw piece.
FEATHER = 90


def _distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def cut_background(img: Image.Image) -> Image.Image:
    """Return an RGBA copy with the edge-connected background made transparent."""
    img = img.convert("RGBA")
    width, height = img.size
    pixels = img.load()

    border = [pixels[x, 0] for x in range(width)] + [pixels[x, height - 1] for x in range(width)]
    background = max(set(p[:3] for p in border), key=lambda c: border.count(c + (255,)))

    # Flood fill inwards from every border pixel.
    outside: set[tuple[int, int]] = set()
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        queue.extend([(x, 0), (x, height - 1)])
    for y in range(height):
        queue.extend([(0, y), (width - 1, y)])

    while queue:
        x, y = queue.popleft()
        if (x, y) in outside or not (0 <= x < width and 0 <= y < height):
            continue
        if _distance(pixels[x, y][:3], background) > TOLERANCE:
            continue
        outside.add((x, y))
        queue.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])

    for x, y in outside:
        pixels[x, y] = (0, 0, 0, 0)

    # Soften whatever is left touching a hole, so the downscale doesn't leave a
    # hard pale rim around the icon.
    for x, y in list(outside):
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if not (0 <= nx < width and 0 <= ny < height):
                continue
            r, g, b, a = pixels[nx, ny]
            if a == 0:
                continue
            nearness = _distance((r, g, b), background)
            if nearness < FEATHER:
                pixels[nx, ny] = (r, g, b, int(255 * nearness / FEATHER))
    return img


def square(img: Image.Image) -> Image.Image:
    if img.width == img.height:
        return img
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    return img.crop((left, top, left + side, top + side))


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "wow-raid-bot/1.0 (icon fetcher)"

    failures = 0
    for role in VENDORED_ROLE_ICONS:
        url = ROLE_ICON_SOURCE_URLS.get(role)
        if not url:
            print(f"  ! {role.value}: no source URL recorded")
            failures += 1
            continue
        try:
            response = session.get(url, timeout=25)
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"  ! {role.value}: {exc}")
            failures += 1
            continue

        img = square(Image.open(io.BytesIO(response.content)))
        img = cut_background(img).resize((SIZE, SIZE), Image.Resampling.LANCZOS)

        out = DEST / f"{role.value}.png"
        img.save(out, "PNG", optimize=True)
        alpha = img.getchannel("A").histogram()
        clear, opaque = alpha[0], sum(alpha[251:])
        print(
            f"  = {role.value:<7} {out.stat().st_size:>6,} bytes  "
            f"{clear:>4} transparent / {opaque:>4} opaque of {SIZE * SIZE}"
        )

    print("\nRun `python -m tools.fetch_icons` to copy these into assets/icons/role/,")
    print("then restart the bot so it re-uploads the application emojis.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
