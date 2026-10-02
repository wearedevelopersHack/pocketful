"""Pocketful client — wallet UI, send flow and the durable idempotency key.

The client's one money-critical obligation (plan §2.3) is the idempotency key:
it is generated here, at request-construction time, persisted *before* the first
attempt, and reused verbatim on every retry — including after the process dies.

Public surface (see the room plan §2.3 and the T4 handoff):

    app.money     parse_amount_to_minor / format_minor     (the float boundary)
    app.keys      new_idempotency_key / is_idempotency_key
    app.keystore  PendingTransfer / PendingStore           (the durable store)
    app.client    ApiClient / ApiError                     (the wire)
    app.send      send / retry / resume / SendOutcome      (key lifetime)
    app.wallet    Wallet / ActivityRow                     (what the UI reads)

Nothing under ``app/`` computes a balance by summing a list, and no amount that
is sent or stored is ever a float.
"""

from __future__ import annotations

from . import client as _client
from . import keys as _keys
from . import keystore as _keystore
from . import money as _money
from . import send as _send
from . import wallet as _wallet

__all__ = [
    "ApiClient",
    "ApiError",
    "ActivityRow",
    "PendingStore",
    "PendingTransfer",
    "RetryableSendError",
    "SendOutcome",
    "TerminalSendError",
    "IdempotencyConflictForSend",
    "Wallet",
    "format_minor",
    "parse_amount_to_minor",
    "new_idempotency_key",
    "is_idempotency_key",
    "send_transfer",
    "retry",
    "resume",
]

ApiClient = _client.ApiClient
ApiError = _client.ApiError
ActivityRow = _wallet.ActivityRow
PendingStore = _keystore.PendingStore
PendingTransfer = _keystore.PendingTransfer
RetryableSendError = _send.RetryableSendError
SendOutcome = _send.SendOutcome
TerminalSendError = _send.TerminalSendError
IdempotencyConflictForSend = _send.IdempotencyConflictForSend
Wallet = _wallet.Wallet
format_minor = _money.format_minor
parse_amount_to_minor = _money.parse_amount_to_minor
new_idempotency_key = _keys.new_idempotency_key
is_idempotency_key = _keys.is_idempotency_key
# NOT bound as `send`: a package-level name equal to a submodule name shadows
# that submodule, so `import app.send as m; m.resume` would bind this function
# and raise AttributeError. The submodule is reached as `app.send`; the function
# is exposed here under a name that cannot collide.
send_transfer = _send.send
retry = _send.retry
resume = _send.resume
