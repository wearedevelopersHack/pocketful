"""T9 at the gate: a reload of the response to ``POST /send`` must mint nothing.

The obligation (plan T9, §2.3 amended): the web layer must leave the browser
holding a **GET** after every press. A failure branch that answers the POST with
the rendered page instead of a 303 leaves the address bar on ``POST /send``, so a
plain reload re-issues the POST, ``PendingStore.begin`` mints a second key, and
the server applies a second transfer. No ledger invariant trips: both transfers
are individually balanced and correctly keyed. The money simply moves twice.

That is why the assertion cannot read the page. What lies here is the *page* —
"Nothing was sent twice" is printed by exactly the code path that sent it twice.
So the evidence is the persisted record count and SQL over the ledger's own
``idempotency_keys`` / ``ledger_entries``.

Why this is not the existing ``[WEB]`` row. ``app/selfcheck.py`` reloads with a
``GET /``, which the success-path 303 already protects; it has never reloaded the
POST's own response, which is the path with no PRG guard. Modelling the browser
has to be explicit — a 3xx is followed with a GET, a 200/202 body is answered by
re-issuing the POST — or the test re-issues the safe verb and goes green on the
broken tree. That is how the selfcheck reported 36/36 across a live double-spend.

Teeth. ``test_the_pre_fix_shape_inverts_the_assertion`` runs the identical flow
against a handler that reproduces the pre-fix branch (a retryable failure answered
with the page, status 202) and asserts the assertion *would* fail there: two
records, two distinct keys, two debits. The shape is reconstructed from
``app/web.py`` at ``f9de7cf2``; the fix moved the file on, so the broken bytes no
longer exist and this stub is the only way to exercise the second branch.

The second door. A handler's ``except`` list can only name the failures someone
already imagined, and ``SendProtocolError`` (``app/send.py:138``) went unnamed —
an unhandled exception here is *no status line*, not a 500, so the browser stays
on the POST exactly as if the page had been rendered. The guarantee therefore had
to stop being an enumeration: the live ``do_POST`` answers 303 from a ``finally``
unless a status line was already committed, which holds for a type nobody wrote
down. ``test_an_unforeseen_exception_still_leaves_the_browser_on_a_get`` injects
one, and requires the record to be left **pending** — the transfer applied under
that key, so the key is the only way back to it.

Boundary note: like ``test_wire_refusal.py`` and ``test_client_retry.py``, this
file leaves the ``Ledger`` surface to drive the real stack over real HTTP. It
patches nothing global — the client is injected per server through
``create_ui_server(client=...)`` — so it carries no cross-file serialization
requirement.
"""

import contextlib
import http.client
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

from app.client import ApiClient
from app.keystore import PendingStore
from app.send import RetryableSendError
from app.wallet import Wallet
import app.web as app_web

from ledger import OPENING_GRANT_MINOR

MINT = "acct-t9-mint"
PAYER = "acct-t9-payer"
PAYEE = "acct-t9-payee"
FUND = 1000
SEND_TEXT = "1.00"
SEND = 100
FIXTURE_KEY = "t9-fixture-fund"

# T20: opening a USD account mints a balanced grant transfer from `__system__`
# (ledger.OPENING_GRANT_MINOR). `_fund()` opens three USD accounts, so PAYER
# starts at the grant rather than 0. The grant is a credit to PAYER, so `_debits`
# (entries < 0) is unaffected; only the balance carries it.
GRANT = OPENING_GRANT_MINOR

REDIRECTS = (301, 302, 303, 307, 308)

PENDING_NOTICE = ("The transfer could not be confirmed and is recorded as pending. "
                  "Nothing was sent twice. Press Retry to try again with the same key.")


def _import_api():
    import api.app as api_app
    return api_app


class LostFirstTransferResponse:
    """Real HTTP, but the FIRST transfer response is lost after the server applied it.

    The web analogue of the T4 process kill: the request commits server-side and
    the client sees a transport failure instead of the answer. Every later call
    passes through, and each ``Idempotency-Key`` that crosses the wire is kept.
    """

    def __init__(self):
        self.sent_keys = []
        self._lost = False

    def __call__(self, method, url, body, headers):
        if "Idempotency-Key" in headers:
            self.sent_keys.append(headers["Idempotency-Key"])
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            try:
                status, raw = exc.code, exc.read()
            finally:
                exc.close()  # unclosed HTTPError -> ResourceWarning at GC
        if not self._lost and method == "POST" and urlsplit(url).path == "/transfers":
            self._lost = True
            raise OSError("connection reset while reading the response")
        return status, raw


class PreFixSendHandler(app_web.WalletUIHandler):
    """MUTANT: the pre-fix ``_handle_send`` from ``app/web.py f9de7cf2``.

    In the pre-fix code only the ``else:`` branch called ``_redirect``; every
    failure branch answered the POST with the rendered page. This reproduces the
    one branch the scenario reaches — ``except RetryableSendError`` — so the
    reload-of-the-POST-response path exists again and the assertion can be shown
    to have teeth. Nothing else about the handler is changed.
    """

    def _handle_send(self) -> None:
        wallet: Wallet = self.server.wallet
        form = self._form()
        to_account = (form.get("to_account_id") or [""])[0].strip()
        amount_text = (form.get("amount") or [""])[0].strip()
        try:
            with self.server.lock:
                outcome = wallet.send(to_account, amount_text)
        except RetryableSendError:
            # THE PRE-FIX SHAPE: the failure is answered with the page itself,
            # so the browser stays on the POST and a reload re-issues it.
            self._render(notice=PENDING_NOTICE, status=202)
        else:
            self._redirect(f"/?sent={outcome.status}")


class UnforeseenFailure(Exception):
    """A failure type that appears on NO ``except`` list in ``app/web.py``.

    That is its whole purpose. ``ApiClient`` wraps only ``OSError``
    (``app/client.py:76-78``), so this type escapes ``_handle_send`` through the
    ``else`` of every clause it has. An enumerative fix cannot answer it; only a
    structural one can.
    """


class UnforeseenTransferResponse:
    """Real HTTP, then an unforeseen exception instead of the answer.

    The transfer commits server-side and the client cannot read the outcome:
    the record stays pending and the money has moved. If the browser is left on
    the POST, its reload is a re-POST with a *fresh* key — a new transfer, not a
    replay — which is the double-spend door.
    """

    def __init__(self):
        self.sent_keys = []
        self._sprung = False

    def __call__(self, method, url, body, headers):
        if "Idempotency-Key" in headers:
            self.sent_keys.append(headers["Idempotency-Key"])
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            try:
                status, raw = exc.code, exc.read()
            finally:
                exc.close()  # unclosed HTTPError -> ResourceWarning at GC
        if not self._sprung and method == "POST" and urlsplit(url).path == "/transfers":
            self._sprung = True
            raise UnforeseenFailure("a type no `except` clause names")
        return status, raw


class NoExitGuardHandler(app_web.WalletUIHandler):
    """MUTANT: ``do_POST`` as it was before the exit guard — dispatch and return.

    This is the live ``do_POST`` (``app/web.py aa2cccce``) with the
    ``try/except/finally`` guard removed and nothing else changed: no catch-all,
    no ``_responded`` bookkeeping. It reproduces ``app/web.py 9e254fac``, the
    revision whose claim — "every branch redirects" — was true of the *list* and
    false of the *property*. ``_handle_send`` is left live, so the mutant does
    not get to rely on a clause it did not write.

    Reconstructed from deleted bytes (no version control), so its fidelity is a
    judgment call and not the author's to certify — same caveat as
    ``PreFixSendHandler``.
    """

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/send":
            self._handle_send()
            return
        if path == "/retry":
            self._handle_retry()
            return
        self._json(404, {"error": "not_found"})


class DiscardingExitGuardHandler(app_web.WalletUIHandler):
    """MUTANT: a fallback that lands the browser on a GET and throws the key away.

    The browser is safe — the reload is a GET — and the record is gone, so the
    transfer that already applied server-side can never be replayed. The payer
    has been debited and the client has no key left to name that transfer with.
    This is the same defect through the other door, and it is why "a 303" is not
    by itself the whole requirement: the record must be left **pending**.
    """

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        self._responded = False
        try:
            if path == "/send":
                self._handle_send()
            elif path == "/retry":
                self._handle_retry()
            else:
                self._json(404, {"error": "not_found"})
        except Exception:                       # noqa: BLE001 - the mutant's point
            for record in self.server.wallet.store.outstanding():
                self.server.wallet.store.discard(record.key)
        finally:
            if not self._responded:
                self._redirect("/?error=internal")


@contextlib.contextmanager
def _running_api():
    """A real `api/` server over a real on-disk ledger, on an ephemeral port."""
    api_app = _import_api()
    workdir = tempfile.mkdtemp(prefix="pocketful-t9-api-")
    db_path = os.path.join(workdir, "t9.db")
    server = api_app.create_server(db_path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        _wait_until_answering(port)
        yield db_path, f"http://127.0.0.1:{port}", port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)


def _wait_until_answering(port, timeout=10.0):
    """Poll the API until the socket answers. Any HTTP reply counts as answered."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/accounts/nobody/balance", timeout=1):
                pass
            return
        except urllib.error.HTTPError as exc:
            # An HTTPError wraps the response fp (urllib's addbase subclasses
            # tempfile._TemporaryFileWrapper); if it is not closed the close is
            # deferred to GC and emits a ResourceWarning. Close it here so the
            # warning channel stays meaningful.
            exc.close()
            return
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"api server on port {port} never answered")


def _request(port, method, path, form=None):
    """One raw HTTP call to the UI. Redirects are NOT followed: the caller needs
    to see whether the response to a POST was a redirect or a page."""
    payload = None
    headers = {}
    if form is not None:
        payload = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=payload,
                                     headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, dict(exc.headers), exc.read().decode()
        finally:
            exc.close()  # unclosed HTTPError -> ResourceWarning at GC


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _sql(db_path, statement, params=()):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(statement, params).fetchall()
    finally:
        conn.close()


class ReloadOfTheSendResponse(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = os.path.join(self._tmp.name, "ui-store.json")

    def _fund(self, base):
        """Fixture only: three accounts and a funded payer, over real HTTP."""
        client = ApiClient(base)
        client.create_account(owner_id=MINT, currency="USD",
                              allow_overdraft=True, account_id=MINT)
        client.create_account(owner_id=PAYER, currency="USD", account_id=PAYER)
        client.create_account(owner_id=PAYEE, currency="USD", account_id=PAYEE)
        client.send_transfer(idempotency_key=FIXTURE_KEY, from_account_id=MINT,
                             to_account_id=PAYER, amount_minor=FUND, currency="USD")

    @contextlib.contextmanager
    def _ui(self, base, transport, handler=app_web.WalletUIHandler):
        """A real `app.web` server, optionally with the pre-fix handler."""
        store = PendingStore(self.store_path)
        wallet = Wallet(ApiClient(base, transport=transport), store, PAYER)
        server = app_web.WalletUIServer(("127.0.0.1", 0), handler, wallet=wallet)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            time.sleep(0.05)
            yield port
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    # -- evidence readers -----------------------------------------------------

    def _records(self):
        return PendingStore(self.store_path).all_records()

    def _debits(self, db_path):
        return _sql(db_path,
                    "SELECT COUNT(*) FROM ledger_entries "
                    "WHERE account_id = ? AND amount_minor < 0", (PAYER,))[0][0]

    def _balance(self, db_path):
        return _sql(db_path,
                    "SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries "
                    "WHERE account_id = ?", (PAYER,))[0][0]

    def _client_keys(self, db_path):
        return sorted(row[0] for row in
                      _sql(db_path, "SELECT key FROM idempotency_keys WHERE key != ?",
                           (FIXTURE_KEY,)))

    def _press_then_reload(self, port):
        """The whole scenario, verbatim: one press, then the browser's reload.

        A reload re-issues the request that produced the page currently on
        screen. If the press answered with a redirect the browser is showing a
        GET, and the reload is that GET; if it answered with the page itself the
        browser is still on the POST, and the reload is the identical POST. The
        test asks the response what it was rather than assuming.
        """
        form = {"to_account_id": PAYEE, "amount": SEND_TEXT}
        status, headers, _ = _request(port, "POST", "/send", form)
        if status in REDIRECTS:
            location = headers.get("Location") or "/"
            reload_status, reload_headers, _ = _request(port, "GET", location)
            reloaded_what = "GET"
        else:
            reload_status, reload_headers, _ = _request(port, "POST", "/send", form)
            reloaded_what = "POST"
        return status, headers, reload_status, reloaded_what

    # -- the contract ---------------------------------------------------------

    def test_a_failed_press_answers_with_a_redirect_never_the_page(self):
        """The control the whole obligation rests on. If a press is ever answered
        with the page, the browser is on the POST and the next reload re-issues
        it — so this is asserted before anything about the reload."""
        with _running_api() as (db_path, base, _):
            self._fund(base)
            transport = LostFirstTransferResponse()
            with self._ui(base, transport) as port:
                status, headers, _ = _request(
                    port, "POST", "/send",
                    {"to_account_id": PAYEE, "amount": SEND_TEXT})
                self.assertIn(status, REDIRECTS,
                              "a failed press must answer 303, not the page; "
                              f"got {status}")
                self.assertTrue(headers.get("Location"),
                                "the redirect must name where the browser goes")
                self.assertEqual(len(self._records()), 1)
                self.assertEqual(self._debits(db_path), 1,
                                 "the server applied the transfer once")

    # -- the contract, as one function ---------------------------------------

    def _assert_reload_is_inert(self, db_path):
        """T9: after a press and a reload, the ledger shows one of everything.

        This is the assertion, in one place, so the mutant test can run *the same
        function* against the pre-fix shape and watch it raise — rather than
        asserting the inverted numbers and hoping the two agree.
        """
        records = self._records()
        self.assertEqual(len(records), 1,
                         f"T9 second key minted: the reload of the POST response "
                         f"created another record: "
                         f"{[(r.state, r.key) for r in records]}")
        self.assertEqual(self._debits(db_path), 1,
                         f"T9 double debit: the reload moved money twice; payer "
                         f"balance is {self._balance(db_path)}, "
                         f"expected {GRANT + FUND - SEND}")
        self.assertEqual(self._client_keys(db_path), [records[0].key],
                         "T9 extra key: the ledger recorded more than the one "
                         "client key")
        self.assertEqual(self._balance(db_path), GRANT + FUND - SEND)
        return records

    def test_a_reload_of_the_post_response_mints_no_second_key(self):
        """T9. The press loses its response; the browser reloads what it is
        showing. One record, one key, one debit — asserted on the ledger, never
        on the page, because the page is what claims 'nothing was sent twice'."""
        with _running_api() as (db_path, base, _):
            self._fund(base)
            transport = LostFirstTransferResponse()
            with self._ui(base, transport) as port:
                press, _, reload_status, reloaded_what = self._press_then_reload(port)

                self.assertIn(press, REDIRECTS,
                              f"the press answered {press}, so the reload had to "
                              f"be a {reloaded_what}")
                self.assertEqual(reloaded_what, "GET",
                                 "a press that redirects puts the browser on a "
                                 "GET, which is why the reload is safe")
                self._assert_reload_is_inert(db_path)

    def test_the_retry_press_does_not_compound_the_leak(self):
        """Severity, pinned so it cannot be inflated. The pending record's key was
        genuinely recorded server-side, so Retry replays it rather than applying
        a third transfer: the leak is one duplicated debit, not a cascade."""
        with _running_api() as (db_path, base, _):
            self._fund(base)
            transport = LostFirstTransferResponse()
            with self._ui(base, transport) as port:
                self._press_then_reload(port)
                status, _, _ = _request(port, "POST", "/retry")
                self.assertIn(status, REDIRECTS)
                self.assertEqual(self._debits(db_path), 1,
                                 "Retry must replay the pending key, not add a debit")
                self.assertEqual(self._balance(db_path), GRANT + FUND - SEND)

    # -- the teeth ------------------------------------------------------------

    def test_the_pre_fix_shape_inverts_the_assertion(self):
        """Falsifiability, and the only way to exercise the second branch: the
        broken bytes are gone (no version control), so the pre-fix shape is
        reconstructed as a handler.

        The check is not "the mutant produces 2 and 2" — that would be asserting
        the inverted numbers and hoping they agree with the contract. It runs
        **the same `_assert_reload_is_inert`** the contract test runs, and
        requires it to raise, naming the invariant that fired.
        """
        with _running_api() as (db_path, base, _):
            self._fund(base)
            transport = LostFirstTransferResponse()
            with self._ui(base, transport, handler=PreFixSendHandler) as port:
                press, _, _, reloaded_what = self._press_then_reload(port)
                self.assertNotIn(press, REDIRECTS,
                                 "the mutant exists to answer the POST with the "
                                 "page; if this stops being true it no longer "
                                 "models the defect")
                self.assertEqual(reloaded_what, "POST",
                                 "with no redirect the browser is still on the "
                                 "POST, so the reload re-issues it")

                with self.assertRaises(AssertionError) as caught:
                    self._assert_reload_is_inert(db_path)
                self.assertIn("T9 second key minted", str(caught.exception),
                              "the same assertion must fail here, and its message "
                              "must name the invariant that fired — not merely "
                              "raise somewhere")

                # And the shape of the break, so the numbers are on the record.
                records = self._records()
                self.assertEqual(len(records), 2)
                self.assertEqual(len({r.key for r in records}), 2,
                                 "two distinct keys — a fresh key is a fresh "
                                 "transfer to the ledger")
                self.assertEqual(self._debits(db_path), 2,
                                 "the reload moved money a second time")
                self.assertEqual(self._balance(db_path), GRANT + FUND - 2 * SEND,
                                 "one duplicated debit, and the payer pays it")
                self.assertEqual(len(set(transport.sent_keys)), 2,
                                 "two distinct keys crossed the wire")

    # -- the door enumeration could not see ----------------------------------

    def _press_unforeseen_then_reload(self, port):
        """One press whose failure type is on no list, then the browser's reload.

        The reload is decided by what came back, exactly as in
        ``_press_then_reload``: a 3xx is followed with a GET, anything else
        means the browser is still on the POST and re-issues it.
        """
        form = {"to_account_id": PAYEE, "amount": SEND_TEXT}
        try:
            status, headers, _ = _request(port, "POST", "/send", form)
        except (OSError, http.client.HTTPException) as exc:
            raise AssertionError(
                f"T12 no status line: the reply to the press never arrived "
                f"({exc!r}), so the browser is still on POST /send and its "
                f"reload is a re-POST") from exc
        if status in REDIRECTS:
            _request(port, "GET", headers.get("Location") or "/")
            reloaded_what = "GET"
        else:
            _request(port, "POST", "/send", form)
            reloaded_what = "POST"
        return status, reloaded_what

    def test_an_unforeseen_exception_still_leaves_the_browser_on_a_get(self):
        """The property is structural, not a longer list.

        ``UnforeseenFailure`` is named by no ``except`` clause in ``app/web.py``
        and is not an ``OSError``, so ``ApiClient`` does not wrap it into the
        ``ApiError`` the clauses do name. Nothing in ``_handle_send`` can catch
        it; the only thing that can answer this POST is the exit guard on
        ``do_POST`` itself. So this test is green only while the guarantee is a
        property of *exiting the handler* rather than of the handler's clauses.
        """
        with _running_api() as (db_path, base, _):
            self._fund(base)
            transport = UnforeseenTransferResponse()
            with self._ui(base, transport) as port:
                status, reloaded_what = self._press_unforeseen_then_reload(port)
                self.assertIn(status, REDIRECTS,
                              "an unforeseen failure must still leave the browser "
                              f"on a GET; the press answered {status}")
                self.assertEqual(reloaded_what, "GET",
                                 "the reload of a redirect is a GET, which mints "
                                 "nothing")
                # The same assertion the T9 contract runs, because it is the same
                # contract: the money left the payer once, and the reload added
                # nothing. Two doors into one double-spend; one assertion.
                records = self._assert_reload_is_inert(db_path)

                # The record must be *pending* — untouched. The transfer applied
                # server-side under its key, so the reply the client could not
                # read is a replay waiting to happen: `resume` replays that same
                # key and gets the same transfer. That only holds while the
                # record is left alone. A fallback that discarded it, or settled
                # it without a transfer_id, would turn a benign unknown into a
                # lost key and re-open the door from the other side.
                self.assertEqual([r.state for r in records], ["pending"],
                                 "the unforeseen failure must leave the record "
                                 f"pending; found {[r.state for r in records]}")

    def test_without_the_exit_guard_the_same_press_costs_a_second_debit(self):
        """Teeth: the guard is the only thing holding the property.

        With the guard removed the press gets no status line at all, which is the
        pre-fix defect exactly — not a 500, not a page, *nothing*. The assertion
        must fail and must say so, rather than raising somewhere incidentally.
        """
        with _running_api() as (db_path, base, _):
            self._fund(base)
            transport = UnforeseenTransferResponse()
            with self._ui(base, transport, handler=NoExitGuardHandler) as port:
                with self.assertRaises(AssertionError) as caught:
                    self._press_unforeseen_then_reload(port)
                self.assertIn("T12 no status line", str(caught.exception),
                              "the failure must name what was observed — no reply "
                              "committed — not merely raise")

                # The browser is still on the POST, so this reload is a re-POST.
                # Its key is fresh, so the server applies a second transfer.
                _request(port, "POST", "/send",
                         {"to_account_id": PAYEE, "amount": SEND_TEXT})
                with self.assertRaises(AssertionError) as caught_money:
                    self._assert_reload_is_inert(db_path)
                self.assertIn("T9 second key minted", str(caught_money.exception),
                              "the money assertion must fail here too, and name "
                              "the invariant that fired")

                self.assertEqual(len(self._records()), 2)
                self.assertEqual(self._debits(db_path), 2,
                                 "the re-POST moved money a second time")
                self.assertEqual(self._balance(db_path), GRANT + FUND - 2 * SEND)

    def test_a_fallback_that_drops_the_record_loses_the_key(self):
        """The other half of clause 2, and the teeth under the state assertion.

        A 303 alone is not the requirement. This fallback lands the browser on a
        GET — the reload is inert, the page is fine — and has already discarded
        the record the server needs in order to replay. The debit stands and the
        client is holding nothing that names it. Green on every assertion a
        page-reader would write; caught only by looking at the record's state.
        """
        with _running_api() as (db_path, base, _):
            self._fund(base)
            transport = UnforeseenTransferResponse()
            with self._ui(base, transport, handler=DiscardingExitGuardHandler) as port:
                status, reloaded_what = self._press_unforeseen_then_reload(port)
                self.assertIn(status, REDIRECTS,
                              "the mutant does answer with a redirect — that is "
                              "what makes it worth having a mutant for")
                self.assertEqual(reloaded_what, "GET")

                self.assertEqual(self._records(), [],
                                 "the fallback discarded the pending record")
                self.assertEqual(self._debits(db_path), 1,
                                 "and the debit it was the only proof of still "
                                 "stands, with no key left to replay it")


class UnforeseenReadFailure(Exception):
    """A failure type that appears on NO ``except`` list in ``app/web.py``.

    The GET twin of :class:`UnforeseenFailure`, and it exists for the same
    reason: ``_render_feed`` catches the ``ApiError`` family it knows about, and
    this type is named nowhere. ``ApiClient`` wraps only ``OSError``, so it
    escapes every clause the render has. Nothing in ``_render_feed`` can answer
    it — the only thing that can answer the request is ``do_GET``'s own exit
    guard. An enumerative fix cannot reach it; only a structural one can.
    """


class UnforeseenReadResponse:
    """Real HTTP, then an unforeseen exception instead of the answer.

    The read reaches the API and is answered — the server has done its part
    correctly — and the failure is raised on the way back, inside the render
    that was going to use it. The page therefore genuinely cannot be built. The
    question this transport exists to ask is what the *handler* does about that.
    """

    def __init__(self, path_fragment="/accounts/"):
        self.path_fragment = path_fragment
        self.spring_count = 0

    def __call__(self, method, url, body, headers):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            try:
                status, raw = exc.code, exc.read()
            finally:
                exc.close()  # unclosed HTTPError -> ResourceWarning at GC
        if method == "GET" and self.path_fragment in url:
            self.spring_count += 1
            raise UnforeseenReadFailure("a type no `except` clause names")
        return status, raw


class NoGetExitGuardHandler(app_web.WalletUIHandler):
    """MUTANT: ``do_GET`` as it was before the exit guard — dispatch and return.

    The live ``do_GET`` (``app/web.py:516``) with the ``try/except/finally``
    guard removed and nothing else changed: no catch-all, no ``_responded``
    bookkeeping. ``_render_feed`` is left live, so the mutant does not get to
    rely on a clause it did not write.

    Reconstructed from deleted bytes (no version control), so its fidelity is a
    judgment call and not the author's to certify — the same caveat
    ``PreFixSendHandler`` and ``NoExitGuardHandler`` carry.
    """

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if parts.path == "/":
            self._render_feed(app_web.parse_qs(parts.query, keep_blank_values=True))
            return
        self._json(404, {"error": "not_found"})


class TheGetExitGuard(unittest.TestCase):
    """The GET half of the exit guarantee, which had no row anywhere.

    ``app/web.py:516`` is the only ``do_GET`` in the tree. Every fault row in
    this file overrides ``do_POST``, and the ``[WEB]`` fault labels in
    ``app/selfcheck.py`` are the POST path too — so the GET guard (a ``try`` that
    catches ``Exception``, and a ``finally`` that sends the fallback when nothing
    was written) was exercised by no row at all, in-gate or out, while
    demonstrably working. A guarantee no row can fail is a guarantee that
    survives being deleted.

    The defect it guards against is the one this whole file is about, one
    method over: a request answered by *nothing* — the socket closed with no
    status line — which a browser sees as a dead page rather than a wrong one,
    and which no status-code assertion can distinguish from a client that never
    asked.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = os.path.join(self._tmp.name, "ui-pending.json")

    def _fund(self, base):
        client = ApiClient(base)
        client.create_account(owner_id=MINT, currency="USD",
                              allow_overdraft=True, account_id=MINT)
        client.create_account(owner_id=PAYER, currency="USD", account_id=PAYER)

    @contextlib.contextmanager
    def _ui(self, base, transport, handler=app_web.WalletUIHandler):
        """A real `app.web` server, optionally with the pre-fix GET handler."""
        store = PendingStore(self.store_path)
        wallet = Wallet(ApiClient(base, transport=transport), store, PAYER)
        server = app_web.WalletUIServer(("127.0.0.1", 0), handler, wallet=wallet)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            time.sleep(0.05)
            yield port
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def _read(self, port, path="/"):
        """One GET, with "no answer at all" turned into a legible failure.

        ``_request`` returns any HTTP status, including 500 — so a caller that
        only checks the status cannot tell "the guard answered badly" from "the
        guard did not answer". This turns the second into a failure that names
        what was observed, the same shape as ``_press_unforeseen_then_reload``.
        """
        try:
            return _request(port, "GET", path)
        except (OSError, http.client.HTTPException) as exc:
            raise AssertionError(
                f"T43 no status line: the reply to GET {path} never arrived "
                f"({type(exc).__name__}: {exc})") from exc

    def test_the_get_guard_answers_a_fault_no_clause_could_have_named(self):
        """The live ``do_GET``: an unforeseen render failure is still answered.

        The premise is measured before the effect. Without it, a 500 could be a
        response to the wrong thing entirely — a route that never rendered, a
        server that refused the request — and the row would be crediting the
        guard for a fault it never saw.
        """
        with _running_api() as (_, base, _):
            self._fund(base)
            transport = UnforeseenReadResponse()
            with self._ui(base, transport) as port:
                before = transport.spring_count
                status, _, body = self._read(port, "/")
                # Measured inside the block: the transport is the mutant's, and a
                # later assertion outside it could not attribute the count.
                self.assertGreater(
                    transport.spring_count, before,
                    "premise: the injected read failure must actually have fired, "
                    "or the answer below is a response to something else")

        self.assertEqual(
            status, 500,
            "a render failure the handler has no clause for must still be answered "
            f"with a status line; got {status}")
        self.assertIn(
            "The page could not be built. Nothing was sent and nothing moved.", body,
            "the fallback must be do_GET's own — a body that is not the fallback "
            "would mean the request was answered by something other than the guard")

    def test_without_the_get_guard_the_same_fault_answers_nothing(self):
        """Teeth: the guard is the only thing answering this request.

        With the guard removed the GET gets no status line at all — not a 500,
        not a page, *nothing*. That is the pre-fix defect exactly, and it is why
        the assertion above has to distinguish "answered badly" from "not
        answered": both are green under a status-only check on a wildcard.

        The mutant's premise is asserted too, and it is a different premise from
        the control's: it must have faulted for the **same reason**, or "no
        answer" could be attributable to something the mutant broke elsewhere.
        """
        with _running_api() as (_, base, _):
            self._fund(base)
            transport = UnforeseenReadResponse()
            with self._ui(base, transport, handler=NoGetExitGuardHandler) as port:
                with self.assertRaises(AssertionError) as caught:
                    self._read(port, "/")
                self.assertGreater(
                    transport.spring_count, 0,
                    "premise: the mutant must have faulted on the read exactly as "
                    "the control did, or its silence is not attributable to the "
                    "missing guard")

        self.assertIn(
            "T43 no status line", str(caught.exception),
            "the failure must name what was observed — no reply committed — not "
            "merely raise somewhere incidental")


if __name__ == "__main__":
    unittest.main()
