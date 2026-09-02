"""Transport-level behaviour against a mocked Entra + admin endpoint."""

import jwt
import pytest
from conftest import CLIENT_ID, TENANT

from exo_mcp.api_client import ExoClient, ExoError
from exo_mcp.auth import SCOPE, ExoCredentials, certificate_thumbprint
from exo_mcp.tools._common import parse_size_bytes, project


def _client(credentials: ExoCredentials) -> ExoClient:
    return ExoClient(credentials, "https://outlook.office365.com", "https://login.example.test")


@pytest.mark.asyncio
async def test_client_assertion_is_signed_by_the_certificate(
    upstream, credentials, signing_material
):
    _key, cert = signing_material
    upstream.responses["Get-Mailbox"] = [{"Identity": "alice"}]

    await _client(credentials).invoke("Get-Mailbox", {"Identity": "alice"})

    assert len(upstream.token_requests) == 1
    form = upstream.token_requests[0]["form"]
    endpoint = f"https://login.example.test/{TENANT}/oauth2/v2.0/token"
    assert upstream.token_requests[0]["url"] == endpoint
    assert form["grant_type"] == "client_credentials"
    assert form["scope"] == SCOPE
    assert form["client_id"] == CLIENT_ID
    assert form["client_assertion_type"] == (
        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    )

    assertion = form["client_assertion"]
    header = jwt.get_unverified_header(assertion)
    assert header["alg"] == "RS256"
    # Entra looks the certificate up by x5t (SHA-1); x5t#S256 is not accepted.
    assert header["x5t"] == certificate_thumbprint(cert)
    assert "x5t#S256" not in header

    claims = jwt.decode(
        assertion, cert.public_key(), algorithms=["RS256"], audience=endpoint
    )
    assert claims["iss"] == CLIENT_ID
    assert claims["sub"] == CLIENT_ID
    assert claims["jti"]
    assert 0 < claims["exp"] - claims["iat"] <= 600


@pytest.mark.asyncio
async def test_invoke_posts_cmdlet_input_and_admin_headers(upstream, credentials):
    upstream.responses["Get-MobileDevice"] = [{"Guid": "g1"}]

    records = await _client(credentials).invoke(
        "Get-MobileDevice", {"Mailbox": "alice@contoso.com", "Ignored": None}, max_page_size=25
    )

    assert records == [{"Guid": "g1"}]
    sent = upstream.invoke_requests[0]
    assert sent["url"] == (
        f"https://outlook.office365.com/adminapi/beta/{TENANT}/InvokeCommand"
    )
    assert sent["cmdlet"] == "Get-MobileDevice"
    # None-valued parameters are dropped rather than sent as null.
    assert sent["parameters"] == {"Mailbox": "alice@contoso.com"}
    assert sent["headers"]["authorization"] == "Bearer fake-access-token"
    assert sent["headers"]["x-responseformat"] == "json"
    assert sent["headers"]["prefer"] == "odata.maxpagesize=25"
    # A domain-form tenant lets us anchor app-only calls on the system mailbox.
    assert sent["headers"]["x-anchormailbox"] == (
        f"UPN:SystemMailbox{{bb558c35-97f1-4cb9-8ff7-d53741dc928c}}@{TENANT}"
    )
    assert sent["headers"]["client-request-id"]


@pytest.mark.asyncio
async def test_guid_tenant_omits_anchor_and_explicit_anchor_wins(upstream, credentials):
    upstream.responses["Get-Mailbox"] = [{"Identity": "alice"}]
    guid_creds = ExoCredentials(
        tenant_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        client_id=CLIENT_ID,
        certificate_b64=credentials.certificate_b64,
    )
    await _client(guid_creds).invoke("Get-Mailbox", {"Identity": "alice"})
    assert "x-anchormailbox" not in upstream.invoke_requests[0]["headers"]

    anchored = ExoCredentials(
        tenant_id=guid_creds.tenant_id,
        client_id=CLIENT_ID,
        certificate_b64=credentials.certificate_b64,
        anchor_mailbox="UPN:admin@contoso.com",
    )
    await _client(anchored).invoke("Get-Mailbox", {"Identity": "alice"})
    assert upstream.invoke_requests[1]["headers"]["x-anchormailbox"] == "UPN:admin@contoso.com"


@pytest.mark.asyncio
async def test_one_token_exchange_per_client_instance(upstream, credentials):
    upstream.responses["Get-Mailbox"] = [{"Identity": "alice"}]
    client = _client(credentials)
    await client.invoke("Get-Mailbox", {"Identity": "alice"})
    await client.invoke("Get-Mailbox", {"Identity": "alice"})
    # Per-request client instance: two cmdlets, one certificate exchange, and
    # nothing cached beyond the instance.
    assert len(upstream.token_requests) == 1
    assert len(upstream.invoke_requests) == 2


@pytest.mark.asyncio
async def test_pkcs12_certificate_with_password(upstream, pfx_certificate_b64):
    upstream.responses["Get-Mailbox"] = [{"Identity": "alice"}]
    creds = ExoCredentials(
        tenant_id=TENANT,
        client_id=CLIENT_ID,
        certificate_b64=pfx_certificate_b64,
        certificate_password="pfx-password",
    )
    await _client(creds).invoke("Get-Mailbox", {"Identity": "alice"})
    assert len(upstream.invoke_requests) == 1


@pytest.mark.asyncio
async def test_wrong_pkcs12_password_is_unauthorized(upstream, pfx_certificate_b64):
    creds = ExoCredentials(
        tenant_id=TENANT,
        client_id=CLIENT_ID,
        certificate_b64=pfx_certificate_b64,
        certificate_password="wrong",
    )
    with pytest.raises(ExoError) as excinfo:
        await _client(creds).invoke("Get-Mailbox", {"Identity": "alice"})
    assert excinfo.value.status_code == 401
    assert "Password" in excinfo.value.message


@pytest.mark.asyncio
async def test_unparseable_certificate_is_unauthorized(upstream):
    creds = ExoCredentials(
        tenant_id=TENANT, client_id=CLIENT_ID, certificate_b64="not base64 at all!!"
    )
    with pytest.raises(ExoError) as excinfo:
        await _client(creds).invoke("Get-Mailbox", {"Identity": "alice"})
    assert excinfo.value.status_code == 401
    assert "base64" in excinfo.value.message
    assert not upstream.token_requests


@pytest.mark.asyncio
async def test_entra_rejection_maps_to_unauthorized(upstream, credentials):
    upstream.token_status = 400
    with pytest.raises(ExoError) as excinfo:
        await _client(credentials).invoke("Get-Mailbox", {"Identity": "alice"})
    assert excinfo.value.status_code == 401
    assert "AADSTS700027" in excinfo.value.message
    # The certificate itself never appears in an error message.
    assert "BEGIN" not in excinfo.value.message


@pytest.mark.asyncio
async def test_cmdlet_error_details_are_flattened(upstream, credentials):
    upstream.invoke_status = 400
    upstream.invoke_error = {
        "error": {
            "message": "Error executing cmdlet",
            "details": [
                {
                    "code": "",
                    "message": "|Microsoft.Exchange.Configuration.Tasks."
                    "ManagementObjectNotFoundException|The operation couldn't be performed "
                    "because object 'ghost' couldn't be found.",
                }
            ],
            "innererror": {"message": "Error executing cmdlet"},
        }
    }
    with pytest.raises(ExoError) as excinfo:
        await _client(credentials).invoke("Get-Mailbox", {"Identity": "ghost"})
    message = excinfo.value.message
    assert "ManagementObjectNotFoundException" in message
    # Repeated boilerplate is collapsed, and a missing object maps to not_found.
    assert message.count("Error executing cmdlet") == 1
    assert '"code":"not_found"' in excinfo.value.to_envelope()


@pytest.mark.asyncio
async def test_retries_then_succeeds_on_503(upstream, credentials, monkeypatch):
    import exo_mcp.api_client as module

    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)

    calls = {"n": 0}
    original = upstream.handler

    def handler(request):
        if str(request.url).endswith("/InvokeCommand"):
            calls["n"] += 1
            if calls["n"] == 1:
                import httpx

                return httpx.Response(503, json={"error": {"message": "try later"}})
        return original(request)

    import httpx

    module.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    upstream.responses["Get-Mailbox"] = [{"Identity": "alice"}]

    records = await _client(credentials).invoke("Get-Mailbox", {"Identity": "alice"})
    assert records == [{"Identity": "alice"}]
    assert calls["n"] == 2
    assert slept == [1.0]


@pytest.mark.asyncio
async def test_admin_calls_ask_for_identity_encoding(upstream, credentials):
    """Compression must stay off so a status code is never hidden behind a body
    httpx cannot decode — Exchange sends NUL-filled bodies labelled gzip."""
    upstream.responses["Get-Mailbox"] = [{"Identity": "alice"}]
    await _client(credentials).invoke("Get-Mailbox", {"Identity": "alice"})
    assert upstream.invoke_requests[0]["headers"]["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_nul_filled_403_body_reports_the_status_not_the_nuls(upstream, credentials):
    """Exchange denies app-only cmdlets with a 403 whose body is all NUL bytes.

    The status has to survive, and the NULs must not reach the envelope.
    """
    import exo_mcp.api_client as module
    import httpx

    def handler(request):
        if str(request.url).endswith("/InvokeCommand"):
            return httpx.Response(403, content=b"\x00" * 985)
        return upstream.handler(request)

    module.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    with pytest.raises(ExoError) as excinfo:
        await _client(credentials).invoke("Get-Mailbox", {"Identity": "alice"})

    assert excinfo.value.status_code == 403
    assert "\x00" not in excinfo.value.message
    # The message has to name the actual remedy: consent alone is not enough.
    assert "New-ManagementRoleAssignment" in excinfo.value.message
    envelope = excinfo.value.to_envelope()
    assert '"code":"unauthorized"' in envelope
    assert '"retryable":false' in envelope
    assert "\\u0000" not in envelope


@pytest.mark.asyncio
async def test_undecodable_body_is_not_retried(upstream, credentials, monkeypatch):
    """A DecodingError subclasses RequestError, but the response did arrive —
    retrying it three times only delays a verdict that will not change."""
    import exo_mcp.api_client as module
    import httpx

    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)

    calls = {"n": 0}

    def handler(request):
        if str(request.url).endswith("/InvokeCommand"):
            calls["n"] += 1
            # Declared gzip, not actually gzip — httpx raises DecodingError.
            return httpx.Response(
                403, headers={"Content-Encoding": "gzip"}, content=b"\x00" * 336
            )
        return upstream.handler(request)

    module.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    with pytest.raises(ExoError) as excinfo:
        await _client(credentials).invoke("Get-Mailbox", {"Identity": "alice"})

    assert calls["n"] == 1
    assert slept == []
    assert "could not be decoded" in excinfo.value.message
    assert '"retryable":false' in excinfo.value.to_envelope()


@pytest.mark.asyncio
async def test_unparseable_success_body_is_not_reported_as_no_records(upstream, credentials):
    """An unreadable 200 body must not be indistinguishable from an empty result:
    "this mailbox has no devices" is an offboarding decision."""
    import exo_mcp.api_client as module
    import httpx

    def handler(request):
        if str(request.url).endswith("/InvokeCommand"):
            return httpx.Response(200, content=b"\x00" * 64)
        return upstream.handler(request)

    module.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    with pytest.raises(ExoError) as excinfo:
        await _client(credentials).invoke("Get-MobileDevice", {"Mailbox": "alice@contoso.com"})
    assert "non-JSON" in excinfo.value.message


def test_project_drops_odata_companions_and_absent_fields():
    record = {
        "Identity": "alice",
        "Identity@odata.type": "#String",
        "DisplayName": "Alice",
        "Noise": "unused",
        "ItemCount": {"Value": 42},
        "ForwardingSmtpAddress": None,
    }
    assert project(
        record,
        {
            "Identity": "identity",
            "DisplayName": "display_name",
            "ItemCount": "item_count",
            "ForwardingSmtpAddress": "forwarding_smtp_address",
        },
    ) == {"identity": "alice", "display_name": "Alice", "item_count": 42}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.66 GB (1,782,579,200 bytes)", 1782579200),
        ("48.5 MB (50,855,936 bytes)", 50855936),
        ("60 GB", 64424509440),
        ({"Value": "1.66 GB (1,782,579,200 bytes)"}, 1782579200),
        (1024, 1024),
        ("", None),
        (None, None),
    ],
)
def test_parse_size_bytes(raw, expected):
    assert parse_size_bytes(raw) == expected
