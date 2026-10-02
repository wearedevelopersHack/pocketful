"""The send flow: key generation, persistence and retry in one place.

The contract (plan §2.3, T4 DoD) is entirely about *when* the key is minted:

* :func:`send` is **one user press**. It calls ``PendingStore.begin`` — which
  mints the key and durably persists the record — and only then talks to the
  server. Two presses produce two records and therefore two keys.
* :func:`retry` and :func:`resume` **never** call ``new_idempotency_key``. They
  reload the stored record and re-send its exact key *and* its exact body. A
  retry that minted a fresh key would be a double-spend the ledger cannot stop,
  because it would be a different transfer.

Error policy (§2.3.4), applied by :func:`_attempt`:

* 2xx                 -> settle the record, return the server's outcome.
* 409 / other 4xx     -> terminal: surface it, do **not** retry. The record is
                         discarded so a later ``resume`` will not re-send it.
* 5xx / transport     -> retryable: the record stays ``pending`` with its key
                         intact, ready for the next :func:`retry`/:func:`resume`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .client import ApiClient, ApiError
from .keystore import PendingStore, PendingTransfer


@dataclass(frozen=True)
class SendOutcome:
    """The settled result of a send. ``status`` is the server's word for it."""

    transfer_id: str
    status: Literal["applied", "replayed"]
    key: str


class SendError(Exception):
    """Base class for send-flow failures."""


class TerminalSendError(SendError):
    """A deterministic refusal (400/404/422/...). The record was discarded."""

    def __init__(self, error: ApiError) -> None:
        self.error = error
        super().__init__(str(error))


class IdempotencyConflictForSend(SendError):
    """409: the key was seen before with a different body. Surfaced, never retried."""

    def __init__(self, error: ApiError) -> None:
        self.error = error
        super().__init__(str(error))


class RetryableSendError(SendError):
    """5xx or transport failure. The record is still pending; retry the same key."""

    def __init__(self, error: ApiError) -> None:
        self.error = error
        super().__init__(str(error))


class SendProtocolError(SendError):
    """A 2xx whose body did not carry a usable outcome. The key stays pending."""


def send(store: PendingStore, client: ApiClient, *, from_account_id: str,
         to_account_id: str, amount_minor: int, currency: str) -> SendOutcome:
    """One user action: mint + persist a key, then attempt the transfer once."""
    pending = store.begin(
        from_account_id=from_account_id,
        to_account_id=to_account_id,
        amount_minor=amount_minor,
        currency=currency,
    )
    return _attempt(store, client, pending)


def retry(store: PendingStore, client: ApiClient, key: str) -> SendOutcome:
    """Retry an existing record. Reuses its key and body verbatim — never remints."""
    pending = store.get(key)
    if pending is None:
        raise KeyError(f"no record for key {key!r}")
    if pending.state == "settled":
        # Already settled: replay the recorded outcome locally. No request, and
        # certainly no new key.
        if pending.transfer_id is None:
            raise SendProtocolError(f"settled record {key!r} has no transfer_id")
        return SendOutcome(transfer_id=pending.transfer_id, status="replayed", key=key)
    return _attempt(store, client, pending)


def resume(store: PendingStore, client: ApiClient) -> list[SendOutcome]:
    """After a crash/restart: retry every outstanding record with its own key.

    This is the recovery path the T4 evidence exercises. Retryable failures are
    swallowed (the record stays pending for a later attempt); terminal failures
    propagate so they are surfaced rather than hidden.
    """
    outcomes: list[SendOutcome] = []
    for pending in store.outstanding():
        try:
            outcomes.append(_attempt(store, client, pending))
        except RetryableSendError:
            continue
    return outcomes


def _attempt(store: PendingStore, client: ApiClient,
             pending: PendingTransfer) -> SendOutcome:
    try:
        response = client.send_transfer(
            idempotency_key=pending.key,
            from_account_id=pending.from_account_id,
            to_account_id=pending.to_account_id,
            amount_minor=pending.amount_minor,
            currency=pending.currency,
        )
    except ApiError as exc:
        if exc.retryable():
            raise RetryableSendError(exc) from exc          # keep pending
        if exc.status == 409 or exc.code == "idempotency_conflict":
            store.discard(pending.key)
            raise IdempotencyConflictForSend(exc) from exc
        store.discard(pending.key)
        raise TerminalSendError(exc) from exc

    transfer_id = response.get("transfer_id")
    status = response.get("status")
    if not isinstance(transfer_id, str) or status not in ("applied", "replayed"):
        # A 2xx we cannot interpret: do not settle, do not claim success. The key
        # stays pending so the next attempt replays rather than double-sends.
        raise SendProtocolError(f"unusable transfer response: {response!r}")

    store.settle(pending.key, transfer_id=transfer_id)
    return SendOutcome(transfer_id=transfer_id, status=status, key=pending.key)
