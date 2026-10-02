"""Obligation 5 — invariant fuzz.

A randomized operation sequence; I1 and I2 are asserted after **every single
operation**, successful or rejected. The seed is fixed per run and printed, so a
failure is reproducible.
"""

import unittest

from tests import scenarios
from tests.base import RealLedgerCase

SEEDS = (1234, 7, 20261002, 99)


class InvariantFuzz(RealLedgerCase):

    def test_fuzz_asserts_invariants_after_every_operation(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                backend = self.new_backend()
                summary = scenarios.scenario_fuzz(backend, seed=seed, steps=150)
                self.assertGreater(
                    summary["applied"] + summary["replayed"], 0,
                    f"seed {seed} performed no successful operations; the fuzz is "
                    f"not exercising the ledger",
                )


if __name__ == "__main__":
    unittest.main()
