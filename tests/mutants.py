"""Deliberately broken §2.1 implementations, used to prove the suite can fail.

The briefing is explicit: *a test that cannot fail is not a test*. These mutants
are the "failing case" for each scenario. `test_harness_selfcheck.py` runs each
scenario against its mutant and asserts the scenario **detects** the defect — and
also asserts the mutants pass the scenarios they are *not* broken for, so a
detection cannot be a false positive from a scenario that is simply always red.

They are test scaffolding, not production code and not an alternative ledger:
nothing in the gate imports them except the self-check, and no application is
ever pointed at one.

Each mutant is correct everywhere except its one named defect, so a self-check
failure is attributable to that defect and not to a mutant being incoherent.
Idempotency state lives in the database (not in a per-instance dict) because the
harness builds a fresh object per connection, exactly as the real API would.
"""

import json
import sqlite3
import time
import uuid
from types import SimpleNamespace

RETRIES = 60

# Mirrors ledger.types.MAX_MINOR. Defined locally so this file stays free of
# imports from the package under test.
MAX_MINOR = 2 ** 53 - 1


class MutantError(Exception):
    pass


class MutantInsufficientFunds(MutantError):
    pass


class MutantInvalidAmount(MutantError):
    pass


class ControlLedger:
    """A deliberately *correct* implementation, used as the control.

    Its whole job is to prove each scenario **can** pass. Without a control, a
    scenario that is always red for an unrelated reason would look like a
    detection. It implements enough of §2.1 to satisfy the five scenarios —
    begin-immediate, in-transaction balance read, balanced pair, key stored in
    the same transaction — and nothing more. It is test scaffolding: never
    shipped, never imported by an application, and not a substitute for T1.
    """

    script = "control"

    def __init__(self, conn):
        self.conn = conn

    # -- plumbing ---------------------------------------------------------
    def _exec(self, sql, params=()):
        last = None
        for attempt in range(RETRIES):
            try:
                return self.conn.execute(sql, params)
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) or "busy" in str(exc):
                    last = exc
                    time.sleep(0.005 * (attempt + 1))
                    continue
                raise
        raise last

    # -- §2.1 surface -----------------------------------------------------
    def open_account(self, *, account_id, owner_id, currency, allow_overdraft=False):
        self._exec(
            "INSERT INTO accounts (account_id, owner_id, currency, allow_overdraft,"
            " version, created_at) VALUES (?,?,?,?,0,?)",
            (account_id, owner_id, currency, 1 if allow_overdraft else 0, "now"),
        )
        self.conn.commit()
        return SimpleNamespace(
            account_id=account_id, owner_id=owner_id, currency=currency,
            allow_overdraft=allow_overdraft, created_at="now",
        )

    def get_balance(self, account_id):
        row = self._exec(
            "SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries"
            " WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return row[0]

    def _account(self, account_id):
        return self._exec(
            "SELECT account_id, currency, allow_overdraft FROM accounts"
            " WHERE account_id = ?",
            (account_id,),
        ).fetchone()

    def _validate(self, amount_minor):
        # `type(...) is not int` and not merely isinstance: a bool IS an int in
        # Python, and accepting True as 1 minor unit is the never-coerce defect.
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise MutantInvalidAmount("amount must be a plain int")
        if amount_minor <= 0:
            raise MutantInvalidAmount("amount must be positive")
        if amount_minor > MAX_MINOR:
            raise MutantInvalidAmount("amount is out of range")

    # -- idempotency store (database-backed, as the real one must be) ------
    def _idem_get(self, key, fingerprint):
        row = self._exec(
            "SELECT request_fingerprint, transfer_id, response_json FROM"
            " idempotency_keys WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise MutantError("idempotency conflict")
        return json.loads(row[2])

    def _idem_put(self, key, fingerprint, result):
        self._exec(
            "INSERT INTO idempotency_keys (key, request_fingerprint, transfer_id,"
            " response_json, created_at) VALUES (?,?,?,?,?)",
            (key, fingerprint, result.transfer_id, json.dumps(result.__dict__), "now"),
        )

    def _write_pair(self, transfer_id, from_id, to_id, amount, currency):
        self._exec(
            "INSERT INTO ledger_entries (entry_id, transfer_id, account_id,"
            " amount_minor, created_at) VALUES (?,?,?,?,?)",
            (f"{transfer_id}-d", transfer_id, from_id, -amount, "now"),
        )
        self._exec(
            "INSERT INTO ledger_entries (entry_id, transfer_id, account_id,"
            " amount_minor, created_at) VALUES (?,?,?,?,?)",
            (f"{transfer_id}-c", transfer_id, to_id, amount, "now"),
        )
        self._exec(
            "INSERT INTO transfers (transfer_id, from_account_id, to_account_id,"
            " amount_minor, currency, created_at) VALUES (?,?,?,?,?,?)",
            (transfer_id, from_id, to_id, amount, currency, "now"),
        )

    def _result(self, transfer_id, status, from_id, to_id, amount, currency):
        return SimpleNamespace(
            transfer_id=transfer_id, status=status, from_account_id=from_id,
            to_account_id=to_id, amount_minor=amount, currency=currency,
            created_at="now",
        )

    def _new_transfer_id(self):
        return str(uuid.uuid4())

    def transfer(self, *, idempotency_key, request_fingerprint, from_account_id,
                 to_account_id, amount_minor, currency):
        """A correct-enough transfer: balanced pair, key stored in the same
        transaction as the entries."""
        self._validate(amount_minor)
        self._exec("BEGIN IMMEDIATE")
        try:
            stored = self._idem_get(idempotency_key, request_fingerprint)
            if stored is not None:
                self._exec("COMMIT")
                return self._result(
                    stored["transfer_id"], "replayed", from_account_id,
                    to_account_id, amount_minor, currency,
                )
            payer = self._account(from_account_id)
            payee = self._account(to_account_id)
            if payer is None or payee is None:
                self._exec("ROLLBACK")
                raise MutantError("unknown account")
            if payer[1] != currency or payee[1] != currency:
                self._exec("ROLLBACK")
                raise MutantError("currency mismatch")
            if from_account_id == to_account_id:
                self._exec("ROLLBACK")
                raise MutantError("same account transfer")
            if self.get_balance(from_account_id) - amount_minor < 0 and not payer[2]:
                self._exec("ROLLBACK")
                raise MutantInsufficientFunds("insufficient funds")
            tid = self._new_transfer_id()
            self._write_pair(tid, from_account_id, to_account_id, amount_minor, currency)
            result = self._result(tid, "applied", from_account_id, to_account_id,
                                  amount_minor, currency)
            self._idem_put(idempotency_key, request_fingerprint, result)
            self._exec("COMMIT")
            return result
        except BaseException:
            with self._suppress():
                self.conn.rollback()
            raise

    @staticmethod
    def _suppress():
        import contextlib
        return contextlib.suppress(Exception)

    @staticmethod
    def split(total, parts):
        base, extra = divmod(total, parts)
        return [base + 1 if i < extra else base for i in range(parts)]


# --------------------------------------------------------------------------
# 1. writes only half the double entry -> I1
# --------------------------------------------------------------------------

class UnbalancedLedger(ControlLedger):
    """Debits the payer and forgets the credit: value is destroyed."""

    script = "unbalanced"

    def transfer(self, *, idempotency_key, request_fingerprint, from_account_id,
                 to_account_id, amount_minor, currency):
        self._validate(amount_minor)
        self._exec("BEGIN IMMEDIATE")
        try:
            stored = self._idem_get(idempotency_key, request_fingerprint)
            if stored is not None:
                self._exec("COMMIT")
                return self._result(stored["transfer_id"], "replayed", from_account_id,
                                    to_account_id, amount_minor, currency)
            tid = self._new_transfer_id()
            # half a double entry: the credit is missing
            self._exec(
                "INSERT INTO ledger_entries (entry_id, transfer_id, account_id,"
                " amount_minor, created_at) VALUES (?,?,?,?,?)",
                (f"{tid}-d", tid, from_account_id, -amount_minor, "now"),
            )
            self._exec(
                "INSERT INTO transfers (transfer_id, from_account_id, to_account_id,"
                " amount_minor, currency, created_at) VALUES (?,?,?,?,?,?)",
                (tid, from_account_id, to_account_id, amount_minor, currency, "now"),
            )
            result = self._result(tid, "applied", from_account_id, to_account_id,
                                  amount_minor, currency)
            self._idem_put(idempotency_key, request_fingerprint, result)
            self._exec("COMMIT")
            return result
        except BaseException:
            self.conn.rollback()
            raise


# --------------------------------------------------------------------------
# 2. ignores the idempotency key -> obligation 2
# --------------------------------------------------------------------------

class NonIdempotentLedger(ControlLedger):
    """Balanced and atomic, but the key is never consulted: every retry applies."""

    script = "non-idempotent"

    def transfer(self, *, idempotency_key, request_fingerprint, from_account_id,
                 to_account_id, amount_minor, currency):
        self._validate(amount_minor)
        self._exec("BEGIN IMMEDIATE")
        try:
            tid = self._new_transfer_id()
            self._write_pair(tid, from_account_id, to_account_id, amount_minor, currency)
            self._exec("COMMIT")
        except BaseException:
            self.conn.rollback()
            raise
        return self._result(tid, "applied", from_account_id, to_account_id,
                            amount_minor, currency)


# --------------------------------------------------------------------------
# 3. commits each entry separately -> obligation 4
# --------------------------------------------------------------------------

class NonAtomicLedger(ControlLedger):
    """Correct on the happy path, but no transaction spans the double entry: a
    mid-transfer failure leaves the first half committed."""

    script = "non-atomic"

    def transfer(self, *, idempotency_key, request_fingerprint, from_account_id,
                 to_account_id, amount_minor, currency):
        self._validate(amount_minor)
        stored = self._idem_get(idempotency_key, request_fingerprint)
        if stored is not None:
            return self._result(stored["transfer_id"], "replayed", from_account_id,
                                to_account_id, amount_minor, currency)
        payer = self._account(from_account_id)
        if payer is None:
            raise MutantError("unknown account")
        if self.get_balance(from_account_id) - amount_minor < 0 and not payer[2]:
            raise MutantInsufficientFunds("insufficient funds")
        tid = self._new_transfer_id()
        # no BEGIN/COMMIT: each statement commits on its own
        self._write_pair(tid, from_account_id, to_account_id, amount_minor, currency)
        result = self._result(tid, "applied", from_account_id, to_account_id,
                              amount_minor, currency)
        self._idem_put(idempotency_key, request_fingerprint, result)
        self.conn.commit()
        return result


# --------------------------------------------------------------------------
# 4. truncating split -> I6
# --------------------------------------------------------------------------

class TruncatingSplitLedger(ControlLedger):
    """Truncates the remainder away instead of distributing it."""

    script = "truncating-split"

    @staticmethod
    def split(total, parts):
        return [total // parts] * parts


# --------------------------------------------------------------------------
# 5. check-then-act across transaction boundaries -> obligation 1
# --------------------------------------------------------------------------

class CheckThenActLedger(ControlLedger):
    """Idempotent, balanced and atomic per transaction — but the payer balance is
    read *outside* the write transaction. Concurrent debits of the same account
    all read the same pre-transfer balance and all pass the overdraft check.

    The sleep only widens a window that is real without it; the assertion is on
    the final balances, and the defect is check-then-act, not the sleep.
    """

    script = "check-then-act"
    READ_WRITE_GAP = 0.003

    def transfer(self, *, idempotency_key, request_fingerprint, from_account_id,
                 to_account_id, amount_minor, currency):
        self._validate(amount_minor)

        # THE DEFECT: read the balance that gates the write outside the write's
        # transaction, so the check can be stale by the time the write happens.
        stale_balance = self.get_balance(from_account_id)
        payer = self._account(from_account_id)
        if payer is None:
            raise MutantError("unknown account")
        time.sleep(self.READ_WRITE_GAP)

        self._exec("BEGIN IMMEDIATE")
        try:
            stored = self._idem_get(idempotency_key, request_fingerprint)
            if stored is not None:
                self._exec("COMMIT")
                return self._result(stored["transfer_id"], "replayed", from_account_id,
                                    to_account_id, amount_minor, currency)
            if stale_balance - amount_minor < 0 and not payer[2]:
                self._exec("ROLLBACK")
                raise MutantInsufficientFunds("insufficient funds")
            tid = self._new_transfer_id()
            self._write_pair(tid, from_account_id, to_account_id, amount_minor, currency)
            result = self._result(tid, "applied", from_account_id, to_account_id,
                                  amount_minor, currency)
            self._idem_put(idempotency_key, request_fingerprint, result)
            self._exec("COMMIT")
            return result
        except BaseException:
            with self._suppress():
                self.conn.rollback()
            raise


CONTROL = ControlLedger


# --------------------------------------------------------------------------
# 6. coerces the amount instead of rejecting it -> never-coerce rule
# --------------------------------------------------------------------------

class CoercingLedger(ControlLedger):
    """The classic money bug: whatever it is handed is converted to an int.

    `12.34` becomes 12 minor units, `"500"` becomes 500, and `True` becomes 1 —
    because `int()` is happy with all three. Everything else about this ledger
    is correct, so a detection is attributable to the coercion and not to a
    ledger that is broken in several ways at once.
    """

    script = "coercing"

    def transfer(self, *, idempotency_key, request_fingerprint, from_account_id,
                 to_account_id, amount_minor, currency):
        try:
            coerced = int(amount_minor)     # THE DEFECT: converts, never rejects
        except (TypeError, ValueError):
            raise MutantInvalidAmount("amount must be a plain int")
        return super().transfer(
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            amount_minor=coerced,
            currency=currency,
        )


# --------------------------------------------------------------------------
# 7. remembers a refusal under the caller's key -> silent money loss
# --------------------------------------------------------------------------

class RefusalBurnsKeyLedger(ControlLedger):
    """Correct everywhere except one thing: a refusal is remembered.

    The refusal is genuine — funds really are short and the money really does
    not move — so I1, I2 and every balance assertion still hold. But the key is
    written anyway, so the caller's next attempt with that key is answered as a
    replay of a transfer that never happened: the caller gets success and a
    transfer_id, and the money silently never moves. No invariant in the suite
    can see this. The refusal-path scenario exists to see it.
    """

    script = "refusal-burns-key"

    def transfer(self, *, idempotency_key, request_fingerprint, from_account_id,
                 to_account_id, amount_minor, currency):
        try:
            return super().transfer(
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                amount_minor=amount_minor,
                currency=currency,
            )
        except MutantInsufficientFunds:
            # THE DEFECT: remember the refusal under the caller's key anyway.
            # The transfer never happened, but the key now says it did.
            self._idem_put(
                idempotency_key, request_fingerprint,
                self._result("never-happened", "applied", from_account_id,
                             to_account_id, amount_minor, currency),
            )
            self.conn.commit()
            raise


# --------------------------------------------------------------------------
# 8. applies a refused request, then refuses it -> the untouched check
# --------------------------------------------------------------------------

class RejectAfterWriteLedger(ControlLedger):
    """Applies the transfer and *then* refuses it.

    The refusal looks correct: the caller sees `MutantInvalidAmount` and no
    result. By then, though, the double entry is committed — so a *rejected*
    request has moved money. The pair is balanced, which is the point: I1 and I2
    both hold, and every invariant in the suite agrees the books are fine. The
    only thing that can see this is the amount matrix's second assertion, that a
    rejection leaves the ledger untouched — which is why that assertion has a
    mutant of its own rather than riding on `NonAtomicLedger`.

    (`NonAtomicLedger` validates *before* it writes, so it passes the matrix: a
    bad amount never reaches its write path. Verified by drive, not assumed.)
    """

    script = "reject-after-write"

    def transfer(self, *, idempotency_key, request_fingerprint, from_account_id,
                 to_account_id, amount_minor, currency):
        try:
            self._validate(amount_minor)
        except MutantInvalidAmount:
            # THE DEFECT: the refused request is applied anyway, in its own
            # committed transaction, before the refusal is raised. The pair is
            # balanced, so no invariant will ever complain.
            tid = self._new_transfer_id()
            self._write_pair(tid, from_account_id, to_account_id, 1, currency)
            self.conn.commit()
            raise
        return super().transfer(
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            amount_minor=amount_minor,
            currency=currency,
        )


# --------------------------------------------------------------------------
# 9. burns the key on a refused *amount* -> the matrix's key assertion
# --------------------------------------------------------------------------

class BurnsKeyOnBadAmountLedger(ControlLedger):
    """Claims the idempotency key before it decides the amount is legal.

    The same defect class as `RefusalBurnsKeyLedger`, reached from the other
    refusal. There the request is refused for funds, inside the transaction;
    here it is refused for its amount, and the key is recorded anyway — so the
    caller's corrected retry replays a transfer that never happened.

    This is the mutant for the amount matrix's own key assertion. That assertion
    is not vacuous, but nothing else in the suite kills it: `RefusalBurnsKeyLedger`
    is driven by `scenario_refusal_path` and a bad amount never reaches its path,
    so it passes the matrix. A mutant is a possible implementation, not a branch
    this ledger can currently reach — the same reason `UnbalancedLedger` and
    `NonIdempotentLedger` ship although the real ledger has neither defect.

    **The current control flow cannot reach this defect.** `ledger/core.py`
    raises from `_require_amount` before `db.run_immediate` opens a transaction,
    so a refused amount never reaches anything that could claim a key. The change
    that would make it live is moving `_require_amount` inside `db.run_immediate`
    (or into `work()`), so validation runs after the transaction opens. Nothing
    here says the ledger has this bug today; it says the gate would see it if
    someone introduced it.
    """

    script = "burns-key-on-bad-amount"

    def transfer(self, *, idempotency_key, request_fingerprint, from_account_id,
                 to_account_id, amount_minor, currency):
        try:
            self._validate(amount_minor)
        except MutantInvalidAmount:
            # THE DEFECT: the key is claimed even though nothing was applied.
            self._idem_put(
                idempotency_key, request_fingerprint,
                self._result("never-happened", "applied", from_account_id,
                             to_account_id, 1, currency),
            )
            self.conn.commit()
            raise
        return super().transfer(
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            amount_minor=amount_minor,
            currency=currency,
        )


# --------------------------------------------------------------------------
# 10. validates the amount's shape, not its type -> the 1.0 value
# --------------------------------------------------------------------------

class ShapeCheckLedger(ControlLedger):
    """Checks that the amount *looks* like a whole number instead of being one.

    The test is `int(v) == v`, which catches `12.34` and is therefore correct on
    every value a hand-written list is likely to carry. It accepts `1.0`,
    because `int(1.0) == 1.0` is True, and an integrally-valued float is then
    applied as amount 1.

    The bool trap is guarded explicitly here, which is deliberate: without that
    guard `True` would also slip through, and the point of this mutant is to
    isolate `1.0` as the *single* value separating a type check from a shape
    check. With the guard in place the claim is testable — the mutant is RED on
    the full matrix and PASSES the moment `1.0` is removed — and
    `test_one_point_oh_is_the_only_value_that_separates_the_two` asserts exactly
    that, both directions.

    **The current control flow cannot reach this defect.** `ledger/core.py`
    `_require_amount` rejects a non-int by type, so `1.0` never reaches a write.
    The change that would make it live is dropping `type(value) is not int` in
    favour of a `v == int(v)` shape check. Nothing here says the ledger has this
    bug today; it says the gate would see it if someone introduced it.
    """

    script = "shape-check"

    def _validate(self, amount_minor):
        if isinstance(amount_minor, bool):
            raise MutantInvalidAmount("amount must be a plain int")
        try:
            # THE DEFECT: a shape test, not a type test. `int(1.0) == 1.0`.
            if int(amount_minor) != amount_minor:
                raise MutantInvalidAmount("amount must be a whole number")
        except (TypeError, ValueError):
            raise MutantInvalidAmount("amount must be a plain int")
        if amount_minor <= 0:
            raise MutantInvalidAmount("amount must be positive")
        if amount_minor > MAX_MINOR:
            raise MutantInvalidAmount("amount is out of range")


MUTANTS = {
    "control": ControlLedger,
    "unbalanced": UnbalancedLedger,
    "non-idempotent": NonIdempotentLedger,
    "non-atomic": NonAtomicLedger,
    "truncating-split": TruncatingSplitLedger,
    "check-then-act": CheckThenActLedger,
    "coercing": CoercingLedger,
    "refusal-burns-key": RefusalBurnsKeyLedger,
    "reject-after-write": RejectAfterWriteLedger,
    "burns-key-on-bad-amount": BurnsKeyOnBadAmountLedger,
    "shape-check": ShapeCheckLedger,
}
