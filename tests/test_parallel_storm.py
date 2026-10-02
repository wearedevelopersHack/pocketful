"""Obligation 1 — parallel transfer storm over overlapping account pairs.

Genuinely concurrent: every transfer runs on its own SQLite connection, on its
own thread, all released from a shared barrier. A sequential loop would not find
the defect this is here to find.
"""

import unittest

from tests import scenarios
from tests.base import RealLedgerCase


class ParallelTransferStorm(RealLedgerCase):

    def test_parallel_transfer_storm(self):
        summary = scenarios.scenario_parallel_storm(self.backend)
        self.assertEqual(
            summary["hot_successes"], summary["hot_expected"],
            f"the hot account accepted {summary['hot_successes']} concurrent debits "
            f"but only {summary['hot_expected']} were funded",
        )
        self.assertEqual(summary["hot_final"], 0,
                         "the hot account must land on exactly 0")

    def test_parallel_transfer_storm_repeated(self):
        """Run the storm several times on fresh databases.

        A concurrency defect is not always visible in one interleaving; repeating
        the storm is how a flaky-but-real bug is given a chance to show itself.
        """
        for run in range(3):
            with self.subTest(run=run):
                backend = self.new_backend()
                summary = scenarios.scenario_parallel_storm(backend)
                self.assertEqual(summary["hot_successes"], summary["hot_expected"])
                self.assertEqual(summary["hot_final"], 0)

    def test_concurrent_overdraft_race_admits_only_what_is_affordable(self):
        """20 barrier-released debits of a payer holding exactly 10 of them.

        The companion mutation fixture (`tests/mutants.py::CheckThenActLedger`)
        reads the payer balance outside the write transaction and turns this RED.
        Against the real ledger it must be GREEN — exactly 10 accepted, src on 0.
        """
        summary = scenarios.scenario_overdraft_race(self.backend)
        self.assertEqual(summary["applied"], summary["affordable"])
        self.assertEqual(summary["src_final"], 0)


if __name__ == "__main__":
    unittest.main()
