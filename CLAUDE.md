# CLAUDE.md — codebase orientation

Read this before touching the code. It's written to get you (Claude)
productive in one pass without re-deriving the architecture from scratch.

## What this is

A small, single-purpose **aiohttp + SQLite** web app for running live STAR
(or Score) voting elections among a group of people in the same room/chat,
with real-time updates via Server-Sent Events. No build step, no frontend
framework, no ORM. Everything fits in a handful of files; read the actual
source rather than trusting any summary that drifts from it, but here's the
map.

## Verifying changes — do NOT run the server yourself

This app only really runs against a real, shared `voting.db` on a remote
machine (see `push-to-production.sh`, gitignored). There's no meaningful
local dev/test instance: starting `python3 server.py` here just binds a
port and creates a throwaway `voting.db` unrelated to the actual
deployment — that's not how this project gets verified.

**Don't start the server yourself to test a change, and don't try to.**
Instead:
- Syntax-check what you touched (`python3 -m py_compile server.py
  database.py`; for the templates, extract the `<script>` block and run
  `node --check` on it, or similar).
- Read through the change for correctness rather than exercising it live.
- Tell the user what you changed and ask them to push to production
  (`push-to-production.sh`) and try it there themselves — that's the real
  verification step, and it's theirs to run, not yours.

## File map

| File | Role |
|---|---|
| `server.py` | Everything: the `VotingServer` class — routes, handlers, STAR/Score algorithms, SSE broadcast. ~1600 lines, one class. Start here. |
| `database.py` | `init_db()` (schema, idempotent `CREATE TABLE IF NOT EXISTS`) + small pure query helpers (`get_setting`, `current_round`, `eligible_candidates`, `voter_has_voted`, `eliminated_ids`). No business logic lives here — that's all in `server.py`. |
| `templates/index.html` | The entire voter-facing app: one HTML file with inline `<style>` and inline `<script>`. No build step — edit it directly, reload the page. |
| `templates/admin.html` | The entire `/admin` page, same single-file-with-inline-JS style. |
| `test_data.py` | `generate_test_candidates(n)` — used by the admin "Generate Test Ballot" button to seed fake candidates for trying out the UI. |
| `sets_tool.py` | CLI for import/export of "candidate sets" (see below), bypassing the HTTP API. |
| `snapshot.py` | CLI that opens the DB **read-only** and dumps a JSON snapshot of the current election state — for backups/debugging without touching the live DB. |
| `instructions.txt` | The original product spec this app was built from. Still mostly accurate; treat as historical context, not a live source of truth — `server.py` wins on any conflict. |
| `NGINX.md` | How to reverse-proxy this under a path prefix (e.g. `/voting/`) via the `--prefix` CLI flag. Relevant if you touch `_make_base_middleware`. |
| `push-to-production.sh` | Deploy script (gitignored). |

There is no test suite. Changes are verified by syntax-checking and then
asking the user to push to production and test there (see "Verifying
changes" section above).

## Core data model

Everything lives in one SQLite file (`voting.db` by default, gitignored).
`database.get_db()` opens it with `row_factory = sqlite3.Row` (so rows are
dict-like: `row["title"]`) and foreign keys on.

- **`settings`** (key/value) — single source of truth for election-wide
  config: `n_winners`, `voting_mode` (`star`|`score`), `election_title`,
  `election_state`, `entry_context` (free-text shown to voters), `show_author`.
  Read via `database.get_setting(db, key, default)`; written via
  `INSERT OR REPLACE INTO settings VALUES (?, ?)`. **Add new global toggles
  here, not as new tables**, unless they need their own rows (see
  `candidate_sets` below for when a new table *is* right).
- **`candidates`** — the things being voted on this round: `title`, `body`
  (markdown), `author`, `image_path`. This table is the live, mutable pool.
  Deleting a candidate is blocked once it has votes (`_admin_delete_candidate`).
- **`voters`** — registered participants (`first_name`, `last_name`,
  `name_lower` unique). Name is the *only* identity concept in this app —
  there's no password/session for voters, just case-insensitive name
  matching, persisted in the browser via `localStorage`.
- **`rounds`** / **`votes`** / **`ballots`** / **`winners`** — the live
  state of the *current* election: which round we're on, every
  voter→candidate→score triple, who has submitted a ballot this round, and
  the winner(s) revealed so far (with STAR runoff detail baked in as
  `finalist1_id`/`finalist2_id`/etc.). `winners` accumulates one row per
  round in N-sequential-rounds mode.
- **`elections`** / **`election_results`** / **`election_ballots`** —
  *historical* snapshots. When `/api/admin/reset` is called and `winners`
  is non-empty, the current election is archived here (title, per-place
  results, full per-voter ballot grid) before everything live gets wiped.
  This is what powers the "Election History" card on `/admin`.
- **`candidate_sets`** / **`candidate_set_items`** — reusable, named lists
  of candidates an admin can save/load (e.g. "Team Lunch Options") without
  re-typing them each time. Independent of the live `candidates` table
  until explicitly "loaded".
- **`candidate_entries`** — defined in the schema, **currently unused** by
  any code in `server.py`. Don't assume it's wired up to anything; if you
  need it, you're building the wiring, not relying on existing wiring.

`database.init_db()` is called once at server startup and is safe to call
against an existing DB file (every `CREATE TABLE` is `IF NOT EXISTS`) — this
is the *only* migration mechanism. If you add a column to an existing
table, `IF NOT EXISTS` won't help existing DBs; you'd need an `ALTER TABLE`
guarded by a `try/except` or a `PRAGMA table_info` check. Check how new
tables/settings were added historically (git log) before inventing a new
pattern.

## Request lifecycle

- `VotingServer.__init__` builds the `aiohttp.web.Application` with two
  middlewares (`_make_db_middleware`, `_make_base_middleware`) and calls
  `database.init_db()`.
- `_make_db_middleware` opens a fresh `sqlite3.Connection` per request,
  stashed at `request["db"]`, closed in a `finally`. Every handler pulls
  `db = request["db"]` — there's no connection pooling, this is a
  low-concurrency internal tool.
- `_make_base_middleware` rewrites outgoing HTML when `--prefix` is set
  (reverse-proxy support) — injects a `<base href>` tag and monkey-patches
  `fetch`/`EventSource` in a `<script>` so relative API calls still hit the
  right path. Only touch this if you're changing prefix/proxy behavior.
- Routes are registered in `_setup_routes()` — it's the index of every
  endpoint that exists. Read it top-to-bottom before adding a new route so
  you place it sensibly and don't duplicate.

## State broadcasting (SSE)

- `_sse_clients: dict[name_lower -> asyncio.Queue]` — one queue per
  connected voter (keyed by their lowercased name), filled by
  `_broadcast(event_type, data)`.
- `GET /api/stream?voter_name=...` is a long-lived `text/event-stream`
  response: sends an initial `state` event, then relays whatever lands in
  that voter's queue, with a 20s keepalive comment if idle.
- Any handler that mutates election-visible state must call
  `await self._broadcast("state_update", self._build_state(db))` (or
  `"voters_update"` for just the voter list) after committing — this is how
  every connected browser tab stays in sync without polling. **If you add a
  new mutating endpoint, you almost certainly need this call at the end.**
- `_build_state(db)` is the single function that assembles everything the
  frontend needs to render: settings, current round, eligible candidates
  (markdown already rendered to HTML server-side via `python-markdown`),
  voters + their voted/active status, and resolved winner records. This is
  also literally the JSON served by `GET /api/state`. **Anything sensitive
  must never go in here** — it's pushed to every connected browser tab,
  admin or not.

## Voting algorithms

- `_compute_star_winner`: sum all scores per eligible candidate → top 2 by
  total go to an automatic runoff → whichever more voters strictly
  preferred wins (tie → higher total score wins, since it's picked
  first/equal in the `>=` comparison). Special-cased for exactly 1 eligible
  candidate (trivial winner, no runoff needed).
- `_compute_score_winners`: just sorts by total score and takes top N — no
  runoff. Used by `voting_mode = 'score'` for "pick the top N candidates in
  one shot" elections (no sequential rounds).
- `_admin_reveal_winner` dispatches on `voting_mode`, writes to `winners`,
  and — in STAR mode — either opens the next round (status `'voting'`) or
  marks the round `'complete'` once `len(winners) >= n_winners`.

## `election_state` — the secondary state machine

Independent of `rounds.status` (which only tracks STAR-round progression
within an active vote), `settings.election_state` gates what the voter page
shows at all:
- `ELECTION_ACTIVE` — normal voting is open.
- `ELECTION_INACTIVE` — voting paused/closed; voter page shows a "not open"
  notice.
- `CANDIDATE_ENTRY_NAMES` — an "open submission" phase: the voter page
  shows a text box (`/api/entry`) instead of a ballot, and every submission
  becomes a new row in `candidates` directly (no validation beyond
  non-empty). `index.html`'s `isEntryMode()` checks
  `election_state.startsWith('CANDIDATE_ENTRY_')` — i.e. this is meant to be
  a *family* of entry-phase states, not just one. **If you're adding a new
  open-submission-style phase, prefer extending this pattern** (a new
  `CANDIDATE_ENTRY_*`-shaped or sibling state value handled by
  `isEntryMode()`) over inventing a parallel mechanism.

## Auth — read carefully, it's inconsistent on purpose

There are **two unrelated gates**, both fairly informal (this is a trusted
internal tool, not a public service):
1. **"Mason Hieb" voter-identity gate** (`_require_mason` /
   `MASON_FIRST_NAME`/`MASON_LAST_NAME`): used by `/api/admin/settings`,
   `/api/admin/reveal`, `/api/admin/reset`. These expect the POST body to
   contain `voter_name: "Mason Hieb"` — there's no real secret here, it's
   just checking *who you claim to be*, matching the product idea that only
   the person who registered as "Mason Hieb" on the voter page can do these
   actions from that page. `admin.html`'s JS literally hardcodes
   `voter_name: 'Mason Hieb'` in these requests, since the admin page is a
   second front-door to the same actions.
2. **Admin password gate** (`ADMIN_PASSWORD = "hunter2"` — yes, hardcoded,
   change it if this ever leaves a trusted network): `/api/admin/login`
   checks it and the admin page stashes it in `sessionStorage`;
   `/api/admin/unsubmit` is the only other endpoint that actually
   *re-checks* it server-side. Most other `/api/admin/*` endpoints (add
   candidate, manage sets, import voters, etc.) have **no server-side check
   at all** — the password prompt on `/admin` is the only thing standing in
   front of them, which is enforced purely client-side. Don't assume an
   `/api/admin/...` path is authenticated just because it lives under that
   prefix — check the handler.

If you add an endpoint that should be gated, pick whichever of these two
patterns fits (identity-style if it's something the "Mason Hieb" voter
persona should trigger from the main page; password-style if it's
admin-page-only and touches something sensitive) rather than inventing a
third.

## Frontend conventions

Both templates are single self-contained HTML files: CSS in a `<style>`
block using CSS custom properties for the dark theme (`--bg`, `--surface`,
`--primary`, etc. — reuse these, don't hardcode colors), then markup, then
one `<script>` block of vanilla JS (no framework, no bundler).
- `index.html` keeps a module-level `state` object refreshed by SSE events
  and a `renderAll()` that re-renders every section from it — when you add
  a new piece of server state, thread it through `_build_state` →
  `renderAll`'s sub-functions, don't bolt on a separate fetch.
- `admin.html` is more traditional fetch-on-demand per card (no SSE) —
  each card's data is loaded by its own `loadX()` function called from
  `initAdmin()`.
- Both files have a local `esc()` helper for HTML-escaping interpolated
  text — always use it for any user-supplied string before putting it in
  `innerHTML`.

## Company Codenaming mode

A second, opt-in workflow layered on top of the standard election machinery.
When `app_mode = 'codenaming'` (settings table), the admin flow becomes:
**configure company name → open submissions → STAR vote → reveal → finish
round**, cycling per company. The full design is in
`COMPANY_CODENAMING_DESIGN.md` (gitignored); this section is the quick
orientation for code changes.

**New settings keys** (all via the existing `get_setting` / `INSERT OR
REPLACE` pattern):
- `app_mode` — `'standard'` (default) | `'codenaming'`
- `codename_company_name` — **private**, the real company name being codenamed
  right now. **Never add this to `_build_state()`** — it must never reach
  the public `/api/state` or SSE stream.
- `codename_enforce_letter` — `'1'`/`'0'`, whether submissions must start
  with the company's first letter.

**New table: `selected_codenames`** — permanent, deduplicated archive of
`(codename TEXT UNIQUE, company_first_letter TEXT)`. Codenames are stored
lowercase; no company name is ever written here.

**New `election_state` value: `CODENAME_SUBMISSION`** — the open-entry phase.
`index.html`'s `isEntryMode()` already handles it (the check covers both
`CANDIDATE_ENTRY_*` and `CODENAME_SUBMISSION`).

**New public endpoint:** `POST /api/codename/submit` — mirrors `_submit_entry`
but adds letter-match and reuse validation before inserting into `candidates`.

**New admin endpoints** (all password-gated like `_admin_unsubmit`):
- `GET  /api/admin/codename` — company name, enforce flag, full codename history
- `POST /api/admin/codename/configure` — save company name + enforce toggle
- `POST /api/admin/codename/open-submissions` — set `election_state = CODENAME_SUBMISSION`
- `POST /api/admin/codename/start-voting` — force `n_winners=1`, `voting_mode=star`, `election_state=ELECTION_ACTIVE`
- `POST /api/admin/codename/finish-round` — archive round to election history, wipe candidates + votes + rounds + winners + company name, set `election_state=ELECTION_INACTIVE`

**`_admin_reveal_winner` hook:** when `app_mode == 'codenaming'`, also
`INSERT OR IGNORE` the winning title (lowercased) + required letter into
`selected_codenames` — the archive happens at reveal, not at finish-round.

**`_archive_current_election(db)`** — shared helper factored out of
`_admin_reset` so both `_admin_reset` and `finish-round` can call it.
`finish-round` then additionally deletes candidates (standard reset never
touches them).

**`app_mode` toggle** goes through the existing `_admin_update_settings`
endpoint (the Mason-gated one), not the password-gated admin endpoints, since
the mode value itself isn't sensitive.

## No em dashes — ever

**Em dashes (`—`, `&mdash;`, `—`) must never appear in any user-visible text in this project** — not in HTML, not in JS strings, not in template literals. This applies everywhere: UI copy, error messages, labels, tooltips, select options, round badges, page titles, and comments that could become visible text. Use a colon, a period, a comma, a semicolon, or a hyphen instead.

**The round badge in `index.html`'s header is especially important** — it is prominent and highly visible. Any text rendered into `#round-badge` or `#vote-heading` must be checked to contain zero em dashes.

If you are about to write `—` anywhere in a template file, stop and use something else.

## Intentional rules — confirm before changing

The following behaviors are deliberate constraints, not bugs. If a user request would remove or weaken them, **stop and ask the user to confirm explicitly** before proceeding.

- **Reveal Winner and Finish Round require `election_state = ELECTION_INACTIVE`.** Both the server-side handlers (`_admin_reveal_winner`, `_admin_codename_finish_round`) and the admin UI enforce this. The admin must explicitly close voting before either action becomes available. Do not relax this check, add bypass flags, or make it conditional without a clear explicit instruction from the user.

## Gotchas worth knowing before you change things

- `_build_state` is sent to **every** connected client regardless of who
  they are — there is no per-viewer filtering. Anything admin-only or
  privacy-sensitive must be fetched through a separate, explicitly-gated
  endpoint instead of riding along in `/api/state`/SSE.
- `_admin_reset` **keeps** `candidates` and `voters` — it only wipes the
  live vote/round/winner state (after archiving to `elections` history).
  This is intentional for the "re-run the same ballot" use case. If a
  future feature needs candidates wiped too, that's a deliberate departure
  from `_admin_reset`, not a bug to "fix" by changing it for everyone.
- `eligible_candidates()` = all candidates minus anything already in
  `winners` — this is how sequential N-round STAR removes the previous
  round's winner from the pool without deleting the candidate row.
  `/api/vote` requires the score payload to cover *exactly* this set.
- Image uploads are resized server-side via Pillow if available
  (`PIL_AVAILABLE` guard) and fall back to storing the raw upload otherwise
  — don't assume Pillow is guaranteed present.
- `requirements.txt` / `README.md` are minimal; there's no `pyproject.toml`
  or lockfile. Check `requirements.txt` for the actual dependency list
  before assuming a package is available.

## Where to look for feature design docs

`COMPANY_CODENAMING_DESIGN.md` (gitignored, local-only) is the full design
doc for the Company Codenaming feature — data model, API surface, privacy
rules, and implementation checklist. The feature is implemented; read the
doc if you need to understand *why* something is the way it is, or are
extending/debugging the codenaming flow.
