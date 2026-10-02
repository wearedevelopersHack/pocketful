"""Obligation 3 — rounding sweep.

`split(total, n)` over many (total, n) pairs, including prime totals and n=1.
I6 must hold every single time: the parts sum exactly to the total, and no cent
is created or destroyed by the remainder policy.
"""

import unittest

from tests import scenarios
from tests.base import RealLedgerCase
from tests.harness import MAX_MINOR, load_ledger


class RoundingSweep(RealLedgerCase):

    def test_split_sweep(self):
        Ledger, _db, _schema = load_ledger()
        summary = scenarios.scenario_rounding_sweep(Ledger.split)
        self.assertGreater(summary["pairs_checked"], 500,
                           "the sweep must actually cover many (total, n) pairs")

    def test_single_part_returns_the_total(self):
        Ledger, _db, _schema = load_ledger()
        for total in (0, 1, -1, 7, 999, MAX_MINOR, -MAX_MINOR):
            with self.subTest(total=total):
                self.assertEqual(Ledger.split(total, 1), [total])

    def test_prime_totals_are_never_truncated(self):
        Ledger, _db, _schema = load_ledger()
        for total in scenarios.PRIME_TOTALS:
            for parts in (2, 3, 7, 13):
                with self.subTest(total=total, parts=parts):
                    parts_out = Ledger.split(total, parts)
                    self.assertEqual(sum(parts_out), total)
                    self.assertLessEqual(max(parts_out) - min(parts_out), 1)


if __name__ == "__main__":
    unittest.main()
