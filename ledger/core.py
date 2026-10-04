"""Ledger operations — Pocketful.

The only writers of money. Every mutation writes ledger entries that sum to
zero, inside one ``BEGIN IMMEDIATE`` transaction, with the balance read that
feeds the write performed inside that same transaction.

Money is an integer count of minor units, bounded to ``+-MAX_MINOR``. There is
no floating point anywhere in this module.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import db
from .types import (
    MAX_MINOR,
    Account,
    AccountExists,
    ActivityItem,
    Currency,
    CurrencyMismatch,
    IdempotencyConflict,
    InsufficientFunds,
    InvalidAmount,
    LedgerError,
    Money,
    ReservedAccountId,
    SameAccountTransfer,
    SystemAccountTransfer,
    TransferResult,
    UnknownAccount,
)

# The opening grant ("$100 for every new account"), in integer minor units.
# Defined ONCE, here (plan §C1.1): nothing else re-spells it, it is never a
# float and never a decimal string, and callers pass the value they read from
# this constant rather than a literal of their own.
OPENING_GRANT_MINOR: Money = 10_000

# The single system account that funds opening grants (plan §C1.2). A real row
# in ``accounts``, created lazily inside the first granting transaction so it
# appears on a live database with no boot step and no migration. Its balance
# goes negative as grants accumulate, which is why it is opened with
# ``allow_overdraft``: the issuance is then an explicit, auditable double entry
# against a named account instead of value appearing from nowhere.
SYSTEM_ACCOUNT_ID = "__system__"

# ``owner_id`` for the system account — and therefore the visible counterparty
# on every welcome-grant activity row, because ``list_activity`` carries the
# counterparty's owner on the item (§C2.5). Named for the issuer rather than the
# mechanism so the UI reads it as a sender and never has to special-case the
# system id, which would be a second place in the tree that knows the system
# account exists. Decided by the planner (msgs 9dfb1dfa / 2ab8ad33) at the
# designer's request (msg 9bc0369c).
SYSTEM_ACCOUNT_OWNER_ID = "Pocketful"

# The system account is single-currency. A grant therefore cannot fund an
# account denominated in anything else: INVARIANTS §1 forbids multi-currency
# arithmetic without an explicit conversion step carrying its own recorded rate.
SYSTEM_ACCOUNT_CURRENCY: Currency = "USD"


def _now() -> str:
    """Wall-clock timestamp, ISO-8601 UTC. Not money; never an idempotency key."""
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    """Opaque identifier for a transfer. Not money; never an idempotency key."""
    return uuid.uuid4().hex


def _require_amount(value: object) -> Money:
    """Reject — never coerce — an invalid amount.

    ``type(value) is not int`` rejects ``bool`` as well (``True`` is an ``int``
    in Python, but its type is ``bool``, and ``true`` must not become ``1``),
    and rejects floats and numeric strings.
    """
    if type(value) is not int:
        raise InvalidAmount(f"amount_minor must be an int, got {type(value).__name__}")
    if value <= 0 or value > MAX_MINOR:
        raise InvalidAmount(f"amount_minor must satisfy 0 < v <= {MAX_MINOR}, got {value}")
    return value


def _require_opening_grant(value: object) -> Money:
    """Reject — never coerce — an invalid opening grant.

    Same discipline as :func:`_require_amount` (``type(...) is not int`` rejects
    ``bool`` along with floats and numeric strings), with one difference: zero
    is legal here and only here, because "no grant" is a real, and the default,
    value.
    """
    if type(value) is not int:
        raise InvalidAmount(
            f"opening_grant_minor must be an int, got {type(value).__name__}"
        )
    if value < 0 or value > MAX_MINOR:
        raise InvalidAmount(
            f"opening_grant_minor must satisfy 0 <= v <= {MAX_MINOR}, got {value}"
        )
    return value


class Ledger:
    """Double-entry ledger over a single SQLite connection.

    The connection is supplied by the caller and must come from
    :func:`ledger.db.connect`. A ledger serializes its own access to that one
    connection with a re-entrant lock, so a single ``Ledger`` can be shared
    across threads for correctness; real write concurrency still happens at the
    SQLite level when callers use one connection per thread.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    # -- reads ---------------------------------------------------------------

    def _load_account(self, account_id: str) -> Account:
        row = self._conn.execute(
            "SELECT account_id, owner_id, currency, allow_overdraft, created_at "
            "FROM accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise UnknownAccount(f"unknown account: {account_id}")
        return Account(
            account_id=row["account_id"],
            owner_id=row["owner_id"],
            currency=row["currency"],
            allow_overdraft=bool(row["allow_overdraft"]),
            created_at=row["created_at"],
        )

    def get_account(self, account_id: str) -> Account:
        """Read one account, or raise ``UnknownAccount``.

        A READ, and deliberately shaped like one: it uses the caller's
        connection, opens no transaction and takes no write lock. It is not a
        mutation-shaped method that happens to be convenient here.
        """
        with self._lock:
            return self._load_account(account_id)

    # -- writes --------------------------------------------------------------

    def open_account(
        self,
        *,
        account_id: str,
        owner_id: str,
        currency: Currency,
        allow_overdraft: bool = False,
        opening_grant_minor: Money = 0,
    ) -> Account:
        """Open an account, optionally posting an opening grant with it.

        ``opening_grant_minor`` defaults to ``0`` (plan §C1.6): the ledger never
        grants by default, so every existing caller and fixture keeps its exact
        current behaviour, and funding a new account is the caller's policy
        decision rather than this primitive's.

        When it is positive the grant is a real balanced transfer from
        ``SYSTEM_ACCOUNT_ID`` — one ``transfers`` row and two ``ledger_entries``
        summing to zero (plan §C1.3) — written inside the SAME
        ``run_immediate`` transaction as the account row (plan §C1.4). The
        account and its opening grant are one unit of state: a failure leaves
        neither, so there is never an account that can no longer be granted
        (creation is not replayable).

        Exactly-once per ``account_id`` comes from the accounts PRIMARY KEY
        (plan §C1.5), not from a counter and not from a check-then-act: the
        duplicate id fails on the first INSERT, before the system account, the
        transfer or any entry is touched, so a refused duplicate moves nothing.
        """
        if account_id == SYSTEM_ACCOUNT_ID:
            raise ReservedAccountId(
                f"account id {SYSTEM_ACCOUNT_ID!r} is reserved for the system account"
            )
        grant = _require_opening_grant(opening_grant_minor)
        if grant > 0 and currency != SYSTEM_ACCOUNT_CURRENCY:
            # INVARIANTS §1: no cross-currency arithmetic without a recorded
            # rate. The system account is single-currency, so it cannot fund an
            # account denominated in anything else. Refused BEFORE the
            # transaction, so nothing is written.
            raise CurrencyMismatch(
                f"an opening grant is {SYSTEM_ACCOUNT_CURRENCY} only; "
                f"account currency is {currency!r}"
            )
        created_at = _now()

        def work() -> Account:
            # The account INSERT is the first write in this transaction. A
            # duplicate id fails here and takes the whole unit down with it.
            try:
                self._conn.execute(
                    "INSERT INTO accounts "
                    "(account_id, owner_id, currency, allow_overdraft, version, created_at) "
                    "VALUES (?, ?, ?, ?, 0, ?)",
                    (account_id, owner_id, currency, 1 if allow_overdraft else 0, created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountExists(f"account already exists: {account_id}") from exc

            if grant > 0:
                # Both of these write inside the caller's transaction, so the
                # account row, the lazily-created system row, the transfer,
                # both entries and both version bumps commit or roll back as one.
                self._ensure_system_account(created_at)
                self._post_opening_grant(account_id, grant, created_at)

            return Account(
                account_id=account_id,
                owner_id=owner_id,
                currency=currency,
                allow_overdraft=bool(allow_overdraft),
                created_at=created_at,
            )

        with self._lock:
            return db.run_immediate(self._conn, work)

    def _ensure_system_account(self, created_at: str) -> None:
        """Create the system account if it is absent — inside the caller's txn.

        Deliberately lazy (plan §C1.2): no boot step and no migration, so the
        row appears on a live database that already has accounts and none.
        ``allow_overdraft`` is what lets its balance go negative as grants
        accumulate; without it the very first grant would raise
        ``InsufficientFunds``.
        """
        row = self._conn.execute(
            "SELECT 1 FROM accounts WHERE account_id = ?", (SYSTEM_ACCOUNT_ID,)
        ).fetchone()
        if row is not None:
            return
        self._conn.execute(
            "INSERT INTO accounts "
            "(account_id, owner_id, currency, allow_overdraft, version, created_at) "
            "VALUES (?, ?, ?, 1, 0, ?)",
            (SYSTEM_ACCOUNT_ID, SYSTEM_ACCOUNT_OWNER_ID, SYSTEM_ACCOUNT_CURRENCY, created_at),
        )

    def _post_opening_grant(self, account_id: str, amount: Money, created_at: str) -> str:
        """The one and only path that moves money out of the system account.

        A real transfer with two entries summing to zero (plan §C1.3) — never a
        single-entry credit. Writes into the caller's transaction; the caller
        owns atomicity.
        """
        transfer_id = _new_id()
        self._conn.execute(
            "INSERT INTO transfers "
            "(transfer_id, from_account_id, to_account_id, amount_minor, currency, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (transfer_id, SYSTEM_ACCOUNT_ID, account_id, amount,
             SYSTEM_ACCOUNT_CURRENCY, created_at),
        )
        self._conn.execute(
            "INSERT INTO ledger_entries (transfer_id, account_id, amount_minor, created_at) "
            "VALUES (?, ?, ?, ?)",
            (transfer_id, SYSTEM_ACCOUNT_ID, -amount, created_at),
        )
        self._conn.execute(
            "INSERT INTO ledger_entries (transfer_id, account_id, amount_minor, created_at) "
            "VALUES (?, ?, ?, ?)",
            (transfer_id, account_id, amount, created_at),
        )
        self._conn.execute(
            "UPDATE accounts SET version = version + 1 WHERE account_id IN (?, ?)",
            (SYSTEM_ACCOUNT_ID, account_id),
        )
        return transfer_id

    def get_balance(self, account_id: str) -> Money:
        """Derived balance: SUM of this account's entries. No cached column."""
        with self._lock:
            self._load_account(account_id)
            row = self._conn.execute(
                "SELECT COALESCE(SUM(amount_minor), 0) AS balance "
                "FROM ledger_entries WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            return int(row["balance"])

    def transfer(
        self,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        from_account_id: str,
        to_account_id: str,
        amount_minor: Money,
        currency: Currency,
    ) -> TransferResult:
        amount = _require_amount(amount_minor)
        if from_account_id == to_account_id:
            raise SameAccountTransfer("from_account_id and to_account_id are the same")
        if SYSTEM_ACCOUNT_ID in (from_account_id, to_account_id):
            # Plan §C1.8: the system account carries allow_overdraft so that
            # grants can debit it without limit. Honouring a transfer that
            # touches it would turn one POST /transfers into a mint of any
            # amount, so the opening grant is the ONLY path out of it. Refused
            # before the transaction opens; nothing is read and nothing is written.
            raise SystemAccountTransfer(
                f"{SYSTEM_ACCOUNT_ID!r} may not take part in a transfer; "
                f"it funds opening grants only"
            )

        def work() -> TransferResult:
            # Inside the write transaction: has this exact key been seen?
            existing = self._conn.execute(
                "SELECT request_fingerprint, response_json FROM idempotency_keys WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise IdempotencyConflict(
                        f"idempotency key {idempotency_key!r} was used with a different request"
                    )
                stored = json.loads(existing["response_json"])
                return TransferResult(status="replayed", **stored)

            from_account = self._load_account(from_account_id)
            to_account = self._load_account(to_account_id)
            if from_account.currency != currency or to_account.currency != currency:
                raise CurrencyMismatch(
                    f"transfer currency {currency!r} does not match accounts "
                    f"{from_account.currency!r} and {to_account.currency!r}"
                )

            # The balance read that feeds this write happens INSIDE this
            # transaction, so there is no check-then-act across boundaries.
            balance = int(
                self._conn.execute(
                    "SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries WHERE account_id = ?",
                    (from_account_id,),
                ).fetchone()[0]
            )
            if balance - amount < 0 and not from_account.allow_overdraft:
                raise InsufficientFunds(
                    f"account {from_account_id} balance {balance} cannot cover {amount}"
                )

            transfer_id = _new_id()
            created_at = _now()
            self._conn.execute(
                "INSERT INTO transfers "
                "(transfer_id, from_account_id, to_account_id, amount_minor, currency, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (transfer_id, from_account_id, to_account_id, amount, currency, created_at),
            )
            # Double entry: the two entries sum to zero by construction.
            self._conn.execute(
                "INSERT INTO ledger_entries (transfer_id, account_id, amount_minor, created_at) "
                "VALUES (?, ?, ?, ?)",
                (transfer_id, from_account_id, -amount, created_at),
            )
            self._conn.execute(
                "INSERT INTO ledger_entries (transfer_id, account_id, amount_minor, created_at) "
                "VALUES (?, ?, ?, ?)",
                (transfer_id, to_account_id, amount, created_at),
            )

            response = {
                "transfer_id": transfer_id,
                "from_account_id": from_account_id,
                "to_account_id": to_account_id,
                "amount_minor": amount,
                "currency": currency,
                "created_at": created_at,
            }
            # Key, fingerprint and stored response land in the SAME transaction
            # as the entries above, so a crash can never leave the key recorded
            # without its result, or vice versa.
            self._conn.execute(
                "INSERT INTO idempotency_keys "
                "(key, request_fingerprint, transfer_id, response_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    request_fingerprint,
                    transfer_id,
                    json.dumps(response, separators=(",", ":"), sort_keys=True),
                    created_at,
                ),
            )
            self._conn.execute(
                "UPDATE accounts SET version = version + 1 WHERE account_id IN (?, ?)",
                (from_account_id, to_account_id),
            )
            return TransferResult(status="applied", **response)

        with self._lock:
            return db.run_immediate(self._conn, work)

    def list_activity(
        self,
        account_id: str,
        *,
        limit: int = 50,
        before_entry_id: Optional[str] = None,
    ) -> list[ActivityItem]:
        """Newest-first activity for one account, keyset-paginated by entry id."""
        with self._lock:
            self._load_account(account_id)
            if type(limit) is not int or limit <= 0:
                return []

            where = "e.account_id = ?"
            params: list[object] = [account_id]
            if before_entry_id is not None:
                try:
                    cursor = int(before_entry_id)
                except (TypeError, ValueError) as exc:
                    raise LedgerError(f"invalid before_entry_id: {before_entry_id!r}") from exc
                where += " AND e.entry_id < ?"
                params.append(cursor)
            params.append(limit)

            rows = self._conn.execute(
                f"""
                SELECT e.entry_id, e.transfer_id, e.amount_minor, e.created_at,
                       t.from_account_id, t.to_account_id,
                       ca.owner_id AS counterparty_owner_id,
                       (SELECT COALESCE(SUM(e2.amount_minor), 0)
                          FROM ledger_entries e2
                         WHERE e2.account_id = e.account_id
                           AND e2.entry_id <= e.entry_id) AS balance_after_minor
                  FROM ledger_entries e
                  JOIN transfers t ON t.transfer_id = e.transfer_id
                  -- The counterparty's owner, joined in the same read so the
                  -- caller never has to resolve an id to a name itself (§C2.5).
                  -- The FK on transfers guarantees the row exists; the CASE
                  -- mirrors the Python counterparty selection exactly.
                  LEFT JOIN accounts ca
                         ON ca.account_id = CASE
                              WHEN t.from_account_id = e.account_id
                                   THEN t.to_account_id
                              ELSE t.from_account_id
                            END
                 WHERE {where}
                 ORDER BY e.entry_id DESC
                 LIMIT ?
                """,
                params,
            ).fetchall()

            items: list[ActivityItem] = []
            for row in rows:
                signed = int(row["amount_minor"])
                counterparty = (
                    row["to_account_id"]
                    if row["from_account_id"] == account_id
                    else row["from_account_id"]
                )
                items.append(
                    ActivityItem(
                        entry_id=str(row["entry_id"]),
                        transfer_id=row["transfer_id"],
                        # Exactly one encoding of the sign: direction carries it,
                        # amount_minor stays non-negative.
                        direction="debit" if signed < 0 else "credit",
                        amount_minor=abs(signed),
                        balance_after_minor=int(row["balance_after_minor"]),
                        counterparty_account_id=counterparty,
                        counterparty_owner_id=row["counterparty_owner_id"],
                        created_at=row["created_at"],
                    )
                )
            return items

    # -- pure ----------------------------------------------------------------

    @staticmethod
    def split(total_minor: Money, parts: int) -> list[Money]:
        """Split ``total_minor`` into ``parts`` integers that sum to exactly it.

        Policy: ``base, extra = divmod(total, parts)``; the first ``extra``
        parts get one extra minor unit. Deterministic, order-stable, and no
        cent is ever truncated away.
        """
        if type(total_minor) is not int:
            raise InvalidAmount(f"total_minor must be an int, got {type(total_minor).__name__}")
        if type(parts) is not int or type(parts) is bool:
            raise InvalidAmount(f"parts must be an int, got {type(parts).__name__}")
        if parts < 1:
            raise InvalidAmount(f"parts must be >= 1, got {parts}")
        base, extra = divmod(total_minor, parts)
        return [base + 1 if index < extra else base for index in range(parts)]
