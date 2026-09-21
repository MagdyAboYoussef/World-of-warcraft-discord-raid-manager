"""Cross-server overview: every raid and who signed up, in one read-only page.

Reachable at /overview/<key>. The key is a random string persisted under the
data directory (or OVERVIEW_KEY); the route does not exist while there is no
key, and a wrong one is answered with a plain 404.
"""

from __future__ import annotations

import hmac
import html
import logging
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from ..config import DB_PATH, region_label
from ..data.specs import CLASS_COLORS, get_spec
from ..store import Raid, RaidState, Status, raid_is_finished
from .page import _csp, _shell, STYLE  # noqa: F401  (STYLE re-used by _shell)

if TYPE_CHECKING:
    from ..client import RaidClient

log = logging.getLogger(__name__)

KEY_FILE: Path = DB_PATH.parent / ".overview_key"


def load_key() -> str | None:
    """OVERVIEW_KEY if set, else the persisted one, else None (route disabled)."""
    env = os.getenv("OVERVIEW_KEY", "").strip()
    if env:
        return env
    try:
        return KEY_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def create_key() -> str:
    """Generate and persist a key. Refuses to overwrite one that exists."""
    key = secrets.token_urlsafe(32)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(key + "\n")
    return key


def key_matches(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(supplied.encode(), expected.encode())


# ------------------------------------------------------------------ rendering

_STYLE = """
.ov { max-width: 1200px; margin: 0 auto; padding: 20px 20px 80px; }
.ov h1 { font-size: 22px; margin: 0 0 4px; }
.ov .sub { color: var(--muted); font-size: 13px; margin-bottom: 18px; }
.ov .tools { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 18px; }
.ov input[type=search] {
  flex: 1; min-width: 220px; font: inherit; padding: 7px 11px; border-radius: 7px;
  background: var(--panel-2); color: var(--text); border: 1px solid var(--line);
}
.ov .guild { margin-bottom: 26px; }
.ov .guild > h2 {
  font-size: 15px; margin: 0 0 8px; padding-bottom: 6px; border-bottom: 1px solid var(--line);
  display: flex; justify-content: space-between; align-items: baseline;
}
.ov .guild > h2 .n { color: var(--muted); font-weight: 500; font-size: 12.5px; }
.ov details { margin-bottom: 6px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
.ov summary {
  cursor: pointer; padding: 8px 12px; display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;
  list-style: none;
}
.ov summary::-webkit-details-marker { display: none; }
.ov summary .t { font-weight: 600; flex: 1; min-width: 200px; }
.ov summary .w { color: var(--muted); font-size: 12.5px; font-variant-numeric: tabular-nums; }
.ov summary .c { color: var(--muted); font-size: 12.5px; }
.ov details.past summary .t { color: var(--muted); }
.ov table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ov th { text-align: left; color: var(--muted); font-weight: 600; font-size: 11px;
  text-transform: uppercase; letter-spacing: .07em; padding: 6px 12px; }
.ov td { padding: 5px 12px; border-top: 1px solid var(--line); vertical-align: top; }
.ov td.h { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted); }
.ov td.s { white-space: nowrap; }
.ov tr.hide, .ov details.hide, .ov .guild.hide { display: none; }
.ov .pill { font-size: 10.5px; }
"""

_SCRIPT = """
const q = document.getElementById('q');
const scope = document.getElementById('scope');
function apply() {
  const needle = q.value.trim().toLowerCase();
  const which = scope.value;
  for (const guild of document.querySelectorAll('.guild')) {
    let guildVisible = 0;
    for (const raid of guild.querySelectorAll('details')) {
      const past = raid.classList.contains('past');
      const scoped = which === 'all' || (which === 'upcoming' ? !past : past);
      let rows = 0;
      for (const tr of raid.querySelectorAll('tbody tr')) {
        const hit = !needle || tr.dataset.k.includes(needle);
        tr.classList.toggle('hide', !hit);
        if (hit) rows++;
      }
      const show = scoped && (needle ? rows > 0 : true);
      raid.classList.toggle('hide', !show);
      if (show && needle) raid.open = true;
      if (show) guildVisible++;
    }
    guild.classList.toggle('hide', guildVisible === 0);
  }
}
q.addEventListener('input', apply);
scope.addEventListener('change', apply);
apply();
"""


def _when(raid: Raid) -> str:
    if raid.starts_at is None:
        return "no time set"
    local = datetime.fromtimestamp(raid.starts_at, tz=timezone.utc)
    return local.strftime("%a %d %b %H:%M") + f" UTC · {region_label(raid.timezone)}"


def _state_pill(raid: Raid) -> str:
    if raid.state is RaidState.CANCELLED:
        return '<span class="pill cancelled">cancelled</span>'
    if raid_is_finished(raid):
        return '<span class="pill">finished</span>'
    return f'<span class="pill {raid.state.value}">{raid.state.value}</span>'


def render_overview(bot: "RaidClient") -> web.Response:
    store = bot.store
    raids = store.all_raids()
    by_guild: dict[int, list[Raid]] = defaultdict(list)
    for raid in raids:
        by_guild[raid.guild_id].append(raid)

    total_signups = 0
    people: set[int] = set()
    sections: list[str] = []

    for guild_id, guild_raids in by_guild.items():
        guild = bot.get_guild(guild_id)
        name = html.escape(guild.name) if guild is not None else f"server {guild_id} (bot not present)"
        blocks: list[str] = []
        for raid in guild_raids:
            signups = store.signups(raid.id)
            total_signups += len(signups)
            people.update(s.user_id for s in signups)
            past = raid.state is RaidState.CANCELLED or raid_is_finished(raid)

            rows: list[str] = []
            for s in sorted(signups, key=lambda x: (x.status.value != "accepted", x.character_name.lower())):
                spec = get_spec(s.spec_key)
                colour = f"#{CLASS_COLORS[spec.wow_class]:06X}" if spec else "inherit"
                handle = f"@{html.escape(s.discord_name)}" if s.discord_name else f"id {s.user_id}"
                key = " ".join(
                    filter(None, [s.character_name, s.discord_name or "", str(s.user_id),
                                  spec.full_name if spec else "", s.status.value])
                ).lower()
                rows.append(
                    f'<tr data-k="{html.escape(key, quote=True)}">'
                    f'<td style="color:{colour};font-weight:600">{html.escape(s.character_name)}</td>'
                    f'<td class="h">{handle}</td>'
                    f'<td>{html.escape(spec.full_name) if spec else html.escape(s.spec_key)}</td>'
                    f'<td class="s">{s.status.emoji} {s.status.label}</td>'
                    f'<td class="s">{datetime.fromtimestamp(s.updated_at, tz=timezone.utc):%d %b %H:%M}</td>'
                    f"</tr>"
                )
            accepted = sum(1 for s in signups if s.status is Status.ACCEPTED)
            table = (
                "<table><thead><tr><th>Character</th><th>Discord</th><th>Spec</th>"
                "<th>Status</th><th>Applied</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table>"
                if rows else '<div class="empty" style="padding:8px 12px">nobody signed up</div>'
            )
            blocks.append(
                f'<details class="{"past" if past else ""}">'
                f"<summary>{_state_pill(raid)}"
                f'<span class="t">#{raid.id} {html.escape(raid.title)}</span>'
                f'<span class="w">{_when(raid)}</span>'
                f'<span class="c">{accepted} accepted / {len(signups)} signed up</span>'
                f"</summary>{table}</details>"
            )
        sections.append(
            f'<section class="guild"><h2><span>{name}</span>'
            f'<span class="n">{len(guild_raids)} raids</span></h2>{"".join(blocks)}</section>'
        )

    generated = datetime.now(tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")
    nonce = secrets.token_urlsafe(16)
    body = f"""
<div class="ov">
  <h1>Raid overview</h1>
  <div class="sub">{len(by_guild)} servers · {len(raids)} raids · {total_signups} signups ·
    {len(people)} distinct people · generated {generated}</div>
  <div class="tools">
    <input type="search" id="q" placeholder="Search character, @handle, id, spec, status…" autofocus>
    <select id="scope" style="font:inherit;padding:7px;border-radius:7px;background:var(--panel-2);color:var(--text);border:1px solid var(--line)">
      <option value="all">All raids</option>
      <option value="upcoming">Upcoming only</option>
      <option value="past">Past only</option>
    </select>
  </div>
  {''.join(sections)}
</div>
<script nonce="{nonce}">{_SCRIPT}</script>
"""
    page = _shell("Raid overview", body, nonce).replace(
        "</style>", _STYLE + "</style>", 1
    )
    response = web.Response(text=page, content_type="text/html")
    response.headers["Content-Security-Policy"] = _csp(nonce)
    return response
