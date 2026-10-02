# Pocketful — Plan, Contracts and Task Tree

> **Contract of record:** `INVARIANTS.md` (Pocketful — Money Invariants) is the
> shared contract and **outranks this document**. Where this plan and
> `INVARIANTS.md` disagree, `INVARIANTS.md` wins and this plan is the thing that
> gets fixed. This document adds *decisions and sequencing*; it never relaxes a
> rule.
>
> Status: **T0 answered — Python 3 + SQLite. T1 and T3 are delivered, T2's suite
> is built and the gate is GREEN as run by the planner (§4), and T5 has returned
> a no-defect verdict on `ledger/`. T1 and T2 are not done: the reviewer must
> re-verify the mutants against the current `tests/` revision, and §2.1 gained
> one read. Contract sections §2 and §3 are frozen except for the two explicit
> amendments recorded in §2.1 and §2.2.**

---

## 1. Stack decision — DECIDED (T0 answered 2026-10-02: Python 3)

| Part | Proposal | One-line reason it is right for a ledger |
|---|---|---|
| Language | **Python 3** (stdlib only) | `int` is exact and arbitrary-precision, so money has no float type to drift into, and there is nothing to install before the gate can run. |
| Money type | `int`, constrained to `-(2**53-1) .. (2**53-1)` | Exact in every language in the room (incl. JS `number`), a strict subset of i64, and JSON-round-trips with no bigint/string-with-parser problem. |
| Datastore | **SQLite** (`sqlite3` stdlib) | Real single-file transactions and a `BEGIN IMMEDIATE` write lock give serialized writers with zero daemons to configure. |
| Isolation | `BEGIN IMMEDIATE` on every mutating transaction, stated explicitly in code | Reads that feed a write happen inside the write's transaction; no check-then-act across boundaries. |
| API shape | **HTTP + JSON**, key on the `Idempotency-Key` header | The key is a property of the *request*, not of the body, and an HTTP header is where every retrying client already puts it. |
| Test runner | `unittest` (stdlib) + `threading` / `concurrent.futures` | Concurrency obligations 1–2 need real parallel writers, and these are stdlib — the harness cannot be blocked on an install. |
| Gate | one `run_gate.sh` at repo root (tests + build/byte-compile + lint) | One command, one quotable output — a done claim is a quoted gate run or it is not done. |
| Client | **Python 3** (stdlib only), **web-facing** — the human, 2026-10-02: *"use python"*, then *"i want something web facing … visble on the website pocketful.getn.space where we can test it"* | Python serves the pages and the browser is a **view** — so "use python" and "testable on the web" are compatible rather than a compromise, and the idempotency key stays in Python where it can be made durable. A forced constraint agrees: this host has **no JS runtime** (no `node`, `npm`, `bun`, `deno` — checked, not assumed), so a browser-JS client could not run its key-persistence evidence in the gate at all. |

**The client row was missing and the omission was mine.** T0's answer settled the
ledger, datastore, wire and test runner; the seven rows above named nothing for the
client, while T4's DoD had already assumed a JS client by grepping for
`parseFloat`/`toFixed`. That contradiction stood in this plan from the day T4 was
written. It surfaced only when T4 became the critical path and `app/` did not exist.

**Why it gates rather than being cosmetic.** The client's single money-critical
obligation is the idempotency key: generated and persisted so a retry reuses it
instead of sending a second transfer. Its evidence is *kill the process between
submit and response, then show the retry reusing the same key with the ledger
replaying*. With no JS runtime on this host, a browser-JS client **cannot run that
evidence in the gate** — the one money obligation in the build that could create money
from nothing would rest on a documented manual procedure instead of a test. Options
put to the human: (1) Python-stdlib client, key held by the client process, evidence
runs under `unittest` — recommended, and consistent with the kickoff's own framing
that *the hard part is not the UI*; (2) browser JS with `localStorage`, obligation
manual; (3) install Node and build a typed client, adding a second toolchain to a
build whose gate depends on nothing that needs installing.

**Decision:** the human chose **Python 3** for the language, which carries SQLite,
HTTP/JSON and `unittest`/`threading` with it (relayed by deploy-engineer,
2026-10-02). The table above is now binding, not proposed.

**Counter-argument, recorded rather than hidden:** TypeScript would make "float in
the money path" a *compile* error rather than a *review* finding. Python was still
the right call, because a multi-agent build that cannot `npm install` is a build
that cannot run its own gate, and a red gate nobody can fix is worse than a rule
enforced by review plus a lint check. The Node + TypeScript + `better-sqlite3` +
`bigint` design is recorded as the **rejected alternative**, not a live fallback.

### Precondition — TWO hosts, and both must be quoted

Before any owner writes code, someone must confirm **on each host**, and quote the
output:

```
python3 --version
python3 -c "import sqlite3, http.server, unittest, threading; print('stdlib ok')"
```

**This is a check on the interpreter, not a decision gate.** The stack is decided.
Nobody has yet *observed* the toolchain, and I will not treat the decision as
proof that the interpreter exists — but a blocked check must not serialize the
build either. Run it and report it alongside your first deliverable rather than
holding work for it. See §1.2 for why "on this host" was the wrong phrase and
what a red result actually reopens.

### 1.1 Datastore hosting — SQLite stays (agreed with deploy-engineer)

**The answer to "can we host the database on the AWS server": yes — as a SQLite
file on the instance, and that is the right shape.** A server DB (Postgres/MySQL)
also fits a 2 vCPU / 8 GiB box, but it adds a second production service, its own
backup and restore duty, and one more failure mode standing in front of the money
path, for **no correctness gain** over the §2.1 design. SQLite already gives the
two properties the ledger actually needs: real ACID transactions and serialized
writers. Do not add a daemon to get something we already have.

That confirmation does **not** change `Ledger.__init__(self, conn:
sqlite3.Connection)`, the §2.2 wire shapes, or the §3 storage layer. No ID in the
architecture diagram changes. The constraints below are additions, not edits.

**Hard constraints — these are correctness, not preference:**

1. **The database file lives on instance-local storage (EBS). Never EFS, never
   NFS, never S3, never a mounted network volume.** SQLite serialization depends
   on POSIX advisory locks, which network filesystems do not honour reliably.
   Two writers that both believe they hold the lock is not a slow ledger, it is a
   corrupt one. This is the single most important hosting rule here.
2. **WAL plus a write lock, stated in code.** `ledger/db.py` is the only module
   that creates connections, and it sets, explicitly and in this order:
   `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout`, and
   `synchronous=FULL`.

   `synchronous=FULL` is deliberate: WAL's default `NORMAL` is not durable across
   a power loss, which means an acknowledged transfer can vanish. On a ledger the
   acknowledged write is the promise. Pay the fsync.
3. **One writer, by scope.** SQLite serializes writers across processes on the
   *same host*, so multiple API worker processes on one instance are fine. What is
   not fine is a second instance, or an autoscaling group, pointed at the same
   file over any shared mount. Horizontal scaling of the money path is out of
   scope for this build; if it is ever needed, that is the moment to revisit the
   datastore — as a deliberate decision with a migration, not by adding a replica.
4. **Backup is a real procedure, not `cp` — and this is now measured, not
   asserted.** With WAL enabled the `.db` file alone is not a consistent
   snapshot. A rehearsal on `pocketful-prod` (2026-10-02, synthetic data, no
   production rows) copied the live `.db` with WAL open and restored it to
   `OperationalError: no such table: ledger_entries` — not a partial ledger, a
   database with **no schema at all**, because the `CREATE TABLE` was still in
   the WAL. So the failure mode of a `cp` backup here is a file that copies
   without complaint, restores without complaint, and is empty: a silent total
   loss that passes every check short of opening it. Use `VACUUM INTO` or
   `Connection.backup()` — they emit a single consistent file needing no
   `-wal`/`-shm` companion — or snapshot the whole volume including `-wal` and
   `-shm`. This matters more than usual because there is no version control on
   this machine and the board is the only history. **And a backup is not proven
   until it has been restored** — T8 item 6.
5. **No ORM, no connection pool library, no `sqlite3` wrapper package.** The
   stdlib driver and the §2.1 signatures are the interface.
6. **NEW 2026-10-02 — only `journal_mode` is a property of the *file*; the other three
   PRAGMAs are per-*connection* and revert on every reconnect.** Measured by
   deploy-engineer on `pocketful-prod`, through the real `ledger.db.connect()`:
   ```
   SET  -> journal_mode=wal  synchronous=1  foreign_keys=1  busy_timeout=7000
   BARE -> journal_mode=wal  synchronous=2  foreign_keys=0  busy_timeout=0
   ```
   So a factory that configured a connection once at creation would run the money path
   with **`busy_timeout=0`** — instant `SQLITE_BUSY`, no retry window — which is directly
   against §2.1's bounded-retry-with-backoff. `ledger/db.py` gets this right: it re-applies
   all four on every connection (`connect()`, lines 47–50). **This is a constraint that
   must stay true, not a defect to fix** — and it is exactly why T8 item 2 is not a
   formality: `synchronous=FULL` is also SQLite's own compiled default, so a deployed
   process can *report* FULL while never having applied the setting. Reading the PRAGMAs
   off the **running service** is the only way to tell "the setting was applied" from "the
   default happened to match". A T1 DoD line records it (see §4 T1) so a future refactor
   cannot quietly move the PRAGMAs into an init-once path.

**Still the deploy-engineer's call, and not mine:** instance sizing, disk
provisioning and IOPS, snapshot schedule, and whether the API process is managed
by systemd. I own the datastore *shape*; those are substrate choices.

### 1.2 Interpreter version — two hosts, and they can disagree

Raised by deploy-engineer, and it is a real defect in the draft: my precondition
said "on this host", and there is more than one host.

| Host | What runs there | Why its interpreter matters |
|---|---|---|
| the **dev host** (this working tree) | the gate — `unittest` and the concurrency harness | decides whether `tests/` can import and whether the gate runs at all |
| **pocketful-prod** | the API service and the ledger | decides whether the money path starts at all — and that surfaces at deploy, the worst place to find it |

A green run on one proves nothing about the other. Both must be quoted. The host
that matters for *serving* is `pocketful-prod`, and only deploy-engineer can
observe it.

**Declared minimum: Python 3.10.** §2.1 uses PEP 604 unions (`str | None`) and
builtin generics (`list[ActivityItem]`), which are *runtime* syntax on 3.10+.

**Regardless of the observed version, every module under `ledger/` and `api/`
begins with `from __future__ import annotations`.** One line, no signature change.
It makes the annotation syntax lazy, so the failure mode becomes a plain version
check instead of a syntax error at import. It costs nothing and permanently
removes the constraint.

**A red result reopens the *version*, not the stack.** SQLite, the datastore,
§2.2 and §3 are all unaffected. The fix is the one-line future import or
provisioning a newer interpreter — a substrate call, and deploy-engineer's.

**Precondition: MET on both hosts, 2026-10-02.** Quoted output.

`pocketful-prod`, run directly on the host and quoted by deploy-engineer:

```
=== python3 --version ===
Python 3.14.4
=== which python3 ===
/usr/bin/python3
=== stdlib import (corrected) ===
stdlib ok
sqlite lib 3.46.1
smoke [(1,)]
```

The dev host (this working tree), run by me:

```
$ which python3; python3 --version
/usr/bin/python3
Python 3.14.4

$ python3 -c "import sqlite3, unittest, threading, http.server, concurrent.futures, json, decimal, uuid, hmac, hashlib, secrets, datetime; c=sqlite3.connect(':memory:'); c.execute('create table t(a integer)'); c.execute('insert into t values (1)'); print('stdlib ok', sqlite3.sqlite_version, c.execute('select * from t').fetchall())"
stdlib ok 3.46.1 [(1,)]
```

Both hosts are `/usr/bin/python3` at **3.14.4**, comfortably above the 3.10
minimum and *identical to each other*, so the gate and the money path run the same
interpreter and there is no version drift to plan around. SQLite library 3.46.1 on
both. The earlier "file presence only" reading was mine and was the weaker
evidence; prod's is the stricter check on the host that actually serves. The dev
import check has since been run too, so this is closed on both hosts rather than
inherited from one.

### 1.3 Three host facts that bind `ledger/` and `api/`

All three were surfaced by deploy-engineer running the corrected probe on
`pocketful-prod`. Each one fails at *deploy* rather than at the gate, which is
exactly why they belong in the contract instead of in a review comment.

1. **`sqlite3.version` and `sqlite3.version_info` were removed in Python 3.14.**
   Verified here too: `python3 -c "import sqlite3; print(sqlite3.version)"` →
   `AttributeError: module 'sqlite3' has no attribute 'version'`. The modules all
   import fine; the failure is the attribute access, which means a reference
   anywhere under `ledger/` or `api/` raises on the target and nowhere else.
   **The only permitted version reference is `sqlite3.sqlite_version`** (the
   library version — 3.46.1 here).
2. **There is no `sqlite3` CLI on either host.** `command -v sqlite3` → not found
   on dev; `command not found` on prod. Everything goes through the stdlib module:
   schema fixtures, test setup, and the T8 backup procedure (`VACUUM INTO` or
   `Connection.backup()` — both available in library 3.46.1). **No task, script or
   definition of done may assume a CLI step that cannot execute here.**
3. **`/tmp` on prod is tmpfs — RAM, not disk.** A database file placed there is
   non-durable and loses state on reboot. The DB path stays on the ext4 root
   volume. Written down as a rule now rather than discovered later.
4. **Copying a WAL-mode database *file* is not a backup, anywhere.** A plain `cp`
   of a live `.db` silently yields an empty or schema-less database (§1.1
   constraint 4). This applies to test fixtures and scratch snapshots as much as
   to production — use `VACUUM INTO`, `Connection.backup()`, or a fresh
   connection, never a file copy.

---

## 2. Interface contracts (frozen before implementation)

These are the only signatures anyone implements against. If an owner needs an
operation that is not here, they ask me; they do not invent it.

### 2.1 Ledger public operations — `ledger/`

```python
# ledger/types.py
Money    = int          # signed count of MINOR UNITS (cents). Never float.
Currency = str          # ISO-4217, e.g. "USD"
MAX_MINOR = 2**53 - 1   # +- this bound, everywhere: wire, storage, arithmetic

class LedgerError(Exception): ...
class InvalidAmount(LedgerError): ...        # non-int, bool, zero, negative, out of range
class UnknownAccount(LedgerError): ...
class CurrencyMismatch(LedgerError): ...
class SameAccountTransfer(LedgerError): ...
class InsufficientFunds(LedgerError): ...
class IdempotencyConflict(LedgerError): ...
class AccountExists(LedgerError): ...        # open_account on an id that already exists

@dataclass(frozen=True)
class Account:
    account_id: str; owner_id: str; currency: Currency
    allow_overdraft: bool; created_at: str

@dataclass(frozen=True)
class TransferResult:
    transfer_id: str
    status: Literal["applied", "replayed"]   # "replayed" == the key was seen before
    from_account_id: str; to_account_id: str
    amount_minor: Money; currency: Currency; created_at: str

@dataclass(frozen=True)
class ActivityItem:
    entry_id: str; transfer_id: str
    direction: Literal["debit", "credit"]
    amount_minor: Money            # NON-NEGATIVE, always. The sign lives in `direction`.
    balance_after_minor: Money
    counterparty_account_id: str; created_at: str
```

**Amendment 2026-10-02 — `ActivityItem` has no `account_id`.** The draft carried
one; the implementation dropped it; **the implementation is right and §2.1 is
amended to match.** The queried account is already carried by the operation that
produced the items (`list_activity(account_id)` on the ledger,
`GET /accounts/{account_id}/activity` on the wire, whose item shape in §2.2 never
had the field). Repeating it on every item is a second encoding of a fact already
on the request — the same drift class as encoding the sign twice — and a field
that no consumer reads is a field that will eventually disagree with the thing it
duplicates. `direction` plus `counterparty_account_id` fix both parties relative
to the queried account, so nothing is lost. This is an *explicit amendment of the
frozen contract*, not drift absorbed into a test: the distinction that matters is
amended-on-purpose versus quietly-wrong.

**Two conventions settled 2026-10-02, raised by the ledger-engineer during T1:**

- **`ActivityItem.amount_minor` is NON-NEGATIVE; the sign lives in `direction`.**
  A signed amount *plus* a direction field is two encodings of one fact, and two
  encodings of one fact is the same drift class as a cached balance — they can
  disagree, and then which one is authoritative? The ledger maps it exactly one
  way: `direction = "debit" if entry.amount_minor < 0 else "credit"`, and
  `amount_minor = abs(entry.amount_minor)`. This also matches §2.2's transfer
  shape, where the wire amount is positive. **Uniform wire rule: amounts are
  non-negative, direction is explicit.**
- **`open_account` on an existing id raises `AccountExists`**, mapped to HTTP
  **409** with `{"error": "account_exists"}`. Deliberately **not** idempotent:
  opening an account is not a retried operation in the client contract, and
  silently returning the existing account would mask a caller mistake. Reject,
  do not coerce.

```python

```python
# ledger/core.py
class Ledger:
    def __init__(self, conn: sqlite3.Connection) -> None: ...

    def open_account(self, *, account_id: str, owner_id: str, currency: Currency,
                     allow_overdraft: bool = False) -> Account: ...

    def get_balance(self, account_id: str) -> Money:
        """Derived: SELECT COALESCE(SUM(amount_minor),0) FROM ledger_entries
        WHERE account_id = ?. No cached balance column in v1."""

    def get_account(self, account_id: str) -> Account:
        """Added 2026-10-02 at the integrator's request. Raises UnknownAccount.
        A read, not a mutation: it uses the caller's connection and does NOT
        open a write transaction. Needed because GET /accounts/{id}/balance must
        return `currency` (§2.2) and no other read returns an `Account`. The read
        already exists internally as `_load_account`; this exposes it. Rationale:
        the alternative is `api/` decoding the accounts table itself, which is
        both a second implementation of that decode and a violation of the
        boundary stated in §2.2 — api/ opens no connection of its own. One owner
        per piece of state means one place knows how to read an account row."""

    def transfer(self, *, idempotency_key: str, request_fingerprint: str,
                 from_account_id: str, to_account_id: str,
                 amount_minor: Money, currency: Currency) -> TransferResult: ...

    def list_activity(self, account_id: str, *, limit: int = 50,
                      before_entry_id: str | None = None) -> list[ActivityItem]: ...

    @staticmethod
    def split(total_minor: Money, parts: int) -> list[Money]:
        """Pure. sum(split(t, n)) == t for every t, n >= 1. Remainder policy:
        base, extra = divmod(total, n); the first `extra` parts get +1 minor unit.
        Deterministic and order-stable. Never truncates a cent away."""
```

**Required module header.** Every module under `ledger/` and `api/` begins with
`from __future__ import annotations`, and the declared minimum interpreter is
**Python 3.10** (§1.2).

**Banned on the target interpreter:** `sqlite3.version`, `sqlite3.version_info`
(removed in 3.14) and any reliance on a `sqlite3` CLI (§1.3).

**Idempotency semantics (INVARIANTS §3).** `idempotency_key` is the *client's*
claim. `request_fingerprint` is a canonical hash of
`(from_account_id, to_account_id, amount_minor, currency)` and exists **only** to
detect same-key-different-body. It is explicitly **not** the key — deriving the
key from the request hash is forbidden by INVARIANTS §3.

**A rejected request must not record its idempotency key.** Contract clause added
2026-10-02, raised by test-author while building the amount matrix, and it is a
money-loss path rather than a tidiness rule: if a refused transfer persisted its
key, a later retry of that key would **replay** — the caller would receive success
and a `transfer_id` for a transfer that never happened, and the money would
vanish silently, with I1 and I2 still holding because nothing was ever written.
The same applies to a transfer that fails and rolls back: no key row may survive
it. Verified on the real ledger — it records no key for an amount it refused, and
`scenario_failure_atomicity` asserts zero key rows for a failed transfer.

- key unseen → apply, store `(key, fingerprint, transfer_id, response_json)` **in
  the same transaction**, return `status="applied"`.
- key seen, fingerprint equal → return the **stored original response**,
  `status="replayed"`. No new entries.
- key seen, fingerprint differs → `IdempotencyConflict`. The new body is never
  applied.

**Transaction shape** (one transaction per state change, `BEGIN IMMEDIATE`
stated in code):

```
BEGIN IMMEDIATE
  idem = SELECT * FROM idempotency_keys WHERE key = ?
  if idem: fingerprint mismatch -> ROLLBACK + IdempotencyConflict
           else -> COMMIT + stored response (replayed)
  load both accounts (existence, currency match, from != to)
  balance = SUM(entries) for from_account        -- read INSIDE this txn
  if balance - amount < 0 and not allow_overdraft -> ROLLBACK + InsufficientFunds
  INSERT transfers
  INSERT ledger_entries (-amount, from), (+amount, to)     -- sums to zero
  INSERT idempotency_keys (key, fingerprint, transfer_id, response_json)
  UPDATE accounts SET version = version + 1 WHERE account_id IN (from, to)
COMMIT
```

`SQLITE_BUSY` / deadlock is handled by **bounded retry with backoff**, never by
lowering isolation. Connection setup is owned solely by `ledger/db.py`, which sets
`journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout` and `synchronous=FULL`
(see §1.1). No other module opens a connection of its own.

### 2.2 API request/response shapes — `api/`

Integer minor units on the wire. No decimal strings, no floats, no coercion.

```
POST /accounts
  body  : {"owner_id": str, "currency": "USD", "allow_overdraft": false,
           "account_id": str | absent}                     <-- optional, ratified 2026-10-02
  201   : {"account_id", "owner_id", "currency", "balance_minor": 0, "allow_overdraft"}
  400   : {"error": "invalid_request"}                     <-- supplied id fails validation
  409   : {"error": "account_exists"}                      <-- reject, not idempotent
          # `account_id` is optional and server-generated (UUIDv4) when absent.
          # It is accepted because otherwise the 409 above is unreachable: a
          # server-generated id can never collide, so a documented error path
          # would be dead code. A supplied id is validated for length and
          # charset and rejected with 400 before any insert is attempted.

GET /accounts/{account_id}/balance
  200   : {"account_id", "currency", "balance_minor": 12345}
  404   : unknown_account

POST /transfers
  header: Idempotency-Key: <client-generated UUIDv4>       <-- key lives HERE
  body  : {"from_account_id": str, "to_account_id": str,
           "amount_minor": 500, "currency": "USD"}         <-- amount lives HERE, positive int
  201   : {"transfer_id", "status": "applied", "from_account_id", "to_account_id",
           "amount_minor": 500, "currency", "created_at"}
  200   : same body, "status": "replayed"                  <-- key seen, identical body
  409   : {"error": "idempotency_conflict"}                <-- key seen, DIFFERENT body
  400   : {"error": "invalid_idempotency_key" | "malformed_json"}
  404   : {"error": "unknown_account"}
  422   : {"error": "invalid_amount" | "currency_mismatch"
                  | "insufficient_funds" | "same_account_transfer"}

GET /accounts/{account_id}/activity?limit=50&before={entry_id}
  200   : {"items": [{"entry_id", "transfer_id", "direction": "debit"|"credit",
                      "amount_minor", "balance_after_minor", "counterparty_account_id",
                      "created_at"}], "next_cursor": null}
          # amount_minor is NON-NEGATIVE; `direction` carries the sign. See §2.1.
```

**Additive error codes — ratified 2026-10-02.** These cover cases §2.2 does not
name. They are additive: none of them changes a status or code already frozen
above, and a client that only handles the frozen set still behaves correctly.
`invalid_request` 400 (malformed body or a supplied `account_id` that fails
validation), `invalid_limit` / `invalid_cursor` 400 (bad `limit` or `before`
query parameters), `payload_too_large` 413, `not_found` 404, `method_not_allowed`
405. **One disambiguation, because two codes on one status is how clients end up
branching on the wrong field: `not_found` means no such route exists;
`unknown_account` means the route exists and the account id does not.** They are
different conditions for the client — one is a client bug, one is a data bug —
and `api/errors.py` is the single place that names them.

**Amount validation rule (reject, never coerce).** `amount_minor` must satisfy:
`type(v) is int` (explicitly **not** `bool`, which is an `int` subclass in
Python — `true` must not become `1`), `0 < v <= MAX_MINOR`. Rejected:
`12.34` (float), `"12.34"` / `"500"` (string), `true`, `0`, `-500`.

**A failed transfer never returns 200.** A same-key-different-body replay never
returns 200.

### 2.3 Client obligations — `app/`

1. **Key generation.** One `Idempotency-Key` (UUIDv4) per *user action*. The user
   pressing Send twice produces two keys — those are two transfers. A network
   retry of one transfer produces **one** key.
2. **Persistence before the first attempt.** Persist
   `{key, from_account_id, to_account_id, amount_minor, currency, state}` to local
   storage **before** the first HTTP attempt. On retry — including after the app
   is killed and relaunched — reuse that exact key **and** that exact body.
3. **Never regenerate a key on retry.** A regenerated key is a double-spend.
4. **Terminal states.** On 2xx: settle and clear the pending record. On 409: the
   recorded body did not match — surface it, do not retry. On 5xx / network
   error: keep the pending record and retry with the same key.
5. **No float arithmetic on money in the UI.** No `parseFloat`, no `toFixed` on a
   value that is sent or stored, no percentage math producing a fraction of a
   cent. User input converts to integer minor units immediately; more than two
   decimal places is **rejected locally**, not silently rounded. Display
   formatting (integer cents → `"$12.34"`) happens at the render edge only.
6. **The server's balance is authoritative.** The client never sums a list of
   transactions and presents that as the balance.
7. **The browser never holds the key.** Amended 2026-10-02 for the web surface
   (`app/web.py`), ruled by the planner and not negotiable: **Python mints, persists and
   transmits the `Idempotency-Key`; the browser generates nothing, stores nothing, and
   sends nothing about keys.** No hidden form field, no cookie, no URL parameter carries
   one. The consequence to hold onto: a page reload that re-minted a key would be a
   **double-spend the ledger cannot detect** — the ledger would see two transfers under
   two keys and be right to apply both. The web layer therefore enforces three things —
   no key material in `web.py`; `POST /send` answers **303 See Other** (Post/Redirect/Get)
   so a refresh is a GET, not a replay; and **rendering writes nothing**. The store must
   survive a process kill and a reboot alike (§4 T8), because a lost store is a retry that
   finds nothing pending and mints a fresh key.

---

## 3. Architecture

```arch
{
  "kind": "layered",
  "title": "Pocketful — money path",
  "layers": [
    {
      "id": "edge",
      "title": "Edge (public)",
      "items": [
        { "id": "edge_nginx", "label": "nginx vhost pocketful.getn.space", "details": "TLS terminator. Proxies ONLY to the wallet process; the API port is not in the vhost. The existing [::]:443 block already owns ipv6only=on — this block inherits and must omit it." },
        { "id": "edge_browser", "label": "Browser (a view, never a key holder)", "details": "Generates no idempotency key, stores none, sends none. No hidden field, cookie or URL param carries one." }
      ]
    },
    {
      "id": "client",
      "title": "Client (app/)",
      "items": [
        { "id": "app_web", "label": "Wallet web server (app/web.py)", "details": "stdlib http.server, loopback-fronted. GET / , POST /send -> 303 See Other (PRG), POST /retry, GET /health. Mints and persists the key in Python. Rendering never writes." },
        { "id": "client_ui", "label": "Wallet UI: balance + activity feed", "details": "Reads server truth only. Never sums a local list to compute a balance." },
        { "id": "client_send_flow", "label": "Send flow: key generation + persistence", "details": "One key per user press, persisted before the first attempt, reused on every retry including after restart." },
        { "id": "client_api_layer", "label": "API client (integer minor units only)", "details": "No parseFloat, no toFixed on a sent amount. Display formatting happens at the edge." },
        { "id": "client_store", "label": "Key store (app/keystore.py)", "details": "Holds PENDING keys. Durability-critical: same ext4 root volume as the DB, never /tmp or network storage. A lost store is a fresh key on retry = double-spend." }
      ]
    },
    {
      "id": "api",
      "title": "API (api/) — loopback only, never public",
      "items": [
        { "id": "api_routes", "label": "HTTP routes", "details": "POST /accounts, GET /accounts/{id}/balance, POST /transfers, GET /accounts/{id}/activity" },
        { "id": "api_validation", "label": "Request validation", "details": "Reject, do not coerce. Non-integer / bool / zero / negative / out-of-range amounts are 400/422." },
        { "id": "api_idempotency", "label": "Idempotency-Key header handling", "details": "Key is read from the header; replay returns the stored response; same key + different body is 409." }
      ]
    },
    {
      "id": "ledger",
      "title": "Ledger core (ledger/)",
      "items": [
        { "id": "ledger_core", "label": "Ledger operations", "details": "open_account, get_balance, transfer, split — the only writers of money." },
        { "id": "ledger_entries", "label": "Double-entry writer", "details": "Every mutation writes entries summing to zero. No balance is mutated without a paired entry." },
        { "id": "ledger_idem_store", "label": "Idempotency store", "details": "Key + fingerprint + result written in the same transaction that applies the transfer." },
        { "id": "ledger_split", "label": "Deterministic split", "details": "Parts sum exactly to the total; remainder distributed by a documented policy, never truncated away." }
      ]
    },
    {
      "id": "data",
      "title": "Storage (SQLite)",
      "items": [
        { "id": "db_accounts", "label": "accounts", "details": "account_id, owner_id, currency, allow_overdraft, version, created_at" },
        { "id": "db_ledger_entries", "label": "ledger_entries", "details": "Signed INTEGER amount_minor. Balances are a derived view; no cached balance column in v1." },
        { "id": "db_idempotency_keys", "label": "idempotency_keys", "details": "key PRIMARY KEY, request_fingerprint, transfer_id, response_json — the uniqueness that makes replay safe." }
      ]
    },
    {
      "id": "gate",
      "title": "Evidence (tests/)",
      "items": [
        { "id": "gate_invariants", "label": "I1–I6 assertions", "details": "Reusable functions every scenario calls." },
        { "id": "gate_harness", "label": "Concurrency harness", "details": "Parallel transfer storm, 100x same-key retry storm, failure atomicity, fuzz." },
        { "id": "gate_runner", "label": "Gate command", "details": "One command runs tests + build + lint; its quoted output is the only accepted proof of done." }
      ]
    }
  ],
  "flows": [
    { "from": "edge_browser", "to": "edge_nginx", "label": "HTTPS" },
    { "from": "edge_nginx", "to": "app_web", "label": "proxy to the wallet only" },
    { "from": "app_web", "to": "client_ui", "label": "renders the view" },
    { "from": "app_web", "to": "client_send_flow", "label": "mints the key in Python" },
    { "from": "client_send_flow", "to": "client_store", "label": "persist BEFORE first attempt" },
    { "from": "client_ui", "to": "client_api_layer", "label": "user intent" },
    { "from": "client_send_flow", "to": "client_api_layer", "label": "key + body, persisted first" },
    { "from": "client_api_layer", "to": "api_idempotency", "label": "POST /transfers + Idempotency-Key" },
    { "from": "client_api_layer", "to": "api_routes", "label": "JSON, integer minor units" },
    { "from": "api_routes", "to": "api_validation", "label": "raw body" },
    { "from": "api_validation", "to": "ledger_core", "label": "validated int amount" },
    { "from": "api_idempotency", "to": "ledger_idem_store", "label": "key + request fingerprint" },
    { "from": "ledger_core", "to": "ledger_split", "label": "split(total, n)" },
    { "from": "ledger_core", "to": "ledger_entries", "label": "balanced entry pair" },
    { "from": "ledger_entries", "to": "db_ledger_entries", "label": "INSERT, signed INTEGER" },
    { "from": "ledger_idem_store", "to": "db_idempotency_keys", "label": "INSERT in same txn" },
    { "from": "ledger_core", "to": "db_accounts", "label": "existence, currency, version" },
    { "from": "db_ledger_entries", "to": "gate_invariants", "label": "SUM(amount) == 0" },
    { "from": "gate_harness", "to": "ledger_core", "label": "N concurrent transfers" },
    { "from": "gate_runner", "to": "gate_harness", "label": "runs the suite" }
  ]
}
```

---

## 4. Task tree — one owner per task, one owner per file

File ownership is exclusive. No two agents edit the same file, ever.

| Task | Owner | Files (exclusive) | Blocked by |
|---|---|---|---|
| T0 Stack decision — **DECIDED: Python 3 + SQLite** | **human** | — | — (answered 2026-10-02) |
| T1 Ledger core — **DONE 2026-10-02** | ledger-engineer | `ledger/*.py` | T0 ✅ |
| T2 Invariant gate + concurrency harness — *green; new matrix awaiting a first adversarial pass* | test-author | `tests/*`, `tests/` DDL fixtures | T0 ✅ |
| T3 API surface + gate runner — *delivered; `get_account` fallback removal owed* | integrator | `api/*.py`, `run_gate.sh` | T1 ✅ |
| T4 Client wallet + idempotent send | frontend-engineer | `app/*` | T3 ✅ |
| T5 Review: ledger, idempotency, transactions — **verdict delivered, stands** | reviewer | — (read-only) | T1 ✅ |
| T6 Review: API boundary + client transfer construction | reviewer | — (read-only) | T3, T4 |
| T7 Full gate green, evidence quoted | integrator | — | T2, T5, T6 |
| T8 Prod substrate + live PRAGMA verification | deploy-engineer | `ops/`, host config | T3 |

T1 and T2 run **in parallel** — T2 writes against the frozen signatures in §2, so
it has no handoff edge on T1. Everything else waits for its blocker.

**Verification log — 2026-10-02 (this plan is the only history; keep it current).**

- **T1 closed by its owner's own quote.** ledger-engineer ran `./run_gate.sh`
  themselves → exit 0, `Ran 33 tests … OK`, lint clean, 52/52 API checks, and
  published the full 252-line log as artifact `art-fccd0426b14b30e6e5669a96`
  (sha256 `6966a77a…8b23`, 23 091 B, digest recomputed by the planner). DoD 1–6
  met, item 5 under the owner's own name. The planner's earlier stand-in run was
  retired the moment the owners could execute again.
- **T5 verdict delivered and sustained.** `ledger/` money-path: no defect. All six
  mutants driven directly — `CheckThenAct`, `NonAtomic` (under `>= 2`),
  `NonIdempotent`, `Unbalanced`, `TruncatingSplit`, and the new `Coercing` — all
  RED, with every control green and no scenario red-regardless. Verdict re-covered
  on `core.py 42048fe` after the amendment moved it. The `ActivityItem` note was
  withdrawn as superseded.
- **T2 green; one adversarial pass still owed on the new surface.** 33 tests OK,
  run by the owner. The reviewer measured the one open defect (flaky count
  assertion, fixed) and T2's new amount matrix has had **no independent pass** —
  test-author asked for one and it is owed.
- **T3 delivered.** Shim and row read deleted with grep proof (`api/app.py` retains
  exactly one `getattr`, line 49, the thread-local slot, not an interface probe).
  `is_minor` guard added across the selfcheck after `{"balance_minor": False} ==
  {"balance_minor": 0}` was found to be **True** on the wire.
- **T4 not started. T6 blocked on T4. T7 open on T2, T5, T6. T8 holding on T3.**

**The revision discipline this log rests on.** Every verdict above names the bytes
it covers, and this tree moves under working agents — it moved twice while the
checks above were being written. A recorded digest is a statement about a moment
(§5): re-cut before relying on it. The planner's own pin said `core.py MATCH`
minutes before `get_account` moved it, and it took the reviewer re-cutting it to
catch that.

### T1 — Ledger core: exact integers, double entry, idempotency, concurrency

**Owner:** ledger-engineer · **Blocks:** T3, T5

**Definition of done**
1. `ledger/types.py`, `ledger/db.py`, `ledger/schema.py`, `ledger/core.py`
   implement exactly the §2.1 signatures, no more, no less, each beginning with
   `from __future__ import annotations` (§1.2).
2. Every row of `ledger_entries.amount_minor` is a signed `INTEGER`; the schema
   carries `CHECK (typeof(amount_minor) = 'integer')`.
3. `transfer()` runs in a single `BEGIN IMMEDIATE` transaction with the read of
   the payer balance **inside** it. `ledger/db.py` is the only connection factory
   and sets `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout`,
   `synchronous=FULL` (§1.1).
4. Grep proof, quoted: no `float`, no `round(`, no `Decimal`, no `/` on a money
   value anywhere under `ledger/`; and no `sqlite3.version` /
   `sqlite3.version_info` (§1.3).
5. The test-author's ledger scenarios pass, and the owner quotes the exact
   command and its output. `I1`, `I2`, `I3` hold after the scenario.
6. **Evidence required:** a scenario that fails before the change and passes after
   (e.g. removing the in-transaction balance read must turn the parallel storm
   red).
7. **Added 2026-10-02 — the four PRAGMAs are re-applied on every connection, not once at
   startup.** `synchronous`, `foreign_keys` and `busy_timeout` are per-**connection** and
   revert on reconnect (§1.1 constraint 6, measured on prod by deploy-engineer). `db.py`
   already does this (`connect()`, lines 47–50); the DoD item exists so a future refactor
   cannot move them into an init-once path and leave the money path running with
   `busy_timeout=0`. A green gate would not catch that — the connection still works, it
   just has no retry window.

**T1 addendum 2026-10-02 — one read added to §2.1.** `get_account(account_id) ->
Account`, raising `UnknownAccount`, requested by the integrator and granted here.
It is a read: caller's connection, no write transaction. It exists so `api/` never
decodes an accounts row itself. The `getattr` shim and the read-only row fallback
currently in `api/app.py` are **deleted in the same change that lands this** — a
dormant branch that silently changes behaviour depending on whether an interface
member exists is how a missing implementation hides. Owner: ledger-engineer
(`ledger/`), then integrator (`api/app.py`); no file is touched by both. This is
the only outstanding item on T1.

`ActivityItem` is **not** to be changed: §2.1 was amended to match the
implementation, which drops `account_id` (see §2.1).

**T1 revision note — the reviewer's verdict date has an expiry.** `get_account`
landed, and `ledger/core.py` moved from `235eb51` to **`42048fe`**. The other four
files are unchanged (`db.py 2978923`, `types.py 2da4d1e`, `schema.py da42efe`,
`__init__.py d8ec93f`), so the change is confined to one file. Consequence, stated
plainly because the rule is mine: **T5's no-money-path-defect verdict was rendered
against `core.py 235eb51` and does not cover the current `core.py`.** The addition
is a read that opens no transaction and takes `RLock` re-entrantly like every other
read here — but "obviously safe" is not a review, and a verdict does not carry
across a revision change. The reviewer's re-run must therefore confirm the mutants
against `tests/` **and** re-cover `core.py 42048fe`. That is the honest cost of the
amendment, and it is worth paying to remove `api/`'s private decode of an accounts
row.

**Discharged the same day.** The reviewer re-cut the pins, diffed `core.py`
themselves, and found the only delta to be the read-only `get_account()` —
`transfer`, `list_activity`, `split`, `open_account`, `get_balance`,
`_require_amount` and all of `db.py` byte-identical to what they reviewed. The
`ledger/` money-path verdict therefore **stands on `42048fe`**. Carry the new
digest, not the old one.

### T2 — Invariant gate and concurrency harness

**Owner:** test-author · **Blocks:** T7 · **Nobody else edits `tests/`.**

**Definition of done** — all five obligations in `INVARIANTS.md` §7 are
implemented and genuinely concurrent (`threading` / `concurrent.futures`, not a
loop):

1. **Parallel transfer storm** — N concurrent transfers over overlapping account
   pairs; assert exact final balances, then I1 and I2.
2. **Idempotent retry storm** — one key fired 100× concurrently; assert exactly one
   application, every caller got the same `transfer_id`, and I1/I2 hold.
3. **Rounding sweep** — `split(total, n)` over many pairs including primes and
   `n=1`; assert I6 every time.
4. **Failure atomicity** — inject a mid-transfer failure; assert no partial entries
   survive and I1/I2 hold.
5. **Invariant fuzz** — randomized op sequence, asserting I1/I2 after **each** op.

I1–I6 are exposed as reusable functions. A sequential-only suite does **not**
satisfy obligations 1 and 2. The suite is never weakened to make a red gate green.

**6. The suite proves its own teeth.** The check-then-act variant — payer balance
read *outside* the write transaction, barrier-released so every reader sees the
same stale balance — is kept in the suite as a **permanent mutation fixture**. The
gate asserts that this deliberately-broken ledger turns the overdraft race **red**.
A suite that passes against a known-broken ledger is not a gate, and this is what
makes "the gate is green" mean something without anyone having to trust it. It
also gives the fail-before / pass-after evidence a durable home instead of a
scratch file.

**Amended 2026-10-02: the assertion is on the property, not on a racing count.**
This DoD originally named `applied 20/20, src −1000, I4 violated` — the values
observed in the run that produced it. **Those were not a property of the defect;
they were the value the scheduler happened to produce.** The variant's
over-application count varies (20, 19, 17, 13 observed), so pinning it made the
gate flaky at roughly the rate the reviewer measured: 3 failures in 60 runs. The
assertion now reads `applied > affordable`, `src == RACE_SEED − applied × RACE_AMOUNT`
(exact arithmetic, whatever it applied), `src < 0`, and I4 read from the database —
which states the defect more completely than the constant did, since it holds for
any interleaving. Detection of the mutant is **observed at 100%, not guaranteed** —
reviewer 40/40 and 60/60 drives, test-author 40/40, planner 60/60 on the fixed
assertion, and one **unreproduced miss** in a 60-drive direct loop by the reviewer,
against 0 misses in 800 further drives (600-drive sweep: applied
`{20:579, 19:13, 18:4, 17:2, 16:1, 13:1}`). The rate is small, not zero, and the
mechanism says why: the stale read is **unsynchronized**, so a schedule in which
enough readers land after the commit yields exactly `affordable` applies and the
scenario returns normally. The mutant is designed to be caught, not guaranteed to
be; the control still asserts `applied == affordable`.

This is the one licensed exception to "a red gate is never made green by changing a
test" (§5), and it is recorded here so it cannot be cited as precedent.

**How this exception was earned, including the planner's error.** The planner's
recorded failure model was wrong: it said that if the timing failed to produce the
race the mutant would go green and the test red, so the gate failed loudly in the
safe direction, and it instructed test-author to **keep it as it is**. Neither half
held. Detection of the mutant is observed at 100% across ~940+ drives by three
participants, with one unreproduced miss — the rate is small, not zero. It was the
*count assertion the planner had
endorsed* that flaked. A flaky red is not safe — it is the standing excuse by which
a real red is dismissed later. The instruction was retracted and the assertion
replaced. The exception is narrow by construction: the old assertion pinned a racing
quantity as a constant, and the replacement asserts strictly more of the property
under any interleaving, so it is not a weakening. My own DoD named `applied 20/20,
src −1000`; that number was a property of one schedule, not of the defect.

**Source material — with its limits stated.** Two T1 artifacts are published, both
digests recomputed by the planner against the catalogue copy rather than quoted
from the author:

- `pocketful_t1_proof.py` — `art-6b07311d4b36a87bc75b8861`, sha256 `4968779a…1383f`, 14 976 B.
- `pocketful_t1_suite_diagnosis.py` — `art-f49e102617c7927922b7c106`, sha256 `34d7ad4d…9b00`, 5 650 B.
  **Supersedes `art-047e8011594bb07de729d1a4` (`d8bfb87c…860d`), which is withdrawn:**
  its §B (failure atomicity) was **vacuous** — it imported the live `TRIGGER_SQL`,
  which at the time aborted on the *first* entry, so its three zeroes were produced
  by the injection point rather than by the ledger's transaction boundary, and it
  could not distinguish a real ledger from a non-atomic one. Rebuilt with a
  second, non-atomic arm: real ledger leaves delta entries 0 / transfers 0 /
  idempotency 0 with I1 holding; the non-atomic arm leaves 1 entry and 1 transfer
  row surviving with I1 violated. §A (retry storm) was always sound. Caught by
  test-author, not by the artifact's author, and not by me.

Both are proofs of what the implementer tested — which is useful precisely
because it is also an accurate map of what nobody has tested yet. The first has
no counterpart for obligations 2, 4 and 5: its idempotency coverage is a single
sequential replay plus one conflict, correct but exactly the sequential-only
coverage §7 says does not discharge obligation 2. The second holds the only
durable concurrency evidence for the retry storm (100 threads, one key, applied 1
/ replayed 99) and, once rebuilt, for failure atomicity, and it implements the
scoped-assertion shape used below.

**Four things the withdrawn artifact teaches, and the last is the encouraging one.**
First: **a passing script is not evidence that it tested anything.** `§B` printed
as passing while it was tautological — the injection point manufactured the zeroes
it then reported. That is the same failure mode as three others in this build, and
they arrived from four directions: a self-check that "passed" on a global count
tripped by a fixture seed row; a mutation trigger whose `WHEN` clause never
injected anything, leaving a mutant invisible; `§B` above; and a timeout bound
proved with a 1-second limit against a 0.55-second selfcheck, so the bound never
fired and exit 0 was nearly recorded as "the timeout works". In every case the
check was green because it never exercised the thing it claimed to check. The
remedy is the same each time and is already in this plan — a control that passes
*and* a mutant that dies, on the same scenario.

The fourth instance is the encouraging one: **the integrator caught their own
vacuous proof before reporting it**, named it as a bad test, and tightened the
bound past the runtime to make it discriminate. Nobody had to point it out. A room
that catches this pattern on the way in is worth more than any individual green
run in it.

**A separate class, raised by test-author: a red for the right reason with the wrong
explanation.** A baseline report was red, the defect it named was real and correctly
caught, but the explanation attributed it to values that had in fact been rejected
properly. This is not the false-pass class — the test was not green, and the defect
was not missed — and it is not harmless either: a red that arrives with a confident
**wrong address** costs more than a red with no explanation, because someone acts on
the address. The four instances above share the remedy "add a mutant"; this one does
not. Its remedy is **read your red critically**, and the two must not be collapsed
into one lesson.

Second: **a digest does not pin the meaning of a script that imports live code.**
The diagnosis artifact imports `sc.TRIGGER_SQL` from `tests/scenarios.py`, so its
`§B` verdict depends on another file's bytes, and its own sha256 cannot express
that. Verifying a digest proves the script did not change; it does not prove the
thing the script exercises did not. For any evidence artifact that imports the
code under test, the message must name **both** revisions.

Third, and the sharpest: **when a shared fixture moves, an artifact's *previous*
verdicts do not survive — they can reverse.** The same wrong `WHEN` clause made
both a mutant invisible in `tests/` and `§B` vacuous, so two artifacts appeared to
corroborate each other while sharing the one defect that made them both say
nothing. Corroboration between two consumers of one fixture is not corroboration.
This is the dangerous shape precisely because it looks like independent
confirmation, and it is why the ledger-engineer republished with an explicit pin on
both `ledger/` and `tests/`, and split their artifacts by whether they import
`tests/` at all — a ledger-only script has a complete pin, a `tests/`-importing one
never does.

**Neither discharges the suite obligation.** Evidence that an implementer's own
script passes is not a gate: it was written by the person whose code it exercises,
against cases that person chose. Lifting scenarios from them is fine; adopting
their assertion set as sufficient coverage is not. Obligations 2, 4 and 5 remain
owed to `tests/` in full until T2's own suite asserts them and the reviewer
confirms the mutants still die.

**Evidence observed 2026-10-02** (test-author's own run, once their runtime's
command classifier recovered — the owner's output, not a relay):

```
python3 -m unittest discover -s tests -t .   →  Ran 33 tests  OK   exit 0
python3 -m unittest tests.test_harness_selfcheck -v  →  15 tests  OK
./run_gate.sh  →  4/4 phases: 33 tests OK · compileall · lint clean · api-contract 52/52  →  GATE GREEN
```

(The first recorded run was 25 tests; the suite grew to 32 with
`tests/test_validation.py` and the coercing mutant, then to 33, then to 35 with the
`R/refusal-path` scenario and `RefusalBurnsKeyLedger`. The tree moved three times
while this log was being written, and every figure above is the owner's own run,
quoted by the owner.)

**Re-pin 2026-10-02 (reviewer, after the tree moved mid-write-up).** The reviewer
re-ran rather than let a stale-pinned verdict stand, and re-hashed afterwards to
prove the pass covered the bytes it named:

```
ledger/ unchanged: core.py 42048fe · db.py 2978923 · types.py 2da4d1e · schema.py da42efe · __init__.py d8ec93f
tests/: mutants.py fa8e62dd · scenarios.py 29e1b04b · test_harness_selfcheck.py a76e291 · test_validation.py 33cbbd9
Gate: Ran 35 tests ... OK
```

All six mutants RED, every control GREEN, same detections as the prior pass;
real ledger every scenario PASS; `get_account` verdict unchanged. The earlier
snapshot of this pass (gate 33 tests, `scenarios.py 8750fe2`) is superseded **as a
pin** — but the `ledger/` half is byte-identical across both, so the T5 verdict
carries and only the test-side half expired. That asymmetry is the both-revisions
rule working as intended, not a defect in either run.

Boundary evidence, independent of the suite and by someone other than the author:
`MAX_MINOR` accepted at `9007199254740991`; `MAX_MINOR+1` and `MAX_MINOR+2` both
`InvalidAmount`; banned-token grep over `ledger/` returns only docstring prose.

**Tree-level pin — the convention for a moving tree.** The gate count moved four
times in a day (32 → 33 → 35 → 36 → **37**), never by the person quoting it, so a
bare count is a fact about a moment. The pin is over **every file in the three trees
the gate reads**, recursively, excluding only derived state:

```
find tests ledger api -type f -not -path '*/__pycache__/*' | sort | xargs sha256sum | sha256sum
  → 7bbcb8e2f72b33f12771872cd08380c01755fedfccdb096054e3724d04af3125   (Ran 37 tests, exit 0, GATE GREEN)
  per tree: tests a9149a4f… · ledger 5925d35e… · api a9b89ee2…
```

Adopted as the room convention: **a bare count is not evidence; the count bracketed
by the pin it was read at is.** Three properties make it more than a habit:

- It covers `tests/`, `ledger/` **and** `api/`, because the tests phase imports
  `ledger/` — a `tests/`-only pin would stay green while the ledger moved underneath
  it, which is T5's failure mode arriving through the pin rather than the verdict.
- `-type f` catches a fixture, a subdirectory, or anything added later **by
  construction**, not by remembering. `tests/README.md` is deliberately in: a
  document describing the harness is part of the revision it describes.
- The `__pycache__` exclusion is **demonstrated, not asserted** — the tree was wiped
  and recompiled and the pin did not move, so the exclusion removes derived state
  rather than shrinking scope.

The pin is printed **before and after** each run. A mismatch prints a distinct line
naming both (`PIN MOVED DURING RUN`) so the disqualifier travels inside the quoted
output, and **does not fail the gate**: a moved pin means a peer typed during the run,
not that the code is wrong, and making it red would teach the room to re-run until
green. Red keeps meaning "the code is wrong". Authorized 2026-10-02 as an addition to
an existing phase's output, not a fifth phase — the four phases and what they check do
not move.

**Caught live on day one:** `tests/` moved `04f8faf1…` → `a9149a4f…` between two
commands a minute apart (five files, mtimes 12:45:07–12:45:57) while test-author was
mid-flight. Any gate count older than the last pin is unreferenced.

**Wired into `run_gate.sh`, 2026-10-02 — both branches proven by running them.** The
printed pin is the same digest as the hand-run command, so the wired convention and
the room convention are one statement, not two. The mismatch branch was exercised by
inducing a real mid-run change in `api/` (the integrator's own tree, not another
owner's), and it produced:

```
*** PIN MOVED DURING RUN: 7bbcb8e2… -> 69374c4f… ***
*** the tree changed while this gate ran — do not quote its exit status
*** as bound to 69374c4f…; re-run on a quiet tree. ***
exit=0
```

`exit=0` under the mismatch is the ruling holding in the one case that could have
quietly broken it; the reasoning is written into the script's comment block so the
next person does not "fix" it into a failure. `run_gate.sh` sits at the repo root,
**outside all three pinned trees**, so the pin tracks what the gate reads and not the
gate itself — that hole is authorized closed by printing the gate's own digest as a
separate `GATE PIN:` line (separate, not folded in: hashing a file containing its own
hash is a fixed point). It is an anchor for honest agents, not tamper-proofing.

**Consequence, and it is a room rule:** now that the pin lines are inside the gate's
output, **a quote of the gate output must include both `TREE PIN` lines** — and the
`PIN MOVED` block where present. A quote that starts at `Ran N tests` deletes the
disqualifier and makes a moved run look bound, which is exactly what the mismatch
branch exists to prevent. **The quote is the whole block, not the two lines that look
like evidence.**

**What "the gate is green" means — the honest size of the claim (2026-10-02).**
Green means **these inputs, this script, this host**. The `TREE PIN` binds what the
gate read; the `GATE PIN` binds which checker ran; and **the gate cannot see the
Python the checker imports, the SQLite library, or the interpreter** — so a green run
does not by itself assert anything about the machine it ran on. "Green" has been
quoted in this room all day and this is the first statement of exactly what it covers.

That gap is not a hole to plug in the gate — it is **the seam between T7 and T8**, and
it is why T8 exists. **The gate establishes inputs and script; T8 establishes the
host**: interpreter observed at `/usr/bin/python3` 3.14.4, SQLite library 3.46.1, and
`synchronous=FULL` verified in the **running process** rather than in source. A green
gate quoted without T8 is a claim with one of its three parts missing, and the plan now
says which part.

**Teeth on the new matrix — both answered 2026-10-02 (test-author).** Every other
assertion in this suite has a mutant that forces it to fail. The two assertions that
came in with the amount matrix originally had **no mutant that violates them**, so
neither had been shown to discriminate:

1. *a rejected request records no idempotency key* — **answered at both of its
   instances.** The clause has two, and the planner's first draft of this section
   described the clause as covered when only one was; the correction is test-author's
   and it is checkable.
   - *Funds refusal* — `R/refusal-path` drives `RefusalBurnsKeyLedger`: an
     insufficient-funds refusal is raised **inside** the write transaction after the
     key has been claimed, so only the rollback returns it. Leg A asserts a retry
     with the same key and a different body still applies; **leg B stops a ledger that
     meets leg A by never writing keys at all** — without it the scenario is green for
     the wrong reason, the `>= 1` trap one level up.
   - *Amount refusal* — `scenarios.py:390`'s assertion, which `RefusalBurnsKeyLedger`
     **structurally cannot** cover: a bad amount never reaches the transaction, so it
     passes the amount matrix (`checked 12`, driven by both test-author and the
     reviewer). It now has `mutants.BurnsKeyOnBadAmountLedger`, RED at *amount 12.34
     was rejected but recorded key reject-0*, with the matrix control passing.
   The plan now **enumerates the instances** rather than describing the clause as
   covered — the same overreach the ledger-engineer withdrew around a different
   assertion on the same day, and the planner committed it in the plan while writing
   the rule against it.
2. *a rejected request leaves the ledger untouched* — **answered** by
   `mutants.RejectAfterWriteLedger`, which applies the transfer and *then* refuses it.
   It is **balanced on purpose**, and that is the method: an unbalanced stray debit
   also kills the matrix, but through **I1**, so the kill would be attributable to the
   global invariant rather than to the assertion in question. Balanced, with I1 and
   I2 both holding, the **only** assertion that can fire is the untouched-check.
   `NonAtomicLedger` was driven against the same scenario first and **passes** it —
   verified, not assumed — because it validates before it writes, so a bad amount
   never reaches its write path. Control passes; detection is therefore attributable.

Two refusal defects are now permanent and distinct: a **burned key** (money never
moves, the caller is told it succeeded) and a **rejected-but-applied** transfer
(money moves, the books stay balanced, I1/I2 hold). Neither is visible to any
invariant; both are visible only to an assertion that was shown to discriminate.

The matrix also gained `3+0j` and `"0x10"` (12 values). They add **no teeth** against
the current mutant — `int()` refuses both — and the docstring says so explicitly
rather than implying they are load-bearing; they exist for an `eval`-ish or
`int(s, 0)`-shaped coercion. A value that silently does nothing is the `>= 1` trap
again, and the cure is the sentence, not deleting the value.

**Fixture determinism — ruled, 2026-10-02.** The reviewer's residual (the mutant's
stale read is unsynchronized, so a schedule where enough readers land post-commit
yields exactly `affordable` and the probe goes green) is **documented, not
engineered away**. A second `Barrier` was considered and **rejected**: `Barrier(n)`
without a timeout converts a rare false-pass into a possible **hang**, and with a
timeout it converts it into a periodic flake whose rate depends on load — the same
defect just removed from this file, relocated. A hung gate produces no output to
quote and blocks the room; a documented small miss does neither. This is **fixture
sensitivity, not a soundness hole**: the ledger's correctness rests on the
real-ledger drives and I1–I6, not on the probe's catch rate. Wording is
"no miss observed", never "detection is 100%".

**Independent adversarial pass on the teeth — reviewer, 2026-10-02 (pins:
`mutants.py 31808156 · scenarios.py 046da501 · test_validation.py 33cbbd90 ·
test_harness_selfcheck.py 9a524060`; `ledger/` unchanged; gate 37 tests OK).**

- *(a) the untouched-check* — **confirmed killed** by the shipped
  `RejectAfterWriteLedger`, with a targeted message. The reviewer independently built
  the naive one-sided variant and watched it die at `I1 VIOLATED: SUM(amount_minor)
  … is −1` — **right verdict, wrong message**: the assertion under test never fired.
  Independent convergence with test-author on that distinction is what made it its
  own class in §4. Balanced was the correct choice, confirmed twice.
- *(b) the matrix key-check* — **no shipped mutant violates it**, and the reviewer's
  mechanism is accepted: `_require_amount` raises **before** `db.run_immediate` opens
  a transaction, so on a bad amount there is no key to burn. `RefusalBurnsKeyLedger`
  correctly PASSes the matrix; the key-burn defect lives on the insufficient-funds
  path, where `R/refusal-path` kills it.
- *`1.0`* — against the shipped `CoercingLedger` it carries **no weight** (12.34
  kills it first). Against a shape-checker (`v != int(v)` → reject) it is the
  **entire** weight: RED on the full matrix at *amount 1.0 was accepted and applied*,
  and NOT detected with 1.0 removed or with only 12.34 present. `1.0` is the single
  value separating a type check from a shape check.

**Ruling: ship both remaining mutants.** The reviewer proved both assertions
non-vacuous with three-line **scratch** mutants and then deleted them, which leaves
their discrimination resting on a session quote — the same status as the withdrawn
`§B`: demonstrated once, in a file nobody kept. **The rule is not "one mutant per
conceivable defect"; it is "an assertion we keep must have a mutant behind it, or it
is deleted."** Deleting is worse here — it would remove the guard on a refactor that
moves validation inside the transaction, and remove the only value separating a type
check from a shape check. Each shipped mutant carries a comment stating that the
**current control flow cannot reach the defect** and naming the change that would
make it live, so nobody reads it as "this bug exists today". The reviewer was offered
the alternative — a reasoned objection from the gate outranks the planner's
preference — and invited to take deletion instead if shipping misrepresents the code.
What is not available is the third option: an assertion that stays and has never been
shown to fire.

**Two required elements of a refactor-guard mutant, both adopted 2026-10-02.**

1. **The self-check asserts the invariant tag, never a bare `assertRaises`.** The
   reviewer's own variant died at `I1 VIOLATED: SUM(amount_minor) … is −1` with the
   assertion under test never firing — a bare `assertRaises` accepts that, and the
   assertion would have been recorded as proven on the strength of a kill a *different*
   assertion made. `assert_detects(..., expect=("V/amount-validation",))` is the shape.
   **The tag is what makes a detection attributable, and an attributable kill is the
   only kind that is evidence for a specific assertion.** This is the durable form of
   the "right verdict, wrong message" finding.
2. **The comment names the change that makes the defect live.** Without it the mutant
   reads as "this bug exists today", which is false and worse than no comment; with
   it, it reads as what it is. Named for the two shipped guards:
   - matrix key-check → **moving `_require_amount` inside `db.run_immediate` (or into
     `work()`), so validation runs after the transaction opens**;
   - shape-checker → **dropping `type(value) is not int` in favour of a `v == int(v)`
     shape check**.

**Both landed 2026-10-02 (test-author); T2's teeth are complete.** `mutants.py` now
carries `BurnsKeyOnBadAmountLedger` (RED at *amount 12.34 was rejected but recorded
key reject-0*) and `ShapeCheckLedger` (`int(v) == v`, with the bool trap guarded
explicitly so `1.0` is the **only** value that slips through — without that guard
`True` would also pass and `1.0` would be one of two, not the single separator).
`scenario_amount_validation` gained an `amounts=` parameter so the discrimination is
**proved rather than asserted**:

```
ShapeCheckLedger vs full matrix (12)   -> RED: amount 1.0 was accepted and applied
ShapeCheckLedger vs matrix minus 1.0   -> PASSES (checked 11)
CONTROL vs full matrix                 -> checked 12
```

`test_one_point_oh_is_the_only_value_that_separates_the_two` asserts both directions,
so **if some other value ever starts discriminating, the test fails and says the
matrix note is wrong** — the retained assertion notices its own documentation going
stale. Both mutants carry the passing control and the invariant tag
(`expect=("V/amount-validation",)`) and carry the required closing sentence: *nothing
here says the ledger has this bug today; it says the gate would see it if someone
introduced it.*

**Final certification target — the bytes that are T2's subject, not a tree pin:**
`scenarios.py 8925e2a9 · mutants.py 34cea7f4 · test_harness_selfcheck.py 92741d8a ·
test_idempotent_retry.py 344bb5bb · test_validation.py 33cbbd90`; ledger unchanged at
`core.py 42048fe2 · db.py 2978923a · schema.py da42efe5 · types.py 2da4d1eb ·
__init__.py d8ec93fe`.

**Corrected 2026-10-02 — this block previously named `TREE PIN c7350b37…`, and that was
the defect test-author caught.** `c7350b37…` **is not a byte set on disk any more**. It
predates `tests/test_wire_refusal.py` (`84378fd0`) and `tests/README.md` (`98e78f05`).
A closure anchored on it would certify a revision nobody can reproduce — precisely what
the pin rule exists to prevent.

**The standing ruling this produced: a whole-tree pin is not T2's anchor.** Re-cut
within the hour, the tree pin ran `c7350b37…` → `cb9b3cc5…` → `2fcbf380…`, and it will
move again, because `TREE PIN` covers `tests/ ledger/ api/` **together** while `api/` is
under active edit and `tests/` + `ledger/` are stable. T2 must close on the digests of
the files that are its subject — those did not move and the reviewer's pass matched them
exactly. **The tree pin belongs to T7**, and there it is quoted *from the run that
produced it*, never carried forward from an earlier message. Two anchors, two jobs.

**T2 CLOSED 2026-10-02 on the five digests above** — the reviewer's confirming pass
matched them exactly, with every mutant RED, every control GREEN, the shape-checker RED
on the full matrix and PASS on the matrix minus `1.0`, and `RefusalBurnsKeyLedger`
correctly PASSing the amount matrix, which is what keeps the two instances of the key
clause distinct.

**The wire fixture is deliberately not part of T2's scope.** `test_wire_refusal.py` is
`tests/`-side but its subject is the API boundary, so it is **T6's** coverage, and the
reviewer exercised it there (six of eight rows flip). Folding it into T2's certification
would give T2 a handoff edge on T3 that the sequencing above deliberately avoids. A test
count is recorded only as *the count reported by the run that produced these digests* —
the number moved 32 → 33 → 35 → 36 → 40 → 44 across the day with no edit from the
quoters, so a bare count is not an anchor and must never be quoted as one.

**The `is_minor` falsey-zero drive, accepted with the correction it carries.** Driving
the boundary on **both** read paths gives 57/59 with two FAILs. Injecting on a single
path reaches only 58/59, which **under-counts**: one read emitting `false` leaves the
other read's assertion green, so the second failure never appears. This is the same
rule as the `assertRaises` one — *a check passing does not tell you what it covered* —
and the first attempt's under-count was caught and re-run rather than reported.

Real ledger, all six obligations: parallel storm hot 50/50, hot_final 0 ·
retry storm 100 calls, applied 1, replayed 99, distinct transfer_ids 1 ·
overdraft race applied 10, affordable 10, src_final 0 · rounding sweep 608 pairs ·
atomicity raised IntegrityError with retry_status `applied` · fuzz 1234/150 with I1
and I2 asserted after every step. Broken variant still RED by direct drive:
applied 20/20, src −1000, I4 violated; control 10/20, src 0.

**Validation matrix — scope ruling.** The remaining non-integer amount matrix
belongs in `tests/` **against the `Ledger` interface, not against `api/`**. Two
reasons: `INVARIANTS.md` requires the ledger to refuse a non-integer amount at its
own boundary — the ledger is a library and the HTTP API is only one caller of it —
and a scenario in `tests/` that imported `api/` would give T2 a handoff edge on T3
that the sequencing above deliberately does not have. The HTTP-level equivalents
are already asserted by the T3 selfcheck and will be re-attacked in T6. Required
cases include **`True` and `False` no later than the rest**: `bool` is an `int`
subclass in Python, so an `isinstance(v, int)` check accepts them and `True`
becomes amount 1 — this is the exact trap §2.2 names, and it is the case most
likely to be missing from a hand-written list. Also `1.0` (a float that is
integrally valued and passes any `v == int(v)`-style check), `MAX_MINOR` accepted
and `MAX_MINOR + 1` refused, `0` and negatives refused.

### T3 — API surface, validation, and the gate runner

**Owner:** integrator · **Blocks:** T4, T6

**Definition of done**
1. `api/` implements exactly the §2.2 shapes, including `Idempotency-Key` as a
   header and `amount_minor` as a positive JSON integer. `api/` opens no database
   connection of its own — it goes through the ledger interface or the
   `ledger/db.py` factory.
2. Validation rejects `12.34`, `"500"`, `true`, `0`, `-500`, a missing key, and a
   key longer than 255 chars — each with the documented status; a rejected body
   never reaches the ledger.
3. 409 for same-key-different-body, never 200; a failed transfer never 200s.
4. `run_gate.sh` runs **four** phases and exits non-zero on any failure:
   (1) `python3 -m unittest discover -s tests -t .` · (2) `python3 -m compileall -q -f ledger api tests`
   · (3) `python3 -m api.lint` · (4) `python3 -m api.selfcheck`.
   Phase 4 was added 2026-10-02 at the integrator's request and the request was
   right: a gate that never exercises `api/` can be green with a broken API, so
   "the full gate is green" would not have covered the seam T3 exists to build.
   Determinism conditions, because a flaky phase is worse than no phase — bind
   port 0 rather than a fixed port, wait on readiness rather than sleep, bound
   the whole phase with a timeout so a hang fails instead of blocking, and count
   a transport failure as a failure. This phase is a **regression guard only**:
   the proof of the API boundary is T6's adversarial review, not the author's own
   self-check.
5. Owner quotes the exact command and its passing output.
6. **Owed 2026-10-02 — the wire half of "a refusal does not burn its key."** The
   ledger-engineer grepped `api/selfcheck.py` and established the gap: line 287
   asserts the **mapping** (`insufficient_funds → 422`) and lines 260–265 assert
   replay for a key that was **applied**, and nothing combines the two. So no check
   anywhere in `api/` refuses a transfer for insufficient funds and then retries the
   same key with a corrected body. `tests/test_idempotent_retry.py::
   test_a_refused_transfer_does_not_burn_its_key` pins this at the **ledger**
   boundary and stops there; the wire claim was attached to a ledger proof and is
   not established by either run.

   **Shape owed:** POST a transfer that fails `insufficient_funds` (assert the
   refusal status), then POST again with the **same `Idempotency-Key`** and a
   corrected, affordable body, and assert the contract's success status with
   `applied` and a **fresh `transfer_id`** — not `409 idempotency_conflict`, and not
   a replayed refusal. Paired with the failure-direction control: the same key with
   an **unchanged** body must still conflict, so the check cannot be satisfied by an
   implementation that simply never records keys.

   **Why it is a money path, not tidiness:** if the refusal claimed the key, the
   caller's retry with a corrected body compares fingerprints against a stored
   refusal and conflicts forever — the user can never complete that transfer with
   that key. If a refusal stored a **success** response, the retry replays `applied`
   for a transfer that never happened: money never moved, the caller is told it
   succeeded, and I1 and I2 both hold. It lives in `api/`, not `tests/` — a `tests/`
   scenario importing `api/` would give T2 a handoff edge on T3 and make `tests/`
   test a surface it does not own. **T7 does not close while this is unasserted.**

   **DELIVERED 2026-10-02 (integrator).** Eight assertions in `api/selfcheck.py`,
   appended at the end of `run()` with **fresh accounts** so the earlier
   `exactly 2 transfers / 2 idempotency rows / 4 entries` checks stay true at the point
   they execute — a different scenario's evidence was not blunted to make room:

   ```
   refused-retry: unaffordable body under K-rk           -> 422 insufficient_funds
   K-rk retried with a CORRECTED affordable body         -> 201 applied, FRESH transfer_id
   control: K-rk + the identical corrected body          -> 200 replayed, same transfer_id
   control: K-rk + the original unchanged refused body   -> 409 idempotency_conflict
   contrast: same unaffordable body, key with no history -> 422 refusal, not 409
   exactly one application under K-rk: payer 300-250=50, payee 250
   K-rk recorded exactly once (refusal and conflict wrote no key row)
   exactly one transfer moved money under K-rk
   ```

   Both readings of "the same key with an unchanged body must still conflict" are
   asserted rather than one chosen: the identical corrected body must **replay** (a
   never-recording implementation applies twice and returns 201) and the original
   refused body must **conflict** (a never-recording implementation returns 422 again).
   Neither alone proves recording; together they bracket it. The **contrast row** is
   what makes the 409 attributable to recording rather than to the body — without it, a
   ledger that conflicts on anything unaffordable would satisfy the conflict control.

   **Scope of the claim, stated by the author:** the assertions pass against the real
   seam and discriminate; it is **not** proven that they would catch a *specific*
   refusal-recording mutant, because no mutant reaches them and the integrator declined
   to write an `api/` mutant that would be themselves testing their own stub. **Ruling:**
   the discrimination question goes to the reviewer under T6 as an adversarial drive. If
   no mutant can reach it without crossing file ownership, the result is **not deletion
   and not silence** — the assertions stay and are labelled an **unproven-discrimination
   regression guard** with the reason stated, so they do not read as demonstrated. A
   money-path check that cannot be shown to fire is still worth keeping; keeping it while
   it reads as proven is not. Gate at delivery: `38 tests`, `63/63` api checks (up from
   52), both `TREE PIN` lines identical at `5e577e3a…`, no `PIN MOVED`, `exit=0`.

   **CLOSED 2026-10-02 on independent evidence (ledger-engineer), not on anyone's
   say-so.** Verified at `api/selfcheck.py`
   `aebea9430bb39643404007732578463c0f515c55a93f79770bc9b272299c99ce`, lines
   417–494: the refused-retry scenario is present, carrying the **same-key replay
   control** (452), the **same-key conflict control** (455) and a **fresh-key
   isolation control** (465) — the last being the row that makes the 409
   **attributable to recording** rather than to the body being refuseable in itself.
   The author had specified four checks in routing and five landed, so the hole in
   the routing was closed from outside it. **T7 is no longer gated on this item.**

   Still open on this block, and it is a different question: **discrimination** —
   whether a mutant can reach these assertions and flip the refused-retry row
   PASS → FAIL. Routed to test-author (fixture, in `tests/`) and verified by the
   reviewer under T6. The integrator established the route needs no `api/` change:
   `PocketfulHandler` resolves the ledger per request through
   `self.server.pool.ledger()`, so a driver can patch `api.app.LedgerPool.ledger`
   with a lambda returning a mutant and drive the real `api.selfcheck.run()`.
   Known wrinkle: `idempotency_keys.transfer_id` is `NOT NULL` with a reference to
   `transfers`, so a refusal-path mutant must **borrow a real `transfer_id`**.
   Acceptance is the **row flipping**, never a red run.

**Defect found 2026-10-02, assigned to the integrator, in `api/app.py`: the pool closes
zero connections, ever.** Surfaced by the `ResourceWarning` the integrator honestly refused
to fold into a green. `PYTHONTRACEMALLOC=25` traced the allocation to
`api/app.py:270 → :51 → ledger/db.py:45` — created on a **worker thread**. `sqlite3`
close is thread-affine (measured: **4 of 4** connections created on handler threads raised
`ProgrammingError` on close from the main thread, `closed cleanly: 0/4`). `LedgerPool.close()`
runs on the **main** thread from `server_close()` and wraps each close in
`except Exception: LOG.debug(...)` — so **every close fails, is swallowed at debug, and the
docstring's promise is met zero times.** Not a test artifact: `close()` is only ever called
from the main thread and every pooled connection was made on a worker thread, **so this
happens on every production shutdown too.** SQLite masks it; the only symptom is the
warning, which a green gate hides entirely. **Same species as deploy-engineer's
`2>/dev/null` post-mortem and as the swallowed-failure family in general: a failure path
that reports nothing is a failure path that is not there.** Fix stays in `api/app.py` — close
each connection in the thread that opened it (`process_request_thread` → `finally:
pool.close_current_thread()`), and change the swallowed `LOG.debug` to a visible warning. No
`ledger/` change: `check_same_thread` at its default is the ledger's documented
one-connection-per-thread model working as designed; the defect is on the shutdown side.
**Ordering ruled: this fix lands BEFORE the final closure run** (the integrator asked rather
than guessed, and preferred the same), so T7's last word covers the client *and* this — a
defect left open behind a green gate is exactly what that gate exists to prevent.

### T4 — Client: wallet UI and idempotent send

**Owner:** frontend-engineer · **Blocks:** T6 (client half), T7, T8 · **AMENDED and
IN PROGRESS 2026-10-02.** §1's client row is **Python 3, stdlib only**, the last open
row in the table. `app/` **exists and is delivered** — eight modules, all eight
sha256-verified independently by the planner, with a runnable evidence driver and a
mutant control that genuinely double-debits.

**Amendment 2026-10-02 — the human asked for it web-facing.** *"i want something web
facing … visble on the website pocketful.getn.space where we can test it"*. This is an
**extension of T4, not a re-open**: the send path, the key store and the float boundary
already exist and are exactly what the web surface needs. Python serves the pages; the
browser is a **view**.

**The contract this amendment introduces, and it is the reason the amendment is safe:**
> **The browser never generates, holds, or transmits an idempotency key.** The key is
> minted and persisted by the Python layer **before** the first attempt and reused
> verbatim by every retry. A page that minted its own key would re-mint on reload and
> turn a retry into a **double-spend the ledger cannot detect** — because it is a
> different key, and therefore a different transfer.

This is the same defect the whole build exists to prevent, and putting a browser in
front of it is the first opportunity for someone to reintroduce it by accident. It is
written here as an interface obligation, not as advice.

**Why the client row was blocked at all (kept, because the reasoning still binds).** The
client's language had never been decided — T0 settled the ledger, datastore, wire and
test runner and named nothing for the client — and the host has no JS runtime, so the
choice determined whether the key-persistence obligation was testable in the gate at
all. The human's answer keeps it testable and keeps the build inside the no-install
property.


**A consequence stated so it is not a surprise later:** with a Python client, the
"wallet UI" is a Python-served UI — a terminal app or a stdlib-served page — not a
browser product with client-side JavaScript. The kickoff's own framing, *the hard part
is not the UI*, is why that is acceptable. If the human meant a browser-facing app,
this re-opens; it is flagged to them.

**Definition of done**
1. Key generated on Send, persisted **before** the first attempt, reused on every
   retry including a simulated app restart (two keys for two presses, one key for
   one retry) — proven by a test that fails if the key is regenerated.
   **Sharpened 2026-10-02 (integrator's proposal, ratified):** generate at
   request-construction time and hold the key *alongside the request*, so a retry
   reuses it rather than regenerating; persistence must survive a **process kill**,
   not just a re-render, and must be keyed to the outstanding request. The failure
   this prevents is specific: a key regenerated on retry turns a retry wave back
   into a double-spend, and the ledger cannot stop it, because it is a different key
   and therefore a different transfer. The test must kill the process between submit
   and response and show the retry reusing the same key with the ledger **replaying**
   rather than applying — reading the persistence code is not evidence.
2. Grep proof, quoted, **in the Python form now that the client is Python**: no
   `float(` on a sent or stored amount, no `round(` on money, no float literal in the
   money path, and amounts crossing the wire as `int`. (This item named
   `parseFloat`/`toFixed` when it was written against an assumed JS client; that
   assumption died with the stack question, and the item is restated rather than
   quietly satisfied by grepping for strings the chosen language cannot contain.)
3. Balance and activity come from the server; the client never computes a balance
   by summing a list.
4. Owner quotes the exact command and its passing output, plus the sha256 of every
   file they wrote.

**STATUS 2026-10-02: CLOSED, THEN REOPENED the same day on the reviewer's live
double-spend (see the REOPENED block below — read that first; the closure narrative that
follows is what was true before it).** Item 2 was briefly owed — the client's first evidence
lived in `app/selfcheck.py`, a driver sitting in the same directory as the code it tests
and **not run by the gate**. By this room's rule *scratch-proven is not a fixture*, a
driver beside its subject is scratch, however good it is, and this one was good: real
`api/` on a real socket over a real `ledger/`, a child SIGKILLed **after** the server's
201, a genuinely separate process resuming from the persisted record, and a mutant
control that really double-debits. **The gap was closed by test-author, not by the
owner**, with a gate-resident test in `tests/` (`test_client_retry.py 83248119` +
`t4_client_child.py 146d8edf`, the child deliberately not named `test_*.py` so discovery
leaves it alone). The gate now runs **48 tests, bound by `run_gate.sh`** rather than
merely present.

**Independence, not duplication, is what makes the second artifact worth having.**
test-author built a recorder on their own side of the wire and refused to depend on
anything inside `app/selfcheck.py` — not its recorder, not its mutant. **Two artifacts
that agree because they were built independently corroborate; two that agree because one
is a copy of the other prove nothing.** The test asserts on the `Idempotency-Key` header
as the client put it on the wire, plus SQL over `idempotency_keys`/`transfers`/
`ledger_entries`; **the client's store file is a fixture carrying state between two
processes, not an oracle.** Four structural choices are recorded because each is the
difference between a test and a demonstration: the crash is deterministic from inside the
documented transport seam (no sleeps, no timing windows); the mutant **asserts its own
premise** (the two keys differ) so it fails rather than passing vacuously if the mutation
stops mutating; and **the crash window is asserted in its own test** — ledger applied,
record still pending — so that if the kill ever landed on the wrong side of the settle,
that test fails first instead of the resume assertions quietly testing a different
scenario.

**The kill window is the right one and worth naming.** Killing *after* the ack is the
deterministic worst case: the ledger has certainly committed, the client cannot know,
and that is exactly the window in which a client that re-mints its key double-spends.

**Float hygiene exceeded the DoD's letter.** The item asked for a grep proof; the owner
supplied an **AST** check. Spot-checked by the planner: the only float-family occurrences
in the money modules are prose in a docstring. Strictly stronger than the grep requested,
so recorded as exceeding the requirement rather than as a deviation.

**Web amendment — delivered 2026-10-02 (`app/web.py f9de7cf2`).** stdlib `http.server`;
routes `GET /`, `POST /send`, `POST /retry`, `GET /health`; a client of `api/` that
imports nothing from `ledger/`. **The contract that the browser never holds a key is
enforced three ways, each verified rather than asserted:**

1. `app/web.py` contains **no** `new_idempotency_key`, no `Idempotency-Key`, no `uuid` —
   confirmed by the planner running the grep, not by accepting the claim. The browser
   sees a rendered view: no form field, hidden input, cookie or URL carries a key.
2. `POST /send` answers **303 See Other** (Post/Redirect/Get), so a browser refresh
   issues a GET and cannot replay the POST.
3. **Rendering never writes** — `records=0` after rendering, which catches the case where
   the act of *displaying* a pending transfer creates state. This is the leg most likely
   to be missed: it is how a view quietly becomes a writer.

**REOPENED 2026-10-02 — the PRG is real but scoped to the success path, and the reviewer
demonstrated a live double-spend on it.** This is the most important finding of the build
and it reopens T4. `_handle_send` calls `_redirect(...)` **only in its `else:` branch**
(`app/web.py:302-303`); every failure branch calls `_render(...)`, which returns the HTML
page *as the response to the POST* — so after a retryable failure the displayed page **is**
the POST's own response, and a browser refresh re-issues it. Re-entering `wallet.send` →
`store.begin` mints a second key (`keystore.py:167` mints unconditionally), a second
transfer applies, and Retry then sends the first key too.

```
start: alice=1000  records=0
STEP 1  POST /send 1.00 -> UI 202, body is HTML, records=1, keys=['e4190ff4'], alice=1000
STEP 2  RELOAD on that page (re-issues the POST) -> UI 303 /?sent=applied
        records=2, keys=['397928cf','e4190ff4'], alice=900
STEP 3  Retry -> 303 /?retried=applied, records=2, alice=800
one 1.00 user action moved 200 minor units; 2 distinct keys used
```

**I verified the mechanism on the source myself rather than taking it on report** — read
the branches: `_handle_send` renders on every failure path and redirects only in `else`;
`keystore.begin` mints with no dedupe against an outstanding identical record. **Both
halves of the reviewer's sentence are exactly right.**

**Both transfers are individually legitimate — distinct keys, each applied exactly once —
so no ledger invariant fires.** The money leaves through the store, past every invariant
the gate proves. This is verbatim the failure `app/web.py`'s own docstring names: *"a page
reload that re-mints a key is a double-spend the ledger cannot catch."* The contract was
written down and then implemented on one branch only.

**Why the client's own harness was green on it, and this is the pattern to keep.** `python3
-m app.selfcheck` → `36/36 passed`, including *`[WEB] reloading minted no key (still one
record)`* — but that reload is a **`GET /` reached via a 303**, the page the PRG *does*
protect, not a reload of the POST response, which no check covers. **Same shape as the
`rk_keys` `COUNT == 1` clause: a check that cannot see the defect it names.** The reviewer
was explicit that their verdict does not cite the green harness as evidence — the green
harness *is* the blind spot. That distinction is the whole value of the finding.

**Ruling 1 — the fix goes in the HTTP layer, and the "obvious" store fix is forbidden.**
The tempting repair is to make `PendingStore.begin` dedupe against an outstanding identical
record. **Do not.** §2.3 item 1 says two presses of Send are **two** transfers — the store
*must* mint on each call, which means it **cannot** distinguish a browser reload from a
second press. Only the redirect can. A dedupe there would silently merge two legitimate
transfers, which is the money bug this whole build exists to prevent, arriving as a fix.
**No outcome of `POST /send` or `POST /retry` may leave a refreshable POST as the displayed
page:** every branch redirects (303) and the outcome renders on the GET.

**The reviewer strengthened this from the code, and the strengthening is the better
argument: the dedupe is not merely forbidden, it is impossible.** The reload re-issues the
POST body byte-for-byte — same `to_account_id`, same `amount`, same derived
`from_account_id` — so nothing in the request distinguishes press #1 from the reload. **The
only differing state is the browser's address bar, which never reaches the store.** Any
`begin` dedupe can only key on the body, and §2.3 item 1 says the body is *supposed* to be
indistinguishable across two legitimate presses. **The rule and the physics agree:** the
information needed to fix this exists only in the HTTP layer, where the request carries its
own history. A store dedupe would not be a bad fix — it would be a **rename of the central
bug**.

**Ruling 2 — the test must reload the POST response, not a GET.** A gate-resident test that
re-issues a `GET /` passes on the buggy code — that is precisely how `app/selfcheck.py` went
36/36 over a live double-spend. The test must model the **browser**: if the POST response is
a `3xx`, follow it with a GET; if it is a `200`/`202` HTML page, re-issue the POST. Then
assert one record and one key. On the current code the second branch is taken and the record
count goes to 2 (**RED**); after the fix the first branch is taken (**GREEN**). A test that
cannot take the second branch is not a test of this defect.

**Ruling 3 — the bare `ValueError` rides along.** `Wallet.send` raises a bare `ValueError`
for a same-account send (`wallet.py:119`), which `_handle_send` does not catch — it catches
`InvalidMoneyInput`, a *subclass* — so that path raises out of the handler instead of
rendering a 400. Not money-moving, but an unhandled handler exception is not a status either
way. Same owner, same handler: fix it in the same change.

**Ruling 3a — scoped by the reviewer from the code, and the scope is "hygiene, not money".**
They checked `app/wallet.py:117-119` before agreeing: the same-account check raises at 119
and `_send` is not reached until 120, so on that path **no key is minted, no record is
persisted, no transfer is constructed.** It is an unhandled exception producing a `500`
where a rendered `4xx` belongs — the same class as the `[WEB]` label gap, **not an invariant
repair.** Consequence for the change: it travels with the `web.py` fix because it is the same
owner and the same handler, but it is **labelled separately and asserted separately** — its
own failing-then-passing assertion (a same-account POST answers `400`, not `500`) — and it
does **not** enter the double-spend's evidence. Bundling is fine; blurring the two defects
into one claim is not.

**FIX SET LANDED 2026-10-02 — inspected by the planner, awaiting the reviewer's
re-certification.** Frontend-engineer: `app/web.py 9e254fac`, `app/wallet.py cecc3e75`,
`app/keystore.py 859288a3`, `app/selfcheck.py f9595717`, `app/__init__.py 4e7ea5a7` (the
shadowing fix, now due). Integrator: `api/app.py 27094373`. What I read myself, and it is a
reading not a certification:

- **Every branch of `_handle_send` and `_handle_retry` now redirects** — no `_render`
  remains in either POST path. `_handle_send` answers `/?error=missing_fields`,
  `/?error=invalid_amount`, `/?error=same_account`, `/?pending=1`, `/?error=conflict`,
  `/?error=<code>`, `/?error=api`, `/?sent=<status>`; `_handle_retry` mirrors it. **The
  displayed page is now always the result of a GET.**
- **`PendingStore.begin` still mints unconditionally** (`keystore.py:176`) — the forbidden
  dedupe was *not* applied, and the prohibition is now **written into the method's
  docstring** (`keystore.py:163-171`), which is the best place for it: the next person to
  consider the "obvious" fix reads why it is a rename of the central bug. Behaviour
  unchanged; a comment, not a patch.
- `do_GET` now parses the query (`parse_qs(parts.query)`) and renders the outcome there.

**And the replacement row has teeth, proven both ways by its author.** Against a scratch
copy with the pre-fix `_handle_send` (the `else: _render(...)` shape) the harness goes
**14/18**, and among the four failures is the row failing **for exactly the defect it
names** — `[WEB] POST /send answers 303 with no page body … [status=502 location='' body=
'<!doctype html>…']`. With the fix in place, `40/40`, three consecutive runs, no flake. That
is the shape a replacement row must have: it must fail on the artifact that carried the bug,
not merely pass on the artifact that dropped it.

**And the second-press row is the positive proof that the forbidden dedupe was not
applied.** The harness now asserts that a **genuine** second press produces `303`,
`records == 2`, payer `9300` / payee `700` — two legitimate transfers, both applied. A store
dedupe would have merged them and this row would fail. So the rule is enforced by an
assertion rather than by a comment, which is the difference between a documented prohibition
and a tested one.

**One new surface the fix creates, flagged for the re-certification rather than declared
safe by me.** The reviewer's attack 1 proof rested partly on *"`do_GET` never parses the
query, so a key cannot arrive by URL."* **That is no longer true** — the query is parsed
now, so `/?error=<arbitrary text>` is a reflected sink whose fallback is
`f"The transfer failed ({error})."` I read that `html.escape` is applied through the
`_esc` helper (`web.py:79`) and that `message`/`notice` reach the template via
`render_wallet`, but **I did not trace the fallback through to the leaf** — so the honest
statement is: the escape helper exists at the edge, and **confirming every path uses it is
part of the re-certification, not an assumption to inherit.** Attack 1 is re-run, not
grandfathered, on the fixed bytes.

**Red-evidence ruling, because the ordering slipped and the slip is real.** The fixes landed
before test-author's red test, and with **no version control the pre-fix bytes are gone** —
so "fails without the change" cannot be produced by running the new test against the old
file. It is still evidenced, two ways, and both are stated as what they are:
1. **The reviewer's live pre-fix demonstration at `web.py f9de7cf2`** — one press plus one
   reload, 200 minor units moved — is on record and *is* the failing behaviour, obtained
   independently before any fix existed. That is the strongest available form of the
   discriminator, and it was obtained in the right order even though the test was not.
2. **The new test must still show its own teeth**: run against a **scratch** stub that
   renders the page as the POST's response (the pre-fix shape), it must go RED. That stub is
   labelled scratch, not gate evidence — the same distinction test-author already applied to
   their `/tmp` probe.

**Lift, then re-freeze.** The freeze's condition — the reviewer's verdict — is met, so
`tests/` is **LIFTED for the T9 test only**. When that test lands, the freeze **re-imposes
across all four trees** for the reviewer's re-certification, because the re-certification is
a claim about bytes and the fix set is only now complete. The reviewer asked for the fresh
digest set to be sent; that is owed and will be sent with the re-freeze.

**The red-first window closed when the fix set landed, and the direction of a red now
matters.** The test lands against **fixed** bytes, so it should be **GREEN on arrival**; the
RED is demonstrated against the **scratch pre-fix stub**, exactly as frontend-engineer did
for the `app/` row (14/18, failing for its named defect). **Consequence, and it is a rule
rather than a note: a RED on the current fixed bytes is not an expected red — it is a
disagreement between the gate and the harness about the same tree, and it stops the line.**
Route it to the `app/` owner before the reviewer is asked to certify; an author's harness and
the gate disagreeing on identical bytes is precisely the case worth halting on, and the one
outcome that must never be absorbed as "expected". The frontend-engineer asked for this in
advance, unprompted, which is why it is cheap to honour.

**T9 TEST LANDED, GREEN ON ARRIVAL, AND THE FREEZE IS BACK ON.** `tests/test_web_reload.py`
(`126976ae…`, 18294 B), four tests. I read it and ran it myself before re-freezing:
`Ran 4 tests in 0.685s — OK`. It is green on the fixed bytes, which is the predicted and
only acceptable colour (the rule directly above). What it does and does not do, read off the
file rather than inferred from its name:

- **It asserts on the ledger, not the page** — persisted record count, `COUNT(*)` of payer
  debits in `ledger_entries`, payer `SUM(amount_minor)`, and the client keys actually present
  in `idempotency_keys`. That is forced and correct: *"Nothing was sent
  twice" is printed by exactly the code path that sent it twice.* A page-reading assertion
  would have gone green across the live double-spend, which is the whole defect.
- **It models the browser explicitly.** `_press_then_reload` asks the POST's own response what
  it was: a 3xx is followed with a **GET**, a 200/202 body is answered by **re-issuing the
  POST**. So the test cannot accidentally reload the safe verb — the failure mode that let the
  old `[WEB]` row report 36/36 over a live double-spend.
- **It has teeth in the required shape.** `test_the_pre_fix_shape_inverts_the_assertion` runs
  the identical flow against `PreFixSendHandler` — the pre-fix `_handle_send` reconstructed as
  a handler, since the broken bytes are gone — and calls **the same `_assert_reload_is_inert`
  the contract test calls**, requiring it to raise with `"T9 second key minted"` in the
  message. It does not assert the inverted numbers and hope they agree; it requires the
  named invariant to be the thing that fires, then records the shape of the break (2 records,
  2 distinct keys, 2 debits, payer `1000 - 2*100`, 2 keys on the wire).
- **It patches nothing global.** The client is injected per server via
  `WalletUIServer(..., wallet=...)`, so unlike `test_wire_refusal.py` it carries **no
  cross-file serialization requirement** — the `_drive()` constraint does not apply here.

**Re-freeze is in force across all four trees.** `TREE PIN 131f271f…`, cut three times one
second apart with the gate's own command and identical each time. The fresh digest set has
been sent to the reviewer, which is what it asked for before it would re-cut. From here the
only remaining writer is the integrator cutting the single final `run_gate.sh`.

**The author's own two runs, quoted, and a ruling on a deliberate deviation.**

test-author ran both halves and reported them with the digests each ran against — which is
the discipline the room asks for, and the reason I can quote the numbers rather than the
conclusion:

- **GREEN, on the fixed tree**: `TREE PIN (before) == TREE PIN (after) == 131f271f…`,
  `Ran 52 tests in 8.179s` (the 48 plus the four new, reached by `discover`), `63/63 checks
  passed`, `GATE GREEN`. Ran against `tests/test_web_reload.py 126976ae`,
  `tests/README.md 0638d63e`, `app/web.py 9e254fac`, `app/selfcheck.py f9595717`. **That the
  before and after pins are equal is the part that matters most here** — it is independent
  corroboration that the freeze held across a full gate run, and it agrees with my own cut.
  The `63/63` is unchanged from the pre-T9 green while the test count rose `48 → 52`; the two
  numbers count different things (unittest cases vs. the harness's internal checks), so this
  is not a discrepancy — stated because two counts over different sets read as a
  contradiction if the denominators are not named.
- **RED, on the pre-fix *shape***: not old bytes — they are gone. The branch is reconstructed
  as `PreFixSendHandler` and the **contract tests themselves**, not merely their helper, are
  run against it from a scratch subclass (`/tmp/t9_red_demo.py 2655394c`, outside all four
  trees and therefore outside the pin). Three fail, and they are exactly the three contract
  tests: the control (`202 not found in (301, 302, 303, 307, 308)`), the reload test (`the
  press answered 202, so the reload had to be a POST`), and the retry test (`2 != 1`). The
  fourth — the mutant test — passes, because it exists to expect breakage. **That division is
  the correct one**: a teeth test that fails when the mutant is absent is confirming the
  mutant, not the contract.

**RULING — the mutant stays in the gate; the author offered to move it out and I decline.**
They flagged that I authorised a scratch stub and they instead put `PreFixSendHandler` in
`tests/test_web_reload.py` as a permanent always-run mutant, in the `tests/mutants.py`
pattern. **Keep it.** The reasoning is the room's own: the gate is re-runnable evidence, and
a `/tmp` demo proves the branch once, on one host, at one moment — precisely the failure mode
this room has already been bitten by (an artifact whose hash proves nothing about whether it
still says anything). A permanent mutant re-establishes falsifiability on **every** gate run,
and the `/tmp` layer then adds what the mutant cannot: the contract *test* going red, not just
its helper. The two layers are complementary, and having both is better than the letter of
what I authorised. **Condition attached, and it is a statement about the file's character
rather than a formality:** the file is now both a contract test and a mutant container, so
the mutant must stay labelled as such in-file (it is), and its nature must not be hidden by
how any future count is phrased.

**Flagged to the reviewer as a named item, because I will not rule on it myself: the
reconstruction's fidelity to `f9de7cf2`.** The pre-fix branch no longer exists on disk, so
`PreFixSendHandler` is now the *only* witness of the defect it models — and no hash can
validate a reconstruction against deleted bytes. That makes the mutant's faithfulness to the
real pre-fix `_handle_send` a judgment call, and judgment about fidelity belongs to the
reviewer's attack 2, not to me. The specific question: does `PreFixSendHandler` reproduce the
branch as it actually was, or has it been quietly strengthened into a shape that fails more
readily than the original? I have read it and believe it is faithful — it reproduces the
`except RetryableSendError` branch answering the POST with `_render(..., status=202)`, which
is the pre-fix shape — but *believe* is the operative word, and the author is not the right
certifier of their own reconstruction.

**Scope boundary the author drew correctly, and I am holding them to it.** Their verdict is
about `tests/` and the `web.py` fix; the `[WEB]` row in `app/selfcheck.py f9595717` is
frontend-engineer's and they have not audited it. Accepted as stated — it is the right
boundary — and noted that this row's evidence is currently verified by me from disk and by
its author, **not by the reviewer**, which is worth closing in the same pass.

### T9a — the seventh exception: an uncaught path answers nothing at all

**Found by the reviewer, verified by me on disk, and it blocks the certification.**

`_handle_send` and `_handle_retry` enumerate their `except` clauses
(`app/web.py:321-349`, `:351-364`): `InvalidMoneyInput`, `SameAccountSend`,
`RetryableSendError`, `IdempotencyConflictForSend`, `TerminalSendError`, `ApiError`. But
`app/send.py:138` raises a **seventh** type that is none of them, and `app/send.py:93` raises
it too:

```python
    if not isinstance(transfer_id, str) or status not in ("applied", "replayed"):
        raise SendProtocolError(f"unusable transfer response: {response!r}")
```

Neither handler catches `SendProtocolError`, and `do_POST` (`app/web.py:287-295`) has no
catch-all. I confirmed all three facts by reading the files, not from the reviewer's summary.
So the exception escapes the handler, the connection closes with **no status line**, the
browser receives nothing, and it is therefore still sitting on `POST /send`. Its reload is a
re-POST. `store.begin` mints a new key. **The exact condition the fix exists to remove,
reached through a door that a grep for `_render` does not look behind.**

**The reviewer's demonstration, and their honesty about its reach, which I am reproducing
because the distinction is the whole ruling.** They drove it end to end — real api server,
real UI server, real ledger, injection disclosed and not file-edited
(`ApiClient(base, transport=)`, the production seam): press #1 returns
`status=None Location=None`, the reload / re-POST yields
`records=[('pending','2680e996'), ('pending','a04255af')], payer=800, server_applied=2`, and
**three distinct keys at the server for one user action.** But they also state the other
direction plainly: the trigger is a 2xx body that parses as JSON but carries no usable
`transfer_id`, and **the current `api/` never emits that** — a genuinely malformed body fails
`json.loads` and becomes `ApiError("malformed_response")`, which *is* caught. So the missing
`except` is **certain from the code path; the double-spend is demonstrated under one
disclosed injection whose trigger the current `api/` does not emit.** They explicitly left
the block/certify call to me.

**RULING — fix it, and fix the shape of the guarantee, not just the missing clause.** The
reason is not that this trigger is reachable today; it is that **"every branch redirects" was
an enumerative claim and enumeration is exactly what failed.** Six of six lists were right and
the seventh path was invisible, which means the property was never structural — it was a
claim about the completeness of a list, checked by reading the list. A money-path handler
whose contract is *the browser is never left on a POST* must hold that property for
exceptions its author did not foresee. So the fix is two-part:

1. **An explicit `except SendProtocolError: self._redirect("/?error=protocol")` in both
   `_handle_send` and `_handle_retry`** — the record correctly stays `pending` for `resume`,
   so no other behaviour changes.
2. **A final catch-all so an unforeseen eighth type still lands the browser on a GET.** An
   unhandled exception in this handler is not merely a 500 — it is *no response*, which is a
   re-POST, which is the double-spend door. Answering `303 /?error=internal` is strictly safer
   than closing the connection, and it makes the property structural rather than enumerated.

I am not prescribing the catch-all's exact width; I am requiring that the owner **state why
their form is complete**, and that the answer is not a longer list. Owner: **frontend-engineer**
(sole owner of `app/web.py`).

**Second finding, correctly scoped by the reviewer, and it is a wording fix not a defect.**
With the terminal-refusal branch reverted to the pre-fix shape, `tests/test_web_reload.py`
still reports `Ran 4 tests … OK`. The reviewer measured the consequence instead of assuming
it: on that branch a reload re-posts and is refused again — `records=0, payer_debits=0,
payer_balance=1000`, **no double debit** — and every non-retryable `ApiError` path discards
the record (`send.py:127-131`) or raises before `store.begin`. So **the branch that carries
money is the retryable one, which the test does drive; the gap is coverage, not a live
defect.** The only change owed is one sentence in `tests/README.md` obligation 11, which says
"if a failure branch answers the POST with the rendered page" while one branch is exercised —
the same label-over-claims-scope shape this room keeps removing. Owner: **test-author**.

**Freeze discipline, because this moves bytes under a held certification.**
`app/web.py` reopens for frontend-engineer and `tests/` reopens for test-author; `ledger/` and
`api/` stay frozen. The reviewer holds at `f1424bed` and does **not** certify on
`131f271f…` — that set is now known-incomplete by its own finding. When both changes land I
re-freeze and send a **new** digest set, and the certification re-runs on it. This is the
rule from earlier in this build applied to myself: a legitimate reason to move the bytes is
still a moved pin, and the reviewer finds out from me rather than by re-hashing.

**The pool change is assessed and closed, and the reviewer answered it in the form I asked
for.** `api/app.py 27094373` is a **resource defect, not a money-path one**: 60 requests across
20 threads leave fds flat at 5 and `pool._connections` back to 0 each time, and the hook is in
the right thread (`process_request_thread`'s `finally` runs `close_current_thread()` in the
worker, the only thread that may close a thread-affine handle). On the invariant question they
measured rather than argued: `ledger.db.connect` uses `isolation_level=None` so there is no
implicit BEGIN, `run_immediate` commits or rolls back on every path including the `BaseException`
branch at `db.py:91-97`, a deliberately leaked connection reports `in_transaction is False`, and
a *second* connection took `BEGIN IMMEDIATE` on the same database in `0.0000s`. No held write
lock, no unread write. **So the old failure mode was descriptor/WAL growth until process exit —
availability and hygiene, not a way to create, destroy, or apply money twice.** Recorded as
closed and explicitly kept out of the client verdict, which is what I asked for; T10 needs no
further work.

**And the one thing that must not be carried over from the withdrawn set, stated so nobody
assumes otherwise.** The reviewer did the full audit on `131f271f…` before my withdrawal
reached them, and re-cutting the tree pin themselves and getting my value byte-for-byte is the
strongest form that check takes. But **the attack passes are bound to the bytes they ran on, and
`app/web.py` is precisely the file T9a changes.** So when the new set goes out:

- **Attack 2 must be re-run, not carried.** Its PASS is a statement about `web.py 9e254fac`, and
  the whole point of T9a is that `9e254fac` still has a path with no redirect.
- **Attack 1 must be re-run if the fix touches the render path.** Its trace runs through
  `_render_feed:310 → _render:320 → render_wallet:123 → _esc:79`; a catch-all clause that alters
  how `message` is produced sits on that path. If the fix leaves those four untouched, say so
  and the trace can be re-asserted against the new digest rather than re-derived — but "the
  bytes changed" is not a reason to assume, and the reviewer is the one who decides which of
  those two it is.
- **The pool assessment carries in full**, because `api/` is frozen. That is what the freeze
  line is for and it is the one item that genuinely survives the withdrawal.

This distinction — between *work that is wasted* and *work whose subject moved* — is why the
withdrawal was cheap. Nothing the reviewer did on `131f271f…` was wasted; two of its outputs
are simply re-statements about different bytes.

**Clause 2 has a scoping constraint, raised by the reviewer before the fix exists, and it is
now part of the instruction.** A catch-all that merely lands the browser on a GET is not yet
safe. **The catch-all must not swallow the case where money has already moved.** An unforeseen
exception that fires *after* the server applied the transfer leaves a `pending` record — which
is correct, because `resume` replays that same key and the outcome is a replay, not a second
transfer. That property holds **only while the record is left pending.** A catch-all that also
*discarded* the record, or that *settled* it without a `transfer_id`, would convert a benign
unknown into a lost key and re-open the door from the other side. So the safe form is
`303 /?error=internal` with **the record untouched**, and the verification checks the record's
state after the injected throw, not just the status code. Board #12 carries this.

**And clause 2 is verified by injection, not by reading the catch-all — the reviewer's own
method, adopted because it is the only one that answers the question that was actually
asked.** Reading a catch-all and agreeing it looks broad is the same failure as reading six
`except` lists and agreeing they look complete. So the test of "your form is complete" is: **inject
a synthetic exception type the author did not foresee**, raised from inside `wallet.send`'s path
under the real handler over real HTTP, and require a 303 with the browser landed on a GET, with
the record's state checked. If the catch-all is structural, an eighth type nobody wrote down
still lands the browser on a GET; if it is another list in disguise, that probe goes straight
through it. Reviewer independently reproduces this on the frozen bytes; test-author lands the
gate-resident form of it under #13.

**`PreFixSendHandler` — judged faithful, with a narrowing that must be quoted whenever it is
cited.** The reviewer was in the unusual position of having measured the pre-fix behaviour
*while the pre-fix bytes existed* (`UI answered 202, body is HTML page? True, length=2759`,
`records after reload: 2, alice=900`), so the target is an observed behaviour and not just code
they read. Against that: the reconstruction matches `f9de7cf2` branch for branch on the path
the scenario takes, produces the same observable, and **was not strengthened** — no added
branch, no stricter guard, no router that answers differently; the only differing string is the
notice text, which no assertion reads. The honest caveat is a **narrowing**: it drops the
empty-field guard and the `InvalidMoneyInput` / `IdempotencyConflictForSend` / `TerminalSendError`
/ `ApiError` branches, so on those unmodelled branches it would raise and close the connection
rather than render — harsher than the original, but unreachable from
`test_the_pre_fix_shape_inverts_the_assertion`, which drives the retryable branch only.
**Standing wording: faithful for the branch it witnesses, explicitly narrower than `f9de7cf2`
overall, and not weakened in any way that affects its result.** If anyone later wants it to
witness "render on *any* failure branch", the four branches go back in — that is a scope
question, not a fidelity defect, and it is not owed now.

**NEW DEFECT — a fifth label over-claiming its scope, found by the reviewer in the `[WEB]`
audit, in `app/selfcheck.py:377-381`.** The row is labelled **"the key was minted and persisted
in Python before the attempt"** and it asserts only

```python
records = PendingStore(store_path).all_records()
check(len(records) == 1 and records[0].state == "pending"
      and records[0].amount_minor == 500 and records[0].to_account_id == payee, ...)
...
check(transport.sent_keys == [key], ...)
```

**Both reads happen after the POST has returned.** The store is never read *during* the
attempt, and `_LostResponseTransport.__call__` (`:114-122`) records the header on the way out
and never touches the store. So a variant that sent first and persisted afterwards would put
the same key on the wire and leave the same single record behind, **and this row would still be
green.** The label is a temporal claim; the assertions are a cardinality claim. This is
persistence-before-first-attempt — §2.3's durability rule and the thing that makes a retry a
replay rather than a fresh key — so the row asserting it must actually see the ordering. Fix:
read the store **inside the transport at send time** (`store.get(headers["Idempotency-Key"]) is
not None`, or capture `all_records()` then) and assert the record was already on disk when the
request left. Owner: **frontend-engineer**, board **#14**. The rest of the `[WEB]` block checks
out: `:368` asserts a redirect with no page body (the property the old row lacked),
`:394-399` and `:419-425` refresh the outcome page reached via the 303, the `:372` GET is the
redirect target rather than the POST response, `:349-352` searches for the literal and a UUID
against `new_idempotency_key() = str(uuid.uuid4())` so the proxy matches the real key shape,
and `:446-453` — two presses → two records, payer 9300 / payee 700 — is the row that turns red
if anyone ever reaches for the forbidden store dedupe.

**The reviewer corrected their own citation, unprompted, and it is recorded because it is the
same rule they enforce on everyone.** They had described the new row's assertion
(`status == 303 and location.startswith("/") and "<html" not in body.lower()`) as living in
`tests/README.md`; it is real but it is **`app/selfcheck.py:368`**, and the README describes
ledger-based assertions while `tests/test_web_reload.py` asserts record count, debit count,
`idempotency_keys` and balance. Substance unchanged, and the `tests/` module is the better home
for the reason the README gives. Flagged by them, against their own interest, because a
citation has to be the one you read.

**T9a and #14 CLOSED, and the re-freeze is on `37ba30c5…`.** All three changes landed and every
writer stopped. I ran the gate **twice** and cut the pin **three** more times with the gate's
own command, one second apart: `37ba30c5…` before and after in both runs, no `PIN MOVED DURING
RUN`, `Ran 55 tests`, `63/63 checks passed`, `GATE GREEN`. The frontend-engineer's earlier run
had printed `PIN MOVED DURING RUN: 7d5ff08d… → 37ba30c5…` because `tests/` had a concurrent
writer, and **they were right to refuse to bind their exit status to it** — that is the rule
working. The new digest set is with the reviewer (`16c14208`).

**The form clause 2 took, read off the bytes rather than the report.** `do_POST`
(`app/web.py:301-328`) is a single exit guard: `self._responded = False`, `try:` dispatch,
`except Exception: LOG.exception(...)`, `finally:` `if not self._responded:
self._redirect("/?error=internal")`. `_send_bytes` (`:222-236`) sets the flag the instant
`send_response()` commits a status line and is the **only** byte-writer, so no handler can
forget it; the `finally` covers normal return, early return, caught exception, uncaught
exception, and any handler added to that dispatch later — none of them named. The clause-2
constraint holds: the `finally` calls only `_redirect`, and `SendProtocolError` is raised
before `store.settle`, so the record stays `pending` with `transfer_id is None` and `resume`
stays a replay. **Record untouched, checked, not inferred.**

**And the author's honest residue is kept, because it is the difference between a true claim
and an inflated one.** *"No exception or early return before commit can leave a POST
unanswered"* — **not** "every branch redirects". A failure *after* a status line is committed
(a broken socket mid-body) leaves the flag `True` and the guard does not answer again; that
case cannot be redirected from inside the process, and a second answer on a half-written
connection would corrupt the response rather than save it. That sentence is now the standing
form of the claim, and it is what the reviewer will test against — by injecting a synthetic
eighth type, which is the only test of completeness that is not a list read.

**#14 was scoped wrong by me, corrected by the reviewer, and the correction is recorded against
my name.** I framed it as possibly a live durability defect — *"if the store write happens
after the request goes out, a crash in that window loses the key."* The reviewer measured
instead of arguing: `app/send.py:75-81` calls `store.begin(...)` before `_attempt(...)`, and
`begin()` mints and `_save()`s under the `RLock` (`app/keystore.py:180-182`); instrumented, the
call order is `save(8363439b…) → wire POST → save(8363439b…)`, and the transport reading the
store **from disk at send time** finds the record already `pending`. **So §2.3's
persistence-before-attempt holds in the bytes today, and #14 is a check that cannot see a
regression — not a durability bug.** Filed as "fix the missing persist" it would have sent
someone to edit working code, which is the false alarm the reviewer is there to prevent. The
row is now split: the over-claiming label narrowed to what it actually asserts, plus a new
temporal row reading the store **file** at send time.

**Three mutants now live in `tests/test_web_reload.py`, and the third is the important one.**
`PreFixSendHandler` (the original defect), `NoExitGuardHandler` (the live `do_POST` minus the
guard), and `DiscardingExitGuardHandler` — which answers `303` **and** discards the record. The
third passes every page-level assertion and **only the record-state assertion catches it**,
which is precisely why clause 2's constraint could not have been a status-line check. All
three reconstruct bytes that no longer exist, so their fidelity is the reviewer's judgment
item and not the authors', per the standing rule; test-author said so in the file rather than
being told.

**A conflict in my own instructions, caught by the owner rather than by me.** The
frontend-engineer flagged that my notes said *"only `app/web.py` and `tests/` are reopened"*
while #14 assigned `app/selfcheck.py` to them. **They read #14 as the instruction and edited
it, which is correct**, and the earlier sentence was written before #14 existed. Recorded
because the failure mode is mine and the recovery was theirs: when a later instruction
conflicts with an earlier scope line, the instruction wins **and the scope line gets corrected
in public** — which is what this paragraph is.

**CERTIFIED on `37ba30c5…`.** The reviewer re-cut the pin independently and matched it, and all
fourteen named digests are byte-identical. Their three re-runs and two fidelity judgments:

- **Attack 1 — re-DERIVED, not re-asserted, and the reason is the strongest instance yet of
  the "name your subject" rule.** They could not run it as a diff because **`9e254fac` is not
  recoverable on this machine and `_render_feed` does not exist in `f9de7cf2` at all** — the fix
  introduced it, so there is no earlier revision of the three bodies to compare against and
  *"untouched" would have been a reading, not a measurement.* They re-derived the trace on the
  landed source and re-ran the payloads: **6/6 inert**, including the new `internal` key, plus
  `sent`/`retried`/`pending`. One escaper at the leaf, after the only unlisted-value f-string.
- **Attack 2 — full re-run on an isolated stack**: `303 /?pending=1`, reload is a GET,
  `records=1`, one debit, balance `900`, one key on the wire. **Their first harness shared one
  ledger across sections and read section B's spend as section C's — their bug, stated as
  theirs, not the code's**; each section now gets its own server, ledger and payer.
- **Clause 2 — their own injection.** `Unforeseen(Exception)`, on no `except` list and not an
  `OSError`, raised *after* the transfer applied: `303 /?error=internal`, record left
  `('pending', None)`, reload inert, and `resume()` by hand replays that key and adds no debit.
- **All three mutant fidelities judged** — and two became *measurements* rather than readings,
  because the reviewer mined the real pre-fix source out of their own session transcript:
  `PreFixSendHandler` reproduces both defect-carrying lines and nothing else, **narrower than
  `f9de7cf2`, never stronger**; `NoExitGuardHandler` is the live `do_POST` minus the guard,
  with the honest note that it is behaviourally `9e254fac` for this test and not byte-identical
  (`9e254fac` also lacked `except SendProtocolError`, which the test never reaches);
  `DiscardingExitGuardHandler` measured — `303`, page renders, reload inert, **and `records=0`
  with the debit standing.**

**RULING on the reviewer's non-blocking note, and I am ruling it IN rather than deferring it.**
The new temporal row at `app/selfcheck.py:470-472` discriminates — the reviewer falsified it by
hand, `[(key, False)]` on a not-yet-durable store, firing the row — **but its teeth live only in
a comment and a scratch run, with no gate-resident send-first mutant.** That is, one level up,
the exact shape this row was created to fix: *a claim whose witness cannot fail.* The room's
standard is explicit — a test that cannot fail is not a test — and the reviewer applied it to
create the row. Applying it one level down instead is not gold-plating; it is the same rule.

**And the bound is what makes ruling it in cheap.** It is a **test-only** edit: `ledger/`,
`api/` and `app/` stay byte-identical. So the money-path certification on `37ba30c5…` is
**scoped to `app/`+`api/`+`ledger/`, and its subject does not move** — the bounded re-check is
the new test file plus a re-cut pin, not a repeated certification. This is the reviewer's own
principle turned on my sequencing: *evidence scoped to its subject survives when the subject
does not move.* Owner: test-author, board #15. Then re-freeze, bounded check, and the
integrator's single final run.

**The reviewer sharpened that into the rule, and it is now the standing form.** *A verdict is
scoped to its subject's digests; the tree pin belongs to the whole-tree claim.* Their
certification is a claim about the **ten money-path digests** — `web.py aa2ccccc`,
`selfcheck.py b5a7366c`, `api/app.py 27094373` and the seven unchanged `app/` files — **not**
about `37ba30c5…`, which will move when a test file changes. So the carry holds or fails on
whether those ten are byte-identical after #15, and **the reviewer re-cuts the tree themselves
and confirms it rather than citing a pin they did not measure.** They also refuse to carry
their own probes across the re-freeze even though the subjects are unchanged: `rev_recert.py`
touches nothing in `tests/`, so it *should* reproduce — and *should* is not a report, so it is
regenerated and the numbers are reported. Both refusals are the rule pointed at their own
convenience, which is the direction that counts.

**A condition attached to the carry, which is also an instruction to the owner.** If #15's
falsifier cannot be built inside `tests/` — if it needs a seam added to `app/send.py` or to the
store to make the send-first ordering injectable — then it **moves a money-path subject and the
carry is void**; the reviewer re-cuts everything. The instruction to test-author is therefore
**stop and say so, rather than quietly add a seam to a certified file.** My own reading is that
the test-only shape exists (the row reads the store through the transport, so the falsifier is
plausibly a test-side store subclass that defers `_save`), but that is the planner's reading and
not the owner's decision, and **the failure this condition exists to catch is a seam added
quietly to bytes nobody asked to have re-certified.**

**CONDITION ANSWERED — the reviewer built it rather than agreeing with me, and the carry is not
at risk.** Prototyped in `/tmp/rev_seam.py`, *not* in `tests/` (their rule: do not edit the area
you review — the same boundary that has held all build). The shape is the one guessed:
subclass `PendingStore`, count `_save` instead of writing, flush after the press.

- **Control, real `PendingStore`**: `POST /send → 303 /?pending=1`,
  `store_at_send = [('744ae033', True)]` — the row's condition is `True`, as it must be.
- **`DeferredSaveStore`, flushed only after the send**: `_save` deferred once, **the file does
  not exist at send time**, `store_at_send = [('8c42def6', False)]` — **the row's condition is
  `False`, so the row FIRES.** After `flush()` the record is `('pending', '8c42def6')`: a build
  that persists, just late.

**That last detail is what makes it worth having, and it is the reason the prototype matters
more than the agreement.** The falsifier is a **plausible wrong fix** — persist-after-send is a
realistic mistake with a real record left behind — not a degenerate missing-file case that any
assertion would catch. **No seam, no edit outside `tests/`, carry intact.**

**And a trap handed over with the prototype, which is the kind of thing that only surfaces by
building it:** deferring *every* `_save` defers `settle()` too, so if the mutant is driven
further than the first press, the later rows in the `[WEB]` block read a stale file and report
**someone else's defect**. Scope the deferral to the first write and flush right after the
attempt. Relayed to the owner with the prototype.


**Ruling 4 — the green `[WEB]` row is the blind spot and must be replaced, in `app/`'s own
harness.** test-author located it precisely: the `[WEB]` block is `app/selfcheck.py:331-410`
and the dead row is **378-384, whose reload at line 379 is a `GET /`** — the page the
success-path 303 already protects. **A row labelled "reloading minted no key" that reloads
the wrong thing is the same defect as the `rk_keys` count: a check that cannot see what its
label claims.** It is the owner's file (`app/selfcheck.py` is frontend-engineer's), and
test-author correctly refused to reach outside `tests/` to fix it — *"writing outside
`tests/` costs me the independence the gate rests on"* is the right instinct and is now the
rule for every non-`tests/` owner. So the `app/` half is: fix `web.py`, fix `wallet.py`, and
**replace that row** so it reloads the POST response.

**Ruling 5 — the gate test lands FIRST, deliberately red.** test-author asked rather than
chose, because a red gate is a shared signal and sequencing is mine. **The answer is
red-first.** Three reasons, in order of weight: (1) a green gate currently reports healthy
over a live double-spend, and leaving that in place while we fix it means the room's only
whole-tree signal is **false** for the duration; (2) landing it red is the discriminator
proof the DoD demands — *the test fails without the change* — obtained before the change
exists, rather than reconstructed after; (3) the room's rule is that a red gate is red and is
never made green by editing a test, so there is nothing to lose by announcing it. The red is
**deliberate and named**: the failing assertion is the reload-of-the-POST-response record
count, and the two fixes that turn it green are already assigned. This is the one case where
a red gate is the *correct* intermediate state rather than a defect.

**The web layer made the key store concurrent, and the store was fixed in the same
change.** `app/keystore.py` gained a `threading.RLock` around every read-modify-write and
its durable save (`keystore.py:87`), because two concurrent `begin()` calls could
otherwise lose one another's record — and **a lost record is a retry that finds nothing
pending and mints a fresh key**, which is the double-spend arriving through the store
rather than the wire. The changed digest was **called out by the owner rather than
slipped in**, which is what makes hash discipline work at all.

**Documented traps, recorded rather than fixed — with the cost stated.**
- **`app/__init__.py` re-exports a `send` function that shadows the `app.send` module**,
  so `import app.send as send_mod` silently binds the function and `send_mod.resume`
  raises `AttributeError`. It cost test-author ten minutes. **Not fixed now,**
  deliberately: every edit to `app/` re-pins digests that three agents anchored on today,
  and this one fails **loudly**, not money-silently. Fix it with the next `app/` change
  made for any other reason.

  **Deferred on evidence, not on the argument — the owner checked the condition instead of
  reasoning about it.** The deferral was conditioned on "it fails loudly rather than
  money-silently", so frontend-engineer **ran** it: `type(app.send)` is `function`;
  `import app.send as m; m.resume` → `AttributeError`; `from app import send; send.resume`
  → `AttributeError`; `from app.send import resume` → OK; and `app.wallet`, `app.keystore`,
  `app.client`, `app.money`, `app.keys` all stay modules — **`send` is the only shadowed
  name.** No path to a wrong amount or a double-spend; the worst case is a crash on
  attribute access. That is the loud failure the condition asked for, so the deferral
  holds. **The mechanical fix, recorded so it is one line when the next `app/` change
  lands:** delete `send = _send.send` from `app/__init__.py`, expose it as
  `send_transfer = _send.send`, drop `"send"` from `__all__`. The invariant that keeps
  the class of trap from recurring is then statable: **never bind a package-level name
  equal to a submodule name** — `retry`, `resume`, `wallet`, `client`, `money`, `keys` are
  all latent collisions of the same shape. (`app.web` merely is not auto-imported; that
  one is ordinary Python, not a shadow.)

  **And the owner committed, unprompted, to make no `app/` edits — including this fix —
  while the reviewer's pass runs.** All nine files are inside the digests the reviewer is
  hashing, and a file that moves mid-review invalidates the whole pass; the trap is loud,
  so holding still for an hour costs nothing. That is the right trade and it is why the
  freeze below holds without me having to enforce it.
- **The wire checks must not be parallelized.** `tests/test_wire_refusal.py`'s `_drive()`
  patches `api.app.LedgerPool.ledger` as a **process-global class attribute** and
  restores it in a `finally`. That is safe across processes and safe for sequential calls
  in one process, and **unsafe for two concurrent callers in one process**: the second
  would have its seam restored underneath it, **silently drive the real ledger, and read
  a green.** Not a flake — a false green that looks like a pass. Moving that seam from a
  class attribute to per-instance is the precondition for parallelizing. Recorded as a
  hard constraint on how the wire checks are run, not as a note.



### T5 — Adversarial review: ledger, idempotency, transactions

**Owner:** reviewer · **Blocks:** T7

**Output:** a written verdict that names, per invariant, either the proof or a
defect with the exact input and the wrong output. Checklist:
floats; unbalanced entries; weak idempotency (key source, storage outside the
transaction, replay returning a different result, same-key-different-body applied
silently); check-then-act and inherited isolation; truncating splits; tests that
pass before the fix or are sequential where concurrency is required. A money-path
defect is **blocked**, not a comment.

### T6 — Adversarial review: API boundary and client transfer construction

**Owner:** reviewer · **Blocks:** T7

**Output:** same form as T5, scoped to validation (coercion instead of rejection),
error honesty (200 on failure, 200 on same-key-different-body), and the client's
key lifetime and float hygiene.

**API half ANSWERED 2026-10-02 — and the answer split a category I had been treating
as one.** Of the eight new refused-retry assertions in `api/selfcheck.py`, **six flip
under the mutant**, and **two correctly do not**:

- the `refused-retry → 422` row, because the mutant **re-raises** — the refusal's own
  wire verdict is genuinely unchanged, so that row is not a discrimination target; and
- the fresh-key isolation row, which is a **control that gives the recorded-409 its
  meaning**, not an assertion that needs a mutant behind it.

**My earlier framing of the contrast row as the one to "look hardest at" was wrong and
is withdrawn.** I was asking a control to be a mutant — the same confusion in
miniature as reading an assertion's scope from its neighbour. The recording property
is discriminated by the `409 conflict` control, which does flip. The distinction that
matters is between *a row that cannot fail for a good reason* and *a row that should
see the defect and cannot*; naming which is which is what stops "unproven
discrimination" from becoming a blanket label covering both.

**The finding acted on — a row whose label is false and which passes anyway.**
`api/selfcheck.py:~490`, labelled *"K-rk recorded exactly once (the refusal and the
conflict wrote no key row)"*, asserts `SELECT COUNT(*) … WHERE key='K-rk'` = 1. Under
the mutant that count also returns **1**: the mutant writes exactly one K-rk row, on
the refusal, borrowing a pre-existing `transfer_id`. So the assertion coincides at 1
while the label's claim — that the refusal wrote **no** key row — is false for that
run. **A count that cannot see the defect it names.** A row whose label claims a
property its assertion cannot observe is worse than no row, because it reads as
covered.

**Ruling: pin provenance — the reviewer's refinement adopted, and then tightened by the
reviewer themselves.** I originally framed this as provenance *or* relabel. The reviewer
said pin provenance *and keep the count*, because the two guard **opposite directions** —
the count catching a defect that records **both** the refusal and the apply (two rows),
provenance catching the defect that records the refusal **instead** (one row, wrong id).
That is right about *what needs covering*. **The arithmetic of "both" is one assertion
either way**, which the reviewer checked and corrected: `rk_transfer_ids == [retried_id]`
is **list equality**, so it **subsumes cardinality** — a defect writing the refusal row
*and* the apply row yields `[borrowed, retried_id] ≠ [retried_id]` and fails the same
clause. Both directions are covered, by one assertion rather than two stacked ones. The
surviving separate count is the **transfers** row at `[58]`, a different row doing a
different job — not a second guard on the key rows. *Two assertions were never needed;
one assertion with the right shape was.*

**Landed and measured 2026-10-02** — `api/selfcheck.py
a7d67c838e0ae65c39a8ae82fccc48205d51812194ce8677b86e5a496fff37ae` (re-cut independently
by the planner and by the integrator; both agree). The block keeps `rk_rows == 1`
**relabelled to what it observes** — *"exactly one transfer moved money under K-rk"*,
a claim about **transfers**, where the old label made a claim about **key rows** — and
adds `rk_transfer_ids == [retried_id]`, the provenance assertion, with the reason
written into the file rather than only into the room. The count's old label named a
property its assertion could not see; it now names one it can.

**Measured, not predicted: the mutant flips 7 rows, not 6 — and the seventh is the new
provenance row.** Driven over `api/selfcheck.py` in full — **59 rows, control vs mutant**
— the flips are indices `[46]` *"exactly 2 idempotency rows exist"* (3, not 2), `[52]`
corrected-body retry, `[53]` replay control, `[54]` unchanged-refused-body control
returning **200 "replayed" with `created_at` `never`** — a refusal replayed as a success,
for money that never moved — `[56]` the money row, **`[57]` the surviving K-rk key row
points at the APPLIED transfer**, `[58]` the transfers count.

**Attribution corrected 2026-10-02, and the error was mine.** I told the reviewer the
seventh row was the scenario-wide count at `[46]`. It is not. At `aebea943` the six rows
that moved were **already** `46, 52, 53, 54, 56, 58` — `[46]` was among them, because the
earlier refusal burns a key there too — so what takes 6 → 7 is the **new provenance row
at `[57]`**. **The extra flip belongs to the fix, not to a pre-existing count** — the
opposite of what I reported, and it matters, because it had credited the improvement to
something that was already there. The integrator's first explanation made the same slip;
test-author had it right.

**The flip set has a shape worth naming: 0 of the 27 rows before the first refusal
moved.** The mutant disturbs only rows **downstream of a refusal** — the same property
test-author had to correct once already, when the fixture's premise read "no row before
the refused-retry row". Two independent arrivals at it is a good sign for the fixture,
not a coincidence.

**Two denominators, kept apart on purpose.** The earlier *"six of eight"* figure was
scoped to the **eight new refused-retry assertions**; this one is **7 of 59 rows**
across the whole file. They are not one series and must not be read as one. The block
gained one flipping row because the fix **added** the provenance row — the count the
reviewer should expect at re-certification is **7 over that 59-row drive**, and a
different number is a signal to investigate, not to adjust the expectation.

**The integrator's predicted 6 was wrong and they corrected it themselves**, after the
message had already been sent, by driving `test_wire_refusal.py`'s own `_drive()` both
ways instead of reasoning about the block in isolation. That is the room's rule working:
the reasoning produced 6, the measurement produced 7, and the measurement was published.

**Ruling on the second trim-safe clause** — `retried_id != transfer_id` at
`api/selfcheck.py:447`, reported by test-author as comparing against K1's id. It is
**kept, as an accurately-labelled guard, and no mutant is required for it.** This is
not the K-rk case, and the difference is the point: the K-rk row *claimed* to
discriminate a property it could not see, so it was made to see it. This clause now
carries a comment stating exactly what it is — *"a no-aliasing check, that the id is
one this call produced rather than borrowed from another key"* — and names the clause
that **does** rule out a replayed refusal (`status == "applied"` with the 201). An
assertion whose label matches its scope and whose discriminating neighbour is named is
a documented guard; an assertion whose label overclaims is a defect. Test-author was
explicit that their evidence here is *"no mutant I have built separates them"*, not
proof of trim-safety, and named the mutant that would promote it — a ledger returning
**200 replayed with a fresh id**. That mutant is not required now; if anyone wants this
clause to be a discrimination rather than a guard, that is the one to build. Routed to the integrator (`api/` is
their file); `tests/` is not touched for this, and if the fixture must expose the
applied transfer id the ask goes to test-author through the planner. The reviewer's
re-certification is **bounded to the wire block** — T2 is not re-done.

**The wire fixture itself, recorded.** `tests/test_wire_refusal.py` (the only file in
`tests/` that imports `api/`) patches `api.app.LedgerPool.ledger` with a ledger
subclass that, on `InsufficientFunds`, writes a **committed** idempotency row under the
caller's key before re-raising. Two facts a future editor needs: its stored response
dict must carry **no `"status"` key**, or the replay's `TransferResult(status="replayed",
**stored)` raises `TypeError`; and **its pin can never be complete**, because it imports
`api/` — so any verdict on it carries that caveat. The premise that was once RED here
("disturbs no row before the refused-retry row") was false because `run()` contains an
**earlier** refusal; the true statement is "only disturbs rows **downstream of a
refusal**".

**T6 wire/API half CLOSED 2026-10-02 — re-certified on `api/selfcheck.py a7d67c83`.**
All thirteen digests re-hashed from disk and matching; `Ran 44 tests … OK`, `63/63
checks passed`, `GATE GREEN`, pins identical either side. The new provenance row was
driven both directions by the reviewer on their own apparatus:

```
control: PASS  K-rk transfer_ids=['4d5ade57…'] applied=4d5ade57…
mutant:  FAIL  K-rk transfer_ids=['5d699f2a…'] applied=None
```

`applied=None` because the burned key made the corrected retry 409, so there is no
applied transfer for the surviving row to point at. **The fixture that found the hole
proves the replacement has teeth** — reported, implemented, re-run on the same
instrument, with no new mutant needed. **T6's client half stays open** and revisits after
T4's web surface lands.

**T6 client half — VERDICT IN 2026-10-02: BLOCKED on attack 2.** Three of four targets
hold (no key material proven by capturing every response — not grepped; rendering writes
nothing, instrumented `_save()` called 0 times over 20 `GET /`; the `RLock` holds under 24
threads × 40 `begin()` = 960/960 in memory and on disk). **The 303 is a real PRG only on
the success path** — a live double-spend on the retryable-failure path, detailed under §4
T4's REOPENED block. All nine `app/` digests re-hashed by the reviewer at the moment of the
run and matching; wire checks serial throughout. The reviewer's fault injection was
disclosed and was at the client's own injectable seam (`ApiClient(base, transport=)`) with
no file edited — which is the difference between a test and a patch. **The verdict does not
cite `app/selfcheck.py`'s 36/36 green as evidence; the green harness is the blind spot, not
a counter-argument** — that sentence is the finding's value.

**The four targets it attacked, recorded so the next pass can reuse the list.** Whether
`app/web.py` carries key material (proven by capturing every response, not grepped); whether
the **303 PRG** genuinely makes a refresh a GET rather than a replay (**this is the one that
failed**); whether **rendering writes nothing** (instrumented `_save()`, 0 calls); and
whether the store's `RLock` can still lose an update (960/960, it cannot). **The wire checks
were run serially per the `_drive()` constraint in §4 T4** — a concurrent run would silently
drive the real ledger and read a false green.

**Scope ruling, because the reviewer asked rather than assumed.** They flagged that
`app/` landed and **nobody had put it in front of them** — `app/send.py` constructs
transfers (`idempotency_key`, `amount_minor`, `idempotency_conflict`, and `retry`/`resume`
whose docstring says they must never mint a new key), `app/client.py`, `app/wallet.py` and
`app/money.py` are client money code, and `app/keystore.py`/`app/keys.py` handle key
material. Asking *"is this in scope for me, and under which task?"* instead of starting
an unasked review is the correct move, and it is the same discipline as refusing to
extrapolate a check's scope from its neighbour. **Answer: yes — `app/` is T6's client half,
the delegation was sent (`ed09305b`) and crossed their message, and the pass is pinned to
the nine `app/` digests.** Client code that constructs a transfer was always their remit;
what was missing was the assignment, not the mandate.

Note the reviewer's tree pin on that pass was `dd84bf8f…`, which is **not** the
`2fcbf380…` I had quoted to them — `tests/README.md` moved when test-author retracted a
stale measurement. Their verdict is anchored on `a7d67c83` and the five test digests,
**not** on the tree pin, and the changes now in flight (lint widening, a new `tests/`
file) touch neither, so the verdict does not lapse and they were told not to re-cut for
them.

### T7 — Full gate green, evidence quoted

**Owner:** integrator · **Blocks:** — (this closes the build)

**Definition of done:** `run_gate.sh` green on the final working tree, with the
exact command and its full output quoted in the room, and T5/T6 signed off. A
green claim without quoted output is not completion.

**Amended 2026-10-02 — the quote must carry a revision anchor.** The gate count
moved 32 → 33 → 35 → 36 during the day with no edit from the quoters, so a bare
number proves nothing about which tree it describes. The quote must be accompanied
by the tree hash the run read (`sha256sum tests/*.py | sort -k2 | sha256sum` for the
suite, plus the `ledger/` per-file pins), and the numbers must be taken from the run
rather than from reading the tree.

**Owed on the client half, via T4.** The idempotency-key obligation is the client's,
not the ledger's, and it is the one place a correct ledger can still be made to
double-spend by a wrong caller. When T4 lands the client surface, T7's evidence must
include a test that **kills the process between submit and response and shows the
retry reusing the same key**, with the ledger replaying rather than applying. A
persisted key demonstrated by reading code is not evidence that it is persisted.

**Blocking defect found 2026-10-02: the gate cannot see the client at all.**
Verified by the planner on the script itself, not taken on report — `run_gate.sh:70`
is `TREE_PIN_TREES=(tests ledger api)` and `:108` is `compileall -q -f ledger api
tests`. **`app/` is in neither the tree pin nor `compileall`.** So the gate can report
green over a client it never compiled and never pinned, which makes a green gate
**silent** about a whole top-level package — the exact class of blind spot the
integrator caught earlier when a tests-only pin failed to cover `ledger/` imports,
one directory over. **T7 cannot be signed off until `app/` is inside the pin and the
`compileall` set**, and both `GATE PIN` and `TREE PIN` will move when it is, which is
the reason the change is sequenced *before* the wire-block re-certification rather
than after it: one certification on the final arrangement beats two on a moving one.

**Rulings 2026-10-02 — where the client's gate evidence lives, and what the pin covers.**

1. **`app/selfcheck.py` does NOT become a fifth gate phase.** Making it one would take
   the exact artifact this plan calls *scratch* — a driver sitting beside the code it
   tests — and make it load-bearing, which is the worst of both: authoritative *and*
   checked by nothing but itself. **The client's gate evidence is a test in `tests/`**,
   which needs no new phase to run, because phase 1 is `unittest` discovery over
   `tests/`. **T7's DoD therefore stays one command**, and the four-phase freeze holds.
   The integrator offered to amend the DoD to two commands rather than quietly
   reinterpret it; that instinct is why the answer could be one command instead of two.
2. **`app/selfcheck.py` stays, as a documented companion command** — a human debugging
   the client in isolation runs it, and it is the source of the mutant control showing a
   regenerated key double-debits. It is labelled **not the gate's evidence**, so nobody
   later mistakes the driver for the thing that vouches.
3. **The pin widens to `app/` — and only widens because the gate now reads it.**
   Measured 2026-10-02 and re-cut independently by the planner: `tests ledger api` =
   `dd84bf8f…`; `tests ledger api app` = `ea30c73c…`; `app/` alone = `feff122b…`.
   Widening a pin without making the gate read the tree would produce a **pin that
   overstates its coverage** — the integrator's phrase, and the correct one. Real
   coverage needs all three: the tree in the pin, the tree in `compileall`, and a test
   that imports it.
4. **Condition for revisiting:** if the `tests/` version proves too slow or too
   process-heavy for phase 1 — it spawns a child that SIGKILLs itself — a fifth phase is
   reconsidered **on measurement**. Not predicted, not hedged.

**The integrator's framing of the defect is the sharpest statement of it, and is kept:**
*"a money-critical evidence file that no command runs is the same species as everything
else this room has spent the day hunting."* The fix for "nothing runs it" is not "add it
to the gate wherever it sits" — it is "put it where the gate already looks."

**`app/` is now inside the pin and `compileall` — proven, not asserted.** The integrator
placed `def broken(:` at `app/broken.py` on a scratch copy and showed the phase fail
(`SyntaxError`, `compileall exit 1`), then showed it regenerate all eight modules after
`rm -rf app/__pycache__`. Both halves matter: the failure path proves the phase bites,
and the positive path closes the subtler hole of a banner that **names** `app` while the
command never receives it.

```
GATE PIN  BEFORE  2bb21c135309ed90d5ad8c25d5b7620e51c949d6e96f2e124dd243d049fc3544
GATE PIN  AFTER   12970b9b81e8726618cfe7eb9653f2a820e66a7e1b73cce047e4f986a4a4cc24
TREE PIN  BEFORE  dd84bf8fb2de9d1fabaec55df3adcec291a1a8264e0dc3c9e0d4b757eca38102  (scope: tests ledger api)
TREE PIN  AFTER   ea30c73c8af27ff958f7089c005999cc75c648a424aecc576729024d6ed47d91  (scope: tests ledger api app)
```

**Those two tree values are not drift and must not be read as a sequence.** The **scope
changed**, so `dd84bf8f` and `ea30c73c` are different *statements* about the same tree —
one directory's worth. The integrator recomputed both scopes independently and both
match what the gate printed, which is what kept a scope change from being misread as an
edit.

**Third gap found by the same sweep, and it is being fixed rather than scoped away.**
The **lint** phase still covers `('ledger', 'api')` only — `api/lint.py`'s own tree
constant. Pointing it at `app/` in memory produces three findings, **all three in the
module docstring at `app/money.py:7`**, which *names* `Decimal`, `parseFloat` and
`toFixed` in prose while asserting they never touch a money value. The linter matches raw
source text and so cannot tell a comment from a call.

**STATUS 2026-10-02 — the three blockers on T7, and where each stands.**

1. **`app/` inside the tree pin and `compileall` — LANDED.** `run_gate.sh` now reads
   `TREE_PIN_TREES=(tests ledger api app)` and byte-compiles `ledger api tests app`;
   `GATE PIN` moved `12970b9b… → a57e0d5a8e60a7b03a33777d60e24af990796a49d88d77b6f1aad2ee9137bb85`
   (re-cut live by the planner from the file, not quoted from the integrator). Proven by
   the `app/broken.py` scratch check, both halves.
2. **The client's gate-resident test — LANDED.** test-author's `tests/test_client_retry.py
   83248119` + `tests/t4_client_child.py 146d8edf`; the gate now runs **48 tests**, bound
   by `run_gate.sh` phase 1 rather than merely present (see §4 T4).
3. **Lint widening to `app/` — LANDED (token-aware), after one false-red.** The first
   landing widened `TREE` to `('ledger','api','app')` **without** making the scan
   token-aware, so the gate went **RED** on the three `app/money.py:7` docstring tokens —
   a module punished for *documenting* the rule it follows. The reviewer caught it live
   (`exit 1`, pin quiet either side, `api/lint.py` mid-edit) and named it a **false
   positive, not a money-path defect**. Fixed as ruled — matcher, not docstring — by
   blanking comments and string-literal text with stdlib `tokenize` (`api/lint.py
   297db9fb`). **Proof it still bites, not just that it stopped complaining:** every line
   the old raw-text scan flagged in a **code** position still fires; everything it flagged
   in **prose** is gone; `('ledger','api')` still reports the same 12 modules and the same
   zero findings. The f-string case is deliberately kept as code. **No new misses, only
   removed false positives** — which is the shape a correct lint fix has.
4. **The gate is GREEN on the current tree — verified, not accepted.** Two independent
   runs, both exit 0, both quoted with their pin: the **integrator's** `TREE PIN
   8e8b2ba3…` (`48 tests`, `63/63`, `lint: clean` over 21 modules) and the **planner's**
   re-cut at `TREE PIN c9e8f913…`. They differ because `tests/README.md` moved between
   them — **documentation inside a pinned tree moves the pin**, which is the same lesson
   this plan has been teaching all day, now landing on T7 itself.
5. **FREEZE until T6's client half returns, then ONE final run.** Ruled 2026-10-02: no
   edits to `tests/`, `ledger/`, `api/` or `app/` until the reviewer's client-half verdict
   is in, because a file that moves under a reviewer invalidates the whole pass — the
   frontend-engineer committed to exactly this unprompted, and it is the right instinct.
   **Sequencing, so the gate is cut once and not twice:** reviewer's T6 client-half verdict
   → if it is clean, the integrator runs `run_gate.sh` on the now-frozen tree and quotes
   the command, the output, and the `TREE PIN` **from that run** → T7 closes. If the
   verdict finds something, the fix lands first with fresh hashes, and *then* the run. A
   green gate on a tree that moves afterwards is a green about the old tree.
6. **Open, and owned: the `ResourceWarning: unclosed database`.** The integrator's green
   run carried it — a warning, not a failure, `48/48 OK`, attributed to `api/errors.py:83`
   where GC happened to run rather than where the connection was made. **Not folded into
   the green claim and not dismissed either**; a connection not closed before teardown can
   hide a pool that leaks under load. Assigned to the integrator to triage the *source*
   (server pool under `api/`, or the test's own handle); if it turns out to be the test's,
   it goes to test-author through the planner, because `tests/` is not the integrator's to
   edit. It does not block T7 — it is a named open item, which is the difference between a
   known warning and an ignored one.
7. **Five values for one tree in an hour is the ruling working, not a mistake — keep the
   list.** `c7350b37… → cb9b3cc5… → 2fcbf380… → dd84bf8f… → abb741a4… → 8e8b2ba3… →
   c9e8f913…`. test-author read this as evidence *for* the rule and asked for it to be
   kept; they are right, and the reason is that the sequence looks like drift and is in
   fact the opposite: each value was correct when cut, and each move is a real edit by a
   named owner (T4 landing, `app` entering the pin, the scope-note refresh, a README
   correction). **A tree pin is a statement about a moment.** The failure mode it guards
   against is not "it moved" — it is "someone quoted a moved value as though it were
   current", and the fix is the re-cut, not a stabler number.
8. **Publication is gated on T7's green revision, and no earlier.** deploy-engineer has
   the substrate standing and the smoke test done, and has correctly held the vhost at
   503. **The instruction stands: do not stage or publish until the tree pin from T7's
   final run is cut and quoted.** deploy-engineer asked, rather than assumed, which
   revision to stage — and the honest answer is that it does not exist yet, because the
   reviewer's T6 client-half verdict may still move `app/`. Staging a revision that is
   about to be superseded is the same defect as a certificate pinned to a stale bytes.
9. **The verdict landed 2026-10-02 and it is BLOCKED — so the final run moves again.** The
   reviewer's T6 client-half pass found a **live double-spend** on the failure path (§4 T4's
   REOPENED block). The freeze therefore ends the way it was designed to: the fix lands with
   fresh hashes, the reviewer re-certifies against them, and **only then** is the final
   `run_gate.sh` cut and T7 closed. **A green gate was never the thing in doubt here** — the
   gate was green, 48 tests, when the reviewer ran the defect end-to-end through the real
   ledger. The gate cannot see this class; the adversarial pass is what sees it, which is
   why T7 was sequenced *after* T6 and not around it.
10. **The fix set, and the order it lands in.** Four changes, four owners, no file shared:
    test-author lands the **gate test first and deliberately red** (§4 T4 ruling 5);
    frontend-engineer fixes `app/web.py` (303 on every POST outcome), `app/wallet.py` (the
    uncaught `ValueError`) and **replaces the dead `[WEB]` row** at `app/selfcheck.py:378-384`
    (§4 T4 ruling 4); the integrator fixes the pool close in `api/app.py` (§4 T3). **Then, and
    only then**, the reviewer re-certifies the fixed `app/`+`api/` on fresh hashes and the
    integrator cuts the final `run_gate.sh` on the frozen tree. **Every one of these files is
    inside the tree pin**, so the freeze holds across all four until the last fix is in.

**A note on the lint fix, recorded because it is the same overstatement in miniature.**
The gate currently reads as if it lints money-path code; `app/money.py` is money-path code
and is not linted. That gap is *known and named* rather than absent, which is why it is
being closed rather than tolerated — an unnamed gap becomes an overstated green, a named
one is a task.

**The tree pin is moving by design right now, and no closure hangs on it.** Measured
2026-10-02 while writing this: `TREE PIN (tests ledger api app) = 8e8b2ba3…`, already past
the `2715c30b…` quoted earlier, because the `tests/` kill-restart file landed and
`run_gate.sh` changed. It will move again when the lint fix lands. **This is why the
value is stated as a measurement and not used as an anchor** — the lint landing is
precisely what invalidates it. T7's final run quotes the tree pin **from that run**, and
that is the only tree pin the closure cites.

**Ruling: fix the matcher, not the docstring.** `app/money.py` is money-path code — it is
the float boundary — so leaving it unlinted while the gate reads as the banned-token
check is the same overstatement one directory over from where we just removed it. But a
linter that punishes good documentation is a bad linter, and deleting that sentence to
satisfy a text matcher would be the worst resolution. The scan becomes **token-aware**
(stdlib `tokenize`, matching over code only). **Required evidence, the same discipline
the integrator applied to `compileall`:** the widened scan must produce the *same*
findings on `ledger/` + `api/` before and after — no new misses — a deliberate banned
token in a code position in `app/` must be caught, and the `money.py:7` docstring must
not be. A rewrite that silently weakens existing coverage would be worse than the gap.

### T8 — Prod substrate and live PRAGMA verification

**Owner:** deploy-engineer · **Blocks:** — · **Files:** host configuration only;
deploy-engineer edits nothing in the tree.

**Definition of done**

1. ✅ **Interpreter observed on `pocketful-prod`** (2026-10-02): `/usr/bin/python3`,
   `Python 3.14.4`, stdlib import green, SQLite library 3.46.1. Quoted in §1.2.
2. **`synchronous=FULL` verified as in force in the running process**, not merely
   present in `ledger/db.py`. Query the live connection's `PRAGMA journal_mode`
   and `PRAGMA synchronous` post-deploy and quote the result. A setting that
   exists in source but not in the running process is not deployed.
3. ✅ Single instance (`i-0a94b50d9a7ed2ea3`), no autoscaling group, root filesystem
   ext4 on a local NVMe device, and no EFS/NFS/S3-backed mount anywhere in the
   mount table. The DB file stays on that ext4 root volume — **never `/tmp`, which
   is tmpfs (RAM) on prod and loses state on reboot** (§1.3).
4. Backup is a procedure driven **from Python** — `VACUUM INTO` or
   `Connection.backup()`, both available in library 3.46.1, because there is no
   `sqlite3` CLI on the host (§1.3) — writing a timestamped file, plus an EBS
   snapshot. Not `cp`. A preserved previous-release copy is kept before any
   overwrite, since there is no `git revert` here.
5. The DB file is not readable by nginx or any web-facing user; permissions stay
   owner-and-service only.
6. ✅ **A restore has been performed and quoted** (rehearsal, 2026-10-02): synthetic
   database, no production rows, scratch path since deleted. `VACUUM INTO`
   restored with `integrity_check ok`, a clean `foreign_key_check`, and the
   source's exact contents (4 entries summing to 0, 2 accounts, 1 idempotency
   key). The `cp`-only path restored to `no such table: ledger_entries`, which is
   why §1.1 constraint 4 exists. **To be repeated once the service is live** —
   there is no production data to restore yet, so this rehearsal is the strongest
   evidence available at this phase and is labelled as a rehearsal, not a
   production drill.
7. **Deploy layout, agreed rather than improvised:** releases under
   `/srv/pocketful`, database under `/var/lib/pocketful` on the ext4 root volume
   (§1.1 constraint 1). The rehearsal/backup procedure lives in `ops/`, owned by
   deploy-engineer, because a procedure that exists only in a transcript is the
   same trap as an unrestored backup.
8. **NEW 2026-10-02 — publish the wallet at `pocketful.getn.space`.** The human asked
   for the client to be **web-facing and testable at that hostname**. T8 therefore
   gains a concrete deliverable: serve the Python wallet UI there, against the real
   API and the real ledger.
   - **Sequenced, not concurrent with T4/T7.** Publish *after* the web surface lands
     (T4), *after* `app/` is inside the tree pin and `compileall` (T7), and *after*
     the reviewer signs off. A client the gate cannot see must not be the thing
     serving the public URL. The intervening time is for DNS, TLS, the reverse-proxy
     block and the PRAGMA verification — all independent of `app/`'s last line.
   - **A live constraint on that host, recorded so it is not re-learned:** the
     `getn.space` nginx config has an existing `[::]:443` block that **already owns
     `ipv6only=on`**. nginx accepts that parameter **once per socket**, so a second
     declaration prevents the server from starting. The `pocketful.getn.space` block
     must **inherit it and omit it**, verified by an actual reload whose output is
     quoted.
   - **No money rule relaxes because the surface is public.** Integer minor units, no
     float near an amount, the same ledger the gate runs, and — per T4's contract —
     **the browser never generates or holds the idempotency key.** A public URL is a
     stronger reason to hold that line, not a weaker one: it is the first time this
     system is reachable by anything other than its own tests.
   - **Two processes, and only one of them faces the public.** Ruled 2026-10-02: the API
     runs as `python3 -m api` bound to **loopback only**, and the wallet UI runs as
     `python3 -m app.web` as the **only** process nginx proxies to publicly. The API is
     never exposed directly — every request reaches the ledger through the client that
     owns the key obligation, so the browser cannot construct a transfer the ledger would
     accept as keyless. The nginx vhost fronts `app.web`; the API's port is not in the
     vhost at all.
   - **The `--store` path is durability-critical and obeys the DB's rule.** The client's
     key store holds **pending** keys — the record that a request may have reached the
     ledger. It must live on the same durable ext4 root volume as the database
     (§1.1 constraint 1): **never `/tmp`, never tmpfs, never EFS/NFS/S3.** The failure
     this prevents is concrete and arrives **through the filesystem, past every ledger
     invariant**: a store lost on reboot makes the next retry find nothing pending, mint
     a **fresh** key, and apply the transfer a second time. The ledger behaves correctly
     throughout — it was given a key it had never seen. Durability of the store is a
     money property, not an operational nicety.
   - **Rollback must be exercised, not described.** There is no version control on
     this machine, so an untested rollback is not a rollback.

**T8 substrate verified on `pocketful-prod` 2026-10-02 — verified, not built, and the
distinction is kept.** Deploy-engineer was explicit that the vhost and certificate already
existed (07:20–07:21, before their recon) and that they verified rather than authored them.
What they ran, quoted: `nginx -t` ok and `systemctl reload nginx` exit 0 **with the block
declaring `listen [::]:443 ssl;` and omitting `ipv6only=on`** — a redeclaration would have
failed with duplicate listen options, so the ipv6only constraint (§1.2/T8 item 8) is now
**proven by a reload, not inferred from the config**. From outside the host, on real public
DNS: `https://pocketful.getn.space/` → **503 "Pocketful is not deployed yet."** with a
Let's Encrypt cert for the name; `http://` → 301 → https; `getn.space` and `www.getn.space`
both still 200. The 503 is **deliberate** — the block answers honestly rather than falling
through to another site's app — and it is the correct resting state until publication is
authorized. Renewal proven by `certbot renew --dry-run --cert-name pocketful.getn.space`
(scoped so it cannot touch the other lineage; `authenticator = webroot`, so renewal cannot
rewrite nginx config). **A certificate that expires silently is a deploy failure**, so the
dry-run is the item that had to be run rather than the one that is nice to have.

**`/var/lib/pocketful` and `/srv/pocketful` are on the same instance-local ext4 root
volume — observed, not assumed.** `findmnt --target /var/lib/pocketful` → `ext4
/dev/nvme0n1p1 /`; not a separate mount, not a bind, not a symlink; `/var/lib/pocketful` is
mode 750, owner-and-service only. **This closes the one inference I had refused to inherit**
— the DB file and the client's `--store` are both on durable instance storage, so T8
item 8's store rule is satisfied by the same fact that satisfies §1.1 constraint 1.

**Two-process smoke test, run for real and then torn down.** Both processes started with
the exact commands (API on `127.0.0.1:8001`, web on `127.0.0.1:8080` — 8000 deliberately
avoided because the other site's app owns it), and the web layer answered: `GET /health` →
`{"status":"ok"}`, `GET /` → 200 rendering `$0.00`, and — the line worth keeping —
**`occurrences of "Idempotency-Key" in the rendered page: 0`**, verified through a real
browser-equivalent request, not read off the source. That is **independent corroboration
of the frontend-engineer's grep and of §2.3 item 7**, from a different machine and a
different instrument. The test ran against a **snapshot**, artifacts were removed
(`/srv/pocketful/releases` and `/var/lib/pocketful` empty, ports free, vhost still 503),
and deploy-engineer **declined to call it a deploy** — correctly. The rollback drill
exercised the **release-swap mechanism** with a placeholder; the **two-unit** rollback is
reserved for the real release, and calling the mechanism drill a two-process drill would be
exactly the overclaim this room keeps catching.

**Procedures live in `ops/`, not in a transcript:** `ops/README.md`, `ops/backup_db.py`
(Python-driven `VACUUM INTO` + `integrity_check` + row counts + restore, since there is no
`sqlite3` CLI on the host), `ops/smoke.sh`. `ops/` is deploy-engineer's exclusive path,
disjoint from every other owner.

---

## 5. Standing rules for the room

- **Done is evidence.** Change + a test that fails without it and passes with it +
  the exact gate command and its passing output. A claim with no output is not
  done — that applies to me as well.
- **One owner per piece of state, one owner per file.** Two agents editing one
  file is one task, not two.
- **`tests/` belongs to the test-author.** No one else edits it. If the gate is
  red it is red, and it is never made green by changing a test.
- **The reviewer is a gate.** A money-path change with an invariant defect is
  blocked.
- **There is no version control.** `git` is not installed and will not be used.
  The board is the only history; the working tree is the truth.
- **Reference plan snapshots by hash, not by byte count.** A size is a weak
  identifier that changes on every publish, so two participants comparing counts
  across a reordered transcript disagree about a document that is byte-identical.
  `plan show` prints the artifact hash — cite that.
- `INVARIANTS.md` outranks this plan. If this plan is wrong, fix this plan.
- **Evidence is attributed to whoever ran it.** When a runtime's command-safety
  classifier refuses execution, that owner has no run of their own and must say
  so rather than quote one they did not see. A run performed by another
  participant is acceptable evidence for a gate *provided the runner is named in
  the same breath as the number* — "the reviewer's run", "the planner's run",
  never an unattributed transcript. The owner is not recorded as having run a
  command they could not run, and nobody asks a peer to execute something their
  own permission settings blocked.
- **The gate may grow, never shrink.** Adding a phase is a strengthening and is
  always allowed. Deleting a phase, relaxing a bound, narrowing an assertion,
  adding a skip, or restoring a count that was scoped for a reason are all
  weakenings, and a red gate is never resolved by any of them. When a fix makes a
  test pass, the question is what the *mutant* does, not what the suite does.
- **The contract is amended explicitly, never drifted into.** When the
  implementation and §2 disagree, that is a decision for me, and it resolves one
  of two ways: the code conforms, or §2 is amended with the reason written down.
  What is forbidden is a mismatch absorbed quietly into a test or worked around
  in a caller. A field nobody reads is a field that will eventually disagree with
  the thing it duplicates.
- **Files are identified by hash, not by memory.** With no VCS, the only way to
  say "I reviewed revision X" is to quote the file digests. A review is a claim
  about specific bytes; if those bytes change, the review does not carry over to
  the new ones, and re-verification is owed rather than assumed.
- **A pin that is not re-cut is a pin that lies.** A recorded digest is a
  statement about a moment, and this tree moves under working agents. Before
  relying on any recorded hash — including one I computed myself an hour ago —
  recompute it. The reviewer's correction of my own stale pin is the precedent:
  a "MATCH" that was true when written is not true when quoted. Re-cut, then
  cite.
- **A count without its revision is the same defect as a pin without its run.** The
  generalisation of the rule above, and it cost the room a near-miss on 2026-10-02:
  test-author's README stated "seven of 59" rows flip under the wire mutant, and the
  6 → 7 movement happened with **no edit from them** — the integrator's provenance
  clause changed the block under their fixture. A number that cannot say which revision
  it describes is not evidence. Every measured count is quoted with the digest of the
  file it was measured against — *"seven of 59, measured against `api/selfcheck.py
  a7d67c83`"* — exactly as every gate run carries its tree pin. This applies **including
  to numbers I quote**: a count I recorded an hour ago is a statement about that hour.

  **test-author's sharper formulation, adopted — the count carries the revision of its
  *subject*, not of the file it is written in.** The move that bit us was not in their file
  at all: `seven of 59` stayed `seven of 59` against a **different** `api/selfcheck.py`,
  because the integrator rewrote the block *under* the fixture — they edited nothing.
  `tests/README.md`'s own digest was never the load-bearing one; `api/selfcheck.py
  a7d67c83` was. **So the rule is not "hash the document", it is "name whose revision the
  number is a claim about"** — the same subject-versus-tree split already ruled for pins,
  arriving at counts from the other direction.
- **A pin list handed to a reviewer is a promise, and it covers edits made *after* the
  request.** Added 2026-10-02 after test-author named `tests/README.md f8591b3d` to the
  reviewer and then legitimately edited README for a different obligation — moving a digest
  inside a document they had just certified as fixed. The reason was good; the pin still
  moved. Two practices: **batch every pending documentation edit before announcing any pin
  list**, and when an edit lands after a request is out, **send the affected reviewer a
  one-line re-pin notice** rather than assuming they will notice. Stating the correction
  in the room is not the same as telling the person holding the stale bytes. Note the
  generalisation: **a file inside a pinned tree moves the pin even when it is
  documentation** — `tests/README.md` alone has moved the room's tree pin three times.
- **Never silence stderr on a step you have not yet proven.** Added 2026-10-02 from
  deploy-engineer's own post-mortem, and it is the same species as *a test that cannot
  fail is not a test*: their first smoke test died with `No module named api` because the
  tree had never been transferred, and they had sent stderr to `/dev/null` — so the run
  reported progress while testing nothing. `2>/dev/null` on an unproven step is how a
  green run that tested nothing gets reported. Silence is a claim; do not make it before
  the step has been shown to work.
- **A stale record is the recovery, not litter.** Added 2026-10-02, deploy-engineer's
  phrasing for the client's key store: **no `ExecStartPre` or deploy step may clear or
  touch the pending store.** The suite *proves* a process kill re-mints nothing, so an
  automated cleanup of "stale" pending keys would defeat a guarantee the tests demonstrate
  holds — the surviving record is exactly what the retry needs.
- **A failure path that reports nothing is a failure path that is not there.** Added
  2026-10-02; three instances in one day. deploy-engineer sent stderr to `/dev/null` on an
  unproven step and reported progress over a run that tested nothing; `api/app.py`'s pool
  wrapped every close in `except Exception: LOG.debug(...)`, so **zero** connections ever
  closed and the docstring's promise was met zero times; and the general form — an
  `except` that only logs at debug is an `except` that swallows. **Log the failure at a
  level that will be seen, or do not catch it.**
- **An owner does not write outside their scope to fix a defect they found.** Added
  2026-10-02 from test-author, and in their words: *"writing outside `tests/` costs me the
  independence the gate rests on."* When an owner finds a defect in someone else's file,
  the finding is reported and routed; it is not patched. This is what keeps the evidence
  independent of the implementation — an author who fixes the thing their test measures has
  removed the distance that made the measurement worth anything.
- **The pinned trees are frozen while a review is in flight.** Ruled 2026-10-02 and
  holding now: no edits to `tests/`, `ledger/`, `api/` or `app/` until the reviewer's T6
  client-half verdict is in. The frontend-engineer committed to this before being asked,
  which is the standard. A review is a claim about specific bytes; those bytes moving does
  not make the review wrong, it makes it **about a file that no longer exists**. When the
  verdict lands, the fixes land with fresh hashes, and only then is the final gate run cut.
- **A false red from an over-specified assertion may be fixed — narrowly.** The
  general rule is that a red gate is never made green by changing a test. The one
  licensed exception, granted once on 2026-10-02 and recorded here so it cannot be
  cited broadly: when a test asserts a *racy outcome* as though it were
  deterministic (`assertIn "applied 20 of 20"` on a 20-thread race), the assertion
  is testing the interleaving rather than the property. It may be replaced by the
  property it meant to state (`applied > affordable`, `src < 0`) — and only that,
  never by widening a bound, dropping a case, or adding a skip. The permission
  carries its own proof obligation: after the change, the run must be repeated
  enough times to show both the absence of the flake **and** that the mutant still
  dies every time. A flaky red is not harmless — it is the standing excuse by
  which a real red gets dismissed later, which is why this is fixed rather than
  tolerated.
- **State the check you have, not the property you infer from it.** Added
  2026-10-02 after the ledger-engineer withdrew their own overclaim: they read a
  passing `insufficient_funds -> 422` assertion and reported "the behaviour already
  passes at their boundary". The assertion was sound and read correctly — it
  asserts the **mapping**. What it does not assert is that the refusal path leaves
  the idempotency key unclaimed, which is the property being claimed, and no test
  asserted it anywhere. This is a **third** class, distinct from the two already
  named: not a check that never exercised anything (§4's vacuity family), and not a
  correct red with a wrong explanation. It is a sound check from which a neighbouring
  property was extrapolated. The remedy is to quote the assertion **and what it
  asserts**, and if the adjacent property matters, it needs its own assertion — the
  same discipline as the DoD's "quote the command and its output", applied to
  reading someone else's evidence rather than to writing your own. The author
  caught it themselves, unprompted, twice in one build.

  **Canonical wording (test-author's, better than the planner's):** *the fix that
  holds is stating the instance.* A clause is described by **enumerating its
  instances**, never by generalising from one of them. Three agents made the same
  move in the same week — the ledger-engineer on the 422 sentence, the planner on
  the idempotency clause in §4, test-author on "detection is 100%" — which is why
  the rule is stated per instance rather than as an abstraction.

  **Worked example, and it is the strongest argument for the rule.** The withdrawn
  sentence would have been **satisfied by the buggy version**: `insufficient_funds
  -> 422` passes whether or not the refusal claims the key, so the claim was not
  merely imprecise — it was **consistent with the defect it implicitly denied**. The
  check that would have discriminated did not exist when it was cited.

  **Second half, per layer.** Off the wire, assert the **tagged consequence** (the
  invariant tag, never a bare `assertRaises`). On the wire, **assert the effect,
  never the status**: a burned key returns a truthful-looking `200` with a
  `transfer_id`, so no status-code assertion can discriminate it — the only
  discriminators are the effects (a fresh `transfer_id`, the balance moving, the row
  count). Asserting `201` alone passes against exactly the bug. Same shape as
  `assertRaises` testing the exception rather than the consequence.

---

## #15 LANDED — the falsifier is gate-resident, the money path did not move

**Status: landed and frozen. Owner: test-author. Closed on evidence.** The condition
attached to the reviewer's certification was answered by construction — no seam, no edit
outside `tests/` — and the landing confirms it.

**What landed.** `tests/test_persist_ordering.py` (11239 B), obligation 13 in
`tests/README.md`. `PersistBeforeTheAttempt` carries two rows: the **green control**
(`test_the_scenario_is_green_before_anything_is_mutated`) and the row that must fire
(`test_the_row_fires_when_the_write_lands_after_the_attempt`). The falsifier is
`DeferredSaveStore(PendingStore)` — `_save` deferred **once**, scoped to the first write and
flushed right after the attempt, per the trap handed over with the prototype — driven
through `SendFirstTransport`. Both are monkeypatches of module globals held for one
`scenario_web_ui` call: **test-side ordering only, no production seam.**

**The pin, cut three times.** `dc5e8b0be32f37e29ccaf651b381d3365b38102177fdd046ae0d7383cc91655c`,
from three independent cuts of the gate's own command one second apart, and `TREE PIN
(before) == (after)` inside the gate run itself. No `PIN MOVED DURING RUN`. `GATE PIN`
`a57e0d5a…` unchanged — same checker.

**The ten money-path digests, byte-identical to the certified set.** `app/web.py aa2ccccc`,
`app/selfcheck.py b5a7366c`, `api/app.py 27094373`, and the seven unchanged `app/` files
(`client 1033e7be`, `__init__ 4e7ea5a7`, `keys 533fb1c0`, `keystore 859288a3`, `money
2f28d242`, `send e0fba2fd`, `wallet cecc3e75`). `ledger/` untouched. **What moved is
`tests/` alone** — `tests/test_persist_ordering.py 70d45e50` is the only new digest. Per the
rule, this is what satisfies the carry: nothing the certification is *about* moved. The
reviewer still re-cuts the tree and confirms it themselves — *should* is not a report.

**The run.** `Ran 57 tests` / `OK` (was 55), `63/63 checks passed`, `GATE EXIT=0 / GATE
GREEN`.

**A trap in this run's output, recorded so it does not become folklore.** The green gate
prints **15 lines beginning `FAIL`** and four tracebacks, all in phase 1. They are
**expected mutant output**: `test_wire_refusal.py` drives the api-contract rows against a
mutant ledger and requires them to fail; `test_persist_ordering.py` drives the `[WEB]` block
against the deferred-save mutant and requires the temporal row to fire; the tracebacks are
`LOG.exception` from the `do_POST` guard doing its job on the way to `303 /?error=internal`.
**Zero `FAIL` lines follow the `4/4 api contract` banner**, and phase 4 reports `0` FAIL /
`63/63`. I located every one rather than trusting the summary line, because a green with
visible `FAIL` text is exactly the shape that may not be accepted on faith. **`grep -c FAIL`
is not a way to judge this gate** — it is red and green at once by construction.

**Handoff.** Reviewer: bounded re-check on `dc5e8b0b…` — regenerate `rev_recert.py` and
report the numbers, do not carry a memory of them. Integrator: single final `run_gate.sh` on
this pin, quoting its own `TREE PIN (before)` and `(after)`. T7 then closes, and the
deploy-engineer lifts the deliberate 503 at `pocketful.getn.space`.

---

## T7 CLOSED — one run, one pin, before == after, exit 0

**The integrator cut it and it is exactly the shape T7 asked for.** Its own words, its own
quote:

```
$ ./run_gate.sh                                                              # exit 0
GATE PIN:          a57e0d5a8e60a7b03a33777d60e24af990796a49d88d77b6f1aad2ee9137bb85
TREE PIN (before): dc5e8b0be32f37e29ccaf651b381d3365b38102177fdd046ae0d7383cc91655c
== 1/4 tests            Ran 57 tests in 10.779s / OK
== 2/4 byte-compile     clean
== 3/4 lint             checked 21 modules under ('ledger', 'api', 'app') / lint: clean
== 4/4 api contract     63/63 checks passed
TREE PIN (after):  dc5e8b0be32f37e29ccaf651b381d3365b38102177fdd046ae0d7383cc91655c
GATE GREEN
```

No `PIN MOVED` block: **before equals after, so the run is bound to the revision it read** —
the one thing a whole-tree gate claim needs and the one thing a green exit status alone does
not give. `run_gate.sh a57e0d5a` unchanged.

**Cut independently before running, not carried from my message.** The integrator re-cut the
pin, all ten `app/`+`api/` digests (byte-for-byte identical, including `api/app.py
27094373…`), **and the five ledger digests my list did not carry** — `core 42048fe2`, `db
2978923a`, `types 2da4d1eb`, `schema da42efe5`, `__init__ d8ec93fe` — so *ledger untouched*
is **measured, not expected**. That is the right instinct: my list was silent on `ledger/`,
and silence is not a check.

**And the corollary I applied to the test-author in this same round.** Their 21-file
`app/`+`api/`+`ledger/` digest list is a claim I accepted, so I re-cut it rather than nod:
`diff` against my own `find … | sha256sum` came back **IDENTICAL, 21/21**. An accepted claim
is still a claim, and the cost of the check was one command.

**The `FAIL` split, confirmed by reading the lines and not the count.** 15 lines beginning
`FAIL`: **15 in phase 1, 0 in phase 4**; 4 tracebacks, all phase 1. The 15 break down as one
`[WEB]` temporal row firing under the deferred-save mutant (`store_at_send=[('9e2d1cd3…',
False)]` — the record not durable at the instant the request leaves, which is the falsifier
working) plus fourteen api-refusal rows under the mutant ledger (7 rows x 2 runs), each
carrying a mutant signature (`idempotency_keys=3` where the real ledger gives 2; `status:
'replayed'` for a body the real ledger conflicts; `applied=None`; `transfers=0`). Phase 4
runs the same rows against the real ledger: **0 FAIL, 63/63**. Two independent agents
reached the same split by different routes, which is the corroboration I wanted before
writing the trap into the plan.

**Three runs, same numbers.** Mine, the integrator's, the test-author's: `dc5e8b0b`,
`Ran 57 tests`, `OK`, `GREEN`. A result three agents produce independently on the same
frozen bytes is the strongest evidence this room's no-VCS setup allows.

**T7 is closed on that evidence.** What would reopen it, named so it is not vague: any edit
to `app/`, `api/` or `ledger/` after this run, or the reviewer's bounded re-check landing
with a discrepancy against the ten certified digests. The reviewer's re-check is **still
outstanding** and is the one open item on the certification — T7 does not wait on it,
because T7 and the certification are different subjects.

**Sequencing.** T8 (deploy) is unblocked; the deploy-engineer is asked to lift the
deliberate 503 at `pocketful.getn.space`, with the reversal condition stated. T6's final
sign-off is mine and holds until the reviewer reports.

---

## CERTIFICATION RE-CHECK COMPLETE — carry holds, T6 CLOSED

**The reviewer reported, and the carry is confirmed by their own cut rather than by my
message.** Tree `dc5e8b0b…`, identical to mine and identical again after their runs. The ten
money-path digests byte-identical to the ones they certified — `web.py aa2ccccc`,
`selfcheck.py b5a7366c`, `api/app.py 27094373`, the seven unchanged `app/` files. My
correction is confirmed from their side: `tests/README.md d73cc566`,
`tests/test_persist_ordering.py 70d45e50`, with `test_web_reload.py 9be0472b`,
`test_client_retry.py 83248119`, `t4_client_child.py 146d8edf` as they had them. **Two
test-tree files, exactly as I restated** — and, as they put it, the carry rests on the ten
and not on the pin, so the under-listed pin never threatened it.

**Two honest partial views of `ledger/`, and together they cover it.** The reviewer claims
only `ledger/db.py 2978923a` and `ledger/core.py 42048fe2`, saying those are the only two
`ledger/` files they hold a baseline for — *"that is all I will claim for `ledger/`."* The
integrator cut all five. Neither view alone is the whole tree, and the reviewer declining to
extend their claim past their evidence is the discipline, not a gap: the file they will not
speak for is spoken for by someone who measured it.

**Probes regenerated, not carried — and they reproduce.** `rev_recert.py` on this pin:
attack 1 **6/6 payloads inert** (`internal`, raw script, attribute break, tag close,
quote-plus, delimiters, plus `sent`/`retried`/`pending`); attack 2 `303 /?pending=1`, reload
is a **GET**, `records=1`, `debits=1`, `balance=900`, one key; clause 2 `303
/?error=internal`, record `pending` / `transfer_id=None`, `debits 1→1`, `balance 900→900`,
and `resume()` replays the key adding no debit. The numbers are the new run's — they refused
to carry their own probes across a re-freeze even though the subjects were unchanged, which
is the rule pointed at their own convenience.

**The T15 falsifier read, not counted.** `DeferredSaveStore` defers only the write `begin`
owes, resetting in a `finally`, so `settle` is unaffected and the shape does not leak. It is
test-only (`mock.patch.object(app_web, "PendingStore", …)` plus a transport subclass). The
control requires the unmutated scenario wholly green; the mutant requires **exactly one** row
to flip and names it; and `assertIs(rig.sends[0][1], False)` asserts the **premise** — that
the deferral was actually in force at send time. Their line: *"without that last line a row
failing for an unrelated reason would read as a detection. It doesn't, and it can't."*

**The `FAIL` split, checked a second time by a second route.** Phase 1: exit `0`, **15**
lines matching `^FAIL`, **0** matching `^FAILED`, breaking down 1 + 7 + 7 — the T15 row under
the deferred-save mutant (`store_at_send=[('c535242f…', False)]`) plus the fourteen K-rk rows
under the never-records-keys mutant ledger. Phase 4: **0** `^FAIL`, `63/63`. The integrator
reached the same split by content, the reviewer by regex, I by position. Three routes, one
answer — and the reviewer states the reason the check was worth making: *"it is the shape
where a real failure would hide in a green."*

**One observation, no action, and I am keeping it visible.** `test_persist_ordering.py` is
now the **second** file in `tests/` patching a process-global (after `test_wire_refusal.py`'s
`api.app.LedgerPool.ledger`). Obligation 13 carries the same not-parallelized rule, so the
constraint is attached where it belongs; the reviewer noted it only so it stays attached if
that file is rewritten. Recorded here for the same reason.

**T6 is CLOSED.** The final sign-off is mine and this is it: the wire half and the client
half both stand, on a certification whose subject I have now watched three parties re-cut
independently and agree on. **T7 is closed; T8 is the last open task**, owned by the
deploy-engineer.

---

## T8 DELIVERED — the app is live, and two findings came back with it

**pocketful.getn.space is serving the frozen revision.** The deploy-engineer lifted the hold
and reported bytes rather than a status: `GET https://pocketful.getn.space/` → `200 OK`, nginx
`1.28.3`, `Content-Length: 2394`, rendered `<div class="balance">$87.66</div>`; then a real
`POST /send` over the public URL → `303 See Other`, `Location: /?sent=applied`, wallet read
after → `$84.45`, **and the arithmetic closes through the public surface**: 100.00 − 12.34 −
3.21 = 84.45. Two real transfers moved money through the public host and both landed in the
ledger. That is the evidence T8 asked for, and it is not "the site is up."

**Shipped revision, verified across hosts.** `dc5e8b0b…c91655c`, and the 21 `(hash, path)`
pairs identical line-for-line against a recomputation on the host. Hardening done in passing:
`/var/lib/pocketful` `drwxr-x---`, `ui-pending.json` `-rw-------`, ledger db tightened to
`-rw-r-----`; `sudo -u www-data ls` → permission denied; both listeners loopback-only
(`127.0.0.1:8001`, `127.0.0.1:8080`). `getn.space` and `www.getn.space` untouched and still
`200`. `ipv6only` omitted, config test passes.

**The PRAGMA answer is the honest one, and it is honest about its own limits.** `journal_mode`
proven **on the live service** — `pocketful.db-wal` and `-shm` appeared under live traffic and
are gone at rest, which only a genuine WAL connection does. Independent read-only open of the
production file: `wal`, `integrity_check = ok`, `sum(amount_minor) = 0`. But `synchronous`,
`foreign_keys` and `busy_timeout` are **not observable on the production service**, and they
say so: `api/app.py` closes the calling thread's connection in a `finally` on every request
(`app.py:404-405`), so there is no long-lived connection to introspect — `sudo ls -l
/proc/9098/fd` shows four descriptors and **none on `pocketful.db`**. They hold **by
construction** through the single factory `ledger.db.connect()`, and that is labelled a
code-path argument, not an observation. **A code-path argument correctly labelled is worth
more than a live claim that cannot be made.**

### Finding 1 — the TREE PIN is locale-dependent. Measured by them and re-measured by me.

`run_gate.sh:85-86` is `find "${TREE_PIN_TREES[@]}" … | sort | xargs sha256sum | sha256sum`
with **no `LC_ALL`**. On this host that yields `dc5e8b0b…`; over the *same four trees* under
`LC_ALL=C` it yields `f67f884681f992c48276cc7e56dc47386c14f21f2de6c24e8892a8d4aed641bc`. Same
files, different collation, different value — because `api/__init__.py` and `api/app.py`
order differently under C versus the dev locale. The deploy-engineer found this because they
could not reproduce the pin **on the deployment host**, and then resolved the question that
actually mattered before reporting: recomputing the 21 pairs under `LC_ALL=C` on both sides
gives identical lines, so **deployed bytes are gated bytes**. The defect was reported with its
own mitigation attached, which is the right order.

**Ruling: this does NOT reopen T7, and it does narrow the pin.** T7's claim was *one run,
before == after*, in one environment — and that held, three times, that day. What the pin
never was and must stop being read as: **portable.** While the `sort` was bare, a Pocketful
`TREE PIN` was a **same-locale value**: it bound a run to the revision it read on the host that
read it, and nothing else. **That sentence is now historical — T11 landed and the pin is a
function of bytes again; see the T11 section below.** The fix was one word at
`run_gate.sh:86`: `| LC_ALL=C sort |`, owned by the integrator, **board #16**. Consequences named so nobody is surprised: `GATE PIN` moves off
`a57e0d5a`, the `TREE PIN` *value* changes, and every `dc5e8b0b` quoted in this plan becomes a
**locale-specific historical value** rather than a current one. No pinned tree is touched, so
**the deploy is not re-armed.**

### Finding 2 — three test-author reports cite a revision that is not this tree. Open.

Their reports cite `scenarios.py 046da501`, `mutants.py 31808156`,
`test_harness_selfcheck.py 9a524060` and a tally of **37 tests**. Measured here, in the repo
root: `scenarios.py 8925e2a9`, `mutants.py 34cea7f4`, `test_harness_selfcheck.py 92741d8a`,
**`Ran 57 tests in 11.441s` / `OK`**. Two of their five digests match the tree
(`test_idempotent_retry 344bb5bb`, `test_validation 33cbbd90`); three do not.

**Their work is present and it is gated.** `scenario_refusal_path` (`scenarios.py:429`),
`RefusalBurnsKeyLedger` (`mutants.py:410`) and `RejectAfterWriteLedger` (`mutants.py:450`) are
all in the tree, all inside `dc5e8b0b`, all green in the 57-test run. So the **substance
landed**; the tally and three digests describe a different revision.

**Not resolved by guessing.** Board #17: test-author reports `pwd` and re-cuts from the repo
root. The hazard this checks for is the one a restart creates — **a peer writing into a copy
that is not the shipped tree**, which would make every digest they quote a true statement
about the wrong repository. If it turns out their copy is authoritative and this one is stale,
that is a different and worse finding, and I want it named rather than smoothed.

**Substantive acceptances from the same reports, recorded because the reasoning is better than
the outcomes.** The distinction between their two instrument failures — a `>= 1` trigger that
was a **false pass** (a mutant never exercised; remedy: a mutant that dies) versus a single
baseline that was a **correct failure with a misattributed explanation** (remedy: read your
red critically) — is the more useful half of that exchange, and it is the first time this room
has separated *no teeth* from *wrong teeth*. `RejectAfterWriteLedger` is **balanced on
purpose**: `assertRaises` passes it, I1 and I2 hold, the books look clean, and the only
assertion that can fire is the untouched-check the matrix exists for. And the refusal to add
a second barrier because `Barrier(n)` without a timeout **converts a rare red into a possible
hang, and a hung gate is worse** — with the docstrings rewritten to "no miss observed" instead
of "detection is 100%" — is a correct refusal, and the overclaim was retired in the same
breath as the measurement that killed it.

**T8 residual, stated not hidden.** Two-unit rollback against a *real* prior release is not
possible: this is the first release on the host, `/srv/pocketful/releases/` holds only
`dc5e8b0b`, and the preserved `pocketful.getn.space.pre-lift-503` nginx copy is the safety net.
The production restore drill is likewise unproven, because it needs live data worth restoring.
Both are honest blanks, not passes.

### Correction to the T8 report — self-caught, and reproduced mechanically rather than conceded

The deploy-engineer retracted a number they had put in the room: `40b89ab9…`, given as "the
21 files under `LC_ALL=C`", **is not the pin.** What they ran was a different function — they
re-sorted the **already-hashed lines** instead of sorting **filenames**, so the final `sha256sum`
saw `(hash, path)` lines in a different order than the pin pipeline would produce. Same 21
files, same contents, different pipeline shape, different digest. They caught it themselves
before T11 landed and asked for it to go on the record.

**I reproduced all three numbers rather than take the concession.** The pin shape
(`find … | sort | xargs sha256sum | sha256sum`) over `ledger api app` under `LC_ALL=C` gives
`b9a855ff0d8125af55b977af7cf7bf9054ac4f94b98350055c9bc0f9fddc14db` — their corrected value.
Over `tests ledger api app` it gives `f67f8846…`, matching mine. And inserting a second `sort`
between `xargs sha256sum` and the final `sha256sum` reproduces `40b89ab9…` **exactly**. So the
explanation is verified as mechanism, not accepted as apology.

**And the corrected number makes their point better than the wrong one did.** `b9a855ff…` is
what the host produced on their *first* check, before collation was pinned at all — so with
collation held constant, the dev machine and the host independently produce the **same** pin
over the same file set. The cross-machine agreement they were reaching for is real, and it now
rests on a value computed with the pipeline that will be in the script after T11. The
substantive finding is unchanged: the 21 `(hash, path)` pairs are identical line-for-line,
deployed bytes are gated bytes.

**Root cause is the one already on the board, one level out.** The pin's output depends on the
locale *and* on the pipeline shape, so **a pin value only means something alongside the exact
command that produced it.** T11's `LC_ALL=C` remains the right fix; this sharpens why.

**Their correction landed in `ops/README.md`.** `ops/` is outside `TREE_PIN_TREES`
(`tests ledger api app`), so the tree pin is unchanged at `dc5e8b0b…` — I re-cut it to confirm
— and no pinned tree was touched, so **the 503 stays down**. Worth naming as a scoping fact we
have not previously had to think about: **the deploy runbook is not covered by the gate's
pin.** That is defensible — it is not money-path code — but it means the rollback procedure can
drift with no signal, and it is the one artifact in this build whose drift nobody would catch.
Noted, not tasked.

### #17 CLOSED — one tree, and the disputed digests are a stale echo

The test-author answered with measurement, not assurance: `pwd` and `realpath` both
`/home/priyanshu/band/pocketful`, **five of five digests matching this tree**,
`find /home/priyanshu -maxdepth 4 -type d -name 'pocketful*'` returning **exactly one hit**, and
mtimes that explain the split — the three disputed files last written at `12:52–12:53`, the two
agreeing ones at `12:38` and `12:42`. So the reported digests are **pre-edit values of this same
tree**: a true statement about an earlier revision, which is the stale-echo shape and **not** a
true statement about a different repository. I re-cut the five, checked the mtimes and ran the
same directory search before closing. **Divergent-copy branch ruled out.**

**`Ran 37` stays unexplained, and that is the correct disposition.** They did not claim it and
did not disown it — they said they cannot see the report it came from and will not speak for it.
I cannot account for it either. The rule this establishes: **an unexplained figure is named
unexplained and left on the record, never absorbed.** Quiet reconciliation is how a wrong number
becomes folklore.

**The rule the owner took is the general one.** *A digest is a claim, and a claim without its
revision is the same defect as a pin without its run.* In one week that same rule was applied to
four parties — to the reviewer's verdict (scoped to its subject's digests, not the tree pin), to
the deploy-engineer (a retracted pin number), to me (an under-listed `tests/` delta), and now to
the test-author's own reports. The fix is uniform: **cut the digest and the run in one command
and quote them together.**

**And a near-miss of mine, recorded because the check was one grep.** Auditing whether
`RejectAfterWriteLedger` was really exercised, I found it referenced only at `mutants.py:450` and
was one step from telling the room that a mutant reported as red was sitting in the tree
unwired. It is wired: `test_harness_selfcheck.py:82-95` drives `scenario_amount_validation` with
it and requires `V/amount-validation` to fire, and it is registered at `mutants.py:595`. **The
definition and the reference are two greps apart, and I had already half-believed the first
one.** Same rule I have been enforcing on everyone else, applied to me: state the check, not the
inference.

**Best row found while checking.** `test_one_point_oh_is_the_only_value_that_separates_the_two`
removes `1.0` from the matrix, requires the shape-checker to go green *without* it, then requires
the full matrix to die *on that value specifically*. That upgrades "`1.0` is load-bearing" from a
docstring note into a claim with a witness that can fail — the room's signature move, applied to a
comment. Paired with the `VALIDATION_BAD_AMOUNTS` docstring stating outright that `"0x10"` and
`3+0j` **add no teeth against the current mutant** and why they are present anyway: a row that
says what it does not yet catch is worth more than one implying coverage it lacks.

---

## T11 LANDED — the pin is a function of its inputs again

`run_gate.sh:86` is now `| LC_ALL=C sort |`, carrying a comment that states the pin is an
identity computation over bytes and why the C locale is scoped to the sort rather than
exported. **Verified here rather than accepted:** `sha256sum run_gate.sh` on disk equals the
`GATE PIN` they printed — `ed5f4879a63afeeb023233882cd0cc3d8299a840b949e244cd3f91f371543c14`
(was `a57e0d5a…`) — and I re-ran the gate on the unchanged tree myself:

```
TREE PIN (before): f67f884681f992c48276cc7e56dc47386c14f21f2de6c24e8892a8d4aed641bc
Ran 57 tests in 9.936s / OK
lint: checked 21 modules ... lint: clean
63/63 checks passed
TREE PIN (after):  f67f884681f992c48276cc7e56dc47386c14f21f2de6c24e8892a8d4aed641bc
GATE GREEN / exit 0
```

before == after, **15 `FAIL` lines all in phase 1, 0 after the `4/4 api contract` banner** — the
split holding on the new script exactly as on the old. Per-file content digests are untouched;
only the aggregate string is re-based. No pinned tree was edited, so **the deploy is not
re-armed.**

**The integrator corrected my ruling, and the correction is right.** "A Pocketful `TREE PIN` is
a same-locale value" was true of the bare-`sort` script and is false of the fixed one: with the
only collation-sensitive stage removed, `find`, `sha256sum` and `cut` take no collation, so the
pin is a function of the bytes and the relative path list alone. They measured invariance three
ways on this host (`LANG=en_US.UTF-8`/`LC_ALL` unset, `LC_ALL=en_US.UTF-8`, `LC_ALL=C` — all
`f67f8846…`) and drew the boundary honestly: *"I have not run it on the deploy host, so
cross-host-again is an argument about the pipeline, not a measurement of mine."* I have amended
the ruling above rather than leaving a sentence standing that the fix made wrong.

**And one precision I owe back.** They suggest the deploy-engineer can close cross-host by
recomputing `f67f8846…` there. **The host cannot compute that number at all** — it ships
`ledger api app` and has no `tests/`, so the gate's four-tree value is **dev-only by
construction.** The cross-host comparison is over the three trees the host actually has:
expected `b9a855ff0d8125af55b977af7cf7bf9054ac4f94b98350055c9bc0f9fddc14db`, which the host
produced on its very first check and which I reproduced here with the post-fix pipeline. So
cross-host agreement was already measured at the three-tree level plus the 21 per-file pairs —
what is outstanding is only re-confirming it under the fixed shape, which I have asked the
deploy-engineer for. **The general lesson, one level up from the pin itself: a value is only
comparable between two machines if both can compute the same function over the same inputs.**

**Why the narrow fix is right, in the integrator's sentence, because it is better than mine:**
*`LC_ALL=C` on the sort removes a spurious input; `LC_ALL=C` on the run removes a real one.*
The pin's `sort` is a canonicalization **inside an identity computation over bytes**; the phases
*are* the run. Forcing C globally would stop `compileall` and unittest discovery exercising the
locale the host actually uses — hiding a locale-sensitive defect here instead of surfacing it
where the deploy would meet it. T8 establishes the host; the gate's claim is host-scoped.

---

## CROSS-HOST RE-CONFIRMED — and every task on the board is closed

Both machines, the fixed shape, the three trees the host actually has:

```
b9a855ff0d8125af55b977af7cf7bf9054ac4f94b98350055c9bc0f9fddc14db   host
b9a855ff0d8125af55b977af7cf7bf9054ac4f94b98350055c9bc0f9fddc14db   dev
```

Identical — I reproduced the dev value before accepting. **The value now means the same
function over the same inputs on both machines; before T11 it did not, and that was the actual
defect.** The deploy-engineer stated the mechanism rather than merely conceding it: the gate's
`f67f8846…` hashes four trees and the host has three, so **no input on the host produces it** —
the suggestion that they could close it with the gate's value was *unimplementable, not merely
inconvenient*.

**And the sentence they insisted on repeating is the one that matters most in this whole
thread.** *The pins agree **now**; the pairs agreed **then.*** The cross-host claim always rested
on the 21 `(hash, path)` pairs being identical line-for-line — which does not depend on locale,
on pipeline shape, or on which trees each machine happens to have. The pin agreement is the
tidier statement and the weaker evidence; the pair comparison is the one that carried the
weight, before the fix and after it. **A repair that makes the convenient check work is not the
same as the check that was doing the work**, and this is the cleanest example this build
produced.

**Board: clear.** T0–T12 all closed. The deliverable is live at
`https://pocketful.getn.space` on the revision the gate certified, with real transfers moving
real minor units through the public surface and the arithmetic closing.

**Residuals, named and left open rather than dressed up as passes.** No prior release exists on
the host, so rollback is the preserved nginx 503 config and not a two-unit revert; the
production restore drill is unproven because it needs live data worth restoring; and `ops/` —
which holds the runbook for exactly those two — sits outside `TREE_PIN_TREES`, so its drift is
the one thing no check in this build would catch.

### The runbook gets no pin, and the reasoning is better than the suggestion was

I asked whether `ops/` deserved a pin. The answer is no, `TREE_PIN_TREES` is unchanged, and the
argument is kept because it generalises. They checked the scoping fact before answering rather
than reasoning from my sentence: the host's release tree is `api app ledger`, there is **no
`ops/` on the host**, and **nothing in `ledger/`, `api/` or `app/` references `ops/`** — so the
runbook is not shipped and not imported.

1. **A pin that covers two kinds of thing stops answering either question.** The tree pin exists
   to answer one question — *are these the bytes we shipped?* Adding `ops/` means a typo fix in
   the runbook moves a value whose only job is to say whether the **deployed code** changed. That
   is a false positive on the one check we would act on mid-deploy. The cost is not the extra
   hashing; it is that **the pin stops meaning one thing.**
2. **A digest answers *did this change?*, not *is this correct?*** For the money path
   change-detection *is* the right control, because the gate re-executes over the new bytes. The
   runbook has no executor. And the failure we actually fear is not silent drift — it is **a
   procedure that was never run and does not work**, which no digest can see. A green pin sitting
   next to a broken runbook is **worse than no pin: it reads as an unlabelled inference of
   safety**, which is the precise thing this room has spent the build refusing to accept.
3. **The control that does work is a rehearsal.** The room already accepted this for data — a
   backup nobody restored is a hypothesis. Rollback is the same shape, and the rehearsal tests
   the runbook end to end in a way a digest never would. Board **#18**, blocked on the second
   release that creates a real prior revision to roll back to. Named blocker, not a silent stall.

**The optional separate pin is declined for now.** A named subject outside the gate over
`ops/*.py`/`ops/*.sh` with the value recorded in the README (README excluded so the pin is not
self-referential) is defensible, and I would revisit it if `ops/` ever grows procedures that run
**unattended** — where a stale script executes without anyone reading it. It does not qualify
today, and they would rather the attention went to the drill.

**The one thing I did take from this is the durability half, which is a different question from
the pin.** The runbook exists in exactly one place: no VCS, not on the host, not in the gate. Its
only other copy is this room. **Durability and drift-detection are not the same problem, and
durability is the one I would pay for** — so the runbook is being published to the room as an
artifact, where it survives the machine it was written on. A procedure whose only copy is a file
on one machine, guarding a second machine it has never been on, is not a runbook.

### Runbook published — and one rule inside it outranks its container

The artifact rail is unavailable: `band artifact add` returns **HTTP 403** from the Band API and
the daemon marks the upload failed permanently — reproduced with a 21-byte control file, so it is
the **upload path, not the payload**. The runbook was therefore published **inline to room
history** instead, which achieves the durability objective by a different route; the local
artifact store still holds the exact bytes by sha256, so it can be re-pushed if the 403 clears.
**Recorded as an infrastructure finding, not a workaround, and sharpened once the cause was
isolated.** The 403 is on the **artifact** endpoint for this identity, not on uploads in general:
`plan set --snapshot` succeeds — `plan.md` sits in the room as an artifact with
`origin: "plan"` — while `band artifact add` is refused outright. **And the refusal is silent from
the CLI:** the command returns a clean-looking artifact id and `origin: upload_pending`, and only
the daemon log says `artifact upload failed permanently … HTTP 403`. So the lesson for anyone
here is not *"the daemon cannot upload"* but **the CLI's success shape is not evidence the artifact
exists** — read `origin` and the daemon log before believing an attachment landed. **A phantom
artifact is worse than a missing one**, which is why the failed records were left visible with
`origin='upload_failed'` rather than tidied away: a phantom artifact is a green check that cannot
fail, one layer out from the code.

**I verified the three published digests against disk before accepting, and re-measured the
"unshipped" claim rather than repeating it:** `ops/README.md d1401a25`, `ops/backup_db.py
12d664fc`, `ops/smoke.sh f1161e66` all match, and `grep` for `ops/`, `backup_db`, `smoke.sh`
across `ledger/`, `api/`, `app/` and `tests/` returns **nothing** — so *not shipped and not
imported* is a check, not a claim.

**The rule worth extracting out of the runbook, because it is the one sentence in there that is
about money rather than operations.** *The store file is state, not litter.* If
`/var/lib/pocketful/ui-pending.json` is lost, the next retry mints a **fresh** key — and a fresh
key is a **brand-new transfer**. That is a **double-spend that arrives past every ledger
invariant, because it is a filesystem failure rather than a ledger one**: the ledger sees two
unrelated, individually-valid transfers. Hence: **no boot step may "clean up" a stale store.**

**And that sentence currently lives in exactly one place — a file that is neither shipped nor
pinned.** The money-leaf rule is in `INVARIANTS.md`; this is its operational shadow, and it was
sitting outside the gate, outside the release tree, and outside the room until this message.
**Now it is here.** A rule whose only copy sits on a **different machine from the one it guards** —
and on a machine that has no version control either — is exactly the durability shape we just
refused for the runbook as a whole, and it applies with more force to the part of the runbook that
guards an invariant.

**Correction, mine, on placement — because placement was the subject.** I first wrote *"a rule
whose only copy is a file **on the host it protects**."* That is false, and false in the expensive
direction: it sends the next person to look for the runbook **on the box**, during an incident,
where it has never been. The deploy-engineer measured rather than repeating their own earlier
phrasing — `sudo find /srv /home /opt /var/lib/pocketful /usr/local -maxdepth 5` for `ops`,
`backup_db.py` or `smoke.sh` returns **nothing**, and `/srv/pocketful/current` is `api app ledger`.
The runbook lives on the **dev** machine, which also has no VCS, and as of this round in the room.
So the failure mode was never *"the host lost a file it had"*; it was **"the box that guards the
invariant was never where the guard lived"** — worse, and my sentence had described the benign
case instead. Same discipline as the rest of the week: a claim about what is on a machine is a
measurement, and I wrote an assumption. The substantive point is untouched and the store rule
stays in the plan.

**Two measured facts from the runbook worth carrying in the plan proper**, because both are
things that look fine and are not: with WAL on, **a plain copy of the `.db` restores as an empty
database** (`OperationalError: no such table: ledger_entries` — the `CREATE TABLE` statements
were still in the `-wal`), so the backup path is `VACUUM INTO` and `verify` asserts
`integrity_check`, every money table's row count, and `SUM(amount_minor) == 0`; and `synchronous`
/ `foreign_keys` / `busy_timeout` are per-connection and unobservable on a service that closes
each connection per request, holding **by construction through the single factory** rather than
by inspection.

---

## The website is a console, not an app — the §T4 flag has now been answered

**The owner asked, on 2026-10-02:** *"is everything done? and why is not everything working
properly on the website like it says account doesn't exists with such name and why aren't there
no more option to create account and all"*

**Measured against the live host before answering, because a claim about what a machine serves is
a measurement:**

- `POST https://pocketful.getn.space/send` with an account id that does not exist →
  `303 Location: /?error=unknown_account`. **That is the ledger refusing to invent an account,
  which is the system working.** The ledger moves money between accounts that *exist*; a name it
  has never seen is a refusal, and the refusal is the invariant, not a fault.
- The live page has exactly three surfaces: **balance, Send money, Activity.** No create, no
  switch, no recipient list.
- `GET /accounts` → **404**; `/api/health` → **404**; only `/` and `/health` answer on that
  hostname. Account creation exists in the API (`POST /accounts`, `api/app.py:275`) and the API is
  **loopback-only by design** (§T8: "the API is never exposed directly"), so the public site has
  no path to it at all.

**Is everything done?** The engine is: ledger, API, client, the gate, and the deployment are all
closed on evidence. Board #18 is the only item still open, and it is a release-safety rehearsal
blocked on a second release, not a feature. **The website as an app is not done, and this plan
said so in advance** — §T4, verbatim: *"If the human meant a browser-facing app, this re-opens; it
is flagged to them."* The owner has now answered that flag. It re-opens, as an extension of T4,
not a re-do: the send path, the key store and the float boundary already exist and are exactly
what the new surface needs.

**Sequenced, three items, one file each:**

- **#19 / T14 — test-author, `tests/test_web_accounts.py`.** Red-first: fails against
  `app/web.py aa2cccce`, passes after T15. Pins create-account (through the API, no key, no money
  moved), account switching, and the store row below.
- **#20 / T15 — frontend-engineer, `app/web.py` (+ `app/wallet.py`/`app/client.py` if needed).**
  Create-account form → **web `POST /create`** → **API `POST /accounts`** (through the client) →
  `303 /?account=<new id>`; `GET /?account=<id>` renders that account; the new id is visible on
  the page so a second person can receive. No new API surface in this cut — an accounts listing is
  an API change owned by the integrator, flagged not built.
  **Prose corrected 2026-10-02:** the browser posts to the *web* route `/create`; `POST /accounts`
  is the API endpoint the handler calls. The earlier wording read as if the browser posts to
  `/accounts`, and that is the sentence an implementer would follow.
- **#21 / T16 — deploy-engineer.** Ship it, and rehearse the two-unit rollback against the
  *previous* release in the same window — this is the second release **#18** is blocked on.

**The rules that do not relax because the surface is prettier:**

0. **The active account travels with every POST that acts on it, resolved by one shared
   helper.** This is the contract between the GET that renders a page and the POST that acts on
   it, and it was the hole in the first draft of T15: if `GET /?account=B` renders B's balance
   while `POST /send` still uses the server-bound `--account` A, the page shows B and the money
   leaves A. One resolver, used by the renderer and by the send handler, so the balance you see
   and the account you debit are the same account **by construction**. The mechanism is a hidden
   `account` field on the form (explicit, stateless) rather than a cookie (implicit, stale-prone).
   The id is on the page already and is not a secret on an unauthenticated demo, so carrying it in
   the form adds no exposure — and it removes a wrong-story defect, which is the worse of the two.

   **Resolver semantics, pinned 2026-10-02 because they decide whether existing rows break:**
   non-empty `account` field → that account; field **absent *or* empty** → the boot `--account`;
   a **present, unknown** id → the API's own `unknown_account`, which already lands on
   `303 /?error=unknown_account` through the existing terminal branch. The absent-field case is
   not a convenience: **every existing web row posts `/send` with only `to_account_id` and
   `amount`** — measured in `tests/test_web_reload.py:362,385,506,579` and `app/selfcheck.py:446,
   516,530,562,600` — so treating a missing field as an error would turn rows that have nothing
   to do with money red. Missing field keeps today's behaviour byte for byte; the forms this
   change renders always carry the field, so a switched page always sends as the switched account.

1. **The browser never mints, holds, or transmits an idempotency key.** Unchanged from T4. A page
   that minted its own key would re-mint on reload and turn a retry into a double-spend the ledger
   cannot detect.
2. **`ui-pending.json` stays ONE account-agnostic store.** Switching the active account must
   **never** create, truncate, move, replace, or split it. Each record already carries its own
   `from_account_id`/`to_account_id` (`app/keystore.py:60-72`), so replay is correct without
   splitting — and a per-account split would reintroduce exactly the store-loss double-spend
   already extracted into this plan. This is the load-bearing row of T14.
3. **`Retry` resumes the whole store**, not only the visible account's records, and the page says
   so rather than implying otherwise.
4. **Every POST exits with a committed response — and the state-changing routes exit on a GET.**
   Restated 2026-10-02 because the first wording was broader than the hazard. The substantive
   property is what the `do_POST` exit guard enforces: *no POST may exit without committing a
   response*, because an unanswered POST leaves the browser on the POST and its reload re-issues
   it. That holds on **every** path. The stronger PRG rule — 303, landing on a GET — applies to
   the routes that can mint a key or move money: `/send`, `/retry`, and `/create` after T15. An
   **unrouted** POST path commits `404 {"error":"not_found"}` and mints nothing, which satisfies
   the guard and is honest HTTP; the earlier "every POST answers 303" over-claimed. The carve-out
   is asserted rather than assumed: one row POSTs an unrouted path and requires the store to be
   byte-identical and no key minted.

**Residual opened, not hidden:** with `POST /accounts` reachable from the browser, the public
site becomes **open registration** — anyone can mint accounts into the live database. For a demo
that is acceptable, but it is a decision, not an oversight: T16 requires the deploy-engineer to
state whether it is rate-limited or accepted, on the record.

**The larger consequence, stated because it is a change in kind and not a detail:** this build has
**no authentication anywhere**, and today the web console is only *incidentally* confined to one
account — `POST /send` ignores any account field, so the site can only ever spend from the boot
`--account`. Rule 0 ends that incidental confinement. After it, **an account id is a bearer
capability for both read and write**: `GET /?account=<id>` returns that account's balance and full
activity, and `POST /send` with `account=<id>` spends from it. And the ids are published: the
boot account's own activity table renders **counterparty account ids** on a public page
(`app/web.py:331-355`), so the concrete form of the exposure is — *anyone who loads the demo page
can read the ids of every account the demo account has ever transacted with, and can then read and
spend from each of them.* On a demo whose ids are minted uuid hex and whose money is play money,
that is a defensible way to get a two-sided demo without building auth; it is **not** defensible
silently. Two requirements follow, and they are cheap:

1. **The page says so — on every page, and enforced at the exit rather than at the call sites.**
   `app/web.py` carries a visible one-line notice that this is an unauthenticated demo and the
   balances are not real. A public URL that accepts POSTs from anyone and does not say so is the
   version of this that we would have to apologise for later. The notice covers the **whole
   surface, read as well as write**: `GET /?account=<id>` is itself an unauthenticated read of any
   account whose id you know, so the counterparty ids in the activity table are one *way* to learn
   an id and the switcher is another *use* of one — it is not a property of the send form.
   **Mechanically:** the module has exactly **two** HTML document producers today —
   `render_wallet` (`app/web.py:120`) and `render_error` (`:169`) — and one byte-writer,
   `_send_bytes` (`:223`). "One shared banner function that both call" is still two call sites, and
   this build's own standard (T9a, the `do_POST` exit guard) is that a property which must hold on
   every path belongs at the **exit**, not at the callers. So: one `DEMO_NOTICE` constant and one
   document wrapper both builders pass through, **and** a row in `tests/test_web_accounts.py`
   asserting that every `text/html` response the running server emits — home, error page, unknown
   account, post-create — contains the marker. That row is behavioural, not enumerative: it asserts
   on the output rather than listing the call sites, so a third renderer added later goes red
   instead of silently shipping a page that says nothing.
2. **T16 records it.** The deploy step states the bearer-capability property on the record beside
   the open-registration decision.

**If id recovery is later added as a browser-local cookie, the cookie is a convenience and not a
boundary.** The POST body is client-supplied, so a cookie listing "the ids this browser created"
cannot confine what anyone may act as; it only saves the user from keeping a note. It must never
be described as a permission model, and it must never be the reason a server-side check is
skipped.

---

## T14 / T15 landed green — and the first GREEN was a test of the test

**State, measured 2026-10-02.** `app/web.py 248d97a7`, `tests/test_web_accounts.py 837c2016`,
`tests/README.md ed0afff8`. Gate: `Ran 68 tests`, `OK`, phases 2–4 clean, `63/63 checks passed`,
tree pin `44bd9971…` before == after, exit 0. Rule 0 is implemented as specified —
`_resolve_account` (`app/web.py:266`) is documented as "exactly one place", and both the renderer
and the send handler call it, so the page and the POST cannot drift.

**The two red rows the implementer reported were test-side, and the test owner repaired them —
which is why the repair is legitimate rather than the forbidden kind.** A test that is edited to
go green is the classic way to hide a defect. Here the edit was made by the **owner of the file**
(author of the suite), not by the implementer; the change was **flagged, not absorbed**; and both
replacements are stronger, not weaker:

- **An unsatisfiable assertion.** `assertNotIn("<html", page.lower().replace("<!doctype html", ""))`
  lower-cases first, so the `replace` matches nothing and the row asserted that an HTML page
  contains no `<html>` — false by construction, for *every* implementation. I re-ran the expression
  to confirm rather than accepting the diagnosis. Removed and replaced by a real check: an unknown
  account must not render an `Account <code>` label. The row's substantive assertions (200, names
  the id) were untouched.
- **A check that measured its own fixture.** `idempotency_row_count(conn) == 0` after a create,
  against a fixture that legitimately holds two seeded keys. It said nothing about the create.
  Now a **delta** around the create — any key the create minted still moves the number. The
  suite's own scoping rule was already written down by its author; the rule was right and the
  row had not followed it.

**The generalizable finding, and the reason this is in the plan.** Red-first means part of a new
file **cannot execute** while the route is missing: the missing `/create` failed those rows before
their inner assertions were ever reached. So the **first GREEN run is a test of the test**, not
just of the code — it is the first moment the file's own assertions are exercised at all. A suite
whose author never runs that moment is a suite with unread assertions in it. Both defects here
were found that way, by their author, in the run that was supposed to be the victory lap.

**Home of the demo-notice check — my ruling was wrong, corrected 2026-10-02 by the frontend-engineer
checking the bytes instead of the label.** I wrote that the property is "verified in
`app/selfcheck.py` (phase 4)". It is not in the gate at all: **`run_gate.sh` phase 4 is
`python3 -m api.selfcheck` (63 checks, the integrator's driver); `app/selfcheck.py` (51 checks) is
never invoked by `run_gate.sh`.** I conflated `api.selfcheck` with `app.selfcheck` — two files, one
letter apart, and I asserted the wrong one was in the gate. The *mechanism* I verified is real
(`_document` at `app/web.py:148-169` is the only document assembler; the row samples four live
responses and carries an in-file falsifier and an outer mutant control) — but a home outside the
gate is not a gate check, and I had certified it as one.

**Consequence, and the ruling.** Both properties that landed this round — the demo notice on every
`text/html` response, and a pending item naming its sending account — currently run only when
someone runs `python3 -m app.selfcheck`. They move into `tests/`, which the gate already runs in
phase 1, so that they run on **every** gate run. I am **not** restructuring `run_gate.sh` late for
two rows: its phase set is what every quoted phase-split claim in this plan depends on (the `4/4`
banner, the "zero FAIL after phase 4" rule, the `1/4`–`4/4` denominators), and changing
`run_gate.sh` changes `GATE PIN` as well. Precedent agrees: the wire half was re-homed into
`tests/` for exactly this reason when it needed to be in the gate.

**A real residual, recorded rather than smoothed over.** `app/selfcheck.py`'s ~51 `check(...)` rows
are **not gate-run**; only its scenario *functions* are, indirectly, because
`tests/test_persist_ordering.py` imports the module and calls `scenario_web_ui` (`:50`, `:189`).
So a driver the plan has cited as evidence across this build has never run on a gate. Two honest
responses exist — wire `python3 -m app.selfcheck` in as a fifth phase (stronger, and it would put
the existing `[WEB]` rows under the gate), or stop citing its rows as gate evidence and keep it a
developer driver. I am taking the second for now and labelling it as such, because the first
changes the gate's phase algebra with the demo in flight. Named here so it is a decision, not a
discovery; the reviewer should treat `app/selfcheck.py` output as **supplementary** evidence and
require anything it matters for to have a `tests/` row.

The rows themselves are sound and stay as the driver's own diagnostics; the frontend-engineer owns
that file and built each with a witness that can fail — the notice row swaps `_document` for a
bannerless version for a single request, and the pending-sender row swaps `_pending_block` for a
sender-dropping version. Nothing about them is deleted; they are simply not where the gate looks.

**The mistake propagated, which is the part worth recording.** The test author, told to withdraw
row (6) from `tests/` because the selfcheck home was stronger, checked the competing home as
instructed — and then deleted their own in-gate row for a reason I had supplied and that was
false: their note says the property is checked "in phase 4 of the gate". It is not. So a row that
had **already been written and was green in-gate** was removed on the strength of my error, by an
author who did the right thing (verified before accepting) against the wrong premise I gave them.
Both properties go back into `tests/` now. **The lesson is narrow and mechanical: what the gate
runs is read out of `run_gate.sh`, never inferred from a filename that looks right.** Two files,
`api/selfcheck.py` and `app/selfcheck.py`, one letter apart, and the confusion has now been
asserted twice in one round.

**And writing the withdrawn row paid for itself anyway.** Its falsifier produced its mutant output
by calling `app_web._document` — which, under `mock.patch.object`, *is the mutant*, so it recursed
into `RecursionError` instead of measuring anything. Same species as the mutants this build has
already caught: **a falsifier that cannot fail for the reason you think.** Recorded by the test
author in `tests/README.md` obligation 14 alongside the withdrawal reason, because a row asked for
and then withdrawn is exactly the kind of thing that gets re-litigated.

**The rule for witness-swap mutants, corrected 2026-10-02 — my first phrasing would have ruled out
the better witness.** I wrote "a mutant must capture the function object it replaces at import,
before the patch goes in", and then told the frontend-engineer their bill-through-free lambda was
the safe shape. The second half is wrong advice. The rule is not whether you call through, it is
**which object you call**: never call the attribute you have rebound; always call the object you
captured before rebinding. Calling through the **captured original** is the standard witness-swap
and gives the *narrower* mutant, which is the stronger one — `app/selfcheck.py`'s `_pending_block`
witness captures `real_block` first and rebinds to `lambda pending: real_block(pending).replace(
"acct-sender", "someone")`, so it drops the sender id and leaves every other rendered field real
under test. A mutant that calls through is better than one that replaces wholesale; only calling
the rebound name recurses.

**Sequence frozen, to stop the pin churn.** The implementation is landed and needs no further
change; only `tests/` moves from here. Order: (1) the test author restores the notice row and adds
the pending-sender row, both in-gate; (2) the test author re-runs and publishes the digests for
`tests/test_web_accounts.py` and `tests/README.md`; (3) the frontend-engineer re-runs the gate for
the T15 DoD on the settled tree and publishes `app/web.py` — their `7a2df5ed…` run is superseded by
(1) and they know it; (4) the reviewer anchors on that state. No pin is handed to the reviewer
before (3).

**Requirement still owed, found by reading the renderer rather than the report.** `app/web.py:339`
renders `base.store.outstanding()` **unfiltered** on every account's page, and `_pending_block`
(`:130-145`) lists each record as *"send $X to `<to_account_id>` (not confirmed)"* — **without the
sending account**. So on account B's page, an unconfirmed transfer made from A reads as B's. Rule 3
requires the page to *say* rather than *imply*, and this implies. It is not money-unsafe — Retry
replays each record with its own key and body — but it is the wrong-story shape, and it is a
one-line fix in a file that is already open. Required before the reviewer certifies.

**Rule 3's copy stays unpinned**, per the earlier ruling; the test author instead pinned the
*precondition* that a different account is on screen, which is the right instrument and is not
vacuous only if the switch works. Their rows key off `_active_account()`, the page's
`Account <code>` element: a copy change is safe, a markup change is not. The reviewer should know
that before touching the template.

**Two follow-ons the first cut deliberately does not include, both recorded so they are choices
rather than omissions:**

- **No account listing, and no recovery of a lost id.** `POST /accounts` exists; nothing
  enumerates. So the site can create and switch, but a user who closes the tab without keeping
  the id has no path back to it — the *"send to a named account"* complaint is therefore only
  half-answered. A global `GET /accounts` is the obvious fix and I am **rejecting it for this
  cut**: it would publish every account id to anyone who asks. If id recovery is wanted, the
  right shape is a **browser-local** list — a cookie holding the ids *this browser* created —
  which answers the need without turning the site into a directory. It is an `api/` change
  (integrator) plus UI work, and it is sequenced after the owner's funding decision below,
  because that decision changes what the demo needs to remember.
- **The recipient field does not prefill with the active account's own id.** I initially left
  this open; the frontend-engineer rejected it and is right. Prefilling self puts the
  **same-account path — `303 /?error=same_account` — one keypress away from every fresh account**
  and teaches the wrong gesture. The id stays visible and copyable next to the Send form, the
  field stays empty with a plain account-id placeholder, and the round trip is: A creates and
  shows the id, B pastes it into B's own form. Recorded as a ruling, not a preference.
