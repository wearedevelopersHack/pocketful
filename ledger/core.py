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
    SameAccountTransfer,
    TransferResult,
    UnknownAccount,
)


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
    ) -> Account:
        created_at = _now()

        def work() -> Account:
            try:
                self._conn.execute(
                    "INSERT INTO accounts "
                    "(account_id, owner_id, currency, allow_overdraft, version, created_at) "
                    "VALUES (?, ?, ?, ?, 0, ?)",
                    (account_id, owner_id, currency, 1 if allow_overdraft else 0, created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountExists(f"account already exists: {account_id}") from exc
            return Account(
                account_id=account_id,
                owner_id=owner_id,
                currency=currency,
                allow_overdraft=bool(allow_overdraft),
                created_at=created_at,
            )

        with self._lock:
            return db.run_immediate(self._conn, work)

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
                       (SELECT COALESCE(SUM(e2.amount_minor), 0)
                          FROM ledger_entries e2
                         WHERE e2.account_id = e.account_id
                           AND e2.entry_id <= e.entry_id) AS balance_after_minor
                  FROM ledger_entries e
                  JOIN transfers t ON t.transfer_id = e.transfer_id
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
