"""Pocketful HTTP API — the wire boundary in front of the ledger.

Owned exclusively by the integrator. Public surface:

* :func:`api.app.create_server` — build the server (does not serve it).
* :func:`api.app.serve` — build the server, ready for ``serve_forever()``.

Wire rules that bind this package (plan §2.1, §2.2):

* Money is an **integer count of minor units**. There are no decimal strings and
  no floating-point values anywhere on this path; a value that is not a JSON
  integer is rejected, never coerced.
* ``amount_minor`` is **non-negative** on the wire. The sign lives in
  ``direction`` (``"debit"`` / ``"credit"``). The activity endpoint derives both
  from the signed ledger entry, so the two fields cannot disagree.
* The ``Idempotency-Key`` is a request **header**; the amount is in the **body**.
* A rejected request never reaches the ledger, and a refused ledger operation
  never becomes a 2xx.
"""

from __future__ import annotations

from .app import PocketfulServer, create_server, serve

__all__ = ["PocketfulServer", "create_server", "serve"]
