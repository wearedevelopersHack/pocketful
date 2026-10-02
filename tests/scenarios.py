"""The five required test obligations (INVARIANTS.md §7).

These are written against a `Backend` (see `harness.py`) rather than directly
against `ledger.core.Ledger`, for one reason: `test_harness_selfcheck.py` points
the *same* scenario code at deliberately broken implementations and asserts the
scenario fails. A scenario that only ever passes is not evidence of anything.

Each `scenario_*` function raises `InvariantViolation` (or any AssertionError)
when the ledger is wrong, and returns a small summary dict when it is right.
"""

import random
import uuid

from tests import invariants as inv
from tests.harness import CURRENCY, MAX_MINOR, fingerprint, run_concurrent

MINT = "__mint__"


# --------------------------------------------------------------------------
# small helpers on the §2.1 surface
# --------------------------------------------------------------------------

def open_account(backend, account_id, *, allow_overdraft=False, currency=CURRENCY):
    with backend.fresh() as (conn, ledger):
        return ledger.open_account(
            account_id=account_id,
            owner_id=f"owner-{account_id}",
            currency=currency,
            allow_overdraft=allow_overdraft,
        )


def transfer(backend, *, key, from_id, to_id, amount, currency=CURRENCY):
    with backend.fresh() as (conn, ledger):
        return ledger.transfer(
            idempotency_key=key,
            request_fingerprint=fingerprint(from_id, to_id, amount, currency),
            from_account_id=from_id,
            to_account_id=to_id,
            amount_minor=amount,
            currency=currency,
        )


def transfer_task(backend, *, key, from_id, to_id, amount, currency=CURRENCY):
    """One concurrent unit of work: one connection, one request."""
    def task():
        return transfer(
            backend, key=key, from_id=from_id, to_id=to_id, amount=amount,
            currency=currency,
        )
    return task


def fund(backend, target, amount, *, tag="seed"):
    """Seed `target` from the overdraft mint account, keeping the system closed."""
    return transfer(
        backend, key=f"{tag}-{target}-{amount}-{uuid.uuid4().hex}",
        from_id=MINT, to_id=target, amount=amount,
    )


def _setup_wallets(backend, accounts):
    """accounts: dict name -> initial funded balance."""
    open_account(backend, MINT, allow_overdraft=True)
    for name, amount in accounts.items():
        open_account(backend, name)
        if amount:
            fund(backend, name, amount)


# --------------------------------------------------------------------------
# Obligation 1 — parallel transfer storm
# --------------------------------------------------------------------------

HOT = "acct-hot"
RING = [f"acct-ring-{i}" for i in range(4)]
HOT_SEED = 50
RING_SEED = 100
HOT_TASKS = 100
RING_TASKS = 100


def scenario_parallel_storm(backend):
    """N concurrent transfers over overlapping account pairs, exact final balances.

    Two storms run at once and overlap on the same accounts:

    * 100 concurrent debits of 1 minor unit out of HOT, which is seeded with
      exactly 50 — so exactly 50 must succeed and 50 must be rejected, whatever
      the interleaving. A ledger that reads the payer balance outside the write
      transaction (check-then-act) over-applies here and drives HOT negative.
    * 100 concurrent transfers of 1 around the RING of four accounts, which
      overlap the same counterparties. Net zero, and it keeps every connection
      contending on the same rows.

    Exact expectations: HOT == 0, sum(RING) == RING_SEED*4 + 50, mint == -(that),
    total over all accounts == 0.
    """
    _setup_wallets(backend, {HOT: HOT_SEED, **{r: RING_SEED for r in RING}})

    tasks = []
    hot_index = []
    for i in range(HOT_TASKS):
        hot_index.append(len(tasks))
        tasks.append(transfer_task(
            backend, key=f"hot-{i}", from_id=HOT, to_id=RING[i % len(RING)], amount=1,
        ))
    ring_index = []
    for i in range(RING_TASKS):
        ring_index.append(len(tasks))
        tasks.append(transfer_task(
            backend, key=f"ring-{i}",
            from_id=RING[i % len(RING)], to_id=RING[(i + 1) % len(RING)], amount=1,
        ))

    results, errors = run_concurrent(tasks)

    with backend.read_conn() as conn:
        bal = inv.balances(conn)
        hot_success = sum(1 for i in hot_index if errors[i] is None)
        hot_errors = [errors[i] for i in hot_index if errors[i] is not None]
        ring_errors = [errors[i] for i in ring_index if errors[i] is not None]

        problems = []
        if ring_errors:
            problems.append(f"{len(ring_errors)} RING transfers failed (all should succeed): "
                            f"{ring_errors[:3]!r}")
        if hot_success != HOT_SEED:
            problems.append(
                f"HOT accepted {hot_success} debits, expected exactly {HOT_SEED} "
                f"(HOT was seeded with {HOT_SEED})"
            )
        if not all(isinstance(e, Exception) for e in hot_errors):
            problems.append("a HOT failure was not an exception")
        if bal.get(HOT) != 0:
            problems.append(f"HOT final balance is {bal.get(HOT)}, expected exactly 0")
        ring_total = sum(bal.get(r, 0) for r in RING)
        expected_ring = RING_SEED * len(RING) + HOT_SEED
        if ring_total != expected_ring:
            problems.append(
                f"sum(RING) is {ring_total}, expected exactly {expected_ring}"
            )

        if problems:
            raise inv.InvariantViolation(
                "O1/parallel-storm",
                "; ".join(problems),
                state=inv.state_dump(conn),
            )

        inv.assert_all(conn)   # I1 + I2
        inv.assert_i3(conn)
        inv.assert_i4(conn)

    return {
        "hot_successes": hot_success,
        "hot_expected": HOT_SEED,
        "hot_final": bal.get(HOT),
        "ring_total": ring_total,
        "tasks": len(tasks),
    }


# --------------------------------------------------------------------------
# Obligation 2 — idempotent retry storm
# --------------------------------------------------------------------------

RETRY_N = 100


def scenario_retry_storm(backend):
    """ONE idempotency key, fired 100x concurrently.

    Must apply exactly once (one entry pair, one transfer row), and every one of
    the 100 callers must receive the *same* transfer_id. Exactly one call is
    'applied'; the rest are 'replayed'.
    """
    payer, payee = "acct-payer", "acct-payee"
    _setup_wallets(backend, {payer: 1000, payee: 0})

    key = "one-key-fired-100x"
    amount = 10
    tasks = [
        transfer_task(backend, key=key, from_id=payer, to_id=payee, amount=amount)
        for _ in range(RETRY_N)
    ]

    with backend.read_conn() as before_conn:
        entries_before = inv.entry_count(before_conn)
        inv.assert_all(before_conn)

    results, errors = run_concurrent(tasks)

    with backend.read_conn() as conn:
        problems = []
        raised = [(i, errors[i]) for i in range(RETRY_N) if errors[i] is not None]
        if raised:
            problems.append(
                f"{len(raised)} of {RETRY_N} same-key calls raised (none should): "
                f"{raised[:3]!r}"
            )

        ok = [r for r in results if r is not None]
        transfer_ids = {r.transfer_id for r in ok}
        if len(transfer_ids) != 1:
            problems.append(
                f"callers received {len(transfer_ids)} distinct transfer_ids "
                f"{sorted(transfer_ids)[:4]}, expected exactly 1"
            )
        applied = [r for r in ok if r.status == "applied"]
        replayed = [r for r in ok if r.status == "replayed"]
        if len(applied) != 1:
            problems.append(f"{len(applied)} calls reported 'applied', expected exactly 1")
        if len(replayed) != RETRY_N - 1:
            problems.append(
                f"{len(replayed)} calls reported 'replayed', expected {RETRY_N - 1}"
            )

        entries_after = inv.entry_count(conn)
        if entries_after - entries_before != 2:
            problems.append(
                f"ledger_entries grew by {entries_after - entries_before}, expected 2 "
                f"(the operation must apply exactly once)"
            )
        if len(transfer_ids) == 1:
            tid = next(iter(transfer_ids))
            rows = conn.execute(
                "SELECT COUNT(*) FROM ledger_entries WHERE transfer_id = ?", (tid,)
            ).fetchone()[0]
            if rows != 2:
                problems.append(f"transfer {tid!r} has {rows} entries, expected 2")
            trows = inv.transfer_count_for_id(conn, tid)
            if trows != 1:
                problems.append(
                    f"transfer {tid!r} has {trows} transfer rows, expected exactly 1"
                )
        # Scoped to this operation's key. A global row count includes the seed
        # transfers and was never testing what it claimed to test.
        key_rows = inv.idempotency_count_for_key(conn, key)
        if key_rows != 1:
            problems.append(
                f"the storm key {key!r} has {key_rows} idempotency rows, "
                f"expected exactly 1"
            )
        if inv.balances(conn).get(payee) != amount:
            problems.append(
                f"payee balance is {inv.balances(conn).get(payee)}, expected {amount} "
                f"(applied more or less than once)"
            )

        if problems:
            raise inv.InvariantViolation("O2/retry-storm", "; ".join(problems),
                                         state=inv.state_dump(conn))
        inv.assert_all(conn)

    return {
        "calls": RETRY_N,
        "applied": len(applied),
        "replayed": len(replayed),
        "distinct_transfer_ids": len(transfer_ids),
    }


# --------------------------------------------------------------------------
# Obligation 3 — rounding sweep
# --------------------------------------------------------------------------

PRIME_TOTALS = (2, 3, 7, 101, 997, 1009, 65537, 104729, 2147483647, MAX_MINOR)
SPLIT_TOTALS = (
    (0, 1, 2, 3, 5, 8, 10, 77, 99, 100, 1000, 1001, 12345, 123457, 999999)
    + PRIME_TOTALS
    + (-1, -2, -7, -13, -101, -1000, -104729)
)
SPLIT_PARTS = (1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 13, 17, 32, 100, 101, 256, 997,
               1000, 65537)


def scenario_rounding_sweep(split_fn):
    """I6 over many (total, n) pairs, including prime totals and n=1."""
    checked = 0
    for total in SPLIT_TOTALS:
        for parts in SPLIT_PARTS:
            if parts < 1:
                continue
            first = inv.assert_i6(split_fn, total, parts)
            checked += 1

            # Deterministic and order-stable: same call, same answer.
            if split_fn(total, parts) != first:
                raise inv.InvariantViolation(
                    "I6", f"split({total}, {parts}) is not deterministic: "
                          f"{first!r} then {split_fn(total, parts)!r}",
                )
            # The documented remainder policy: parts differ by at most one unit.
            if max(first) - min(first) > 1:
                raise inv.InvariantViolation(
                    "I6", f"split({total}, {parts}) = {first} spreads the remainder by "
                          f"more than one minor unit",
                )
            if total != 0 and all(p == 0 for p in first):
                raise inv.InvariantViolation(
                    "I6", f"split({total}, {parts}) discarded a non-zero total: {first!r}",
                )
    return {"pairs_checked": checked}


# --------------------------------------------------------------------------
# Never-coerce guard — an amount is a plain int or it is rejected
# --------------------------------------------------------------------------

VALIDATION_BAD_AMOUNTS = (
    12.34, 1.0, "500", "0x10", 3 + 0j, True, False, 0, -500, 2 ** 53, None, [1],
)
"""Every way an amount can be something other than a plain positive int.

Four entries earn their place specifically:

* ``True``/``False`` — ``bool`` is an ``int`` subclass in Python, so an
  ``isinstance(v, int)`` check accepts them and ``True`` silently becomes
  amount 1. §2.2 names this trap; it is the case a hand-written list misses.
* ``1.0`` — a float that is integrally valued. It slips past any
  ``v == int(v)``-shaped check, while ``12.34`` is caught by every naive one.
  It is what separates a real type check from a shape check.
* ``"0x10"`` and ``3 + 0j`` — the shapes a *clever* coercion lets through.
  ``int()`` refuses both (``"0x10"`` is not decimal, a complex is not
  ``__int__``-able in the way a float is), so they add no teeth against the
  current mutant; they are here because a validator that reached for ``eval``,
  ``int(s, 0)`` or a numpy scalar would accept them, and the rule is that the
  only acceptable amount is a plain ``int``.
"""


def scenario_amount_validation(backend, *, amounts=None):
    """Every non-int, non-positive and out-of-range amount must be rejected with
    the ledger's own InvalidAmount, and nothing may move.

    `amounts` defaults to the full matrix; passing a subset is how the gate
    proves *which* value is load-bearing rather than asserting it — the
    shape-checker mutant passes this scenario the moment `1.0` is removed, and
    that is a checkable claim only if the list can be varied.

    `assertRaises` on its own is a weak test: a validator that writes half a
    double entry and *then* raises would satisfy it and destroy money. So each
    rejection is also checked to have left the ledger untouched — including the
    request's own idempotency key. A rejected request that recorded its key
    would make a retry of a transfer that never happened *replay* instead of
    apply, and the money would vanish silently.
    """
    payer, payee = "val-a", "val-b"
    _setup_wallets(backend, {payer: 10000, payee: 0})
    expected_error = backend.invalid_amount_error

    if amounts is None:
        amounts = VALIDATION_BAD_AMOUNTS

    problems = []
    for index, amount in enumerate(amounts):
        key = f"reject-{index}"
        # The baseline is taken immediately before *this* attempt. A single
        # baseline for the whole loop would blame every later value for the
        # damage an earlier bad value did, naming the wrong amount in the
        # failure — the first value that wrongly applies is the finding.
        with backend.read_conn() as conn:
            entries_before = inv.entry_count(conn)
            transfers_before = inv.transfer_row_count(conn)
            balances_before = inv.balances(conn)
        try:
            transfer(backend, key=key, from_id=payer, to_id=payee, amount=amount)
        except expected_error:
            pass
        except Exception as exc:  # noqa: BLE001 - the wrong error is the finding
            problems.append(
                f"amount {amount!r} raised {type(exc).__name__}({exc}), expected "
                f"{expected_error.__name__}"
            )
            continue
        else:
            problems.append(
                f"amount {amount!r} was accepted and applied; it must be rejected "
                f"with {expected_error.__name__}"
            )
            continue

        with backend.read_conn() as conn:
            if inv.entry_count(conn) != entries_before:
                problems.append(
                    f"amount {amount!r} was rejected but left ledger entries behind"
                )
            if inv.transfer_row_count(conn) != transfers_before:
                problems.append(
                    f"amount {amount!r} was rejected but left a transfer row behind"
                )
            if inv.balances(conn) != balances_before:
                problems.append(f"amount {amount!r} was rejected but moved money")
            if inv.idempotency_count_for_key(conn, key) != 0:
                problems.append(
                    f"amount {amount!r} was rejected but recorded key {key!r}; a "
                    f"retry would replay a transfer that never happened"
                )
            inv.assert_all(conn)

    if problems:
        with backend.read_conn() as conn:
            state = inv.state_dump(conn)
        raise inv.InvariantViolation(
            "V/amount-validation", "; ".join(problems), state=state
        )
    return {"checked": len(amounts)}


# --------------------------------------------------------------------------
# Refusal path — a refused request must not record its key
# --------------------------------------------------------------------------

REFUSAL_PAYER = "refusal-payer"
REFUSAL_PAYEE = "refusal-payee"
REFUSAL_SEED = 100
REFUSAL_TOO_MUCH = 500
REFUSAL_AFFORDABLE = 100
REFUSAL_KEY = "refusal-key"
REFUSAL_B_KEY = "refusal-b-key"
REFUSAL_B_PAYER = "refusal-b-payer"
REFUSAL_B_PAYEE = "refusal-b-payee"


def scenario_refusal_path(backend):
    """A refused transfer must leave no trace — including its idempotency key.

    Both refusals here are raised *inside* the write transaction, which is the
    case the amount-validation matrix does not reach: `_require_amount` runs
    before a transaction exists, so a refused amount never had a key to burn. An
    insufficient-funds refusal is different — by the time it is discovered, the
    bank has already claimed the key, and the rollback is the only thing that
    gives it back.

    Getting this wrong is the worst failure the build can have, and I1–I6 cannot
    see it. The refusal records its key; the money never moves; every balance
    still sums to zero; and the next caller who retries that key is told the
    transfer *succeeded*, holding a `transfer_id` for a transfer that never
    happened. Silent, permanent, green. So the assertion here is deliberately
    not "no key row exists" — that is a shape. It is "a retry with that key
    still does the work", which is the property the shape exists to protect.

    Two legs, the second so the first cannot be satisfied by a ledger that
    simply never writes keys:

      A. a refusal leaves no key, no entries, no transfer row, no balance move,
         and a retry with the SAME key and a DIFFERENT body still applies;
      B. once a request HAS applied, same key + same body replays with the same
         transfer_id and no balance move, and same key + a different body
         conflicts with the balance still untouched.
    """
    _setup_wallets(backend, {REFUSAL_PAYER: REFUSAL_SEED, REFUSAL_PAYEE: 0})

    with backend.read_conn() as conn:
        before = (inv.entry_count(conn), inv.transfer_row_count(conn),
                  inv.balances(conn))

    problems = []

    # -- Leg A: the refusal itself ---------------------------------------
    refused = None
    try:
        transfer(backend, key=REFUSAL_KEY, from_id=REFUSAL_PAYER,
                 to_id=REFUSAL_PAYEE, amount=REFUSAL_TOO_MUCH)
    except Exception as exc:  # noqa: BLE001 - any failure is the point
        refused = exc
    if refused is None:
        problems.append(
            f"a transfer of {REFUSAL_TOO_MUCH} from an account holding "
            f"{REFUSAL_SEED} was accepted; there is nothing to test"
        )

    with backend.read_conn() as conn:
        if (inv.entry_count(conn), inv.transfer_row_count(conn),
                inv.balances(conn)) != before:
            problems.append(
                "the refused transfer wrote a ledger entry, a transfer row or a "
                "balance change"
            )
        burned = inv.idempotency_count_for_key(conn, REFUSAL_KEY)
        if burned != 0:
            problems.append(
                f"the refused transfer left {burned} idempotency row(s) for key "
                f"{REFUSAL_KEY!r}; the refusal will now be replayed as a success "
                f"and the money will never move"
            )

    # -- Leg A, continued: the retry must still do the work --------------
    if refused is not None:
        # Same key, different body. The refusal never happened, so this is a new
        # request — not a conflict with a stored result.
        try:
            retry = transfer(backend, key=REFUSAL_KEY, from_id=REFUSAL_PAYER,
                             to_id=REFUSAL_PAYEE, amount=REFUSAL_AFFORDABLE)
        except Exception as exc:  # noqa: BLE001 - the raised error is the finding
            retry = None
            problems.append(
                f"retrying the refused key with a different, affordable body "
                f"raised {type(exc).__name__}({exc}); the refused request must "
                f"leave nothing for the retry to conflict with"
            )
        if retry is not None and retry.status != "applied":
            problems.append(
                f"the retry after refusal reported status {retry.status!r}, "
                f"expected 'applied'"
            )
        with backend.read_conn() as conn:
            if retry is not None and (
                inv.balances(conn).get(REFUSAL_PAYEE) != REFUSAL_AFFORDABLE
            ):
                problems.append(
                    f"after the retry {REFUSAL_PAYEE} holds "
                    f"{inv.balances(conn).get(REFUSAL_PAYEE)}, expected "
                    f"{REFUSAL_AFFORDABLE}"
                )
            if inv.entry_count(conn) != before[0] + 2:
                problems.append(
                    f"the retry left {inv.entry_count(conn) - before[0]} ledger "
                    f"entr(ies), expected exactly 2"
                )
            inv.assert_all(conn)

    # -- Leg B: applied requests replay and conflict ----------------------
    open_account(backend, REFUSAL_B_PAYER)
    open_account(backend, REFUSAL_B_PAYEE)
    fund(backend, REFUSAL_B_PAYER, REFUSAL_SEED)

    first = transfer(backend, key=REFUSAL_B_KEY, from_id=REFUSAL_B_PAYER,
                     to_id=REFUSAL_B_PAYEE, amount=REFUSAL_AFFORDABLE)
    if first.status != "applied":
        problems.append(
            f"the first use of a fresh key reported {first.status!r}, expected "
            f"'applied'"
        )

    with backend.read_conn() as conn:
        settled = inv.balances(conn)

    replay = transfer(backend, key=REFUSAL_B_KEY, from_id=REFUSAL_B_PAYER,
                      to_id=REFUSAL_B_PAYEE, amount=REFUSAL_AFFORDABLE)
    if replay.status != "replayed":
        problems.append(
            f"same key + same body reported status {replay.status!r}, expected "
            f"'replayed'"
        )
    if replay.transfer_id != first.transfer_id:
        problems.append(
            f"the replay returned transfer_id {replay.transfer_id!r} but the "
            f"original was {first.transfer_id!r}; the caller is being told a "
            f"different thing happened"
        )

    conflict = None
    try:
        transfer(backend, key=REFUSAL_B_KEY, from_id=REFUSAL_B_PAYER,
                 to_id=REFUSAL_B_PAYEE, amount=REFUSAL_AFFORDABLE + 1)
    except Exception as exc:  # noqa: BLE001 - the raised error is the finding
        conflict = exc
    if conflict is None:
        problems.append(
            "same key + a different body was accepted; the second body must be "
            "refused, not applied"
        )

    with backend.read_conn() as conn:
        if inv.balances(conn) != settled:
            problems.append(
                f"the balance moved across a replay or a conflicting retry: "
                f"{settled} -> {inv.balances(conn)}"
            )
        if inv.idempotency_count_for_key(conn, REFUSAL_B_KEY) != 1:
            problems.append(
                f"key {REFUSAL_B_KEY!r} has "
                f"{inv.idempotency_count_for_key(conn, REFUSAL_B_KEY)} idempotency "
                f"row(s) after one application, expected exactly 1"
            )
        inv.assert_all(conn)

    if problems:
        with backend.read_conn() as conn:
            state = inv.state_dump(conn)
        raise inv.InvariantViolation("R/refusal-path", "; ".join(problems),
                                     state=state)

    return {"applied": first.status, "replayed": replay.status,
            "refusal": type(refused).__name__ if refused else None}


# --------------------------------------------------------------------------
# Obligation 4 — failure atomicity
# --------------------------------------------------------------------------

INJECT_TRIGGER = "tests_inject_mid_transfer_failure"

TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS {INJECT_TRIGGER}
AFTER INSERT ON ledger_entries
WHEN (SELECT COUNT(*) FROM ledger_entries WHERE transfer_id = NEW.transfer_id) >= 2
BEGIN
    SELECT RAISE(ABORT, 'injected mid-transfer failure');
END;
"""


def scenario_failure_atomicity(backend, *, keep_trigger=False):
    """A failure injected mid-transfer must leave nothing behind.

    The failure is injected by the test, not by editing production code: an
    AFTER INSERT trigger aborts the *second* entry of the next transfer, i.e.
    after the first half of the double entry has been written and before the
    transaction commits. A ledger that commits entries as it goes leaves a
    half-transfer (money destroyed, I1 broken); a ledger with a real transaction
    rolls the whole thing back.
    """
    payer, payee = "acct-atomic-a", "acct-atomic-b"
    _setup_wallets(backend, {payer: 1000, payee: 0})
    key = "atomicity-key"
    amount = 100

    with backend.read_conn() as conn:
        entries_before = inv.entry_count(conn)
        transfers_before = inv.transfer_row_count(conn)
        balances_before = inv.balances(conn)
        conn.execute(TRIGGER_SQL)
        conn.commit()

    raised = None
    try:
        transfer(backend, key=key, from_id=payer, to_id=payee, amount=amount)
    except Exception as exc:  # noqa: BLE001 - any failure is the point
        raised = exc

    with backend.read_conn() as conn:
        problems = []
        if raised is None:
            problems.append(
                "the injected mid-transfer failure did not propagate: transfer() "
                "returned success"
            )
        # Everything below is scoped to THIS operation: deltas around it, and the
        # one key it used. The fixture's seed transfers are not survivors of it.
        entries_after = inv.entry_count(conn)
        if entries_after != entries_before:
            problems.append(
                f"{entries_after - entries_before} ledger entr(ies) survived the "
                f"failed transfer; expected 0 partial entries"
            )
        transfers_after = inv.transfer_row_count(conn)
        if transfers_after != transfers_before:
            problems.append(
                f"{transfers_after - transfers_before} transfer row(s) survived the "
                f"failed transfer; expected 0"
            )
        if inv.balances(conn) != balances_before:
            problems.append(
                f"balances changed across a failed transfer: {balances_before} -> "
                f"{inv.balances(conn)}"
            )
        key_rows = inv.idempotency_count_for_key(conn, key)
        if key_rows != 0:
            problems.append(
                f"the failed transfer's key {key!r} left {key_rows} idempotency "
                f"row(s) behind; a retry would be treated as already-applied and the "
                f"money would vanish"
            )
        if problems:
            raise inv.InvariantViolation("O4/atomicity", "; ".join(problems),
                                         state=inv.state_dump(conn))
        inv.assert_all(conn)

    if keep_trigger:
        return {"raised": type(raised).__name__}

    # Drop the trigger and prove the same key still applies exactly once.
    with backend.read_conn() as conn:
        conn.execute(f"DROP TRIGGER IF EXISTS {INJECT_TRIGGER}")
        conn.commit()

    result = transfer(backend, key=key, from_id=payer, to_id=payee, amount=amount)
    with backend.read_conn() as conn:
        if result.status != "applied":
            raise inv.InvariantViolation(
                "O4/atomicity",
                f"retry after the rolled-back failure returned status "
                f"{result.status!r}; the rolled-back attempt must not have been "
                f"recorded",
                state=inv.state_dump(conn),
            )
        if inv.entry_count(conn) != entries_before + 2:
            raise inv.InvariantViolation(
                "O4/atomicity",
                f"retry produced {inv.entry_count(conn) - entries_before} entries, "
                f"expected exactly 2",
                state=inv.state_dump(conn),
            )
        if inv.balances(conn).get(payee) != amount:
            raise inv.InvariantViolation(
                "O4/atomicity",
                f"payee balance is {inv.balances(conn).get(payee)}, expected {amount}",
                state=inv.state_dump(conn),
            )
        inv.assert_all(conn)

    return {"raised": type(raised).__name__, "retry_status": result.status}


# --------------------------------------------------------------------------
# Obligation 6 — the permanent check-then-act mutation probe
# --------------------------------------------------------------------------

RACE_SRC = "race-src"
RACE_DST = "race-dst"
RACE_N = 20
RACE_SEED = 1000
RACE_AMOUNT = 100


def scenario_overdraft_race(backend, *, n=RACE_N, seed=RACE_SEED, amount=RACE_AMOUNT):
    """N barrier-released concurrent debits of a payer that can afford only half.

    `src` is seeded with exactly `seed`; `n` transfers of `amount` are released
    from a shared barrier, so exactly `seed // amount` of them can be afforded.
    A ledger that reads the payer balance OUTSIDE the write transaction sees the
    same stale balance in every reader, accepts all `n`, drives `src` to
    `seed - n * amount`, and violates I4.

    This is the permanent mutation probe for obligation 6 (room plan §4, T2 DoD
    item 6): it must turn the check-then-act fixture RED — it over-applies, src
    goes negative, I4 is violated — and the real ledger GREEN.

    Note for anyone tempted to assert an exact count against the broken variant:
    the mutant's stale-read gap *widens* the race window, it does not promise
    that all `n` readers read before the first commit. The read is
    unsynchronized (`mutants.CheckThenActLedger`), so a run in which enough
    readers reach `get_balance` after the first commit sees a post-commit
    balance, and a run in which that happens for `n - affordable` of them yields
    exactly `affordable` applies and no over-application at all. The observed
    distribution over 600 drives was {20: 579, 19: 13, 18: 4, 17: 2, 16: 1,
    13: 1}, with a single miss in an earlier 60-drive loop that could not be
    reproduced in 800 further drives. So the honest claim is "no miss observed",
    not "cannot miss" — an earlier revision of this docstring said "detection is
    100%", which asserted more than the mechanism guarantees.

    The property the self-check asserts is therefore **over**-application
    (`applied > affordable`), never an exact count. If the margin is ever lost
    the gate goes red, loudly, in the direction where someone looks — which is
    why the fixture is left as it is rather than made deterministic with a
    second barrier: a barrier that can hang is a worse failure than a red.
    """
    src, dst = RACE_SRC, RACE_DST
    _setup_wallets(backend, {src: seed, dst: 0})

    tasks = [
        transfer_task(backend, key=f"race-{i}", from_id=src, to_id=dst, amount=amount)
        for i in range(n)
    ]
    _results, errors = run_concurrent(tasks)

    affordable = seed // amount
    with backend.read_conn() as conn:
        applied = sum(1 for i in range(n) if errors[i] is None)
        src_balance = inv.balances(conn).get(src)
        problems = []
        if applied != affordable:
            problems.append(
                f"applied {applied} of {n} concurrent transfers; expected {affordable} "
                f"(src holds {seed}, each transfer is {amount})"
            )
        expected_src = seed - affordable * amount
        if src_balance != expected_src:
            problems.append(
                f"src final balance is {src_balance}, expected exactly {expected_src}"
            )
        if problems:
            raise inv.InvariantViolation("O1/overdraft-race", "; ".join(problems),
                                         state=inv.state_dump(conn))
        inv.assert_all(conn)   # I1 + I2
        inv.assert_i4(conn)    # no negative balance without an overdraft policy
    return {"applied": applied, "n": n, "affordable": affordable,
            "src_final": src_balance}


# --------------------------------------------------------------------------
# Obligation 5 — invariant fuzz
# --------------------------------------------------------------------------

FUZZ_ACCOUNTS = ["fz-a", "fz-b", "fz-c", "fz-d"]


def scenario_fuzz(backend, *, seed=1234, steps=150):
    """Randomized operation sequence; I1/I2 asserted after EVERY operation.

    The sequence mixes successful transfers, replays of earlier keys, same-key
    different-body conflicts, and invalid requests. Whatever the outcome of an
    operation, the ledger's invariants must hold immediately afterwards.
    """
    rng = random.Random(seed)
    _setup_wallets(backend, {a: 500 for a in FUZZ_ACCOUNTS})
    seen = {}   # key -> (from, to, amount, transfer_id)

    with backend.read_conn() as check:
        inv.assert_all(check)

    outcomes = {"applied": 0, "replayed": 0, "rejected": 0, "invalid": 0}

    for step in range(steps):
        kind = rng.choices(
            ["transfer", "transfer", "replay", "conflict", "invalid_amount",
             "unknown_account", "same_account"],
            k=1,
        )[0]

        try:
            if kind == "replay" and seen:
                key, (f, t, amt, tid) = rng.choice(list(seen.items()))
                res = transfer(backend, key=key, from_id=f, to_id=t, amount=amt)
                if res.transfer_id != tid:
                    raise AssertionError(
                        f"I5: replay of {key!r} returned transfer_id "
                        f"{res.transfer_id!r}, expected {tid!r}"
                    )
                if res.status != "replayed":
                    raise AssertionError(
                        f"I5: replay of {key!r} reported status {res.status!r}"
                    )
                outcomes["replayed"] += 1

            elif kind == "conflict" and seen:
                key, (f, t, amt, tid) = rng.choice(list(seen.items()))
                try:
                    transfer(backend, key=key, from_id=f, to_id=t, amount=amt + 1)
                except Exception:
                    outcomes["rejected"] += 1
                else:
                    raise AssertionError(
                        f"same key {key!r} with a different body was applied instead "
                        f"of conflicting"
                    )

            elif kind == "same_account":
                a = rng.choice(FUZZ_ACCOUNTS)
                try:
                    transfer(backend, key=f"fz-{step}", from_id=a, to_id=a, amount=1)
                except Exception:
                    outcomes["invalid"] += 1
                else:
                    raise AssertionError("same-account transfer was applied")

            elif kind == "unknown_account":
                try:
                    transfer(backend, key=f"fz-{step}", from_id="nope-xyz",
                             to_id=rng.choice(FUZZ_ACCOUNTS), amount=1)
                except Exception:
                    outcomes["invalid"] += 1
                else:
                    raise AssertionError("transfer from an unknown account was applied")

            elif kind == "invalid_amount":
                bad = rng.choice([0, -1, -500, True, False])
                try:
                    transfer(backend, key=f"fz-{step}",
                             from_id=rng.choice(FUZZ_ACCOUNTS),
                             to_id=rng.choice(FUZZ_ACCOUNTS), amount=bad)
                except Exception:
                    outcomes["invalid"] += 1
                else:
                    raise AssertionError(f"invalid amount {bad!r} was applied")

            else:  # a plain transfer
                f, t = rng.sample(FUZZ_ACCOUNTS, 2)
                amt = rng.choice([1, 2, 3, 5, 10, 50, 200, 1000])
                key = f"fz-{step}"
                try:
                    res = transfer(backend, key=key, from_id=f, to_id=t, amount=amt)
                except Exception:
                    outcomes["rejected"] += 1   # e.g. insufficient funds
                else:
                    outcomes["applied"] += 1
                    seen[key] = (f, t, amt, res.transfer_id)

        except AssertionError:
            raise
        except Exception:
            # A rejected operation is fine; the invariants below are the point.
            outcomes["rejected"] += 1

        with backend.read_conn() as check:
            inv.assert_i1(check)   # I1 after EVERY operation
            inv.assert_i2(check)   # I2 after EVERY operation
            inv.assert_i3(check)
            inv.assert_i4(check)

    return {"seed": seed, "steps": steps, **outcomes}
