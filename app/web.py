"""Python-served wallet UI — the browser is a view, never a key holder.

Amendment to T4 (planner, 2026-10-02): Pocketful is web-facing at
``pocketful.getn.space`` and still Python. This module serves that UI with the
standard library only.

**The load-bearing rule.** The browser must never generate or hold the
idempotency key. A page reload that re-mints a key is a double-spend the ledger
cannot catch, because it is a different key and therefore a different transfer.
So the key never enters an HTML form, a hidden field, a cookie or a URL: it is
generated and persisted in the Python layer by :meth:`PendingStore.begin` (via
``app.send``), exactly as before, and the browser only ever sees a rendered view.

Three consequences, each deliberate:

* ``POST /send`` answers **303 See Other** to ``/`` on **every** outcome — the
  success path, every refusal, and an unforeseen internal error alike
  (Post/Redirect/Get) — so a browser refresh issues a GET and cannot replay the
  POST. The guarantee is structural, not a list of handled cases: ``do_POST``
  has one exit guard, so a handler cannot leave the browser on the POST either
  by raising an exception nobody named or by returning without answering.
* A retryable failure leaves the pending record on disk; the page then shows it
  with a Retry button. ``POST /retry`` calls ``app.send.resume``, which re-sends
  the *stored* key. The browser supplies no key and no body — it only says
  "retry what you already have".
* Rendering (``GET /``) mints no key and writes nothing to the store. Its only
  output is the account-list cookie, which carries ids and no money at all (see
  below). Reloading the page cannot mint a key.

**Accounts are presentational, and the active account is not a cookie.** ``GET
/?account=<id>`` renders that account's balance and activity; a bare ``GET /``
and an empty ``?account=`` fall back to the boot ``--account``. The active
account then rides with the POST that acts on it in a hidden ``account`` field
resolved by :func:`_resolve_account` — the *same* helper the renderer uses, so
the page and the action cannot disagree about who the sender is. The request
carries it; neither the cookie nor any server state selects it.

The one cookie, ``pocketful_accounts`` (plan §C3.3), exists so a browser can
find its way back to accounts it has seen. It is a JSON list of **ids only**,
capped at 12, and it is **never authoritative**: which account is active, every
name, every balance and every activity row is read from the server, and an id
the server does not know renders as *unavailable* — never with an invented
balance — and is dropped on the next write (plan §C3.4). It does not touch the
pending store, which stays one store and account-agnostic, so the keys a retry
needs are still a file on disk and never something a cookie could lose.

That is a change in kind, stated rather than implied: the field is
client-supplied, so **an account id is the capability**. The UI no longer
confines sends to the boot account — anyone who knows an id can act as it. On
this demo that was already the model (ids are minted uuid hex and shown only to
their creator), and ``DEMO_NOTICE`` says so on every page the application
renders (see ``_document`` for the server responses that are not ours).

The one store stays account-agnostic. Switching accounts creates, truncates,
moves, splits and rewrites nothing: a per-account store would lose the pending
keys on a switch, and a lost key is a fresh transfer, past every ledger
invariant.

Run it:

    python3 -m app.web --api http://127.0.0.1:8000 --store ./ui-pending.json \\
                       --account acct-alice --host 127.0.0.1 --port 8080

It is a client of ``api/`` like the rest of ``app/``: it opens no database
connection and imports nothing from ``ledger/``.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .client import ApiClient, ApiError
from .design import (STYLE, _esc, account_switcher, activity_table, banner,
                     create_form, pending_block, send_form, wallet_header)
from .keystore import PendingStore
from .money import InvalidMoneyInput, format_minor
from .send import (IdempotencyConflictForSend, RetryableSendError, SendProtocolError,
                   TerminalSendError, resume)
from .wallet import ActivityRow, SameAccountSend, Wallet, WalletProtocolError

LOG = logging.getLogger("app.web")


# -- rendering (pure string functions, escaped at the edge) --------------------
#
# Every component below lives in ``app/design.py``: this module composes the page
# and owns no markup of its own. ``_esc`` is imported rather than reimplemented so
# there is exactly one escaper in the application, and the stylesheet is
# ``design.STYLE`` so a component's classes and their rules cannot drift apart.

# The one line that has to be on every page the application renders. The UI is
# not a boundary: an account id in the hidden field is the whole capability, and
# the money is fixture data. Kept as a constant so a test can import the marker
# rather than re-spell it, and rendered by ``_document`` so no document built
# there can omit it. ``send_error``'s 501/505 pages are the server's, not ours —
# see ``_document``.
DEMO_NOTICE = ("Unauthenticated demo: an account id is the only credential, and "
               "these balances are not real money.")


def _document(*, title: str, body: str) -> str:
    """The single place *this application* assembles a ``text/html`` document.

    Both builders go through here, so the demo notice is a property of every
    document ``_document`` builds rather than something each page has to
    remember: a document cannot be built here without it, the same way a POST
    cannot exit without answering. The stylesheet is ``design.STYLE`` and the
    shell classes (``topbar``/``shell``/``foot``) come from it, so this module
    carries no CSS of its own.

    Scope, stated rather than implied (T19, 2026-10-04). This is the
    application's only document assembler, not the server's only HTML emitter.
    ``WalletUIHandler`` does not override ``BaseHTTPRequestHandler.send_error``,
    so an unsupported method (``PUT``/``DELETE``/``PATCH``) is answered with the
    stdlib's own 501 page and a request line it cannot parse with a 505 page
    (also stdlib ``text/html``). Neither passes through here; neither carries
    ``DEMO_NOTICE`` — measured locally, 0 markers on both. Those pages show no
    balance, no account id and cannot mint a key, so the notice's stated purpose
    is not engaged on them. The claim is "every document ``_document`` builds",
    not "every ``text/html`` response the server emits"; making the universal
    one true would mean overriding ``send_error`` and ``error_message_format``,
    which is new surface and its own task.
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<header class="topbar"><div class="topbar__inner">
<span class="brand">Pocketful</span>
</div></header>
<main class="shell">
<p class="demo" role="note">{_esc(DEMO_NOTICE)}</p>
{body}
</main>
<footer class="foot"><p class="hint">Balances and activity are the server's
values, shown verbatim.</p></footer>
</body>
</html>
"""


def render_wallet(*, account_id: str, balance_minor: int, currency: str,
                  rows: list[ActivityRow], pending: list,
                  message: str | None = None, notice: str | None = None,
                  name: str | None = None, accounts: list | None = None,
                  create_error: str | None = None, create_name: str | None = None,
                  create_address: str | None = None) -> str:
    """Render the wallet view. Contains no idempotency key and no key input field.

    Every component is ``app/design.py``'s; this function only decides the order
    and passes the server's values down. The only hidden field is the active
    account id, which is not a secret and not a key: it says which account the
    form acts as — the same rule ``_resolve_account`` applies to the POST.

    ``balance_minor`` is formatted here, at the edge, by the one formatter
    (``app/money.py``): the value travels as an integer and becomes a string only
    on its way into markup.
    """
    banners = ""
    if message:
        banners += banner(kind="error", message=message)
    if notice:
        banners += banner(kind="notice", message=notice)
    body = "".join([
        wallet_header(name=name, address=account_id,
                      balance_display=format_minor(balance_minor, currency)),
        banners,
        account_switcher(accounts=accounts or [], active_id=account_id),
        send_form(action="/send", address=account_id),
        pending_block(pending=pending),
        create_form(action="/create", name_value=create_name,
                    address_value=create_address, error=create_error),
        '<section class="card card--wide">',
        '<h2 class="h2">Activity</h2>',
        activity_table(rows=rows),
        "</section>",
    ])
    return _document(title=f"Pocketful — {account_id}", body=body)


def render_error(message: str, status: int) -> str:
    return _document(
        title="Pocketful — error",
        body=(f"<p class='error' role='alert'>{_esc(message)}</p>\n"
              "<p><a href='/'>Back to the wallet</a></p>"))


# Outcomes are carried back to the GET as a short code rather than as rendered
# HTML on the POST's own response — see the module docstring.
_ERROR_MESSAGES = {
    "invalid_amount": "That amount was not accepted. Use digits with at most two "
                      "decimal places; nothing was sent.",
    "same_account": "You cannot send to the same account. Nothing was sent.",
    "missing_fields": "Enter both an account and an amount.",
    "protocol": "The API answered with something this wallet could not read, so the "
                "transfer was not confirmed. It is recorded below — Retry reuses the "
                "same key, so nothing is sent twice.",
    "internal": "The wallet hit an unexpected error handling that request. Check the "
                "Unconfirmed transfers list before retrying; nothing was sent twice.",
    "conflict": "The server reports this transfer conflicts with one already "
                "recorded. It was not retried.",
    "idempotency_conflict": "The server reports this transfer conflicts with one "
                            "already recorded. It was not retried.",
    "insufficient_funds": "The account does not have enough funds. Nothing was sent.",
    "unknown_account": "One of the accounts does not exist. Nothing was sent.",
    "currency_mismatch": "The two accounts use different currencies. Nothing was sent.",
    "same_account_transfer": "You cannot send to the same account. Nothing was sent.",
    "invalid_idempotency_key": "The request was refused: invalid idempotency key.",
    "api": "The API did not answer. Nothing was sent.",
    "missing_owner": "Enter a name for the account you are creating.",
    "account_exists": "That account ID is already taken. Nothing was created.",
    "name_taken": "That name is already taken. Nothing was created — pick another.",
    "invalid_account": "The API refused those account details. Nothing was created.",
    "invalid_request": "The API refused those account details. Nothing was created.",
}

# The one refusal whose *subject* is not in the table above. On the ``name_taken``
# branch the name is free and the **address** is taken: two names can derive one
# address ("Grace Hopper", "grace  hopper" and "Grace-Hopper" all render
# ``acct-grace-hopper``), so a sentence that says the name is taken is describing
# the wrong thing. The address is not knowable here — it exists only on the create
# side, in ``_derive_account_id`` — so it arrives from the redirect in its own
# parameter and is formatted in at the render site. Never a literal: a sentence
# that bakes ``acct-grace-hopper`` becomes a false witness the moment the
# derivation moves. ``_ERROR_MESSAGES["name_taken"]`` stays the fallback for a URL
# that arrives without the address (see the lookup in ``_render_feed``).
_NAME_TAKEN_TEMPLATE = ("That name's address ({address}) is already taken. "
                        "Nothing was created — pick another name.")

# Refusals that belong to the CREATE card rather than to the page. They are a set
# rather than a property of the URL so the routing question ("which control was
# this about?") has one answer: these codes are produced only by ``_handle_create``
# — ``missing_owner`` and ``invalid_account`` are local to it, and a transfer
# route has no 409 that means "that name is taken" — so a code in this set can
# never be a send's, and a code outside it can never be a create's.
_CREATE_ERROR_CODES = frozenset({"missing_owner", "account_exists", "name_taken",
                                 "invalid_account"})

_PENDING_NOTICE = ("The transfer could not be confirmed and is recorded as pending. "
                   "Nothing was sent twice. Press Retry to try again with the same key.")


# -- accounts: deriving an address, and remembering the ones this browser saw ---

# plan §C3.2: creation asks for a NAME, and when the address is left blank the
# browser derives one from the name and sends it *explicitly*. This function is
# the whole of that derivation, so the address a name gets is a function of the
# name and nothing else — no clock, no counter, no randomness. That determinism
# is a money property, not a nicety: the account id is the ledger's
# exactly-once guard (§C2.1; ``api/validation.py`` requires one and the server
# never mints), so a retry of the same submission — including a retry after an
# outcome the browser never saw — derives the *same* id and earns the API's 409
# instead of opening a SECOND account carrying a SECOND opening grant. An id
# minted per attempt would satisfy the form and defeat the guard.
_SLUG_MAX = 40


def _derive_account_id(owner_id: str) -> str:
    """``acct-`` + an ASCII slug of the name. Readable, stable, retry-safe.

    ASCII-only deliberately: this id is quoted into a URL, into the create
    response and into the ``pocketful_accounts`` cookie, while the account name —
    free-form and possibly not Latin — is carried separately by ``owner_id``. A
    name with no ASCII alphanumerics at all falls back to a digest of the *name*,
    which is still a pure function of it, and so still retry-stable.
    """
    slug: list[str] = []
    pending_dash = False
    for ch in owner_id.lower():
        if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            if pending_dash and slug:
                slug.append("-")
            pending_dash = False
            slug.append(ch)
        else:
            pending_dash = True
    text = "".join(slug)[:_SLUG_MAX].strip("-")
    if not text:
        text = "u" + hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:12]
    return "acct-" + text


# The cookie that lists the ids this browser has seen (plan §C3.3): ids only,
# never a name and never a balance. It is read to build the switch list and for
# nothing else — it never answers "which account is this request about?".
_COOKIE_NAME = "pocketful_accounts"
_COOKIE_MAX = 12
# 30 days. The plan pins the shape (Path, HttpOnly, SameSite, the cap of 12), not
# the lifetime. A session cookie would forget the switch list on every browser
# restart while the pending store — the keys, the one thing that must not be
# lost — lives on disk; that asymmetry is the wrong way round.
_COOKIE_MAX_AGE = 30 * 24 * 60 * 60


def _remembered_account_ids(cookie_header: str | None) -> list[str]:
    """The ids from the one cookie, or ``[]``. Never anything but ids.

    An unreadable cookie is an empty list rather than an error: the cookie is a
    convenience, so a value this build cannot parse costs a switch list and
    nothing else. A name or a balance cannot get in even if a hand-written cookie
    tries — only ``str`` entries survive, and each is used as an account id.
    """
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name != _COOKIE_NAME:
            continue
        try:
            decoded = json.loads(unquote(value))
        except (TypeError, ValueError):
            return []
        if not isinstance(decoded, list):
            return []
        return [item for item in decoded
                if isinstance(item, str) and item.strip()][:_COOKIE_MAX]
    return []


def _account_cookie(account_ids: list[str]) -> str:
    """The ``Set-Cookie`` value for the ids being remembered.

    The payload is the JSON list §C3.3 specifies, percent-encoded so the header
    is a legal ``cookie-value`` (RFC 6265 excludes ``"`` ``,`` and ``;`` from the
    raw form) and so an id containing a comma cannot split the list in two.
    """
    payload = json.dumps(account_ids[:_COOKIE_MAX], separators=(",", ":"))
    return (f"{_COOKIE_NAME}={quote(payload, safe='')}; Path=/; HttpOnly; "
            f"SameSite=Lax; Max-Age={_COOKIE_MAX_AGE}")


def _resolve_account(values: dict[str, list[str]], default: str) -> str:
    """Answer "which account is this request about?" in exactly one place.

    Non-empty ``account`` wins; absent or empty falls back to the boot
    ``--account``, which is the pre-existing behaviour byte for byte. The
    renderer and the send handler both call this, so the account a page shows
    and the account a POST acts as cannot drift apart.

    The value is client-supplied and is therefore a capability, not a boundary
    (see ``DEMO_NOTICE``); it is also not validated here — an id that does not
    exist is answered by the API and rendered as a named page, not a crash.
    """
    candidate = ((values.get("account") or [""])[0] or "").strip()
    return candidate or default


# -- the HTTP surface ----------------------------------------------------------

class WalletUIHandler(BaseHTTPRequestHandler):
    server_version = "PocketfulUI/1.0"
    protocol_version = "HTTP/1.1"
    # Set by ``_send_bytes`` the instant a status line is committed for this
    # request. ``do_POST`` reads it on the way out (see ``do_POST``).
    _responded: bool = False

    def log_message(self, fmt: str, *args) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    # -- plumbing -------------------------------------------------------------

    def _send_bytes(self, status: int, content_type: str, data: bytes,
                    extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        # A status line for this request is committed from here on: the exit
        # guard in ``do_POST`` must not try to answer a second time.
        self._responded = True
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _html(self, status: int, markup: str,
              extra: dict[str, str] | None = None) -> None:
        self._send_bytes(status, "text/html; charset=utf-8", markup.encode("utf-8"),
                         extra)

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self._send_bytes(status, "application/json", data)

    def _redirect(self, location: str) -> None:
        # 303: the browser follows with a GET, so a refresh cannot replay the POST.
        self._send_bytes(303, "text/plain; charset=utf-8", b"",
                         {"Location": location})

    def _render(self, *, account_id: str | None = None,
                message: str | None = None,
                notice: str | None = None, status: int = 200,
                create_error: str | None = None,
                create_name: str | None = None,
                create_address: str | None = None) -> None:
        base: Wallet = self.server.wallet
        # ``account_id=None`` means the boot account, the same fallback
        # ``_resolve_account`` applies to an absent field.
        account_id = account_id or base.account_id
        # One wallet per request over the *same* client and the *same* store:
        # switching accounts moves no file and mints no key.
        wallet = Wallet(base.client, base.store, account_id)
        try:
            with self.server.lock:
                balance = wallet.balance_minor()
                rows = wallet.activity(limit=50)
                pending = base.store.outstanding()
        except ApiError as exc:
            if exc.status == 404:
                # An id the API does not know is a page naming it, not a 502:
                # the browser asked for an account, so it is told there is none.
                self._html(200, render_error(
                    f"There is no account {account_id!r}. Nothing was read and "
                    "nothing was sent.", 200))
                return
            self._html(502, render_error(
                f"The API did not answer ({exc.code or 'transport error'}).", 502))
            return
        except WalletProtocolError as exc:
            self._html(502, render_error(f"Unexpected API answer: {exc}", 502))
            return
        accounts, cookie = self._remember_accounts(wallet)
        self._html(status, render_wallet(
            account_id=wallet.account_id,
            balance_minor=balance,
            currency=wallet.currency,
            rows=rows,
            pending=pending,
            message=message,
            notice=notice,
            # The name comes from the same balance read as the balance itself
            # (§C2.5), so the heading and the figure cannot come from different
            # moments or different accounts.
            name=wallet.owner_id,
            accounts=accounts,
            create_error=create_error,
            create_name=create_name,
            create_address=create_address,
        ), {"Set-Cookie": cookie})

    def _probe_account(self, account_id: str) -> dict:
        """What the server says about one remembered id: its name, or not-found.

        A non-404 failure is *not* proof that the account is gone, so the entry
        stays a link with no invented name and no invented balance — absence of
        proof is not proof of absence. Only the API's own 404 marks an id
        unavailable (§C3.4), and that is the verdict the caller acts on.
        """
        base: Wallet = self.server.wallet
        probe = Wallet(base.client, base.store, account_id)
        try:
            with self.server.lock:
                probe.balance_minor()
        except ApiError as exc:
            if exc.status == 404:
                return {"account_id": account_id, "name": "", "available": False}
            # Anything else (a 5xx, a transport failure) falls through: the id is
            # kept and rendered exactly as a remembered id with no name.
        except (WalletProtocolError, OSError):
            pass
        except http.client.HTTPException:
            # An id this transport cannot even phrase a request for (a control
            # character surviving a hand-written cookie, before any encoding
            # rule could help) is not evidence the account is gone. Keep the
            # entry, invent no name, and answer the page: §C3.4 says the page
            # never crashes, and "cannot ask" is not "not there".
            pass
        return {"account_id": account_id, "name": probe.owner_id or "",
                "available": True}

    def _remember_accounts(self, active: Wallet) -> tuple[list[dict], str]:
        """The switch list (plan §C3.3/§C3.4) and the cookie that records it.

        The list starts with the account being rendered — already read for this
        page, so its name and its balance came from one response — then the ids
        the browser sent, each verified against the server. Every name in the
        list is therefore the server's, an id the server 404s is marked
        unavailable rather than given a balance, and that same id is left out of
        the cookie: "dropped on the next write", in the one render that still
        shows it.
        """
        remembered = _remembered_account_ids(self.headers.get("Cookie"))
        candidates = [active.account_id] + [item for item in remembered
                                            if item != active.account_id]
        entries: list[dict] = []
        kept: list[str] = []
        for account_id in candidates:
            if any(entry["account_id"] == account_id for entry in entries):
                continue
            if account_id == active.account_id:
                entry = {"account_id": account_id, "name": active.owner_id or "",
                         "available": True}
            else:
                entry = self._probe_account(account_id)
            entries.append(entry)
            if entry["available"]:
                kept.append(account_id)
        return entries, _account_cookie(kept)

    def _form(self) -> dict[str, list[str]]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        body = self.rfile.read(length)
        try:
            return parse_qs(body.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            return {}

    # -- routes ---------------------------------------------------------------

    def do_GET(self) -> None:
        """One exit guard for the whole GET surface — see the ``finally``.

        Same shape as ``do_POST``'s, for the same reason: the list of exception
        types a render can raise is only ever the list someone already imagined,
        and a GET answered by nothing (the socket closes with no status line) is
        worse than a wrong page — a browser reloading a create page it never got
        an answer for is a user retyping into a fresh form. A page id is not a
        path segment, so an id from the query string or from a hand-written
        cookie reaches the transport; whatever it does there, this GET answers.
        """
        self._responded = False
        try:
            parts = urlsplit(self.path)
            if parts.path == "/health":
                self._json(200, {"status": "ok"})
                return
            if parts.path == "/":
                self._render_feed(parse_qs(parts.query, keep_blank_values=True))
                return
            self._json(404, {"error": "not_found"})
        except Exception:
            LOG.exception("unhandled error answering GET %s", self.path)
        finally:
            if not self._responded:
                try:
                    self._html(500, render_error(
                        "The page could not be built. Nothing was sent and "
                        "nothing moved.", 500))
                except Exception:
                    LOG.exception("could not send the fallback 500; connection closed")

    def do_POST(self) -> None:
        """One exit guard for the whole POST surface — see the ``finally``."""
        path = urlsplit(self.path).path
        self._responded = False
        try:
            if path == "/send":
                self._handle_send()
            elif path == "/retry":
                self._handle_retry()
            elif path == "/create":
                self._handle_create()
            else:
                self._json(404, {"error": "not_found"})
        except Exception:
            LOG.exception("unhandled error answering POST %s", path)
        finally:
            if not self._responded:
                # Every exit from a POST must leave the browser on a GET. That
                # is not a property of the handlers' `except` lists, which can
                # only ever name the failures someone already imagined; it is a
                # property of *exiting do_POST*. An exception type nobody
                # enumerated, a handler added to this dispatch later, and a
                # `return` that forgot to answer are all the same thing here:
                # no status line was committed, so the browser is still on the
                # POST, and its reload is a re-POST — which mints a new key and
                # moves money twice. Answering 303 unconditionally closes that
                # door without needing to name how it was reached.
                try:
                    self._redirect("/?error=internal")
                except Exception:
                    LOG.exception("could not send the fallback 303; connection closed")

    def _render_feed(self, query: dict[str, list[str]]) -> None:
        """GET / — the only place a send/retry outcome is ever rendered.

        Every POST answers 303 and lands here, so the page a browser is showing
        after a send is the result of a GET. A refresh therefore re-issues a GET,
        which mints nothing, instead of re-issuing the POST, which would.
        """
        def first(name: str) -> str:
            return (query.get(name) or [""])[0]

        message: str | None = None
        notice: str | None = None
        create_error: str | None = None
        error = first("error")
        if error:
            if error in _CREATE_ERROR_CODES:
                # A refusal of the create form is rendered *in the create card*,
                # beside the fields it is about and with the typing still in
                # them, rather than as a page banner over a form that is empty
                # again. The control is named by the same code that produced it.
                create_error = _ERROR_MESSAGES.get(
                    error, "That account could not be created.")
                if error == "name_taken":
                    # The refusal that names its subject, from the parameter the
                    # redirect carries (``_handle_create``). The static sentence
                    # above is the fallback, not dead code: ``/?error=name_taken``
                    # is a URL a person can type and it has no address in it, and
                    # formatting a template with a missing key would raise at the
                    # lookup rather than answer (§C3.4 — the page always answers).
                    taken_id = first("taken_id")
                    if taken_id:
                        create_error = _NAME_TAKEN_TEMPLATE.format(address=taken_id)
            else:
                message = _ERROR_MESSAGES.get(error, f"The transfer failed ({error}).")
        if first("pending"):
            notice = _PENDING_NOTICE
        sent = first("sent")
        if sent:
            notice = f"Transfer {sent}."
        retried = first("retried")
        if retried:
            notice = ("No unconfirmed transfers remained." if retried == "none"
                      else f"Retry {retried}.")
        self._render(account_id=_resolve_account(query, self.server.wallet.account_id),
                     message=message, notice=notice,
                     create_error=create_error,
                     # Echoed straight back so a refusal costs no typing: the
                     # form is rebuilt from the query with the name and the
                     # address that were submitted (plan §C3.2, "the form still
                     # filled in").
                     create_name=first("new_name"),
                     create_address=first("new_id"))

    def _handle_send(self) -> None:
        form = self._form()
        base: Wallet = self.server.wallet
        # Rule 0: the page and the action resolve the account the same way.
        wallet = Wallet(base.client, base.store,
                        _resolve_account(form, base.account_id))
        to_account = (form.get("to_account_id") or [""])[0].strip()
        amount_text = (form.get("amount") or [""])[0].strip()
        if not to_account or not amount_text:
            # No key has been minted at this point; nothing to recover.
            self._redirect("/?error=missing_fields")
            return
        try:
            with self.server.lock:
                outcome = wallet.send(to_account, amount_text)
        except InvalidMoneyInput:
            # Rejected locally, before any key is minted or anything persisted.
            self._redirect("/?error=invalid_amount")
        except SameAccountSend:
            self._redirect("/?error=same_account")
        except RetryableSendError:
            # The key is minted and persisted; the record stays pending.
            self._redirect("/?pending=1")
        except SendProtocolError:
            # A 2xx we could not interpret. No key was re-minted and the record
            # is still pending, so Retry replays it; the browser is sent to a
            # GET like every other outcome.
            self._redirect("/?error=protocol")
        except IdempotencyConflictForSend:
            self._redirect("/?error=conflict")
        except TerminalSendError as exc:
            self._redirect(f"/?error={quote(exc.error.code or 'refused', safe='')}")
        except ApiError:
            self._redirect("/?error=api")
        else:
            self._redirect(f"/?sent={quote(outcome.status, safe='')}")

    def _handle_create(self) -> None:
        """POST /create — the API's ``POST /accounts``, and nothing else.

        It mints no idempotency key and touches no store: creating an account
        moves no money, so there is nothing to be idempotent about. Every
        branch answers 303 into a GET like every other POST.

        **The blank address is filled in here, deterministically.** plan §C3.2:
        creation asks for a name, and if the address is blank the browser derives
        one from the name and sends it explicitly — the server still never mints
        (§C2.1). ``_derive_account_id`` is a function of the submitted name alone,
        which is what keeps the exactly-once guard intact on this path: the same
        submission always asks for the same id, so a retry after an outcome the
        browser never saw earns the API's 409 rather than a second account with a
        second opening grant.

        A refusal is reported by the *code*, and answered into a GET that still
        carries the name and the address the user typed, so the card can be
        re-rendered with them rather than blank.
        """
        form = self._form()
        owner_id = ((form.get("owner_id") or [""])[0]).strip()
        currency = ((form.get("currency") or [""])[0]).strip() or "USD"
        typed = ((form.get("account_id") or [""])[0]).strip()
        echo = (f"&new_name={quote(owner_id, safe='')}"
                f"&new_id={quote(typed, safe='')}")
        if not owner_id:
            self._redirect("/?error=missing_owner" + echo)
            return
        account_id = typed or _derive_account_id(owner_id)
        client = self.server.wallet.client
        try:
            with self.server.lock:
                account = client.create_account(owner_id=owner_id,
                                                currency=currency,
                                                account_id=account_id)
        except ApiError as exc:
            code = exc.code or "api"
            if code in ("invalid_request", "validation_failed"):
                code = "invalid_account"
            elif code == "account_exists" and not typed:
                # The id was derived from the name, so what is taken is the
                # **address** the name derives (§C3.2) — not the name, which is
                # free. The address is carried to the render side in its own
                # parameter rather than in ``new_id``: that field prefills the
                # address input, so filling it would turn a blank-address
                # resubmit into an explicit-id one and stop exercising this very
                # branch (and its ``account_exists`` sibling would answer next).
                code = "name_taken"
                echo += f"&taken_id={quote(account_id, safe='')}"
            self._redirect(f"/?error={quote(code, safe='')}" + echo)
            return
        new_id = account.get("account_id") if isinstance(account, dict) else None
        if not isinstance(new_id, str) or not new_id:
            # The API created something we cannot name; do not pretend to show it.
            self._redirect("/?error=api" + echo)
            return
        self._redirect(f"/?account={quote(new_id, safe='')}")

    def _handle_retry(self) -> None:
        wallet: Wallet = self.server.wallet
        try:
            with self.server.lock:
                outcomes = resume(wallet.store, wallet.client)
        except RetryableSendError:
            self._redirect("/?pending=1")
        except SendProtocolError:
            # ``resume`` propagated an unreadable 2xx for one of the records.
            # Those records stay pending; the outcome is a GET like any other.
            self._redirect("/?error=protocol")
        except (IdempotencyConflictForSend, TerminalSendError):
            self._redirect("/?error=conflict")
        except ApiError:
            self._redirect("/?error=api")
        else:
            status = outcomes[0].status if outcomes else "none"
            self._redirect(f"/?retried={quote(status, safe='')}")


class WalletUIServer(ThreadingHTTPServer):
    """Threaded UI server carrying the wallet and a lock for its store."""

    daemon_threads = True

    def __init__(self, address, handler, *, wallet: Wallet) -> None:
        super().__init__(address, handler)
        self.wallet = wallet
        self.lock = threading.RLock()


def create_ui_server(*, api_base_url: str, store_path: str, account_id: str,
                     host: str = "127.0.0.1", port: int = 8080,
                     client: ApiClient | None = None) -> WalletUIServer:
    """Build the UI server. Port 0 binds an ephemeral port (used by the tests)."""
    store = PendingStore(store_path)
    api = client if client is not None else ApiClient(api_base_url)
    return WalletUIServer((host, port), WalletUIHandler,
                          wallet=Wallet(api, store, account_id))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.web", description="Pocketful wallet UI")
    parser.add_argument("--api", default=os.environ.get("POCKETFUL_API", "http://127.0.0.1:8000"))
    parser.add_argument("--store", default=os.environ.get("POCKETFUL_UI_STORE", "ui-pending.json"))
    parser.add_argument("--account", default=os.environ.get("POCKETFUL_ACCOUNT", "acct-alice"))
    parser.add_argument("--host", default=os.environ.get("POCKETFUL_UI_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("POCKETFUL_UI_PORT", "8080")))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    server = create_ui_server(api_base_url=args.api, store_path=args.store,
                              account_id=args.account, host=args.host, port=args.port)
    LOG.info("wallet UI on http://%s:%s (account=%s, api=%s, store=%s)",
             server.server_address[0], server.server_address[1],
             args.account, args.api, args.store)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
