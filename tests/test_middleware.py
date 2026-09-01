"""Credential middleware: 401 without headers, request-scoped isolation with them."""

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from exo_mcp.config import Settings
from exo_mcp.server import (
    REQUIRED_HEADERS,
    GatewayCredentialMiddleware,
    _credentials_var,
    get_client_from_context,
)

HEADERS = {
    "X-Exo-Tenant-Id": "contoso.onmicrosoft.com",
    "X-Exo-Client-Id": "11111111-2222-3333-4444-555555555555",
    "X-Exo-Certificate": "Zm9v",
}


def _app(settings: Settings | None = None):
    settings = settings or Settings()

    async def probe(_request):
        client = get_client_from_context(settings)
        if client is None:
            return JSONResponse({"client": None})
        creds = client._creds
        return JSONResponse(
            {
                "tenant_id": creds.tenant_id,
                "client_id": creds.client_id,
                "certificate_b64": creds.certificate_b64,
                "certificate_password": creds.certificate_password,
                "anchor_mailbox": creds.anchor_mailbox,
                "invoke_url": client.invoke_url,
            }
        )

    inner = Starlette(
        routes=[
            Route("/mcp", probe, methods=["GET", "POST"]),
            Route("/health", probe),
        ]
    )
    return GatewayCredentialMiddleware(inner, settings)


def test_missing_all_headers_returns_401_listing_them():
    resp = TestClient(_app()).post("/mcp")
    assert resp.status_code == 401
    body = resp.json()
    assert body["required_headers"] == list(REQUIRED_HEADERS)
    assert body["missing_headers"] == list(REQUIRED_HEADERS)


@pytest.mark.parametrize("omitted", list(REQUIRED_HEADERS))
def test_each_required_header_is_enforced(omitted):
    headers = {k: v for k, v in HEADERS.items() if k != omitted}
    resp = TestClient(_app()).post("/mcp", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["missing_headers"] == [omitted]


def test_credentials_reach_the_request_context():
    resp = TestClient(_app()).post(
        "/mcp",
        headers={
            **HEADERS,
            "X-Exo-Certificate-Password": "pw",
            "X-Exo-Anchor-Mailbox": "UPN:admin@contoso.com",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == HEADERS["X-Exo-Tenant-Id"]
    assert body["client_id"] == HEADERS["X-Exo-Client-Id"]
    assert body["certificate_b64"] == HEADERS["X-Exo-Certificate"]
    assert body["certificate_password"] == "pw"
    assert body["anchor_mailbox"] == "UPN:admin@contoso.com"
    assert body["invoke_url"] == (
        "https://outlook.office365.com/adminapi/beta/contoso.onmicrosoft.com/InvokeCommand"
    )


def test_non_mcp_paths_are_not_gated():
    resp = TestClient(_app()).get("/health")
    assert resp.status_code == 200
    assert resp.json()["client"] is None


@pytest.mark.asyncio
async def test_context_is_set_then_reset_in_the_same_task():
    """The finally-reset is what stops one tenant's certificate leaking into the
    next request handled by the same worker task."""
    settings = Settings()
    seen: dict[str, str] = {}

    async def inner(scope, receive, send):
        client = get_client_from_context(settings)
        seen["tenant_id"] = client._creds.tenant_id
        await JSONResponse({"ok": True})(scope, receive, send)

    app = GatewayCredentialMiddleware(inner, settings)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in HEADERS.items()],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    assert _credentials_var.get() is None
    await app(scope, receive, send)

    assert seen["tenant_id"] == HEADERS["X-Exo-Tenant-Id"]
    assert sent[0]["status"] == 200
    assert _credentials_var.get() is None
