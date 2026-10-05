# ops/ — deployment, rollback, backup

Owned exclusively by **deploy-engineer** (plan §4). Disjoint from `ledger/`,
`api/`, `app/`, `tests/`, `run_gate.sh`. Nothing here is imported by the
application; these are operator procedures.

**Version control exists now — as of 2026-10-04, not when this file was
written.** Measured 2026-10-05: the dev machine has `~/.local/bin/git`
(2.53.0, a root-free extracted `.deb` — there is still no *system* git, and
`sudo` needs a TTY), and the prod host has `/usr/bin/git`.
`scripts/git-sync.sh` (written 2026-10-04 23:07) snapshots the working tree
into commits.

**None of that changes a procedure below. A commit is not a release.** The
deploy path transfers directories, systemd runs `/srv/pocketful/current`, and
no commit has ever been deployed to the host. The rollback is still a preserved
release directory — *not* `git revert` — and the working tree is still the
truth for what gets staged. Every procedure worth keeping still lives here as a
file rather than in a transcript.

## Host facts (observed, not assumed)

| Thing | Value |
|---|---|
| Target hostname | `pocketful.getn.space` — only this name |
| Resolves to | `52.206.33.138` (`ssh pocketful-prod`) |
| Storage | `/dev/root` **ext4** on `/dev/nvme0n1p1`, instance-local |
| `/tmp` | **tmpfs (RAM)** — never put state here |
| SQLite CLI | **not installed** — drive everything through the `sqlite3` module |
| Python | 3.14.4 at `/usr/bin/python3`; sqlite library 3.46.1 |

`getn.space` and `www.getn.space` serve a **different live application**. Its
nginx `server` block, document root, certificate and files are never edited.

## Layout

```
/srv/pocketful/releases/<rev>/     immutable release tree (ledger/ api/ app/)
/srv/pocketful/current -> releases/<rev>    what systemd runs
/var/lib/pocketful/pocketful.db    ledger database
/var/lib/pocketful/ui-pending.json client pending-key store
/var/lib/pocketful                 mode 750, owner-and-service only
```

Both `/srv/pocketful` and `/var/lib/pocketful` are on the ext4 root volume.
The **store file is state, not litter**: it holds the client's pending
idempotency keys. If it is lost, the next retry mints a fresh key and a fresh
key is a brand-new transfer — a double-spend that arrives past every ledger
invariant, because it is a filesystem failure rather than a ledger one.
**No boot step may "clean up" a stale store.**

## Tree pin — reproduced on the host

The T7 pin covers `tests ledger api app` **as they exist on the dev machine**.
It will not reproduce on the host, and the reason is structural, not a content
difference:

1. `tests/` is not shipped (the release tree is `ledger api app` only), so the
   file set differs.
2. `sort` collates differently — under a UTF-8 locale `api/__init__.py` sorts
   after `app.py`; under `LC_ALL=C` it sorts before. The pin pipeline pipes
   `sort` into `sha256sum`, so collation changes the digest.

To compare trees across machines, fix the collation and the file set:

```sh
find ledger api app -type f -not -path '*/__pycache__/*' | LC_ALL=C sort \
  | xargs sha256sum | sha256sum | cut -d' ' -f1
# three trees (what the host has), dev and host both -> b9a855ff0d8125af55b977af7cf7bf9054ac4f94b98350055c9bc0f9fddc14db
# four trees (adds tests), LC_ALL=C             -> f67f884681f992c48276cc7e56dc47386c14f21f2de6c24e8892a8d4aed641bc
```

The pin pipeline sorts **filenames**, then hashes each, then digests the lines.
Sorting the already-hashed lines instead is a different function and yields a
different value — that mistake produced `40b89ab9…` once and it is not the pin.
Comparing `(hash, path)` pairs with `LC_ALL=C sort` is the check that actually
proves the deployed bytes equal the gated bytes.

## Two-process shape

```
python3 -m api     --db /var/lib/pocketful/pocketful.db --host 127.0.0.1 --port 8001
python3 -m app.web --api http://127.0.0.1:8001 \
                   --store /var/lib/pocketful/ui-pending.json \
                   --account <id> --host 127.0.0.1 --port 8080
```

`api/` is the money surface and stays on **loopback**. Only `app.web` faces the
vhost. Port 8000 belongs to the other application — never use it.

### Connection lifecycle (why there is no fd to inspect)

`api/app.py` uses a thread-local `LedgerPool` and closes the calling thread's
connection in a `finally` on every request. So the API process holds **no**
database descriptor at rest — `ls -l /proc/<pid>/fd` shows only stdin/stdout/
stderr and the listen socket. Consequences:

- `synchronous`, `foreign_keys` and `busy_timeout` are per-connection and live
  only for the duration of one request; they cannot be read off the running
  process. They hold by construction — every connection comes from
  `ledger.db.connect()`, the single factory.
- `journal_mode` is a **file** property and can be read from the production db
  with a read-only open: `sqlite3.connect("file:...?mode=ro", uri=True)` then
  `PRAGMA journal_mode` → `wal`. Reading this way modifies nothing.
- Live proof the running service is on WAL: under request traffic
  `pocketful.db-wal` and `-shm` exist, and they are gone at rest. Verified by
  sampling the data dir while driving the live API.

## Preconditions before any deploy

1. A **quoted passing gate** (`run_gate.sh`) for the exact revision, from T7.
   A green run I produced myself is not the gate.
2. A preserved copy of the current release (there is no `git revert`).

## Deploy

```sh
# 1. preserve what is running
REV=$(date +%Y%m%d-%H%M%S)
sudo cp -a /srv/pocketful/releases/current /srv/pocketful/releases/$REV.prev  # if current exists

# 2. transfer the tree and point current at it
#    (from the dev machine; the repo is NOT on the host)
tar -czf - --exclude=__pycache__ ledger api app \
  | ssh pocketful-prod "sudo mkdir -p /srv/pocketful/releases/$REV && sudo tar -xzf - -C /srv/pocketful/releases/$REV"
ssh pocketful-prod "sudo chown -R ubuntu:ubuntu /srv/pocketful && sudo ln -sfn /srv/pocketful/releases/$REV /srv/pocketful/current"

# 3. restart, then prove it from outside
ssh pocketful-prod "sudo systemctl restart pocketful-api pocketful-web"
curl -sS -o /dev/null -w '%{http_code}\n' https://pocketful.getn.space/
```

A process that is running is not a site that is up. Verify over the real public
URL, not from inside the host.

## Rollback

```sh
ssh pocketful-prod "sudo ln -sfn /srv/pocketful/releases/$REV.prev /srv/pocketful/current \
                    && sudo systemctl restart pocketful-api pocketful-web"
```

The previous release is preserved on disk, so this is the way back. Exercise it
once against the real release; a rollback you have not run is not a rollback.

## Backup and restore

With WAL on, the `.db` file alone is **not** a consistent snapshot. Measured on
this host: a plain copy taken while WAL was live restored to
`OperationalError: no such table: ledger_entries` — no schema at all, because
the `CREATE TABLE` statements were still in the `-wal` file. It looks like a
successful copy and restores as an **empty database**.

```sh
python3 ops/backup_db.py backup  --db /var/lib/pocketful/pocketful.db \
                                 --out /var/backups/pocketful/pocketful-$(date +%Y%m%d-%H%M%S).db
python3 ops/backup_db.py verify  --backup /var/backups/pocketful/pocketful-<ts>.db
python3 ops/backup_db.py restore --backup <file> --db /var/lib/pocketful/pocketful.db   # target must not exist
```

`VACUUM INTO` emits one consistent file needing no `-wal`/`-shm` companion.
`verify` runs `integrity_check`, counts every money table, and asserts
`SUM(ledger_entries.amount_minor) == 0`. An unrestored backup is a hypothesis:
restore into a scratch path and verify before trusting one.

## TLS

`pocketful.getn.space` has its **own** certbot lineage
(`/etc/letsencrypt/live/pocketful.getn.space/`), separate from the getn.space
one. It renews via `authenticator = webroot` (`/var/www/certbot`), which does
not rewrite nginx configuration. `certbot.timer` is active.

The vhost declares `listen [::]:443 ssl;` and **omits `ipv6only=on`** — that
option may be set only once per `[::]:443` socket and the getn.space block
already owns it. Redeclaring it makes `nginx -t` fail. Validate after any edit:

```sh
sudo nginx -t && sudo systemctl reload nginx
```

## T16 — shipped 2026-10-04: release `58466a7c`

The T14/T15 revision (create-account, account switching, the demo notice) is live at
`https://pocketful.getn.space/`. Recorded here because there is no version control on
the host and this file is the history.

### The two release-safety records plan §T16 owes

1. **Open registration: ACCEPTED, not rate-limited.** Measured, not assumed — eight
   rapid `POST /create` requests against the live URL returned
   `303 303 303 303 303 303 303 303`, zero `429`. `grep -rn -i
   "ratelimit\|throttle\|429" app/ api/` finds no limiter; the only `429` in the tree
   is `app/client.py:42`, where the *client* treats an API `429` as retryable. So
   anyone who can reach the page can mint accounts into the live database, at line
   rate. Accepted for a play-money demo; flagged here so it is a decision, not an
   oversight.
2. **An account id is a bearer capability — for read *and* write.** There is no
   authentication anywhere in this build. After rule 0 (one shared account resolver),
   `GET /?account=<id>` returns that account's balance and full activity, and
   `POST /send` with `account=<id>` spends from it. The ids are published: the boot
   account's activity table renders counterparty ids on the public page, so anyone who
   loads the demo can read the ids of every account the demo account has transacted
   with, then read and spend from each. An id is the *only* credential and it is not a
   secret. Accepted for this demo, and **stated on the page rather than left implicit**:
   `DEMO_NOTICE` (`app/web.py:106`) is rendered by the single document assembler
   (`_document`) — the only assembler the *application* uses. Measured live on the shipped
   revision (2026-10-04): `/` → `200 text/html`, marker `1`; switched `/?account=<id>` →
   marker `1`; unknown-account page → `200`, marker `1`.
   **Correction, measured 2026-10-04 — the application does not own every HTML response.**
   `PUT` / `DELETE` / `PATCH /` return `501 text/html;charset=utf-8` from
   `BaseHTTPRequestHandler.send_error`, with `grep -c 'Unauthenticated demo'` → **0**; an
   unrouted `POST /nonexistent` returns `404 application/json {"error":"not_found"}`. So
   *"every `text/html` response carries the notice"* is **false on this revision**. The
   markered set is "every document `_document` builds", not "every HTML response the server
   emits". Recorded rather than smoothed over; the remediation is board **T19**
   (`app/web.py` belongs to the frontend-engineer, not to `ops/`).

### Ship evidence (2026-10-04)

- Gate (planner's quoted run, pin re-cut independently here):
  `GATE GREEN / exit 0`, `Ran 72 tests … OK`, phases 1–4, `63/63 checks passed`,
  `TREE PIN (before) == (after) == 58466a7c292159db1cab3d4cca2748927eeef590c29b192c841256df9529a2ee`.
- Staged bytes proven equal to the gated bytes before the swap: 3-tree pin
  `7fe22cc63f5cfe238e90d9688349666742b1996dae4287f8d8f2c622e578fa4a` identical on dev and
  host; `app/web.py` `02eaafddaab9af763bf093d7644c5a65364c22f471911497622840e170348e32`
  identical on dev and host.
- Before (what was live): `dc5e8b0b`, 3-tree pin
  `b9a855ff0d8125af55b977af7cf7bf9054ac4f94b98350055c9bc0f9fddc14db`, `app/web.py`
  `aa2ccccc95…`, 0 notice markers, `POST /create` → `404`.
- After: 1 notice marker on `/`, `POST /create` → `303` (redirect to
  `/?account=<new id>`), `GET /?account=<new id>` renders `Account <code><new id></code>`
  and carries `name="account" value="<new id>"` on the send form, and the boot account
  id appears 0 times — i.e. the page really switched.

### Rollback, exercised — not just named

Rollback target preserved as a byte copy at `/srv/pocketful/releases/dc5e8b0b.prev`
before the swap (`cp -a /srv/pocketful/releases/dc5e8b0b …`, not `cp -a current …` —
`current` is a symlink and `cp -a` would copy the link, not the tree).

```sh
ssh pocketful-prod "sudo ln -sfn /srv/pocketful/releases/dc5e8b0b.prev /srv/pocketful/current \
                    && sudo systemctl restart pocketful-api pocketful-web"
```

Rehearsed this window: after the swap to `58466a7c`, this command reverted the live
site to the old behaviour (0 notice markers, `POST /create` → `404`, `/` → `200`), both
units `active`; the roll-forward
`ln -sfn /srv/pocketful/releases/58466a7c …` restored the new behaviour. A rollback you
have not run is not a rollback.

### The store is state, and the swap did not touch it

`/var/lib/pocketful/ui-pending.json` is service-level state, not per-release: its path
is fixed in `pocketful-web.service` and the release tree is not its parent. Measured
across the whole window — swap, rollback and roll-forward — the file was byte-identical
at every point:

```
sha256  f3e2a029bb1b07ea577703c94599a0d8349a9fa1e761333420c10786a8c217c7
618 bytes  mode 600  mtime 2026-10-02 13:54:52 +0000
```

The `mtime` is the load-bearing part: it predates the deploy window entirely, so
neither restart so much as opened the file for write. Losing this file would make the
next retry mint a fresh key, and a fresh key is a brand-new transfer arriving past
every ledger invariant.

### Edge / TLS — measured against the host on 2026-10-04

`pocketful.getn.space` has its **own** nginx `server` block
(`/etc/nginx/sites-available/pocketful.getn.space`) and its **own** certbot lineage
(`/etc/letsencrypt/renewal/pocketful.getn.space.conf`, `authenticator = webroot`,
expiry `2026-12-31 06:22:08+00:00`). The `getn.space` block is separate and was not
touched. `certbot.timer` is `enabled`/`active`. The subdomain block proxies to
`127.0.0.1:8080`; the ledger API stays on `127.0.0.1:8001`; port `8000` belongs to the
other application. **This deploy changed no nginx config, no TLS, no DNS.**

> Note recorded 2026-10-04: an earlier operator briefing asserted there was only one
> certbot lineage and no subdomain `server` block. That is not what the host shows
> today — the subdomain block and lineage were created `2026-10-02 07:20`. Measure the
> topology; do not inherit it.

### Post-ship divergence — measured, not predicted (2026-10-04) — **CLOSED** by the final ship below

**The host still serves exactly the gated revision.** Measured after the record edits:

```
host /srv/pocketful/current 3-tree pin : 7fe22cc63f5cfe238e90d9688349666742b1996dae4287f8d8f2c622e578fa4a
host app/web.py                        : 02eaafddaab9af763bf093d7644c5a65364c22f471911497622840e170348e32
```

Both equal the values staged and verified *before* the swap, so everything written since landed
outside `/srv/pocketful/releases/58466a7c` — nothing has rewritten the release.

**The dev working tree has moved**, during this window and not by `ops/`: `app/web.py` is now
`686368f3bfe95fd3c03589b0510cc9420f969bd16553cd9f0907f61e0386772c` (was `02eaafdd…`) and
`app/selfcheck.py` is now `79bb974477b5ea1e453083aeb4490bbfe0dc2521d785d1103b2decd90be9d957`.
So the pins are now:

| pin | shipped | now |
|---|---|---|
| 4-tree (gate) | `58466a7c…` | `aefd7e04dc998bd881c1b101ccbc050b65cc37c92e216450b164ae4a5bb4e9d3` |
| 3-tree (release) | `7fe22cc6…` | `04c16b73699da3e52147be8f7b995b88ae94d6abfe2c0d6793df91cacbaf2f45` |

`app/` is the released tree, so the live release `58466a7c` **no longer equals the working
tree** — that is now a measurement, not the prediction it was an hour ago. T18's `tests/` half
moves the gate pin only; the `app/` change above moves both.

**Consequences, so the next operator does not rediscover them:**

- The live site **is still the gate-certified revision.** The divergence is in the working tree,
  not on the host — nothing here invalidated the deploy.
- **Do not deploy for a docstring or a row label.** The next ship is a **new** revision with a
  **new** pin, gated afresh from the settled tree — never a re-cut of `58466a7c`, and never a
  copy of whatever the working tree happens to hold.
- Rollback target unaffected: `/srv/pocketful/releases/dc5e8b0b.prev`.

### Probe accounts left in the live DB by the T16 ship (disclosure)

`POST /create` is the DoD's own probe, so the ship necessarily minted accounts. All are
play-money demo accounts, zero balance, no keys:

- this ship: `probe`, `t16probe` (`fe9434dedb524a4da68c692810f89443`),
  `ratelimit-probe-1` … `ratelimit-probe-8`
- the planner's independent verification probe: `planner-probe-4`
  (`0e3352cd92ae4ce8842549b3f9a803bc`)
- the final ship: `final-ship-probe`, `rollback-rehearsal-probe`

### Final ship 2026-10-04 — divergence CLOSED at `6c766987`

T18 and T19 landed, so the tree moved onto the corrections and the site was shipped onto it. Site
and tree now agree.

| | revision |
|---|---|
| 4-tree gate pin | `58466a7c…` → **`6c766987d2cb2909be91bff5cdb9b8e342386104da83038693cedf8267ef4d73`** |
| 3-tree release pin | `7fe22cc6…` → **`04c16b73699da3e52147be8f7b995b88ae94d6abfe2c0d6793df91cacbaf2f45`** |
| `app/web.py` | `02eaafdd…` → **`686368f3bfe95fd3c03589b0510cc9420f969bd16553cd9f0907f61e0386772c`** |
| release dir | `58466a7c` → **`6c766987`** (named for the new pin, never a re-cut) |
| armed rollback | **`/srv/pocketful/releases/58466a7c.prev`** (byte copy of the previous live release, pin `7fe22cc6…`) |

- Gate, **run by the deploy-engineer** on the settled tree: exit 0, `GATE GREEN`; 1/4
  `Ran 72 tests in 13.088s … OK`; 2/4 compile clean; 3/4 `lint: checked 21 modules … clean`;
  4/4 `63/63 checks passed`; `TREE PIN (before) == (after) == 6c766987…`.
- **The gate log contains 15 `FAIL` rows and is still green.** That is not a contradiction and it
  must not be judged by `grep -c FAIL`: `run_gate.sh` records each phase's `$?` into `STATUS` and
  exits 0 only if all four returned 0, and the rows come from the suite's deliberate mutant
  drivers (the never-records-keys ledger, `test_the_mutant_only_disturbs_rows_downstream_of_a_refusal`,
  the mutated-notice-wrapper witness). Read the criterion out of the script; do not apply a
  remembered rule about it.
- Staged bytes proven equal to the gated bytes **before** the pointer moved: 3-tree pin and
  `app/web.py` digest identical dev vs host.
- Live after: `GET /` → `200 text/html`, notice markers `1`; `POST /create` → `303`;
  **served `app/web.py` digest == the tree's `686368f3…`** — the point of the ship.
- Rollback **rehearsed against the new target**: `current → 58466a7c.prev` served the old bytes
  (`app/web.py 02eaafdd…`) with both units `active`; the roll-forward restored `686368f3…`.
- Store `/var/lib/pocketful/ui-pending.json` **unchanged across the whole window** — swap, revert
  and roll-forward — at digest `470590c509a063f6193a7362333be2b57e01b3a5dea5d13e91c2edba68585001`,
  892 bytes, mode 600, **mtime `2026-10-04 08:34:35`**, which predates the window.

**One observation worth keeping, because it looks like a defect and is not.** At the T16 ship the
store was 618 bytes / mtime `2026-10-02 13:54:52`; by the time this ship's baseline was taken it was
892 bytes / mtime `2026-10-04 08:34:35`, and `pocketful.db` had been written a minute earlier. That
change happened **before** this window opened and was not caused by the gate or by any test:
nothing outside `ops/` references the live paths (tests build their store under `TemporaryDirectory`;
`app/web.py` defaults `--store` to a *relative* path), and `run_gate.sh` writes only a throwaway
fixture under `TMPDIR`. The coherent explanation is **live traffic through the public site** — a
send writes the ledger and mints a pending key. That is the application working, and the store
holding a pending record is exactly the state the T16 rehearsal proved survives both a swap and a
revert. **No boot step may clean it up.**

### Divergence reopened 2026-10-05 — the tree has moved on; the live release has not

Both pins recomputed here, not quoted:

| pin | live release `6c766987` | working tree now |
|---|---|---|
| 3-tree (release) | `04c16b73699da3e52147be8f7b995b88ae94d6abfe2c0d6793df91cacbaf2f45` | `a7f269cb5a1d9b36260570e686e1e559cd7ede5e32e1a15ecde832954175cbab` |
| 4-tree (gate) | — (`tests/` is never shipped) | `6b249d0f998a6629acb92d229a0e74c9365718c91da7223aac77eead2b0d3956` |

Since the final ship, `api/`, `app/` and `tests/` have all changed: the
client-supplied account id (§C2.1), the opening grant, the activity owner
column. `tests/` now holds **100** test definitions, against 72 at that ship.

**This is the normal state between ships, not a defect.** The site is correct
for the revision it runs; it is simply behind. The rules are unchanged: the
next ship is a **new** revision, gated afresh from the settled tree — never a
re-cut of `6c766987`, never a copy of whatever the working tree happens to
hold.

**One consequence to name:** nothing added since the final ship is on the site.
Anyone verifying a change through a browser is verifying `6c766987`.

### `ops/smoke.sh` was broken by §C2.1, and is fixed here

The pre-gate smoke test POSTed `{"owner_id","currency","allow_overdraft"}` with
no `account_id`. Once §C2.1 made the id required and client-supplied, the API
answered `400 {"error":"invalid_request"}`, the parser read an empty id, and the
script died at line 40 — **on every run, before reaching the app at all**.

The body now sends a **per-run unique** id. A fixed one would have been worse
than the bug: because the id is the ledger's exactly-once guard, a literal
`acct-smoke` passes once and then returns `409 {"error":"account_exists"}`
forever. The script also asserts the id comes back **unchanged**, since a server
that quietly mints its own is the regression this test exists to catch.

Verified 2026-10-05 on the current tree: two consecutive runs, `exit=0` both,
two distinct accounts. And verified to *have teeth* — against a copy of `api/`
patched to ignore the caller's id, it exits 1 with
`SMOKE FAIL: asked for acct-smoke-…, got server-minted-id`.

Note the script opens one throwaway account in whatever database it is pointed
at and does not remove it. Running it with the default `DB` writes a
grant-carrying row to **production**.

### T26 ship 2026-10-05 — revision `5df83d92`, divergence CLOSED

| | |
|---|---|
| 4-tree gate pin (dev-only) | `5df83d92820d79edec518482150d46ff7b7295c1f561ad576c3a13990ee372f1` |
| 3-tree release pin | `559481459beac10b18b01059f9f4a978b1c422b6842fcf883e21bed720570243` |
| release dir | `/srv/pocketful/releases/5df83d92` |
| superseded | `6c766987` → preserved as `6c766987.prev` |
| armed rollback | `/srv/pocketful/releases/6c766987.prev` |

**Binding.** The four-tree pin was recomputed here and matched the gated value exactly, and all
seven per-file digest prefixes matched, so the staged revision *is* the gated revision. The fix
this revision carries was read rather than assumed: `app/client.py:125` and `:131` both carry
`quote(account_id, safe='')`.

**Both pins matter and neither substitutes for the other.** The host has no `tests/`, so the
four-tree value **cannot** be reproduced there — asking for it would manufacture a false mismatch
that looks like a moved tree. Compare the three-tree value on the host.

**Staged bytes == gated bytes** before the pointer moved (3-tree pin identical, dev and host).

**Live after, measured from outside:**
```
GET https://pocketful.getn.space/          -> 200  text/html; charset=utf-8  13399 bytes
served /srv/pocketful/current/app/web.py   -> 6c452b5501697d28…   (== the tree's)
```

**The fix exercised end to end on the live site.** An account id that genuinely needs encoding —
`probe 5df8/1`, containing a space and a slash — was created and read back:
```
POST /create     -> 303   Location: /?account=probe%205df8%2F1
GET that URL     -> 200, title "Pocketful — probe 5df8/1",
                    name="account" value="probe 5df8/1", $100.00, __system__ opening-grant credit
control: ?account=definitely-not-real -> 200 but 10112 bytes (the unknown-account state)
```
So both patched call sites are covered by a live request: `:125` by the balance, `:131` by the
activity. Without the fix the slash would have split the path. Note the control: an unknown
account also returns `200`, just a smaller page — status alone would not have distinguished them.

**Rollback rehearsed against this ship's own armed target**, not cited from a previous ship:
```
current -> 6c766987.prev   served app/web.py 686368f3…   units active   GET / 200
current -> 5df83d92        served app/web.py 6c452b55…   units active   GET / 200
```

**Store untouched across the whole window** — swap, revert and roll-forward:
```
sha256 470590c509a063f6193a7362333be2b57e01b3a5dea5d13e91c2edba68585001
892 bytes  mode 600  mtime 2026-10-04 08:34:35 +0000
```
Identical to the pre-window baseline, with the mtime predating the window entirely — so neither
restart so much as opened it for write.

**Probe account left by this ship:** `probe 5df8/1` (the fix's own verification), carrying the
§C2.3 opening grant. Play-money demo account, disclosed like the others.

No nginx, TLS or DNS change in this deploy. `getn.space` and `www.getn.space` both still answer
`200`. **The 2026-10-05 divergence recorded above is CLOSED by this ship.**
