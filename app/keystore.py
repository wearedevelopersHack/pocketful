"""The durable pending-transfer store: where the idempotency key lives.

Plan §2.3.2 is the whole reason this module exists: the record
``{key, from_account_id, to_account_id, amount_minor, currency, state}`` must be
persisted **before** the first HTTP attempt and survive the process dying.

Design rule that makes the obligation hard to get wrong: the **only** way to
obtain a key is :meth:`PendingStore.begin`, which mints the key and writes the
record to disk (``fsync`` then atomic ``os.replace``) before it returns. There
is no second key source and no way to construct a request without a durable
record, so "persist before send" is not a step a caller can forget — it is the
shape of the API.

A record moves to ``settled`` on a 2xx and is removed on a terminal refusal
(:meth:`discard`). A record that stays ``pending`` after a crash is exactly the
thing ``app.send.resume`` retries — with the same key, never a new one.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .keys import new_idempotency_key

STATE_PENDING = "pending"
STATE_SETTLED = "settled"

_FORMAT_VERSION = 1


class StoreCorrupt(RuntimeError):
    """The on-disk store could not be read as a well-formed pending record set."""


class DuplicateKey(RuntimeError):
    """A key collision on ``begin`` — never silently overwrite a live record."""


@dataclass(frozen=True)
class PendingTransfer:
    """One outstanding (or settled) transfer, keyed by the client's key.

    ``amount_minor`` is an ``int`` count of minor units. It is re-validated as a
    real ``int`` (never a ``bool``, never a float) on load, so a corrupted file
    fails loudly instead of sending a wrong amount.
    """

    key: str
    from_account_id: str
    to_account_id: str
    amount_minor: int
    currency: str
    state: str
    created_at: str
    transfer_id: str | None = None

    def body(self) -> dict[str, object]:
        """The exact request body this key was first used with."""
        return {
            "from_account_id": self.from_account_id,
            "to_account_id": self.to_account_id,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
        }


class PendingStore:
    """A JSON-file-backed, crash-durable store of pending transfers.

    Writes are ``write temp -> flush -> fsync -> os.replace -> fsync(dir)``, so
    a process killed at any instant leaves either the old complete file or the
    new complete file — never a truncated one. That property is what makes the
    key survive :func:`os.kill`.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        # A web-served store is touched by several request threads: a send and a
        # retry can overlap. Every mutating method holds this lock across the
        # read-modify-write of the record map *and* the durable save, so two
        # concurrent begins cannot lose one another's record.
        self._lock = threading.RLock()
        self._records: dict[str, PendingTransfer] = {}
        self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            self._records = {}
            return
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreCorrupt(f"cannot read pending store {self.path}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("records"), dict):
            raise StoreCorrupt(f"pending store {self.path} is not a record map")
        records: dict[str, PendingTransfer] = {}
        for key, item in raw["records"].items():
            if not isinstance(item, dict):
                raise StoreCorrupt(f"record {key!r} is not an object")
            amount = item.get("amount_minor")
            if type(amount) is not int:
                raise StoreCorrupt(
                    f"record {key!r} amount_minor is {type(amount).__name__}, not int")
            state = item.get("state")
            if state not in (STATE_PENDING, STATE_SETTLED):
                raise StoreCorrupt(f"record {key!r} has unknown state {state!r}")
            records[key] = PendingTransfer(
                key=key,
                from_account_id=item["from_account_id"],
                to_account_id=item["to_account_id"],
                amount_minor=amount,
                currency=item["currency"],
                state=state,
                created_at=item["created_at"],
                transfer_id=item.get("transfer_id"),
            )
        self._records = records

    def _save(self) -> None:
        """Write the whole record set durably. Callers hold ``self._lock``."""
        payload = {
            "version": _FORMAT_VERSION,
            "records": {key: _to_json(rec) for key, rec in self._records.items()},
        }
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".pending-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        # fsync the directory so the rename itself is durable, not just the bytes.
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    # -- the key's lifetime ---------------------------------------------------

    def begin(self, *, from_account_id: str, to_account_id: str,
              amount_minor: int, currency: str) -> PendingTransfer:
        """Mint a key AND persist the record before returning. The only key source.

        Mints **unconditionally, on every call**. Two presses of Send are two
        transfers (plan §2.3 item 1), and this store cannot tell a browser reload
        of a response page from a second press — so deduping against an
        outstanding identical record here would silently merge two legitimate
        transfers, which is the bug this build exists to prevent, dressed as a
        fix. The refresh problem is solved at the HTTP layer (every POST answers
        303, so a refresh re-issues a GET), never here.
        """
        if type(amount_minor) is not int:
            raise TypeError("amount_minor must be an int (minor units)")
        with self._lock:
            key = new_idempotency_key()
            if key in self._records:
                raise DuplicateKey(f"key {key!r} already present; refusing to overwrite")
            record = PendingTransfer(
                key=key,
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                amount_minor=amount_minor,
                currency=currency,
                state=STATE_PENDING,
                created_at=datetime.now(timezone.utc).isoformat(),
                transfer_id=None,
            )
            self._records[key] = record
            self._save()
            return record

    def get(self, key: str) -> PendingTransfer | None:
        with self._lock:
            return self._records.get(key)

    def outstanding(self) -> list[PendingTransfer]:
        """Every record still ``pending``, oldest first. Survives a restart."""
        with self._lock:
            pending = [rec for rec in self._records.values() if rec.state == STATE_PENDING]
            return sorted(pending, key=lambda rec: (rec.created_at, rec.key))

    def all_records(self) -> list[PendingTransfer]:
        with self._lock:
            return sorted(self._records.values(), key=lambda rec: (rec.created_at, rec.key))

    def settle(self, key: str, *, transfer_id: str) -> PendingTransfer:
        """Record a 2xx outcome against the key (terminal; the record is kept)."""
        with self._lock:
            record = self._require(key)
            settled = replace(record, state=STATE_SETTLED, transfer_id=transfer_id)
            self._records[key] = settled
            self._save()
            return settled

    def discard(self, key: str) -> None:
        """Remove a terminal-refused record. Its key is never reused after this."""
        with self._lock:
            if key in self._records:
                del self._records[key]
                self._save()

    def _require(self, key: str) -> PendingTransfer:
        record = self._records.get(key)
        if record is None:
            raise KeyError(f"no pending transfer for key {key!r}")
        return record


def _to_json(record: PendingTransfer) -> dict[str, object]:
    return {
        "from_account_id": record.from_account_id,
        "to_account_id": record.to_account_id,
        "amount_minor": record.amount_minor,
        "currency": record.currency,
        "state": record.state,
        "created_at": record.created_at,
        "transfer_id": record.transfer_id,
    }
