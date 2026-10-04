"""HTTP surface for Pocketful — routing, serialization, and the error boundary.

This module owns the seam between the wire and the ledger. It opens **no
database connection of its own**: every connection comes from
:func:`ledger.db.connect`, the ledger's only factory, exactly as plan §2.1
requires. All money semantics — double entry, idempotency, overdraft, currency
agreement — belong to :class:`ledger.core.Ledger`; this module validates the
wire, calls the ledger, and maps its refusals to §2.2 statuses.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from ledger import (
    OPENING_GRANT_MINOR,
    SYSTEM_ACCOUNT_CURRENCY,
    AccountExists,
    Ledger,
    connect,
)
from ledger.types import Account, ActivityItem, LedgerError, TransferResult

from . import errors, validation

LOG = logging.getLogger("pocketful.api")

# A transfer body is a handful of small fields. 1 MiB is already absurdly
# generous; beyond it the request is refused rather than buffered.
MAX_BODY_BYTES = 1_048_576


class LedgerPool:
    """Thread-local ledger handles built from the ledger's connection factory.

    One connection per worker thread is the ledger's documented concurrency
    model ("real write concurrency still happens at the SQLite level when
    callers use one connection per thread"). The connection is created by
    ``ledger.db.connect`` — never by this package.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list = []

    def connection(self) -> Any:
        conn = getattr(self._local, "connection", None)
        if conn is None:
            conn = connect(self._db_path)
            self._local.connection = conn
            self._local.ledger = Ledger(conn)
            with self._lock:
                self._connections.append(conn)
        return conn

    def ledger(self) -> Ledger:
        self.connection()
        return self._local.ledger

    def close_current_thread(self) -> None:
        """Close the calling thread's connection, in the thread that opened it.

        ``sqlite3.Connection.close()`` is thread-affine, exactly like every other
        sqlite3 operation: called from a thread other than the creator it raises
        ``ProgrammingError``. So a worker thread's connection can only be closed
        by that worker, on its way out — which is why this exists and why
        :meth:`close` cannot do the job for a thread that has already exited.
        """
        conn = getattr(self._local, "connection", None)
        if conn is None:
            return
        # Drop the thread-local reference first, so this connection is unreachable
        # from the pool whether or not the close below succeeds.
        self._local.connection = None
        self._local.ledger = None
        with self._lock:
            try:
                self._connections.remove(conn)
            except ValueError:
                pass
        try:
            conn.close()
        except Exception:
            # A failed close is not silent: the connection stays open until the
            # process exits, which is worth a warning even during shutdown.
            LOG.warning("worker connection failed to close", exc_info=True)

    def close(self) -> None:
        """Close any connection still tracked. Best effort, but never silent.

        Worker threads close their own connections as they exit
        (:meth:`close_current_thread`), so on a clean shutdown this finds nothing.
        Anything still here was opened on a thread this call is not running on —
        the one case sqlite3 refuses to close — and that refusal is logged at
        warning rather than swallowed: a failure path that reports nothing is a
        failure path that is not there.
        """
        with self._lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except Exception:
                LOG.warning("connection close failed during shutdown", exc_info=True)


# -- serialization ------------------------------------------------------------
#
# Each function produces exactly the §2.2 shape for its endpoint — no extra
# keys, no missing ones. Money crosses this boundary as a non-negative integer.


def account_to_wire(account: Account, *, balance_minor: int) -> dict:
    return {
        "account_id": account.account_id,
        "owner_id": account.owner_id,
        "currency": account.currency,
        "balance_minor": balance_minor,
        "allow_overdraft": account.allow_overdraft,
    }


def transfer_to_wire(result: TransferResult) -> dict:
    return {
        "transfer_id": result.transfer_id,
        "status": result.status,
        "from_account_id": result.from_account_id,
        "to_account_id": result.to_account_id,
        "amount_minor": result.amount_minor,
        "currency": result.currency,
        "created_at": result.created_at,
    }


def activity_to_wire(item: ActivityItem) -> dict:
    """§2.1 uniform wire rule: the amount is non-negative, the sign is explicit.

    ``list_activity`` already applies the ledger's single sign mapping —
    ``direction = "debit" if entry < 0 else "credit"`` and
    ``amount_minor = abs(entry)`` — so the wire passes ``direction`` through and
    takes the magnitude again. ``abs()`` here is the boundary's own guarantee:
    whatever the ledger hands over, no signed amount can leave this endpoint.
    """
    return {
        "entry_id": item.entry_id,
        "transfer_id": item.transfer_id,
        "direction": item.direction,
        "amount_minor": abs(int(item.amount_minor)),
        "balance_after_minor": int(item.balance_after_minor),
        "counterparty_account_id": item.counterparty_account_id,
        # §C2.5: the counterparty's owner, beside its id and never instead of it.
        # ``list_activity`` resolves it in the same read; this boundary only
        # projects it, so the client never has to turn an id into a name itself.
        "counterparty_owner_id": item.counterparty_owner_id,
        "created_at": item.created_at,
    }


def _first(query: dict[str, list[str]], name: str) -> Optional[str]:
    values = query.get(name)
    if not values:
        return None
    return values[0]


def _next_cursor(items: list[ActivityItem], limit: int) -> Optional[str]:
    """A full page implies there may be more; the next page starts strictly older."""
    if limit <= 0 or len(items) < limit:
        return None
    return items[-1].entry_id


class PocketfulHandler(BaseHTTPRequestHandler):
    server_version = "Pocketful/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            status, payload, headers = self._route(method)
        except errors.ApiError as exc:
            # Every refusal is logged with the detail needed to act on, and is
            # never converted into a success.
            LOG.warning("%s %s -> %s %s: %s", method, self.path,
                        exc.status, exc.code, exc.detail or "-")
            status, payload, headers = exc.status, exc.body(), exc.headers
        except Exception:
            LOG.exception("unhandled error on %s %s", method, self.path)
            status, payload, headers = 500, {"error": errors.INTERNAL_ERROR}, None
        self._respond(status, payload, headers)

    def _respond(self, status: int, payload: dict,
                 extra_headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _read_body(self) -> Optional[bytes]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return None
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise errors.ApiError(400, errors.MALFORMED_JSON,
                                  "invalid Content-Length") from exc
        if length < 0:
            raise errors.ApiError(400, errors.MALFORMED_JSON, "negative Content-Length")
        if length > MAX_BODY_BYTES:
            raise errors.ApiError(413, errors.PAYLOAD_TOO_LARGE,
                                  f"body exceeds {MAX_BODY_BYTES} bytes")
        if length == 0:
            return None
        return self.rfile.read(length)

    # -- routing --------------------------------------------------------------

    def _route(self, method: str) -> tuple[int, dict, dict[str, str] | None]:
        parts = urlsplit(self.path)
        segments = [unquote(segment) for segment in parts.path.split("/") if segment != ""]
        query = parse_qs(parts.query, keep_blank_values=True)

        if segments == ["accounts"]:
            self._require_method(method, "POST")
            return self._post_accounts()

        if segments == ["transfers"]:
            self._require_method(method, "POST")
            return self._post_transfers()

        if len(segments) == 3 and segments[0] == "accounts":
            if segments[2] == "balance":
                self._require_method(method, "GET")
                return self._get_balance(segments[1])
            if segments[2] == "activity":
                self._require_method(method, "GET")
                return self._get_activity(segments[1], query)

        raise errors.ApiError(404, errors.NOT_FOUND,
                              f"no route for {method} {parts.path}")

    @staticmethod
    def _require_method(actual: str, expected: str) -> None:
        if actual != expected:
            raise errors.ApiError(405, errors.METHOD_NOT_ALLOWED,
                                  f"method {actual} is not allowed here",
                                  headers={"Allow": expected})

    # -- handlers -------------------------------------------------------------

    def _post_accounts(self) -> tuple[int, dict, None]:
        raw = validation.require_object(
            validation.parse_json_body(self._read_body()), what="account body")
        owner_id = validation.require_non_empty_str(raw, "owner_id")
        currency = validation.require_currency(raw)
        allow_overdraft = validation.require_bool(raw, "allow_overdraft", False)
        # §C2.1: required and client-supplied — the server never mints one. The
        # caller's id is what makes the ledger's accounts PRIMARY KEY the
        # exactly-once guard (§C1.5), so a retry earns a 409 rather than a
        # second account carrying a second $100.
        account_id = validation.require_account_id(raw)

        # §C2.3 and the currency ruling that goes with it. The grant is the
        # API's POLICY (the ledger's own default is 0, §C1.6) and it is USD-only
        # because the system account it posts against is single-currency
        # (§C1.2) and INVARIANTS §1 forbids cross-currency arithmetic without a
        # recorded rate. A non-USD account still opens — ungranted, at 0. What
        # is restricted is the grant, not the generality of creation.
        opening_grant_minor = (
            OPENING_GRANT_MINOR if currency == SYSTEM_ACCOUNT_CURRENCY else 0
        )

        try:
            account = self.server.pool.ledger().open_account(
                account_id=account_id,
                owner_id=owner_id,
                currency=currency,
                allow_overdraft=allow_overdraft,
                opening_grant_minor=opening_grant_minor,
            )
        except AccountExists as exc:
            # Reject; never hand back the existing account (§2.1, §C2.4:
            # deliberately not idempotent). The duplicate fails on the first
            # INSERT, before the transfer or any entry is touched, so the
            # refusal posts no second grant.
            raise errors.account_exists_error(exc) from exc
        except LedgerError as exc:
            # §C1.7: a reserved id is refused by the ledger and MUST reach the
            # client as a 4xx. Falling through to the generic 500 would report a
            # policy refusal as a crash and let a client tell the two apart.
            raise errors.from_ledger_error(exc) from exc

        LOG.info("account opened: %s (grant %s minor)", account.account_id,
                 opening_grant_minor)
        # A brand-new account's only entry is the grant posted in the same
        # transaction (§C1.4), so its derived balance is exactly that grant:
        # 10000 for a granted USD account, 0 for an ungranted one. This reports
        # the value the ledger actually posted rather than a hard-coded 0, so it
        # stays honest when the policy changes.
        return 201, account_to_wire(account, balance_minor=opening_grant_minor), None

    def _get_balance(self, account_id: str) -> tuple[int, dict, None]:
        try:
            # get_account proves existence (§2.1: raises UnknownAccount) and
            # supplies the currency; get_balance supplies the derived sum. Both
            # are total reads — a known account with a zero balance is 200, an
            # unknown id is 404, and neither can be mistaken for the other.
            ledger = self.server.pool.ledger()
            account = ledger.get_account(account_id)
            balance_minor = ledger.get_balance(account_id)
        except LedgerError as exc:
            raise errors.from_ledger_error(exc) from exc
        return 200, {
            "account_id": account.account_id,
            # §C2.5: the owner rides the read path, so the client prints the
            # name it is given instead of holding an id→name map of its own.
            "owner_id": account.owner_id,
            "currency": account.currency,
            "balance_minor": balance_minor,
        }, None

    def _get_activity(self, account_id: str,
                      query: dict[str, list[str]]) -> tuple[int, dict, None]:
        limit = validation.parse_limit(_first(query, "limit"))
        before = validation.parse_before(_first(query, "before"))
        try:
            items = self.server.pool.ledger().list_activity(
                account_id, limit=limit, before_entry_id=before)
        except LedgerError as exc:
            raise errors.from_ledger_error(exc) from exc
        return 200, {
            "items": [activity_to_wire(item) for item in items],
            "next_cursor": _next_cursor(items, limit),
        }, None

    def _post_transfers(self) -> tuple[int, dict, None]:
        # The key lives in the header (§2.2). It is validated before the body so
        # that a missing key is rejected deterministically, whatever the body is.
        idempotency_key = validation.require_idempotency_key(self.headers)
        raw = validation.require_object(
            validation.parse_json_body(self._read_body()), what="transfer body")

        from_account_id = validation.require_non_empty_str(raw, "from_account_id")
        to_account_id = validation.require_non_empty_str(raw, "to_account_id")
        amount_minor = validation.require_amount_minor(raw, "amount_minor")
        currency = validation.require_currency(raw)

        fingerprint = validation.request_fingerprint(
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            amount_minor=amount_minor,
            currency=currency,
        )

        try:
            result = self.server.pool.ledger().transfer(
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                amount_minor=amount_minor,
                currency=currency,
            )
        except LedgerError as exc:
            raise errors.from_ledger_error(exc) from exc

        if result.status == "applied":
            http_status = 201
        elif result.status == "replayed":
            http_status = 200
        else:
            raise errors.ApiError(500, errors.INTERNAL_ERROR,
                                  f"unexpected transfer status {result.status!r}")

        LOG.info("transfer %s (%s): %s -> %s %s minor", result.transfer_id,
                 result.status, result.from_account_id, result.to_account_id,
                 result.amount_minor)
        return http_status, transfer_to_wire(result), None


class PocketfulServer(ThreadingHTTPServer):
    """Threading HTTP server over one ledger, one connection per worker thread."""

    daemon_threads = True
    allow_reuse_address = True
    # socketserver's default listen backlog is 5. A burst of simultaneous
    # transfers (a storm, a retry wave) overflows it and the kernel resets the
    # surplus connections before they are ever accepted — a transport failure
    # the API cannot answer. 128 absorbs the burst; this is a money endpoint, so
    # a request that was never accepted is not an acceptable outcome.
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], db_path: str,
                 handler: type[PocketfulHandler] = PocketfulHandler) -> None:
        self.pool = LedgerPool(db_path)
        super().__init__(address, handler)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        """Serve one connection, then close this worker's ledger connection.

        The thread body is the only correct place to close a thread-local sqlite3
        connection: sqlite3 refuses the close from any other thread, so this
        worker has to do it before it exits. Without this, every connection a
        request thread opens is closed by nobody and survives until the process
        exits — visible only as a shutdown ``ResourceWarning``.
        """
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.pool.close_current_thread()

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.pool.close()


def create_server(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> PocketfulServer:
    """Build (but do not start) the API server over the ledger at ``db_path``.

    ``port=0`` binds an ephemeral port; read the real one from
    ``server.server_address[1]``.
    """
    if not isinstance(db_path, str) or db_path == "":
        raise ValueError("db_path must be a non-empty path")
    return PocketfulServer((host, port), db_path)


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> PocketfulServer:
    """Build the server and log where it is listening; the caller serves it."""
    httpd = create_server(db_path, host=host, port=port)
    LOG.info("Pocketful API listening on http://%s:%s (db=%s)",
             httpd.server_address[0], httpd.server_address[1], db_path)
    return httpd
