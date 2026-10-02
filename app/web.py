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
* Rendering (``GET /``) never writes. Reloading the page cannot mint a key.

**Accounts are presentational and multi-account is stateless.** ``GET
/?account=<id>`` renders that account's balance and activity; a bare ``GET /``
and an empty ``?account=`` fall back to the boot ``--account``. The active
account then rides with the POST that acts on it in a hidden ``account`` field
resolved by :func:`_resolve_account` — the *same* helper the renderer uses, so
the page and the action cannot disagree about who the sender is. It is not a
cookie and not server state: the request carries it or it does not.

That is a change in kind, stated rather than implied: the field is
client-supplied, so **an account id is the capability**. The UI no longer
confines sends to the boot account — anyone who knows an id can act as it. On
this demo that was already the model (ids are minted uuid hex and shown only to
their creator), and ``DEMO_NOTICE`` says so on every page.

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
import html
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from .client import ApiClient, ApiError
from .keystore import PendingStore
from .money import InvalidMoneyInput, format_minor
from .send import (IdempotencyConflictForSend, RetryableSendError, SendProtocolError,
                   TerminalSendError, resume)
from .wallet import ActivityRow, SameAccountSend, Wallet, WalletProtocolError

LOG = logging.getLogger("app.web")


# -- rendering (pure string functions, escaped at the edge) --------------------

_STYLE = """
:root { color-scheme: light dark; }
body { font: 16px/1.5 system-ui, sans-serif; max-width: 44rem; margin: 2rem auto;
       padding: 0 1rem; }
h1 { font-size: 1.35rem; } h2 { font-size: 1.05rem; margin-top: 2rem; }
.balance { font-size: 2rem; font-weight: 600; margin: .25rem 0 1rem; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: .35rem .5rem; border-bottom: 1px solid #8884; }
td.amt { text-align: right; font-variant-numeric: tabular-nums; }
form.inline { display: flex; gap: .5rem; flex-wrap: wrap; align-items: end;
              margin: .75rem 0; }
label { display: flex; flex-direction: column; font-size: .85rem; gap: .2rem; }
input { padding: .4rem; font: inherit; }
button { padding: .45rem .9rem; font: inherit; cursor: pointer; }
.notice { padding: .6rem .8rem; border-radius: .4rem; background: #ffd8; }
.error  { padding: .6rem .8rem; border-radius: .4rem; background: #fdd; }
.pending { padding: .6rem .8rem; border-radius: .4rem; background: #ffd8; }
.demo { padding: .5rem .8rem; border-radius: .4rem; background: #eef3ff;
        border: 1px solid #88a; font-size: .85rem; }
code { font-size: .85em; }
"""

# The one line that has to be on every page. The UI is not a boundary: an
# account id in the hidden field is the whole capability, and the money is
# fixture data. Kept as a constant so a test can import the marker rather than
# re-spell it, and rendered by ``_document`` so no page can be built without it.
DEMO_NOTICE = ("Unauthenticated demo: an account id is the only credential, and "
               "these balances are not real money.")


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _activity_rows(rows: list[ActivityRow]) -> str:
    if not rows:
        return "<tr><td colspan='4'>No activity yet.</td></tr>"
    cells = []
    for row in rows:
        sign = "-" if row.direction == "debit" else "+"
        cells.append(
            "<tr>"
            f"<td>{_esc(row.created_at)}</td>"
            f"<td>{_esc(row.direction)}</td>"
            f"<td>{_esc(row.counterparty_account_id)}</td>"
            f"<td class='amt'>{_esc(sign + row.amount_display)}</td>"
            "</tr>")
    return "".join(cells)


def _pending_block(pending: list) -> str:
    if not pending:
        return ""
    items = "".join(
        f"<li><code>{_esc(rec.created_at)}</code> — send "
        f"{_esc(format_minor(rec.amount_minor, rec.currency))} from "
        f"{_esc(rec.from_account_id)} to {_esc(rec.to_account_id)} "
        f"<em>(not confirmed)</em></li>"
        for rec in pending)
    return (
        "<section><h2>Unconfirmed transfers</h2>"
        "<p class='pending'>These were sent but not confirmed. Pressing Retry "
        "reuses the key already stored on the server — it cannot create a second "
        "transfer.</p>"
        f"<ul>{items}</ul>"
        "<form method='post' action='/retry'>"
        "<button type='submit'>Retry</button></form></section>")


def _document(*, title: str, body: str) -> str:
    """The single place a ``text/html`` document is assembled.

    Both builders go through here, so the demo notice is a property of emitting
    HTML rather than something each page has to remember: a page cannot be
    built without it, the same way a POST cannot exit without answering.
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>Pocketful</h1>
<p class="demo" role="note">{_esc(DEMO_NOTICE)}</p>
{body}
</body>
</html>
"""


def render_wallet(*, account_id: str, balance_minor: int, currency: str,
                  rows: list[ActivityRow], pending: list,
                  message: str | None = None, notice: str | None = None) -> str:
    """Render the wallet view. Contains no idempotency key and no key input field.

    The only hidden field is the active account id, which is not a secret and
    not a key: it says which account the form acts as.
    """
    balance = format_minor(balance_minor, currency)
    banner = ""
    if message:
        banner += f"<p class='error' role='alert'>{_esc(message)}</p>"
    if notice:
        banner += f"<p class='notice' role='status'>{_esc(notice)}</p>"
    body = f"""<p>Account <code>{_esc(account_id)}</code></p>
<div class="balance">{_esc(balance)}</div>
{banner}
<section>
<h2>Send money</h2>
<form class="inline" method="post" action="/send">
<input type="hidden" name="account" value="{_esc(account_id)}">
<label>To account
<input name="to_account_id" required autocomplete="off" placeholder="acct-bob"></label>
<label>Amount
<input name="amount" required inputmode="decimal" autocomplete="off" placeholder="12.34"></label>
<button type="submit">Send</button>
</form>
<p><small>Amounts are sent as whole minor units; more than two decimal places is
rejected, never rounded.</small></p>
</section>
{_pending_block(pending)}
<section>
<h2>Create an account</h2>
<form class="inline" method="post" action="/create">
<label>Owner
<input name="owner_id" required autocomplete="off" placeholder="alice"></label>
<label>Currency
<input name="currency" value="USD" autocomplete="off"></label>
<label>Account id (optional)
<input name="account_id" autocomplete="off" placeholder="blank, or an id you choose"></label>
<button type="submit">Create</button>
</form>
<p><small>Creating an account mints no key and moves no money. The new id then
appears at the top of this page — share it with whoever will pay you.</small></p>
</section>
<section>
<h2>Activity</h2>
<table>
<thead><tr><th>When</th><th>Direction</th><th>Counterparty</th><th class="amt">Amount</th></tr></thead>
<tbody>{_activity_rows(rows)}</tbody>
</table>
<p><small>Balance and activity are the server's values, shown verbatim.</small></p>
</section>
"""
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
    "missing_owner": "Enter an owner for the account you are creating.",
    "account_exists": "That account id is already taken. Nothing was created.",
    "invalid_account": "The API refused those account details. Nothing was created.",
    "invalid_request": "The API refused those account details. Nothing was created.",
}

_PENDING_NOTICE = ("The transfer could not be confirmed and is recorded as pending. "
                   "Nothing was sent twice. Press Retry to try again with the same key.")


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

    def _html(self, status: int, markup: str) -> None:
        self._send_bytes(status, "text/html; charset=utf-8", markup.encode("utf-8"))

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self._send_bytes(status, "application/json", data)

    def _redirect(self, location: str) -> None:
        # 303: the browser follows with a GET, so a refresh cannot replay the POST.
        self._send_bytes(303, "text/plain; charset=utf-8", b"",
                         {"Location": location})

    def _render(self, *, account_id: str | None = None,
                message: str | None = None,
                notice: str | None = None, status: int = 200) -> None:
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
        self._html(status, render_wallet(
            account_id=wallet.account_id,
            balance_minor=balance,
            currency=wallet.currency,
            rows=rows,
            pending=pending,
            message=message,
            notice=notice,
        ))

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
        parts = urlsplit(self.path)
        if parts.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if parts.path == "/":
            self._render_feed(parse_qs(parts.query, keep_blank_values=True))
            return
        self._json(404, {"error": "not_found"})

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
        error = first("error")
        if error:
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
                     message=message, notice=notice)

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
        """
        form = self._form()
        owner_id = ((form.get("owner_id") or [""])[0]).strip()
        currency = ((form.get("currency") or [""])[0]).strip() or "USD"
        account_id = ((form.get("account_id") or [""])[0]).strip() or None
        if not owner_id:
            self._redirect("/?error=missing_owner")
            return
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
            self._redirect(f"/?error={quote(code, safe='')}")
            return
        new_id = account.get("account_id") if isinstance(account, dict) else None
        if not isinstance(new_id, str) or not new_id:
            # The API created something we cannot name; do not pretend to show it.
            self._redirect("/?error=api")
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
