"""Ledger value types, errors and bounds — Pocketful money path.

Money is an integer count of MINOR UNITS (cents). There is no floating point
type in this module, and there must never be one anywhere under ``ledger/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Signed count of minor units (cents). Never float.
Money = int

# ISO-4217 currency code, e.g. "USD".
Currency = str

# Every amount is bounded to +-MAX_MINOR: on the wire, in storage, and in
# arithmetic. 2**53 - 1 is exact in every language in the room (including a
# JS ``number``) and is a strict subset of i64.
MAX_MINOR = 2**53 - 1


class LedgerError(Exception):
    """Base class for every error this package raises."""


class InvalidAmount(LedgerError):
    """Amount was not an int, was a bool, was zero, negative, or out of range."""


class AccountExists(LedgerError):
    """The account id is already taken.

    Deliberately not idempotent: opening an account is not a retried operation,
    and returning the existing account would silently mask a caller mistake.
    """


class UnknownAccount(LedgerError):
    """The referenced account does not exist."""


class CurrencyMismatch(LedgerError):
    """Accounts and/or the transfer disagree about currency."""


class SameAccountTransfer(LedgerError):
    """Payer and payee are the same account."""


class InsufficientFunds(LedgerError):
    """The payer balance would go negative and overdraft is not allowed."""


class IdempotencyConflict(LedgerError):
    """The key was seen before with a different request body."""


@dataclass(frozen=True)
class Account:
    account_id: str
    owner_id: str
    currency: Currency
    allow_overdraft: bool
    created_at: str


@dataclass(frozen=True)
class TransferResult:
    transfer_id: str
    status: Literal["applied", "replayed"]  # "replayed" == the key was seen before
    from_account_id: str
    to_account_id: str
    amount_minor: Money
    currency: Currency
    created_at: str


@dataclass(frozen=True)
class ActivityItem:
    """One ledger entry as seen from the queried account.

    ``amount_minor`` is NON-NEGATIVE and ``direction`` carries the sign: a
    ``"debit"`` is money leaving the queried account and a ``"credit"`` is money
    arriving. There is exactly one encoding of the sign on this type, so the
    amount and the direction can never disagree. ``balance_after_minor`` is the
    signed account balance immediately after this entry was applied.
    """

    entry_id: str
    transfer_id: str
    direction: Literal["debit", "credit"]
    amount_minor: Money
    balance_after_minor: Money
    counterparty_account_id: str
    created_at: str
