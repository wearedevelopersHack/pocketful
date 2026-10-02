"""Storage schema — Pocketful ledger.

DDL only. No module outside ``ledger/`` may create tables, and no module at all
may open a connection except ``ledger/db.py``.

Money columns are signed INTEGER minor units. ``ledger_entries.amount_minor``
carries ``CHECK (typeof(amount_minor) = 'integer')`` so SQLite itself refuses to
store a float or a numeric string in the money column. There is deliberately no
cached balance column: a balance is ``SUM(ledger_entries.amount_minor)``.
"""

from __future__ import annotations

# Written as a literal so the DDL reads correctly on its own; this is 2**53 - 1,
# matching types.MAX_MINOR.
_MAX_MINOR_LITERAL = "9007199254740991"

DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS accounts (
        account_id      TEXT    PRIMARY KEY,
        owner_id        TEXT    NOT NULL,
        currency        TEXT    NOT NULL,
        allow_overdraft INTEGER NOT NULL DEFAULT 0
                                CHECK (allow_overdraft IN (0, 1)),
        version         INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT    NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS transfers (
        transfer_id     TEXT    PRIMARY KEY,
        from_account_id TEXT    NOT NULL REFERENCES accounts(account_id),
        to_account_id   TEXT    NOT NULL REFERENCES accounts(account_id),
        amount_minor    INTEGER NOT NULL
                                CHECK (typeof(amount_minor) = 'integer'
                                       AND amount_minor > 0
                                       AND amount_minor <= {_MAX_MINOR_LITERAL}),
        currency        TEXT    NOT NULL,
        created_at      TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_entries (
        entry_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        transfer_id  TEXT    NOT NULL REFERENCES transfers(transfer_id),
        account_id   TEXT    NOT NULL REFERENCES accounts(account_id),
        amount_minor INTEGER NOT NULL
                             CHECK (typeof(amount_minor) = 'integer'
                                    AND amount_minor <> 0),
        created_at   TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        key                 TEXT PRIMARY KEY,
        request_fingerprint TEXT NOT NULL,
        transfer_id         TEXT NOT NULL REFERENCES transfers(transfer_id),
        response_json       TEXT NOT NULL,
        created_at          TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ledger_entries_account
        ON ledger_entries(account_id, entry_id)
    """,
)


def initialize(conn) -> None:
    """Create the schema on an already-configured connection.

    Idempotent: every statement is ``IF NOT EXISTS``. Only ``ledger/db.py``
    calls this, from its connection factory.
    """
    for statement in DDL:
        conn.execute(statement)
