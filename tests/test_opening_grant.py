"""C1 — the opening grant (the $100 welcome credit).

Authority: ``plan.md`` §C1.

This is money, so it is asserted the way money is: through the real ledger,
against I1/I2/I3/I4, with the refusal paths included rather than assumed. Every
assertion here is written to FAIL before the C1 primitive exists — the keyword
``opening_grant_minor`` does not exist, and the ``__system__`` guards do not
exist — and to pass after.

The grant is a *transfer*, so "a single +10000 entry" is not among the legal
shapes this file can accidentally accept: the entry-count and net-sum
assertions below reject it.
"""

import unittest
from unittest import mock

from tests import invariants as inv
from tests.base import RealLedgerCase


def _core():
    """``ledger.core``, imported lazily so a missing primitive is a test
    FAILURE with a readable message, not a collection error that reds the
    whole module."""
    import ledger.core as lcore
    return lcore


def _types():
    import ledger.types as ltypes
    return ltypes


class OpeningGrant(RealLedgerCase):

    def _open(self, account_id, **kwargs):
        with self.backend.fresh() as (_conn, ledger):
            return ledger.open_account(
                account_id=account_id,
                owner_id=f"owner-{account_id}",
                currency=kwargs.pop("currency", "USD"),
                **kwargs,
            )

    def _balance(self, account_id):
        with self.backend.fresh() as (_conn, ledger):
            return ledger.get_balance(account_id)

    def _accounts_present(self, ids):
        with self.backend.read_conn() as conn:
            rows = conn.execute(
                "SELECT account_id FROM accounts WHERE account_id IN "
                "(" + ",".join("?" * len(ids)) + ")",
                tuple(ids),
            ).fetchall()
            return sorted(r[0] for r in rows)

    # -- the positive path -------------------------------------------------

    def test_grant_is_a_balanced_transfer_and_the_new_balance_is_100(self):
        """C1.1/C1.3: the credit is 10000 minor units, posted as ONE transfer
        of exactly TWO nonzero entries summing to zero. The new account reads
        +10000, the system account reads -10000, and I1/I2/I3/I4 hold."""
        lcore = _core()
        self.assertEqual(lcore.OPENING_GRANT_MINOR, 10_000,
                         "C1.1: the grant is 10000 minor units, defined once")
        sys_id = lcore.SYSTEM_ACCOUNT_ID

        self._open("grant-one", opening_grant_minor=lcore.OPENING_GRANT_MINOR)

        self.assertEqual(self._balance("grant-one"), 10_000)
        self.assertEqual(self._balance(sys_id), -10_000,
                         "the system account carries the negative leg")

        with self.backend.read_conn() as conn:
            inv.assert_all(conn)                       # I1 + I2
            inv.assert_i4(conn)
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(amount_minor), 0) AS net "
                "  FROM ledger_entries "
                " WHERE transfer_id IN (SELECT transfer_id FROM transfers "
                "                        WHERE from_account_id = ?)",
                (sys_id,),
            ).fetchone()
            self.assertEqual(row["n"], 2,
                             "C1.3: the grant is two entries, never one")
            self.assertEqual(row["net"], 0,
                             "C1.3: the two grant entries sum to zero")
            transfer_id = conn.execute(
                "SELECT transfer_id FROM transfers WHERE from_account_id = ?",
                (sys_id,),
            ).fetchone()[0]
            inv.assert_i3(conn, transfer_id)

    def test_system_account_is_a_real_usd_overdraft_row(self):
        """C1.2: it is a real row in ``accounts``, USD, overdraft on. Without
        overdraft the very first grant would raise InsufficientFunds."""
        lcore = _core()
        self._open("grant-two", opening_grant_minor=lcore.OPENING_GRANT_MINOR)
        account = None
        with self.backend.fresh() as (_conn, ledger):
            account = ledger.get_account(lcore.SYSTEM_ACCOUNT_ID)
        self.assertEqual(account.currency, "USD")
        self.assertTrue(account.allow_overdraft)

    def test_granting_twice_accumulates_a_closed_system(self):
        """I1/I2 must hold after ANY number of grants, not just one. The two
        sides are asserted separately: the system's negative IS the grants
        accumulated (``-10000 * N``), and each new account holds exactly its own
        grant — so a build that debited the system once and credited N accounts,
        or that credited the system too, is ruled out by the pair and not just by
        the sum, which would still be 0."""
        lcore = _core()
        n = 5
        for i in range(n):
            self._open(f"grant-many-{i}", opening_grant_minor=lcore.OPENING_GRANT_MINOR)
        with self.backend.read_conn() as conn:
            inv.assert_all(conn)
            inv.assert_i4(conn)
        self.assertEqual(self._balance(lcore.SYSTEM_ACCOUNT_ID),
                         -lcore.OPENING_GRANT_MINOR * n,
                         "the system account carries one negative leg per grant")
        for i in range(n):
            self.assertEqual(self._balance(f"grant-many-{i}"),
                             lcore.OPENING_GRANT_MINOR,
                             f"account {i} holds its own grant and nothing else")

    def test_a_non_usd_account_opens_at_zero_and_the_system_stays_untouched(self):
        """INVARIANTS §1: the system account is single-currency, so a non-USD
        account is simply opened with no grant. The refusal path is the one with
        a grant attached (asserted below); without one, the open must succeed at
        balance 0 and must not conjure the system account into the database."""
        lcore = _core()
        self._open("grant-eur-plain", currency="EUR")
        self.assertEqual(self._balance("grant-eur-plain"), 0,
                         "no grant was asked for, so the balance is 0")
        self.assertEqual(self._accounts_present([lcore.SYSTEM_ACCOUNT_ID]), [],
                         "a non-USD open without a grant must not create the "
                         "system account")
        with self.backend.read_conn() as conn:
            inv.assert_all(conn)
            inv.assert_i4(conn)

    # -- C1.4 atomicity ----------------------------------------------------

    def test_a_failing_grant_leaves_no_account_and_no_entries(self):
        """C1.4: the account row and its grant are ONE unit of state. A failure
        after the account INSERT must roll the whole transaction back — no
        half-created account that can never be granted (create is not
        replayable)."""
        lcore = _core()
        with mock.patch.object(lcore, "_new_id",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._open("grant-atomic", opening_grant_minor=lcore.OPENING_GRANT_MINOR)

        present = self._accounts_present(["grant-atomic", lcore.SYSTEM_ACCOUNT_ID])
        self.assertEqual(present, [],
                         "the failed grant must leave neither the account nor "
                         "the lazily-created system account behind")
        with self.backend.read_conn() as conn:
            inv.assert_all(conn)

    # -- C1.5 exactly once -------------------------------------------------

    def test_duplicate_open_is_refused_and_grants_exactly_once(self):
        """C1.5: the accounts PRIMARY KEY is the exactly-once mechanism. A
        duplicate raises AccountExists BEFORE any write in that transaction, so
        the second attempt adds no account, no entries and no second $100."""
        lcore = _core()
        self._open("grant-dup", opening_grant_minor=lcore.OPENING_GRANT_MINOR)

        with self.assertRaises(_types().AccountExists):
            self._open("grant-dup", opening_grant_minor=lcore.OPENING_GRANT_MINOR)

        self.assertEqual(self._balance("grant-dup"), 10_000,
                         "a refused duplicate must not grant a second time")
        self.assertEqual(self._balance(lcore.SYSTEM_ACCOUNT_ID), -10_000)
        with self.backend.read_conn() as conn:
            inv.assert_all(conn)

    # -- C1.6 opt-in default ----------------------------------------------

    def test_no_grant_by_default_and_no_system_account_appears(self):
        """C1.6: ``opening_grant_minor`` defaults to 0, so every existing
        caller and fixture keeps its exact current behaviour — including that
        no system account is conjured into the database."""
        lcore = _core()
        self._open("grant-none")
        self.assertEqual(self._balance("grant-none"), 0)
        self.assertEqual(self._accounts_present([lcore.SYSTEM_ACCOUNT_ID]), [],
                         "a non-granting open must not create the system account")
        with self.backend.read_conn() as conn:
            inv.assert_all(conn)

    # -- C1.7 the reserved id ---------------------------------------------

    def test_the_system_id_cannot_be_opened_through_the_public_path(self):
        """C1.7: the system account can never be created or re-created by a
        caller, grant or no grant."""
        lcore = _core()
        with self.assertRaises(_types().LedgerError):
            self._open(lcore.SYSTEM_ACCOUNT_ID)
        with self.assertRaises(_types().LedgerError):
            self._open(lcore.SYSTEM_ACCOUNT_ID,
                       opening_grant_minor=lcore.OPENING_GRANT_MINOR)
        self.assertEqual(self._accounts_present([lcore.SYSTEM_ACCOUNT_ID]), [])

    # -- C1.8 no client mint path -----------------------------------------

    def test_no_transfer_may_touch_the_system_account(self):
        """C1.8: the system account carries overdraft, so a transfer from it
        would mint unbounded value. The opening grant is the only way out."""
        lcore = _core()
        self._open("grant-c1", opening_grant_minor=lcore.OPENING_GRANT_MINOR)
        ltypes = _types()
        sys_id = lcore.SYSTEM_ACCOUNT_ID

        for from_id, to_id in ((sys_id, "grant-c1"), ("grant-c1", sys_id)):
            with self.assertRaises(ltypes.LedgerError):
                with self.backend.fresh() as (_conn, ledger):
                    ledger.transfer(
                        idempotency_key=f"mint-{from_id}-{to_id}",
                        request_fingerprint="fp",
                        from_account_id=from_id,
                        to_account_id=to_id,
                        amount_minor=1,
                        currency="USD",
                    )

        self.assertEqual(self._balance("grant-c1"), 10_000,
                         "a refused mint must move nothing")
        self.assertEqual(self._balance(sys_id), -10_000)
        with self.backend.read_conn() as conn:
            inv.assert_all(conn)

    # -- representation and currency (INVARIANTS §1) ----------------------

    def test_a_grant_must_be_an_integer_and_may_not_cross_currencies(self):
        """INVARIANTS §1: never a float, and no cross-currency arithmetic
        without a recorded rate. A USD system account cannot fund a EUR
        account, and none of these refusals may leave a row behind."""
        lcore = _core()
        ltypes = _types()

        for bad in (100.0, "10000", True, -1):
            with self.assertRaises(ltypes.LedgerError):
                self._open("grant-bad", opening_grant_minor=bad)

        with self.assertRaises(ltypes.LedgerError):
            self._open("grant-eur", currency="EUR",
                       opening_grant_minor=lcore.OPENING_GRANT_MINOR)

        self.assertEqual(self._accounts_present(["grant-bad", "grant-eur"]), [])
        with self.backend.read_conn() as conn:
            inv.assert_all(conn)

    # -- the falsifier: I2 must reject the shape this path must never write -----

    def test_i2_rejects_a_single_entry_grant_posted_through_the_real_path(self):
        """Permanent in-gate falsifier for I1/I2 over the grant.

        Every row above reads the real ``_post_opening_grant``, and several of
        them would go red if it ever credited a single entry — but only because
        they *name* the shape (the entry count, the net sum of the pair). That is
        evidence about those rows, not about the invariant. "I1/I2 are asserted
        after every grant" has to mean the assertions themselves catch the defect.

        So the defect is injected into the one method that moves grant money: a
        single credit, no transfer row and no negative leg. ``assert_i2`` must
        reject it, naming I2 — inside the gate, not in a scratch run. The control
        immediately before it runs the identical call with the real method and
        must be closed, or a red run here would say nothing about the mutant.
        """
        lcore = _core()
        real_post = lcore.Ledger._post_opening_grant   # captured, never re-read
        self.assertTrue(callable(real_post),
                        "the seam this falsifier patches must exist; patching a "
                        "name that is not there would make the row inert")

        def single_entry(self, account_id, amount, created_at):
            """MUTANT: the transfer row is written, but of its two entries only
            the credit is posted — the system's negative leg is missing, so value
            is created out of nothing. (The transfers row is required: the schema
            has a foreign key from ``ledger_entries.transfer_id``, and a mutant
            that cannot write its entry at all would be measuring the schema.)"""
            transfer_id = lcore._new_id()
            self._conn.execute(
                "INSERT INTO transfers (transfer_id, from_account_id, to_account_id,"
                " amount_minor, currency, created_at) VALUES (?,?,?,?,?,?)",
                (transfer_id, lcore.SYSTEM_ACCOUNT_ID, account_id, amount, "USD",
                 created_at),
            )
            self._conn.execute(
                "INSERT INTO ledger_entries (transfer_id, account_id, amount_minor,"
                " created_at) VALUES (?,?,?,?)",
                (transfer_id, account_id, amount, created_at),
            )
            return transfer_id

        # Control: the unpatched path leaves the system closed.
        self._open("grant-falsifier-control",
                   opening_grant_minor=lcore.OPENING_GRANT_MINOR)
        with self.backend.read_conn() as conn:
            inv.assert_i2(conn)
            system_leg_before = conn.execute(
                "SELECT COUNT(*) FROM ledger_entries WHERE account_id = ?",
                (lcore.SYSTEM_ACCOUNT_ID,)).fetchone()[0]
            entries_before = inv.entry_count(conn)

        with mock.patch.object(lcore.Ledger, "_post_opening_grant", single_entry):
            self._open("grant-falsifier",
                       opening_grant_minor=lcore.OPENING_GRANT_MINOR)

        with self.backend.read_conn() as conn:
            # The mutant's premise, asserted rather than assumed: a single credit
            # really was written, and the system leg really was not. A row that
            # went red because the patch was inert, or raised, would otherwise
            # read as a detection. Both are DELTAS around the mutant's own open —
            # the control above legitimately wrote a balanced pair of its own.
            entries = tuple(conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_minor), 0) FROM ledger_entries"
                " WHERE account_id = ?", ("grant-falsifier",)).fetchone())
            self.assertEqual(entries, (1, lcore.OPENING_GRANT_MINOR),
                             "the mutant must have posted exactly one credit; "
                             f"got {entries}")
            self.assertEqual(inv.entry_count(conn), entries_before + 1,
                             "the mutant's open must write ONE entry where the "
                             "real path writes two")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ledger_entries WHERE account_id = ?",
                             (lcore.SYSTEM_ACCOUNT_ID,)).fetchone()[0],
                system_leg_before,
                "the single-entry grant must have left the system leg unwritten, "
                "or it is not the shape under test")

            with self.assertRaises(inv.InvariantViolation) as caught:
                inv.assert_i2(conn)
            self.assertEqual(caught.exception.invariant, "I2",
                             "the violation must name I2, not some neighbouring "
                             f"check; it named {caught.exception.invariant!r}")
            with self.assertRaises(inv.InvariantViolation):
                inv.assert_all(conn)


if __name__ == "__main__":
    unittest.main()
