"""Test-owned DDL fixture.

T2 owns the `tests/` DDL fixtures (room plan §4). This is the *fallback*
schema: it mirrors room plan §3 exactly. `harness.py` prefers the ledger's own
schema module when it exposes an init function, so in the normal case the
ledger's DDL is what the tests run against and this file is only a backstop.

The only column names the invariants actually depend on are the ones pinned by
the plan: `ledger_entries.amount_minor` (§3), `*.account_id`, `*.transfer_id`
and `*.entry_id` (§2.2). They are still resolved by name at query time.
"""

DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id      TEXT PRIMARY KEY,
    owner_id        TEXT NOT NULL,
    currency        TEXT NOT NULL,
    allow_overdraft INTEGER NOT NULL DEFAULT 0
                    CHECK (allow_overdraft IN (0, 1)),
    version         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id     TEXT PRIMARY KEY,
    from_account_id TEXT NOT NULL,
    to_account_id   TEXT NOT NULL,
    amount_minor    INTEGER NOT NULL CHECK (typeof(amount_minor) = 'integer'),
    currency        TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id     TEXT PRIMARY KEY,
    transfer_id  TEXT NOT NULL,
    account_id   TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK (typeof(amount_minor) = 'integer'),
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key                 TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    transfer_id         TEXT NOT NULL,
    response_json       TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_account  ON ledger_entries (account_id);
CREATE INDEX IF NOT EXISTS idx_entries_transfer ON ledger_entries (transfer_id);
"""


def create_fixture_schema(conn):
    conn.executescript(DDL)
    conn.commit()
    return conn
