"""Idempotency-key generation — the client's claim, minted here.

INVARIANTS §3: the key is the *client's* claim that "this is the same request I
already sent". It must not be derived from the request body, a timestamp or a
server-side value; a UUIDv4 is independent of all three.

One key per **user action**. The user pressing Send twice produces two keys —
those are two transfers. A network retry of one transfer produces one key,
because a retry reuses the key it already holds (see ``app.send``) and never
calls :func:`new_idempotency_key` again.
"""

from __future__ import annotations

import re
import uuid

# Canonical lowercase UUIDv4, exactly 36 chars — well inside the API's 255-char
# ``Idempotency-Key`` limit (§2.2 / api/validation.py).
_UUID4 = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


def new_idempotency_key() -> str:
    """Mint one fresh key, at request-construction time. Not derived from input."""
    return str(uuid.uuid4())


def is_idempotency_key(key: object) -> bool:
    """True iff ``key`` is a canonical UUIDv4 string."""
    return isinstance(key, str) and _UUID4.match(key) is not None
