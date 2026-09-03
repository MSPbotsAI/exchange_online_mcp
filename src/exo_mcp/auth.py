"""App-only authentication against Entra ID.

Two credential forms are accepted, and either one on its own is enough:

- a certificate, which signs an RS256 client assertion (`x5t` header), or
- a client secret, sent directly in the client_credentials request.

The Exchange Online PowerShell module offers only the certificate form, which is
why app-only access to Exchange is widely described as certificate-only; the
admin REST endpoint this service calls documents both. The certificate form
stays supported because the secret form is not yet proven against the
undocumented `adminapi/beta` InvokeCommand path this server uses.

Everything is per-request: the credential arrives in headers, mints a
short-lived token, and that token is discarded with the request. No credential
or derived token is cached across requests, and none of it is ever logged.
"""

import base64
import binascii
import re
import time
import uuid
from dataclasses import dataclass

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12

# Exchange Online admin endpoint audience. app-only tokens for the admin API are
# always ".default" — Exchange has no granular delegated-style scopes here.
SCOPE = "https://outlook.office365.com/.default"

_ASSERTION_TTL_SECONDS = 300
_TOKEN_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)

_PEM_BLOCK = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----.*?-----END \1-----", re.DOTALL)


class CredentialError(Exception):
    """The supplied certificate material could not be loaded/used.

    Messages must stay generic — never echo the certificate or password.
    """


class TokenError(Exception):
    """Entra ID refused to issue an access token."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"token request failed ({status_code}): {message}")


@dataclass(frozen=True)
class ExoCredentials:
    """One tenant's app-only credentials, valid for the current request only.

    The proof of identity is either `certificate_b64` or `client_secret`;
    server.py refuses the request before it reaches here when neither header was
    sent. A certificate takes precedence if both happen to arrive.
    """

    tenant_id: str
    client_id: str
    certificate_b64: str | None = None
    certificate_password: str | None = None
    anchor_mailbox: str | None = None
    client_secret: str | None = None


def _decode_blob(certificate_b64: str) -> bytes:
    # Headers routinely arrive with wrapped/padded whitespace from copy-paste.
    compact = re.sub(r"\s+", "", certificate_b64)
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CredentialError(
            "X-Exo-Certificate is not valid base64 (expected base64 of a PEM bundle or a .pfx)"
        ) from exc


def load_signing_material(creds: ExoCredentials):
    """Load (private_key, certificate) from a base64 PEM bundle or PKCS#12 blob."""
    blob = _decode_blob(creds.certificate_b64)
    password = creds.certificate_password.encode() if creds.certificate_password else None

    if b"-----BEGIN" in blob:
        key_pem = cert_pem = None
        for match in _PEM_BLOCK.finditer(blob):
            label = match.group(1).decode()
            if "PRIVATE KEY" in label and key_pem is None:
                key_pem = match.group(0)
            elif label == "CERTIFICATE" and cert_pem is None:
                cert_pem = match.group(0)
        if key_pem is None or cert_pem is None:
            raise CredentialError(
                "PEM bundle must contain both a PRIVATE KEY block and a CERTIFICATE block"
            )
        try:
            key = serialization.load_pem_private_key(key_pem, password=password)
            cert = x509.load_pem_x509_certificate(cert_pem)
        except (ValueError, TypeError) as exc:
            raise CredentialError(
                "could not load the PEM certificate/key "
                "(wrong X-Exo-Certificate-Password, or unsupported key type)"
            ) from exc
    else:
        try:
            key, cert, _chain = pkcs12.load_key_and_certificates(blob, password)
        except (ValueError, TypeError) as exc:
            raise CredentialError(
                "could not load the PKCS#12 (.pfx) certificate "
                "(wrong X-Exo-Certificate-Password, or not a PKCS#12 blob)"
            ) from exc
        if key is None or cert is None:
            raise CredentialError("PKCS#12 blob has no private key and certificate pair")

    return key, cert


def certificate_thumbprint(cert: x509.Certificate) -> str:
    """`x5t` header value: base64 of the certificate's SHA-1 DER fingerprint.

    Entra ID identifies the uploaded certificate by `x5t` (SHA-1) or `kid`; it
    does NOT accept `x5t#S256`.
    """
    return base64.urlsafe_b64encode(cert.fingerprint(hashes.SHA1())).decode()


def token_endpoint(login_base_url: str, tenant_id: str) -> str:
    return f"{login_base_url.rstrip('/')}/{tenant_id}/oauth2/v2.0/token"


def build_client_assertion(creds: ExoCredentials, audience: str) -> str:
    """Sign the RS256 client assertion Entra exchanges for an access token."""
    key, cert = load_signing_material(creds)
    now = int(time.time())
    claims = {
        "aud": audience,
        "iss": creds.client_id,
        "sub": creds.client_id,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + _ASSERTION_TTL_SECONDS,
    }
    try:
        return jwt.encode(
            claims, key, algorithm="RS256", headers={"x5t": certificate_thumbprint(cert)}
        )
    except Exception as exc:  # unsupported key type (e.g. an EC key)
        raise CredentialError(
            "could not sign the client assertion; Entra requires an RSA certificate (RS256)"
        ) from exc


def _grant_payload(creds: ExoCredentials, endpoint: str) -> dict[str, str]:
    """The client_credentials form fields for whichever credential was supplied."""
    payload = {
        "grant_type": "client_credentials",
        "client_id": creds.client_id,
        "scope": SCOPE,
    }
    if creds.certificate_b64:
        payload["client_assertion_type"] = (
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        )
        payload["client_assertion"] = build_client_assertion(creds, endpoint)
    elif creds.client_secret:
        payload["client_secret"] = creds.client_secret
    else:
        raise CredentialError(
            "no app-only credential supplied: send either X-Exo-Certificate or "
            "X-Exo-Client-Secret"
        )
    return payload


async def fetch_access_token(
    creds: ExoCredentials, login_base_url: str, http_client: httpx.AsyncClient
) -> str:
    """Exchange the credential for an Exchange Online admin access token."""
    endpoint = token_endpoint(login_base_url, creds.tenant_id)
    try:
        resp = await http_client.post(
            endpoint,
            data=_grant_payload(creds, endpoint),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_TOKEN_TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise TokenError(0, f"could not reach the Entra token endpoint: {type(exc).__name__}")

    if resp.status_code >= 400:
        raise TokenError(resp.status_code, _entra_error_message(resp))

    token = resp.json().get("access_token")
    if not token:
        raise TokenError(resp.status_code, "Entra token response contained no access_token")
    return token


def _entra_error_message(resp: httpx.Response) -> str:
    """Entra error text, trimmed. AADSTS codes are diagnostic, not sensitive."""
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:300]
    code = payload.get("error") or "invalid_request"
    description = (payload.get("error_description") or "").split("\r\n")[0]
    return f"{code}: {description[:300]}" if description else code
