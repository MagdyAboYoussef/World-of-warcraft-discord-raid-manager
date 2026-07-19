"""Raid buff / debuff / utility coverage model.

Every entry counts how many *accepted* roster members can provide it, so the
panel reads like the in-game NSRT addon:

    Battle Shout (2)          <- covered, 2 providers
    Missing Devotion Aura     <- nobody brings it

Some entries have an `upgrade`: a strictly better version provided by a narrower
set of specs. When at least one upgrade provider is on the roster the entry
renders as the upgrade instead. This is how Grip works - any Death Knight brings
single-target Grip, but a Blood DK brings Mass Grip, so the panel collapses to
`Mass Grip (1)`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum

from .specs import Spec

Predicate = Callable[[Spec], bool]


class Category(str, Enum):
    BUFF = "Raid Buffs"
    DEBUFF = "Debuffs"
    UTILITY = "Utility"


def by_class(*classes: str) -> Predicate:
    allowed = frozenset(classes)
    return lambda s: s.wow_class in allowed


def by_spec(*keys: str) -> Predicate:
    allowed = frozenset(keys)
    return lambda s: s.key in allowed


@dataclass(frozen=True, slots=True)
class Upgrade:
    label: str
    icon: str
    provided_by: Predicate


@dataclass(frozen=True, slots=True)
class BuffDef:
    key: str
    label: str
    icon: str
    category: Category
    provided_by: Predicate
    #: Shown in the "missing" line instead of `label` when it reads better.
    short: str | None = None
    upgrade: Upgrade | None = None
    #: The single class that brings this, if there is one. Drives the class
    #: icon on the missing line - "who do we still need?" is the whole question
    #: that panel answers. None for anything several classes can cover.
    wow_class: str | None = None

    @property
    def missing_label(self) -> str:
        return self.short or self.label


@dataclass(frozen=True, slots=True)
class BuffStatus:
    """Resolved coverage for one BuffDef against a concrete roster."""

    definition: BuffDef
    label: str
    icon: str
    count: int
    upgraded: bool = False

    @property
    def covered(self) -> bool:
        return self.count > 0

    @property
    def emoji_name(self) -> str:
        return f"{self.definition.key}_up" if self.upgraded else self.definition.key


# fmt: off
BUFFS: tuple[BuffDef, ...] = (
    # ---------------- Throughput raid buffs (one per class) ----------------
    BuffDef("battle_shout", "Battle Shout", "ability_warrior_battleshout",
            Category.BUFF, by_class("Warrior"), wow_class="Warrior"),
    BuffDef("arcane_intellect", "Arcane Intellect", "spell_holy_magicalsentry",
            Category.BUFF, by_class("Mage"), short="Intellect", wow_class="Mage"),
    BuffDef("fortitude", "Power Word: Fortitude", "spell_holy_wordfortitude",
            Category.BUFF, by_class("Priest"), short="Fortitude", wow_class="Priest"),
    BuffDef("mark_of_the_wild", "Mark of the Wild", "spell_nature_regeneration",
            Category.BUFF, by_class("Druid"), wow_class="Druid"),
    BuffDef("skyfury", "Skyfury", "inv_10_elementalshardfoozles_air",
            Category.BUFF, by_class("Shaman"), wow_class="Shaman"),
    BuffDef("bronze", "Blessing of the Bronze", "ability_evoker_blessingofthebronze",
            Category.BUFF, by_class("Evoker"), short="Bronze", wow_class="Evoker"),
    BuffDef("devotion_aura", "Devotion Aura", "spell_holy_devotionaura",
            Category.BUFF, by_class("Paladin"), short="Devotion", wow_class="Paladin"),

    # ---------------- Raid-wide debuffs ----------------
    BuffDef("magic_debuff", "Chaos Brand", "ability_demonhunter_empowerwards",
            Category.DEBUFF, by_class("Demon Hunter"), short="Magic Debuff",
            wow_class="Demon Hunter"),
    BuffDef("phys_debuff", "Mystic Touch", "ability_monk_sparring",
            Category.DEBUFF, by_class("Monk"), short="Phys Debuff", wow_class="Monk"),
    BuffDef("hunters_mark", "Hunter's Mark", "ability_hunter_markedfordeath",
            Category.DEBUFF, by_class("Hunter"), wow_class="Hunter"),
    BuffDef("rogue_poison", "Atrophic Poison", "ability_rogue_deadlybrew",
            Category.DEBUFF, by_class("Rogue"), short="Rogue Poison", wow_class="Rogue"),

    # ---------------- Utility ----------------
    BuffDef("lust", "Lust", "spell_nature_bloodlust", Category.UTILITY,
            # Shaman (Bloodlust/Heroism), Mage (Time Warp), Evoker (Fury of the
            # Aspects), BM Hunter (Primal Rage, pet-provided).
            lambda s: s.wow_class in {"Shaman", "Mage", "Evoker"} or s.key == "hunter_bm"),
    BuffDef("battle_res", "Combat Res", "spell_nature_reincarnation", Category.UTILITY,
            by_class("Druid", "Death Knight", "Warlock", "Paladin"), short="Combat Res"),
    BuffDef("grip", "Grip", "spell_deathknight_strangulate", Category.UTILITY,
            by_class("Death Knight"), wow_class="Death Knight",
            upgrade=Upgrade("Mass Grip", "ability_deathknight_aoedeathgrip",
                            by_spec("dk_blood"))),
    BuffDef("as_slow", "Attack Speed Slow", "spell_nature_thunderclap",
            Category.UTILITY,
            # Thunder Clap (Warrior), Frost Fever (DK), Infected Wounds (Feral).
            lambda s: s.wow_class in {"Warrior", "Death Knight"} or s.key == "druid_feral",
            short="AS Slow"),
    BuffDef("innervate", "Innervate", "spell_nature_lightning",
            Category.UTILITY, by_class("Druid"), wow_class="Druid"),
    BuffDef("source_of_magic", "Source of Magic", "ability_evoker_blue_01",
            Category.UTILITY, by_spec("evoker_pres", "evoker_aug"), wow_class="Evoker"),
    BuffDef("warlock", "Healthstones / Summon", "inv_stone_04",
            Category.UTILITY, by_class("Warlock"), short="Warlock", wow_class="Warlock"),
)
# fmt: on

BUFFS_BY_KEY: dict[str, BuffDef] = {b.key: b for b in BUFFS}


def evaluate(specs: Iterable[Spec]) -> list[BuffStatus]:
    """Resolve every BuffDef against the given roster specs, in declared order."""
    roster = list(specs)
    out: list[BuffStatus] = []
    for definition in BUFFS:
        if definition.upgrade is not None:
            upgraded = [s for s in roster if definition.upgrade.provided_by(s)]
            if upgraded:
                out.append(
                    BuffStatus(
                        definition,
                        label=definition.upgrade.label,
                        icon=definition.upgrade.icon,
                        count=len(upgraded),
                        upgraded=True,
                    )
                )
                continue
        count = sum(1 for s in roster if definition.provided_by(s))
        out.append(
            BuffStatus(definition, label=definition.label, icon=definition.icon, count=count)
        )
    return out


def missing(statuses: Iterable[BuffStatus]) -> list[BuffStatus]:
    return [s for s in statuses if not s.covered]


def covered(statuses: Iterable[BuffStatus]) -> list[BuffStatus]:
    return [s for s in statuses if s.covered]


def icon_jobs() -> list[tuple[str, str]]:
    """(emoji_name, cdn_slug) for every icon this module can render.

    Named by buff key rather than CDN slug so the asset filename and the Discord
    emoji name stay short, stable, and independent of which slug we resolved to.
    """
    jobs: list[tuple[str, str]] = []
    for b in BUFFS:
        jobs.append((b.key, b.icon))
        if b.upgrade is not None:
            jobs.append((f"{b.key}_up", b.upgrade.icon))
    return jobs
