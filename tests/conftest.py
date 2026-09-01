"""Shared fixtures: a throwaway signing certificate and a mocked upstream.

Nothing here talks to Entra ID or Exchange Online — the certificate is
generated in-process and both endpoints are served by httpx.MockTransport.
"""

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from exo_mcp import api_client
from exo_mcp.auth import ExoCredentials

TENANT = "contoso.onmicrosoft.com"
CLIENT_ID = "11111111-2222-3333-4444-555555555555"
ACCESS_TOKEN = "fake-access-token"


@dataclass
class Upstream:
    """Records what the client sent and replies with canned cmdlet output."""

    responses: dict[str, object] = field(default_factory=dict)
    token_status: int = 200
    token_body: dict | None = None
    invoke_status: int = 200
    invoke_error: dict | None = None
    # cmdlet -> (status, body): fail one cmdlet while the others still answer
    cmdlet_errors: dict[str, tuple[int, dict]] = field(default_factory=dict)
    token_requests: list[dict] = field(default_factory=list)
    invoke_requests: list[dict] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/oauth2/v2.0/token"):
            from urllib.parse import parse_qs

            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append({"url": url, "form": form})
            if self.token_status >= 400:
                return httpx.Response(
                    self.token_status,
                    json=self.token_body
                    or {
                        "error": "invalid_client",
                        "error_description": "AADSTS700027: Client assertion failed signature "
                        "validation.\r\nTrace ID: x",
                    },
                )
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3599})

        if url.endswith("/InvokeCommand"):
            body = json.loads(request.content.decode())
            cmdlet = body["CmdletInput"]["CmdletName"]
            self.invoke_requests.append(
                {
                    "url": url,
                    "cmdlet": cmdlet,
                    "parameters": body["CmdletInput"]["Parameters"],
                    "headers": dict(request.headers),
                }
            )
            if cmdlet in self.cmdlet_errors:
                status, body = self.cmdlet_errors[cmdlet]
                return httpx.Response(status, json=body)
            if self.invoke_status >= 400:
                return httpx.Response(
                    self.invoke_status,
                    json=self.invoke_error
                    or {"error": {"message": "Error executing cmdlet", "details": []}},
                )
            value = self.responses.get(cmdlet, [])
            return httpx.Response(200, json={"Value": value})

        return httpx.Response(404, json={"error": {"message": f"unexpected url {url}"}})


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture(autouse=True)
def mocked_http(upstream):
    """Route every outbound call through the mock and reset the shared pool."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    api_client.set_http_client(client)
    yield client
    api_client.set_http_client(None)


@pytest.fixture(scope="session")
def signing_material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "exo-mcp-test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return key, cert


@pytest.fixture
def pem_certificate_b64(signing_material) -> str:
    key, cert = signing_material
    bundle = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ) + cert.public_bytes(serialization.Encoding.PEM)
    return base64.b64encode(bundle).decode()


@pytest.fixture
def pfx_certificate_b64(signing_material) -> str:
    key, cert = signing_material
    blob = pkcs12.serialize_key_and_certificates(
        name=b"exo-mcp-test",
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(b"pfx-password"),
    )
    return base64.b64encode(blob).decode()


@pytest.fixture
def credentials(pem_certificate_b64) -> ExoCredentials:
    return ExoCredentials(
        tenant_id=TENANT, client_id=CLIENT_ID, certificate_b64=pem_certificate_b64
    )


def tool_json(result):
    """FastMCP returns either content blocks or (content, structured)."""
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)
