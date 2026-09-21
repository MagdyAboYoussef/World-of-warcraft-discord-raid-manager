"""HTML for the raid manager.

One self-contained page: no build step, no framework, no external assets beyond
the Wowhead spec icons the bot already depends on. Everything user-supplied -
character names, notes - is written with textContent on the client rather than
interpolated into markup, so a character called `<img onerror=...>` renders as
that literal text.
"""

from __future__ import annotations

import html
import secrets

from aiohttp import web

ICON_HOST = "https://wow.zamimg.com"

STYLE = """
*, *::before, *::after { box-sizing: border-box; }
:root {
  --bg: #0d1016; --panel: #151a22; --panel-2: #1b212b; --line: #262e3a;
  --text: #e6edf3; --muted: #8b95a5; --gold: #d9b25f;
  --accepted: #3fb950; --declined: #f85149; --bench: #7d8590;
  --absent: #5a6270; --pending: #d29922;
}
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--gold); }
.wrap { max-width: 1500px; margin: 0 auto; padding: 20px 20px 80px; }

.warn {
  border: 1px solid #6b4a1f; background: #241a0c; color: #f0c674;
  border-radius: 8px; padding: 10px 14px; margin-bottom: 18px;
  font-size: 13px; letter-spacing: .02em;
}

header.top {
  display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-end;
  justify-content: space-between; margin-bottom: 18px;
}
h1 { margin: 0; font-size: 26px; letter-spacing: -.01em; }
.meta { color: var(--muted); font-size: 13.5px; margin-top: 4px; }
.meta span + span::before { content: "·"; margin: 0 8px; opacity: .5; }
.pill {
  display: inline-block; padding: 2px 9px; border-radius: 999px;
  font-size: 11.5px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .06em; border: 1px solid var(--line); background: var(--panel-2);
}
.pill.open { color: var(--accepted); border-color: #1e4b2b; }
.pill.locked { color: var(--pending); border-color: #5c451a; }
.pill.cancelled { color: var(--declined); border-color: #5e2626; }

.panel {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 14px 16px; margin-bottom: 18px;
}
.panel h2 {
  margin: 0 0 10px; font-size: 11.5px; text-transform: uppercase;
  letter-spacing: .1em; color: var(--muted); font-weight: 600;
}
.panel h2 .n { color: var(--text); font-variant-numeric: tabular-nums; letter-spacing: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12.5px; padding: 3px 9px 3px 5px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--panel-2); color: var(--muted);
}
img.mini {
  width: 17px; height: 17px; border-radius: 4px; flex: none;
  border: 1px solid rgba(0,0,0,.45); background: var(--bg);
}
/* Missing entries keep their class icon at full strength - the icon is the
   answer to "who do we still need?", so dimming it defeats the point. */

/* Final roster: what the raid actually looks like right now, before any of the
   pending noise below it. */
#targets { margin-bottom: 12px; }
.roster { display: grid; grid-template-columns: 132px minmax(0, 1fr); gap: 9px 14px; }
.rlabel { display: flex; align-items: baseline; gap: 7px; padding-top: 3px; }
.rlabel span:first-child {
  font-size: 11.5px; text-transform: uppercase; letter-spacing: .09em;
  color: var(--muted); font-weight: 600;
}
.members { display: flex; flex-wrap: wrap; gap: 5px; }
.member {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--panel-2); border: 1px solid var(--line);
  border-radius: 6px; padding: 3px 9px 3px 4px;
  font-size: 13px; font-weight: 600; line-height: 1.35;
}
.member.slot {
  color: var(--pending); border-style: dashed; border-color: #5c451a;
  background: transparent; font-weight: 500; font-size: 12.5px; padding: 3px 9px;
}
.chip.covered { color: var(--accepted); border-color: #234b30; background: #10251a; }
.chip.missing { color: var(--declined); border-color: #4d2222; background: #241414; }

/* "Who do we still need?" - one row per class worth inviting. */
.needs { display: flex; flex-direction: column; gap: 7px; }
.need { display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap; }
.need .cls {
  display: inline-flex; align-items: center; gap: 6px;
  min-width: 148px; font-weight: 700; font-size: 13px;
}
.need .n {
  font-size: 11px; font-weight: 700; color: var(--bg); background: var(--gold);
  border-radius: 999px; padding: 1px 7px; font-variant-numeric: tabular-nums;
}
.need .fixes { display: flex; flex-wrap: wrap; gap: 5px; }
.need .fix {
  font-size: 12px; padding: 2px 8px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--panel-2); color: var(--text);
}
/* A spec-locked gap is the one that gets raids wrong - "a Hunter" does not
   bring Lust, only a Beast Mastery one does. Called out rather than hidden. */
.need .fix em { font-style: normal; color: var(--pending); }
.need.best .cls { font-size: 14px; }
.need.best .fix { border-color: #3a4553; }

.toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 16px; }
.toolbar .grow { flex: 1; }
.toolbar input.search {
  font: inherit; padding: 6px 11px; border-radius: 7px; min-width: 200px;
  background: var(--panel-2); color: var(--text); border: 1px solid var(--line);
}
.toolbar input.search:focus { outline: none; border-color: var(--gold); }
button {
  font: inherit; color: inherit; cursor: pointer;
  background: var(--panel-2); border: 1px solid var(--line);
  border-radius: 7px; padding: 6px 12px;
}
button:hover { border-color: #3a4553; }
button.on { background: var(--gold); color: #1a1206; border-color: var(--gold); font-weight: 600; }
.hint { color: var(--muted); font-size: 12.5px; }
kbd {
  font: 600 11px ui-monospace, SFMono-Regular, Menlo, monospace;
  background: var(--panel-2); border: 1px solid var(--line);
  border-bottom-width: 2px; border-radius: 4px; padding: 1px 5px; color: var(--text);
}

.board { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
@media (max-width: 1200px) { .board { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 680px)  { .board { grid-template-columns: 1fr; } }

.col { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px; }
.col > h3 {
  margin: 0 0 4px; font-size: 13px; letter-spacing: .04em;
  display: flex; justify-content: space-between; align-items: baseline;
}
.count { font-variant-numeric: tabular-nums; color: var(--muted); font-weight: 500; }
.count.under { color: var(--pending); }
.count.full { color: var(--accepted); }
.bar { height: 3px; border-radius: 2px; background: var(--panel-2); margin-bottom: 12px; overflow: hidden; }
.bar > i { display: block; height: 100%; background: var(--accepted); transition: width .2s; }
.bar > i.under { background: var(--pending); }

.card {
  border: 1px solid var(--line); border-left: 3px solid var(--muted);
  border-radius: 8px; background: var(--panel-2);
  padding: 9px 10px; margin-bottom: 8px; outline: none;
}
.card:focus-visible { border-color: var(--gold); box-shadow: 0 0 0 2px rgba(217,178,95,.25); }
.card[data-status="declined"], .card[data-status="absent"] { opacity: .62; }
.card[data-status="tentative"] { border-style: dashed; }
.card .who { display: flex; gap: 9px; align-items: center; }
/* Says the status in words. Without it the only cue that someone is Bench or
   Tentative is a dashed border or a dimmed card, and every state that is not
   Accepted ends up reading as Pending. */
.card .badge {
  margin-left: auto; align-self: flex-start; white-space: nowrap;
  font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em;
  padding: 2px 7px; border-radius: 999px;
  border: 1px solid var(--line); background: var(--bg); color: var(--muted);
}
.card .badge[data-s="accepted"]  { color: var(--accepted); border-color: #234b30; background: #10251a; }
.card .badge[data-s="pending"]   { color: var(--pending);  border-color: #5c451a; background: #241a0c; }
.card .badge[data-s="tentative"] { color: #c8a2fc;         border-color: #4a3570; background: #1c1430; }
.card .badge[data-s="bench"]     { color: #c9d1d9;         border-color: #3a4150; background: #20252e; }
.card .badge[data-s="absent"]    { color: #b3bcc9;         border-color: #333a46; background: #1b1f27; }
.card .badge[data-s="declined"]  { color: var(--declined); border-color: #4d2222; background: #241414; }
.card img.icon {
  width: 28px; height: 28px; border-radius: 5px; flex: none;
  border: 1px solid var(--line); background: var(--bg);
}
.card .name { font-weight: 600; font-size: 14.5px; line-height: 1.25; word-break: break-word; }
.card .spec { font-size: 12px; color: var(--muted); cursor: pointer; }
/* Who is actually behind the character. Monospaced so a lookalike handle -
   the whole trick an impersonator relies on - does not blend in. */
.card .handle {
  font: 11px ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--muted); margin-top: 2px; word-break: break-all;
}
.card .name { cursor: pointer; }
.card .name:hover { text-decoration: underline dotted; }
.card input.nameedit {
  width: 100%; margin-top: 4px; background: var(--bg); color: var(--text);
  border: 1px solid var(--gold); border-radius: 6px; padding: 4px 6px;
  font: inherit; font-size: 13.5px; font-weight: 600;
}
.card input.nameedit:focus { outline: none; }
.card .spec:hover { color: var(--text); text-decoration: underline dotted; }
.card .note.add { color: var(--muted); font-style: italic; opacity: .7; }
.card .note.add:hover { opacity: 1; color: var(--gold); }
.card .note { cursor: pointer; }
.card textarea.noteedit {
  width: 100%; margin-top: 6px; background: var(--bg); color: var(--text);
  border: 1px solid var(--gold); border-radius: 6px; padding: 5px 7px;
  font: inherit; font-size: 12.5px; resize: vertical;
}
.card textarea.noteedit:focus { outline: none; }
.card .note {
  font-size: 12.5px; color: var(--muted); margin-top: 6px;
  border-left: 2px solid var(--line); padding-left: 7px; word-break: break-word;
}
.card .links { font-size: 12px; margin-top: 5px; }
.card select { width: 100%; margin-top: 6px; background: var(--bg); color: var(--text);
  border: 1px solid var(--line); border-radius: 6px; padding: 4px; font: inherit; font-size: 12.5px; }

.acts { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }
.acts button {
  flex: 1 1 30%; min-width: 0; padding: 4px 2px; font-size: 11.5px; font-weight: 600;
  border-radius: 6px; background: transparent; letter-spacing: -.01em;
}
.acts button[data-s="accepted"]:hover, .card[data-status="accepted"] .acts button[data-s="accepted"] {
  background: rgba(63,185,80,.16); border-color: var(--accepted); color: var(--accepted); }
.acts button[data-s="declined"]:hover, .card[data-status="declined"] .acts button[data-s="declined"] {
  background: rgba(248,81,73,.16); border-color: var(--declined); color: var(--declined); }
.acts button[data-s="tentative"]:hover, .card[data-status="tentative"] .acts button[data-s="tentative"] {
  background: rgba(163,113,247,.18); border-color: #a371f7; color: #c8a2fc; }
.acts button[data-s="bench"]:hover, .card[data-status="bench"] .acts button[data-s="bench"] {
  background: rgba(125,133,144,.2); border-color: var(--bench); color: var(--text); }
.acts button[data-s="absent"]:hover, .card[data-status="absent"] .acts button[data-s="absent"] {
  background: rgba(90,98,112,.22); border-color: var(--absent); color: var(--text); }
.acts button[data-s="pending"]:hover, .card[data-status="pending"] .acts button[data-s="pending"] {
  background: rgba(210,153,34,.16); border-color: var(--pending); color: var(--pending); }
/* Destructive, and on its own line: deleting an application is not a status
   change, and it must not sit shoulder to shoulder with the six that are. */
.acts button[data-s="remove"] {
  flex: 1 1 100%; margin-top: 2px; color: var(--muted); border-style: dashed;
}
.acts button[data-s="remove"]:hover {
  background: rgba(248,81,73,.14); border-color: var(--declined);
  color: var(--declined); border-style: solid;
}

/* Activity log. Only ever rendered here - Discord never shows it. */
.log { max-height: 340px; overflow-y: auto; }
.logrow {
  display: grid; grid-template-columns: 66px minmax(0, 1fr); gap: 10px;
  padding: 5px 2px; border-bottom: 1px solid var(--line); font-size: 12.5px;
}
.logrow:last-child { border-bottom: 0; }
.logrow .lt { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.logrow .lb { word-break: break-word; }
.logrow b.who { font-weight: 600; color: var(--text); }
.logrow .ld { color: var(--muted); }
.logrow .src {
  margin-left: 7px; font-size: 10px; text-transform: uppercase; letter-spacing: .08em;
  color: var(--muted); border: 1px solid var(--line); border-radius: 4px; padding: 0 4px;
}

.empty { color: var(--muted); font-size: 12.5px; font-style: italic; padding: 6px 2px; }

#toast {
  position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%);
  background: #2a1517; border: 1px solid #6b2b2b; color: #ffc9c4;
  padding: 10px 16px; border-radius: 8px; font-size: 13.5px;
  opacity: 0; pointer-events: none; transition: opacity .18s; max-width: 90vw;
}
#toast.show { opacity: 1; }

.center { max-width: 520px; margin: 18vh auto; text-align: center; padding: 0 20px; }
.center h1 { font-size: 22px; margin-bottom: 10px; }
.center p { color: var(--muted); }
"""

SCRIPT = """
const BASE = location.pathname.replace(/\\/+$/, '');
const ICONS = 'https://wow.zamimg.com/images/wow/icons/large/';
// tools/build_preview.py inlines the icons as data URIs, because the preview is
// served under a CSP that blocks third-party images. Absent that map - which is
// every real request - icons come from the CDN as normal.
const ICON_MAP = window.ICON_MAP || null;
const iconUrl = (slug) => (ICON_MAP ? ICON_MAP[slug] || '' : ICONS + slug + '.jpg');
// Sort order within a column: undecided first, since those are the ones asking
// the raid lead for a decision.
const ORDER = { pending: 0, tentative: 1, accepted: 2, bench: 3, absent: 4, declined: 5 };
const KEYS = { a: 'accepted', d: 'declined', t: 'tentative', b: 'bench', n: 'absent',
               p: 'pending' };
// Not derived from the status labels: "Accepted" and "Absent" both start with
// A, so first-letter buttons would give the roster two identical controls.
const SHORT = {
  accepted: ['Accept', 'A'], declined: ['Out', 'D'], tentative: ['Tent', 'T'],
  bench: ['Backup', 'B'], absent: ['Absent', 'N'], pending: ['Reset', 'P'],
};

// Audit actions -> what the log says happened. Kept here rather than sent from
// the server so an entry written by an older build still reads sensibly.
const VERBS = {
  apply: 'signed up', status: 'set', spec: 'reassigned', remove: 'removed',
  withdraw: 'withdrew', raid: 'changed the raid', character: 'renamed', note: 'noted',
};

let state = null;
let filter = 'active';
let search = '';  // live text query; when set it overrides the status filter
// Which statuses each filter shows. 'inactive' (Not playing) is everyone who
// is neither waiting on a decision nor confirmed in — Out, Backup, Absent,
// Tentative. Centralised so the board, the empty text and the keyboard
// advance can never disagree about what a filter means.
// A signup matches the search box on character name, Discord handle, class or
// spec. Substring, case-insensitive — the same forgiving match the overview
// page uses, so 'mage', 'mimz' or '@kaz' all narrow the board as you type.
function matchesSearch(s) {
  if (!search) return true;
  return [s.character, s.discord_name, s.wow_class, s.spec_label]
    .filter(Boolean).join(' ').toLowerCase().includes(search);
}

function inFilter(status, f) {
  if (f === 'active') return status === 'accepted';
  if (f === 'pending') return status === 'pending';
  if (f === 'inactive') return status !== 'pending' && status !== 'accepted';
  return true;  // 'all'
}
let inflight = 0;
let rendered = null;  // signature of what the DOM currently shows

const $ = (sel) => document.querySelector(sel);

// One ordering everywhere: group by class alphabetically, then by name -
// the same logic the Discord board uses, so a name sits in the same place
// on every surface. wow_class can be null for an unrecognised spec.
function byClassThenName(a, b) {
  return (a.wow_class || '').localeCompare(b.wow_class || '')
      || a.character.localeCompare(b.character);
}

function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 4200);
}

async function api(path, body) {
  const res = await fetch(BASE + path, {
    method: body ? 'POST' : 'GET',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    cache: 'no-store',
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* fall through to status */ }
  if (!res.ok) throw new Error((data && data.error) || ('Request failed (' + res.status + ').'));
  return data;
}

async function mutate(path, body, card) {
  inflight++;
  if (card && body.status) card.dataset.status = body.status;  // instant feedback
  try {
    render(await api(path, body));
  } catch (err) {
    toast(err.message);
    try { render(await api('/state')); } catch (_) { /* keep what we have */ }
  } finally {
    inflight--;
  }
}

function fmtWhen(ts) {
  if (!ts) return 'No start time set';
  const d = new Date(ts * 1000);
  return d.toLocaleString([], {
    weekday: 'short', day: 'numeric', month: 'short',
    hour: '2-digit', minute: '2-digit',
  });
}

function fmtLeft(ts) {
  const mins = Math.max(0, Math.round((ts * 1000 - Date.now()) / 60000));
  if (mins < 60) return mins + 'm';
  return Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;   // never innerHTML: this is user data
  return node;
}

// A null cap means the role has no target of its own - melee and ranged under a
// combined DPS target. That is not the same as a target of zero, so it shows a
// bare count and never an under-target warning.
const tally = (e) => (e.cap === null ? String(e.accepted) : e.accepted + ' / ' + e.cap);
const isUnder = (e) => e.cap !== null && e.accepted < e.cap;

function mini(slug, alt) {
  const img = el('img', 'mini');
  img.src = iconUrl(slug);
  img.alt = alt || '';
  img.loading = 'lazy';
  img.addEventListener('error', () => img.remove());  // CDN down: text still reads
  return img;
}

function noteEditor(signup, noteEl) {
  const box = el('textarea');
  box.className = 'noteedit';
  box.value = signup.note || '';
  box.placeholder = 'Note for this player (blank clears it) — Esc to cancel';
  box.rows = 2;
  box.maxLength = 200;
  let done = false;
  const commit = () => {
    if (done) return;
    done = true;
    const value = box.value.trim();
    if (value === (signup.note || '')) { box.replaceWith(noteEl); return; }
    mutate('/note', { user_id: signup.user_id, note: value });
  };
  box.addEventListener('keydown', (e) => {
    e.stopPropagation();  // keep the card's A/D/B shortcuts from firing while typing
    if (e.key === 'Escape') { done = true; box.replaceWith(noteEl); }
  });
  box.addEventListener('blur', commit);
  return box;
}

function nameEditor(signup) {
  const input = el('input');
  input.type = 'text';
  input.value = signup.character;
  input.placeholder = 'Name-Server';
  input.className = 'nameedit';
  const commit = () => {
    const value = input.value.trim();
    if (!value || value === signup.character) { input.remove(); return; }
    mutate('/character', { user_id: signup.user_id, character: value });
  };
  input.addEventListener('keydown', (e) => {
    e.stopPropagation();  // don't trigger the card's A/D/B... shortcuts
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') input.remove();
  });
  input.addEventListener('blur', commit);
  return input;
}

function specSelect(signup) {
  const select = el('select');
  for (const spec of state.specs) {
    const option = el('option', null, spec.label);
    option.value = spec.key;
    if (spec.key === signup.spec_key) option.selected = true;
    select.appendChild(option);
  }
  select.addEventListener('change', () => {
    mutate('/spec', { user_id: signup.user_id, spec_key: select.value });
  });
  select.addEventListener('keydown', (e) => e.stopPropagation());
  return select;
}

// Status metadata comes from the server's own Status enum, so a label added
// there needs no change here.
function statusMeta(value) {
  return (state.statuses || []).find((s) => s.value === value)
    || { value, label: value, emoji: '' };
}

function card(signup) {
  const root = el('div', 'card');
  root.tabIndex = 0;
  root.dataset.status = signup.status;
  root.dataset.user = signup.user_id;
  root.style.borderLeftColor = signup.color;

  const who = el('div', 'who');
  if (signup.icon) {
    const img = el('img', 'icon');
    img.src = iconUrl(signup.icon);
    img.alt = '';
    img.loading = 'lazy';
    img.addEventListener('error', () => img.remove());
    who.appendChild(img);
  }
  const box = el('div');
  const name = el('div', 'name', signup.character);
  name.style.color = signup.color;
  name.title = 'Click to edit the character (Name-Server)';
  name.addEventListener('click', () => {
    if (box.querySelector('.nameedit')) return;
    const input = nameEditor(signup);
    name.after(input);
    input.focus();
    input.select();
  });
  const spec = el('div', 'spec', signup.spec_label);
  spec.title = 'Click to reassign spec';
  spec.addEventListener('click', () => {
    if (root.querySelector('select')) return;
    root.appendChild(specSelect(signup));
  });
  // Who is actually behind this character. The point of the whole card for a
  // raid lead dealing with a troll: character names are free to invent, the
  // account is not.
  const handle = el('div', 'handle',
    signup.discord_name ? '@' + signup.discord_name : 'id ' + signup.user_id);
  handle.title = 'Discord ID ' + signup.user_id
    + (signup.discord_name ? '' : ' — handle not resolved yet');
  box.append(name, spec, handle);
  who.appendChild(box);

  const meta = statusMeta(signup.status);
  const badge = el('span', 'badge', meta.label);
  badge.dataset.s = signup.status;
  badge.title = meta.emoji + ' ' + meta.label;
  who.appendChild(badge);

  root.appendChild(who);

  const note = el('div', signup.note ? 'note' : 'note add', signup.note || '+ add note');
  note.title = 'Click to edit the note';
  note.addEventListener('click', () => {
    if (root.querySelector('.noteedit')) return;
    note.replaceWith(noteEditor(signup, note));
  });
  root.appendChild(note);

  if (signup.logs_url) {
    const links = el('div', 'links');
    const a = el('a', null, 'Warcraft Logs ↗');
    a.href = signup.logs_url;
    a.target = '_blank';
    // noreferrer is load-bearing: the token lives in this page's URL.
    a.rel = 'noreferrer noopener';
    links.appendChild(a);
    root.appendChild(links);
  }

  const acts = el('div', 'acts');
  for (const status of state.statuses) {
    const [short, key] = SHORT[status.value] || [status.label, ''];
    const button = el('button', null, short);
    button.dataset.s = status.value;
    button.title = status.label + (key ? '  (' + key + ')' : '');
    button.addEventListener('click', (e) => {
      e.stopPropagation();
      apply(root, signup, status.value);
    });
    acts.appendChild(button);
  }
  const wipe = el('button', null, 'Remove application');
  wipe.dataset.s = 'remove';
  wipe.title = 'Delete this application entirely  (X)';
  wipe.addEventListener('click', (e) => {
    e.stopPropagation();
    removeSignup(signup);
  });
  acts.appendChild(wipe);
  root.appendChild(acts);

  root.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const key = e.key.toLowerCase();
    if (KEYS[key]) {
      e.preventDefault();
      apply(root, signup, KEYS[key]);
    } else if (key === 'x') {
      e.preventDefault();
      removeSignup(signup);
    } else if (key === 'arrowdown' || key === 'arrowup') {
      e.preventDefault();
      step(root, key === 'arrowdown' ? 1 : -1);
    }
  });
  return root;
}

// Naming the account in the prompt, not just the character: the character name
// is the part a troll controls, and it is the wrong thing to confirm against.
function removeSignup(signup) {
  const who = signup.character
    + (signup.discord_name ? ' (@' + signup.discord_name + ')' : '');
  const warning = 'Delete ' + who + "'s application?\\n\\n"
    + 'They come off the roster entirely. The removal is recorded in '
    + 'the activity log, with your name on it.';
  if (!confirm(warning)) return;
  mutate('/remove', { user_id: signup.user_id });
}

function apply(root, signup, status) {
  if (signup.status === status) return;
  // Remember where to land before the re-render throws this node away.
  const next = nextCard(root);
  mutate('/status', { user_id: signup.user_id, status }, root).then(() => {
    // In pending-only mode the card just handled disappears, so advance; other-
    // wise stay put, or every keystroke would move the selection out from under
    // someone still reading.
    // Advance only if this new status drops the card out of the current filter;
    // otherwise it stays put (e.g. Backup -> Absent while on 'Not playing').
    focusCard(!inFilter(status, filter) && next ? next : signup.user_id);
  });
}

function focusCard(userId) {
  const target = document.querySelector('.card[data-user="' + userId + '"]');
  if (!target) return;
  // This one *is* a deliberate move, so bring it into view - but 'nearest'
  // nudges rather than recentring the whole page.
  target.focus({ preventScroll: true });
  target.scrollIntoView({ block: 'nearest' });
}

function nextCard(root) {
  const cards = [...document.querySelectorAll('.card')];
  const at = cards.indexOf(root);
  const following = cards[at + 1] || cards[at - 1];
  return following ? following.dataset.user : null;
}

function step(root, delta) {
  const cards = [...document.querySelectorAll('.card')];
  const target = cards[cards.indexOf(root) + delta];
  if (target) focusCard(target.dataset.user);
}

function renderRoster() {
  const accepted = state.signups.filter((s) => s.status === 'accepted');
  $('#roster-count').textContent = accepted.length + ' / ' + state.raid_size;

  // Only in combined mode. With four separate targets each row already states
  // its own, and repeating them here would just be noise.
  const strip = $('#targets');
  strip.textContent = '';
  strip.style.display = state.combined_dps ? '' : 'none';
  if (state.combined_dps) {
    for (const target of state.targets) {
      strip.appendChild(
        el('span', 'chip ' + (isUnder(target) ? 'missing' : 'covered'),
           target.label + ' ' + target.accepted + ' / ' + target.cap),
      );
    }
  }

  const roster = $('#roster');
  roster.textContent = '';
  for (const role of state.roles) {
    const under = isUnder(role);

    const label = el('div', 'rlabel');
    label.append(
      el('span', null, role.label),
      el('span', 'count ' + (under ? 'under' : 'full'), tally(role)),
    );

    const members = el('div', 'members');
    // Alphabetical, matching the roster embed in Discord - a final roster is
    // read to find a name, not to see who signed up first.
    const mine = accepted
      .filter((s) => s.role === role.key)
      .sort(byClassThenName);

    for (const signup of mine) {
      const chip = el('span', 'member');
      if (signup.icon) chip.appendChild(mini(signup.icon, signup.spec_label));
      const name = el('span', null, signup.character);
      name.style.color = signup.color;
      chip.appendChild(name);
      chip.title = signup.spec_label;
      members.appendChild(chip);
    }
    if (under) {
      members.appendChild(
        el('span', 'member slot', '+' + (role.cap - role.accepted) + ' needed'),
      );
    }
    roster.append(label, members);
  }
}

function renderHeader() {
  const raid = state.raid;
  $('#title').textContent = raid.title;
  document.title = raid.title + ' — roster';

  const meta = $('#meta');
  meta.textContent = '';
  meta.append(
    el('span', 'pill ' + raid.state, raid.state),
    el('span', null, fmtWhen(raid.starts_at)),
    el('span', null, raid.region + ' server time'),
    el('span', null, 'link expires in ' + fmtLeft(state.expires_at)),
  );
  if (raid.auto_accept) {
    const badge = el('span', 'pill open', '\u26a1 auto-accept');
    badge.title = 'New applications are accepted immediately';
    meta.appendChild(badge);
  }

  const counts = {};
  for (const s of state.signups) counts[s.status] = (counts[s.status] || 0) + 1;
  $('#counts').textContent = state.statuses
    .map((s) => s.emoji + ' ' + s.label + ' ' + (counts[s.value] || 0))
    .join('   ');
}

function renderBuffs() {
  const chips = $('#buffs');
  chips.textContent = '';
  for (const buff of state.buffs) {
    const chip = el('span', 'chip ' + (buff.covered ? 'covered' : 'missing'));
    if (buff.icon) chip.appendChild(mini(buff.icon, buff.wow_class || ''));
    chip.appendChild(
      el('span', null, buff.covered ? buff.label + ' (' + buff.count + ')' : buff.label),
    );
    chip.title = buff.covered
      ? buff.count + ' on the accepted roster'
      : 'Missing' + (buff.wow_class ? ' — needs a ' + buff.wow_class : '');
    chips.appendChild(chip);
  }
}

function fmtClock(ts) {
  const d = new Date(ts * 1000);
  // Today's entries want the time; anything older wants the day, and a log
  // showing "14:32" against last Tuesday is actively misleading.
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { day: 'numeric', month: 'short' });
}

function who(name, id) {
  const node = el('b', 'who', name ? '@' + name : (id ? 'id ' + id : 'the bot'));
  if (id) node.title = 'Discord ID ' + id;
  return node;
}

function renderLog() {
  const entries = state.audit || [];
  const box = $('#log');
  box.textContent = '';
  $('#log-count').textContent = entries.length;
  if (!entries.length) {
    box.appendChild(el('div', 'empty', 'nothing recorded yet'));
    return;
  }
  for (const entry of entries) {
    const row = el('div', 'logrow');

    const when = el('span', 'lt', fmtClock(entry.at));
    when.title = new Date(entry.at * 1000).toLocaleString();

    const body = el('span', 'lb');
    body.appendChild(who(entry.actor, entry.actor_id));
    body.appendChild(document.createTextNode(' ' + (VERBS[entry.action] || entry.action)));
    // Self-service reads "@mimz signed up", not "@mimz signed up @mimz".
    if (entry.target_id && entry.target_id !== entry.actor_id) {
      body.appendChild(document.createTextNode(' '));
      body.appendChild(who(entry.target, entry.target_id));
    }
    if (entry.detail) body.appendChild(el('span', 'ld', ' \u2014 ' + entry.detail));
    const tag = el('span', 'src', entry.source);
    tag.title = entry.source === 'web' ? 'done on this page' : 'done in Discord';
    body.appendChild(tag);

    row.append(when, body);
    box.appendChild(row);
  }
}

function renderNeeds() {
  const needs = state.recruit || [];
  const box = $('#needs');
  const panel = $('#needs-panel');
  box.textContent = '';
  // Nothing missing is worth saying out loud - an empty panel just looks broken.
  if (!needs.length) {
    panel.classList.add('done');
    $('#needs-title').textContent = 'Raid buffs — every buff is covered';
    box.appendChild(el('div', 'empty', 'Nothing missing. This roster brings the lot.'));
    return;
  }
  panel.classList.remove('done');
  $('#needs-title').textContent = 'Still needed — bring one of these';

  const most = needs[0].count;
  for (const need of needs) {
    const row = el('div', 'need' + (need.count === most ? ' best' : ''));

    const cls = el('span', 'cls');
    if (need.icon) cls.appendChild(mini(need.icon, need.wow_class));
    const name = el('span', null, need.wow_class);
    name.style.color = need.color;
    cls.append(name, el('span', 'n', String(need.count)));

    const fixes = el('div', 'fixes');
    for (const cover of need.covers) {
      const chip = el('span', 'fix', cover.label);
      if (cover.specs && cover.specs.length) {
        // textContent on the child, so a spec name is still never markup.
        chip.appendChild(el('em', null, '  ' + cover.specs.join(' / ') + ' only'));
        chip.title = need.wow_class + ' brings ' + cover.label
          + ' only as ' + cover.specs.join(' or ');
      }
      fixes.appendChild(chip);
    }

    row.append(cls, fixes);
    box.appendChild(row);
  }
}

function renderBoard() {
  const board = $('#board');
  board.textContent = '';
  for (const role of state.roles) {
    const col = el('div', 'col');
    const head = el('h3');
    head.appendChild(el('span', null, role.label));
    const under = isUnder(role);
    head.appendChild(el('span', 'count ' + (under ? 'under' : 'full'), tally(role)));
    col.appendChild(head);

    // No target, no progress bar - there is nothing to be a fraction of.
    if (role.cap !== null) {
      const bar = el('div', 'bar');
      const fill = el('i', under ? 'under' : null);
      fill.style.width = Math.min(100, role.cap ? (role.accepted / role.cap) * 100 : 0) + '%';
      bar.appendChild(fill);
      col.appendChild(bar);
    }

    let members = state.signups.filter((s) => s.role === role.key);
    // Search looks across the whole raid regardless of the status tab; without a
    // query the selected tab applies as before.
    if (search) members = members.filter(matchesSearch);
    else if (filter !== 'all') members = members.filter((s) => inFilter(s.status, filter));
    members.sort((a, b) =>
      (ORDER[a.status] - ORDER[b.status]) || byClassThenName(a, b));

    if (!members.length) {
      const blank = search ? 'no match'
        : filter === 'pending' ? 'nothing pending'
        : filter === 'active' ? 'nobody accepted yet'
        : filter === 'inactive' ? 'nobody sitting out' : 'nobody yet';
      col.appendChild(el('div', 'empty', blank));
    }
    for (const signup of members) col.appendChild(card(signup));
    board.appendChild(col);
  }

  // Specs the data no longer recognises have no role and would silently vanish.
  const orphans = state.signups.filter((s) => !s.role);
  $('#orphans').textContent = orphans.length
    ? 'Unrecognised spec on: ' + orphans.map((s) => s.character).join(', ')
    : '';
}

// Everything the heavy DOM depends on. A poll that comes back identical is a
// no-op: tearing the board down and rebuilding it on a timer collapses the
// page height mid-scroll and drags the reader around for no reason.
function signature() {
  const audit = state.audit || [];
  return JSON.stringify([
    filter,
    search,
    state.raid,
    state.roles,
    state.signups.map((s) =>
      [s.user_id, s.status, s.spec_key, s.character, s.note, s.logs_url, s.discord_name]),
    // Entries are append-only and trimmed from the tail, so the newest id plus
    // the length pins the list down without hashing all 120 rows every poll.
    [audit.length, audit.length ? audit[0].id : 0],
  ]);
}

function render(next) {
  state = next;
  renderHeader();  // cheap, and keeps the expiry countdown ticking

  const sig = signature();
  if (sig === rendered) return;
  rendered = sig;

  const active = document.activeElement;
  const focused = active && active.classList.contains('card') ? active.dataset.user : null;
  const scroll = window.scrollY;

  renderRoster();
  renderBuffs();
  renderNeeds();
  renderBoard();
  renderLog();

  // Emptying the board shortens the document, and the browser clamps scrollY to
  // fit; put it back now the new content has restored the height.
  window.scrollTo(0, scroll);
  // preventScroll: a background poll must never yank the page to whichever card
  // happened to hold focus.
  if (focused) {
    const restored = document.querySelector('.card[data-user="' + focused + '"]');
    if (restored) restored.focus({ preventScroll: true });
  }
}

$('#refresh').addEventListener('click', async () => {
  const btn = $('#refresh');
  btn.disabled = true;
  const was = btn.textContent;
  btn.textContent = '↻ Refreshing…';
  try {
    await api('/refresh', {});
    toast('Discord board refreshed. If it still looks stale, scroll away and back — that is Discord caching, not the data.');
  } catch (err) {
    toast(err.message);
  } finally {
    btn.textContent = was;
    btn.disabled = false;
  }
});

$('#q').addEventListener('input', (e) => {
  search = e.target.value.trim().toLowerCase();
  // Typing searches the whole raid, so surface every status, not just the tab.
  if (search && filter !== 'all') setFilter('all');  // setFilter re-renders
  else if (state) render(state);
});

$('#f-active').addEventListener('click', () => setFilter('active'));
$('#f-pending').addEventListener('click', () => setFilter('pending'));
$('#f-inactive').addEventListener('click', () => setFilter('inactive'));
$('#f-all').addEventListener('click', () => setFilter('all'));

function setFilter(next) {
  filter = next;
  for (const [id, val] of [['#f-active','active'], ['#f-pending','pending'],
                           ['#f-inactive','inactive'], ['#f-all','all']]) {
    $(id).classList.toggle('on', next === val);
  }
  if (state) render(state);
}

async function poll() {
  if (document.hidden || inflight) return;
  try {
    render(await api('/state'));
  } catch (err) {
    toast(err.message);
  }
}

poll();
setInterval(poll, 5000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
"""


def _shell(title: str, body: str, nonce: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(title)}</title>
<style nonce="{nonce}">{STYLE}</style>
</head><body>{body}</body></html>"""


def _csp(nonce: str) -> str:
    return (
        "default-src 'none'; "
        f"img-src {ICON_HOST} data:; "
        f"style-src 'nonce-{nonce}'; "
        f"script-src 'nonce-{nonce}'; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )


#: The whole page body, minus the two things that differ between the live page
#: and the preview: the banner at the top and the raid title.
#:
#: Shared with tools/build_preview.py deliberately. The preview used to keep its
#: own copy of this markup, drifted from it, and ended up missing an element the
#: real script writes to — which threw on first render and stopped the preview
#: updating at all. One template is the fix for that whole class of bug.
BODY = """
<div class="wrap">
{banner}
  <header class="top">
    <div>
      <h1 id="title">{title}</h1>
      <div class="meta" id="meta"></div>
    </div>
    <div class="meta" id="counts"></div>
  </header>

  <div class="panel">
    <h2>Final roster — <span class="n" id="roster-count"></span> accepted</h2>
    <div class="chips" id="targets"></div>
    <div class="roster" id="roster"></div>
  </div>

  <div class="panel">
    <h2>Raid buffs — accepted roster</h2>
    <div class="chips" id="buffs"></div>
  </div>

  <div class="panel" id="needs-panel">
    <h2 id="needs-title">Still needed — bring one of these</h2>
    <div class="needs" id="needs"></div>
  </div>

  <div class="toolbar">
    <button id="f-active" class="on">Active roster</button>
    <button id="f-pending">Pending only</button>
    <button id="f-inactive">Not playing</button>
    <button id="f-all">All signups</button>
    <input id="q" class="search" type="search" autocomplete="off"
           placeholder="Search name, @handle or class…">
    <button id="refresh" title="Redraw the raid board in Discord now">↻ Refresh board</button>
    <span class="grow"></span>
    <span class="hint">
      Focus a card, then <kbd>A</kbd>ccept <kbd>D</kbd> out <kbd>T</kbd>entative
      <kbd>B</kbd>ackup <kbd>N</kbd> absent <kbd>P</kbd>ending <kbd>X</kbd> remove ·
      <kbd>↑</kbd><kbd>↓</kbd> to move · click a name or spec to edit it · every change is logged below
    </span>
  </div>

  <div class="hint" id="orphans"></div>
  <div class="board" id="board"></div>

  <div class="panel" id="log-panel">
    <h2>Activity log — <span class="n" id="log-count"></span> entries · visible only here</h2>
    <div class="log" id="log"></div>
  </div>
</div>
<div id="toast"></div>
"""

WARNING = """
  <div class="warn">
    <strong>DO NOT SHARE THIS LINK.</strong> Anyone who opens it can change this
    raid's roster as you, and every change is recorded against your name. It
    expires on its own — ask the bot for a fresh one rather than forwarding this.
  </div>
"""


def render_page(title: str) -> web.Response:
    nonce = secrets.token_urlsafe(16)
    body = (
        BODY.format(banner=WARNING, title=html.escape(title))
        + f'<script nonce="{nonce}">{SCRIPT}</script>'
    )
    response = web.Response(text=_shell(title, body, nonce), content_type="text/html")
    response.headers["Content-Security-Policy"] = _csp(nonce)
    return response


def render_error(status: int, message: str) -> web.Response:
    nonce = secrets.token_urlsafe(16)
    heading = {401: "Link expired", 403: "No access", 404: "Not found", 410: "Raid retired",
     503: "Starting up"}.get(
        status, "Something went wrong"
    )
    body = (
        f'<div class="center"><h1>{html.escape(heading)}</h1>'
        f"<p>{html.escape(message)}</p></div>"
    )
    response = web.Response(
        text=_shell(heading, body, nonce), content_type="text/html", status=status
    )
    response.headers["Content-Security-Policy"] = _csp(nonce)
    return response
