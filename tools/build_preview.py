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
from bot.data.specs import (  # noqa: E402
    CLASS_COLORS, CLASS_ICONS, ROLE_ORDER, SPECS, get_spec,
)
from bot.web.page import SCRIPT, STYLE  # noqa: E402

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
            {"value": "declined", "label": "Declined", "emoji": "❌"},
            {"value": "bench", "label": "Benched", "emoji": "🪑"},
            {"value": "absent", "label": "Absent", "emoji": "🚫"},
        ],
        "specs": [
            {"key": s.key, "label": s.full_name, "icon": s.icon,
             "wow_class": s.wow_class, "role": s.role.value}
            for s in SPECS
        ],
        "viewer_id": "1",
        "expires_at": 0,
    }


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
}

// The real client script is used verbatim; only the transport is faked.
window.fetch = async (url, options) => {
  const path = String(url);
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
        .replace("__ICONS__", json.dumps(icon_map(slugs)))
    )
    return f"""<title>Raid roster manager — preview</title>
<style>{STYLE}</style>
<div class="wrap">
{BANNER}
  <header class="top">
    <div>
      <h1 id="title">Manaforge Omega — Mythic</h1>
      <div class="meta" id="meta"></div>
    </div>
    <div class="meta" id="counts"></div>
  </header>

  <div class="panel">
    <h2>Final roster — <span class="n" id="roster-count"></span> accepted</h2>
    <div class="roster" id="roster"></div>
  </div>

  <div class="panel">
    <h2>Raid buffs — accepted roster</h2>
    <div class="chips" id="buffs"></div>
  </div>

  <div class="toolbar">
    <button id="f-all" class="on">All signups</button>
    <button id="f-pending">Pending only</button>
    <span class="grow"></span>
    <span class="hint">
      Focus a card, then <kbd>A</kbd>ccept <kbd>D</kbd>ecline <kbd>B</kbd>ench
      <kbd>N</kbd> absent <kbd>P</kbd>ending <kbd>X</kbd> remove · <kbd>↑</kbd><kbd>↓</kbd> to move
    </span>
  </div>

  <div class="hint" id="orphans"></div>
  <div class="board" id="board"></div>
</div>
<div id="toast"></div>
<script>{mock}</script>
<script>{SCRIPT}</script>
"""


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "preview/roster-preview.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
