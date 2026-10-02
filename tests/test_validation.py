"""Amount validation: reject, never coerce.

Lifted from ledger-engineer's T1 proof script section 5, with the assertions
kept exactly as strict. The rule being pinned is that an amount is a plain
integer count of minor units and nothing else is quietly converted into one:
no float rounded, no numeric string parsed, no bool accepted on the grounds
that `isinstance(True, int)` is True.

The matrix itself lives in `scenarios.scenario_amount_validation` so the mutant
self-check can point the identical assertions at a deliberately coercing ledger
(`mutants.CoercingLedger`) and prove they turn red. The tests here run it
against the real ledger and add the contract surface around it.
"""

import unittest

from tests import invariants as inv
from tests import scenarios
from tests.base import RealLedgerCase
from tests.harness import MAX_MINOR


def _ledger_types():
    """`ledger.types`, imported lazily so a missing ledger fails as a test
    failure with a readable message rather than an import error at collection."""
    import ledger.types as ltypes
    return ltypes


class AmountValidation(RealLedgerCase):

    def _funded_pair(self):
        """Two ordinary accounts plus the overdraft mint, funded through the
        §2.1 surface so nothing here depends on private ledger internals."""
        scenarios._setup_wallets(self.backend, {"val-a": 10_000, "val-b": 0})

    # -- the never-coerce matrix -----------------------------------------
    def test_non_integer_amounts_are_rejected_not_coerced(self):
        """A float, an integrally-valued float, a numeric string, both bools,
        zero, a negative, an out-of-range value, None and a list are all
        rejected — and each rejection leaves the ledger untouched, including
        the request's key."""
        summary = scenarios.scenario_amount_validation(self.backend)
        self.assertEqual(summary["checked"], len(scenarios.VALIDATION_BAD_AMOUNTS))

    def test_max_minor_is_the_boundary_and_it_is_inclusive(self):
        """±MAX_MINOR is legal; one minor unit past it is not. A validator that
        rejects MAX_MINOR itself, or accepts MAX_MINOR + 1, is off by one at the
        only boundary that matters to a JS client."""
        ltypes = _ledger_types()
        self._funded_pair()

        applied = scenarios.transfer(
            self.backend, key="max-ok", from_id=scenarios.MINT, to_id="val-b",
            amount=MAX_MINOR,
        )
        self.assertEqual(applied.status, "applied",
                         f"MAX_MINOR ({MAX_MINOR}) is inside the contract bound and "
                         f"must be a legal amount")

        with self.assertRaises(ltypes.InvalidAmount):
            scenarios.transfer(
                self.backend, key="max-bad", from_id=scenarios.MINT, to_id="val-b",
                amount=MAX_MINOR + 1,
            )

        with self.backend.read_conn() as conn:
            inv.assert_all(conn)
            inv.assert_i4(conn)

    # -- the rest of the frozen §2.1 surface -----------------------------
    def test_get_account_returns_the_account_and_rejects_an_unknown_id(self):
        """`get_account(account_id) -> Account` is part of the frozen read
        surface; an unknown id must raise rather than return a blank account."""
        ltypes = _ledger_types()
        self._funded_pair()
        with self.backend.fresh() as (_conn, ledger):
            account = ledger.get_account("val-a")
            self.assertEqual(account.account_id, "val-a")
            self.assertEqual(account.currency, "USD")
            self.assertFalse(account.allow_overdraft)
            with self.assertRaises(ltypes.UnknownAccount):
                ledger.get_account("does-not-exist")

    def test_same_account_transfer_is_rejected(self):
        ltypes = _ledger_types()
        self._funded_pair()
        with self.assertRaises(ltypes.SameAccountTransfer):
            scenarios.transfer(
                self.backend, key="same-acct", from_id="val-a", to_id="val-a",
                amount=1,
            )
        with self.backend.read_conn() as conn:
            inv.assert_all(conn)

    def test_unknown_account_is_rejected(self):
        ltypes = _ledger_types()
        self._funded_pair()
        with self.assertRaises(ltypes.UnknownAccount):
            scenarios.transfer(
                self.backend, key="unknown", from_id="does-not-exist",
                to_id="val-b", amount=1,
            )
        with self.backend.read_conn() as conn:
            inv.assert_all(conn)

    def test_duplicate_open_account_is_rejected_not_idempotent(self):
        """Opening an account twice is a caller mistake, not a retry: it must be
        refused rather than silently returning the existing account."""
        ltypes = _ledger_types()
        scenarios.open_account(self.backend, "val-dup")
        with self.assertRaises(ltypes.AccountExists):
            scenarios.open_account(self.backend, "val-dup")


if __name__ == "__main__":
    unittest.main()
