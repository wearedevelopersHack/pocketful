"""End-to-end evidence for the client's one money-critical obligation.

Run it:

    python3 -m app.selfcheck      # exit 0 iff every check passed

What it proves (T4 DoD / plan §2.3), against a **real** ``api/`` server on a
real socket over a **real** ``ledger/`` database:

1. The key is generated at request-construction time and persisted *before* the
   first attempt.
2. A process killed **after the server applied the transfer but before the
   client could record the outcome** leaves a durable pending record behind.
3. The restart path (``app.send.resume``) retries with the **same**
   ``Idempotency-Key`` header and the ledger **replays** — same ``transfer_id``,
   no second debit, balances unchanged.
4. A negative control ("mutant") that mints a fresh key on the retry path
   **does** double-debit, so the assertion in (3) has teeth: it fails for the
   defect it names, not merely for a broken harness.

The transport is real HTTP (``http.client``); nothing here is a canned response.
The child process SIGKILLs itself inside the transport, after the server's 201
has been read and before ``app.send`` can act on it — the worst case for
idempotency, because the server has certainly committed.

This module lives under ``app/`` (the frontend-engineer's tree). The
gate-resident copy of the same scenario belongs in ``tests/`` and is the
test-author's to write; this is the owner's runnable evidence.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from api.app import create_server

from .client import ApiClient
from .keys import new_idempotency_key
from .keystore import PendingStore
from .money import InvalidMoneyInput, format_minor, parse_amount_to_minor
from .send import resume, send
from .wallet import ActivityRow
from . import web as _web
from .web import DEMO_NOTICE, create_ui_server

REPO_ROOT = Path(__file__).resolve().parent.parent
READY_TIMEOUT_SECONDS = 10.0

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    ok = bool(ok)
    RESULTS.append((ok, label))
    suffix = f"  [{detail}]" if detail else ""
    print(f"{'PASS' if ok else 'FAIL'}  {label}{suffix}")
    return ok


# -- real HTTP, shared by the production client and the evidence transports ----

def _real_http(method: str, url: str, body: bytes | None,
               headers: dict, *, timeout: float = 15.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # Same shape as ``app/client.py``'s transport, and the same reason for
        # the ``finally``: this is the path the gate's own runs take, so an
        # unclosed refusal here would be the warning that hides the next real
        # one. Read first — the body is the answer — then release the response.
        try:
            return exc.code, exc.read()
        finally:
            exc.close()


def _wait_until_ready(host: str, port: int) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.02)
    raise RuntimeError(f"server on {host}:{port} not ready within "
                       f"{READY_TIMEOUT_SECONDS}s (last error: {last_error})")


class _LostResponseTransport:
    """Real HTTP, but the FIRST transfer response is lost after the server applies it.

    This is the web layer's version of the kill: the request reaches the server
    and commits, then the client sees a transport failure instead of the answer.
    Every later call passes through normally, and every ``Idempotency-Key`` that
    crosses the wire is recorded so an assertion can be on the wire value.

    ``store_at_send`` answers the one question a post-hoc read of the store
    cannot: was the record already durable *when the request left*? It is
    collected here, in the transport, at the instant of the send — because
    reading the store after the POST returns only proves cardinality, and a
    build that sent first and persisted afterwards would leave the same single
    record and the cardinality row would still be green.
    """

    def __init__(self, store_path: str | None = None) -> None:
        self.sent_keys: list[str | None] = []
        self.store_at_send: list[tuple[str, bool]] = []
        self.store_path = store_path
        self._lost = False

    def __call__(self, method: str, url: str, body: bytes | None,
                 headers: dict) -> tuple[int, bytes]:
        key = headers.get("Idempotency-Key")
        if key is not None:
            self.sent_keys.append(key)
            if self.store_path is not None:
                # Re-read the store FILE, not the in-process object.
                self.store_at_send.append(
                    (key, PendingStore(self.store_path).get(key) is not None))
        status, raw = _real_http(method, url, body, headers)
        if not self._lost and method == "POST" and urlsplit(url).path == "/transfers":
            self._lost = True
            raise OSError("connection reset while reading the response")
        return status, raw


class _UnusableResponseTransport:
    """Real HTTP, except the first transfer answers 2xx with nothing usable in it.

    `app/send.py` refuses such an answer with ``SendProtocolError`` rather than
    settling, so the record stays pending. This is the injected 2xx the handler
    did not name: it must still leave the browser on a GET, because an uncaught
    raise here is not a 500 — it is *no response at all*, and a browser that
    received no response reloads the POST.
    """

    def __init__(self) -> None:
        self.sent_keys: list[str | None] = []
        self._bad = True

    def __call__(self, method: str, url: str, body: bytes | None,
                 headers: dict) -> tuple[int, bytes]:
        if "Idempotency-Key" in headers:
            self.sent_keys.append(headers["Idempotency-Key"])
        if self._bad and method == "POST" and urlsplit(url).path == "/transfers":
            self._bad = False
            # 2xx, but no transfer_id: unusable to the client, so no settle.
            return 200, json.dumps({"status": "applied"}).encode()
        return _real_http(method, url, body, headers)


class _SyntheticFault(Exception):
    """A type no ``except`` list in ``app/web.py`` names — the eighth path."""


class _SyntheticFaultTransport:
    """Real HTTP; the first transfer APPLIES, then the client raises an unforeseen type.

    Not an ``OSError``, so ``ApiClient`` does not wrap it in ``ApiError`` and no
    handler names it: it escapes ``wallet.send`` and ``_handle_send`` alike. The
    server has the money and the client has nothing usable — so the record must
    be left exactly as it was, ``pending`` with no ``transfer_id``, for
    ``resume`` to replay under the same key.
    """

    def __init__(self, store_path: str) -> None:
        self.sent_keys: list[str | None] = []
        self.store_at_send: list[tuple[str, bool]] = []
        self.store_path = store_path
        self._faulted = False

    def __call__(self, method: str, url: str, body: bytes | None,
                 headers: dict) -> tuple[int, bytes]:
        key = headers.get("Idempotency-Key")
        if key is not None:
            self.sent_keys.append(key)
            self.store_at_send.append(
                (key, PendingStore(self.store_path).get(key) is not None))
        if not self._faulted and method == "POST" and urlsplit(url).path == "/transfers":
            status, _raw = _real_http(method, url, body, headers)   # the server applies it
            self._faulted = True
            raise _SyntheticFault(f"the server answered {status}, then the client blew up")
        return _real_http(method, url, body, headers)


def _ui_call(host: str, port: int, method: str, path: str,
             form: dict | None = None) -> tuple[int, dict, str]:
    payload = None
    headers: dict[str, str] = {}
    if form is not None:
        payload = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn = http.client.HTTPConnection(host, port, timeout=15)
    try:
        conn.request(method, path, body=payload, headers=headers)
        try:
            response = conn.getresponse()
        except (http.client.HTTPException, OSError):
            # The handler died without committing a status line. That is not an
            # exception to abort the run on — "no response" is precisely the
            # failure mode the redirect rows exist to catch, so surface it as
            # status 0 and let them fail loudly. A connexion closed mid-response
            # cannot be redirected from in-process, so this is the honest result.
            return 0, {}, ""
        body = response.read().decode("utf-8", "replace")
        return response.status, dict(response.getheaders()), body
    finally:
        conn.close()


_UUID_ANY = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# The balance the page displays, read from the element that carries it.
#
# A regex over the class attribute, **not an HTML parse** — said out loud so the
# next editor reads this as a claim about one element rather than about the
# document's structure. It is anchored on `class="balance"`, which user text
# cannot forge: `app/design.py`'s `_esc` escapes with `quote=True`, so a value
# interpolated into an attribute cannot close it and open another.
#
# Why the element and not the page: a page is one string, so a bare
# ``"$100.00" in page`` is satisfied by *any* element that happens to print that
# text — the grant's activity row prints exactly it while the balance element
# shows something else. A label that names an element while asserting over the
# document is only accidentally true.
_BALANCE_ELEMENT = re.compile(
    r'<p class="balance">(?:<span[^>]*></span>)?\s*([^<]*)</p>')

# One activity row's direction word and its amount, read from the SAME <tr>.
#
# Row-scoped on purpose (designer, 2026-10-04). A page-wide search for "Sent to"
# is satisfied by any element; a *column*-wide search is satisfied by a sibling
# row, so on a page with one debit and one credit the sibling keeps a
# column-scoped assertion green while the row under test renders the wrong word.
# Reading both cells out of one <tr> makes the claim "this row's two channels
# agree" instead of "these strings appear somewhere on the page".
_ACTIVITY_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_ROW_DIRECTION = re.compile(r'<td class="dir">([^<]*)</td>')
_ROW_AMOUNT = re.compile(r'<td class="amt[^"]*">([^<]*)</td>')


def _direction_cells(page: str) -> list[tuple[str, str]]:
    """``(word, amount)`` for every activity row, each pair from one ``<tr>``.

    Rows that carry only one of the two cells are skipped rather than guessed at;
    the caller asserts non-emptiness, so a page whose rows stopped rendering
    cannot pass by having nothing to disagree.
    """
    pairs: list[tuple[str, str]] = []
    for row in _ACTIVITY_ROW.findall(page):
        word = _ROW_DIRECTION.search(row)
        amount = _ROW_AMOUNT.search(row)
        if word and amount:
            pairs.append((word.group(1), amount.group(1)))
    return pairs


# -- child modes --------------------------------------------------------------

def _child_send(base_url: str, store_path: str, from_id: str, to_id: str,
                amount_minor: int, currency: str) -> int:
    """Persist a key, send over real HTTP, then die before the client records it."""
    store = PendingStore(store_path)
    parts = urlsplit(base_url)

    def kill_after_ack(method: str, url: str, body: bytes | None,
                       headers: dict) -> tuple[int, bytes]:
        status, raw = _real_http(method, url, body, headers)
        # The server has answered: the transfer is committed. Emit the key we
        # put on the wire, then die before app/send.py can settle the record.
        print(json.dumps({"event": "child-send-acked", "status": status,
                          "key": headers.get("Idempotency-Key")}), flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
        time.sleep(3600.0)              # unreachable; SIGKILL is not catchable
        return status, raw

    client = ApiClient(base_url, transport=kill_after_ack)
    send(store, client, from_account_id=from_id, to_account_id=to_id,
         amount_minor=amount_minor, currency=currency)
    # send() should never return: the transport killed us.
    print(json.dumps({"event": "child-send-unexpected-completion"}), flush=True)
    return 1


def _child_resume(base_url: str, store_path: str, mutate: bool) -> int:
    """Restart: retry outstanding records with their stored keys (or mutate)."""
    store = PendingStore(store_path)
    sent_keys: list[str | None] = []

    def recording(method: str, url: str, body: bytes | None,
                  headers: dict) -> tuple[int, bytes]:
        sent_keys.append(headers.get("Idempotency-Key"))
        return _real_http(method, url, body, headers)

    client = ApiClient(base_url, transport=recording)

    if mutate:
        # MUTANT: the defect this scenario is built to catch — mint a fresh key
        # on the retry path. It is a different transfer, so it double-debits.
        pending = store.outstanding()[0]
        response = client.send_transfer(
            idempotency_key=new_idempotency_key(),
            from_account_id=pending.from_account_id,
            to_account_id=pending.to_account_id,
            amount_minor=pending.amount_minor,
            currency=pending.currency,
        )
        print(json.dumps({"mutant": True, "status": response.get("status"),
                          "transfer_id": response.get("transfer_id"),
                          "sent_keys": sent_keys}), flush=True)
        return 0

    outcomes = resume(store, client)
    print(json.dumps({
        "statuses": [{"key": o.key, "status": o.status, "transfer_id": o.transfer_id}
                     for o in outcomes],
        "sent_keys": sent_keys,
    }), flush=True)
    return 0


def _child_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.selfcheck --child")
    parser.add_argument("--child-send", nargs=5, metavar=("URL", "STORE", "FROM", "TO", "AMOUNT"))
    parser.add_argument("--child-resume", nargs=2, metavar=("URL", "STORE"))
    parser.add_argument("--child-resume-mutant", nargs=2, metavar=("URL", "STORE"))
    parser.add_argument("--currency", default="USD")
    args = parser.parse_args(argv)
    if args.child_send:
        url, store_path, from_id, to_id, amount = args.child_send
        return _child_send(url, store_path, from_id, to_id, int(amount), args.currency)
    if args.child_resume:
        url, store_path = args.child_resume
        return _child_resume(url, store_path, mutate=False)
    if args.child_resume_mutant:
        url, store_path = args.child_resume_mutant
        return _child_resume(url, store_path, mutate=True)
    parser.error("a --child-* mode is required")


# -- orchestrator -------------------------------------------------------------

def _spawn_child(*child_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "app.selfcheck", *child_args, "--currency", "USD"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )


def _last_json(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def scenario_kill_and_resume(base_url: str, workdir: str, funder: str,
                             payer: str, payee: str, mutate: bool) -> None:
    """Send 500 fine, kill the sender mid-flight, restart and retry."""
    store_path = str(Path(workdir) / ("store-mutant.json" if mutate else "store.json"))
    label = "MUTANT (fresh key on retry)" if mutate else "NORMAL"

    sent = _spawn_child("--child-send", base_url, store_path, payer, payee, "500")
    check(sent.returncode == -signal.SIGKILL,
          f"[{label}] the sender process was SIGKILLed mid-flight",
          f"returncode={sent.returncode}")
    acked = _last_json(sent.stdout)
    check(acked is not None and acked.get("status") in (200, 201),
          f"[{label}] the server had already applied the transfer before the kill",
          f"{acked}")

    first_key = acked.get("key") if acked else None
    store = PendingStore(store_path)
    outstanding = store.outstanding()
    check(len(outstanding) == 1 and outstanding[0].key == first_key
          and outstanding[0].amount_minor == 500 and outstanding[0].state == "pending",
          f"[{label}] exactly one pending record survived on disk, key + body intact",
          f"outstanding={[(r.key, r.amount_minor, r.state) for r in outstanding]}")

    resume_arg = "--child-resume-mutant" if mutate else "--child-resume"
    restarted = _spawn_child(resume_arg, base_url, store_path)
    check(restarted.returncode == 0,
          f"[{label}] the restarted process exited cleanly",
          f"returncode={restarted.returncode} stderr={restarted.stderr.strip()[:200]}")
    payload = _last_json(restarted.stdout) or {}
    sent_keys = payload.get("sent_keys") or []
    if mutate:
        check(sent_keys != [] and sent_keys != [first_key],
              f"[{label}] the mutant put a DIFFERENT key on the wire",
              f"sent={sent_keys} stored={first_key}")
    else:
        check(sent_keys == [first_key],
              f"[{label}] the retry PUT THE SAME Idempotency-Key on the wire",
              f"sent={sent_keys} stored={first_key}")

    # Re-read from disk: the child that retried settled the record in its own
    # process, so the parent's earlier handle is stale by construction.
    store = PendingStore(store_path)
    client = ApiClient(base_url)
    payer_balance = client.get_balance(payer)["balance_minor"]
    payee_balance = client.get_balance(payee)["balance_minor"]
    if mutate:
        # The mutant mints a new key, so the server applies a SECOND transfer.
        # This is the money the naive implementation loses, and it is why the
        # normal-path assertion below is not vacuous.
        check(payer_balance == 9000 and payee_balance == 1000,
              f"[{label}] a regenerated key double-debits (payer 9000, payee 1000) — "
              "the defect the next assertion catches",
              f"payer={payer_balance} payee={payee_balance}")
        return

    statuses = payload.get("statuses") or []
    check(len(statuses) == 1 and statuses[0].get("status") == "replayed",
          f"[{label}] the ledger REPLAYED the retry (status=replayed, one outcome)",
          f"{statuses}")
    settled = store.get(first_key) if first_key else None
    check(settled is not None and settled.state == "settled"
          and settled.transfer_id == statuses[0].get("transfer_id"),
          f"[{label}] the record settled to the replayed transfer_id",
          f"{settled}")
    check(payer_balance == 9500 and payee_balance == 500,
          f"[{label}] balances moved exactly once: payer 9500, payee 500",
          f"payer={payer_balance} payee={payee_balance}")

    activity = client.list_activity(payer)["items"]
    debits = [item for item in activity if item["direction"] == "debit"]
    check(len(debits) == 1 and debits[0]["amount_minor"] == 500
          and debits[0]["transfer_id"] == statuses[0].get("transfer_id"),
          f"[{label}] the payer activity feed shows exactly ONE 500 debit",
          f"items={len(activity)} debits={len(debits)}")


def scenario_web_ui(base_url: str, workdir: str, payer: str, payee: str) -> None:
    """The web layer: browser is a view, the key never leaves Python (§2.3 amended).

    A real ``app.web`` server is started against the real API and driven with real
    HTTP. The first transfer's response is lost after the server applied it — the
    web analogue of the process kill — so the retry path is the one that matters.
    """
    store_path = str(Path(workdir) / "ui-store.json")
    transport = _LostResponseTransport(store_path)
    ui = create_ui_server(api_base_url=base_url, store_path=store_path,
                          account_id=payer, port=0,
                          client=ApiClient(base_url, transport=transport))
    port = ui.server_address[1]
    thread = threading.Thread(target=ui.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        _wait_until_ready("127.0.0.1", port)
        client = ApiClient(base_url)

        # The amounts this scenario posts, in minor units, so the string it sends
        # and the integer it asserts cannot drift apart. Integer arithmetic only.
        SEND = 500
        SECOND = 200

        # The caller's fixture — not this scenario — decides what the accounts
        # open with: §C1's opening grant posts 10000 to every account it creates,
        # on top of whatever the caller funds. So capture the opening balances and
        # assert the MOVEMENT this scenario performs. Asserting a total instead
        # renumbers these rows whenever the caller's fixture changes; worse, a
        # renumbered "money moved again" row can launder a double-spend into a
        # pass, because a total no longer says which transfer moved the money.
        payer_open = client.get_balance(payer)["balance_minor"]
        payee_open = client.get_balance(payee)["balance_minor"]

        status, _, page = _ui_call("127.0.0.1", port, "GET", "/")
        # Two claims, two rows. The page carries the wallet's copy — and,
        # separately, the balance *element* carries the server's figure for
        # *this* account. The literal that used to stand here ("$100.00") passed
        # for a reason it did not name: the opening grant's activity row prints
        # that same text, so the row would have stayed green while the balance
        # element showed anything at all.
        check(status == 200 and "Send money" in page,
              "[WEB] GET / renders the wallet page with its send form",
              f"status={status}")
        balance_match = _BALANCE_ELEMENT.search(page)
        check(balance_match is not None
              and balance_match.group(1) == format_minor(payer_open, "USD"),
              "[WEB] the balance element shows the server's balance for this account",
              f"element={balance_match.group(1)!r}" if balance_match
              else "no balance element found (regex over class=\"balance\")")
        check("Idempotency-Key" not in page and "idempotency" not in page.lower()
              and _UUID_ANY.search(page) is None,
              "[WEB] the rendered page carries no idempotency key and no key field",
              "no key string, no UUID in the HTML")

        store = PendingStore(store_path)
        check(len(store.all_records()) == 0,
              "[WEB] rendering the page minted no key", f"records={len(store.all_records())}")

        # The demo notice is a property of every document the single wrapper
        # builds, not of one page: both builders pass through _document. So this
        # samples the documents the running server actually builds, including the
        # two that are not the happy path — a page built by another route, and a
        # page built from an API 404.
        #
        # Scope (T19, 2026-10-04): this is the wrapper's property, not the
        # server's. WalletUIHandler does not override send_error, so PUT/DELETE/
        # PATCH / are answered by the stdlib with a 501 text/html page (and a
        # malformed request line with a 505) that never reaches _document and
        # carries no marker. Sampled locally: 0 markers on both. Not sampled
        # here; the label says only what the assertion measures.
        notice_paths = ("/", f"/?account={payee}", "/?account=no-such-account",
                        "/?error=invalid_amount")
        missing = [path for path in notice_paths
                   if DEMO_NOTICE not in _ui_call("127.0.0.1", port, "GET", path)[2]]
        check(not missing,
              "[WEB] every document the UI's _document wrapper builds carries the demo notice",
              f"missing on {missing}" if missing
              else f"{len(notice_paths)} pages sampled, marker on all")

        # ... and the witness above can fail. This is the same live server with
        # the ONE document wrapper replaced for a single request: the row's
        # predicate must invert, or the row is a claim about a constant rather
        # than a measurement of the responses.
        real_document = _web._document
        try:
            _web._document = lambda *, title, body: (
                "<!doctype html><title>x</title><body>" + body + "</body>")
            mutant_page = _ui_call("127.0.0.1", port, "GET", "/")[2]
        finally:
            _web._document = real_document
        check(DEMO_NOTICE not in mutant_page,
              "[WEB] the notice row's witness can fail: wrapper without the banner loses the marker",
              "same live server, wrapper mutated for one request")

        # Send, with the server applying it and the response then lost.
        #
        # The assertion that gives this row teeth is the FIRST one: the POST must
        # answer 303 with no page body. Before the fix it answered 202 with the
        # rendered page as its own response, so a browser refresh re-issued the
        # POST and minted a second key — a live double-spend the ledger cannot
        # see, because both transfers are individually legitimate.
        status, headers, body = _ui_call("127.0.0.1", port, "POST", "/send",
                                         {"to_account_id": payee,
                                          "amount": f"{SEND // 100}.{SEND % 100:02d}"})
        location = str(headers.get("Location", ""))
        check(status == 303 and location.startswith("/") and "<html" not in body.lower(),
              "[WEB] POST /send answers 303 with no page body: the response is not a refreshable POST",
              f"status={status} location={location!r} body={body[:32]!r}")

        status, _, page = _ui_call("127.0.0.1", port, "GET", location)
        check(status == 200 and "Unconfirmed transfers" in page
              and _UUID_ANY.search(page) is None,
              "[WEB] the redirect target shows the pending transfer without its key",
              f"status={status}")
        # The row's two channels must agree, read within one <tr>: the word says
        # "Sent to" exactly when the amount is negative. A presence check would
        # pass a row rendering the wrong word, and a column- or page-scoped one
        # would be satisfied by the sibling row — this page has both a debit (the
        # send) and credits (the grant and the funding), so the scoping is doing
        # work rather than decorating the label. Both minus forms are accepted
        # because the claim is the agreement, not the glyph.
        pairs = _direction_cells(page)
        disagreements = [
            (word, amount) for word, amount in pairs
            if not ((word == "Sent to" and amount[:1] in ("−", "-"))
                    or (word == "Received from" and amount[:1] == "+"))]
        check(pairs and not disagreements
              and any(word == "Sent to" for word, _ in pairs)
              and any(word == "Received from" for word, _ in pairs),
              "[WEB] each activity row's direction word agrees with its amount's "
              "sign, both read from the same <tr>",
              f"rows={pairs[:4]} disagreements={disagreements}")

        # ...and the row above can fail. Same renderer, one row's word flipped:
        # the mutant's premise is asserted (the word really is the flipped one)
        # so an inert mutation reports as a failure instead of as a pass.
        real_table = _web.activity_table
        try:
            _web.activity_table = lambda rows: real_table(rows=rows).replace(
                '<td class="dir">Sent to</td>', '<td class="dir">Received from</td>')
            mutant_page = _web.render_wallet(
                account_id=payer, balance_minor=payer_open, currency="USD",
                rows=[ActivityRow(
                    entry_id="e-mutant", transfer_id="t-mutant", direction="debit",
                    amount_minor=SEND, amount_display="$5.00",
                    balance_after_minor=payer_open - SEND,
                    counterparty_account_id=payee,
                    created_at="2026-01-01T00:00:00Z")],
                pending=[])
        finally:
            _web.activity_table = real_table
        mutant_pairs = _direction_cells(mutant_page)
        mutant_disagreements = [
            (word, amount) for word, amount in mutant_pairs
            if not ((word == "Sent to" and amount[:1] in ("−", "-"))
                    or (word == "Received from" and amount[:1] == "+"))]
        check(mutant_pairs and mutant_pairs[0][0] == "Received from"
              and len(mutant_disagreements) == 1,
              "[WEB] the row-consistency check can fail: a flipped direction word "
              "is a disagreement, in the row that carries it",
              f"mutant rows={mutant_pairs} disagreements={mutant_disagreements}")
        records = PendingStore(store_path).all_records()
        check(len(records) == 1 and records[0].state == "pending"
              and records[0].amount_minor == SEND and records[0].to_account_id == payee,
              "[WEB] exactly one record exists for the transfer, amount and payee intact",
              f"{[(r.key, r.amount_minor, r.state) for r in records]}")
        key = records[0].key
        check(transport.sent_keys == [key],
              "[WEB] the first attempt put exactly that server-side key on the wire",
              f"sent={transport.sent_keys}")
        # Cardinality is not durability. The row above would also be green for a
        # build that sent first and persisted afterwards; this one reads the
        # store FILE from inside the transport at the instant the request left.
        check(transport.store_at_send == [(key, True)],
              "[WEB] the record was already durable when the request left: persisted BEFORE the attempt",
              f"store_at_send={transport.store_at_send}")
        balance_after_send = client.get_balance(payer)["balance_minor"]
        check(balance_after_send == payer_open - SEND
              and client.get_balance(payee)["balance_minor"] == payee_open + SEND,
              "[WEB] the server had already applied the transfer (money moved once, by exactly the sent amount)",
              f"payer {payer_open}->{balance_after_send} "
              f"payee={client.get_balance(payee)['balance_minor']}")

        # Refreshing the page the browser is now showing re-issues a GET, which
        # mints nothing. Without the 303 above, the browser would be showing the
        # POST's own response and this refresh would be a second send.
        for _ in range(3):
            _ui_call("127.0.0.1", port, "GET", location)
        check(len(PendingStore(store_path).all_records()) == 1
              and transport.sent_keys == [key],
              "[WEB] three refreshes of the outcome page mint no key and send nothing",
              f"records={len(PendingStore(store_path).all_records())} sent={transport.sent_keys}")

        # Retry: the browser posts nothing but the word "retry".
        status, headers, _ = _ui_call("127.0.0.1", port, "POST", "/retry")
        check(status == 303 and str(headers.get("Location", "")).startswith("/"),
              "[WEB] POST /retry redirects (Post/Redirect/Get, refresh cannot replay)",
              f"status={status} location={headers.get('Location')}")
        check(transport.sent_keys == [key, key],
              "[WEB] the retry reused the SAME key; the browser supplied none",
              f"sent={transport.sent_keys}")
        settled = PendingStore(store_path).all_records()
        check(len(settled) == 1 and settled[0].state == "settled"
              and settled[0].transfer_id,
              "[WEB] the record settled after the retry",
              f"{[(r.state, r.transfer_id) for r in settled]}")
        check(client.get_balance(payer)["balance_minor"] == payer_open - SEND
              and client.get_balance(payee)["balance_minor"] == payee_open + SEND,
              "[WEB] the retry moved no additional money: the deltas are still the first send's",
              f"payer={client.get_balance(payer)['balance_minor']} "
              f"payee={client.get_balance(payee)['balance_minor']}")
        for _ in range(3):
            _ui_call("127.0.0.1", port, "GET", "/")
        debits = [item for item in client.list_activity(payer)["items"]
                  if item["direction"] == "debit"]
        check(len(debits) == 1 and debits[0]["amount_minor"] == 500,
              "[WEB] three refreshes after success sent nothing: exactly one debit at the API",
              f"debits={len(debits)}")

        # Local rejections redirect too — a handler that raises instead of
        # answering is not a status either way.
        status, headers, _ = _ui_call("127.0.0.1", port, "POST", "/send",
                                      {"to_account_id": payer, "amount": "1.00"})
        check(status == 303 and headers.get("Location") == "/?error=same_account",
              "[WEB] a same-account send answers 303 to an error page, not an unhandled raise",
              f"status={status} location={headers.get('Location')}")
        status, headers, _ = _ui_call("127.0.0.1", port, "POST", "/send",
                                      {"to_account_id": payee, "amount": "1.005"})
        check(status == 303 and headers.get("Location") == "/?error=invalid_amount",
              "[WEB] a 3-decimal amount is rejected with a redirect, not rounded",
              f"status={status} location={headers.get('Location')}")

        # The forbidden fix, ruled out by observation: a genuine SECOND PRESS is
        # a second transfer (§2.3 item 1). If someone ever "fixed" the refresh by
        # deduping in the store, this row turns red — which is the point.
        status, headers, _ = _ui_call("127.0.0.1", port, "POST", "/send",
                                      {"to_account_id": payee,
                                       "amount": f"{SECOND // 100}.{SECOND % 100:02d}"})
        _ui_call("127.0.0.1", port, "GET", str(headers.get("Location", "/")))
        check(status == 303 and len(PendingStore(store_path).all_records()) == 2,
              "[WEB] two presses -> two records and two distinct keys (no store-side dedupe)",
              f"records={len(PendingStore(store_path).all_records())}")
        check(client.get_balance(payer)["balance_minor"] == payer_open - SEND - SECOND
              and client.get_balance(payee)["balance_minor"] == payee_open + SEND + SECOND,
              "[WEB] the second press moved money AGAIN, over and above the first send: "
              "a second transfer, not a retry of the first",
              f"payer={client.get_balance(payer)['balance_minor']} "
              f"payee={client.get_balance(payee)['balance_minor']} "
              f"(open {payer_open}, sent {SEND} then {SECOND})")

        # -- the eighth path: the exception nobody named -----------------------
        #
        # "Every branch redirects" was a claim about a list, checked by reading
        # the list. Six of six lists were right and this path was invisible, so
        # the property was never structural. These rows drive the two shapes
        # that a list cannot cover — a named-but-uncaught type, and a fault
        # nobody enumerated at all — and they are on their own server so the
        # balances above are untouched.
        proto_store = str(Path(workdir) / "ui-store-protocol.json")
        proto_transport = _UnusableResponseTransport()
        ui2 = create_ui_server(api_base_url=base_url, store_path=proto_store,
                               account_id=payer, port=0,
                               client=ApiClient(base_url, transport=proto_transport))
        port2 = ui2.server_address[1]
        thread2 = threading.Thread(target=ui2.serve_forever,
                                   kwargs={"poll_interval": 0.05}, daemon=True)
        thread2.start()
        try:
            _wait_until_ready("127.0.0.1", port2)

            status, headers, body = _ui_call("127.0.0.1", port2, "POST", "/send",
                                             {"to_account_id": payee, "amount": "1.00"})
            location = str(headers.get("Location", ""))
            check(status == 303 and location == "/?error=protocol"
                  and "<html" not in body.lower(),
                  "[WEB] an unusable 2xx answers 303 /?error=protocol, not an uncaught raise",
                  f"status={status} location={location!r} body={body[:32]!r}")
            proto_records = PendingStore(proto_store).all_records()
            check(len(proto_records) == 1 and proto_records[0].state == "pending"
                  and proto_transport.sent_keys == [proto_records[0].key],
                  "[WEB] the unreadable 2xx left the key persisted and pending for resume",
                  f"{[(r.state) for r in proto_records]} sent={proto_transport.sent_keys}")
            for _ in range(3):
                _ui_call("127.0.0.1", port2, "GET", location)
            check(len(PendingStore(proto_store).all_records()) == 1
                  and proto_transport.sent_keys == [proto_records[0].key],
                  "[WEB] refreshing the protocol page mints no key and sends nothing",
                  f"records={len(PendingStore(proto_store).all_records())} "
                  f"sent={proto_transport.sent_keys}")

            # Now a fault nobody named, raised from INSIDE wallet.send's path —
            # after the server has applied the transfer and before the client can
            # settle. This is the case the catch-all must survive without
            # touching the record: it stays pending with no transfer_id, so
            # resume replays that same key. A catch-all that discarded it, or
            # settled it without an id, would turn a benign unknown into a lost
            # key and re-open the door from the other side.
            fault_store = str(Path(workdir) / "ui-store-fault.json")
            fault_transport = _SyntheticFaultTransport(fault_store)
            ui3 = create_ui_server(api_base_url=base_url, store_path=fault_store,
                                   account_id=payer, port=0,
                                   client=ApiClient(base_url, transport=fault_transport))
            port3 = ui3.server_address[1]
            thread3 = threading.Thread(target=ui3.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
            thread3.start()
            try:
                _wait_until_ready("127.0.0.1", port3)
                status, headers, body = _ui_call("127.0.0.1", port3, "POST", "/send",
                                                 {"to_account_id": payee, "amount": "1.00"})
                location = str(headers.get("Location", ""))
                check(status == 303 and location == "/?error=internal"
                      and "<html" not in body.lower(),
                      "[WEB] an unforeseen exception still answers 303 /?error=internal, not no response",
                      f"status={status} location={location!r} body={body[:32]!r}")
                fault_records = PendingStore(fault_store).all_records()
                check(len(fault_records) == 1 and fault_records[0].state == "pending"
                      and fault_records[0].transfer_id is None
                      and fault_transport.store_at_send == [(fault_records[0].key, True)],
                      "[WEB] the unforeseen fault left the record pending and unsettled: no key lost, resume can replay",
                      f"{[(r.state, r.transfer_id) for r in fault_records]} "
                      f"store_at_send={fault_transport.store_at_send}")
                for _ in range(3):
                    _ui_call("127.0.0.1", port3, "GET", location)
                check(len(PendingStore(fault_store).all_records()) == 1
                      and fault_transport.sent_keys == [fault_records[0].key],
                      "[WEB] refreshing the internal-error page mints no key and sends nothing",
                      f"records={len(PendingStore(fault_store).all_records())} "
                      f"sent={fault_transport.sent_keys}")
            finally:
                ui3.shutdown()
                ui3.server_close()
        finally:
            ui2.shutdown()
            ui2.server_close()
    finally:
        ui.shutdown()
        ui.server_close()


def run_local_checks() -> None:
    """The non-network obligations: one key per press, and the float boundary."""
    store_dir = tempfile.mkdtemp(prefix="pocketful-keys-")
    try:
        store = PendingStore(str(Path(store_dir) / "s.json"))
        first = store.begin(from_account_id="a", to_account_id="b",
                            amount_minor=500, currency="USD")
        second = store.begin(from_account_id="a", to_account_id="b",
                             amount_minor=500, currency="USD")
        check(first.key != second.key and len(store.outstanding()) == 2,
              "two presses -> two distinct keys, two pending records",
              f"{first.key} != {second.key}")

        # Reload from disk: the pending record survives a fresh store object,
        # which is the in-process half of "survives the process dying".
        reloaded = PendingStore(str(Path(store_dir) / "s.json"))
        check(reloaded.get(first.key) == first,
              "the pending record round-trips through disk unchanged",
              f"{reloaded.get(first.key)}")

        # The pending list is the whole store by design — the store is
        # account-agnostic — so every item has to name the SENDING account too.
        # On B's page an item that said only "to B" would read as B's own
        # transfer: the page implied instead of saying.
        sender_rec = store.begin(from_account_id="acct-sender",
                                 to_account_id="acct-payee", amount_minor=250,
                                 currency="USD")
        listed = _web.render_wallet(account_id="acct-payee", balance_minor=0,
                                    currency="USD", rows=[], pending=[sender_rec])
        check("acct-sender" in listed and "acct-payee" in listed,
              "[WEB] a pending item names its sending account, not only the payee",
              "render_wallet(pending=[one record sent by acct-sender])")

        real_block = _web.pending_block
        try:
            _web.pending_block = lambda pending: real_block(pending=pending).replace(
                "acct-sender", "someone")
            mutant = _web.render_wallet(account_id="acct-payee", balance_minor=0,
                                        currency="USD", rows=[], pending=[sender_rec])
        finally:
            _web.pending_block = real_block
        check("acct-sender" not in mutant,
              "[WEB] the pending-item row's witness can fail: sender dropped -> id gone",
              "same builder, pending_block mutated for one call")

        check(type(parse_amount_to_minor("12.34")) is int
              and parse_amount_to_minor("12.34") == 1234,
              "user text '12.34' -> int 1234 minor units",
              repr(parse_amount_to_minor("12.34")))
        check(parse_amount_to_minor("0.05") == 5 and parse_amount_to_minor("7") == 700,
              "single-digit cents and whole amounts are exact",
              f"{parse_amount_to_minor('0.05')} {parse_amount_to_minor('7')}")
        for bad in ("12.345", "1,000.00", "-5", "0", "abc", "", "1.2.3"):
            try:
                parse_amount_to_minor(bad)
            except InvalidMoneyInput:
                continue
            check(False, f"input {bad!r} must be rejected locally")
            break
        else:
            check(True, "3+ decimals / separators / signed / zero / zero / junk rejected locally")

        try:
            parse_amount_to_minor(12.34)
        except InvalidMoneyInput:
            check(True, "a float is refused, never coerced into the money path")
        else:
            check(False, "a float must be refused")
        check(format_minor(1234) == "$12.34" and format_minor(-5) == "-$0.05",
              "display formatting is integer math at the edge",
              f"{format_minor(1234)} {format_minor(-5)}")
    finally:
        shutil.rmtree(store_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a.startswith("--child-") for a in argv):
        return _child_main(argv)

    workdir = tempfile.mkdtemp(prefix="pocketful-client-")
    db_path = str(Path(workdir) / "client.db")
    server = create_server(db_path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    print(f"Pocketful client self-check — {base_url} over {db_path}")
    print("-" * 72)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        _wait_until_ready("127.0.0.1", port)
        client = ApiClient(base_url)
        client.create_account(owner_id="funder", currency="USD",
                              allow_overdraft=True, account_id="acct-funder")
        for name in ("alice", "bob", "carol", "dave", "erin", "frank"):
            client.create_account(owner_id=name, currency="USD",
                                  account_id=f"acct-{name}")
        # Fund each payer from the overdraft-allowed funder. This is money moved
        # by the ledger's own double-entry path, not a client-side shortcut.
        for payer in ("alice", "carol", "erin"):
            client.send_transfer(idempotency_key=new_idempotency_key(),
                                 from_account_id="acct-funder", to_account_id=f"acct-{payer}",
                                 amount_minor=10000, currency="USD")

        scenario_kill_and_resume(base_url, workdir, "acct-funder",
                                 "acct-alice", "acct-bob", mutate=False)
        scenario_kill_and_resume(base_url, workdir, "acct-funder",
                                 "acct-carol", "acct-dave", mutate=True)
        scenario_web_ui(base_url, workdir, "acct-erin", "acct-frank")
        run_local_checks()
    except RuntimeError as exc:
        check(False, "the server bound and answered", str(exc))
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for ok, _ in RESULTS if ok)
    print("-" * 72)
    print(f"{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
