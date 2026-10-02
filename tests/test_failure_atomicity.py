"""Obligation 4 — failure atomicity.

A failure is injected in the middle of a transfer by the test (a SQLite trigger,
not an edit to production code). Afterwards no partial entries may survive, the
balances must be exactly what they were, and — crucially — the rolled-back
attempt must not have left an idempotency row behind, or the retry would be
treated as already-applied and the money would vanish.
"""

import unittest

from tests import invariants as inv
from tests import scenarios
from tests.base import RealLedgerCase


class FailureAtomicity(RealLedgerCase):

    def test_mid_transfer_failure_leaves_nothing_behind(self):
        summary = scenarios.scenario_failure_atomicity(self.backend)
        self.assertIn("raised", summary)
        self.assertEqual(summary["retry_status"], "applied",
                         "the retried transfer must apply, not replay")

    def test_ledger_is_not_wedged_by_a_rolled_back_failure(self):
        """After the injected failure is rolled back and the trigger removed, the
        ledger must still be usable and consistent — a rollback that leaves the
        transaction open is its own defect."""
        scenarios.scenario_failure_atomicity(self.backend, keep_trigger=True)

        with self.backend.read_conn() as conn:
            conn.execute(f"DROP TRIGGER IF EXISTS {scenarios.INJECT_TRIGGER}")
            conn.commit()

        result = scenarios.transfer(
            self.backend, key="after-failure", from_id="acct-atomic-a",
            to_id="acct-atomic-b", amount=100,
        )
        self.assertEqual(result.status, "applied")

        with self.backend.read_conn() as conn:
            self.assertEqual(inv.balances(conn)["acct-atomic-b"], 100)
            inv.assert_all(conn)
            inv.assert_i3(conn)
            inv.assert_i4(conn)


if __name__ == "__main__":
    unittest.main()
