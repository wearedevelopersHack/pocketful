"""The wire error vocabulary, and the mapping from ledger refusals to HTTP.

Every body this API ships is exactly the shape plan §2.2 names — ``{"error":
<code>}`` with nothing added. The diagnostic text (the ledger's own message)
goes to the server log, not to the wire, so the freeze stays the freeze.

Status map, with §2.2 provenance:

===========================  ======  ===========================================
code                         status  source
===========================  ======  ===========================================
``account_exists``           409     §2.2 POST /accounts
``idempotency_conflict``     409     §2.2 POST /transfers (same key, other body)
``invalid_idempotency_key``  400     §2.2 POST /transfers (missing / over-long)
``malformed_json``           400     §2.2 POST /transfers
``invalid_amount``           422     §2.2 POST /transfers
``currency_mismatch``        422     §2.2 POST /transfers
``insufficient_funds``       422     §2.2 POST /transfers
``same_account_transfer``    422     §2.2 POST /transfers
``unknown_account``          404     §2.2 balance and transfers
===========================  ======  ===========================================

Documented extensions beyond §2.2 (this API has to answer *something*, and
silently ignoring a bad query or an unknown route would be a form of coercion):

* ``invalid_request`` (400) — a required field is absent or the wrong JSON type.
* ``invalid_limit`` (400), ``invalid_cursor`` (400) — bad activity query args.
* ``payload_too_large`` (413) — body over the read limit.
* ``not_found`` (404) — no route matches.
* ``method_not_allowed`` (405) — route exists, method does not.
* ``internal_error`` (500) — an unexpected failure; the traceback is logged.
"""

from __future__ import annotations

from ledger import (
    CurrencyMismatch,
    IdempotencyConflict,
    InsufficientFunds,
    InvalidAmount,
    LedgerError,
    SameAccountTransfer,
    UnknownAccount,
)

ACCOUNT_EXISTS = "account_exists"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
INVALID_IDEMPOTENCY_KEY = "invalid_idempotency_key"
MALFORMED_JSON = "malformed_json"
INVALID_REQUEST = "invalid_request"
INVALID_CURSOR = "invalid_cursor"
INVALID_LIMIT = "invalid_limit"
NOT_FOUND = "not_found"
METHOD_NOT_ALLOWED = "method_not_allowed"
UNKNOWN_ACCOUNT = "unknown_account"
INVALID_AMOUNT = "invalid_amount"
CURRENCY_MISMATCH = "currency_mismatch"
INSUFFICIENT_FUNDS = "insufficient_funds"
SAME_ACCOUNT_TRANSFER = "same_account_transfer"
INTERNAL_ERROR = "internal_error"
PAYLOAD_TOO_LARGE = "payload_too_large"

# Ledger refusal -> (HTTP status, wire code). Nothing here is a 2xx: a refused
# operation is never reported as a success (§2.2, role obligation).
LEDGER_REFUSALS: tuple[tuple[type[LedgerError], int, str], ...] = (
    (InvalidAmount, 422, INVALID_AMOUNT),
    (UnknownAccount, 404, UNKNOWN_ACCOUNT),
    (CurrencyMismatch, 422, CURRENCY_MISMATCH),
    (SameAccountTransfer, 422, SAME_ACCOUNT_TRANSFER),
    (InsufficientFunds, 422, INSUFFICIENT_FUNDS),
    (IdempotencyConflict, 409, IDEMPOTENCY_CONFLICT),
)


class ApiError(Exception):
    """A refusal that becomes an HTTP response. Never a 2xx."""

    def __init__(self, status: int, code: str, detail: str = "",
                 headers: dict[str, str] | None = None) -> None:
        text = f"{status} {code}"
        if detail:
            text = f"{text}: {detail}"
        super().__init__(text)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers

    def body(self) -> dict[str, str]:
        """Exactly the §2.2 body shape: the code, and nothing else."""
        return {"error": self.code}


def from_ledger_error(exc: LedgerError) -> ApiError:
    """Translate a ledger refusal into its documented HTTP status.

    Order matters: the most specific class wins, so subclasses are checked as
    given in ``LEDGER_REFUSALS``.
    """
    for exc_type, status, code in LEDGER_REFUSALS:
        if isinstance(exc, exc_type):
            return ApiError(status, code, str(exc))
    return ApiError(500, INTERNAL_ERROR, str(exc))


def account_exists_error(exc: LedgerError) -> ApiError:
    """``open_account`` on an id that already exists (§2.1 ``AccountExists``).

    A 409, and deliberately **not** idempotent — the existing account is never
    returned, because silently succeeding would mask a caller mistake.
    """
    return ApiError(409, ACCOUNT_EXISTS, str(exc))
