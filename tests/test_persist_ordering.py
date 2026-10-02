"""T15 at the gate: the persist-before-attempt row must be able to fail.

The row under test is `app/selfcheck.py`'s temporal check:

    check(transport.store_at_send == [(key, True)],
          "[WEB] the record was already durable when the request left: "
          "persisted BEFORE the attempt", ...)

Its point is that **cardinality is not durability**. A build that put the key on
the wire first and wrote the store afterwards leaves exactly one record, with
the right key, the right amount and the right state — so every count-shaped
assertion around it stays green. The only thing that separates the two builds is
*when* the record reached the disk, and the only place that can be observed is
inside the transport, at the instant the request leaves: reading the store after
the POST returns cannot tell the two apart, because by then both have written.

That row shipped with its teeth in a comment and a scratch run. A claim whose
witness cannot fail is the shape this room has removed four times now, so this
file is its permanent falsifier: a store that defers its write past the attempt,
and a transport that lands it on the far side — nothing else changed.

Why this is test-only. The row reads the store through ``transport.store_at_send``,
so the ordering is falsifiable from the test side: the store is a subclass whose
``begin`` suppresses the disk write, and the transport subclass flushes it after
the HTTP call returns. Both are monkeypatches of module globals for the duration
of one ``scenario_web_ui`` call — the same technique ``test_wire_refusal.py``
uses on ``api.app.LedgerPool.ledger``. No production file gains a seam; no file
outside ``tests/`` is touched.

Two tests, because a mutant with no control proves nothing: the control runs the
same scenario unpatched and must be entirely green, and the mutant must fail
**exactly one** row — this one, named. Attribution is the whole point; "the run
went red" was never evidence.
"""

import contextlib
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import api.app as api_app

from app.keystore import PendingStore as RealPendingStore
import app.selfcheck as app_selfcheck
import app.web as app_web

PAYER = "acct-erin"
PAYEE = "acct-frank"
FUND = 10000

ROW = "persisted BEFORE the attempt"


@contextlib.contextmanager
def _running_api():
    """A real `api/` server over a real on-disk ledger, on an ephemeral port."""
    workdir = tempfile.mkdtemp(prefix="pocketful-t15-api-")
    db_path = os.path.join(workdir, "t15.db")
    server = api_app.create_server(db_path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        _wait_until_answering(port)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)


def _wait_until_answering(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/accounts/nobody/balance", timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"api server on port {port} never answered")


class DeferredSaveStore(RealPendingStore):
    """MUTANT: ``begin`` mints the key in memory and writes the disk later.

    ``PendingStore.begin`` mints and persists before returning — that ordering is
    the durability rule, and it is invisible to any assertion taken afterwards.
    This subclass suppresses the write the mint performs; the record exists in
    this process from the first instant, so the send path, the retry path and
    every in-process read behave normally. Only the store *file* is behind.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self._defer = False

    def begin(self, **kwargs):
        self._defer = True
        try:
            return super().begin(**kwargs)
        finally:
            self._defer = False

    def _save(self) -> None:
        if self._defer:
            return                      # written on the far side of the attempt
        super()._save()

    def flush(self) -> None:
        """The write that ``begin`` owed, paid once the attempt has been made."""
        with self._lock:
            super()._save()


class SendFirstRig:
    """Holds the one mutated store and knows which path it belongs to.

    The scenario builds three UI servers over three store files; only the first
    carries the ordering under test, so the deferral is keyed on that file's path
    and the other two stores are the real class, untouched.
    """

    def __init__(self, target_path: str) -> None:
        self.target = os.path.abspath(target_path)
        self._store = None
        self.sends: list[tuple[str, bool]] = []

    def store_for(self, path: str) -> RealPendingStore:
        """Stands in for ``app.web.PendingStore``."""
        if os.path.abspath(path) == self.target:
            if self._store is None:
                self._store = DeferredSaveStore(path)
            return self._store
        return RealPendingStore(path)

    def observe(self, store_at_send) -> None:
        """What the transport saw on the wire-side of the first send."""
        if not self.sends and store_at_send:
            self.sends = [(key, durable) for key, durable in store_at_send]

    def flush(self) -> None:
        if self._store is not None:
            self._store.flush()


class SendFirstTransport(app_selfcheck._LostResponseTransport):
    """MUTANT TRANSPORT: production behaviour, with the persist moved late.

    ``super().__call__`` records ``store_at_send`` by reading the store FILE at
    the instant of the send, does the real HTTP, and then raises to model the
    lost response. The only change here is the ``finally``: by the time it runs,
    the request has already left and been answered, so the write lands *after*
    the attempt — exactly the ordering the row exists to catch, and the one a
    post-hoc read of the store cannot see.
    """

    rig: SendFirstRig | None = None

    def __call__(self, method, url, body, headers):
        try:
            return super().__call__(method, url, body, headers)
        finally:
            rig = type(self).rig
            if (rig is not None and self.store_path is not None
                    and os.path.abspath(self.store_path) == rig.target):
                rig.observe(self.store_at_send)
                rig.flush()


class PersistBeforeTheAttempt(unittest.TestCase):

    def setUp(self):
        self._workdir = tempfile.mkdtemp(prefix="pocketful-t15-")
        self.addCleanup(shutil.rmtree, self._workdir, True)
        self.addCleanup(setattr, SendFirstTransport, "rig", None)

    def _scenario(self, base_url):
        app_selfcheck.scenario_web_ui(base_url, self._workdir, PAYER, PAYEE)

    def _run(self, base_url):
        before = len(app_selfcheck.RESULTS)
        self._scenario(base_url)
        return app_selfcheck.RESULTS[before:]

    # -- the control ----------------------------------------------------------

    def test_the_scenario_is_green_before_anything_is_mutated(self):
        """Without this, a red run under the mutant would say nothing: the
        scenario could be red for a reason that has nothing to do with ordering.

        The control reads ``scenario_web_ui`` end to end — the same rows the
        mutant run will be measured against — on the same bytes, unpatched.
        """
        with _running_api() as base_url:
            self._fund(base_url)
            rows = self._run(base_url)

        failures = [label for ok, label in rows if not ok]
        self.assertEqual(failures, [],
                         "the unmutated scenario must be entirely green, or the "
                         f"mutant run proves nothing; failures={failures}")
        self.assertTrue(any(ROW in label for _, label in rows),
                        "the row under test must actually have run in the control")

    # -- the falsifier --------------------------------------------------------

    def test_the_row_fires_when_the_write_lands_after_the_attempt(self):
        """T15. The store defers its write, the transport pays it after the HTTP
        call. The wire is unchanged, the key is unchanged, the record count is
        unchanged — and the row must fail, naming the ordering.

        The attribution assertion is not "the run went red": it is that
        **exactly one** row flips, and it is this one. If the deferral leaked
        into any neighbouring row, the mutant would be measuring something other
        than the ordering and the count would say so.
        """
        with _running_api() as base_url:
            self._fund(base_url)
            rig = SendFirstRig(os.path.join(self._workdir, "ui-store.json"))
            SendFirstTransport.rig = rig
            with mock.patch.object(app_web, "PendingStore", rig.store_for), \
                 mock.patch.object(app_selfcheck, "_LostResponseTransport",
                                   SendFirstTransport):
                rows = self._run(base_url)

        failures = [label for ok, label in rows if not ok]
        self.assertEqual(len(failures), 1,
                         "the deferral must move exactly the ordering row; "
                         f"failures={failures}")
        self.assertIn(ROW, failures[0],
                      "and the row that fails must be the temporal one, naming "
                      f"what it observed; it was {failures[0]!r}")

        # The mutant's premise, asserted rather than assumed: the store file did
        # not hold the key at the instant the request left. Without this, a row
        # failing for an unrelated reason would read as a detection.
        self.assertEqual(len(rig.sends), 1,
                         f"the transport must have observed one send; got {rig.sends}")
        self.assertIs(rig.sends[0][1], False,
                      "the deferral must actually have been in force at send time; "
                      f"the store read back {rig.sends!r}")

    def _fund(self, base_url):
        """Fixture only: a funder, a payer with 10000, and a payee."""
        from app.client import ApiClient
        client = ApiClient(base_url)
        client.create_account(owner_id="funder", currency="USD",
                              allow_overdraft=True, account_id="acct-funder")
        client.create_account(owner_id=PAYER, currency="USD", account_id=PAYER)
        client.create_account(owner_id=PAYEE, currency="USD", account_id=PAYEE)
        client.send_transfer(idempotency_key="t15-fixture-fund",
                             from_account_id="acct-funder", to_account_id=PAYER,
                             amount_minor=FUND, currency="USD")


if __name__ == "__main__":
    unittest.main()
