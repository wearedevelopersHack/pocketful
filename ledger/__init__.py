"""Pocketful ledger core — accounts, entries, transfers, idempotency.

Owned exclusively by the ledger-engineer. Public surface is exactly the §2.1
contract: :class:`~ledger.core.Ledger` with ``open_account``, ``get_balance``,
``transfer``, ``list_activity`` and ``split``. Connections come only from
:func:`ledger.db.connect`.
"""

from __future__ import annotations

from .core import Ledger
from .db import connect
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

__all__ = [
    "Ledger",
    "connect",
    "MAX_MINOR",
    "Money",
    "Currency",
    "Account",
    "ActivityItem",
    "TransferResult",
    "LedgerError",
    "InvalidAmount",
    "AccountExists",
    "UnknownAccount",
    "CurrencyMismatch",
    "SameAccountTransfer",
    "InsufficientFunds",
    "IdempotencyConflict",
]
