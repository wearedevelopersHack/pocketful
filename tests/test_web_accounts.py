"""T14 at the gate: creating an account and switching between accounts.

The obligation (room plan #19/#20, amending T4): the wallet UI gains a create
form and a presentational account switch, and neither of them may disturb the
money-path rules that were already closed.

**This file is red-first and is supposed to be red on the bytes it was written
against.** It pins a surface that does not exist yet: ``app/web.py`` at
``aa2ccccc951ac21467f0e0563ff464ed6559987b83f5b3fae4e5810b93067ff9`` has no
``/create`` route and no ``?account=`` handling, so the contract rows fail
against it, each for a reason named in its own failure message. The falsifiers
described below are green immediately on those same bytes — which is the point
of them: they have to be, or they could not catch the thing they exist to catch.
(The file has gained rows since that red run; the count in this paragraph is
about the draft, and the gate output is the current one.)

The four rules that do not relax because the surface is prettier:

1. **The browser never mints, holds or transmits an idempotency key.** Creating
   an account is not a money movement; it mints no key and moves no money. The
   evidence for "no key" is the *store file*, not the page: the page says
   whatever the code that wrote it says.
2. **``ui-pending.json`` stays ONE account-agnostic store.** Switching the
   active account must never create, truncate, move, replace or split it. Each
   record already carries its own ``from_account_id``/``to_account_id``, so
   replay is correct without splitting — and a per-account split would
   reintroduce the store-loss double-spend this project already extracted once.
   This is the load-bearing row, and it is the one with the most teeth here:
   ``_store_violations`` is a pure function of (before, after), and the two
   ``test_the_store_guard_fires_...`` tests drive it against a split store and a
   rewritten store, so the row that matters cannot be vacuous.
3. **``Retry`` resumes the whole store**, not only the on-screen account's
   records, and each record replays with **its own key and its own body**.
   Asserting only that *a* transfer happened would pass for an implementation
   that re-minted — the set equality of (key, body) pairs is the assertion.
4. **Every POST that can mint a key or move money still answers 303 and lands on
   a GET**, and the ``do_POST`` exit guard is untouched. That set is ``/send``,
   ``/retry`` and ``/create``. "Every POST answers 303" was an over-claim, since
   an *unrouted* POST cannot do either and answers honest HTTP instead — but it
   must still **commit a response**, because a dead connection leaves the browser
   on the POST. The narrowed rule is tested here in both halves.

The addendum's row, and why it is here. With only the renderer account-aware,
``GET /?account=B`` would show B's balance while ``POST /send`` still debited
the boot account A — the page and the action disagreeing about who you are. So
the active account must travel with the POST that acts on it, and
``test_a_send_acts_as_the_account_the_page_shows`` is the assertion that would
have caught it. Its control,
``test_a_send_without_the_account_field_keeps_todays_behaviour``, pins the
resolution rule that keeps every existing test valid: an absent or empty
``account`` field means the boot ``--account``, byte for byte.

**Stated rather than hidden** (the implementer asked for it in this file's
story): the field is client-supplied, so the UI no longer confines sends to the
boot account — anyone holding an id can act as it. On this demo that is already
the model, because account ids are minted uuid hex and shown only to their
creator, so holding one is the capability. It is still a change in kind from a
single-account console, and the reviewer should see it named.

Boundary note: like ``test_web_reload.py``, this file drives the real stack over
real HTTP and leaves the ``Ledger`` surface to the invariant helpers. It patches
nothing global — the client is injected per server — so it carries no
cross-file serialization requirement.
"""

import contextlib
import http.client
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock
from urllib.parse import urlsplit

from app.client import ApiClient
from app.keystore import PendingStore
from app.wallet import Wallet
import app.web as app_web

from tests.invariants import assert_i1, assert_i2, entry_count, idempotency_row_count

BOOT = "acct-t14-boot"          # the boot --account
OTHER = "acct-t14-other"        # a second, pre-existing account
NEW = "acct-t14-new"            # created through the web form
FUNDER = "acct-t14-funder"      # overdraft-allowed mint for the fixture

BOOT_FUND = 123456              # $1234.56
OTHER_FUND = 78901              # $789.01
BOOT_TEXT = "$1234.56"
OTHER_TEXT = "$789.01"

FIXTURE_KEY = "t14-fixture-fund"

REDIRECTS = (301, 302, 303, 307, 308)

# The active account must ride with the POST that acts on it. Any attribute
# order and either quoting style is accepted; only the name and the value are
# pinned, because those are what the resolution rule is about.
_HIDDEN_ACCOUNT = re.compile(r"<input\b[^>]*\bname=(?:\"account\"|'account')[^>]*>", re.I)

# "Which account does this page claim to be?" has to be read from the element
# that answers it, not from a substring search of the whole document. A bare
# ``assertIn(other_id, page)`` is satisfied by an activity row's counterparty or
# by the payee of a pending transfer — this file's first draft went green on the
# broken tree exactly that way, with the id arriving from the pending block.
_ACCOUNT_LABEL = re.compile(r"Account\s*<code>([^<]+)</code>", re.I)


def _import_api():
    import api.app as api_app
    return api_app


@contextlib.contextmanager
def _running_api():
    """A real `api/` server over a real on-disk ledger, on an ephemeral port."""
    api_app = _import_api()
    workdir = tempfile.mkdtemp(prefix="pocketful-t14-api-")
    db_path = os.path.join(workdir, "t14.db")
    server = api_app.create_server(db_path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        _wait_until_answering(port)
        yield db_path, f"http://127.0.0.1:{port}"
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


@contextlib.contextmanager
def _ui(store_path, base, transport=None):
    """A real `app.web` server over the real API. Redirects are not followed here."""
    store = PendingStore(store_path)
    client = ApiClient(base) if transport is None else ApiClient(base, transport=transport)
    wallet = Wallet(client, store, BOOT)
    server = app_web.WalletUIServer(("127.0.0.1", 0), app_web.WalletUIHandler, wallet=wallet)
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


def _request(port, method, path, form=None):
    """One raw HTTP call to the UI. Redirects are NOT followed: the caller needs
    to see whether the response to a POST was a 303 or a page."""
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
        return exc.code, dict(exc.headers), exc.read().decode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _active_account(page):
    """The account the page says it is showing, or a loud failure.

    See ``_ACCOUNT_LABEL``: the point is that this cannot be satisfied by the
    same id appearing somewhere else on the document.
    """
    match = _ACCOUNT_LABEL.search(page)
    if match is None:
        raise AssertionError(
            "the page names no active account (no `Account <code>` element), so it "
            "cannot be shown to be rendering the requested one")
    return match.group(1)


class RecordingTransport:
    """Real HTTP; keeps every ``POST /transfers`` that crossed the wire.

    The point is not that a transfer happened — it is the (key, body) *pairing*.
    A retry that re-minted a key, or that sent one record's key with another
    record's body, would still move money and still be a bug.
    """

    def __init__(self):
        self.sends: list[tuple[str | None, dict]] = []

    def __call__(self, method, url, body, headers):
        if method == "POST" and urlsplit(url).path == "/transfers":
            parsed = json.loads(body.decode("utf-8")) if body else {}
            self.sends.append((headers.get("Idempotency-Key"), parsed))
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


# -- the disclosure row and its falsifier --------------------------------------

_REAL_DOCUMENT = app_web._document
"""The genuine assembly point, captured before the mutant replaces it.

The mutant cannot call ``app_web._document``: while the patch is held that name
*is* the mutant, so the first cut recursed until ``RecursionError`` and measured
nothing. Capture the function object at import, then patch the name.
"""


def _document_without_the_notice(*, title, body):
    """MUTANT: the document assembly point with the disclosure left out.

    ``app/web.py``'s ``_document`` is a module-level function that both builders
    call, so "every page carries the notice" is a property of emitting HTML rather
    than of each page remembering — the same structural shape as ``do_POST``'s
    exit guard. Forgetting it is therefore a change to exactly one function, and
    this is that change.

    Note for anyone reusing this file: an earlier draft of this mutant overrode a
    ``_document`` *method* on the handler class. There is no such method — the
    real one is module-level — so the subclass was inert, nothing was mutated, and
    the falsifier failed by reporting no violation. The mutation has to be made at
    the seam that actually runs.

    This patches a **module global** for the duration of one server, the technique
    obligation 13 uses; it is process-wide while held, so this row is not
    parallelizable.
    """
    return _REAL_DOCUMENT(title=title, body=body).replace(app_web.DEMO_NOTICE, "")


def _documents_missing_the_notice(port, paths):
    """Which of ``paths`` rendered a document without the demo disclosure.

    Returns a list, empty when every document carried it. Non-document responses
    (``/health`` is JSON) are skipped: the notice is a property of documents.
    """
    missing = []
    for path in paths:
        _, headers, body = _request(port, "GET", path)
        if "text/html" not in (headers.get("Content-Type") or "").lower():
            continue
        if app_web.DEMO_NOTICE not in body:
            missing.append(path)
    return missing


# -- the pending item names its sender -----------------------------------------

_REAL_PENDING_BLOCK = app_web._pending_block
"""Captured for the same reason as ``_REAL_DOCUMENT``: the mutant must not reach
the behaviour it replaced through the name that now points at the mutant."""


def _pending_block_without_the_sender(pending):
    """MUTANT: the renderer as it was before T18's fix — the sender named nowhere.

    ``app/web.py`` rendered each unconfirmed transfer as
    ``send <amount> to <to_account_id> (not confirmed)``, so on B's page a record
    whose ``from_account_id`` is A read as B's own: the page implied, rather than
    said, who was sending. The fix added ``from <from_account_id>``.

    Shape, and it is the rule this file now follows for every mutant: **call the
    object captured before the rebinding, never the attribute you rebound.** This
    calls ``_REAL_PENDING_BLOCK``, so the mutant is the genuine renderer with one
    phrase removed — every other rendered field stays byte-real, and a kill is
    attributable to the missing sender alone rather than to a second difference
    the mutant introduced.

    This is a **reconstruction** (there is no version control on this machine, so
    the pre-fix bytes are gone): it strips exactly the `` from <id>`` phrase the
    fix added, which is what the pre-fix f-string produced for the same record.
    Faithfulness here is a reviewer's judgment item, not the author's.
    """
    html = _REAL_PENDING_BLOCK(pending)
    for rec in pending:
        html = html.replace(f" from {rec.from_account_id}", "")
    return html


def _pending_items(page):
    """The rendered unconfirmed-transfer items, as *elements*.

    Scope matters: an id anywhere on the page is satisfied by any element that
    happens to print one — the payee of an activity row, the active-account label,
    the hidden form field. This file's first draft went green on a broken tree
    exactly that way. The item is the thing that answers "who is sending this?",
    so the item is what gets read.
    """
    items = []
    for chunk in re.split(r"<li>", page)[1:]:
        item = chunk.split("</li>", 1)[0]
        if "not confirmed" in item.lower():
            items.append(item)
    return items


def _pending_items_missing_the_sender(page, sender_id):
    """Pending items that fail to name the account the transfer is sent FROM.

    Empty means every rendered unconfirmed item said who was sending.
    """
    return [item for item in _pending_items(page) if sender_id not in item]


# -- the load-bearing guard, as a pure function --------------------------------

def _store_state(store_path):
    """``(bytes or None, sorted names in the file's directory)``."""
    directory = os.path.dirname(os.path.abspath(store_path)) or "."
    try:
        with open(store_path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        raw = None
    return raw, sorted(os.listdir(directory))


def _store_violations(store_path, before):
    """Every way the store failed to survive untouched. Empty means untouched.

    Two halves, because there are two ways to break it: rewrite the one file
    (bytes differ) or keep a file per account (the directory gains a name). The
    failure text says which, and why it matters.
    """
    before_bytes, before_names = before
    after_bytes, after_names = _store_state(store_path)
    problems = []
    if after_bytes != before_bytes:
        problems.append(
            f"the pending store file changed ({len(before_bytes or b'')} -> "
            f"{len(after_bytes or b'')} bytes)")
    if after_names != before_names:
        problems.append(
            f"the directory holding the store changed ({before_names} -> "
            f"{after_names}); a per-account store split reintroduces the "
            "store-loss double-spend")
    return problems


class AccountsAndSwitching(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = os.path.join(self._tmp.name, "ui-pending.json")

    # -- fixture --------------------------------------------------------------

    def _fund(self, base):
        """Fixture only: a mint, two funded accounts with distinct balances."""
        client = ApiClient(base)
        client.create_account(owner_id=FUNDER, currency="USD",
                              allow_overdraft=True, account_id=FUNDER)
        client.create_account(owner_id=BOOT, currency="USD", account_id=BOOT)
        client.create_account(owner_id=OTHER, currency="USD", account_id=OTHER)
        client.send_transfer(idempotency_key=FIXTURE_KEY, from_account_id=FUNDER,
                             to_account_id=BOOT, amount_minor=BOOT_FUND, currency="USD")
        client.send_transfer(idempotency_key=FIXTURE_KEY + "-other",
                             from_account_id=FUNDER, to_account_id=OTHER,
                             amount_minor=OTHER_FUND, currency="USD")

    def _fund_store(self, base):
        """A pending record seeded before the UI server starts, so the switching
        path runs against a store that is non-empty and byte-distinctive."""
        return PendingStore(self.store_path).begin(
            from_account_id=BOOT, to_account_id=OTHER, amount_minor=250, currency="USD")

    def _balance(self, base, account_id):
        return ApiClient(base).get_balance(account_id)["balance_minor"]

    def _records(self):
        return PendingStore(self.store_path).all_records()

    def _ledger(self, db_path):
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    # -- 1. create-account mints no key and moves no money ---------------------

    def test_creating_an_account_goes_through_the_api_and_mints_nothing(self):
        with _running_api() as (db_path, base):
            self._fund(base)
            before_store = _store_state(self.store_path)
            before_keys = [rec.key for rec in self._records()]
            funder_before = self._balance(base, FUNDER)
            with self._ledger(db_path) as conn:
                entries_before = entry_count(conn)
                idempotency_before = idempotency_row_count(conn)

            with _ui(self.store_path, base) as port:
                status, headers, body = _request(
                    port, "POST", "/create",
                    {"owner_id": "t14-owner", "currency": "USD", "account_id": NEW})
                self.assertEqual(
                    status, 303,
                    f"POST /create must answer 303 like every other POST; got {status} "
                    f"with body {body[:120]!r}")
                location = headers.get("Location") or ""
                self.assertTrue(location.startswith("/?account="),
                                f"the create must land on the new account; got {location!r}")
                self.assertIn(NEW, location,
                              f"the Location must name the new account; got {location!r}")
                self.assertNotIn("<html", body.lower(),
                                 "no branch may render HTML on the POST's own response")
                follow, _, page = _request(port, "GET", location)
                self.assertEqual(follow, 200,
                                 f"the 303 target must be a GET that answers 200; got {follow}")
                self.assertEqual(_active_account(page), NEW,
                                 "the page landed on must be showing the new account")

            # It really exists at the API, and it is empty: balance 0, no activity.
            self.assertEqual(self._balance(base, NEW), 0,
                             "a brand-new account's derived balance must be 0")
            self.assertEqual(ApiClient(base).list_activity(NEW)["items"], [],
                             "a brand-new account must have no ledger activity")
            # ... and nothing moved: the funder is untouched, and so is the ledger.
            self.assertEqual(self._balance(base, FUNDER), funder_before,
                             "creating an account must move no money")
            with self._ledger(db_path) as conn:
                self.assertEqual(entry_count(conn), entries_before,
                                 "creating an account must write no ledger entries")
                self.assertEqual(idempotency_row_count(conn), idempotency_before,
                                 "the create must mint no idempotency key at the API; "
                                 "this is a DELTA around the create, because the "
                                 "fixture's own transfers legitimately hold keys")
                assert_i1(conn)
                assert_i2(conn)
            # ... and the browser minted nothing either: the store is the evidence.
            self.assertEqual(_store_state(self.store_path), before_store,
                             "creating an account must not touch the pending store")
            self.assertEqual([rec.key for rec in self._records()], before_keys,
                             "creating an account must mint no idempotency key")

    # -- 2. switching is presentational ---------------------------------------

    def test_switching_renders_that_account_not_the_boot_one(self):
        with _running_api() as (_, base):
            self._fund(base)
            with _ui(self.store_path, base) as port:
                status, _, page = _request(port, "GET", f"/?account={OTHER}")
                self.assertEqual(status, 200, f"GET /?account= must answer 200; got {status}")
                self.assertEqual(_active_account(page), OTHER,
                                 "the page must be showing the requested account")
                self.assertIn(OTHER_TEXT, page,
                              f"{OTHER} must render its own balance {OTHER_TEXT}")
                self.assertNotIn(BOOT_TEXT, page,
                                 f"{OTHER}'s page must not render the boot account's "
                                 f"balance {BOOT_TEXT}")

                # The bare page is still the --account default: the switch is
                # presentational, not a change of identity for the session.
                status, _, page = _request(port, "GET", "/")
                self.assertEqual(status, 200)
                self.assertEqual(_active_account(page), BOOT,
                                 "a bare GET / must still be the boot account")
                self.assertIn(BOOT_TEXT, page)
                self.assertNotIn(OTHER_TEXT, page,
                                 "the boot account's page must not render another "
                                 "account's balance")

                status, _, page = _request(port, "GET", "/?account=")
                self.assertEqual(status, 200)
                self.assertEqual(_active_account(page), BOOT,
                                 "an empty account parameter must fall back to the "
                                 "boot account")

    def test_an_unknown_account_renders_a_named_error_not_a_500(self):
        with _running_api() as (_, base):
            self._fund(base)
            with _ui(self.store_path, base) as port:
                status, _, page = _request(port, "GET", "/?account=does-not-exist")
                self.assertEqual(status, 200,
                                 "a missing account is a page, not a server error")
                self.assertIn("does-not-exist", page,
                              "the error page must name the id it could not find")
                self.assertIsNone(
                    _ACCOUNT_LABEL.search(page),
                    "an unknown account is an error, not a page about the boot "
                    "account: it must not claim to be showing one")

    # -- 3. the load-bearing row: the store is account-agnostic ---------------

    def test_creating_and_switching_leave_the_store_byte_identical(self):
        with _running_api() as (_, base):
            self._fund(base)
            seeded = self._fund_store(base)
            before = _store_state(self.store_path)
            self.assertIsNotNone(before[0], "the fixture must leave a store to protect")

            with _ui(self.store_path, base) as port:
                status, headers, _ = _request(
                    port, "POST", "/create",
                    {"owner_id": "t14-owner", "currency": "USD", "account_id": NEW})
                self.assertEqual(status, 303, f"POST /create must answer 303; got {status}")
                _request(port, "GET", headers.get("Location") or "/")
                status, _, _ = _request(port, "GET", f"/?account={OTHER}")
                self.assertEqual(status, 200, f"the switch must answer 200; got {status}")

            problems = _store_violations(self.store_path, before)
            self.assertEqual(
                problems, [],
                "the pending store is account-agnostic and must survive creating and "
                "switching untouched: " + "; ".join(problems))
            after = self._records()
            self.assertEqual([rec.key for rec in after], [seeded.key],
                             "the store must still hold exactly the records it held")
            self.assertEqual([rec.state for rec in after], ["pending"],
                             "and their states must be unchanged")
            self.assertEqual([rec.key for rec in PendingStore(self.store_path).outstanding()],
                             [seeded.key],
                             "the seeded record must still be outstanding and replayable")

    def test_the_store_guard_fires_on_a_split_store(self):
        """The load-bearing row's teeth, half one.

        A guard that cannot go red is not a guard. This drives the same pure
        function the row uses against the implementation the rule forbids: one
        store file per account. The mutation is made directly, so it proves the
        detector and not the route.
        """
        seeded = PendingStore(self.store_path).begin(
            from_account_id=BOOT, to_account_id=OTHER, amount_minor=250, currency="USD")
        self.assertIsNotNone(seeded)
        before = _store_state(self.store_path)

        split = PendingStore(f"{self.store_path}.{NEW}")
        split.begin(from_account_id=NEW, to_account_id=BOOT, amount_minor=1, currency="USD")

        problems = _store_violations(self.store_path, before)
        self.assertTrue(problems, "a per-account store split must be caught")
        self.assertIn("split", " ".join(problems),
                      f"and the failure must name the rule; got {problems}")

    def test_the_store_guard_fires_on_a_rewritten_store(self):
        """The load-bearing row's teeth, half two: same file, changed bytes."""
        seeded = PendingStore(self.store_path).begin(
            from_account_id=BOOT, to_account_id=OTHER, amount_minor=250, currency="USD")
        before = _store_state(self.store_path)

        PendingStore(self.store_path).discard(seeded.key)

        problems = _store_violations(self.store_path, before)
        self.assertTrue(problems, "a rewritten store file must be caught")
        self.assertIn("file changed", " ".join(problems),
                      f"and the failure must name what moved; got {problems}")

    # -- 4. retry resumes the whole store, whichever account is on screen -----

    def test_retry_resumes_the_whole_store_regardless_of_the_account_on_screen(self):
        with _running_api() as (_, base):
            self._fund(base)
            store = PendingStore(self.store_path)
            first = store.begin(from_account_id=BOOT, to_account_id=OTHER,
                                amount_minor=300, currency="USD")
            second = store.begin(from_account_id=OTHER, to_account_id=BOOT,
                                 amount_minor=500, currency="USD")
            transport = RecordingTransport()

            with _ui(self.store_path, base, transport=transport) as port:
                # A record belonging to the boot account is replayed while a
                # *different* account is the one on screen.
                status, _, page = _request(port, "GET", f"/?account={OTHER}")
                self.assertEqual(status, 200, f"the switch must work first; got {status}")
                self.assertEqual(_active_account(page), OTHER,
                                 "the on-screen account must be the other one")
                self.assertIn(OTHER_TEXT, page,
                              "and it must be showing that account's balance")

                status, headers, _ = _request(port, "POST", "/retry")
                self.assertEqual(status, 303, f"POST /retry must answer 303; got {status}")
                follow, _, _ = _request(port, "GET", headers.get("Location") or "/")
                self.assertEqual(follow, 200)

            sent = sorted((key, json.dumps(body, sort_keys=True))
                          for key, body in transport.sends)
            expected = sorted(
                (rec.key, json.dumps(rec.body(), sort_keys=True))
                for rec in (first, second))
            self.assertEqual(
                sent, expected,
                "retry must replay every record in the store — not only the visible "
                "account's — each with its own key AND its own body")
            states = [rec.state for rec in self._records()]
            self.assertEqual(states, ["settled", "settled"],
                             f"both records must be settled by the retry; got {states}")

    # -- the addendum: the active account travels with the POST ---------------

    def test_a_send_acts_as_the_account_the_page_shows(self):
        with _running_api() as (_, base):
            self._fund(base)
            with _ui(self.store_path, base) as port:
                status, _, page = _request(port, "GET", f"/?account={OTHER}")
                self.assertEqual(status, 200)
                self.assertEqual(_active_account(page), OTHER,
                                 "the precondition: the page must show the other account")
                match = _HIDDEN_ACCOUNT.search(page)
                self.assertIsNotNone(
                    match,
                    "the page must carry the active account with the form that acts "
                    "on it; no `account` input was rendered")
                self.assertIn(OTHER, match.group(0),
                              f"the hidden input must carry the active account; "
                              f"got {match.group(0)!r}")

                boot_before = self._balance(base, BOOT)
                other_before = self._balance(base, OTHER)
                status, _, _ = _request(
                    port, "POST", "/send",
                    {"account": OTHER, "to_account_id": BOOT, "amount": "1.00"})
                self.assertEqual(status, 303, f"POST /send must answer 303; got {status}")

                self.assertEqual(
                    self._balance(base, OTHER), other_before - 100,
                    "a send carrying account= must debit THAT account, not the boot one")
                self.assertEqual(self._balance(base, BOOT), boot_before + 100,
                                 "and the boot account must be the payee, not the payer")

    def test_a_send_without_the_account_field_keeps_todays_behaviour(self):
        """The resolution rule's control: absent or empty means the boot account,
        byte for byte — which is what keeps every pre-existing reload test valid."""
        with _running_api() as (_, base):
            self._fund(base)
            with _ui(self.store_path, base) as port:
                boot_before = self._balance(base, BOOT)
                other_before = self._balance(base, OTHER)
                status, _, _ = _request(
                    port, "POST", "/send",
                    {"to_account_id": OTHER, "amount": "1.00"})
                self.assertEqual(status, 303, f"POST /send must answer 303; got {status}")
                self.assertEqual(self._balance(base, BOOT), boot_before - 100,
                                 "an absent account field must keep the boot account")
                self.assertEqual(self._balance(base, OTHER), other_before + 100)

    # -- 5. every POST still answers 303 and lands on a GET -------------------

    def test_every_ui_post_answers_303_and_lands_on_a_get(self):
        with _running_api() as (_, base):
            self._fund(base)
            cases = [
                ("/send", {"to_account_id": "", "amount": ""}),
                ("/retry", {}),
                ("/create", {"owner_id": "t14-owner", "currency": "USD",
                             "account_id": NEW}),
            ]
            with _ui(self.store_path, base) as port:
                for path, form in cases:
                    status, headers, body = _request(port, "POST", path, form)
                    self.assertIn(status, REDIRECTS,
                                  f"POST {path} must answer a redirect, not a page or a "
                                  f"status; got {status} with body {body[:120]!r}")
                    self.assertEqual(status, 303,
                                     f"POST {path} must answer 303 specifically; got {status}")
                    location = headers.get("Location") or ""
                    self.assertTrue(location,
                                    f"POST {path}'s 303 must carry a Location")
                    self.assertNotIn("<html", body.lower(),
                                     f"POST {path} must render no HTML on its own response")
                    follow, _, _ = _request(port, "GET", location)
                    self.assertEqual(follow, 200,
                                     f"POST {path} must land on a GET that answers 200; "
                                     f"{location!r} gave {follow}")

    def test_every_rendered_document_carries_the_demo_disclosure(self):
        """Every ``text/html`` document the UI serves carries ``DEMO_NOTICE``.

        This is a disclosure property, not a money invariant, and it is pinned
        because the plan resolved open registration as a *decision*: any visitor
        can mint an account into a live database, and the notice is what makes
        that acceptable on a public page. The requirement came from the
        frontend-engineer, not from this file's author — recorded so the
        provenance is not quietly re-attributed.

        The wording is not mine to own: the row imports ``app.web.DEMO_NOTICE``
        rather than re-spelling the sentence, so the implementer keeps the copy
        and this row keeps the placement.

        Restored under T18 after a withdrawal this file had honoured. The row
        that was said to cover it, ``app/selfcheck.py``'s, is not run by
        ``run_gate.sh``; it reaches the gate only incidentally, through
        ``tests/test_persist_ordering.py``'s blanket "no row failed" assertion.
        A named row with its own falsifier is the stronger home.
        """
        paths = ["/", f"/?account={OTHER}", "/?account=does-not-exist",
                 "/?error=invalid_amount"]
        with _running_api() as (_, base):
            self._fund(base)
            with _ui(self.store_path, base) as port:
                missing = _documents_missing_the_notice(port, paths)
        self.assertEqual(
            missing, [],
            "every rendered document must carry the unauthenticated-demo "
            f"disclosure; these did not: {missing}")

    def test_the_disclosure_row_fires_on_a_document_assembled_without_it(self):
        """The disclosure row's teeth: a document built without the notice must
        be caught, or the row above is a sentence that cannot fail."""
        paths = ["/", f"/?account={OTHER}", "/?account=does-not-exist"]
        with _running_api() as (_, base):
            self._fund(base)
            with mock.patch.object(app_web, "_document", _document_without_the_notice):
                with _ui(self.store_path, base) as port:
                    # The mutant's premise, asserted rather than assumed: without
                    # this, an inert mutation makes the guard's silence look like
                    # a pass. (It did exactly that on the first draft.)
                    _, _, raw = _request(port, "GET", "/")
                    self.assertNotIn(
                        app_web.DEMO_NOTICE, raw,
                        "the mutation must actually be in force, or the guard "
                        "reporting nothing would read as a pass")
                    missing = _documents_missing_the_notice(port, paths)
        self.assertEqual(
            sorted(missing), sorted(paths),
            "a document assembled without the disclosure must be reported for "
            f"every document route; the guard reported {missing}")

    # -- rule 3 on the pending block: the page must say, not imply -------------

    def test_a_pending_transfer_names_the_account_that_sent_it(self):
        """On B's page, A's unconfirmed transfer must not read as B's.

        ``_pending_block`` lists every record in the store unfiltered, so a page
        can show a transfer the viewer is not party to. What makes that honest is
        the sender being named in the record's own item. Asserting on the item
        rather than the page is deliberate: an id somewhere on the document —
        an activity counterparty, the account label, the hidden form field —
        would satisfy a whole-page search while saying nothing about the record.
        """
        with _running_api() as (_, base):
            self._fund(base)
            record = self._fund_store(base)          # from BOOT to OTHER
            self.assertNotEqual(record.from_account_id, OTHER,
                                "the fixture must send FROM the account that is not "
                                "on screen, or the row proves nothing")
            with _ui(self.store_path, base) as port:
                status, _, page = _request(port, "GET", f"/?account={OTHER}")
                self.assertEqual(status, 200, f"the switch must work first; got {status}")
                self.assertEqual(_active_account(page), OTHER,
                                 "the page must be showing the OTHER account, not the "
                                 "sending one")
                items = _pending_items(page)
                self.assertEqual(
                    len(items), 1,
                    "the unconfirmed record must render as exactly one item; got "
                    f"{items}")
                missing = _pending_items_missing_the_sender(page, BOOT)
        self.assertEqual(
            missing, [],
            "an unconfirmed transfer rendered on a page showing another account must "
            "name the account it is sent FROM; this item did not: "
            f"{missing and missing[0]!r}")

    def test_the_pending_sender_row_fires_on_a_block_rendered_without_it(self):
        """The pending-sender row's teeth, against the pre-fix renderer.

        Without this the row above could be satisfied by the sender id arriving
        from anywhere on the item; the mutant is the renderer as it stood before
        the fix, and the guard has to reject it.
        """
        with _running_api() as (_, base):
            self._fund(base)
            self._fund_store(base)
            with mock.patch.object(app_web, "_pending_block",
                                   _pending_block_without_the_sender):
                with _ui(self.store_path, base) as port:
                    _, _, page = _request(port, "GET", f"/?account={OTHER}")
                    items = _pending_items(page)
                    self.assertEqual(
                        len(items), 1,
                        "the mutation must still render an item, or the guard's "
                        f"silence would read as a pass; got {items}")
                    self.assertNotIn(
                        BOOT, items[0],
                        "the mutation must actually be in force — the pre-fix "
                        f"renderer named no sender; item={items[0]!r}")
                    missing = _pending_items_missing_the_sender(page, BOOT)
        self.assertEqual(
            len(missing), 1,
            "a pending item rendered without its sender must be reported; the guard "
            f"reported {missing}")

    def test_an_unrouted_post_commits_a_response_and_mints_nothing(self):
        """Row (7): the narrowed rule 4, placed beside the row that over-claimed it.
        "Every POST answers 303 and lands on a GET" was an over-claim: the PRG rule
        belongs to the routes that can **mint a key or move money** (`/send`,
        `/retry`, `/create`). An unrouted POST can do neither, so it is allowed to
        answer plain honest HTTP. What is *not* allowed — on any path — is exiting
        without committing a response, because a dead connection leaves the browser
        on the POST, and that is the state the exit guard exists to prevent.

        So the two halves here are: a response was committed (not a dead socket),
        and the money-adjacent part, which is the store — untouched, and no key
        minted. This row is green today and is a guard, not a red-first row.
        """
        with _running_api() as (_, base):
            self._fund(base)
            seeded = self._fund_store(base)
            before = _store_state(self.store_path)
            with _ui(self.store_path, base) as port:
                try:
                    status, _, body = _request(port, "POST", "/no-such-route",
                                               {"anything": "1"})
                except (OSError, http.client.HTTPException) as exc:
                    raise AssertionError(
                        f"an unrouted POST must still commit a response; the "
                        f"connection died instead ({exc!r}), which leaves the "
                        f"browser on the POST") from exc
                self.assertTrue(
                    400 <= status < 500,
                    f"an unrouted POST is a committed client error, not {status}; "
                    f"body {body[:120]!r}")
            self.assertEqual(_store_state(self.store_path), before,
                             "an unrouted POST must not touch the pending store")
            self.assertEqual([rec.key for rec in self._records()], [seeded.key],
                             "an unrouted POST must mint no idempotency key")


if __name__ == "__main__":
    unittest.main()
