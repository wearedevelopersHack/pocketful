"""I1..I6 as reusable assertions (INVARIANTS.md §6).

Design rule that gives these checks their teeth: **every function here reads
the database directly with SQL.** None of them calls `ledger.get_balance()`,
`ledger.list_activity()` or any other ledger API to decide whether the ledger
is correct. A bug in the ledger therefore cannot hide itself from the check
that is supposed to catch it.

The single exception is I5, which is about *behaviour under replay* and so has
to invoke the operation again; it still verifies the effect by counting rows.

Every failure raises `InvariantViolation`, which names the invariant id and
carries a dump of the state needed to reproduce it.
"""

import sqlite3


class InvariantViolation(AssertionError):
    """Raised when a money invariant is observed to be false."""

    def __init__(self, invariant, message, state=None):
        self.invariant = invariant
        self.state = state
        body = f"{invariant} VIOLATED: {message}"
        if state is not None:
            body += f"\n  state: {state}"
        super().__init__(body)


# --------------------------------------------------------------------------
# schema introspection — resolve column names rather than assuming them
# --------------------------------------------------------------------------

def table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def columns(conn, table):
    if not table_exists(conn, table):
        return set()
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _amount_col(conn, table="ledger_entries"):
    cols = columns(conn, table)
    for name in ("amount_minor", "amount", "amount_cents", "amount_units"):
        if name in cols:
            return name
    raise InvariantViolation(
        "schema",
        f"no minor-unit amount column found in {table!r} (columns={sorted(cols)})",
    )


def _account_col(conn, table="ledger_entries"):
    cols = columns(conn, table)
    for name in ("account_id", "acct_id"):
        if name in cols:
            return name
    raise InvariantViolation(
        "schema", f"no account column found in {table!r} (columns={sorted(cols)})"
    )


def state_dump(conn):
    """A compact, reproducible snapshot for failure messages."""
    dump = {}
    if table_exists(conn, "ledger_entries"):
        a = _amount_col(conn)
        dump["ledger_entries"] = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM({a}), 0) FROM ledger_entries"
        ).fetchone()
    if table_exists(conn, "accounts"):
        dump["accounts"] = [
            tuple(r)
            for r in conn.execute(
                "SELECT account_id, allow_overdraft FROM accounts ORDER BY account_id"
            )
        ]
    if table_exists(conn, "idempotency_keys"):
        dump["idempotency_keys"] = conn.execute(
            "SELECT COUNT(*) FROM idempotency_keys"
        ).fetchone()[0]
    dump["balances"] = balances(conn)
    return dump


def balances(conn):
    """Per-account derived balance, computed from entries only."""
    if not table_exists(conn, "accounts") or not table_exists(conn, "ledger_entries"):
        return {}
    acct = _account_col(conn)
    amount = _amount_col(conn)
    rows = conn.execute(
        f"""
        SELECT a.account_id, COALESCE(SUM(e.{amount}), 0)
          FROM accounts a
          LEFT JOIN ledger_entries e ON e.{acct} = a.account_id
         GROUP BY a.account_id
         ORDER BY a.account_id
        """
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def entry_count(conn):
    return conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]


def transfer_row_count(conn):
    if not table_exists(conn, "transfers"):
        return 0
    return conn.execute("SELECT COUNT(*) FROM transfers").fetchone()[0]


def idempotency_row_count(conn):
    if not table_exists(conn, "idempotency_keys"):
        return 0
    return conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0]


def idempotency_count_for_key(conn, key):
    """Rows for ONE idempotency key.

    Scoped deliberately: a global row count includes the fixture's seed
    transfers, so it answers a different question than "was this operation
    recorded exactly once".
    """
    if not table_exists(conn, "idempotency_keys"):
        return 0
    return conn.execute(
        "SELECT COUNT(*) FROM idempotency_keys WHERE key = ?", (key,)
    ).fetchone()[0]


def transfer_count_for_id(conn, transfer_id):
    if not table_exists(conn, "transfers"):
        return 0
    return conn.execute(
        "SELECT COUNT(*) FROM transfers WHERE transfer_id = ?", (transfer_id,)
    ).fetchone()[0]


# --------------------------------------------------------------------------
# I1 — SUM(amount) over all ledger entries == 0
# --------------------------------------------------------------------------

def assert_i1(conn):
    amount = _amount_col(conn)
    total = conn.execute(
        f"SELECT COALESCE(SUM({amount}), 0) FROM ledger_entries"
    ).fetchone()[0]
    if total != 0:
        raise InvariantViolation(
            "I1",
            f"SUM(amount_minor) over all ledger entries is {total}, expected 0 "
            f"(value was created or destroyed)",
            state_dump(conn),
        )
    return total


# --------------------------------------------------------------------------
# I2 — SUM(balance) over all accounts == 0 (the system is closed)
# --------------------------------------------------------------------------

def assert_i2(conn):
    if not table_exists(conn, "accounts") or not table_exists(conn, "ledger_entries"):
        return 0
    acct = _account_col(conn)

    orphans = conn.execute(
        f"""
        SELECT COUNT(*) FROM ledger_entries e
         WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.account_id = e.{acct})
        """
    ).fetchone()[0]
    if orphans:
        raise InvariantViolation(
            "I2",
            f"{orphans} ledger entr(ies) reference an account that does not exist; "
            f"their value is outside the closed system",
            state_dump(conn),
        )

    total = sum(balances(conn).values())
    if total != 0:
        raise InvariantViolation(
            "I2",
            f"SUM(balance) over all accounts is {total}, expected 0 "
            f"(system is not closed)",
            state_dump(conn),
        )
    return total


# --------------------------------------------------------------------------
# I3 — every transfer's entries sum to 0
# --------------------------------------------------------------------------

def assert_i3(conn, transfer_id=None):
    amount = _amount_col(conn)
    if transfer_id is None:
        where, params = "", ()
    else:
        where, params = "WHERE transfer_id = ?", (transfer_id,)
    rows = conn.execute(
        f"""
        SELECT transfer_id, SUM({amount}) AS net, COUNT(*) AS n
          FROM ledger_entries {where}
         GROUP BY transfer_id
        """,
        params,
    ).fetchall()
    for tid, net, n in rows:
        if net != 0:
            raise InvariantViolation(
                "I3",
                f"transfer {tid!r} entries net to {net}, expected 0",
                state_dump(conn),
            )
        if n < 2:
            raise InvariantViolation(
                "I3",
                f"transfer {tid!r} has only {n} entr(ies); double-entry needs >= 2",
                state_dump(conn),
            )
    return rows


# --------------------------------------------------------------------------
# I4 — no negative balance unless the account's policy allows overdraft
# --------------------------------------------------------------------------

def assert_i4(conn):
    if not table_exists(conn, "accounts") or not table_exists(conn, "ledger_entries"):
        return
    overdraft = set(
        r[0]
        for r in conn.execute(
            "SELECT account_id FROM accounts WHERE allow_overdraft = 1"
        )
    )
    for account_id, balance in balances(conn).items():
        if balance < 0 and account_id not in overdraft:
            raise InvariantViolation(
                "I4",
                f"account {account_id!r} has balance {balance} < 0 and does not "
                f"allow overdraft",
                state_dump(conn),
            )


# --------------------------------------------------------------------------
# I6 — sum of split parts == the original total   (pure, no ledger needed)
# --------------------------------------------------------------------------

def split_ok(split_fn, total, parts):
    """Return None if (split_fn, total, parts) satisfies I6, else a reason str."""
    result = split_fn(total, parts)
    if len(result) != parts:
        return f"split({total}, {parts}) returned {len(result)} parts, expected {parts}"
    for i, p in enumerate(result):
        if isinstance(p, bool) or not isinstance(p, int):
            return f"split({total}, {parts})[{i}] = {p!r} is not a plain int"
    if sum(result) != total:
        return (
            f"split({total}, {parts}) = {result} sums to {sum(result)}, "
            f"expected {total} (delta {sum(result) - total})"
        )
    return None


def assert_i6(split_fn, total, parts):
    """Assert I6 for one (total, parts) pair."""
    reason = split_ok(split_fn, total, parts)
    if reason is not None:
        raise InvariantViolation("I6", reason)
    return split_fn(total, parts)


# --------------------------------------------------------------------------
# I5 — replaying a request with its original key is a no-op
# --------------------------------------------------------------------------

def assert_i5_replay_noop(conn, invoke, *, expected_transfer_id):
    """Invoke `invoke()` (a call that replays the original request) and assert it
    changed nothing.

    `conn` must be a connection that can see the committed state both before and
    after the call. Asserts: same transfer_id comes back, and no new ledger
    entries / transfers / idempotency rows appear.
    """
    before = (entry_count(conn), transfer_row_count(conn), idempotency_row_count(conn))
    result = invoke()
    after = (entry_count(conn), transfer_row_count(conn), idempotency_row_count(conn))
    if getattr(result, "transfer_id", None) != expected_transfer_id:
        raise InvariantViolation(
            "I5",
            f"replay returned transfer_id "
            f"{getattr(result, 'transfer_id', None)!r}, expected the original "
            f"{expected_transfer_id!r}",
            state_dump(conn),
        )
    if before != after:
        raise InvariantViolation(
            "I5",
            f"replay was not a no-op: (entries, transfers, keys) went "
            f"{before} -> {after}",
            state_dump(conn),
        )
    return result


def assert_all(conn):
    """I1 + I2 together — the pair the contract requires after every scenario."""
    assert_i1(conn)
    assert_i2(conn)
