# ops/ — deployment, rollback, backup

Owned exclusively by **deploy-engineer** (plan §4). Disjoint from `ledger/`,
`api/`, `app/`, `tests/`, `run_gate.sh`. Nothing here is imported by the
application; these are operator procedures.

There is **no version control** on the dev machine or on the host. The working
tree is the truth and the board is the only history, so every procedure that is
worth keeping lives here as a file rather than in a transcript.

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
