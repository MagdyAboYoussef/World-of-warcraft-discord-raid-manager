"""Download every icon the bot needs from the Wowhead CDN, resize, and sort.

    python -m tools.fetch_icons          # fetch anything missing
    python -m tools.fetch_icons --force  # re-fetch everything

Layout produced under assets/icons/:

    tank/    healer/    melee/    ranged/     <- one file per spec, by role
    class/                                    <- one file per class
    buff/                                     <- raid buff / debuff / utility

Wowhead serves icons at 56x56 ("large"); they are upscaled to ICON_SIZE with
LANCZOS, which is ample for Discord emojis (rendered at ~22-32px inline).

Some icon slugs are guesses - notably the Midnight-new Devourer spec. Each slug
may declare alternates in ALIASES; the first that returns HTTP 200 wins, and
anything that resolves to nothing at all is reported at the end so it can be
fixed rather than silently shipping a broken emoji.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.data import buffs as buffs_data  # noqa: E402
from bot.data.specs import CLASS_ICONS, ROLE_ORDER, SPECS  # noqa: E402

CDN = "https://wow.zamimg.com/images/wow/icons/large/{slug}.jpg"
ICON_SIZE = 64
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "icons"

#: Fallback slugs tried in order when the primary 404s.
ALIASES: dict[str, tuple[str, ...]] = {
    "inv_112_ability_demonhunter_metamorphasisvoid": (
        "ability_demonhunter_devourer",
        "classicon_demonhunter_devourer",
        "ability_demonhunter_specdps",
    ),
    "ability_shaman_repulsion": (
        "ability_skyreach_lens",
        "spell_nature_earthbindtotem",
        "ability_thunderking_thunderstruck",
    ),
    "ability_evoker_blessingofthebronze": ("ability_evoker_bronze", "ability_evoker_timespiral"),
    "ability_evoker_blueflight": ("ability_evoker_sourceofmagic", "spell_arcane_arcane01"),
    "ability_monk_sparring": ("ability_monk_mysticaltouch", "ability_monk_touchofdeath"),
    "ability_hunter_markedfordeath": ("ability_hunter_snipershot",),
    "ability_deathknight_aoedeathgrip": ("ability_deathknight_gorefiendsgrasp",),
    "spell_holy_wordfortitude": ("spell_holy_prayeroffortitude",),
    "warlock_healthstone": ("inv_stone_04", "warlock_-healthstone"),
    "ui_allianceicon-round": ("inv_shield_06", "ability_defend"),
}

session = requests.Session()
session.headers["User-Agent"] = "wow-raid-bot/1.0 (icon fetcher)"


def resolve(slug: str) -> tuple[str, bytes] | None:
    """Return (winning_slug, jpeg_bytes) for the first candidate that exists."""
    for candidate in (slug, *ALIASES.get(slug, ())):
        try:
            resp = session.get(CDN.format(slug=candidate), timeout=20)
        except requests.RequestException as exc:
            print(f"  ! {candidate}: {exc}")
            continue
        if resp.status_code == 200 and resp.content:
            return candidate, resp.content
    return None


def save(raw: bytes, dest: Path) -> None:
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    img = img.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG", optimize=True)


def fetch(slug: str, dest: Path, *, force: bool) -> str | None:
    """Download+save one icon. Returns the slug that actually worked, or None."""
    if dest.exists() and not force:
        return slug
    found = resolve(slug)
    if found is None:
        return None
    winner, raw = found
    save(raw, dest)
    if winner != slug:
        print(f"  ~ {slug} -> resolved via alias {winner}")
    return winner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="re-download existing icons")
    args = parser.parse_args()

    jobs: list[tuple[str, str, Path]] = []
    for spec in SPECS:
        jobs.append((f"{spec.role.value}/{spec.key}", spec.icon, ASSETS / spec.role.value / f"{spec.key}.png"))
    for role in ROLE_ORDER:
        jobs.append((f"role/{role.value}", role.icon, ASSETS / "role" / f"{role.value}.png"))
    for wow_class, slug in CLASS_ICONS.items():
        key = wow_class.lower().replace(" ", "_")
        jobs.append((f"class/{key}", slug, ASSETS / "class" / f"{key}.png"))
    for name, slug in buffs_data.icon_jobs():
        jobs.append((f"buff/{name}", slug, ASSETS / "buff" / f"{name}.png"))

    # Prune icons left behind by earlier runs whose slug has since changed.
    expected = {dest for _, _, dest in jobs}
    for stale in ASSETS.rglob("*.png"):
        if stale not in expected:
            stale.unlink()
            print(f"  - pruned stale {stale.relative_to(ASSETS)}")

    failures: list[tuple[str, str]] = []
    aliased: list[tuple[str, str]] = []
    for name, slug, dest in jobs:
        print(f"-> {name}")
        winner = fetch(slug, dest, force=args.force)
        if winner is None:
            failures.append((name, slug))
        elif winner != slug:
            aliased.append((slug, winner))

    print(f"\nDone. {len(jobs) - len(failures)}/{len(jobs)} icons in {ASSETS}")
    for role in ("tank", "healer", "melee", "ranged"):
        n = len(list((ASSETS / role).glob("*.png"))) if (ASSETS / role).exists() else 0
        print(f"  {role:<7} {n}")

    if aliased:
        print("\nResolved via alias - update the slug in bot/data/ to make it primary:")
        for slug, winner in aliased:
            print(f"  {slug} -> {winner}")
    if failures:
        print("\nFAILED - no candidate returned 200:")
        for name, slug in failures:
            print(f"  {name}  (tried {slug} + {len(ALIASES.get(slug, ()))} alias(es))")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
