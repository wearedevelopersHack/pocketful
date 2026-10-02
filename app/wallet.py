"""The wallet UI surface: balance and activity, read from the server.

Two things this module deliberately does **not** do:

* It never sums a list of transfers to produce a balance. The balance is the
  server's derived value (plan §2.3.6, INVARIANTS §2), fetched verbatim.
* It never does float arithmetic on money. User-entered text is converted to
  integer minor units at the edge by :mod:`app.money`; display strings are
  produced from integers by :func:`app.money.format_minor`.

The UI layer is thin on purpose — all of the money-critical work lives in
``app.money`` (the float boundary), ``app.keys``/``app.keystore`` (the key's
lifetime) and ``app.send`` (retry semantics).
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import ApiClient
from .keystore import PendingStore
from .money import format_minor, parse_amount_to_minor
from .send import SendOutcome, send as _send


class WalletProtocolError(RuntimeError):
    """The server's answer was not the shape §2.2 promises."""


class SameAccountSend(ValueError):
    """The send names the wallet's own account as the payee.

    A named type rather than a bare ``ValueError`` so the UI can catch it and
    render a status. It subclasses ``ValueError`` so any existing caller that
    catches ``ValueError`` keeps working. Raised *before* any key is minted.
    """


@dataclass(frozen=True)
class ActivityRow:
    """One activity item, ready to render.

    ``amount_minor`` is non-negative and ``direction`` carries the sign
    (INVARIANTS §3, plan §2.1) — the sign is encoded once, never twice.
    """

    entry_id: str
    transfer_id: str
    direction: str
    amount_minor: int
    amount_display: str
    balance_after_minor: int
    counterparty_account_id: str
    created_at: str


class Wallet:
    """Balance, activity and send, for one account. Reads server truth only."""

    def __init__(self, client: ApiClient, store: PendingStore, account_id: str) -> None:
        self.client = client
        self.store = store
        self.account_id = account_id
        self._currency: str | None = None

    # -- reads ---------------------------------------------------------------

    def balance_minor(self) -> int:
        """The server's authoritative balance, in integer minor units."""
        payload = self.client.get_balance(self.account_id)
        value = payload.get("balance_minor")
        if type(value) is not int:
            raise WalletProtocolError(
                f"balance_minor was {type(value).__name__}, not int: {payload!r}")
        currency = payload.get("currency")
        if isinstance(currency, str):
            self._currency = currency
        return value

    def balance_display(self) -> str:
        return format_minor(self.balance_minor(), self.currency)

    def activity(self, limit: int = 50) -> list[ActivityRow]:
        """The activity feed, as the server returned it. No local summing."""
        payload = self.client.list_activity(self.account_id, limit=limit)
        items = payload.get("items")
        if not isinstance(items, list):
            raise WalletProtocolError(f"activity items was not a list: {payload!r}")
        rows: list[ActivityRow] = []
        for item in items:
            if not isinstance(item, dict):
                raise WalletProtocolError(f"activity item was not an object: {item!r}")
            amount = item.get("amount_minor")
            if type(amount) is not int:
                raise WalletProtocolError(
                    f"amount_minor was {type(amount).__name__}, not int: {item!r}")
            rows.append(ActivityRow(
                entry_id=item["entry_id"],
                transfer_id=item["transfer_id"],
                direction=item["direction"],
                amount_minor=amount,
                amount_display=format_minor(amount, item.get("currency") or self.currency),
                balance_after_minor=item["balance_after_minor"],
                counterparty_account_id=item["counterparty_account_id"],
                created_at=item["created_at"],
            ))
        return rows

    # -- writes --------------------------------------------------------------

    @property
    def currency(self) -> str:
        if self._currency is None:
            self.balance_minor()          # fills _currency from the server
        if self._currency is None:
            raise WalletProtocolError("the server did not report a currency")
        return self._currency

    def send(self, to_account_id: str, amount_text: str) -> SendOutcome:
        """Send money from user-entered text.

        The text is converted to integer minor units *here*, before anything is
        persisted or sent, and a value that cannot be represented exactly is
        rejected rather than rounded.
        """
        amount_minor = parse_amount_to_minor(amount_text, self.currency)
        if to_account_id == self.account_id:
            raise SameAccountSend("cannot send to the same account")
        return _send(
            self.store,
            self.client,
            from_account_id=self.account_id,
            to_account_id=to_account_id,
            amount_minor=amount_minor,
            currency=self.currency,
        )
