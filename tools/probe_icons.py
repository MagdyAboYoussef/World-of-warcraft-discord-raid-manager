"""Probe candidate icon slugs against the Wowhead CDN. Ad-hoc helper.

    python -m tools.probe_icons skyfury spell_nature_thunderclap ...
"""

from __future__ import annotations

import sys

import requests

CDN = "https://wow.zamimg.com/images/wow/icons/large/{slug}.jpg"

CANDIDATES: dict[str, list[str]] = {
    "death_grip": ["spell_deathknight_strangulate", "ability_deathknight_deathgrip2"],
    "thunder_clap": ["spell_nature_thunderclap", "ability_thunderclap"],
    "skyfury": [
        "inv_ability_shaman_skyfury",
        "ability_skyreach_lens",
        "spell_shaman_blessingoftheeternals",
        "inv_10_elementalshardfoozles_air",
        "spell_nature_cyclone",
    ],
    "source_of_magic": [
        "ability_evoker_sourceofmagic",
        "ability_evoker_blue_01",
        "inv_ability_evoker_sourceofmagic",
        "spell_arcane_arcane01",
    ],
    "mystic_touch": ["ability_monk_mysticaltouch", "monk_ability_transcendence", "ability_monk_sparring"],
    "chaos_brand": ["ability_demonhunter_chaosbrand", "spell_shadow_shadowbolt", "ability_demonhunter_empowerwards"],
    "hunters_mark": ["ability_hunter_markedfordeath", "ability_hunter_snipershot"],
    "fortitude": ["spell_holy_wordfortitude", "spell_holy_prayeroffortitude"],
    "atrophic_poison": ["ability_rogue_atrophicpoison", "inv_misc_herb_16", "ability_rogue_deadlybrew"],
    "bronze": ["ability_evoker_blessingofthebronze", "ability_evoker_bronze"],
    "devotion_aura": ["spell_holy_devotionaura"],
    "battle_res": ["spell_nature_reincarnation", "spell_shadow_deadofnight"],
}


def probe(slug: str) -> bool:
    try:
        return requests.head(CDN.format(slug=slug), timeout=15).status_code == 200
    except requests.RequestException:
        return False


def main() -> None:
    groups = sys.argv[1:] or list(CANDIDATES)
    for group in groups:
        print(f"\n{group}:")
        for slug in CANDIDATES.get(group, [group]):
            print(f"  {'OK  ' if probe(slug) else 'MISS'} {slug}")


if __name__ == "__main__":
    main()
