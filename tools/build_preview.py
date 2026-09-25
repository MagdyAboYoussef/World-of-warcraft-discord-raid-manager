"""Generate a standalone, interactive preview of the roster manager page.

    python -m tools.build_preview [outfile]

Uses the real stylesheet and the real client script, stubbing only `fetch`, so
the preview cannot drift from what the bot actually serves. Buff coverage is
resolved by evaluating the real predicates in bot.data.buffs against every spec
and shipping the resulting table, rather than reimplementing the rules here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.data import targets as targets_data  # noqa: E402
from bot.data.buffs import BUFFS  # noqa: E402
from bot.data.specs import CLASSES, specs_for_class  # noqa: E402
from bot.data.specs import (  # noqa: E402
    CLASS_COLORS, CLASS_ICONS, ROLE_ORDER, SPECS, get_spec,
)
from bot.web.page import BODY, SCRIPT, STYLE  # noqa: E402

# character, spec, status, note, has_logs
ROSTER = [
    ("Thornhoof",   "pal_prot",     "accepted", None, True),
    ("Grimbark",    "druid_guardian", "pending", "Can flex to Resto if you need a healer.", True),
    ("Ironveil",    "dk_blood",     "pending",  None, False),
    ("Sunwell",     "pal_holy",     "accepted", None, True),
    ("Mistcaller",  "monk_mw",      "accepted", "Late by ~10min, coming from work.", False),
    ("Voidmend",    "priest_disc",  "pending",  None, True),
    ("Tidecaller",  "sham_resto",   "pending",  "First time with the guild — logs from last tier.", True),
    ("Blossomrot",  "druid_resto",  "bench",    None, False),
    ("Shadowstep",  "rogue_sub",    "accepted", None, True),
    ("Ragebound",   "warr_fury",    "accepted", None, True),
    ("Duskblade",   "dh_havoc",     "pending",  "Have a Vengeance offspec at the same ilvl.", True),
    ("Stormfist",   "sham_enh",     "pending",  None, False),
    ("Palefang",    "dk_unholy",    "declined", None, False),
    ("Windwhisper", "monk_ww",      "bench",    None, True),
    ("Emberlash",   "pal_ret",      "pending",  None, False),
    ("Frostquill",  "mage_frost",   "accepted", None, True),
    ("Nightbloom",  "lock_affli",   "accepted", "Bringing the summon stone, ping me early.", True),
    ("Starweaver",  "druid_balance", "pending", None, True),
    ("Dawnpiercer", "hunter_mm",    "pending",  None, False),
    ("Voidgaze",    "priest_shadow", "accepted", None, True),
    ("Emberdrake",  "evoker_aug",   "pending",  "Aug or Dev, whichever the comp needs.", True),
    ("Thundercall", "sham_ele",     "absent",   "Out this week — holiday.", False),
    ("Duskwarden",  "dk_blood",     "tentative", "Might be 20 min late, kids' bedtime.", True),
    ("Riverchant",  "sham_resto",   "tentative", None, False),
]

BASE_ID = 400000000000000000  # realistic snowflake width, so nothing looks fake


def build_state() -> dict:
    signups = []
    for index, (name, spec_key, status, note, has_logs) in enumerate(ROSTER):
        spec = get_spec(spec_key)
        assert spec is not None, spec_key
        signups.append({
            "user_id": str(BASE_ID + index),
            "character": name,
            "spec_key": spec_key,
            "spec_label": spec.full_name,
            "icon": spec.icon,
            "wow_class": spec.wow_class,
            "color": f"#{CLASS_COLORS[spec.wow_class]:06X}",
            "role": spec.role.value,
            # One row deliberately left without a handle, to show the fallback
            # for an account that no longer resolves.
            "discord_name": None if name == "Palefang" else name.lower(),
            "status": status,
            "note": note,
            "logs_url": (
                f"https://www.warcraftlogs.com/character/eu/kazzak/{name.lower()}"
                if has_logs else None
            ),
            "updated_at": 1770000000 + index * 60,
        })

    # Combined-DPS mode, so the preview shows the 2 / 4 / 14 case: melee and
    # ranged stay separate in the roster but share one target.
    caps = {"tank": 2, "healer": 4, "dps": 14}
    return {
        "raid": {
            "id": 17,
            "title": "Manaforge Omega — Mythic",
            "description": None,
            "state": "open",
            "editable": True,
            "starts_at": None,  # filled in by the preview script, relative to now
            "duration_minutes": 180,
            "region": "EU",
            "expires_at": 0,
        },
        # Built through the real helpers so the preview can't disagree with the
        # server about what a combined target looks like.
        "roles": [
            {
                "key": r.value,
                "label": r.label,
                "cap": targets_data.role_cap(caps, r),
                "accepted": 0,
            }
            for r in ROLE_ORDER
        ],
        "targets": [
            {
                "key": t.key,
                "label": t.label,
                "cap": t.cap,
                "accepted": 0,
                "roles": [r.value for r in t.roles],
            }
            for t in targets_data.targets(caps)
        ],
        "combined_dps": targets_data.is_combined(caps),
        "raid_size": targets_data.raid_size(caps),
        "signups": signups,
        "buffs": [],
        "statuses": [
            {"value": "pending", "label": "Pending", "emoji": "🕓"},
            {"value": "accepted", "label": "Accepted", "emoji": "✅"},
            {"value": "declined", "label": "Out", "emoji": "❌"},
            {"value": "bench", "label": "Backup", "emoji": "⭐"},
            {"value": "absent", "label": "Absent", "emoji": "🚫"},
            {"value": "tentative", "label": "Tentative", "emoji": "❔"},
        ],
        "specs": [
            {"key": s.key, "label": s.full_name, "icon": s.icon,
             "wow_class": s.wow_class, "role": s.role.value}
            for s in SPECS
        ],
        "audit": AUDIT,
        "viewer_id": "1",
        "expires_at": 0,
    }


#: A plausible few minutes of raid-lead work, newest first.
AUDIT = [
    {"id": 9, "at": 1770000900, "actor": "mimz", "actor_id": "1",
     "action": "remove", "target": "palefang", "target_id": str(BASE_ID + 12),
     "detail": "Palefang — was Declined", "source": "web"},
    {"id": 8, "at": 1770000840, "actor": "mimz", "actor_id": "1",
     "action": "status", "target": "voidgaze", "target_id": str(BASE_ID + 19),
     "detail": "Voidgaze — Pending -> Accepted", "source": "web"},
    {"id": 7, "at": 1770000780, "actor": "officer_kez", "actor_id": "2",
     "action": "spec", "target": "emberdrake", "target_id": str(BASE_ID + 20),
     "detail": "Emberdrake — Devastation Evoker -> Augmentation Evoker",
     "source": "discord"},
    {"id": 6, "at": 1770000720, "actor": "officer_kez", "actor_id": "2",
     "action": "status", "target": "blossomrot", "target_id": str(BASE_ID + 7),
     "detail": "Blossomrot — Accepted -> Backup", "source": "discord"},
    {"id": 5, "at": 1770000660, "actor": "duskwarden", "actor_id": str(BASE_ID + 22),
     "action": "apply", "target": "duskwarden", "target_id": str(BASE_ID + 22),
     "detail": "Duskwarden — Blood Death Knight — Tentative", "source": "discord"},
    {"id": 4, "at": 1770000600, "actor": "mimz", "actor_id": "1",
     "action": "raid", "target": None, "target_id": None,
     "detail": "auto-accept turned off", "source": "discord"},
    {"id": 3, "at": 1770000540, "actor": "thundercall", "actor_id": str(BASE_ID + 21),
     "action": "withdraw", "target": "thundercall", "target_id": str(BASE_ID + 21),
     "detail": "Thundercall", "source": "discord"},
]


def buff_table() -> list[dict]:
    """Each buff plus the spec keys that provide it, straight from the predicates."""
    table = []
    for definition in BUFFS:
        # Same rule the server applies: the class icon where one class owns the
        # buff, the spell icon where several can cover it.
        icon = CLASS_ICONS[definition.wow_class] if definition.wow_class else definition.icon
        entry = {
            "key": definition.key,
            "label": definition.label,
            "icon": icon,
            "wow_class": definition.wow_class,
            "specs": [s.key for s in SPECS if definition.provided_by(s)],
        }
        if definition.upgrade is not None:
            entry["up_label"] = definition.upgrade.label
            entry["up_icon"] = icon if definition.wow_class else definition.upgrade.icon
            entry["up_specs"] = [s.key for s in SPECS if definition.upgrade.provided_by(s)]
        table.append(entry)
    return table


def needs_table() -> dict:
    """{buff key: [every class that could cover it]}, straight from the predicates.

    The preview recomputes coverage as you click, so it needs to recompute the
    recruit panel too - but the *rules* stay here in Python. The mock only
    assembles this table against whichever buffs are currently missing, so it
    cannot drift from what the server would have said.
    """
    out: dict[str, list[dict]] = {}
    for definition in BUFFS:
        entries = []
        for wow_class in CLASSES:
            class_specs = specs_for_class(wow_class)
            providers = [s for s in class_specs if definition.provided_by(s)]
            if not providers:
                continue
            entries.append({
                "wow_class": wow_class,
                "icon": CLASS_ICONS[wow_class],
                "color": f"#{CLASS_COLORS[wow_class]:06X}",
                "label": definition.label,
                "specs": (
                    [s.name for s in providers]
                    if len(providers) != len(class_specs) else []
                ),
            })
        out[definition.key] = entries
    return out


ICON_CDN = "https://wow.zamimg.com/images/wow/icons/large/{slug}.jpg"
ICON_CACHE = Path(__file__).resolve().parents[1] / "preview" / ".icons"
ICON_PX = 36  # rendered at 17-28px; 36 covers 2x displays without bloating the page


def icon_map(slugs: set[str]) -> dict[str, str]:
    """Download each icon once and return {slug: data URI}.

    The preview is viewed under a CSP that blocks third-party images, so the
    icons have to travel inside the file. Cached on disk because this otherwise
    re-downloads ~55 files on every rebuild.
    """
    import base64
    import io

    import requests
    from PIL import Image

    ICON_CACHE.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    fetched = 0
    for slug in sorted(slugs):
        cached = ICON_CACHE / f"{slug}.jpg"
        if not cached.exists():
            try:
                response = requests.get(ICON_CDN.format(slug=slug), timeout=15)
                response.raise_for_status()
            except Exception as exc:  # a bad slug must not fail the whole build
                print(f"  ! {slug}: {exc}")
                continue
            image = Image.open(io.BytesIO(response.content)).convert("RGB")
            image = image.resize((ICON_PX, ICON_PX), Image.LANCZOS)
            image.save(cached, "JPEG", quality=82, optimize=True)
            fetched += 1
        encoded = base64.b64encode(cached.read_bytes()).decode()
        out[slug] = f"data:image/jpeg;base64,{encoded}"
    print(f"  icons: {len(out)} embedded ({fetched} newly downloaded)")
    return out


MOCK = """
const STATE = __STATE__;
const BUFFS = __BUFFS__;
const NEEDS = __NEEDS__;
window.ICON_MAP = __ICONS__;

// Anchor the demo raid to this evening so the header reads like a real one.
const soon = new Date();
soon.setHours(20, 30, 0, 0);
if (soon < new Date()) soon.setDate(soon.getDate() + 1);
STATE.raid.starts_at = Math.floor(soon.getTime() / 1000);
STATE.expires_at = Math.floor(Date.now() / 1000) + 3 * 3600;
STATE.raid.expires_at = STATE.expires_at + 30 * 86400;

function recompute() {
  const accepted = STATE.signups.filter((s) => s.status === 'accepted');
  for (const role of STATE.roles) {
    role.accepted = accepted.filter((s) => s.role === role.key).length;
  }
  for (const target of STATE.targets) {
    target.accepted = accepted.filter((s) => target.roles.includes(s.role)).length;
  }
  const keys = accepted.map((s) => s.spec_key);
  STATE.buffs = BUFFS.map((buff) => {
    const base = { key: buff.key, wow_class: buff.wow_class };
    if (buff.up_specs) {
      const up = keys.filter((k) => buff.up_specs.includes(k)).length;
      if (up) {
        return Object.assign(base, {
          label: buff.up_label, icon: buff.up_icon, count: up, covered: true,
        });
      }
    }
    const count = keys.filter((k) => buff.specs.includes(k)).length;
    return Object.assign(base, {
      label: buff.label, icon: buff.icon, count, covered: count > 0,
    });
  });
  recomputeNeeds();
}

// Mirrors bot.data.buffs.recruits: group the classes that cover each missing
// buff, best first, ties alphabetical.
function recomputeNeeds() {
  const byClass = {};
  for (const buff of STATE.buffs) {
    if (buff.covered) continue;
    for (const entry of (NEEDS[buff.key] || [])) {
      const row = byClass[entry.wow_class] || (byClass[entry.wow_class] = {
        wow_class: entry.wow_class, icon: entry.icon, color: entry.color,
        count: 0, covers: [],
      });
      row.covers.push({ label: entry.label, specs: entry.specs });
      row.count = row.covers.length;
    }
  }
  STATE.recruit = Object.values(byClass).sort(
    (a, b) => (b.count - a.count) || a.wow_class.localeCompare(b.wow_class));
}

// The real client script is used verbatim; only the transport is faked.
// A stand-in member directory so the reassign picker works in the preview too
// (the live page searches the real server). Ids are fake.
const PREVIEW_MEMBERS = [
  { id: '900000000000000001', name: 'freshmeat', display: 'Fresh Meat' },
  { id: '900000000000000002', name: 'benchwarmer2', display: 'Benchwarmer' },
  { id: '900000000000000003', name: 'subhealer', display: 'Sub Healer' },
  { id: '900000000000000004', name: 'randomdps', display: 'Random DPS' },
  { id: '900000000000000005', name: 'lastminute', display: 'Last Minute' },
];

window.fetch = async (url, options) => {
  const path = String(url);
  // GET /members?q= — type-ahead for the reassign picker
  const mm = path.match(/\/members\?q=([^&]*)/);
  if (mm) {
    const q = decodeURIComponent(mm[1]).toLowerCase();
    const members = q.length >= 2
      ? PREVIEW_MEMBERS.filter((m) => m.name.includes(q) || m.display.toLowerCase().includes(q))
      : [];
    return { ok: true, status: 200, json: async () => ({ members }) };
  }
  if (options && options.body) {
    const body = JSON.parse(options.body);
    const signup = STATE.signups.find((s) => s.user_id === body.user_id);
    if (signup) {
      if (path.endsWith('/status')) {
        signup.status = body.status;
      } else if (path.endsWith('/spec')) {
        const spec = STATE.specs.find((s) => s.key === body.spec_key);
        if (spec) {
          Object.assign(signup, {
            spec_key: spec.key, spec_label: spec.label,
            icon: spec.icon, wow_class: spec.wow_class, role: spec.role,
          });
        }
      } else if (path.endsWith('/remove')) {
        STATE.signups = STATE.signups.filter((s) => s !== signup);
      } else if (path.endsWith('/character')) {
        signup.character = body.character;
      } else if (path.endsWith('/note')) {
        signup.note = body.note || null;
      } else if (path.endsWith('/reassign')) {
        const m = PREVIEW_MEMBERS.find((x) => x.id === body.new_user_id);
        signup.user_id = body.new_user_id;
        if (m) signup.discord_name = m.name;
      }
    }
  }
  recompute();
  return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(STATE)) };
};

recompute();
"""

BANNER = """
  <div class="warn">
    <strong>PREVIEW.</strong> Sample roster, running the bot's real stylesheet and
    real client code with the network stubbed out — every button, keyboard
    shortcut and buff recount works. The deployed page shows a
    <strong>DO NOT SHARE THIS LINK</strong> warning here instead.
  </div>
"""


def build() -> str:
    state = build_state()
    buffs = buff_table()

    slugs = {s.icon for s in SPECS} | set(CLASS_ICONS.values())
    for entry in buffs:
        slugs.add(entry["icon"])
        if entry.get("up_icon"):
            slugs.add(entry["up_icon"])

    mock = (
        MOCK.replace("__STATE__", json.dumps(state))
        .replace("__BUFFS__", json.dumps(buffs))
        .replace("__NEEDS__", json.dumps(needs_table()))
        .replace("__ICONS__", json.dumps(icon_map(slugs)))
    )
    # The real body, not a copy of it: everything the client script reaches for
    # is guaranteed to be here because the deployed page uses the same template.
    body = BODY.format(banner=BANNER, title="Manaforge Omega — Mythic")
    return (
        "<title>Raid roster manager — preview</title>\n"
        f"<style>{STYLE}</style>\n"
        f"{body}\n"
        f"<script>{mock}</script>\n"
        f"<script>{SCRIPT}</script>\n"
    )


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "preview/roster-preview.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
