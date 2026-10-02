"""HTTP request layer — the client's only conversation with ``api/``.

Stdlib ``urllib`` only, integer minor units only, and one rule with teeth: the
idempotency key is a **required parameter** of :meth:`ApiClient.send_transfer`.
This class never mints a key. A caller therefore cannot accidentally send a
retry under a fresh key, because there is no code path here that would produce
one — the key always comes from the caller's persisted ``PendingTransfer``.

The transport is injectable so an evidence driver can do real HTTP with a real
socket while still controlling exactly when the process dies; the default
transport is ordinary ``urllib``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Optional

# (method, url, body_bytes, headers) -> (status, body_bytes)
Transport = Callable[[str, str, Optional[bytes], dict], tuple]


class ApiError(Exception):
    """A non-2xx answer, or a transport failure (``status is None``)."""

    def __init__(self, status: Optional[int], code: Optional[str], detail: str = "") -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"HTTP {status}: {code or 'transport_error'} {detail}".strip())

    def retryable(self) -> bool:
        """5xx, 429 and transport failure keep the pending record; 4xx is terminal.

        §2.3.4: 409 is terminal *and* surfaced — retrying the same key+body would
        just conflict again. Only a retryable error leaves the key in place.
        """
        if self.status is None:
            return True
        if self.status == 429:
            return True
        return 500 <= self.status < 600


class ApiClient:
    """Thin HTTP client for the §2.2 routes. Amounts are ints; nothing is coerced."""

    def __init__(self, base_url: str, *, timeout: float = 10.0,
                 transport: Transport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport if transport is not None else self._urllib_transport

    # -- transport ------------------------------------------------------------

    def _urllib_transport(self, method: str, url: str,
                          body: Optional[bytes], headers: dict) -> tuple:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            # A refusal is an answer, not a transport failure: keep the status
            # and body so the caller can branch on the server's error code.
            return exc.code, exc.read()

    def _request(self, method: str, path: str, body: object = None,
                 headers: dict[str, str] | None = None) -> dict:
        url = self.base_url + path
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        try:
            status, raw = self._transport(method, url, payload, dict(headers or {}))
        except OSError as exc:
            raise ApiError(None, "transport_error", str(exc)) from exc

        parsed: object = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None

        if not (200 <= status < 300):
            code = parsed.get("error") if isinstance(parsed, dict) else None
            raise ApiError(status, code, "" if parsed is None else json.dumps(parsed))
        if not isinstance(parsed, dict):
            raise ApiError(status, "malformed_response", "expected a JSON object")
        return parsed

    # -- routes (§2.2) --------------------------------------------------------

    def create_account(self, *, owner_id: str, currency: str,
                       allow_overdraft: bool = False,
                       account_id: str | None = None) -> dict:
        body: dict[str, object] = {"owner_id": owner_id, "currency": currency,
                                   "allow_overdraft": allow_overdraft}
        if account_id is not None:
            body["account_id"] = account_id
        return self._request("POST", "/accounts", body,
                             {"Content-Type": "application/json"})

    def get_balance(self, account_id: str) -> dict:
        return self._request("GET", f"/accounts/{account_id}/balance")

    def list_activity(self, account_id: str, *, limit: int = 50,
                      before: str | None = None) -> dict:
        path = f"/accounts/{account_id}/activity?limit={int(limit)}"
        if before is not None:
            path += f"&before={before}"
        return self._request("GET", path)

    def send_transfer(self, *, idempotency_key: str, from_account_id: str,
                      to_account_id: str, amount_minor: int, currency: str) -> dict:
        """POST /transfers with the caller's key. This client never mints one."""
        if not isinstance(idempotency_key, str) or idempotency_key.strip() == "":
            raise ValueError("idempotency_key is required and must be a non-empty string")
        if type(amount_minor) is not int:
            raise ValueError(
                f"amount_minor must be an int (minor units), got {type(amount_minor).__name__}")
        body = {
            "from_account_id": from_account_id,
            "to_account_id": to_account_id,
            "amount_minor": amount_minor,
            "currency": currency,
        }
        return self._request("POST", "/transfers", body, {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        })
