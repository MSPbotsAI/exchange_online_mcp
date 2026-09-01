"""Exchange Online admin endpoint client.

Every cmdlet runs through the same REST entry point the Exchange Online
PowerShell module itself uses:

    POST {base_url}/adminapi/beta/{tenant}/InvokeCommand
    {"CmdletInput": {"CmdletName": "...", "Parameters": {...}}}

so this service needs no PowerShell runtime.
"""

import asyncio
import uuid
from typing import Any

import httpx

from ._json import error_envelope
from .auth import CredentialError, ExoCredentials, TokenError, fetch_access_token

DEFAULT_BASE_URL = "https://outlook.office365.com"
DEFAULT_LOGIN_BASE_URL = "https://login.microsoftonline.com"

# Tenant-wide system mailbox used as the routing hint for app-only calls, where
# there is no signed-in user to anchor on. Only usable when the tenant is
# identified by domain rather than GUID.
_SYSTEM_MAILBOX_ANCHOR = "UPN:SystemMailbox{bb558c35-97f1-4cb9-8ff7-d53741dc928c}@%s"

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 2 retries * (30s read + <=8s backoff) keeps a single tool call — token mint
# included — comfortably under the 120s upstream budget.
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 8.0

# One shared connection pool for the process lifetime. It holds no credentials:
# tokens are minted per request from header-supplied certificates, so sharing
# the pool across tenants is safe (server.py's contextvar isolation is what
# actually keeps tenants apart).
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _http_client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Test seam: inject a mock transport client (or reset with None)."""
    global _http_client
    _http_client = client


# status_code -> (error code, retryable). status_code 0 means a network/
# connection-level failure (no response at all).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    422: ("invalid_argument", False),
    429: ("rate_limited", True),
}

# Exchange reports "no such recipient/device" as a 400 carrying a cmdlet
# exception, not as a 404, so the text is the only signal available.
_NOT_FOUND_MARKERS = (
    "managementobjectnotfound",
    "objectnotfound",
    "couldn't be found",
    "couldn't find",
    "wasn't found",
    "does not exist",
)


def _classify(status_code: int, message: str = "") -> tuple[str, bool]:
    if status_code in (400, 422) and any(m in message.lower() for m in _NOT_FOUND_MARKERS):
        return "not_found", False
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    return "invalid_argument", False


class ExoError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Exchange Online error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = _classify(self.status_code, self.message)
        return error_envelope(code, self.message, retryable)


def strip_odata(record: dict) -> dict:
    """Drop the `@odata.type` / `@data.type` companion keys the admin endpoint
    emits next to almost every property — pure token cost for an agent."""
    return {k: v for k, v in record.items() if "@odata." not in k and "@data." not in k}


class ExoClient:
    """One tenant's admin-endpoint client, scoped to a single MCP request.

    The access token is minted lazily and kept on the instance so a tool that
    runs two cmdlets (e.g. set + verify read) pays for one token exchange. The
    instance itself is created per request from the credential contextvar, so
    nothing survives the request.
    """

    def __init__(
        self,
        credentials: ExoCredentials,
        base_url: str = DEFAULT_BASE_URL,
        login_base_url: str = DEFAULT_LOGIN_BASE_URL,
    ):
        self._creds = credentials
        self._base_url = base_url.rstrip("/")
        self._login_base_url = login_base_url
        self._token: str | None = None

    @property
    def invoke_url(self) -> str:
        return f"{self._base_url}/adminapi/beta/{self._creds.tenant_id}/InvokeCommand"

    async def _access_token(self) -> str:
        if self._token is None:
            client = _get_http_client()
            try:
                self._token = await fetch_access_token(
                    self._creds, self._login_base_url, client
                )
            except CredentialError as exc:
                raise ExoError(401, str(exc)) from exc
            except TokenError as exc:
                # A rejected certificate is a credential problem, not a bad
                # argument: report it as unauthorized unless the network failed.
                status = 401 if exc.status_code and exc.status_code != 0 else 0
                raise ExoError(status, exc.message) from exc
        return self._token

    def _anchor_mailbox(self) -> str | None:
        if self._creds.anchor_mailbox:
            return self._creds.anchor_mailbox
        # A GUID tenant id gives us no domain to build the system mailbox from.
        if "." in self._creds.tenant_id:
            return _SYSTEM_MAILBOX_ANCHOR % self._creds.tenant_id
        return None

    def _headers(self, token: str, max_page_size: int | None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-ResponseFormat": "json",
            "client-request-id": str(uuid.uuid4()),
        }
        anchor = self._anchor_mailbox()
        if anchor:
            headers["X-AnchorMailbox"] = anchor
        if max_page_size:
            headers["Prefer"] = f"odata.maxpagesize={max_page_size}"
        return headers

    async def invoke(
        self,
        cmdlet: str,
        parameters: dict[str, Any] | None = None,
        max_page_size: int | None = None,
    ) -> list[dict]:
        """Run one cmdlet and return its result records (empty list for none)."""
        body = {
            "CmdletInput": {
                "CmdletName": cmdlet,
                "Parameters": {k: v for k, v in (parameters or {}).items() if v is not None},
            }
        }
        payload = await self._post(body, max_page_size)
        if payload is None:
            return []
        value = payload.get("Value", payload.get("value"))
        if value is None:
            # Some cmdlets (Set-*/Remove-*) answer with an empty or scalar body.
            return []
        if isinstance(value, dict):
            return [value]
        return [record for record in value if isinstance(record, dict)]

    async def _post(self, body: dict, max_page_size: int | None) -> dict | None:
        client = _get_http_client()
        token = await self._access_token()
        url = self.invoke_url

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await client.post(
                    url, headers=self._headers(token, max_page_size), json=body
                )
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(min(2**attempt, _MAX_BACKOFF_SECONDS))
                    continue
                raise ExoError(0, f"{type(exc).__name__} calling the admin endpoint") from exc

            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                await asyncio.sleep(self._retry_delay(resp, attempt))
                continue

            self._raise_for_status(resp)
            return self._parse_body(resp)

        # Unreachable in practice; keeps the contract explicit for future edits.
        raise ExoError(0, f"request failed with no response ({last_exc})")

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), _MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
        return min(2**attempt, _MAX_BACKOFF_SECONDS)

    def _parse_body(self, resp: httpx.Response) -> dict | None:
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else {"Value": payload}

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        raise ExoError(resp.status_code, _error_message(resp))


def _error_message(resp: httpx.Response) -> str:
    """Readable message from the admin endpoint's OData error shape.

    Never returns the whole response body — cmdlet errors can be long and may
    quote tenant data.
    """
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:500] or f"HTTP {resp.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return f"HTTP {resp.status_code}"
    parts = [error.get("message") or ""]
    for detail in error.get("details") or []:
        if isinstance(detail, dict) and detail.get("message"):
            parts.append(detail["message"])
    inner = error.get("innererror")
    if isinstance(inner, dict) and inner.get("message"):
        parts.append(inner["message"])
    # Deduplicate: the generic "Error executing cmdlet" repeats across levels.
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return "; ".join(seen)[:500] or f"HTTP {resp.status_code}"
