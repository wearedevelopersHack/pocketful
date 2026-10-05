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
import inspect
import json
import os
import re
import shutil
import sqlite3
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock
from urllib.parse import quote, urlsplit

import app.client as app_client
from app.client import ApiClient
from app.keystore import PendingStore
from app.money import format_minor
from app.wallet import Wallet
import app.web as app_web

from ledger import OPENING_GRANT_MINOR, SYSTEM_ACCOUNT_ID
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
_ACCOUNT_LABEL = re.compile(r'<code class="addr"[^>]*>([^<]+)</code>', re.I)
"""Read from the element that carries the address (``app/design.py``'s
``wallet_header``): ``<code class="addr" title="Account ID">…</code>``. Re-cut
2026-10-04 with the redesign, which moved the address out of the old
``Account <code>…</code>`` line; the subject is unchanged — the element that says
which account this page is showing, not any id that happens to appear."""

# The balance the page displays, read from the element that carries it. Same
# reason as _ACCOUNT_LABEL: a bare ``assertIn("$100.00", page)`` can be satisfied
# by a figure in the activity feed or the send form, which is not "the page shows
# this account's balance".
_BALANCE = re.compile(r'<p class="balance">(?:<span[^>]*></span>)?\s*([^<]*)</p>')
"""Re-cut 2026-10-04: the element is now ``<p class="balance">`` and carries an
empty ``<span class="cur">`` before the figure (the class hook the stylesheet
targets, emitted with ``aria-hidden``), so the reader skips the span rather than
the whole element being renamed away."""

# A create refusal is rendered **inside the create card** (``app/design.py``'s
# ``create_form``), beside the fields it is about — not as a page banner. Two
# scopes are therefore needed to read it, and both are load-bearing:
#
#   * ``banner--error`` alone is not discriminating. ``design.banner`` emits the
#     same class for page-level refusals, so a document-wide search for the class
#     can return a *send* refusal's banner just as easily.
#   * a document-wide search for the **address** is green on the broken tree for
#     a third, quieter reason: ``wallet_header`` renders
#     ``<code class="addr" title="…">`` and the title attribute carries the
#     active account's id. ``assertIn(derived, page)`` is satisfied by that
#     attribute, by an activity row's counterparty, or by the payee of a pending
#     transfer — none of which is "the refusal named the address".
#
# So read the section, then the banner inside it, then the text node. A create
# card has exactly one ``<section>``, so the non-greedy close is unambiguous.
_CREATE_CARD = re.compile(r'<section\b[^>]*\bid="create"[^>]*>(.*?)</section>',
                          re.I | re.S)
_CARD_ERROR_TEXT = re.compile(
    r'<p\b[^>]*class="banner banner--error"[^>]*>.*?<span>([^<]*)</span>',
    re.I | re.S)


def _create_card_error(page):
    """The create card's error-banner text, or ``None`` when it carries none."""
    card = _CREATE_CARD.search(page)
    if card is None:
        return None
    found = _CARD_ERROR_TEXT.search(card.group(1))
    return found.group(1) if found else None


def _handle_create_without_the_parameter():
    """MUTANT: the landed ``_handle_create``, minus the ``&taken_id=`` echo.

    Filtered out of ``inspect.getsource`` of the real method rather than retyped,
    for the same reason ``_unquoted`` is: a hand-reconstruction of the pre-fix
    bytes would have no witness but its author's memory, and would stop being a
    mutant the moment the landed body changed shape around it.

    The premise that it really removed the line is the falsifier's job, not this
    function's — a filter that matched nothing would otherwise return the
    *unmutated* method and the falsifier would go green on a no-op.
    """
    source = textwrap.dedent(
        inspect.getsource(app_web.WalletUIHandler._handle_create))
    kept = [line for line in source.splitlines() if "taken_id" not in line]
    namespace = dict(vars(app_web))
    exec("\n".join(kept), namespace)
    return namespace["_handle_create"]


def _refusal_transport_problems(location, derived):
    """Why the refusal failed to **carry** ``derived``; ``[]`` when it does.

    The other half of the composition, and a separate predicate on purpose. The
    banner can only name the address because the redirect carried it, so the two
    halves are causally chained — but they fail independently: the pre-fix
    template reds the banner alone, while dropping ``&taken_id=`` reds this one
    (and, through the chain, the banner too). Two predicates make the report say
    *which* link broke instead of "the refusal is wrong".

    ``new_id`` is asserted empty here as well: filling it from the derived
    address would turn a blank-address resubmit into an explicit-id create and
    hand the next request to the ``account_exists`` sibling, so the branch this
    row is about would stop being reachable from the control that reached it.
    """
    query = urllib.parse.parse_qs(urlsplit(location).query, keep_blank_values=True)
    problems = []
    if query.get("error") != ["name_taken"]:
        problems.append(f"a blank-address collision is the address refusal; the "
                        f"redirect says {location!r}")
    if query.get("taken_id") != [derived]:
        problems.append(f"the refusal's subject must travel in its own parameter "
                        f"and be the address that collided ({derived!r}); the "
                        f"redirect says {location!r}")
    if query.get("new_id") != [""]:
        problems.append(f"nothing was typed into the address field, so the echo "
                        f"must leave it blank; the redirect says {location!r}")
    return problems


def _refusal_problems(page, derived):
    """Why the create card's refusal fails to name ``derived``; ``[]`` if it does.

    One predicate, used by the row and by its falsifier, so the falsifier cannot
    be a second, weaker paraphrase of the row: it is the row's own condition.
    """
    message = _create_card_error(page)
    if message is None:
        return ["the create card carries no error banner, so the refusal is not "
                "rendered in the control it is about"]
    if derived not in message:
        return [f"the refusal does not name the address it collided with "
                f"({derived!r}); the banner says {message!r}"]
    return []


_REAL_NAME_TAKEN_TEMPLATE = app_web._NAME_TAKEN_TEMPLATE
"""The landed template, captured before the falsifier replaces it.

The falsifier cannot read ``app_web._NAME_TAKEN_TEMPLATE`` while its patch is
held — that name *is* the mutant — so the genuine object is captured at import,
the same technique ``_REAL_DOCUMENT`` uses below.
"""

_PRE_FIX_NAME_TAKEN_COPY = ("That name is already taken. Nothing was created — "
                            "pick another.")
"""The refusal's copy as it read **before** the address was carried to the render
site: the static ``_ERROR_MESSAGES["name_taken"]`` sentence, with no address and
no placeholder to format one in.

This is the mutant's shape, not a paraphrase of it. The pre-fix render site had
no ``taken_id`` lookup at all and answered this string unconditionally; patching
the template to this placeholder-free sentence reproduces exactly that rendering
while leaving every other line of the landed code in place — so the red below is
attributable to the missing address and not to a broken page.
"""


# -- the id is opaque: it has to survive the trip into a URL path --------------
#
# ``app/client.py`` builds ``/accounts/{id}/balance`` and ``/accounts/{id}/activity``
# by interpolation, and an account id is not a URL path segment. Three shapes
# come out of getting that wrong, and only one of them is "no answer":
#
#   * a space or a control character -> ``http.client.InvalidURL`` raised inside
#     urllib, so the request is never sent and the socket closes unanswered;
#   * ``/``, ``?`` or ``#`` -> the request *is* sent, but the path is re-cut so
#     it lands on a different route (a 404, or a different account);
#   * ``%`` -> the request is sent and the server decodes the segment once more
#     than the caller meant, so an id that looks like an encoding of another id
#     **reads that other account, silently, with a 200**.
#
# The third is the one worth a row that fails on ``owner_id``: an assertion like
# "an answer came back" is green on it, and a mutant that produced only
# ``InvalidURL`` would satisfy "the row went red" while still serving someone
# else's balance.
#
# The fix is ``quote(account_id, safe="")`` at the two call sites, and it stays
# **client-side**: ``%20`` in a path segment *is* a space (RFC 3986), so decoding
# it on the server is correct and "hardening" the parser against it would be a
# regression, not a fix.

_PLAIN_ID = "acct a b"
_ENCODED_ID = "acct%20a%20b"
"""Two accounts that are each other's trap: ``_ENCODED_ID`` is not the encoding
of ``_PLAIN_ID`` as a *string* — it is a distinct 11-character id that only
becomes ``_PLAIN_ID`` when a URL decoder runs over it one extra time."""

_FUNDER_A, _FUNDER_B = "acct-funder-a", "acct-funder-b"
_PLAIN_AMOUNT, _ENCODED_AMOUNT = 111, 222

# (id, owner, the funder that paid it, the amount it was paid) — the id is read
# back through both client methods, and each must answer with its own account.
# The two lookalikes are funded from **different** accounts so an activity read
# that retargeted onto one of them is distinguishable from the other's.
#
# Every account also has an opening-grant row from ``__system__``, so "the right
# activity" is not "exactly one row": it is *this account's funding row present
# and the other lookalike's absent*.
_LOOKALIKES = ((_PLAIN_ID, "alice", _FUNDER_A, _PLAIN_AMOUNT, _FUNDER_B),
               (_ENCODED_ID, "mallory", _FUNDER_B, _ENCODED_AMOUNT, _FUNDER_A))


def _ownership_problems(answer, account_id, owner):
    """Why a balance read is not ``account_id``'s own; ``[]`` when it is.

    Keyed on ``account_id`` and ``owner_id`` rather than on the amount. A
    retarget that happened to land on an account with the same balance would
    slip past an amount check; it cannot slip past the identity.
    """
    problems = []
    if not isinstance(answer, dict):
        return [f"the read answered {answer!r}, not a balance object"]
    if answer.get("account_id") != account_id:
        problems.append(f"answered for {answer.get('account_id')!r}, "
                        f"not for {account_id!r}")
    if answer.get("owner_id") != owner:
        problems.append(f"answered as owner {answer.get('owner_id')!r}, "
                        f"not {owner!r}")
    return problems


def _activity_problems(answer, counterparty_owner, amount_minor, other_owner):
    """Why an activity read is not the expected account's; ``[]`` when it is.

    The counterparty owner is the discriminator: the two lookalikes are funded
    from different accounts, so a read that retargeted onto one of them carries
    the *other* one's funding row. Presence and absence are both checked —
    asserting only that this account's row is present would pass on a read that
    returned another account's rows alongside it.
    """
    items = answer.get("items") if isinstance(answer, dict) else None
    if not isinstance(items, list) or not items:
        return [f"the activity read returned no rows: {items!r}"]
    pairs = [(item.get("counterparty_owner_id"), item.get("amount_minor"))
             for item in items]
    problems = []
    if (counterparty_owner, amount_minor) not in pairs:
        problems.append(f"this account's own funding row is missing: expected one "
                        f"from {counterparty_owner!r} for {amount_minor}; got {pairs!r}")
    if any(owner == other_owner for owner, _ in pairs):
        problems.append(f"the read carries {other_owner!r}'s funding row, so it "
                        f"answered for the other account: {pairs!r}")
    return problems


def _unquoted(method_name):
    """MUTANT: one landed client method, with ``quote`` replaced by the identity.

    Built by ``inspect.getsource`` of the real method rather than retyped, so it
    cannot drift from the body it is supposed to be mutating — a reconstruction
    of the pre-fix bytes would have no witness but its author's memory, and it
    would silently stop being a mutant the moment the landed body changed shape.

    It reverts exactly **one** site: only the named method is exec'd, so its
    sibling keeps its quoting. That is what keeps the detection attributable —
    reverting both at once (patching ``app.client.quote``) reds two rows at the
    same instant and says nothing about which belongs to which.
    """
    namespace = dict(vars(app_client))
    namespace["quote"] = lambda value, safe="": value
    source = textwrap.dedent(
        inspect.getsource(getattr(app_client.ApiClient, method_name)))
    exec(source, namespace)
    return namespace[method_name]


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
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/accounts/nobody/balance", timeout=1):
                pass
            return
        except urllib.error.HTTPError as exc:
            # An HTTPError wraps the response fp (urllib's addbase subclasses
            # tempfile._TemporaryFileWrapper); leaving it unclosed defers the
            # close to GC and emits a ResourceWarning. Close it here so the
            # warning channel stays meaningful.
            exc.close()
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
        try:
            return exc.code, dict(exc.headers), exc.read().decode()
        finally:
            exc.close()  # see _wait_until_answering: unclosed -> ResourceWarning


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
            "the page names no active account (no `<code class=\"addr\">` element), "
            "so it cannot be shown to be rendering the requested one")
    return match.group(1)


def _balance_text(page):
    """The money the page displays, or a loud failure. See ``_BALANCE``."""
    match = _BALANCE.search(page)
    if match is None:
        raise AssertionError(
            "the page renders no balance element (`<div class=\"balance\">`), so "
            "the figure on it cannot be attributed to the account it names")
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
            try:
                return exc.code, exc.read()
            finally:
                exc.close()  # unclosed HTTPError -> ResourceWarning at GC


class _SubstitutedBalance:
    """Real HTTP, except one account's balance read answers with a substitute.

    The point is not to break the API — every other request, including the create
    itself and the activity read that follows it, goes over the real wire. It is
    to make the *server* say a number nobody could have guessed from the request,
    so "the page shows $100.00" can be told apart from "the page echoes the
    server". A renderer that prints the grant (or any constant) shows the same
    figure either way; only a renderer that reads the answer follows.
    """

    def __init__(self, account_id, balance_minor, currency="USD"):
        self.account_id = account_id
        self.balance_minor = balance_minor
        self.currency = currency

    def __call__(self, method, url, body, headers):
        # Expect the **quoted** path, because that is what the client actually
        # sends: an account id is opaque and ``app/client.py`` quotes it on the
        # way into the URL (``pocketful-id-in-url-path-unencoded``). This is not a
        # weakened check — it is the same equality, with the fake on the same
        # terms as the wire. Left unquoted it would compare against a path the
        # client no longer sends for any non-safe id, silently *not* intercept,
        # and forward that read over the real wire; the row using this fake would
        # then be judging the server's answer while believing it was judging the
        # substitute. The fixture id here is a legal path segment, so the two
        # spellings coincide today and a diff of this line alone would look like
        # a no-op — which is exactly why the reason is written down rather than
        # the equality being loosened to a ``in url``.
        if (method == "GET"
                and urlsplit(url).path
                == f"/accounts/{quote(self.account_id, safe='')}/balance"):
            return 200, json.dumps({
                "account_id": self.account_id,
                "balance_minor": self.balance_minor,
                "currency": self.currency,
            }).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read()
            finally:
                exc.close()  # unclosed HTTPError -> ResourceWarning at GC


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


# -- C3.7: the welcome is a ledger ROW, not a note -----------------------------

_WELCOME = re.compile(r"welcome", re.I)
"""C3.7's forbidden copy, case-insensitive — the plan's own wording: "the
create-success document contains no occurrence of ``welcome`` (case-insensitive)"."""


def _welcome_occurrences(document):
    """Every occurrence of the forbidden grant announcement in ``document``.

    Returns short snippets, empty when there are none. A **returned measurement**
    rather than an assertion, so the row that requires the absence and the
    falsifier that requires the presence make the *same* measurement — a
    falsifier that re-spelled the search would be evidence about the falsifier.
    """
    return [document[max(0, m.start() - 40):m.end() + 40].replace("\n", " ")
            for m in _WELCOME.finditer(document)]


_REAL_CREATE_FORM = app_web.create_form
"""Captured before the mutant replaces the module global, for the same reason as
``_REAL_DOCUMENT``: a mutant that reached the original through the name it just
replaced would call itself."""

_WELCOME_COPY = "New accounts start with a $100.00 Welcome bonus."
"""The copy C3.7 rules out, in the shape C3.7(a) names: a note on ``create_form``.
Injected through the component that actually renders the form, so the mutant
travels the real create path rather than a reconstruction of it."""


def _create_form_with_welcome_copy(*, action, name_value, address_value, error=None):
    """MUTANT: C3.7's defect (a), reintroduced into the real create form.

    ``app/design.py:138`` records that ``WELCOME_BONUS_LABEL`` was deleted rather
    than left as dead copy; this puts an announcement back on the form the create
    page renders, which is the only seam through which a served document could
    acquire a "welcome" string without the ledger being involved at all.
    """
    return _REAL_CREATE_FORM(action=action, name_value=name_value,
                             address_value=address_value, error=error) + (
        f'<p class="hint">{_WELCOME_COPY}</p>')


# -- the pending item names its sender -----------------------------------------

_REAL_PENDING_BLOCK = app_web.pending_block
"""Captured for the same reason as ``_REAL_DOCUMENT``: the mutant must not reach
the behaviour it replaced through the name that now points at the mutant.

Renamed with the renderer (frontend-engineer, 2026-10-04): the component moved to
``app/design.py`` and ``app/web.py`` now imports it as ``pending_block``. The
seam is unchanged in kind — ``app/web.py`` still looks the name up on its own
module at call time, so rebinding ``app_web.pending_block`` still substitutes the
renderer for the whole page."""


def _pending_block_without_the_sender(pending):
    """MUTANT: the renderer as it was before T18's fix — the sender named nowhere.

    The defect is unchanged by the redesign: the item named only the payee, so on
    B's page a record whose ``from_account_id`` is A read as B's own. The *shape*
    the reconstruction removes changed with the component (``app/design.py``):
    the pre-redesign ``app/web.py`` item read
    ``send <amount> to <to_account_id> (not confirmed)`` and the fix added
    ``from <from_account_id>``; the component now renders ``<sender> → <payee>``
    in one span, so the sender is dropped from there.

    Shape, and it is the rule this file now follows for every mutant: **call the
    object captured before the rebinding, never the attribute you rebound.** This
    calls ``_REAL_PENDING_BLOCK``, so the mutant is the genuine renderer with one
    phrase removed — every other rendered field stays byte-real, and a kill is
    attributable to the missing sender alone rather than to a second difference
    the mutant introduced.

    This is a **reconstruction** (there is no version control on this machine, so
    the pre-redesign bytes are gone): faithfulness is a reviewer's judgment item,
    not the author's. The row below asserts the mutant actually rendered and that
    the sender really is gone, so an inert mutant reports as a failure rather than
    as a pass.
    """
    html = _REAL_PENDING_BLOCK(pending=pending)
    for rec in pending:
        html = html.replace(f"{rec.from_account_id} → ", "")
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
        # The pending item's shape after the redesign (app/design.py): one <li>
        # carrying amount, who and when spans. Activity rows are <tr>, so this
        # cannot pick up a settled row; the old discriminator was the phrase
        # "(not confirmed)", which the component now says once outside the list.
        if all(marker in item for marker in ('class="amt"', 'class="who"',
                                             'class="when"')):
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
        # ``with sqlite3.connect(...) as conn`` commits a transaction but does
        # NOT close the connection — sqlite3's context manager is not a file
        # closer. Wrapping in closing() makes the ``with ... as conn`` at the
        # call sites actually close, instead of leaking to GC (ResourceWarning).
        return contextlib.closing(
            sqlite3.connect(f"file:{db_path}?mode=ro", uri=True))

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
                # T20: and it shows the new account's MONEY, taken from the
                # server — a page landed on after a create that still displayed
                # the boot account's balance would be showing the wrong account's
                # money while naming the right one.
                self.assertEqual(_balance_text(page), "$100.00",
                                 "the landed-on page must show the new account's "
                                 "opening grant, rendered from 10000 minor units")

            # T20: creating a USD account mints a BALANCED opening grant from the
            # named __system__ account. So the row's name still holds — nothing is
            # minted and I1/I2 below are untouched — but the derived balance is the
            # grant, not 0, and the account now has exactly one activity row: it.
            self.assertEqual(self._balance(base, NEW), OPENING_GRANT_MINOR,
                             "a brand-new account's balance is the opening grant "
                             "minted from __system__, not 0")
            activity = ApiClient(base).list_activity(NEW)["items"]
            self.assertEqual(len(activity), 1,
                             "a brand-new account's only ledger activity is its "
                             "opening grant")
            self.assertEqual(activity[0]["counterparty_account_id"], SYSTEM_ACCOUNT_ID,
                             "the grant's counterparty must be the named __system__ "
                             "account, so the balanced pair is identifiable")
            # ... and nothing was minted: the funder is untouched (the grant comes
            # from __system__, not from the funder), and I2 below stays 0.
            self.assertEqual(self._balance(base, FUNDER), funder_before,
                             "creating an account must move no money from the funder")
            with self._ledger(db_path) as conn:
                self.assertEqual(entry_count(conn), entries_before + 2,
                                 "creating an account writes exactly the grant's "
                                 "balanced pair — two entries — and nothing else")
                self.assertEqual(idempotency_row_count(conn), idempotency_before,
                                 "the grant is posted directly, not through the keyed "
                                 "transfer path, so the create mints no idempotency "
                                 "key; this is a DELTA around the create, because the "
                                 "fixture's own transfers legitimately hold keys")
                assert_i1(conn)
                assert_i2(conn)
            # ... and the browser minted nothing either: the store is the evidence.
            self.assertEqual(_store_state(self.store_path), before_store,
                             "creating an account must not touch the pending store")
            self.assertEqual([rec.key for rec in self._records()], before_keys,
                             "creating an account must mint no idempotency key")

    def test_the_rendered_balance_comes_from_the_server_not_from_a_constant(self):
        """The claim above — the page shows ``$100.00`` — is only evidence if the
        page would show something *else* when the server says something else.

        So the server is made to answer 4242 minor units for the new account. The
        page landed on after the create must render ``$42.42``. A template that
        printed the grant — or any constant — shows ``$100.00`` here and fails
        this row, while every row above it stays green; that difference is exactly
        what "from the server value" buys, and nothing else in this file tests it.

        Read through ``_balance_text``, not ``assertIn``: the grant's own activity
        row legitimately renders ``$100.00`` in the feed, so a substring search
        would be satisfied by the wrong element — and by a page showing the wrong
        balance.
        """
        with _running_api() as (_, base):
            self._fund(base)
            with _ui(self.store_path, base,
                     transport=_SubstitutedBalance(NEW, 4242)) as port:
                status, headers, _ = _request(
                    port, "POST", "/create",
                    {"owner_id": "t14-owner", "currency": "USD", "account_id": NEW})
                self.assertEqual(status, 303,
                                 f"POST /create must answer 303; got {status}")
                follow, _, page = _request(port, "GET", headers.get("Location") or "")
                self.assertEqual(follow, 200,
                                 f"the 303 target must be a GET answering 200; got "
                                 f"{follow}")
                self.assertEqual(_active_account(page), NEW,
                                 "the page must still be showing the new account")
                self.assertEqual(_balance_text(page), "$42.42",
                                 "the balance element must carry the number the "
                                 "server answered with, not the grant the create "
                                 "implies and not a constant")

    def test_the_create_success_document_announces_no_welcome(self):
        """C3.7 as the negative the plan says is checkable: the create-success
        document contains no occurrence of "welcome", case-insensitive.

        The rule (plan §C3.7, ratified as ``DESIGN-SPEC`` §5.8 option (c) on
        2026-10-04) is that the welcome is a ledger **row**, not a note: the user
        learns the $100 the way they learn every other movement, from the activity
        row ``Received from Pocketful  +$100.00``, which is true on every render.
        This row therefore does not assert that the grant is *shown* —
        ``test_activity_counterparty.py`` owns the grant's own row — it asserts the
        absence of an announcement *beside* it. Two ways of breaking it were
        already ruled out and are not re-opened here: a "starts with $100.00" note
        on ``create_form`` is false for a EUR choice (the grant is USD-only), and
        keying copy on ``counterparty_account_id == "__system__"`` couples visible
        text to a reserved ledger internal.

        It is a negative, so it can pass for the wrong reason, and the guards are
        built in rather than assumed: the landed-on document must be the **new
        account's real page** — ``_active_account`` reads the address element and
        the balance element must render the grant — so an error page or a stub
        body cannot be "no welcome" by vacuity. The falsifier below drives this
        same predicate against a document that carries the copy, so the row is not
        a sentence that is always true.

        Scope, stated because the plan's sentence is broader than this row. The
        plan says "no ``welcome`` copy on any render path"; this row checks the
        **create-success document** — the document the 303 lands on, which is the
        named checkable form — and the wallet GETs listed below, because
        ``create_form`` is rendered on all of them and the ruled-out defect (a) is
        a note on that component. It does **not** cover the stdlib error pages
        that never pass through ``_document`` (the blind spot
        ``test_every_document_the_ui_builds_carries_the_demo_disclosure`` names),
        nor any HTML that does not come from ``app/design.py``'s components.

        One more boundary, found by writing this row rather than by reading the
        plan: the plan's checkable form is a **raw substring search over the whole
        document**, and the create-success document echoes the name the creator
        typed. The first draft of this row used the fixture name
        ``t14-welcome-owner`` and went red on correct code — the "occurrence" it
        found was the user's own account name, in the ``<h1>`` and in the
        switcher. So the negative is a property of the app's **copy**, and the
        fixture has to keep the word out of its own data for the measurement to be
        about the app at all. The name is deliberately neutral (``t14-owner``) and
        that is stated here rather than left as an accident of the fixture; a
        creator who names their account "welcome" would still redden the raw
        search, which is a fact about the check's shape and is raised as such.
        """
        with _running_api() as (_, base):
            self._fund(base)
            with _ui(self.store_path, base) as port:
                status, headers, _ = _request(
                    port, "POST", "/create",
                    {"owner_id": "t14-owner", "currency": "USD",
                     "account_id": NEW})
                self.assertEqual(status, 303,
                                 f"POST /create must answer 303; got {status}")
                location = headers.get("Location") or ""
                follow, _, landed = _request(port, "GET", location)
                self.assertEqual(follow, 200,
                                 f"the 303 target must be a GET answering 200; "
                                 f"{location!r} gave {follow}")
                self.assertEqual(
                    _active_account(landed), NEW,
                    "the create-success document must be the new account's page, or "
                    "the negative below is asserted over the wrong document")
                self.assertEqual(
                    _balance_text(landed), format_minor(OPENING_GRANT_MINOR, "USD"),
                    "the create-success document must render the new account's "
                    "money, so the negative is asserted over a document that "
                    "actually carries the grant it does not announce")
                landed_hits = _welcome_occurrences(landed)
                other_hits = {
                    path: _welcome_occurrences(_request(port, "GET", path)[2])
                    for path in ("/", f"/?account={OTHER}", f"/?account={NEW}",
                                 "/?account=does-not-exist")
                }
        self.assertEqual(
            landed_hits, [],
            "C3.7: the create-success document must carry no 'welcome' copy — the "
            "welcome is the activity row, not an announcement beside it; found "
            f"{landed_hits}")
        self.assertEqual(
            {path: hits for path, hits in other_hits.items() if hits}, {},
            "C3.7: no wallet render path may carry 'welcome' copy; these did: "
            f"{ {path: hits for path, hits in other_hits.items() if hits} }")

    def test_the_no_welcome_row_fires_on_a_grant_announcement(self):
        """MUTANT: C3.7's defect (a) injected through the component that renders
        the create form, driven down the real create path.

        The mutant's **premise** is asserted before its effect: the injected copy
        must actually reach the served document. Without that, an inert mutation —
        a patch at a seam the page no longer renders, or a form the landed-on
        document no longer contains — would make the guard's silence read as a
        pass. This file has already recorded that failure once, on the disclosure
        mutant, so it is checked here rather than hoped for.

        Both halves of the row above are exercised: the landed-on document and one
        of the wallet GETs, since the patch lives in a component they all render.

        This patches a **module global** for the duration of one server (the
        technique obligation 13 uses); it is process-wide while held, so this row
        is not parallelizable.
        """
        with _running_api() as (_, base):
            self._fund(base)
            with mock.patch.object(app_web, "create_form",
                                   _create_form_with_welcome_copy):
                with _ui(self.store_path, base) as port:
                    status, headers, _ = _request(
                        port, "POST", "/create",
                        {"owner_id": "t14-owner", "currency": "USD",
                         "account_id": NEW})
                    self.assertEqual(status, 303,
                                     f"POST /create must answer 303; got {status}")
                    _, _, landed = _request(port, "GET", headers.get("Location") or "")
                    self.assertIn(
                        _WELCOME_COPY, landed,
                        "the mutation must actually be in force, or the guard "
                        "reporting nothing would read as a pass")
                    landed_hits = _welcome_occurrences(landed)
                    root_hits = _welcome_occurrences(_request(port, "GET", "/")[2])
        self.assertNotEqual(
            landed_hits, [],
            "a create-success document carrying the announcement C3.7 rules out "
            "must be reported by the predicate the row above uses; it found nothing")
        self.assertNotEqual(
            root_hits, [],
            "a wallet GET rendering the same announcement must be reported too; the "
            "predicate found nothing, so that half of the row above is toothless")

    # -- 1c. a taken *address* is refused by naming the address ---------------

    def _land_a_derived_address(self, port):
        """Create with a blank address and read back the address that landed.

        The id is read from the **landed page's own active-account element**, not
        re-derived here: re-deriving would make this fixture agree with a moved
        ``_derive_account_id`` by construction, and the row is about what the
        server created, not about what the test can compute.
        """
        status, headers, body = _request(
            port, "POST", "/create",
            {"owner_id": "Grace Hopper", "currency": "USD", "account_id": ""})
        self.assertEqual(
            status, 303,
            f"a create with a blank address must answer 303; got {status} with "
            f"body {body[:160]!r}")
        location = headers.get("Location") or ""
        landed, _, page = _request(port, "GET", location)
        self.assertEqual(landed, 200,
                         f"the 303 target must be a GET answering 200; got {landed}")
        derived = _active_account(page)
        self.assertTrue(derived, f"the landed page must name the account; got {location!r}")
        return derived

    def _collide_on_that_address(self, port):
        """A **different** name that derives the **same** address, blank address.

        ``"Grace  Hopper"`` is a second space: the name is free, the address it
        derives is not. This is the branch whose refusal has to name the address,
        and the reason it does — a sentence about the *name* would be describing
        something that is not taken.
        """
        return _request(port, "POST", "/create",
                        {"owner_id": "Grace  Hopper", "currency": "USD",
                         "account_id": ""})

    def test_a_taken_address_is_refused_by_naming_the_address(self):
        """#28. Two names, one derived address: the refusal must name the address.

        The composition, all three links read rather than assumed:

          * the redirect carries the address in its **own** parameter, so the
            refusal's subject survives the round trip;
          * the address it carries is the one the server actually created — read
            from the first create's landed page, so a derivation that moved
            cannot make this row pass by agreeing with itself;
          * the create card's banner, and not some other element of the page,
            renders it.

        Nothing here asserts a *sentence*. The copy is the owner's to change; the
        identity is the contract, so the row would survive a rewrite and still
        fail a refusal that named the wrong thing or named nothing.
        """
        with _running_api() as (_, base):
            self._fund(base)
            with _ui(self.store_path, base) as port:
                derived = self._land_a_derived_address(port)
                status, headers, body = self._collide_on_that_address(port)
                self.assertEqual(
                    status, 303,
                    f"the colliding create must be refused into a GET, not "
                    f"rendered on the POST; got {status} with body {body[:160]!r}")
                location = headers.get("Location") or ""
                landed, _, page = _request(port, "GET", location)

        self.assertEqual(landed, 200, "the refusal must land on a GET answering 200")
        self.assertEqual(
            _refusal_transport_problems(location, derived), [],
            "the refusal must carry the address it collided with, in its own "
            "parameter; "
            + "; ".join(_refusal_transport_problems(location, derived)))
        self.assertEqual(_refusal_problems(page, derived), [],
                         "the create card's refusal must name the address that "
                         "collided; " + "; ".join(_refusal_problems(page, derived)))

    def test_an_explicitly_taken_address_still_gets_the_account_refusal(self):
        """The sibling branch, so the row above cannot pass by flattening both.

        A *typed* taken address is a different refusal with a different subject:
        nothing was derived, so nothing about a name is true. Pinning it here is
        what makes the row above discriminating — a mutant that answered every
        ``account_exists`` with the address sentence would satisfy that row and
        red this one.
        """
        with _running_api() as (_, base):
            self._fund(base)
            with _ui(self.store_path, base) as port:
                status, headers, body = _request(
                    port, "POST", "/create",
                    {"owner_id": "t14-owner", "currency": "USD",
                     "account_id": BOOT})
                self.assertEqual(status, 303, f"POST /create must answer 303; got {status}")
                location = headers.get("Location") or ""
                self.assertIn("error=account_exists", location,
                              f"an explicitly taken id is the account refusal; "
                              f"got {location!r}")
                self.assertNotIn("taken_id", location,
                                 f"nothing was derived, so there is no address to "
                                 f"name; got {location!r}")
                landed, _, page = _request(port, "GET", location)

        self.assertEqual(landed, 200, "the refusal must land on a GET answering 200")
        message = _create_card_error(page)
        self.assertEqual(message, app_web._ERROR_MESSAGES["account_exists"],
                         "the explicit-id refusal keeps its own copy, in the same "
                         "create card")

    def test_the_taken_address_row_goes_red_without_the_address(self):
        """FALSIFIER: the pre-fix rendering, restored, reds the row above.

        The window for showing this row red on the *landed-then* bytes closed
        before it could be written — ``app/web.py a7d234d4`` landed at 09:50:45
        with the address already carried — so the red is delivered the durable
        way instead: an in-gate mutant that puts the pre-fix rendering back
        (``_PRE_FIX_NAME_TAKEN_COPY``, no placeholder, no lookup) and leaves
        every other line of the landed code in place.

        The mutant's **premise** is asserted before its effect, twice over,
        because two different things could make this red for the wrong reason:

          * the patch must be the object the render site reads, and must carry no
            ``{address}`` for ``str.format`` to fill in — otherwise the mutant
            would be inert (patching a name nobody looks up) or would inject the
            address itself and measure nothing;
          * the mutant must still reach the branch and render a banner. If the
            card came back with no banner at all, the row would go red on a
            broken page, and the failure would be attributable to nothing.
        """
        with mock.patch.object(app_web, "_NAME_TAKEN_TEMPLATE",
                               _PRE_FIX_NAME_TAKEN_COPY):
            self.assertIs(app_web._NAME_TAKEN_TEMPLATE, _PRE_FIX_NAME_TAKEN_COPY,
                          "the mutant must be the object the render site reads")
            self.assertNotIn("{address}", app_web._NAME_TAKEN_TEMPLATE,
                             "the pre-fix copy had no address to format in; a "
                             "placeholder here would let the mutant inject one")
            with _running_api() as (_, base):
                self._fund(base)
                with _ui(self.store_path, base) as port:
                    derived = self._land_a_derived_address(port)
                    _, headers, _ = self._collide_on_that_address(port)
                    location = headers.get("Location") or ""
                    _, _, page = _request(port, "GET", location)

            self.assertIn("error=name_taken", location,
                          "the mutant must still reach the address refusal, or the "
                          "premise below is untested")
            message = _create_card_error(page)
            self.assertEqual(
                message, _PRE_FIX_NAME_TAKEN_COPY,
                "the mutant must render its banner in the create card; a card with "
                "no banner reds the row above for a reason that is not the address")
            self.assertNotIn(derived, message,
                             "the mutant's premise: the pre-fix copy names no address")
            problems = _refusal_problems(page, derived)
            self.assertEqual(
                len(problems), 1,
                f"the row above, run as written against the pre-fix rendering, must "
                f"report exactly one problem; got {problems!r}")
            self.assertIn("does not name the address", problems[0],
                          f"and the problem must be the missing address; got {problems!r}")

    def test_the_taken_address_row_goes_red_when_the_parameter_is_dropped(self):
        """FALSIFIER (the other half): the redirect, with ``&taken_id=`` removed.

        The banner cannot name an address the redirect never carried, so this
        half is causally upstream of the one above — and until this row existed
        nothing showed that *either* half of the composition could fail. A link
        with no falsifier is a link whose assertion has never been observed
        failing, which is the same thing as not having one.

        The mutant is built from ``inspect.getsource`` of the landed
        ``_handle_create`` with the one ``taken_id`` line filtered out, so it
        cannot drift from the body it mutates. Two premises are asserted before
        the effect: the mutant must still reach the address refusal (or the row
        below measures a different branch), and it must still render a create-card
        banner (or the red is a broken page, not a dropped parameter).

        Because the halves are chained, this mutant reds the banner predicate
        too. That is stated rather than engineered away: the *transport*
        predicate is the one the red is attributed to, and the chained red is
        what "the banner can only name what the redirect carried" means.
        """
        mutant = _handle_create_without_the_parameter()
        landed_method = app_web.WalletUIHandler._handle_create
        with mock.patch.object(app_web.WalletUIHandler, "_handle_create", mutant):
            self.assertIs(app_web.WalletUIHandler._handle_create, mutant,
                          "the mutant must be the method the handler dispatches to")
            self.assertNotEqual(
                mutant.__code__.co_code, landed_method.__code__.co_code,
                "the mutant must differ from the landed method: the filter that "
                "builds it returns the original if it matches nothing, and a "
                "no-op mutant would make the red below unreachable")
            with _running_api() as (_, base):
                self._fund(base)
                with _ui(self.store_path, base) as port:
                    derived = self._land_a_derived_address(port)
                    _, headers, _ = self._collide_on_that_address(port)
                    location = headers.get("Location") or ""
                    _, _, page = _request(port, "GET", location)

            self.assertIn("error=name_taken", location,
                          "the mutant must still reach the address refusal, or the "
                          "output below measures a different branch")
            self.assertIsNotNone(
                _create_card_error(page),
                "the mutant must still render a create-card banner, or the red is a "
                "broken page rather than a dropped parameter")
            problems = _refusal_transport_problems(location, derived)
            self.assertEqual(
                len(problems), 1,
                f"the row above, run as written against a redirect that dropped its "
                f"parameter, must report exactly one transport problem; got "
                f"{problems!r}")
            self.assertIn("own parameter", problems[0],
                          f"and the problem must be the missing parameter; got "
                          f"{problems!r}")
            self.assertNotEqual(
                _refusal_problems(page, derived), [],
                "and the chained half must red with it: a banner cannot name an "
                "address the redirect did not carry")

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

    def test_every_document_the_ui_builds_carries_the_demo_disclosure(self):
        """Every document built through ``app/web.py``'s ``_document`` carries
        ``DEMO_NOTICE`` — here, the four wallet routes listed below, each of
        which renders through it.

        Scope, stated because the previous name over-claimed it. The name used to
        read "every ``text/html`` document the UI serves", which is false:
        ``WalletUIHandler`` does not override ``BaseHTTPRequestHandler.send_error``,
        so the stdlib emits its own ``text/html`` error pages that never pass
        through ``_document`` and carry no notice. Measured on this tree
        (2026-10-04): ``PUT``/``DELETE``/``PATCH /`` answer ``501`` with
        ``Content-Type: text/html;charset=utf-8`` and zero markers; a malformed
        request line answers ``400`` and an unsupported HTTP version answers
        ``505``, both stdlib pages with zero markers. Closing that hole means
        overriding ``send_error``/``error_message_format`` in ``app/`` — not this
        row's work, and deliberately not asserted here. The claim is therefore the
        covered set: documents the UI itself assembles, sampled at the routes
        below. The uncovered emitter is ``BaseHTTPRequestHandler.send_error``.

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

        ``pending_block`` lists every record in the store unfiltered, so a page
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
            with mock.patch.object(app_web, "pending_block",
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


class AccountIdsAreOpaqueInAPath(unittest.TestCase):
    """#27. Every id the API accepted must be addressable by the id it accepted.

    Real HTTP against a real server, and the ids are created *through* the API,
    so the row is about the contract the API already made rather than about a
    string this file chose. Two accounts are created that are each other's trap:
    the client's two interpolating methods are both exercised, because a single
    unquoted site is enough to serve the wrong account and a row that only
    checked one would leave the other unpinned.
    """

    def _lookalikes(self, base):
        """Fixture: the two lookalikes, each funded from its own account."""
        client = ApiClient(base)
        client.create_account(owner_id="alice", currency="USD", account_id=_PLAIN_ID)
        client.create_account(owner_id="mallory", currency="USD", account_id=_ENCODED_ID)
        for funder, account_id, amount, key in (
                (_FUNDER_A, _PLAIN_ID, _PLAIN_AMOUNT, "t41-plain"),
                (_FUNDER_B, _ENCODED_ID, _ENCODED_AMOUNT, "t41-encoded")):
            client.create_account(owner_id=funder, currency="USD",
                                  allow_overdraft=True, account_id=funder)
            client.send_transfer(idempotency_key=key, from_account_id=funder,
                                 to_account_id=account_id, amount_minor=amount,
                                 currency="USD")
        return client

    def _read(self, client, method, account_id):
        """A client read, with a transport failure reported as what it means.

        A dead request and a wrong answer are different defects, and letting the
        raw ``InvalidURL`` escape would report this row as an *error*, which
        reads like a broken fixture rather than a broken contract.
        """
        try:
            return getattr(client, method)(account_id)
        except Exception as exc:
            raise AssertionError(
                f"{method}({account_id!r}) must answer: the API accepted this id, "
                f"so it is a legal account id and has to survive becoming a URL "
                f"path segment. The request never completed instead "
                f"({type(exc).__name__}: {exc}).") from exc

    def test_every_id_the_api_accepted_reads_back_as_its_own_account(self):
        with _running_api() as (_, base):
            client = self._lookalikes(base)
            for account_id, owner, funder, amount, other in _LOOKALIKES:
                with self.subTest(account_id=account_id):
                    read = self._read(client, "get_balance", account_id)
                    self.assertEqual(
                        _ownership_problems(read, account_id, owner), [],
                        f"GET the balance of {account_id!r} answered for another "
                        f"account: the id was not carried through the path as the "
                        f"id it is")
                    activity = self._read(client, "list_activity", account_id)
                    self.assertEqual(
                        _activity_problems(activity, funder, amount, other), [],
                        f"the activity of {account_id!r} is not its own")

    def test_the_read_row_fires_when_the_balance_site_goes_unquoted(self):
        """FALSIFIER (one site): the pre-fix ``get_balance``, red on the row above.

        The mutant's **premise** is asserted as the retarget itself — an answer
        came back, with a 200, for the *other* account — and not as "the read
        raised". That distinction is the whole finding: a mutant that produced
        only ``InvalidURL`` would leave the wrong account unread and still make
        the row above go red, so a falsifier that stops at "it went red" would
        certify a fix that had not closed the leak.

        The sibling site is asserted still quoted under the same mutant, which is
        what makes the attribution exact: one site reverted, one row red.
        """
        mutant = _unquoted("get_balance")
        with mock.patch.object(app_client.ApiClient, "get_balance", mutant):
            self.assertIs(app_client.ApiClient.get_balance, mutant,
                          "the mutant must be the attribute the row's client reads")
            with _running_api() as (_, base):
                client = self._lookalikes(base)
                read = client.get_balance(_ENCODED_ID)
                self.assertEqual(
                    read.get("account_id"), _PLAIN_ID,
                    f"the mutant's premise: with ``quote`` reverted at this site, "
                    f"a read for {_ENCODED_ID!r} silently answers for "
                    f"{_PLAIN_ID!r} with a 200; got {read!r}")
                problems = _ownership_problems(read, _ENCODED_ID, "mallory")
                self.assertEqual(
                    _activity_problems(client.list_activity(_ENCODED_ID),
                                       _FUNDER_B, _ENCODED_AMOUNT,
                                       _FUNDER_A), [],
                    "reverting one site must leave the other quoting; if both "
                    "went unquoted this mutant proves nothing about either")
        self.assertNotEqual(
            problems, [],
            "the row above, run as written against the pre-fix balance site, must "
            "go red; it reported no problem")
        self.assertTrue(
            any("alice" in problem for problem in problems),
            f"and the problem must name the account it answered for, so the red is "
            f"the retarget and not some other breakage; got {problems!r}")

    def test_the_read_row_fires_when_the_activity_site_goes_unquoted(self):
        """FALSIFIER (the other site): the pre-fix ``list_activity``.

        Same shape and same premise — a silent retarget, read off the
        counterparty — because the second site is the one a row that only drove
        ``get_balance`` would have left unpinned.
        """
        mutant = _unquoted("list_activity")
        with mock.patch.object(app_client.ApiClient, "list_activity", mutant):
            self.assertIs(app_client.ApiClient.list_activity, mutant,
                          "the mutant must be the attribute the row's client reads")
            with _running_api() as (_, base):
                client = self._lookalikes(base)
                answer = client.list_activity(_ENCODED_ID)
                pairs = [(item.get("counterparty_owner_id"), item.get("amount_minor"))
                         for item in answer.get("items", [])]
                self.assertIn(
                    (_FUNDER_A, _PLAIN_AMOUNT), pairs,
                    "the mutant's premise: with ``quote`` reverted at this site, "
                    "the activity read for the encoded id silently answers with "
                    f"the plain id's funding row; got {pairs!r}")
                self.assertNotIn(
                    (_FUNDER_B, _ENCODED_AMOUNT), pairs,
                    f"and its own row is nowhere in the answer; got {pairs!r}")
                problems = _activity_problems(answer, _FUNDER_B, _ENCODED_AMOUNT,
                                              _FUNDER_A)
                self.assertEqual(
                    _ownership_problems(client.get_balance(_ENCODED_ID),
                                        _ENCODED_ID, "mallory"), [],
                    "reverting one site must leave the other quoting")
        self.assertNotEqual(
            problems, [],
            "the row above, run as written against the pre-fix activity site, must "
            "go red; it reported no problem")
        self.assertTrue(
            any(_FUNDER_A in problem for problem in problems),
            f"and the problem must name the account it answered for, so the red is "
            f"the retarget and not some other breakage; got {problems!r}")


if __name__ == "__main__":
    unittest.main()
