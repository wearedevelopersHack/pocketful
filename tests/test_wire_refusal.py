"""The wire half of the refusal semantic, with a mutant behind it.

`api/selfcheck.py` asserts a money-critical property at the HTTP boundary: a
transfer refused for insufficient funds leaves its idempotency key unclaimed, so
the same key retried with a corrected body applies rather than conflicts. Those
assertions have been green since they landed — and by the rule this room settled,
green is not evidence until something has been shown to turn them red.

This file supplies the something, without editing `api/`. It drives the real
`api.selfcheck.run` against a real server over a real database, and swaps in one
ledger that refuses correctly and then claims the key anyway.

Why the base is the real `ledger.core.Ledger` and not `ControlLedger`: the api
checks exercise the whole surface — accounts, balances, transfers, activity —
and a partial ledger would fail many rows at once, so a red would say nothing
about which assertion fired. Overriding only `transfer`'s refusal path keeps the
mutant behaviour-preserving everywhere *outside* that path, and
`test_the_mutant_only_disturbs_rows_downstream_of_a_refusal` asserts that rather
than assuming it.

Boundary note: this is the only file in `tests/` that imports `api/`. The rest of
the gate stops at the `Ledger` surface by design; this one exists precisely
because the wire mapping is a different surface with its own assertions, and
assertions need a mutant like any other.
"""

import json
import os
import shutil
import tempfile
import threading
import unittest

#: Enough of the real label to identify the refused-retry row, without pinning
#: the whole sentence. The tests assert exactly one row matches.
ROW_LABEL = "retried with a CORRECTED affordable body"

#: The first refusal in `run()`. Every row before it is unreachable by a defect
#: that only acts on refusals, which is what the attribution test leans on.
FIRST_REFUSAL_LABEL = "insufficient funds -> 422, never 200"


def _import_wire():
    """Import `api/` and `ledger/` lazily, so a broken import fails as a
    readable test failure rather than a collection error for the whole suite."""
    import api.app as api_app
    import api.selfcheck as api_selfcheck
    import ledger.core as ledger_core
    import ledger.types as ledger_types
    return api_app, api_selfcheck, ledger_core, ledger_types


def _mutant_class():
    """The real ledger, refusing correctly and then claiming the key anyway.

    Everything but the refusal path is `super()`, so the mutant differs from the
    real ledger in exactly one behaviour.

    **The current control flow cannot reach this defect.** `ledger/core.py`
    records the key only on the applied path, inside the same transaction as the
    entries. The change that would make it live is claiming the key before the
    funds check, or writing it in its own committed statement after the rollback
    — which is what this mutant does. Nothing here says the ledger has this bug
    today; it says the wire assertions would see it if someone introduced it.
    """
    _, _, ledger_core, ledger_types = _import_wire()

    class RefusalRecordsKey(ledger_core.Ledger):

        def transfer(self, *, idempotency_key, request_fingerprint,
                     from_account_id, to_account_id, amount_minor, currency):
            try:
                return super().transfer(
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    from_account_id=from_account_id,
                    to_account_id=to_account_id,
                    amount_minor=amount_minor,
                    currency=currency,
                )
            except ledger_types.InsufficientFunds:
                self._claim_the_key(idempotency_key, request_fingerprint,
                                    from_account_id, to_account_id, amount_minor,
                                    currency)
                raise

        def _claim_the_key(self, key, fingerprint, from_id, to_id, amount, currency):
            # THE DEFECT: the refusal is remembered. The money never moves, every
            # invariant still holds, and the caller's corrected retry is answered
            # as a replay of a transfer that never happened.
            #
            # `idempotency_keys.transfer_id` is NOT NULL with a foreign key to
            # `transfers`, so the row has to borrow a real id — a refusal has no
            # transfer of its own to point at. That is the shape a real
            # implementation of this bug would have, too: it would be reusing the
            # only transfer_id in scope.
            row = self._conn.execute(
                "SELECT transfer_id FROM transfers LIMIT 1").fetchone()
            if row is None:
                raise
            borrowed = row[0]
            response = {
                "transfer_id": borrowed,
                "from_account_id": from_id,
                "to_account_id": to_id,
                "amount_minor": amount,
                "currency": currency,
                "created_at": "never",
            }
            self._conn.execute(
                "INSERT INTO idempotency_keys "
                "(key, request_fingerprint, transfer_id, response_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, fingerprint, borrowed,
                 json.dumps(response, separators=(",", ":"), sort_keys=True),
                 "never"),
            )
            self._conn.commit()

    return RefusalRecordsKey


def _drive(ledger_cls=None):
    """Run the real `api.selfcheck.run` against a real server.

    `ledger_cls` is swapped in through the pool's per-request resolution, so no
    edit to `api/` is needed: `PocketfulHandler` asks `self.server.pool.ledger()`
    for a ledger on every request, and that is the seam.
    """
    api_app, api_selfcheck, _, _ = _import_wire()
    workdir = tempfile.mkdtemp(prefix="pocketful-wire-")
    db_path = os.path.join(workdir, "wire.db")
    server = api_app.create_server(db_path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    original = api_app.LedgerPool.ledger
    try:
        api_selfcheck._wait_until_ready("127.0.0.1", port)
        if ledger_cls is not None:
            api_app.LedgerPool.ledger = lambda self: ledger_cls(self.connection())
        api_selfcheck.RESULTS.clear()
        api_selfcheck.run(api_selfcheck.Client("127.0.0.1", port), db_path)
        return list(api_selfcheck.RESULTS)
    finally:
        api_app.LedgerPool.ledger = original
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)


class WireRefusedRetry(unittest.TestCase):

    def _row(self, results):
        matches = [(ok, label) for ok, label in results if ROW_LABEL in label]
        self.assertEqual(
            len(matches), 1,
            f"expected exactly one row matching {ROW_LABEL!r}, found "
            f"{len(matches)}; the label in api/selfcheck.py may have moved and "
            f"this file has to move with it",
        )
        return matches[0][0]

    def test_the_control_run_is_entirely_green(self):
        """The server, the driver and the scenario all work: every row passes
        against the real ledger. Without this, a red under the mutant could be
        the harness rather than the defect."""
        results = _drive()
        failures = [label for ok, label in results if not ok]
        self.assertEqual(failures, [],
                         f"the real ledger must pass every api self-check row; "
                         f"{len(failures)} failed: {failures[:3]}")

    def test_the_real_ledger_passes_the_refused_retry_row(self):
        self.assertTrue(self._row(_drive()),
                        "the refused-retry row must pass against the real ledger")

    def test_a_ledger_that_records_the_refusal_flips_that_row(self):
        """The acceptance criterion, and the whole point of this file: the
        refused-retry row **specifically** goes PASS -> FAIL.

        `run()` is one long scenario, so "the run went red" proves nothing about
        which assertion fired — the rows before this one fail identically if the
        harness is broken. Naming the row is what makes the red attributable to
        the assertion rather than to the run.
        """
        results = _drive(_mutant_class())
        self.assertTrue(self._row(results) is False,
                        "a ledger that records a refusal under the caller's key "
                        "must fail the refused-retry row; it is the only row that "
                        "asserts a corrected retry applies")

    def test_the_mutant_only_disturbs_rows_downstream_of_a_refusal(self):
        """The mutant differs from the real ledger in one behaviour — refusals
        are remembered — so it can only move a row that runs *after* a refusal.
        Every row before the first one must be identical to the control run.

        This is what stops the flip above from being a mutant that is simply
        broken: a harness fault would scatter failures through the early rows.

        It is deliberately not "only the refused-retry row flips". A faithful
        mutant flips every refusal-observing row, and there is more than one:
        `run()` contains an earlier insufficient-funds refusal, so the global
        `idempotency_keys` count downstream of it moves too. That is the defect
        working, not the mutant misbehaving — and it is exactly why "the run
        went red" was never evidence and the row had to be named.
        """
        control = _drive()
        mutated = _drive(_mutant_class())
        self.assertEqual(
            [label for _, label in control], [label for _, label in mutated],
            "the mutant run must be the same row set as the control run",
        )

        first_refusal = next(
            (i for i, (_, label) in enumerate(control)
             if label.startswith(FIRST_REFUSAL_LABEL)),
            None,
        )
        self.assertIsNotNone(
            first_refusal,
            f"no row starts with {FIRST_REFUSAL_LABEL!r}; the label in "
            f"api/selfcheck.py may have moved and this file has to move with it",
        )

        before = [(c, m) for c, m in zip(control[:first_refusal],
                                         mutated[:first_refusal])]
        self.assertTrue(before, "expected rows before the first refusal")
        disturbed = [c_label for (c_ok, c_label), (m_ok, _) in before if c_ok != m_ok]
        self.assertEqual(
            disturbed, [],
            f"the mutant must be behaviour-preserving on every row that runs "
            f"before the first refusal; {len(disturbed)} earlier row(s) moved: "
            f"{disturbed[:3]}",
        )

        flipped = [label for (c_ok, _), (m_ok, label) in zip(control, mutated)
                   if c_ok and not m_ok]
        self.assertTrue(flipped, "the mutant must flip something")
        self.assertTrue(self._row(mutated) is False,
                        "the refused-retry row must be among the flipped rows")


if __name__ == "__main__":
    unittest.main()
