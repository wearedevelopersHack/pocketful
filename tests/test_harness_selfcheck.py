"""Prove the gate can fail.

Every scenario in this suite is run against a deliberately broken implementation
of §2.1 (`tests/mutants.py`) and must **detect** it. Each broken implementation
is also run against a *correct* control, so a detection cannot be a false
positive from a scenario that is red no matter what.

This file is the evidence for the briefing rule: *a test that cannot fail is not
a test*. It runs without the real ledger, so it is green from the start and
stays green as a standing check that the harness itself has not rotted.
"""

import os
import tempfile
import unittest
import uuid

from tests import invariants as inv
from tests import mutants, scenarios
from tests.harness import MutantBackend


class HarnessSelfCheck(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def backend(self, ledger_cls):
        path = os.path.join(self._tmp.name, f"mutant-{uuid.uuid4().hex}.db")
        return MutantBackend(path, ledger_cls).prepare()

    # -- detections -------------------------------------------------------
    def assert_detects(self, scenario, ledger_cls, expect, **kwargs):
        backend = self.backend(ledger_cls)
        with self.assertRaises(inv.InvariantViolation) as ctx:
            scenario(backend, **kwargs)
        self.assertIn(
            ctx.exception.invariant, expect,
            f"{scenario.__name__} detected {ledger_cls.__name__} as "
            f"{ctx.exception.invariant!r}, expected one of {expect!r}:\n{ctx.exception}",
        )

    def test_storm_detects_check_then_act(self):
        """The parallel storm must catch a payer-balance read outside the write."""
        self.assert_detects(scenarios.scenario_parallel_storm,
                            mutants.CheckThenActLedger,
                            expect=("O1/parallel-storm", "I4", "I1", "I2"))

    def test_storm_detects_unbalanced_writes(self):
        """The parallel storm must catch value being destroyed."""
        self.assert_detects(scenarios.scenario_parallel_storm,
                            mutants.UnbalancedLedger,
                            expect=("O1/parallel-storm", "I1", "I2"))

    def test_retry_storm_detects_a_missing_idempotency_key(self):
        """The retry storm must catch a ledger that reapplies on every retry."""
        self.assert_detects(scenarios.scenario_retry_storm,
                            mutants.NonIdempotentLedger,
                            expect=("O2/retry-storm", "I1", "I2"))

    def test_atomicity_detects_a_committed_half_transfer(self):
        """The atomicity scenario must catch a mid-transfer failure that leaves
        the first half of the double entry committed."""
        self.assert_detects(scenarios.scenario_failure_atomicity,
                            mutants.NonAtomicLedger,
                            expect=("O4/atomicity", "I1", "I2"))

    def test_rounding_detects_a_truncating_split(self):
        """The rounding sweep must catch a split that drops the remainder."""
        with self.assertRaises(inv.InvariantViolation) as ctx:
            scenarios.scenario_rounding_sweep(mutants.TruncatingSplitLedger.split)
        self.assertEqual(ctx.exception.invariant, "I6")

    def test_validation_detects_a_coercing_ledger(self):
        """The never-coerce matrix must catch a validator that converts instead
        of rejecting: `12.34` -> 12 minor units, `"500"` -> 500, `True` -> 1."""
        self.assert_detects(scenarios.scenario_amount_validation,
                            mutants.CoercingLedger,
                            expect=("V/amount-validation",))

    def test_validation_detects_a_rejection_that_wrote_first(self):
        """The matrix's *second* assertion — a rejection leaves the ledger
        untouched — must have a mutant of its own.

        `assertRaises` alone passes a ledger that applies the transfer and then
        refuses it. So does every invariant: the mutant's pair is balanced, I1
        and I2 hold, and the books look clean. Only the untouched check sees
        that a refused request moved money. The control for this same scenario
        already exists (`test_control_passes_the_amount_validation_matrix`), so
        the detection is attributable and not a scenario that is always red.
        """
        self.assert_detects(scenarios.scenario_amount_validation,
                            mutants.RejectAfterWriteLedger,
                            expect=("V/amount-validation",))

    def test_validation_detects_a_key_burned_on_a_refused_amount(self):
        """The matrix's *first* assertion — a rejected request records no key —
        must have a mutant too, and it is not the same one as the refusal path.

        `RefusalBurnsKeyLedger` fires on a funds refusal raised inside the
        transaction; a bad amount never reaches that path, so it passes this
        matrix. The clause is general — a rejected request must not record its
        key — and this is its amount-path instance.
        """
        self.assert_detects(scenarios.scenario_amount_validation,
                            mutants.BurnsKeyOnBadAmountLedger,
                            expect=("V/amount-validation",))

    def test_validation_detects_a_shape_checker(self):
        """A validator written `v != int(v)` -> reject catches `12.34` and lets
        `1.0` through. It must die on the full matrix."""
        self.assert_detects(scenarios.scenario_amount_validation,
                            mutants.ShapeCheckLedger,
                            expect=("V/amount-validation",))

    def test_one_point_oh_is_the_only_value_that_separates_the_two(self):
        """`1.0` is load-bearing in the matrix, and that is a checkable claim.

        Removing it must turn the shape-checker green: every other value in the
        matrix is caught by a shape check as readily as by a type check, so the
        shape-checker would survive without it. If this ever stops holding — if
        some other value starts discriminating — the matrix note is wrong and
        this test says so, rather than `1.0` sitting there looking load-bearing
        with nothing exercising it.
        """
        without_one = tuple(
            v for v in scenarios.VALIDATION_BAD_AMOUNTS
            if not (type(v) is float and v == 1.0)
        )
        self.assertEqual(len(without_one),
                         len(scenarios.VALIDATION_BAD_AMOUNTS) - 1,
                         "the filter must remove exactly one value")

        # Green without it...
        summary = scenarios.scenario_amount_validation(
            self.backend(mutants.ShapeCheckLedger), amounts=without_one)
        self.assertEqual(summary["checked"], len(without_one))

        # ...red with it, and on that value specifically.
        backend = self.backend(mutants.ShapeCheckLedger)
        with self.assertRaises(inv.InvariantViolation) as ctx:
            scenarios.scenario_amount_validation(backend)
        self.assertIn("amount 1.0 was accepted and applied", str(ctx.exception),
                      "the full matrix must die on 1.0 and say so")

    def test_refusal_path_detects_a_burned_key(self):
        """The refusal-path scenario must catch a ledger that remembers a
        refusal under the caller's key.

        This is the defect no invariant can see: the money never moves, so I1
        and I2 hold, and the caller is still told the transfer succeeded. Only
        the retry reveals it — the same key, a different body, and the work that
        should still be doable."""
        self.assert_detects(scenarios.scenario_refusal_path,
                            mutants.RefusalBurnsKeyLedger,
                            expect=("R/refusal-path",))

    # -- obligation 6: the permanent check-then-act mutation fixture ------
    def test_mutation_check_then_act_variant_turns_the_race_red(self):
        """The permanent mutation fixture: a ledger that reads the payer balance
        outside the write transaction, barrier-released so every reader sees the
        same stale balance, must turn the overdraft race RED.

        This is what makes a green gate mean something. If a refactor ever lets
        this variant pass, the gate has lost its teeth and this test says so.

        The assertion is on **over-application**, not on the exact count the
        variant reaches. An earlier version asserted `applied == RACE_N`, which
        encoded a racing quantity as deterministic and flaked 1 run in 40 — the
        gate then failed for a reason that had nothing to do with the ledger.

        Detection has never been observed to miss, but it is not guaranteed: the
        mutant's stale read is unsynchronized, so a run in which enough readers
        read after the first commit yields exactly `affordable` applies and this
        test fails. That is the safe direction — a loud red, not a silent pass —
        and the count is scheduler-dependent, so pinning it was the defect.
        """
        backend = self.backend(mutants.CheckThenActLedger)
        with self.assertRaises(inv.InvariantViolation) as ctx:
            scenarios.scenario_overdraft_race(backend)

        self.assertIn(ctx.exception.invariant, ("O1/overdraft-race", "I4", "I1", "I2"))

        affordable = scenarios.RACE_SEED // scenarios.RACE_AMOUNT
        with backend.read_conn() as conn:
            applied = conn.execute(
                "SELECT COUNT(*) FROM transfers WHERE from_account_id = ?",
                (scenarios.RACE_SRC,),
            ).fetchone()[0]
            src = inv.balances(conn).get(scenarios.RACE_SRC)

            self.assertGreater(
                applied, affordable,
                f"the broken variant applied {applied} of {scenarios.RACE_N} "
                f"concurrent debits of a payer that could afford {affordable}; a "
                f"ledger reading the balance outside the write transaction must "
                f"over-apply",
            )
            # Exact arithmetic, not a racing quantity: whatever it applied, the
            # balance must be exactly that.
            self.assertEqual(
                src, scenarios.RACE_SEED - applied * scenarios.RACE_AMOUNT,
                "src balance does not match the number of transfers applied",
            )

            # The defect is real, not just a failed assertion: src is overdrawn
            # and I4 — no negative balance without an overdraft policy — is
            # violated.
            self.assertLess(src, 0, "the broken variant must drive src negative")
            with self.assertRaises(inv.InvariantViolation) as i4:
                inv.assert_i4(conn)
            self.assertEqual(i4.exception.invariant, "I4")

    def test_control_passes_the_overdraft_race(self):
        """...and the same scenario passes a correct implementation, so the
        detection above is not a scenario that is simply always red."""
        summary = scenarios.scenario_overdraft_race(self.backend(mutants.CONTROL))
        self.assertEqual(summary["applied"], summary["affordable"])
        self.assertEqual(summary["src_final"], 0)

    # -- controls: the scenarios are satisfiable, not always-red ----------
    def test_control_passes_the_parallel_storm(self):
        scenarios.scenario_parallel_storm(self.backend(mutants.CONTROL))

    def test_control_passes_the_retry_storm(self):
        scenarios.scenario_retry_storm(self.backend(mutants.CONTROL))

    def test_control_passes_failure_atomicity(self):
        scenarios.scenario_failure_atomicity(self.backend(mutants.CONTROL))

    def test_control_passes_the_rounding_sweep(self):
        scenarios.scenario_rounding_sweep(mutants.CONTROL.split)

    def test_control_passes_the_amount_validation_matrix(self):
        scenarios.scenario_amount_validation(self.backend(mutants.CONTROL))

    def test_control_passes_the_refusal_path(self):
        summary = scenarios.scenario_refusal_path(self.backend(mutants.CONTROL))
        self.assertEqual(summary["applied"], "applied")
        self.assertEqual(summary["replayed"], "replayed")

    def test_control_passes_the_fuzz(self):
        scenarios.scenario_fuzz(self.backend(mutants.CONTROL), seed=1234, steps=60)

    def test_truncating_split_still_passes_exact_divisions(self):
        """I6 detects the real defect, not 'anything unusual': a truncating split
        that happens to divide exactly is genuinely correct and must pass."""
        inv.assert_i6(mutants.TruncatingSplitLedger.split, 10, 5)
        inv.assert_i6(mutants.TruncatingSplitLedger.split, 9, 3)


if __name__ == "__main__":
    unittest.main()
