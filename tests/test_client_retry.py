"""T4 at the gate: the client's idempotency key survives the process dying.

The obligation (plan §2.3, T4 DoD): the key is minted and durably persisted
**before** the first attempt and reused **verbatim** on every retry. A client
that regenerates on retry is a double-spend the ledger cannot catch, because a
different key is a different transfer. Nothing about that is visible to `tests/`
through `ledger/` or `api/` — the defect lives entirely in the client — so this
file drives the real `app/` modules as a real process against a real server.

Why this is not `app/selfcheck.py` copied. The frontend-engineer's driver is
evidence that lives beside its subject, and by the room's own rule scratch-proven
is not gate-proven. This file shares the *contract* with it and nothing else: it
drives `app.send` itself, records the outgoing header in its own transport, and
reads the ledger with SQL. Two artifacts agreeing because they share code prove
nothing; these two agree because they independently observe the same wire.

Why a separate process. `app/keystore.py`'s docstring is explicit that the record
must survive the process dying, and an in-process `raise` cannot show that. So the
child is SIGKILLed — see `tests/t4_client_child.py`, which does it deterministically
from inside its transport, after the server has answered 201 and before the client
can settle.

What is asserted, and on which surface:

* the **outgoing** `Idempotency-Key`, recorded by the child's transport — the
  header as the client put it on the wire, before any server-side parsing;
* the ledger's own verdict, read with **SQL** from `idempotency_keys` and
  `transfers` — the keys the server recorded, the transfer count, and the balance;
* the store was never re-read to decide anything: the client's own file is only a
  fixture to carry state between the two children.

Every scenario also has the mutant behind it:
`test_a_client_that_remints_on_retry_double_spends` runs the same flow against a
retry path that mints a fresh key, and asserts the double-spend lands. Without it
this file would be a green test that had never been shown able to fail.
"""

import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MINT = "acct-t4-mint"
PAYER = "acct-t4-payer"
PAYEE = "acct-t4-payee"
FUND = 1000
SEND = 500


def _import_app():
    """Import the client under test lazily, so a broken import is a readable
    failure rather than a collection error for the whole suite."""
    import app.client as app_client
    import app.send as app_send
    return app_client, app_send


def _import_api():
    import api.app as api_app
    return api_app


@contextlib.contextmanager
def _running_server():
    """A real `api/` server over a real on-disk ledger, on an ephemeral port."""
    api_app = _import_api()
    workdir = tempfile.mkdtemp(prefix="pocketful-t4-")
    db_path = os.path.join(workdir, "t4.db")
    server = api_app.create_server(db_path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        _wait_until_answering(port)
        yield db_path, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)


def _wait_until_answering(port, timeout=10.0):
    """Poll with a real request until the socket answers. 404 counts as answered."""
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
    raise AssertionError(f"server on port {port} never answered")


def _child(phase, *, port, store, out, crash=False, mutate=False,
           from_id=PAYER, to_id=PAYEE, amount=SEND):
    command = [sys.executable, "-m", "tests.t4_client_child",
               "--base-url", f"http://127.0.0.1:{port}",
               "--store", store, "--out", out, "--phase", phase,
               "--from", from_id, "--to", to_id,
               "--amount", str(amount), "--currency", "USD"]
    if crash:
        command.append("--crash")
    if mutate:
        command.append("--mutate-fresh-key")
    return subprocess.run(command, cwd=REPO_ROOT, capture_output=True, timeout=60)


def _events(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _requests(path):
    return [event["key"] for event in _events(path) if event["event"] == "request"]


def _outcomes(path):
    return [event for event in _events(path) if event["event"] == "outcome"]


def _sql(db_path, statement, params=()):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(statement, params).fetchall()
    finally:
        conn.close()


class ClientRetrySurvivesACrash(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = os.path.join(self._tmp.name, "pending.json")
        self.log = os.path.join(self._tmp.name, "wire.jsonl")

    def _fund(self, port):
        """Fixture only: three accounts and a funded payer, over real HTTP."""
        app_client, _ = _import_app()
        client = app_client.ApiClient(f"http://127.0.0.1:{port}")
        client.create_account(owner_id=MINT, currency="USD",
                              allow_overdraft=True, account_id=MINT)
        client.create_account(owner_id=PAYER, currency="USD", account_id=PAYER)
        client.create_account(owner_id=PAYEE, currency="USD", account_id=PAYEE)
        client.send_transfer(idempotency_key="t4-fixture-fund",
                             from_account_id=MINT, to_account_id=PAYER,
                             amount_minor=FUND, currency="USD")

    def _balance(self, db_path, account_id):
        rows = _sql(db_path,
                    "SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries "
                    "WHERE account_id = ?", (account_id,))
        return rows[0][0]

    def _transfer_count(self, db_path):
        return _sql(db_path, "SELECT COUNT(*) FROM transfers")[0][0]

    def _recorded_keys(self, db_path):
        return sorted(row[0] for row in
                      _sql(db_path, "SELECT key FROM idempotency_keys"))

    # -- the evidence -----------------------------------------------------

    def test_the_crash_leaves_the_record_pending_and_the_ledger_applied(self):
        """The crash window is real, not modelled: the server applied, and the
        client did not record it. Everything after this test depends on it, so it
        is asserted on its own rather than inferred from the resume that follows."""
        with _running_server() as (db_path, port):
            self._fund(port)
            result = _child("send", port=port, store=self.store, out=self.log,
                            crash=True)

            self.assertEqual(result.returncode, -signal.SIGKILL,
                             f"the child must die by SIGKILL, not exit; stderr:\n"
                             f"{result.stderr.decode()[-2000:]}")

            # Server side: applied once.
            self.assertEqual(self._transfer_count(db_path), 2,
                             "the funding transfer plus exactly one send")
            self.assertEqual(self._balance(db_path, PAYER), FUND - SEND)

            # Client side: the key is on the wire and the record is still pending,
            # so `resume` has something to recover.
            self.assertEqual(len(_requests(self.log)), 1,
                             "the crashed attempt put exactly one request on the wire")
            with open(self.store, encoding="utf-8") as handle:
                recovered = json.load(handle)["records"]
            self.assertEqual(len(recovered), 1)
            state = list(recovered.values())[0]["state"]
            self.assertEqual(state, "pending",
                             "a killed client must leave its record pending, or "
                             "there is nothing for resume to retry")

    def test_the_retry_reuses_the_same_key_on_the_wire_and_the_ledger_replays(self):
        """The obligation, end to end: same key on the wire, and the ledger says
        `replayed` rather than applying a second transfer."""
        with _running_server() as (db_path, port):
            self._fund(port)
            _child("send", port=port, store=self.store, out=self.log, crash=True)
            result = _child("resume", port=port, store=self.store, out=self.log)
            self.assertEqual(result.returncode, 0,
                             f"resume must succeed; stderr:\n"
                             f"{result.stderr.decode()[-2000:]}")

            sent = _requests(self.log)
            self.assertEqual(len(sent), 2, "one attempt, one retry")
            self.assertEqual(sent[0], sent[1],
                             "the retry must put the SAME Idempotency-Key on the "
                             "wire as the attempt it is retrying; a client that "
                             "remints is a double-spend the ledger cannot catch")

            outcomes = _outcomes(self.log)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["status"], "replayed",
                             "the ledger's verdict for the retry must be `replayed`")

            # The ledger, read with SQL: one send, not two, and the retry's
            # transfer_id is the one the first attempt created.
            self.assertEqual(self._transfer_count(db_path), 2)
            self.assertEqual(self._balance(db_path, PAYER), FUND - SEND,
                             "the payer must be debited exactly once")
            self.assertEqual(self._recorded_keys(db_path),
                             sorted(["t4-fixture-fund", sent[0]]),
                             "the ledger recorded the fixture key and the "
                             "client's own key, and nothing else")
            (transfer_id,) = _sql(
                db_path, "SELECT transfer_id FROM idempotency_keys WHERE key = ?",
                (sent[0],))[0]
            self.assertEqual(outcomes[0]["transfer_id"], transfer_id)

    def test_control_without_a_crash_applies_once(self):
        """The scenario is satisfiable without a crash, so the assertions above
        are not describing a flow that only exists in its broken form."""
        with _running_server() as (db_path, port):
            self._fund(port)
            result = _child("send", port=port, store=self.store, out=self.log)
            self.assertEqual(result.returncode, 0, result.stderr.decode()[-2000:])

            outcomes = _outcomes(self.log)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["status"], "applied")
            self.assertEqual(self._transfer_count(db_path), 2)
            self.assertEqual(self._balance(db_path, PAYER), FUND - SEND)
            self.assertEqual(len(_requests(self.log)), 1)

    # -- the mutant behind the assertion ----------------------------------

    def test_a_client_that_remints_on_retry_double_spends(self):
        """Falsifiability: with a retry path that mints a fresh key, every
        assertion in the evidence test inverts.

        This is the defect the obligation exists to prevent, and it is invisible
        to both `ledger/` and `api/` — the ledger is behaving correctly, it is
        just being asked to make a second, different transfer. So the test has to
        catch it on the client's own wire behaviour: a different key, a second
        transfer, and the payer debited twice.
        """
        with _running_server() as (db_path, port):
            self._fund(port)
            _child("send", port=port, store=self.store, out=self.log, crash=True)
            result = _child("resume", port=port, store=self.store, out=self.log,
                            mutate=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode()[-2000:])

            sent = _requests(self.log)
            self.assertEqual(len(sent), 2)
            self.assertNotEqual(
                sent[0], sent[1],
                "the mutant exists to put a DIFFERENT key on the retry; if this "
                "ever stops being true the mutant no longer models the defect")

            outcomes = _outcomes(self.log)
            self.assertEqual(outcomes[0]["status"], "applied",
                             "a fresh key is a fresh transfer, so the ledger "
                             "applies it rather than replaying")

            self.assertEqual(self._transfer_count(db_path), 3,
                             "the funding transfer plus TWO sends — the double spend")
            self.assertEqual(self._balance(db_path, PAYER), FUND - 2 * SEND,
                             "the payer is debited twice, which is the whole defect")
            self.assertEqual(len(self._recorded_keys(db_path)), 3)


if __name__ == "__main__":
    unittest.main()
