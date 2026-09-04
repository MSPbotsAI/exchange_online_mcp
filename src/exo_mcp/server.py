import contextvars

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import ExoClient
from .auth import ExoCredentials
from .config import Settings

# Header names are part of the integration contract — they must match README.md
# character for character.
HEADER_TENANT_ID = "X-Exo-Tenant-Id"
HEADER_CLIENT_ID = "X-Exo-Client-Id"
HEADER_CERTIFICATE = "X-Exo-Certificate"
HEADER_CERTIFICATE_PASSWORD = "X-Exo-Certificate-Password"
HEADER_CLIENT_SECRET = "X-Exo-Client-Secret"
HEADER_ANCHOR_MAILBOX = "X-Exo-Anchor-Mailbox"

REQUIRED_HEADERS = (HEADER_TENANT_ID, HEADER_CLIENT_ID)
# The app-only credential itself: a certificate or a client secret, either one
# alone. Both are optional headers on their own, so their absence is checked
# together rather than through REQUIRED_HEADERS.
CREDENTIAL_HEADERS = (HEADER_CERTIFICATE, HEADER_CLIENT_SECRET)

# Per-request credential isolation via contextvars. GatewayCredentialMiddleware
# sets this before the MCP handler runs and resets it in finally; asyncio copies
# the context per task, so concurrent tenant requests never see each other's
# credentials. Never store credentials in a module-level variable.
_credentials_var: contextvars.ContextVar[ExoCredentials | None] = contextvars.ContextVar(
    "exo_credentials", default=None
)


def get_client_from_context(settings: Settings) -> ExoClient | None:
    """Resolve the active ExoClient for the current request context."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return ExoClient(credentials, settings.exo_base_url, settings.entra_login_base_url)


class GatewayCredentialMiddleware:
    """ASGI middleware.

    Reads the per-tenant app-only credentials from request headers into the
    contextvar. Returns 401 on /mcp requests that are missing any required
    header, or that carry neither of the two credential headers. There is
    deliberately no environment-variable fallback: one tenant silently using
    another's credential would be a cross-tenant data leak.
    """

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        headers = Request(scope).headers
        missing = [name for name in REQUIRED_HEADERS if not headers.get(name.lower())]
        if not any(headers.get(name.lower()) for name in CREDENTIAL_HEADERS):
            missing.append(" or ".join(CREDENTIAL_HEADERS))
        if missing:
            response = JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": (
                        "This server requires the tenant's Exchange Online app-only "
                        "credentials in request headers: the tenant and application "
                        "ids, plus either a certificate or a client secret"
                    ),
                    "missing_headers": missing,
                    "required_headers": [
                        *REQUIRED_HEADERS,
                        " or ".join(CREDENTIAL_HEADERS),
                    ],
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        credentials = ExoCredentials(
            tenant_id=headers[HEADER_TENANT_ID.lower()],
            client_id=headers[HEADER_CLIENT_ID.lower()],
            certificate_b64=headers.get(HEADER_CERTIFICATE.lower()) or None,
            certificate_password=headers.get(HEADER_CERTIFICATE_PASSWORD.lower()) or None,
            anchor_mailbox=headers.get(HEADER_ANCHOR_MAILBOX.lower()) or None,
            client_secret=headers.get(HEADER_CLIENT_SECRET.lower()) or None,
        )

        ctx_token = _credentials_var.set(credentials)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(ctx_token)


def create_mcp_server(settings: Settings) -> FastMCP:
    """Build the FastMCP server instance and register all Exchange Online tools."""
    mcp = FastMCP(
        name="exchange-online-mcp",
        instructions=(
            "Exchange Online is the Microsoft 365 email service. This server runs the "
            "Exchange admin operations Microsoft Graph cannot express: mailbox objects "
            "(a mailbox is a separate object from the Entra user who owns it), "
            "distribution group membership, and Exchange ActiveSync mobile device "
            "associations. Use it alongside a Microsoft Graph server, not instead of "
            "one — user accounts, licences, Microsoft 365 groups and security groups "
            "belong to Graph. Typical user offboarding: exo_get_mailbox (size, archive "
            "and holds; shared_conversion.license_required says whether the mailbox can "
            "go licence-free) -> exo_convert_mailbox_to_shared -> "
            "exo_set_mailbox_hidden(hidden=true) -> exo_remove_distribution_group_member "
            "for each mail-enabled group -> exo_list_mobile_devices -> "
            "exo_remove_mobile_device per device. Removing a mobile device removes only "
            "the mailbox association; data already on the handset is not wiped. Every "
            "tool acts on the one tenant whose app-only certificate is supplied with the "
            "request; there is no cross-tenant access."
        ),
        # A reverse proxy / docker network sends a non-localhost Host header, which
        # the SDK's DNS-rebinding protection rejects with 421. This service is never
        # exposed publicly, so disabling it is safe.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
        json_response=True,
    )

    def client_factory() -> ExoClient | None:
        return get_client_from_context(settings)

    from .tools import devices, groups, mailbox

    mailbox.register(mcp, client_factory)
    groups.register(mcp, client_factory)
    devices.register(mcp, client_factory)

    return mcp
