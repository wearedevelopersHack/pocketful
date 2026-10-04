"""§C2.5 — the activity read carries the counterparty's owner.

``app/design.py:551`` already renders a counterparty as
``counterparty_label or counterparty_owner_id``, falling back to a truncated
address. That field has to come from the ledger: the API has no owner to join
in, and a client-side id-to-name table would be a second source of truth about
identity.

The rule this file pins, beyond "the field is there":

* the owner is an identity, not a display label — ``counterparty_account_id``
  is still the full address and is never replaced by it, so a name can never
  hide who was actually paid;
* the owner is resolved in the SAME read as the entry (no N+1, no second
  lookup the caller could get wrong);
* a grant's counterparty is the system account, so the welcome credit is
  labelled rather than nameless.
"""

import unittest

from tests import invariants as inv
from tests.base import RealLedgerCase


def _core():
    import ledger.core as lcore
    return lcore


class ActivityCounterpartyOwner(RealLedgerCase):

    def _open(self, account_id, **kwargs):
        with self.backend.fresh() as (_conn, ledger):
            return ledger.open_account(
                account_id=account_id,
                owner_id=f"owner-{account_id}",
                currency=kwargs.pop("currency", "USD"),
                **kwargs,
            )

    def _activity(self, account_id, limit=50):
        with self.backend.fresh() as (_conn, ledger):
            return ledger.list_activity(account_id, limit=limit)

    def test_a_grant_is_labelled_with_the_system_accounts_owner(self):
        """The welcome credit is the one row a new user is guaranteed to see;
        it must name its counterparty, not just show an opaque id."""
        lcore = _core()
        self._open("cp-granted", opening_grant_minor=lcore.OPENING_GRANT_MINOR)

        items = self._activity("cp-granted")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.direction, "credit")
        self.assertEqual(item.amount_minor, lcore.OPENING_GRANT_MINOR)
        self.assertEqual(item.counterparty_account_id, lcore.SYSTEM_ACCOUNT_ID)
        self.assertEqual(item.counterparty_owner_id, lcore.SYSTEM_ACCOUNT_OWNER_ID)
        self.assertTrue(item.counterparty_owner_id)

    def test_a_transfer_names_the_counterparty_on_both_sides(self):
        """The payer's row and the payee's row describe the SAME counterparty
        differently — each sees the other — and each keeps the full address
        beside the name."""
        self._open("cp-a", opening_grant_minor=_core().OPENING_GRANT_MINOR)
        self._open("cp-b")
        with self.backend.fresh() as (_conn, ledger):
            ledger.transfer(
                idempotency_key="cp-key",
                request_fingerprint="fp",
                from_account_id="cp-a",
                to_account_id="cp-b",
                amount_minor=250,
                currency="USD",
            )

        sent = self._activity("cp-a")[0]
        self.assertEqual(sent.direction, "debit")
        self.assertEqual(sent.amount_minor, 250)
        self.assertEqual(sent.counterparty_account_id, "cp-b")
        self.assertEqual(sent.counterparty_owner_id, "owner-cp-b")

        received = self._activity("cp-b")[0]
        self.assertEqual(received.direction, "credit")
        self.assertEqual(received.amount_minor, 250)
        self.assertEqual(received.counterparty_account_id, "cp-a")
        self.assertEqual(received.counterparty_owner_id, "owner-cp-a")

        # The name sits beside the address, never instead of it.
        for item, address in ((sent, "cp-b"), (received, "cp-a")):
            self.assertEqual(item.counterparty_account_id, address,
                             "the address must survive alongside the owner name")
            self.assertNotEqual(item.counterparty_owner_id, address,
                                "the owner is a distinct identity from the account id")

    def test_every_activity_row_carries_a_non_empty_owner(self):
        """No row may hand the caller an empty name it would render as blank:
        the join is total because every transfer references real accounts."""
        lcore = _core()
        self._open("cp-src", opening_grant_minor=lcore.OPENING_GRANT_MINOR)
        self._open("cp-dst")
        with self.backend.fresh() as (_conn, ledger):
            for i in range(3):
                ledger.transfer(
                    idempotency_key=f"cp-many-{i}",
                    request_fingerprint="fp",
                    from_account_id="cp-src",
                    to_account_id="cp-dst",
                    amount_minor=10,
                    currency="USD",
                )

        for account_id in ("cp-src", "cp-dst"):
            for item in self._activity(account_id):
                self.assertIsInstance(item.counterparty_owner_id, str)
                self.assertTrue(item.counterparty_owner_id.strip(),
                                f"empty owner on {account_id} {item.entry_id}")

        with self.backend.read_conn() as conn:
            inv.assert_all(conn)


if __name__ == "__main__":
    unittest.main()
