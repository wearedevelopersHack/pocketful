"""Obligation 2 — idempotent retry storm.

One key, fired 100 times concurrently. The operation must apply exactly once and
every caller must receive the same `transfer_id`. Sequential retries prove
nothing here: the interesting failure is two writers both believing they are the
first to see the key.
"""

import unittest

from tests import invariants as inv
from tests import scenarios
from tests.base import RealLedgerCase

PAYER, PAYEE = "acct-payer", "acct-payee"
KEY, AMOUNT = "one-key-fired-100x", 10


def _transfer(ledger, *, from_id=PAYER, to_id=PAYEE, amount=AMOUNT, key=KEY):
    return ledger.transfer(
        idempotency_key=key,
        request_fingerprint=scenarios.fingerprint(
            from_id, to_id, amount, scenarios.CURRENCY
        ),
        from_account_id=from_id,
        to_account_id=to_id,
        amount_minor=amount,
        currency=scenarios.CURRENCY,
    )


class IdempotentRetryStorm(RealLedgerCase):

    def test_one_key_fired_100x_concurrently(self):
        summary = scenarios.scenario_retry_storm(self.backend)
        self.assertEqual(summary["applied"], 1,
                         "exactly one call must report status='applied'")
        self.assertEqual(summary["replayed"], summary["calls"] - 1,
                         "every other call must report status='replayed'")
        self.assertEqual(summary["distinct_transfer_ids"], 1,
                         "every caller must receive the same transfer_id")

    def test_replay_after_the_storm_is_a_noop(self):
        """I5: once the storm has settled, one more replay changes nothing."""
        scenarios.scenario_retry_storm(self.backend)

        with self.backend.fresh() as (conn, ledger):
            original = _transfer(ledger)
            self.assertEqual(original.status, "replayed")
            inv.assert_i5_replay_noop(
                conn,
                lambda: _transfer(ledger),
                expected_transfer_id=original.transfer_id,
            )
            inv.assert_all(conn)

    def test_same_key_different_body_conflicts(self):
        """Same key, different body: rejected, never applied."""
        payer, payee = "acct-conflict-a", "acct-conflict-b"
        scenarios.open_account(self.backend, scenarios.MINT, allow_overdraft=True)
        scenarios.open_account(self.backend, payer)
        scenarios.open_account(self.backend, payee)
        scenarios.fund(self.backend, payer, 1000)

        first = scenarios.transfer(self.backend, key="k-conflict", from_id=payer,
                                   to_id=payee, amount=10)
        self.assertEqual(first.status, "applied")

        with self.backend.read_conn() as conn:
            snap = (inv.entry_count(conn), inv.transfer_row_count(conn),
                    inv.idempotency_row_count(conn))

        with self.assertRaises(Exception,
                               msg="same key + different body must be rejected"):
            scenarios.transfer(self.backend, key="k-conflict", from_id=payer,
                               to_id=payee, amount=11)

        with self.backend.read_conn() as conn:
            self.assertEqual(
                (inv.entry_count(conn), inv.transfer_row_count(conn),
                 inv.idempotency_row_count(conn)),
                snap,
                "the conflicting body must not have been applied",
            )
            inv.assert_all(conn)

    def test_a_refused_transfer_does_not_burn_its_key(self):
        """The refusal that matters is the one raised *inside* the transaction.

        A bad amount is refused before a transaction exists, so it never had a
        key to lose. An insufficient-funds refusal is different: the key has
        already been claimed when the shortfall is discovered, and only the
        rollback gives it back. If it does not, the money never moves, every
        invariant still holds, and the caller's retry is answered as a success
        for a transfer that never happened.
        """
        summary = scenarios.scenario_refusal_path(self.backend)
        self.assertEqual(summary["refusal"], "InsufficientFunds",
                         "the seeded refusal must be an ordinary funds refusal, "
                         "not some other error that happens to raise first")
        self.assertEqual(summary["applied"], "applied",
                         "a fresh key must apply")
        self.assertEqual(summary["replayed"], "replayed",
                         "the same key and body must replay, not reapply")


if __name__ == "__main__":
    unittest.main()
