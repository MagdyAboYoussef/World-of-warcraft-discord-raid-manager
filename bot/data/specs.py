"""Static game data for WoW: Midnight (12.0) - 13 classes, 40 specializations.

Roles are the four the roster is grouped by: TANK, HEALER, MELEE, RANGED.
`icon` is the Wowhead/zamimg icon slug; tools/fetch_icons.py resolves it to
https://wow.zamimg.com/images/wow/icons/large/<icon>.jpg
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    TANK = "tank"
    HEALER = "healer"
    MELEE = "melee"
    RANGED = "ranged"

    @property
    def label(self) -> str:
        return {
            Role.TANK: "Tanks",
            Role.HEALER: "Healers",
            Role.MELEE: "Melee DPS",
            Role.RANGED: "Ranged DPS",
        }[self]

    @property
    def icon(self) -> str:
        return {
            Role.TANK: "ability_defend",
            Role.HEALER: "spell_holy_flashheal",
            Role.MELEE: "ability_dualwield",
            Role.RANGED: "ability_hunter_focusedaim",
        }[self]


# Role display order everywhere in the bot.
ROLE_ORDER: tuple[Role, ...] = (Role.TANK, Role.HEALER, Role.MELEE, Role.RANGED)

#: Roles whose icon ships with the bot instead of being downloaded, as PNGs
#: under assets/role_icons/. Vendored deliberately: these came from cache URLs
#: that can stop resolving at any time, and an icon set that breaks when a
#: third party reorganises its CDN is not worth the bytes it saves. Melee keeps
#: the Wowhead icon and is still downloaded.
VENDORED_ROLE_ICONS: tuple[Role, ...] = (Role.TANK, Role.HEALER, Role.RANGED)

#: Where each vendored icon originally came from. Kept for provenance only -
#: nothing reads these at runtime or at fetch time.
ROLE_ICON_SOURCE_URLS: dict[Role, str] = {
    Role.TANK: (
        "https://ih1.redbubble.net/image.3875681670.0301/"
        "raf,360x360,075,t,fafafa:ca443f4786.jpg"
    ),
    Role.HEALER: (
        "https://encrypted-tbn0.gstatic.com/images"
        "?q=tbn:ANd9GcS1jul9lXx4Jiw7oImZ51yV8t6ioLxNxRH2Z1sT5ODAUg&s=10"
    ),
    Role.RANGED: (
        "https://encrypted-tbn0.gstatic.com/images"
        "?q=tbn:ANd9GcTHjDZ0r7QCo6juu40da47rpO_sgAByS6ZrJRPJZ6_hZenVJ_TpIRnijjEt&s=10"
    ),
}


@dataclass(frozen=True, slots=True)
class Spec:
    key: str  # stable id, e.g. "paladin_holy"
    wow_class: str  # "Paladin"
    name: str  # "Holy"
    role: Role
    icon: str  # zamimg icon slug

    @property
    def full_name(self) -> str:
        return f"{self.name} {self.wow_class}"

    @property
    def color(self) -> int:
        return CLASS_COLORS[self.wow_class]

    @property
    def emoji_name(self) -> str:
        """Discord application-emoji names allow only [A-Za-z0-9_]."""
        return self.key


CLASS_COLORS: dict[str, int] = {
    "Death Knight": 0xC41E3A,
    "Demon Hunter": 0xA330C9,
    "Druid": 0xFF7C0A,
    "Evoker": 0x33937F,
    "Hunter": 0xAAD372,
    "Mage": 0x3FC7EB,
    "Monk": 0x00FF98,
    "Paladin": 0xF48CBA,
    "Priest": 0xFFFFFF,
    "Rogue": 0xFFF468,
    "Shaman": 0x0070DD,
    "Warlock": 0x8788EE,
    "Warrior": 0xC69B6D,
}

CLASS_ICONS: dict[str, str] = {
    "Death Knight": "classicon_deathknight",
    "Demon Hunter": "classicon_demonhunter",
    "Druid": "classicon_druid",
    "Evoker": "classicon_evoker",
    "Hunter": "classicon_hunter",
    "Mage": "classicon_mage",
    "Monk": "classicon_monk",
    "Paladin": "classicon_paladin",
    "Priest": "classicon_priest",
    "Rogue": "classicon_rogue",
    "Shaman": "classicon_shaman",
    "Warlock": "classicon_warlock",
    "Warrior": "classicon_warrior",
}


# fmt: off
SPECS: tuple[Spec, ...] = (
    # --- Death Knight ---
    Spec("dk_blood",        "Death Knight", "Blood",        Role.TANK,   "spell_deathknight_bloodpresence"),
    Spec("dk_frost",        "Death Knight", "Frost",        Role.MELEE,  "spell_deathknight_frostpresence"),
    Spec("dk_unholy",       "Death Knight", "Unholy",       Role.MELEE,  "spell_deathknight_unholypresence"),
    # --- Demon Hunter --- (Devourer is the new Midnight void caster spec)
    Spec("dh_havoc",        "Demon Hunter", "Havoc",        Role.MELEE,  "ability_demonhunter_specdps"),
    Spec("dh_vengeance",    "Demon Hunter", "Vengeance",    Role.TANK,   "ability_demonhunter_spectank"),
    Spec("dh_devourer",     "Demon Hunter", "Devourer",     Role.RANGED, "inv_112_ability_demonhunter_metamorphasisvoid"),
    # --- Druid ---
    Spec("druid_balance",   "Druid",        "Balance",      Role.RANGED, "spell_nature_starfall"),
    Spec("druid_feral",     "Druid",        "Feral",        Role.MELEE,  "ability_druid_catform"),
    Spec("druid_guardian",  "Druid",        "Guardian",     Role.TANK,   "ability_racial_bearform"),
    Spec("druid_resto",     "Druid",        "Restoration",  Role.HEALER, "spell_nature_healingtouch"),
    # --- Evoker ---
    Spec("evoker_dev",      "Evoker",       "Devastation",  Role.RANGED, "classicon_evoker_devastation"),
    Spec("evoker_pres",     "Evoker",       "Preservation", Role.HEALER, "classicon_evoker_preservation"),
    Spec("evoker_aug",      "Evoker",       "Augmentation", Role.RANGED, "classicon_evoker_augmentation"),
    # --- Hunter ---
    Spec("hunter_bm",       "Hunter",       "Beast Mastery", Role.RANGED, "ability_hunter_bestialdiscipline"),
    Spec("hunter_mm",       "Hunter",       "Marksmanship", Role.RANGED, "ability_hunter_focusedaim"),
    Spec("hunter_surv",     "Hunter",       "Survival",     Role.MELEE,  "ability_hunter_camouflage"),
    # --- Mage ---
    Spec("mage_arcane",     "Mage",         "Arcane",       Role.RANGED, "spell_holy_magicalsentry"),
    Spec("mage_fire",       "Mage",         "Fire",         Role.RANGED, "spell_fire_firebolt02"),
    Spec("mage_frost",      "Mage",         "Frost",        Role.RANGED, "spell_frost_frostbolt02"),
    # --- Monk ---
    Spec("monk_brew",       "Monk",         "Brewmaster",   Role.TANK,   "spell_monk_brewmaster_spec"),
    Spec("monk_mw",         "Monk",         "Mistweaver",   Role.HEALER, "spell_monk_mistweaver_spec"),
    Spec("monk_ww",         "Monk",         "Windwalker",   Role.MELEE,  "spell_monk_windwalker_spec"),
    # --- Paladin ---
    Spec("pal_holy",        "Paladin",      "Holy",         Role.HEALER, "spell_holy_holybolt"),
    Spec("pal_prot",        "Paladin",      "Protection",   Role.TANK,   "ability_paladin_shieldofthetemplar"),
    Spec("pal_ret",         "Paladin",      "Retribution",  Role.MELEE,  "spell_holy_auraoflight"),
    # --- Priest ---
    Spec("priest_disc",     "Priest",       "Discipline",   Role.HEALER, "spell_holy_powerwordshield"),
    Spec("priest_holy",     "Priest",       "Holy",         Role.HEALER, "spell_holy_guardianspirit"),
    Spec("priest_shadow",   "Priest",       "Shadow",       Role.RANGED, "spell_shadow_shadowwordpain"),
    # --- Rogue ---
    Spec("rogue_assa",      "Rogue",        "Assassination", Role.MELEE, "ability_rogue_deadlybrew"),
    Spec("rogue_outlaw",    "Rogue",        "Outlaw",       Role.MELEE,  "ability_rogue_waylay"),
    Spec("rogue_sub",       "Rogue",        "Subtlety",     Role.MELEE,  "ability_stealth"),
    # --- Shaman ---
    Spec("sham_ele",        "Shaman",       "Elemental",    Role.RANGED, "spell_nature_lightning"),
    Spec("sham_enh",        "Shaman",       "Enhancement",  Role.MELEE,  "spell_shaman_improvedstormstrike"),
    Spec("sham_resto",      "Shaman",       "Restoration",  Role.HEALER, "spell_nature_magicimmunity"),
    # --- Warlock ---
    Spec("lock_affli",      "Warlock",      "Affliction",   Role.RANGED, "spell_shadow_deathcoil"),
    Spec("lock_demo",       "Warlock",      "Demonology",   Role.RANGED, "spell_shadow_metamorphosis"),
    Spec("lock_destro",     "Warlock",      "Destruction",  Role.RANGED, "spell_shadow_rainoffire"),
    # --- Warrior ---
    Spec("warr_arms",       "Warrior",      "Arms",         Role.MELEE,  "ability_warrior_savageblow"),
    Spec("warr_fury",       "Warrior",      "Fury",         Role.MELEE,  "ability_warrior_innerrage"),
    Spec("warr_prot",       "Warrior",      "Protection",   Role.TANK,   "ability_warrior_defensivestance"),
)
# fmt: on

SPECS_BY_KEY: dict[str, Spec] = {s.key: s for s in SPECS}

CLASSES: tuple[str, ...] = tuple(dict.fromkeys(s.wow_class for s in SPECS))


def specs_for_class(wow_class: str) -> list[Spec]:
    return [s for s in SPECS if s.wow_class == wow_class]


def specs_for_role(role: Role) -> list[Spec]:
    return [s for s in SPECS if s.role is role]


def get_spec(key: str) -> Spec | None:
    return SPECS_BY_KEY.get(key)
