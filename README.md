# WoW Raid Bot

Our raid group is a bunch of 30-somethings with real life, kids and full-time
jobs. Collectively we have a lot of raiding experience. We are also, somehow,
completely incapable of organising a Tuesday night.


So I built this for us. It turned out to be genuinely useful, so here it is for
any other guild that needs it.

Built for **WoW: Midnight (12.0)** — 13 classes, 40 specs.

## And this is briefly what you can do

**Who can create a raid**

Anyone with Discord's Administrator permission, plus anyone holding a role you
list in `ADMIN_ROLES` (defaults to `Raid Leader` and `Officer`). Everyone else
gets buttons only — they can't touch the roster.

**How you create a raid**

`/raid create title:"Voidspire— Mythic" when:"sat 19:00" duration:3h`

Everything except the title is optional. Times are **server time**, so 19:00
means 19:00 on your realm — no timezone maths, and it survives daylight saving.
Click the `when` field and it suggests the usual raid slots; you can also type
`20:30`, `tomorrow 20:00`, or `in 90m`. The board then shows the raid window in
server time, a live countdown, *and* each person's own local time. Your one
raider in Australia will finally stop asking.

The first time you make a raid, set `timezone:` to your region — `EU`, `NA`,
`KR`, `TW` or `OCE`. After that every new raid remembers it, so you never have
to touch it again.

The accepted roster gets pinged ten minutes before start.

**Role targets, and not over-committing to a split**

By default you set four targets — `tanks`, `healers`, `melee`, `ranged`. If you
don't actually care how the DPS breaks down, pass `dps:` instead and you get one
combined number:

`/raid create title:"Voidspire — Mythic" tanks:2 healers:4 dps:14`

The board then reads **2 / 4 / 14**. Melee and ranged are still listed
separately everywhere people appear — you can always see who's what — but
neither gets flagged as "under target" on its own, so a night that turns up
9 melee and 5 ranged is simply a full raid rather than two warnings.

`dps` and `melee`/`ranged` are mutually exclusive; set one or the other. Raids
made before this existed keep their four targets and are untouched.

**How people sign up**

They hit **Apply** on the board and give a character name, a Warcraft Logs link
(optional) and a note for the raid lead (optional), then pick class → spec.
That lands them in Pending until an officer looks at it.

The bot remembers all of that. Next week it's one click to sign up again, or
"change spec only" if you're swapping. There are also **Bench me** and
**Absent** buttons so people can rule themselves out without anyone chasing
them.

**How buffs are shown**

This is the bit that actually changed our raid nights. The board tracks what
your *accepted* roster covers, live:

> ⚠️ **Missing Raid Buffs**
> 🛡️ Battle Shout   ⚡ Skyfury   ✨ Devotion   🏹 Hunter's Mark
>
> ✅ **Available Buffs**
> 🔮 Arcane Intellect `1`   🌿 Mark of the Wild `2`   💀 Mass Grip `1`   ⚡ Lust `2`

Missing buffs show the **class icon** of whoever you still need, so you can see
at a glance that you're short a Paladin. Covered buffs show how many people
bring them, because one Lust and two Lusts are very different problems.

Accept a Holy Priest and Fortitude stops being missing. Some entries are
smarter than a checkbox — any Death Knight gives you **Grip**, but a *Blood* DK
gives you **Mass Grip**, and the panel says so.

**What admins can do**

From **Manage roster**: accept, decline, bench, mark absent, or push someone
back to pending. Change anyone's spec (for when your Feral agrees to go Resto
to fix the healer count), or remove them entirely. You see their logs link and
their note while you decide, and live comp-vs-target counts so you know what
you're still short of.

From **Raid settings**: edit the title, description, start time and duration,
change your role targets, lock signups, or cancel the raid.

Everything an admin does is private to them — no ephemeral spam in the channel.

## Try it without hosting anything

If you just want to see whether it fits your guild, invite the instance I run:

**[Invite WoW Raid Bot to your Discord](https://discord.com/oauth2/authorize?client_id=1528172224392204420&permissions=347136&scope=bot+applications.commands)**

No token, no Python, no server. Give it a channel and run `/raid create`.

If you'd rather your raid data never left your own machine, host it yourself —
that's the next section.

## Setup

You run your own instance. It takes about five minutes, and your raid data
(character names, specs, logs links, signup status) stays in a SQLite file on
your own machine — nothing is sent anywhere else.

```bash
git clone https://github.com/MagdyAboYoussef/wow-raid-bot.git
cd wow-raid-bot

python -m venv .venv
source .venv/bin/activate         # Linux/macOS;  .venv\Scripts\activate on Windows
pip install -r requirements.txt

python -m tools.fetch_icons       # download + resize the 76 icons
cp .env.example .env              # then fill in DISCORD_TOKEN
python -m bot
```

Needs Python 3.11+.

On first launch the bot uploads its icons as **application emojis** (76 of
them). These belong to the bot, not your server, so they don't touch your
server's 50-emoji limit and they work in every guild the bot joins. It takes a
minute or two once, then never again.

#### Creating your own Discord application

1. https://discord.com/developers/applications → **New Application**
2. **Bot** → Reset Token → copy into `.env`
3. **Installation** → Guild Install, scopes `bot` + `applications.commands`,
   bot permissions: View Channels, Send Messages, Embed Links,
   Read Message History, Use External Emojis
4. Use the generated install link to add it to your server

No privileged intents are required — everything runs through slash commands and
buttons.

Set `GUILD_ID` in `.env` to your server's ID while you're testing: slash
commands then appear instantly instead of taking up to an hour to propagate.
Leave it blank in production so the commands work in every server that adds it.

#### Keeping it running

A systemd unit is the least fuss:

```ini
[Unit]
Description=WoW Raid Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/wow-raid-bot
ExecStart=/opt/wow-raid-bot/.venv/bin/python -m bot
Restart=always
RestartSec=10
User=raidbot

[Install]
WantedBy=multi-user.target
```

Restarts are safe: the buttons on existing raid boards use static IDs so they
keep working, and reminders are claimed in the database, so a restart in the
middle of raid night won't re-ping everybody.

#### Bringing old boards up to date

A board is only redrawn when something about it changes, so a raid posted
before a layout or button change keeps the old look until someone touches its
roster. To pull them all forward at once:

```bash
python -m tools.refresh_boards --dry-run   # see what would be touched
python -m tools.refresh_boards             # every live raid
python -m tools.refresh_boards 3 7         # or just these
```

This **edits the existing messages**, so each raid keeps its place in the
channel and its replies — unlike `/raid repost`, which abandons the old message
and posts a new one. It changes no roster data. Cancelled raids are skipped,
and a raid whose message was deleted is reported rather than silently ignored:
that one does need `/raid repost`.

Safe to run while the bot is up — it opens its own short-lived connection and
deliberately doesn't start the reminder loop, so it can't double-ping anyone.

## Web roster manager

Accepting people one dropdown at a time gets slow once a raid has twenty
applicants. The **🌐 Open UI** button on the board hands a raid lead a private,
expiring link to a page showing all four roles side by side, every applicant in
them, and live buff coverage — with keyboard shortcuts, so a full queue is one
keypress per person.

It's off by default. To turn it on, set `WEB_BASE_URL` in `.env` to the public
HTTPS origin you'll serve it from, and put a TLS proxy in front:

```
wow-raid-manager.magdy.org {
    reverse_proxy 127.0.0.1:8080
}
```

That's a whole Caddyfile — Caddy gets the certificate itself. Point an `A`
record at the host, open 443, and leave the bot bound to loopback. Then add to
the systemd unit above:

```ini
# aiohttp binds a port, so wait for real connectivity, not just the network
After=network-online.target
Wants=network-online.target
```

Everything else in `.env.example` under *web manager* has a working default.

**How access works.** There is no login. The link itself is the credential: it
is signed with a server-side secret and names one raid, one Discord user, and
one expiry, none of which the holder can edit. Every request re-checks that the
user it was issued to still holds an admin role, so losing the role
immediately kills any link already handed out. Links expire after 3 hours by
default, and a raid's page stops answering 30 days after the raid ends —
that's an access gate, not a delete, so your roster history stays intact.

Because the link is a bearer credential, the bot shouts about not sharing it
and the page repeats the warning. Press the button again for a fresh one rather
than forwarding an old one.

The **🌐 Open UI** button sits on the public board next to the other admin
buttons. Discord has no way to show a component to some viewers and not others,
so raiders can see it too — pressing it gets them a private note explaining
it's for raid leads and pointing them at **📝 Apply** instead. Nobody ever sees
anyone else's link.

## Command reference

| Command | Who | What |
|---|---|---|
| `/raid create` | admin | Post a signup board. `title` required; `description`, `when`, `duration`, `timezone` and role targets optional |
| `/raid manage` | admin | Roster manager (same as the 🛠️ button) |
| `/raid page` | admin | Private link to the web roster manager (same as the 🌐 button) |
| `/raid settings` | admin | Title, description, time, duration, targets, lock, cancel |
| `/raid list` | anyone | Recent raids with jump links |
| `/raid repost` | admin | Post the board again if it's buried |
| `/profile` | anyone | Show or clear the character the bot remembers for you |
| `/help` | anyone | In-Discord version of this section |

`when` accepts:

| Input | Means |
|---|---|
| `20:30` | next time it's 20:30 |
| `sat 19:00` | next Saturday |
| `tomorrow 20:30` / `today 20:30` | that day |
| `2026-07-22 20:30` | an exact date |
| `in 90m` / `in 2h` | relative |

`duration` accepts `3h`, `2h30m`, `90m`, `2.5h`, or a bare `3` (read as hours).
All three fields suggest values as soon as you click them.

### How "server time" works

The bot can't detect what region your realms are on — Discord doesn't expose
that. So you tell it once, with the `timezone:` option on `/raid create`:

| Region | Realm clock | Tracks |
|---|---|---|
| `EU` | CET/CEST | Europe/Paris |
| `NA` | CST/CDT | America/Chicago |
| `KR` | KST | Asia/Seoul |
| `TW` | UTC+8 | Asia/Taipei |
| `OCE` | AEST/AEDT | Australia/Sydney |

Those are Blizzard's gameplay regions. Oceanic realms technically live inside
the Americas region but keep Sydney time, so they get their own entry.

If your realm's clock doesn't match its region — or you just want to be
explicit — any IANA name is accepted: `Europe/London`, `America/New_York`,
`America/Sao_Paulo`. `US-East`, `US-West`, `US-Central`, `CN` and `BR` also
work as shorthands; they're just not cluttering the picker.

**You only set it once.** Each raid stores its own region, and a new raid
defaults to whatever your last one used — so one shared bot instance can serve
an EU guild and a US guild at the same time without either seeing the other's
clock. Got it wrong? **Raid settings → Edit title / time** fixes it, and the
board re-renders.

These are real timezones, not fixed offsets, which is why a raid booked for
19:00 is still 19:00 after the clocks change in spring and autumn.

Under the hood a raid is stored as a single UTC instant. The board prints that
instant twice: once in your realm's time (the same for everybody, so the guild
has one time to agree on) and once through Discord's own `<t:…>` markup, which
each person's client renders in their own local timezone. Nobody converts
anything by hand.

## Tuning the buff list

Everything is data-driven in `bot/data/buffs.py` — a buff is a label, an icon, a
category and a predicate over specs. Tuning for a new patch means editing that
one file.

The Grip → Mass Grip behaviour comes from the `Upgrade` field on a `BuffDef`:
a strictly better version provided by a narrower set of specs, which replaces
the base entry when anyone on the roster can bring it. Add your own the same way.

## Icons

`python -m tools.fetch_icons` pulls every icon from the Wowhead CDN, resizes to
64×64 PNG, and sorts them:

```
assets/icons/
  tank/    6 specs      healer/  7 specs
  melee/  13 specs      ranged/ 14 specs
  class/  13            role/    4        buff/  19
```

Slugs are verified at fetch time: each may declare fallbacks in `ALIASES`, the
first HTTP 200 wins, and anything unresolvable is reported instead of silently
shipping a broken emoji. Re-running prunes files whose slug has changed.

Two helpers for when a patch moves things:

```bash
python -m tools.probe_icons      # test candidate slugs for existence
python -m tools.contact_sheet    # render every icon into one labelled PNG
```

A 200 only proves a file exists, not that it's the right art — the contact sheet
is how you check that by eye.

## Tests

```bash
python -m tools.smoke_test     # data integrity, buff maths, store, embed rendering
python -m tools.import_check   # imports, custom_ids, Discord's 25-option/5-row limits
python -m tools.web_check      # link signing, auth, routes and mutations
```

All three run offline with no token. `tools.web_check` drives the real aiohttp
app against a temporary database and a stand-in bot.

```bash
python -m tools.build_preview  # writes preview/roster-preview.html
```

An interactive preview of the manager page built from the real stylesheet and
the real client script with the network stubbed, so it can't drift from what
the bot serves. Open it in a browser — no bot, token or server needed.

## Layout

```
bot/
  __main__.py        entry point
  client.py          the bot: command sync, persistent views, lifecycle
  config.py          .env-backed settings, realm-region timezones
  store.py           SQLite: profile cache, raids, signups, reminder claims
  emojis.py          application-emoji upload + lookup
  data/specs.py      40 specs -> class, role, icon, colour
  data/buffs.py      buff/debuff/utility rules and coverage evaluation
  data/targets.py    role targets and the combined-DPS mode
  ui/common.py       permissions, URL validation, mention policy, refresh
  ui/embeds.py       roster embed + buff panel + Discord limit guards
  ui/panel.py        persistent Apply/Bench/Absent/Withdraw/admin buttons
  ui/apply.py        modal + two-step class→spec picker + profile cache
  ui/admin.py        roster manager and raid settings
  ui/schedule.py     time/duration parsing, autocomplete, reminders
  cogs/raid.py       slash commands
  web/tokens.py      signed, expiring, single-raid links
  web/server.py      aiohttp app: auth, JSON state, roster mutations
  web/page.py        the manager page — one file, no build step
tools/               icon fetch/probe/contact-sheet, smoke test, import check,
                     web check, UI preview builder, board refresher,
                     db inspector, timezone explainer
```

## Security notes

The bot assumes raid titles, character names, notes and logs links are hostile
input — anyone in a guild it joins can put text into them.

- **No message can ping `@everyone` or a role.** Raid titles are admin-supplied
  free text that reaches message *content* in reminders. A single
  `AllowedMentions` policy (`bot/ui/common.py: SAFE_MENTIONS`) is set both
  client-wide and explicitly at that call site.
- **Logs URLs are allow-listed**, not merely checked for a substring. The
  pattern pins the host to `warcraftlogs.com` and forbids `()<>[]`, so a URL
  cannot close the surrounding `[name](url)` markdown and inject its own link.
  `javascript:` and lookalike hosts like `warcraftlogs.com.evil.example` are
  rejected.
- **Player-supplied text is escaped** with `discord.utils.escape_markdown`
  everywhere it is interpolated into markdown.
- **Admin gating** is Discord's Administrator permission plus any role named in
  `ADMIN_ROLES`, checked on the command, on the button, *and* in each view's
  `interaction_check` — a stale ephemeral panel cannot be reused by a
  non-admin.
- **No privileged intents.** The bot cannot read message content or the member
  list.
- **Manager links are signed, scoped and re-checked.** HMAC-SHA256 over
  `raid_id.user_id.expiry`, verified with a constant-time compare, so none of
  the three can be edited by the holder. Admin status is re-checked against
  Discord on every request rather than trusted from when the link was issued.
- **The manager page never leaks its own URL.** The token is in the path, so
  every response sets `Referrer-Policy: no-referrer` and outbound links carry
  `rel="noreferrer"` — otherwise clicking a Warcraft Logs link would hand
  warcraftlogs.com a working admin URL. Request paths are kept out of the
  access log for the same reason.
- **The page renders user text as text.** Character names and notes are written
  with `textContent`, never `innerHTML`, under a nonce-based CSP that blocks
  inline and third-party script outright.
- **An unauthenticated request allocates nothing.** The signature is checked
  before any other work, and the flood guard is keyed on the verified
  `(raid, user)` pair. Keying it on the URL instead would mean every forged
  token opened its own bucket — so the guard would never fire for the flood it
  exists to stop, while the bucket table grew until the process ran out of
  memory. Buckets are also pruned, so they can't accumulate over a long uptime.
- Only `.env` holds secrets and it is gitignored; `.env.example` is the
  template.

Found a security issue? Open a
[private security advisory](https://github.com/MagdyAboYoussef/wow-raid-bot/security/advisories/new)
rather than a public issue.

## Notes

- **Warcraft Logs** links are validated and stored, not scraped. Adding parse
  percentiles means a WCL API v2 client (id + secret) and an OAuth layer; the
  schema already has a home for it.
- Discord's embed limits (1024/field, 256 title, 4096 description, 6000 total)
  are all enforced before sending — exceeding any one makes the API reject the
  whole message, which would freeze the board rather than truncate it. Long
  rosters truncate with `…+N more`; if the total is still too large, the side
  queues are dropped before the comp and buff panel.
- Discord caps a select at 25 options, which is why spec picking is class-first
  (13 → ≤4) and the roster manager has a status filter.
- Reminder dispatch is claimed atomically in SQLite, so a restart mid-evening
  cannot re-ping a roster; rescheduling a raid re-arms them.
- The bot writes SQLite in **WAL** mode. Don't inspect the database with a
  read-only connection — it cannot attach the `-shm` index and will show you a
  stale snapshot. Use `python -m tools.inspect_db`.
