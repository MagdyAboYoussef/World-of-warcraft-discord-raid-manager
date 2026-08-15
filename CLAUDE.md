# Working on this repo

## Commit identity

This repository is public, and a commit's author address is permanent: it stays
in the object even after the branch is rewritten, and hosting providers keep
serving unreachable objects by SHA. Treat it as unrecoverable once pushed.

**Only ever commit as `<username>@users.noreply.github.com`.** Never use a
work, employer, or organisation address here — not in `user.email`, not in a
`Co-Authored-By:` trailer, not in a commit message, and not in a file.

The machine-wide git identity is not necessarily the right one, so this repo
sets its own:

```bash
git config user.email 'USERNAME@users.noreply.github.com'
git config user.name  'USERNAME'
```

A `pre-commit` hook enforces this. Turn it on once per clone:

```bash
git config core.hooksPath .githooks
```

It allowlists the noreply form rather than blocking particular addresses —
listing the addresses to avoid would put them in the repository, which is the
thing being prevented.

Worth also enabling **Settings → Emails → Block command line pushes that expose
my email** on the account. That is the backstop that catches a clone where the
hook was never enabled.

## Never commit

`.env`, anything under `data/`, and `preview/` are gitignored and must stay
that way. `data/` holds the live SQLite database and the web signing secret;
leaking the latter would let anyone forge a roster-manager link.

`.env.example` is the template and is committed — keep every value in it blank
or a placeholder.

## Before committing

```bash
python -m tools.smoke_test     # data, buff maths, store, embed rendering
python -m tools.import_check   # imports, custom_ids, Discord's component limits
python -m tools.web_check      # link signing, auth, routes, mutations
```

All three run offline with no token. They are fast; run all three.

Discord's structural limits are the usual source of breakage — 5 components per
row, 5 rows per view, 25 options per select, 25 autocomplete choices, 1024
characters per embed field, 6000 per embed. `import_check` and `smoke_test`
assert these, so a change that violates one fails locally rather than silently
freezing a live raid board.
